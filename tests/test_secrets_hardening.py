"""Secrets: sk-…-Keys (auch gekürzt) nie im Log; config.json und Backups nur für den Besitzer lesbar (0600)."""
import io
import json
import logging
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import heating_chat as hc
import main as main_mod
import redact
import web

OPENAI_KEY = "sk-proj-AbCdEfGh1234567890_xyz-TESTKEY"
ANTHROPIC_KEY = "sk-ant-api03-ZyXwVu9876543210_abc-TESTKEY"
# So zitiert OpenAI einen ungültigen Key in der 401-Meldung: Anfang und Ende bleiben stehen
OPENAI_TRUNCATED = "Incorrect API key provided: sk-proj-AbCd********************************Tkey. You can find your API key at https://platform.openai.com/account/api-keys."


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


class RedactTest(unittest.TestCase):
    def test_full_and_truncated_keys(self):
        for text in (f"key {OPENAI_KEY} invalid", f"x-api-key: {ANTHROPIC_KEY}", OPENAI_TRUNCATED,
                     "sk-proj-1234...abcd", "sk-ant-api03-Ab…yZ", "sk-…wxyz", "(sk-abc123)", "Key=sk-abc123."):
            with self.subTest(text=text):
                out = redact.redact(text)
                self.assertIn("sk-***", out)
                for frag in ("AbCd", "Tkey", "TESTKEY", "1234", "wxyz", "abc123", "yZ"):
                    self.assertNotIn(frag, out)
        self.assertEqual(redact.redact(OPENAI_TRUNCATED),
                         "Incorrect API key provided: sk-***. You can find your API key at "
                         "https://platform.openai.com/account/api-keys.")

    def test_normal_text_untouched(self):
        for text in ("task-123 erledigt", "risk-free", "desk-lamp an", "Wohnen Soll → 21 °C", "sk- allein", ""):
            self.assertEqual(redact.redact(text), text)

    def _logger(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        redact.install(h)
        redact.install(h)                                       # nie doppelt
        self.assertEqual(sum(isinstance(f, redact.RedactFilter) for f in h.filters), 1)
        lg = logging.getLogger("test.redact." + self.id())
        lg.handlers[:] = [h]
        lg.propagate = False
        lg.setLevel(logging.INFO)
        return lg, buf

    def test_log_filter_message_args_and_traceback(self):
        lg, buf = self._logger()
        lg.warning("Chat: API-Fehler %s: %s", "AuthenticationError", OPENAI_TRUNCATED)
        lg.info("Header %s", {"authorization": f"Bearer {ANTHROPIC_KEY}"})
        try:
            raise RuntimeError(f"401 for key {OPENAI_KEY}")
        except RuntimeError:
            lg.exception("Unerwarteter Fehler")
        out = buf.getvalue()
        self.assertEqual(out.count("sk-***"), 3)
        for secret in (OPENAI_KEY, ANTHROPIC_KEY, "AbCd", "Tkey", "TESTKEY"):
            self.assertNotIn(secret, out)
        self.assertIn("RuntimeError: 401 for key sk-***", out)      # Traceback bleibt lesbar
        self.assertIn("Traceback", out)

    def test_cron_and_web_logging_have_the_filter(self):
        with tempfile.TemporaryDirectory() as d:
            root = logging.getLogger()
            saved = root.handlers[:]
            try:
                main_mod.setup_logging(str(Path(d) / "cron.log"))
                self.assertTrue(all(any(isinstance(f, redact.RedactFilter) for f in h.filters) for h in root.handlers))
                logging.getLogger("pv-controller.heating").warning("Fehler mit %s", OPENAI_TRUNCATED)
                for h in root.handlers:
                    h.flush()
                text = (Path(d) / "cron.log").read_text(encoding="utf-8")
                self.assertIn("sk-***", text)
                self.assertNotIn("AbCd", text)
            finally:
                for h in root.handlers:
                    h.close()
                root.handlers[:] = saved

    def test_app_log_file_is_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(runtime=SimpleNamespace(log_file=str(Path(d) / "app.log")))
            with mock.patch.object(web, "_load_cfg", lambda: cfg):
                web._app_log(f"Chat-Fehler: {OPENAI_TRUNCATED}", logging.WARNING)
            text = (Path(d) / "app.log").read_text(encoding="utf-8")
        self.assertIn("sk-***", text)
        self.assertNotIn("AbCd", text)


class ChatErrorTextTest(unittest.TestCase):
    """Fehlertexte der APIs (Log und Chat): gekürzter Key wird maskiert – bei OpenAI und Anthropic."""

    def _err(self, message):
        e = Exception(message)
        e.body = {"error": {"message": message}}
        e.status_code = 401
        e.response = SimpleNamespace(headers={"x-request-id": "req_1"})
        return e

    def test_api_message_openai_and_anthropic(self):
        for cfg, msg in ((hc.AssistantConfig(enabled=True, provider="openai", openai_api_key=OPENAI_KEY), OPENAI_TRUNCATED),
                         (hc.AssistantConfig(enabled=True, provider="anthropic", api_key=ANTHROPIC_KEY),
                          f"invalid x-api-key: sk-ant-api03-ZyXw...KEY (full: {ANTHROPIC_KEY})")):
            with self.subTest(provider=cfg.provider):
                out = hc._api_message(self._err(msg), cfg)
                self.assertIn("sk-***", out)
                for frag in ("AbCd", "Tkey", "ZyXw", "TESTKEY"):
                    self.assertNotIn(frag, out)

    def test_log_line_has_no_fragment(self):
        cfg = hc.AssistantConfig(enabled=True, provider="openai", openai_api_key="sk-proj-OTHER-key-000")
        with self.assertLogs("pv-controller.heating", level="WARNING") as cm:
            hc._log_api_error(self._err(OPENAI_TRUNCATED), cfg)       # zitierter Key ≠ konfigurierter Key
        line = cm.output[0]
        self.assertIn("sk-***", line)
        self.assertNotIn("AbCd", line)
        self.assertNotIn("Tkey", line)
        self.assertIn("request-id req_1", line)


class ConfigPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = self.dir / "config.json"
        self.cfg.write_text(json.dumps({"openhab": {"token": "oh.geheim"}, "runtime": {"dry_run": True}}))
        os.chmod(self.cfg, 0o644)                                   # Altbestand: für alle lesbar
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_write_config_makes_config_and_backup_private(self):
        old_umask = os.umask(0o022)                                 # auch bei großzügiger umask
        try:
            (self.dir / "config.json.tmp").write_text("alt")        # liegen gebliebene Temp-Datei mit 644
            os.chmod(self.dir / "config.json.tmp", 0o644)
            name = web._write_config({"openhab": {"token": "oh.neu"}, "runtime": {"dry_run": True}})
        finally:
            os.umask(old_umask)
        backup = self.dir / name
        self.assertEqual(mode(self.cfg), 0o600)
        self.assertEqual(mode(backup), 0o600)
        self.assertEqual(json.loads(backup.read_text())["openhab"]["token"], "oh.geheim")   # Backup = alter Stand
        self.assertEqual(json.loads(self.cfg.read_text())["openhab"]["token"], "oh.neu")
        self.assertFalse((self.dir / "config.json.tmp").exists())

    def test_deploy_secure_configs(self):
        """deploy.sh: config*.json und Backups → 600, config.example.json und andere Dateien bleiben."""
        script = (Path(web.PROJECT_ROOT) / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        start = script.index("secure_configs() {")
        func = script[start:script.index("\n}\n", start) + 3]
        names = {"config.json": 0o600, "config.backup-20261013-101112.json": 0o600,
                 "config.json.bak-20261013-133414-vor-shutters": 0o600, "config.json.tmp": 0o600,
                 "config.example.json": 0o644, "pvcontroller.db": 0o644, "README.md": 0o644}
        for n in names:
            (self.dir / n).write_text("{}")
            os.chmod(self.dir / n, 0o644)
        subprocess.run(["bash", "-c", f'set -euo pipefail\nPROD="$1"\n{func}\nsecure_configs', "x", str(self.dir)],
                       check=True)
        self.assertEqual({n: mode(self.dir / n) for n in names}, names)


if __name__ == "__main__":
    unittest.main()
