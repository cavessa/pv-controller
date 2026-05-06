"""Persistiert stündliche PV-Snapshots und Tagesstatistiken in SQLite."""
from __future__ import annotations

import logging
from datetime import datetime

import db
from models import ControllerResult

log = logging.getLogger(__name__)


def maybe_log(result: ControllerResult) -> None:
    """Einmal pro Cron-Lauf aufrufen (jede Minute)."""
    try:
        _log_hourly(result)
    except Exception:
        log.exception("pv_logger: hourly snapshot fehlgeschlagen")
    try:
        _log_daily()
    except Exception:
        log.exception("pv_logger: daily summary fehlgeschlagen")
    try:
        db.cleanup_old_hourly(90)
    except Exception:
        log.exception("pv_logger: cleanup fehlgeschlagen")


def _log_hourly(result: ControllerResult) -> None:
    import clients.solax_client as sc
    raw = sc._cached_raw
    if raw is None or len(raw) < 93:
        return
    r = result.readings
    if r.pv_power_w is None:
        return

    feed_in_w = int(sc._signed16(raw[34]))
    consumption_w = int(sc._signed16(raw[47]))
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
    )


def _log_daily() -> None:
    import clients.solax_client as sc
    raw = sc._cached_raw
    if raw is None or len(raw) < 93:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    db.log_daily_summary(
        date_str=today,
        pv_kwh=raw[82] / 10.0,
        feed_out_kwh=raw[90] / 100.0,
        feed_in_kwh=raw[92] / 100.0,
    )
