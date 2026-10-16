"""Watchdog und Wallbox-Wächter für die Zendure-Regelung – eigener Cron-Eintrag, unabhängig von main.py.

zenSDK kennt keinen Heartbeat/keine Gültigkeitsdauer: ein geschriebener Sollwert gilt, bis
jemand einen neuen schreibt.

- Watchdog (einmal pro Minute): Regelung (zendure_ctl.decided_at) älter als ``STALE_S`` →
  Standby schreiben (inputLimit = outputLimit = 0), sofern das Gerät nicht schon steht.
- Wallbox-Wächter (Schleife bis kurz vor Minutenende, alle ``TICK_S``): Entlädt die Batterie
  laut Regelzustand oder Gerät und lädt die Wallbox (go-e car=2 oder > 100 W, nicht lesbar =
  lädt) → sofort Standby. So liefert der Akku beim Ladestart höchstens ein paar Sekunden mit.
- ``--boot`` (Cron ``@reboot``): Standby unbedingt, mit Wiederholung bis das Gerät antwortet.

Prozess-Regeln (Cron: ``flock -n /tmp/pvc-zendure-watch.lock`` → nie zwei Instanzen):
- ``/tmp/pvc-code.lock`` wird nur beim Start (Import + Config) geteilt gehalten, nicht in der
  Schleife. Jeder Takt prüft: hält deploy.sh den Lock exklusiv oder hat sich der Code-Stand
  (git HEAD) geändert → Schleife endet, der nächste Cron-Lauf startet mit neuem Code.
- Alle Schreibbefehle laufen über das gemeinsame Schreib-Lock (zendure_control.write_guard).

Nur aktiv mit ``zendure.enabled`` + ``zendure.control_enabled``; bei ``runtime.dry_run`` nur Log.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("pv-controller.zendure-watchdog")

ROOT = Path(__file__).resolve().parent
CODE_LOCK_PATH = "/tmp/pvc-code.lock"
STALE_S = 5 * 60
TICK_S = 3.0
LOOP_END_S = 58.0          # Schleife endet bei Sekunde 58 der Minute (Cron startet zur vollen Minute neu)
BOOT_TRIES = 12
BOOT_WAIT_S = 15.0


# ---- Code-Stand / Deploy ---------------------------------------------------------------

def code_head(root: Path = ROOT) -> str:
    """Aktueller Code-Stand (git HEAD, auch detached); leer, wenn nicht lesbar."""
    try:
        gd = root / ".git"
        if gd.is_file():                                   # Worktree: "gitdir: …"
            gd = Path(gd.read_text().split(":", 1)[1].strip())
        head = (gd / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            p = gd / ref
            return p.read_text().strip() if p.exists() else ref
        return head
    except OSError:
        return ""


def deploy_running(path: str = CODE_LOCK_PATH) -> bool:
    """True, wenn deploy.sh den Code-Lock gerade exklusiv hält (geteilt nicht zu bekommen)."""
    try:
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def _shared_code_lock(wait_s: float = 50.0):
    """Code-Lock geteilt halten (nur für Import + Config beim Start); liefert die offene Datei."""
    f = open(CODE_LOCK_PATH, "a+")
    end = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() >= end:
                f.close()
                return None
            time.sleep(0.2)


# ---- Watchdog (Regelung läuft nicht) ---------------------------------------------------

def _is_standby(client) -> Optional[bool]:
    """True = Gerät meldet beide Limits ~0; None = nicht lesbar."""
    from zendure_control import WRITE_DELTA_W
    st = client.get_status()
    if st is None:
        return None
    try:
        return all(abs(float(st.props.get(k, 0) or 0)) <= WRITE_DELTA_W for k in ("inputLimit", "outputLimit"))
    except (TypeError, ValueError):
        return False


def check(cfg, client, now_age: Optional[float] = None) -> str:
    """Ein Watchdog-Durchlauf; liefert ein kurzes Ergebnis (für Log/Tests)."""
    from zendure_control import control_age_s, force_standby
    age = control_age_s() if now_age is None else now_age
    if age is not None and age < STALE_S:
        return "ok"
    since = "nie" if age is None else f"seit {age / 60:.0f} min"
    if _is_standby(client):
        log.info("Zendure-Watchdog: Regelung läuft nicht (%s) – Batterie steht bereits im Standby", since)
        return "already_standby"
    force_standby(client, f"Watchdog: Regelung läuft nicht ({since})", cfg.runtime.dry_run)
    return "standby"


def boot(cfg, client, tries: int = BOOT_TRIES, wait_s: float = BOOT_WAIT_S,
         sleep: Callable[[float], None] = time.sleep) -> bool:
    """Pi-Start: Standby unbedingt schreiben (Netz/Gerät brauchen evtl. etwas)."""
    from zendure_control import force_standby
    for _ in range(tries):
        if force_standby(client, "Pi-Start", cfg.runtime.dry_run):
            return True
        sleep(wait_s)
    log.error("Zendure-Watchdog: Standby beim Start nach %d Versuchen nicht geschrieben", tries)
    return False


# ---- Wallbox-Wächter -------------------------------------------------------------------

def guard_tick(cfg, client, goe, device_discharging: bool = False, state: Optional[dict] = None) -> str:
    """Ein Takt: entlädt die Batterie und lädt die Wallbox (oder go-e nicht lesbar) → Standby.

    ``device_discharging``: Gerät meldete beim Minutenstart outputLimit > 0 (z. B. nach
    App-Eingriff), auch wenn der Regelzustand nicht „discharge“ ist."""
    import db
    from clients.goe_client import charging_now
    from zendure_control import force_standby
    state = {} if state is None else state
    try:
        mode = db.get_zendure_ctl().get("mode")
    except Exception:
        mode = None                                       # DB nicht lesbar → vorsichtshalber prüfen
    if mode != "discharge" and not (device_discharging and not state.get("stopped")) and mode is not None:
        return "idle"
    if not charging_now(goe):
        return "ok"
    if cfg.runtime.dry_run and state.get("stopped"):
        return "standby"                                  # dry_run: nur einmal pro Lauf loggen
    force_standby(client, "Wächter: Wallbox lädt – Entladen sofort gestoppt", cfg.runtime.dry_run)
    state["stopped"] = True
    return "standby"


def guard_loop(cfg, client, goe, *, end_at: float, tick_s: float = TICK_S,
               stop: Callable[[], Optional[str]] = lambda: None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> list[str]:
    """Wächter-Schleife bis ``end_at`` (Unix-Zeit); ``stop()`` → Grund zum Beenden oder None."""
    st = client.get_status()
    device_discharging = False
    if st is not None:
        try:
            device_discharging = float(st.props.get("outputLimit", 0) or 0) > 0
        except (TypeError, ValueError):
            device_discharging = False
    state: dict = {}
    out: list[str] = []
    while clock() < end_at:
        why = stop()
        if why:
            log.info("Zendure-Wächter beendet: %s", why)
            out.append("stop")
            break
        try:
            out.append(guard_tick(cfg, client, goe, device_discharging, state))
        except Exception:
            log.exception("Zendure-Wächter: Fehler im Takt")
            out.append("error")
        sleep(max(0.0, min(tick_s, end_at - clock())))
    return out


# ---- Einstieg --------------------------------------------------------------------------

def _setup_logging(log_file: str) -> None:
    """Leichtgewichtig (ohne main.py-Import), gleiches Format und gleiche Datei wie main.py."""
    p = Path(log_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(p, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(h)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Zendure-Watchdog und Wallbox-Wächter")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--boot", action="store_true", help="Pi-Start: Standby unbedingt schreiben")
    ap.add_argument("--once", action="store_true", help="nur Watchdog-Prüfung, keine Wächter-Schleife")
    args = ap.parse_args(argv)
    started = time.time()

    lock = _shared_code_lock()                            # nur für Import + Config
    if lock is None:
        return 0                                          # Deploy läuft: nächste Minute
    try:
        from clients.goe_client import BATTERY_CHECK_TIMEOUT_S, GoeClient
        from clients.zendure_client import ZendureClient
        from config import load_config
        cfg = load_config(args.config)
        head = code_head()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    _setup_logging(cfg.runtime.log_file)
    if not (cfg.zendure_active and cfg.zendure.control_enabled):
        return 0
    client = ZendureClient(cfg.zendure.ip, cfg.zendure.sn)
    if args.boot:
        return 0 if boot(cfg, client) else 1
    check(cfg, client)
    if args.once or not cfg.wallbox.url:
        return 0
    goe = GoeClient(cfg.wallbox.url, BATTERY_CHECK_TIMEOUT_S)
    end_at = started - (started % 60) + LOOP_END_S

    def stop() -> Optional[str]:
        if deploy_running():
            return "Deploy läuft"
        if code_head() != head:
            return "Code-Stand geändert"
        return None

    guard_loop(cfg, client, goe, end_at=end_at, stop=stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
