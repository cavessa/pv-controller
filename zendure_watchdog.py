"""Watchdog und Wallbox-Wächter für die Zendure-Regelung – eigener Cron-Eintrag, unabhängig von main.py.

zenSDK kennt keinen Heartbeat/keine Gültigkeitsdauer: ein geschriebener Sollwert gilt, bis
jemand einen neuen schreibt.

- Watchdog (einmal pro Minute): Regelung (zendure_ctl.decided_at) älter als ``STALE_S`` →
  Standby schreiben (inputLimit = outputLimit = 0), sofern das Gerät nicht schon steht.
- Wallbox-Wächter (Schleife bis kurz vor Minutenende, alle ``TICK_S``): Entlädt die Batterie
  laut Regelzustand oder Gerät und lädt die Wallbox (go-e car=2 oder > 100 W, nicht lesbar =
  lädt) → sofort Standby. So liefert der Akku beim Ladestart höchstens ein paar Sekunden mit.
- Fremdregler-Wächter (gleicher Takt, liest das Gerät jeden Takt, ~30–130 ms): Weichen die
  Gerätewerte in zwei Takten hintereinander vom Regelzustand ab, ohne dass pvcontroller in den
  letzten ``FOREIGN_GRACE_S`` geschrieben hat (App-Energieplan/HEMS), → Hinweis + sofort
  Standby (höchstens alle ``FOREIGN_STANDBY_MIN_S``).
- Lade-Drosselung (gleicher Takt): lädt die Batterie und misst der Hauptzähler zwei Takte in
  Folge Netzbezug > ``THROTTLE_IMPORT_W`` → Ladesollwert sofort um den Bezug senken (bis 0).
  Nur senken, nie erhöhen (das macht der Minutenlauf); Zähler nicht lesbar → nichts tun.
  Fälle: Heizstab-Thermostat schließt wieder, Wolke, Herd an.
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
FOREIGN_TICKS = 2           # so viele Takte in Folge, bevor ein Fremdeingriff gilt
FOREIGN_STANDBY_MIN_S = 30.0
THROTTLE_IMPORT_W = 100.0   # Netzbezug, ab dem das Laden sofort gesenkt wird
THROTTLE_TICKS = 2
THROTTLE_SETTLE_S = 6.0     # nach einer Senkung so lange warten (Gerät stellt in < 5 s um)
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
    if goe is None or not charging_now(goe):              # keine Wallbox konfiguriert: nichts zu sperren
        return "ok"
    if cfg.runtime.dry_run and state.get("stopped"):
        return "standby"                                  # dry_run: nur einmal pro Lauf loggen
    force_standby(client, "Wächter: Wallbox lädt – Entladen sofort gestoppt", cfg.runtime.dry_run)
    state["stopped"] = True
    return "standby"


def foreign_tick(cfg, client, st, state: dict, clock: Callable[[], float] = time.time) -> Optional[str]:
    """Fremdregler: Gerät weicht ``FOREIGN_TICKS`` Takte in Folge vom Regelzustand ab, ohne eigenen
    Befehl in den letzten ``FOREIGN_GRACE_S`` → Hinweis + Standby. None = nichts erkannt."""
    import db
    from zendure_control import FOREIGN_GRACE_S, _age_s, force_standby, foreign_deviation, record_foreign
    if st is None or cfg.runtime.dry_run:                  # dry_run: Regelzustand ≠ Gerät (schreibt nie)
        state.pop("foreign_n", None)
        return None
    ctl = db.get_zendure_ctl()
    age = _age_s(ctl.get("written_at"))
    dev = "" if age is not None and age < FOREIGN_GRACE_S else foreign_deviation(
        st, ctl["mode"], ctl["limit_w"], target_soc=cfg.zendure.target_soc, min_soc=cfg.zendure.min_soc)
    key = (ctl["mode"], ctl["limit_w"])
    if not dev or state.get("foreign_key") != key:
        state["foreign_key"], state["foreign_n"] = key, (1 if dev else 0)
        return None
    state["foreign_n"] = state.get("foreign_n", 0) + 1
    if state["foreign_n"] < FOREIGN_TICKS:
        return None
    record_foreign(dev, "Wächter")
    now = clock()
    if now - state.get("foreign_standby_at", -1e18) < FOREIGN_STANDBY_MIN_S:
        return "foreign"
    state["foreign_standby_at"] = now
    force_standby(client, f"Fremdregler aktiv ({dev}) – HEMS in der Zendure-App prüfen", cfg.runtime.dry_run)
    state["foreign_n"] = 0
    return "foreign_standby"


def throttle_tick(cfg, client, meter, state: dict, clock: Callable[[], float] = time.time) -> Optional[str]:
    """Lädt die Batterie und bezieht das Haus Strom → Ladesollwert sofort um den Bezug senken.

    ``meter``: Hauptzähler (``get_total_power_w()``, + = Bezug, None = nicht lesbar). Liefert
    "throttle" nach einer Senkung, sonst None."""
    import db
    from clients.zendure_client import AC_MODE_CHARGE
    from zendure_control import _round10, write_guard
    if meter is None:
        return None
    ctl = db.get_zendure_ctl()
    if ctl["mode"] != "charge" or not ctl["limit_w"]:
        state.pop("imports", None)
        return None
    now = clock()
    if now < state.get("throttle_settle_until", 0.0):
        return None
    try:
        grid = meter.get_total_power_w()
    except Exception:
        grid = None
    if grid is None or grid <= THROTTLE_IMPORT_W:
        state.pop("imports", None)                         # nicht lesbar oder kein Bezug: nichts tun
        return None
    imports = state.setdefault("imports", [])
    imports.append(float(grid))
    if len(imports) < THROTTLE_TICKS:
        return None
    cut = min(imports[-THROTTLE_TICKS:])                   # nur den anhaltenden Bezug abziehen
    state["imports"] = []
    old = int(ctl["limit_w"])
    new = max(0, _round10(old - cut))
    if new >= old:
        return None
    if cfg.runtime.dry_run:
        log.info("[Dry-Run] Zendure-Wächter: Netzbezug %.0f W – würde Laden von %d auf %d W senken", cut, old, new)
        state["throttle_settle_until"] = now + THROTTLE_SETTLE_S
        return "throttle"
    with write_guard() as locked:
        ctl = db.get_zendure_ctl()                         # unter dem Lock neu: Minutenlauf könnte geschrieben haben
        if not locked or ctl["mode"] != "charge" or int(ctl["limit_w"] or 0) != old:
            return None
        ok = client.set_mode(AC_MODE_CHARGE, input_limit=new, output_limit=0)
        if ok:
            from datetime import datetime
            db.set_zendure_ctl(limit_w=new, written_at=datetime.now().isoformat(timespec="seconds"))
    state["throttle_settle_until"] = now + THROTTLE_SETTLE_S
    if ok:
        log.info("Zendure-Wächter: Netzbezug %.0f W beim Laden – Ladung sofort von %d auf %d W gesenkt", cut, old, new)
        return "throttle"
    log.warning("Zendure-Wächter: Senken des Ladens auf %d W fehlgeschlagen", new)
    return None


def _out_limit(st) -> float:
    try:
        return float(st.props.get("outputLimit", 0) or 0) if st is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def guard_loop(cfg, client, goe, *, end_at: float, tick_s: float = TICK_S,
               stop: Callable[[], Optional[str]] = lambda: None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
               meter=None) -> list[str]:
    """Wächter-Schleife bis ``end_at`` (Unix-Zeit); ``stop()`` → Grund zum Beenden oder None.

    Liest das Gerät in jedem Takt (Wallbox-Wächter: entlädt es gerade? Fremdregler-Wächter)."""
    state: dict = {}
    out: list[str] = []
    while clock() < end_at:
        why = stop()
        if why:
            log.info("Zendure-Wächter beendet: %s", why)
            out.append("stop")
            break
        try:
            st = client.get_status()
            res = guard_tick(cfg, client, goe, _out_limit(st) > 0, state)
            if res != "standby":
                res = foreign_tick(cfg, client, st, state, clock) or res
            if res not in ("standby", "foreign_standby"):
                res = throttle_tick(cfg, client, meter, state, clock) or res
            out.append(res)
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
    if args.once:
        return 0
    from clients.shelly_client import Shelly3EMClient
    goe = GoeClient(cfg.wallbox.url, BATTERY_CHECK_TIMEOUT_S) if cfg.wallbox.url else None
    meter = Shelly3EMClient(cfg.shelly.main_meter_url, 1.5, name="main_meter",
                            type_=cfg.shelly.main_meter_type) if cfg.shelly.main_meter_url else None
    end_at = started - (started % 60) + LOOP_END_S

    def stop() -> Optional[str]:
        if deploy_running():
            return "Deploy läuft"
        if code_head() != head:
            return "Code-Stand geändert"
        return None

    guard_loop(cfg, client, goe, end_at=end_at, stop=stop, meter=meter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
