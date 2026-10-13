"""Heizung Phase 2: Zeitpläne für die openHAB-Thermostate, ausgeführt im Minuten-Cron.

Tabelle heating_schedule (in pvcontroller.db):
  wiederkehrend: days = "mo,di,mi,do,fr", date = NULL
  einmalig:      days = NULL, date = "YYYY-MM-DD" (nach Ausführung enabled=0, nach 7 Tagen gelöscht)
Uhrzeiten sind Europe/Berlin-Wanduhrzeit.

Ablauf je Minute (``run``), alles nach der PV-/Heizstab-/Wallbox-Steuerung:
1. Heizung oder Schalter „Zeitpläne aktiv“ (heating.schedules_enabled) aus → nichts, auch kein DB-Zugriff.
2. Nur DB: fällige Einträge (Zeit erreicht, heute noch nicht gelaufen, Wochentag/Datum passt) und
   offene Rückprüfungen. Ist beides leer → Ende, **kein** openHAB-Request.
3. Sollwerte der betroffenen Räume in **einem** Request lesen.
4. Rückprüfung: zuletzt gesendeten Sollwert mit dem Gerät vergleichen. Abweichung → Warnung (Log + Card),
   höchstens **ein** erneutes Senden, danach bleibt nur die Meldung.
5. Fällige Einträge: verspätet > 15 min → übersprungen (geloggt); mehrere je Raum → nur der späteste;
   Sollwert unverändert → nichts senden. ``dry_run`` → nur loggen, nie senden.
Ein Zeitbudget (Default 5 s) begrenzt den ganzen Teil; was nicht mehr passt, bleibt fällig für die
nächste Minute (innerhalb des Nachhol-Fensters).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import db
import heating
from clients.openhab_client import OpenHabError

log = logging.getLogger("pv-controller.heating")

TZ = ZoneInfo("Europe/Berlin")
DAYS = ("mo", "di", "mi", "do", "fr", "sa", "so")       # Index = datetime.weekday()
CATCH_UP_MIN = 15                                       # Nachholen nur bis 15 min Verspätung
BUDGET_S = 5.0                                          # Gesamtzeit Heizungsteil im Cron
MIN_REQUEST_S = 0.5                                     # weniger Restzeit → keinen Request mehr starten
VERIFY_STEP_MIN = 5                                     # Rückprüfung 5, 10, 15 min nach dem Senden …
VERIFY_WATCH_MIN = 15                                   # … (openHAB-autoupdate setzt das Item sofort auf den
                                                        # gesendeten Wert; erst die Meldung des Geräts zählt)
VERIFY_GIVE_UP_MIN = 30                                 # Rückprüfung unmöglich (openHAB weg) → aufgeben
MAX_ATTEMPTS = 2                                        # erstes Senden + höchstens 1 Retry
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Ergebnis der letzten Ausführung (Spalte last_result)
SENT, UNCHANGED, DRY_RUN, SKIPPED, SUPERSEDED, FAILED = (
    "sent", "unchanged", "dry_run", "skipped", "superseded", "failed")
RETRY = "retry"     # Senden fehlgeschlagen, ohne last_run: nächste Minute noch einmal fällig


# ── DB ──────────────────────────────────────────────────────────────────────


_initialized: set[str] = set()


def _conn() -> sqlite3.Connection:
    if str(db.DB_PATH) not in _initialized:       # Tabellen beim ersten Zugriff (nicht beim Import)
        init_tables()
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    _initialized.add(str(db.DB_PATH))
    with sqlite3.connect(db.DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS heating_schedule (
                id          INTEGER PRIMARY KEY,
                room_id     TEXT NOT NULL,
                setpoint    REAL NOT NULL,
                time        TEXT NOT NULL,
                days        TEXT,
                date        TEXT,
                enabled     INTEGER DEFAULT 1,
                source      TEXT,
                note        TEXT,
                last_run    TEXT,
                last_result TEXT,
                created_at  TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_heating_schedule_enabled ON heating_schedule(enabled)")
        # Rückprüfung je Raum: pending = wird 5/10/15 min nach sent_at gelesen; mismatch = Gerät meldet
        # anderen Wert (Meldung auf der Card). previous = Sollwert vor dem Senden (Zurückspringen erkennen).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS heating_verify (
                room_id   TEXT PRIMARY KEY,
                setpoint  REAL NOT NULL,
                sent_at   TEXT NOT NULL,
                attempts  INTEGER NOT NULL DEFAULT 1,
                status    TEXT NOT NULL DEFAULT 'pending',
                reported  REAL,
                message   TEXT,
                previous  REAL,
                checks    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(heating_verify)")}
        if "previous" not in cols:                      # Tabelle aus 8225dcd/7c53710
            conn.execute("ALTER TABLE heating_verify ADD COLUMN previous REAL")
        if "checks" not in cols:
            conn.execute("ALTER TABLE heating_verify ADD COLUMN checks INTEGER NOT NULL DEFAULT 0")


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["days"] = d["days"].split(",") if d.get("days") else []
    d["enabled"] = bool(d["enabled"])
    return d


def list_schedules(room_id: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM heating_schedule"
    args: tuple = ()
    if room_id:
        sql += " WHERE room_id = ?"
        args = (room_id,)
    with _conn() as conn:
        rows = conn.execute(sql + " ORDER BY time, id", args).fetchall()
    return [_row(r) for r in rows]


def get_schedule(sid: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM heating_schedule WHERE id = ?", (sid,)).fetchone()
    return _row(r) if r else None


def validate_entry(cfg: heating.HeatingConfig, room_id: str, setpoint: Any, time: str,
                   days: Optional[list[str]] = None, date: Optional[str] = None,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Prüft einen Eintrag; ValueError mit Klartext bei Fehlern. Liefert die DB-Felder."""
    room = cfg.room(room_id)
    if room is None:
        raise ValueError(f"Raum {room_id!r} unbekannt")
    if not room.controllable:
        raise ValueError(f"{room.name} hat keinen Sollwert (nur Anzeige)")
    sp = heating.validate_setpoint(cfg, setpoint)
    if not isinstance(time, str) or not _TIME_RE.match(time):
        raise ValueError("Uhrzeit muss HH:MM sein")
    days = [d.strip().lower() for d in (days or []) if d and d.strip()]
    if days and date:
        raise ValueError("Entweder Wochentage oder ein Datum, nicht beides")
    if days:
        bad = [d for d in days if d not in DAYS]
        if bad:
            raise ValueError(f"Unbekannte Wochentage: {', '.join(bad)}")
        days = [d for d in DAYS if d in days]                     # sortiert, ohne Doppelte
        return {"room_id": room_id, "setpoint": sp, "time": time, "days": ",".join(days), "date": None}
    if not date:
        raise ValueError("Wochentage oder Datum angeben")
    try:
        d = Date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError("Datum muss YYYY-MM-DD sein")
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if _utc(due_instant(d, time)) <= _utc(now):
        raise ValueError("Zeitpunkt liegt in der Vergangenheit")
    return {"room_id": room_id, "setpoint": sp, "time": time, "days": None, "date": d.isoformat()}


def create_schedule(fields: dict[str, Any], source: str = "ui", note: str = "",
                    now: Optional[datetime] = None) -> dict[str, Any]:
    created = (now or datetime.now(TZ)).astimezone(TZ).isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO heating_schedule (room_id, setpoint, time, days, date, enabled, source, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (fields["room_id"], fields["setpoint"], fields["time"], fields["days"], fields["date"],
             source, note or None, created),
        )
        sid = cur.lastrowid
    return get_schedule(sid)


def set_enabled(sid: int, enabled: bool) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        n = conn.execute("UPDATE heating_schedule SET enabled = ? WHERE id = ?", (int(enabled), sid)).rowcount
    return get_schedule(sid) if n else None


def delete_schedule(sid: int) -> bool:
    with _conn() as conn:
        return conn.execute("DELETE FROM heating_schedule WHERE id = ?", (sid,)).rowcount > 0


def clear_verify(room_id: str) -> None:
    """Neuer Sollwert für den Raum (z. B. per Button) → alte Prüfung/Meldung ist erledigt."""
    with _conn() as conn:
        conn.execute("DELETE FROM heating_verify WHERE room_id = ?", (room_id,))


def _pending_verifies() -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM heating_verify WHERE status = 'pending'")]


def _verify_due(p: dict[str, Any], now: datetime) -> bool:
    """Nächste Prüfung fällig? (5, 10, 15 min nach dem Senden; dazwischen kein openHAB-Request)"""
    try:
        sent = datetime.fromisoformat(p["sent_at"]).astimezone(TZ)
    except ValueError:
        return True
    return _utc(now) >= _utc(sent) + timedelta(minutes=VERIFY_STEP_MIN * (int(p.get("checks") or 0) + 1))


def _utc(dt: datetime) -> datetime:
    """Für Vergleiche und Abstände: aware datetimes mit derselben tzinfo vergleicht/subtrahiert Python als
    Wanduhrzeit (fold wird ignoriert) – in der doppelten Stunde im Oktober 1 h daneben. In UTC stimmt es."""
    return dt.astimezone(timezone.utc)


def _c(v: float) -> str:
    return f"{v:g}".replace(".", ",") + " °C"


# ── Zeit ────────────────────────────────────────────────────────────────────


def due_instant(day: Date, hhmm: str) -> datetime:
    """Zeitpunkt (aware, Europe/Berlin) von HH:MM an ``day``.

    Zeitumstellung: doppelte Stunde im Oktober → erstes Auftreten (fold=0);
    fehlende Stunde im März (02:00–02:59) → Moment der Umstellung (03:00 Sommerzeit).
    """
    h, m = map(int, hhmm.split(":"))
    wall = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    back = wall.astimezone(timezone.utc).astimezone(TZ)
    if (back.hour, back.minute) == (h, m):
        return wall
    # Lücke: ab dem Instant vor der Lücke minutenweise vor bis die Wanduhr HH:MM erreicht
    t = wall.replace(fold=1).astimezone(timezone.utc)
    for _ in range(180):
        local = t.astimezone(TZ)
        if (local.hour, local.minute) >= (h, m) or local.date() > day:
            return local
        t += timedelta(minutes=1)
    return wall


def _ran_on(entry: dict[str, Any], day: Date) -> bool:
    lr = entry.get("last_run")
    if not lr:
        return False
    try:
        return datetime.fromisoformat(lr).astimezone(TZ).date() == day
    except ValueError:
        return False


def applies_on(entry: dict[str, Any], day: Date) -> bool:
    if entry.get("date"):
        return entry["date"] == day.isoformat()
    return DAYS[day.weekday()] in (entry.get("days") or [])


def due_entries(entries: list[dict[str, Any]], now: datetime) -> list[tuple[dict[str, Any], datetime]]:
    """Heute fällige, noch nicht gelaufene Einträge → [(Eintrag, Fälligkeit)]."""
    now = now.astimezone(TZ)
    today = now.date()
    out = []
    for e in entries:
        if not e["enabled"] or not applies_on(e, today) or _ran_on(e, today):
            continue
        at = due_instant(today, e["time"])
        if _utc(at) > _utc(now):
            continue
        created = e.get("created_at")
        if created:
            try:
                if _utc(datetime.fromisoformat(created)) > _utc(at):
                    continue        # erst nach der Uhrzeit angelegt → heute nicht nachholen
            except ValueError:
                pass
        out.append((e, at))
    return out


def next_changes(entries: list[dict[str, Any]], now: datetime, horizon_days: int = 7) -> dict[str, dict]:
    """Nächster Schaltpunkt je Raum (für die Card): {room_id: {"at": iso, "setpoint": x}}."""
    now = now.astimezone(TZ)
    out: dict[str, dict] = {}
    for offset in range(horizon_days + 1):
        day = now.date() + timedelta(days=offset)
        for e in entries:
            if not e["enabled"] or not applies_on(e, day) or (offset == 0 and _ran_on(e, day)):
                continue
            at = due_instant(day, e["time"])
            if _utc(at) <= _utc(now):
                continue
            cur = out.get(e["room_id"])
            if cur is None or _utc(at) < _utc(cur["_at"]):
                out[e["room_id"]] = {"_at": at, "at": at.isoformat(timespec="minutes"),
                                     "setpoint": e["setpoint"], "id": e["id"]}
    for v in out.values():
        v.pop("_at")
    return out


# ── Ausführung im Cron ──────────────────────────────────────────────────────


@dataclass
class RunResult:
    active: bool = False            # Schalter an und Heizung konfiguriert
    due: int = 0                    # fällige Einträge (inkl. übersprungene)
    requests: int = 0               # openHAB-Requests
    sent: int = 0
    skipped: int = 0
    timed_out: bool = False
    duration_s: float = 0.0


class _Budget:
    def __init__(self, seconds: float, clock: Callable[[], float]):
        self.clock = clock
        self.end = clock() + seconds

    def left(self) -> float:
        return self.end - self.clock()


def _mark(ids: list[int], result: str, at: datetime, disable_once: bool = False) -> None:
    if not ids:
        return
    ph = ",".join("?" for _ in ids)
    with _conn() as conn:
        conn.execute(f"UPDATE heating_schedule SET last_run = ?, last_result = ? WHERE id IN ({ph})",
                     (at.isoformat(timespec="seconds"), result, *ids))
        if disable_once:
            conn.execute(f"UPDATE heating_schedule SET enabled = 0 WHERE id IN ({ph}) AND date IS NOT NULL", ids)


def run(raw: dict[str, Any], dry_run: bool, now: Optional[datetime] = None, client: Any = None,
        budget_s: float = BUDGET_S, clock: Callable[[], float] = _time.monotonic,
        app_log: Optional[Callable[[str, int], None]] = None) -> RunResult:
    """Ein Minuten-Durchlauf. Wirft nichts außer echten Programmfehlern (Aufrufer fängt alles)."""
    t0 = clock()
    res = RunResult()
    emit = app_log or (lambda msg, level=logging.INFO: log.log(level, msg))
    cfg = heating.parse_heating(raw)
    if not (cfg.enabled and cfg.schedules_enabled):
        return res
    res.active = True
    now = (now or datetime.now(TZ)).astimezone(TZ)
    budget = _Budget(budget_s, clock)

    # 2. nur DB
    entries = [e for e in list_schedules() if cfg.room(e["room_id"]) and cfg.room(e["room_id"]).controllable]
    due = due_entries(entries, now)
    pending = [p for p in _pending_verifies() if cfg.room(p["room_id"]) and _verify_due(p, now)]
    res.due = len(due)
    if not due and not pending:
        res.duration_s = clock() - t0
        return res

    # Nachholen: > 15 min verspätet → überspringen
    late = [(e, at) for e, at in due if _utc(now) - _utc(at) > timedelta(minutes=CATCH_UP_MIN)]
    for e, at in late:
        room = cfg.room(e["room_id"])
        emit(f"Zeitplan übersprungen: {room.name} {e['time']} → {e['setpoint']:g} °C "
             f"({int((_utc(now) - _utc(at)).total_seconds() // 60)} min verspätet, max. {CATCH_UP_MIN})", logging.WARNING)
    _mark([e["id"] for e, _ in late], SKIPPED, now, disable_once=True)
    res.skipped = len(late)
    late_ids = {e["id"] for e, _ in late}
    due = [(e, at) for e, at in due if e["id"] not in late_ids]

    # mehrere je Raum → nur der späteste
    latest: dict[str, tuple[dict, datetime]] = {}
    for e, at in sorted(due, key=lambda x: (_utc(x[1]), x[0]["id"])):
        latest[e["room_id"]] = (e, at)
    superseded = [e["id"] for e, _ in due if latest[e["room_id"]][0] is not e]
    _mark(superseded, SUPERSEDED, now, disable_once=True)

    if not latest and not pending:
        res.duration_s = clock() - t0
        return res

    if client is None:
        client = heating.make_client(raw)
    if client is None:
        emit("Zeitpläne: openhab.url fehlt in config.json", logging.WARNING)
        res.duration_s = clock() - t0
        return res
    base_timeout = getattr(client, "timeout", 3.0)

    def request(fn: Callable[[], Any]) -> Any:
        left = budget.left()
        if left < MIN_REQUEST_S:
            res.timed_out = True
            raise TimeoutError("Zeitbudget Heizung erschöpft")
        if hasattr(client, "timeout"):
            client.timeout = max(0.2, min(base_timeout, left))
        res.requests += 1
        return fn()

    # 3. ein Request für alle betroffenen Sollwerte
    rooms = {rid: cfg.room(rid) for rid in [*latest.keys(), *(p["room_id"] for p in pending)]}
    try:
        items = request(lambda: client.get_items([r.setpoint_item for r in rooms.values()]))
        current = {rid: heating.norm_temp(items.get(r.setpoint_item)) for rid, r in rooms.items()}
    except (OpenHabError, TimeoutError) as e:
        emit(f"Zeitpläne: openHAB nicht erreichbar ({e}) – fällige Einträge bleiben fällig", logging.WARNING)
        _give_up_stale_verifies(pending, cfg, now, emit)
        res.duration_s = clock() - t0
        return res

    def send(room: heating.Room, value: float) -> None:
        request(lambda: client.send_command(room.setpoint_item, f"{value:g}"))

    # 4. Rückprüfung: 5, 10 und 15 min nach dem Senden. Gesendeter Wert → weiter beobachten bzw. fertig;
    #    alter Wert (zurückgesprungen) → Warnung + einmal erneut senden; anderer Wert → nur Warnung.
    for p in pending:
        if p["room_id"] in latest:
            continue                    # neuer Eintrag für den Raum geht vor
        room = rooms[p["room_id"]]
        got = current.get(p["room_id"])
        if got is None:
            _give_up_stale_verifies([p], cfg, now, emit)   # Item ohne Wert: nach 30 min aufgeben
            continue
        checks = int(p.get("checks") or 0) + 1
        if abs(got - p["setpoint"]) < 0.01:
            if checks * VERIFY_STEP_MIN >= VERIFY_WATCH_MIN:
                clear_verify(room.id)   # 15 min stabil: fertig
            else:
                _verify_update(room.id, status="pending", reported=got, message=None, checks=checks)
            continue
        prev = p.get("previous")
        jumped = prev is not None and abs(got - prev) < 0.01
        msg = f"{room.name}: {_c(p['setpoint'])} gesendet, Gerät meldet {'wieder ' if jumped else ''}{_c(got)}"
        if jumped and p["attempts"] < MAX_ATTEMPTS and not dry_run:
            try:
                send(room, p["setpoint"])
                emit(f"{msg} (zurückgesprungen) – sende einmal erneut", logging.WARNING)
                _verify_update(room.id, status="pending", reported=got, message=msg + " (erneut gesendet)",
                               attempts=p["attempts"] + 1, sent_at=now, checks=0)
            except (OpenHabError, TimeoutError) as e:
                emit(f"{msg} – erneutes Senden fehlgeschlagen: {e}", logging.WARNING)
                _verify_update(room.id, status="mismatch", reported=got, message=msg)
        else:
            if not jumped:
                msg += " (am Gerät, Fenster-offen oder Wochenprofil geändert?)"
            emit(msg + " – kein erneutes Senden", logging.WARNING)
            _verify_update(room.id, status="mismatch", reported=got, message=msg)

    # 5. fällige Einträge ausführen
    for rid, (e, at) in latest.items():
        room = rooms[rid]
        target = float(e["setpoint"])
        label = f"{room.name} Soll → {target:g} °C (Zeitplan {e['time']}" + (", einmalig" if e["date"] else "") + ")"
        if current.get(rid) is not None and abs(current[rid] - target) < 0.01:
            emit(f"{label}: bereits eingestellt, nichts gesendet")
            _mark([e["id"]], UNCHANGED, now, disable_once=True)
            continue
        if dry_run:
            emit(f"DRY-RUN: {label}, nicht gesendet")
            _mark([e["id"]], DRY_RUN, now, disable_once=True)
            continue
        try:
            send(room, target)
        except TimeoutError:
            emit(f"{label}: Zeitbudget erschöpft, nächste Minute erneut", logging.WARNING)
            continue                    # bleibt fällig (Nachhol-Fenster)
        except OpenHabError as e2:
            # Ob der Befehl angekommen ist, ist offen (z. B. Lese-Timeout): einmal nächste Minute erneut –
            # dann wird vorher gelesen, ein schon übernommener Sollwert wird nicht doppelt gesendet.
            if e.get("last_result") != RETRY:
                emit(f"FEHLER: {label}: {e2} – nächste Minute einmal erneut", logging.WARNING)
                _set_result(e["id"], RETRY)
            else:
                emit(f"FEHLER: {label}: {e2} – aufgegeben", logging.WARNING)
                _mark([e["id"]], FAILED, now, disable_once=True)
            continue
        emit(label)
        _mark_sent(e["id"], rid, target, now, previous=current.get(rid))
        res.sent += 1

    res.duration_s = clock() - t0
    return res


def _set_result(sid: int, result: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE heating_schedule SET last_result = ? WHERE id = ?", (result, sid))


def _mark_sent(sid: int, room_id: str, setpoint: float, now: datetime, previous: Optional[float] = None) -> None:
    """Gesendet: last_run und Rückprüfung in **einer** Transaktion (bricht der Lauf hier ab, gilt beides
    oder keins; ohne Eintrag ist er nächste Minute fällig und liest vorher den Sollwert)."""
    with _conn() as conn:
        conn.execute("UPDATE heating_schedule SET last_run = ?, last_result = ? WHERE id = ?",
                     (now.isoformat(timespec="seconds"), SENT, sid))
        conn.execute("UPDATE heating_schedule SET enabled = 0 WHERE id = ? AND date IS NOT NULL", (sid,))
        _verify_start(room_id, setpoint, now, conn, previous=previous)


def _verify_start(room_id: str, setpoint: float, now: datetime, conn: Optional[sqlite3.Connection] = None,
                  previous: Optional[float] = None) -> None:
    sql = ("INSERT OR REPLACE INTO heating_verify (room_id, setpoint, sent_at, attempts, status, previous, checks) "
           "VALUES (?, ?, ?, 1, 'pending', ?, 0)")
    args = (room_id, setpoint, now.isoformat(timespec="seconds"), previous)
    if conn is not None:
        conn.execute(sql, args)
        return
    with _conn() as c:
        c.execute(sql, args)


def _verify_update(room_id: str, status: str, reported: Optional[float], message: Optional[str],
                   attempts: Optional[int] = None, sent_at: Optional[datetime] = None,
                   checks: Optional[int] = None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE heating_verify SET status = ?, reported = ?, message = ?, "
            "attempts = COALESCE(?, attempts), sent_at = COALESCE(?, sent_at), checks = COALESCE(?, checks) "
            "WHERE room_id = ?",
            (status, reported, message, attempts,
             sent_at.isoformat(timespec="seconds") if sent_at else None, checks, room_id),
        )


def _give_up_stale_verifies(pending: list[dict], cfg: heating.HeatingConfig, now: datetime,
                            emit: Callable[[str, int], None]) -> None:
    for p in pending:
        try:
            age = _utc(now) - _utc(datetime.fromisoformat(p["sent_at"]))
        except ValueError:
            continue
        if age > timedelta(minutes=VERIFY_GIVE_UP_MIN):
            room = cfg.room(p["room_id"])
            msg = f"{room.name}: {_c(p['setpoint'])} gesendet, Rückmeldung nicht prüfbar (openHAB/Gerät ohne Wert)"
            emit(msg, logging.WARNING)
            _verify_update(p["room_id"], status="mismatch", reported=None, message=msg)


def alerts() -> dict[str, dict[str, Any]]:
    """Meldungen für die Cards: Abweichung (endgültig) oder Abweichung mit laufendem Retry."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT room_id, setpoint, status, message FROM heating_verify WHERE message IS NOT NULL"
        ).fetchall()
    return {r["room_id"]: dict(r) for r in rows}

