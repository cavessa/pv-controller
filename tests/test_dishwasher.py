"""Geschirrspüler: Startbedingungen, Überschuss, Frist, ein Startbefehl, Fehlstart, über Nacht, Dry-Run, Lernen."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import dishwasher as dw
import reasons
from config import DishwasherConfig, load_config
from tests.test_solax_timeout import EXAMPLE

ITEMS = {"remote_start": "DW_Remote", "operation_state": "DW_Op", "selected_program": "DW_Prog",
         "door": "DW_Door", "remaining": "DW_Rem", "action": "DW_Action"}
T0 = datetime(2026, 10, 18, 9, 0)             # Sonntag 09:00


def cfg(**kw):
    base = dict(enabled=True, items=dict(ITEMS), openhab_url="http://oh:8080")
    base.update(kw)
    return DishwasherConfig(**base)


def ready(**over):
    o = {"remote_start": True, "op": "Ready", "program": "Dishcare.Dishwasher.Program.Eco50", "door": "CLOSED",
         "remaining_s": None}
    o.update(over)
    return o


SUN = [2000.0] * 5
DARK = [100.0] * 5


def step(c, state, obs, now, series=DARK, learned=None):
    return dw.decide(c, state, obs, now, series, learned or {})


class ReadinessTest(unittest.TestCase):
    def test_each_condition(self):
        c = cfg()
        for over, text in (({"remote_start": False}, "Fernstart nicht erlaubt"), ({"door": "OPEN"}, "Tür offen"),
                           ({"program": None}, "kein Programm gewählt"), ({"op": "Inactive"}, "Gerät nicht bereit")):
            _st, s, _ = step(c, {}, ready(**over), T0, SUN)
            self.assertEqual(s["code"], "dw_not_ready", over)
            self.assertIn(text, s["data"]["missing"])
            self.assertNotIn("action", s)

    def test_running_or_delayed_does_nothing(self):
        c = cfg()
        _st, s, _ = step(c, {}, ready(op="Run", remaining_s=3600), T0, SUN)
        self.assertEqual((s["code"], s["data"]["by"], s["data"]["remaining"]), ("dw_running", "device", "1:00 h"))
        _st, s, _ = step(c, {}, ready(op="DelayedStart"), T0, SUN)
        self.assertEqual(s["code"], "dw_delayed_start")
        self.assertNotIn("action", s)

    def test_unreadable(self):
        self.assertEqual(step(cfg(), {}, None, T0)[1]["code"], "dw_unreadable")


class SurplusTest(unittest.TestCase):
    def test_too_short_and_enough(self):
        c = cfg()
        _st, s, _ = step(c, {}, ready(), T0, [100, 2000, 2000, 2000, 2000])
        self.assertEqual(s["code"], "dw_waiting_surplus")
        self.assertEqual(s["data"]["surplus_w"], 2000)
        _st, s, _ = step(c, {}, ready(), T0, [2000, 2000, None, 2000, 2000])   # Lücke = nicht genug
        self.assertEqual(s["code"], "dw_waiting_surplus")
        _st, s, _ = step(c, {}, ready(), T0, [1500, 1600, 2000, 1800, 1700])
        self.assertEqual((s["code"], s["action"], s["by"]), ("dw_started_surplus", "start", "surplus"))
        self.assertEqual(s["data"]["surplus_w"], 1500)

    def test_series_counts_battery_charge(self):
        tmp = tempfile.TemporaryDirectory()
        old = db.DB_PATH
        db.DB_PATH = Path(tmp.name) / "t.db"
        try:
            db.init_db()
            db.init_pv_logging_tables()
            with sqlite3.connect(db.DB_PATH) as conn:
                for i in range(1, 4):
                    ts = (T0 - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                    conn.execute("INSERT INTO main_meter_min (ts, avg_w, max_w, min_w, n) VALUES (?,?,?,?,1)",
                                 (ts, -300.0, 0, 0))
                    conn.execute("INSERT INTO zendure_min (ts, soc, charge_w, discharge_w, n) VALUES (?,50,1400,0,1)",
                                 (ts,))
            self.assertEqual(dw.surplus_series(T0, 4), [None, 1700.0, 1700.0, 1700.0])
        finally:
            db.DB_PATH = old
            tmp.cleanup()


class HeaterYieldsTest(unittest.TestCase):
    def config(self, summer=True, winter_min=None):
        c = mock.Mock()
        c.summer_mode.enabled = summer
        c.comfort_min_for.side_effect = lambda season: 50.0 if season == "summer" else winter_min
        return c

    def test_only_in_stage_max(self):
        d, c = cfg(), self.config()
        self.assertTrue(d.heater_yields)                                     # Default an
        self.assertTrue(dw.heater_yields_now(d, c, 55.0))                    # über Min → „bis Max“
        self.assertTrue(dw.heater_yields_now(d, c, 50.0))
        self.assertFalse(dw.heater_yields_now(d, c, 45.0))                   # „bis Min“: Heizstab hat Vorrang
        self.assertFalse(dw.heater_yields_now(d, c, None))                   # Temperatur unbekannt
        self.assertFalse(dw.heater_yields_now(cfg(heater_yields=False), c, 55.0))   # Schalter aus
        self.assertTrue(dw.heater_yields_now(d, self.config(summer=False), 30.0))   # Winter ohne Min-Stufe
        self.assertFalse(dw.heater_yields_now(d, self.config(summer=False, winter_min=40.0), 30.0))

    def test_series_adds_heater_and_starts(self):
        tmp = tempfile.TemporaryDirectory()
        old = db.DB_PATH
        db.DB_PATH = Path(tmp.name) / "t.db"
        try:
            db.init_db()
            db.init_pv_logging_tables()
            with sqlite3.connect(db.DB_PATH) as conn:
                for i in range(1, 6):
                    ts = (T0 - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                    conn.execute("INSERT INTO main_meter_min (ts, avg_w, max_w, min_w, n) VALUES (?,-100,0,0,1)", (ts,))
                    conn.execute("INSERT INTO load_min (ts, wallbox_w, heater_w) VALUES (?,0,3000)", (ts,))
            without = dw.surplus_series(T0, 5)
            with_h = dw.surplus_series(T0, 5, with_heater=True)
            self.assertEqual(without, [100.0] * 5)
            self.assertEqual(with_h, [3100.0] * 5)
            _st, s, _ = step(cfg(), {}, ready(), T0, without)
            self.assertEqual(s["code"], "dw_waiting_surplus")                # Heizstab nimmt alles
            _st, s, _ = dw.decide(cfg(), {}, ready(), T0, with_h, {}, heater_yields=True)
            self.assertEqual(s["code"], "dw_started_surplus")
            self.assertIn("Heizstab „bis Max“", reasons.text(s["code"], s["data"]))
        finally:
            db.DB_PATH = old
            tmp.cleanup()


class DeadlineTest(unittest.TestCase):
    def test_latest_start(self):
        c = cfg(latest_start="14:00")
        st, s, _ = step(c, {}, ready(), T0)
        self.assertEqual((s["code"], s["data"]["deadline"], s["data"]["day"]), ("dw_waiting_surplus", "14:00", "heute"))
        _st, s, _ = step(c, st, ready(), T0.replace(hour=14))
        self.assertEqual((s["code"], s["by"]), ("dw_started_deadline", "deadline"))

    def test_finish_by_uses_learned_duration(self):
        c = cfg(deadline_mode="finish_by", finish_by="18:00")
        _st, s, _ = step(c, {}, ready(), T0)
        self.assertEqual(s["data"]["deadline"], "13:05")                    # 18:00 − 295 min (Startwert Eco50)
        _st, s, _ = step(c, {}, ready(), T0, learned={"Eco50": 240.0})
        self.assertEqual(s["data"]["deadline"], "14:00")
        _st, s, _ = step(c, {}, ready(program="X.Y.Intensiv70"), T0)
        self.assertEqual(s["data"]["deadline"], "15:00")                    # Default 180 min

    def test_armed_after_deadline_waits_for_tomorrow(self):
        c = cfg(latest_start="14:00")
        evening = T0.replace(hour=20)
        st, s, _ = step(c, {}, ready(), evening, SUN)                     # Sonne zählt heute nicht mehr
        self.assertEqual((s["code"], s["data"]["day"], s["data"]["deadline"]), ("dw_waiting_tomorrow", "morgen", "14:00"))
        st, s, _ = step(c, st, ready(), T0.replace(hour=23, minute=59), SUN)
        self.assertEqual(s["code"], "dw_waiting_tomorrow")
        night = T0 + timedelta(days=1, hours=-6)                           # 03:00 am Zieltag
        st, s, _ = step(c, st, ready(), night, DARK)
        self.assertEqual(s["code"], "dw_waiting_surplus")                  # nicht über Nacht starten
        st, s, _ = step(c, st, ready(), T0 + timedelta(days=1, hours=2), SUN)
        self.assertEqual(s["code"], "dw_started_surplus")

    def test_finish_by_not_reachable_today(self):
        c = cfg(deadline_mode="finish_by", finish_by="18:00")
        _st, s, _ = step(c, {}, ready(), T0.replace(hour=13, minute=30))  # 13:30 > 13:05
        self.assertEqual(s["code"], "dw_waiting_tomorrow")

    def test_missed_deadline_catchup_then_next_day(self):
        c = cfg(latest_start="14:00")
        armed = {"armed_at": T0.isoformat()}
        _st, s, _ = step(c, armed, ready(), T0.replace(hour=15, minute=30))   # 1,5 h zu spät: nachholen
        self.assertEqual(s["code"], "dw_started_deadline")
        _st, s, _ = step(c, armed, ready(), T0.replace(hour=17))              # 3 h zu spät: morgen
        self.assertEqual(s["code"], "dw_waiting_tomorrow")


class OneStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def client(self, **over):
        states = {"DW_Remote": "ON", "DW_Op": "Ready", "DW_Prog": "Dishcare.Dishwasher.Program.Eco50",
                  "DW_Door": "CLOSED", "DW_Rem": "UNDEF"}
        states.update(over)
        cl = mock.Mock()
        cl.get_states.return_value = states
        return cl

    def run_at(self, now, cl, series=SUN, dry=False, c=None):
        with mock.patch.object(dw, "surplus_series", return_value=series):
            return dw.run(c or cfg(), dry, now=now, client=cl)

    def test_exactly_one_start_and_confirm(self):
        cl = self.client()
        s = self.run_at(T0, cl)
        self.assertEqual(s["code"], "dw_started_surplus")
        cl.send_command.assert_called_once_with("DW_Action", "start")
        s = self.run_at(T0 + timedelta(minutes=1), cl)                   # noch Ready, Sonne weiter da
        self.assertEqual(s["code"], "dw_start_sent")
        self.assertEqual(cl.send_command.call_count, 1)
        cl.get_states.return_value["DW_Op"] = "Run"
        cl.get_states.return_value["DW_Rem"] = "17700 s"
        s = self.run_at(T0 + timedelta(minutes=2), cl)
        self.assertEqual((s["code"], s["data"]["by"], s["data"]["remaining"]), ("dw_running", "surplus", "4:55 h"))
        self.assertEqual(cl.send_command.call_count, 1)

    def test_start_fails_no_retry(self):
        cl = self.client()
        self.run_at(T0, cl)
        for m in (1, 3, 10, 60):
            s = self.run_at(T0 + timedelta(minutes=m), cl)
        self.assertEqual(s["code"], "dw_start_failed")
        self.assertIn("Run", s["data"]["detail"])
        self.assertEqual(cl.send_command.call_count, 1)
        cl.get_states.return_value["DW_Remote"] = "OFF"                   # Freigabe aus → zurückgesetzt
        self.run_at(T0 + timedelta(minutes=61), cl)
        cl.get_states.return_value["DW_Remote"] = "ON"
        s = self.run_at(T0 + timedelta(minutes=62), cl)
        self.assertEqual(s["code"], "dw_started_surplus")                  # neue Freigabe, neuer Start
        self.assertEqual(cl.send_command.call_count, 2)

    def test_command_error_no_retry(self):
        cl = self.client()
        cl.send_command.side_effect = RuntimeError("HTTP 500")
        s = self.run_at(T0, cl)
        self.assertEqual(s["code"], "dw_start_failed")
        s = self.run_at(T0 + timedelta(minutes=1), cl)
        self.assertEqual(s["code"], "dw_start_failed")
        self.assertEqual(cl.send_command.call_count, 1)

    def test_dry_run_never_sends(self):
        cl = self.client()
        for dry, c in ((True, cfg()), (False, cfg(dry_run=True))):
            dw.save_state({})
            s = self.run_at(T0, cl, dry=dry, c=c)
            self.assertEqual(s["code"], "dw_start_simulated")
            s = self.run_at(T0 + timedelta(minutes=10), cl, dry=dry, c=c)
            self.assertEqual(s["code"], "dw_start_simulated")             # kein Fehlstart, kein zweiter Versuch
        cl.send_command.assert_not_called()
        res = dw.start_now(cfg(), True, now=T0, client=self.client())
        self.assertTrue(res["ok"] and res["dry_run"])
        cl.send_command.assert_not_called()

    def test_no_auto_and_manual_start(self):
        cl = self.client()
        dw.set_no_auto(True)
        s = self.run_at(T0, cl)
        self.assertEqual(s["code"], "dw_no_auto")
        cl.send_command.assert_not_called()
        res = dw.start_now(cfg(), False, now=T0, client=cl)
        self.assertTrue(res["ok"])
        self.assertEqual(res["code"], "dw_started_manual")
        cl.send_command.assert_called_once_with("DW_Action", "start")
        self.assertEqual(dw.start_now(cfg(), False, now=T0, client=self.client(DW_Door="OPEN"))["error"],
                         "nicht startbereit: Tür offen")

    def test_learn_duration(self):
        cl = self.client(DW_Op="Run", DW_Remote="OFF")
        self.run_at(T0, cl)
        cl.get_states.return_value["DW_Op"] = "Finished"
        self.run_at(T0 + timedelta(minutes=250), cl)
        self.assertEqual(dw.learned_minutes(), {"Eco50": 250.0})
        for start, mins in ((1, 280), (2, 300), (3, 310)):
            dw.record_run({"start": (T0 + timedelta(days=start)).isoformat(), "program": "Eco50", "minutes": mins})
        self.assertAlmostEqual(dw.learned_minutes()["Eco50"], (280 + 300 + 310) / 3)
        cl.get_states.return_value["DW_Op"] = "Run"                         # abgebrochen: nichts lernen
        self.run_at(T0 + timedelta(days=5), cl)
        cl.get_states.return_value["DW_Op"] = "Ready"
        self.run_at(T0 + timedelta(days=5, minutes=10), cl)
        self.assertAlmostEqual(dw.learned_minutes()["Eco50"], (280 + 300 + 310) / 3)


class ConfigWebTest(unittest.TestCase):
    def test_validation(self):
        for bad in ({"latest_start": "25:00"}, {"deadline_mode": "x"}, {"surplus_minutes": 0},
                    {"start_surplus_w": 50}, {"items": {"foo": "X"}}, {"items": {"door": "a b"}}):
            with self.assertRaises(ValueError, msg=bad):
                DishwasherConfig(**bad)
        with self.assertRaises(ValueError):
            DishwasherConfig(enabled=True, items={"door": "X"}, openhab_url="http://oh")   # Items fehlen
        self.assertFalse(load_config(EXAMPLE).dishwasher_active)

    def test_reason_codes_known(self):
        for code in ("dw_waiting_surplus", "dw_waiting_tomorrow", "dw_started_surplus", "dw_started_deadline",
                     "dw_started_manual", "dw_start_sent", "dw_start_simulated", "dw_start_failed", "dw_not_ready",
                     "dw_running", "dw_delayed_start", "dw_no_auto", "dw_unreadable"):
            self.assertTrue(reasons.is_known(code), code)
        t = reasons.text("dw_not_ready", {"missing": ["Tür offen", "kein Programm gewählt"]})
        self.assertEqual(t, "Nicht startbereit: Tür offen, kein Programm gewählt")

    def test_settings_and_start_endpoint(self):
        import web
        from fastapi import HTTPException
        d = Path(tempfile.mkdtemp())
        try:
            raw = json.loads(EXAMPLE.read_text())
            raw["runtime"]["log_file"] = str(d / "app.log")
            raw["openhab"] = {"url": "http://oh:8080", "token": "", "timeout_s": 3}
            (d / "config.json").write_text(json.dumps(raw))
            with mock.patch.object(web, "CONFIG_PATH", d / "config.json"):
                web.api_config_put(web.ConfigUpdate(dishwasher={"enabled": True, "items": ITEMS,
                                                                "deadline_mode": "finish_by", "finish_by": "17:30",
                                                                "heater_yields": False}))
                saved = json.loads((d / "config.json").read_text())["dishwasher"]
                self.assertEqual((saved["finish_by"], saved["items"]["action"], saved["heater_yields"]),
                                 ("17:30", "DW_Action", False))
                for bad in ({"latest_start": "9"}, {"items": {"door": 5}}, {"cmd": "start"}, {"enabled": "ja"},
                            {"heater_yields": "ja"}):
                    with self.assertRaises(HTTPException, msg=bad):
                        web.api_config_put(web.ConfigUpdate(dishwasher=bad))
                with self.assertRaises(HTTPException):
                    web.dishwasher_start(web.DishwasherStart(confirm=False))    # ohne Bestätigung nie
        finally:
            shutil.rmtree(d)


class SnapshotTest(unittest.TestCase):
    def test_entry_and_explain(self):
        import decisions
        import explain
        c = mock.Mock(dishwasher_active=True)
        now = datetime(2026, 10, 18, 9, 0, 30)
        st = {"status": {"code": "dw_waiting_surplus", "ts": "2026-10-18T09:00:05",
                         "data": {"surplus_w": 800, "threshold_w": 1500, "minutes": 5, "day": "heute",
                                  "deadline": "14:00"}}}
        with mock.patch.object(dw, "load_state", return_value=st):
            e = decisions._dishwasher_entry(c, now)
        self.assertEqual((e["id"], e["reason"]), ("dishwasher", "dw_waiting_surplus"))
        line = explain.device_line({"devices": [e]}, e)
        self.assertIn("Geschirrspüler", line)
        self.assertIn("nötig 1500 W über 5 min; startet spätestens heute 14:00", line)
        with mock.patch.object(dw, "load_state", return_value=st):
            self.assertIsNone(decisions._dishwasher_entry(c, now + timedelta(minutes=10)))   # veraltet


if __name__ == "__main__":
    unittest.main()
