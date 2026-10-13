"""Secrets in /api/config: maskiert ausliefern, Maske zurück = alter Wert bleibt."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import web

MASK = web._SECRET_MASK


class ConfigSecretsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        raw["solax"]["pwd"] = "SOLAX1"
        raw["solax2"] = {"enabled": True, "url": "http://192.168.2.141", "pwd": "SOLAX2", "name": "Pergola"}
        raw["auth_enabled"] = True
        raw["auth_user"] = "admin"
        raw["auth_password"] = "geheim"
        raw["openhab"] = {"url": "http://oh:8080", "token": "oh.TOKEN", "timeout_s": 3}
        raw["assistant"] = {"enabled": True, "api_key": "sk-ant-KEY", "model": "claude-haiku-5-5"}
        raw["zendure"] = {**raw.get("zendure", {}), "secret_hint": ""}   # leer bleibt leer
        self.cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def saved(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))

    def test_get_masks_all_secret_fields(self):
        c = web.api_config_get()
        self.assertEqual(c["solax"]["pwd"], MASK)
        self.assertEqual(c["solax2"]["pwd"], MASK)
        self.assertEqual(c["auth_password"], MASK)
        self.assertEqual(c["openhab"]["token"], MASK)
        self.assertEqual(c["assistant"]["api_key"], MASK)
        self.assertEqual(c["zendure"]["secret_hint"], "")          # leer → nicht maskiert
        self.assertEqual(c["solax"]["url"], self.saved()["solax"]["url"])  # Rest unverändert
        self.assertEqual(c["auth_user"], "admin")
        self.assertNotIn("geheim", json.dumps(c))
        self.assertNotIn("TOKEN", json.dumps(c))

    def test_pattern(self):
        for k in ("token", "api_key", "auth_password", "pwd", "client_secret", "passwd", "Token"):
            self.assertTrue(web._is_secret_key(k), k)
        for k in ("url", "name", "user", "auth_user", "enabled", "sn"):
            self.assertFalse(web._is_secret_key(k), k)

    def test_masked_values_sent_back_keep_old_values(self):
        """Settings laden → unverändert speichern: Secrets bleiben, andere Änderungen greifen."""
        c = web.api_config_get()
        patch = web.ConfigUpdate(
            runtime={"dry_run": not c["runtime"]["dry_run"]},
            solax={"url": c["solax"]["url"], "pwd": c["solax"]["pwd"]},
            solax2={"enabled": True, "url": c["solax2"]["url"], "pwd": c["solax2"]["pwd"], "name": "Pergola"},
            auth={"enabled": True, "user": "admin", "password": c["auth_password"]},
        )
        res = web.api_config_put(patch)
        s = self.saved()
        self.assertEqual(s["solax"]["pwd"], "SOLAX1")
        self.assertEqual(s["solax2"]["pwd"], "SOLAX2")
        self.assertEqual(s["auth_password"], "geheim")
        self.assertEqual(s["openhab"]["token"], "oh.TOKEN")
        self.assertEqual(s["assistant"]["api_key"], "sk-ant-KEY")
        self.assertEqual(s["runtime"]["dry_run"], not c["runtime"]["dry_run"])
        self.assertNotIn("geheim", json.dumps(res))   # PUT-Antwort ebenfalls maskiert
        self.assertEqual(res["config"]["solax"]["pwd"], MASK)

    def test_new_value_overwrites_and_empty_password_still_disables(self):
        web.api_config_put(web.ConfigUpdate(solax={"pwd": "NEU"}, auth={"password": ""}))
        s = self.saved()
        self.assertEqual(s["solax"]["pwd"], "NEU")
        self.assertEqual(s["auth_password"], "")

    def test_solax2_test_uses_stored_password_for_mask(self):
        with mock.patch("clients.solax_client.SolaxClient") as cls:
            cls.return_value.get_realtime_data.return_value = None
            with self.assertRaises(web.HTTPException):   # None → 503, uns interessiert nur das Passwort
                web.api_solax2_test(web.Solax2TestRequest(url="http://192.168.2.141", pwd=MASK), web.Response())
        self.assertEqual(cls.call_args[0][1], "SOLAX2")


if __name__ == "__main__":
    unittest.main()
