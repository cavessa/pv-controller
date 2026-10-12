"""cascade_log nur noch mit Ereignissen, Retention (db.prune) und Verlauf-API."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

NOW = datetime(2026, 10, 12, 4, 0, 0)                 # lokale Zeit; cascade_log nutzt UTC


def utc(now, **delta):
    return (datetime.fromtimestamp(now.timestamp(), tz=timezone.utc).replace(tzinfo=None) - timedelta(**delta)
            ).strftime("%Y-%m-%d %H:%M:%S")


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        db.init_cascade_tables()
        db.init_cascade_settings_table()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def conn(self):
        return sqlite3.connect(db.DB_PATH)

    def count(self, sql, *params):
        with self.conn() as c:
            return c.execute(sql, params).fetchone()[0]


class EventLoggingTest(TempDb):
    def log(self, action, verbose=False, device="wallbox"):
        return db.log_cascade_action(1000, device, action, f"Grund {action}", 500, verbose=verbose)

    def test_keep_actions_are_not_written_without_verbose(self):
        self.assertFalse(self.log("keep_off"))
        self.assertFalse(self.log("keep_on"))
        self.assertFalse(self.log("budget", device="heizstab"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 0)

    def test_events_are_written(self):
        for a in ("turn_on", "turn_off", "auto_disabled", "auto_reenabled"):
            self.assertTrue(self.log(a))
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 4)

    def test_verbose_writes_keep_like_before(self):
        self.assertTrue(self.log("keep_off", verbose=True))
        self.assertTrue(self.log("keep_off", verbose=True))
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log WHERE action='keep_off'"), 2)

    def test_min_time_blocks_and_override_are_written_once_per_episode(self):
        for _ in range(5):
            self.log("skip_min_off")
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log WHERE action='skip_min_off'"), 1)
        self.log("keep_off")                     # dazwischen eine Minute ohne Sperre
        self.log("skip_min_off")                 # neue Sperre = neuer Eintrag
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log WHERE action='skip_min_off'"), 2)
        for _ in range(3):
            self.log("manual_override")
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log WHERE action='manual_override'"), 1)

    def test_current_decision_is_kept_in_the_device_row(self):
        self.log("keep_off")
        self.log("keep_off")
        dec = db.get_cascade_decisions()["wallbox"]
        self.assertEqual((dec["action"], dec["reason"]), ("keep_off", "Grund keep_off"))
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 0)

    def test_history_api_returns_events_and_empty_is_an_empty_list(self):
        self.assertEqual(db.get_cascade_log(100), [])             # UI zeigt dann „Keine Schaltvorgänge.“
        self.log("turn_on")
        self.log("keep_on")
        entries = db.get_cascade_log(100)
        self.assertEqual([e["action"] for e in entries], ["turn_on"])
        self.assertEqual((entries[0]["device_id"], entries[0]["surplus_watts"]), ("wallbox", 1000))


class PruneTest(TempDb):
    def add_log(self, action, ts):
        with self.conn() as c:
            c.execute("INSERT INTO cascade_log (timestamp, surplus_watts, device_id, action, reason, remaining_watts) "
                      "VALUES (?,?,?,?,?,?)", (ts, 0, "wallbox", action, "x", 0))

    def test_only_strictly_older_rows_are_deleted(self):
        self.add_log("turn_on", utc(NOW, days=365))                       # genau 365 Tage: bleibt
        self.add_log("turn_on", utc(NOW, days=365, seconds=1))            # eine Sekunde älter: weg
        self.add_log("turn_off", utc(NOW, days=10))
        out = db.prune(NOW)
        self.assertEqual(out["cascade_log (Ereignisse)"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 2)

    def test_verbose_rows_are_kept_two_days(self):
        self.add_log("keep_off", utc(NOW, days=2))                        # genau 2 Tage: bleibt
        self.add_log("keep_on", utc(NOW, days=2, seconds=1))
        self.add_log("budget", utc(NOW, days=5))
        self.add_log("turn_on", utc(NOW, days=5))                         # Ereignis: bleibt
        out = db.prune(NOW)
        self.assertEqual(out["cascade_log (keep_*/budget)"], 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log WHERE action IN ('keep_off','turn_on')"), 2)

    def test_hourly_and_minute_tables(self):
        with self.conn() as c:
            c.execute("INSERT INTO pv_hourly_log (timestamp, pv_w, feed_in_w, consumption_w, str1_w, str2_w, str1_v, str1_a, str2_v, str2_a) "
                      "VALUES (?,0,0,0,0,0,0,0,0,0)", ((NOW - timedelta(days=91)).strftime("%Y-%m-%d %H:00"),))
            c.execute("INSERT INTO pv_hourly_log (timestamp, pv_w, feed_in_w, consumption_w, str1_w, str2_w, str1_v, str1_a, str2_v, str2_a) "
                      "VALUES (?,0,0,0,0,0,0,0,0,0)", ((NOW - timedelta(days=89)).strftime("%Y-%m-%d %H:00"),))
            c.execute("INSERT INTO main_meter_min (ts, avg_w, max_w, min_w, n) VALUES (?,1,1,1,1)",
                      ((NOW - timedelta(days=40)).strftime("%Y-%m-%d %H:%M"),))
            c.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
            c.execute("INSERT INTO heater_budget_log (ts, raw_w) VALUES (?,1)", ((NOW - timedelta(days=15)).strftime("%Y-%m-%d %H:%M"),))
            c.execute("INSERT INTO heater_budget_log (ts, raw_w) VALUES (?,1)", ((NOW - timedelta(days=13)).strftime("%Y-%m-%d %H:%M"),))
        db.prune(NOW)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pv_hourly_log"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM main_meter_min"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM heater_budget_log"), 1)

    def test_long_lived_tables_are_untouched(self):
        with self.conn() as c:
            c.execute("INSERT INTO pv_daily_log (date, pv_kwh) VALUES ('2020-01-01', 1.0)")
            c.execute("INSERT INTO weather_log (date) VALUES ('2020-01-01')")
            c.execute("INSERT INTO house_peak_day (date, peak_w, peak_time) VALUES ('2020-01-01', 5, '12:00')")
        db.prune(NOW)
        for t in ("pv_daily_log", "weather_log", "house_peak_day"):
            self.assertEqual(self.count(f"SELECT COUNT(*) FROM {t}"), 1, t)

    def test_batches_delete_everything(self):
        for i in range(25):
            self.add_log("keep_off", utc(NOW, days=10, minutes=i))
        out = db.prune(NOW, batch=10)
        self.assertEqual(out["cascade_log (keep_*/budget)"], 25)
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 0)

    def test_dry_run_only_counts(self):
        self.add_log("keep_off", utc(NOW, days=10))
        out = db.prune(NOW, dry_run=True)
        self.assertEqual(out["cascade_log (keep_*/budget)"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM cascade_log"), 1)

    def test_runs_once_per_day_after_three(self):
        self.add_log("keep_off", utc(NOW, days=10))
        self.assertIsNone(db.maybe_prune(datetime(2026, 10, 12, 2, 59)))          # zu früh
        first = db.maybe_prune(datetime(2026, 10, 12, 3, 0, 5))
        self.assertIsNotNone(first)
        self.assertEqual(first["cascade_log (keep_*/budget)"], 1)
        self.assertIsNone(db.maybe_prune(datetime(2026, 10, 12, 3, 1)))           # am selben Tag nicht nochmal
        self.assertIsNone(db.maybe_prune(datetime(2026, 10, 12, 23, 0)))
        self.assertIsNotNone(db.maybe_prune(datetime(2026, 10, 13, 3, 0)))        # nächster Tag wieder


if __name__ == "__main__":
    unittest.main()
