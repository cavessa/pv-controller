"""Tests für Sommer-/Winterprofil, Verteilung auf Batterie und Heizstab, Grundlast-Entladen.

python -m unittest tests.test_cascade_profiles
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import db
from cascade_plan import (
    BATTERY, HEATER_MAX, HEATER_MIN, HeaterCtx, default_profile, normalize_profile, walk,
)
from clients.zendure_client import ZendureStatus
from config import ZendureConfig, load_config
from models import Readings
from zendure_control import plan

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"
ZC = ZendureConfig(enabled=True, ip="x", min_soc=10, target_soc=100, max_charge_w=2400, max_discharge_w=2400)

WINTER = [BATTERY, HEATER_MAX]
SUMMER = [HEATER_MIN, BATTERY, HEATER_MAX]


def battery_model(soc):
    """Batterie-Ladeplanung mit der echten plan()-Logik (SoC fix)."""
    st = ZendureStatus(soc=soc, home_out_w=0)

    def fn(avail_w):
        p = plan(ZC, st, avail_w=avail_w, import_w=0, wallbox_charging=False, override=None,
                 ctl_mode="idle", ctl_limit_w=0, seconds_since_change=1e9)
        return p.limit_w if p.mode == "charge" else 0
    return fn


def run(order, surplus, temp, soc, comfort_min=52.0, max_temp=63.0, grid_assist=400.0):
    ctx = HeaterCtx(phase_w=1500, grid_assist_w=grid_assist, n_phases=3, temp=temp,
                    comfort_min=comfort_min, max_temp=max_temp)
    return walk(order, surplus, ctx, battery_model(soc), lambda k, r: r)


class DistributionTest(unittest.TestCase):
    def test_winter_3kw_battery_half(self):
        r = run(WINTER, 3000, 45, 50)
        self.assertEqual(r.battery_w, 2400)
        self.assertEqual(r.heater_phases, 0)               # Rest 600 W < Startschwelle 1100 W

    def test_winter_6kw_battery_half_both_charge(self):
        r = run(WINTER, 6000, 45, 50)
        self.assertEqual(r.battery_w, 2400)
        self.assertEqual(r.heater_phases, 2)               # 3600 W + 400 W Netz → 2 Phasen

    def test_winter_3kw_battery_full(self):
        r = run(WINTER, 3000, 45, 100)
        self.assertEqual((r.battery_w, r.heater_phases), (0, 2))

    def test_summer_cold_water_3kw_heater_first(self):
        r = run(SUMMER, 3000, 45, 50)
        self.assertEqual((r.battery_w, r.heater_phases), (0, 2))

    def test_summer_cold_water_800w_goes_to_battery(self):
        r = run(SUMMER, 800, 45, 50)
        self.assertEqual((r.battery_w, r.heater_phases, r.heater_budget_w), (800, 0, 0))

    def test_summer_warm_water_skips_min_stage(self):
        r = run(SUMMER, 3000, 55, 50)
        self.assertEqual(r.battery_w, 2400)
        self.assertEqual(r.heater_phases, 0)               # Rest 600 W

    def test_summer_cold_water_big_surplus_both(self):
        # 9 kW: Stufe „bis Min“ nimmt 3 Phasen (4,5 kW), Batterie 2,4 kW, Rest bleibt beim Heizstab
        r = run(SUMMER, 9000, 45, 50)
        self.assertEqual(r.battery_w, 2400)
        self.assertEqual(r.heater_phases, 3)
        self.assertEqual(r.heater_budget_w, 6600)          # 9000 − 2400

    def test_summer_cold_water_5500w_battery_gets_the_remainder_not_the_heater(self):
        r = run(SUMMER, 5500, 45, 50)                      # Min-Stufe: 3 Phasen (4500), Rest 1000 → Batterie
        self.assertEqual((r.battery_w, r.heater_phases), (1000, 3))
        self.assertEqual(r.heater_budget_w, 4500)

    def test_full_storage_no_heater_budget_users_after_get_remainder(self):
        r = run(WINTER, 3000, 64, 100)                     # Speicher voll, Batterie voll
        self.assertFalse(r.heater_active)
        self.assertEqual(r.battery_w, 0)

    def test_unknown_temp_keeps_max_stage_active(self):
        r = run(WINTER, 3000, None, 100)
        self.assertEqual(r.heater_phases, 2)

    def test_other_device_between_keeps_position(self):
        # eBike (180 W) steht vor dem Block und bekommt seinen Anteil zuerst
        r = walk(["ebike", BATTERY, HEATER_MAX], 3000,
                 HeaterCtx(temp=45, grid_assist_w=400), battery_model(50), lambda k, rem: rem - 180)
        self.assertEqual(r.battery_w, 2400)                # 2820 verfügbar → Batterie 2,4 kW
        self.assertEqual(r.heater_budget_w, 420)

    def test_measured_heater_power_is_booked_for_followers(self):
        seen = []
        walk([HEATER_MAX, "x"], 4000, HeaterCtx(temp=45, actual_w=1500), battery_model(50),
             lambda k, rem: (seen.append(rem), rem)[1])
        self.assertEqual(seen, [2500])                     # gemessene Leistung, nicht Nominal


class WinterFloorTest(unittest.TestCase):
    WINTER_FLOOR = [HEATER_MIN, BATTERY, HEATER_MAX]

    def test_cold_water_gets_heater_before_battery(self):
        r = run(self.WINTER_FLOOR, 3000, 35, 50, comfort_min=40.0)
        self.assertEqual((r.battery_w, r.heater_phases), (0, 2))

    def test_above_floor_battery_comes_first(self):
        r = run(self.WINTER_FLOOR, 3000, 45, 50, comfort_min=40.0)
        self.assertEqual((r.battery_w, r.heater_phases), (2400, 0))

    def test_small_surplus_below_floor_still_charges_battery(self):
        r = run(self.WINTER_FLOOR, 800, 35, 50, comfort_min=40.0)
        self.assertEqual((r.battery_w, r.heater_phases), (800, 0))


class SmoothingTest(unittest.TestCase):
    def test_gross_is_reported_before_battery(self):
        seen = []
        ctx = HeaterCtx(temp=45, grid_assist_w=400)
        r = walk(WINTER, 6000, ctx, battery_model(50), lambda k, rem: rem,
                 lambda g, b, rem: seen.append((g, b, rem)))
        self.assertEqual(seen, [(6000, 2400, 3600)])
        self.assertEqual((r.heater_gross_w, r.heater_budget_w), (6000, 3600))


class DischargeTest(unittest.TestCase):
    def discharge(self, **kw):
        st = ZendureStatus(soc=50, home_out_w=kw.pop("home_out_w", 0))
        base = dict(avail_w=0, import_w=0, wallbox_charging=False, override=None, ctl_mode="idle",
                    ctl_limit_w=0, seconds_since_change=1e9)
        base.update(kw)
        return plan(ZC, st, **base)

    def test_heater_from_grid_house_400w_discharge_max_400(self):
        p = self.discharge(import_w=1900, base_load_w=400)   # Heizstab 1500 W + Grundlast 400 W aus dem Netz
        self.assertEqual((p.mode, p.limit_w), ("discharge", 400))

    def test_discharge_limited_by_import_too(self):
        p = self.discharge(import_w=300, base_load_w=900)
        self.assertEqual((p.mode, p.limit_w), ("discharge", 300))

    def test_only_heater_import_means_no_discharge(self):
        p = self.discharge(import_w=1500, base_load_w=0)
        self.assertEqual(p.mode, "idle")
        self.assertIn("Heizstab", p.reason)

    def test_wallbox_charging_blocks_discharge(self):
        p = self.discharge(import_w=2000, base_load_w=2000, wallbox_charging=True)
        self.assertEqual((p.mode, p.limit_w), ("idle", 0))

    def test_unknown_base_load_blocks_discharge(self):
        p = self.discharge(import_w=800, base_load_w=None)
        self.assertEqual(p.mode, "idle")

    def test_no_discharge_while_charging(self):
        p = self.discharge(avail_w=1000, import_w=800, base_load_w=800)
        self.assertEqual(p.mode, "charge")

    def test_running_discharge_tracks_import_within_base_load(self):
        p = self.discharge(import_w=200, home_out_w=300, ctl_mode="discharge", ctl_limit_w=300, base_load_w=450)
        self.assertEqual((p.mode, p.limit_w), ("discharge", 450))


class NeutralSurplusTest(unittest.TestCase):
    def test_charging_battery_counts_as_surplus(self):
        r = Readings(main_meter_power_w=-2000.0, heater_meter_power_w=0.0,
                     zendure=ZendureStatus(soc=50, charge_w=1000.0))
        # Netz −2 kW (Einspeisung) + 1 kW Ladung → 3 kW Überschuss (Konvention: negativ = Überschuss)
        self.assertEqual(r.true_surplus_w, -3000.0)
        self.assertEqual(r.surplus_without_heater_w, -3000.0)

    def test_discharge_is_no_surplus(self):
        r = Readings(main_meter_power_w=-500.0, heater_meter_power_w=0.0,
                     zendure=ZendureStatus(soc=50, discharge_w=800.0))
        self.assertEqual(r.true_surplus_w, -500.0 + 800.0)

    def test_without_battery_unchanged(self):
        r = Readings(main_meter_power_w=-2000.0, heater_meter_power_w=500.0, wallbox_power_w=100.0)
        self.assertEqual(r.true_surplus_w, -2000.0 - 500.0 - 100.0)

    def test_house_consumption_excludes_battery_charge(self):
        r = Readings(pv_power_w=3000.0, main_meter_power_w=-500.0,
                     zendure=ZendureStatus(soc=50, charge_w=1000.0))
        self.assertEqual(r.house_consumption_w, 3000.0 - 500.0 - 1000.0)
        r = Readings(pv_power_w=0.0, main_meter_power_w=300.0,
                     zendure=ZendureStatus(soc=50, discharge_w=800.0))
        self.assertEqual(r.house_consumption_w, 1100.0)


class ProfileTest(unittest.TestCase):
    DEVICES = ["ebike", "heizstab", "wallbox", "zendure"]

    def test_defaults_keep_positions(self):
        self.assertEqual(default_profile("winter", self.DEVICES), ["ebike", BATTERY, HEATER_MAX, "wallbox"])
        self.assertEqual(default_profile("summer", self.DEVICES),
                         ["ebike", HEATER_MIN, BATTERY, HEATER_MAX, "wallbox"])

    def test_normalize_appends_new_devices_and_drops_unknown(self):
        out = normalize_profile("winter", [BATTERY, "gone", HEATER_MAX, BATTERY], self.DEVICES + ["neu"])
        self.assertEqual(out, [BATTERY, HEATER_MAX, "ebike", "wallbox", "neu"])

    def test_winter_floor_adds_min_stage_before_battery(self):
        self.assertEqual(default_profile("winter", self.DEVICES, True),
                         ["ebike", HEATER_MIN, BATTERY, HEATER_MAX, "wallbox"])
        out = normalize_profile("winter", [BATTERY, HEATER_MAX, "ebike", "wallbox"], self.DEVICES, True)
        self.assertEqual(out[:3], [HEATER_MIN, BATTERY, HEATER_MAX])
        self.assertNotIn(HEATER_MIN, normalize_profile("winter", [HEATER_MIN, BATTERY, HEATER_MAX], self.DEVICES, False))

    def test_winter_has_no_min_stage_summer_orders_min_before_max(self):
        self.assertNotIn(HEATER_MIN, normalize_profile("winter", [HEATER_MIN, BATTERY, HEATER_MAX], self.DEVICES))
        out = normalize_profile("summer", [HEATER_MAX, HEATER_MIN, BATTERY], self.DEVICES)
        self.assertLess(out.index(HEATER_MIN), out.index(HEATER_MAX))


class ServiceIntegrationTest(unittest.TestCase):
    """run_cascade mit Fake-Geräten: Profilwahl nach Sommermodus, Budget für den Controller."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        db.init_cascade_tables()
        db.init_cascade_settings_table()
        with open(EXAMPLE) as f:
            raw = json.load(f)
        raw["wallbox"]["enabled"] = False
        raw["runtime"]["dry_run"] = True
        raw["heater"].update({"storage_max_temp": 63, "storage_temp_hysteresis": 1, "grid_assist_w": 400,
                              "budget_smooth_min": 1, "budget_smooth_down_min": 1})
        raw["summer_mode"] = {"enabled": False, "min_temp": 45, "target_temp": 52}
        raw["zendure"] = {"enabled": True, "ip": "x", "control_enabled": True}
        self.raw = raw
        self.path = Path(self.tmp.name) / "c.json"

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def service(self, summer, temp, main_w, soc=50, pv=6000.0, heater_w=0.0, zd=None):
        from cascade_service import CascadeService
        self.raw["summer_mode"]["enabled"] = summer
        self.path.write_text(json.dumps(self.raw))
        cfg = load_config(self.path)
        svc = CascadeService(cfg)
        svc.heater_meter = SimpleNamespace(get_total_power_w=lambda: heater_w)
        svc.main_meter = SimpleNamespace(get_total_power_w=lambda: main_w)
        svc.storage = SimpleNamespace(get_temperature_c=lambda: temp)
        svc.solax = SimpleNamespace(get_pv_power_w=lambda: pv)
        svc.solax2 = None
        st = zd or ZendureStatus(soc=soc)
        if svc.zendure_ctl is not None:
            svc.zendure_client = SimpleNamespace(get_status=lambda: st)
            svc.zendure_ctl.client = svc.zendure_client
        return svc

    def budget(self):
        return db.get_cascade_budget("heizstab")["budget_w"]

    def test_winter_profile_battery_first(self):
        svc = self.service(False, 45.0, -3000.0)
        out = svc.run_cascade()
        self.assertEqual(out["profile"], "winter")
        self.assertEqual(self.budget(), 600)                 # 3000 − 2400 Batterie

    def test_summer_profile_cold_water_heater_first(self):
        svc = self.service(True, 45.0, -3000.0)
        out = svc.run_cascade()
        self.assertEqual(out["profile"], "summer")
        self.assertEqual(self.budget(), 3000)                # Min-Stufe nimmt 2 Phasen, Batterie 0

    def test_summer_warm_water_battery_first(self):
        svc = self.service(True, 55.0, -3000.0)
        svc.run_cascade()
        self.assertEqual(self.budget(), 600)

    def test_winter_floor_cold_water_heats_before_battery(self):
        svc = self.service(False, 35.0, -3000.0)             # Untergrenze 40 °C (Default)
        svc.run_cascade()
        self.assertEqual(self.budget(), 3000)

    def test_winter_floor_can_be_switched_off(self):
        self.raw["heater"]["winter_comfort_min_temp"] = None
        svc = self.service(False, 35.0, -3000.0)
        svc.run_cascade()
        self.assertEqual(self.budget(), 600)                 # Batterie zuerst, Rest 600 W

    def test_battery_full_raises_heater_budget_in_the_same_cycle(self):
        import sqlite3
        from datetime import datetime, timedelta
        def seed():
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
                conn.execute("DELETE FROM heater_budget_log")
                for i in range(1, 10):
                    ts = (datetime.now() - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                    conn.execute("INSERT OR REPLACE INTO heater_budget_log (ts, raw_w) VALUES (?, ?)", (ts, 5400))
        # Gesamt-Überschuss seit 9 Minuten 5,4 kW; Batterie lädt jetzt 2,4 kW → Budget 3 kW
        seed()
        svc = self.service(False, 45.0, -3000.0, soc=50, zd=ZendureStatus(soc=50, charge_w=2400.0))
        svc.run_cascade()
        self.assertEqual(self.budget(), 3000)
        # Batterie ist voll: dasselbe Mittel, aber nichts mehr abzuziehen → sofort 5,4 kW
        seed()
        svc = self.service(False, 45.0, -3000.0, soc=100, zd=ZendureStatus(soc=100, charge_w=2400.0))
        svc.run_cascade()
        self.assertEqual(self.budget(), 5400)

    def test_collapsing_surplus_does_not_start_heater_from_the_old_average(self):
        import sqlite3
        from datetime import datetime, timedelta
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
            for i in range(1, 10):
                ts = (datetime.now() - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                conn.execute("INSERT OR REPLACE INTO heater_budget_log (ts, raw_w) VALUES (?, ?)", (ts, 5400))
        # Überschuss bricht auf 500 W ein: Batterie nimmt ihn, der Heizstab startet nicht auf dem Mittel 4,9 kW
        svc = self.service(False, 45.0, -500.0, soc=50)
        svc.run_cascade()
        self.assertEqual(self.budget(), 0)

    def test_heater_start_is_capped_at_current_surplus_even_without_battery(self):
        import sqlite3
        from datetime import datetime, timedelta
        self.raw["zendure"]["enabled"] = False
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS heater_budget_log (ts TEXT PRIMARY KEY, raw_w REAL)")
            for i in range(1, 10):
                ts = (datetime.now() - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
                conn.execute("INSERT OR REPLACE INTO heater_budget_log (ts, raw_w) VALUES (?, ?)", (ts, 5400))
        svc = self.service(False, 45.0, -500.0)
        svc.run_cascade()
        self.assertEqual(self.budget(), 500)                  # nicht das Mittel 4,9 kW

    def test_charging_battery_is_added_back_to_the_surplus(self):
        # Batterie lädt 1 kW, Netz −2 kW (Einspeisung) → Kaskaden-Überschuss 3 kW
        svc = self.service(False, 45.0, -2000.0, zd=ZendureStatus(soc=50, charge_w=1000.0))
        out = svc.run_cascade()
        self.assertEqual(out["surplus_watts"], 3000)

    def test_discharge_only_covers_base_load_not_the_heater(self):
        # Heizstab zieht 1,5 kW aus dem Netz, Haus ohne Heizstab 400 W → Entladen max. 400 W
        svc = self.service(False, 45.0, 1900.0, pv=0.0, heater_w=1500.0)
        svc.run_cascade()
        decision = db.get_zendure_ctl()["decision"]
        self.assertIn("discharge 400 W", decision)

    def test_cascade_log_has_no_keep_or_budget_rows_unless_verbose(self):
        def actions():
            with sqlite3.connect(db.DB_PATH) as conn:
                return {r[0] for r in conn.execute("SELECT DISTINCT action FROM cascade_log")}
        import sqlite3
        svc = self.service(False, 45.0, -3000.0)
        svc.run_cascade(); svc.run_cascade()
        self.assertFalse(actions() & {"keep_on", "keep_off", "budget"})
        dec = db.get_cascade_decisions()
        self.assertIn("heizstab", dec)                         # aktuelle Entscheidung bleibt sichtbar
        self.assertIn("Budget", dec["heizstab"]["reason"])
        self.raw["cascade"] = {"log_verbose": True}
        svc = self.service(False, 45.0, -3000.0)
        svc.run_cascade()
        self.assertTrue(actions() & {"keep_on", "keep_off", "budget"})

    def test_battery_disabled_means_heater_gets_everything(self):
        self.raw["zendure"]["enabled"] = False
        svc = self.service(False, 45.0, -3000.0)
        self.assertIsNone(svc.zendure_ctl)
        svc.run_cascade()
        self.assertEqual(self.budget(), 3000)

if __name__ == "__main__":
    unittest.main()
