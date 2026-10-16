"""Thermostat im Heizstab (Relais an, 0 W): Erkennung, Relais halten, Kaskade nach Messung,
„Speicher praktisch voll“; schnelle Lade-Drosselung im Zendure-Wächter. Nur Fakes."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import db
import reasons
import web
import zendure_watchdog as wd
from cascade_plan import BATTERY, HEATER_MAX, HEATER_MIN, HeaterCtx, walk
from clients.zendure_client import ZendureStatus
from heater_phases import HeaterParams, PhaseIn, decide_phases, thermostat_step
from models import PhaseAction
from tests import test_cascade_profiles as tcp
from tests.test_zendure_safety import DbCase
from tests.test_zendure_wallbox import Device, Goe, IDLE

T0 = datetime(2026, 10, 16, 15, 40)
P = HeaterParams(phase_w=1600, grid_assist_w=400, max_temp=64, resume_temp=62, min_on_s=420, min_off_s=120)
EMPTY = {"open_since": None, "open_temp": None, "full_since": None, "full_temp": None}


def step(st, minute, relay_on=True, w=0.0, temp=62.1, max_temp=64.0):
    upd, op, full, msg = thermostat_step(st, T0 + timedelta(minutes=minute), relay_on, w, temp, max_temp)
    return {**st, **upd}, op, full, msg


class ThermostatStepTest(unittest.TestCase):
    def test_second_reading_confirms(self):
        st, op, full, _ = step(EMPTY, 0)
        self.assertIsNotNone(st["open_since"])
        self.assertIsNone(op)                                   # erste Messung: nur merken
        st, op, full, msg = step(st, 1)
        self.assertEqual(op, {"open_min": 1, "temp_c": 62.1})
        self.assertIn("Thermostat offen", msg)
        st, op, _, msg = step(st, 4)
        self.assertEqual(op["open_min"], 4)
        self.assertEqual(msg, "")                               # nur einmal loggen

    def test_heater_comes_back(self):
        st, *_ = step(EMPTY, 0)
        st, *_ = step(st, 1)
        st, op, _, msg = step(st, 10, w=1560.0)                 # 13.10.: nach 10 min von selbst wieder
        self.assertIsNone(op)
        self.assertIsNone(st["open_since"])
        self.assertIn("zieht wieder (1560 W) nach 10 min", msg)

    def test_relay_off_clears(self):
        st, *_ = step(EMPTY, 0)
        st, op, _, _ = step(st, 1, relay_on=False)
        self.assertIsNone(op)
        self.assertIsNone(st["open_since"])

    def test_cold_storage_or_unknown_power_not_thermostat(self):
        st, *_ = step(EMPTY, 0, temp=45.0)
        self.assertIsNone(st["open_since"])
        st, *_ = step(EMPTY, 0, w=None)
        self.assertIsNone(st["open_since"])

    def test_practically_full_after_15_min_near_max(self):
        st, *_ = step(EMPTY, 0, temp=62.5)
        st, op, full, _ = step(st, 14, temp=62.6)
        self.assertIsNotNone(op)
        self.assertIsNone(full)
        st, op, full, msg = step(st, 15, temp=62.6)
        self.assertIsNone(op)
        self.assertEqual(full, {"open_min": 15, "temp_c": 62.6, "release_c": 60.6})
        self.assertIn("praktisch voll", msg)
        self.assertIsNone(st["open_since"])
        # bleibt, bis der Speicher 2 °C gefallen ist (Relais dann aus)
        st, op, full, _ = step(st, 40, relay_on=False, temp=61.0)
        self.assertIsNotNone(full)
        st, op, full, msg = step(st, 90, relay_on=False, temp=60.6)
        self.assertIsNone(full)
        self.assertIn("aufgehoben", msg)

    def test_long_open_but_not_near_max_stays_open(self):
        st, *_ = step(EMPTY, 0, temp=59.1)
        st, op, full, _ = step(st, 30, temp=59.2)
        self.assertIsNone(full)
        self.assertEqual(op["open_min"], 30)


class DecidePhasesTest(unittest.TestCase):
    def phases(self, *on):
        old = T0 - timedelta(hours=1)
        return [PhaseIn(f"PH{i + 1}", i + 1 in on, old) for i in range(3)]

    def test_open_holds_relays_even_without_budget(self):
        outs = decide_phases(self.phases(1), 62.1, 0.0, P, T0, thermostat_open={"open_min": 3, "temp_c": 62.1})
        self.assertTrue(all(o.action is PhaseAction.UNCHANGED for o in outs))
        self.assertEqual({o.code for o in outs}, {"heater_thermostat_open"})
        self.assertIn("bleibt EIN", outs[0].reason)

    def test_open_no_new_phase(self):
        outs = decide_phases(self.phases(1), 59.0, 6000.0, P, T0, thermostat_open={"open_min": 2, "temp_c": 59.0})
        self.assertEqual([o.action for o in outs], [PhaseAction.UNCHANGED] * 3)

    def test_max_temp_still_wins(self):
        outs = decide_phases(self.phases(1, 2), 64.1, 6000.0, P, T0, thermostat_open={"open_min": 5, "temp_c": 64.1})
        self.assertEqual(outs[0].code, "max_temp_reached")
        self.assertEqual(outs[0].action, PhaseAction.TURN_OFF)

    def test_practically_full_turns_off_within_min_on_time(self):
        ph = [PhaseIn("PH1", True, T0 - timedelta(seconds=60)), PhaseIn("PH2", False), PhaseIn("PH3", False)]
        outs = decide_phases(ph, 62.6, 6000.0, P, T0,
                             practically_full={"open_min": 15, "temp_c": 62.6, "release_c": 60.6})
        self.assertEqual(outs[0].action, PhaseAction.TURN_OFF)
        self.assertEqual(outs[0].code, "storage_practically_full")
        self.assertIn("Freigabe ab 60.6 °C", outs[0].reason)

    def test_manual_on_overrides(self):
        outs = decide_phases(self.phases(), 61.0, 0.0, P, T0, force="on",
                             practically_full={"open_min": 15, "temp_c": 62.6, "release_c": 60.6})
        self.assertEqual(outs[0].action, PhaseAction.TURN_ON)

    def test_reason_texts_exist(self):
        self.assertIn("Thermostat", reasons.REASONS["heater_thermostat_open"])
        self.assertIn("praktisch voll", reasons.REASONS["storage_practically_full"])


class WalkTest(unittest.TestCase):
    order = [HEATER_MIN, BATTERY, HEATER_MAX]

    def run_walk(self, **ctx):
        got = {}
        res = walk(self.order, 3000, HeaterCtx(phase_w=1500, grid_assist_w=0, temp=45, comfort_min=52,
                                               max_temp=64, **ctx),
                   lambda a: got.setdefault("battery_avail", a) and min(a, 2400), lambda k, r: r)
        return res, got["battery_avail"]

    def test_min_stage_claims_phases_normally(self):
        _, avail = self.run_walk()
        self.assertEqual(avail, 0)                              # 2 Phasen à 1500 W

    def test_open_thermostat_claims_only_measured(self):
        _, avail = self.run_walk(thermostat_open=True, actual_w=0.0)
        self.assertEqual(avail, 3000)                           # alles an die Batterie

    def test_practically_full_stages_inactive(self):
        res, avail = self.run_walk(practically_full=True)
        self.assertEqual(avail, 3000)
        self.assertFalse(res.heater_active)


class CascadeThermostatTest(tcp.ServiceIntegrationTest):
    def test_summer_open_thermostat_surplus_goes_to_battery(self):
        db.set_heater_thermostat(open_since=(datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds"),
                                 open_temp=55.0)
        svc = self.service(True, 45.0, -3000.0, heater_w=0.0)
        svc.run_cascade()
        self.assertEqual(svc._last_plan.want_w, 2400)           # sonst: Min-Stufe nimmt 2 Phasen, Batterie 0

    def test_heater_drawing_again_ignores_stale_state(self):
        db.set_heater_thermostat(open_since=(datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds"))
        svc = self.service(True, 45.0, -3000.0, heater_w=1600.0)
        svc.run_cascade()
        self.assertNotEqual(svc._last_plan.want_w, 2400)


class WebThermostatTest(unittest.TestCase):
    def test_status_from_decisions(self):
        d = SimpleNamespace(code="heater_thermostat_open", data={"open_min": 3, "temp_c": 62.1})
        self.assertEqual(web._heater_thermostat(SimpleNamespace(decisions=[d])),
                         {"state": "open", "open_min": 3, "temp_c": 62.1})
        f = SimpleNamespace(code="storage_practically_full", data={"open_min": 15, "temp_c": 62.6, "release_c": 60.6})
        self.assertEqual(web._heater_thermostat(SimpleNamespace(decisions=[d, f]))["state"], "full")
        self.assertEqual(web._heater_thermostat(SimpleNamespace(decisions=[])), {"state": None})


# ---- Schnelle Lade-Drosselung im Wächter -------------------------------------------------

class Meter:
    def __init__(self, *seq):
        self.seq = list(seq)
        self.calls = 0

    def get_total_power_w(self):
        self.calls += 1
        v = self.seq[min(self.calls - 1, len(self.seq) - 1)]
        if isinstance(v, Exception):
            raise v
        return v


OLD = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")


class ThrottleTest(DbCase):
    def setUp(self):
        super().setUp()
        self.t = [0.0]

    def clock(self):
        return self.t[0]

    def tick(self, dev, meter, state, dt=3.0, dry=False):
        r = wd.throttle_tick(self.cfg(dry), dev, meter, state, self.clock)
        self.t[0] += dt
        return r

    def charging(self, limit=2000):
        db.set_zendure_ctl(mode="charge", limit_w=limit, written_at=OLD)
        return Device(ac=1, inp=limit)

    def test_two_ticks_import_cuts_by_import(self):
        dev, m, st = self.charging(), Meter(800.0, 900.0), {}
        self.assertIsNone(self.tick(dev, m, st))
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO") as logs:
            self.assertEqual(self.tick(dev, m, st), "throttle")
        self.assertEqual(dev.writes, [(1, 1200, 0)])            # 2000 − min(800, 900)
        self.assertEqual(db.get_zendure_ctl()["limit_w"], 1200)
        self.assertIn("von 2000 auf 1200 W gesenkt", logs.output[0])

    def test_single_spike_ignored(self):
        dev, m, st = self.charging(), Meter(3000.0, -200.0, 3000.0, -100.0), {}
        for _ in range(4):
            self.assertIsNone(self.tick(dev, m, st))
        self.assertEqual(dev.writes, [])

    def test_big_import_down_to_zero(self):
        dev, m, st = self.charging(600), Meter(2500.0, 2600.0), {}
        self.tick(dev, m, st)
        self.tick(dev, m, st)
        self.assertEqual(dev.writes, [(1, 0, 0)])

    def test_meter_unreadable_does_nothing(self):
        dev, st = self.charging(), {}
        for m in (Meter(None, None), Meter(OSError("x"), OSError("x"))):
            self.tick(dev, m, st)
            self.tick(dev, m, st)
        self.assertEqual(dev.writes, [])
        self.assertIsNone(wd.throttle_tick(self.cfg(), dev, None, st, self.clock))

    def test_not_charging_meter_not_read(self):
        db.set_zendure_ctl(mode="discharge", limit_w=500)
        m = Meter(2000.0)
        self.tick(Device(ac=2, out=500), m, {})
        self.assertEqual(m.calls, 0)

    def test_export_never_raises(self):
        dev, m, st = self.charging(500), Meter(-3000.0, -3000.0, -3000.0), {}
        for _ in range(3):
            self.tick(dev, m, st)
        self.assertEqual(dev.writes, [])

    def test_settle_then_cut_again(self):
        dev, m, st = self.charging(2000), Meter(500.0), {}
        self.tick(dev, m, st)
        self.tick(dev, m, st)                                    # → 1500
        self.tick(dev, m, st)                                    # Pause (6 s)
        self.tick(dev, m, st)
        self.tick(dev, m, st)                                    # 1. Messung nach der Pause
        self.tick(dev, m, st)                                    # → 1000
        self.assertEqual(dev.writes, [(1, 1500, 0), (1, 1000, 0)])

    def test_dry_run_only_logs(self):
        dev, m, st = self.charging(), Meter(800.0), {}
        self.tick(dev, m, st, dry=True)
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO"):
            self.assertEqual(self.tick(dev, m, st, dry=True), "throttle")
        self.assertEqual(dev.writes, [])

    def test_minute_run_wrote_meanwhile(self):
        dev, st = self.charging(2000), {}

        class M(Meter):
            def get_total_power_w(s):
                v = super().get_total_power_w()
                if s.calls == 2:
                    db.set_zendure_ctl(limit_w=800)               # Minutenlauf hat inzwischen gesenkt
                return v
        m = M(500.0)
        self.tick(dev, m, st)
        self.assertIsNone(self.tick(dev, m, st))
        self.assertEqual(dev.writes, [])

    def test_loop_integration_no_foreign_alarm_after_own_cut(self):
        db.set_zendure_ctl(mode="charge", limit_w=2000, written_at=OLD)
        dev = Device(ac=1, inp=2000)
        t = [0.0]
        cfg = self.cfg()
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO"):
            out = wd.guard_loop(cfg, dev, Goe(IDLE), end_at=12.0, tick_s=3.0, meter=Meter(700.0, 700.0, -50.0),
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertIn("throttle", out)
        self.assertEqual(dev.writes, [(1, 1300, 0)])
        self.assertIsNone(db.get_zendure_ctl()["foreign_at"])

    def test_no_wallbox_configured_guard_ok(self):
        db.set_zendure_ctl(mode="discharge", limit_w=500, written_at=OLD)
        dev = Device(ac=2, out=500)
        self.assertEqual(wd.guard_tick(self.cfg(), dev, None), "ok")
        self.assertEqual(dev.writes, [])


if __name__ == "__main__":
    unittest.main()
