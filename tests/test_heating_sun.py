"""Heizungs-Zeitpläne mit Zeittyp: Uhrzeit | Sonnenaufgang | Sonnenuntergang ± Offset, Grenzen.
Migration (bestehende Einträge bleiben „Uhrzeit“), Cron, Zeitumstellung 24./25.10.2026, Web, Chat."""
import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import db
import heating
import heating_chat as hc
import heating_schedule as hs
import suntime
import web
from tests import test_heating as th
from tests.test_heating_chat import KEY, FakeAPI
from tests.test_heating_schedule import SchedClient

TZ = hs.TZ
UTC = timezone.utc
LOC = (48.0628, 9.6071)
TUE = date(2026, 10, 13)                    # Sonnenaufgang 07:37, Sonnenuntergang 18:37
NOW = datetime(2026, 10, 13, 12, 30, tzinfo=TZ)
DAILY = list(hs.DAYS)


def raw(**extra):
    r = th.raw_cfg(schedules_enabled=True, **extra)
    r["runtime"] = {"dry_run": False}
    r["latitude"], r["longitude"] = LOC
    return r


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        self.cfg = heating.parse_heating(raw())
        self.logs = []

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def v(self, **kw):
        args = dict(room_id="wohnen", setpoint=21, days=DAILY, loc=LOC, now=NOW)
        args.update(kw)
        return hs.validate_entry(self.cfg, **args)

    def add(self, created=None, **kw):
        return hs.create_schedule(self.v(**kw), now=created or datetime(2026, 1, 1, tzinfo=TZ))

    def run_at(self, now, client, r=None):
        return hs.run(r or raw(), dry_run=False, now=now, client=client,
                      app_log=lambda m, level=logging.INFO: self.logs.append(m))

    def text(self):
        return "\n".join(self.logs)


class MigrationTest(DbCase):
    OLD = """CREATE TABLE heating_schedule (id INTEGER PRIMARY KEY, room_id TEXT NOT NULL, setpoint REAL NOT NULL,
             time TEXT NOT NULL, days TEXT, date TEXT, enabled INTEGER DEFAULT 1, source TEXT, note TEXT,
             last_run TEXT, last_result TEXT, created_at TEXT)"""

    def test_old_table_and_rows_stay_time_entries(self):
        with sqlite3.connect(db.DB_PATH) as conn:                      # Stand vor der Erweiterung
            conn.execute(self.OLD)
            conn.execute("INSERT INTO heating_schedule (room_id, setpoint, time, days, enabled, source, created_at) "
                         "VALUES ('wohnen', 21.0, '07:00', 'mo,di,mi,do,fr', 1, 'ui', '2026-10-01T08:00:00+02:00')")
            conn.execute("INSERT INTO heating_schedule (room_id, setpoint, time, date, enabled, source, created_at) "
                         "VALUES ('kind', 19.0, '11:00', '2026-10-14', 1, 'chat', '2026-10-01T08:00:00+02:00')")
        rows = hs.list_schedules()                                      # löst die Migration aus
        self.assertEqual([(r["id"], r["room_id"], r["setpoint"], r["time"], r["days"], r["date"], r["source"])
                          for r in rows],
                         [(1, "wohnen", 21.0, "07:00", ["mo", "di", "mi", "do", "fr"], None, "ui"),
                          (2, "kind", 19.0, "11:00", [], "2026-10-14", "chat")])
        self.assertEqual([(r["kind"], r["offset_min"], r["not_before"], r["not_after"], r["when"]) for r in rows],
                         [("time", 0, None, None, "07:00"), ("time", 0, None, None, "11:00")])
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 7, 0, 30, tzinfo=TZ), c)     # alter Eintrag läuft wie bisher
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertIn("Wohnen Soll → 21 °C (Zeitplan 07:00)", self.text())
        hs.init_tables()                                                # zweimal migrieren schadet nicht
        self.assertEqual(len(hs.list_schedules()), 2)

    def test_old_style_fields_still_create_time_entries(self):
        e = hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": "mo", "date": None})
        self.assertEqual((e["kind"], e["time"], e["when"]), ("time", "06:30", "06:30"))
        f = hs.validate_entry(self.cfg, "wohnen", 21, "06:30", days=["mo"])   # alte Aufrufform ohne kind/loc
        self.assertEqual((f["kind"], f["time"], f["offset_min"]), ("time", "06:30", 0))


class ValidateTest(DbCase):
    def test_sun_entries(self):
        f = self.v(kind="sunrise", offset_min=-30)
        self.assertEqual((f["kind"], f["time"], f["offset_min"]), ("sunrise", "", -30))
        f = self.v(kind="sunset", offset_min=45, not_before="18:00", not_after="22:00")
        self.assertEqual((f["offset_min"], f["not_before"], f["not_after"]), (45, "18:00", "22:00"))
        self.assertEqual(hs.when({**f, "days": DAILY}), "Sonnenuntergang +45 min (nicht vor 18:00, nicht nach 22:00)")

    def test_rejected(self):
        for kw in ({"kind": "sunrise", "offset_min": 181}, {"kind": "sunrise", "offset_min": -181},
                   {"kind": "sunset", "offset_min": 1.5}, {"kind": "noon"},
                   {"kind": "sunset", "not_before": "22:00", "not_after": "20:00"},
                   {"kind": "sunset", "not_before": "25:00"}, {"kind": "sunrise", "loc": None},
                   {"kind": "time"}, {"kind": "time", "time": "7 Uhr"},
                   {"kind": "sunrise", "days": None, "date": "2026-10-13"},      # Aufgang heute schon vorbei
                   {"kind": "sunset", "setpoint": 35}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.v(**kw)
        self.v(kind="sunset", days=None, date="2026-10-13")                       # Untergang heute kommt noch


class InstantTest(DbCase):
    def at(self, day, **kw):
        return hs.instant({**self.v(**kw), "days": DAILY}, day, LOC)

    def test_offsets_plus_minus(self):
        self.assertEqual(self.at(TUE, kind="sunrise").strftime("%H:%M"), "07:37")
        self.assertEqual(self.at(TUE, kind="sunrise", offset_min=-30).strftime("%H:%M"), "07:07")
        self.assertEqual(self.at(TUE, kind="sunrise", offset_min=30).strftime("%H:%M"), "08:07")
        self.assertEqual(self.at(TUE, kind="sunset", offset_min=-15).strftime("%H:%M"), "18:22")
        self.assertEqual(self.at(TUE, kind="sunset", offset_min=15).strftime("%H:%M"), "18:52")

    def test_limits(self):
        self.assertEqual(self.at(TUE, kind="sunrise", offset_min=-120, not_before="06:30").strftime("%H:%M"), "06:30")
        self.assertEqual(self.at(TUE, kind="sunset", offset_min=120, not_after="20:00").strftime("%H:%M"), "20:00")
        self.assertEqual(self.at(TUE, kind="sunset", not_before="19:00").strftime("%H:%M"), "19:00")
        self.assertEqual(self.at(TUE, kind="sunset", not_before="18:00", not_after="22:00").strftime("%H:%M"), "18:37")

    def test_no_coordinates(self):
        self.assertIsNone(hs.instant({**self.v(kind="sunset"), "days": DAILY}, TUE, None))

    def test_same_sun_calculation_as_shutters(self):
        import shutter_schedule as ss
        self.assertIs(ss.sun_times, suntime.sun_times)
        e = {**self.v(kind="sunset", offset_min=-15), "days": DAILY}
        self.assertEqual(hs.instant(e, TUE, LOC), ss.instant(e, TUE, LOC))


class RunTest(DbCase):
    def test_sunset_minus_15_runs_at_computed_minute(self):
        e = self.add(kind="sunset", offset_min=-15)                     # 18:22
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 18, 21, 30, tzinfo=TZ), c)
        self.assertEqual(c.calls, [])                                   # nicht fällig → kein openHAB-Request
        self.run_at(datetime(2026, 10, 13, 18, 22, 30, tzinfo=TZ), c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertIn("Wohnen Soll → 21 °C (Zeitplan Sonnenuntergang −15 min)", self.text())
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SENT)
        c.calls.clear()
        self.run_at(datetime(2026, 10, 13, 18, 40, 30, tzinfo=TZ), c)
        self.assertEqual(c.sends, [])                                   # heute nur einmal

    def test_sunrise_plus_30_and_catch_up(self):
        e = self.add(kind="sunrise", offset_min=30)                     # 08:07
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 8, 30, 30, tzinfo=TZ), c)    # 23 min zu spät
        self.assertEqual(c.sends, [])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SKIPPED)
        self.assertIn("Sonnenaufgang +30 min → 21 °C (23 min verspätet", self.text())

    def test_limit_not_before(self):
        self.add(kind="sunrise", offset_min=-120, not_before="06:30")
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 6, 29, 30, tzinfo=TZ), c)
        self.assertEqual(c.sends, [])
        self.run_at(datetime(2026, 10, 13, 6, 30, 30, tzinfo=TZ), c)
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_latest_wins_between_time_and_sun(self):
        a = self.add(kind="time", time="18:20", setpoint=19)
        b = self.add(kind="sunset", offset_min=-15, setpoint=22)        # 18:22
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 18, 23, 30, tzinfo=TZ), c)
        self.assertEqual(c.sends, [("W_Set", "22")])
        self.assertEqual(hs.get_schedule(a["id"])["last_result"], hs.SUPERSEDED)
        self.assertEqual(hs.get_schedule(b["id"])["last_result"], hs.SENT)

    def test_no_coordinates_skips_with_log(self):
        e = self.add(kind="sunset")
        r = raw()
        del r["latitude"]
        c = SchedClient()
        self.run_at(datetime(2026, 10, 13, 20, 0, 30, tzinfo=TZ), c, r=r)
        self.assertEqual(c.sends, [])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SKIPPED)
        self.assertIn("latitude/longitude fehlen", self.text())

    def test_next_change_shows_type(self):
        self.add(kind="sunset", offset_min=-15)
        self.add(kind="time", time="22:00", setpoint=17)
        n = hs.next_changes(hs.list_schedules(), NOW, loc=LOC)["wohnen"]
        self.assertEqual((n["at"], n["when"], n["setpoint"]), ("2026-10-13T18:22+02:00", "Sonnenuntergang −15 min", 21.0))
        n = hs.next_changes(hs.list_schedules(), datetime(2026, 10, 13, 19, tzinfo=TZ), loc=LOC)["wohnen"]
        self.assertEqual((n["at"], n["when"]), ("2026-10-13T22:00+02:00", "22:00"))


class DstTest(DbCase):
    """Nacht 24./25.10.2026 (So 25.10. 03:00 MESZ → 02:00 MEZ): Sonnenaufgang 24.: 07:54 MESZ, 25.: 06:55 MEZ."""

    def test_sunrise_minus_30_once_per_day_at_right_time(self):
        self.add(kind="sunrise", offset_min=-30)
        self.add(kind="sunset", offset_min=15, setpoint=18)
        c = SchedClient()
        c.echo = False
        sent = []
        t = datetime(2026, 10, 23, 22, 0, 10, tzinfo=UTC)               # Sa 24.10. 00:00 MESZ
        while t < datetime(2026, 10, 26, 23, 0, tzinfo=UTC):            # bis Mo 26.10. 24:00 MEZ
            n = len(c.sends)
            self.run_at(t, c)
            if len(c.sends) > n:
                sent.append(t.astimezone(TZ).strftime("%d. %H:%M %Z") + " " + c.sends[-1][1])
            hs.clear_verify("wohnen")
            c.sp["W_Set"] = 17.5
            t += timedelta(minutes=1)
        self.assertEqual(sent, ["24. 07:24 CEST 21", "24. 18:31 CEST 18",
                                "25. 06:25 CET 21", "25. 17:29 CET 18",
                                "26. 06:27 CET 21", "26. 17:28 CET 18"])

    def test_limit_in_doubled_hour(self):
        """„nicht nach 02:30“ am 25.10.: erstes 02:30 (MESZ) – der Eintrag läuft genau einmal."""
        self.add(kind="sunrise", not_after="02:30")
        c = SchedClient()
        c.echo = False
        sent = []
        t = datetime(2026, 10, 25, 0, 0, 10, tzinfo=UTC)                # 02:00 MESZ
        while t < datetime(2026, 10, 25, 3, 0, tzinfo=UTC):
            n = len(c.sends)
            self.run_at(t, c)
            if len(c.sends) > n:
                sent.append(t.astimezone(UTC).strftime("%H:%M"))
            hs.clear_verify("wohnen")
            c.sp["W_Set"] = 17.5
            t += timedelta(minutes=1)
        self.assertEqual(sent, ["00:30"])                               # 02:30 MESZ, nicht noch einmal um 02:30 MEZ


class WebTest(unittest.TestCase):
    setUp_web = th.WebTest.setUp
    tearDown_web = th.WebTest.tearDown

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        self.setUp_web()
        c = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        c["latitude"], c["longitude"] = LOC
        c["assistant"] = {"enabled": True, "provider": "anthropic", "api_key": KEY, "max_requests_per_day": 20}
        self.cfg_path.write_text(json.dumps(c), encoding="utf-8")
        self.api = FakeAPI()
        self.p2 = [mock.patch.object(hc, "_default_client", lambda a: self.api),
                   mock.patch.object(hc, "sdk_available", lambda *a: True)]
        for p in self.p2:
            p.start()
        hc._PROPOSALS.clear()
        self.tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()

    def tearDown(self):
        for p in self.p2:
            p.stop()
        self.tearDown_web()
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def log_text(self):
        return (self.dir / "app.log").read_text(encoding="utf-8")

    def test_create_list_edit(self):
        e = web.heating_schedule_create(web._HeatingScheduleCreate(
            room_id="wohnen", setpoint=21, kind="sunset", offset_min=-15, not_after="22:00", days=["mo", "di"]))
        self.assertEqual((e["kind"], e["offset_min"], e["when"]),
                         ("sunset", -15, "Sonnenuntergang −15 min (nicht nach 22:00)"))
        self.assertIn("Zeitplan angelegt: Wohnen Sonnenuntergang −15 min (nicht nach 22:00) → 21 °C (mo,di)",
                      self.log_text())
        lst = web.heating_schedules(web.Response())
        self.assertRegex(lst["items"][0]["at_today"], r"^\d\d:\d\d$")
        self.assertRegex(lst["sun_today"]["sunrise"], r"^\d\d:\d\d$")
        old = web.heating_schedule_create(web._HeatingScheduleCreate(room_id="kind", time="06:30", setpoint=20,
                                                                    days=["mo"]))       # alte Aufrufform
        self.assertEqual((old["kind"], old["time"]), ("time", "06:30"))
        self.assertIsNone(next(i for i in web.heating_schedules(web.Response())["items"]
                               if i["id"] == old["id"])["at_today"])
        hs._mark([e["id"]], hs.SENT, datetime.now(TZ))
        u = web.heating_schedule_update(e["id"], web._HeatingScheduleCreate(
            room_id="wohnen", setpoint=19.5, kind="sunrise", offset_min=30, days=list(hs.DAYS)))
        self.assertEqual((u["id"], u["kind"], u["offset_min"], u["setpoint"], u["not_after"], u["last_run"]),
                         (e["id"], "sunrise", 30, 19.5, None, None))
        self.assertIn("Zeitplan geändert: Wohnen Sonnenaufgang +30 min → 19.5 °C", self.log_text())
        for body, code in (({"room_id": "wohnen", "setpoint": 21, "kind": "sunset", "offset_min": 500, "days": ["mo"]}, 400),
                           ({"room_id": "wohnen", "setpoint": 21, "kind": "time", "days": ["mo"]}, 400)):
            with self.subTest(body=body), self.assertRaises(web.HTTPException) as cm:
                web.heating_schedule_update(e["id"], web._HeatingScheduleCreate(**body))
            self.assertEqual(cm.exception.status_code, code)
        with self.assertRaises(web.HTTPException) as cm:
            web.heating_schedule_update(999, web._HeatingScheduleCreate(room_id="wohnen", time="07:00", setpoint=21,
                                                                        days=["mo"]))
        self.assertEqual(cm.exception.status_code, 404)

    def test_status_next_has_type(self):
        web.heating_schedule_create(web._HeatingScheduleCreate(
            room_id="wohnen", setpoint=21, kind="sunrise", offset_min=-30, days=list(hs.DAYS)))
        w = next(r for r in web.heating_status(web.Response())["rooms"] if r["id"] == "wohnen")
        self.assertEqual(w["next"]["when"], "Sonnenaufgang −30 min")

    def test_chat_30_min_before_sunrise(self):
        """„Wohnen 30 min vor Sonnenaufgang auf 21“ → Vorschau mit Zeittyp und berechneter Uhrzeit, dann speichern."""
        self.api.tool_input = {"entries": [{"room_id": "wohnen", "setpoint": 21, "kind": "sunrise", "time": None,
                                            "offset_min": -30, "not_before": "05:30", "not_after": None,
                                            "date": None, "days": list(hs.DAYS)}],
                               "immediate": [], "delete_ids": [], "question": None, "summary": None,
                               "shutter_entries": [], "shutter_immediate": [], "shutter_delete_ids": []}
        res = web.heating_chat(web._ChatRequest(messages=[web._ChatMessage(
            role="user", content="Wohnen jeden Tag 30 min vor Sonnenaufgang auf 21, aber nicht vor halb sechs")]))
        self.assertEqual(res["type"], "proposal")
        row = res["proposal"]["groups"][0]["rows"][0]
        self.assertEqual((row["room"], row["setpoint"], row["when"]),
                         ("Wohnen", 21.0, "Sonnenaufgang −30 min (nicht vor 05:30)"))
        self.assertRegex(row["at"], r"morgen \d\d:\d\d")
        self.assertRegex(row["time"], r"^\d\d:\d\d$")
        self.assertIn("Sonne", self.api.calls[0]["system"])
        out = web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        saved = hs.get_schedule(out["created"][0])
        self.assertEqual((saved["kind"], saved["offset_min"], saved["not_before"], saved["source"]),
                         ("sunrise", -30, "05:30", "chat"))
        self.assertIn("Zeitplan angelegt (Chat): Wohnen Sonnenaufgang −30 min (nicht vor 05:30) → 21 °C", self.log_text())

    def test_tool_schema_strict(self):
        item = hc.TOOL["input_schema"]["properties"]["entries"]["items"]
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertEqual(item["properties"]["kind"]["enum"], ["time", "sunrise", "sunset"])
        self.assertIn("Sonnenaufgang", hc.SYSTEM_PROMPT)


class ChatHelpersTest(DbCase):
    def test_preview_and_local_answer(self):
        p, err = hc.validate_proposal(self.cfg, {"entries": [
            {"room_id": "wohnen", "setpoint": 21, "kind": "sunset", "time": None, "offset_min": -15,
             "not_before": None, "not_after": None, "date": None, "days": DAILY},
            {"room_id": "kind", "setpoint": 19, "time": "11:00", "date": "2026-10-14", "days": None}]}, NOW, LOC)
        self.assertEqual(err, [])
        pv = hc.preview(self.cfg, p, NOW, LOC)
        rows = {r["room"]: r for g in pv["groups"] for r in g["rows"]}
        self.assertEqual(rows["Wohnen"], {"time": "18:22", "room": "Wohnen", "setpoint": 21.0,
                                          "when": "Sonnenuntergang −15 min", "at": "heute 18:22, morgen 18:20"})
        self.assertEqual(rows["Kind"], {"time": "11:00", "room": "Kind", "setpoint": 19.0})   # Uhrzeit-Zeile wie bisher
        hs.create_schedule(p.entries[0], now=NOW - timedelta(days=1))
        a = hc.local_answer("Was ist für heute geplant?", self.cfg, hs.list_schedules(), NOW, LOC)
        self.assertIn("18:22  Wohnen → 21 °C · Sonnenuntergang −15 min", a)
        ctx = hc.build_context(self.cfg, [], hs.list_schedules(), NOW, LOC)
        self.assertIn("#1 wohnen Sonnenuntergang −15 min 21 °C wiederkehrend", ctx)
        self.assertIn("- 2026-10-13 Di 18:22 wohnen → 21 °C (#1, wiederkehrend, Sonnenuntergang −15 min)", ctx)


if __name__ == "__main__":
    unittest.main()
