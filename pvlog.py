#!/usr/bin/env python3
"""PV-Daten Logging: stündliche Snapshots und tägliche Zusammenfassung (eigenständiger Cron-Aufruf).

Schreibt dieselben Werte wie der Minuten-Lauf (`main.py` → `pv_logger`): Gesamt-PV und Hausverbrauch
inklusive Pergola und Batterie, Einspeisung/Bezug aus dem Hauptzähler. Der Minuten-Lauf überschreibt
die Zeilen ohnehin laufend; dieser Aufruf bleibt für Setups ohne Controller-Cron nützlich.
(Die frühere Prognose-Berechnung entfiel: Die Morgen-Prognose landete in der Zeile von heute und wurde
vom Minuten-Lauf sofort überschrieben; Prognosen schreibt `/api/forecast`.)

Cron-Einträge (crontab -e), Pfade des Produktionsverzeichnisses:
  0 * * * * /home/openhabian/pvcontroller-prod/.venv/bin/python /home/openhabian/pvcontroller-prod/pvlog.py --mode hourly
  55 23 * * * /home/openhabian/pvcontroller-prod/.venv/bin/python /home/openhabian/pvcontroller-prod/pvlog.py --mode daily
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config

CONFIG_PATH = PROJECT_ROOT / "config.json"
log = logging.getLogger("pvlog")


def _read_result(cfg):
    """Alle Messwerte wie im Controller-Lauf lesen (Haupt-WR, Pergola, Batterie, Hauptzähler, …).

    Liefert ein Objekt mit ``readings`` und ``wallbox_status`` – genau das, was die Logger-Funktionen
    in ``pv_logger`` brauchen. Damit schreibt dieser (Legacy-)Cron dieselben Werte wie der
    Minuten-Lauf: Hausverbrauch inkl. Pergola und Batterie, Gesamt-PV, Eigenverbrauch und
    Tagesbilanz aus dem Hauptzähler.
    """
    from types import SimpleNamespace
    from controller import Controller
    c = Controller(cfg)
    readings = c._read_all()
    wallbox_status = None
    try:
        wallbox_status, _ = c._read_wallbox_status()
    except Exception:
        log.warning("Wallbox-Status nicht lesbar", exc_info=True)
    return SimpleNamespace(readings=readings, wallbox_status=wallbox_status)


def hourly_job(cfg) -> None:
    import pv_logger
    result = _read_result(cfg)
    if result.readings.pv_power_w is None:
        log.error("Solax read failed for hourly log")
        return
    pv_logger._log_hourly(result)
    r = result.readings
    log.info("Hourly snapshot: pv=%dW (Pergola %s W) Batterie %+d W",
             int(r.pv_power_w), "n/a" if r.pv2_power_w is None else int(r.pv2_power_w), int(r.battery_net_w))


def daily_job(cfg) -> None:
    import pv_logger
    result = _read_result(cfg)
    if result.readings.pv_power_w is None:
        log.error("Solax read failed for daily log")
        return
    try:
        pv_logger._fetch_weather_if_needed()
    except Exception:
        log.warning("Wetter-Abruf fehlgeschlagen", exc_info=True)
    pv_logger._log_daily(result)
    log.info("Daily log: date=%s pv=%.1f kWh", date.today().isoformat(),
             (result.readings.solax_yield_today_kwh or 0.0) + (result.readings.pv2_today_kwh or 0.0))


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
