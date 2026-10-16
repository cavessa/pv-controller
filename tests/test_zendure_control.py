"""Tests für die reine Planungslogik der Zendure-Regelung (python -m unittest)."""
import unittest

from clients.zendure_client import ZendureStatus
from config import ZendureConfig
from zendure_control import device_drift, plan


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

    def test_direct_switch_without_pause(self):
        # Laden → Entladen und Entladen → Laden direkt im nächsten Lauf (keine Standby-Pause)
        self.st.home_out_w = 0
        p = plan(self.zc, self.st, **mk(import_w=500, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=30))
        self.assertEqual((p.mode, p.limit_w, p.write), ("discharge", 500, True))
        p = plan(self.zc, self.st, **mk(avail_w=3000, ctl_mode="discharge", ctl_limit_w=600, seconds_since_change=30))
        self.assertEqual((p.mode, p.limit_w, p.write), ("charge", 2400, True))
        p = plan(self.zc, self.st, **mk(import_w=500, ctl_mode="idle", seconds_since_change=5))
        self.assertEqual(p.mode, "discharge")

    def test_direct_switch_respects_wallbox_lock(self):
        # Laden → (Wolke, Auto lädt, 2 kW Bezug): kein Entladen, Standby
        p = plan(self.zc, self.st, **mk(import_w=2000, wallbox_charging=True, ctl_mode="charge", ctl_limit_w=1000,
                                         seconds_since_change=30))
        self.assertEqual((p.mode, p.limit_w, p.write, p.code), ("idle", 0, True, "wallbox_lock"))

    def test_direct_switch_never_feeds_heater_or_car(self):
        # Laden → 3 kW Bezug, davon Grundlast nur 300 W (Rest Heizstab/Wallbox): höchstens 300 W
        p = plan(self.zc, self.st, **mk(import_w=3000, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=30,
                                         base_load_w=300))
        self.assertEqual((p.mode, p.limit_w), ("discharge", 300))
        # Bezug gehört ganz zu Heizstab/Wallbox → gar kein Entladen
        p = plan(self.zc, self.st, **mk(import_w=3000, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=30,
                                         base_load_w=20))
        self.assertEqual((p.mode, p.code), ("idle", "no_discharge_controlled_load"))

    def test_small_surplus_no_flapping(self):
        # Entladen läuft, 150 W Einspeisung: unter Ladeschwelle (100 + 100 Hysterese) → kein Laden
        self.st.home_out_w = 400
        p = plan(self.zc, self.st, **mk(avail_w=150, import_w=-150, ctl_mode="discharge", ctl_limit_w=400))
        self.assertEqual((p.mode, p.limit_w), ("discharge", 250))

    def test_override_force_charge_and_block_discharge(self):
        p = plan(self.zc, self.st, **mk(override="on", seconds_since_change=5, ctl_mode="discharge", ctl_limit_w=300))
        self.assertEqual((p.mode, p.limit_w), ("charge", 2400))
        p = plan(self.zc, self.st, **mk(override="off", import_w=900))
        self.assertEqual((p.mode, p.action), ("idle", "blocked"))


class DriftTest(unittest.TestCase):
    """Gerät meldet andere Sollwerte als der Regelzustand (App/HEMS, Neustart) → erneut schreiben."""

    def setUp(self):
        self.zc = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100,
                                max_charge_w=2400, max_discharge_w=2400)

    def st(self, **props):
        return ZendureStatus(soc=64, home_out_w=props.get("outputLimit", 0), props=props)

    def test_idle_but_device_discharges_fixed_200(self):
        st = self.st(acMode=2, inputLimit=0, outputLimit=200)      # Zustand nach Inbetriebnahme (App)
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            p = plan(self.zc, st, **mk(import_w=-1500))
        self.assertEqual((p.mode, p.limit_w, p.write), ("idle", 0, True))
        self.assertIn("outputLimit 200 statt 0", p.data["drift"])
        self.assertIn("erneut", logs.output[0])

    def test_matching_device_no_write(self):
        st = self.st(acMode=1, inputLimit=1000, outputLimit=0)
        p = plan(self.zc, st, **mk(avail_w=1030, ctl_mode="charge", ctl_limit_w=1000))
        self.assertFalse(p.write)
        st = self.st(acMode=2, inputLimit=0, outputLimit=0)          # idle: acMode egal
        self.assertFalse(plan(self.zc, st, **mk()).write)

    def test_charge_overwritten_by_app(self):
        st = self.st(acMode=2, inputLimit=0, outputLimit=200)
        with self.assertLogs("zendure_control", level="WARNING"):
            p = plan(self.zc, st, **mk(avail_w=1030, ctl_mode="charge", ctl_limit_w=1000))
        self.assertEqual((p.mode, p.limit_w, p.write), ("charge", 1000, True))

    def test_small_difference_and_missing_props_ignored(self):
        self.assertEqual(device_drift(self.st(acMode=2, inputLimit=0, outputLimit=430), "discharge", 400), "")
        self.assertEqual(device_drift(ZendureStatus(soc=50), "charge", 800), "")
        self.assertEqual(device_drift(self.st(acMode=1, inputLimit=0, outputLimit=400), "discharge", 400),
                         "acMode 1 statt 2")


if __name__ == "__main__":
    unittest.main()
