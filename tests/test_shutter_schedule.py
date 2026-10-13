"""Rollläden Phase 2: Sonnenzeiten, Zeitpläne, Sicherheitsregeln im Cron, Web-Endpunkte."""
import json
import logging
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import heating
import main as main_mod
import shutter_schedule as ss
import shutters
import web
from clients.openhab_client import OpenHabError

TZ = ss.TZ
LOC = (48.0628, 9.6071)          # Koordinaten der Anlage (config latitude/longitude)
ROOMS = [
    {"id": "schlafen", "name": "Schlafzimmer", "item": "S_Roll", "device": "shelly", "automation": "pvcontroller"},
    {"id": "terrasse", "name": "Terrasse", "item": "T_Roll", "contact_item": "T_Door", "is_door": True,
     "device": "shelly", "automation": "pvcontroller"},
    {"id": "hinten", "name": "Hintereingang", "item": "H_Roll", "is_door": True, "device": "homematic",
     "automation": "pvcontroller"},
    {"id": "wohnen", "name": "Wohnen", "item": "W_Roll", "device": "homematic", "automation": "openhab"},
]
ITEMS = {
    "S_Roll": {"name": "S_Roll", "type": "Rollershutter", "state": "0"},
    "T_Roll": {"name": "T_Roll", "type": "Rollershutter", "state": "0"},
    "T_Door": {"name": "T_Door", "type": "Contact", "state": "CLOSED"},
    "H_Roll": {"name": "H_Roll", "type": "Rollershutter", "state": "0"},
    "W_Roll": {"name": "W_Roll", "type": "Rollershutter", "state": "0"},
    "Out_T": {"name": "Out_T", "type": "Number:Temperature", "state": "8.5 °C"},
    "CCU_DC": {"name": "CCU_DC", "type": "Number", "state": "10"},
}


def raw(**extra):
    return {"openhab": {"url": "http://oh:8080", "token": "t"}, "latitude": LOC[0], "longitude": LOC[1],
            "shutters": {"enabled": True, "schedules_enabled": True, "command_spacing_s": 2,
                         "frost_temp_item": "Out_T", "duty_cycle_item": "CCU_DC", "rooms": ROOMS, **extra}}


class Client:
    def __init__(self, items=ITEMS):
        self.items = {k: dict(v) for k, v in items.items()}
        self.calls = []
        self.history = {}            # item → [(epoch_ms, value)]
        self.down = False
        self.history_fail = False
        self.send_fail = set()
        self.timeout = 3.0

    def get_items(self, names):
        self.calls.append(("get_items", tuple(names)))
        if self.down:
            raise OpenHabError("timeout")
        return {n: self.items[n] for n in names if n in self.items}

    def get_history(self, name, start, end=None, service_id=None):
        self.calls.append(("history", name))
        if self.history_fail:
            raise OpenHabError("persistence weg")
        return self.history.get(name, [])

    def send_command(self, name, value):
        self.calls.append(("send", name, value))
        if name in self.send_fail:
            raise OpenHabError("timeout")

    @property
    def sends(self):
        return [c[1:] for c in self.calls if c[0] == "send"]


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        self.cfg = shutters.parse_shutters(raw())

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def add(self, shutter_id="schlafen", action="down", kind="time", time="21:00", days="mo,di,mi,do,fr,sa,so",
            date=None, position=None, offset_min=0, not_before=None, not_after=None, created=None):
        return ss.create_schedule({"shutter_id": shutter_id, "action": action, "position": position, "kind": kind,
                                   "time": time if kind == "time" else None, "offset_min": offset_min,
                                   "not_before": not_before, "not_after": not_after, "days": days, "date": date},
                                  now=created or datetime(2026, 10, 1, tzinfo=TZ))


class SunTest(unittest.TestCase):
    """Referenz: Open-Meteo für dieselben Koordinaten (weather_log), ±2 min."""

    def assertNear(self, dt, hhmm, tz_name):
        h, m = map(int, hhmm.split(":"))
        ref = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        self.assertLessEqual(abs((dt - ref).total_seconds()), 120, f"{dt} vs {hhmm}")
        self.assertEqual(dt.tzname(), tz_name)

    def test_autumn(self):
        rise, sset = ss.sun_times(date(2026, 10, 13), *LOC)
        self.assertNear(rise, "07:37", "CEST")
        self.assertNear(sset, "18:37", "CEST")

    def test_summer_and_winter(self):
        _r, sset = ss.sun_times(date(2026, 6, 21), *LOC)
        self.assertNear(sset, "21:24", "CEST")
        rise, sset = ss.sun_times(date(2026, 12, 21), *LOC)
        self.assertNear(rise, "08:08", "CET")
        self.assertNear(sset, "16:30", "CET")

    def test_dst_end_25_10(self):
        """Zeitumstellung 25.10.2026: Sonnenaufgang springt um ~1 h früher (Open-Meteo rechnet hier falsch)."""
        r24, s24 = ss.sun_times(date(2026, 10, 24), *LOC)
        r25, s25 = ss.sun_times(date(2026, 10, 25), *LOC)
        r26, _ = ss.sun_times(date(2026, 10, 26), *LOC)
        self.assertNear(r24, "07:54", "CEST")
        self.assertNear(s24, "18:16", "CEST")
        self.assertNear(r25, "06:55", "CET")
        self.assertNear(s25, "17:14", "CET")
        self.assertNear(r26, "06:57", "CET")
        # tatsächlicher Zeitabstand: knapp 24 h (Tage werden kürzer), nicht 23 h
        utc = timezone.utc
        self.assertAlmostEqual((r25.astimezone(utc) - r24.astimezone(utc)).total_seconds() / 3600, 24.02, delta=0.05)

    def test_dst_start_29_03(self):
        r28, _ = ss.sun_times(date(2026, 3, 28), *LOC)
        r29, _ = ss.sun_times(date(2026, 3, 29), *LOC)
        self.assertEqual((r28.tzname(), r29.tzname()), ("CET", "CEST"))
        self.assertAlmostEqual((r29.hour * 60 + r29.minute) - (r28.hour * 60 + r28.minute), 58, delta=2)

    def test_offset_and_limits(self):
        e = {"kind": "sunset", "offset_min": 15, "not_before": None, "not_after": None}
        d = date(2026, 10, 13)
        _r, sset = ss.sun_times(d, *LOC)
        self.assertEqual(ss.instant(e, d, LOC), (sset + timedelta(minutes=15)).replace(second=0, microsecond=0))
        self.assertEqual(ss.instant({**e, "not_after": "18:30"}, d, LOC), datetime(2026, 10, 13, 18, 30, tzinfo=TZ))
        self.assertEqual(ss.instant({**e, "not_before": "20:00"}, d, LOC), datetime(2026, 10, 13, 20, 0, tzinfo=TZ))
        e2 = {"kind": "sunrise", "offset_min": -30, "not_before": "06:30", "not_after": None}
        # 25.10. Aufgang 06:55 − 30 = 06:25 → nicht vor 06:30
        self.assertEqual(ss.instant(e2, date(2026, 10, 25), LOC), datetime(2026, 10, 25, 6, 30, tzinfo=TZ))
        self.assertIsNone(ss.instant(e, d, None))

    def test_fixed_time_in_doubled_hour(self):
        at = ss.instant({"kind": "time", "time": "02:30"}, date(2026, 10, 25), LOC)
        self.assertEqual(at.utcoffset(), timedelta(hours=2))                  # erstes Auftreten

    def test_describe(self):
        self.assertEqual(ss.describe({"kind": "time", "time": "07:00"}), "07:00")
        self.assertEqual(ss.describe({"kind": "sunset", "offset_min": 15, "not_after": "22:00"}),
                         "Sonnenuntergang +15 min (nicht nach 22:00)")
        self.assertEqual(ss.describe({"kind": "sunrise", "offset_min": -10}), "Sonnenaufgang −10 min")


class ValidateTest(DbCase):
    def v(self, **kw):
        args = dict(shutter_id="schlafen", action="down", kind="sunset", offset_min=15, days=["mo"], loc=LOC,
                    now=datetime(2026, 10, 13, 12, tzinfo=TZ))
        args.update(kw)
        return ss.validate_entry(self.cfg, **args)

    def test_ok(self):
        f = self.v(not_after="22:00")
        self.assertEqual((f["kind"], f["offset_min"], f["not_after"], f["days"]), ("sunset", 15, "22:00", "mo"))
        f = self.v(kind="time", time="07:00", action="position", position=40, days=None, date="2026-10-14")
        self.assertEqual((f["time"], f["position"], f["date"]), ("07:00", 40, "2026-10-14"))

    def test_rejected(self):
        for kw in ({"shutter_id": "wohnen"},                       # openHAB-Regel zuständig
                   {"shutter_id": "nope"},
                   {"action": "stop"}, {"action": "position", "position": 120},
                   {"kind": "noon"}, {"kind": "time", "time": "25:00"}, {"kind": "time"},
                   {"offset_min": 300}, {"offset_min": 1.5},
                   {"not_before": "22:00", "not_after": "20:00"},
                   {"loc": None},
                   {"days": None}, {"days": ["xx"]}, {"date": "2026-10-14"},
                   {"days": None, "date": "2026-10-12"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.v(**kw)


class RunCase(DbCase):
    def setUp(self):
        super().setUp()
        self.c = Client()
        self.logs = []
        self.sleeps = []

    def run_at(self, hh, mm, day=13, cfg=None, dry=False, **kw):
        now = datetime(2026, 10, day, hh, mm, tzinfo=TZ)
        return ss.run(cfg or raw(), dry_run=dry, now=now, client=self.c, sleep=self.sleeps.append,
                      app_log=lambda m, level=logging.INFO: self.logs.append(m), **kw)

    def text(self):
        return "\n".join(self.logs)


class RunBasicsTest(RunCase):
    def test_switch_off_no_db_no_request(self):
        self.add()
        res = self.run_at(21, 0, cfg=raw(schedules_enabled=False))
        self.assertFalse(res.active)
        self.assertEqual(self.c.calls, [])

    def test_nothing_due_no_request(self):
        self.add(time="22:00")
        res = self.run_at(21, 0)
        self.assertEqual((res.due, res.requests), (0, 0))
        self.assertEqual(self.c.calls, [])

    def test_sends_at_time_and_records(self):
        e = self.add(time="21:00")
        res = self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])
        self.assertEqual(res.sent, 1)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.SENT)
        cmd = shutters.last_commands()["schlafen"]
        self.assertEqual((cmd["source"], cmd["target"], cmd["status"]), ("Zeitplan", 100, "pending"))
        self.run_at(21, 1)                                  # nicht doppelt
        self.assertEqual(len(self.c.sends), 1)

    def test_sunset_offset(self):
        self.add(kind="sunset", offset_min=15)              # 18:37 + 15 = 18:52
        self.run_at(18, 51)
        self.assertEqual(self.c.sends, [])
        self.run_at(18, 52)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_catch_up_15_min(self):
        self.add(time="21:00")
        self.run_at(21, 15)
        self.assertEqual(len(self.c.sends), 1)
        self.c = Client()
        e = self.add(shutter_id="terrasse", time="20:00")
        self.run_at(20, 16)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.SKIPPED)
        self.assertIn("16 min verspätet", self.text())

    def test_latest_wins(self):
        a = self.add(time="21:00", action="up")
        b = self.add(time="21:02", action="position", position=50)
        self.run_at(21, 3)
        self.assertEqual(self.c.sends, [("S_Roll", "50")])
        self.assertEqual(ss.get_schedule(a["id"])["last_result"], ss.SUPERSEDED)
        self.assertEqual(ss.get_schedule(b["id"])["last_result"], ss.SENT)

    def test_unchanged(self):
        self.c.items["S_Roll"]["state"] = "100"
        e = self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.UNCHANGED)

    def test_openhab_rule_shutter_never_scheduled(self):
        with sqlite3.connect(db.DB_PATH) as conn:              # z. B. vor dem Umstellen auf „openHAB-Regel“ angelegt
            ss.init_tables()
            conn.execute("INSERT INTO shutter_schedule (shutter_id, action, kind, time, offset_min, days, enabled, "
                         "created_at) VALUES ('wohnen', 'down', 'time', '21:00', 0, 'mo,di,mi,do,fr,sa,so', 1, "
                         "'2026-10-01T00:00:00+02:00')")
        res = self.run_at(21, 0)
        self.assertEqual((res.due, self.c.calls), (0, []))

    def test_dry_run_never_sends(self):
        e = self.add()
        self.run_at(21, 0, dry=True)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.DRY_RUN)
        self.assertIn("DRY-RUN: Schlafzimmer → zu", self.text())

    def test_once_disabled_after_run(self):
        e = self.add(days=None, date="2026-10-13", time="07:00", action="up")
        self.c.items["S_Roll"]["state"] = "100"
        self.run_at(7, 0)
        self.assertFalse(ss.get_schedule(e["id"])["enabled"])

    def test_openhab_down_stays_due(self):
        self.add()
        self.c.down = True
        self.run_at(21, 0)
        self.assertIn("nicht erreichbar", self.text())
        self.c.down = False
        self.run_at(21, 1)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_send_error_retry_once_then_failed(self):
        e = self.add()
        self.c.send_fail = {"S_Roll"}
        self.run_at(21, 0)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.RETRY)
        self.run_at(21, 1)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.FAILED)
        self.run_at(21, 2)
        self.assertEqual(len(self.c.sends), 2)

    def test_no_coordinates(self):
        e = self.add(kind="sunset")
        cfg = raw()
        del cfg["latitude"]
        self.run_at(23, 0, cfg=cfg)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.NO_SUN)
        self.assertEqual(self.c.sends, [])


class DstRunTest(RunCase):
    """Nacht 24./25.10.2026: 03:00 CEST → 02:00 CET."""

    def utc(self, h, m):
        return datetime(2026, 10, 25, h, m, 20, tzinfo=timezone.utc)

    def test_late_across_fall_back_skipped(self):
        e = self.add(time="02:50")
        self.c.down = True
        t = self.utc(0, 40)
        while t < self.utc(2, 0):
            self.c.down = t < self.utc(1, 9)
            ss.run(raw(), dry_run=False, now=t, client=self.c, sleep=self.sleeps.append,
                   app_log=lambda m, level=0: self.logs.append(m))
            t += timedelta(minutes=1)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.SKIPPED)

    def test_sunrise_on_switch_day_once(self):
        """Sonnenaufgang 25.10. (06:55 CET): genau einmal, zur richtigen Zeit."""
        self.c.items["S_Roll"]["state"] = "100"
        self.add(kind="sunrise", action="up", time=None)
        sent = []
        t = datetime(2026, 10, 25, 4, 0, 10, tzinfo=timezone.utc)
        while t < datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc):
            n = len(self.c.sends)
            ss.run(raw(), dry_run=False, now=t, client=self.c, sleep=self.sleeps.append,
                   app_log=lambda m, level=0: None)
            if len(self.c.sends) > n:
                sent.append(t.astimezone(TZ).strftime("%H:%M %Z"))
            t += timedelta(minutes=1)
        self.assertEqual(len(sent), 1)
        self.assertIn(sent[0][:5], ("06:54", "06:55", "06:56"))
        self.assertTrue(sent[0].endswith("CET"))

    def test_manual_change_window_across_fall_back(self):
        """Eigener Befehl 02:58 CEST, Positionsmeldung 02:01 CET (3 min später) → eigener, nicht „von Hand“."""
        s = datetime(2026, 10, 25, 0, 58, tzinfo=timezone.utc).astimezone(TZ)
        t1 = int(datetime(2026, 10, 25, 0, 55, tzinfo=timezone.utc).timestamp() * 1000)
        t2 = int(datetime(2026, 10, 25, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        since = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(TZ)
        self.assertIsNone(ss.manual_change([(t1, 0), (t2, 100)], [(s, 60)], since))


class StaggerAndDutyCycleTest(RunCase):
    def test_staggered(self):
        self.c.items["T_Roll"]["state"] = "100"
        self.c.items["H_Roll"]["state"] = "100"
        for sid in ("schlafen", "terrasse", "hinten"):
            self.c.items[{"schlafen": "S_Roll", "terrasse": "T_Roll", "hinten": "H_Roll"}[sid]]["state"] = "100"
            self.add(shutter_id=sid, time="07:00", action="up")
        self.run_at(7, 0)
        self.assertEqual([s[0] for s in self.c.sends], ["S_Roll", "T_Roll", "H_Roll"])
        self.assertEqual(self.sleeps, [2.0, 2.0])

    def test_budget_leaves_rest_due(self):
        for item, sid in (("S_Roll", "schlafen"), ("T_Roll", "terrasse"), ("H_Roll", "hinten")):
            self.c.items[item]["state"] = "100"
            self.add(shutter_id=sid, time="07:00", action="up")
        t = [0.0]

        def clock():
            return t[0]

        def sleep(s):
            t[0] += s
        res = ss.run(raw(), dry_run=False, now=datetime(2026, 10, 13, 7, 0, tzinfo=TZ), client=self.c,
                     budget_s=3.0, clock=clock, sleep=sleep, app_log=lambda m, level=0: self.logs.append(m))
        self.assertEqual(len(self.c.sends), 2)
        self.assertTrue(res.timed_out)
        self.c.calls.clear()
        self.run_at(7, 1)                                      # Rest in der nächsten Minute
        self.assertEqual([s[0] for s in self.c.sends], ["H_Roll"])

    def test_duty_cycle_high_pauses(self):
        self.add()
        self.c.items["CCU_DC"]["state"] = "85"
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertIn("Duty Cycle 85", self.text())
        self.c.items["CCU_DC"]["state"] = "40"
        self.run_at(21, 5)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])


class DoorTest(RunCase):
    def test_open_door_skips_closing_no_catch_up(self):
        self.c.items["T_Door"]["state"] = "OPEN"
        e = self.add(shutter_id="terrasse")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.DOOR_OPEN)
        self.assertIn("Tür offen", ss.hints(datetime(2026, 10, 13, 21, 1, tzinfo=TZ))["terrasse"])
        self.c.items["T_Door"]["state"] = "CLOSED"
        self.run_at(21, 5)
        self.assertEqual(self.c.sends, [])                     # kein Nachholen

    def test_unknown_contact_counts_as_open(self):
        self.c.items["T_Door"]["state"] = "NULL"
        self.add(shutter_id="terrasse")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertIn("Kontakt unbekannt", self.text())

    def test_open_door_does_not_block_opening(self):
        self.c.items["T_Door"]["state"] = "OPEN"
        self.c.items["T_Roll"]["state"] = "100"
        self.add(shutter_id="terrasse", action="up", time="07:00")
        self.run_at(7, 0)
        self.assertEqual(self.c.sends, [("T_Roll", "UP")])

    def test_retry_when_closed(self):
        rooms = [dict(r, retry_when_closed=True) if r["id"] == "terrasse" else r for r in ROOMS]
        cfg = raw(rooms=rooms)
        self.c.items["T_Door"]["state"] = "OPEN"
        e = self.add(shutter_id="terrasse")
        self.run_at(21, 0, cfg=cfg)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.WAITING_DOOR)
        self.run_at(21, 30, cfg=cfg)                           # Tür noch offen → weiter warten
        self.assertEqual(self.c.sends, [])
        self.assertIn("Wartet auf geschlossene Tür", ss.hints(datetime(2026, 10, 13, 21, 31, tzinfo=TZ))["terrasse"])
        self.c.items["T_Door"]["state"] = "CLOSED"
        self.run_at(22, 10, cfg=cfg)                           # über 15 min später: bewusst nachgeholt
        self.assertEqual(self.c.sends, [("T_Roll", "DOWN")])
        self.assertEqual(ss.door_waits(), {})
        self.run_at(22, 11, cfg=cfg)
        self.assertEqual(len(self.c.sends), 1)

    def test_retry_wait_expires_after_12h(self):
        rooms = [dict(r, retry_when_closed=True) if r["id"] == "terrasse" else r for r in ROOMS]
        cfg = raw(rooms=rooms)
        self.c.items["T_Door"]["state"] = "OPEN"
        self.add(shutter_id="terrasse", time="20:00", days="mo")       # 12.10. ist ein Montag
        self.run_at(20, 0, day=12, cfg=cfg)
        self.c.items["T_Door"]["state"] = "CLOSED"
        self.run_at(8, 30, day=13, cfg=cfg)
        self.assertEqual(self.c.sends, [])
        self.assertIn("nach 12 h beendet", self.text())

    def test_manual_command_ends_wait(self):
        ss._set_wait("terrasse", 1, 100, datetime(2026, 10, 13, 21, tzinfo=TZ))
        ss.clear_wait("terrasse")
        self.assertEqual(ss.door_waits(), {})

    def test_door_without_contact_gap(self):
        self.add(shutter_id="hinten")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("H_Roll", "80")])
        self.assertIn("nur bis 80 %", self.text())

    def test_door_without_contact_full_if_configured(self):
        rooms = [dict(r, auto_close_max=100) if r["id"] == "hinten" else r for r in ROOMS]
        self.add(shutter_id="hinten")
        self.run_at(21, 0, cfg=raw(rooms=rooms))
        self.assertEqual(self.c.sends, [("H_Roll", "DOWN")])


class UncertainPositionTest(RunCase):
    """13.10.2026 (Prod): ▼ Ab + ■ Stop am Shelly → openHAB meldet weiter 100 (autoupdate), Rollladen steht ~halb."""

    def test_after_stop_door_guard_still_applies(self):
        sh = self.cfg.shutter("terrasse")
        shutters.record_command(sh, "down", 100, "manuell", datetime(2026, 10, 13, 20, 40, tzinfo=TZ))
        shutters.record_command(sh, "stop", None, "manuell", datetime(2026, 10, 13, 20, 40, 1, tzinfo=TZ))
        self.c.items["T_Roll"]["state"] = "100"               # veraltete Vorhersage
        self.c.items["T_Door"]["state"] = "OPEN"
        e = self.add(shutter_id="terrasse")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.DOOR_OPEN)   # nicht „steht schon dort“

    def test_after_stop_sends_instead_of_unchanged(self):
        sh = self.cfg.shutter("schlafen")
        shutters.record_command(sh, "stop", None, "manuell", datetime(2026, 10, 13, 20, 40, tzinfo=TZ))
        self.c.items["S_Roll"]["state"] = "100"
        self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_after_stop_frost_applies_to_up(self):
        sh = self.cfg.shutter("schlafen")
        shutters.record_command(sh, "stop", None, "manuell", datetime(2026, 10, 13, 6, 40, tzinfo=TZ))
        self.c.items["S_Roll"]["state"] = "0"                 # meldet „offen“, steht aber evtl. halb
        self.c.items["Out_T"]["state"] = "-3 °C"
        e = self.add(action="up", time="07:00")
        self.run_at(7, 0)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.FROST)

    def test_reliable_again_after_60_min(self):
        sh = self.cfg.shutter("schlafen")
        shutters.record_command(sh, "stop", None, "manuell", datetime(2026, 10, 13, 19, 50, tzinfo=TZ))
        self.c.items["S_Roll"]["state"] = "100"
        e = self.add()
        self.run_at(21, 0)
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.UNCHANGED)

    def test_status_marks_uncertain(self):
        sh = self.cfg.shutter("schlafen")
        now = datetime(2026, 10, 13, 13, 40, tzinfo=TZ)
        shutters.record_command(sh, "stop", None, "manuell", now - timedelta(minutes=2))
        st = shutters.read_status(self.cfg, self.c, now=now)
        r = next(x for x in st["rooms"] if x["id"] == "schlafen")
        self.assertIn("evtl. ungenau", r["uncertain"])
        st = shutters.read_status(self.cfg, self.c, now=now + timedelta(minutes=61))
        self.assertIsNone(next(x for x in st["rooms"] if x["id"] == "schlafen")["uncertain"])


class ShellySourceRunTest(RunCase):
    """Zeitpläne mit Ist-Position vom Shelly (Schlafzimmer) bzw. aus einem Lese-Item."""

    STOP = {"state": "stop", "current_pos": 47, "positioning": True, "is_valid": True}

    def cfg_with(self, **src):
        rooms = [dict(r, **src) if r["id"] in ("schlafen", "terrasse") else r for r in ROOMS]
        return raw(rooms=rooms)

    def run_shelly(self, hh, mm, reply, **kw):
        with mock.patch.object(shutters, "read_shelly", side_effect=[reply] * 5
                               if not isinstance(reply, Exception) else reply) as rs:
            res = self.run_at(hh, mm, cfg=self.cfg_with(position_source="shelly", shelly_url="http://s"), **kw)
        return res, rs

    def test_real_position_not_stale_item(self):
        """Item meldet veraltet 100, Shelly 53 % zu → „zu“ wird gesendet, nicht „steht schon dort“."""
        self.c.items["S_Roll"]["state"] = "100"
        e = self.add()
        self.run_shelly(21, 0, self.STOP)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.SENT)

    def test_unchanged_by_shelly(self):
        self.c.items["S_Roll"]["state"] = "0"                       # Item falsch, Shelly: zu
        e = self.add()
        self.run_shelly(21, 0, {**self.STOP, "current_pos": 0})
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.UNCHANGED)

    def test_door_guard_with_shelly_position(self):
        self.c.items["T_Roll"]["state"] = "100"                     # veraltet
        self.c.items["T_Door"]["state"] = "OPEN"
        e = self.add(shutter_id="terrasse")
        self.run_shelly(21, 0, self.STOP)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.DOOR_OPEN)

    def test_moving_waits_a_minute(self):
        e = self.add()
        self.run_shelly(21, 0, {**self.STOP, "state": "open"})
        self.assertEqual(self.c.sends, [])
        self.assertIsNone(ss.get_schedule(e["id"])["last_run"])    # bleibt fällig
        self.assertIn("fährt gerade", self.text())
        self.run_shelly(21, 1, self.STOP)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_shelly_unreachable_falls_back_to_unknown(self):
        self.c.items["S_Roll"]["state"] = "100"                     # würde sonst „steht schon dort“
        self.c.items["Out_T"]["state"] = "-2 °C"
        e = self.add(action="up", time="07:00")
        self.run_shelly(7, 0, shutters.ShellyError("timeout"))
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.FROST)  # unbekannt → Frostregel greift
        self.assertIn("Position aus openHAB", self.text())

    def test_history_from_read_item(self):
        rooms = [dict(r, position_source="item:S_Read") if r["id"] == "schlafen" else r for r in ROOMS]
        self.c.items["S_Read"] = {"name": "S_Read", "type": "Rollershutter", "state": "60"}
        self.c.history["S_Read"] = [(int(datetime(2026, 10, 13, 20, 30, tzinfo=TZ).timestamp() * 1000), 0),
                                    (int(datetime(2026, 10, 13, 20, 35, tzinfo=TZ).timestamp() * 1000), 60)]
        self.add()
        self.run_at(21, 0, cfg=raw(rooms=rooms))
        self.assertEqual(self.c.sends, [])
        self.assertIn(("history", "S_Read"), self.c.calls)
        self.assertIn("von Hand bewegt", self.text())


class FrostTest(RunCase):
    def setUp(self):
        super().setUp()
        self.c.items["S_Roll"]["state"] = "100"

    def test_frost_blocks_opening_only(self):
        self.c.items["Out_T"]["state"] = "-2.5 °C"
        e = self.add(action="up", time="07:00")
        self.run_at(7, 0)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.FROST)
        self.assertIn("-2,5 °C", self.text())
        self.c.items["T_Roll"]["state"] = "0"
        self.add(shutter_id="terrasse", action="down", time="21:00")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("T_Roll", "DOWN")])  # Schließen bleibt erlaubt

    def test_above_frost_opens(self):
        self.add(action="up", time="07:00")
        self.run_at(7, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "UP")])

    def test_unknown_temperature_skips_opening(self):
        self.c.items["Out_T"]["state"] = "NULL"
        self.add(action="up", time="07:00")
        self.run_at(7, 0)
        self.assertEqual(self.c.sends, [])
        self.assertIn("unbekannt", self.text())

    def test_weather_fallback(self):
        cfg = raw(frost_temp_item="")
        db.init_pv_logging_tables()
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO weather_log (date, temp_min) VALUES ('2026-10-13', -1.0)")
        self.add(action="up", time="07:00")
        self.run_at(7, 0, cfg=cfg)
        self.assertEqual(self.c.sends, [])
        self.assertIn("Prognose-Tagesminimum", self.text())

    def test_partial_open_counts_as_opening(self):
        self.c.items["Out_T"]["state"] = "-1 °C"
        self.add(action="position", position=40, time="07:00")
        self.run_at(7, 0)
        self.assertEqual(self.c.sends, [])


class ManualHoldTest(RunCase):
    """Positionsänderung ohne eigenen Befehl in den letzten 60 min → Zeitplan fährt nicht."""

    def ms(self, hh, mm, day=13):
        return int(datetime(2026, 10, day, hh, mm, tzinfo=TZ).timestamp() * 1000)

    def test_manual_move_blocks(self):
        self.c.history["S_Roll"] = [(self.ms(20, 10), 0), (self.ms(20, 30), 0), (self.ms(20, 31), 60),
                                    (self.ms(20, 59), 60)]
        self.c.items["S_Roll"]["state"] = "60"
        e = self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertEqual(ss.get_schedule(e["id"])["last_result"], ss.MANUAL_HOLD)
        self.assertIn("um 20:31 von Hand bewegt", self.text())

    def test_old_manual_move_ok(self):
        self.c.history["S_Roll"] = [(self.ms(19, 50), 0), (self.ms(19, 55), 60), (self.ms(20, 59), 60)]
        self.c.items["S_Roll"]["state"] = "60"
        self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_own_command_is_not_manual(self):
        sh = self.cfg.shutter("schlafen")
        shutters.record_command(sh, "position", 60, "manuell", datetime(2026, 10, 13, 20, 30, 30, tzinfo=TZ))
        # autoupdate (sofort Ziel), Zwischenwert, dann Endposition
        self.c.history["S_Roll"] = [(self.ms(20, 30), 0), (self.ms(20, 31), 60), (self.ms(20, 32), 30),
                                    (self.ms(20, 33), 60)]
        self.c.items["S_Roll"]["state"] = "60"
        self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])

    def test_small_jitter_ignored(self):
        self.c.history["S_Roll"] = [(self.ms(20, 30), 100), (self.ms(20, 31), 99), (self.ms(20, 32), 98)]
        self.c.items["S_Roll"]["state"] = "98"
        self.add(action="up", time="21:00")
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [("S_Roll", "UP")])

    def test_history_unreadable_skips(self):
        self.c.history_fail = True
        self.add()
        self.run_at(21, 0)
        self.assertEqual(self.c.sends, [])
        self.assertIn("nicht prüfbar", self.text())

    def test_hold_zero_disables_check(self):
        self.c.history_fail = True
        self.add()
        self.run_at(21, 0, cfg=raw(manual_hold_min=0))
        self.assertEqual(self.c.sends, [("S_Roll", "DOWN")])
        self.assertNotIn(("history", "S_Roll"), self.c.calls)


class NextRidesTest(DbCase):
    def test_next(self):
        self.add(kind="sunset", offset_min=15)
        self.add(time="07:00", action="up")
        n = ss.next_rides(ss.list_schedules(), datetime(2026, 10, 13, 12, tzinfo=TZ), LOC)["schlafen"]
        self.assertEqual(n["at"], "2026-10-13T18:52+02:00")
        self.assertEqual((n["action"], n["when"]), ("down", "Sonnenuntergang +15 min"))
        n = ss.next_rides(ss.list_schedules(), datetime(2026, 10, 13, 19, tzinfo=TZ), LOC)["schlafen"]
        self.assertEqual(n["at"], "2026-10-14T07:00+02:00")


class CronIsolationTest(unittest.TestCase):
    def test_shutter_error_does_not_change_rc(self):
        tmp = Path(tempfile.mkdtemp())
        old_db = db.DB_PATH
        db.DB_PATH = tmp / "t.db"
        root = logging.getLogger().handlers[:]
        cfg = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        cfg["runtime"]["log_file"] = str(tmp / "app.log")
        (tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        ctrl = mock.MagicMock()
        ctrl.return_value.run.return_value = SimpleNamespace(
            wallbox_status=None, readings=SimpleNamespace(storage_temp_c=50.0, heater_meter_power_w=0.0))
        try:
            with mock.patch.object(main_mod, "Controller", ctrl), mock.patch.object(main_mod, "CascadeService"), \
                    mock.patch("pv_logger.maybe_log"), mock.patch.object(db, "append_temp_history"), \
                    mock.patch.object(ss, "run", side_effect=RuntimeError("kaputt")):
                rc = main_mod.main(["--config", str(tmp / "config.json")])
            self.assertEqual(rc, 0)
            self.assertIn("Rollladen-Zeitpläne: Fehler", (tmp / "app.log").read_text(encoding="utf-8"))
        finally:
            for h in logging.getLogger().handlers:
                h.close()
            logging.getLogger().handlers[:] = root
            db.DB_PATH = old_db
            shutil.rmtree(tmp)


class WebScheduleTest(DbCase):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        example = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        example["runtime"]["log_file"] = str(self.dir / "app.log")
        example["runtime"]["dry_run"] = False
        example.update(raw(schedules_enabled=False))
        self.cfg_path.write_text(json.dumps(example), encoding="utf-8")
        self.c = Client()
        self.patches = [mock.patch.object(web, "CONFIG_PATH", self.cfg_path),
                        mock.patch.object(heating, "make_client", lambda r: self.c)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)
        super().tearDown()

    def test_create_list_toggle_delete(self):
        e = web.shutters_schedule_create(web._ShutterScheduleCreate(
            shutter_id="schlafen", action="down", kind="sunset", offset_min=15, days=["mo", "di"]))
        self.assertEqual((e["kind"], e["when"]), ("sunset", "Sonnenuntergang +15 min"))
        lst = web.shutters_schedules(web.Response())
        self.assertFalse(lst["schedules_enabled"])
        self.assertEqual(len(lst["items"]), 1)
        self.assertRegex(lst["sun_today"]["sunset"], r"^\d\d:\d\d$")
        self.assertFalse(web.shutters_schedule_patch(e["id"], web._ShutterSchedulePatch(enabled=False))["enabled"])
        self.assertTrue(web.shutters_schedule_delete(e["id"])["ok"])
        self.assertIn("Rollladen-Zeitplan gelöscht: Schlafzimmer Sonnenuntergang +15 min → zu",
                      (self.dir / "app.log").read_text(encoding="utf-8"))
        with self.assertRaises(web.HTTPException):
            web.shutters_schedule_delete(e["id"])

    def test_openhab_rule_shutter_schedule_is_saved_but_dormant(self):
        """Formular an jedem Rollladen: bei „Automatik: openHAB-Regel“ gespeichert, aber nicht ausgeführt."""
        e = web.shutters_schedule_create(web._ShutterScheduleCreate(
            shutter_id="wohnen", action="down", kind="sunset", offset_min=-15, days=list(ss.DAYS)))
        self.assertEqual(e["when"], "Sonnenuntergang −15 min")
        st = web.shutters_status(web.Response())
        self.assertIsNone(next(r for r in st["rooms"] if r["id"] == "wohnen")["next"])      # keine „Nächste Fahrt“
        c = json.loads(self.cfg_path.read_text()); c["shutters"]["schedules_enabled"] = True
        res = ss.run(c, dry_run=False, now=datetime(2026, 10, 13, 23, 0, tzinfo=TZ), client=self.c)
        self.assertEqual((res.due, self.c.sends), (0, []))                                  # Cron fährt ihn nie
        with self.assertRaises(ValueError):                                                 # Chat/Standard: weiter abgelehnt
            ss.validate_entry(shutters.parse_shutters(c), "wohnen", "down", "time", time="21:00", days=["mo"])

    def test_edit_and_list_with_time_today(self):
        e = web.shutters_schedule_create(web._ShutterScheduleCreate(
            shutter_id="schlafen", action="down", kind="sunset", offset_min=-15, not_after="22:00", days=["mo"]))
        lst = web.shutters_schedules(web.Response())["items"]
        self.assertRegex(lst[0]["at_today"], r"^\d\d:\d\d$")
        self.assertEqual(lst[0]["when"], "Sonnenuntergang −15 min (nicht nach 22:00)")
        ss._mark([e["id"]], ss.SENT, datetime.now(TZ))
        u = web.shutters_schedule_update(e["id"], web._ShutterScheduleCreate(
            shutter_id="schlafen", action="position", position=40, kind="time", time="23:59", days=["mo", "di"]))
        self.assertEqual((u["id"], u["kind"], u["time"], u["position"], u["days"], u["last_run"]),
                         (e["id"], "time", "23:59", 40, ["mo", "di"], None))
        self.assertIsNone(web.shutters_schedules(web.Response())["items"][0]["at_today"])
        self.assertIn("Rollladen-Zeitplan geändert: Schlafzimmer 23:59 → 40 %", (self.dir / "app.log").read_text(encoding="utf-8"))
        with self.assertRaises(web.HTTPException) as cm:
            web.shutters_schedule_update(999, web._ShutterScheduleCreate(shutter_id="schlafen", action="up", kind="time",
                                                                         time="07:00", days=["mo"]))
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(web.HTTPException) as cm:
            web.shutters_schedule_update(e["id"], web._ShutterScheduleCreate(shutter_id="schlafen", action="up",
                                                                             kind="sunrise", offset_min=999, days=["mo"]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_status_next_and_hint(self):
        web.shutters_schedule_create(web._ShutterScheduleCreate(
            shutter_id="schlafen", action="up", kind="time", time="07:00", days=list(ss.DAYS)))
        web.shutters_schedule_create(web._ShutterScheduleCreate(
            shutter_id="schlafen", action="up", kind="time", time="07:00", days=list(ss.DAYS)))
        st = web.shutters_status(web.Response())
        by = {r["id"]: r for r in st["rooms"]}
        self.assertEqual(by["schlafen"]["next"]["action"], "up")
        self.assertIsNone(by["wohnen"]["next"])
        ss._mark([1], ss.FROST, datetime.now(TZ), "Schlafzimmer: Frost")
        st = web.shutters_status(web.Response())
        self.assertEqual(next(r for r in st["rooms"] if r["id"] == "schlafen")["hint"], "Schlafzimmer: Frost")

    def test_config_put_schedule_switch(self):
        body = dict(enabled=True, rooms=ROOMS)
        web.shutters_config_put(web._ShuttersConfigUpdate(**body, schedules_enabled=True, frost_temp_c=1.5,
                                                          manual_hold_min=30))
        c = web.shutters_config_get()["shutters"]
        self.assertEqual((c["schedules_enabled"], c["frost_temp_c"], c["manual_hold_min"]), (True, 1.5, 30))
        web.shutters_config_put(web._ShuttersConfigUpdate(**body))          # ohne Felder → unverändert
        self.assertTrue(web.shutters_config_get()["shutters"]["schedules_enabled"])
        self.assertIn("Rollladen-Zeitpläne aktiviert", (self.dir / "app.log").read_text(encoding="utf-8"))
        with self.assertRaises(web.HTTPException):
            web.shutters_config_put(web._ShuttersConfigUpdate(**body, frost_temp_c=40))


if __name__ == "__main__":
    unittest.main()
