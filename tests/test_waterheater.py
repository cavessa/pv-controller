"""Durchlauferhitzer-Zähler (Shelly Pro 3EM, nur lesen): Config, Summe der Phasen, Anzeige-Block."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clients import shelly_client
from clients.shelly_client import Shelly3EMClient
from config import WaterheaterMeterConfig, load_config
from controller import Controller
from models import Readings
from tests.test_main_meter import fake_get
from tests.test_solax_timeout import EXAMPLE


class ConfigTest(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            WaterheaterMeterConfig(enabled=True, url="")
        with self.assertRaises(ValueError):
            WaterheaterMeterConfig(type="foo")
        self.assertEqual(WaterheaterMeterConfig().type, "shelly_pro_3em")

    def test_load_config(self):
        d = Path(tempfile.mkdtemp())
        try:
            raw = json.loads(EXAMPLE.read_text())
            self.assertFalse(load_config(EXAMPLE).waterheater_active)        # Beispiel: aus
            raw["waterheater_meter"] = {"enabled": True, "url": "http://192.0.2.76"}
            (d / "c.json").write_text(json.dumps(raw))
            cfg = load_config(d / "c.json")
            self.assertTrue(cfg.waterheater_active)
            self.assertEqual(cfg.waterheater_meter.type, "shelly_pro_3em")
        finally:
            shutil.rmtree(d)


class ReadTest(unittest.TestCase):
    def test_pro_phases_summed(self):
        routes = {"/rpc/EM.GetStatus": {"a_act_power": 3000, "b_act_power": 3010.5, "c_act_power": 2990}}
        with mock.patch.object(shelly_client.requests, "get", fake_get(routes, [])):
            ph = Shelly3EMClient("http://w", type_="shelly_pro_3em").get_phase_powers_w()
        r = Readings()
        Controller._apply_waterheater(r, ph)
        self.assertEqual((r.waterheater_power_w, r.waterheater_reachable), (9000.5, True))

    def test_negative_clamped_and_offline(self):
        r = Readings()
        Controller._apply_waterheater(r, [-2.0, 1.0, -1.0])
        self.assertEqual(r.waterheater_power_w, 0.0)
        r = Readings()
        Controller._apply_waterheater(r, None)
        self.assertIsNone(r.waterheater_power_w)
        self.assertFalse(r.waterheater_reachable)
        self.assertEqual(r.errors, [])                                      # kein Fail-safe


if __name__ == "__main__":
    unittest.main()
