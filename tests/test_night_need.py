"""Batterie-Größe: Bedarf pro Nacht, Überschuss pro Tag, Wallbox/Heizstab herausgerechnet (night_need.py)."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import night_need as nn

D0 = datetime(2026, 10, 14)            # Tag 0 der Testreihe (00:00)


def ts(day: int, hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return (D0 + timedelta(days=day, hours=h, minutes=m)).strftime("%Y-%m-%d %H:%M")


def fill(target: dict, day: int, frm: str, to: str, value) -> None:
    """Minutenwerte [frm, to) an Tag ``day`` setzen; ``to`` darf am Folgetag liegen (z. B. "33:00")."""
    h1, m1 = map(int, frm.split(":"))
    h2, m2 = map(int, to.split(":"))
    t = D0 + timedelta(days=day, hours=h1, minutes=m1)
    end = D0 + timedelta(days=day, hours=h2, minutes=m2)
    while t < end:
        target[t.strftime("%Y-%m-%d %H:%M")] = value
        t += timedelta(minutes=1)


def standard_days(n_days: int = 3, night_w: float = 500.0, day_w: float = -2000.0) -> dict:
    """Jeden Tag Überschuss 10:00–16:00, sonst Bedarf ``night_w`` (Zähler, ohne Batterie)."""
    meter = {}
    for d in range(n_days):
        fill(meter, d, "00:00", "24:00", night_w)
        fill(meter, d, "10:00", "16:00", day_w)
    return {"meter": meter, "bat": {}, "loads": {}, "hourly": {}}


def run(data, now, days_back=2, **kw):
    return nn.compute(days_back, now=now, loc=None, data=data, **kw)


class NightBoundsTest(unittest.TestCase):
    def test_night_from_last_to_first_surplus(self):
        res = run(standard_days(), now=D0 + timedelta(days=2, hours=20))
        n = next(x for x in res["nights"] if x["date"] == ts(0, "00:00")[:10])
        # Glättung über 15 min verschiebt die Grenzen um höchstens ~7 min
        self.assertAlmostEqual(int(n["start"][:2]) * 60 + int(n["start"][3:]), 16 * 60, delta=8)
        self.assertAlmostEqual(int(n["end"][:2]) * 60 + int(n["end"][3:]), 10 * 60, delta=8)
        self.assertAlmostEqual(n["need_wh"], 500 * 18, delta=150)
        self.assertEqual(n["need_wh"], n["capacity_wh"])
        self.assertTrue(n["complete"])
        self.assertFalse(n["ongoing"])
        self.assertFalse(n["estimated"])

    def test_day_surplus(self):
        res = run(standard_days(), now=D0 + timedelta(days=2, hours=20))
        day = next(x for x in res["days"] if x["date"] == ts(1, "00:00")[:10])
        self.assertAlmostEqual(day["surplus_wh"], 2000 * 6, delta=300)
        self.assertTrue(day["complete"])

    def test_cloudy_day_uses_sun_fallback(self):
        data = standard_days()
        fill(data["meter"], 1, "00:00", "24:00", 400.0)      # Tag 1 ganz ohne Überschuss
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        n0 = next(x for x in res["nights"] if x["date"] == ts(0, "00:00")[:10])
        self.assertEqual(n0["end"], "08:00")                 # ohne Standort: Festwert Sonnenaufgang
        self.assertTrue(n0["estimated"])


class OngoingTest(unittest.TestCase):
    def test_night_running_before_morning_surplus(self):
        res = run(standard_days(), now=D0 + timedelta(days=2, hours=3))
        n = res["nights"][-1]
        self.assertEqual(n["date"], ts(1, "00:00")[:10])
        self.assertTrue(n["ongoing"])
        self.assertFalse(n["complete"])
        self.assertEqual(n["end"], "03:00")

    def test_still_dark_after_sunrise_fallback_keeps_night_open(self):
        data = standard_days()
        fill(data["meter"], 2, "00:00", "24:00", 600.0)      # heute bisher kein Überschuss
        res = run(data, now=D0 + timedelta(days=2, hours=9))   # nach dem Festwert 08:00
        self.assertTrue(res["nights"][-1]["ongoing"])

    def test_no_new_night_before_sunset(self):
        res = run(standard_days(), now=D0 + timedelta(days=2, hours=17))   # Festwert Sonnenuntergang 18:00
        self.assertNotIn(ts(2, "00:00")[:10], [n["date"] for n in res["nights"]])
        today = next(x for x in res["days"] if x["date"] == ts(2, "00:00")[:10])
        self.assertFalse(today["complete"])

    def test_summary_ignores_incomplete(self):
        res = run(standard_days(), now=D0 + timedelta(days=2, hours=3))
        s = nn.summary(res, 8640, 10)
        self.assertEqual(s["nights"], 1)                     # nur die Nacht 0→1


class SplitTest(unittest.TestCase):
    def test_peak_above_discharge_limit(self):
        data = standard_days()
        fill(data["meter"], 0, "20:00", "21:00", 3400.0)
        res = run(data, now=D0 + timedelta(days=2, hours=20), max_discharge_w=2400)
        n = res["nights"][0]
        self.assertAlmostEqual(n["need_wh"] - n["capacity_wh"], 1000, delta=5)

    def test_battery_discharge_counts_as_need(self):
        data = standard_days()
        fill(data["meter"], 0, "20:00", "22:00", 0.0)        # Batterie deckt alles
        fill(data["bat"], 0, "20:00", "22:00", (0.0, 500.0))
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        n = res["nights"][0]
        self.assertAlmostEqual(n["need_wh"], 500 * 18, delta=150)
        self.assertAlmostEqual(n["battery_wh"], 1000, delta=5)

    def test_battery_charging_is_surplus(self):
        data = standard_days()
        fill(data["meter"], 1, "10:00", "16:00", 0.0)        # Überschuss geht komplett in die Batterie
        fill(data["bat"], 1, "10:00", "16:00", (1500.0, 0.0))
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        day = next(x for x in res["days"] if x["date"] == ts(1, "00:00")[:10])
        self.assertAlmostEqual(day["surplus_wh"], 1500 * 6, delta=300)
        self.assertAlmostEqual(day["charged_wh"], 1500 * 6, delta=300)

    def test_wallbox_excluded_from_minutes(self):
        data = standard_days()
        fill(data["meter"], 0, "20:00", "24:00", 4800.0)     # 4300 W Auto + 500 W Haus
        fill(data["loads"], 0, "20:00", "24:00", 4300.0)
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        n = res["nights"][0]
        self.assertAlmostEqual(n["need_wh"], 500 * 18, delta=150)
        self.assertAlmostEqual(n["excluded_wh"], 4300 * 4, delta=20)
        self.assertFalse(n["loads_estimated"])

    def test_wallbox_from_hourly_when_minutes_missing(self):
        data = standard_days()
        fill(data["meter"], 0, "20:00", "22:00", 4800.0)
        data["hourly"] = {ts(0, "20:00")[:13]: 4300.0, ts(0, "21:00")[:13]: 4300.0}
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        n = res["nights"][0]
        self.assertAlmostEqual(n["excluded_wh"], 4300 * 2, delta=20)
        self.assertTrue(n["loads_estimated"])

    def test_load_larger_than_net_never_negative(self):
        data = standard_days()
        data["hourly"] = {ts(0, "20:00")[:13]: 9000.0}       # Stundenwert zu hoch
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        n = res["nights"][0]
        self.assertAlmostEqual(n["need_wh"], 500 * 17, delta=150)
        self.assertAlmostEqual(n["excluded_wh"], 500, delta=10)

    def test_night_cut_by_data_start_is_dropped(self):
        data = standard_days()
        for k in [k for k in data["meter"] if k < ts(0, "22:00")]:
            del data["meter"][k]                              # Daten beginnen mitten in Nacht 0
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        self.assertNotIn(ts(0, "00:00")[:10], [n["date"] for n in res["nights"]])
        self.assertIn(ts(1, "00:00")[:10], [n["date"] for n in res["nights"]])

    def test_night_cut_by_data_end_is_dropped(self):
        data = standard_days()
        for k in [k for k in data["meter"] if ts(1, "05:00") <= k < ts(1, "10:00")]:
            del data["meter"][k]
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        self.assertNotIn(ts(0, "00:00")[:10], [n["date"] for n in res["nights"]])

    def test_gap_marks_incomplete(self):
        data = standard_days()
        for k in [k for k in data["meter"] if ts(0, "18:00") <= k < ts(0, "23:00")]:
            del data["meter"][k]
        res = run(data, now=D0 + timedelta(days=2, hours=20))
        self.assertFalse(res["nights"][0]["complete"])


class SummaryTest(unittest.TestCase):
    def test_sizes(self):
        res = {"nights": [
            {"date": "2026-10-14", "need_wh": 12000, "capacity_wh": 11000, "complete": True},
            {"date": "2026-10-15", "need_wh": 8000, "capacity_wh": 7000, "complete": True},
            {"date": "2026-10-16", "need_wh": 99000, "capacity_wh": 99000, "complete": False},
        ], "days": [
            {"date": "2026-10-14", "surplus_wh": 9000, "complete": True},
            {"date": "2026-10-15", "surplus_wh": 20000, "complete": True},
        ]}
        s = nn.summary(res, 8640, 10)
        self.assertEqual(s["nights"], 2)
        self.assertEqual(s["need_avg_wh"], 10000)
        self.assertEqual(s["capacity_max_wh"], 11000)
        self.assertEqual(s["peak_avg_wh"], 1000)
        self.assertEqual(s["usable_now_wh"], round(8640 * 0.9 * 0.95))
        self.assertEqual(s["size_for_max_wh"], round(11000 / 0.9 / 0.95))
        self.assertEqual(s["fillable_max_wh"], 9000)          # min(11000, 9000), min(7000, 20000)
        self.assertEqual(s["nights_covered"], 1)              # 7000 ≤ 7387

    def test_empty(self):
        s = nn.summary({"nights": [], "days": []}, 8640, 10)
        self.assertEqual(s["nights"], 0)
        self.assertNotIn("need_avg_wh", s)


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def test_record_and_load(self):
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 14, 21, 5, 30)
            db.record_loads(4300.0, None)
            db.record_loads(4200.0, 0.0)                     # gleiche Minute: ersetzt
        with sqlite3.connect(db.DB_PATH) as c:
            c.execute("INSERT INTO main_meter_min (ts, avg_w, max_w, min_w, n) VALUES ('2026-10-14 21:05', 4700, 0, 0, 1)")
        data = nn.load_data(datetime(2026, 10, 14).date(), datetime(2026, 10, 14).date())
        self.assertEqual(data["loads"], {"2026-10-14 21:05": 4200.0})
        self.assertEqual(data["meter"], {"2026-10-14 21:05": 4700.0})

    def test_retention(self):
        with sqlite3.connect(db.DB_PATH) as c:
            c.execute("INSERT INTO load_min (ts, wallbox_w, heater_w) VALUES ('2026-08-01 10:00', 1, 1)")
            c.execute("INSERT INTO load_min (ts, wallbox_w, heater_w) VALUES ('2026-10-10 10:00', 1, 1)")
        out = db.prune(datetime(2026, 10, 14, 4, 0))
        self.assertEqual(out["load_min"], 1)

    def test_api(self):
        import web
        from fastapi import Response
        from tests.test_solax_timeout import EXAMPLE
        cfg = json.loads(EXAMPLE.read_text())
        cfg["zendure"] = {"enabled": True, "ip": "1.2.3.4", "capacity_wh": 8640, "min_soc": 10}
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(cfg))
        with mock.patch.object(web, "CONFIG_PATH", path):
            j = web.api_battery_nights(Response(), days=3)
        self.assertTrue(j["enabled"])
        self.assertEqual(j["summary"]["capacity_now_wh"], 8640)
        self.assertEqual(j["max_discharge_w"], 2400)
        self.assertEqual(j["nights"], [])

if __name__ == "__main__":
    unittest.main()
