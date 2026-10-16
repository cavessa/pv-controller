"""Thermostat im Heizstab (Relais an, 0 W): Erkennung, Relais halten, Kaskade nach Messung,
„Speicher praktisch voll“; schnelle Lade-Drosselung im Zendure-Wächter. Nur Fakes."""
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

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


class RingCase(DbCase):
    def setUp(self):
        super().setUp()
        import meter_ring
        self._old_ring = meter_ring.RING_PATH
        meter_ring.RING_PATH = str(Path(self.tmp.name) / "ring.txt")

    def tearDown(self):
        import meter_ring
        meter_ring.RING_PATH = self._old_ring
        super().tearDown()


class ThrottleTest(RingCase):
    def setUp(self):
        super().setUp()
        self.t = [0.0]

    def clock(self):
        return self.t[0]

    def tick(self, dev, meter, state, dt=3.0, dry=False):
        try:
            grid = meter.get_total_power_w()
        except Exception:
            grid = None
        r = wd.throttle_tick(self.cfg(dry), dev, grid, state, self.clock)
        self.t[0] += dt
        return r

    def charging(self, limit=2000):
        db.set_zendure_ctl(mode="charge", limit_w=limit, written_at=OLD)
        return Device(ac=1, inp=limit)

    def discharging(self, limit=1530):
        db.set_zendure_ctl(mode="discharge", limit_w=limit, written_at=OLD)
        return Device(ac=2, out=limit)

    # ---- Laden ----
    def test_two_ticks_import_cuts_by_import(self):
        dev, m, st = self.charging(), Meter(800.0, 900.0), {}
        self.assertIsNone(self.tick(dev, m, st))
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO") as logs:
            self.assertEqual(self.tick(dev, m, st), "throttle")
        self.assertEqual(dev.writes, [(1, 1200, 0)])            # 2000 − min(800, 900)
        self.assertEqual(db.get_zendure_ctl()["limit_w"], 1200)
        self.assertIn("Netzbezug 800 W beim Laden – sofort von 2000 auf 1200 W gesenkt", logs.output[0])

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

    def test_charging_meter_unreadable_does_nothing(self):
        dev, st = self.charging(), {}
        for m in (Meter(None, None, None), Meter(OSError("x"), OSError("x"), OSError("x"))):
            for _ in range(3):
                self.tick(dev, m, st)
        self.assertEqual(dev.writes, [])

    def test_export_never_raises_charge(self):
        dev, m, st = self.charging(500), Meter(-3000.0), {}
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
        import contextlib
        import zendure_control as zcm
        dev, m, st = self.charging(2000), Meter(500.0), {}

        @contextlib.contextmanager
        def guard(*a, **kw):
            db.set_zendure_ctl(limit_w=800)                       # Minutenlauf schrieb, während wir aufs Lock warteten
            yield True
        self.tick(dev, m, st)
        with mock.patch.object(zcm, "write_guard", guard):
            self.assertIsNone(self.tick(dev, m, st))
        self.assertEqual(dev.writes, [])

    def test_idle_no_action(self):
        db.set_zendure_ctl(mode="idle", limit_w=0)
        dev, st = Device(), {}
        for v in (2000.0, None, -2000.0, None):
            self.assertIsNone(wd.throttle_tick(self.cfg(), dev, v, st, self.clock))
        self.assertEqual(dev.writes, [])

    # ---- Entladen ----
    def test_discharge_export_cuts_by_export(self):
        dev, m, st = self.discharging(1530), Meter(-1941.0, -1400.0), {}   # 15.10.: Batterie speist ein
        self.tick(dev, m, st)
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO") as logs:
            self.assertEqual(self.tick(dev, m, st), "throttle")
        self.assertEqual(dev.writes, [(2, 0, 130)])             # 1530 − 1400
        self.assertIn("Einspeisung 1400 W beim Entladen", logs.output[0])

    def test_discharge_export_larger_than_limit_standby(self):
        dev, m, st = self.discharging(1230), Meter(-3190.0, -1895.0), {}
        self.tick(dev, m, st)
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            self.assertEqual(self.tick(dev, m, st), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")
        self.assertIn("Einspeisung 1895 W beim Entladen – Entladen gestoppt", logs.output[0])

    def test_discharge_import_never_raises(self):
        dev, m, st = self.discharging(500), Meter(2000.0), {}
        for _ in range(3):
            self.tick(dev, m, st)
        self.assertEqual(dev.writes, [])

    def test_discharge_meter_unreadable_two_ticks_standby(self):
        dev, m, st = self.discharging(800), Meter(None, None), {}
        self.assertIsNone(self.tick(dev, m, st))
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            self.assertEqual(self.tick(dev, m, st), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertIn("Hauptzähler nicht lesbar – Entladen gestoppt", logs.output[0])

    def test_discharge_single_unreadable_ignored(self):
        dev, m, st = self.discharging(800), Meter(None, 20.0, None, 15.0), {}
        for _ in range(4):
            self.tick(dev, m, st)
        self.assertEqual(dev.writes, [])

    def test_mode_change_resets_counter(self):
        dev, st = self.charging(1000), {}
        wd.throttle_tick(self.cfg(), dev, 500.0, st, self.clock)          # Laden, Bezug 500
        db.set_zendure_ctl(mode="discharge", limit_w=1000)
        self.assertIsNone(wd.throttle_tick(self.cfg(), dev, -500.0, st, self.clock))   # neuer Modus: erst 1. Takt
        self.assertEqual(dev.writes, [])

    # ---- Schleife / Ringpuffer ----
    def test_loop_integration_no_foreign_alarm_after_own_cut(self):
        db.set_zendure_ctl(mode="charge", limit_w=2000, written_at=OLD)
        dev = Device(ac=1, inp=2000)
        t = [0.0]
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO"):
            out = wd.guard_loop(self.cfg(), dev, Goe(IDLE), end_at=12.0, tick_s=3.0, meter=Meter(700.0, 700.0, -50.0),
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertIn("throttle", out)
        self.assertEqual(dev.writes, [(1, 1300, 0)])
        self.assertIsNone(db.get_zendure_ctl()["foreign_at"])

    def test_loop_fills_ring_every_tick_also_idle(self):
        import meter_ring
        db.set_zendure_ctl(mode="idle", limit_w=0)
        t = [0.0]
        wd.guard_loop(self.cfg(), Device(), Goe(IDLE), end_at=36.0, tick_s=3.0, meter=Meter(*[100.0 * i for i in range(12)]),
                      sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertEqual(meter_ring.mean(), (550.0, 12))

    def test_no_wallbox_configured_guard_ok(self):
        db.set_zendure_ctl(mode="discharge", limit_w=500, written_at=OLD)
        dev = Device(ac=2, out=500)
        self.assertEqual(wd.guard_tick(self.cfg(), dev, None), "ok")
        self.assertEqual(dev.writes, [])


class RingTest(RingCase):
    def test_mean_window_and_minimum(self):
        import meter_ring
        now = 1000.0
        for i in range(9):
            meter_ring.append(100.0, 0.0, ts=now - 3 * i)
        self.assertIsNone(meter_ring.mean(now=now))             # < 10 Werte → Einzelmessung
        meter_ring.append(1100.0, 0.0, ts=now)
        self.assertEqual(meter_ring.mean(now=now), (200.0, 10))
        meter_ring.append(5000.0, 0.0, ts=now - 90)             # älter als 60 s zählt nicht
        self.assertEqual(meter_ring.mean(now=now)[1], 10)

    def test_stale_buffer_falls_back(self):
        import meter_ring
        now = 10_000.0
        for i in range(40):                                     # Wächter hängt seit 61 s: 40 alte Werte
            meter_ring.append(500.0, 0.0, ts=now - 61 - 3 * i)
        self.assertIsNone(meter_ring.mean(now=now))
        for i in range(9):                                      # nur 9 frische
            meter_ring.append(500.0, 0.0, ts=now - 3 * i)
        self.assertIsNone(meter_ring.mean(now=now))
        meter_ring.append(500.0, 0.0, ts=now - 30)
        self.assertEqual(meter_ring.mean(now=now), (500.0, 10))

    def test_mean_is_grid_without_battery(self):
        import meter_ring
        now = 1000.0
        for i in range(10):                                     # Batterie entlädt 1500 W, Zähler −200 W
            meter_ring.append(-200.0, 1500.0, ts=now - 3 * i)
        self.assertEqual(meter_ring.mean(now=now), (1300.0, 10))   # Haus ohne Batterie: 1300 W Bezug

    def test_old_two_field_lines_ignored(self):
        import meter_ring
        Path(meter_ring.path()).write_text("".join(f"{1000.0 - i} 300\n" for i in range(20)))
        self.assertIsNone(meter_ring.mean(now=1000.0))

    def test_prune_keeps_recent(self):
        import meter_ring
        for i in range(800):
            meter_ring.append(float(i), 0.0, ts=float(i * 3))
        lines = Path(meter_ring.path()).read_text().splitlines()
        self.assertLess(len(lines), 600)                        # zwischendurch gekürzt
        self.assertEqual(lines[-1].split()[1], "799")

    def test_missing_file(self):
        import meter_ring
        self.assertIsNone(meter_ring.mean())


class CascadeMeanTest(tcp.ServiceIntegrationTest):
    """Minutenlauf: Batterie auf das 60-s-Mittel, Heizstab/Kaskade auf die Einzelmessung."""

    def setUp(self):
        super().setUp()
        import meter_ring
        self._old_ring = meter_ring.RING_PATH
        meter_ring.RING_PATH = str(Path(self.tmp.name) / "ring.txt")

    def tearDown(self):
        import meter_ring
        meter_ring.RING_PATH = self._old_ring
        super().tearDown()

    def fill(self, *vals, battery=0.0):
        """Werte neuester zuerst; ``battery``: Batterie-Ist (+ Entladen) – Zahl oder Liste je Wert."""
        import meter_ring, time
        now = time.time()
        for i, v in enumerate(vals):
            b = battery[i] if isinstance(battery, list) else battery
            meter_ring.append(v, b, ts=now - 3 * i)

    def test_pulse_at_measurement_no_discharge(self):
        # Herd-Puls genau beim Messen (+1700 W), Mittel −400 W (Einspeisung): kein Entladen
        self.fill(*([-1000.0] * 14 + [1700.0] * 6))
        svc = self.service(False, 45.0, 1700.0, pv=2500.0, zd=ZendureStatus(soc=98, home_out_w=0))
        out = svc.run_cascade()
        self.assertNotEqual(svc._last_plan.mode, "discharge")
        self.assertEqual(out["battery_feed_in_avg_n"], 20)

    def test_export_dip_at_measurement_no_charge_flip(self):
        # Entladen läuft, beim Messen kurz −2000 W, im Mittel Bezug 600 W → weiter entladen
        db.set_zendure_ctl(mode="discharge", limit_w=1200)
        self.fill(*([-2000.0] * 3 + [600.0] * 17), battery=1200.0)
        svc = self.service(False, 45.0, -2000.0, pv=500.0, zd=ZendureStatus(soc=98, discharge_w=1200.0, home_out_w=1200.0))
        svc.run_cascade()
        self.assertNotEqual(svc._last_plan.mode, "charge")

    def test_own_throttle_mid_minute_does_not_change_decision(self):
        # Netz ohne Batterie die ganze Minute 1800 W Bezug. A: Batterie entlud durchgehend 300 W.
        # B: Batterie entlud erst 1500 W, der Wächter drosselte nach 30 s auf 300 W (Zähler sprang dabei).
        # Jetzt entlädt sie in beiden Fällen 300 W → gleiche Entscheidung.
        results = []
        for meters, bats in (([1500.0] * 20, [300.0] * 20),
                             ([1500.0] * 10 + [300.0] * 10, [300.0] * 10 + [1500.0] * 10)):
            import meter_ring
            Path(meter_ring.path()).unlink(missing_ok=True)
            db.set_zendure_ctl(mode="discharge", limit_w=300)
            self.fill(*meters, battery=bats)
            svc = self.service(False, 45.0, 1500.0, pv=500.0,
                               zd=ZendureStatus(soc=60, discharge_w=300.0, home_out_w=300.0))
            out = svc.run_cascade()
            results.append((svc._last_plan.mode, svc._last_plan.limit_w, out["battery_feed_in_avg_w"]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][2], -1500)                   # −(1800 − 300)

    def test_too_few_values_single_measurement(self):
        self.fill(*([-1000.0] * 5))
        svc = self.service(False, 45.0, 1700.0, pv=2500.0, zd=ZendureStatus(soc=98))
        out = svc.run_cascade()
        self.assertIsNone(out["battery_feed_in_avg_n"])

    def test_heater_budget_uses_single_measurement(self):
        self.fill(*([3000.0] * 20))                             # Mittel: Bezug – darf den Heizstab nicht stören
        svc = self.service(False, 45.0, -3000.0, soc=100, zd=ZendureStatus(soc=100))
        svc.run_cascade()
        self.assertEqual(db.get_cascade_budget("heizstab")["raw_w"], 3000)


if __name__ == "__main__":
    unittest.main()
