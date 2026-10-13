"""Heizstab-Zähler tot: Ersatzwert aus den Phasen-Shellys (Controller und Kaskade), nie stillschweigend 0 W."""
import unittest
from types import SimpleNamespace
from unittest import mock

import db
import web
from heater_power import SOURCE_METER, SOURCE_PHASE_SUM, PhaseRead, estimate_from_phases, resolve
from tests import test_cascade_profiles as tcp
from tests import test_controller_phases as tcph
from tests.test_controller_phases import FakeMeter

P = 1500.0


class EstimateTest(unittest.TestCase):
    def test_meter_wins(self):
        hp = resolve(812.0, [PhaseRead(True, 1500.0)] * 3, P)
        self.assertEqual((hp.watts, hp.source), (812.0, SOURCE_METER))

    def test_measured_phase_power_is_summed(self):
        hp = resolve(None, [PhaseRead(True, 1480.0), PhaseRead(True, 1510.0), PhaseRead(False, 0.0)], P)
        self.assertEqual((hp.watts, hp.source), (2990.0, SOURCE_PHASE_SUM))
        self.assertIn("PH1 1480 W", hp.detail)

    def test_all_off_is_zero_but_marked(self):
        hp = resolve(None, [PhaseRead(False, 0.0)] * 3, P)
        self.assertEqual((hp.watts, hp.source), (0.0, SOURCE_PHASE_SUM))

    def test_unknown_phase_counts_nominal_if_it_could_be_on(self):
        phases = [PhaseRead(True, 1500.0), PhaseRead(None, None), PhaseRead(False, 0.0)]
        self.assertEqual(estimate_from_phases(phases, P, {2: "on"})[0], 3000.0)
        self.assertEqual(estimate_from_phases(phases, P, {})[0], 3000.0)          # ohne Historie: evtl. an
        self.assertEqual(estimate_from_phases(phases, P, {2: "off"})[0], 1500.0)  # zuletzt sicher aus

    def test_without_measurement_on_means_nominal(self):
        self.assertEqual(estimate_from_phases([PhaseRead(True, None), PhaseRead(False, None)], P)[0], 1500.0)

    def test_disabled_phase_is_ignored(self):
        self.assertEqual(estimate_from_phases([None, PhaseRead(True, 1500.0), None], P)[0], 1500.0)

    def test_negative_measurement_clamped(self):
        self.assertEqual(estimate_from_phases([PhaseRead(False, -2.0)], P)[0], 0.0)


class ControllerDeadMeterTest(unittest.TestCase):
    setUp = tcph.ControllerPhaseTest.setUp
    tearDown = tcph.ControllerPhaseTest.tearDown
    make = tcph.ControllerPhaseTest.make

    def dead(self, ph, powers, main=-2000.0):
        c = self.make(ph=ph, main=main)
        c.heater_meter = FakeMeter(None)
        for plug, p in zip((c.ph1, c.ph2, c.ph3), powers):
            plug.power = p
        return c

    def test_dead_meter_with_two_phases_on(self):
        c = self.dead((True, True, False), (1490.0, 1505.0, 0.0))
        r = c._read_all()
        self.assertEqual(r.heater_meter_power_w, 2995.0)
        self.assertEqual(r.heater_power_source, SOURCE_PHASE_SUM)
        self.assertEqual(r.surplus_without_heater_w, -2000.0 - 2995.0)   # nicht −2000 (= 0 W angenommen)
        self.assertNotIn("heater_meter", r.errors)

    def test_dead_meter_with_unknown_phase(self):
        db.log_phase_change(2, "on")
        c = self.dead((True, None, False), (1500.0, None, 0.0))
        r = c._read_all()
        self.assertEqual(r.heater_meter_power_w, 3000.0)                  # PH2 unbekannt, zuletzt an → 1500
        db.log_phase_change(2, "off")
        r = self.dead((True, None, False), (1500.0, None, 0.0))._read_all()
        self.assertEqual(r.heater_meter_power_w, 1500.0)                  # zuletzt sicher aus → 0

    def test_live_meter_keeps_source_meter(self):
        r = self.make(ph=(True, False, False), heater=1450.0)._read_all()
        self.assertEqual((r.heater_meter_power_w, r.heater_power_source), (1450.0, SOURCE_METER))


class CascadeDeadMeterTest(unittest.TestCase):
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service
    budget = tcp.ServiceIntegrationTest.budget

    def dead_service(self, phases, main_w=-1000.0):
        self.raw["zendure"] = {"enabled": False}             # ohne Batterie: Budget = Einspeisung + Heizstab
        svc = self.service(False, 45.0, main_w)
        svc.heater_meter = SimpleNamespace(get_total_power_w=lambda: None)
        svc._heater_phase_reads = lambda: phases
        return svc

    def test_dead_meter_phases_on_budget_correct(self):
        svc = self.dead_service([PhaseRead(True, 1500.0), PhaseRead(True, 1500.0), PhaseRead(False, 0.0)])
        out = svc.run_cascade()
        self.assertEqual(out["surplus_watts"], 4000)          # 1000 W Einspeisung + 3000 W laufender Heizstab
        self.assertEqual(self.budget(), 4000)                 # vorher: 1000 W → zwei Phasen wären abgeschaltet worden

    def test_dead_meter_unknown_phase(self):
        db.log_phase_change(3, "on")
        svc = self.dead_service([PhaseRead(True, 1500.0), PhaseRead(False, 0.0), PhaseRead(None, None)])
        self.assertEqual(svc.run_cascade()["surplus_watts"], 4000)   # PH3 unbekannt, evtl. an → +1500
        db.log_phase_change(3, "off")
        svc = self.dead_service([PhaseRead(True, 1500.0), PhaseRead(False, 0.0), PhaseRead(None, None)])
        self.assertEqual(svc.run_cascade()["surplus_watts"], 2500)

    def test_db_nominal_is_no_longer_used(self):
        # is_on=1 mit power_watts=1500, aber alle Phasen aus (z. B. Max-Temp): kein Phantom-Budget
        db.update_cascade_device_state("heizstab", True)
        svc = self.dead_service([PhaseRead(False, 0.0)] * 3)
        self.assertEqual(svc.run_cascade()["surplus_watts"], 1000)


class WebHeaterSourceTest(unittest.TestCase):
    def test_banner_text_for_dead_heater_meter(self):
        cfg = SimpleNamespace(
            shelly=SimpleNamespace(ph1_url="http://a", ph2_url="http://b", ph3_url="http://c",
                                   storage_url="http://s", heater_meter_url="http://192.168.2.148",
                                   main_meter_url="http://m"),
            heater=SimpleNamespace(disabled_phases=[False, False, False]),
        )
        with mock.patch("db.get_device_alerts", return_value=[{"id": "heater_meter", "since": "18:43"}]):
            a = web._device_alerts(cfg)
        self.assertEqual(a[0]["text"],
                         "Heizstab-Zähler (192.168.2.148) seit 18:43 Uhr nicht erreichbar – Ersatzwert aus Phasen")


if __name__ == "__main__":
    unittest.main()
