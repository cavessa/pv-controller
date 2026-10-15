"""Grund-Codes, Entscheidungs-Schnappschuss, Zeitplan-Verlauf und Geräte-Ereignislog (reines Protokoll)."""
import json
import logging
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import db
import decisions
import heating_schedule as hs
import reasons
import schedule_log
import shutter_schedule as ss
from clients.zendure_client import ZendureStatus
from config import ZendureConfig, load_config
from controller import Controller, wallbox_reason_code
from heater_phases import HeaterParams, PhaseIn, decide_phases
from models import PhaseAction as A
from models import WallboxAction, WallboxStatus
from tests import test_controller_phases as tcp
from tests import test_heating_schedule as ths
from tests import test_shutter_schedule as tss
from zendure_control import plan

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"
TZ = ss.TZ
NOW = datetime(2026, 10, 14, 14, 32, 0)
P = HeaterParams(phase_w=1500, grid_assist_w=400, max_temp=64, resume_temp=62, min_on_s=420, min_off_s=120)


def phases(*states, changed=None):
    return [PhaseIn(f"PH{i + 1}", s, (changed or {}).get(i)) for i, s in enumerate(states)]


class ReasonTextTest(unittest.TestCase):
    def test_templates_are_german_and_fill_numbers(self):
        self.assertEqual(reasons.text("below_start_threshold", {"rest_w": 820, "threshold_w": 1100}),
                         "Überschuss 820 W unter der Startschwelle 1100 W")
        self.assertEqual(reasons.text("min_on_time", {"remaining_s": 140}), "Mindestlaufzeit läuft noch (140 s)")
        self.assertIn("21,5 °C", reasons.text("max_temp_reached", {"temp_c": 21.5, "max_temp_c": 64}))

    def test_missing_number_never_raises(self):
        self.assertEqual(reasons.text("min_on_time"), "Mindestlaufzeit läuft noch (? s)")
        self.assertEqual(reasons.text("gibt_es_nicht", {"a": 1}), "gibt_es_nicht")
        self.assertEqual(reasons.text(None), "")

    def test_no_english_templates(self):
        for code, tpl in reasons.REASONS.items():
            self.assertNotRegex(tpl, r"\b(the|not|is|will|charging mode|disabled)\b", code)


class PhaseCodeTest(unittest.TestCase):
    """decide_phases: zum bestehenden Text kommt der richtige Code – die Aktion bleibt, wie sie war."""

    def codes(self, outs):
        return [o.code for o in outs]

    def test_below_start_threshold(self):
        outs = decide_phases(phases(False, False, False), 50.0, 820, P, NOW)
        self.assertEqual([o.action for o in outs], [A.UNCHANGED] * 3)
        self.assertEqual(self.codes(outs), ["below_start_threshold"] * 3)
        self.assertEqual(outs[0].data["threshold_w"], 1100)            # 1500 − 400 Netz-Zuschuss
        self.assertEqual(outs[0].data["budget_w"], 820)
        self.assertIn("bleibt AUS", outs[0].reason)                    # Text unverändert

    def test_turn_on_and_one_phase_per_run(self):
        outs = decide_phases(phases(False, False, False), 50.0, 4000, P, NOW)
        self.assertEqual([o.action for o in outs], [A.TURN_ON, A.UNCHANGED, A.UNCHANGED])
        self.assertEqual(self.codes(outs), ["budget_available", "one_phase_per_run", "below_start_threshold"])

    def test_min_on_time(self):
        outs = decide_phases(phases(True, False, False, changed={0: NOW - timedelta(seconds=280)}), 50.0, 0, P, NOW)
        self.assertEqual(outs[0].action, A.UNCHANGED)
        self.assertEqual(outs[0].code, "min_on_time")
        self.assertEqual(outs[0].data["remaining_s"], 140)

    def test_min_off_time(self):
        outs = decide_phases(phases(False, False, False, changed={0: NOW - timedelta(seconds=30)}), 50.0, 2000, P, NOW)
        self.assertEqual((outs[0].action, outs[0].code, outs[0].data["remaining_s"]), (A.UNCHANGED, "min_off_time", 90))

    def test_turn_off_below_stop_threshold(self):
        outs = decide_phases(phases(True, False, False, changed={0: NOW - timedelta(hours=1)}), 50.0, 0, P, NOW)
        self.assertEqual((outs[0].action, outs[0].code), (A.TURN_OFF, "below_stop_threshold"))

    def test_max_temp(self):
        outs = decide_phases(phases(True, False, False), 64.5, 5000, P, NOW)
        self.assertEqual(outs[0].action, A.TURN_OFF)
        self.assertEqual(self.codes(outs), ["max_temp_reached"] * 3)
        self.assertEqual(outs[0].data, {"temp_c": 64.5, "max_temp_c": 64})

    def test_temp_unknown(self):
        outs = decide_phases(phases(True, None, False), None, 5000, P, NOW)
        self.assertEqual(self.codes(outs), ["temp_unknown"] * 3)
        self.assertTrue(outs[1].blind and outs[1].data["blind_off"])

    def test_hysteresis_band(self):
        outs = decide_phases(phases(False, False, False), 63.0, 5000, P, NOW)
        self.assertEqual(self.codes(outs), ["hysteresis_band"] * 3)
        self.assertEqual(outs[0].data["resume_temp_c"], 62)

    def test_summer_cold_water_heats_from_grid(self):
        outs = decide_phases(phases(False, True, None), 41.0, 0, P, NOW, summer="heat", summer_reason="Sommer")
        self.assertEqual([o.action for o in outs], [A.TURN_ON, A.UNCHANGED, A.UNCHANGED])
        self.assertEqual(self.codes(outs), ["summer_mode_grid_heat", "summer_mode_grid_heat", "failsafe_phase"])
        self.assertEqual(outs[0].data["temp_c"], 41.0)

    def test_summer_stop_and_manual_off(self):
        self.assertEqual(self.codes(decide_phases(phases(True, True, True), 53.0, 0, P, NOW, summer="stop")),
                         ["summer_mode_stop"] * 3)
        outs = decide_phases(phases(True, True, True), 50.0, 5000, P, NOW, force="off")
        self.assertEqual((outs[0].code, outs[0].data["mode"]), ("manual_override", "aus"))

    def test_manual_on(self):
        outs = decide_phases(phases(False, False, False), 50.0, 0, P, NOW, force="on")
        self.assertEqual((outs[0].action, outs[0].code, outs[0].data["mode"]), (A.TURN_ON, "manual_override", "an"))

    def test_global_failsafe(self):
        outs = decide_phases(phases(True, False, False), 50.0, 5000, P, NOW, surplus_fail_safe=True)
        self.assertEqual([o.action for o in outs], [A.UNCHANGED] * 3)
        self.assertEqual(self.codes(outs), ["failsafe_global"] * 3)

    def test_phase_unknown_and_disabled(self):
        ph = phases(None, False, False)
        ph[2].disabled = True
        outs = decide_phases(ph, 50.0, 1200, P, NOW)                   # unbekannte Phase zählt als laufend
        self.assertEqual(self.codes(outs), ["phase_unknown", "below_start_threshold", "phase_disabled"])
        outs = decide_phases(phases(None, False, False, changed={0: NOW - timedelta(hours=1)}), 50.0, 0, P, NOW)
        self.assertEqual((outs[0].action, outs[0].code, outs[0].blind), (A.TURN_OFF, "phase_unknown_off_sent", True))

    def test_every_code_is_known(self):
        seen = set()
        for temp, budget, kw in ((50.0, 0, {}), (50.0, 5000, {}), (65.0, 0, {}), (None, 0, {}), (63.0, 5000, {}),
                                 (50.0, 0, {"force": "on"}), (50.0, 0, {"force": "off"}),
                                 (50.0, 0, {"summer": "heat"}), (50.0, 0, {"summer": "stop"}),
                                 (50.0, 0, {"surplus_fail_safe": True})):
            for st in ((False, False, False), (True, True, None), (None, None, None)):
                for age in (10, 10_000):
                    ch = {i: NOW - timedelta(seconds=age) for i in range(3)}
                    seen |= {o.code for o in decide_phases(phases(*st, changed=ch), temp, budget, P, NOW, **kw)}
        self.assertNotIn("", seen)
        self.assertEqual({c for c in seen if not reasons.is_known(c)}, set())


def mk(**kw):
    base = dict(avail_w=0, import_w=0, wallbox_charging=False, override=None,
                ctl_mode="idle", ctl_limit_w=0, seconds_since_change=1e9)
    base.update(kw)
    return base


class BatteryCodeTest(unittest.TestCase):
    def setUp(self):
        self.zc = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100, max_charge_w=2400,
                                max_discharge_w=2400)
        self.st = ZendureStatus(soc=50, home_out_w=0)

    def code(self, **kw):
        p = plan(self.zc, self.st, **mk(**kw))
        self.assertTrue(reasons.is_known(p.code), p.code)
        json.dumps(p.data)                                              # immer serialisierbar (kein inf)
        return p.code

    def test_wallbox_lock(self):
        p = plan(self.zc, self.st, **mk(import_w=500, wallbox_charging=True))
        self.assertEqual((p.mode, p.action, p.code), ("idle", "blocked", "wallbox_lock"))

    def test_codes(self):
        self.assertEqual(self.code(avail_w=1234), "charging_surplus")
        self.assertEqual(self.code(avail_w=150), "below_start_threshold")
        self.assertEqual(self.code(import_w=420), "discharging_base_load")
        self.assertEqual(self.code(import_w=420, base_load_w=None), "base_load_unknown")
        self.assertEqual(self.code(import_w=420, base_load_w=20), "no_discharge_controlled_load")
        self.assertEqual(self.code(), "below_start_threshold")
        self.assertEqual(self.code(override="off", import_w=500), "manual_override")
        self.assertEqual(self.code(override="on"), "manual_override")
        self.assertEqual(self.code(import_w=500, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=30),
                         "mode_change_pause")
        self.st.soc = 100
        self.assertEqual(self.code(avail_w=2000), "soc_target_reached")
        self.st.soc = 8
        self.zc.target_soc = 5                                          # Laden kein Thema, Entladen: leer
        self.assertEqual(self.code(import_w=500), "battery_empty")


class WallboxCodeTest(unittest.TestCase):
    def test_codes(self):
        st = lambda **kw: WallboxStatus(**{"pv_surplus_active": True, "car_state": 3, "power_w": 0.0, **kw})
        U = WallboxAction.UNCHANGED
        self.assertEqual(wallbox_reason_code(False, None, WallboxAction.SKIPPED)[0], "wallbox_disabled")
        self.assertEqual(wallbox_reason_code(True, None, WallboxAction.ERROR)[0], "device_unreachable")
        self.assertEqual(wallbox_reason_code(True, st(car_state=1), U)[0], "not_connected")
        self.assertEqual(wallbox_reason_code(True, st(pv_surplus_active=False), WallboxAction.SKIPPED)[0],
                         "wallbox_not_eco_mode")
        self.assertEqual(wallbox_reason_code(True, st(car_state=2, power_w=4100.0), U)[0], "charging")
        self.assertEqual(wallbox_reason_code(True, st(pv_surplus_active=False, car_state=2, power_w=4170.0),
                                             WallboxAction.SKIPPED)[0], "charging_manual")
        self.assertEqual(wallbox_reason_code(True, st(car_state=4), U)[0], "car_full")
        self.assertEqual(wallbox_reason_code(True, st(), WallboxAction.RELEASE)[0], "wallbox_released")
        code, data = wallbox_reason_code(True, st(amp=6, psm=1), U)
        self.assertEqual((code, data["amp"], data["phases"]), ("waiting_for_car", 6, 1))


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        db.init_cascade_tables()
        db.init_cascade_settings_table()

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def config(self, **patch):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["wallbox"]["enabled"] = False
        raw["runtime"].update({"dry_run": False, "enabled": True})
        raw["heater"].update({"storage_max_temp": 64, "storage_temp_hysteresis": 2, "grid_assist_w": 400,
                              "budget_smooth_min": 1, "budget_smooth_down_min": 1})
        raw["summer_mode"] = {"enabled": False, "min_temp": 45, "target_temp": 52}
        for k, v in patch.items():
            raw[k] = {**raw.get(k, {}), **v} if isinstance(v, dict) else v
        p = Path(self.tmp.name) / "c.json"
        p.write_text(json.dumps(raw))
        self.raw = raw
        return load_config(p)

    def controller(self, cfg, ph=(False, False, False), temp=50.0, main=-5000.0, heater=0.0, pv=6000.0):
        c = Controller(cfg)
        c.solax, c.solax2, c.zendure, c.goe = tcp.FakeSolax(pv), None, None, None
        c.ph1, c.ph2, c.ph3 = (tcp.FakePlug(s) for s in ph)
        c.storage, c.main_meter, c.heater_meter = tcp.FakeTemp(temp), tcp.FakeMeter(main), tcp.FakeMeter(heater)
        return c

    def cascade(self, cfg, temp=50.0, main=-3000.0, pv=6000.0, heater=0.0, soc=50):
        from cascade_service import CascadeService
        svc = CascadeService(cfg)
        svc.heater_meter = SimpleNamespace(get_total_power_w=lambda: heater)
        svc.main_meter = SimpleNamespace(get_total_power_w=lambda: main)
        svc.storage = SimpleNamespace(get_temperature_c=lambda: temp)
        svc.solax = SimpleNamespace(get_pv_power_w=lambda: pv)
        svc.solax2 = None
        if svc.zendure_ctl is not None:
            svc.zendure_client = SimpleNamespace(get_status=lambda: ZendureStatus(soc=soc))
            svc.zendure_ctl.client = svc.zendure_client
        return svc

    def by_id(self, snap):
        return {d["id"]: d for d in snap["devices"]}


class CascadeCodeTest(TempDb):
    def test_winter_battery_has_priority(self):
        cfg = self.config(runtime={"dry_run": True}, zendure={"enabled": True, "ip": "x", "control_enabled": True})
        out = self.cascade(cfg, temp=45.0, main=-3000.0).run_cascade()
        dev = {d["id"]: d for d in out["devices"]}
        self.assertEqual(out["profile"], "winter")
        self.assertEqual((dev["zendure"]["action"], dev["zendure"]["code"]), ("charge", "priority_first"))
        self.assertEqual(dev["zendure"]["data"]["limit_w"], 2400)
        # Heizstab: 3000 − 2400 = 600 W, Startschwelle 1500 − 400 = 1100 W
        self.assertEqual(dev["heizstab"]["code"], "below_start_threshold")
        self.assertEqual((dev["heizstab"]["data"]["budget_w"], dev["heizstab"]["data"]["threshold_w"],
                          dev["heizstab"]["data"]["battery_w"]), (600, 1100, 2400))
        self.assertEqual(db.get_cascade_budget("heizstab")["budget_w"], 600)     # Entscheidung unverändert

    def test_heater_budget_codes(self):
        cfg = self.config(runtime={"dry_run": True})
        dev = lambda main: {d["id"]: d for d in self.cascade(cfg, main=main).run_cascade()["devices"]}["heizstab"]
        self.assertEqual(dev(-3000.0)["code"], "budget_available")
        self.assertEqual(dev(-1200.0)["code"], "grid_assist")          # 1 Phase mit Netz-Zuschuss
        self.assertEqual(dev(-500.0)["code"], "below_start_threshold")

    def test_smoothing_wait(self):
        cfg = self.config(runtime={"dry_run": True}, heater={"budget_smooth_min": 10, "budget_smooth_down_min": 2})
        fmt = "%Y-%m-%d %H:%M"
        now = datetime.now()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
            for m in range(1, 10):
                conn.execute("INSERT INTO heater_budget_log VALUES (?, 0)", ((now - timedelta(minutes=m)).strftime(fmt),))
        out = self.cascade(cfg, main=-3000.0).run_cascade()
        h = {d["id"]: d for d in out["devices"]}["heizstab"]
        self.assertEqual(h["code"], "smoothing_wait")
        self.assertEqual((h["data"]["raw_w"], h["data"]["budget_w"], h["data"]["smooth_min"]), (3000, 300, 10))

    def test_other_devices(self):
        cfg = self.config(runtime={"dry_run": True})
        db.create_cascade_device("ebike", "E-Bike", "shelly_gen2", 180, 9)
        run = lambda main, heater=0.0: {d["id"]: d for d in self.cascade(cfg, main=main, heater=heater)
                                        .run_cascade()["devices"]}["ebike"]
        self.assertEqual(run(0.0)["code"], "no_surplus")
        d = run(-3000.0, heater=3000.0)                                # 6000 brutto
        self.assertEqual((d["action"], d["code"], d["data"]["threshold_w"]), ("turn_on", "surplus_sufficient", 280))
        d = run(-3000.0, heater=3000.0)
        self.assertEqual((d["action"], d["code"]), ("keep_on", "keep_running"))
        d = run(-100.0)
        self.assertEqual((d["action"], d["code"]), ("skip_min_on", "min_on_time"))
        self.assertGreater(d["data"]["remaining_s"], 0)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE cascade_devices SET turned_on_at = ? WHERE id = 'ebike'",
                         ((datetime.now() - timedelta(minutes=30)).isoformat(),))
        d = run(-100.0)
        self.assertEqual((d["action"], d["code"]), ("turn_off", "budget_exhausted"))
        d = run(-100.0)                                # die Wallbox davor läuft noch (Mindestlaufzeit): nichts übrig
        self.assertEqual((d["action"], d["code"]), ("keep_off", "not_reached"))
        self.assertLessEqual(d["data"]["rest_w"], 0)


class SnapshotTest(TempDb):
    def run_all(self, cfg, **kw):
        cas = self.cascade(cfg, **{k: v for k, v in kw.items() if k in ("temp", "main", "pv", "heater")}).run_cascade()
        res = self.controller(cfg, **kw).run()
        now = datetime(2026, 10, 14, 14, 32, tzinfo=TZ)
        return decisions.record_run(cfg, self.raw, cas, res, now), res

    def test_snapshot_shape(self):
        snap, _ = self.run_all(self.config(), main=-820.0, pv=3120.0)
        self.assertEqual(snap["ts"], "2026-10-14T14:32:00+02:00")
        self.assertEqual(snap["profile"], "winter")
        self.assertIsNone(snap["failsafe"])
        self.assertEqual((snap["energy"]["pv_w"], snap["energy"]["grid_w"], snap["energy"]["surplus_w"]),
                         (3120, -820, 820))
        d = self.by_id(snap)
        self.assertEqual(d["zendure"]["reason"], "not_in_cascade")                # vorgesehen, leer
        self.assertEqual((d["heizstab"]["reason"], d["heizstab"]["data"]["threshold_w"]),
                         ("below_start_threshold", 1100))
        self.assertNotIn("rest_w", d["heizstab"]["data"])
        self.assertEqual(decisions.reason_text(snap, d["heizstab"]), "Überschuss 820 W unter der Startschwelle 1100 W")
        self.assertEqual((d["heizstab_phase1"]["action"], d["heizstab_phase1"]["reason"]),
                         ("keep_off", "below_start_threshold"))
        # Budget-Zahlen der Phasen stehen einmal im Schnappschuss, nicht in jeder Phase
        self.assertEqual(snap["phases"], {"budget_w": 820, "target_phases": 0, "running": 0, "threshold_w": 1100})
        for i in (1, 2, 3):
            self.assertNotIn("data", d[f"heizstab_phase{i}"])
        self.assertEqual(decisions.device_data(snap, d["heizstab_phase1"]),
                         {"budget_w": 820, "target_phases": 0, "running": 0, "threshold_w": 1100, "rest_w": 820})
        self.assertEqual(decisions.reason_text(snap, d["heizstab_phase1"]),
                         "Überschuss 820 W unter der Startschwelle 1100 W")
        self.assertEqual(d["wallbox_control"]["reason"], "wallbox_disabled")
        self.assertEqual(decisions.latest(), snap)
        for dev in snap["devices"]:
            self.assertTrue(reasons.is_known(dev["reason"]), dev)

    def test_one_row_per_run_and_range_query(self):
        cfg = self.config()
        for minute in (30, 31, 31, 32):
            decisions.save(decisions.build(cfg, None, None, datetime(2026, 10, 14, 14, minute, tzinfo=TZ)))
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_snapshot").fetchone()[0], 3)
        got = decisions.snapshots(datetime(2026, 10, 14, 14, 31, tzinfo=TZ), datetime(2026, 10, 14, 14, 32, tzinfo=TZ))
        self.assertEqual([s["ts"][11:16] for s in got], ["14:31", "14:32"])

    def test_global_failsafe_names_the_unreadable_device(self):
        snap, res = self.run_all(self.config(), ph=(True, False, False), main=None)
        self.assertTrue(res.fail_safe_active)
        self.assertEqual(snap["failsafe"], {"reason": "failsafe_global", "unreadable": ["main_meter"],
                                            "names": ["Hauptzähler"]})
        d = self.by_id(snap)
        self.assertEqual((d["heizstab_phase1"]["action"], d["heizstab_phase1"]["reason"]),
                         ("keep_on", "failsafe_global"))
        self.assertNotIn("data", d["heizstab_phase1"])
        self.assertIn("Hauptzähler nicht lesbar", decisions.reason_text(snap, d["heizstab_phase1"]))
        self.assertIn({"id": "main_meter", "name": "Hauptzähler", "offline_since": snap["ts"]}, snap["health"])

    def test_max_temp_and_unknown_phase_in_snapshot(self):
        snap, _ = self.run_all(self.config(), ph=(None, True, False), temp=65.0, main=0.0, heater=1500.0)
        d = self.by_id(snap)
        self.assertEqual((d["heizstab_phase2"]["action"], d["heizstab_phase2"]["reason"]),
                         ("turn_off", "max_temp_reached"))
        self.assertEqual((d["heizstab_phase1"]["reason"], d["heizstab_phase1"]["blind"]), ("max_temp_reached", True))
        self.assertIn("ph1", [h["id"] for h in snap["health"]])

    def test_temp_unknown_in_snapshot(self):
        snap, _ = self.run_all(self.config(), ph=(True, False, False), temp=None)
        self.assertEqual(self.by_id(snap)["heizstab_phase1"]["reason"], "temp_unknown")
        self.assertNotIn("storage_temp_c", snap["energy"])

    def test_summer_cold_water(self):
        cfg = self.config(summer_mode={"enabled": True, "min_temp": 45, "target_temp": 52})
        snap, res = self.run_all(cfg, temp=41.0, main=200.0, pv=0.0)
        self.assertEqual(snap["profile"], "summer")
        self.assertTrue(res.summer_mode_heating)
        d = self.by_id(snap)["heizstab_phase1"]
        self.assertEqual((d["action"], d["reason"], d["data"]["temp_c"]), ("turn_on", "summer_mode_grid_heat", 41.0))
        self.assertNotIn("phases", snap)
        self.assertTrue(snap["summer_mode_heating"])

    def test_no_addresses_or_secrets_in_snapshot(self):
        cfg = self.config(zendure={"enabled": True, "ip": "192.168.2.77", "control_enabled": True})
        db.create_cascade_device("ebike", "E-Bike", "shelly_gen2", 180, 9, ip_address="192.168.2.99")
        snap, _ = self.run_all(cfg, ph=(None, False, False), main=None)
        text = json.dumps(snap, ensure_ascii=False)
        self.assertNotRegex(text, r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
        self.assertNotRegex(text, r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}")
        self.assertNotRegex(text, r"(?i)https?://|token|passw|pwd|api[_-]?key|sk-")

    def test_phase_specific_numbers_stay_at_the_phase(self):
        cfg = self.config()
        db.log_phase_change(1, "on")                                   # Phase 1 läuft seit eben: Mindestlaufzeit
        cas = self.cascade(cfg, main=1500.0, heater=1500.0, pv=0.0).run_cascade()
        res = self.controller(cfg, ph=(True, False, False), main=1500.0, heater=1500.0, pv=0.0).run()
        snap = decisions.build(cfg, cas, res, datetime(2026, 10, 14, 14, 32, tzinfo=TZ))
        d = self.by_id(snap)
        self.assertEqual(d["heizstab_phase1"]["reason"], "min_on_time")
        self.assertEqual(list(d["heizstab_phase1"]["data"]), ["remaining_s"])
        self.assertNotIn("data", d["heizstab_phase2"])
        self.assertRegex(decisions.reason_text(snap, d["heizstab_phase1"]), r"Mindestlaufzeit läuft noch \(4\d\d s\)")

    def test_record_run_never_raises(self):
        cfg = self.config()
        self.assertIsNone(decisions.record_run(cfg, None, {"devices": "kaputt"}, None))
        snap = decisions.record_run(cfg, None, None, None, datetime(2026, 10, 14, 14, 32, tzinfo=TZ))
        self.assertEqual(snap["controller"], "error")

    def test_size_stays_small(self):
        cfg = self.config(runtime={"dry_run": True}, zendure={"enabled": True, "ip": "x", "control_enabled": True})
        cas = self.cascade(cfg).run_cascade()
        res = self.controller(cfg).run()
        size = decisions.save(decisions.build(cfg, cas, res, datetime(2026, 10, 14, 14, 32, tzinfo=TZ)))
        self.assertLess(size, 3000)

    def test_prune_keeps_48_hours(self):
        cfg = self.config()
        now = datetime(2026, 10, 14, 4, 0)
        for hours in (1, 47, 49, 100):
            decisions.save(decisions.build(cfg, None, None, (now - timedelta(hours=hours)).replace(tzinfo=TZ)))
        self.assertEqual(db.prune(now)["decision_snapshot"], 2)
        self.assertEqual(len(decisions.snapshots()), 2)


class OpenHabCheckTest(TempDb):
    RAW = {"openhab": {"url": "http://192.168.2.5:8080", "token": "oh.geheim"}}

    def at(self, minute):
        return datetime(2026, 10, 14, 12, minute, tzinfo=TZ)

    def test_every_five_minutes_short_timeout_no_token(self):
        calls = []

        def get(url, timeout, headers):
            calls.append((url, timeout, headers))
            return SimpleNamespace(status_code=200)
        self.assertEqual([decisions.check_openhab(self.RAW, self.at(m), get=get) for m in range(0, 11)],
                         [True, None, None, None, None, True, None, None, None, None, True])
        self.assertEqual(calls[0], ("http://192.168.2.5:8080/rest/", 2.0, {"Accept": "application/json"}))
        self.assertEqual(decisions.health_events(), [])                # erreichbar von Anfang an: kein Ereignis

    def test_outage_without_due_schedule_is_logged_without_address(self):
        def dead(url, timeout, headers):
            raise ConnectionError(f"HTTPConnectionPool(host='192.168.2.5', port=8080): {url}")
        self.assertFalse(decisions.check_openhab(self.RAW, self.at(0), get=dead))
        self.assertFalse(decisions.check_openhab(self.RAW, self.at(5), get=dead))
        self.assertFalse(decisions.check_openhab(self.RAW, self.at(10),
                                                 get=lambda *a, **k: SimpleNamespace(status_code=503)))
        self.assertTrue(decisions.check_openhab(self.RAW, self.at(15), get=lambda *a, **k: SimpleNamespace(status_code=200)))
        ev = list(reversed(decisions.health_events()))
        self.assertEqual([(e["device_id"], e["event"], e["ts"][11:16]) for e in ev],
                         [("openhab", "offline_ab", "12:00"), ("openhab", "wieder_da", "12:15")])
        self.assertEqual(ev[0]["detail"], "ConnectionError")
        self.assertNotIn("192.168", json.dumps(ev))

    def test_no_url_no_check_and_never_raises(self):
        self.assertIsNone(decisions.check_openhab({}, self.at(0), get=lambda *a, **k: 1 / 0))
        self.assertIsNone(decisions.check_openhab(None, self.at(0)))
        self.assertFalse(decisions.check_openhab(self.RAW, self.at(0), get=lambda *a, **k: 1 / 0))

    def test_error_text_from_schedule_runs_is_scrubbed(self):
        decisions.note_health("openhab", False, self.at(0),
                              "GET /items: HTTPConnectionPool(host='192.168.2.5', port=8080): Max retries "
                              "http://192.168.2.5:8080/rest/items aa:bb:cc:dd:ee:ff sk-proj-AbCd1234")
        detail = decisions.health_events()[0]["detail"]
        for frag in ("192.168", "8080", "aa:bb", "AbCd"):
            self.assertNotIn(frag, detail)
        schedule_log.record(schedule_log.HEATING, 1, "wohnen", "openhab_error", self.at(0),
                            message="HTTPConnectionPool(host='192.168.2.5', port=8080) http://oh:8080/rest")
        msg = schedule_log.results()[0]["message"]
        self.assertNotIn("192.168", msg)
        self.assertNotIn("http", msg)


class WallboxTextTest(TempDb):
    """Wallbox-Texte in Log und /api/status sind deutsch; die Entscheidung selbst ist dieselbe."""

    def test_reasons_are_german(self):
        c = Controller(self.config(wallbox={"enabled": True, "url": "http://wb"}))
        st = WallboxStatus(pv_surplus_active=False, force_state=0, car_state=1)
        cases = [(st, None), (None, None), (None, "go-e-Status nicht lesbar."),
                 (WallboxStatus(pv_surplus_active=True, force_state=1, car_state=3), None),
                 (WallboxStatus(pv_surplus_active=True, force_state=0, car_state=3), None)]
        actions = []
        for status, err in cases:
            _s, d = c._decide_wallbox(status, err)
            actions.append(d.action)
            self.assertNotRegex(d.reason, r"(?i)\b(is not|will not|disabled|initialised|could not|unplugged|switching)\b")
        self.assertEqual(actions, [WallboxAction.SKIPPED, WallboxAction.ERROR, WallboxAction.ERROR,
                                   WallboxAction.RELEASE, WallboxAction.UNCHANGED])
        _s, d = c._decide_wallbox(st, None)
        self.assertIn("nicht im PV-Überschussmodus", d.reason)
        self.assertEqual((d.target_logic_mode, d.target_amp), (4, c.cfg.wallbox.eco_amps))   # Auto-Rückstellung bleibt
        c2 = Controller(self.config())
        self.assertEqual(c2._decide_wallbox(None, None)[1].reason, "Wallbox in der Config deaktiviert.")


class HealthEventTest(TempDb):
    T = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    def events(self):
        return [(e["device_id"], e["event"], e["expected"]) for e in reversed(decisions.health_events())]

    def test_only_changes_are_logged(self):
        for i, ok in enumerate((True, False, False, False, True, True, False)):
            decisions.health_update({"solax": {"ok": ok}}, self.T + timedelta(minutes=i))
        self.assertEqual(self.events(), [("solax", "offline_ab", 0), ("solax", "wieder_da", 0),
                                         ("solax", "offline_ab", 0)])
        self.assertEqual(decisions.offline_devices(),
                         [{"id": "solax", "name": "Wechselrichter (Solax)", "since": "2026-10-14T12:06:00+02:00",
                           "expected": False}])

    def test_pergola_expected_offline_without_sun(self):
        loc = tss.LOC
        night, noon = datetime(2026, 10, 14, 23, 0, tzinfo=TZ), datetime(2026, 10, 14, 13, 0, tzinfo=TZ)
        self.assertTrue(decisions.pergola_expected_offline(0.0, noon, loc))
        self.assertTrue(decisions.pergola_expected_offline(40.0, night, loc))
        self.assertFalse(decisions.pergola_expected_offline(1500.0, noon, loc))
        self.assertTrue(decisions.pergola_expected_offline(None, night, loc))    # Haupt-WR weg: Nacht zählt
        self.assertFalse(decisions.pergola_expected_offline(None, noon, loc))
        self.assertFalse(decisions.pergola_expected_offline(None, night, None))

    def test_pergola_night_then_still_dead_at_day(self):
        decisions.health_update({"pergola": {"ok": False, "expected": True}}, self.T)
        decisions.health_update({"pergola": {"ok": False, "expected": True}}, self.T + timedelta(hours=1))
        decisions.health_update({"pergola": {"ok": False, "expected": False}}, self.T + timedelta(hours=9))
        decisions.health_update({"pergola": {"ok": False, "expected": False}}, self.T + timedelta(hours=10))
        decisions.health_update({"pergola": {"ok": True}}, self.T + timedelta(hours=11))
        self.assertEqual(self.events(), [("pergola", "offline_ab", 1), ("pergola", "offline_ab", 0),
                                         ("pergola", "wieder_da", 0)])

    def test_collect_covers_all_devices(self):
        cfg = self.config(zendure={"enabled": True, "ip": "x"}, wallbox={"enabled": True, "url": "http://wb"},
                          solax2={"enabled": True, "url": "http://p", "pwd": "x"})
        db.create_cascade_device("ebike", "E-Bike", "shelly_gen2", 180, 9, ip_address="192.168.2.99")
        db.increment_cascade_device_errors("ebike")
        c = self.controller(cfg, ph=(None, False, False), temp=None, main=None, pv=None)
        c.heater_meter = tcp.FakeMeter(None)
        res = c.run()
        st = decisions.collect_health(cfg, res, datetime(2026, 10, 14, 23, 0, tzinfo=TZ), tss.LOC)
        self.assertEqual({k for k, v in st.items() if not v["ok"]},
                         {"solax", "pergola", "main_meter", "heater_meter", "storage_temp", "ph1", "battery",
                          "wallbox", "cascade:ebike"})
        self.assertTrue(st["ph2"]["ok"] and st["ph3"]["ok"])
        self.assertTrue(st["pergola"]["expected"])                     # 23 Uhr, Haupt-WR nicht lesbar
        self.assertEqual(st["cascade:ebike"]["name"], "E-Bike")
        self.assertEqual(st["heater_meter"]["detail"], "Ersatzwert aus den Phasen")

    def test_prune_keeps_30_days(self):
        now = datetime(2026, 10, 14, 4, 0)
        for days in (1, 29, 31):
            decisions.health_update({f"d{days}": {"ok": False}}, (now - timedelta(days=days)).replace(tzinfo=TZ))
        self.assertEqual(db.prune(now)["device_health_event"], 1)


class ShutterResultTest(tss.RunCase):
    """Rollladen-Zeitpläne: jedes Ergebnis landet mit Grund-Code im Verlauf – die Fahrten bleiben wie bisher."""

    def rows(self, **kw):
        return [(r["target_id"], r["code"]) for r in reversed(schedule_log.results(schedule_log.SHUTTER, **kw))]

    def one(self):
        rows = schedule_log.results(schedule_log.SHUTTER)
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(reasons.is_known(rows[0]["code"]))
        return rows[0]

    def test_sent(self):
        e = self.add()
        self.run_at(21, 0)
        r = self.one()
        self.assertEqual((r["schedule_id"], r["target_id"], r["code"], r["ts"]),
                         (e["id"], "schlafen", "sent", "2026-10-13T21:00:00+02:00"))
        self.assertEqual((r["data"]["position"], r["data"]["target_pct"]), (0, 100))

    def test_door_open(self):
        self.c.items["T_Door"]["state"] = "OPEN"
        self.add(shutter_id="terrasse")
        self.run_at(21, 0)
        r = self.one()
        self.assertEqual((r["code"], r["data"]["door"]), ("skipped_door_open", "offen"))
        self.assertIn("Tür offen", r["message"])
        self.assertEqual(self.c.sends, [])

    def test_waiting_door_then_closed(self):
        rooms = [dict(r, retry_when_closed=True) if r["id"] == "terrasse" else r for r in tss.ROOMS]
        cfg = tss.raw(rooms=rooms)
        self.c.items["T_Door"]["state"] = "OPEN"
        self.add(shutter_id="terrasse")
        self.run_at(21, 0, cfg=cfg)
        self.run_at(21, 30, cfg=cfg)
        self.c.items["T_Door"]["state"] = "CLOSED"
        self.run_at(21, 40, cfg=cfg)
        self.assertEqual(self.rows(), [("terrasse", "waiting_door"), ("terrasse", "sent")])

    def test_frost(self):
        self.c.items["S_Roll"]["state"] = "100"
        self.c.items["Out_T"]["state"] = "-3 °C"
        self.add(action="up", time="07:00")
        self.run_at(7, 0)
        r = self.one()
        self.assertEqual((r["code"], r["data"]["outdoor_c"]), ("skipped_frost", -3.0))

    def test_manual_hold(self):
        ms = lambda hh, mm: int(datetime(2026, 10, 13, hh, mm, tzinfo=TZ).timestamp() * 1000)
        self.c.history["S_Roll"] = [(ms(20, 10), 0), (ms(20, 30), 0), (ms(20, 31), 60), (ms(20, 59), 60)]
        self.c.items["S_Roll"]["state"] = "60"
        self.add()
        self.run_at(21, 0, cfg=tss.raw(manual_hold_min=120))
        r = self.one()
        self.assertEqual((r["code"], r["data"]["manual_at"], r["data"]["hold_min"]),
                         ("skipped_manual_hold", "20:31", 120))

    def test_history_unreadable(self):
        self.c.history_fail = True
        self.add()
        self.run_at(21, 0, cfg=tss.raw(manual_hold_min=120))
        self.assertEqual(self.one()["code"], "history_unreadable")

    def test_door_without_contact_gap(self):
        self.add(shutter_id="hinten")
        self.run_at(21, 0)
        r = self.one()
        self.assertEqual((r["code"], r["data"]["limit"], r["data"]["limited_to"], r["data"]["limited_from"]),
                         ("sent", "limited_door_gap", 80, 100))

    def test_already_set_late_and_superseded(self):
        self.c.items["S_Roll"]["state"] = "50"                          # Zwischenposition (Endlagen gehen immer raus)
        self.add(action="position", position=50)
        self.run_at(21, 0)
        self.add(shutter_id="terrasse", time="20:00")
        self.run_at(21, 1)
        self.add(shutter_id="hinten", time="20:55", action="up")
        self.add(shutter_id="hinten", time="20:58")
        self.c.items["H_Roll"]["state"] = "80"
        self.run_at(21, 2)
        self.assertEqual(self.rows(), [("schlafen", "already_set"), ("terrasse", "skipped_late"),
                                       ("hinten", "superseded"), ("hinten", "already_set")])
        late = schedule_log.results(schedule_log.SHUTTER, target_id="terrasse")[0]
        self.assertEqual((late["data"]["late_min"], late["data"]["max_min"]), (61, 15))

    def test_duty_cycle_once_per_entry_and_day(self):
        self.add()
        self.c.items["CCU_DC"]["state"] = "85"
        for minute in (0, 1, 2):
            self.run_at(21, minute)
        r = self.one()
        self.assertEqual((r["code"], r["data"]["duty_cycle"]), ("skipped_duty_cycle", 85.0))
        self.c.items["CCU_DC"]["state"] = "40"
        self.run_at(21, 5)
        self.assertEqual(self.rows(), [("schlafen", "skipped_duty_cycle"), ("schlafen", "sent")])

    def test_schedules_off_once_per_entry_and_day(self):
        e = self.add()
        off = tss.raw(schedules_enabled=False)
        for minute in (0, 1, 5, 14):
            res = self.run_at(21, minute, cfg=off)
            self.assertFalse(res.active)
        self.assertEqual(self.c.calls, [])                              # weiterhin kein openHAB-Request
        self.assertEqual(self.rows(), [("schlafen", "skipped_schedules_off")])
        self.assertIsNone(ss.get_schedule(e["id"])["last_run"])         # Eintrag bleibt unberührt
        self.run_at(20, 59, day=14, cfg=off)                            # noch nicht fällig
        self.run_at(21, 0, day=14, cfg=off)                             # nächster Tag: wieder einmal
        self.run_at(21, 1, day=14, cfg=off)
        self.assertEqual(len(self.rows()), 2)

    def test_schedules_off_does_not_log_outside_the_catch_up_window(self):
        self.add()
        self.run_at(23, 0, cfg=tss.raw(schedules_enabled=False))
        self.assertEqual(self.rows(), [])

    def test_automation_openhab_once_per_entry_and_day(self):
        e = self.add(shutter_id="wohnen")
        for minute in (0, 1, 2):
            self.run_at(21, minute)
        self.assertEqual(self.c.calls, [])
        self.assertEqual(self.rows(), [("wohnen", "skipped_automation_openhab")])
        self.assertIsNone(ss.get_schedule(e["id"])["last_run"])

    def test_openhab_down_and_dry_run(self):
        self.add()
        self.c.down = True
        self.run_at(21, 0)
        self.run_at(21, 1)
        self.assertEqual(self.rows(), [("schlafen", "openhab_unreachable")])
        self.assertEqual([d["id"] for d in decisions.offline_devices()], ["openhab"])
        self.c.down = False
        self.run_at(21, 2, dry=True)
        self.assertEqual(self.rows()[-1], ("schlafen", "dry_run"))
        self.assertEqual(decisions.offline_devices(), [])
        self.assertEqual([e["event"] for e in decisions.health_events()], ["wieder_da", "offline_ab"])

    def test_send_error_retry_then_failed(self):
        self.add()
        self.c.send_fail = {"S_Roll"}
        self.run_at(21, 0)
        self.run_at(21, 1)
        self.assertEqual(self.rows(), [("schlafen", "openhab_retry"), ("schlafen", "openhab_error")])

    def test_readback_mismatch_after_schedule_ride(self):
        self.add()
        self.run_at(21, 0)
        self.c.items["S_Roll"]["state"] = "40"                          # Rollladen blieb hängen
        self.run_at(21, 10)
        self.assertEqual(self.rows(), [("schlafen", "sent"), ("schlafen", "readback_mismatch")])
        r = schedule_log.results(schedule_log.SHUTTER)[0]
        self.assertEqual((r["data"]["expected"], r["data"]["reported"], r["schedule_id"]), (100, 40, None))

    def test_log_failure_never_blocks_a_ride(self):
        self.add()
        orig = schedule_log._conn
        schedule_log._conn = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full"))
        try:
            self.run_at(21, 0)
        finally:
            schedule_log._conn = orig
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_prune_keeps_14_days(self):
        now = datetime(2026, 10, 14, 4, 0)
        for days in (1, 13, 15):
            schedule_log.record(schedule_log.SHUTTER, 1, "schlafen", "sent", (now - timedelta(days=days)).replace(tzinfo=TZ))
        db.init_cascade_settings_table()
        self.assertEqual(db.prune(now)["schedule_result"], 1)


class HeatingResultTest(ths.DbCase):
    def rows(self):
        return [(r["target_id"], r["code"]) for r in reversed(schedule_log.results(schedule_log.HEATING))]

    def test_sent_then_already_set(self):
        e = self.add(days=["di", "mi"])
        c = ths.SchedClient()
        self.run_at(ths.at(ths.TUE, "07:00"), c)
        self.run_at(ths.at(ths.TUE + timedelta(days=1), "07:00"), c)
        self.assertEqual(self.rows(), [("wohnen", "sent"), ("wohnen", "already_set")])
        r = schedule_log.results(schedule_log.HEATING)[1]
        self.assertEqual((r["schedule_id"], r["data"]["setpoint"], r["data"]["previous"]), (e["id"], 21.0, 17.5))

    def test_late_and_superseded(self):
        self.add(days=["di"])
        c = ths.SchedClient()
        self.run_at(ths.at(ths.TUE, "07:16"), c)
        self.add(sp=18.0, time="08:00", days=["di"])
        self.add(sp=20.0, time="08:05", days=["di"])
        self.run_at(ths.at(ths.TUE, "08:06"), c)
        self.assertEqual(self.rows(), [("wohnen", "skipped_late"), ("wohnen", "superseded"), ("wohnen", "sent")])
        self.assertEqual(schedule_log.results(schedule_log.HEATING)[2]["data"]["late_min"], 16)

    def test_schedules_off_once_per_entry_and_day(self):
        self.add(days=["di"])
        c = ths.SchedClient()
        off = ths.raw(schedules=False)
        for hhmm in ("06:59", "07:00", "07:01", "07:10", "09:00"):
            self.assertFalse(self.run_at(ths.at(ths.TUE, hhmm), c, r=off).active)
        self.assertEqual(c.calls, [])
        self.assertEqual(self.rows(), [("wohnen", "skipped_schedules_off")])

    def test_openhab_down_then_error_codes(self):
        self.add(days=["di"])
        c = ths.SchedClient(dead=True)
        self.run_at(ths.at(ths.TUE, "07:00"), c)
        self.run_at(ths.at(ths.TUE, "07:01"), c)
        self.assertEqual(self.rows(), [("wohnen", "openhab_unreachable")])
        self.assertEqual([d["id"] for d in decisions.offline_devices()], ["openhab"])

    def test_readback_mismatch(self):
        self.add(days=["di"])
        c = ths.SchedClient()
        c.echo = False
        self.run_at(ths.at(ths.TUE, "07:00"), c)
        c.sp["W_Set"] = 19.0                                            # weder gesendeter noch alter Wert
        self.run_at(ths.at(ths.TUE, "07:05"), c)
        self.assertEqual(self.rows(), [("wohnen", "sent"), ("wohnen", "readback_mismatch")])
        r = schedule_log.results(schedule_log.HEATING)[0]
        self.assertEqual((r["data"]["expected"], r["data"]["reported"]), (21.0, 19.0))

    def test_all_codes_known(self):
        for code in ("sent", "already_set", "dry_run", "superseded", "skipped_late", "skipped_no_sun",
                     "skipped_schedules_off", "skipped_automation_openhab", "skipped_duty_cycle",
                     "skipped_door_open", "waiting_door", "door_wait_expired", "skipped_frost", "skipped_manual_hold",
                     "history_unreadable", "deferred_moving", "deferred_time_budget", "openhab_error",
                     "openhab_retry", "openhab_unreachable", "readback_mismatch", "readback_retry"):
            self.assertTrue(reasons.is_known(code), code)

    def test_source_uses_only_known_codes(self):
        """Jeder im Code vergebene Grund-Code steht in reasons.py (Tippfehler fallen hier auf)."""
        root = Path(__file__).resolve().parent.parent
        pat = re.compile(r'(?:note\w*\(\s*[\w{}*., "\[\]:]*?|code\s*=\s*|code,\s*data\s*=\s*|return\s+)"([a-z][a-z_]+)"')
        found = set()
        for name in ("heating_schedule.py", "shutter_schedule.py", "shutters.py", "heater_phases.py",
                     "zendure_control.py", "cascade_service.py", "controller.py", "decisions.py"):
            for m in pat.finditer((root / name).read_text(encoding="utf-8")):
                found.add(m.group(1))
        candidates = {c for c in found if "_" in c}
        ignore = {"turn_on", "turn_off", "keep_on", "keep_off", "skip_min_on", "skip_min_off", "phase_sum",
                  "shelly_gen1", "shelly_gen2", "auto_reenabled", "offline_ab", "wieder_da", "pv_w", "main_meter",
                  "read_only", "dry_run", "heater_meter", "storage_temp", "shutter_id"}
        unknown = {c for c in candidates - ignore if not reasons.is_known(c)}
        self.assertEqual(unknown, set())
        self.assertGreater(len(candidates & set(reasons.REASONS)), 25)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
