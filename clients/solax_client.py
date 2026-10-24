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

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
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
# Notfall-Fallback bei Lesefehler: nur kurz alte Daten, ältere gelten als „nicht erreichbar“. Sonst zeigt der
# langlaufende Web-Prozess Stunden alte Werte (z. B. Pergola-WR, der ohne Sonne offline geht) als aktuell.
_STALE_MAX_S: float = 180.0
# Nach einem Totalausfall (alle Versuche fehlgeschlagen) das Gerät so lange nicht erneut abfragen. Ein
# offline Pergola-WR kostete sonst bei jedem Aufruf den vollen Timeout (Dashboard ~3–6 s).
_FAIL_BACKOFF_S: float = 30.0
# Offline-Meldungen: nur beim Wechsel (offline / wieder erreichbar) und danach höchstens alle 30 min. Der
# Zustand liegt in einer Datei, weil jeder Cron-Lauf ein neuer Prozess ist (sonst käme die Meldung jede Minute).
_OFFLINE_LOG_EVERY_S: float = 1800.0
_OFFLINE_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "offline_log_state.json"
_PROC_TAG = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "proc"   # main.py / uvicorn / …: je Prozessart
_offline_state: Optional[dict] = None
_fail_until: dict[str, float] = {}                 # base_url -> monotonic-Zeitpunkt, bis zu dem nicht gelesen wird
_url_locks: dict[str, threading.Lock] = {}


def _offline_load() -> dict:
    global _offline_state
    if _offline_state is None:
        try:
            _offline_state = json.loads(_OFFLINE_STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(_offline_state, dict):
                _offline_state = {}
        except (OSError, ValueError):
            _offline_state = {}
    return _offline_state


def _offline_save(state: dict) -> None:
    try:
        _OFFLINE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OFFLINE_STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(_OFFLINE_STATE_PATH)
    except OSError:
        pass                                   # ohne Zustandsdatei wird eben öfter gemeldet


def _hhmm(epoch: float) -> str:
    return time.strftime("%d.%m. %H:%M", time.localtime(epoch))


def _note_failure(url: str, why: str) -> None:
    """Totalausfall melden: beim ersten Mal und danach höchstens alle _OFFLINE_LOG_EVERY_S, sonst still."""
    now, key = time.time(), f"{url}|{_PROC_TAG}"
    with _lock:
        state = _offline_load()
        e = state.get(key)
        if e is None:
            state[key] = {"since": now, "last_log": now}
            msg = "Solax %s nicht erreichbar (%s) – weitere Meldungen höchstens alle 30 min."
        elif now - e.get("last_log", 0) >= _OFFLINE_LOG_EVERY_S:
            e["last_log"] = now
            msg = "Solax %s weiterhin nicht erreichbar (%s), seit " + _hhmm(e.get("since", now)) + "."
        else:
            return
        _offline_save(state)
    log.warning(msg, url, why)


def _note_success(url: str) -> None:
    key = f"{url}|{_PROC_TAG}"
    with _lock:
        state = _offline_load()
        e = state.pop(key, None)
        if e is not None:
            _offline_save(state)
    if e is not None:
        log.info("Solax %s wieder erreichbar (offline seit %s).", url, _hhmm(e.get("since", time.time())))


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

    def __init__(self, base_url: str, password: str, timeout: int = 5, connect_timeout: Optional[float] = None):
        """``connect_timeout``: kürzeres Limit nur für den Verbindungsaufbau (Pergola-WR: offline ohne Sonne soll
        schnell auffallen). Ohne Angabe gilt ``timeout`` – der Haupt-WR nimmt Verbindungen mitunter langsam an."""
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.connect_timeout = timeout if connect_timeout is None else min(connect_timeout, timeout)
        self.device_type: Optional[int] = None

    def _url_lock(self) -> threading.Lock:
        with _lock:
            return _url_locks.setdefault(self.base_url, threading.Lock())

    def reset_backoff(self) -> None:
        """Fehler-Backoff dieses Geräts löschen (z. B. für den „Verbindung testen“-Button)."""
        with _lock:
            _fail_until.pop(self.base_url, None)

    def _in_backoff(self) -> bool:
        with _lock:
            return time.monotonic() < _fail_until.get(self.base_url, 0.0)

    def _stale_fallback(self) -> Optional[list]:
        """Letzter Cache-Eintrag, aber höchstens _STALE_MAX_S alt (sonst None = nicht erreichbar)."""
        with _lock:
            entry = _cache.get(self.base_url)
            if entry is not None and (time.monotonic() - entry[1]) <= _STALE_MAX_S:
                self.device_type = entry[2]
                log.debug(
                    "Solax %s: HTTP fehlgeschlagen, nutze Cache (%.0f s alt).",
                    self.base_url, time.monotonic() - entry[1],
                )
                return entry[0]
        return None

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
        2. Gerät nach Totalausfall noch im Backoff → sofort Fallback, kein HTTP.
        3. Pro Gerät nur ein Lesender gleichzeitig (Pocket-WiFi); Wartende prüfen danach Cache/Backoff neu.
        4. HTTP-Request (Versuch 1), bei Fehlschlag nach _RETRY_DELAY_S Versuch 2.
        5. Alle Versuche fehlgeschlagen → Backoff setzen, Cache höchstens _STALE_MAX_S alt als Fallback.
        6. Kein brauchbarer Cache → None (echte Nichterreichbarkeit).
        """
        global _cached_raw, _cached_ts

        # Fast-Path: Cache ist frisch genug
        fresh = self._cache_get(_CACHE_TTL_S)
        if fresh is not None:
            return fresh
        if self._in_backoff():
            return self._stale_fallback()

        with self._url_lock():
            # Ein paralleler Aufruf hat inzwischen gelesen (Erfolg: Cache frisch, Fehlschlag: Backoff)
            fresh = self._cache_get(_CACHE_TTL_S)
            if fresh is not None:
                return fresh
            if self._in_backoff():
                return self._stale_fallback()

            why = "keine Antwort"
            for attempt in range(2):
                if attempt:
                    time.sleep(_RETRY_DELAY_S)
                try:
                    r = requests.post(
                        self.base_url,
                        data={"optType": "ReadRealTimeData", "pwd": self.password},
                        headers=self._HEADERS,
                        timeout=(self.connect_timeout, self.timeout),
                    )
                    r.raise_for_status()
                    payload = r.json()
                except (requests.RequestException, ValueError) as e:
                    why = type(e).__name__
                    log.debug("Solax %s: Read fehlgeschlagen (Versuch %d/2): %s", self.base_url, attempt + 1, e)
                    continue

                raw = payload.get("Data")
                if not isinstance(raw, list) or len(raw) < 93:
                    why = f"unerwartete Antwort (len={len(raw) if isinstance(raw, list) else 'n/a'})"
                    log.debug("Solax %s: %s", self.base_url, why)
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
                    _fail_until.pop(self.base_url, None)
                    if dev_type not in _X1_TYPES:
                        _cached_raw, _cached_ts = raw, now
                _note_success(self.base_url)
                return raw

            with _lock:
                _fail_until[self.base_url] = time.monotonic() + _FAIL_BACKOFF_S
            _note_failure(self.base_url, why)

        return self._stale_fallback()

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
