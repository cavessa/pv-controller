"""Einstiegspunkt: ein Controller-Durchlauf, gedacht für Cron."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

import redact
from cascade_service import CascadeService
from config import load_config
from controller import Controller


def setup_logging(log_file: str) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    redact.install(file_handler, stream)        # API-Keys (sk-…) nie ins Log


HEATING_BUDGET_S = 5.0     # Heizungs-Zeitpläne: maximale Gesamtzeit nach der PV-Steuerung
SHUTTER_BUDGET_S = 20.0    # Rollladen-Zeitpläne (gestaffeltes Senden braucht Zeit)


def _run_isolated(module: str, budget_s: float, config_path: str, dry_run: bool, logger: str,
                  summary: str, name: str, thread: str) -> None:
    """``<module>.run`` nach der PV-Steuerung, isoliert: eigener Thread mit hartem Zeitlimit.

    Fehler und hängende openHAB-Requests werden geloggt und nie weitergereicht. Läuft der Thread
    über das Limit, endet der Cron-Lauf trotzdem (Daemon-Thread); offene Einträge bleiben fällig.
    """
    log = logging.getLogger(logger)
    t0 = time.monotonic()

    def work() -> None:
        try:
            mod = __import__(module)
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            res = mod.run(raw, dry_run=dry_run, budget_s=budget_s)
            if res.requests or res.skipped or res.timed_out:
                log.info("%s: %d fällig, %d gesendet, %d übersprungen, %d openHAB-Requests, %.2f s",
                         summary, res.due, res.sent, res.skipped, res.requests, res.duration_s)
        except Exception:
            log.exception("%s: Fehler (PV-Steuerung nicht betroffen)", name)

    th = threading.Thread(target=work, name=thread, daemon=True)
    th.start()
    th.join(budget_s + 0.5)
    if th.is_alive():
        log.warning("%s: nach %.1f s abgebrochen (openHAB hängt?) – nächste Minute erneut",
                    name, time.monotonic() - t0)


def run_heating_schedules(config_path: str, dry_run: bool, budget_s: float | None = None) -> None:
    """Heizungs-Zeitpläne nach der PV-Steuerung (eigenes Zeitlimit, Fehler isoliert)."""
    _run_isolated("heating_schedule", HEATING_BUDGET_S if budget_s is None else budget_s, config_path, dry_run,
                  "pv-controller.heating", "Zeitpläne", "Heizungs-Zeitpläne", "heating-schedules")


def run_shutter_schedules(config_path: str, dry_run: bool, budget_s: float | None = None) -> None:
    """Rollladen-Zeitpläne nach der Heizung (eigenes Zeitlimit, Fehler isoliert)."""
    _run_isolated("shutter_schedule", SHUTTER_BUDGET_S if budget_s is None else budget_s, config_path, dry_run,
                  "pv-controller.shutters", "Rollladen-Zeitpläne", "Rollladen-Zeitpläne", "shutter-schedules")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PV-Überschuss Heizstab-Controller")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="Pfad zur config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Überschreibt runtime.dry_run und schaltet nichts.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.dry_run:
        config.runtime.dry_run = True

    setup_logging(config.runtime.log_file)
    log = logging.getLogger("pv-controller.main")

    # Kaskade zuerst: setzt is_on-Flags, die der Controller danach liest.
    cascade_res = None
    try:
        cascade_res = CascadeService(config).run_cascade()
    except Exception:
        log.exception("Unerwarteter Fehler im Kaskaden-Durchlauf")

    rc = 0
    result = None
    try:
        result = Controller(config).run()
        from pv_logger import maybe_log
        maybe_log(result)
        try:
            from db import append_temp_history
            wb_w = result.wallbox_status.power_w if result.wallbox_status else 0.0
            append_temp_history(
                result.readings.storage_temp_c, wb_w, result.readings.heater_meter_power_w
            )
        except Exception:
            log.warning("append_temp_history fehlgeschlagen", exc_info=True)
    except Exception:
        log.exception("Unerwarteter Fehler im Controller-Lauf")
        rc = 1

    # Entscheidungs-Schnappschuss + Geräte-Ereignislog (reines Protokoll, Fehler werden dort abgefangen)
    try:
        import decisions
        with open(args.config, "r", encoding="utf-8") as f:
            raw_cfg = json.load(f)
        decisions.record_run(config, raw_cfg, cascade_res, result)
    except Exception:
        log.warning("Entscheidungs-Schnappschuss fehlgeschlagen", exc_info=True)

    # Erst nach PV/Heizstab/Wallbox: Heizungs-, dann Rollladen-Zeitpläne (je eigenes Zeitlimit, Fehler isoliert)
    run_heating_schedules(args.config, config.runtime.dry_run)
    run_shutter_schedules(args.config, config.runtime.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
