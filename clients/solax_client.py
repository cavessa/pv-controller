"""Solax X3 Pocket-WiFi Client (ReadRealTimeData).

Liest PV-Leistung direkt vom Wechselrichter-Logger.
pv_power_w  = Data[14] (PV1) + Data[15] (PV2).
get_realtime_data() liefert alle Felder für das Dashboard.

Race-condition-Schutz: Das Pocket-WiFi-Modul verarbeitet nur eine
HTTP-Verbindung gleichzeitig. Modul-Level-Cache (TTL _CACHE_TTL_S) +
1 Retry mit Cache-Check verhindern dass parallele Aufrufe
(z. B. /api/status + /api/solax) den Logger blockieren.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---- Modul-Level-Cache (prozesslokal, thread-safe) -------------------
_lock = threading.Lock()
_cached_raw: Optional[list] = None
_cached_ts: float = 0.0
_CACHE_TTL_S: float = 25.0   # Gecachte Daten gelten bis zu 25 s als frisch
_RETRY_DELAY_S: float = 0.25  # Pause vor dem 2. Versuch


def _signed16(value: int) -> float:
    return float(value - 65536 if value > 32767 else value)


@dataclass
class SolaxRealtimeData:
    pv1_power_w: float
    pv2_power_w: float
    pv_total_w: float
    pv1_voltage_v: float
    pv2_voltage_v: float
    pv1_current_a: float
    pv2_current_a: float
    feed_in_w: float          # signed: pos=Einspeisung ins Netz, neg=Netzbezug
    consumption_w: float      # Hausverbrauch gesamt
    yield_today_kwh: float    # PV-Ertrag heute
    grid_out_today_kwh: float # Einspeisung heute
    grid_in_today_kwh: float  # Netzbezug heute
    mode: int


class SolaxClient:
    _HEADERS = {"X-Forwarded-For": "5.8.8.8"}

    def __init__(self, base_url: str, password: str, timeout: int = 5):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _fetch(self) -> Optional[list]:
        """Holt ReadRealTimeData vom Logger.

        Ablauf:
        1. Fast-Path: Cache frisch genug → sofort zurückgeben, kein HTTP.
        2. HTTP-Request (Versuch 1).
        3. Fehlschlag → vor Retry prüfen ob anderer Thread inzwischen gecacht hat.
        4. HTTP-Request (Versuch 2) nach _RETRY_DELAY_S.
        5. Beide Versuche fehlgeschlagen → älteren Cache als Notfall-Fallback.
        6. Kein Cache → None (echte Nichterreichbarkeit).
        """
        global _cached_raw, _cached_ts

        # Fast-Path: Cache ist frisch genug
        with _lock:
            if _cached_raw is not None and (time.monotonic() - _cached_ts) < _CACHE_TTL_S:
                return _cached_raw

        for attempt in range(2):
            if attempt:
                # Vor Retry: prüfen ob ein paralleler Thread den Cache inzwischen
                # aktualisiert hat (häufig bei /api/status + /api/solax gleichzeitig).
                time.sleep(_RETRY_DELAY_S)
                with _lock:
                    if _cached_raw is not None and (time.monotonic() - _cached_ts) < _CACHE_TTL_S:
                        return _cached_raw

            try:
                r = requests.post(
                    self.base_url,
                    data={"optType": "ReadRealTimeData", "pwd": self.password},
                    headers=self._HEADERS,
                    timeout=self.timeout,
                )
                r.raise_for_status()
                payload = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("Solax: Read fehlgeschlagen (Versuch %d/2): %s", attempt + 1, e)
                continue

            raw = payload.get("Data")
            if not isinstance(raw, list) or len(raw) < 93:
                log.error(
                    "Solax: unerwartete Antwort (len=%s)",
                    len(raw) if isinstance(raw, list) else "n/a",
                )
                continue

            # Erfolg – Cache aktualisieren
            with _lock:
                _cached_raw = raw
                _cached_ts = time.monotonic()
            return raw

        # Alle Versuche gescheitert – älteren Cache als letzten Ausweg nutzen
        with _lock:
            if _cached_raw is not None:
                age = time.monotonic() - _cached_ts
                log.warning(
                    "Solax: HTTP fehlgeschlagen, nutze Cache (%.0f s alt).", age
                )
                return _cached_raw

        log.error("Solax: alle Reads fehlgeschlagen, kein Cache vorhanden.")
        return None

    def get_pv_power_w(self) -> Optional[float]:
        raw = self._fetch()
        if raw is None:
            return None
        pv1, pv2 = raw[14], raw[15]
        if not isinstance(pv1, (int, float)) or not isinstance(pv2, (int, float)):
            log.error("Solax: PV-Register ungültig – pv1=%r pv2=%r", pv1, pv2)
            return None
        return float(pv1 + pv2)

    def get_realtime_data(self) -> Optional[SolaxRealtimeData]:
        raw = self._fetch()
        if raw is None:
            return None
        return SolaxRealtimeData(
            pv1_power_w=float(raw[14]),
            pv2_power_w=float(raw[15]),
            pv_total_w=float(raw[14] + raw[15]),
            pv1_voltage_v=raw[10] / 10.0,
            pv2_voltage_v=raw[11] / 10.0,
            pv1_current_a=raw[12] / 10.0,
            pv2_current_a=raw[13] / 10.0,
            feed_in_w=_signed16(raw[34]),
            consumption_w=_signed16(raw[47]),
            yield_today_kwh=raw[82] / 10.0,
            grid_out_today_kwh=raw[90] / 100.0,
            grid_in_today_kwh=raw[92] / 100.0,
            mode=int(raw[19]),
        )
