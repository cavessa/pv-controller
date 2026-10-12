"""Persistiert stündliche PV-Snapshots und Tagesstatistiken in SQLite."""
from __future__ import annotations

import logging
from datetime import datetime

import db
from meter import compute_meter_today
from models import ControllerResult

log = logging.getLogger(__name__)


def maybe_log(result: ControllerResult) -> None:
    """Einmal pro Cron-Lauf aufrufen (jede Minute)."""
    try:
        _fetch_weather_if_needed()
    except Exception:
        log.exception("pv_logger: Wetter-Abruf fehlgeschlagen")
    try:
        _log_hourly(result)
    except Exception:
        log.exception("pv_logger: hourly snapshot fehlgeschlagen")
    try:
        _log_daily(result)
    except Exception:
        log.exception("pv_logger: daily summary fehlgeschlagen")
    try:
        _check_string_anomaly(result)
    except Exception:
        log.exception("pv_logger: string anomaly check fehlgeschlagen")
    try:
        db.cleanup_old_hourly(90)
    except Exception:
        log.exception("pv_logger: cleanup fehlgeschlagen")


def _pergola_w(result: ControllerResult) -> int | None:
    """Pergola-Leistung (W) oder None, wenn deaktiviert / nicht lesbar."""
    p2 = result.readings.pv2_power_w
    return int(p2) if p2 is not None else None


def _log_hourly(result: ControllerResult) -> None:
    import clients.solax_client as sc
    raw = sc._cached_raw
    if raw is None or len(raw) < 93:
        return
    r = result.readings
    if r.pv_power_w is None:
        return
    zd = r.zendure

    pv2_w = _pergola_w(result)
    feed_in_w = int(sc._signed16(raw[34]))
    # Der Haupt-WR sieht den Pergola-Anteil im Hausverbrauch nicht
    # Batterie: Entladung gehört zum Verbrauch, Ladung nicht
    consumption_w = int(sc._signed16(raw[47])) + (pv2_w or 0) - int(r.battery_net_w)
    heater_w = int(r.heater_meter_power_w) if r.heater_meter_power_w is not None else None
    wallbox_w = None
    if result.wallbox_status is not None and result.wallbox_status.power_w is not None:
        wallbox_w = int(result.wallbox_status.power_w)

    db.log_hourly_snapshot(
        pv_w=int(r.pv_power_w),
        feed_in_w=feed_in_w,
        consumption_w=consumption_w,
        str1_w=int(raw[14]),
        str2_w=int(raw[15]),
        str1_v=raw[10] / 10.0,
        str1_a=raw[12] / 10.0,
        str2_v=raw[11] / 10.0,
        str2_a=raw[13] / 10.0,
        storage_temp_c=r.storage_temp_c,
        heater_w=heater_w,
        wallbox_w=wallbox_w,
        pv2_w=pv2_w,
        zendure_soc=zd.soc if zd is not None else None,
        zendure_charge_w=int(zd.charge_w) if zd is not None else None,
        zendure_discharge_w=int(zd.discharge_w) if zd is not None else None,
    )


def _fetch_weather_if_needed() -> None:
    import json as _json
    from pathlib import Path as _Path
    cfg_path = _Path(__file__).resolve().parent / "config.json"
    try:
        with open(cfg_path) as f:
            raw = _json.load(f)
        lat = float(raw.get("latitude", 0.0))
        lon = float(raw.get("longitude", 0.0))
    except Exception:
        return
    if lat == 0.0 and lon == 0.0:
        return
    import weather as _weather
    _weather.fetch_and_store(lat, lon)


def _check_string_anomaly(result: ControllerResult) -> None:
    import clients.solax_client as sc
    raw = sc._cached_raw
    if raw is None or len(raw) < 16:
        return
    r = result.readings
    # Strings gehören zum Haupt-WR: Schwelle auf dessen Leistung, nicht die Summe
    if r.pv1_power_w is None or r.pv1_power_w < 500:
        return
    str1_w = int(raw[14])
    str2_w = int(raw[15])
    if str1_w < 10 and str2_w > 200:
        db.log_string_alert(
            "error", "String 1 liefert 0 W während String 2 aktiv ist",
            str1_w, str2_w, 0.0, db.get_normal_ratio(),
        )
        return
    if str2_w < 10 and str1_w > 200:
        db.log_string_alert(
            "error", "String 2 liefert 0 W während String 1 aktiv ist",
            str1_w, str2_w, None, db.get_normal_ratio(),
        )
        return


def _log_daily(result: ControllerResult) -> None:
    import clients.solax_client as sc
    raw = sc._cached_raw
    if raw is None or len(raw) < 93:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    today_weather = db.get_weather(today)

    # Pergola-Tagesertrag; bei Lesefehler den zuletzt gespeicherten Wert behalten,
    # damit pv_kwh nicht kurzzeitig auf den Haupt-WR-Wert zurückfällt.
    pv2_kwh = result.readings.pv2_today_kwh
    if pv2_kwh is None and result.readings.pv2_power_w is None:
        stored = db.get_daily_for_date(today)
        pv2_kwh = stored.get("pv2_kwh") if stored else None
    pv_kwh = raw[82] / 10.0 + (pv2_kwh or 0.0)

    # Einspeisung/Bezug bevorzugt aus dem Hauptzähler (Shelly), sonst Solax
    feed_out_kwh, feed_in_kwh = raw[90] / 100.0, raw[92] / 100.0
    mt = compute_meter_today(result.readings, pv_kwh=pv_kwh)
    if mt is not None and mt["source"] == "shelly":
        feed_out_kwh, feed_in_kwh = mt["einspeisung_kwh"], mt["bezug_kwh"]

    zen = db.get_zendure_energy(today)
    db.log_daily_summary(
        date_str=today,
        pv_kwh=pv_kwh,
        pv2_kwh=pv2_kwh,
        zendure_charged_kwh=zen["charged_kwh"],
        zendure_discharged_kwh=zen["discharged_kwh"],
        feed_out_kwh=feed_out_kwh,
        feed_in_kwh=feed_in_kwh,
        sunshine_h=today_weather.get("sunshine_hours") if today_weather else None,
        ghi_kwh_m2=today_weather.get("ghi_kwh_m2") if today_weather else None,
    )
