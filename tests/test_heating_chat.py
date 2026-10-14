"""Heizung Phase 3: Chat mit gemockter Claude API (kein echter Key, kein Netz).

Prüft Kontext, Vorschlag/Validierung („morgen“, „übermorgen“, „Freitag“, „jeden Werktag“, Rückfrage,
35 °C, Vergangenheit), Übernehmen (source='chat'), Abfragen ohne API, Tageslimit, Freigabe-Schalter,
Fehler (404 Modell, Key ungültig, offline) und dass der API-Key nirgends auftaucht.
"""
import json
import logging
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import heating
import heating_chat as hc
import heating_schedule as hs
import web
from tests import test_heating as th

KEY = "sk-ant-TESTKEY-0123456789"
TZ = hs.TZ
NOW = datetime(2026, 10, 13, 12, 30, tzinfo=TZ)        # Dienstag


class FakeAPI:
    """Wie anthropic.Anthropic: messages.create → Tool-Aufruf mit vorgegebener Eingabe."""

    def __init__(self, tool_input=None, text=None, exc=None, stop_reason="tool_use"):
        self.tool_input, self.text, self.exc, self.stop_reason = tool_input, text, exc, stop_reason
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        content = []
        if self.text:
            content.append(SimpleNamespace(type="text", text=self.text))
        if self.tool_input is not None:
            content.append(SimpleNamespace(type="tool_use", name="propose_schedule", input=self.tool_input))
        return SimpleNamespace(stop_reason=self.stop_reason, content=content)


def tool(entries=(), immediate=(), delete_ids=(), question=None, summary=None):
    return {"entries": list(entries), "immediate": list(immediate), "delete_ids": list(delete_ids),
            "question": question, "summary": summary}


def entry(room="wohnen", sp=21.0, time="07:00", date_=None, days=None):
    return {"room_id": room, "setpoint": sp, "time": time, "date": date_, "days": days}


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        hs.init_tables()
        self.cfg = heating.parse_heating(th.raw_cfg())

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()


class ProposalTest(DbCase):
    """Server-Prüfung der (gemockten) Modellantwort."""

    def check(self, inp):
        return hc.validate_proposal(self.cfg, inp, NOW)

    def test_tomorrow_two_entries_preview(self):
        p, err = self.check(tool([entry("wohnen", 21, "07:00", "2026-10-14"), entry("wohnen", 19, "11:00", "2026-10-14")],
                                 summary="Morgen Bad 7–11 Uhr wärmer"))
        self.assertEqual(err, [])
        pv = hc.preview(self.cfg, p, NOW)
        self.assertEqual(pv["groups"], [{"label": "Morgen (Mi 14.10.)", "rows": [
            {"time": "07:00", "room": "Wohnen", "setpoint": 21.0}, {"time": "11:00", "room": "Wohnen", "setpoint": 19.0}]}])

    def test_day_after_tomorrow_and_friday(self):
        p, err = self.check(tool([entry(date_="2026-10-15"), entry(room="kind", date_="2026-10-16", sp=20)]))
        self.assertEqual(err, [])
        self.assertEqual([g["label"] for g in hc.preview(self.cfg, p, NOW)["groups"]],
                         ["Übermorgen (Do 15.10.)", "Fr 16.10."])

    def test_every_workday(self):
        p, err = self.check(tool([entry(time="06:00", days=["mo", "di", "mi", "do", "fr"])]))
        self.assertEqual(err, [])
        self.assertEqual(p.entries[0]["days"], "mo,di,mi,do,fr")
        self.assertEqual(hc.preview(self.cfg, p, NOW)["groups"][0]["label"], "Jeden Mo–Fr")

    def test_35_degrees_rejected(self):
        _, err = self.check(tool([entry(sp=35, date_="2026-10-14")]))
        self.assertIn("außerhalb", err[0])

    def test_past_rejected(self):
        _, err = self.check(tool([entry(time="07:00", date_="2026-10-13")]))      # heute 07:00 < 12:30
        self.assertIn("Vergangenheit", err[0])
        _, err = self.check(tool([entry(date_="2026-10-12")]))
        self.assertIn("Vergangenheit", err[0])

    def test_unknown_or_display_only_room_and_bad_delete(self):
        _, err = self.check(tool([entry(room="garage", date_="2026-10-14")], immediate=[{"room_id": "wasch", "setpoint": 20}],
                                 delete_ids=[999]))
        self.assertEqual(len(err), 3)
        self.assertIn("unbekannt", err[0])
        self.assertIn("nur Anzeige", err[1])
        self.assertIn("#999", err[2])

    def test_delete_preview_marks_whole_recurring_plan(self):
        e = hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": "mo,di,mi,do,fr",
                                "date": None}, now=NOW)
        p, err = self.check(tool(delete_ids=[e["id"]]))
        self.assertEqual(err, [])
        self.assertEqual(hc.preview(self.cfg, p, NOW)["deletes"][0]["when"], "Jeden Mo–Fr (ganzer Plan)")


class ContextTest(DbCase):
    def test_context_has_date_rooms_limits_and_next_7_days(self):
        hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": "mo,di,mi,do,fr",
                            "date": None}, now=NOW)
        hs.create_schedule({"room_id": "kind", "setpoint": 19.0, "time": "11:00", "days": None,
                            "date": "2026-10-14"}, now=NOW)
        rooms = heating.read_status(self.cfg, th.FakeClient())["rooms"]
        ctx = hc.build_context(self.cfg, rooms, hs.list_schedules(), NOW)
        self.assertIn("Jetzt: Dienstag, 13.10.2026 12:30 (Europe/Berlin)", ctx)
        self.assertIn("Fr = 2026-10-16", ctx)
        self.assertIn("Grenzen: Sollwert 5 °C bis 26 °C", ctx)
        self.assertIn("- wohnen: Wohnen – Ist 18,4 °C, Soll 17,5 °C", ctx)
        self.assertIn("- wasch: Waschküche – nur Anzeige", ctx)
        self.assertIn("#2 kind 11:00 19 °C einmalig 2026-10-14", ctx)
        self.assertIn("- 2026-10-14 Mi 06:30 wohnen → 21 °C (#1, wiederkehrend)", ctx)
        self.assertIn("- 2026-10-14 Mi 11:00 kind → 19 °C (#2, einmalig)", ctx)
        self.assertNotIn("2026-10-13 Di 06:30", ctx)                         # heute schon vorbei

    def test_request_shape(self):
        api = FakeAPI(tool())
        hc.call_model(hc.AssistantConfig(enabled=True, api_key=KEY), "KONTEXT", [{"role": "user", "content": "x"}],
                      client_factory=lambda a: api)
        kw = api.calls[0]
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertEqual([t["name"] for t in kw["tools"]], ["propose_schedule"])
        self.assertTrue(kw["tools"][0]["strict"])
        self.assertEqual(kw["tool_choice"]["type"], "auto")
        self.assertTrue(kw["system"].endswith("KONTEXT"))
        self.assertNotIn(KEY, json.dumps(kw, default=str))


class LocalAnswerTest(DbCase):
    def test_what_is_planned(self):
        hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": "mo,di,mi,do,fr",
                            "date": None}, now=NOW)
        hs.create_schedule({"room_id": "kind", "setpoint": 19.0, "time": "11:00", "days": None,
                            "date": "2026-10-15"}, now=NOW)
        e = hs.list_schedules()
        a = hc.local_answer("Was ist für morgen geplant?", self.cfg, e, NOW)
        self.assertEqual(a, "Morgen (Mi 14.10.):\n  06:30  Wohnen → 21 °C")
        a = hc.local_answer("was ist geplant", self.cfg, e, NOW)
        self.assertIn("Übermorgen (Do 15.10.)\n  06:30  Wohnen → 21 °C\n  11:00  Kind → 19 °C", a)
        self.assertEqual(hc.local_answer("was ist geplant im Kind", self.cfg, e, NOW).count("Kind"), 2)
        self.assertEqual(hc.local_answer("Was ist heute geplant?", self.cfg, e, NOW), "Heute (Di 13.10.): nichts geplant.")
        self.assertIsNone(hc.local_answer("morgen Bad um 7 auf 21", self.cfg, e, NOW))
        self.assertIsNone(hc.local_answer("lösch die geplanten Pläne von morgen", self.cfg, e, NOW))


@unittest.skipUnless(hc.sdk_available(), "anthropic nicht installiert (Prod-Vorabtest vor pip install)")
class ApiErrorTest(unittest.TestCase):
    def setUp(self):
        import anthropic
        import httpx2
        self.A = anthropic
        self.req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        self.resp = lambda code, msg: httpx2.Response(code, request=self.req, headers={"request-id": "req_42"})
        self.acfg = hc.AssistantConfig(enabled=True, api_key=KEY, model="claude-haiku-5-5")

    def run_with(self, exc):
        with self.assertLogs("pv-controller.heating", level="WARNING") as logs:
            with self.assertRaises(hc.ChatError) as cm:
                hc.call_model(self.acfg, "ctx", [{"role": "user", "content": "x"}],
                              client_factory=lambda a: FakeAPI(exc=exc))
        out = "\n".join(logs.output)
        self.assertNotIn(KEY, out)
        self.assertNotIn(KEY, str(cm.exception))
        return cm.exception, out

    def test_404_invalid_model(self):
        e, out = self.run_with(self.A.NotFoundError(f"model: claude-haiku-5-5 (key {KEY})",
                                                    response=self.resp(404, ""), body=None))
        self.assertEqual(str(e), "Modell „claude-haiku-5-5“ nicht verfügbar – in Settings ändern.")
        self.assertEqual(e.kind, "model")
        self.assertIn("NotFoundError 404", out)
        self.assertIn("req_42", out)

    def test_invalid_key(self):
        e, _ = self.run_with(self.A.AuthenticationError(f"invalid x-api-key {KEY}", response=self.resp(401, ""),
                                                        body={"error": {"message": KEY}}))
        self.assertEqual(e.kind, "auth")

    def test_offline(self):
        e, _ = self.run_with(self.A.APIConnectionError(request=self.req))
        self.assertEqual(e.kind, "offline")

    def test_timeout(self):
        e, _ = self.run_with(self.A.APITimeoutError(request=self.req))
        self.assertEqual(e.kind, "timeout")
        self.assertIn("20 s", str(e))

    def test_bad_request_mentioning_model(self):
        e, _ = self.run_with(self.A.BadRequestError("model: invalid model name", response=self.resp(400, ""), body=None))
        self.assertEqual(e.kind, "model")

    def test_credit_balance_too_low(self):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message":
                "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing "
                "to upgrade or purchase credits."}}
        e, _ = self.run_with(self.A.BadRequestError("Error code: 400", response=self.resp(400, ""), body=body))
        self.assertEqual(e.kind, "billing")
        self.assertIn("Plans & Billing", str(e))

    def test_key_without_workspace(self):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message":
                "This API key is not scoped to a workspace, so this request must include the "
                "anthropic-workspace-id header with the ID of the workspace to use."}}
        e, out = self.run_with(self.A.BadRequestError("Error code: 400", response=self.resp(400, ""), body=body))
        self.assertEqual(e.kind, "auth")
        self.assertIn("assistant.workspace_id", str(e))
        self.assertIn("not scoped to a workspace", out)                      # Grund steht im Log

    def test_bad_request_shows_api_reason_without_key(self):
        body = {"type": "error", "error": {"type": "invalid_request_error",
                                           "message": f"tools.0.custom.input_schema: type list not supported ({KEY})"}}
        e, out = self.run_with(self.A.BadRequestError("Error code: 400", response=self.resp(400, ""), body=body))
        self.assertEqual(str(e), "Die API hat die Anfrage abgelehnt (400): "
                                 "tools.0.custom.input_schema: type list not supported (***)")
        self.assertIn("type list not supported (***)", out)


class WebChatTest(unittest.TestCase):
    """Endpunkte gegen temporäre config.json + DB, API gemockt."""

    setUp_web = th.WebTest.setUp
    tearDown_web = th.WebTest.tearDown

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        hs.init_tables()
        self.setUp_web()
        self.set_assistant(enabled=True, provider="anthropic", api_key=KEY, openai_api_key="",
                           model="claude-haiku-5-5", max_requests_per_day=3)
        self.api = FakeAPI(tool())
        self.factory = mock.Mock(side_effect=lambda a: self.api)
        self.p2 = [mock.patch.object(hc, "_default_client", self.factory),
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

    def set_assistant(self, **a):
        c = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        c["assistant"] = {**(c.get("assistant") or {}), **a}
        self.cfg_path.write_text(json.dumps(c), encoding="utf-8")

    def chat(self, *texts):
        msgs = [web._ChatMessage(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts)]
        return web.heating_chat(web._ChatRequest(messages=msgs))

    def log_text(self):
        p = self.dir / "app.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_switch_off_blocks_chat_and_status_shows_hint(self):
        self.set_assistant(enabled=False)
        with self.assertRaises(web.HTTPException) as cm:
            self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual(cm.exception.status_code, 409)
        st = web.heating_status(web.Response())["chat"]
        self.assertFalse(st["ready"])
        self.assertIn("Chat aktiv", st["hint"])
        self.factory.assert_not_called()

    def test_proposal_then_apply_creates_chat_schedules(self):
        self.api.tool_input = tool([entry("wohnen", 21, "07:00", self.tomorrow), entry("wohnen", 19, "11:00", self.tomorrow)],
                                   summary="Morgen Wohnen")
        res = self.chat("morgen Wohnen um 7 auf 21 Grad und um 11 wieder auf 19")
        self.assertEqual(res["type"], "proposal")
        self.assertEqual(hs.list_schedules(), [])                                # nichts ohne Klick
        self.assertFalse(res["schedules_enabled"])                               # UI-Hinweis
        self.assertEqual(res["chat"]["used_today"], 1)
        out = web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        self.assertEqual(len(out["created"]), 2)
        rows = hs.list_schedules()
        self.assertEqual({r["source"] for r in rows}, {"chat"})
        self.assertEqual(rows[0]["note"], "morgen Wohnen um 7 auf 21 Grad und um 11 wieder auf 19")
        self.assertIn("Zeitplan angelegt (Chat): Wohnen 07:00 → 21 °C", self.log_text())
        with self.assertRaises(web.HTTPException) as cm:                         # nur einmal übernehmen
            web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        self.assertEqual(cm.exception.status_code, 410)

    def test_question_is_returned_and_nothing_stored(self):
        self.api.tool_input = tool(question="Meinst du das Bad oder das Gäste-WC?")
        res = self.chat("Bad auf 21")
        self.assertEqual((res["type"], res["text"]), ("question", "Meinst du das Bad oder das Gäste-WC?"))
        self.assertEqual(hc._PROPOSALS, {})
        # Antwort geht mit dem Verlauf zurück
        self.api.tool_input = tool([entry("wohnen", 21, "07:00", self.tomorrow)])
        res = self.chat("Bad auf 21", "Meinst du das Bad oder das Gäste-WC?", "das Bad, morgen um 7")
        self.assertEqual(res["type"], "proposal")
        self.assertEqual([m["role"] for m in self.api.calls[-1]["messages"]], ["user", "assistant", "user"])

    def test_invalid_proposal_stores_nothing(self):
        self.api.tool_input = tool([entry("wohnen", 35, "07:00", self.tomorrow)])
        res = self.chat("morgen Wohnen auf 35 Grad")
        self.assertEqual(res["type"], "invalid")
        self.assertIn("außerhalb", res["text"])
        self.assertEqual(hc._PROPOSALS, {})

    def test_immediate_and_delete_after_confirmation(self):
        e = hs.create_schedule({"room_id": "kind", "setpoint": 19.0, "time": "06:00", "days": None,
                                "date": self.tomorrow})
        self.api.tool_input = tool(immediate=[{"room_id": "wohnen", "setpoint": 22}], delete_ids=[e["id"]])
        res = self.chat("jetzt Wohnen auf 22 und lösch den Kind-Plan morgen")
        self.assertEqual(self.fake.sent, [])                                     # erst nach Klick
        out = web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        self.assertEqual(self.fake.sent, [("W_Set", "22")])
        self.assertEqual(out["deleted"], [e["id"]])
        self.assertIn("Wohnen Soll → 22 °C (Chat)", self.log_text())

    def test_query_answered_from_db_without_api(self):
        hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": "06:30", "days": ",".join(hs.DAYS),
                            "date": None})
        res = self.chat("Was ist für morgen geplant?")
        self.assertEqual(res["type"], "answer")
        self.assertIn("06:30  Wohnen → 21 °C", res["text"])
        self.factory.assert_not_called()
        self.assertEqual(hc.used_today(), 0)                                     # zählt nicht

    def test_daily_limit_friendly(self):
        for _ in range(3):
            self.assertEqual(self.chat("x")["type"], "question")                # leerer Vorschlag → Rückfrage
        res = self.chat("noch einer")
        self.assertEqual(res["type"], "limit")
        self.assertIn("Tageslimit erreicht (3/3)", res["text"])
        self.assertEqual(len(self.api.calls), 3)
        st = web.heating_status(web.Response())["chat"]
        self.assertEqual((st["used_today"], st["limit"], st["ready"]), (3, 3, False))

    def test_missing_key_or_sdk(self):
        self.set_assistant(api_key="")
        res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual(res["type"], "unavailable")
        self.assertIn("API-Key fehlt", res["text"])
        self.set_assistant(api_key=KEY)
        with mock.patch.object(hc, "sdk_available", lambda *a: False):
            self.assertIn("anthropic", self.chat("morgen Wohnen um 7 auf 21")["text"])
        self.factory.assert_not_called()

    def test_model_error_shown_in_chat(self):
        self.factory.side_effect = None
        self.factory.return_value = None
        with mock.patch.object(hc, "call_model",
                               side_effect=hc.ChatError("Modell „x“ nicht verfügbar – in Settings ändern.", "model")):
            res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual((res["type"], res["kind"]), ("error", "model"))
        self.assertIn("in Settings ändern", res["text"])

    def test_key_never_leaves_backend(self):
        self.api.tool_input = tool([entry("wohnen", 21, "07:00", self.tomorrow)])
        res = self.chat("morgen Wohnen um 7 auf 21")
        blobs = [json.dumps(res, default=str), json.dumps(web.heating_status(web.Response()), default=str),
                 json.dumps(web.heating_config_get()), json.dumps(web.api_config_get()), self.log_text()]
        for b in blobs:
            self.assertNotIn(KEY, b)
        self.assertEqual(web.heating_config_get()["assistant"]["keys_set"], {"anthropic": True, "openai": False})

    def test_settings_switch_model_limit_keep_key(self):
        body = dict(enabled=True, setpoint_min=5, setpoint_max=26, rooms=th.ROOMS)
        web.heating_config_put(web._HeatingConfigUpdate(**body, assistant={"enabled": False, "model": "claude-opus-5-5",
                                                                           "max_requests_per_day": 20, "api_key": "HACK",
                                                                           "openai_api_key": "HACK2"}))
        saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))["assistant"]
        self.assertEqual(saved, {"enabled": False, "provider": "anthropic", "openai_api_key": "", "api_key": KEY,
                                 "model": "claude-opus-5-5", "max_requests_per_day": 20, "workspace_id": ""})
        self.assertIn("Chat deaktiviert (Settings)", self.log_text())
        with self.assertRaises(web.HTTPException):
            web.heating_config_put(web._HeatingConfigUpdate(**body, assistant={"model": "kein modell!"}))


@unittest.skipUnless(hc.sdk_available(), "anthropic nicht installiert")
class ClientConfigTest(unittest.TestCase):
    def test_workspace_header_only_when_set(self):
        c = hc._default_client(hc.AssistantConfig(enabled=True, api_key=KEY, workspace_id="wrkspc_abc123"))
        self.assertEqual(c.default_headers.get("anthropic-workspace-id"), "wrkspc_abc123")
        c = hc._default_client(hc.AssistantConfig(enabled=True, api_key=KEY))
        self.assertNotIn("anthropic-workspace-id", c.default_headers)

    def test_timeout_20s_no_retry(self):
        c = hc._default_client(hc.AssistantConfig(enabled=True, api_key=KEY))
        self.assertEqual(c.max_retries, 0)
        self.assertEqual((c.timeout.read, c.timeout.connect), (20.0, 5.0))

    def test_chat_endpoints_run_in_threadpool(self):
        """Synchrone Endpunkte laufen im Threadpool: ein hängender API-Aufruf blockiert die Event-Loop nicht."""
        import inspect
        self.assertFalse(inspect.iscoroutinefunction(web.heating_chat))
        self.assertFalse(inspect.iscoroutinefunction(web.heating_chat_apply))


# ── OpenAI (ChatGPT) ────────────────────────────────────────────────────────

OKEY = "sk-proj-OPENAITESTKEY-987654321"


class FakeOpenAI:
    """Wie openai.OpenAI: chat.completions.create → Tool-Aufruf (arguments als JSON-String)."""

    def __init__(self, tool_input=None, text=None, exc=None, finish_reason="tool_calls", refusal=None):
        self.tool_input, self.text, self.exc, self.finish_reason, self.refusal = tool_input, text, exc, finish_reason, refusal
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        calls = None
        if self.tool_input is not None:
            calls = [SimpleNamespace(type="function", id="call_1", function=SimpleNamespace(
                name="propose_schedule", arguments=json.dumps(self.tool_input)))]
        msg = SimpleNamespace(content=self.text, tool_calls=calls, refusal=self.refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish_reason)])


OCFG = hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OKEY, api_key=KEY, model="gpt-4.1-mini")


class OpenAIRequestTest(unittest.TestCase):
    def call(self, api, history=None):
        return hc.call_model(OCFG, "KONTEXT", history or [{"role": "user", "content": "x"}], client_factory=lambda a: api)

    def test_request_shape(self):
        api = FakeOpenAI(tool())
        self.call(api, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b?"},
                        {"role": "user", "content": "c"}])
        kw = api.calls[0]
        self.assertEqual(kw["model"], "gpt-4.1-mini")
        self.assertEqual([m["role"] for m in kw["messages"]], ["system", "user", "assistant", "user"])
        self.assertTrue(kw["messages"][0]["content"].endswith("KONTEXT"))
        self.assertEqual(kw["tools"][0]["type"], "function")
        fn = kw["tools"][0]["function"]
        self.assertEqual((fn["name"], fn["strict"]), ("propose_schedule", True))
        self.assertIs(fn["parameters"], hc.TOOL["input_schema"])                 # gleiches Schema wie bei Claude
        self.assertEqual(kw["tool_choice"], {"type": "function", "function": {"name": "propose_schedule"}})
        self.assertFalse(kw["parallel_tool_calls"])
        self.assertNotIn("temperature", kw)
        self.assertNotIn(OKEY, json.dumps(kw, default=str))

    def test_tool_arguments_parsed(self):
        inp = tool([entry(date_="2026-10-14")], summary="s")
        self.assertEqual(self.call(FakeOpenAI(inp)), inp)

    def test_text_only_becomes_question_and_refusal_and_length(self):
        self.assertEqual(self.call(FakeOpenAI(text="Welcher Raum?", finish_reason="stop"))["question"], "Welcher Raum?")
        with self.assertRaises(hc.ChatError):
            self.call(FakeOpenAI(refusal="nein", finish_reason="stop"))
        with self.assertRaises(hc.ChatError) as cm:
            self.call(FakeOpenAI(finish_reason="length"))
        self.assertIn("unvollständig", str(cm.exception))

    def test_broken_arguments(self):
        api = FakeOpenAI(tool())
        api.tool_input = None
        orig = api.create

        def bad(**kw):
            r = orig(**kw)
            r.choices[0].message.tool_calls = [SimpleNamespace(type="function", function=SimpleNamespace(
                name="propose_schedule", arguments="{kaputt"))]
            return r
        api.create = bad
        with self.assertRaises(hc.ChatError):
            self.call(api)


@unittest.skipUnless(hc.sdk_available("openai"), "openai nicht installiert (Prod-Vorabtest vor pip install)")
class OpenAIErrorTest(unittest.TestCase):
    def setUp(self):
        import httpx2
        import openai
        self.O = openai
        self.req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        self.resp = lambda code: httpx2.Response(code, request=self.req, headers={"x-request-id": "req_oa"})

    def run_with(self, exc):
        with self.assertLogs("pv-controller.heating", level="WARNING") as logs:
            with self.assertRaises(hc.ChatError) as cm:
                hc.call_model(OCFG, "ctx", [{"role": "user", "content": "x"}], client_factory=lambda a: FakeOpenAI(exc=exc))
        out = "\n".join(logs.output)
        for k in (OKEY, KEY):
            self.assertNotIn(k, out)
            self.assertNotIn(k, str(cm.exception))
        return cm.exception, out

    def test_insufficient_quota(self):
        body = {"message": f"You exceeded your current quota ({OKEY})", "type": "insufficient_quota",
                "code": "insufficient_quota"}
        e, out = self.run_with(self.O.RateLimitError("quota", response=self.resp(429), body=body))
        self.assertEqual(e.kind, "billing")
        self.assertIn("OpenAI", str(e))
        self.assertIn("ChatGPT RateLimitError 429", out)
        self.assertIn("req_oa", out)

    def test_plain_rate_limit(self):
        e, _ = self.run_with(self.O.RateLimitError("slow down", response=self.resp(429),
                                                   body={"message": "Rate limit reached", "code": "rate_limit_exceeded"}))
        self.assertEqual(e.kind, "busy")

    def test_invalid_key_names_openai_field(self):
        e, _ = self.run_with(self.O.AuthenticationError("bad", response=self.resp(401),
                                                        body={"message": f"Incorrect API key provided: {OKEY}"}))
        self.assertEqual(e.kind, "auth")
        self.assertIn("assistant.openai_api_key", str(e))

    def test_model_not_found(self):
        e, _ = self.run_with(self.O.NotFoundError("nf", response=self.resp(404),
                                                  body={"message": "The model `gpt-x` does not exist", "code": "model_not_found"}))
        self.assertEqual(str(e), "Modell „gpt-4.1-mini“ nicht verfügbar – in Settings ändern.")

    def test_bad_request_reason_shown(self):
        e, _ = self.run_with(self.O.BadRequestError("bad", response=self.resp(400),
                                                    body={"message": "Invalid schema for function 'propose_schedule'"}))
        self.assertIn("Invalid schema for function", str(e))

    def test_timeout_and_offline(self):
        e, _ = self.run_with(self.O.APITimeoutError(request=self.req))
        self.assertEqual(e.kind, "timeout")
        self.assertIn("ChatGPT antwortet nicht", str(e))
        e, _ = self.run_with(self.O.APIConnectionError(request=self.req))
        self.assertIn("api.openai.com", str(e))

    def test_client_config(self):
        c = hc._default_client(OCFG)
        self.assertEqual(c.max_retries, 0)
        self.assertEqual((c.timeout.read, c.timeout.connect), (20.0, 5.0))
        self.assertEqual(c.api_key, OKEY)


class OpenAIWebTest(WebChatTest):
    """Gleicher Ablauf über die Endpunkte mit provider=openai."""

    def setUp(self):
        super().setUp()
        self.set_assistant(provider="openai", openai_api_key=OKEY, model="gpt-4.1-mini")
        self.api = FakeOpenAI(tool())

    def test_flow_with_openai(self):
        self.api.tool_input = tool([entry("wohnen", 21, "07:00", self.tomorrow)])
        res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual(res["type"], "proposal")
        self.assertEqual((res["chat"]["provider"], res["chat"]["provider_name"]), ("openai", "ChatGPT"))
        out = web.heating_chat_apply(web._ChatApply(proposal_id=res["proposal"]["id"]))
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(self.factory.call_args[0][0].provider, "openai")
        blob = json.dumps(res, default=str) + json.dumps(web.heating_config_get()) + self.log_text()
        self.assertNotIn(OKEY, blob)
        self.assertEqual(web.heating_config_get()["assistant"]["keys_set"], {"anthropic": True, "openai": True})

    def test_missing_openai_key(self):
        self.set_assistant(openai_api_key="")
        res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual(res["type"], "unavailable")
        self.assertIn("assistant.openai_api_key", res["text"])

    # aus WebChatTest geerbte Anthropic-spezifische Tests hier nicht wiederholen
    test_key_never_leaves_backend = None
    test_settings_switch_model_limit_keep_key = None
    test_missing_key_or_sdk = None
    test_question_is_returned_and_nothing_stored = None


class ParseTest(unittest.TestCase):
    def test_defaults_and_invalid(self):
        a = hc.parse_assistant({})
        self.assertEqual((a.enabled, a.model, a.max_requests_per_day, a.key_set), (False, "claude-haiku-5-5", 100, False))
        o = hc.parse_assistant({"assistant": {"provider": "openai", "openai_api_key": " sk-x "}})
        self.assertEqual((o.provider, o.model, o.key, o.key_field), ("openai", "gpt-6-luna", "sk-x", "openai_api_key"))
        self.assertEqual(hc.parse_assistant({"assistant": {"workspace_id": " wrkspc_01ABC "}}).workspace_id, "wrkspc_01ABC")
        for bad in ({"enabled": "ja"}, {"model": "x y"}, {"max_requests_per_day": 0}, {"api_key": 5},
                    {"workspace_id": "wrkspc 1"}, {"workspace_id": 7}, {"provider": "gemini"},
                    {"openai_api_key": 3}):
            with self.assertRaises(ValueError):
                hc.parse_assistant({"assistant": bad})


if __name__ == "__main__":
    unittest.main()
