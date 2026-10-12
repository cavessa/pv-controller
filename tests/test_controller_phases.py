"""Integrationstest des Controller-Laufs mit Fake-Geräten (eigene SQLite-Datei, kein Netzwerk).

python -m unittest tests.test_controller_phases
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import db
from config import load_config
from controller import Controller, ControllerMode
from models import PhaseAction as A

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


class FakePlug:
    def __init__(self, state, delay=0.0):
        self.state, self.delay, self.sets = state, delay, []

    def get_relay_state(self):
        time.sleep(self.delay)
        return self.state

    def set_relay(self, on):
        self.sets.append(on)
        self.state = on
        return True


class FakeMeter:
    def __init__(self, power):
        self.power = power

    def get_total_power_w(self):
        return self.power

    def get_energy_wh(self):
        return None


class FakeTemp:
    def __init__(self, t):
        self.t = t

    def get_temperature_c(self):
        return self.t


class FakeSolax:
    def __init__(self, pv):
        self.pv = pv

    def get_pv_power_w(self):
        return self.pv

    def get_realtime_data(self):
        return None


class ControllerPhaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        db.init_cascade_tables()
        raw = json.load(open(EXAMPLE))
        raw["wallbox"]["enabled"] = False
        raw["runtime"]["dry_run"] = False
        raw["runtime"]["enabled"] = True
        raw["heater"].update({"storage_max_temp": 64, "storage_temp_hysteresis": 2, "grid_assist_w": 400})
        p = Path(self.tmp.name) / "c.json"
        with open(p, "w") as f:
            json.dump(raw, f)
        self.cfg = load_config(p)

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def make(self, ph=(False, False, False), temp=50.0, main=-5000.0, heater=0.0, pv=6000.0, delays=(0, 0, 0)):
        c = Controller(self.cfg)
        c.solax, c.solax2, c.zendure, c.goe = FakeSolax(pv), None, None, None
        c.ph1, c.ph2, c.ph3 = (FakePlug(s, d) for s, d in zip(ph, delays))
        c.storage, c.main_meter, c.heater_meter = FakeTemp(temp), FakeMeter(main), FakeMeter(heater)
        return c

    def sets(self, c):
        return [c.ph1.sets, c.ph2.sets, c.ph3.sets]

    def test_unreachable_phase_does_not_block_the_others(self):
        c = self.make(ph=(None, False, False))
        res = c.run()
        self.assertEqual(self.sets(c), [[], [True], []])            # PH2 darf trotz PH1-Ausfall starten
        self.assertEqual(res.readings.unreachable_phases, ["ph1"])
        self.assertFalse(res.fail_safe_active)

    def test_max_temp_turns_off_reachable_phases_even_if_one_is_dead(self):
        c = self.make(ph=(None, True, True), temp=65.0, main=0.0, heater=3000.0)
        c.run()
        self.assertEqual(self.sets(c), [[], [False], [False]])

    def test_max_temp_applies_even_with_main_meter_down(self):
        c = self.make(ph=(True, True, False), temp=64.5, main=None)
        res = c.run()
        self.assertTrue(res.fail_safe_active)
        self.assertEqual(self.sets(c), [[False], [False], []])

    def test_unknown_storage_temp_turns_heater_off(self):
        c = self.make(ph=(True, False, True), temp=None)
        res = c.run()
        self.assertEqual(self.sets(c), [[False], [], [False]])
        self.assertIn("Speichertemperatur unbekannt", res.decisions[0].reason)

    def test_main_meter_down_blocks_switching_on(self):
        c = self.make(ph=(False, False, False), main=None)
        res = c.run()
        self.assertTrue(res.fail_safe_active)
        self.assertEqual(self.sets(c), [[], [], []])

    def test_read_only_mode_never_switches(self):
        c = self.make(ph=(False, False, False))
        c.run(mode=ControllerMode.READ_ONLY)
        self.assertEqual(self.sets(c), [[], [], []])

    def test_dead_device_does_not_slow_down_the_run(self):
        # Alle drei Phasen antworten erst nach 1,0 s: parallel ≈ 1 s, sequenziell ≈ 3 s
        c = self.make(delays=(1.0, 1.0, 1.0))
        t0 = time.monotonic()
        c.run(mode=ControllerMode.READ_ONLY)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_min_on_blocks_switch_off_but_not_max_temp(self):
        db.log_phase_change(1, "on")                                  # gerade eben eingeschaltet
        c = self.make(ph=(True, False, False), main=2000.0, heater=1500.0)   # kein Überschuss mehr
        c.run()
        self.assertEqual(self.sets(c), [[], [], []])                  # Mindestlaufzeit
        c = self.make(ph=(True, False, False), temp=64.5)
        c.run()
        self.assertEqual(self.sets(c), [[False], [], []])             # Max-Temp übersteuert

    def test_disabled_phase_is_not_read_switched_nor_reported(self):
        self.cfg.heater.phase1_disabled = True
        c = self.make(ph=(None, False, False))
        reads = []
        orig = c.ph1.get_relay_state
        c.ph1.get_relay_state = lambda: (reads.append(1), orig())[1]
        res = c.run()
        self.assertEqual(reads, [])                                   # nicht gelesen
        self.assertEqual(self.sets(c), [[], [True], []])              # PH2 startet, PH1 bleibt unberührt
        self.assertEqual(res.readings.disabled_phases, ["ph1"])
        self.assertEqual(res.readings.unreachable_phases, [])
        self.assertNotIn("ph1_state", res.readings.errors)
        self.assertEqual(db.get_device_alerts(0), [])                 # kein Banner

    def test_disabled_phase_limits_the_budget_to_two_phases(self):
        self.cfg.heater.phase1_disabled = True
        db.set_cascade_budget("heizstab", 9999, 9999)
        c = self.make(ph=(None, True, True), main=-9000.0)
        c.run()
        self.assertEqual(self.sets(c), [[], [], []])

    def test_cascade_budget_is_used_and_fallback_otherwise(self):
        db.set_cascade_budget("heizstab", 3000, 3000)
        c = self.make(main=0.0)                       # Eigenberechnung hätte Budget 0 → nichts
        c.run()
        self.assertEqual(self.sets(c), [[True], [], []])
        c = self.make(main=-4500.0)
        db.set_cascade_budget("heizstab", 0, 0, "off")  # manuell AUS (Kaskade)
        c.ph1.state = True
        c.run()
        self.assertEqual(self.sets(c), [[False], [], []])

    def test_cascade_writes_budget_and_books_actual_power(self):
        from cascade_plan import HEATER_MAX, HeaterCtx, walk
        from cascade_service import CascadeService
        svc = CascadeService(self.cfg)
        dev = db.get_cascade_device("heizstab")
        ctx = HeaterCtx(temp=50.0, actual_w=1500.0)
        res = walk([HEATER_MAX], 4000, ctx, lambda a: 0.0, lambda k, r: r)
        self.assertEqual(res.remaining_w, 4000 - 1500)  # gemessene, nicht nominale Leistung
        svc._store_heater_budget(dev, res.heater_budget_w, 0, res.remaining_w, 4000, "winter", 50.0, ctx, 1500.0, None)
        info = db.get_cascade_budget("heizstab")
        self.assertEqual(info["raw_w"], 4000)
        self.assertEqual(info["budget_w"], 4000)      # erste Minute: Mittel = Rohwert
        self.assertEqual(info["budget_down_w"], 4000)
        res0 = walk([HEATER_MAX], 0, HeaterCtx(temp=50.0), lambda a: 0.0, lambda k, r: r)
        svc._store_heater_budget(dev, res0.heater_budget_w, 0, res0.remaining_w, 0, "winter", 50.0, HeaterCtx(temp=50.0), 0.0, "off")
        self.assertEqual(db.get_cascade_budget("heizstab")["force"], "off")

if __name__ == "__main__":
    unittest.main()
