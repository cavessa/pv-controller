"""Rollläden Phase 3: ein Chat für Heizung + Rollläden (API gemockt, wie beim Heizungs-Chat)."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import heating
import heating_chat as hc
import heating_schedule as hs
import shutter_chat as sc
import shutter_schedule as ss
import shutters
import web
from tests import test_heating as th
from tests.test_heating_chat import KEY, FakeAPI, FakeOpenAI

TZ = ss.TZ
NOW = datetime(2026, 10, 13, 12, 30, tzinfo=TZ)        # Dienstag
LOC = (48.0628, 9.6071)
ROOMS = [
    {"id": "wohnen_links", "name": "Wohnen links", "item": "WL_Roll", "device": "homematic"},
    {"id": "wohnen_rechts", "name": "Wohnen rechts (Straße)", "item": "WR_Roll", "device": "homematic"},
    {"id": "hintereingang", "name": "Hintereingang", "item": "H_Roll", "is_door": True, "device": "homematic"},
    {"id": "schlafzimmer", "name": "Schlafzimmer", "item": "S_Roll", "device": "shelly", "automation": "pvcontroller"},
]
SH_ITEMS = {n: {"name": n, "type": "Rollershutter", "state": "0"} for n in ("WL_Roll", "WR_Roll", "H_Roll", "S_Roll")}
RULES = [
    {"uid": "6d392afc96", "name": "Rollladen Wohnen runter Automatik", "status": {"status": "IDLE"},
     "triggers": [{"type": "timer.GenericCronTrigger", "configuration": {"cronExpression": "0 40 18 * * ? *"}}],
     "actions": [{"type": "core.ItemCommandAction", "configuration": {"command": "DOWN", "itemName": "WL_Roll"}},
                 {"type": "core.ItemCommandAction", "configuration": {"command": "DOWN", "itemName": "WR_Roll"}}]},
    {"uid": "1ed3deed21", "name": "Rollladen Wohnen hoch Automatik", "status": {"status": "IDLE"},
     "triggers": [{"type": "timer.TimeOfDayTrigger", "configuration": {"time": "06:50"}}],
     "actions": [{"type": "core.ItemCommandAction", "configuration": {"command": "UP", "itemName": "WL_Roll"}}]},
    {"uid": "75088ccfb7", "name": "Rollladen runter wenn nicht zuhause", "status": {"status": "IDLE"},
     "triggers": [{"type": "timer.GenericCronTrigger", "configuration": {"cronExpression": "0 */15 22-05 * * *"}}],
     "actions": [{"type": "script.ScriptAction", "configuration": {
         "script": "if (items.getItem('AndyZuhause').state == 'OFF') { items.getItem('H_Roll').sendCommand('DOWN'); }"}}]},
    {"uid": "off", "name": "Urlaub", "status": {"status": "UNINITIALIZED", "statusDetail": "DISABLED"},
     "triggers": [{"type": "timer.GenericCronTrigger", "configuration": {"cronExpression": "0 0 20 * * ? *"}}],
     "actions": [{"type": "core.ItemCommandAction", "configuration": {"command": "DOWN", "itemName": "S_Roll"}}]},
]


def raw_shutters(**extra):
    return {"enabled": True, "command_spacing_s": 2, "rooms": ROOMS, **extra}


def tool(entries=(), immediate=(), delete_ids=(), question=None, summary=None,
         s_entries=(), s_immediate=(), s_delete_ids=()):
    return {"entries": list(entries), "immediate": list(immediate), "delete_ids": list(delete_ids),
            "question": question, "summary": summary, "shutter_entries": list(s_entries),
            "shutter_immediate": list(s_immediate), "shutter_delete_ids": list(s_delete_ids)}


def s_entry(sid="schlafzimmer", action="down", kind="sunset", time=None, offset=0, nb=None, na=None,
            date_=None, days=None, position=None):
    return {"shutter_id": sid, "action": action, "position": position, "kind": kind, "time": time,
            "offset_min": offset, "not_before": nb, "not_after": na, "date": date_, "days": days}


def s_now(sid, action, position=None):
    return {"shutter_id": sid, "action": action, "position": position}


class Client(th.FakeClient):
    def __init__(self):
        super().__init__(items={**th.ITEMS, **SH_ITEMS})
        self.rules_calls = 0

    def get_rules(self):
        self.rules_calls += 1
        return RULES


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        self.scfg = shutters.parse_shutters({"shutters": raw_shutters()})
        sc._RULES_CACHE.update(t=0.0, rules=None)

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def check(self, inp, now=NOW):
        p = hc.Proposal(id="x")
        return p, sc.validate(self.scfg, p, inp, now, LOC)


class ToolSchemaTest(unittest.TestCase):
    def test_one_tool_strict_all_required(self):
        sch = hc.TOOL["input_schema"]
        self.assertEqual(hc.TOOL["name"], "propose_schedule")
        self.assertEqual(set(sch["required"]), set(sch["properties"]))          # strict: alles required
        for k in ("shutter_entries", "shutter_immediate"):
            item = sch["properties"][k]["items"]
            self.assertEqual(set(item["required"]), set(item["properties"]))
            self.assertFalse(item["additionalProperties"])
        self.assertIs(hc.OPENAI_TOOL["function"]["parameters"], sch)          # gleiches Schema für ChatGPT
        self.assertIn("Rollläden", hc.SYSTEM_PROMPT)


class ValidateTest(DbCase):
    def test_sunset_offset_not_before(self):
        """„Schlafzimmer jeden Tag 30 min nach Sonnenuntergang runter, aber nicht vor 18 Uhr“."""
        p, err = self.check(tool(s_entries=[s_entry(offset=30, nb="18:00", days=list(hs.DAYS))]))
        self.assertEqual(err, [])
        e = p.shutter_entries[0]
        self.assertEqual((e["kind"], e["offset_min"], e["not_before"], e["days"]),
                         ("sunset", 30, "18:00", "mo,di,mi,do,fr,sa,so"))
        pv = sc.preview(self.scfg, p, NOW, LOC, [])
        row = pv["groups"][0]["rows"][0]
        self.assertEqual(pv["groups"][0]["label"], "Täglich")
        self.assertEqual(row["when"], "Sonnenuntergang +30 min (nicht vor 18:00)")
        self.assertEqual(row["at"], "heute 19:07, morgen 19:05")                # 18:37 + 30 / 18:35 + 30
        self.assertEqual(row["target"], "▼ zu")

    def test_openhab_shutters_declined_friendly_rest_valid(self):
        """„morgen früh alle Rollläden um 7 hoch“ → Schlafzimmer geplant, die openHAB-Rollläden freundlich abgelehnt."""
        ents = [s_entry(sid, "up", "time", "07:00", date_="2026-10-14") for sid in
                ("wohnen_links", "wohnen_rechts", "hintereingang", "schlafzimmer")]
        p, err = self.check(tool(s_entries=ents))
        self.assertEqual(err, [])
        self.assertEqual([e["shutter_id"] for e in p.shutter_entries], ["schlafzimmer"])
        self.assertEqual(p.declined, [
            "Wohnen links wird über eine openHAB-Regel gesteuert – Zeitplan erst nach Umstellung.",
            "Wohnen rechts (Straße) wird über eine openHAB-Regel gesteuert – Zeitplan erst nach Umstellung.",
            "Hintereingang wird über eine openHAB-Regel gesteuert – Zeitplan erst nach Umstellung."])
        pv = sc.preview(self.scfg, p, NOW, LOC, [])
        self.assertEqual(pv["groups"][0]["label"], "Morgen (Mi 14.10.)")
        self.assertEqual(pv["groups"][0]["rows"][0]["at"], "")                          # Uhrzeit steht schon da
        p2, _ = self.check(tool(s_entries=[s_entry(kind="sunrise", offset=-15, date_="2026-10-14")]))
        self.assertEqual(sc.preview(self.scfg, p2, NOW, LOC, [])["groups"][0]["rows"][0]["at"], "07:24")

    def test_immediate_allowed_for_all(self):
        p, err = self.check(tool(s_immediate=[s_now("wohnen_links", "down"), s_now("schlafzimmer", "position", 50.0),
                                              s_now("hintereingang", "stop")]))
        self.assertEqual(err, [])
        self.assertEqual([(i["shutter_id"], i["action"], i["target"]) for i in p.shutter_immediate],
                         [("wohnen_links", "down", 100), ("schlafzimmer", "position", 50), ("hintereingang", "stop", None)])

    def test_door_without_contact_warns(self):
        p, _ = self.check(tool(s_immediate=[s_now("hintereingang", "down"), s_now("hintereingang", "up"),
                                            s_now("schlafzimmer", "down")]))
        st = shutters.read_status(self.scfg, Client(), NOW)["rooms"]
        pv = sc.preview(self.scfg, p, NOW, LOC, st)
        self.assertEqual([i["warn"] for i in pv["immediate"]],
                         ["Hintereingang: Tür ohne Kontakt – ist niemand draußen?", None, None])
        self.assertEqual(len(pv["door_confirm"]), 1)

    def test_errors(self):
        _, err = self.check(tool(s_entries=[s_entry(offset=500, days=["mo"]), s_entry(sid="garage", days=["mo"])],
                                 s_immediate=[s_now("schlafzimmer", "position", None), s_now("nope", "up")],
                                 s_delete_ids=[999]))
        self.assertEqual(len(err), 5)
        self.assertIn("Offset", err[0])
        self.assertIn("unbekannt", err[1])
        self.assertIn("Position", err[2])
        self.assertIn("#999", err[4])

    def test_shutters_disabled(self):
        self.scfg = shutters.parse_shutters({"shutters": raw_shutters(enabled=False)})
        _, err = self.check(tool(s_immediate=[s_now("schlafzimmer", "up")]))
        self.assertIn("nicht aktiv", err[0])

    def test_past_once_rejected(self):
        _, err = self.check(tool(s_entries=[s_entry(kind="time", time="07:00", date_="2026-10-13")]))
        self.assertIn("Vergangenheit", err[0])


class AmbiguityTest(DbCase):
    def amb(self, text, touched):
        return sc.ambiguity(text, self.scfg, set(touched))

    def test_wohnen_ambiguous(self):
        q = self.amb("Wohnen jetzt runter", ["wohnen_links", "wohnen_rechts"])
        self.assertEqual(q, "„Wohnen“ passt auf Wohnen links und Wohnen rechts (Straße). Welchen meinst du – oder beide?")
        self.assertIsNotNone(self.amb("Rollladen Wohnen um 8 hoch", ["wohnen_links"]))   # geraten → nachfragen

    def test_clear_cases(self):
        self.assertIsNone(self.amb("Wohnen links jetzt runter", ["wohnen_links"]))
        self.assertIsNone(self.amb("Wohnen Straße runter", ["wohnen_rechts"]))
        self.assertIsNone(self.amb("beide Wohnen runter", ["wohnen_links", "wohnen_rechts"]))
        self.assertIsNone(self.amb("Rollläden Wohnen jeden Tag bei Sonnenuntergang runter",
                                   ["wohnen_links", "wohnen_rechts"]))                  # Plural = beide
        self.assertIsNone(self.amb("Schlafzimmer jetzt auf 50 %", ["schlafzimmer"]))
        self.assertIsNone(self.amb("Wohnen runter", ["schlafzimmer"]))                  # betrifft Wohnen nicht


class RulesTest(unittest.TestCase):
    def test_cron_times(self):
        tue, sat = date(2026, 10, 13), date(2026, 10, 17)
        self.assertEqual(sc.cron_times("0 40 18 * * ? *", tue), ["18:40"])
        self.assertEqual(sc.cron_times("0 30 7 ? * MON-FRI *", tue), ["07:30"])
        self.assertEqual(sc.cron_times("0 30 7 ? * MON-FRI *", sat), [])
        self.assertEqual(sc.cron_times("0 10 19 ? * SAT-SUN *", sat), ["19:10"])
        t = sc.cron_times("0 */15 22-05 * * *", tue)
        self.assertEqual((t[0], t[-1], len(t)), ("00:00", "23:45", 32))
        self.assertIsNone(sc.cron_times("0 0 L * * ?", tue))

    def test_rule_rides_tonight(self):
        scfg = shutters.parse_shutters({"shutters": raw_shutters()})
        rides = sc.openhab_rule_rides(RULES, scfg, datetime(2026, 10, 13, 17, tzinfo=TZ),
                                      datetime(2026, 10, 14, 0, tzinfo=TZ))
        got = [(r["at"].strftime("%H:%M"), r["shutter"], r["target"]) for r in rides]
        self.assertIn(("18:40", "Wohnen links", "▼ zu"), got)
        self.assertIn(("18:40", "Wohnen rechts (Straße)", "▼ zu"), got)
        night = [r for r in rides if r["shutter"] == "Hintereingang"]
        self.assertEqual([r["at"].strftime("%H:%M") for r in night], ["22:00"])          # zusammengefasst
        self.assertIn("(22:00–23:45 mehrfach, mit Bedingungen)", night[0]["source"])
        self.assertFalse(any(r["shutter"] == "Schlafzimmer" for r in rides))             # deaktiviert/pvcontroller


class LocalAnswerTest(DbCase):
    def test_tonight_with_rules_and_schedule(self):
        ss.create_schedule({"shutter_id": "schlafzimmer", "action": "down", "position": None, "kind": "sunset",
                            "time": None, "offset_min": 15, "not_before": None, "not_after": None,
                            "days": ",".join(hs.DAYS), "date": None}, now=NOW - timedelta(days=1))
        a = sc.local_answer("Was fahren die Rollläden heute Abend?", self.scfg, ss.list_schedules(), NOW, LOC,
                            RULES, schedules_enabled=False)
        lines = a.splitlines()
        self.assertEqual(lines[0], "Heute Abend:")
        self.assertIn("  18:40  Wohnen links ▼ zu · openHAB-Regel „Rollladen Wohnen runter Automatik“", lines)
        self.assertIn("  18:52  Schlafzimmer ▼ zu · Zeitplan (Sonnenuntergang +15 min) – Zeitpläne aus", lines)
        self.assertLess(lines.index("  18:40  Wohnen links ▼ zu · openHAB-Regel „Rollladen Wohnen runter Automatik“"),
                        lines.index("  18:52  Schlafzimmer ▼ zu · Zeitplan (Sonnenuntergang +15 min) – Zeitpläne aus"))
        self.assertFalse(any("06:50" in l for l in lines))                              # früh gehört nicht dazu

    def test_tomorrow_morning_and_filter(self):
        a = sc.local_answer("wann geht Wohnen links morgen früh hoch?", self.scfg, [], NOW, LOC, RULES, True)
        self.assertEqual(a, "Morgen (Mi 14.10.) früh · Wohnen links:\n"
                            "  06:50  Wohnen links ▲ auf · openHAB-Regel „Rollladen Wohnen hoch Automatik“")

    def test_direction_filter(self):
        a = sc.local_answer("wann geht der Hintereingang morgen früh hoch?", self.scfg, [], NOW, LOC, RULES, True)
        self.assertEqual(a, "Morgen (Mi 14.10.) früh · Hintereingang: keine Fahrten geplant.")
        a = sc.local_answer("wann geht der Hintereingang morgen früh runter?", self.scfg, [], NOW, LOC, RULES, True)
        self.assertIn("00:00  Hintereingang ▼ zu", a)
        self.assertIn("(00:00–05:45 mehrfach, mit Bedingungen)", a)

    def test_not_a_query(self):
        for t in ("Schlafzimmer jetzt auf 50 %", "Rollläden morgen um 7 hoch", "Was ist für morgen geplant?",
                  "lösch den Rollladen-Zeitplan"):
            with self.subTest(t=t):
                self.assertIsNone(sc.local_answer(t, self.scfg, [], NOW, LOC, RULES, True))

    def test_rules_unreadable(self):
        a = sc.local_answer("was fahren die Rollläden heute?", self.scfg, [], NOW, LOC, None, True)
        self.assertIn("openHAB-Regeln nicht lesbar", a)


class WebChatTest(unittest.TestCase):
    """Ein Chat für Heizung + Rollläden über /api/heating/chat (API gemockt)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        hs.init_tables()
        self.dir = Path(self.tmp.name)
        self.cfg_path = self.dir / "config.json"
        cfg = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        cfg["runtime"]["log_file"] = str(self.dir / "app.log")
        cfg["runtime"]["dry_run"] = False
        cfg.update(th.raw_cfg())
        cfg["latitude"], cfg["longitude"] = LOC
        cfg["shutters"] = raw_shutters()
        cfg["assistant"] = {"enabled": True, "provider": "anthropic", "api_key": KEY, "max_requests_per_day": 20}
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.fake = Client()
        self.api = FakeAPI(tool())
        self.sleeps = []
        self.patches = [mock.patch.object(web, "CONFIG_PATH", self.cfg_path),
                        mock.patch.object(heating, "make_client", lambda raw: self.fake),
                        mock.patch.object(hc, "_default_client", lambda a: self.api),
                        mock.patch.object(hc, "sdk_available", lambda *a: True),
                        mock.patch.object(shutters._time, "sleep", self.sleeps.append)]
        for p in self.patches:
            p.start()
        hc._PROPOSALS.clear()
        sc._RULES_CACHE.update(t=0.0, rules=None)
        self.tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def chat(self, *texts):
        msgs = [web._ChatMessage(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts)]
        return web.heating_chat(web._ChatRequest(messages=msgs))

    def apply(self, res, confirm=False):
        return web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"], confirm_doors=confirm))

    def log_text(self):
        p = self.dir / "app.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_context_contains_shutters_and_sun(self):
        self.api.tool_input = tool(question="Welcher Rollladen?")
        self.chat("Rollladen um 8 hoch")
        system = self.api.calls[0]["system"]
        self.assertIn("- schlafzimmer: Schlafzimmer – Automatik PV Controller (Zeitpläne möglich), Position 0 %", system)
        self.assertIn("- wohnen_links: Wohnen links – Automatik openHAB-Regel (nur Sofort-Befehle, keine Zeitpläne)",
                      system)
        self.assertIn("Tür ohne Kontakt", system)
        self.assertRegex(system, r"Sonne \d{4}-\d\d-\d\d: Aufgang \d\d:\d\d, Untergang \d\d:\d\d")
        self.assertIn("- wohnen: Wohnen – Ist 18,4 °C, Soll 17,5 °C", system)               # Heizung weiter drin

    def test_wohnen_every_day_sunset_declined(self):
        """„Rollläden Wohnen jeden Tag bei Sonnenuntergang runter“ → freundlich abgelehnt (openHAB-Regel)."""
        self.api.tool_input = tool(s_entries=[s_entry(sid, days=list(hs.DAYS)) for sid in ("wohnen_links", "wohnen_rechts")])
        res = self.chat("Rollläden Wohnen jeden Tag bei Sonnenuntergang runter")
        self.assertEqual(res["type"], "answer")
        self.assertEqual(res["text"], "Wohnen links wird über eine openHAB-Regel gesteuert – Zeitplan erst nach "
                                      "Umstellung.\nWohnen rechts (Straße) wird über eine openHAB-Regel gesteuert – "
                                      "Zeitplan erst nach Umstellung.")
        self.assertEqual(hc._PROPOSALS, {})
        self.assertEqual(ss.list_schedules(), [])

    def test_all_tomorrow_7_up(self):
        """„morgen früh alle Rollläden um 7 hoch“: jeder einzeln, Schlafzimmer geplant, Rest als Info."""
        self.api.tool_input = tool(s_entries=[s_entry(sid, "up", "time", "07:00", date_=self.tomorrow) for sid in
                                              ("wohnen_links", "wohnen_rechts", "hintereingang", "schlafzimmer")])
        res = self.chat("morgen früh alle Rollläden um 7 hoch")
        self.assertEqual(res["type"], "proposal")
        pv = res["proposal"]["shutters"]
        self.assertEqual([r["shutter"] for g in pv["groups"] for r in g["rows"]], ["Schlafzimmer"])
        self.assertEqual(len(pv["declined"]), 3)
        self.assertFalse(res["shutter_schedules_enabled"])
        self.assertEqual(ss.list_schedules(), [])                                        # erst nach Klick
        out = self.apply(res)
        self.assertEqual(len(out["shutter_created"]), 1)
        row = ss.list_schedules()[0]
        self.assertEqual((row["shutter_id"], row["time"], row["date"], row["source"], row["note"]),
                         ("schlafzimmer", "07:00", self.tomorrow, "chat", "morgen früh alle Rollläden um 7 hoch"))
        self.assertIn("Rollladen-Zeitplan angelegt (Chat): Schlafzimmer 07:00 → auf", self.log_text())
        self.assertEqual(self.fake.sent, [])

    def test_schlafzimmer_now_50(self):
        self.api.tool_input = tool(s_immediate=[s_now("schlafzimmer", "position", 50)])
        res = self.chat("Schlafzimmer jetzt auf 50 %")
        self.assertEqual(res["proposal"]["shutters"]["immediate"],
                         [{"shutter": "Schlafzimmer", "target": "→ 50 %", "warn": None}])
        self.assertEqual(self.fake.sent, [])                                             # erst nach Klick
        out = self.apply(res)
        self.assertEqual(self.fake.sent, [("S_Roll", "50")])
        self.assertTrue(out["ok"])
        self.assertIn("Schlafzimmer → 50 % (Chat)", self.log_text())
        self.assertEqual(shutters.last_commands()["schlafzimmer"]["source"], "Chat")

    def test_door_needs_confirmation_like_buttons(self):
        self.api.tool_input = tool(s_immediate=[s_now("hintereingang", "down")])
        res = self.chat("Hintereingang jetzt runter")
        self.assertEqual(res["proposal"]["shutters"]["immediate"][0]["warn"],
                         "Hintereingang: Tür ohne Kontakt – ist niemand draußen?")
        with self.assertRaises(web.HTTPException) as cm:
            self.apply(res)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("Bestätigung", cm.exception.detail)
        self.assertEqual(self.fake.sent, [])
        self.assertIn(res["proposal"]["id"], hc._PROPOSALS)                              # nicht verbraucht
        self.apply(res, confirm=True)
        self.assertEqual(self.fake.sent, [("H_Roll", "DOWN")])

    def test_all_down_now_staggered_each_listed(self):
        ids = ("wohnen_links", "wohnen_rechts", "hintereingang", "schlafzimmer")
        self.api.tool_input = tool(s_immediate=[s_now(sid, "down") for sid in ids])
        res = self.chat("alle Rollläden jetzt runter")
        pv = res["proposal"]["shutters"]
        self.assertEqual([i["shutter"] for i in pv["immediate"]],
                         ["Wohnen links", "Wohnen rechts (Straße)", "Hintereingang", "Schlafzimmer"])
        self.assertEqual([bool(i["warn"]) for i in pv["immediate"]], [False, False, True, False])
        self.apply(res, confirm=True)
        self.assertEqual([n for n, _ in self.fake.sent], ["WL_Roll", "WR_Roll", "H_Roll", "S_Roll"])
        self.assertEqual(self.sleeps, [2.0, 2.0, 2.0])

    def test_ambiguous_asks_instead_of_guessing(self):
        self.api.tool_input = tool(s_immediate=[s_now("wohnen_links", "down")])           # Modell rät
        res = self.chat("Wohnen jetzt runter")
        self.assertEqual(res["type"], "question")
        self.assertIn("Wohnen links und Wohnen rechts (Straße)", res["text"])
        self.assertEqual(hc._PROPOSALS, {})
        self.api.tool_input = tool(s_immediate=[s_now("wohnen_links", "down"), s_now("wohnen_rechts", "down")])
        res = self.chat("Wohnen jetzt runter", res["text"], "beide")
        self.assertEqual(res["type"], "proposal")

    def test_query_tonight_local_without_api(self):
        ss.create_schedule({"shutter_id": "schlafzimmer", "action": "down", "position": None, "kind": "time",
                            "time": "23:59", "offset_min": 0, "not_before": None, "not_after": None,
                            "days": ",".join(hs.DAYS), "date": None}, now=datetime.now(TZ) - timedelta(days=1))
        res = self.chat("Was fahren die Rollläden heute Abend?")
        self.assertEqual(res["type"], "answer")
        self.assertIn("23:59  Schlafzimmer ▼ zu · Zeitplan (23:59)", res["text"])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(hc.used_today(), 0)
        self.assertEqual(self.fake.rules_calls, 1)
        self.chat("was fahren die Rollläden morgen?")
        self.assertEqual(self.fake.rules_calls, 1)                                      # 10 min zwischengespeichert

    def test_heating_and_shutter_in_one_proposal(self):
        self.api.tool_input = tool(entries=[{"room_id": "wohnen", "setpoint": 21, "time": "07:00",
                                             "date": self.tomorrow, "days": None}],
                                   s_entries=[s_entry("schlafzimmer", "up", "time", "07:00", date_=self.tomorrow)])
        res = self.chat("morgen um 7 Wohnen auf 21 Grad und Schlafzimmer hoch")
        self.assertEqual(len(res["proposal"]["groups"]), 1)                             # Heizung
        self.assertEqual(len(res["proposal"]["shutters"]["groups"]), 1)                 # Rollladen
        out = self.apply(res)
        self.assertEqual((len(out["created"]), len(out["shutter_created"])), (1, 1))

    def test_delete_shutter_schedule(self):
        e = ss.create_schedule({"shutter_id": "schlafzimmer", "action": "down", "position": None, "kind": "sunset",
                                "time": None, "offset_min": 0, "not_before": None, "not_after": None,
                                "days": "mo,di", "date": None})
        self.api.tool_input = tool(s_delete_ids=[e["id"]])
        res = self.chat("lösch den Schlafzimmer-Zeitplan")
        self.assertEqual(res["proposal"]["shutters"]["deletes"][0]["days"], "Jeden Mo, Di (ganzer Plan)")
        self.apply(res)
        self.assertEqual(ss.list_schedules(), [])

    def test_openai_same_flow(self):
        c = json.loads(self.cfg_path.read_text())
        c["assistant"].update(provider="openai", openai_api_key="sk-proj-TEST", model="gpt-4.1-mini")
        self.cfg_path.write_text(json.dumps(c))
        api = FakeOpenAI(tool(s_immediate=[s_now("schlafzimmer", "up")]))
        with mock.patch.object(hc, "_default_client", lambda a: api):
            res = self.chat("Schlafzimmer jetzt hoch")
        self.assertEqual(res["proposal"]["shutters"]["immediate"][0]["target"], "▲ auf")

    def test_heating_off_shutters_on_chat_works(self):
        c = json.loads(self.cfg_path.read_text())
        c["heating"]["enabled"] = False
        self.cfg_path.write_text(json.dumps(c))
        self.api.tool_input = tool(s_immediate=[s_now("schlafzimmer", "up")])
        self.assertEqual(self.chat("Schlafzimmer jetzt hoch")["type"], "proposal")


if __name__ == "__main__":
    unittest.main()
