"""Kaskade bei dry_run: Entscheidung wie sonst (DB, cascade_log), aber kein Shelly-Schaltbefehl."""
import unittest
from unittest import mock

import db
from tests import test_cascade_profiles as tcp


class CascadeDryRunTest(unittest.TestCase):
    setUp_base = tcp.ServiceIntegrationTest.setUp
    tearDown = tcp.ServiceIntegrationTest.tearDown
    service = tcp.ServiceIntegrationTest.service

    def setUp(self):
        self.setUp_base()
        self.raw["zendure"] = {"enabled": False}
        db.create_cascade_device("pumpe", "Pumpe", "shelly_gen2", 500, 5, ip_address="192.168.2.250")

    def run_with(self, dry_run, override=None):
        self.raw["runtime"]["dry_run"] = dry_run
        svc = self.service(False, 45.0, -3000.0)
        svc.shelly = mock.Mock()
        svc.shelly.get_status.return_value = {"power": 0.0, "on": False}
        if override:
            svc._active_override = lambda d: override if d["id"] == "pumpe" else None
        with self.assertLogs("cascade_service", level="INFO") as logs:
            svc.run_cascade()
        return svc, "\n".join(logs.output)

    def test_dry_run_decides_but_does_not_switch(self):
        svc, out = self.run_with(True)
        svc.shelly.turn_on.assert_not_called()
        svc.shelly.turn_off.assert_not_called()
        svc.shelly.get_status.assert_called()                      # Lesen bleibt erlaubt
        self.assertTrue(db.get_cascade_device("pumpe")["is_on"])   # Entscheidung unverändert
        self.assertIn("turn_on", [e["action"] for e in db.get_cascade_log() if e["device_id"] == "pumpe"])
        self.assertIn("DRY-RUN: Kaskade würde Shelly Pumpe einschalten (192.168.2.250, Kanal 0)", out)

    def test_same_decision_switches_without_dry_run(self):
        svc, _ = self.run_with(False)
        svc.shelly.turn_on.assert_called_once_with("192.168.2.250", "shelly_gen2", 0)
        self.assertTrue(db.get_cascade_device("pumpe")["is_on"])

    def test_manual_override_respects_dry_run(self):
        svc, out = self.run_with(True, override="off")
        svc.shelly.turn_off.assert_not_called()
        self.assertIn("DRY-RUN: Kaskade würde Shelly Pumpe ausschalten", out)


if __name__ == "__main__":
    unittest.main()
