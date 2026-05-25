"""SQLite-Persistenz für Phase-Zustandsänderungen des Heizstabs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "pvcontroller.db"
TEMP_HISTORY_PATH = Path(__file__).resolve().parent / "temp_history.json"


def append_temp_history(temp: float | None, wb_w: float | None) -> None:
    now = datetime.now()
    entry: dict = {"t": now.isoformat(timespec="minutes")}
    if temp is not None:
        entry["temp"] = round(temp, 1)
    if wb_w is not None:
        entry["wb_w"] = round(wb_w, 0)
    if len(entry) == 1:
        return
    data: list[dict] = []
    if TEMP_HISTORY_PATH.exists():
        try:
            with open(TEMP_HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    if data and data[-1]["t"] == entry["t"]:
        data[-1].update(entry)
    else:
        cutoff = (now - timedelta(hours=25)).isoformat(timespec="minutes")
        data = [e for e in data if e["t"] >= cutoff]
        data.append(entry)
    try:
        with open(TEMP_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


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
        for col, definition in (
            ("storage_temp_c", "REAL"),
            ("heater_w",       "INTEGER"),
            ("wallbox_w",      "INTEGER"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE pv_hourly_log ADD COLUMN {col} {definition}"
                )
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS string_alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                level        TEXT NOT NULL,
                message      TEXT NOT NULL,
                str1_w       INTEGER,
                str2_w       INTEGER,
                ratio        REAL,
                normal_ratio REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_log (
                date           TEXT PRIMARY KEY,
                sunshine_hours REAL,
                ghi_kwh_m2     REAL,
                temp_max       REAL,
                temp_min       REAL,
                weathercode    INTEGER,
                sunset         TEXT
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
                forecast_kwh    REAL,
                forecast_ghi    REAL
            )
        """)
        for col, definition in (
            ("forecast_ghi", "REAL"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE pv_daily_log ADD COLUMN {col} {definition}"
                )
            except sqlite3.OperationalError:
                pass
        for col, definition in (
            ("sunset", "TEXT"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE weather_log ADD COLUMN {col} {definition}"
                )
            except sqlite3.OperationalError:
                pass


def log_hourly_snapshot(
    pv_w: int, feed_in_w: int, consumption_w: int,
    str1_w: int, str2_w: int,
    str1_v: float, str1_a: float,
    str2_v: float, str2_a: float,
    storage_temp_c: Optional[float] = None,
    heater_w: Optional[int] = None,
    wallbox_w: Optional[int] = None,
) -> None:
    ratio = round(str1_w / str2_w, 3) if str2_w > 0 else None
    ts = datetime.now().strftime("%Y-%m-%d %H:00")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pv_hourly_log
               (timestamp, pv_w, feed_in_w, consumption_w,
                str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, string_ratio,
                storage_temp_c, heater_w, wallbox_w)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, pv_w, feed_in_w, consumption_w,
             str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, ratio,
             storage_temp_c, heater_w, wallbox_w),
        )


def log_daily_summary(
    date_str: str, pv_kwh: float, feed_out_kwh: float, feed_in_kwh: float,
    sunshine_h: float | None = None, ghi_kwh_m2: float | None = None,
    cloud_cover_pct: float | None = None, temp_avg_c: float | None = None,
    forecast_kwh: float | None = None, forecast_ghi: float | None = None,
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
                sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c, forecast_kwh, forecast_ghi)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date_str, round(pv_kwh, 2), round(feed_out_kwh, 2), round(feed_in_kwh, 2),
             round(selfuse, 2), selfuse_pct, autarky_pct,
             sunshine_h, ghi_kwh_m2, cloud_cover_pct, temp_avg_c, forecast_kwh, forecast_ghi),
        )


def get_hourly_data(date_str: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT timestamp, pv_w, feed_in_w, consumption_w,
                      str1_w, str2_w, str1_v, str1_a, str2_v, str2_a, string_ratio,
                      storage_temp_c, heater_w, wallbox_w
               FROM pv_hourly_log WHERE timestamp LIKE ?
               ORDER BY timestamp ASC""",
            (date_str + "%",),
        ).fetchall()
    return [
        {"ts": r[0], "pv_w": r[1], "feed_in_w": r[2], "consumption_w": r[3],
         "str1_w": r[4], "str2_w": r[5], "str1_v": r[6], "str1_a": r[7],
         "str2_v": r[8], "str2_a": r[9], "string_ratio": r[10],
         "storage_temp_c": r[11], "heater_w": r[12], "wallbox_w": r[13]}
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


def get_daily_for_month(year_month: str) -> list[dict]:
    """Alle Tages-Einträge für einen Monat (Format YYYY-MM)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date, pv_kwh, feed_out_kwh, feed_in_kwh,
                      selfuse_kwh, selfuse_pct, autarky_pct
               FROM pv_daily_log WHERE date LIKE ?
               ORDER BY date ASC""",
            (year_month + "%",),
        ).fetchall()
    return [
        {"date": r[0], "pv_kwh": r[1], "feed_out_kwh": r[2], "feed_in_kwh": r[3],
         "selfuse_kwh": r[4], "selfuse_pct": r[5], "autarky_pct": r[6]}
        for r in rows
    ]


def get_monthly_totals(year: int) -> list[dict]:
    """Monatliche Summen für ein Jahr."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', date) as month,
                      SUM(pv_kwh) as pv_kwh,
                      SUM(feed_out_kwh) as feed_out_kwh,
                      SUM(feed_in_kwh) as feed_in_kwh,
                      SUM(selfuse_kwh) as selfuse_kwh,
                      AVG(selfuse_pct) as selfuse_pct,
                      AVG(autarky_pct) as autarky_pct,
                      COUNT(*) as days
               FROM pv_daily_log WHERE date LIKE ?
               GROUP BY month ORDER BY month ASC""",
            (f"{year}%",),
        ).fetchall()
    return [
        {"month": r[0], "pv_kwh": round(r[1] or 0, 2),
         "feed_out_kwh": round(r[2] or 0, 2), "feed_in_kwh": round(r[3] or 0, 2),
         "selfuse_kwh": round(r[4] or 0, 2),
         "selfuse_pct": round(r[5] or 0, 1), "autarky_pct": round(r[6] or 0, 1),
         "days": r[7]}
        for r in rows
    ]


def get_grid_week_data(end_date: Optional[str] = None) -> dict:
    """Tägliche Netz-Daten (Einspeisung/Bezug) für 7 Tage bis end_date (Standard: heute)."""
    from datetime import date, timedelta
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = end - timedelta(days=6)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date, feed_out_kwh, feed_in_kwh
               FROM pv_daily_log
               WHERE date >= ? AND date <= ?
               ORDER BY date ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "entries": [
            {
                "date": r[0],
                "einspeisung_kwh": round(r[1] or 0, 2),
                "bezug_kwh": round(r[2] or 0, 2),
            }
            for r in rows
        ],
    }


def cleanup_old_hourly(keep_days: int = 90) -> None:
    """Löscht stündliche Einträge älter als keep_days Tage."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM pv_hourly_log WHERE timestamp < date('now', ?)",
            (f"-{keep_days} days",),
        )


def upsert_weather(
    date_str: str,
    sunshine_hours: Optional[float],
    ghi_kwh_m2: Optional[float],
    temp_max: Optional[float],
    temp_min: Optional[float],
    weathercode: Optional[int],
    sunset: Optional[str] = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO weather_log
               (date, sunshine_hours, ghi_kwh_m2, temp_max, temp_min, weathercode, sunset)
               VALUES (?,?,?,?,?,?,?)""",
            (date_str, sunshine_hours, ghi_kwh_m2, temp_max, temp_min, weathercode, sunset),
        )


def get_weather(date_str: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT date, sunshine_hours, ghi_kwh_m2, temp_max, temp_min, weathercode, sunset
               FROM weather_log WHERE date = ?""",
            (date_str,),
        ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0], "sunshine_hours": row[1], "ghi_kwh_m2": row[2],
        "temp_max": row[3], "temp_min": row[4], "weathercode": row[5], "sunset": row[6],
    }


def get_forecast_correlation_data(limit: int = 90) -> list[dict]:
    """Historische (GHI, PV) Paare für lineare Regression – nur Tage mit beiden Werten."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date, pv_kwh, ghi_kwh_m2 FROM pv_daily_log
               WHERE pv_kwh > 0 AND ghi_kwh_m2 > 0
               ORDER BY date DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"date": r[0], "pv_kwh": r[1], "ghi_kwh_m2": r[2]} for r in rows]


def get_forecast_accuracy_data(days: int = 30) -> list[dict]:
    """Prognose-Genauigkeit: für jeden Tag Prognose (vom Vortag) vs. tatsächlicher Ertrag."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT d.date,
                      prev.forecast_kwh AS forecast_kwh,
                      d.pv_kwh          AS actual_kwh
               FROM pv_daily_log d
               LEFT JOIN pv_daily_log prev ON prev.date = date(d.date, '-1 day')
               WHERE d.pv_kwh IS NOT NULL
               ORDER BY d.date DESC
               LIMIT ?""",
            (days,),
        ).fetchall()
    result = []
    for date_str, fc, actual in rows:
        if fc is None or actual is None:
            result.append({"date": date_str, "forecast_kwh": None, "actual_kwh": actual,
                           "diff_kwh": None, "diff_percent": None, "hit": None})
        else:
            diff = round(actual - fc, 2)
            diff_pct = round((actual - fc) / fc * 100) if fc > 0 else None
            today = datetime.now().date().isoformat()
            hit = (actual >= fc) if date_str != today else None
            result.append({"date": date_str, "forecast_kwh": round(fc, 1),
                           "actual_kwh": round(actual, 1), "diff_kwh": diff,
                           "diff_percent": diff_pct, "hit": hit})
    result.reverse()
    return result


def upsert_daily_forecast(
    date_str: str, forecast_kwh: float, forecast_ghi: Optional[float]
) -> None:
    """Schreibt die berechnete Prognose in den pv_daily_log-Eintrag des Datums.

    Legt den Eintrag an falls noch nicht vorhanden; überschreibt nur die
    Prognose-Spalten damit Tageswerte (pv_kwh etc.) erhalten bleiben.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO pv_daily_log (date, forecast_kwh, forecast_ghi)
               VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   forecast_kwh = excluded.forecast_kwh,
                   forecast_ghi = excluded.forecast_ghi""",
            (date_str, forecast_kwh, forecast_ghi),
        )


def backfill_historical_forecasts() -> int:
    """Einmaliger retroaktiver Backfill: füllt forecast_kwh in pv_daily_log wo NULL.

    Für jeden Tag D ohne forecast_kwh: Regression auf pv_daily_log-Daten bis D-1,
    GHI für D+1 aus weather_log → forecast_kwh für D = Prognose für D+1.
    Nur idempotent – überschreibt keine vorhandenen Werte.
    Gibt Anzahl befüllter Einträge zurück.
    """
    with sqlite3.connect(DB_PATH) as conn:
        candidates = conn.execute(
            "SELECT date FROM pv_daily_log WHERE forecast_kwh IS NULL ORDER BY date ASC"
        ).fetchall()
        count = 0
        for (date_str,) in candidates:
            # GHI für den Folgetag (was am Tag date_str als "morgen" vorhergesagt wurde)
            next_day_row = conn.execute(
                "SELECT ghi_kwh_m2 FROM weather_log WHERE date = date(?, '+1 day')",
                (date_str,),
            ).fetchone()
            if not next_day_row or not next_day_row[0]:
                continue
            ghi_next = next_day_row[0]
            # Regression auf Daten vor date_str (exklusiv)
            hist = conn.execute(
                "SELECT ghi_kwh_m2, pv_kwh FROM pv_daily_log "
                "WHERE date < ? AND pv_kwh > 0 AND ghi_kwh_m2 > 0 "
                "ORDER BY date DESC LIMIT 30",
                (date_str,),
            ).fetchall()
            if len(hist) < 3:
                continue
            xs = [r[0] for r in hist]
            ys = [r[1] for r in hist]
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
            var_x = sum((x - mx) ** 2 for x in xs) / n
            if var_x < 1e-6:
                continue
            slope = cov / var_x
            predicted = max(0.0, round(slope * ghi_next + (my - slope * mx), 1))
            conn.execute(
                "UPDATE pv_daily_log SET forecast_kwh = ?, forecast_ghi = ? "
                "WHERE date = ? AND forecast_kwh IS NULL",
                (predicted, ghi_next, date_str),
            )
            count += 1
    return count


def get_daily_for_date(date_str: str) -> Optional[dict]:
    """Einzelner Tageseintrag aus pv_daily_log."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT date, pv_kwh, feed_out_kwh, feed_in_kwh,
                      selfuse_kwh, selfuse_pct, autarky_pct, sunshine_h, ghi_kwh_m2
               FROM pv_daily_log WHERE date = ?""",
            (date_str,),
        ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0], "pv_kwh": row[1], "feed_out_kwh": row[2],
        "feed_in_kwh": row[3], "selfuse_kwh": row[4],
        "selfuse_pct": row[5], "autarky_pct": row[6],
        "sunshine_h": row[7], "ghi_kwh_m2": row[8],
    }


def get_normal_ratio() -> Optional[float]:
    """Berechnet das Normalverhältnis STR1/STR2 aus den ersten 336 Taglichtstunden."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT AVG(string_ratio) FROM (
               SELECT string_ratio FROM pv_hourly_log
               WHERE pv_w > 500
               AND string_ratio > 0.5 AND string_ratio < 3.0
               ORDER BY timestamp ASC LIMIT 336
            )"""
        ).fetchone()
    return round(row[0], 3) if row and row[0] is not None else None


def get_string_ratio_history(days: int = 30) -> list[dict]:
    """Tägliche Durchschnitts-Ratios der letzten N Tage (nur Taglichtstunden)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date(timestamp) as day,
                      AVG(string_ratio) as avg_ratio,
                      MIN(string_ratio) as min_ratio,
                      MAX(string_ratio) as max_ratio,
                      COUNT(*) as samples
               FROM pv_hourly_log
               WHERE pv_w > 500
               AND string_ratio > 0.5 AND string_ratio < 3.0
               AND timestamp >= date('now', ?)
               GROUP BY day ORDER BY day ASC""",
            (f"-{days} days",),
        ).fetchall()
    return [
        {"day": r[0],
         "avg": round(r[1], 3) if r[1] is not None else None,
         "min": round(r[2], 3) if r[2] is not None else None,
         "max": round(r[3], 3) if r[3] is not None else None,
         "samples": r[4]}
        for r in rows
    ]


def log_string_alert(
    level: str, message: str,
    str1_w: int, str2_w: int,
    ratio: Optional[float], normal_ratio: Optional[float],
) -> None:
    """Schreibt einen String-Alert; max. einer pro Level pro 55 Minuten."""
    ts = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            """SELECT id FROM string_alerts
               WHERE level = ? AND timestamp >= datetime('now', '-55 minutes')""",
            (level,),
        ).fetchone()
        if existing:
            return
        conn.execute(
            """INSERT INTO string_alerts
               (timestamp, level, message, str1_w, str2_w, ratio, normal_ratio)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, level, message, str1_w, str2_w, ratio, normal_ratio),
        )


def get_string_alerts(days: int = 30) -> list[dict]:
    """Gibt String-Alerts der letzten N Tage zurück (neueste zuerst)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT id, timestamp, level, message, str1_w, str2_w, ratio, normal_ratio
               FROM string_alerts
               WHERE timestamp >= datetime('now', ?)
               ORDER BY timestamp DESC LIMIT 200""",
            (f"-{days} days",),
        ).fetchall()
    return [
        {"id": r[0], "ts": r[1], "level": r[2], "message": r[3],
         "str1_w": r[4], "str2_w": r[5], "ratio": r[6], "normal_ratio": r[7]}
        for r in rows
    ]


init_db()
init_pv_logging_tables()
backfill_historical_forecasts()


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
    "consecutive_errors, last_status_power_w, last_status_ok, retry_after"
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
                last_status_ok         INTEGER DEFAULT 1,
                retry_after            TEXT
            )
        """)
        for col, definition in (
            ("consecutive_errors",  "INTEGER DEFAULT 0"),
            ("last_status_power_w", "REAL"),
            ("last_status_ok",      "INTEGER DEFAULT 1"),
            ("retry_after",         "TEXT"),
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


def set_cascade_device_retry_after(device_id: str, retry_after: datetime) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices SET retry_after = ?, updated_at = ? WHERE id = ?",
            (
                retry_after.isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
                device_id,
            ),
        )


def get_cascade_devices_due_for_retry() -> list[dict]:
    """Gibt auto-deaktivierte Geräte zurück, deren retry_after in der Vergangenheit liegt."""
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT {_CASCADE_COLS} FROM cascade_devices "
            "WHERE enabled = 0 AND retry_after IS NOT NULL AND retry_after <= ? "
            "ORDER BY priority ASC",
            (now,),
        ).fetchall()
    return [_row_to_device(r) for r in rows]


def auto_reenable_cascade_device(device_id: str) -> None:
    """Reaktiviert ein auto-deaktiviertes Gerät und setzt Fehlerzähler zurück."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE cascade_devices "
            "SET enabled = 1, consecutive_errors = 0, last_status_ok = 1, "
            "    retry_after = NULL, updated_at = ? "
            "WHERE id = ?",
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
