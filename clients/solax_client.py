"""Solax X3 Pocket-WiFi Client (ReadRealTimeData).

Liest PV-Leistung direkt vom Wechselrichter-Logger.
pv_power_w  = Data[14] (PV1) + Data[15] (PV2) – DC je String (Detail).
AC-Leistung = Data[9] (X3) bzw. Data[2] (X1) – gilt als „PV-Leistung“ (``pv_ac_w``).
get_realtime_data() liefert alle Felder für das Dashboard.

Race-condition-Schutz: Das Pocket-WiFi-Modul verarbeitet nur eine
HTTP-Verbindung gleichzeitig. Modul-Level-Cache (TTL _CACHE_TTL_S) +
1 Retry mit Cache-Check verhindern dass parallele Aufrufe
(z. B. /api/status + /api/solax) den Logger blockieren.

Mehrere Geräte: Der Cache ist pro Basis-URL getrennt (Haupt-WR + Pergola-WR).
Die Register-Belegung hängt vom Gerätetyp ab (payload["type"]):
  Typ 4  = X1 (Pergola-PV): Data[2]=AC-Leistung, [3]/[4]=PV-Spannung,
           [5]/[6]=PV-Strom (je 0,1), [7]/[8]=PV-Leistung, [13]=Ertrag heute (0,1 kWh).
  sonst  = X3-Belegung (Hauptwechselrichter, siehe get_realtime_data).
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
# base_url -> (raw Data-Liste, monotonic-Zeitstempel, Gerätetyp)
_cache: dict[str, tuple[list, float, int]] = {}
# Rückwärtskompatibel (pv_logger): Rohdaten des zuletzt erfolgreich gelesenen
# Haupt-Wechselrichters (nicht-X1). Der Pergola-WR überschreibt das nie.
_cached_raw: Optional[list] = None
_cached_ts: float = 0.0
_X1_TYPES = (4,)
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
    model: str = "x3"         # "x3" (Haupt-WR) | "x1" (Pergola-PV)
    ac_power_w: Optional[float] = None  # AC-Ausgangsleistung (X3: Data[9], X1: Data[2]); None = unbekannt

    @property
    def pv_ac_w(self) -> float:
        """PV-Leistung als AC-Ausgang (nie negativ); ohne AC-Register die DC-Summe."""
        if self.ac_power_w is None:
            return self.pv_total_w
        return max(0.0, self.ac_power_w)


class SolaxClient:
    _HEADERS = {"X-Forwarded-For": "5.8.8.8"}

    def __init__(self, base_url: str, password: str, timeout: int = 5):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.device_type: Optional[int] = None

    def _cache_get(self, max_age: float) -> Optional[list]:
        """Cache-Eintrag dieses Geräts, wenn jünger als max_age (sonst None)."""
        with _lock:
            entry = _cache.get(self.base_url)
            if entry is not None and (time.monotonic() - entry[1]) < max_age:
                self.device_type = entry[2]
                return entry[0]
        return None

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
        fresh = self._cache_get(_CACHE_TTL_S)
        if fresh is not None:
            return fresh

        for attempt in range(2):
            if attempt:
                # Vor Retry: prüfen ob ein paralleler Thread den Cache inzwischen
                # aktualisiert hat (häufig bei /api/status + /api/solax gleichzeitig).
                time.sleep(_RETRY_DELAY_S)
                fresh = self._cache_get(_CACHE_TTL_S)
                if fresh is not None:
                    return fresh

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

            try:
                dev_type = int(payload.get("type", 0))
            except (TypeError, ValueError):
                dev_type = 0
            self.device_type = dev_type

            # Erfolg – Cache aktualisieren
            with _lock:
                now = time.monotonic()
                _cache[self.base_url] = (raw, now, dev_type)
                if dev_type not in _X1_TYPES:
                    _cached_raw, _cached_ts = raw, now
            return raw

        # Alle Versuche gescheitert – älteren Cache als letzten Ausweg nutzen
        with _lock:
            entry = _cache.get(self.base_url)
            if entry is not None:
                self.device_type = entry[2]
                log.warning(
                    "Solax %s: HTTP fehlgeschlagen, nutze Cache (%.0f s alt).",
                    self.base_url, time.monotonic() - entry[1],
                )
                return entry[0]

        log.error("Solax %s: alle Reads fehlgeschlagen, kein Cache vorhanden.", self.base_url)
        return None

    @property
    def is_x1(self) -> bool:
        return self.device_type in _X1_TYPES

    def get_pv_power_w(self) -> Optional[float]:
        raw = self._fetch()
        if raw is None:
            return None
        if self.is_x1:
            pv1, pv2 = raw[7], raw[8]
        else:
            pv1, pv2 = raw[14], raw[15]
        if not isinstance(pv1, (int, float)) or not isinstance(pv2, (int, float)):
            log.error("Solax: PV-Register ungültig – pv1=%r pv2=%r", pv1, pv2)
            return None
        return float(pv1 + pv2)

    def get_pv_ac_w(self) -> Optional[float]:
        """PV-Leistung als AC-Ausgang (aus demselben, gecachten Read)."""
        d = self.get_realtime_data()
        return None if d is None else d.pv_ac_w

    def get_realtime_data(self) -> Optional[SolaxRealtimeData]:
        raw = self._fetch()
        if raw is None:
            return None
        return self._parse(raw)

    def cached_realtime_data(self, max_age: float) -> Optional[SolaxRealtimeData]:
        """Nur aus dem Prozess-Cache (jünger als ``max_age`` s), nie eine HTTP-Abfrage am Gerät."""
        raw = self._cache_get(max_age)
        return None if raw is None else self._parse(raw)

    def cache_age(self) -> Optional[float]:
        """Alter des Cache-Eintrags dieses Geräts in s (None: nie gelesen)."""
        with _lock:
            entry = _cache.get(self.base_url)
        return None if entry is None else time.monotonic() - entry[1]

    def _parse(self, raw: list) -> SolaxRealtimeData:
        if self.is_x1:
            return SolaxRealtimeData(
                pv1_power_w=float(raw[7]),
                pv2_power_w=float(raw[8]),
                pv_total_w=float(raw[7] + raw[8]),
                pv1_voltage_v=raw[3] / 10.0,
                pv2_voltage_v=raw[4] / 10.0,
                pv1_current_a=raw[5] / 10.0,
                pv2_current_a=raw[6] / 10.0,
                feed_in_w=0.0,
                consumption_w=0.0,
                yield_today_kwh=raw[13] / 10.0,
                grid_out_today_kwh=0.0,
                grid_in_today_kwh=0.0,
                mode=int(raw[10]),
                model="x1",
                ac_power_w=float(raw[2]),
            )
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
            ac_power_w=_signed16(raw[9]),
        )
