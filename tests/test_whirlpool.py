"""Whirlpool-Zähler: Abzug der mitlaufenden Heizstab-Phase, Config, Client, Speicherung, Anzeige."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import night_need as nn
import whirlpool
from clients import shelly_client
from clients.shelly_client import Shelly3EMClient
from config import WhirlpoolMeterConfig, load_config
from models import Readings
from tests.test_main_meter import fake_get
from tests.test_night_need import D0, fill, run, standard_days, ts
from tests.test_solax_timeout import EXAMPLE


class ComputeTest(unittest.TestCase):
    def test_no_subtract_is_sum(self):
        w = whirlpool.compute([10.0, 2000.0, -3.0], [0, 1500, 0], "")
        self.assertEqual((w["power_w"], w["raw_w"], w["uncertain"], w["reachable"]), (2007.0, 2007.0, False, True))

    def test_subtract_heater_phase(self):
        w = whirlpool.compute([0.0, 1546.0, -3.0], [0.0, 1500.0, 0.0], "L2")
        self.assertEqual(w["power_w"], 43.0)
        self.assertEqual(w["heater_phase_w"], 1500.0)

    def test_never_below_zero_and_negative_heater_ignored(self):
        self.assertEqual(whirlpool.compute([0.0, 1400.0, 0.0], [0, 1500, 0], "L2")["power_w"], 0.0)
        self.assertEqual(whirlpool.compute([0.0, 40.0, 0.0], [0, -5, 0], "L2")["power_w"], 40.0)

    def test_heater_meter_offline_is_uncertain(self):
        w = whirlpool.compute([0.0, 1546.0, 0.0], None, "L2")
        self.assertIsNone(w["power_w"])
        self.assertTrue(w["uncertain"])
        self.assertEqual(w["raw_w"], 1546.0)

    def test_whirlpool_offline(self):
        w = whirlpool.compute(None, [0, 0, 0], "L2")
        self.assertEqual((w["power_w"], w["reachable"], w["uncertain"]), (None, False, False))


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            WhirlpoolMeterConfig(enabled=True, url="http://x", subtract_heater_phase="L4")
        with self.assertRaises(ValueError):
            WhirlpoolMeterConfig(enabled=True, url="")
        with self.assertRaises(ValueError):
            WhirlpoolMeterConfig(type="foo")
        self.assertEqual(WhirlpoolMeterConfig(enabled=True, url="http://x", subtract_heater_phase="L2")
                         .subtract_heater_phase, "L2")

    def test_load_config(self):
        d = Path(tempfile.mkdtemp())
        try:
            raw = json.loads(EXAMPLE.read_text())
            self.assertFalse(load_config(EXAMPLE).whirlpool_active)          # Beispiel: aus
            raw["whirlpool_meter"] = {"enabled": True, "url": "http://192.0.2.164", "subtract_heater_phase": "L2"}
            (d / "c.json").write_text(json.dumps(raw))
            cfg = load_config(d / "c.json")
            self.assertTrue(cfg.whirlpool_active)
            self.assertEqual(cfg.whirlpool_meter.subtract_heater_phase, "L2")
        finally:
            shutil.rmtree(d)


class ClientTest(unittest.TestCase):
    def test_gen1_phases_share_status(self):
        calls = []
        routes = {"/status": {"total_power": 1543.0, "emeters": [{"power": 0}, {"power": 1546.2}, {"power": -3}]}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, calls)):
            c = Shelly3EMClient("http://w", name="whirlpool_meter")
            self.assertEqual(c.get_total_power_w(), 1543.0)
            self.assertEqual(c.get_phase_powers_w(), [0.0, 1546.2, -3.0])
        self.assertEqual(len(calls), 1)                                     # ein /status-Abruf

    def test_pro_phases(self):
        calls = []
        routes = {"/rpc/EM.GetStatus": {"a_act_power": 1, "b_act_power": 2, "c_act_power": 3}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, calls)):
            self.assertEqual(Shelly3EMClient("http://w", type_="shelly_pro_3em").get_phase_powers_w(), [1.0, 2.0, 3.0])

    def test_offline_none(self):
        with mock.patch.object(shelly_client.requests, "get", fake_get({}, [])):
            self.assertIsNone(Shelly3EMClient("http://w").get_phase_powers_w())


class ControllerTest(unittest.TestCase):
    def _ctrl(self, sub="L2"):
        from controller import Controller
        c = Controller.__new__(Controller)
        c.cfg = mock.Mock()
        c.cfg.whirlpool_meter = WhirlpoolMeterConfig(enabled=True, url="http://w", subtract_heater_phase=sub)
        c.heater_meter = mock.Mock()
        return c

    def test_subtracts_heater_phase(self):
        c = self._ctrl()
        c.heater_meter.get_phase_powers_w.return_value = [0.0, 1500.0, 0.0]
        r = Readings(heater_meter_power_w=1500.0)
        c._apply_whirlpool(r, [0.0, 1546.0, -3.0])
        self.assertEqual((r.whirlpool_power_w, r.whirlpool_raw_w, r.whirlpool_uncertain), (43.0, 1543.0, False))

    def test_heater_meter_offline_no_second_request(self):
        c = self._ctrl()
        r = Readings(heater_meter_power_w=None)
        c._apply_whirlpool(r, [0.0, 1546.0, 0.0])
        c.heater_meter.get_phase_powers_w.assert_not_called()
        self.assertIsNone(r.whirlpool_power_w)
        self.assertTrue(r.whirlpool_uncertain)
        self.assertEqual(r.errors, [])                                      # kein Fail-safe

    def test_without_subtract_no_heater_read(self):
        c = self._ctrl(sub="")
        r = Readings(heater_meter_power_w=1500.0)
        c._apply_whirlpool(r, [0.0, 46.0, -3.0])
        c.heater_meter.get_phase_powers_w.assert_not_called()
        self.assertEqual(r.whirlpool_power_w, 43.0)


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def test_migration_adds_column(self):
        with sqlite3.connect(db.DB_PATH) as c:                              # Tabelle im alten Format
            c.execute("CREATE TABLE load_min (ts TEXT PRIMARY KEY, wallbox_w REAL, heater_w REAL)")
            c.execute("INSERT INTO load_min VALUES ('2026-10-17 08:24', 0, 0)")
        db.init_db()
        db.init_pv_logging_tables()
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 17, 22, 0, 5)
            db.record_loads(0.0, 0.0, 2100.0)
        with sqlite3.connect(db.DB_PATH) as c:
            rows = c.execute("SELECT ts, whirlpool_w FROM load_min ORDER BY ts").fetchall()
        self.assertEqual(rows, [("2026-10-17 08:24", None), ("2026-10-17 22:00", 2100.0)])
        data = nn.load_data(datetime(2026, 10, 17).date(), datetime(2026, 10, 17).date())
        self.assertEqual(data["whirl"], {"2026-10-17 22:00": 2100.0})


class NightShareTest(unittest.TestCase):
    def test_whirlpool_share_and_peak(self):
        data = standard_days()
        fill(data["meter"], 0, "22:00", "23:00", 3500.0)     # 500 W Haus + 3000 W Whirlpool
        data["whirl"] = {}
        fill(data["whirl"], 0, "16:00", "34:00", 0.0)         # Messwerte die ganze Nacht
        fill(data["whirl"], 0, "22:00", "23:00", 3000.0)
        res = run(data, now=D0 + timedelta(days=2, hours=20), max_discharge_w=2400)
        n = res["nights"][0]
        self.assertAlmostEqual(n["whirlpool_wh"], 3000, delta=10)
        self.assertAlmostEqual(n["whirlpool_peak_wh"], 1100, delta=10)    # 3500 − 2400
        self.assertGreaterEqual(n["whirlpool_coverage"], 0.9)

    def test_no_whirlpool_data_is_none(self):
        n = run(standard_days(), now=D0 + timedelta(days=2, hours=20))["nights"][0]
        self.assertIsNone(n["whirlpool_wh"])
        self.assertEqual(n["whirlpool_coverage"], 0)


class WebTest(unittest.TestCase):
    def setUp(self):
        import web
        self.web = web
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads(EXAMPLE.read_text())
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        self.cfg_path.write_text(json.dumps(raw))
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def put(self, **wm):
        return self.web.api_config_put(self.web.ConfigUpdate(whirlpool_meter=wm))

    def test_settings_saved(self):
        self.put(enabled=True, url=" http://192.0.2.164 ", subtract_heater_phase="L2")
        saved = json.loads(self.cfg_path.read_text())["whirlpool_meter"]
        self.assertEqual((saved["url"], saved["subtract_heater_phase"], saved["enabled"]),
                         ("http://192.0.2.164", "L2", True))

    def test_settings_rejected(self):
        from fastapi import HTTPException
        for bad in ({"subtract_heater_phase": "L5"}, {"enabled": True, "url": ""}, {"relay": True},
                    {"enabled": "ja"}):
            with self.assertRaises(HTTPException):
                self.put(**bad)

    def test_status_block(self):
        cfg = load_config(self.cfg_path)
        self.assertEqual(self.web._whirlpool_status_block(cfg, Readings()), {"enabled": False})
        cfg.whirlpool_meter = WhirlpoolMeterConfig(enabled=True, url="http://w", subtract_heater_phase="L2")
        r = Readings(whirlpool_power_w=None, whirlpool_raw_w=1546.0, whirlpool_uncertain=True,
                     whirlpool_reachable=True)
        b = self.web._whirlpool_status_block(cfg, r)
        self.assertTrue(b["uncertain"])
        self.assertEqual(b["subtract_heater_phase"], "L2")

    def test_explain_context(self):
        import explain
        line = explain.energy_line({"energy": {"pv_w": 0, "grid_w": 100, "whirlpool_w": 2100}})
        self.assertIn("Whirlpool 2100 W", line)
        line = explain.energy_line({"energy": {"pv_w": 0, "grid_w": 100, "whirlpool_w": None,
                                               "whirlpool_uncertain": True}})
        self.assertIn("Whirlpool unsicher", line)


if __name__ == "__main__":
    unittest.main()
