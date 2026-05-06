"""Open-Meteo Wetter-Client für Remseck am Neckar (kein API-Key nötig)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"
_LAT  = 48.8764
_LON  = 9.2722
_TZ   = "Europe/Berlin"


def _fetch(past_days: int = 1, forecast_days: int = 2) -> Optional[dict]:
    try:
        r = requests.get(
            _BASE,
            params={
                "latitude": _LAT, "longitude": _LON,
                "daily": "sunshine_duration,shortwave_radiation_sum,cloud_cover_mean,temperature_2m_mean",
                "timezone": _TZ,
                "past_days": past_days,
                "forecast_days": forecast_days,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.error("Open-Meteo request failed: %s", e)
        return None


def get_weather_for_date(target: date) -> Optional[dict]:
    """
    Wetterdaten für ein Datum (Vergangenheit oder Zukunft).
    Gibt dict mit sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c zurück.
    """
    today = date.today()
    delta = (today - target).days
    past = max(0, delta + 1)
    fcast = max(1, -delta + 1)
    payload = _fetch(past_days=past, forecast_days=fcast)
    if not payload:
        return None
    daily = payload.get("daily", {})
    times = daily.get("time", [])
    target_s = target.isoformat()
    if target_s not in times:
        return None
    idx = times.index(target_s)

    def _v(key: str, divisor: float = 1.0) -> Optional[float]:
        vals = daily.get(key, [])
        v = vals[idx] if idx < len(vals) else None
        return round(v / divisor, 2) if v is not None else None

    return {
        "sunshine_h":      _v("sunshine_duration",       3600.0),
        "ghi_kwh_m2":      _v("shortwave_radiation_sum", 3.6),
        "cloud_cover_pct": _v("cloud_cover_mean"),
        "temp_avg_c":      _v("temperature_2m_mean"),
    }


def get_today_weather() -> Optional[dict]:
    return get_weather_for_date(date.today())


def get_tomorrow_weather() -> Optional[dict]:
    return get_weather_for_date(date.today() + timedelta(days=1))
