"""Batterie: nie entladen, solange die Wallbox lädt (Kaskade, Wettlauf, Wächter, Locks) und
Mindest-SoC (Hysterese, Gerät schreiben + zurücklesen). Kein Netz: alle Geräte sind Fakes."""
import fcntl
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import web
import zendure_control as zcm
import zendure_watchdog as wd
from clients.goe_client import car_charging, charging_now
from clients.zendure_client import ZendureStatus
from config import ZendureConfig
from tests import test_cascade_profiles as tcp
from tests.test_zendure_control import mk
from tests.test_zendure_safety import DbCase, FakeDevice
from zendure_control import ZendureControl, apply_min_soc, plan

ZC = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100, max_charge_w=2400, max_discharge_w=2400)
IDLE = {"car": 1, "nrg": [0] * 16}
DONE = {"car": 4, "nrg": [0] * 16}


def car(watts, state=2):
    nrg = [0] * 16
    nrg[11] = watts
    return {"car": state, "nrg": nrg}


class Goe:
    """go-e-Fake: liefert der Reihe nach die Status aus ``seq`` (letzter bleibt); Exception wird geworfen."""

    def __init__(self, *seq):
        self.seq = list(seq)
        self.calls = 0

    def get_status(self):
        self.calls += 1
        v = self.seq[min(self.calls - 1, len(self.seq) - 1)]
        if isinstance(v, Exception):
            raise v
        return v


class Device(FakeDevice):
    """FakeDevice mit write_props (minSoc) und get_status(fresh=…)."""

    def __init__(self, *a, min_soc_scale=1, **kw):
        super().__init__(*a, **kw)
        self.props["minSoc"] = 100
        self.scale = min_soc_scale          # 1 = Gerät übernimmt den Wert, 0 = ignoriert ihn

    def get_status(self, fresh=False):
        return super().get_status()

    def write_props(self, props):
        self.writes.append(("props", dict(props)))
        if self.fail_writes:
            self.fail_writes -= 1
            return False
        if "minSoc" in props and self.scale:
            self.props["minSoc"] = props["minSoc"]
        return True


class DetectionTest(unittest.TestCase):
    """A: lädt = car=2 ODER > 100 W; nicht lesbar = lädt."""

    def test_rules(self):
        self.assertTrue(car_charging(car(11000)))
        self.assertTrue(car_charging(car(50)))                 # Rampe: car=2, noch < 100 W
        self.assertTrue(car_charging(car(1500, state=3)))      # > 100 W auch ohne car=2
        self.assertFalse(car_charging(IDLE))
        self.assertFalse(car_charging(DONE))
        self.assertTrue(car_charging(None))
        self.assertTrue(car_charging({}))

    def test_charging_now(self):
        self.assertFalse(charging_now(None))                   # keine Wallbox-URL
        self.assertTrue(charging_now(Goe(OSError("timeout"))))
        self.assertFalse(charging_now(Goe(IDLE)))


class CascadeWallboxTest(unittest.TestCase):
    """Ganzer Kaskadenlauf: Batterie entlädt, Auto startet → nie entladen."""
    setUp_base = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service

    def setUp(self):
        self.setUp_base()
        self.raw["runtime"]["dry_run"] = False
        self.raw["wallbox"]["url"] = "http://goe"              # wallbox.enabled bleibt False (B)

    def run_once(self, goe, dev, main_w=800.0):
        svc = self.service(False, 60.0, main_w, pv=0.0)        # Nacht: 800 W Bezug, Grundlast 800 W
        self.assertIsNone(svc.goe)                             # Wallbox-Steuerung aus …
        svc.goe_battery = goe                                  # … Batterie-Sperre liest trotzdem
        svc.zendure_client = dev
        svc.zendure_ctl.client = dev
        svc.run_cascade()
        return svc

    def last_write(self, dev):
        return [w for w in dev.writes if w[0] != "props"][-1]

    def start_discharging(self, dev):
        db.set_zendure_ctl(mode="discharge", limit_w=800,
                           changed_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        dev.props.update(acMode=2, outputLimit=800)

    def test_car_charging_with_import_no_discharge(self):
        dev = Device(soc=60)
        self.start_discharging(dev)
        self.run_once(Goe(car(11000)), dev, main_w=11800.0)
        self.assertEqual(self.last_write(dev), (None, 0, 0))
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")
        self.assertIn("Wallbox lädt", db.get_zendure_ctl()["decision"])

    def test_goe_unreadable_no_discharge(self):
        dev = Device(soc=60)
        self.start_discharging(dev)
        with self.assertLogs("cascade_service", level="WARNING"):
            self.run_once(Goe(OSError("timeout")), dev)
        self.assertEqual(self.last_write(dev), (None, 0, 0))

    def test_ramp_50w_no_discharge(self):
        dev = Device(soc=60)
        self.start_discharging(dev)
        self.run_once(Goe(car(50)), dev)
        self.assertEqual(self.last_write(dev), (None, 0, 0))

    def test_car_finished_discharge_allowed_again(self):
        dev = Device(soc=60)
        self.run_once(Goe(DONE), dev)
        self.assertEqual(self.last_write(dev), (2, 0, 760))

    def test_sequence_discharge_car_starts_then_finishes(self):
        dev = Device(soc=60)
        self.run_once(Goe(IDLE), dev)
        self.assertEqual(self.last_write(dev), (2, 0, 760))     # entlädt die Grundlast (− 40 W Puffer)
        self.run_once(Goe(car(11000)), dev, main_w=11000.0)      # Auto startet (Akku liefert noch 800)
        self.assertEqual(self.last_write(dev), (None, 0, 0))
        self.run_once(Goe(DONE), dev)                            # Auto fertig, 800 W Bezug
        self.assertEqual(self.last_write(dev), (2, 0, 760))

    def test_race_car_starts_between_plan_and_write(self):
        dev = Device(soc=60)
        goe = Goe(IDLE, car(11000))          # 1. Lesen (Planung): steht; 2. Lesen (vor dem Schreiben): lädt
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            self.run_once(goe, dev)
        self.assertEqual(goe.calls, 2)
        self.assertNotIn(2, [w[0] for w in dev.writes])          # nie acMode 2 (Entladen) geschrieben
        self.assertEqual(self.last_write(dev), (None, 0, 0))
        self.assertIn("Entladen verworfen – Wallbox lädt (vor dem Schreiben geprüft)", "\n".join(logs.output))

    def test_charging_from_surplus_still_allowed(self):
        dev = Device(soc=60)
        svc = self.service(False, 60.0, -2000.0, pv=4000.0)
        svc.goe_battery = Goe(car(11000))
        svc.zendure_client = dev
        svc.zendure_ctl.client = dev
        svc.run_cascade()
        self.assertEqual(self.last_write(dev)[0], 1)              # acMode 1 = Laden


class UnreadableBatteryTest(DbCase):
    """D: Zendure nicht lesbar + Wallbox lädt → sofort Standby (nicht erst nach 3 Fehlern)."""

    def test_standby_on_first_failure(self):
        dev = Device()
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        ctl = ZendureControl(self.cfg(), dev)
        with self.assertLogs("zendure_control", level="WARNING"):
            p = ctl.run(None, avail_w=0, import_w=3000, wallbox_charging=True, override=None)
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertIn("Standby geschrieben", p.reason)

    def test_without_wallbox_waits_for_third_failure(self):
        dev = Device()
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        ctl = ZendureControl(self.cfg(), dev)
        ctl.run(None, avail_w=0, import_w=300, wallbox_charging=False, override=None)
        self.assertEqual(dev.writes, [])


class GuardTest(DbCase):
    """Wallbox-Wächter (3-s-Takt) im Watchdog."""

    def test_discharging_car_starts_standby(self):
        dev = Device(ac=2, out=600)
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(car(7000))), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")
        self.assertIn("Wächter: Wallbox lädt – Entladen sofort gestoppt", logs.output[0])

    def test_goe_unreadable_standby(self):
        dev = Device(ac=2, out=600)
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        with self.assertLogs("zendure_control", level="WARNING"):
            self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(OSError("x"))), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])

    def test_charging_battery_untouched(self):
        dev = Device(ac=1, inp=1500)
        db.set_zendure_ctl(mode="charge", limit_w=1500)
        goe = Goe(car(7000))
        self.assertEqual(wd.guard_tick(self.cfg(), dev, goe), "idle")
        self.assertEqual((dev.writes, goe.calls), ([], 0))

    def test_zendure_unreadable_still_standby(self):
        dev = Device(ac=2, out=600, readable=False)
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        with self.assertLogs("zendure_control", level="WARNING"):
            self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(car(7000))), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])

    def test_device_discharging_after_app_change(self):
        dev = Device(ac=2, out=200)                                # Regelzustand idle, Gerät entlädt
        with self.assertLogs("zendure_control", level="WARNING"):
            self.assertEqual(wd.guard_tick(self.cfg(), dev, Goe(car(7000)), device_discharging=True), "standby")

    def test_loop_reaction_within_one_tick(self):
        dev = Device(ac=2, out=600)
        db.set_zendure_ctl(mode="discharge", limit_w=600)
        t = [0.0]
        goe = Goe(IDLE, IDLE, IDLE, car(7000))                     # Auto startet zwischen Takt 3 und 4
        with self.assertLogs("zendure_control", level="WARNING"):
            out = wd.guard_loop(self.cfg(), dev, goe, end_at=58.0, tick_s=3.0,
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertEqual(out[:4], ["ok", "ok", "ok", "standby"])
        self.assertEqual(out[4:], ["idle"] * (len(out) - 4))       # danach steht sie, kein Doppelschreiben
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertEqual(len(out), 20)                              # 0, 3, … 57 s

    def test_loop_stops_on_deploy(self):
        out = wd.guard_loop(self.cfg(), Device(), Goe(IDLE), end_at=58.0, stop=lambda: "Deploy läuft",
                            sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(out, ["stop"])

    def test_deploy_running_detects_exclusive_lock(self):
        path = Path(self.tmp.name) / "code.lock"
        self.assertFalse(wd.deploy_running(str(path)))
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_SH)                          # Cron-Lauf hält geteilt: kein Deploy
            self.assertFalse(wd.deploy_running(str(path)))
            fcntl.flock(f, fcntl.LOCK_EX)                          # deploy.sh hält exklusiv
            other = open(path, "a+")
            try:
                self.assertTrue(wd.deploy_running(str(path)))
            finally:
                other.close()

    def test_code_head_change_stops(self):
        root = Path(self.tmp.name) / "repo"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("aaa\n")
        self.assertEqual(wd.code_head(root), "aaa")
        (root / ".git" / "HEAD").write_text("bbb\n")              # deploy.sh: git checkout --detach
        self.assertEqual(wd.code_head(root), "bbb")


class WriteLockTest(DbCase):
    """Gemeinsames Schreib-Lock: ohne Lock wird nie entladen, nur Standby."""

    def setUp(self):
        super().setUp()
        self.p = mock.patch.object(zcm, "WRITE_LOCK_PATH", str(Path(self.tmp.name) / "w.lock"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_lock_is_exclusive(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with zcm.write_guard() as got:
                self.assertTrue(got)
                held.set()
                release.wait(2)
        th = threading.Thread(target=holder)
        th.start()
        held.wait(2)
        with self.assertLogs("zendure_control", level="WARNING"):
            with zcm.write_guard(wait_s=0.3) as got:
                self.assertFalse(got)
        release.set()
        th.join()
        with zcm.write_guard(wait_s=0.3) as got:
            self.assertTrue(got)

    def test_no_lock_no_discharge(self):
        dev = Device()
        ctl = ZendureControl(self.cfg(), dev)
        with mock.patch.object(zcm, "write_guard") as g, self.assertLogs("zendure_control", level="WARNING"):
            g.return_value.__enter__.return_value = False
            ctl.run(dev.get_status(), avail_w=0, import_w=600, wallbox_charging=False, override=None,
                    base_load_w=600, wallbox_check=lambda: False)
        self.assertEqual(dev.writes, [(None, 0, 0)])


class MinSocTest(unittest.TestCase):
    """Mindest-SoC: Stopp an der Grenze, Freigabe ab Grenze + 2 %, SoC unbekannt → kein Entladen."""

    def p(self, soc, ctl_mode="idle"):
        st = ZendureStatus(soc=soc, home_out_w=300 if ctl_mode == "discharge" else 0)
        return plan(ZC, st, **mk(import_w=400, ctl_mode=ctl_mode, ctl_limit_w=300 if ctl_mode == "discharge" else 0))

    def test_stop_at_limit(self):
        self.assertEqual(self.p(11, "discharge").mode, "discharge")
        p = self.p(10, "discharge")
        self.assertEqual((p.mode, p.code, p.write), ("idle", "battery_empty", True))
        self.assertIn("Mindest-SoC 10 %", p.reason)

    def test_hysteresis(self):
        self.assertEqual(self.p(11).mode, "idle")                 # nach Stopp: 11 % reicht nicht
        self.assertIn("Freigabe ab 12 %", self.p(11).reason)
        self.assertEqual(self.p(12).mode, "discharge")
        self.assertEqual(self.p(11, "discharge").mode, "discharge")   # läuft weiter bis zur Grenze

    def test_soc_unknown(self):
        p = self.p(None, "discharge")
        self.assertEqual((p.mode, p.limit_w), ("idle", 0))
        self.assertIn("SoC unbekannt", p.reason)

    def test_other_limit(self):
        zc = ZendureConfig(enabled=True, ip="x", min_soc=20)
        st = ZendureStatus(soc=21)
        self.assertEqual(plan(zc, st, **mk(import_w=400)).mode, "idle")
        st.soc = 22
        self.assertEqual(plan(zc, st, **mk(import_w=400)).mode, "discharge")


class ApplyMinSocTest(DbCase):
    def setUp(self):
        super().setUp()
        self.p = mock.patch.multiple(zcm, READBACK_WAIT_S=0, WRITE_LOCK_PATH=str(Path(self.tmp.name) / "w.lock"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_written_to_flash_and_read_back(self):
        dev = Device()
        with self.assertLogs("zendure_control", level="INFO"):
            res = apply_min_soc(dev, 15)
        self.assertEqual(dev.writes, [("props", {"smartMode": 0, "minSoc": 150})])
        self.assertEqual(res, {"ok": True, "written": 150, "read": 150, "error": None})

    def test_readback_mismatch_reported(self):
        dev = Device(min_soc_scale=0)                              # Gerät übernimmt den Wert nicht
        with self.assertLogs("zendure_control", level="ERROR"):
            res = apply_min_soc(dev, 20)
        self.assertFalse(res["ok"])
        self.assertIn("minSoc=100 statt 200", res["error"])

    def test_write_failed(self):
        res = apply_min_soc(Device(fail_writes=1), 5)
        self.assertEqual((res["ok"], res["error"]), (False, "Schreiben fehlgeschlagen"))


class SettingsMinSocTest(unittest.TestCase):
    """Settings: Auswahl 5/10/15/20; Änderung → Gerät schreiben + zurücklesen, sonst nichts."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        raw["runtime"]["dry_run"] = False
        raw["zendure"] = {"enabled": True, "ip": "192.0.2.1", "sn": "SN", "min_soc": 10,
                          "block_discharge_while_wallbox_charging": True}
        self.cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def put(self, **z):
        with mock.patch("zendure_control.apply_min_soc",
                        return_value={"ok": True, "written": 150, "read": 150, "error": None}) as ap:
            res = web.api_config_put(web.ConfigUpdate(zendure=z))
        return res, ap

    def test_change_writes_device(self):
        res, ap = self.put(min_soc=15)
        ap.assert_called_once()
        self.assertEqual(ap.call_args[0][1], 15)
        self.assertEqual(res["zendure_min_soc"]["read"], 150)
        saved = json.loads(self.cfg_path.read_text())["zendure"]
        self.assertEqual(saved["min_soc"], 15)
        self.assertNotIn("block_discharge_while_wallbox_charging", saved)   # alter Schalter entfernt

    def test_unchanged_no_device_write(self):
        res, ap = self.put(min_soc=10, name="Akku")
        ap.assert_not_called()
        self.assertNotIn("zendure_min_soc", res)

    def test_only_choices_allowed(self):
        from fastapi import HTTPException
        for bad in (0, 12, 25, 50):
            with self.assertRaises(HTTPException):
                self.put(min_soc=bad)

    def test_discharge_buffer_setting(self):
        from fastapi import HTTPException
        res, ap = self.put(discharge_target_grid_w=60)
        ap.assert_not_called()                                       # kein Geräte-Schreiben
        self.assertEqual(json.loads(self.cfg_path.read_text())["zendure"]["discharge_target_grid_w"], 60)
        for bad in (-10, 301, True):
            with self.assertRaises(HTTPException):
                self.put(discharge_target_grid_w=bad)

    def test_old_switch_ignored(self):
        res, ap = self.put(block_discharge_while_wallbox_charging=False)
        self.assertNotIn("block_discharge_while_wallbox_charging", json.loads(self.cfg_path.read_text())["zendure"])

    def test_dry_run_not_written(self):
        raw = json.loads(self.cfg_path.read_text())
        raw["runtime"]["dry_run"] = True
        self.cfg_path.write_text(json.dumps(raw))
        res, ap = self.put(min_soc=20)
        ap.assert_not_called()
        self.assertTrue(res["zendure_min_soc"]["dry_run"])


if __name__ == "__main__":
    unittest.main()
