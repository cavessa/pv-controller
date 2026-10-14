"""Hauptzähler: Typ Shelly 3EM (Gen1) / Pro 3EM (Gen2), Vorzeichen, Zählerwechsel ohne Sprung, Settings."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import meter
import web
from clients import shelly_client
from clients.shelly_client import Shelly3EMClient
from config import ShellyConfig, load_config
from controller import Controller
from models import Readings
from tests.test_solax_timeout import EXAMPLE


class Resp:
    def __init__(self, j):
        self.j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self.j


def fake_get(routes, calls):
    def get(url, params=None, timeout=None):
        calls.append((url, params))
        for suffix, j in routes.items():
            if url.endswith(suffix):
                if isinstance(j, Exception):
                    raise j
                return Resp(j)
        raise shelly_client.requests.ConnectionError("unbekannt: " + url)
    return get


class ConfigTest(unittest.TestCase):
    def base(self, **kw):
        return ShellyConfig(ph1_url="u", ph2_url="u", ph3_url="u", storage_url="u", main_meter_url="u",
                            heater_meter_url="u", **kw)

    def test_default_is_gen1_and_pro_is_allowed(self):
        self.assertEqual(self.base().main_meter_type, "shelly_3em_gen1")
        self.assertEqual(self.base(main_meter_type="shelly_pro_3em").main_meter_type, "shelly_pro_3em")
        with self.assertRaises(ValueError):
            self.base(main_meter_type="shelly_gen2")

    def test_controller_and_cascade_use_the_type(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["shelly"]["main_meter_type"] = "shelly_pro_3em"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(raw, f)
        try:
            cfg = load_config(f.name)
        finally:
            Path(f.name).unlink()
        self.assertTrue(Controller(cfg).main_meter.is_pro)
        from cascade_service import CascadeService
        self.assertTrue(CascadeService(cfg).main_meter.is_pro)
        self.assertFalse(Controller(cfg).heater_meter.is_pro)                    # Heizstab-Zähler bleibt Gen1


class ClientTest(unittest.TestCase):
    def test_pro_3em_power_and_energy(self):
        calls = []
        routes = {"/rpc/EM.GetStatus": {"id": 0, "a_act_power": -300.0, "b_act_power": -250.5,
                                        "c_act_power": 120.0, "total_act_power": -430.5},
                  "/rpc/EMData.GetStatus": {"id": 0, "total_act": 15.2, "total_act_ret": 3.8}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, calls)):
            c = Shelly3EMClient("http://pro", type_="shelly_pro_3em", name="main_meter")
            self.assertEqual(c.get_total_power_w(), -430.5)                     # Einspeisung negativ, unverändert
            self.assertEqual(c.get_energy_wh(), (15.2, 3.8))
        self.assertEqual([(u, p) for u, p in calls], [("http://pro/rpc/EM.GetStatus", {"id": 0}),
                                                      ("http://pro/rpc/EMData.GetStatus", {"id": 0})])

    def test_pro_without_total_sums_the_phases_and_errors_give_none(self):
        routes = {"/rpc/EM.GetStatus": {"a_act_power": 100.0, "b_act_power": 200.0, "c_act_power": 300.5}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, [])):
            self.assertEqual(Shelly3EMClient("http://pro", type_="shelly_pro_3em").get_total_power_w(), 600.5)
        routes = {"/rpc/EM.GetStatus": shelly_client.requests.Timeout("x"),
                  "/rpc/EMData.GetStatus": {"total_act": 1.0}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, [])):
            c = Shelly3EMClient("http://pro", type_="shelly_pro_3em")
            self.assertIsNone(c.get_total_power_w())
            self.assertIsNone(c.get_energy_wh())

    def test_gen1_unchanged(self):
        calls = []
        routes = {"/status": {"total_power": 974.0, "emeters": [{"total": 1000.0, "total_returned": 10.0},
                                                                {"total": 2000.0, "total_returned": 20.0},
                                                                {"total": 3000.0, "total_returned": 30.0}]}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, calls)):
            c = Shelly3EMClient("http://g1", name="main_meter")
            self.assertEqual(c.get_total_power_w(), 974.0)
            self.assertEqual(c.get_energy_wh(), (6000.0, 60.0))
        self.assertEqual(len(calls), 1)                                         # ein /status für beides


class MeterSwapTest(unittest.TestCase):
    OLD, NEW = "shelly_3em_gen1|http://192.168.2.183", "shelly_pro_3em|http://192.168.2.200"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def midnight(self, imp, exp, meter_id=OLD):
        db.track_meter(imp, exp, meter_id)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE meter_snapshots SET taken_at = '00:00:04'")

    def today(self, imp, exp):
        return meter.compute_meter_today(Readings(main_import_wh=imp, main_export_wh=exp, solax_grid_in_kwh=9.9,
                                                  solax_grid_out_kwh=9.9), pv_kwh=20.0)

    def test_swap_to_a_new_meter_starting_at_zero_continues_the_day(self):
        self.midnight(12_229_000, 6_096_000)
        db.track_meter(12_233_000, 6_101_000, self.OLD)                          # bis zum Tausch: +4 / +5 kWh
        self.assertEqual(self.today(12_233_000, 6_101_000)["bezug_kwh"], 4.0)
        db.track_meter(0.4, 0.0, self.NEW)                                       # neuer Zähler, fast 0
        t = self.today(0.4, 0.0)
        self.assertEqual((t["source"], t["bezug_kwh"], t["einspeisung_kwh"]), ("shelly", 4.0, 5.0))
        db.track_meter(1_500.4, 2_000.0, self.NEW)
        t = self.today(1_500.4, 2_000.0)
        self.assertEqual((t["bezug_kwh"], t["einspeisung_kwh"]), (5.5, 7.0))     # alter + neuer Anteil

    def test_swap_is_detected_even_when_the_new_counter_is_larger(self):
        self.midnight(1_000, 500)
        db.track_meter(3_000, 900, self.OLD)
        db.track_meter(50_000, 40_000, self.NEW)                                 # Vorbesitz/Werkstest: kein Sprung
        db.track_meter(51_000, 40_100, self.NEW)
        t = self.today(51_000, 40_100)
        self.assertEqual((t["bezug_kwh"], t["einspeisung_kwh"]), (3.0, 0.5))

    def test_reset_without_new_address_and_small_reorder_is_no_reset(self):
        self.midnight(10_000, 1_000)
        db.track_meter(12_000, 1_000, self.OLD)
        db.track_meter(11_950, 1_000, self.OLD)                                  # 50 Wh „zurück“: Lesereihenfolge
        self.assertEqual(self.today(12_000, 1_000)["bezug_kwh"], 2.0)
        db.track_meter(5, 0, self.OLD)                                           # echter Reset am selben Gerät
        db.track_meter(1_005, 0, self.OLD)
        self.assertEqual(self.today(1_005, 0)["bezug_kwh"], 3.0)

    def test_new_ip_same_meter_stays_continuous(self):
        self.midnight(10_000, 1_000)
        db.track_meter(11_000, 1_200, self.OLD)
        db.track_meter(11_000, 1_200, "shelly_3em_gen1|http://192.168.2.99")    # nur neue IP
        db.track_meter(11_500, 1_300, "shelly_3em_gen1|http://192.168.2.99")
        t = self.today(11_500, 1_300)
        self.assertEqual((t["bezug_kwh"], t["einspeisung_kwh"]), (1.5, 0.3))

    def test_stale_old_reading_after_swap_never_gives_negative_values(self):
        self.midnight(12_229_000, 6_096_000)
        db.track_meter(12_233_000, 6_101_000, self.OLD)
        db.track_meter(10.0, 0.0, self.NEW)
        t = self.today(12_233_000, 6_101_000)                                    # Web liest noch den alten Zähler
        self.assertEqual((t["source"], t["bezug_kwh"]), ("solax", 9.9))          # kein Sprung auf >12 000 kWh
        t = self.today(0.0, 0.0)                                                 # etwas unter der neuen Basis
        self.assertGreaterEqual(t["bezug_kwh"], 0)

    def test_old_table_without_new_columns_is_migrated(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE meter_snapshots")
            conn.execute("CREATE TABLE meter_snapshots (date TEXT PRIMARY KEY, import_wh_at_midnight REAL, "
                         "export_wh_at_midnight REAL, taken_at TEXT)")
            conn.execute("INSERT INTO meter_snapshots VALUES (date('now','localtime'), 100, 50, '00:00:03')")
        db._meter_ready.clear()
        db.track_meter(400, 60, self.OLD)                                        # Tag begann vor dem Deploy
        self.assertEqual(self.today(400, 60)["bezug_kwh"], 0.3)
        db.track_meter(1, 0, self.NEW)
        db.track_meter(101, 10, self.NEW)
        self.assertEqual(self.today(101, 10)["bezug_kwh"], 0.4)


class SettingsTest(unittest.TestCase):
    def test_type_can_be_saved_and_is_validated(self):
        cur = json.loads(EXAMPLE.read_text())
        new = web._validated_update(cur, web.ConfigUpdate(shelly={"main_meter_url": "http://192.168.2.200",
                                                                  "main_meter_type": "shelly_pro_3em"}))
        self.assertEqual((new["shelly"]["main_meter_url"], new["shelly"]["main_meter_type"]),
                         ("http://192.168.2.200", "shelly_pro_3em"))
        with self.assertRaises(web.HTTPException):
            web._validated_update(cur, web.ConfigUpdate(shelly={"main_meter_type": "pro"}))

    def test_connection_test_reports_device_and_type_mismatch(self):
        routes = {"/shelly": {"gen": 2, "model": "SPEM-003CEBEU", "mac": "AABBCCDDEEFF"},
                  "/rpc/EM.GetStatus": {"total_act_power": 812.0},
                  "/rpc/EMData.GetStatus": {"total_act": 1234.0, "total_act_ret": 56.0}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, [])):
            r = web.api_main_meter_test(web.MainMeterTestRequest(url="http://pro", type="shelly_pro_3em"),
                                        web.Response())
            self.assertEqual((r["model"], r["generation"], r["power_w"], r["import_kwh"]),
                             ("SPEM-003CEBEU", 2, 812.0, 1.23))
            with self.assertRaises(web.HTTPException) as e:
                web.api_main_meter_test(web.MainMeterTestRequest(url="http://pro", type="shelly_3em_gen1"),
                                        web.Response())
            self.assertEqual(e.exception.status_code, 409)
