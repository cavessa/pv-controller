"""go-eCharger HTTP-API-v2-Client (read + force-state schalten)."""

from __future__ import annotations

import logging
from typing import Optional, Any

import requests

log = logging.getLogger(__name__)

STATUS_FILTER = "fup,frc,alw,car,amp,nrg,wh,eto,cdi,rbt,lmo,psm"


class GoeError(Exception):
    pass


class GoeClient:
    def __init__(self, base_url: str, timeout: int = 5):
        if not base_url:
            raise ValueError("GoeClient requires a non-empty base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_status(self) -> dict[str, Any]:
        url = f"{self.base_url}/api/status"
        try:
            r = requests.get(url, params={"filter": STATUS_FILTER}, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise GoeError(f"go-e status request failed: {e}") from e
        except ValueError as e:
            raise GoeError(f"go-e status returned non-JSON: {e}") from e
        if not isinstance(data, dict):
            raise GoeError(f"go-e status returned unexpected payload: {data!r}")
        return data

    def set_amp(self, value: int) -> None:
        url = f"{self.base_url}/api/set"
        try:
            r = requests.get(url, params={"amp": value}, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GoeError(f"go-e set amp={value} failed: {e}") from e

    def set_psm(self, value: int) -> None:
        if value not in (1, 2):
            raise ValueError(
                f"set_psm only accepts 1 (einphasig) or 2 (dreiphasig), got {value!r}"
            )
        url = f"{self.base_url}/api/set"
        try:
            r = requests.get(url, params={"psm": value}, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GoeError(f"go-e set psm={value} failed: {e}") from e

    def set_logic_mode(self, value: int) -> None:
        url = f"{self.base_url}/api/set"
        try:
            r = requests.get(url, params={"lmo": value}, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GoeError(f"go-e set lmo={value} failed: {e}") from e

    def set_force_state(self, value: int) -> None:
        if value not in (0, 1):
            raise ValueError(
                f"set_force_state only accepts 0 or 1, got {value!r} "
                "(frc=2 is intentionally not supported)"
            )
        url = f"{self.base_url}/api/set"
        try:
            r = requests.get(url, params={"frc": value}, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GoeError(f"go-e set frc={value} failed: {e}") from e


CHARGING_MIN_W = 100.0     # Ladeleistung, ab der die Wallbox als „lädt“ gilt (auch ohne car=2)
BATTERY_CHECK_TIMEOUT_S = 2.0


def car_charging(status: Optional[dict]) -> bool:
    """Lädt die Wallbox? car=2 ODER Ladeleistung (nrg[11]) > 100 W; ohne Status → True
    (unbekannt gilt als „lädt“: die Batterie darf dann nie entladen)."""
    if not isinstance(status, dict) or not status:
        return True
    nrg = status.get("nrg")
    try:
        power = float(nrg[11]) if isinstance(nrg, list) and len(nrg) >= 12 else 0.0
    except (TypeError, ValueError):
        power = 0.0
    return status.get("car") == 2 or power > CHARGING_MIN_W


def charging_now(client: Optional["GoeClient"]) -> bool:
    """go-e frisch lesen (für die Batterie-Sperre). Ohne Wallbox-URL False; Lesefehler → True."""
    if client is None:
        return False
    try:
        return car_charging(client.get_status())
    except Exception:
        log.warning("go-e nicht lesbar – gilt für die Batterie als „lädt“")
        return True
