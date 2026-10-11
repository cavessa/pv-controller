"""Tests für die reine Planungslogik der Zendure-Regelung (python -m unittest)."""
import unittest

from clients.zendure_client import ZendureStatus
from config import ZendureConfig
from zendure_control import plan


def mk(**kw):
    base = dict(avail_w=0, import_w=0, wallbox_charging=False, override=None,
                ctl_mode="idle", ctl_limit_w=0, seconds_since_change=1e9)
    base.update(kw)
    return base


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.zc = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100,
                                max_charge_w=2400, max_discharge_w=2400)
        self.st = ZendureStatus(soc=50, home_out_w=0)

    def test_charge_from_surplus(self):
        p = plan(self.zc, self.st, **mk(avail_w=1234))
        self.assertEqual((p.mode, p.limit_w, p.write), ("charge", 1230, True))

    def test_charge_needs_hysteresis_to_start_but_not_to_continue(self):
        p = plan(self.zc, self.st, **mk(avail_w=150))
        self.assertEqual(p.mode, "idle")
        p = plan(self.zc, self.st, **mk(avail_w=150, ctl_mode="charge", ctl_limit_w=400))
        self.assertEqual(p.mode, "charge")

    def test_charge_limited_to_max_and_no_write_for_small_change(self):
        p = plan(self.zc, self.st, **mk(avail_w=5000))
        self.assertEqual(p.limit_w, 2400)
        p = plan(self.zc, self.st, **mk(avail_w=1030, ctl_mode="charge", ctl_limit_w=1000))
        self.assertFalse(p.write)
        self.assertEqual(p.limit_w, 1000)

    def test_no_charge_at_target_soc(self):
        self.st.soc = 100
        p = plan(self.zc, self.st, **mk(avail_w=2000))
        self.assertEqual(p.mode, "idle")

    def test_discharge_on_import(self):
        p = plan(self.zc, self.st, **mk(import_w=420))
        self.assertEqual((p.mode, p.limit_w, p.write), ("discharge", 420, True))

    def test_discharge_regulates_to_zero_import(self):
        self.st.home_out_w = 400
        p = plan(self.zc, self.st, **mk(import_w=200, ctl_mode="discharge", ctl_limit_w=400))
        self.assertEqual(p.limit_w, 600)
        p = plan(self.zc, self.st, **mk(import_w=-300, ctl_mode="discharge", ctl_limit_w=400))
        self.assertEqual(p.limit_w, 100)          # Einspeisung -> Abgabe reduzieren

    def test_no_discharge_below_min_soc(self):
        self.st.soc = 10
        p = plan(self.zc, self.st, **mk(import_w=500))
        self.assertEqual(p.mode, "idle")

    def test_wallbox_blocks_discharge_hard(self):
        p = plan(self.zc, self.st, **mk(import_w=3000, wallbox_charging=True,
                                         ctl_mode="discharge", ctl_limit_w=800, seconds_since_change=5))
        self.assertEqual((p.mode, p.limit_w, p.write, p.action), ("idle", 0, True, "blocked"))
        self.assertIn("Wallbox lädt", p.reason)

    def test_wallbox_does_not_block_charging(self):
        p = plan(self.zc, self.st, **mk(avail_w=1500, wallbox_charging=True))
        self.assertEqual(p.mode, "charge")

    def test_wallbox_block_can_be_disabled(self):
        self.zc.block_discharge_while_wallbox_charging = False
        p = plan(self.zc, self.st, **mk(import_w=500, wallbox_charging=True))
        self.assertEqual(p.mode, "discharge")

    def test_pause_after_mode_change(self):
        p = plan(self.zc, self.st, **mk(import_w=500, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=30))
        self.assertEqual((p.mode, p.limit_w, p.write), ("idle", 0, True))   # Laden endet sofort
        p = plan(self.zc, self.st, **mk(import_w=500, ctl_mode="idle", seconds_since_change=30))
        self.assertEqual((p.mode, p.write), ("idle", False))
        p = plan(self.zc, self.st, **mk(import_w=500, ctl_mode="idle", seconds_since_change=130))
        self.assertEqual(p.mode, "discharge")

    def test_override_force_charge_and_block_discharge(self):
        p = plan(self.zc, self.st, **mk(override="on", seconds_since_change=5, ctl_mode="discharge", ctl_limit_w=300))
        self.assertEqual((p.mode, p.limit_w), ("charge", 2400))
        p = plan(self.zc, self.st, **mk(override="off", import_w=900))
        self.assertEqual((p.mode, p.action), ("idle", "blocked"))


if __name__ == "__main__":
    unittest.main()
