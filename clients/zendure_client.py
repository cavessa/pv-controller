"""Zendure SolarFlow 2400 AC – lokale zenSDK-HTTP-API.

GET  http://{ip}/properties/report   -> alle Properties (JSON)
POST http://{ip}/properties/write    -> {"sn": ..., "properties": {...}}

Lesen: `get_status`. Schreiben (`set_mode`) nur über zendure_control (Phase 2).
Mapping am echten Gerät geprüft (SolarFlow 2400 AC+, 16.10.2026): acMode 1 + inputLimit
lädt aus dem Hausnetz (gridInputPower = outputPackPower), acMode 2 + outputLimit entlädt
(packInputPower = outputHomePower), inputLimit = outputLimit = 0 stoppt; Umstellung < 5 s.
packState 0 Standby / 1 lädt / 2 entlädt (folgt ~5 s verzögert).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Property-Namen (R)
PROP_SOC = "electricLevel"
PROP_CHARGE_W = "outputPackPower"      # Leistung ins Akkupaket = Laden
PROP_DISCHARGE_W = "packInputPower"    # Leistung aus dem Akkupaket = Entladen
PROP_GRID_IN_W = "gridInputPower"
PROP_HOME_OUT_W = "outputHomePower"
PROP_PACK_STATE = "packState"          # 0 Standby / 1 lädt / 2 entlädt
PROP_TEMP = "hyperTmp"

# acMode (W) – am Gerät geprüft (16.10.2026)
AC_MODE_CHARGE = 1
AC_MODE_DISCHARGE = 2

_BACKOFF_S = 20.0
_fail_until: dict[str, float] = {}
_lock = threading.Lock()


@dataclass
class ZendureStatus:
    soc: Optional[float] = None
    charge_w: float = 0.0
    discharge_w: float = 0.0
    grid_in_w: float = 0.0
    home_out_w: float = 0.0
    pack_state: Optional[int] = None
    temp_c: Optional[float] = None
    sn: Optional[str] = None
    product: Optional[str] = None
    version: Optional[str] = None
    props: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        """charging | discharging | standby (aus den Leistungswerten abgeleitet)."""
        if self.charge_w > 10:
            return "charging"
        if self.discharge_w > 10:
            return "discharging"
        return "standby"


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ZendureClient:
    def __init__(self, ip: str, sn: str = "", timeout: float = 3.0):
        ip = ip.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
        self.base_url = f"http://{ip}"
        self.ip = ip
        self.sn = sn
        self.timeout = timeout

    # ---- Lesen ----------------------------------------------------------

    def _report(self) -> Optional[dict]:
        r = requests.get(f"{self.base_url}/properties/report", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_status(self) -> Optional[ZendureStatus]:
        """Aktueller Zustand oder None (nicht erreichbar). Kein Fail-safe-Auslöser."""
        with _lock:
            if time.monotonic() < _fail_until.get(self.ip, 0.0):
                return None
        try:
            j = self._report()
        except (requests.RequestException, ValueError) as e:
            log.warning("Zendure %s nicht erreichbar: %s", self.ip, e)
            with _lock:
                _fail_until[self.ip] = time.monotonic() + _BACKOFF_S
            return None
        props = j.get("properties") if isinstance(j.get("properties"), dict) else j
        if not isinstance(props, dict) or PROP_SOC not in props:
            log.warning("Zendure %s: unerwartete Antwort (keys=%s)", self.ip, list(j)[:8])
            return None
        if not self.sn and isinstance(j.get("sn"), str):
            self.sn = j["sn"]
        temp = _num(props.get(PROP_TEMP))
        if temp is not None and temp > 1000:      # Zendure liefert Kelvin × 10 (2731 = 0 °C)
            temp = round((temp - 2731) / 10.0, 1)
        return ZendureStatus(
            soc=_num(props.get(PROP_SOC)),
            charge_w=_num(props.get(PROP_CHARGE_W)) or 0.0,
            discharge_w=_num(props.get(PROP_DISCHARGE_W)) or 0.0,
            grid_in_w=_num(props.get(PROP_GRID_IN_W)) or 0.0,
            home_out_w=_num(props.get(PROP_HOME_OUT_W)) or 0.0,
            pack_state=int(props[PROP_PACK_STATE]) if _num(props.get(PROP_PACK_STATE)) is not None else None,
            temp_c=temp,
            sn=j.get("sn") if isinstance(j.get("sn"), str) else self.sn or None,
            product=j.get("product") if isinstance(j.get("product"), str) else None,
            version=str(j["version"]) if j.get("version") is not None else None,
            props=props,
        )

    # ---- Schreiben (Phase 2) --------------------------------------------

    def set_mode(
        self,
        ac_mode: Optional[int],
        input_limit: Optional[int] = None,
        output_limit: Optional[int] = None,
    ) -> bool:
        """acMode (None = unverändert) + Limits setzen. smartMode=1 immer mitsenden
        (kein Flash-Schreiben)."""
        props: dict = {"smartMode": 1}
        if ac_mode is not None:
            props["acMode"] = ac_mode
        if input_limit is not None:
            props["inputLimit"] = int(input_limit)
        if output_limit is not None:
            props["outputLimit"] = int(output_limit)
        try:
            r = requests.post(
                f"{self.base_url}/properties/write",
                json={"sn": self.sn, "properties": props},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Zendure %s write failed: %s", self.ip, e)
            return False

    # ---- Verbindungstest (Settings) -------------------------------------

    def test_connection(self) -> dict:
        with _lock:
            _fail_until.pop(self.ip, None)
        try:
            j = self._report()
        except (requests.RequestException, ValueError) as e:
            return {"ok": False, "error": str(e)}
        st = self.get_status()
        if st is None:
            return {"ok": False, "error": "Antwort ohne electricLevel (lokale API aktiv?)"}
        return {
            "ok": True, "soc": st.soc, "charge_w": st.charge_w, "discharge_w": st.discharge_w,
            "state": st.state, "temp_c": st.temp_c, "sn": st.sn, "product": st.product,
            "version": st.version, "pack_state": st.pack_state,
            "property_keys": sorted(st.props)[:60],
        }
