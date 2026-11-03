"""Akku-Kalibrierung: Laden aus dem Netz bis 100 %, Haltezeit, Entladen bis 5 %, Zyklen, Abbruch, Wächter,
Kaskade. Kein Netz: Gerät, go-e und DB sind Fakes bzw. Temp-Dateien."""
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import calibration as cal
import db
import headline
import reasons
import zendure_control as zcm
import zendure_watchdog as wd
from clients.zendure_client import ZendureStatus
from config import ZendureConfig
from tests import test_cascade_profiles as tcp
from tests.test_zendure_control import mk
from tests.test_zendure_safety import DbCase
from tests.test_zendure_wallbox import IDLE, Goe, car
from zendure_control import ZendureControl, plan

T0 = datetime(2026, 11, 3, 10, 0)
PARAMS = {"cycles": 2, "charge_w": 1200, "hold_min": 120, "max_grid_w": 10000, "start": "now", "start_time": ""}
ZC = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100, max_charge_w=2400, max_discharge_w=2400,
                   capacity_wh=5760)


def packs(max_mv=3400, min_mv=3380, temp=25.0, n=3):
    return [{"idx": i, "max_mv": max_mv, "min_mv": min_mv, "temp_c": temp} for i in range(1, n + 1)]


def inp(**kw):
    base = dict(soc=50.0, charge_w=1200.0, grid_in_w=1200.0, packs=packs(), avail_w=0.0, house_import_w=500.0,
                meter_import_w=1700.0, capacity_wh=5760)
    base.update(kw)
    return cal.Inputs(**base)


class StepCase(unittest.TestCase):
    def setUp(self):
        self.t = T0
        self.s = cal.begin(dict(PARAMS), T0, 10.0)

    def step(self, minutes=1, **kw):
        self.t += timedelta(minutes=minutes)
        self.s, d, ev = cal.step(self.s, inp(**kw), self.t)
        return d, ev

    def to_hold(self):
        self.step()
        for _ in range(cal.FULL_RUNS):
            d, ev = self.step(soc=100.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "hold")
        return d, ev

    def to_discharge(self):
        self.to_hold()
        d, ev = self.step(minutes=PARAMS["hold_min"], soc=100.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "discharge")
        return d, ev


class ChargePhaseTest(StepCase):
    def test_charges_from_grid_with_pv_on_top(self):
        d, ev = self.step(avail_w=300.0)
        self.assertEqual((d.mode, d.limit_w, d.code), ("charge", 1500, "calib_charge_grid"))
        self.assertEqual(d.data["grid_w"], 1200)
        self.assertIn(("min_soc", cal.CALIB_MIN_SOC), ev)              # Gerät auf 5 %
        self.assertEqual(self.s["phase"], "charge")

    def test_device_max_caps(self):
        d, _ = self.step(avail_w=2000.0)
        self.assertEqual(d.limit_w, 2400)

    def test_total_grid_limit_reduces_then_pauses(self):
        d, _ = self.step(house_import_w=9500.0)
        self.assertEqual((d.mode, d.limit_w), ("charge", 500))
        d, _ = self.step(house_import_w=9950.0)
        self.assertEqual((d.mode, d.code), ("idle", "calib_paused_grid"))

    def test_wallbox_pauses_and_does_not_count_as_full(self):
        self.step()
        for _ in range(5):
            d, _ = self.step(soc=100.0, charge_w=0.0, wallbox_charging=True)
            self.assertEqual((d.mode, d.code), ("idle", "calib_paused_wallbox"))
        self.assertEqual(self.s["phase"], "charge")
        d, _ = self.step(soc=100.0, charge_w=0.0)                       # Pause vorbei: zählt neu
        self.assertEqual(d.mode, "charge")

    def test_full_after_three_runs_without_intake(self):
        self.step()
        self.step(soc=100.0, charge_w=0.0)
        self.step(soc=100.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "charge")
        d, ev = self.step(soc=100.0, charge_w=0.0)
        self.assertEqual((self.s["phase"], d.code), ("hold", "calib_hold_full"))
        self.assertIn(("phase", "hold"), ev)

    def test_full_needs_100_percent(self):
        self.step()
        for _ in range(5):
            self.step(soc=97.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "charge")

    def test_stuck_just_below_100_counts_as_full_after_30_min(self):
        self.step()
        self.step(soc=99.0, charge_w=0.0)
        self.step(minutes=20, soc=99.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "charge")
        self.step(minutes=10, soc=99.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "hold")
        self.assertTrue(any("als voll gewertet" in n for n in self.s["notes"]))

    def test_bms_onset_and_diffs_recorded(self):
        self.step(soc=70.0)
        for soc in (79.0, 80.0, 81.0):
            self.step(soc=soc, charge_w=500.0, packs=packs(3530, 3370))
        self.assertEqual(self.s["cur"]["bms_soc"], 79.0)
        self.assertEqual(self.s["cur"]["diff80"]["1"], 160)
        self.assertGreater(self.s["cur"]["grid_wh"], 0)


class HoldDischargeTest(StepCase):
    def test_hold_keeps_charging_until_time_is_up(self):
        self.to_hold()
        d, _ = self.step(minutes=60, soc=100.0, charge_w=0.0)
        self.assertEqual((d.mode, d.code), ("charge", "calib_hold_full"))
        self.assertEqual(d.data["remaining_min"], 60)

    def test_hold_end_saves_cycle_and_starts_discharge(self):
        d, ev = self.to_discharge()
        rows = [a for k, a, *_ in ev if k == "cycle"]
        self.assertEqual(rows[0]["end_kind"], "full")
        self.assertEqual((d.mode, d.code, d.min_soc), ("normal", "calib_discharge", cal.CALIB_MIN_SOC))

    def test_next_cycle_at_5_percent_then_done(self):
        self.to_discharge()
        self.step(soc=30.0)
        d, ev = self.step(soc=5.0)
        self.assertEqual((self.s["cycle"], self.s["phase"], d.code), (2, "charge", "calib_charge_grid"))
        self.to_hold_again()
        self.step(minutes=PARAMS["hold_min"], soc=100.0, charge_w=0.0)
        d, ev = self.step(soc=5.0)
        self.assertIsNone(d)
        self.assertEqual(self.s["phase"], "done")
        self.assertFalse(self.s["active"])
        self.assertTrue(self.s["restore_pending"])
        self.assertIn(("report", None), ev)
        self.assertIn(("phase", "done"), ev)

    def to_hold_again(self):
        for _ in range(cal.FULL_RUNS):
            self.step(soc=100.0, charge_w=0.0)
        self.assertEqual(self.s["phase"], "hold")

    def test_restore_is_retried_every_10_min(self):
        self.s["restore_pending"], self.s["active"] = True, False
        _, ev = self.step()
        self.assertIn(("min_soc_restore", None), ev)
        _, ev = self.step(minutes=5)
        self.assertNotIn(("min_soc_restore", None), ev)
        _, ev = self.step(minutes=6)
        self.assertIn(("min_soc_restore", None), ev)


class AbortTest(StepCase):
    def assert_abort(self, d, text):
        self.assertEqual((d.mode, d.code), ("idle", "calib_aborted"))
        self.assertEqual(self.s["phase"], "aborted")
        self.assertIn(text, self.s["abort_reason"])
        self.assertTrue(self.s["restore_pending"])

    def test_cell_voltage(self):
        self.step()
        d, ev = self.step(packs=packs(max_mv=3660))
        self.assert_abort(d, "Zellspannung 3.66 V")
        self.assertTrue(any(k == "cycle" for k, *_ in ev))             # angefangener Zyklus gespeichert

    def test_temperature(self):
        d, _ = self.step(packs=packs(temp=46.0))
        self.assert_abort(d, "46 °C")

    def test_foreign_after_start_only(self):
        d, _ = self.step(foreign_at=(T0 - timedelta(minutes=5)).isoformat())
        self.assertEqual(d.mode, "charge")                               # alte Episode zählt nicht
        d, _ = self.step(foreign_at=(T0 + timedelta(minutes=1, seconds=30)).isoformat())
        self.assert_abort(d, "Fremdregler")

    def test_meter_three_runs(self):
        self.step(meter_ok=False)
        d, _ = self.step(meter_ok=False)
        self.assertEqual(self.s["phase"], "charge")
        d, _ = self.step(meter_ok=False)
        self.assert_abort(d, "Hauptzähler")

    def test_zendure_unreadable_three_runs(self):
        self.step()
        for _ in range(cal.FAIL_RUNS - 1):
            self.s, ev = cal.on_unreadable(self.s, self.t)
        self.assertTrue(self.s["active"])
        self.s, ev = cal.on_unreadable(self.s, self.t)
        self.assertEqual(self.s["phase"], "aborted")

    def test_no_abort_checks_while_discharging(self):
        self.to_discharge()
        d, _ = self.step(soc=50.0, packs=packs(temp=47.0))
        self.assertEqual(d.code, "calib_discharge")


class StartParamsTest(unittest.TestCase):
    def test_start_at_time_waits(self):
        s = cal.begin({**PARAMS, "start": "at", "start_time": "22:00"}, T0, 10.0)
        s, d, ev = cal.step(s, inp(), T0 + timedelta(minutes=1))
        self.assertEqual((s["phase"], d.mode, d.code), ("wait", "normal", ""))
        self.assertEqual(ev, [])
        s, d, ev = cal.step(s, inp(), T0.replace(hour=22, minute=0))
        self.assertEqual((s["phase"], d.code), ("charge", "calib_charge_grid"))

    def test_start_time_passed_means_tomorrow(self):
        s = cal.begin({**PARAMS, "start": "at", "start_time": "09:00"}, T0, 10.0)
        self.assertEqual(s["wait_until"][:10], "2026-11-04")

    def test_validate(self):
        ok = cal.validate_params({**PARAMS, "hold_min": 240})
        self.assertEqual(ok["hold_min"], 240)
        for bad in ({"hold_min": 241}, {"cycles": 6}, {"charge_w": 400}, {"start": "x"},
                    {"start": "at", "start_time": "25:00"}):
            with self.assertRaises(ValueError):
                cal.validate_params({**PARAMS, **bad})

    def test_config_defaults(self):
        z = ZendureConfig()
        self.assertEqual((z.calib_cycles, z.calib_charge_w, z.calib_hold_min, z.calib_max_grid_w), (3, 1200, 120, 10000))

    def test_report(self):
        rows = [{"cycle": 1, "grid_wh": 4200, "charge_s": 3 * 3600, "bms_soc": 80.0, "diff80": {"1": 160},
                 "diff100": {"1": 180}, "end_kind": "full"},
                {"cycle": 2, "grid_wh": 4300, "charge_s": 3 * 3600, "bms_soc": 96.0, "diff80": {"1": 40},
                 "diff100": {"1": 60}, "end_kind": "full"}]
        txt = cal.report(rows, 80.0)
        self.assertIn("nach oben verschoben", txt)
        self.assertIn("Ziel (kurz vor 100 %) erreicht", txt)
        self.assertIn("160 → 40 mV (kleiner)", txt)


class CodesTest(unittest.TestCase):
    def test_codes_known_and_rendered(self):
        for code in ("calib_charge_grid", "calib_hold_full", "calib_discharge", "calib_aborted",
                     "calib_paused_wallbox", "calib_paused_grid"):
            self.assertTrue(reasons.is_known(code))
        self.assertIn("1200 W", reasons.text("calib_charge_grid", {"cycle": 1, "cycles": 3, "watts": 1200,
                                                                    "grid_w": 1200, "soc": 64}))

    def test_headline(self):
        snap = {"devices": [{"id": "zendure", "reason": "calib_charge_grid", "action": "charge",
                             "data": {"cycle": 1, "cycles": 3, "watts": 1200, "grid_w": 1200, "soc": 64}}],
                "energy": {}}
        h = headline.cascade_headline(snap, ["zendure"], 50.0, 45.0, 63.0)
        self.assertEqual(h["state"], "Kalibrierung – Laden aus dem Netz")


# ---- Regelung mit Fake-Gerät ---------------------------------------------------------------

class Dev:
    """Zendure-Fake mit RAM-Sollwerten, minSoc (Flash) und Pack-Werten."""

    def __init__(self, soc=50.0, max_mv=3400, readable=True):
        self.props = {"acMode": 1, "inputLimit": 0, "outputLimit": 0, "electricLevel": soc, "minSoc": 100,
                      "socSet": 1000, "batCalTime": 60, "socCompSwitch": 0, "socStatus": 0, "socLimit": 0}
        self.max_mv = max_mv
        self.writes = []
        self.readable = readable

    def get_status(self, fresh=False):
        if not self.readable:
            return None
        p = self.props
        ch = p["inputLimit"] if p["acMode"] == 1 else 0
        return ZendureStatus(soc=p["electricLevel"], charge_w=ch, grid_in_w=ch,
                             discharge_w=p["outputLimit"] if p["acMode"] == 2 else 0,
                             home_out_w=p["outputLimit"] if p["acMode"] == 2 else 0, props=dict(p),
                             packs=packs(max_mv=self.max_mv))

    def set_mode(self, ac_mode, input_limit=None, output_limit=None):
        self.writes.append((ac_mode, input_limit, output_limit))
        if ac_mode is not None:
            self.props["acMode"] = ac_mode
        if input_limit is not None:
            self.props["inputLimit"] = input_limit
        if output_limit is not None:
            self.props["outputLimit"] = output_limit
        return True

    def write_props(self, props):
        self.writes.append(("props", dict(props)))
        if "minSoc" in props:
            self.props["minSoc"] = props["minSoc"]
        return True


class ControlCase(DbCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(zcm, "READBACK_WAIT_S", 0)
        p.start()
        self.addCleanup(p.stop)

    def ctl(self, dev, dry_run=False):
        return ZendureControl(SimpleNamespace(zendure=ZC), dev, dry_run=dry_run)

    def run_(self, c, dev, **kw):
        base = dict(avail_w=0.0, import_w=2000.0, wallbox_charging=False, override=None, base_load_w=2000.0,
                    meter_import_w=2000.0 + dev.get_status().charge_w if dev.readable else None)
        base.update(kw)
        return c.run(dev.get_status(), **base)

    def start(self, **params):
        db.set_calib_request({"action": "start", "params": {**PARAMS, **params}})


class ControlTest(ControlCase):
    def test_charges_from_grid_despite_import_and_sets_flag(self):
        dev = Dev()
        c = self.ctl(dev)
        self.start()
        p = self.run_(c, dev)
        self.assertEqual((p.mode, p.limit_w, p.code, p.grid), ("charge", 1200, "calib_charge_grid", "calib"))
        self.assertIn((1, 1200, 0), dev.writes)
        self.assertEqual(dev.props["minSoc"], 50)                        # Gerät auf 5 % (Readback ok)
        ctl = db.get_zendure_ctl()
        self.assertEqual((ctl["mode"], ctl["grid_charge"]), ("charge", "calib"))
        self.assertIsNone(ctl["foreign_at"])                             # eigenes Netzladen ≠ Fremdregler
        self.assertTrue(db.get_calib_state()["min_soc_ok"])
        # nächster Lauf: Batterie lädt 1200 W, Bezug steigt – kein Fremdregler, kein Drosseln
        p = self.run_(c, dev, import_w=500.0)
        self.assertEqual((p.mode, p.limit_w), ("charge", 1200))
        self.assertIsNone(db.get_zendure_ctl()["foreign_at"])

    def test_without_calibration_no_grid_charging(self):
        dev = Dev()
        p = self.run_(self.ctl(dev), dev)
        self.assertNotEqual(p.mode, "charge")
        st = ZendureStatus(soc=50, charge_w=1000)
        p = plan(ZC, st, **mk(avail_w=1500, ctl_mode="charge", ctl_limit_w=1000), export_cap_w=300)
        self.assertEqual(p.limit_w, 300)                                 # Lade-Deckel gilt
        self.assertEqual(p.grid, "")

    def test_dry_run_never_writes_to_the_device(self):
        dev = Dev()
        c = self.ctl(dev, dry_run=True)
        self.start()
        for _ in range(3):
            p = self.run_(c, dev)
        self.assertEqual(p.code, "calib_charge_grid")
        self.assertEqual(dev.writes, [])
        self.assertEqual(dev.props["minSoc"], 100)

    def test_abort_on_cell_voltage_writes_standby_and_restores_min_soc(self):
        dev = Dev()
        c = self.ctl(dev)
        self.start()
        self.run_(c, dev)
        dev.max_mv = 3700
        p = self.run_(c, dev)
        self.assertEqual((p.mode, p.code), ("idle", "calib_aborted"))
        self.assertEqual(dev.props["inputLimit"], 0)
        self.assertIsNone(db.get_zendure_ctl()["grid_charge"])
        s = db.get_calib_state()
        self.assertEqual(s["phase"], "aborted")
        self.run_(c, dev)                                                # Mindest-SoC zurück
        self.assertEqual(dev.props["minSoc"], 100)
        self.assertFalse(db.get_calib_state()["restore_pending"])

    def test_stop_from_settings(self):
        dev = Dev()
        c = self.ctl(dev)
        self.start()
        self.run_(c, dev)
        db.set_calib_request({"action": "stop"})
        p = self.run_(c, dev)
        self.assertNotEqual(p.code, "calib_charge_grid")
        self.assertEqual(db.get_calib_state()["phase"], "stopped")
        self.assertEqual(dev.props["minSoc"], 100)

    def test_discharge_phase_uses_5_percent_and_never_into_the_car(self):
        dev = Dev(soc=8.0)
        c = self.ctl(dev)
        s = cal.begin(dict(PARAMS), datetime.now() - timedelta(hours=5), 10.0)
        s.update(phase="discharge", cycle=1, min_soc_ok=True)
        db.set_calib_state(s)
        dev.props["minSoc"] = 50
        p = self.run_(c, dev, import_w=500.0)
        self.assertEqual((p.mode, p.code), ("discharge", "calib_discharge"))   # 8 % > 5 % → entlädt
        self.assertEqual(p.data["inner"], "discharging_base_load")
        p = self.run_(c, dev, import_w=500.0, wallbox_charging=True)
        self.assertEqual(p.mode, "idle")
        self.assertEqual(p.data["inner"], "wallbox_lock")
        self.assertIsNone(db.get_zendure_ctl()["foreign_at"])           # minSoc 50 ist erwartet

    def test_done_restores_previous_min_soc(self):
        dev = Dev(soc=5.0)
        c = self.ctl(dev)
        s = cal.begin({**PARAMS, "cycles": 1}, datetime.now() - timedelta(hours=5), 10.0)
        s.update(phase="discharge", cycle=1, min_soc_ok=True)
        db.set_calib_state(s)
        dev.props["minSoc"] = 50
        self.run_(c, dev, import_w=0.0)
        s = db.get_calib_state()
        self.assertEqual(s["phase"], "done")
        self.assertEqual(dev.props["minSoc"], 100)
        self.assertIsNotNone(s["report"])

    def test_zendure_unreadable_three_runs_aborts(self):
        dev = Dev()
        c = self.ctl(dev)
        self.start()
        self.run_(c, dev)
        for _ in range(3):
            c.run(None, avail_w=0, import_w=0, wallbox_charging=False, override=None)
        self.assertEqual(db.get_calib_state()["phase"], "aborted")

    def test_override_on_sets_grid_flag(self):
        dev = Dev()
        p = self.run_(self.ctl(dev), dev, override="on")
        self.assertEqual((p.mode, p.grid), ("charge", "override"))
        self.assertEqual(db.get_zendure_ctl()["grid_charge"], "override")

    def test_soc_props_logged_on_phase_change(self):
        dev = Dev()
        self.start()
        self.run_(self.ctl(dev), dev)
        ev = [r["event"] for r in db.get_soc_props()]
        self.assertIn("calib:charge", ev)
        self.assertTrue(all(r["bat_cal_time"] == 60 for r in db.get_soc_props()))

    def test_settings_min_soc_change_does_not_touch_device_during_calibration(self):
        import web
        db.set_calib_state({**cal.begin(dict(PARAMS), datetime.now(), 10.0), "phase": "charge"})
        old = {"zendure": {"enabled": True, "ip": "x", "min_soc": 10}}
        new = {"zendure": {"enabled": True, "ip": "x", "min_soc": 15}, "runtime": {"dry_run": False}}
        with mock.patch("zendure_control.apply_min_soc") as m:
            res = web._apply_min_soc_change(old, new)
        m.assert_not_called()
        self.assertTrue(res["calib"])


class SocPropsTest(DbCase):
    def test_hourly_and_on_change(self):
        props = {"batCalTime": 60, "socCompSwitch": 0, "socStatus": 0, "socLimit": 2}
        t = datetime(2026, 11, 3, 10, 5)
        self.assertTrue(db.record_soc_props(props, 10, now=t))
        self.assertFalse(db.record_soc_props(props, 10, now=t + timedelta(minutes=10)))
        self.assertTrue(db.record_soc_props({**props, "socLimit": 0}, 11, now=t + timedelta(minutes=20)))
        self.assertTrue(db.record_soc_props({**props, "socLimit": 0}, 12, now=t + timedelta(hours=1)))
        self.assertEqual([r["event"] for r in db.get_soc_props(3650)], ["hour", "change", "hour"])


# ---- Wächter ------------------------------------------------------------------------------

class WatchdogTest(DbCase):
    def clock(self):
        return 1000.0

    def test_no_throttle_with_grid_flag(self):
        for flag in ("calib", "override"):
            dev = Dev()
            db.set_zendure_ctl(mode="charge", limit_w=1200, grid_charge=flag)
            st = {}
            for _ in range(4):
                self.assertIsNone(wd.throttle_tick(self.cfg(), dev, 1500.0, st, self.clock))
            self.assertEqual(dev.writes, [])

    def test_throttle_without_flag(self):
        dev = Dev()
        db.set_zendure_ctl(mode="charge", limit_w=1200, grid_charge=None)
        st = {}
        wd.throttle_tick(self.cfg(), dev, 500.0, st, self.clock)
        self.assertEqual(wd.throttle_tick(self.cfg(), dev, 500.0, st, self.clock), "throttle")
        self.assertEqual(dev.writes[-1], (1, 700, 0))

    def test_wallbox_pauses_calibration_immediately(self):
        dev = Dev()
        db.set_zendure_ctl(mode="charge", limit_w=1200, grid_charge="calib")
        self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(IDLE)), "ok")
        self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(car(3500))), "standby")
        self.assertEqual(dev.writes[-1], (None, 0, 0))
        ctl = db.get_zendure_ctl()
        self.assertEqual((ctl["mode"], ctl["grid_charge"]), ("idle", None))

    def test_override_charging_not_paused_by_wallbox(self):
        dev = Dev()
        db.set_zendure_ctl(mode="charge", limit_w=2400, grid_charge="override")
        self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(car(3500))), "idle")
        self.assertEqual(dev.writes, [])


# ---- Kaskade ------------------------------------------------------------------------------

class CascadeCalibTest(unittest.TestCase):
    """Ganzer Kaskadenlauf (dry_run): Kalibrierung reserviert den ganzen PV-Überschuss, Heizstab bekommt nichts."""
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service
    budget = tcp.ServiceIntegrationTest.budget

    def test_battery_takes_whole_surplus(self):
        svc = self.service(False, 45.0, -3000.0, soc=60)
        svc.run_cascade()
        self.assertEqual(self.budget(), 600)                             # ohne Kalibrierung: 3000 − 2400
        db.set_calib_request({"action": "start", "params": PARAMS})
        svc = self.service(False, 45.0, -3000.0, soc=60)
        out = svc.run_cascade()
        bat = next(d for d in out["devices"] if d["id"] == "zendure")
        self.assertEqual(bat["code"], "calib_charge_grid")
        self.assertEqual(bat["data"]["limit_w"], 2400)                   # 1200 W Netz + 3000 W PV → Gerätemax
        self.assertEqual(self.budget(), 0)

    def test_wallbox_charging_pauses_calibration(self):
        db.set_calib_request({"action": "start", "params": PARAMS})
        svc = self.service(False, 45.0, 500.0, soc=60)
        self.goe_status = car(3500)
        out = svc.run_cascade()
        bat = next(d for d in out["devices"] if d["id"] == "zendure")
        self.assertEqual(bat["code"], "calib_paused_wallbox")


if __name__ == "__main__":
    unittest.main()
