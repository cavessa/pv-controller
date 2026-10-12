"""Tests der Heizstab-Phasenentscheidung (python -m unittest)."""
import unittest
from datetime import datetime, timedelta

from heater_phases import HeaterParams, PhaseIn, decide_phases, target_phases
from models import PhaseAction as A

NOW = datetime(2026, 10, 9, 12, 0, 0)
OLD = NOW - timedelta(hours=1)


def mk(states, last=None):
    last = last or [OLD] * len(states)
    return [PhaseIn(f"PH{i+1}", s, last[i]) for i, s in enumerate(states)]


def acts(out):
    return [o.action for o in out]


class TargetTest(unittest.TestCase):
    def setUp(self):
        self.p = HeaterParams(phase_w=1500, grid_assist_w=400)

    def test_thresholds_with_grid_assist(self):
        # Start Phase n ab n*1500 − 400
        self.assertEqual(target_phases(1099, 0, self.p), 0)
        self.assertEqual(target_phases(1100, 0, self.p), 1)
        self.assertEqual(target_phases(2600, 1, self.p), 2)
        self.assertEqual(target_phases(4100, 2, self.p), 3)
        self.assertEqual(target_phases(99999, 0, self.p), 3)

    def test_down_hysteresis(self):
        # 2 Phasen laufen; Schwelle 2600, Halte-Schwelle 2600 − 225 = 2375
        self.assertEqual(target_phases(2400, 2, self.p), 2)
        self.assertEqual(target_phases(2370, 2, self.p), 1)


class AsymmetricTest(unittest.TestCase):
    def setUp(self):
        self.p = HeaterParams(phase_w=1500, grid_assist_w=400)

    def test_start_uses_long_mean_stop_uses_short_mean(self):
        # langes Mittel 1200 (reicht für Phase 1), kurzes Mittel 300 (Einbruch): läuft die Phase → aus
        self.assertEqual(target_phases(1200, 0, self.p, budget_down_w=300), 1)      # Start: langes Mittel
        self.assertEqual(target_phases(1200, 1, self.p, budget_down_w=300), 0)      # Halten: kurzes Mittel
        # kurzer Peak (kurzes Mittel hoch, langes niedrig) startet NICHT
        self.assertEqual(target_phases(500, 0, self.p, budget_down_w=3000), 0)

    def test_default_down_equals_up(self):
        self.assertEqual(target_phases(2400, 2, self.p), target_phases(2400, 2, self.p, 2400))


class DisabledPhaseTest(unittest.TestCase):
    def setUp(self):
        self.p = HeaterParams(phase_w=1500, grid_assist_w=0)

    def phases(self, states, disabled):
        return [PhaseIn(f"PH{i+1}", s, OLD, disabled=d) for i, (s, d) in enumerate(zip(states, disabled))]

    def test_disabled_phase_is_never_switched_or_counted(self):
        ph = self.phases([None, False, False], [True, False, False])
        out = decide_phases(ph, 50.0, 1500, self.p, NOW)
        self.assertEqual([o.action for o in out], [A.UNCHANGED, A.TURN_ON, A.UNCHANGED])
        self.assertEqual(out[0].reason, "deaktiviert")

    def test_budget_cap_is_the_number_of_active_phases(self):
        ph = self.phases([None, True, True], [True, False, False])      # nur 2 aktive, beide laufen
        out = decide_phases(ph, 50.0, 99999, self.p, NOW)
        self.assertEqual([o.action for o in out], [A.UNCHANGED] * 3)

    def test_disabled_does_not_count_as_running(self):
        # PH1 deaktiviert, Budget für 1 Phase: PH2 startet (PH1 würde sonst als „läuft" zählen)
        ph = self.phases([None, False, False], [True, False, False])
        self.assertEqual(decide_phases(ph, 50.0, 1500, self.p, NOW)[1].action, A.TURN_ON)

    def test_max_temp_ignores_disabled(self):
        ph = self.phases([None, True, True], [True, False, False])
        out = decide_phases(ph, 65.0, 99999, self.p, NOW)
        self.assertEqual([o.action for o in out], [A.UNCHANGED, A.TURN_OFF, A.TURN_OFF])
        self.assertEqual(out[0].reason, "deaktiviert")


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.p = HeaterParams(phase_w=1500, grid_assist_w=400, max_temp=64, resume_temp=62)

    def run_(self, states, temp=50.0, budget=0.0, last=None, **kw):
        return decide_phases(mk(states, last), temp, budget, self.p, NOW, **kw)

    def test_start_one_phase_per_run(self):
        out = self.run_([False, False, False], budget=4500)
        self.assertEqual(acts(out), [A.TURN_ON, A.UNCHANGED, A.UNCHANGED])

    def test_next_run_adds_next_phase(self):
        out = self.run_([True, False, False], budget=4500)
        self.assertEqual(acts(out), [A.UNCHANGED, A.TURN_ON, A.UNCHANGED])

    def test_min_on_blocks_switch_off(self):
        out = self.run_([True, False, False], budget=0, last=[NOW - timedelta(seconds=60), OLD, OLD])
        self.assertEqual(acts(out)[0], A.UNCHANGED)
        self.assertIn("Mindestlaufzeit", out[0].reason)

    def test_switch_off_after_min_on(self):
        out = self.run_([True, True, False], budget=0)
        self.assertEqual(acts(out), [A.TURN_OFF, A.TURN_OFF, A.UNCHANGED])

    def test_min_off_blocks_restart(self):
        out = self.run_([False, False, False], budget=3000, last=[NOW - timedelta(seconds=30), OLD, OLD])
        self.assertEqual(out[0].action, A.UNCHANGED)
        self.assertIn("Mindestpause", out[0].reason)

    def test_hysteresis_keeps_phase_on_at_small_dip(self):
        out = self.run_([True, True, False], budget=2400)
        self.assertEqual(acts(out), [A.UNCHANGED] * 3)

    def test_unknown_phase_counts_as_running(self):
        # PH1 unbekannt, Budget für 2 Phasen: nur eine weitere (PH2) darf an
        out = self.run_([None, False, False], budget=2600)
        self.assertEqual(acts(out), [A.UNCHANGED, A.TURN_ON, A.UNCHANGED])
        out = self.run_([None, True, False], budget=2600)
        self.assertEqual(acts(out), [A.UNCHANGED] * 3)      # Ziel 2 = PH1(?) + PH2
        out = self.run_([None, False, False], budget=1100)
        self.assertEqual(acts(out), [A.UNCHANGED] * 3)      # Ziel 1 ist durch PH1(?) belegt

    def test_other_phases_keep_working_when_one_unknown(self):
        out = self.run_([None, True, True], budget=0)
        self.assertEqual(acts(out), [A.UNCHANGED, A.TURN_OFF, A.TURN_OFF])

    def test_max_temp_turns_off_reachable_phases_even_if_one_unknown(self):
        out = self.run_([None, True, True], temp=64.2, budget=9999)
        self.assertEqual(acts(out), [A.UNCHANGED, A.TURN_OFF, A.TURN_OFF])
        self.assertIn("storage_max_temp", out[1].reason)

    def test_max_temp_ignores_min_on_and_fail_safe(self):
        out = self.run_([True, True, True], temp=65.0, budget=None, surplus_fail_safe=True,
                        last=[NOW - timedelta(seconds=5)] * 3)
        self.assertEqual(acts(out), [A.TURN_OFF] * 3)

    def test_unknown_temp_turns_everything_off(self):
        out = self.run_([True, False, True], temp=None, budget=9999)
        self.assertEqual(acts(out), [A.TURN_OFF, A.UNCHANGED, A.TURN_OFF])
        self.assertIn("Speichertemperatur unbekannt", out[0].reason)

    def test_surplus_fail_safe_changes_nothing(self):
        out = self.run_([True, False, False], budget=0, surplus_fail_safe=True)
        self.assertEqual(acts(out), [A.UNCHANGED] * 3)

    def test_hysteresis_band_blocks_new_cycle_but_allows_additional_phase(self):
        out = self.run_([False, False, False], temp=63.0, budget=4500)
        self.assertEqual(acts(out), [A.UNCHANGED] * 3)
        out = self.run_([True, False, False], temp=63.0, budget=4500)
        self.assertEqual(acts(out), [A.UNCHANGED, A.TURN_ON, A.UNCHANGED])

    def test_force_off_ignores_min_on(self):
        out = self.run_([True, True, False], budget=9999, force="off",
                        last=[NOW - timedelta(seconds=5)] * 3)
        self.assertEqual(acts(out), [A.TURN_OFF, A.TURN_OFF, A.UNCHANGED])

    def test_summer_stop_and_heat(self):
        self.assertEqual(acts(self.run_([True, True, True], summer="stop", summer_reason="x",
                                        last=[NOW - timedelta(seconds=5)] * 3)), [A.TURN_OFF] * 3)
        self.assertEqual(acts(self.run_([False, True, False], summer="heat", summer_reason="x")),
                         [A.TURN_ON, A.UNCHANGED, A.TURN_ON])

    def test_noisy_budget_around_threshold_does_not_flicker(self):
        """Budget pendelt um die Schwelle (1100 W ± 150): höchstens wenige Wechsel."""
        state, last, changes = [False, False, False], [None] * 3, 0
        import random
        random.seed(1)
        t = NOW
        for _ in range(60):                       # 60 Minuten
            budget = 1100 + random.uniform(-150, 150)
            ph = [PhaseIn(f"PH{i+1}", state[i], last[i]) for i in range(3)]
            out = decide_phases(ph, 50.0, budget, self.p, t)
            for i, o in enumerate(out):
                if o.action is A.TURN_ON:
                    state[i], last[i], changes = True, t, changes + 1
                elif o.action is A.TURN_OFF:
                    state[i], last[i], changes = False, t, changes + 1
            t += timedelta(minutes=1)
        self.assertLess(changes, 6)


if __name__ == "__main__":
    unittest.main()
