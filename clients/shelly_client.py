"""Shelly-Clients für Plug (Gen1 /relay/0), 3EM (/status) und Plus-Add-On (/rpc)."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ShellyError(Exception):
    pass


def _float_or_none(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class ShellyPlugClient:
    """Plug/Relay für PH1/PH2/PH3.

    Unterstützt Gen1 (Plug S, 1, 1PM … — /relay/<channel>) und Gen2/Gen3
    (Plus, Pro … — /rpc/Switch.* mit Kanal-ID). Ein Gen2-Gerät mit mehreren
    Kanälen (z. B. Pro 2PM) kann so für zwei Phasen gleichzeitig verwendet
    werden (gleiche base_url, unterschiedlicher channel).
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 5,
        name: str = "shelly",
        type_: str = "shelly_gen1",
        channel: int = 0,
        read_timeout: Optional[float] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Lesen darf kürzer warten als Schalten (ein toter Shelly bremst sonst jeden Lauf)
        self.read_timeout = read_timeout if read_timeout is not None else timeout
        self.name = name
        self.type_ = type_
        self.channel = channel

    def get_relay_state(self) -> Optional[bool]:
        return self.get_status()[0]

    def get_status(self) -> tuple[Optional[bool], Optional[float]]:
        """(Relais an?, gemessene Leistung in W) in einem Abruf; (None, None) wenn nicht lesbar.

        Gen2/Gen3: Switch.GetStatus liefert output + apower. Gen1: /status mit relays[]
        und meters[] (Plug S, 1PM); ohne Messung (Shelly 1) ist die Leistung None.
        """
        try:
            if self.type_ == "shelly_gen2":
                r = requests.get(
                    f"{self.base_url}/rpc/Switch.GetStatus",
                    params={"id": self.channel},
                    timeout=self.read_timeout,
                )
                r.raise_for_status()
                j = r.json()
                return bool(j["output"]), _float_or_none(j.get("apower"))
            r = requests.get(f"{self.base_url}/status", timeout=self.read_timeout)
            r.raise_for_status()
            j = r.json()
            on = bool(j["relays"][self.channel]["ison"])
            meters = j.get("meters") or []
            power = _float_or_none(meters[self.channel].get("power")) if len(meters) > self.channel else None
            return on, power
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
            log.debug("Shelly %s read relay state failed: %s", self.name, e)  # Dauerausfall meldet der Controller einmalig
            return None, None

    def set_relay(self, on: bool, timeout: Optional[float] = None, quiet: bool = False) -> bool:
        """Relais schalten. ``timeout`` überschreibt den Standard; ``quiet``: Fehler nur im Debug-Log
        (Aufrufer meldet selbst, z. B. wiederholte Blind-AUS-Befehle an eine unerreichbare Phase)."""
        action = "on" if on else "off"
        to = self.timeout if timeout is None else timeout
        try:
            if self.type_ == "shelly_gen2":
                r = requests.post(
                    f"{self.base_url}/rpc/Switch.Set",
                    json={"id": self.channel, "on": on},
                    timeout=to,
                )
            else:
                r = requests.get(
                    f"{self.base_url}/relay/{self.channel}",
                    params={"turn": action},
                    timeout=to,
                )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            (log.debug if quiet else log.error)("Shelly %s switch %s failed: %s", self.name, action, e)
            return False


class Shelly3EMClient:
    """Shelly 3EM (Gen1, ``shelly_3em_gen1``) oder Shelly Pro 3EM (Gen2, ``shelly_pro_3em``).

    Leistung (W, + Bezug / − Einspeisung – beide Geräte zählen so, keine Umrechnung):
      Gen1 /status total_power; Pro 3EM /rpc/EM.GetStatus?id=0 total_act_power (fehlt es: Summe
      a/b/c_act_power).
    Energie (Lifetime, Wh): Gen1 emeters[i].total / total_returned (Summe über alle Phasen);
      Pro 3EM /rpc/EMData.GetStatus?id=0 total_act / total_act_ret (ebenfalls Summe der Phasen).
    """

    _STATUS_TTL_S = 2.0  # Leistung + Energie im selben Lauf teilen sich einen Abruf

    def __init__(self, base_url: str, timeout: int = 5, name: str = "3em", type_: str = "shelly_3em_gen1"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name
        self.type_ = type_
        self._status_json: Optional[dict] = None
        self._status_ts: float = 0.0

    @property
    def is_pro(self) -> bool:
        return self.type_ == "shelly_pro_3em"

    def _rpc(self, method: str) -> dict:
        r = requests.get(f"{self.base_url}/rpc/{method}", params={"id": 0}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict):
            raise ValueError(f"{method}: unerwartete Antwort")
        return j

    def _status(self) -> dict:
        if self._status_json is not None and (time.monotonic() - self._status_ts) < self._STATUS_TTL_S:
            return self._status_json
        r = requests.get(f"{self.base_url}/status", timeout=self.timeout)
        r.raise_for_status()
        self._status_json = r.json()
        self._status_ts = time.monotonic()
        return self._status_json

    def get_device_info(self) -> dict:
        """{generation, model, mac} über /shelly (gibt es bei Gen1 und Gen2); wirft bei Fehlern."""
        r = requests.get(f"{self.base_url}/shelly", timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        gen = int(j.get("gen") or 1)
        return {"generation": gen, "model": j.get("model") or j.get("type"), "mac": j.get("mac")}

    def get_total_power_w(self) -> Optional[float]:
        if self.is_pro:
            try:
                j = self._rpc("EM.GetStatus")
                if j.get("total_act_power") is not None:
                    return float(j["total_act_power"])
                return float(sum(float(j[f"{p}_act_power"]) for p in "abc"))
            except (requests.RequestException, KeyError, ValueError, TypeError) as e:
                log.error("Shelly %s read /rpc/EM.GetStatus failed: %s", self.name, e)
                return None
        try:
            return float(self._status()["total_power"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.error("Shelly %s read /status failed: %s", self.name, e)
            return None

    def get_energy_wh(self) -> Optional[tuple[float, float]]:
        """(Bezug, Einspeisung) als Lifetime-Zähler in Wh oder None."""
        if self.is_pro:
            try:
                j = self._rpc("EMData.GetStatus")
                return float(j["total_act"]), float(j["total_act_ret"])
            except (requests.RequestException, KeyError, ValueError, TypeError) as e:
                log.error("Shelly %s read Energiezähler failed: %s", self.name, e)
                return None
        try:
            em = self._status().get("emeters")
            if isinstance(em, list) and em and all(
                isinstance(e, dict) and "total" in e and "total_returned" in e for e in em
            ):
                return (
                    float(sum(e["total"] for e in em)),
                    float(sum(e["total_returned"] for e in em)),
                )
        except (requests.RequestException, ValueError, TypeError) as e:
            log.debug("Shelly %s: Gen1-Energiezähler nicht lesbar: %s", self.name, e)
        try:
            r = requests.get(
                f"{self.base_url}/rpc/EMData.GetStatus",
                params={"id": 0},
                timeout=self.timeout,
            )
            r.raise_for_status()
            j = r.json()
            return float(j["total_act"]), float(j["total_act_ret"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.error("Shelly %s read Energiezähler failed: %s", self.name, e)
            return None


class ShellyStorageTempClient:
    """Shelly Plus mit Temperatur-Add-On — /rpc/Shelly.GetStatus."""

    def __init__(self, base_url: str, timeout: int = 5, name: str = "storage"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name

    def get_temperature_c(self) -> Optional[float]:
        try:
            r = requests.get(
                f"{self.base_url}/rpc/Shelly.GetStatus", timeout=self.timeout
            )
            r.raise_for_status()
            return float(r.json()["temperature:100"]["tC"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.error("Shelly %s read storage temp failed: %s", self.name, e)
            return None
