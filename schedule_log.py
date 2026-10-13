"""Zeitplan-Verlauf: ein Eintrag je Ergebnis eines fälligen Heizungs-/Rollladen-Zeitplans.

``heating_schedule``/``shutter_schedule`` merken je Eintrag nur das **letzte** Ergebnis. Hier steht der
Verlauf mit festem Grund-Code (``reasons.py``), Zahlen und der Log-Meldung – Aufbewahrung 14 Tage
(``db.prune``). Schreiben ist reines Protokoll: ein Fehler hier darf nie eine Fahrt oder einen Sollwert
verhindern, deshalb fängt ``record`` alles ab.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Optional

import db

log = logging.getLogger(__name__)

HEATING, SHUTTER = "heating", "shutter"
_initialized: set[str] = set()


def init_tables() -> None:
    _initialized.add(str(db.DB_PATH))
    with sqlite3.connect(db.DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_result (
                id          INTEGER PRIMARY KEY,
                ts          TEXT NOT NULL,      -- ISO, lokale Zeit mit Offset
                day         TEXT NOT NULL,      -- YYYY-MM-DD (lokal), für „einmal pro Tag“
                kind        TEXT NOT NULL,      -- heating | shutter
                schedule_id INTEGER,            -- NULL: Rückprüfung ohne Bezug zu einem Eintrag
                target_id   TEXT NOT NULL,      -- Raum bzw. Rollladen
                code        TEXT NOT NULL,      -- Grund-Code (reasons.py)
                data        TEXT,               -- JSON mit Zahlen
                message     TEXT                -- Meldung wie im Log
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_schedule_result_ts ON schedule_result (ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_schedule_result_day ON schedule_result (day, kind, schedule_id)")


def _conn() -> sqlite3.Connection:
    if str(db.DB_PATH) not in _initialized:
        init_tables()
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def record(kind: str, schedule_id: Optional[int], target_id: str, code: str, now: datetime,
           data: Optional[dict[str, Any]] = None, message: Optional[str] = None, once_per_day: bool = False) -> bool:
    """Ergebnis festhalten. ``once_per_day``: derselbe Code für denselben Eintrag nur einmal am Tag
    (für Zustände, die sonst jede Minute wiederkämen). Gibt zurück, ob geschrieben wurde; wirft nie."""
    try:
        day = now.date().isoformat()
        with _conn() as conn:
            if once_per_day and conn.execute(
                    "SELECT 1 FROM schedule_result WHERE day = ? AND kind = ? AND schedule_id IS ? AND target_id = ? "
                    "AND code = ? LIMIT 1", (day, kind, schedule_id, target_id, code)).fetchone():
                return False
            conn.execute(
                "INSERT INTO schedule_result (ts, day, kind, schedule_id, target_id, code, data, message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), day, kind, schedule_id, target_id, code,
                 json.dumps(data, ensure_ascii=False, separators=(",", ":")) if data else None, message))
        return True
    except Exception:
        log.debug("Zeitplan-Verlauf nicht geschrieben", exc_info=True)
        return False


def results(kind: Optional[str] = None, target_id: Optional[str] = None, since: Optional[datetime] = None,
            limit: int = 200) -> list[dict[str, Any]]:
    """Ergebnisse, neueste zuerst."""
    where, args = [], []
    for col, val in (("kind", kind), ("target_id", target_id)):
        if val is not None:
            where.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        where.append("ts >= ?")
        args.append(since.isoformat(timespec="seconds"))
    sql = "SELECT * FROM schedule_result" + (" WHERE " + " AND ".join(where) if where else "")
    with _conn() as conn:
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        out.append(d)
    return out
