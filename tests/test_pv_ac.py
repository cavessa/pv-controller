"""PV-Leistung einheitlich als AC-Ausgang; Dashboard-Livewerte aus EINEM Datenstand (/api/status „live“)."""
import unittest
from pathlib import Path
from types import SimpleNamespace

import web
from clients.solax_client import SolaxClient
from models import Readings
from tests.test_pv_now import raw_x1, raw_x3
from tests.test_solax_timeout import cfg_with


def parse(raw, dev_type):
    c = SolaxClient("http://x", "pw")
    c.device_type = dev_type
    return c._parse(raw)


class PvAcTest(unittest.TestCase):
    def test_x3_uses_the_ac_register_strings_stay_dc(self):
        d = parse(raw_x3(620, 112, ac=715), 14)
        self.assertEqual((d.pv_ac_w, d.pv_total_w, d.pv1_power_w, d.pv2_power_w), (715.0, 732.0, 620.0, 112.0))

    def test_x1_pergola_ac_below_dc(self):
        d = parse(raw_x1(63, 0, ac=53), 4)
        self.assertEqual((d.pv_ac_w, d.pv_total_w), (53.0, 63.0))

    def test_negative_ac_at_night_counts_as_zero(self):
        raw = raw_x3(0, 0, ac=65536 - 12)                 # signed16: −12 W Eigenverbrauch
        self.assertEqual(parse(raw, 14).pv_ac_w, 0.0)

    def test_without_ac_register_falls_back_to_dc(self):
        d = parse(raw_x3(500, 100), 14)
        d.ac_power_w = None
        self.assertEqual(d.pv_ac_w, 600.0)


class LiveBlockTest(unittest.TestCase):
    def test_one_snapshot_for_pv_grid_and_house(self):
        main = parse(raw_x3(520, 112, ac=632), 14)
        pg = parse(raw_x1(55, 0, ac=47), 4)
        r = Readings(pv_power_w=679.0, pv1_power_w=632.0, pv2_power_w=47.0, solax_main=main, solax_pergola=pg,
                     main_meter_power_w=13710.0,
                     zendure=SimpleNamespace(charge_w=0.0, discharge_w=0.0))
        cfg = cfg_with()
        live = web._live_block(r, cfg)
        self.assertEqual(live["pv_w"], live["pv_main_w"] + live["pergola_w"])   # Σ = PV-Anlage + Pergola (AC)
        self.assertEqual((live["pv_main_dc_w"], live["pergola_dc_w"]), (632.0, 55.0))
        self.assertEqual([s["w"] for s in live["strings"]], [520.0, 112.0])
        self.assertEqual((live["grid_w"], live["house_w"]), (13710.0, 679.0 + 13710.0))

    def test_inverter_missing_gives_nulls(self):
        live = web._live_block(Readings(main_meter_power_w=300.0), cfg_with())
        self.assertIsNone(live["pv_w"])
        self.assertIsNone(live["strings"])
        self.assertIsNone(live["house_w"])
        self.assertFalse(live["pergola_ok"])


class DashboardMarkupTest(unittest.TestCase):
    def test_cards_use_the_status_snapshot(self):
        root = Path(web.PROJECT_ROOT) / "web"
        html, js = ((root / f).read_text(encoding="utf-8") for f in ("index.html", "app.js"))
        self.assertIn("withLive(solax !== null ? solax : lastSolaxData, data)", js)
        self.assertNotIn("data.heater.pv_power = effectiveSolax.pv_total_w", js)   # kein Mischen zweier Lesungen
        self.assertIn("live?.house_w", js)                                            # PV & Netz
        self.assertIn("Erzeugung heute", html)
        self.assertIn('<span class="pt-label">Stand</span>', html)


if __name__ == "__main__":
    unittest.main()
