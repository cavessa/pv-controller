"""Haupt-WR nachts offline: PV = 0 W statt Fail-safe, solange der Hauptzähler lesbar ist."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import db
import suntime
from cascade_service import CascadeService
from clients.zendure_client import ZendureStatus
from tests import test_cascade_profiles as tcp
from tests.test_zendure_safety import FakeDevice

TZ = suntime.TZ


def at(m, d, h, mi):
    return datetime(2026, m, d, h, mi, tzinfo=TZ)


class NightWindowTest(unittest.TestCase):
    def svc(self, loc=(48.0628, 9.6071)):
        s = CascadeService.__new__(CascadeService)
        s.cfg = SimpleNamespace(location=loc)
        return s

    def night(self, when, **kw):
        s = self.svc(**kw)
        with mock.patch.object(CascadeService, "_now", return_value=when):
            return s._night_without_sun()

    def test_evening_and_night_after_sunset_plus_margin(self):
        # 24.10.: Untergang 18:16 → Nacht ab 18:46
        self.assertFalse(self.night(at(10, 24, 18, 30)))
        self.assertTrue(self.night(at(10, 24, 18, 47)))
        self.assertTrue(self.night(at(10, 24, 21, 57)))
        self.assertTrue(self.night(at(10, 24, 3, 0)))

    def test_morning_until_sunrise_minus_margin(self):
        # 24.10.: Aufgang 07:54 → Tag ab 07:24
        self.assertTrue(self.night(at(10, 24, 7, 20)))
        self.assertFalse(self.night(at(10, 24, 7, 30)))
        self.assertFalse(self.night(at(10, 24, 12, 0)))

    def test_summer_evening_is_not_night_at_21_30(self):
        # 21.6.: Untergang ~21:30 → um 21:40 noch Tag, ab ~22:00 Nacht
        self.assertFalse(self.night(at(6, 21, 21, 40)))
        self.assertTrue(self.night(at(6, 21, 22, 15)))

    def test_without_location_never_night(self):
        self.assertFalse(self.night(at(10, 24, 23, 0), loc=None))


class ReadPvTest(unittest.TestCase):
    def read(self, pv, when, pergola=None):
        s = CascadeService.__new__(CascadeService)
        s.cfg = SimpleNamespace(location=(48.0628, 9.6071))
        s.solax = SimpleNamespace(get_pv_ac_w=lambda: pv)
        s.solax2 = None if pergola is None else SimpleNamespace(get_realtime_data=lambda: SimpleNamespace(pv_ac_w=pergola))
        with mock.patch.object(CascadeService, "_now", return_value=when):
            return s._read_pv_total_w()

    def test_offline_at_night_is_zero(self):
        self.assertEqual(self.read(None, at(10, 24, 22, 0), pergola=None), 0.0)

    def test_offline_by_day_stays_unreadable(self):
        self.assertIsNone(self.read(None, at(10, 24, 12, 0)))

    def test_readable_value_is_never_replaced_at_night(self):
        self.assertEqual(self.read(120.0, at(10, 24, 22, 0), pergola=30.0), 150.0)


class BatteryAtNightTest(unittest.TestCase):
    """Ganzer Kaskadenlauf: Haupt-WR offline, Nacht, Hauptzähler lesbar → Batterie entlädt die Grundlast."""
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service

    def run_night(self, when, main_w=900.0, soc=60):
        self.raw["runtime"]["dry_run"] = False
        self.raw["latitude"], self.raw["longitude"] = 48.0628, 9.6071
        dev = FakeDevice(ac=0, soc=soc)
        svc = self.service(False, 45.0, main_w, pv=None, zd=ZendureStatus(soc=soc))
        svc.zendure_client = dev
        svc.zendure_ctl.client = dev
        svc.solax.get_realtime_data = lambda: None
        with mock.patch.object(CascadeService, "_now", return_value=when):
            svc.run_cascade()
        return dev

    def test_night_battery_discharges_instead_of_failsafe(self):
        dev = self.run_night(at(10, 24, 22, 0))
        self.assertNotIn("Fail-safe", db.get_zendure_ctl()["decision"])
        self.assertEqual(db.get_zendure_ctl()["mode"], "discharge")
        self.assertTrue(any(w[2] for w in dev.writes))           # Ausgangsleistung gesetzt

    def test_same_situation_by_day_is_failsafe(self):
        dev = self.run_night(at(10, 24, 12, 0))
        self.assertIn("Wechselrichter nicht lesbar", db.get_zendure_ctl()["decision"])
        self.assertEqual(db.get_zendure_ctl()["mode"], "idle")

    def test_night_but_meter_unreadable_is_still_failsafe(self):
        self.raw["runtime"]["dry_run"] = False
        self.raw["latitude"], self.raw["longitude"] = 48.0628, 9.6071
        dev = FakeDevice(ac=0, soc=60)
        svc = self.service(False, 45.0, None, pv=None, zd=ZendureStatus(soc=60))
        svc.zendure_client = dev
        svc.zendure_ctl.client = dev
        svc.solax.get_realtime_data = lambda: None
        with mock.patch.object(CascadeService, "_now", return_value=at(10, 24, 22, 0)):
            svc.run_cascade()
        self.assertIn("Hauptzähler", db.get_zendure_ctl()["decision"])


if __name__ == "__main__":
    unittest.main()
