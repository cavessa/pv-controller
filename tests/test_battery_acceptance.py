"""Aufnahmegrenze der Batterie: nimmt sie weniger auf als angeboten (BMS), reserviert die Kaskade
nur Ist + Puffer statt des Wunschwerts – der Rest geht an den Heizstab."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import db
import zendure_control as zc
from clients.zendure_client import ZendureStatus
from tests import test_cascade_profiles as tcp

T0 = datetime(2026, 10, 27, 14, 0)


class AcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "acc.json"
        self.t = T0

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, charge_w, limit_w=2400, mode="charge", minutes=1):
        self.t += timedelta(minutes=minutes)
        return zc.charge_acceptance(ZendureStatus(soc=84, charge_w=charge_w), mode, limit_w,
                                    state_path=self.path, now=self.t)

    def saturate(self):
        for _ in range(zc.ACCEPT_RUNS):
            r = self.step(505)
        return r

    def test_needs_three_runs_then_reserves_actual_plus_headroom(self):
        self.assertIsNone(self.step(505))
        self.assertIsNone(self.step(505))
        self.assertEqual(self.step(505), 505 + zc.ACCEPT_HEADROOM_W)

    def test_small_gap_is_normal(self):
        for _ in range(5):
            self.assertIsNone(self.step(2300))             # Lücke 100 W < 200 W

    def test_interrupted_gap_starts_over(self):
        self.step(505); self.step(505)
        self.assertIsNone(self.step(2390))
        self.assertIsNone(self.step(505))

    def test_own_cap_hiding_the_gap_keeps_the_limit(self):
        self.saturate()
        # Heizstab nimmt den Puffer, Einspeise-Deckel senkt den Sollwert auf ~Ist → Lücke scheinbar weg
        self.assertEqual(self.step(510, limit_w=560), 505 + zc.ACCEPT_HEADROOM_W)

    def test_taking_more_again_releases_the_limit(self):
        self.saturate()
        self.assertIsNone(self.step(505 + zc.ACCEPT_RISE_W, limit_w=700))

    def test_recheck_after_15_minutes_without_confirmed_gap(self):
        self.saturate()
        self.assertIsNotNone(self.step(510, limit_w=560, minutes=10))
        self.assertIsNone(self.step(510, limit_w=560, minutes=6))

    def test_not_charging_resets(self):
        self.saturate()
        self.assertIsNone(self.step(0, limit_w=0, mode="idle"))
        self.assertIsNone(self.step(505))

    def test_broken_state_file_starts_fresh(self):
        self.path.write_text("{kaputt")
        self.assertIsNone(self.step(505))


class CascadeAcceptanceTest(unittest.TestCase):
    """Ganzer Kaskadenlauf: Batterie lädt nur 505 W bei inputLimit 2400 W, 4 kW Einspeisung."""
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service
    budget = tcp.ServiceIntegrationTest.budget

    def run_once(self, charge_w=505.0):
        db.set_zendure_ctl(mode="charge", limit_w=2400)          # gilt seit dem letzten Lauf am Gerät
        svc = self.service(False, 45.0, -4000.0, soc=84, zd=ZendureStatus(soc=84, charge_w=charge_w))
        svc.run_cascade()
        return self.budget()

    def test_state_lives_next_to_the_test_db(self):
        self.run_once()
        self.assertTrue((Path(db.DB_PATH).parent / zc.ACCEPT_STATE_NAME).exists())

    def test_heater_gets_what_the_battery_does_not_take(self):
        gross = 4000 + 505
        self.assertEqual(self.run_once(), gross - 2400)          # erst volle Reservierung
        self.assertEqual(self.run_once(), gross - 2400)
        self.assertEqual(self.run_once(), gross - (505 + zc.ACCEPT_HEADROOM_W))

    def test_battery_taking_everything_keeps_full_reservation(self):
        for _ in range(4):
            b = self.run_once(charge_w=2400.0)
        self.assertEqual(b, 4000 + 2400 - 2400)


if __name__ == "__main__":
    unittest.main()
