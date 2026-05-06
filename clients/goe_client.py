"""go-eCharger HTTP-API-v2-Client (read + force-state schalten)."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

STATUS_FILTER = "fup,frc,alw,car,amp,acs,nrg,wh,eto,cdi,rbt"


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

    def set_access_state(self, value: int) -> None:
        if value not in (0, 1):
            raise ValueError(
                f"set_access_state only accepts 0 or 1, got {value!r}"
            )
        url = f"{self.base_url}/api/set"
        try:
            r = requests.get(url, params={"acs": value}, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise GoeError(f"go-e set acs={value} failed: {e}") from e
