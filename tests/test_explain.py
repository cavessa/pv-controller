"""„Warum?“-Antworten im Chat: Erkennen, Zeitbezug, Erklär-Kontext (ohne Secrets), Endpunkt (API gemockt)."""
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import decisions
import explain
import heating
import heating_chat as hc
import heating_schedule as hs
import schedule_log
import shutter_schedule as ss
import shutters
import web
from config import load_config
from tests import test_heating as th
from tests import test_shutter_chat as tsc
from tests.test_heating_chat import KEY, FakeAPI, FakeOpenAI

TZ = explain.TZ
NOW = datetime(2026, 10, 14, 14, 32, tzinfo=TZ)        # Mittwoch
EXAMPLE = Path(web.PROJECT_ROOT) / "config.example.json"
SECRET_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|(?i:[0-9a-f]{2}(:[0-9a-f]{2}){5})|https?://|sk-|"
                       r"(?i:token|passw|pwd|api[_-]?key|secret)")


def snap(ts, surplus=820, pv=3120, temp=50.0, phases=("keep_off", "below_start_threshold"), budget=None,
         heater=("budget", "below_start_threshold"), wallbox=("keep_off", "below_start_threshold"),
         failsafe=None, health=(), heater_w=0, extra=None):
    """Schnappschuss wie aus ``decisions.build`` (Prod-Besetzung: Heizstab, Wallbox, eBike)."""
    budget = surplus if budget is None else budget
    ph = phases if isinstance(phases, list) else [phases] * 3
    s = {"ts": ts.isoformat(timespec="seconds"),
         "energy": {"pv_w": pv, "grid_w": heater_w - surplus, "house_w": pv - surplus + heater_w, "heater_w": heater_w,
                    "wallbox_w": 0, "surplus_w": surplus, "surplus_effective_w": surplus, "storage_temp_c": temp},
         "profile": "winter", "failsafe": failsafe,
         "phases": {"budget_w": budget, "target_phases": max(0, (budget + 400) // 1500), "running": 0,
                    "threshold_w": 1100},
         "devices": [
             {"id": "zendure", "action": "none", "reason": "not_in_cascade", "name": "Batterie", "type": "zendure"},
             {"id": "shelly_test", "action": "keep_on", "reason": "keep_running", "name": "eBike", "type": "shelly_gen2",
              "on": True, "data": {"rest_w": surplus - 180, "power_w": 180, "hysteresis_w": 100}},
             {"id": "heizstab", "action": heater[0], "reason": heater[1], "name": "Wasserspeicher (Heizstab)",
              "type": "heizstab", "on": False,
              "data": {"budget_w": budget, "budget_down_w": budget, "raw_w": surplus, "battery_w": 0,
                       "threshold_w": 1100, "target_phases": 0, "actual_w": heater_w, "smooth_min": 10,
                       "grid_assist_w": 400, "temp_c": temp}},
             {"id": "wallbox", "action": wallbox[0], "reason": wallbox[1], "name": "Wallbox", "type": "wallbox",
              "on": False, "data": {"rest_w": surplus, "power_w": 1610, "threshold_w": 1710}},
             *({"id": f"heizstab_phase{i + 1}", "action": a, "reason": r,
                **({"data": d[0]} if d else {})} for i, (a, r, *d) in enumerate(ph)),
             {"id": "wallbox_control", "action": "unchanged", "reason": "not_connected",
              "data": {"car_state": 1, "eco_mode": True, "amp": 6, "phases": 1, "power_w": 0}}],
         "health": list(health)}
    s.update(extra or {})
    return s


class DetectTest(unittest.TestCase):
    def test_why_questions(self):
        for t in ("Warum heizt der Heizstab gerade nicht?", "warum lädt das Auto nicht mit PV",
                  "Wieso ging um 14 Uhr Phase 2 aus?", "Weshalb speisen wir ein?", "Woran liegt es, dass Bad kalt ist?",
                  "Was ist los mit dem Rollladen?"):
            self.assertTrue(explain.is_why([{"role": "user", "content": t}]), t)

    def test_normal_requests_are_not_why(self):
        for t in ("morgen Bad um 7 auf 21 Grad", "Schlafzimmer jetzt auf 50 %", "Was ist für morgen geplant?",
                  "lösch den Plan", "alle Rollläden runter"):
            self.assertFalse(explain.is_why([{"role": "user", "content": t}]), t)

    def test_follow_up_after_a_why_question(self):
        hist = [{"role": "user", "content": "Warum heizt der Heizstab nicht?"},
                {"role": "assistant", "content": "Stand 14:32: …"}]
        self.assertTrue(explain.is_why(hist + [{"role": "user", "content": "und um 12 Uhr?"}]))
        self.assertFalse(explain.is_why(hist + [{"role": "user", "content": "dann stell Bad auf 21"}]))
        self.assertFalse(explain.is_why(hist + [{"role": "user", "content": "morgen Bad um 7 auf 21 Grad"}]))

    def test_mixed_question_has_a_command(self):
        self.assertTrue(explain.has_command("warum ist Bad kalt? stell es auf 21"))
        self.assertTrue(explain.has_command("Wieso ist der Rollladen offen? Bitte fahr ihn runter."))
        for t in ("Warum schaltet der Heizstab nicht?", "Warum hat Bad heute früh nicht auf 21 geschaltet?",
                  "Warum ist der Rollladen Hintereingang nicht ganz zu?", "Wieso fährt der Rollladen nicht?"):
            self.assertFalse(explain.has_command(t), t)


class TimeWindowTest(unittest.TestCase):
    def win(self, text, now=NOW):
        return [(w.label, f"{w.start:%d. %H:%M}", f"{w.end:%d. %H:%M}", w.point is not None)
                for w in explain.time_windows(text, now)]

    def test_now_has_no_window(self):
        self.assertEqual(self.win("Warum heizt der Heizstab gerade nicht?"), [])
        self.assertEqual(self.win("Warum ist Bad nicht auf 21 Grad?"), [])       # „auf 21“ ist keine Uhrzeit
        self.assertEqual(self.win("Warum läuft Phase 2 seit 2 Stunden?"), [])

    def test_clock(self):
        self.assertEqual(self.win("Warum ging um 14 Uhr Phase 2 aus?"), [("heute 14:00", "14. 13:45", "14. 14:15", True)])
        self.assertEqual(self.win("was war um 9:30 los?"), [("heute 09:30", "14. 09:15", "14. 09:45", True)])
        self.assertEqual(self.win("wieso gegen 13.05 Uhr?"), [("heute 13:05", "14. 12:50", "14. 13:20", True)])

    def test_clock_in_the_future_means_yesterday_and_window_ends_now(self):
        self.assertEqual(self.win("Warum ging um 22 Uhr der Rollladen nicht zu?"),
                         [("gestern 22:00", "13. 21:45", "13. 22:15", True)])
        self.assertEqual(self.win("warum um 14:30?"), [("heute 14:30", "14. 14:15", "14. 14:32", True)])

    def test_yesterday(self):
        self.assertEqual(self.win("Warum lief gestern um 14 Uhr nichts?"),
                         [("gestern 14:00", "13. 13:45", "13. 14:15", True)])
        self.assertEqual(self.win("Warum lief gestern nichts?"), [("gestern (ganzer Tag)", "13. 00:00", "14. 00:00", False)])

    def test_day_parts(self):
        self.assertEqual(self.win("Warum hat Bad heute früh nicht auf 21 geschaltet?"),
                         [("heute früh (05:00–09:00)", "14. 05:00", "14. 09:00", False)])
        self.assertEqual(self.win("warum heute morgen nicht?")[0][0], "heute früh (05:00–09:00)")
        self.assertEqual(self.win("Warum lief gestern Abend der Heizstab?"),
                         [("gestern abends (17:00–22:00)", "13. 17:00", "13. 22:00", False)])
        self.assertEqual(self.win("warum heute abend?"), [("gestern abends (17:00–22:00)", "13. 17:00", "13. 22:00", False)])
        self.assertEqual(self.win("warum heute nachmittag?"), [("heute nachmittags (13:00–17:00)", "14. 13:00", "14. 14:32", False)])
        self.assertEqual(self.win("warum heute?"), [("heute (ganzer Tag)", "14. 00:00", "14. 14:32", False)])

    def test_ago(self):
        self.assertEqual(self.win("Warum ging vor 20 Minuten Phase 1 aus?"),
                         [("vor 20 min (14:12)", "14. 13:57", "14. 14:27", True)])
        self.assertEqual(self.win("was war vor 2 Stunden?")[0][0], "vor 2 h (12:32)")

    def test_tomorrow_is_not_a_morning(self):
        self.assertEqual(self.win("Warum schaltet Bad morgen um 7?"), [("heute 07:00", "14. 06:45", "14. 07:15", True)])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        db.init_pv_logging_tables()
        db.init_cascade_tables()
        db.init_cascade_settings_table()
        hs.init_tables()
        self.dir = Path(self.tmp.name)
        self.cfg_path = self.dir / "config.json"
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cfg["runtime"].update({"log_file": str(self.dir / "app.log"), "dry_run": False})
        cfg.update(th.raw_cfg(schedules_enabled=True))
        cfg["openhab"] = {"url": "http://192.168.2.5:8080", "token": "oh.SUPERGEHEIM-token"}
        cfg["latitude"], cfg["longitude"] = tsc.LOC
        rooms = [dict(r, automation="pvcontroller") for r in tsc.ROOMS]
        cfg["shutters"] = tsc.raw_shutters(rooms=rooms, schedules_enabled=True)
        cfg["heater"].update({"grid_assist_w": 400, "storage_max_temp": 64, "storage_temp_hysteresis": 2})
        cfg["solax"] = {**cfg.get("solax", {}), "url": "http://192.168.2.195", "pwd": "SOLAXPASSWORT"}
        cfg["wallbox"] = {**cfg.get("wallbox", {}), "enabled": True, "url": "http://192.168.2.160"}
        cfg["assistant"] = {"enabled": True, "provider": "anthropic", "api_key": KEY, "max_requests_per_day": 20}
        self.raw = cfg
        self.write()
        db.create_cascade_device("shelly_test", "eBike", "shelly_gen2", 180, 1, ip_address="192.168.2.77")

    def write(self):
        self.cfg_path.write_text(json.dumps(self.raw), encoding="utf-8")

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def seed(self, now, minutes=60, **kw):
        for m in range(minutes, -1, -1):
            decisions.save(snap(now - timedelta(minutes=m), **kw))

    def build(self, text, now=NOW, rooms=None, sh_status=None):
        return explain.build(text, now, load_config(self.cfg_path), heating.parse_heating(self.raw),
                             shutters.parse_shutters(self.raw), rooms or [], sh_status or [])


class ContextTest(Base):
    def test_now_uses_latest_snapshot_and_last_hour_condensed(self):
        for m in range(90, -1, -1):
            t = NOW - timedelta(minutes=m)
            if m > 40:
                decisions.save(snap(t, surplus=2600, phases=[("keep_on", "budget_available"), ("keep_off", "below_start_threshold"),
                                                             ("keep_off", "below_start_threshold")], heater_w=1500))
            elif m > 35:
                decisions.save(snap(t, surplus=300, heater_w=1500,
                                    phases=[("keep_on", "min_on_time", {"remaining_s": 60 * (m - 35)}),
                                            ("keep_off", "below_start_threshold"), ("keep_off", "below_start_threshold")]))
            else:
                decisions.save(snap(t))
        ctx = self.build("Warum heizt der Heizstab gerade nicht?")
        titles = [t for t, _ in ctx.sections]
        self.assertEqual(titles[:3], ["Aktueller Stand (letzter Lauf der Steuerung)",
                                      "Letzte 60 min – Wechsel (13:32–14:32, 61 Läufe)", "Letzte 60 min – Messwerte"])
        text = ctx.text
        self.assertIn("Stand 14:32 (Winterprofil): PV 3120 W, Einspeisung 820 W", text)
        self.assertIn("- Heizstab Phase 1–3: bleibt aus [below_start_threshold] Überschuss 820 W unter der Startschwelle "
                      "1100 W", text)                                            # gleiche Phasen in einer Zeile
        ch = dict(ctx.sections)["Letzte 60 min – Wechsel (13:32–14:32, 61 Läufe)"]
        self.assertEqual([c[:5] for c in ch], ["13:52", "13:57"])                # Wechsel, nicht jede Minute
        self.assertEqual(explain._merge_phases([("heizstab_phase1", "Heizstab Phase 1: an"), ("wallbox", "Wallbox: aus"),
                                                ("heizstab_phase2", "Heizstab Phase 2: aus"),
                                                ("heizstab_phase3", "Heizstab Phase 3: an")]),
                         ["Heizstab Phase 1+3: an", "Wallbox: aus", "Heizstab Phase 2: aus"])
        self.assertIn("Heizstab Phase 1: bleibt an [min_on_time] Mindestlaufzeit läuft noch (300 s)", ch[0])
        self.assertLessEqual(len(dict(ctx.sections)["Letzte 60 min – Messwerte"]), 8)
        self.assertIn("1 Phase(n) ab 1100 W, 2 Phase(n) ab 2600 W, 3 Phase(n) ab 4100 W", text)
        self.assertIn("Reihenfolge der Verteilung:", text)
        self.assertEqual(ctx.windows, [])

    def test_point_in_time_uses_snapshots_around_it(self):
        day = NOW.replace(hour=0, minute=0)
        for m in range(13 * 60, 14 * 60 + 33):
            t = day + timedelta(minutes=m)
            two = m < 14 * 60
            decisions.save(snap(t, surplus=3300 if two else 900, heater_w=3000 if two else 1500,
                                phases=[("keep_on", "budget_available"),
                                        ("keep_on", "budget_available") if two else
                                        (("turn_off", "below_stop_threshold") if m == 14 * 60 else ("keep_off", "below_start_threshold")),
                                        ("keep_off", "below_start_threshold")]))
        ctx = self.build("Warum ging um 14 Uhr Phase 2 aus?")
        sec = dict(ctx.sections)
        self.assertIn("Zeitpunkt heute 14:00 – nächster Lauf", sec)
        self.assertIn("Stand 14:00", sec["Zeitpunkt heute 14:00 – nächster Lauf"][0])
        ch = sec["Zeitpunkt heute 14:00 – Wechsel 13:45–14:15"]
        self.assertEqual(len(ch), 2)
        self.assertIn("14:00 Heizstab Phase 2: schaltet AUS [below_stop_threshold]", ch[0])
        self.assertIn("14:01 Heizstab Phase 2: bleibt aus", ch[1])

    def test_this_morning_with_schedule_results_for_the_room(self):
        self.seed(NOW, 600)
        e = hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": ",".join(hs.DAYS),
                                "date": None})
        schedule_log.record(schedule_log.HEATING, e["id"], "wohnen", "skipped_schedules_off",
                            NOW.replace(hour=6, minute=30), {"setpoint": 21.0, "when": "06:30"})
        schedule_log.record(schedule_log.HEATING, 99, "kind", "sent", NOW.replace(hour=6, minute=0),
                            {"setpoint": 20.0, "when": "06:00"})
        ctx = self.build("Warum hat Wohnen heute früh nicht auf 21 geschaltet?",
                         rooms=[{"id": "wohnen", "name": "Wohnen", "temp": 18.4, "setpoint": 17.5}])
        sec = dict(ctx.sections)
        self.assertIn("Zeitraum heute früh (05:00–09:00) – Beginn", sec)
        heat = "\n".join(sec["Heizung (Räume)"])
        self.assertIn("Wohnen: Ist 18,4 °C, Soll 17,5 °C", heat)
        self.assertIn(f"Zeitplan #{e['id']}: 06:30 → 21 °C", heat)
        self.assertIn("06:30 Wohnen (Zeitplan 06:30 → 21 °C): [skipped_schedules_off] Nicht ausgeführt: Zeitpläne sind ausgeschaltet",
                      heat)
        self.assertNotIn("Kind", heat)                                           # nur der genannte Raum
        self.assertNotIn("Rollläden", sec)

    def test_shutter_question_gets_door_rule_and_result(self):
        self.seed(NOW, 5)
        self.raw["shutters"]["rooms"][2]["auto_close_max"] = 80
        self.write()
        scfg = shutters.parse_shutters(self.raw)
        e = ss.create_schedule({"shutter_id": "hintereingang", "action": "down", "position": None, "kind": "time",
                                "time": "22:01", "offset_min": 0, "not_before": None, "not_after": None,
                                "days": ",".join(hs.DAYS), "date": None}, now=NOW - timedelta(days=3))
        at = NOW.replace(hour=22, minute=1) - timedelta(days=1)
        shutters.record_command(scfg.shutter("hintereingang"), "position", 80, "Zeitplan", at)
        schedule_log.record(schedule_log.SHUTTER, e["id"], "hintereingang", "sent", at,
                            {"target": "▼ zu", "when": "22:01", "position": 0, "target_pct": 80,
                             "limited_from": 100, "limited_to": 80, "limit": "limited_door_gap"})
        ctx = self.build("Warum ist der Rollladen Hintereingang nicht ganz zu?",
                         sh_status=[{"id": "hintereingang", "position": 80, "position_reliable": True, "moving": None}])
        sh = "\n".join(dict(ctx.sections)["Rollläden"])
        self.assertIn("Hintereingang: Position 80 % zu", sh)
        self.assertIn("Tür ohne Kontakt – schließt automatisch nur bis 80 % (Aussperrschutz)", sh)
        self.assertIn("[sent] Gesendet – Rollladen danach bei 80 % zu; Tür ohne Kontakt: automatisch nur bis 80 % statt 100 %",
                      sh)
        self.assertIn("Letzter Befehl: 13.10. 22:01 → 80 % zu (Zeitplan)", sh)
        self.assertNotIn("Schlafzimmer", sh)

    def test_missing_data_is_marked_unknown(self):
        ctx = self.build("Warum heizt der Heizstab nicht?")
        self.assertIn("unbekannt – es gibt noch keinen Entscheidungs-Schnappschuss", ctx.text)
        self.seed(NOW, 30)
        ctx = self.build("Warum ging gestern um 9 Uhr Phase 1 aus?")
        sec = dict(ctx.sections)
        self.assertIn("unbekannt – für diesen Zeitraum liegen keine Schnappschüsse vor", sec["Zeitraum gestern 09:00"][0])
        self.assertIn("Schnappschüsse vorhanden von 14:02 bis 14:32", sec["Zeitraum gestern 09:00"][0])

    def test_stale_snapshot_is_flagged(self):
        self.seed(NOW - timedelta(minutes=25), 10)
        self.assertIn("ACHTUNG: Der letzte Lauf der Steuerung ist 25 min alt", self.build("Warum kein Heizen?").text)

    def test_failsafe_and_health(self):
        self.seed(NOW, 10)
        decisions.health_update({"main_meter": {"ok": False}}, NOW - timedelta(minutes=3))
        decisions.health_update({"pergola": {"ok": False, "expected": True}}, NOW - timedelta(hours=20))
        decisions.note_health("openhab", False, NOW - timedelta(hours=2),
                              "HTTPConnectionPool(host='192.168.2.5', port=8080): timeout")
        decisions.note_health("openhab", True, NOW - timedelta(hours=1))
        decisions.save(snap(NOW, phases=("keep_off", "failsafe_global"),
                            failsafe={"reason": "failsafe_global", "unreadable": ["main_meter"], "names": ["Hauptzähler"]},
                            extra={"phases": {"unreadable": ["Hauptzähler"]}}))
        text = self.build("Warum speisen wir gerade ein, obwohl der Speicher nicht voll ist?").text
        self.assertIn("Fail-safe aktiv [failsafe_global]: Hauptzähler nicht lesbar", text)
        self.assertIn("Heizstab Phase 1–3: bleibt aus [failsafe_global] Fail-safe: Hauptzähler nicht lesbar", text)
        self.assertIn("14:32 Fail-safe aktiv: Hauptzähler nicht lesbar", text)
        self.assertIn("Jetzt nicht erreichbar: Hauptzähler seit 14:29", text)
        self.assertIn("Jetzt nicht erreichbar: Pergola-Wechselrichter seit 13.10. 18:32 (erwartet, keine Sonne)", text)
        self.assertIn("12:32 openHAB: nicht mehr erreichbar", text)
        self.assertIn("13:32 openHAB: wieder erreichbar", text)

    def test_no_secrets_ips_or_macs_in_context(self):
        self.seed(NOW, 120)
        self.raw["zendure"] = {"enabled": True, "ip": "192.168.2.88", "sn": "SN-GEHEIM-123", "control_enabled": True}
        self.write()
        schedule_log.record(schedule_log.SHUTTER, 1, "schlafzimmer", "openhab_error", NOW - timedelta(hours=1),
                            {"when": "13:30", "target": "▼ zu"},
                            "GET /items: HTTPConnectionPool(host='192.168.2.5', port=8080) aa:bb:cc:dd:ee:ff")
        decisions.save(snap(NOW, extra={"devices": [
            {"id": "wallbox_control", "action": "error", "reason": "device_unreachable",
             "error": "http://192.168.2.160/api/status timeout token=abc sk-ant-api03-XyZ123"}]}))
        rooms = [{"id": "wohnen", "name": "Wohnen", "temp": 18.4, "setpoint": 17.5,
                  "error": "openHAB nicht erreichbar: http://192.168.2.5:8080"}]
        sh = [{"id": "schlafzimmer", "position": 40, "alert": "Rückmeldung fehlt (host='192.168.2.5', port=8080)"}]
        for q in ("Warum fährt der Rollladen Schlafzimmer nicht und warum ist Wohnen kalt und was war um 13 Uhr?",
                  "Warum lädt die Batterie heute nicht?"):
            ctx = self.build(q, rooms=rooms, sh_status=sh)
            for blob in (ctx.text, json.dumps(ctx.basis, ensure_ascii=False)):
                self.assertIsNone(SECRET_RE.search(blob), SECRET_RE.search(blob))
                for frag in ("SUPERGEHEIM", "SOLAXPASSWORT", "SN-GEHEIM", "192.168", "W_Set", "S_Roll", KEY):
                    self.assertNotIn(frag, blob)
        self.assertIn("Batterie: Regelung an; Mindest-SoC", ctx.text)

    def test_size_is_limited_and_measured(self):
        day = NOW.replace(hour=0, minute=0)
        for m in range(0, 14 * 60 + 33):                                         # flatternder Tag: Wechsel jede Minute
            decisions.save(snap(day + timedelta(minutes=m), surplus=1200 if m % 2 else 900,
                                phases=("keep_off", "below_start_threshold") if m % 2 else ("keep_off", "min_off_time", {"remaining_s": 60}),
                                wallbox=("keep_off", "below_start_threshold") if m % 2 else ("keep_off", "no_surplus")))
        for i in range(60):
            schedule_log.record(schedule_log.SHUTTER, i, "schlafzimmer", "skipped_frost", NOW - timedelta(minutes=i * 5),
                                {"when": "07:00", "target": "▲ auf", "outdoor_c": -3.0}, "x" * 200)
        ctx = self.build("Warum fuhren heute die Rollläden nicht und was war um 9 Uhr mit dem Heizstab?")
        self.assertEqual(len(ctx.windows), 1)
        self.assertLess(ctx.tokens, explain.MAX_TOKENS)
        self.assertLessEqual(len(ctx.text), explain.MAX_CHARS)
        self.assertIn("ausgelassen", ctx.text)
        self.assertIn("Stand 14:32", ctx.text)                                   # der aktuelle Stand bleibt vollständig
        small = self.build("Warum heizt der Heizstab nicht?")
        self.assertLess(small.tokens, 3500)

    def test_build_only_reads(self):
        self.seed(NOW, 10)
        self.build("Warum heizt der Heizstab nicht?")                            # Tabellen sind danach angelegt
        with sqlite3.connect(db.DB_PATH) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        self.build("Warum fuhr gestern um 22 Uhr der Rollladen Hintereingang nicht und warum ist Wohnen kalt?")
        with sqlite3.connect(db.DB_PATH) as conn:
            after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        self.assertEqual(before, after)


class ToolTest(unittest.TestCase):
    def test_explain_tool_is_the_same_tool_plus_one_text_field(self):
        sch, base = hc.TOOL_EXPLAIN["input_schema"], hc.TOOL["input_schema"]
        self.assertEqual(hc.TOOL_EXPLAIN["name"], "propose_schedule")
        self.assertEqual(set(sch["properties"]) - set(base["properties"]), {"explanation"})
        self.assertEqual(set(sch["required"]), set(sch["properties"]))           # strict: alles required
        self.assertEqual(sch["properties"]["explanation"]["type"], ["string", "null"])
        self.assertNotIn("explanation", base["properties"])                      # normales Tool unverändert
        self.assertNotIn("explanation", hc.SYSTEM_PROMPT)

    def test_normal_call_is_unchanged_and_explain_call_adds_prompt_and_field(self):
        acfg = hc.AssistantConfig(enabled=True, api_key=KEY)
        api = FakeAPI({"entries": []})
        hc.call_model(acfg, "KONTEXT", [{"role": "user", "content": "x"}], client_factory=lambda a: api)
        hc.call_model(acfg, "KONTEXT", [{"role": "user", "content": "warum?"}], client_factory=lambda a: api, explain=True)
        normal, expl = api.calls
        self.assertEqual(normal["system"], hc.SYSTEM_PROMPT + "\n\nKONTEXT")
        self.assertEqual(normal["tools"], [hc.TOOL])
        self.assertEqual(expl["system"], hc.SYSTEM_PROMPT + hc.EXPLAIN_PROMPT + "\n\nKONTEXT")
        self.assertEqual(expl["tools"], [hc.TOOL_EXPLAIN])
        self.assertIn("Das kann ich nicht sehen", hc.EXPLAIN_PROMPT)

    def test_openai_explain_call_forces_the_tool(self):
        ocfg = hc.AssistantConfig(enabled=True, provider="openai", openai_api_key="sk-proj-TEST", model="gpt-4.1-mini")
        api = FakeOpenAI({"entries": [], "explanation": "Stand 14:32: …"})
        out = hc.call_model(ocfg, "KONTEXT", [{"role": "user", "content": "warum?"}], client_factory=lambda a: api,
                            explain=True)
        kw = api.calls[0]
        self.assertEqual(out["explanation"], "Stand 14:32: …")
        self.assertEqual(kw["tool_choice"], {"type": "function", "function": {"name": "propose_schedule"}})
        fn = kw["tools"][0]["function"]
        self.assertTrue(fn["strict"])
        self.assertIn("explanation", fn["parameters"]["required"])
        self.assertIn("ERKLÄR-KONTEXT", kw["messages"][0]["content"])


def tool(explanation=None, **kw):
    return {**tsc.tool(**kw), "explanation": explanation}


class WebExplainTest(Base):
    def setUp(self):
        super().setUp()
        self.fake = tsc.Client()
        self.api = FakeAPI(tool("Stand 14:32: Überschuss 820 W, Startschwelle 1100 W – keine Phase."))
        self.patches = [mock.patch.object(web, "CONFIG_PATH", self.cfg_path),
                        mock.patch.object(heating, "make_client", lambda raw: self.fake),
                        mock.patch.object(hc, "_default_client", lambda a: self.api),
                        mock.patch.object(hc, "sdk_available", lambda *a: True)]
        for p in self.patches:
            p.start()
        hc._PROPOSALS.clear()
        self.now = datetime.now(TZ).replace(microsecond=0)
        self.seed(self.now, 20)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def chat(self, *texts):
        msgs = [web._ChatMessage(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts)]
        return web.heating_chat(web._ChatRequest(messages=msgs))

    def counts(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    def test_why_question_returns_text_and_basis(self):
        res = self.chat("Warum heizt der Heizstab gerade nicht?")
        self.assertEqual(res["type"], "explain")
        self.assertEqual(res["text"], "Stand 14:32: Überschuss 820 W, Startschwelle 1100 W – keine Phase.")
        self.assertEqual(res["basis"][0]["title"], "Aktueller Stand (letzter Lauf der Steuerung)")
        self.assertTrue(any("[below_start_threshold]" in line for line in res["basis"][0]["lines"]))
        self.assertGreater(res["context_tokens"], 300)
        self.assertNotIn("proposal", res)
        kw = self.api.calls[0]
        self.assertEqual(kw["tools"], [hc.TOOL_EXPLAIN])
        self.assertIn("ERKLÄR-KONTEXT (nur diese Fakten verwenden)", kw["system"])
        self.assertIn("Überschuss 820 W unter der Startschwelle 1100 W", kw["system"])
        self.assertIsNone(SECRET_RE.search(kw["system"].split("ERKLÄR-KONTEXT")[1]))
        self.assertNotIn("SUPERGEHEIM", json.dumps(kw, default=str))
        self.assertEqual(res["chat"]["used_today"], 1)                           # zählt zum Tageslimit

    def test_why_question_never_switches_or_writes(self):
        self.chat("Warum ist der Rollladen Schlafzimmer offen und Wohnen kalt?")  # Tabellen/Zähler sind angelegt
        before = self.counts()
        # Modell liefert trotz reiner Frage Schaltwünsche: werden verworfen
        self.api.tool_input = tool("Stand 14:32: zu wenig Überschuss.",
                                   immediate=[{"room_id": "wohnen", "setpoint": 22}],
                                   s_immediate=[tsc.s_now("schlafzimmer", "down")],
                                   entries=[{"room_id": "wohnen", "setpoint": 21, "time": "07:00", "kind": "time",
                                             "offset_min": 0, "not_before": None, "not_after": None,
                                             "date": None, "days": ["mo"]}])
        res = self.chat("Warum ist der Rollladen Schlafzimmer offen und Wohnen kalt?")
        self.assertEqual(res["type"], "explain")
        self.assertEqual(hc._PROPOSALS, {})                                      # kein Vorschlag, nichts zu übernehmen
        self.assertEqual(self.fake.sent, [])                                     # kein Befehl an openHAB
        after = self.counts()
        self.assertEqual(after, before)                                          # keine neue Zeile …
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT count FROM assistant_usage").fetchone()[0], 2)   # … nur der Zähler

    def test_mixed_question_gives_explanation_and_proposal_with_confirmation(self):
        self.api.tool_input = tool("Stand 14:32: Wohnen hat 18,4 °C bei Soll 17,5 °C.",
                                   immediate=[{"room_id": "wohnen", "setpoint": 21}])
        res = self.chat("Warum ist Wohnen kalt? Stell es auf 21")
        self.assertEqual(res["type"], "proposal")
        self.assertEqual(res["explanation"], "Stand 14:32: Wohnen hat 18,4 °C bei Soll 17,5 °C.")
        self.assertTrue(res["basis"])
        self.assertEqual(res["proposal"]["immediate"], [{"room": "Wohnen", "setpoint": 21.0}])
        self.assertEqual(self.fake.sent, [])                                     # erst nach „Übernehmen“
        web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        self.assertEqual(self.fake.sent, [("W_Set", "21")])

    def test_missing_data_reaches_the_model_as_unknown(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DELETE FROM decision_snapshot")
        self.api.tool_input = tool("Das kann ich nicht sehen: Es gibt noch keine Daten der Steuerung.")
        res = self.chat("Warum heizt der Heizstab nicht?")
        self.assertIn("unbekannt – es gibt noch keinen Entscheidungs-Schnappschuss", self.api.calls[0]["system"])
        self.assertEqual(res["type"], "explain")
        self.assertIn("Das kann ich nicht sehen", res["text"])

    def test_no_explanation_from_model_is_said_plainly(self):
        self.api.tool_input = tool(None)
        res = self.chat("Warum heizt der Heizstab nicht?")
        self.assertEqual(res["type"], "explain")
        self.assertIn("Das kann ich nicht sehen", res["text"])
        self.api.tool_input = tool(None, question="Welchen Rollladen meinst du?")
        self.assertEqual(self.chat("Warum ist der Rollladen offen?")["type"], "question")

    def test_daily_limit_applies(self):
        self.raw["assistant"]["max_requests_per_day"] = 2
        self.write()
        self.assertEqual(self.chat("Warum heizt der Heizstab nicht?")["type"], "explain")
        self.assertEqual(self.chat("Wieso lädt das Auto nicht?")["type"], "explain")
        res = self.chat("Warum speisen wir ein?")
        self.assertEqual(res["type"], "limit")
        self.assertIn("Tageslimit erreicht (2/2)", res["text"])
        self.assertEqual(len(self.api.calls), 2)

    def test_follow_up_question_is_explained_too(self):
        res = self.chat("Warum heizt der Heizstab nicht?", "Stand 14:32: …", "und vor 10 Minuten?")
        self.assertEqual(res["type"], "explain")
        self.assertTrue(any(s["title"].startswith("Zeitpunkt vor 10 min") for s in res["basis"]))

    def test_normal_requests_and_local_answers_are_unchanged(self):
        self.api.tool_input = tsc.tool(immediate=[{"room_id": "wohnen", "setpoint": 22}])
        res = self.chat("jetzt Wohnen auf 22")
        self.assertEqual(res["type"], "proposal")
        self.assertNotIn("basis", res)
        self.assertNotIn("explanation", res)
        kw = self.api.calls[0]
        self.assertEqual(kw["tools"], [hc.TOOL])
        self.assertNotIn("ERKLÄR-KONTEXT", kw["system"])
        self.assertTrue(kw["system"].startswith(hc.SYSTEM_PROMPT + "\n\nJetzt:"))
        res = self.chat("Was ist für morgen geplant?")
        self.assertEqual(res["type"], "answer")
        self.assertEqual(len(self.api.calls), 1)                                 # Abfrage weiter ohne API

    def test_key_in_model_answer_is_masked(self):
        self.api.tool_input = tool("Stand 14:32: Key sk-proj-AbCd1234567890 gefunden.")
        res = self.chat("Warum heizt der Heizstab nicht?")
        self.assertNotIn("AbCd", res["text"])


if __name__ == "__main__":
    unittest.main()
