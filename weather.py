"""Open-Meteo Wetterabruf und PV-Ertragsprognose."""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, timedelta
from typing import Optional

import db

log = logging.getLogger(__name__)

WEATHER_ICONS: dict[int, str] = {
    0:  "☀️",
    1:  "🌤",
    2:  "⛅",
    3:  "☁️",
    45: "🌫",
    48: "🌫",
    51: "🌦",
    53: "🌦",
    55: "🌦",
    61: "🌧",
    63: "🌧",
    65: "🌧",
    71: "🌨",
    73: "🌨",
    75: "🌨",
    80: "🌦",
    81: "🌦",
    82: "🌦",
    95: "⛈",
    96: "⛈",
    99: "⛈",
}


def weathercode_icon(code: Optional[int]) -> str:
    if code is None:
        return "🌡"
    for threshold in sorted(WEATHER_ICONS.keys(), reverse=True):
        if code >= threshold:
            return WEATHER_ICONS[threshold]
    return "🌡"


def fetch_weather(lat: float, lon: float, timeout: int = 10) -> Optional[dict]:
    """Holt 2-Tages-Vorhersage + gestrigen Tag von Open-Meteo."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=sunshine_duration,shortwave_radiation_sum,"
        "temperature_2m_max,temperature_2m_min,weathercode,sunset"
        "&timezone=Europe/Berlin"
        "&past_days=1&forecast_days=2"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.warning("Open-Meteo Abruf fehlgeschlagen: %s", exc)
        return None


def fetch_and_store(lat: float, lon: float) -> bool:
    """Ruft Wetterdaten ab und speichert sie. Überspringt wenn morgen + heutiger Sonnenuntergang vorhanden."""
    if lat == 0.0 and lon == 0.0:
        return False
    today_str = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    today_w = db.get_weather(today_str)
    if db.get_weather(tomorrow) is not None and today_w and today_w.get("sunset"):
        return False
    data = fetch_weather(lat, lon)
    if data is None:
        return False
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    for i, d_str in enumerate(dates):
        def _get(key: str, idx: int = i) -> object:
            vals = daily.get(key, [])
            return vals[idx] if idx < len(vals) and vals[idx] is not None else None
        sh_sec = _get("sunshine_duration")
        rad = _get("shortwave_radiation_sum")
        sunset_raw = _get("sunset")
        sunset_str = sunset_raw[11:16] if sunset_raw and len(sunset_raw) >= 16 else None
        db.upsert_weather(
            date_str=d_str,
            sunshine_hours=round(sh_sec / 3600.0, 2) if sh_sec is not None else None,
            ghi_kwh_m2=round(rad / 3.6, 3) if rad is not None else None,
            temp_max=_get("temperature_2m_max"),
            temp_min=_get("temperature_2m_min"),
            weathercode=_get("weathercode"),
            sunset=sunset_str,
        )
    log.info("Wetterdaten gespeichert für %d Tage", len(dates))
    return True


def calculate_forecast() -> dict:
    """Berechnet PV-Prognose. Gibt immer ein dict zurück; 'tomorrow' ist None wenn unklar."""
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    history = db.get_forecast_correlation_data(90)
    n = len(history)

    today_weather = db.get_weather(today_str)
    tomorrow_weather = db.get_weather(tomorrow_str)
    today_daily = db.get_daily_for_date(today_str)

    result: dict = {
        "data_days": n,
        "today": {
            "date": today_str,
            "actual_kwh": today_daily.get("pv_kwh") if today_daily else None,
            "sunshine_hours": today_weather.get("sunshine_hours") if today_weather else None,
            "ghi_kwh_m2": today_weather.get("ghi_kwh_m2") if today_weather else None,
            "temp_max": today_weather.get("temp_max") if today_weather else None,
            "weathercode": today_weather.get("weathercode") if today_weather else None,
            "weather_icon": weathercode_icon(today_weather.get("weathercode") if today_weather else None),
            "sunset": today_weather.get("sunset") if today_weather else None,
        },
        "tomorrow": None,
        "r2": None,
        "slope": None,
        "intercept": None,
        "correlation": history,
    }

    if n < 7:
        return result

    ghi_vals = [h["ghi_kwh_m2"] for h in history]
    pv_vals = [h["pv_kwh"] for h in history]
    sum_x = sum(ghi_vals)
    sum_y = sum(pv_vals)
    sum_xy = sum(x * y for x, y in zip(ghi_vals, pv_vals))
    sum_xx = sum(x * x for x in ghi_vals)
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-9:
        return result

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in pv_vals)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(ghi_vals, pv_vals))
    r2 = round(1 - ss_res / ss_tot, 3) if ss_tot > 1e-9 else None

    result["r2"] = r2
    result["slope"] = round(slope, 4)
    result["intercept"] = round(intercept, 4)

    if today_weather and today_weather.get("ghi_kwh_m2") is not None:
        result["today"]["predicted_kwh"] = max(0.0, round(
            slope * today_weather["ghi_kwh_m2"] + intercept, 1
        ))

    if tomorrow_weather and tomorrow_weather.get("ghi_kwh_m2") is not None:
        ghi_t = tomorrow_weather["ghi_kwh_m2"]
        result["tomorrow"] = {
            "date": tomorrow_str,
            "predicted_kwh": max(0.0, round(slope * ghi_t + intercept, 1)),
            "sunshine_hours": tomorrow_weather.get("sunshine_hours"),
            "ghi_kwh_m2": ghi_t,
            "temp_max": tomorrow_weather.get("temp_max"),
            "temp_min": tomorrow_weather.get("temp_min"),
            "weathercode": tomorrow_weather.get("weathercode"),
            "weather_icon": weathercode_icon(tomorrow_weather.get("weathercode")),
            "confidence": "good" if n >= 14 else "low",
        }

    return result
