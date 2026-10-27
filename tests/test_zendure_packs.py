"""Akku-Packs (nur Beobachtung): packData parsen, Stundenwerte, Hinweise, erste BMS-Begrenzung pro Tag."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import zendure_control as zc
from clients.zendure_client import ZendureClient, ZendureStatus, parse_packs
from tests import test_cascade_profiles as tcp

# Echter Report vom 27.10.2026 14:16 (gekürzt, ohne Seriennummern)
PACK_DATA = [
    {"packType": 500, "socLevel": 86, "state": 0, "power": 0, "maxTemp": 3121, "maxVol": 353, "minVol": 336},
    {"packType": 350, "socLevel": 84, "state": 1, "power": 207, "maxTemp": 2991, "maxVol": 337, "minVol": 336},
    {"packType": 350, "socLevel": 84, "state": 1, "power": 196, "maxTemp": 3031, "maxVol": 337, "minVol": 336},
]


class TmpDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_real_report(self):
        p = parse_packs(PACK_DATA)
        self.assertEqual([x["idx"] for x in p], [1, 2, 3])
        self.assertEqual((p[0]["max_mv"], p[0]["min_mv"], p[0]["temp_c"], p[0]["soc"]), (3530, 3360, 39.0, 86.0))
        self.assertEqual(p[1]["temp_c"], 26.0)
        self.assertNotIn("sn", p[0])

    def test_missing_or_broken(self):
        self.assertEqual(parse_packs(None), [])
        self.assertEqual(parse_packs([None, {"packType": 1}])[0]["max_mv"], None)

    def test_client_status_carries_packs(self):
        c = ZendureClient("1.2.3.4")
        with mock.patch.object(c, "_report", return_value={"properties": {"electricLevel": 84}, "packData": PACK_DATA}):
            st = c.get_status(fresh=True)
        self.assertEqual(len(st.packs), 3)


class HourTest(TmpDb):
    def test_largest_diff_of_the_hour_wins_with_its_soc(self):
        t = datetime(2026, 10, 27, 14, 5)
        db.record_zendure_packs([{"idx": 1, "type": 500, "soc": 80, "max_mv": 3400, "min_mv": 3350, "temp_c": 35}], t)
        db.record_zendure_packs([{"idx": 1, "type": 500, "soc": 84, "max_mv": 3530, "min_mv": 3360, "temp_c": 39}],
                                t + timedelta(minutes=10))
        db.record_zendure_packs([{"idx": 1, "type": 500, "soc": 86, "max_mv": 3450, "min_mv": 3400, "temp_c": 37}],
                                t + timedelta(minutes=20))
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = t + timedelta(hours=1)
            dt.strftime = datetime.strftime
            rows = db.get_zendure_packs(7)
        r = rows[0]
        self.assertEqual((r["diff_mv"], r["max_mv"], r["min_mv"], r["soc_at_diff"]), (170, 3530, 3360, 84))
        self.assertEqual((r["soc"], r["temp_max_c"], r["n"]), (86, 39, 3))

    def test_unknown_values_stay_none(self):
        db.record_zendure_packs([{"idx": 2, "soc": None, "max_mv": None, "min_mv": None, "temp_c": None}])
        r = db.get_zendure_packs(1)[0]
        self.assertIsNone(r["diff_mv"]); self.assertIsNone(r["temp_max_c"])

    def test_bms_limit_only_first_per_day(self):
        t = datetime(2026, 10, 27, 13, 47)
        self.assertTrue(db.record_bms_limit(t, 80, 505, 2400))
        self.assertFalse(db.record_bms_limit(t + timedelta(hours=1), 90, 300, 2400))
        with mock.patch("db.datetime") as dt:
            dt.now.return_value = t
            rows = db.get_bms_limits(14)
        self.assertEqual((rows[0]["day"], rows[0]["soc"], rows[0]["at"][11:16]), ("2026-10-27", 80, "13:47"))


class ApiTest(TmpDb):
    def call(self):
        import web
        from fastapi import Response
        return web.api_zendure_packs(Response(), days=7)

    def rec(self, idx, soc, mx, mn, temp):
        db.record_zendure_packs([{"idx": idx, "type": 350, "soc": soc, "max_mv": mx, "min_mv": mn, "temp_c": temp}])

    def test_alert_for_large_diff_below_90_and_heat(self):
        self.rec(1, 84, 3530, 3360, 46.5)
        self.rec(2, 84, 3370, 3360, 26)
        d = self.call()
        self.assertEqual([r["diff_mv"] for r in d["latest"]], [170, 10])
        self.assertEqual(len(d["alerts"]), 2)
        self.assertIn("Pack 1: Zellen-Differenz 170 mV bei 84 % SoC", d["alerts"][0])
        self.assertIn("46 °C", d["alerts"][1])

    def test_large_diff_near_full_is_normal(self):
        self.rec(1, 95, 3530, 3360, 30)
        self.assertEqual(self.call()["alerts"], [])

    def test_threshold_is_exclusive(self):
        self.rec(1, 80, 3510, 3360, 45)                     # genau 150 mV / 45 °C → kein Hinweis
        self.assertEqual(self.call()["alerts"], [])


class OnsetTest(unittest.TestCase):
    def test_onset_is_start_of_gap(self):
        with tempfile.TemporaryDirectory() as d:
            path, t = Path(d) / "s.json", datetime(2026, 10, 27, 13, 47)
            for i, soc in enumerate((80, 81, 81)):
                zc.charge_acceptance(ZendureStatus(soc=soc, charge_w=505), "charge", 2400, state_path=path,
                                     now=t + timedelta(minutes=i))
            self.assertEqual(zc.acceptance_onset(path), (t, 80))
            zc.charge_acceptance(ZendureStatus(soc=82, charge_w=0), "idle", 0, state_path=path, now=t)
            self.assertEqual(zc.acceptance_onset(path), (None, None))


class CascadeRecordsBmsLimitTest(unittest.TestCase):
    setUp = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service

    def test_third_run_records_the_day_once(self):
        for soc in (80, 81, 81, 82):
            db.set_zendure_ctl(mode="charge", limit_w=2400)
            self.service(False, 45.0, -4000.0, soc=soc, zd=ZendureStatus(soc=soc, charge_w=505.0)).run_cascade()
        rows = db.get_bms_limits(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["soc"], rows[0]["charge_w"], rows[0]["limit_w"]), (80, 505, 2400))


if __name__ == "__main__":
    unittest.main()
