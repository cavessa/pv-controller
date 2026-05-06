"""Shelly-Clients für Plug (Gen1 /relay/0), 3EM (/status) und Plus-Add-On (/rpc)."""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class ShellyError(Exception):
    pass


class ShellyPlugClient:
    """Gen1-Style Plug/Relay (PH1/PH2/PH3 Shelly Plugs)."""

    def __init__(self, base_url: str, timeout: int = 5, name: str = "shelly"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name

    def get_relay_state(self) -> Optional[bool]:
        try:
            r = requests.get(f"{self.base_url}/relay/0", timeout=self.timeout)
            r.raise_for_status()
            return bool(r.json()["ison"])
        except (requests.RequestException, KeyError, ValueError) as e:
            log.error("Shelly %s read /relay/0 failed: %s", self.name, e)
            return None

    def set_relay(self, on: bool) -> bool:
        action = "on" if on else "off"
        try:
            r = requests.get(
                f"{self.base_url}/relay/0",
                params={"turn": action},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("Shelly %s switch %s failed: %s", self.name, action, e)
            return False


class Shelly3EMClient:
    """Shelly 3EM / EM (Gen1) — /status liefert total_power."""

    def __init__(self, base_url: str, timeout: int = 5, name: str = "3em"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = name

    def get_total_power_w(self) -> Optional[float]:
        try:
            r = requests.get(f"{self.base_url}/status", timeout=self.timeout)
            r.raise_for_status()
            return float(r.json()["total_power"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.error("Shelly %s read /status failed: %s", self.name, e)
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
