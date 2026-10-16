"""Zendure-Sicherheit: Fail-safe → Standby, Laden nur aus echter Einspeisung, Watchdog/Pi-Start.
Kein Netz: Gerät und Zähler sind Fakes, eigene Test-DB."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import main
import zendure_watchdog as wd
from clients.zendure_client import ZendureStatus
from config import ZendureConfig
from tests import test_cascade_profiles as tcp
from tests.test_zendure_control import mk
from zendure_control import ZendureControl, force_standby, plan

ZC = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100, max_charge_w=2400, max_discharge_w=2400)


class FakeDevice:
    """Zendure mit RAM-Sollwerten wie das echte Gerät (acMode/inputLimit/outputLimit)."""

    def __init__(self, ac=2, inp=0, out=0, soc=60, fail_writes=0, readable=True):
        self.props = {"acMode": ac, "inputLimit": inp, "outputLimit": out, "electricLevel": soc}
        self.writes = []
        self.fail_writes = fail_writes
        self.readable = readable

    def get_status(self):
        if not self.readable:
            return None
        p = self.props
        return ZendureStatus(soc=p["electricLevel"], charge_w=p["inputLimit"] if p["acMode"] == 1 else 0,
                             discharge_w=p["outputLimit"] if p["acMode"] == 2 else 0,
                             home_out_w=p["outputLimit"] if p["acMode"] == 2 else 0, props=dict(p))

    def set_mode(self, ac_mode, input_limit=None, output_limit=None):
        self.writes.append((ac_mode, input_limit, output_limit))
        if self.fail_writes:
            self.fail_writes -= 1
            return False
        if ac_mode is not None:
            self.props["acMode"] = ac_mode
        if input_limit is not None:
            self.props["inputLimit"] = input_limit
        if output_limit is not None:
            self.props["outputLimit"] = output_limit
        return True


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def cfg(self, dry_run=False):
        return SimpleNamespace(zendure=ZC, runtime=SimpleNamespace(dry_run=dry_run), zendure_active=True)


class FailsafePlanTest(unittest.TestCase):
    """Punkt 3: Hauptzähler/WR nicht lesbar → Standby, nicht letzten Wert halten."""

    def test_failsafe_stops_charging_immediately(self):
        st = ZendureStatus(soc=50, charge_w=1000)
        p = plan(ZC, st, **mk(avail_w=1500, ctl_mode="charge", ctl_limit_w=1000, seconds_since_change=5),
                 failsafe="Hauptzähler")
        self.assertEqual((p.mode, p.limit_w, p.write, p.code), ("idle", 0, True, "failsafe_global"))
        self.assertIn("Hauptzähler nicht lesbar", p.reason)

    def test_failsafe_stops_discharging_and_ignores_override(self):
        st = ZendureStatus(soc=50, home_out_w=400)
        for override in (None, "on"):
            p = plan(ZC, st, **mk(import_w=300, ctl_mode="discharge", ctl_limit_w=400, override=override),
                     failsafe="Wechselrichter")
            self.assertEqual((p.mode, p.limit_w, p.write), ("idle", 0, True), override)

    def test_failsafe_while_idle_no_extra_write(self):
        p = plan(ZC, ZendureStatus(soc=50), **mk(), failsafe="Hauptzähler")
        self.assertEqual((p.mode, p.write), ("idle", False))


class ChargeCapTest(unittest.TestCase):
    """Punkt 5: Ladesollwert jede Minute auf die echte Einspeisung begrenzt."""

    def test_cloud_reduces_charge_to_actual_surplus(self):
        # lädt 1500 W, Wolke: Zähler zeigt 600 W Bezug → nur noch 900 W Überschuss
        st = ZendureStatus(soc=50, charge_w=1500)
        p = plan(ZC, st, **mk(avail_w=900, ctl_mode="charge", ctl_limit_w=1500), export_cap_w=1500 - 600)
        self.assertEqual((p.mode, p.limit_w, p.write), ("charge", 900, True))

    def test_cloud_to_import_goes_to_zero(self):
        st = ZendureStatus(soc=50, charge_w=800)
        p = plan(ZC, st, **mk(avail_w=150, ctl_mode="charge", ctl_limit_w=800), export_cap_w=800 - 1200)
        self.assertEqual((p.limit_w, p.write), (0, True))

    def test_small_reduction_is_written(self):
        st = ZendureStatus(soc=50, charge_w=1000)
        p = plan(ZC, st, **mk(avail_w=970, ctl_mode="charge", ctl_limit_w=1000), export_cap_w=970)
        self.assertEqual((p.limit_w, p.write), (970, True))
        p = plan(ZC, st, **mk(avail_w=1030, ctl_mode="charge", ctl_limit_w=1000), export_cap_w=1030)
        self.assertFalse(p.write)                             # Erhöhung erst ab > 50 W

    def test_heater_still_running_battery_waits(self):
        # Batterie steht vor dem Heizstab: will 1500 W, aber eingespeist werden nur 200 W
        p = plan(ZC, ZendureStatus(soc=50), **mk(avail_w=1500), export_cap_w=200)
        self.assertEqual((p.mode, p.limit_w, p.want_w), ("charge", 200, 1500))
        self.assertIn("wartet auf 1500 W", p.reason)

    def test_override_charge_not_capped(self):
        p = plan(ZC, ZendureStatus(soc=50), **mk(override="on"), export_cap_w=0)
        self.assertEqual(p.limit_w, 2400)


class CascadeFailsafeTest(unittest.TestCase):
    """Kaskade: Hauptzähler bzw. WR nicht lesbar → Batterie Standby; Einspeise-Deckel wirkt."""
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service

    def run_svc(self, main_w, pv=6000.0, solax_feed=3000, zd=None):
        self.raw["runtime"]["dry_run"] = False
        dev = FakeDevice(ac=1, inp=1000, soc=50)
        svc = self.service(False, 45.0, main_w, pv=pv, zd=zd)
        svc.zendure_client = dev
        svc.zendure_ctl.client = dev
        svc.main_meter = SimpleNamespace(get_total_power_w=lambda: main_w)
        svc.solax.get_realtime_data = lambda: SimpleNamespace(feed_in_w=solax_feed)
        db.set_zendure_ctl(mode="charge", limit_w=1000,
                           changed_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
        svc.run_cascade()
        return dev

    def test_main_meter_unreadable_standby(self):
        dev = self.run_svc(None)
        self.assertEqual(dev.writes[-1], (None, 0, 0))
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")
        self.assertIn("Hauptzähler nicht lesbar", db.get_zendure_ctl()["decision"])

    def test_inverter_unreadable_standby(self):
        dev = self.run_svc(-3000.0, pv=None)
        self.assertEqual(dev.writes[-1], (None, 0, 0))
        self.assertIn("Wechselrichter nicht lesbar", db.get_zendure_ctl()["decision"])

    def test_charge_capped_to_meter(self):
        # Batterie lädt 1000 W, Zähler: 300 W Einspeisung → max 1300 W, obwohl 2400 möglich wären
        zd = ZendureStatus(soc=50, charge_w=1000, props={"acMode": 1, "inputLimit": 1000, "outputLimit": 0})
        dev = self.run_svc(-300.0, zd=zd)
        self.assertEqual(dev.writes[-1], (1, 1300, 0))


class WatchdogTest(DbCase):
    """Punkt 2: Regelung läuft nicht mehr (Cron hängt) → Standby."""

    def run_control(self, dev, when):
        ctl = ZendureControl(self.cfg(), dev)
        with mock.patch("zendure_control.datetime") as dt:
            dt.now.return_value = when
            dt.fromisoformat = datetime.fromisoformat
            ctl.run(dev.get_status(), avail_w=1500, import_w=-1500, wallbox_charging=False, override=None,
                    export_cap_w=1500)

    def test_cron_stops_6_min_then_standby(self):
        dev = FakeDevice()
        t0 = datetime.now() - timedelta(minutes=6)
        self.run_control(dev, t0)                                   # letzter Lauf vor 6 min: lädt
        self.assertEqual(dev.props["inputLimit"], 1500)
        self.assertEqual(db.get_zendure_ctl()["mode"], "charge")
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            self.assertEqual(wd.check(self.cfg(), dev), "standby")
        self.assertEqual((dev.props["inputLimit"], dev.props["outputLimit"]), (0, 0))
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")
        self.assertIn("Watchdog: Regelung läuft nicht (seit 6 min)", logs.output[0])
        # Herzschlag bleibt alt → nächste Minute: steht schon, kein weiterer Schreibbefehl
        n = len(dev.writes)
        with self.assertLogs("pv-controller.zendure-watchdog", level="INFO"):
            self.assertEqual(wd.check(self.cfg(), dev), "already_standby")
        self.assertEqual(len(dev.writes), n)

    def test_recent_control_ok(self):
        dev = FakeDevice()
        self.run_control(dev, datetime.now() - timedelta(minutes=4))
        n = len(dev.writes)
        self.assertEqual(wd.check(self.cfg(), dev), "ok")
        self.assertEqual(len(dev.writes), n)

    def test_never_ran_and_device_unreadable_still_writes(self):
        dev = FakeDevice(out=200, readable=False)                  # App-Wert, Lesen klappt nicht
        with self.assertLogs("zendure_control", level="WARNING"):
            self.assertEqual(wd.check(self.cfg(), dev), "standby")
        self.assertEqual(dev.writes, [(None, 0, 0)])

    def test_dry_run_only_logs(self):
        dev = FakeDevice(inp=800, ac=1)
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            wd.check(self.cfg(dry_run=True), dev, now_age=600)
        self.assertEqual(dev.writes, [])
        self.assertIn("[Dry-Run]", logs.output[0])

    def test_boot_retries_until_device_answers(self):
        dev = FakeDevice(ac=1, inp=800, fail_writes=2)
        sleeps = []
        with self.assertLogs("zendure_control", level="WARNING"):
            self.assertTrue(wd.boot(self.cfg(), dev, sleep=sleeps.append))
        self.assertEqual(len(dev.writes), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(dev.props["inputLimit"], 0)

    def test_standby_keeps_heartbeat(self):
        dev = FakeDevice(ac=1, inp=800)
        self.run_control(dev, datetime.now() - timedelta(minutes=10))
        before = db.get_zendure_ctl()["decided_at"]
        with self.assertLogs("zendure_control", level="WARNING"):
            force_standby(dev, "Test")
        self.assertEqual(db.get_zendure_ctl()["decided_at"], before)


class MainCrashTest(DbCase):
    def test_cascade_exception_puts_battery_in_standby(self):
        dev = FakeDevice(ac=1, inp=1200)
        cfg = SimpleNamespace(zendure=SimpleNamespace(ip="x", sn="", control_enabled=True), zendure_active=True,
                              runtime=SimpleNamespace(dry_run=False))
        with mock.patch("clients.zendure_client.ZendureClient", return_value=dev), \
                self.assertLogs("zendure_control", level="WARNING"):
            main._battery_standby(cfg, "Kaskade abgebrochen")
        self.assertEqual(dev.writes, [(None, 0, 0)])

    def test_without_control_nothing(self):
        cfg = SimpleNamespace(zendure=SimpleNamespace(control_enabled=False), zendure_active=True,
                              runtime=SimpleNamespace(dry_run=False))
        with mock.patch("clients.zendure_client.ZendureClient") as cl:
            main._battery_standby(cfg, "x")
        cl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
