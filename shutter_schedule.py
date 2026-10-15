"""Rollläden Phase 2: Zeitpläne (feste Uhrzeit oder Sonnenauf-/-untergang ± Offset), ausgeführt im Minuten-Cron.

Tabelle shutter_schedule (in pvcontroller.db):
  kind = "time" (time = "HH:MM") | "sunrise" | "sunset" (offset_min, optional not_before/not_after "HH:MM")
  action = "up" | "down" | "position" (position 0–100, 0 = offen, 100 = zu)
  wiederkehrend: days = "mo,di,…", date = NULL; einmalig: days = NULL, date = "YYYY-MM-DD"
Sonnenzeiten werden lokal aus latitude/longitude der Config berechnet (NOAA-Formel, ±1 min), Wanduhrzeit
Europe/Berlin – die Zeitumstellung ergibt sich aus der Umrechnung von UTC.

Nur Rollläden mit ``automation = "pvcontroller"`` werden gefahren; für „openHAB-Regel“ bleibt openHAB zuständig.

Ablauf je Minute (``run``), nach PV-Steuerung und Heizungs-Zeitplänen:
1. Rollläden oder Schalter „Rollladen-Zeitpläne aktiv“ aus → nichts, auch kein DB-Zugriff.
2. Nur DB: fällige Einträge, wartende Türen (retry_when_closed), fällige Rückprüfungen. Nichts davon → Ende,
   **kein** openHAB-Request.
3. > 15 min verspätet → übersprungen (Log); mehrere je Rollladen → nur der späteste.
4. Positionen, Kontakte, Außentemperatur und Duty Cycle in **einem** Request.
5. Je Rollladen, in dieser Reihenfolge (im Zweifel: nicht fahren):
   - Ist-Position aus ``position_source`` (Shelly direkt / Lese-Item); fährt der Rollladen gerade → nächste
     Minute erneut;
   - schon in Zielposition → nichts senden (nur wenn die Position verlässlich ist; sonst – nach einem STOP,
     bei ausstehender Rückmeldung oder ausgefallener Quelle – gilt sie als unbekannt und alle Regeln greifen
     in beide Richtungen);
   - Schließen bei Tür mit offenem/unbekanntem Kontakt → übersprungen (Log + Hinweis auf der Card); mit
     ``retry_when_closed`` wird nachgeholt, sobald der Kontakt „zu“ meldet (höchstens 12 h, ein neuerer
     Eintrag ersetzt das Warten);
   - Schließen bei Tür ohne Kontakt-Item → höchstens bis ``auto_close_max`` (Default 80 %, Spalt);
   - Öffnen bei Außentemperatur unter ``frost_temp_c`` oder unbekannter Temperatur → übersprungen (Festfrieren);
   - in den letzten ``manual_hold_min`` von Hand bewegt (Positionsänderung in der openHAB-Persistence ohne
     eigenen Befehl) → übersprungen; Verlauf nicht lesbar → ebenfalls übersprungen.
6. Duty Cycle über der Grenze → nichts senden, Einträge bleiben fällig (bis zum Nachhol-Fenster).
7. Senden gestaffelt (``command_spacing_s``); ``dry_run`` → nur loggen. Gesendete Befehle landen in
   ``shutter_command`` (Rückprüfung wie bei der manuellen Bedienung, kein automatisches Nachsenden).
Ein Zeitbudget (Default 20 s) begrenzt den Teil; was nicht mehr passt, bleibt fällig.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import db
import decisions
import schedule_log
import shutters
from clients.openhab_client import OpenHabError
import heating
from heating_schedule import DAYS, _Budget, applies_on
from suntime import (KINDS, OFFSET_MAX, TZ, describe, due_instant, instant, location,  # noqa: F401
                     sun_times, validate_when)

log = logging.getLogger("pv-controller.shutters")

CATCH_UP_MIN = 15
BUDGET_S = 20.0
MIN_REQUEST_S = 0.5
DOOR_WAIT_H = 12                     # retry_when_closed: so lange auf die geschlossene Tür warten
MOVE_THRESHOLD = 5                   # Positionsänderung ab 5 % zählt als Bewegung (HmIP meldet 98/99/100)
OWN_SLACK_S = 180                    # eigene Befehle: Änderungen bis verify_after + 3 min gehören dazu
ACTIONS = ("up", "down", "position")

SENT, UNCHANGED, DRY_RUN, SKIPPED, SUPERSEDED, FAILED = (
    "sent", "unchanged", "dry_run", "skipped", "superseded", "failed")
RETRY = "retry"
DOOR_OPEN, WAITING_DOOR, FROST, MANUAL_HOLD, NO_SUN = "door_open", "waiting_door", "frost", "manual_hold", "no_sun"
HINT_RESULTS = (DOOR_OPEN, WAITING_DOOR, FROST, MANUAL_HOLD, NO_SUN, SKIPPED, FAILED)


def _utc(dt: datetime) -> datetime:
    """Für Vergleiche: aware datetimes mit derselben tzinfo vergleicht Python als Wanduhrzeit (fold wird
    ignoriert) – in der doppelten Stunde im Oktober 1 h daneben. In UTC stimmt es."""
    return dt.astimezone(timezone.utc)


def _elapsed(later: datetime, earlier: datetime) -> timedelta:
    """Echte Dauer (über UTC, siehe ``_utc``)."""
    return _utc(later) - _utc(earlier)


def target_label(action: str, position: Optional[int]) -> str:
    return {"up": "auf", "down": "zu"}.get(action) or f"{position} %"


def _pos_label(target: int) -> str:
    return {0: "auf", 100: "zu"}.get(target) or f"{target} %"


# ── DB ──────────────────────────────────────────────────────────────────────


_initialized: set[str] = set()


def _conn() -> sqlite3.Connection:
    if str(db.DB_PATH) not in _initialized:
        init_tables()
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    _initialized.add(str(db.DB_PATH))
    with sqlite3.connect(db.DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shutter_schedule (
                id           INTEGER PRIMARY KEY,
                shutter_id   TEXT NOT NULL,
                action       TEXT NOT NULL,
                position     INTEGER,
                kind         TEXT NOT NULL,
                time         TEXT,
                offset_min   INTEGER NOT NULL DEFAULT 0,
                not_before   TEXT,
                not_after    TEXT,
                days         TEXT,
                date         TEXT,
                enabled      INTEGER DEFAULT 1,
                source       TEXT,
                note         TEXT,
                last_run     TEXT,
                last_result  TEXT,
                last_message TEXT,
                created_at   TEXT
            )
            """
        )
        # retry_when_closed: übersprungenes Schließen wartet auf die geschlossene Tür
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shutter_door_wait (
                shutter_id  TEXT PRIMARY KEY,
                schedule_id INTEGER NOT NULL,
                target      INTEGER NOT NULL,
                since       TEXT NOT NULL
            )
            """
        )


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["days"] = d["days"].split(",") if d.get("days") else []
    d["enabled"] = bool(d["enabled"])
    d["when"] = describe(d)
    return d


def list_schedules(shutter_id: Optional[str] = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM shutter_schedule", ()
    if shutter_id:
        sql, args = sql + " WHERE shutter_id = ?", (shutter_id,)
    with _conn() as conn:
        return [_row(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def get_schedule(sid: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM shutter_schedule WHERE id = ?", (sid,)).fetchone()
    return _row(r) if r else None


def validate_entry(cfg: shutters.ShutterConfig, shutter_id: str, action: str, kind: str,
                   position: Any = None, time: Any = None, offset_min: Any = 0,
                   not_before: Any = None, not_after: Any = None,
                   days: Optional[list[str]] = None, date: Optional[str] = None,
                   loc: Optional[tuple[float, float]] = None, now: Optional[datetime] = None,
                   any_automation: bool = False) -> dict[str, Any]:
    """Prüft einen Eintrag; ValueError mit Klartext. Liefert die DB-Felder.

    ``any_automation``: auch für Rollläden mit „Automatik: openHAB-Regel“ zulassen (Formular im Tab) – solche
    Einträge werden gespeichert, aber erst nach der Umstellung auf „PV Controller“ ausgeführt.
    """
    sh = cfg.shutter(shutter_id)
    if sh is None:
        raise ValueError(f"Rollladen {shutter_id!r} unbekannt")
    if sh.automation != "pvcontroller" and not any_automation:
        raise ValueError(f"{sh.name} wird von einer openHAB-Regel gefahren (Automatik in den Settings umstellen)")
    if action not in ACTIONS:
        raise ValueError("Ziel muss auf, zu oder eine Position sein")
    pos = None
    if action == "position":
        pos = shutters.validate_command(sh, "position", position)
    fields: dict[str, Any] = {"shutter_id": shutter_id, "action": action, "position": pos,
                              **validate_when(kind, time, offset_min, not_before, not_after, loc)}
    days = [d.strip().lower() for d in (days or []) if d and d.strip()]
    if days and date:
        raise ValueError("Entweder Wochentage oder ein Datum, nicht beides")
    if days:
        bad = [d for d in days if d not in DAYS]
        if bad:
            raise ValueError(f"Unbekannte Wochentage: {', '.join(bad)}")
        fields.update(days=",".join(d for d in DAYS if d in days), date=None)
        return fields
    if not date:
        raise ValueError("Wochentage oder Datum angeben")
    try:
        d = Date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError("Datum muss YYYY-MM-DD sein")
    now = (now or datetime.now(TZ)).astimezone(TZ)
    at = instant({**fields, "days": None, "date": d.isoformat()}, d, loc)
    if at is None or _utc(at) <= _utc(now):
        raise ValueError("Zeitpunkt liegt in der Vergangenheit")
    fields.update(days=None, date=d.isoformat())
    return fields


def create_schedule(fields: dict[str, Any], source: str = "ui", note: str = "",
                    now: Optional[datetime] = None) -> dict[str, Any]:
    created = (now or datetime.now(TZ)).astimezone(TZ).isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO shutter_schedule (shutter_id, action, position, kind, time, offset_min, not_before, "
            "not_after, days, date, enabled, source, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (fields["shutter_id"], fields["action"], fields["position"], fields["kind"], fields["time"],
             fields["offset_min"], fields["not_before"], fields["not_after"], fields["days"], fields["date"],
             source, note or None, created))
        sid = cur.lastrowid
    return get_schedule(sid)


def update_schedule(sid: int, fields: dict[str, Any], now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Eintrag bearbeiten. Gilt wie neu angelegt: heute nicht nachholen, wenn der neue Zeitpunkt schon vorbei ist."""
    stamp = (now or datetime.now(TZ)).astimezone(TZ).isoformat(timespec="seconds")
    with _conn() as conn:
        n = conn.execute(
            "UPDATE shutter_schedule SET shutter_id = ?, action = ?, position = ?, kind = ?, time = ?, offset_min = ?, "
            "not_before = ?, not_after = ?, days = ?, date = ?, enabled = 1, created_at = ?, last_run = NULL, "
            "last_result = NULL, last_message = NULL WHERE id = ?",
            (fields["shutter_id"], fields["action"], fields["position"], fields["kind"], fields["time"],
             fields["offset_min"], fields["not_before"], fields["not_after"], fields["days"], fields["date"],
             stamp, sid)).rowcount
        conn.execute("DELETE FROM shutter_door_wait WHERE schedule_id = ?", (sid,))
    return get_schedule(sid) if n else None


def set_enabled(sid: int, enabled: bool) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        n = conn.execute("UPDATE shutter_schedule SET enabled = ? WHERE id = ?", (int(enabled), sid)).rowcount
        if not enabled:
            conn.execute("DELETE FROM shutter_door_wait WHERE schedule_id = ?", (sid,))
    return get_schedule(sid) if n else None


def delete_schedule(sid: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM shutter_door_wait WHERE schedule_id = ?", (sid,))
        return conn.execute("DELETE FROM shutter_schedule WHERE id = ?", (sid,)).rowcount > 0


def door_waits() -> dict[str, dict[str, Any]]:
    with _conn() as conn:
        return {r["shutter_id"]: dict(r) for r in conn.execute("SELECT * FROM shutter_door_wait")}


def _set_wait(shutter_id: str, sid: int, target: int, now: datetime) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO shutter_door_wait (shutter_id, schedule_id, target, since) "
                     "VALUES (?, ?, ?, ?)", (shutter_id, sid, target, now.isoformat(timespec="seconds")))


def clear_wait(shutter_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM shutter_door_wait WHERE shutter_id = ?", (shutter_id,))


def _mark(ids: list[int], result: str, at: datetime, message: Optional[str] = None) -> None:
    """Ergebnis merken; einmalige Einträge sind danach erledigt (enabled = 0)."""
    if not ids:
        return
    ph = ",".join("?" for _ in ids)
    with _conn() as conn:
        conn.execute(f"UPDATE shutter_schedule SET last_run = ?, last_result = ?, last_message = ? WHERE id IN ({ph})",
                     (at.isoformat(timespec="seconds"), result, message, *ids))
        conn.execute(f"UPDATE shutter_schedule SET enabled = 0 WHERE id IN ({ph}) AND date IS NOT NULL", ids)


def _set_result(sid: int, result: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE shutter_schedule SET last_result = ? WHERE id = ?", (result, sid))


# ── Zeit ────────────────────────────────────────────────────────────────────


def _ran_on(entry: dict[str, Any], day: Date) -> bool:
    lr = entry.get("last_run")
    if not lr:
        return False
    try:
        return datetime.fromisoformat(lr).astimezone(TZ).date() == day
    except ValueError:
        return False


def due_entries(entries: list[dict[str, Any]], now: datetime,
                loc: Optional[tuple[float, float]]) -> list[tuple[dict[str, Any], Optional[datetime]]]:
    """Heute fällige, noch nicht gelaufene Einträge → [(Eintrag, Fälligkeit)]; None = Sonnenzeit nicht berechenbar."""
    now = now.astimezone(TZ)
    today = now.date()
    out = []
    for e in entries:
        if not e["enabled"] or not applies_on(e, today) or _ran_on(e, today):
            continue
        at = instant(e, today, loc)
        if at is None:
            out.append((e, None))
            continue
        if _utc(at) > _utc(now):
            continue
        created = e.get("created_at")
        if created:
            try:
                if _utc(datetime.fromisoformat(created)) > _utc(at):
                    continue        # erst nach dem Zeitpunkt angelegt → heute nicht nachholen
            except ValueError:
                pass
        out.append((e, at))
    return out


def next_rides(entries: list[dict[str, Any]], now: datetime, loc: Optional[tuple[float, float]],
               horizon_days: int = 7) -> dict[str, dict]:
    """Nächste Fahrt je Rollladen: {shutter_id: {at, action, position, when, id}}."""
    now = now.astimezone(TZ)
    out: dict[str, dict] = {}
    for offset in range(horizon_days + 1):
        day = now.date() + timedelta(days=offset)
        for e in entries:
            if not e["enabled"] or not applies_on(e, day) or (offset == 0 and _ran_on(e, day)):
                continue
            at = instant(e, day, loc)
            if at is None or _utc(at) <= _utc(now):
                continue
            cur = out.get(e["shutter_id"])
            if cur is None or _utc(at) < _utc(cur["_at"]):
                out[e["shutter_id"]] = {"_at": at, "at": at.isoformat(timespec="minutes"), "action": e["action"],
                                        "position": e["position"], "when": describe(e), "id": e["id"]}
    for v in out.values():
        v.pop("_at")
    return out


def hints(now: datetime) -> dict[str, str]:
    """Letzter übersprungener/gescheiterter Zeitplan-Eintrag je Rollladen (12 h) bzw. wartende Tür → Card."""
    since = (now - timedelta(hours=12)).isoformat(timespec="seconds")
    out: dict[str, str] = {}
    ph = ",".join("?" for _ in HINT_RESULTS)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT shutter_id, last_message, last_run FROM shutter_schedule WHERE last_run >= ? "
            f"AND last_result IN ({ph}) AND last_message IS NOT NULL ORDER BY last_run", (since, *HINT_RESULTS))
        for r in rows:
            out[r["shutter_id"]] = r["last_message"]
    for sid, w in door_waits().items():
        out[sid] = f"Wartet auf geschlossene Tür (Zeitplan seit {w['since'][11:16]} → {w['target']} %)"
    return out


# ── Manuelle Bedienung erkennen ─────────────────────────────────────────────


def manual_change(points: list[tuple[int, float]], own: list[tuple[datetime, int]], since: datetime) -> Optional[datetime]:
    """Letzte Positionsänderung ab ``since``, die keinem eigenen Befehl zugeordnet werden kann.

    points: openHAB-Persistence [(epoch_ms, position)], aufsteigend. own: [(gesendet, verify_after_s)].
    Eine Änderung gehört zu einem eigenen Befehl, wenn sie zwischen „gesendet − 60 s“ und
    „gesendet + verify_after + 3 min“ liegt (autoupdate, Zwischenmeldungen, HmIP-5-min-Raster).
    """
    last: Optional[datetime] = None
    prev: Optional[float] = None
    for t_ms, v in points:
        if prev is not None and abs(v - prev) >= MOVE_THRESHOLD:
            t = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
            mine = any(_utc(s) - timedelta(seconds=60) <= t <= _utc(s) + timedelta(seconds=va + OWN_SLACK_S)
                       for s, va in own)
            if t >= _utc(since) and not mine:
                last = t.astimezone(TZ)
        prev = v
    return last


# ── Ausführung im Cron ──────────────────────────────────────────────────────


@dataclass
class RunResult:
    active: bool = False
    due: int = 0
    requests: int = 0
    sent: int = 0
    skipped: int = 0
    timed_out: bool = False
    duration_s: float = 0.0


def _weather_min_today(now: datetime) -> Optional[float]:
    """Tages-Minimum der Prognose (weather_log), Ersatz wenn kein frost_temp_item konfiguriert ist."""
    try:
        with sqlite3.connect(db.DB_PATH, timeout=10) as conn:
            r = conn.execute("SELECT temp_min FROM weather_log WHERE date = ?", (now.date().isoformat(),)).fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except sqlite3.Error:
        return None


def run(raw: dict[str, Any], dry_run: bool, now: Optional[datetime] = None, client: Any = None,
        budget_s: float = BUDGET_S, clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
        app_log: Optional[Callable[[str, int], None]] = None) -> RunResult:
    """Ein Minuten-Durchlauf. Wirft nichts außer echten Programmfehlern (Aufrufer fängt alles)."""
    t0 = clock()
    res = RunResult()
    emit = app_log or (lambda msg, level=logging.INFO: log.log(level, msg))
    cfg = shutters.parse_shutters(raw)
    if not (cfg.enabled and cfg.schedules_enabled):
        if cfg.enabled:
            _note_not_run(cfg, raw, (now or datetime.now(TZ)).astimezone(TZ), "skipped_schedules_off", lambda sh: True)
        return res
    res.active = True
    now = (now or datetime.now(TZ)).astimezone(TZ)
    budget = _Budget(budget_s, clock)
    loc = location(raw)

    def auto(sid: str) -> Optional[shutters.Shutter]:
        sh = cfg.shutter(sid)
        return sh if sh and sh.automation == "pvcontroller" else None

    def note(e: dict, code: str, message: Optional[str] = None, once: bool = False, **data: Any) -> None:
        if e.get("shutter_id") is None:
            return
        schedule_log.record(schedule_log.SHUTTER, e.get("id"), e["shutter_id"], code, now,
                            {"target": target_label(e["action"], e.get("position")) if e.get("action") else None,
                             "when": describe(e) if e.get("kind") else None, **data}, message, once_per_day=once)

    _note_not_run(cfg, raw, now, "skipped_automation_openhab", lambda sh: sh.automation != "pvcontroller")

    # 2. nur DB
    entries = [e for e in list_schedules() if auto(e["shutter_id"])]
    due = due_entries(entries, now, loc)
    waits = {sid: w for sid, w in door_waits().items() if auto(sid)}
    for sid, w in list(waits.items()):
        if _elapsed(now, datetime.fromisoformat(w["since"])) > timedelta(hours=DOOR_WAIT_H):
            emit(f"{auto(sid).name}: Warten auf geschlossene Tür nach {DOOR_WAIT_H} h beendet – nicht geschlossen",
                 logging.WARNING)
            schedule_log.record(schedule_log.SHUTTER, w["schedule_id"], sid, "door_wait_expired", now,
                                {"hours": DOOR_WAIT_H})
            clear_wait(sid)
            del waits[sid]
    try:
        cmds = shutters.last_commands()
    except sqlite3.Error:
        cmds = {}
    pend = {sid: c for sid, c in cmds.items() if cfg.shutter(sid) and c["status"] == "pending"
            and c["verify_at"] and now >= datetime.fromisoformat(c["verify_at"])}
    res.due = len(due)
    if not due and not waits and not pend:
        res.duration_s = clock() - t0
        return res

    # Sonnenzeit nicht berechenbar (keine Koordinaten) → übersprungen
    nosun = [e for e, at in due if at is None]
    for e in nosun:
        msg = f"{auto(e['shutter_id']).name}: {describe(e)} nicht berechenbar (latitude/longitude fehlen)"
        emit("Rollladen-Zeitplan übersprungen: " + msg, logging.WARNING)
        _mark([e["id"]], NO_SUN, now, msg)
        note(e, "skipped_no_sun", msg)
    due = [(e, at) for e, at in due if at is not None]

    # 3. > 15 min verspätet → übersprungen; mehrere je Rollladen → nur der späteste
    late = [(e, at) for e, at in due if _elapsed(now, at) > timedelta(minutes=CATCH_UP_MIN)]
    for e, at in late:
        msg = (f"{auto(e['shutter_id']).name} {describe(e)} → {target_label(e['action'], e['position'])}: "
               f"{int(_elapsed(now, at).total_seconds() // 60)} min verspätet (max. {CATCH_UP_MIN})")
        emit("Rollladen-Zeitplan übersprungen: " + msg, logging.WARNING)
        _mark([e["id"]], SKIPPED, now, msg)
        note(e, "skipped_late", msg, late_min=int(_elapsed(now, at).total_seconds() // 60), max_min=CATCH_UP_MIN)
    res.skipped += len(late) + len(nosun)
    late_ids = {e["id"] for e, _ in late}
    due = [(e, at) for e, at in due if e["id"] not in late_ids]
    latest: dict[str, tuple[dict, datetime]] = {}
    for e, at in sorted(due, key=lambda x: (_utc(x[1]), x[0]["id"])):
        latest[e["shutter_id"]] = (e, at)
    _mark([e["id"] for e, _ in due if latest[e["shutter_id"]][0] is not e], SUPERSEDED, now)
    for e, _ in due:
        if latest[e["shutter_id"]][0] is not e:
            note(e, "superseded", by=latest[e["shutter_id"]][0]["id"])
    for sid in latest:                          # neuer Eintrag ersetzt das Warten auf die Tür
        if sid in waits:
            clear_wait(sid)
            del waits[sid]
    if not latest and not waits and not pend:
        res.duration_s = clock() - t0
        return res

    if client is None:
        client = heating.make_client(raw)
    if client is None:
        emit("Rollladen-Zeitpläne: openhab.url fehlt in config.json", logging.WARNING)
        res.duration_s = clock() - t0
        return res
    base_timeout = getattr(client, "timeout", 3.0)

    def request(fn: Callable[[], Any]) -> Any:
        left = budget.left()
        if left < MIN_REQUEST_S:
            res.timed_out = True
            raise TimeoutError("Zeitbudget Rollläden erschöpft")
        if hasattr(client, "timeout"):
            client.timeout = max(0.2, min(base_timeout, left))
        res.requests += 1
        return fn()

    # 4. ein Request für alles
    involved = {sid: cfg.shutter(sid) for sid in [*latest, *waits, *pend]}
    names = [n for sh in involved.values() for n in sh.items()]
    names += [n for n in (cfg.frost_temp_item, cfg.duty_cycle_item) if n]
    try:
        items = request(lambda: client.get_items(names))
    except (OpenHabError, TimeoutError) as e:
        emit(f"Rollladen-Zeitpläne: openHAB nicht erreichbar ({e}) – fällige Einträge bleiben fällig", logging.WARNING)
        if isinstance(e, OpenHabError):
            decisions.note_health("openhab", False, now, str(e)[:200])
        for due_e, _ in latest.values():
            note(due_e, "openhab_unreachable", str(e)[:200], once=True)
        res.duration_s = clock() - t0
        return res
    decisions.note_health("openhab", True, now)
    contact = {sid: shutters.norm_contact(items.get(sh.contact_item)) if sh.contact_item else None
               for sid, sh in involved.items()}
    # Ist-Position aus der konfigurierten Quelle (Shelly direkt, Lese-Item) – Fallback Steuer-Item
    pos: dict[str, shutters.Position] = {}
    for sid, sh in involved.items():
        shelly = None
        if sh.position_source == "shelly":
            try:
                shelly = request(lambda: shutters.read_shelly(sh.shelly_url,
                                                              min(shutters.SHELLY_TIMEOUT_S, max(0.2, budget.left()))))
            except (shutters.ShellyError, TimeoutError) as ex:
                emit(f"{sh.name}: {ex} – Position aus openHAB", logging.WARNING)
                shelly = ex
        pos[sid] = shutters.resolve_position(sh, items, cmds.get(sid), now, shelly)

    # Rückprüfung (nur Meldung, kein Nachsenden)
    for sid, c in pend.items():
        if sid not in latest:
            cmds[sid] = shutters.verify_with(c, pos[sid], True, now) or c

    # Außentemperatur (nur bei Bedarf gebraucht)
    if cfg.frost_temp_item:
        outdoor = shutters._num(items.get(cfg.frost_temp_item, {}).get("state"))
        outdoor_src = cfg.frost_temp_item
    else:
        outdoor = _weather_min_today(now)
        outdoor_src = "Prognose-Tagesminimum"

    dc = shutters.norm_duty_cycle(items.get(cfg.duty_cycle_item)) if cfg.duty_cycle_item else None
    if dc is not None and dc > cfg.duty_cycle_max and (latest or waits):
        emit(f"Rollladen-Zeitpläne pausiert: Duty Cycle {dc:g} % über {cfg.duty_cycle_max:g} % "
             f"– fällige Einträge bleiben fällig", logging.WARNING)
        for due_e, _ in latest.values():
            note(due_e, "skipped_duty_cycle", once=True, duty_cycle=dc, max=cfg.duty_cycle_max)
        res.duration_s = clock() - t0
        return res

    # Aufgaben: (Rollladen, Eintrag, Ziel, Bezeichnung, aus Warten)
    jobs = []
    for sid, (e, _at) in latest.items():
        target = shutters.target_of(e["action"], e["position"])
        jobs.append((involved[sid], e, target, f"Zeitplan {describe(e)}", False))
    for sid, w in waits.items():
        e = get_schedule(w["schedule_id"]) or {"id": w["schedule_id"], "date": None}
        jobs.append((involved[sid], e, int(w["target"]), "Zeitplan (Tür jetzt zu)", True))

    sent_any = False
    for sh, e, target, why, from_wait in jobs:
        p = pos[sh.id]
        cur = p.value if p.reliable else None                   # unsicher (Stop, Fallback) → unbekannt
        label = f"{sh.name} → {_pos_label(target)} ({why})"
        if p.moving:
            emit(f"{label}: Rollladen fährt gerade ({'▼' if p.moving == 'down' else '▲'}) – nächste Minute erneut")
            note({**e, "shutter_id": sh.id}, "deferred_moving", once=True)
            continue                                            # bleibt fällig (Nachhol-Fenster)
        e_log = {**e, "shutter_id": sh.id}                      # Verlauf: auch für „Tür jetzt zu“ (ohne Eintrag)
        gap: dict[str, Any] = {}

        def skip(result: str, msg: str, level: int = logging.WARNING, code: Optional[str] = None,
                 e_log: dict = e_log, **data: Any) -> None:
            emit(f"Rollladen-Zeitplan übersprungen: {msg}", level)
            if not from_wait:
                _mark([e["id"]], result, now, msg)
            res.skipped += 1
            note(e_log, code or f"skipped_{result}", msg, position=cur, **data)

        # Endlage (0 %/100 %) wird immer angefahren, auch wenn openHAB sie schon meldet: der Wert kann falsch sein
        # (15.10.2026: nach Stromausfall meldeten HmIP-Aktoren 100 %, die Rollläden waren offen). Zum Zielanschlag
        # fahren ist harmlos. Richtung dann wie bei unbekannter Position → Tür-/Frost-/Handregeln gelten weiter.
        at_target = cur is not None and abs(cur - target) <= 2
        force_end = at_target and target in (0, 100)
        dir_cur = None if force_end else cur
        # Position unbekannt → in beide Richtungen prüfen (im Zweifel: nicht fahren)
        closing = target > 0 if dir_cur is None else target > dir_cur
        opening = target < 100 if dir_cur is None else target < dir_cur
        if at_target and not force_end:
            emit(f"{label}: unverändert – steht schon bei {cur} %, kein Befehl gesendet")
            if from_wait:
                clear_wait(sh.id)
            else:
                _mark([e["id"]], UNCHANGED, now)
            note(e_log, "already_set", position=cur)
            continue

        # Aussperrschutz
        if closing and sh.is_door:
            if sh.contact_item:
                if contact.get(sh.id) != "closed":
                    state = "offen" if contact.get(sh.id) == "open" else "Kontakt unbekannt"
                    if from_wait:
                        continue                                    # weiter warten
                    if sh.retry_when_closed:
                        _set_wait(sh.id, e["id"], target, now)
                        skip(WAITING_DOOR, f"{sh.name}: Tür {state} – Schließen ({describe(e)}) wartet, "
                                           f"bis die Tür zu ist", code="waiting_door", door=state)
                    else:
                        skip(DOOR_OPEN, f"{sh.name}: Tür {state} – automatisches Schließen ({describe(e)}) "
                                        f"übersprungen, wird nicht nachgeholt", door=state)
                    continue
            elif target > sh.auto_close_max:
                emit(f"{sh.name}: Tür ohne Kontakt – automatisch nur bis {sh.auto_close_max} % statt {target} %")
                gap = {"limited_from": target, "limited_to": sh.auto_close_max, "limit": "limited_door_gap"}
                target = sh.auto_close_max
                label = f"{sh.name} → {target} % ({why}, Spalt)"
                # Schon dort oder weiter zu: „zu“ fährt nie hoch – aber nur bei verlässlicher Position (Lese-Item/
                # Shelly oder eigene, bestätigte Fahrt dorthin). Eine gemeldete Endlage aus dem Steuer-Item ist
                # sonst unsicher (Stromausfall 15.10.2026) → Spalt anfahren: lieber 20 % offen als ganz offen.
                if cur is not None and cur >= target - 2 and not _end_unsure(p, cur, cmds.get(sh.id)):
                    emit(f"{label}: unverändert – steht schon bei {cur} %, kein Befehl gesendet")
                    _mark([e["id"]], UNCHANGED, now)
                    note(e_log, "already_set", position=cur, **gap)
                    continue
        # Frost
        if opening and (outdoor is None or outdoor < cfg.frost_temp_c):
            t = "unbekannt" if outdoor is None else f"{outdoor:g} °C".replace(".", ",")
            skip(FROST, f"{sh.name}: Außentemperatur {t} ({outdoor_src}) – automatisches Öffnen ({describe(e)}) "
                        f"übersprungen (Festfrieren)", outdoor_c=outdoor, frost_c=cfg.frost_temp_c,
                 source=outdoor_src)
            if from_wait:
                clear_wait(sh.id)
            continue
        # Manuelle Bedienung hat Vorrang
        if cfg.manual_hold_min > 0:
            since = (now.astimezone(timezone.utc) - timedelta(minutes=cfg.manual_hold_min)).astimezone(TZ)
            try:
                own = [(datetime.fromisoformat(c["sent_at"]), sh.verify_after_s)
                       for c in own_commands(sh.id, since - timedelta(minutes=15))]
                svc = (raw.get("openhab") or {}).get("persistence") or None
                pts = request(lambda: client.get_history(sh.history_item, since - timedelta(minutes=10),
                                                         service_id=svc))
                manual = manual_change(pts, own, since)
            except (OpenHabError, TimeoutError, sqlite3.Error) as ex:
                if isinstance(ex, TimeoutError):
                    emit(f"{label}: Zeitbudget erschöpft, nächste Minute erneut", logging.WARNING)
                    note(e_log, "deferred_time_budget", once=True)
                    continue
                skip(MANUAL_HOLD, f"{sh.name}: Verlauf nicht lesbar ({ex}) – manuelle Bedienung nicht prüfbar, "
                                  f"{describe(e)} übersprungen", code="history_unreadable")
                if from_wait:
                    clear_wait(sh.id)
                continue
            if manual is not None:
                skip(MANUAL_HOLD, f"{sh.name}: um {manual:%H:%M} von Hand bewegt – {describe(e)} übersprungen "
                                  f"(Vorrang {cfg.manual_hold_min} min)", logging.INFO,
                     manual_at=f"{manual:%H:%M}", hold_min=cfg.manual_hold_min)
                if from_wait:
                    clear_wait(sh.id)
                continue

        if dry_run:
            emit(f"DRY-RUN: {label}, nicht gesendet")
            if from_wait:
                clear_wait(sh.id)
            else:
                _mark([e["id"]], DRY_RUN, now)
            note(e_log, "dry_run", position=cur, target_pct=target, **gap)
            continue
        # gestaffelt senden
        if sent_any:
            if budget.left() < cfg.command_spacing_s + MIN_REQUEST_S:
                res.timed_out = True
                emit(f"{label}: Zeitbudget erschöpft, nächste Minute erneut", logging.WARNING)
                note(e_log, "deferred_time_budget", once=True)
                continue
            sleep(cfg.command_spacing_s)
        cmd = "position" if target not in (0, 100) else ("up" if target == 0 else "down")
        try:
            request(lambda: client.send_command(sh.item, shutters.to_openhab(cmd, target)))
        except TimeoutError:
            emit(f"{label}: Zeitbudget erschöpft, nächste Minute erneut", logging.WARNING)
            note(e_log, "deferred_time_budget", once=True)
            continue
        except OpenHabError as ex:
            if from_wait:
                emit(f"FEHLER: {label}: {ex} – nächste Minute erneut", logging.WARNING)
                note(e_log, "openhab_retry", str(ex)[:200], once=True)
            elif e.get("last_result") != RETRY:
                emit(f"FEHLER: {label}: {ex} – nächste Minute einmal erneut", logging.WARNING)
                _set_result(e["id"], RETRY)
                note(e_log, "openhab_retry", str(ex)[:200])
            else:
                msg = f"{sh.name}: {describe(e)} – Senden fehlgeschlagen ({ex})"
                emit(f"FEHLER: {label}: {ex} – aufgegeben", logging.WARNING)
                _mark([e["id"]], FAILED, now, msg)
                note(e_log, "openhab_error", str(ex)[:200])
            continue
        sent_any = True
        if force_end and not gap:
            extra = f" (openHAB meldete schon {cur} % – Endlage wird trotzdem angefahren)"
        elif gap and cur is not None and cur >= target - 2:
            extra = f" (openHAB meldete {cur} % – Endlage unsicher, Spalt wird angefahren)"
        else:
            extra = ""
        emit(f"{label}: Befehl gesendet{extra}")
        shutters.record_command(sh, cmd, target, "Zeitplan", now)
        if from_wait:
            clear_wait(sh.id)
        else:
            _mark([e["id"]], SENT, now)
        note(e_log, "sent", position=cur, target_pct=target, door_closed=True if from_wait else None,
             forced_end=True if force_end and not gap else None, **gap)
        res.sent += 1

    res.duration_s = clock() - t0
    return res


def _end_unsure(p: shutters.Position, cur: int, cmd: Optional[dict[str, Any]]) -> bool:
    """Gemeldete Endlage (0/100 %) aus dem Steuer-Item ohne eigene, bestätigte Fahrt dorthin → unsicher.
    Verlässlich sind Lese-Item/Shelly (eigene Quelle) und eine Rückprüfung „ok“ mit derselben Position."""
    if cur not in (0, 100) or p.origin != "item":
        return False
    return not (cmd and cmd.get("status") == "ok" and cmd.get("reported") is not None
                and abs(int(cmd["reported"]) - cur) <= 2)


def _note_not_run(cfg: shutters.ShutterConfig, raw: dict[str, Any], now: datetime, code: str,
                  applies: Callable[[shutters.Shutter], bool]) -> None:
    """Fällige Einträge, die gar nicht ausgeführt werden (Schalter „Zeitpläne“ aus bzw. Rollladen mit
    Automatik openHAB): einmal je Eintrag und Tag in den Verlauf. Reines Protokoll – nichts wird gesendet
    oder markiert; Fehler werden verschluckt."""
    try:
        entries = [e for e in list_schedules() if cfg.shutter(e["shutter_id"]) and applies(cfg.shutter(e["shutter_id"]))]
        for e, at in due_entries(entries, now, location(raw)):
            if at is None or _elapsed(now, at) > timedelta(minutes=CATCH_UP_MIN):
                continue
            schedule_log.record(schedule_log.SHUTTER, e["id"], e["shutter_id"], code, now,
                                {"target": target_label(e["action"], e["position"]), "when": describe(e)},
                                once_per_day=True)
    except Exception:
        log.debug("Zeitplan-Verlauf (%s) nicht geschrieben", code, exc_info=True)


def own_commands(shutter_id: str, since: datetime) -> list[dict[str, Any]]:
    """Eigene Befehle (alle Quellen) seit ``since`` – für die Erkennung manueller Bedienung."""
    with shutters._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM shutter_command WHERE shutter_id = ? ORDER BY id DESC LIMIT 100", (shutter_id,))]
    return [r for r in rows if _utc(datetime.fromisoformat(r["sent_at"])) >= _utc(since)]
