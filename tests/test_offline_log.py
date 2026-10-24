"""Offline-Meldungen der Solax-Clients: nur beim Wechsel und danach höchstens alle 30 min (auch über Cron-Läufe)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from clients import solax_client as sc

URL = "http://192.168.2.141"


def raw_x1():
    raw = [0] * 100
    raw[2] = 300
    return raw


class OfflineLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "data" / "offline_log_state.json"
        self.patches = [mock.patch.object(sc, "_OFFLINE_STATE_PATH", self.state),
                        mock.patch.object(sc, "_offline_state", None),
                        mock.patch.object(sc.time, "sleep"),
                        mock.patch.object(sc.requests, "post", side_effect=requests.ConnectTimeout("t"))]
        self.post = [p.start() for p in self.patches][-1]
        sc._cache.clear(); sc._fail_until.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        sc._cache.clear(); sc._fail_until.clear()
        self.tmp.cleanup()

    def read(self):
        sc._fail_until.clear()                     # neuer Cron-Lauf = neuer Prozess ohne Backoff
        return sc.SolaxClient(URL, "x", 3).get_realtime_data()

    def msgs(self, cm):
        return [r.getMessage() for r in cm.records if r.levelname in ("WARNING", "ERROR", "INFO")]

    def test_one_message_on_going_offline_then_silence(self):
        with self.assertLogs("clients.solax_client", level="DEBUG") as cm:
            for _ in range(6):
                self.read()
        m = self.msgs(cm)
        self.assertEqual(len(m), 1)
        self.assertIn("nicht erreichbar", m[0])
        self.assertIn("30 min", m[0])

    def test_state_survives_new_processes(self):
        with self.assertLogs("clients.solax_client", level="DEBUG"):
            self.read()
        self.assertIn(URL, " ".join(json.loads(self.state.read_text())))
        sc._offline_state = None                    # neuer Prozess liest die Datei
        with self.assertLogs("clients.solax_client", level="DEBUG") as cm:
            self.read()
        self.assertEqual(self.msgs(cm), [])

    def test_repeat_after_30_minutes(self):
        t = [1000.0]
        with mock.patch.object(sc.time, "time", lambda: t[0]), self.assertLogs("clients.solax_client", level="DEBUG") as cm:
            self.read()
            t[0] += 29 * 60; self.read()
            t[0] += 2 * 60;  self.read()
        m = self.msgs(cm)
        self.assertEqual(len(m), 2)
        self.assertIn("weiterhin", m[1])

    def test_recovery_is_reported_once(self):
        with self.assertLogs("clients.solax_client", level="DEBUG"):
            self.read()
        ok = mock.Mock(); ok.json.return_value = {"Data": raw_x1(), "type": 4}
        sc._fail_until.clear()
        with mock.patch.object(sc.requests, "post", return_value=ok), \
                self.assertLogs("clients.solax_client", level="DEBUG") as cm:
            sc.SolaxClient(URL, "x", 3).get_realtime_data()
            sc._cache.clear()
            sc.SolaxClient(URL, "x", 3).get_realtime_data()
        m = self.msgs(cm)
        self.assertEqual(len(m), 1)
        self.assertIn("wieder erreichbar", m[0])
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_unwritable_state_file_never_breaks_reading(self):
        with mock.patch.object(sc, "_OFFLINE_STATE_PATH", Path("/proc/nope/x.json")), \
                self.assertLogs("clients.solax_client", level="DEBUG"):
            self.assertIsNone(self.read())


if __name__ == "__main__":
    unittest.main()
