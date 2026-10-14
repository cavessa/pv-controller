"""Chat: Ersatzmodell (assistant.fallback_model), wenn die API ein Modell nicht (mehr) kennt, und
reasoning_effort="none" für neuere GPT-Modelle (Tool-Aufruf in Chat Completions). Kein Netz, API gemockt."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import heating_chat as hc
import web
import tests.test_heating_chat as thc
from tests.test_heating_chat import OKEY, FakeOpenAI, entry, tool

TZ = hc.hs.TZ
NOW = datetime(2027, 1, 14, 10, 0, tzinfo=TZ)
CFG = hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OKEY, model="gpt-4.1-mini",
                         explain_model="gpt-4.1", fallback_model="gpt-6-luna")


class Gone(FakeOpenAI):
    """Kennt die Modelle in ``gone`` nicht (404 model_not_found), sonst Tool-Aufruf wie FakeOpenAI."""

    def __init__(self, gone, *a, **kw):
        super().__init__(*a, **kw)
        self.gone = set(gone)

    def create(self, **kw):
        if kw["model"] in self.gone:
            import httpx2
            import openai
            self.calls.append(kw)
            req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
            raise openai.NotFoundError("nf", response=httpx2.Response(404, request=req), body={
                "message": f"The model `{kw['model']}` has been deprecated", "code": "model_not_found"})
        return super().create(**kw)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_fallback_model_optional_and_checked(self):
        base = {"provider": "openai"}
        self.assertEqual(hc.parse_assistant({"assistant": base}).fallback_model, "")
        self.assertEqual(hc.parse_assistant({"assistant": {**base, "fallback_model": "gpt-6-luna"}}).fallback_model,
                         "gpt-6-luna")
        with self.assertRaises(ValueError):
            hc.parse_assistant({"assistant": {**base, "fallback_model": "GPT 6!"}})


class ReasoningTest(unittest.TestCase):
    def test_reasoning_none_only_for_newer_models(self):
        for m in ("gpt-6-luna", "gpt-6-sol", "gpt-5.4-mini", "gpt-5.1", "gpt-6.1-sol"):
            self.assertEqual(hc._openai_extra(m), {"reasoning_effort": "none"}, m)
        for m in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini", "gpt-5", "gpt-5-mini", "o4-mini"):
            self.assertEqual(hc._openai_extra(m), {}, m)

    def test_request_carries_reasoning_none(self):
        api = FakeOpenAI(tool())
        hc.call_model(hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OKEY, model="gpt-6-luna"),
                      "ctx", [{"role": "user", "content": "x"}], client_factory=lambda a: api)
        self.assertEqual(api.calls[0]["reasoning_effort"], "none")
        api = FakeOpenAI(tool())
        hc.call_model(hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OKEY, model="gpt-4.1-mini"),
                      "ctx", [{"role": "user", "content": "x"}], client_factory=lambda a: api)
        self.assertNotIn("reasoning_effort", api.calls[0])


@unittest.skipUnless(hc.sdk_available("openai"), "openai nicht installiert")
class FallbackTest(DbCase):
    def call(self, api, acfg=CFG, explain=False, now=NOW):
        return hc.call_model(acfg, "ctx", [{"role": "user", "content": "x"}], client_factory=lambda a: api,
                             explain=explain, now=now)

    def test_model_gone_switches_to_fallback_and_warns(self):
        api = Gone({"gpt-4.1-mini"}, tool())
        with self.assertLogs("pv-controller.heating", level="WARNING") as logs:
            out = self.call(api)
        self.assertEqual([c["model"] for c in api.calls], ["gpt-4.1-mini", "gpt-6-luna"])
        self.assertEqual(api.calls[1]["reasoning_effort"], "none")
        self.assertEqual(out["_fallback"], {"model": "gpt-4.1-mini", "fallback": "gpt-6-luna"})
        log = "\n".join(logs.output)
        self.assertIn("Ersatzmodell „gpt-6-luna“", log)
        self.assertNotIn(OKEY, log)
        w = hc.model_warning(CFG)
        self.assertIn("„gpt-4.1-mini“ nicht verfügbar (seit 14.01. 10:00)", w)
        self.assertIn("Ersatzmodell „gpt-6-luna“", w)
        self.assertIn("assistant.model ", w)
        self.assertNotIn("explain_model", w)

    def test_known_gone_goes_straight_to_fallback_then_rechecks(self):
        api = Gone({"gpt-4.1-mini"}, tool())
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            self.call(api)
        api.calls.clear()
        out = self.call(api, now=NOW + timedelta(hours=1))
        self.assertEqual([c["model"] for c in api.calls], ["gpt-6-luna"])      # kein zweiter 404
        self.assertIn("_fallback", out)
        api.calls.clear()                                                       # nach 24 h: wieder versuchen
        api.gone.clear()
        out = self.call(api, now=NOW + timedelta(hours=25))
        self.assertEqual([c["model"] for c in api.calls], ["gpt-4.1-mini"])
        self.assertNotIn("_fallback", out)
        self.assertIsNone(hc.model_warning(CFG))

    def test_explain_model_gone(self):
        api = Gone({"gpt-4.1"}, tool())
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            out = self.call(api, explain=True)
        self.assertEqual([c["model"] for c in api.calls], ["gpt-4.1", "gpt-6-luna"])
        self.assertEqual(api.calls[1]["tools"][0]["function"]["parameters"], hc.TOOL_EXPLAIN["input_schema"])
        self.assertEqual(out["_fallback"]["model"], "gpt-4.1")
        self.assertIn("assistant.explain_model", hc.model_warning(CFG))
        api.calls.clear()                                                       # normale Befehle unberührt
        self.call(api)
        self.assertEqual([c["model"] for c in api.calls], ["gpt-4.1-mini"])

    def test_without_fallback_error_as_before(self):
        api = Gone({"gpt-4.1-mini"}, tool())
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            with self.assertRaises(hc.ChatError) as cm:
                self.call(api, acfg=hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OKEY,
                                                       model="gpt-4.1-mini"))
        self.assertEqual(cm.exception.kind, "model")
        self.assertEqual(len(api.calls), 1)

    def test_fallback_also_gone(self):
        api = Gone({"gpt-4.1-mini", "gpt-6-luna"}, tool())
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            with self.assertRaises(hc.ChatError) as cm:
                self.call(api)
        self.assertIn("„gpt-6-luna“", str(cm.exception))
        self.assertEqual(len(api.calls), 2)

    def test_other_errors_no_fallback(self):
        import httpx2
        import openai
        req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        api = FakeOpenAI(exc=openai.RateLimitError("slow", response=httpx2.Response(429, request=req),
                                                   body={"message": "Rate limit reached"}))
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            with self.assertRaises(hc.ChatError) as cm:
                self.call(api)
        self.assertEqual(cm.exception.kind, "busy")
        self.assertEqual(len(api.calls), 1)
        self.assertIsNone(hc.model_warning(CFG))

    def test_warning_gone_when_config_changes(self):
        hc.mark_unavailable("gpt-4.1-mini", "gpt-6-luna", NOW)
        self.assertIsNotNone(hc.model_warning(CFG))
        self.assertIsNone(hc.model_warning(hc.AssistantConfig(provider="openai", model="gpt-6-luna",
                                                              explain_model="gpt-6-sol", fallback_model="gpt-6-luna")))


@unittest.skipUnless(hc.sdk_available("openai"), "openai nicht installiert")
class FallbackWebTest(thc.OpenAIWebTest):
    def test_web_reply_and_status_carry_warning(self):
        self.set_assistant(fallback_model="gpt-6-luna")
        self.api = Gone({"gpt-4.1-mini"}, tool([entry("wohnen", 21, "07:00", self.tomorrow)]))
        with self.assertLogs("pv-controller.heating", level="WARNING"):
            res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertEqual(res["type"], "proposal")
        self.assertIn("Ersatzmodell „gpt-6-luna“", res["fallback"])
        self.assertIn("Ersatzmodell „gpt-6-luna“", res["chat"]["warning"])
        st = web.heating_chat_status(web.Response())
        self.assertIn("gpt-4.1-mini", st["warning"])
        self.assertEqual(st["fallback_model"], "gpt-6-luna")
        self.assertTrue(st["ready"])                                  # Chat bleibt nutzbar
        self.assertNotIn(OKEY, str(res) + str(st))

    def test_no_warning_normally(self):
        res = self.chat("morgen Wohnen um 7 auf 21")
        self.assertNotIn("fallback", res)
        self.assertIsNone(res["chat"]["warning"])



for _n in dir(thc.OpenAIWebTest):                                   # geerbte Tests nicht doppelt laufen lassen
    if _n.startswith("test_") and _n not in FallbackWebTest.__dict__:
        setattr(FallbackWebTest, _n, None)


if __name__ == "__main__":
    unittest.main()
