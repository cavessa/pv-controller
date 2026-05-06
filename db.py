"""SQLite-Persistenz für Phase-Zustandsänderungen des Heizstabs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "pvcontroller.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS HeizstabPhaseLog (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                phase     INTEGER NOT NULL,
                state     TEXT    NOT NULL,
                timestamp TEXT    NOT NULL
            )
            """
        )


def log_phase_change(phase: int, state: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO HeizstabPhaseLog (phase, state, timestamp) VALUES (?, ?, ?)",
            (phase, state, datetime.now().isoformat(timespec="seconds")),
        )


def get_phase_logs(since: Optional[datetime] = None) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        if since is not None:
            rows = conn.execute(
                "SELECT id, phase, state, timestamp FROM HeizstabPhaseLog "
                "WHERE timestamp >= ? ORDER BY timestamp ASC",
                (since.isoformat(timespec="seconds"),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, phase, state, timestamp FROM HeizstabPhaseLog "
                "ORDER BY timestamp ASC"
            ).fetchall()
    return [
        {"id": r[0], "phase": r[1], "state": r[2], "timestamp": r[3]}
        for r in rows
    ]


def init_pv_logging_tables() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pv_hourly_log (
                timestamp       TEXT PRIMARY KEY,
                pv_w            INTEGER,
                feed_in_w       INTEGER,
                consumption_w   INTEGER,
                str1_w          INTEGER,
                str2_w          INTEGER,
                str1_v          REAL,
                str1_a          REAL,
                str2_v          REAL,
                str2_a          REAL,
                string_ratio    REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pv_daily_log (
                date            TEXT PRIMARY KEY,
                pv_kwh          REAL,
                feed_out_kwh    REAL,
                feed_in_kwh     REAL,
                selfuse_kwh     REAL,
                selfuse_pct     REAL,
                autarky_pct     REAL,
                sunshine_h      REAL,
                ghi_kwh_m2      REAL,
                cloud_cover_pct REAL,
                temp_avg_c      REAL,
                forecast_kwh    REAL
            )
        """)


def log_hourly_snapshot(
    pv_w: int, feed_in_w: int, consumption_w: int,
    str1_w: int, str2_w: int,
    str1_v: float, str1_a: float,
    str2_v: float, str2_a: float,
) -> None:
    ratio = round(str1_w / str2_w, 3) if str2_w > 0 else None
    ts = datetime.now().strftime("%Y-%m-%d %H:00")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pv_hourly_log
               (timestamp, pv_w, feed_in_w, consumption_w,
                str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, string_ratio)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, pv_w, feed_in_w, consumption_w,
             str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, ratio),
        )


def log_daily_summary(
    date_str: str, pv_kwh: float, feed_out_kwh: float, feed_in_kwh: float,
    sunshine_h: float | None = None, ghi_kwh_m2: float | None = None,
    cloud_cover_pct: float | None = None, temp_avg_c: float | None = None,
    forecast_kwh: float | None = None,
) -> None:
    selfuse = max(0.0, pv_kwh - feed_out_kwh)
    selfuse_pct = round(selfuse / pv_kwh * 100, 1) if pv_kwh > 0 else 0.0
    denom = selfuse + feed_in_kwh
    autarky_pct = round(selfuse / denom * 100, 1) if denom > 0 else 100.0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pv_daily_log
               (date, pv_kwh, feed_out_kwh, feed_in_kwh,
                selfuse_kwh, selfuse_pct, autarky_pct,
                sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c, forecast_kwh)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date_str, round(pv_kwh, 2), round(feed_out_kwh, 2), round(feed_in_kwh, 2),
             round(selfuse, 2), selfuse_pct, autarky_pct,
             sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c, forecast_kwh),
        )


def get_hourly_data(date_str: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT timestamp, pv_w, feed_in_w, consumption_w,
                      str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, string_ratio
               FROM pv_hourly_log WHERE timestamp LIKE ?
               ORDER BY timestamp ASC""",
            (date_str + "%",),
        ).fetchall()
    return [
        {"ts": r[0], "pv_w": r[1], "feed_in_w": r[2], "consumption_w": r[3],
         "str1_w": r[4], "str2_w": r[5], "str1_v": r[6], "str1_a": r[7],
         "str2_v": r[8], "str2_a": r[9], "string_ratio": r[10]}
        for r in rows
    ]


def get_daily_history(days: int = 30) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date, pv_kwh, feed_out_kwh, feed_in_kwh,
                      selfuse_kwh, selfuse_pct, autarky_pct,
                      sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c, forecast_kwh
               FROM pv_daily_log ORDER BY date DESC LIMIT ?""",
            (days,),
        ).fetchall()
    result = [
        {"date": r[0], "pv_kwh": r[1], "feed_out_kwh": r[2], "feed_in_kwh": r[3],
         "selfuse_kwh": r[4], "selfuse_pct": r[5], "autarky_pct": r[6],
         "sunshine_h": r[7], "ghi_kwh_m2": r[8], "cloud_cover_pct": r[9],
         "temp_avg_c": r[10], "forecast_kwh": r[11]}
        for r in rows
    ]
    result.reverse()
    return result


init_db()
init_pv_logging_tables()


# ── Kaskade ──────────────────────────────────────────────────────────────────

PROTECTED_CASCADE_IDS: frozenset[str] = frozenset({"heizstab", "wallbox"})
VALID_CASCADE_TYPES: frozenset[str] = frozenset(
    {"heizstab", "wallbox", "shelly_gen1", "shelly_gen2"}
)

_CASCADE_COLS = (
    "id, name, type, ip_address, shelly_channel, "
    "power_watts, priority, enabled, min_on_minutes, min_off_minutes, "
    "hysteresis_watts, is_on, turned_on_at, turned_off_at, "
    "manual_override_action, manual_override_until, created_at, updated_at, "
    "consecutive_errors, last_status_power_w, last_status_ok"
)


def init_cascade_tables() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cascade_devices (
                id                     TEXT PRIMARY KEY,
                name                   TEXT NOT NULL,
                type                   TEXT NOT NULL,
                ip_address             TEXT,
                shelly_channel         INTEGER DEFAULT 0,
                power_watts            INTEGER NOT NULL,
                priority               INTEGER NOT NULL,
                enabled                INTEGER DEFAULT 1,
                min_on_minutes         INTEGER DEFAULT 5,
                min_off_minutes        INTEGER DEFAULT 3,
                hysteresis_watts       INTEGER DEFAULT 100,
                is_on                  INTEGER DEFAULT 0,
                turned_on_at           TEXT,
                turned_off_at          TEXT,
                manual_override_action TEXT,
                manual_override_until  TEXT,
                created_at             TEXT DEFAULT (datetime('now')),
                updated_at             TEXT DEFAULT (datetime('now')),
                consecutive_errors     INTEGER DEFAULT 0,
                last_status_power_w    REAL,
                last_status_ok         INTEGER DEFAULT 1
            )
        """)
        for col, definition in (
            ("consecutive_errors",  "INTEGER DEFAULT 0"),
            ("last_status_power_w", "REAL"),
            ("last_status_ok",      "INTEGER DEFAULT 1"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE cascade_devices ADD COLUMN {col} {definition}"
                )
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cascade_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT DEFAULT (datetime('now')),
                surplus_watts   INTEGER,
                device_id       TEXT,
                action          TEXT,
                reason          TEXT,
                remaining_watts INTEGER
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO cascade_devices
                (id, name, type, power_watts, priority)
            VALUES
                ('heizstab', 'Speicher (Heizstab)', 'heizstab', 1500, 1),
                ('wallbox',  'Wallbox',              'wallbox',  2300, 2)
        """)


def _row_to_device(row: tuple) -> dict:
    keys = _CASCADE_COLS.replace(" ", "").split(",")
    d = dict(zip(keys, row))
    d["enabled"] = bool(d["enabled"])
    d["is_on"] = bool(d["is_on"])
    d["last_status_ok"] = bool(d.get("last_status_ok", 1))
    return d


def get_cascade_devices() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT {_CASCADE_COLS} FROM cascade_devices ORDER BY priority ASC"
        ).fetchall()
    return [_row_to_device(r) for r in rows]


def get_cascade_device(device_id: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"SELECT {_CASCADE_COLS} FROM cascade_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    return _row_to_device(row) if row else None


def create_cascade_device(
    device_id: str,
    name: str,
    device_type: str,
    power_watts: int,
    priority: int,
    ip_address: Optional[str] = None,
    shelly_channel: int = 0,
    min_on_minutes: int = 5,
    min_off_minutes: int = 3,
    hysteresis_watts: int = 100,
) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO cascade_devices "
            "(id, name, type, ip_address, shelly_channel, power_watts, priority, "
            "min_on_minutes, min_off_minutes, hysteresis_watts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                device_id, name, device_type, ip_address, shelly_channel,
                power_watts, priority, min_on_minutes, min_off_minutes, hysteresis_watts,
            ),
        )
    return get_cascade_device(device_id)  # type: ignore[return-value]


def update_cascade_device(device_id: str, **kwargs: object) -> Optional[dict]:
    allowed = {
        "name", "power_watts", "priority", "enabled",
        "ip_address", "shelly_channel",
        "min_on_minutes", "min_off_minutes", "hysteresis_watts",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_cascade_device(device_id)
    updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE cascade_devices SET {set_clause} WHERE id = ?",
            [*updates.values(), device_id],
        )
    return get_cascade_device(device_id)


def delete_cascade_device(device_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM cascade_devices WHERE id = ?", (device_id,))
    return cur.rowcount > 0


def reorder_cascade_devices(device_ids: list[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        for i, device_id in enumerate(device_ids):
            conn.execute(
                "UPDATE cascade_devices SET priority = ?, updated_at = ? WHERE id = ?",
                (i + 1, now, device_id),
            )


def update_cascade_device_state(
    device_id: str,
    is_on: bool,
    turned_on_at: Optional[datetime] = None,
    turned_off_at: Optional[datetime] = None,
) -> None:
    updates: dict[str, object] = {
        "is_on": int(is_on),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if turned_on_at is not None:
        updates["turned_on_at"] = turned_on_at.isoformat(timespec="seconds")
    if turned_off_at is not None:
        updates["turned_off_at"] = turned_off_at.isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE cascade_devices SET {set_clause} WHERE id = ?",
            [*updates.values(), device_id],
        )


def set_cascade_override(device_id: str, action: str, until: datetime) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    is_on = 1 if action == "on" else 0
    ts_col = "turned_on_at" if action == "on" else "turned_off_at"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE cascade_devices SET manual_override_action = ?, "
            f"manual_override_until = ?, is_on = ?, {ts_col} = ?, updated_at = ? WHERE id = ?",
            (action, until.isoformat(timespec="seconds"), is_on, now, now, device_id),
        )


def clear_cascade_override(device_id: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices SET manual_override_action = NULL, "
            "manual_override_until = NULL, updated_at = ? WHERE id = ?",
            (now, device_id),
        )


def log_cascade_action(
    surplus_watts: int,
    device_id: str,
    action: str,
    reason: str,
    remaining_watts: int,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO cascade_log "
            "(surplus_watts, device_id, action, reason, remaining_watts) "
            "VALUES (?, ?, ?, ?, ?)",
            (surplus_watts, device_id, action, reason, remaining_watts),
        )


def get_cascade_log(limit: int = 100) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, surplus_watts, device_id, action, reason, remaining_watts "
            "FROM cascade_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": r[0], "timestamp": r[1], "surplus_watts": r[2],
            "device_id": r[3], "action": r[4], "reason": r[5],
            "remaining_watts": r[6],
        }
        for r in rows
    ]


def increment_cascade_device_errors(device_id: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices "
            "SET consecutive_errors = consecutive_errors + 1, last_status_ok = 0, "
            "    updated_at = ? "
            "WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), device_id),
        )
        row = conn.execute(
            "SELECT consecutive_errors FROM cascade_devices WHERE id = ?", (device_id,)
        ).fetchone()
    return row[0] if row else 0


def reset_cascade_device_errors(device_id: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices "
            "SET consecutive_errors = 0, last_status_ok = 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), device_id),
        )


def update_cascade_device_live_status(
    device_id: str, power_w: float, status_ok: bool
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices "
            "SET last_status_power_w = ?, last_status_ok = ?, updated_at = ? WHERE id = ?",
            (power_w, int(status_ok), datetime.now().isoformat(timespec="seconds"), device_id),
        )


def init_cascade_settings_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cascade_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.executemany(
            "INSERT OR IGNORE INTO cascade_settings (key, value) VALUES (?, ?)",
            [
                ("cascade_enabled",    "1"),
                ("polling_interval_s", "60"),
                ("min_surplus_watts",  "0"),
                ("allow_grid_draw",    "1"),
            ],
        )


def get_cascade_settings() -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT key, value FROM cascade_settings").fetchall()
    raw = dict(rows)
    return {
        "cascade_enabled":    bool(int(raw.get("cascade_enabled",    "1"))),
        "polling_interval_s": int(raw.get("polling_interval_s", "60")),
        "min_surplus_watts":  int(raw.get("min_surplus_watts",  "0")),
        "allow_grid_draw":    bool(int(raw.get("allow_grid_draw",    "1"))),
    }


def update_cascade_settings(**kwargs: object) -> dict:
    allowed = {"cascade_enabled", "polling_interval_s", "min_surplus_watts", "allow_grid_draw"}
    with sqlite3.connect(DB_PATH) as conn:
        for k, v in kwargs.items():
            if k in allowed:
                conn.execute(
                    "INSERT OR REPLACE INTO cascade_settings (key, value) VALUES (?, ?)",
                    (k, str(int(v) if isinstance(v, bool) else int(v))),
                )
    return get_cascade_settings()


def get_cascade_permission(device_type: str) -> Optional[bool]:
    """Gibt cascade is_on für einen Gerätetyp zurück, oder None wenn kein Eintrag."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT is_on FROM cascade_devices WHERE type = ? AND enabled = 1 "
                "ORDER BY priority ASC LIMIT 1",
                (device_type,),
            ).fetchone()
        return bool(row[0]) if row is not None else None
    except sqlite3.Error:
        return None


init_cascade_tables()
init_cascade_settings_table()
