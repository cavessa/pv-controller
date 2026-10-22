"""Batterie-Vorrang: Heizstab-Phasen ohne Mindestlaufzeit abschalten, Wallbox-Ladestrom begrenzen."""
import unittest
from datetime import datetime, timedelta

from cascade_plan import wallbox_amp_cap
from heater_phases import HeaterParams, PhaseIn, decide_phases
from models import PhaseAction as A

NOW = datetime(2026, 10, 22, 14, 10, 0)


def run(shed_now):
    p = HeaterParams(phase_w=1620, grid_assist_w=400)
    recent = NOW - timedelta(minutes=2)          # Mindestlaufzeit (7 min) noch nicht erreicht
    phases = [PhaseIn(f"PH{i + 1}", True, recent) for i in range(3)]
    return decide_phases(phases, 50.9, 3700, p, NOW, budget_down_w=3700, shed_now=shed_now)


class HeaterShedTest(unittest.TestCase):
    def test_min_on_holds_without_flag(self):
        self.assertEqual([o.action for o in run(False)], [A.UNCHANGED] * 3)

    def test_battery_priority_sheds_surplus_phase(self):
        out = run(True)
        self.assertEqual([o.action for o in out], [A.UNCHANGED, A.UNCHANGED, A.TURN_OFF])
        self.assertTrue(out[2].data.get("battery_priority"))


class WallboxAmpTest(unittest.TestCase):
    def test_caps(self):
        self.assertEqual(wallbox_amp_cap(10000, 1, 16), 16)       # genug: Maximum
        self.assertEqual(wallbox_amp_cap(2500, 1, 16), 10)        # 2500 W / 230 V
        self.assertEqual(wallbox_amp_cap(-900, 1, 16), 6)         # nichts übrig: Minimum 6 A
        self.assertEqual(wallbox_amp_cap(5000, 3, 16), 7)         # dreiphasig: 5000 / 690


if __name__ == "__main__":
    unittest.main()
