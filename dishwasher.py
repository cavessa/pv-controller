"""Geschirrspüler (Bosch, Home Connect über openHAB): bei PV-Überschuss starten, spätestens zur Frist.

Ablauf: einräumen, Programm wählen, Fernstart am Gerät drücken. Der Cron-Lauf (``run``) liest die Items
und entscheidet (``decide``, reine Logik):

- **Freigabe** = Fernstart erlaubt an. Ab der ersten Minute mit Freigabe gilt sie als „scharf“ (``armed_at``);
  pro Freigabe geht **genau ein** Startbefehl raus (Command ``start`` an ``basic_actions_state``). Wechselt
  der Status nicht binnen ``CONFIRM_MIN`` auf Run → ``dw_start_failed``, kein zweiter Versuch. Fernstart
  aus → alles zurückgesetzt.
- **Startbereit**: Fernstart an, Tür zu, Programm gewählt, Status ``Ready``. ``DelayedStart`` (Startzeit am
  Gerät) oder selbst am Gerät gestartet → der Controller tut nichts.
- **Überschuss**: die letzten ``surplus_minutes`` vollen Minuten jeweils ≥ ``start_surplus_w``. Überschuss
  je Minute = Einspeisung (Minutenmittel Hauptzähler) + Batterie-Ladung − Entladung: die Batterie steckt
  zurück. Mit ``heater_yields`` zählt auch die Heizstab-Leistung (load_min), aber nur, solange der Speicher
  über der Min-Temperatur liegt (Stufe „bis Max“); in der Stufe „bis Min“ hat der Heizstab Vorrang.
- **Frist** je Tag: ``latest_start`` oder ``finish_by`` − gelernte Programmdauer. Freigabe erst nach der Frist
  des Tages → Zieltag ist morgen: dort Start bei Sonne, spätestens zur Frist (nie über Nacht). Verpasste
  Frist (Controller aus) wird bis ``CATCHUP_H`` h nachgeholt, danach zählt der nächste Tag.
- **Dauer lernen**: Run → Finished je Programm messen, Mittel der letzten 3 Läufe; Startwerte
  ``DEFAULT_MINUTES`` (Eco50 295 min, sonst 180 min).

In ``dry_run`` (runtime oder dishwasher) wird nie ein Befehl gesendet (``dw_start_simulated``).
Die Kaskade bekommt keinen eigenen Eintrag: die Leistung des laufenden Spülers wirkt über den Hauptzähler.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date as Date, datetime, time as Time, timedelta
from typing import Any, Optional

import db

log = logging.getLogger("pv-controller.dishwasher")

CONFIRM_MIN = 3                 # so lange darf es nach dem Startbefehl bis „Run“ dauern
CATCHUP_H = 2                   # verpasste Frist so lange nachholen
DEFAULT_MINUTES = {"Eco50": 295.0}
FALLBACK_MINUTES = 180.0
LEARN_LAST = 3
RUN_MIN_MIN, RUN_MAX_MIN = 20, 600   # plausible Laufzeiten fürs Lernen
RUNNING = ("Run", "Pause", "ActionRequired")
IDLE_AFTER_RUN = ("Finished", "Ready", "Inactive")

MISSING_DE = {"remote_start": "Fernstart nicht erlaubt", "door": "Tür offen", "program": "kein Programm gewählt",
              "state": "Gerät nicht bereit"}


# ── Hilfen ───────────────────────────────────────────────────────────────────

def program_key(program: Optional[str]) -> str:
    """„Dishcare.Dishwasher.Program.Eco50“ → „Eco50“."""
    return (program or "").rsplit(".", 1)[-1]


def _hm(t: str) -> Time:
    h, m = t.split(":")
    return Time(int(h), int(m))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _dt(s: Optional[str]) -> Optional[datetime]:
    return None if not s else datetime.fromisoformat(s)


def program_minutes(program: Optional[str], learned: dict[str, float]) -> float:
    key = program_key(program)
    return learned.get(key) or DEFAULT_MINUTES.get(key, FALLBACK_MINUTES)


def deadline_at(cfg, day: Date, minutes: float) -> datetime:
    """Spätester Start am Tag ``day``."""
    if cfg.deadline_mode == "finish_by":
        return datetime.combine(day, _hm(cfg.finish_by)) - timedelta(minutes=minutes)
    return datetime.combine(day, _hm(cfg.latest_start))


def target_day(cfg, armed_at: datetime, now: datetime, minutes: float) -> Date:
    """Tag, an dem gestartet wird: Freigabe nach der Frist → morgen; verpasste Frist + CATCHUP_H → weiter."""
    d = armed_at.date()
    if armed_at >= deadline_at(cfg, d, minutes):
        d += timedelta(days=1)
    while now > deadline_at(cfg, d, minutes) + timedelta(hours=CATCHUP_H):
        d += timedelta(days=1)
    return d


def surplus_ok(series: list[Optional[float]], cfg) -> bool:
    return (len(series) >= cfg.surplus_minutes
            and all(v is not None and v >= cfg.start_surplus_w for v in series[-cfg.surplus_minutes:]))


def _readiness(obs: dict[str, Any]) -> list[str]:
    missing = []
    if obs.get("remote_start") is not True:
        missing.append("remote_start")
    if obs.get("door") != "CLOSED":
        missing.append("door")
    if not obs.get("program"):
        missing.append("program")
    if obs.get("op") != "Ready":
        missing.append("state")
    return missing


def _fmt_remaining(s: Optional[float]) -> str:
    if s is None or s <= 0:
        return "?"
    m = int(round(s / 60))
    return f"{m // 60}:{m % 60:02d} h"


# ── Entscheidung (reine Logik) ───────────────────────────────────────────────

def decide(cfg, state: dict[str, Any], obs: Optional[dict[str, Any]], now: datetime,
           series: list[Optional[float]], learned: dict[str, float],
           heater_yields: bool = False) -> tuple[dict, dict, Optional[dict]]:
    """→ (neuer Zustand, Status {code, data, action?, by?}, gelernter Lauf {program, minutes} oder None).

    ``action == "start"`` heißt: jetzt genau einen Startbefehl senden (der Aufrufer setzt cmd_at).
    """
    st = dict(state)
    run_done: Optional[dict] = None
    if obs is None:
        return st, {"code": "dw_unreadable", "data": {}}, None
    op, prog = obs.get("op"), obs.get("program")

    # Laufzeit lernen (auch bei Start am Gerät)
    if op == "Run" and not st.get("run_start"):
        st["run_start"], st["run_program"] = _iso(now), program_key(obs.get("active_program") or prog)
    elif op in IDLE_AFTER_RUN and st.get("run_start"):
        minutes = (now - _dt(st["run_start"])).total_seconds() / 60
        if op == "Finished" and RUN_MIN_MIN <= minutes <= RUN_MAX_MIN and st.get("run_program"):
            run_done = {"program": st["run_program"], "minutes": round(minutes, 1), "start": st["run_start"]}
        st["run_start"] = st["run_program"] = None
    elif op in ("Error", "Aborting") and st.get("run_start"):
        st["run_start"] = st["run_program"] = None

    # Fernstart aus → Freigabe zurücksetzen
    if obs.get("remote_start") is not True:
        for k in ("armed_at", "cmd_at", "cmd_by", "simulated", "failed", "fail_detail", "no_auto"):
            st.pop(k, None)
        if op in RUNNING:
            return st, _running(obs), run_done
        return st, {"code": "dw_not_ready", "data": {"missing": [MISSING_DE["remote_start"]]}}, run_done

    if not st.get("armed_at"):
        st["armed_at"] = _iso(now)

    # Schon ein Startbefehl in dieser Freigabe
    if st.get("cmd_at"):
        if op in RUNNING:
            return st, _running(obs, st.get("cmd_by")), run_done
        if st.get("simulated"):
            return st, {"code": "dw_start_simulated", "data": {"by": st.get("cmd_by")}}, run_done
        if st.get("failed"):
            return st, {"code": "dw_start_failed", "data": {"detail": st.get("fail_detail") or "?"}}, run_done
        waited = (now - _dt(st["cmd_at"])).total_seconds() / 60
        if waited >= CONFIRM_MIN:
            st["failed"] = True
            st["fail_detail"] = f"Status nach {CONFIRM_MIN} min weiter „{op or '?'}“ statt „Run“"
            log.warning("Geschirrspüler: Startbefehl ohne Wirkung (%s) – kein zweiter Versuch", st["fail_detail"])
            return st, {"code": "dw_start_failed", "data": {"detail": st["fail_detail"]}}, run_done
        return st, {"code": "dw_start_sent", "data": {"cmd_time": st["cmd_at"][11:16], "by": st.get("cmd_by")}}, run_done

    if op in RUNNING:                                     # am Gerät gestartet
        return st, _running(obs, "device"), run_done
    if op == "DelayedStart":
        return st, {"code": "dw_delayed_start", "data": {}}, run_done

    missing = _readiness(obs)
    if missing:
        return st, {"code": "dw_not_ready", "data": {"missing": [MISSING_DE[m] for m in missing]}}, run_done

    minutes = program_minutes(prog, learned)
    day = target_day(cfg, _dt(st["armed_at"]), now, minutes)
    dl = deadline_at(cfg, day, minutes)
    data = {"deadline": dl.strftime("%H:%M"), "day": "heute" if day == now.date() else
            "morgen" if day == now.date() + timedelta(days=1) else day.strftime("%d.%m."),
            "threshold_w": int(cfg.start_surplus_w), "minutes": cfg.surplus_minutes,
            "program": program_key(prog), "duration_min": int(round(minutes)), "mode": cfg.deadline_mode,
            "heater_yields": heater_yields, "heater_note": " und Heizstab „bis Max“" if heater_yields else ""}
    if cfg.deadline_mode == "finish_by":
        data["finish_by"] = cfg.finish_by
    if st.get("no_auto"):
        return st, {"code": "dw_no_auto", "data": data}, run_done
    if now.date() < day:
        return st, {"code": "dw_waiting_tomorrow", "data": data}, run_done
    if now >= dl:
        return st, {"code": "dw_started_deadline", "data": data, "action": "start", "by": "deadline"}, run_done
    last = series[-1] if series else None
    data["surplus_w"] = None if last is None else int(round(last))
    if surplus_ok(series, cfg):
        data["surplus_w"] = int(round(min(series[-cfg.surplus_minutes:])))
        return st, {"code": "dw_started_surplus", "data": data, "action": "start", "by": "surplus"}, run_done
    return st, {"code": "dw_waiting_surplus", "data": data}, run_done


def _running(obs: dict[str, Any], by: Optional[str] = None) -> dict:
    return {"code": "dw_running", "data": {"program": program_key(obs.get("active_program") or obs.get("program")),
                                           "remaining": _fmt_remaining(obs.get("remaining_s")), "by": by or "?"}}


# ── openHAB ──────────────────────────────────────────────────────────────────

def _client(cfg):
    from clients.openhab_client import OpenHabClient
    return OpenHabClient(cfg.openhab_url, timeout=cfg.openhab_timeout_s, token=cfg.openhab_token)


def read(cfg, client=None) -> Optional[dict[str, Any]]:
    """Items lesen (ein REST-Abruf an openHAB, keiner an Bosch); None bei Fehler."""
    import consumers
    client = client or _client(cfg)
    try:
        states = client.get_states()
    except Exception as e:
        log.warning("Geschirrspüler: openHAB nicht lesbar (%s)", e)
        return None
    it = cfg.items

    def s(key: str) -> Optional[str]:
        v = states.get(it.get(key, ""))
        return None if v in (None, "NULL", "UNDEF") else str(v)

    rem = s("remaining")
    rem_s = consumers.parse_power(rem.replace(" s", "")) if rem and rem.endswith(" s") else None
    if rem and rem_s is None:
        try:
            rem_s = float(rem.split()[0]) * {"min": 60, "h": 3600}.get(rem.split()[-1], 1)
        except ValueError:
            rem_s = None
    return {"remote_start": {"ON": True, "OFF": False}.get(s("remote_start") or ""),
            "op": s("operation_state"), "program": s("selected_program"), "door": s("door"),
            "remaining_s": rem_s}


def send_start(cfg, client=None) -> None:
    """Genau ein Command ``start`` an das Aktions-Item (wirft bei Fehler)."""
    (client or _client(cfg)).send_command(cfg.items["action"], "start")


# ── Zustand / Messwerte ──────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS dishwasher_state (id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS dishwasher_runs (
        start TEXT PRIMARY KEY, program TEXT NOT NULL, minutes REAL NOT NULL)""")
    return conn


def load_state() -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute("SELECT json FROM dishwasher_state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def save_state(state: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO dishwasher_state (id, json) VALUES (1, ?)",
                     (json.dumps(state, ensure_ascii=False),))


def record_run(run: dict[str, Any]) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO dishwasher_runs (start, program, minutes) VALUES (?,?,?)",
                     (run["start"], run["program"], run["minutes"]))


def learned_minutes() -> dict[str, float]:
    """Mittel der letzten LEARN_LAST Läufe je Programm."""
    out: dict[str, float] = {}
    with _conn() as conn:
        progs = [r[0] for r in conn.execute("SELECT DISTINCT program FROM dishwasher_runs")]
        for p in progs:
            vals = [r[0] for r in conn.execute(
                "SELECT minutes FROM dishwasher_runs WHERE program = ? ORDER BY start DESC LIMIT ?", (p, LEARN_LAST))]
            out[p] = sum(vals) / len(vals)
    return out


def heater_yields_now(cfg, config, storage_temp_c: Optional[float]) -> bool:
    """Heizstab steckt zurück: Schalter an und Speicher über der Min-Temperatur (Stufe „bis Max“).

    Ohne „bis Min“-Stufe im Profil (Winter ohne Untergrenze) ist jede Heizstab-Leistung „bis Max“;
    Temperatur unbekannt → nein (vorsichtig).
    """
    if not cfg.heater_yields or storage_temp_c is None:
        return False
    season = "summer" if config.summer_mode.enabled else "winter"
    comfort = config.comfort_min_for(season)
    return comfort is None or storage_temp_c >= comfort


def surplus_series(now: datetime, minutes: int, with_heater: bool = False) -> list[Optional[float]]:
    """Überschuss der letzten ``minutes`` vollen Minuten (älteste zuerst): −Zähler + Laden − Entladen
    (+ Heizstab, wenn ``with_heater``)."""
    out: list[Optional[float]] = []
    with sqlite3.connect(db.DB_PATH, timeout=10) as conn:
        for i in range(minutes, 0, -1):
            ts = (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
            m = conn.execute("SELECT avg_w FROM main_meter_min WHERE ts = ?", (ts,)).fetchone()
            if m is None:
                out.append(None)
                continue
            z = conn.execute("SELECT charge_w, discharge_w FROM zendure_min WHERE ts = ?", (ts,)).fetchone()
            ch, dis = (z[0] or 0.0, z[1] or 0.0) if z else (0.0, 0.0)
            heat = 0.0
            if with_heater:
                h = conn.execute("SELECT heater_w FROM load_min WHERE ts = ?", (ts,)).fetchone()
                heat = (h[0] or 0.0) if h else 0.0
            out.append(-m[0] + ch - dis + heat)
    return out


# ── Ablauf ───────────────────────────────────────────────────────────────────

def _send(cfg, st: dict, by: str, now: datetime, dry_run: bool, client=None) -> dict:
    """Startbefehl (oder Simulation) ausführen und im Zustand vermerken; → Status-Ergänzung."""
    st["cmd_at"], st["cmd_by"] = _iso(now), by
    if dry_run:
        st["simulated"] = True
        log.info("Geschirrspüler: DRY-RUN – würde jetzt starten (%s), kein Befehl gesendet", by)
        return {"code": "dw_start_simulated", "data": {"by": by}}
    try:
        send_start(cfg, client)
        log.info("Geschirrspüler: Startbefehl gesendet (%s)", by)
        return {}
    except Exception as e:
        st["failed"] = True
        st["fail_detail"] = "Befehl an openHAB fehlgeschlagen"
        log.warning("Geschirrspüler: Startbefehl fehlgeschlagen: %s", e)
        return {"code": "dw_start_failed", "data": {"detail": st["fail_detail"]}}


def run(cfg, dry_run: bool, now: Optional[datetime] = None, client=None,
        heater_yields: bool = False) -> dict[str, Any]:
    """Ein Cron-Lauf: lesen, entscheiden, ggf. genau einmal starten, Zustand + Status speichern."""
    now = now or datetime.now()
    dry_run = dry_run or cfg.dry_run
    state = load_state()
    obs = read(cfg, client)
    series = surplus_series(now, cfg.surplus_minutes, heater_yields) if obs is not None else []
    st, status, run_done = decide(cfg, state, obs, now, series, learned_minutes(), heater_yields)
    if run_done:
        record_run(run_done)
        log.info("Geschirrspüler: Laufzeit %s = %.0f min gelernt", run_done["program"], run_done["minutes"])
    if status.get("action") == "start":
        status.update(_send(cfg, st, status["by"], now, dry_run, client))
    st["status"] = {"code": status["code"], "data": status.get("data") or {}, "ts": _iso(now),
                    "action": status.get("action"), "dry_run": dry_run}
    st["obs"] = obs
    save_state(st)
    return st["status"]


def start_now(cfg, dry_run: bool, now: Optional[datetime] = None, client=None) -> dict[str, Any]:
    """„Jetzt starten“ (Dashboard, mit Bestätigung): ohne Überschuss/Frist, aber nur wenn startbereit."""
    now = now or datetime.now()
    dry_run = dry_run or cfg.dry_run
    obs = read(cfg, client)
    if obs is None:
        return {"ok": False, "error": "openHAB nicht lesbar"}
    if obs.get("op") in RUNNING:
        return {"ok": False, "error": "läuft bereits"}
    missing = _readiness(obs)
    if missing:
        return {"ok": False, "error": "nicht startbereit: " + ", ".join(MISSING_DE[m] for m in missing)}
    st = load_state()
    st["armed_at"] = st.get("armed_at") or _iso(now)
    for k in ("failed", "fail_detail", "simulated"):
        st.pop(k, None)
    extra = _send(cfg, st, "manual", now, dry_run, client)
    code = extra.get("code") or "dw_started_manual"
    st["status"] = {"code": code, "data": extra.get("data") or {}, "ts": _iso(now), "action": "start",
                    "dry_run": dry_run}
    save_state(st)
    return {"ok": code != "dw_start_failed", "code": code, "dry_run": dry_run,
            "error": (extra.get("data") or {}).get("detail")}


def set_no_auto(on: bool) -> dict[str, Any]:
    """„Nicht automatisch starten“ für die laufende Freigabe (wird mit Fernstart aus zurückgesetzt)."""
    st = load_state()
    if on:
        st["no_auto"] = True
    else:
        st.pop("no_auto", None)
    save_state(st)
    return {"ok": True, "no_auto": bool(st.get("no_auto"))}
