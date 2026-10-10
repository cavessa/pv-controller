"""Shelly-Clients für Plug (Gen1 /relay/0), 3EM (/status) und Plus-Add-On (/rpc)."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ShellyError(Exception):
    pass


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
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name
        self.type_ = type_
        self.channel = channel

    def get_relay_state(self) -> Optional[bool]:
        try:
            if self.type_ == "shelly_gen2":
                r = requests.get(
                    f"{self.base_url}/rpc/Switch.GetStatus",
                    params={"id": self.channel},
                    timeout=self.timeout,
                )
                r.raise_for_status()
                return bool(r.json()["output"])
            r = requests.get(
                f"{self.base_url}/relay/{self.channel}", timeout=self.timeout
            )
            r.raise_for_status()
            return bool(r.json()["ison"])
        except (requests.RequestException, KeyError, ValueError) as e:
            log.error("Shelly %s read relay state failed: %s", self.name, e)
            return None

    def set_relay(self, on: bool) -> bool:
        action = "on" if on else "off"
        try:
            if self.type_ == "shelly_gen2":
                r = requests.post(
                    f"{self.base_url}/rpc/Switch.Set",
                    json={"id": self.channel, "on": on},
                    timeout=self.timeout,
                )
            else:
                r = requests.get(
                    f"{self.base_url}/relay/{self.channel}",
                    params={"turn": action},
                    timeout=self.timeout,
                )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Shelly %s switch %s failed: %s", self.name, action, e)
            return False


class Shelly3EMClient:
    """Shelly 3EM / EM (Gen1) — /status liefert total_power und die Energiezähler.

    Energie (Lifetime, Wh): Gen1 emeters[i].total / total_returned (Summe über
    alle Phasen); Gen2 (Pro 3EM) /rpc/EMData.GetStatus total_act / total_act_ret.
    """

    _STATUS_TTL_S = 2.0  # Leistung + Energie im selben Lauf teilen sich einen Abruf

    def __init__(self, base_url: str, timeout: int = 5, name: str = "3em"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name
        self._status_json: Optional[dict] = None
        self._status_ts: float = 0.0

    def _status(self) -> dict:
        if self._status_json is not None and (time.monotonic() - self._status_ts) < self._STATUS_TTL_S:
            return self._status_json
        r = requests.get(f"{self.base_url}/status", timeout=self.timeout)
        r.raise_for_status()
        self._status_json = r.json()
        self._status_ts = time.monotonic()
        return self._status_json

    def get_total_power_w(self) -> Optional[float]:
        try:
            return float(self._status()["total_power"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.error("Shelly %s read /status failed: %s", self.name, e)
            return None

    def get_energy_wh(self) -> Optional[tuple[float, float]]:
        """(Bezug, Einspeisung) als Lifetime-Zähler in Wh oder None."""
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
