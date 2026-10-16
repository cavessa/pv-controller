"""Fremdregler (App-Energieplan/HEMS) erkennen, Wächter → Standby, Mindest-SoC nachschreiben;
Batterie-Vorrang: Heizstab gibt sofort ab, solange die Batterie wartet; Überschrift. Nur Fakes."""
import sqlite3
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import db
import web
import zendure_control as zcm
import zendure_watchdog as wd
from clients.zendure_client import ZendureStatus
from tests import test_cascade_profiles as tcp
from tests.test_zendure_safety import DbCase
from tests.test_zendure_wallbox import ZC, Device, Goe, IDLE
from zendure_control import ZendureControl, foreign_deviation, foreign_status

OLD = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")


def st(ac=1, inp=0, out=0, soc=60, **extra):
    return ZendureStatus(soc=soc, props={"acMode": ac, "inputLimit": inp, "outputLimit": out, **extra})


class DeviationTest(unittest.TestCase):
    def test_discharge_against_charge(self):
        d = foreign_deviation(st(ac=2, out=337), "charge", 0)
        self.assertIn("Gerät entlädt bis 337 W", d)
        self.assertIn("Regelung: Laden 0 W", d)

    def test_input_changed_without_own_write(self):
        self.assertIn("inputLimit 2253 statt 1260", foreign_deviation(st(inp=2253), "charge", 1260))
        self.assertIn("inputLimit 482 statt 660", foreign_deviation(st(inp=482), "charge", 660))

    def test_matching_and_small_differences_are_fine(self):
        self.assertEqual(foreign_deviation(st(inp=1240), "charge", 1260), "")
        self.assertEqual(foreign_deviation(st(ac=2), "idle", 0), "")          # acMode allein zählt nicht
        self.assertEqual(foreign_deviation(st(ac=2, out=600), "discharge", 600), "")

    def test_device_stops_itself_when_full_or_empty(self):
        self.assertEqual(foreign_deviation(st(ac=2, soc=99), "charge", 420, target_soc=100), "")
        self.assertEqual(foreign_deviation(st(ac=2, soc=11), "discharge", 300, min_soc=10), "")
        self.assertNotEqual(foreign_deviation(st(ac=2, soc=50), "charge", 420, target_soc=100), "")

    def test_without_limits_no_verdict(self):
        self.assertEqual(foreign_deviation(ZendureStatus(soc=50, props={}), "charge", 1000), "")


class WatchdogForeignTest(DbCase):
    def cfg(self, dry_run=False):
        c = super().cfg(dry_run)
        c.wallbox = SimpleNamespace(url="")
        return c

    def loop(self, dev, ticks=4):
        t = [0.0]
        return wd.guard_loop(self.cfg(), dev, Goe(IDLE), end_at=3.0 * ticks, tick_s=3.0,
                             sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])

    def test_hems_discharge_two_ticks_standby(self):
        dev = Device(ac=2, out=300)                                # Regelung: Laden 1000 W, Gerät entlädt
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        with self.assertLogs("zendure_control", level="WARNING") as logs:
            out = self.loop(dev)
        self.assertEqual(out[:2], ["ok", "foreign_standby"])     # Wallbox geprüft (steht), dann Fremdregler
        self.assertEqual(dev.writes, [(None, 0, 0)])
        self.assertTrue(any("Fremdregler aktiv" in m for m in logs.output))
        f = foreign_status()
        self.assertTrue(f["active"])
        self.assertIn("Gerät entlädt bis 300 W", f["detail"])
        self.assertIn("Wächter", f["detail"])

    def test_no_check_right_after_own_write(self):
        dev = Device(ac=1, inp=2400)
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=datetime.now().isoformat(timespec="seconds"))
        self.loop(dev)
        self.assertEqual(dev.writes, [])
        self.assertFalse(foreign_status()["active"])

    def test_single_tick_is_ignored(self):
        dev = Device(ac=1, inp=2400)
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        reads = [st(inp=2400), st(inp=1000), st(inp=1000), st(inp=1000)]
        dev.get_status = lambda fresh=False: reads.pop(0) if reads else st(inp=1000)
        self.loop(dev)
        self.assertEqual(dev.writes, [])

    def test_persistent_hems_standby_rate_limited(self):
        class Hems(Device):                                        # App setzt nach jedem Befehl wieder 300 W
            def set_mode(self, *a, **kw):
                ok = super().set_mode(*a, **kw)
                self.props.update(acMode=2, outputLimit=300)
                return ok
        dev = Hems(ac=2, out=300)
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        with mock.patch.object(zcm, "FOREIGN_GRACE_S", 0.0), self.assertLogs("zendure_control", level="WARNING"):
            out = self.loop(dev, ticks=19)                         # 57 s
        self.assertEqual(out.count("foreign_standby"), 2)          # 0–30 s und danach, nicht jeden Takt
        self.assertEqual(len(dev.writes), 2)

    def test_dry_run_only_watches(self):
        dev = Device(ac=2, out=300)
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        t = [0.0]
        wd.guard_loop(self.cfg(dry_run=True), dev, Goe(IDLE), end_at=12.0, tick_s=3.0,
                      sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertEqual(dev.writes, [])

    def test_matching_device_untouched(self):
        dev = Device(ac=1, inp=1000)
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        self.assertEqual(self.loop(dev), ["idle"] * 4)
        self.assertEqual(dev.writes, [])


class MinuteRunForeignTest(DbCase):
    def test_records_foreign_and_rewrites(self):
        dev = Device(ac=1, inp=2253)
        db.set_zendure_ctl(mode="charge", limit_w=1260, written_at=OLD)
        with self.assertLogs("zendure_control", level="WARNING"):
            ZendureControl(self.cfg(), dev).run(dev.get_status(), avail_w=1260, import_w=0,
                                                wallbox_charging=False, override=None)
        f = foreign_status()
        self.assertTrue(f["active"])
        self.assertIn("inputLimit 2253 statt 1260", f["detail"])
        self.assertIsNotNone(db.get_zendure_ctl()["written_at"])

    def test_own_write_sets_written_at_and_no_foreign(self):
        dev = Device(ac=1, inp=0)
        db.set_zendure_ctl(mode="idle", limit_w=0)
        ZendureControl(self.cfg(), dev).run(dev.get_status(), avail_w=1500, import_w=0,
                                            wallbox_charging=False, override=None)
        ctl = db.get_zendure_ctl()
        self.assertEqual((ctl["mode"], ctl["limit_w"]), ("charge", 1500))
        self.assertIsNotNone(ctl["written_at"])
        self.assertFalse(foreign_status()["active"])

    def test_min_soc_reset_by_app_is_rewritten_once_per_hour(self):
        dev = Device(ac=1, inp=0)
        dev.props["minSoc"] = 50                                   # App hat 5 % gesetzt (16.10.2026 15:32)
        ctl = ZendureControl(self.cfg(), dev)
        with mock.patch.object(zcm, "READBACK_WAIT_S", 0), self.assertLogs("zendure_control", level="ERROR"):
            ctl.run(dev.get_status(), avail_w=0, import_w=0, wallbox_charging=False, override=None)
        self.assertEqual(dev.props["minSoc"], 100)
        self.assertIn("Mindest-SoC am Gerät 5 % statt 10 %", foreign_status()["detail"])
        dev.props["minSoc"] = 50
        ctl.run(dev.get_status(), avail_w=0, import_w=0, wallbox_charging=False, override=None)
        self.assertEqual(dev.props["minSoc"], 50)                  # nicht noch einmal in derselben Stunde
        self.assertEqual(sum(1 for w in dev.writes if w[0] == "props"), 1)

    def test_dry_run_no_foreign_check(self):
        dev = Device(ac=2, out=300)
        dev.props["minSoc"] = 50
        db.set_zendure_ctl(mode="charge", limit_w=1000, written_at=OLD)
        ZendureControl(self.cfg(dry_run=True), dev, dry_run=True).run(
            dev.get_status(), avail_w=1000, import_w=0, wallbox_charging=False, override=None)
        self.assertFalse(foreign_status()["active"])
        self.assertEqual(dev.writes, [])


class StatusBlockTest(DbCase):
    def test_foreign_in_status_block(self):
        db.set_zendure_ctl(foreign_at=datetime.now().isoformat(timespec="seconds"),
                           foreign_since=OLD, foreign_detail="Gerät entlädt bis 300 W – Wächter")
        cfg = SimpleNamespace(zendure=SimpleNamespace(**{**ZC.__dict__, "control_enabled": True, "name": "Batterie",
                                                         "capacity_wh": 8640}), zendure_active=True)
        r = SimpleNamespace(zendure=ZendureStatus(soc=60, props={"minSoc": 50}))
        f = web._zendure_status_block(cfg, r)["foreign"]
        self.assertTrue(f["active"])
        self.assertEqual(f["device_min_soc"], 5.0)

    def test_old_detection_inactive(self):
        old = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
        db.set_zendure_ctl(foreign_at=old, foreign_since=old, foreign_detail="x")
        self.assertFalse(foreign_status()["active"])


class BatteryWaitingHeaterTest(tcp.ServiceIntegrationTest):
    """14:06-Fall: Batterie wartet (Deckel), Heizstab läuft → fürs Abschalten zählt der jetzige Überschuss."""

    def seed(self, raw_w):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
            for i in range(1, 10):
                ts = (datetime.now() - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                conn.execute("INSERT OR REPLACE INTO heater_budget_log (ts, raw_w) VALUES (?, ?)", (ts, raw_w))

    def setUp(self):
        super().setUp()
        self.raw["heater"]["budget_smooth_down_min"] = 2

    def down(self):
        return db.get_cascade_budget("heizstab")["budget_down_w"]

    def test_waiting_battery_drops_heater_now(self):
        self.seed(3600)
        # Heizstab 1600 W, Batterie lädt 600 W, Einspeisung 10 W → Batterie will 2210 W, Deckel 610 W
        svc = self.service(False, 45.0, -10.0, heater_w=1600.0, zd=ZendureStatus(soc=50, charge_w=600.0))
        svc.run_cascade()
        self.assertLessEqual(self.down(), 0)                       # ohne Fix: Mittel − Batterie > 1000 W

    def test_battery_not_waiting_keeps_smoothing(self):
        self.seed(3600)
        svc = self.service(False, 45.0, -10.0, heater_w=1600.0, zd=ZendureStatus(soc=100, charge_w=0.0))
        svc.run_cascade()
        self.assertGreater(self.down(), 1500)                      # Batterie voll: Glättung wie bisher


class HeadlineTest(DbCase):
    def cfg_web(self, summer=False):
        return SimpleNamespace(zendure_active=True, zendure=SimpleNamespace(control_enabled=True),
                               summer_mode=SimpleNamespace(enabled=summer),
                               comfort_min_for=lambda s: 52.0 if s == "summer" else 40.0)

    def setUp(self):
        super().setUp()
        db.init_cascade_tables()
        db.init_cascade_settings_table()
        db.ensure_zendure_cascade_device("Batterie", 2400)
        db.set_zendure_ctl(mode="charge", limit_w=660, decided_at=datetime.now().isoformat(timespec="seconds"))

    def test_winter_battery_first(self):
        self.assertEqual(web._battery_priority(self.cfg_web(), 45.0), 660)

    def test_summer_cold_water_heater_first(self):
        self.assertIsNone(web._battery_priority(self.cfg_web(summer=True), 45.0))

    def test_summer_warm_water_battery_first(self):
        self.assertEqual(web._battery_priority(self.cfg_web(summer=True), 55.0), 660)

    def test_battery_idle_no_priority(self):
        db.set_zendure_ctl(mode="idle", limit_w=0)
        self.assertIsNone(web._battery_priority(self.cfg_web(), 45.0))


if __name__ == "__main__":
    unittest.main()
