"""Schmaler OpenHAB-REST-Client.

Phase 1 liest ausschließlich die aktuelle PV-Leistung als float.
Andere Items werden bewusst nicht mehr gelesen (kein controller_active_item).
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class OpenHabClient:
    def __init__(self, base_url: str, timeout: int = 5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_pv_power_w(self, item_name: str) -> Optional[float]:
        url = f"{self.base_url}/items/{item_name}/state"
        try:
            r = requests.get(url, timeout=self.timeout)
            r.raise_for_status()
            raw = r.text.strip()
        except requests.RequestException as e:
            log.error("OpenHAB read failed for %s: %s", item_name, e)
            return None

        if raw in ("", "NULL", "UNDEF"):
            log.error("OpenHAB item %s has no usable state (got %r)", item_name, raw)
            return None
        try:
            return float(raw.split(" ")[0])
        except ValueError:
            log.error("OpenHAB item %s state %r is not numeric", item_name, raw)
            return None
