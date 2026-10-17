"""Verbraucher aus openHAB (nur lesen): Lesen, veraltet, Summe/Rest, Config, Speicherung, Nacht, Anzeige."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import consumers
import db
import night_need as nn
from clients.openhab_client import OpenHabError
from config import ConsumersConfig, load_config
from models import ControllerResult, Readings
from tests.test_night_need import D0, fill, run, standard_days, ts
from tests.test_solax_timeout import EXAMPLE

DEVICES = [
    {"item": "Zaehler_W", "name": "FeWo-Zähler"},
    {"item": "Klima_W", "name": "Klima", "parent": "Zaehler_W"},
    {"item": "Truhe_W", "name": "Gefriertruhe"},
    {"item": "TV_W", "name": "TV"},
    {"item": "Offen_W", "name": "SHEM-3", "in_sum": False},
]


def cfg(devices=DEVICES, **kw):
    return ConsumersConfig(enabled=True, devices=[dict(d) for d in devices], openhab_url="http://oh:8080", **kw)


class FakeClient:
    def __init__(self, states, things, links, fail=False):
        self.states, self.things, self.links, self.fail = states, things, links, fail
        self.link_calls = 0

    def get_states(self):
        if self.fail:
            raise OpenHabError("GET /items: timeout")
        return self.states

    def get_thing_statuses(self):
        return self.things

    def get_item_things(self):
        self.link_calls += 1
        return self.links


def fake(**over):
    states = {"Zaehler_W": "800.5 W", "Klima_W": "600 W", "Truhe_W": "27.1 W", "TV_W": "46 W", "Offen_W": "170 W"}
    things = {"t:z": "ONLINE", "t:k": "ONLINE", "t:g": "ONLINE", "t:tv": "OFFLINE", "t:o": "ONLINE"}
    links = {"Zaehler_W": "t:z", "Klima_W": "t:k", "Truhe_W": "t:g", "TV_W": "t:tv", "Offen_W": "t:o"}
    states.update(over.pop("states", {}))
    things.update(over.pop("things", {}))
    links.update(over.pop("links", {}))
    return FakeClient(states, things, links, **over)


class ParseTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(consumers.parse_power("27.11 W"), 27.11)
        self.assertEqual(consumers.parse_power("1.5 kW"), 1500.0)
        self.assertEqual(consumers.parse_power("40"), 40.0)
        self.assertEqual(consumers.parse_power("-82.72 W"), -82.72)
        self.assertEqual(consumers.parse_power("3.0E-4 W"), 0.0003)
        for bad in (None, "NULL", "UNDEF", "5 kWh", "ON"):
            self.assertIsNone(consumers.parse_power(bad))


class ReadTest(unittest.TestCase):
    def setUp(self):
        consumers._links.update(ts=-1e12, url="", map={})

    def test_stale_when_thing_not_online(self):
        devs = {d["item"]: d for d in consumers.read(cfg(), fake())}
        self.assertFalse(devs["Truhe_W"]["stale"])
        self.assertEqual(devs["Truhe_W"]["power_w"], 27.1)
        self.assertTrue(devs["TV_W"]["stale"])                       # Thing OFFLINE
        self.assertEqual(devs["TV_W"]["thing_status"], "OFFLINE")
        self.assertEqual(devs["TV_W"]["power_w"], 46.0)               # letzter Wert bleibt sichtbar

    def test_null_missing_thing_and_no_link(self):
        c = fake(states={"Truhe_W": "NULL"}, things={"t:k": None}, links={"TV_W": None})
        del c.things["t:k"]
        del c.links["TV_W"]
        devs = {d["item"]: d for d in consumers.read(cfg(), c)}
        self.assertTrue(devs["Truhe_W"]["stale"])
        self.assertIsNone(devs["Truhe_W"]["power_w"])
        self.assertEqual(devs["Klima_W"]["thing_status"], "MISSING")
        self.assertEqual(devs["TV_W"]["thing_status"], "NO_THING")
        self.assertTrue(devs["Klima_W"]["stale"] and devs["TV_W"]["stale"])

    def test_openhab_down_is_none(self):
        self.assertIsNone(consumers.read(cfg(), fake(fail=True)))

    def test_links_cached(self):
        c = fake()
        consumers.read(cfg(), c)
        consumers.read(cfg(), c)
        self.assertEqual(c.link_calls, 1)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        consumers._links.update(ts=-1e12, url="", map={})

    def ev(self, client, house=3000.0, loads=1000.0):
        return consumers.evaluate(consumers.read(cfg(), client), house, loads)

    def test_parent_counts_child_covered(self):
        ev = self.ev(fake())
        d = {x["item"]: x for x in ev["devices"]}
        self.assertTrue(d["Zaehler_W"]["counted"])
        self.assertFalse(d["Klima_W"]["counted"])
        self.assertTrue(d["Klima_W"]["covered"])
        self.assertEqual(d["Zaehler_W"]["own_w"], 200.5)             # 800,5 − Klima 600
        self.assertFalse(d["TV_W"]["counted"])                       # veraltet
        self.assertFalse(d["Offen_W"]["counted"])                    # in_sum aus
        self.assertEqual(ev["sum_w"], round(800.5 + 27.1))
        self.assertEqual(ev["rest_w"], round(3000 - 1000 - 827.6))

    def test_stale_parent_child_counts(self):
        ev = self.ev(fake(things={"t:z": "OFFLINE"}))
        d = {x["item"]: x for x in ev["devices"]}
        self.assertFalse(d["Zaehler_W"]["counted"])
        self.assertTrue(d["Klima_W"]["counted"])
        self.assertEqual(ev["sum_w"], round(600 + 27.1))

    def test_no_house_value(self):
        self.assertIsNone(self.ev(fake(), house=None)["rest_w"])

    def test_top_only_fresh_leaves(self):
        devs = consumers.read(cfg(), fake())
        self.assertEqual([d["name"] for d in consumers.top(devs)], ["Klima", "SHEM-3", "Gefriertruhe"])


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        for bad in ([{"item": "a b"}], [{"item": "A"}, {"item": "A"}], [{"item": "A", "parent": "X"}],
                    [{"item": "A", "parent": "B"}, {"item": "B", "parent": "A"}], [{"item": "A", "in_sum": "ja"}]):
            with self.assertRaises(ValueError, msg=bad):
                ConsumersConfig(devices=bad)
        with self.assertRaises(ValueError):
            ConsumersConfig(enabled=True, devices=[{"item": "A"}])        # ohne openhab.url
        with self.assertRaises(ValueError):
            ConsumersConfig(small_avg_w=-1)
        self.assertEqual(ConsumersConfig(devices=[{"item": "A"}]).devices[0].name, "A")

    def test_load_config_takes_openhab_block(self):
        d = Path(tempfile.mkdtemp())
        try:
            raw = json.loads(EXAMPLE.read_text())
            self.assertFalse(load_config(EXAMPLE).consumers_active)
            raw["openhab"] = {"url": "http://oh:8080", "token": "geheim", "timeout_s": 2}
            raw["consumers"] = {"enabled": True, "devices": DEVICES}
            (d / "c.json").write_text(json.dumps(raw))
            c = load_config(d / "c.json")
            self.assertTrue(c.consumers_active)
            self.assertEqual((c.consumers.openhab_url, c.consumers.openhab_timeout_s), ("http://oh:8080", 2.0))
            self.assertNotIn("geheim", repr(c.consumers))                 # Token nie im repr/Log
        finally:
            shutil.rmtree(d)


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

    def test_record_only_fresh_and_avg(self):
        consumers._links.update(ts=-1e12, url="", map={})
        devs = consumers.read(cfg(), fake(states={"Klima_W": "0.4 W"}))
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 17, 22, 0, 5)
            db.record_consumers(devs)
            db.record_consumers(devs)                                     # gleiche Minute: ersetzt
        with sqlite3.connect(db.DB_PATH) as c:
            rows = dict(c.execute("SELECT item, w FROM consumer_min"))
        self.assertNotIn("TV_W", rows)                                    # veraltet
        self.assertNotIn("Klima_W", rows)                                 # < 1 W
        self.assertEqual(rows["Truhe_W"], 27.1)
        self.assertEqual(rows[db.CONSUMER_READ_MARK], 4.0)                # 4 aktuelle Geräte
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = datetime(2026, 10, 17, 22, 30)
            avg = db.get_consumer_avg(24)
        self.assertEqual(avg["Truhe_W"], 27.1)
        out = db.prune(datetime(2026, 12, 1, 4, 0))
        self.assertEqual(out["consumer_min"], 4)            # Zähler, Truhe, SHEM-3, Marker


class NightTest(unittest.TestCase):
    def test_top_and_peak_attribution(self):
        data = standard_days()
        fill(data["meter"], 0, "22:00", "23:00", 3500.0)                  # 3,5 kW Bedarf
        fill(data["meter"], 0, "23:00", "23:30", 3000.0)                  # 3,0 kW, nichts Großes gemessen
        data["cons"] = {}
        fill(data["cons"], 0, "16:00", "34:00", None)
        for k in list(data["cons"]):
            data["cons"][k] = {db.CONSUMER_READ_MARK: 3.0, "Truhe_W": 60.0}
        for k in list(data["cons"]):
            if ts(0, "22:00") <= k < ts(0, "23:00"):
                data["cons"][k] = {db.CONSUMER_READ_MARK: 3.0, "Truhe_W": 60.0, "Ofen_W": 2000.0, "Zaehler_W": 2500.0}
        res = run(data, now=D0 + timedelta(days=2, hours=20), max_discharge_w=2400, meters=frozenset({"Zaehler_W"}))
        n = res["nights"][0]
        self.assertAlmostEqual(n["consumers_wh"]["Ofen_W"], 2000, delta=5)
        self.assertGreaterEqual(n["consumers_coverage"], 0.9)
        self.assertAlmostEqual(n["peak_by_wh"]["Ofen_W"], 1100, delta=10)          # Zähler zählt nicht als Gerät
        self.assertAlmostEqual(n["peak_by_wh"][nn.UNMEASURED], 300, delta=10)     # 30 min × 600 W
        self.assertNotIn("Zaehler_W", n["peak_by_wh"])

    def test_whirlpool_can_take_peak(self):
        data = standard_days()
        fill(data["meter"], 0, "22:00", "23:00", 3500.0)
        data["whirl"] = {}
        fill(data["whirl"], 0, "22:00", "23:00", 3000.0)
        n = run(data, now=D0 + timedelta(days=2, hours=20), max_discharge_w=2400)["nights"][0]
        self.assertEqual(n["peak_by_wh"], {})                                     # ohne Verbraucher-Lesung keine Zuordnung
        self.assertEqual(n["consumers_coverage"], 0)
        data["cons"] = {}
        fill(data["cons"], 0, "16:00", "34:00", {db.CONSUMER_READ_MARK: 1.0})
        n = run(data, now=D0 + timedelta(days=2, hours=20), max_discharge_w=2400)["nights"][0]
        self.assertAlmostEqual(n["peak_by_wh"][nn.WHIRLPOOL], 1100, delta=10)


class WebTest(unittest.TestCase):
    def setUp(self):
        import web
        self.web = web
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads(EXAMPLE.read_text())
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        raw["openhab"] = {"url": "http://oh:8080", "token": "", "timeout_s": 3}
        self.cfg_path.write_text(json.dumps(raw))
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()
        web._cons_avg.update(ts=-1e12, avg={})

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def put(self, **c):
        return self.web.api_config_put(self.web.ConfigUpdate(consumers=c))

    def test_settings_saved(self):
        self.put(enabled=True, small_avg_w=15, devices=[{"item": " Truhe_W ", "name": "Truhe"},
                                                        {"item": "K_W", "parent": "Truhe_W", "in_sum": False}])
        saved = json.loads(self.cfg_path.read_text())["consumers"]
        self.assertEqual(saved["small_avg_w"], 15.0)
        self.assertEqual(saved["devices"][0]["item"], "Truhe_W")
        self.assertEqual(saved["devices"][1], {"item": "K_W", "name": "", "parent": "Truhe_W", "in_sum": False})

    def test_settings_rejected(self):
        from fastapi import HTTPException
        for bad in ({"devices": [{"item": "a-b"}]}, {"devices": [{"item": "A", "parent": "B"}]},
                    {"devices": [{"item": "A", "cmd": "ON"}]}, {"enabled": "ja"}, {"relay": True},
                    {"devices": "A"}, {"small_avg_w": "x"}):
            with self.assertRaises(HTTPException, msg=bad):
                self.put(**bad)

    def test_status_block_small_and_rest(self):
        consumers._links.update(ts=-1e12, url="", map={})
        cfg = load_config(self.cfg_path)
        self.assertEqual(self.web._consumers_status_block(cfg, ControllerResult(
            timestamp=datetime.now(), controller_active=True, dry_run=True, temp_status=None,
            storage_max_temp=60, heat_resume_temp=50, readings=Readings())), {"enabled": False})
        raw = json.loads(self.cfg_path.read_text())
        raw["consumers"] = {"enabled": True, "devices": DEVICES}
        self.cfg_path.write_text(json.dumps(raw))
        cfg = load_config(self.cfg_path)
        r = Readings(pv_power_w=1000.0, main_meter_power_w=2000.0, heater_meter_power_w=500.0,
                     consumers=consumers.read(cfg.consumers, fake()))
        res = ControllerResult(timestamp=datetime.now(), controller_active=True, dry_run=True, temp_status=None,
                               storage_max_temp=60, heat_resume_temp=50, readings=r)
        with mock.patch("db.get_consumer_avg", return_value={}):
            b = self.web._consumers_status_block(cfg, res)
        d = {x["item"]: x for x in b["devices"]}
        self.assertTrue(d["Truhe_W"]["small"] is False)                    # ohne Verlauf: aktuell 27 W ≥ 20
        self.assertFalse(d["Zaehler_W"]["small"])
        self.assertEqual(b["rest_w"], round(3000 - 500 - 827.6))
        self.web._cons_avg.update(ts=-1e12, avg={})
        with mock.patch("db.get_consumer_avg", return_value={"Truhe_W": 12.0, "Zaehler_W": 5.0}):
            b = self.web._consumers_status_block(cfg, res)
        d = {x["item"]: x for x in b["devices"]}
        self.assertTrue(d["Truhe_W"]["small"])                             # Ø 24 h 12 W < 20
        self.assertFalse(d["Zaehler_W"]["small"])                          # Zähler mit Geräten nie „Sonstige“
        self.assertTrue(d["Klima_W"]["small"])                             # nie ≥ 1 W gespeichert → Ø 0

    def test_openhab_down_block(self):
        raw = json.loads(self.cfg_path.read_text())
        raw["consumers"] = {"enabled": True, "devices": DEVICES}
        self.cfg_path.write_text(json.dumps(raw))
        cfg = load_config(self.cfg_path)
        res = ControllerResult(timestamp=datetime.now(), controller_active=True, dry_run=True, temp_status=None,
                               storage_max_temp=60, heat_resume_temp=50, readings=Readings(consumers=None))
        self.assertEqual(self.web._consumers_status_block(cfg, res), {"enabled": True, "ok": False})

    def test_explain_and_snapshot(self):
        import decisions
        import explain
        consumers._links.update(ts=-1e12, url="", map={})
        r = Readings(consumers=consumers.read(cfg(), fake()))
        self.assertEqual(decisions._consumers_top(r), [["Klima", 600], ["SHEM-3", 170], ["Gefriertruhe", 27]])
        line = explain.energy_line({"energy": {"pv_w": 0, "grid_w": 100, "consumers_top": [["Klima", 600]]}})
        self.assertIn("Klima 600 W", line)
        self.assertIsNone(decisions._consumers_top(Readings()))


class ControllerTest(unittest.TestCase):
    def test_read_failure_never_raises(self):
        from controller import Controller
        c = Controller.__new__(Controller)
        c.cfg = mock.Mock()
        with mock.patch("consumers.read", side_effect=RuntimeError("kaputt")):
            self.assertIsNone(c._read_consumers())


if __name__ == "__main__":
    unittest.main()
