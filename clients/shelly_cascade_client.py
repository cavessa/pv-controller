"""Shelly-Client speziell für die Kaskaden-Steuerung.

Unterstützt Gen1 (Plug S, 1, 2.5 …) und Gen2 (Plus, Pro …).
Timeout 3 Sekunden, keine Retries — Shelly antwortet schnell oder gar nicht.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 3  # Sekunden


class ShellyCascadeError(Exception):
    pass


class ShellyCascadeClient:

    def __init__(self, timeout: int = _TIMEOUT) -> None:
        self.timeout = timeout

    # ── Status ───────────────────────────────────────────────────────────────

    def get_status(self, ip: str, type_: str, channel: int) -> dict[str, Any]:
        """Gibt {'is_on', 'power', 'voltage', 'temperature'} zurück.

        Raises ShellyCascadeError bei Verbindungsproblemen.
        """
        try:
            if type_ == "shelly_gen2":
                return self._gen2_status(ip, channel)
            else:
                return self._gen1_status(ip, channel)
        except requests.RequestException as e:
            raise ShellyCascadeError(str(e)) from e

    def _gen2_status(self, ip: str, channel: int) -> dict[str, Any]:
        r = requests.get(
            f"http://{ip}/rpc/Switch.GetStatus",
            params={"id": channel},
            timeout=self.timeout,
        )
        r.raise_for_status()
        d = r.json()
        temp_raw = d.get("temperature")
        temp_c: Optional[float] = None
        if isinstance(temp_raw, dict):
            temp_c = temp_raw.get("tC")
        return {
            "is_on": bool(d.get("output", False)),
            "power": float(d.get("apower") or 0),
            "voltage": d.get("voltage"),
            "temperature": temp_c,
        }

    def _gen1_status(self, ip: str, channel: int) -> dict[str, Any]:
        r = requests.get(f"http://{ip}/status", timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        relays = d.get("relays", [{}])
        relay = relays[channel] if channel < len(relays) else {}
        meters = d.get("meters", [])
        meter = meters[channel] if channel < len(meters) else {}
        return {
            "is_on": bool(relay.get("ison", False)),
            "power": float(meter.get("power") or 0),
            "voltage": meter.get("voltage"),
            "temperature": None,
        }

    # ── Schalten ─────────────────────────────────────────────────────────────

    def turn_on(self, ip: str, type_: str, channel: int) -> None:
        self._set_relay(ip, type_, channel, on=True)

    def turn_off(self, ip: str, type_: str, channel: int) -> None:
        self._set_relay(ip, type_, channel, on=False)

    def _set_relay(self, ip: str, type_: str, channel: int, on: bool) -> None:
        try:
            if type_ == "shelly_gen2":
                r = requests.post(
                    f"http://{ip}/rpc/Switch.Set",
                    json={"id": channel, "on": on},
                    timeout=self.timeout,
                )
            else:
                r = requests.get(
                    f"http://{ip}/relay/{channel}",
                    params={"turn": "on" if on else "off"},
                    timeout=self.timeout,
                )
            r.raise_for_status()
        except requests.RequestException as e:
            raise ShellyCascadeError(str(e)) from e

    # ── Verbindungstest ───────────────────────────────────────────────────────

    def test_connection(
        self, ip: str, type_: str, channel: int
    ) -> dict[str, Any]:
        """Verbindungstest für den Hinzufügen-Dialog.

        Gibt immer ein Dict zurück — nie eine Exception.
        """
        try:
            if type_ == "shelly_gen2":
                return self._test_gen2(ip, channel)
            else:
                return self._test_gen1(ip, channel)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _test_gen2(self, ip: str, channel: int) -> dict[str, Any]:
        info_r = requests.get(
            f"http://{ip}/rpc/Shelly.GetDeviceInfo", timeout=self.timeout
        )
        info_r.raise_for_status()
        info = info_r.json()
        status = self._gen2_status(ip, channel)
        return {
            "success": True,
            "model": info.get("model", "Shelly Gen2"),
            "firmware": info.get("fw_id", "–"),
            "is_on": status["is_on"],
            "power": status["power"],
        }

    def _test_gen1(self, ip: str, channel: int) -> dict[str, Any]:
        settings_r = requests.get(f"http://{ip}/settings", timeout=self.timeout)
        settings_r.raise_for_status()
        settings = settings_r.json()
        status = self._gen1_status(ip, channel)
        return {
            "success": True,
            "model": settings.get("device", {}).get("type", "Shelly Gen1"),
            "firmware": settings.get("fw", "–"),
            "is_on": status["is_on"],
            "power": status["power"],
        }
