"""pvlog.py (Cron-Aufruf) schreibt dieselben Werte wie der Controller: Pergola + Batterie im Haus-/Tagesverbrauch."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import clients.solax_client as sc
import db
import pvlog
from clients.zendure_client import ZendureStatus
from models import Readings


def raw_data():
    raw = [0] * 100
    raw[14], raw[15] = 2000, 1000         # Strings Haupt-WR
    raw[10], raw[11], raw[12], raw[13] = 3500, 3000, 57, 33
    raw[34] = 65536 - 500                  # Einspeisung −500 W (Bezug) vorzeichenbehaftet
    raw[47] = 1500                         # Hausverbrauch laut Haupt-WR (ohne Pergola/Batterie)
    raw[82], raw[90], raw[92] = 100, 300, 500   # 10,0 kWh Ertrag, 3,00 kWh Einsp., 5,00 kWh Bezug
    return raw


class FakeController:
    readings = None

    def __init__(self, cfg):
        pass

    def _read_all(self):
        return FakeController.readings

    def _read_wallbox_status(self):
        return None, None


class PvlogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        self._old_raw, self._old_cls = sc._cached_raw, None
        sc._cached_raw = raw_data()
        import controller
        self._old_cls = controller.Controller
        controller.Controller = FakeController
        import pv_logger
        self._old_weather = pv_logger._fetch_weather_if_needed
        pv_logger._fetch_weather_if_needed = lambda: None

    def tearDown(self):
        import controller, pv_logger
        controller.Controller = self._old_cls
        pv_logger._fetch_weather_if_needed = self._old_weather
        sc._cached_raw = self._old_raw
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def readings(self, zd=None):
        return Readings(pv_power_w=3500.0, pv1_power_w=3000.0, pv2_power_w=500.0, pv2_today_kwh=2.0,
                        solax_yield_today_kwh=10.0, solax_grid_out_kwh=3.0, solax_grid_in_kwh=5.0,
                        main_meter_power_w=500.0, heater_meter_power_w=0.0, zendure=zd)

    def hourly_row(self):
        rows = db.get_hourly_data(datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_hourly_includes_pergola_in_consumption_and_total_pv(self):
        FakeController.readings = self.readings()
        pvlog.hourly_job(SimpleNamespace())
        row = self.hourly_row()
        self.assertEqual(row["consumption_w"], 1500 + 500)        # Haus + Pergola (vorher nur 1500)
        self.assertEqual(row["pv_w"], 3500)                       # Gesamt-PV (vorher nur Haupt-WR 3000)
        self.assertEqual(row["pv2_w"], 500)

    def test_hourly_battery_discharge_counts_charge_does_not(self):
        FakeController.readings = self.readings(ZendureStatus(soc=50, charge_w=800.0))
        pvlog.hourly_job(SimpleNamespace())
        self.assertEqual(self.hourly_row()["consumption_w"], 1500 + 500 - 800)
        FakeController.readings = self.readings(ZendureStatus(soc=50, discharge_w=600.0))
        pvlog.hourly_job(SimpleNamespace())
        self.assertEqual(self.hourly_row()["consumption_w"], 1500 + 500 + 600)

    def test_daily_uses_total_pv_and_selfuse_incl_pergola(self):
        FakeController.readings = self.readings()
        pvlog.daily_job(SimpleNamespace())
        d = db.get_daily_for_date(datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(d["pv_kwh"], 12.0)                        # 10,0 Haupt + 2,0 Pergola
        self.assertEqual(d["pv2_kwh"], 2.0)
        self.assertEqual(d["selfuse_kwh"], 9.0)                    # 12 − 3 Einspeisung

    def test_daily_log_keeps_forecast_and_house_peak(self):
        # Prognose (von /api/forecast bzw. Backfill) und Tageshoch dürfen einen _log_daily-Lauf überleben
        import sqlite3
        today = datetime.now().strftime("%Y-%m-%d")
        db.upsert_daily_forecast(today, 21.5, 2.7)
        db.update_house_peak(6200.0)
        FakeController.readings = self.readings()
        pvlog.daily_job(SimpleNamespace())
        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute("SELECT forecast_kwh, forecast_ghi, pv_kwh FROM pv_daily_log WHERE date = ?",
                               (today,)).fetchone()
        self.assertEqual(row, (21.5, 2.7, 12.0))                  # Kennzahlen neu, Prognose erhalten
        self.assertEqual(db.get_house_peak(today)["peak_w"], 6200)
        pvlog.daily_job(SimpleNamespace())                         # und auch ein zweiter Lauf
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT forecast_kwh FROM pv_daily_log WHERE date = ?", (today,)).fetchone()[0], 21.5)

    def test_daily_log_keeps_weather_when_not_passed_and_updates_when_given(self):
        import sqlite3
        today = datetime.now().strftime("%Y-%m-%d")
        db.log_daily_summary(today, 5.0, 1.0, 1.0, sunshine_h=6.5, ghi_kwh_m2=3.1, forecast_kwh=18.0)
        db.log_daily_summary(today, 6.0, 1.5, 1.0)                # ohne Wetter/Prognose
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT pv_kwh, sunshine_h, ghi_kwh_m2, forecast_kwh FROM pv_daily_log WHERE date = ?",
                                          (today,)).fetchone(), (6.0, 6.5, 3.1, 18.0))
        db.log_daily_summary(today, 6.0, 1.5, 1.0, sunshine_h=7.0)  # neuer Wert überschreibt
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT sunshine_h FROM pv_daily_log WHERE date = ?", (today,)).fetchone()[0], 7.0)

    def test_unreadable_inverter_writes_nothing(self):
        FakeController.readings = Readings()
        pvlog.hourly_job(SimpleNamespace())
        self.assertEqual(db.get_hourly_data(datetime.now().strftime("%Y-%m-%d")), [])


if __name__ == "__main__":
    unittest.main()
