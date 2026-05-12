"""Einstiegspunkt: ein Controller-Durchlauf, gedacht für Cron."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

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
    try:
        CascadeService(config).run_cascade()
    except Exception:
        log.exception("Unerwarteter Fehler im Kaskaden-Durchlauf")

    try:
        result = Controller(config).run()
        from pv_logger import maybe_log
        maybe_log(result)
        try:
            from db import append_temp_history
            wb_w = result.wallbox_status.power_w if result.wallbox_status else 0.0
            append_temp_history(result.readings.storage_temp_c, wb_w)
        except Exception:
            log.warning("append_temp_history fehlgeschlagen", exc_info=True)
        return 0
    except Exception:
        log.exception("Unerwarteter Fehler im Controller-Lauf")
        return 1


if __name__ == "__main__":
    sys.exit(main())
