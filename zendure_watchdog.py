"""Watchdog für die Zendure-Regelung – eigener Cron-Eintrag, unabhängig von main.py.

zenSDK kennt keinen Heartbeat/keine Gültigkeitsdauer: ein geschriebener Sollwert gilt, bis
jemand einen neuen schreibt. Hängt oder fehlt die Regelung (main.py hängt, Absturz, Kaskade
aus), würde die Batterie mit dem letzten Wert weiterladen (aus dem Netz) oder -entladen.

- Jede Minute: Regelung (zendure_ctl.decided_at) älter als ``STALE_S`` → Standby schreiben
  (inputLimit = outputLimit = 0), sofern das Gerät nicht schon steht.
- ``--boot`` (Cron ``@reboot``): Standby unbedingt, mit Wiederholung bis das Gerät antwortet.

Nur aktiv mit ``zendure.enabled`` + ``zendure.control_enabled``; bei ``runtime.dry_run`` nur Log.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from clients.zendure_client import ZendureClient
from config import load_config
from zendure_control import WRITE_DELTA_W, control_age_s, force_standby

log = logging.getLogger("pv-controller.zendure-watchdog")

STALE_S = 5 * 60
BOOT_TRIES = 12
BOOT_WAIT_S = 15.0


def _is_standby(client: ZendureClient) -> Optional[bool]:
    """True = Gerät meldet beide Limits ~0; None = nicht lesbar."""
    st = client.get_status()
    if st is None:
        return None
    try:
        return all(abs(float(st.props.get(k, 0) or 0)) <= WRITE_DELTA_W for k in ("inputLimit", "outputLimit"))
    except (TypeError, ValueError):
        return False


def check(cfg, client: ZendureClient, now_age: Optional[float] = None) -> str:
    """Ein Watchdog-Durchlauf; liefert ein kurzes Ergebnis (für Log/Tests)."""
    age = control_age_s() if now_age is None else now_age
    if age is not None and age < STALE_S:
        return "ok"
    since = "nie" if age is None else f"seit {age / 60:.0f} min"
    standby = _is_standby(client)
    if standby:
        log.info("Zendure-Watchdog: Regelung läuft nicht (%s) – Batterie steht bereits im Standby", since)
        return "already_standby"
    force_standby(client, f"Watchdog: Regelung läuft nicht ({since})", cfg.runtime.dry_run)
    return "standby"


def boot(cfg, client: ZendureClient, tries: int = BOOT_TRIES, wait_s: float = BOOT_WAIT_S,
         sleep: Callable[[float], None] = time.sleep) -> bool:
    """Pi-Start: Standby unbedingt schreiben (Netz/Gerät brauchen evtl. etwas)."""
    for i in range(tries):
        if force_standby(client, "Pi-Start", cfg.runtime.dry_run):
            return True
        sleep(wait_s)
    log.error("Zendure-Watchdog: Standby beim Start nach %d Versuchen nicht geschrieben", tries)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Zendure-Watchdog")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.json"))
    ap.add_argument("--boot", action="store_true", help="Pi-Start: Standby unbedingt schreiben")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    from main import setup_logging
    setup_logging(cfg.runtime.log_file)
    if not (cfg.zendure_active and cfg.zendure.control_enabled):
        return 0
    client = ZendureClient(cfg.zendure.ip, cfg.zendure.sn)
    if args.boot:
        return 0 if boot(cfg, client) else 1
    check(cfg, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
