#!/usr/bin/env python3
"""PV-Daten Logging: stündliche Snapshots und tägliche Zusammenfassung.

Cron-Einträge hinzufügen (crontab -e):
  0 * * * * /home/openhabian/pvcontroller/.venv/bin/python /home/openhabian/pvcontroller/pvlog.py --mode hourly
  55 23 * * * /home/openhabian/pvcontroller/.venv/bin/python /home/openhabian/pvcontroller/pvlog.py --mode daily
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from clients.openmeteo_client import get_today_weather, get_tomorrow_weather
from clients.solax_client import SolaxClient
from config import load_config
from db import get_daily_history, log_daily_summary, log_hourly_snapshot

CONFIG_PATH = PROJECT_ROOT / "config.json"
log = logging.getLogger("pvlog")


def _linear_forecast(history: list[dict], ghi_tomorrow: float) -> float | None:
    pairs = [
        (e["ghi_kwh_m2"], e["pv_kwh"])
        for e in history
        if e.get("ghi_kwh_m2") and e.get("pv_kwh") and e["pv_kwh"] > 0
    ]
    if len(pairs) < 3:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    var_x = sum((x - mx) ** 2 for x in xs) / n
    if var_x < 1e-6:
        return None
    slope = cov / var_x
    return max(0.0, round(slope * ghi_tomorrow + (my - slope * mx), 1))


def hourly_job(cfg) -> None:
    client = SolaxClient(cfg.solax.url, cfg.solax.pwd, cfg.runtime.request_timeout_seconds)
    data = client.get_realtime_data()
    if data is None:
        log.error("Solax read failed for hourly log")
        return
    log_hourly_snapshot(
        pv_w=int(data.pv_total_w), feed_in_w=int(data.feed_in_w),
        consumption_w=int(data.consumption_w),
        str1_w=int(data.pv1_power_w), str2_w=int(data.pv2_power_w),
        str1_v=data.pv1_voltage_v, str1_a=data.pv1_current_a,
        str2_v=data.pv2_voltage_v, str2_a=data.pv2_current_a,
    )
    log.info("Hourly snapshot: pv=%dW str1=%dW str2=%dW",
             int(data.pv_total_w), int(data.pv1_power_w), int(data.pv2_power_w))


def daily_job(cfg) -> None:
    client = SolaxClient(cfg.solax.url, cfg.solax.pwd, cfg.runtime.request_timeout_seconds)
    data = client.get_realtime_data()
    if data is None:
        log.error("Solax read failed for daily log")
        return
    today      = date.today().isoformat()
    weather    = get_today_weather()
    weather_tm = get_tomorrow_weather()
    history      = get_daily_history(30)
    forecast_ghi = weather_tm.get("ghi_kwh_m2") if weather_tm else None
    forecast     = _linear_forecast(history, forecast_ghi) if forecast_ghi else None
    log_daily_summary(
        date_str=today,
        pv_kwh=data.yield_today_kwh,
        feed_out_kwh=data.grid_out_today_kwh,
        feed_in_kwh=data.grid_in_today_kwh,
        sunshine_h=weather.get("sunshine_h") if weather else None,
        ghi_kwh_m2=weather.get("ghi_kwh_m2") if weather else None,
        cloud_cover_pct=weather.get("cloud_cover_pct") if weather else None,
        temp_avg_c=weather.get("temp_avg_c") if weather else None,
        forecast_kwh=forecast,
        forecast_ghi=forecast_ghi,
    )
    log.info("Daily log: date=%s pv=%.1f kWh forecast=%s",
             today, data.yield_today_kwh,
             f"{forecast:.1f} kWh" if forecast else "n/a")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PV-Daten Logging")
    parser.add_argument("--mode", choices=["hourly", "daily"], required=True)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = load_config(args.config)
    if args.mode == "hourly":
        hourly_job(cfg)
    elif args.mode == "daily":
        daily_job(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
