"""Pergola-WR (solax2) hat ein eigenes HTTP-Timeout (Default 3 s), der Haupt-WR bleibt beim Runtime-Timeout."""
import json
import tempfile
import unittest
from pathlib import Path

from cascade_service import CascadeService
from config import load_config
from controller import Controller

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def cfg_with(solax2_extra=None, timeout=5):
    with open(EXAMPLE) as f:
        raw = json.load(f)
    raw["runtime"]["request_timeout_seconds"] = timeout
    raw["solax2"] = {"enabled": True, "url": "http://192.168.2.141", "pwd": "x", "name": "Pergola", **(solax2_extra or {})}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(raw, tmp); tmp.close()
    try:
        return load_config(tmp.name)
    finally:
        Path(tmp.name).unlink()


class SolaxTimeoutTest(unittest.TestCase):
    def test_default_is_three_seconds(self):
        c = cfg_with()
        self.assertEqual(c.solax2_timeout, 3.0)

    def test_own_value_from_config(self):
        self.assertEqual(cfg_with({"timeout_s": 2.5}).solax2_timeout, 2.5)

    def test_clients_use_it_and_main_inverter_keeps_runtime_timeout(self):
        c = cfg_with(timeout=5)
        ctl = Controller(c)
        self.assertEqual(ctl.solax2.timeout, 3.0)
        self.assertEqual(ctl.solax.timeout, 5)
        svc = CascadeService(c)
        self.assertEqual(svc.solax2.timeout, 3.0)
        self.assertEqual(svc.solax.timeout, 5)

    def test_without_pergola_no_error(self):
        c = cfg_with()
        c.solax2 = None
        self.assertEqual(c.solax2_timeout, 3.0)


if __name__ == "__main__":
    unittest.main()
