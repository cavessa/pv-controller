"""Wallbox-Pause im PV-Modus: Batterie (Profil davor) hat Vorrang."""
import unittest
from datetime import datetime, timedelta

import wallbox_pause as wp

T0 = datetime(2026, 10, 22, 14, 29, 0)
MIN_W = 6 * 230


def run(state, minutes, budget):
    """Jede Minute ein Kaskadenlauf mit gleichem Budget; liefert (Zustand, letzte Codes)."""
    codes = []
    for m in minutes:
        state, code, _ = wp.step(state, T0 + timedelta(minutes=m), budget, MIN_W)
        codes.append(code)
    return state, codes


class PauseTest(unittest.TestCase):
    def test_enough_keeps_charging(self):
        st, codes = run({}, range(10), 2000)
        self.assertFalse(st["paused"])
        self.assertEqual(set(codes), {"wallbox_charging_budget"})

    def test_pause_after_five_minutes_below(self):
        # 14:29-Fall: Rest ~0 W < 1380 W → erst nach 5 min pausieren
        st, codes = run({}, range(6), 0)
        self.assertEqual(codes[:5], ["wallbox_pause_pending"] * 5)
        self.assertEqual(codes[5], "wallbox_paused_priority")
        self.assertTrue(st["paused"])

    def test_short_dip_resets(self):
        st, _ = run({}, range(4), 0)
        st, codes = run(st, [4], 2000)              # Wolke vorbei → Zähler zurück
        st, codes = run(st, range(5, 9), 0)
        self.assertFalse(st["paused"])
        self.assertEqual(codes[-1], "wallbox_pause_pending")

    def test_resume_after_five_minutes_and_min_run(self):
        st, _ = run({}, range(6), 0)
        self.assertTrue(st["paused"])
        st, codes = run(st, range(6, 11), 2000)
        self.assertEqual(codes[:5], ["wallbox_resume_pending"] * 5)
        self.assertTrue(st["paused"])
        st, codes = run(st, [11], 2000)
        self.assertFalse(st["paused"])
        self.assertEqual(codes, ["wallbox_charging_budget"])
        # nach dem Start mindestens 10 min laden, auch wenn das Budget sofort wieder fehlt
        st, codes = run(st, range(12, 21), 0)
        self.assertFalse(st["paused"])
        st, codes = run(st, [21, 22], 0)
        self.assertTrue(st["paused"])

    def test_paused_stays_while_below(self):
        st, _ = run({}, range(6), 0)
        st, codes = run(st, range(6, 30), 900)
        self.assertTrue(st["paused"])
        self.assertEqual(set(codes), {"wallbox_paused_priority"})


class CarIdleTest(unittest.TestCase):
    def test_car_not_charging_never_pauses(self):
        # car=3/4 (fertig/vom Fahrzeug pausiert): kein Zähler, keine Pause – auch nach 30 min
        st = {}
        for m in range(30):
            st, code, _ = wp.step(st, T0 + timedelta(minutes=m), 0, MIN_W, charging=False)
            self.assertEqual(code, "wallbox_car_idle")
        self.assertFalse(st["paused"])
        self.assertIsNone(st["below_since"])

    def test_idle_resets_counter(self):
        st, _ = run({}, range(4), 0)                       # 4 min lädt das Auto zu viel
        st, code, _ = wp.step(st, T0 + timedelta(minutes=4), 0, MIN_W, charging=False)
        st, codes = run(st, range(5, 9), 0)                # lädt wieder: Zähler von vorn
        self.assertFalse(st["paused"])
        self.assertEqual(codes[-1], "wallbox_pause_pending")

    def test_own_pause_stays_while_car_idle(self):
        # nach eigener Pause meldet der go-e car=4 – Wiederanlauf-Logik läuft trotzdem
        st, _ = run({}, range(6), 0)
        st, code, _ = wp.step(st, T0 + timedelta(minutes=6), 0, MIN_W, charging=False)
        self.assertTrue(st["paused"])
        self.assertEqual(code, "wallbox_paused_priority")


class ReasonTextTest(unittest.TestCase):
    def test_codes_known_and_headline(self):
        import reasons
        from headline import _stage
        for code in ("wallbox_paused_priority", "wallbox_pause_pending", "wallbox_resume_pending",
                     "wallbox_charging_budget"):
            self.assertIn(code, reasons.REASONS)
        devs = {"wallbox": {"id": "wallbox", "type": "wallbox", "name": "Wallbox", "action": "keep_off",
                            "code": "wallbox_paused_priority", "data": {"rest_w": 900, "min_w": 1380}}}
        st = _stage("wallbox", devs, {}, None, None, 64.0, True)
        self.assertEqual(st[2], "Wallbox pausiert – Batterie hat Vorrang (Rest 0,9 kW < 1,4 kW).")
        devs["wallbox"].update(code="wallbox_car_idle")
        self.assertEqual(_stage("wallbox", devs, {}, None, None, 64.0, True)[2], "Auto fertig/pausiert (Fahrzeug).")


class ControllerTest(unittest.TestCase):
    """frc-Entscheidung aus dem Pausen-Zustand (DB gemockt)."""

    def decide(self, pause, frc=0, car=2, eco=True, by_pv=False):
        from unittest import mock
        import controller
        from types import SimpleNamespace
        from models import WallboxStatus
        c = controller.Controller.__new__(controller.Controller)
        c.cfg = SimpleNamespace(wallbox=SimpleNamespace(
            enabled=True, only_control_when_pv_surplus_active=True, auto_reset_on_unplug=False,
            eco_amps=16, eco_phases=1))
        st = WallboxStatus(force_state=frc, car_state=car, pv_surplus_active=eco)
        with mock.patch.object(controller, "get_wallbox_pause", return_value=pause), \
             mock.patch.object(controller, "get_wallbox_frc_by_pv", return_value=by_pv):
            return c._decide_wallbox(st, None)[1]

    def fresh(self, paused):
        return {"paused": paused, "rest_w": 900, "min_w": 1380, "at": datetime.now().isoformat(timespec="seconds")}

    def test_paused_sets_frc1(self):
        d = self.decide(self.fresh(True))
        self.assertEqual((d.action.value, d.target_force_state), ("pause", 1))
        self.assertIn("Rest 0,9 kW < 1,4 kW", d.reason)

    def test_resume_releases(self):
        d = self.decide(self.fresh(False), frc=1, by_pv=True)
        self.assertEqual((d.action.value, d.target_force_state), ("release", 0))

    def test_stale_state_releases(self):
        old = {**self.fresh(True), "at": (datetime.now() - timedelta(minutes=10)).isoformat()}
        d = self.decide(old, frc=1, by_pv=True)
        self.assertEqual(d.target_force_state, 0)

    def test_unplugged_never_paused(self):
        d = self.decide(self.fresh(True), frc=1, car=1, by_pv=True)
        self.assertEqual((d.action.value, d.target_force_state), ("release", 0))

    def test_unplugged_foreign_pause_untouched(self):
        d = self.decide({}, frc=1, car=1, by_pv=False)
        self.assertEqual((d.action.value, d.target_force_state), ("unchanged", 1))

    def test_foreign_pause_untouched_while_plugged(self):
        d = self.decide(self.fresh(False), frc=1, car=4, by_pv=False)
        self.assertEqual(d.action.value, "unchanged")
        self.assertIn("App/Wallbox", d.reason)

    def test_car_idle_no_switching(self):
        d = self.decide(self.fresh(False), frc=0, car=4)
        self.assertEqual(d.action.value, "unchanged")

    def test_manual_mode_releases_our_pause_only(self):
        d = self.decide(self.fresh(True), frc=1, eco=False, by_pv=True)
        self.assertEqual((d.action.value, d.target_force_state), ("release", 0))
        d = self.decide(self.fresh(True), frc=1, eco=False, by_pv=False)
        self.assertEqual(d.action.value, "skipped")


if __name__ == "__main__":
    unittest.main()


from tests.test_decisions import TempDb  # noqa: E402


class CascadeWallboxTest(TempDb):
    """14:29-Fall durch die ganze Kaskade: Batterie 48 %, ~0,9 kW Rest, Auto lädt 1,46 kW mit 6 A."""

    def run_once(self, cfg, main_w, wb_w, car=2):
        from types import SimpleNamespace
        from clients.zendure_client import ZendureStatus
        from cascade_service import CascadeService
        import db
        svc = CascadeService(cfg)
        svc.heater_meter = SimpleNamespace(get_total_power_w=lambda: 0.0)
        svc.main_meter = SimpleNamespace(get_total_power_w=lambda: main_w)
        svc.storage = SimpleNamespace(get_temperature_c=lambda: 54.0)
        svc.solax = SimpleNamespace(get_pv_power_w=lambda: 2100.0, get_pv_ac_w=lambda: 2100.0)
        svc.solax2 = None
        amps = []
        raw = {"lmo": 4, "car": car, "amp": 6, "frc": 0, "nrg": [0] * 11 + [wb_w]}
        svc.goe = SimpleNamespace(get_status=lambda: raw, set_amp=lambda a: amps.append(a))
        svc.goe_battery = svc.goe
        if svc.zendure_ctl is not None:
            svc.zendure_client = SimpleNamespace(get_status=lambda: ZendureStatus(soc=48))
            svc.zendure_ctl.client = svc.zendure_client
        out = svc.run_cascade()
        return {d["id"]: d for d in out["devices"]}, db.get_wallbox_pause(), amps

    def test_pause_after_five_minutes(self):
        import db
        cfg = self.config(runtime={"dry_run": True},
                          wallbox={"enabled": True, "url": "http://x", "eco_phases": 1, "eco_amps": 16},
                          zendure={"enabled": True, "ip": "x", "control_enabled": True})
        # Netzbezug 564 W bei 1460 W Wallbox → brutto ~900 W, alles für die Batterie
        dev, st, _ = self.run_once(cfg, 564.0, 1460.0)
        self.assertEqual(dev["wallbox"]["code"], "wallbox_pause_pending")
        self.assertFalse(st["paused"])
        # 5 min später (Zustand zurückdatieren, Kaskade lief jede Minute)
        old = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        db.set_wallbox_pause({**st, "below_since": old})
        dev, st, _ = self.run_once(cfg, 564.0, 1460.0)
        self.assertEqual(dev["wallbox"]["code"], "wallbox_paused_priority")
        self.assertTrue(st["paused"])
        self.assertIn("Batterie hat Vorrang", dev["wallbox"]["reason"])

    def test_car_idle_no_pause(self):
        cfg = self.config(runtime={"dry_run": True},
                          wallbox={"enabled": True, "url": "http://x", "eco_phases": 1, "eco_amps": 16},
                          zendure={"enabled": True, "ip": "x", "control_enabled": True})
        dev, st, _ = self.run_once(cfg, -900.0, 0.0, car=4)     # 14:45-Fall: car=4, 0 W
        self.assertEqual(dev["wallbox"]["code"], "wallbox_car_idle")
        self.assertIsNone(st["below_since"])
        self.assertFalse(st["paused"])
