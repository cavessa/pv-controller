"""Überschrift „Aktuelle Entscheidung“ aus dem Kaskaden-Ergebnis (headline.py, web._cascade_summary)."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import db
import decisions
import headline
import web
from cascade_plan import BATTERY, HEATER_MAX, HEATER_MIN
from models import Readings
from tests.test_solax_timeout import cfg_with

WINTER = ["shelly_test", BATTERY, HEATER_MAX, "wallbox"]
SUMMER = [HEATER_MIN, BATTERY, HEATER_MAX, "wallbox"]
TMAX = 64.0


def dev(id_, action, reason="", data=None, **kw):
    return {"id": id_, "action": action, "reason": reason, "data": data or {}, **kw}


def ebike(action="keep_off", reason="no_surplus", rest=0):
    return dev("shelly_test", action, reason, {"rest_w": rest, "power_w": 180, "threshold_w": 280},
               name="eBike", type="shelly_gen2")


def battery(action="idle", reason="standby_no_import", limit=0):
    return dev(BATTERY, action, reason, {"limit_w": limit}, name="Batterie", type="zendure")


def heater(actual=0, phases=0, budget=0, thr=1100, reason="below_start_threshold"):
    return dev("heizstab", "budget", reason, {"actual_w": actual, "target_phases": phases, "budget_w": budget,
                                               "threshold_w": thr}, name="Wasserspeicher", type="heizstab")


def wallbox(action="keep_off", reason="no_surplus", rest=0):
    return dev("wallbox", action, reason, {"rest_w": rest, "power_w": 1610, "threshold_w": 1710},
               name="Wallbox", type="wallbox")


def snap(devices, surplus=0, grid=0, profile="winter", connected=True, **energy):
    devices = list(devices) + [dev("wallbox_control", "unchanged", "charging" if connected else "not_connected")]
    return {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "profile": profile,
            "energy": {"surplus_w": surplus, "grid_w": grid, **energy}, "devices": devices}


def head(s, order=WINTER, temp=48.2, comfort=None, **kw):
    return headline.cascade_headline(s, order, temp, comfort, TMAX, **kw)


class HeadlineTest(unittest.TestCase):
    def test_screenshot_case_no_surplus_grid_draw_battery_empty(self):
        """12:06: Winterprofil, Batterie 10 %, Netzbezug 2,75 kW – früher „Wasserspeicher priorisiert“."""
        h = head(snap([ebike(), battery("idle", "battery_empty"), heater(), wallbox()],
                      grid=2750, battery_soc=10, battery_discharge_w=0))
        self.assertEqual(h["state"], "Kein Überschuss")
        self.assertEqual(h["reason"], "Netzbezug 2,75 kW. Batterie leer (SoC 10 %).")
        self.assertEqual(h["profile"], "Winterprofil")
        self.assertNotIn("62", h["reason"])
        self.assertNotIn("darf per PV", h["reason"])

    def test_no_surplus_battery_covers_base_load(self):
        h = head(snap([ebike(), battery("discharge", "discharging_base_load"), heater(), wallbox()],
                      grid=40, battery_soc=55, battery_discharge_w=620))
        self.assertEqual(h["state"], "Kein Überschuss")
        self.assertEqual(h["reason"], "Batterie entlädt 620 W und deckt den Grundbedarf (SoC 55 %).")

    def test_winter_battery_gets_the_surplus_first(self):
        h = head(snap([ebike(rest=1500), battery("charge", "charging_surplus", 1500), heater(), wallbox()],
                      surplus=1500, battery_charge_w=1500, battery_soc=40))
        self.assertEqual(h["state"], "Batterie priorisiert")
        self.assertTrue(h["reason"].startswith("Batterie lädt mit 1,50 kW aus PV-Überschuss (SoC 40 %)."))

    def test_summer_cold_water_heater_min_before_battery(self):
        h = head(snap([heater(actual=1500, phases=1), battery("idle", "standby_no_import"), wallbox()],
                      surplus=1600, profile="summer"), order=SUMMER, temp=45.0, comfort=52.0)
        self.assertEqual(h["state"], "Wasserspeicher bis 52 °C")
        self.assertIn("Heizstab heizt mit 1,50 kW, 1 Phase(n)", h["reason"])
        self.assertEqual(h["profile"], "Sommerprofil")

    def test_summer_warm_water_battery_first(self):
        h = head(snap([heater(), battery("charge", "charging_surplus", 900), wallbox()], surplus=900,
                      profile="summer", battery_charge_w=900), order=SUMMER, temp=55.0, comfort=52.0)
        self.assertEqual(h["state"], "Batterie priorisiert")

    def test_heater_max_waits_below_start_threshold(self):
        h = head(snap([battery("idle", "soc_target_reached"), heater(budget=600), wallbox()],
                      surplus=600, battery_soc=100), order=[BATTERY, HEATER_MAX, "wallbox"])
        self.assertEqual(h["state"], "Wasserspeicher bis Max 64 °C wartet")
        self.assertIn("Überschuss 600 W unter der Startschwelle 1,10 kW", h["reason"])

    def test_first_device_in_the_profile_that_runs(self):
        h = head(snap([ebike("keep_on", "keep_running"), battery("charge", "charging_surplus", 800),
                       heater(), wallbox()], surplus=1000, battery_charge_w=800))
        self.assertEqual(h["state"], "eBike")
        self.assertIn("Batterie lädt", h["reason"])

    def test_wallbox_charging(self):
        h = head(snap([ebike(), battery("idle", "soc_target_reached"), heater(reason="max_temp_reached"),
                       wallbox("keep_on", "keep_running")], surplus=2500, battery_soc=100), temp=64.5)
        self.assertEqual(h["state"], "Wallbox")

    def test_everything_full_means_feed_in(self):
        h = head(snap([ebike(), battery("idle", "soc_target_reached"), heater(reason="max_temp_reached"),
                       wallbox()], surplus=3000, battery_soc=100, connected=False), order=[BATTERY, HEATER_MAX, "wallbox"],
                 temp=64.2)
        self.assertEqual(h["state"], "Einspeisung")
        self.assertIn("3,00 kW gehen ins Netz", h["reason"])
        self.assertIn("Max 64 °C", h["reason"])                 # Max-Temperatur aus der Config, nicht 62

    def test_thermostat_full_counts_as_done(self):
        h = head(snap([battery("idle", "soc_target_reached"), heater()], surplus=2000, battery_soc=100),
                 order=[BATTERY, HEATER_MAX], heater_full=True)
        self.assertEqual(h["state"], "Einspeisung")
        self.assertIn("praktisch voll", h["reason"])


class CascadeSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_cascade_tables()
        db.init_cascade_settings_table()
        self.cfg = cfg_with()

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def test_fresh_snapshot_is_used(self):
        decisions.save(snap([battery("idle", "battery_empty")], grid=900, battery_soc=10))
        out = web._cascade_summary(self.cfg, Readings(main_meter_power_w=1250.0, storage_temp_c=48.0))
        self.assertEqual(out["state"], "Kein Überschuss")
        self.assertIn("Netzbezug 1,25 kW", out["reason"])           # Netz aus dem aktuellen Status, nicht dem Lauf
        self.assertNotIn("nicht aktuell", out["reason"])

    def test_stale_snapshot_falls_back_to_readings(self):
        s = snap([], surplus=2000)
        s["ts"] = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        decisions.save(s)
        out = web._cascade_summary(self.cfg, Readings(main_meter_power_w=-1500.0, storage_temp_c=48.0))
        self.assertEqual(out["state"], "Überschuss 1,50 kW")
        self.assertIn("nicht aktuell", out["reason"])


if __name__ == "__main__":
    unittest.main()
