"""Heizung (Phase 1): Config, Normalisierung, Status mit Fehler-Isolation, Sollwert setzen, Web-Endpunkte."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heating
import web
from clients.openhab_client import OpenHabClient, OpenHabError

ROOMS = [
    {"id": "wohnen", "name": "Wohnen", "setpoint_item": "W_Set", "temp_item": "W_Temp",
     "humidity_item": "W_Hum", "battery_item": "W_Bat", "mode_item": "W_Mode"},
    {"id": "kind", "name": "Kind", "setpoint_item": "K_Set", "temp_item": "K_Temp", "valve_item": "K_Level"},
    {"id": "wasch", "name": "Waschküche", "temp_item": "WK_Temp", "humidity_item": "WK_Hum"},
]
ITEMS = {
    "W_Set": {"name": "W_Set", "type": "Number:Temperature", "state": "17.5 °C"},
    "W_Temp": {"name": "W_Temp", "type": "Number:Temperature", "state": "18.4 °C"},
    "W_Hum": {"name": "W_Hum", "type": "Number:Dimensionless", "state": "0.5"},
    "W_Bat": {"name": "W_Bat", "type": "Switch", "state": "ON"},
    "W_Mode": {"name": "W_Mode", "type": "String", "state": "MANU-MODE"},
    "K_Set": {"name": "K_Set", "type": "Number:Temperature", "state": "19 °C"},
    "K_Temp": {"name": "K_Temp", "type": "Number:Temperature", "state": "19.3 °C"},
    "K_Level": {"name": "K_Level", "type": "Dimmer", "state": "21"},
    "WK_Temp": {"name": "WK_Temp", "type": "Number:Temperature", "state": "20.5 °C"},
    "WK_Hum": {"name": "WK_Hum", "type": "Number", "state": "59"},
}


def raw_cfg(**heating_extra):
    return {"openhab": {"url": "http://oh:8080", "token": "secret"},
            "heating": {"enabled": True, "setpoint_min": 5, "setpoint_max": 26, "rooms": ROOMS, **heating_extra}}


class FakeClient:
    """Wie OpenHabClient; bulk_fail/dead simulieren Ausfälle."""

    def __init__(self, items=ITEMS, bulk_fail=False, dead=()):
        self.items, self.bulk_fail, self.dead = dict(items), bulk_fail, set(dead)
        self.sent = []

    def get_items(self, names):
        if self.bulk_fail:
            raise OpenHabError("timeout")
        return {n: self.items[n] for n in names if n in self.items}

    def get_item(self, name):
        if name in self.dead or name not in self.items:
            raise OpenHabError(f"{name} timeout")
        return self.items[name]

    def send_command(self, name, value):
        if name in self.dead:
            raise OpenHabError(f"{name} timeout")
        self.sent.append((name, value))


class ParseTest(unittest.TestCase):
    def test_rooms_and_display_only(self):
        h = heating.parse_heating(raw_cfg())
        self.assertEqual([r.id for r in h.rooms], ["wohnen", "kind", "wasch"])
        self.assertTrue(h.room("kind").controllable)
        self.assertFalse(h.room("wasch").controllable)

    def test_missing_heating_block_is_disabled(self):
        h = heating.parse_heating({})
        self.assertFalse(h.enabled)
        self.assertEqual(h.rooms, [])

    def test_invalid(self):
        for bad in (
            {"rooms": [{"id": "Bad Raum", "name": "x", "temp_item": "t"}]},
            {"rooms": [{"id": "a", "name": "x"}]},                       # temp_item fehlt
            {"rooms": [{"id": "a", "name": "x", "temp_item": "t"}] * 2},  # doppelte id
            {"setpoint_min": 26, "setpoint_max": 20},
            {"setpoint_max": 40},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                heating.parse_heating({"heating": {"enabled": True, **bad}})


class NormalizeTest(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(heating.norm_percent({"type": "Number:Dimensionless", "state": "0.5"}), 50.0)
        self.assertEqual(heating.norm_percent({"type": "Number:Dimensionless", "state": "0.99"}), 99.0)
        self.assertEqual(heating.norm_percent({"type": "Number", "state": "59"}), 59.0)
        self.assertEqual(heating.norm_percent({"type": "Dimmer", "state": "21"}), 21.0)
        self.assertEqual(heating.norm_percent({"type": "Number:Dimensionless", "state": "34 %"}), 34.0)
        self.assertIsNone(heating.norm_percent({"type": "Number", "state": "NULL"}))

    def test_battery(self):
        self.assertEqual(heating.norm_battery({"state": "OFF"}), {"low": False, "percent": None})
        self.assertEqual(heating.norm_battery({"state": "ON"}), {"low": True, "percent": None})
        self.assertEqual(heating.norm_battery({"state": "85"}), {"low": False, "percent": 85})
        self.assertIsNone(heating.norm_battery({"state": "NULL"}))

    def test_mode(self):
        self.assertEqual(heating.norm_mode({"state": "MANU-MODE"}), "MANU")
        self.assertEqual(heating.norm_mode({"state": "AUTO-MODE"}), "AUTO")
        self.assertEqual(heating.norm_mode({"state": "0"}), "AUTO")   # HmIP SET_POINT_MODE
        self.assertEqual(heating.norm_mode({"state": "1"}), "MANU")
        self.assertIsNone(heating.norm_mode({"state": "NULL"}))
        self.assertIsNone(heating.norm_mode(None))

    def test_temp(self):
        self.assertEqual(heating.norm_temp({"state": "17.5 °C"}), 17.5)
        self.assertIsNone(heating.norm_temp({"state": "UNDEF"}))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.h = heating.parse_heating(raw_cfg())

    def test_bulk(self):
        st = heating.read_status(self.h, FakeClient())
        self.assertTrue(st["ok"])
        w, k, wk = st["rooms"]
        self.assertEqual((w["temp"], w["setpoint"], w["humidity"], w["mode"]), (18.4, 17.5, 50.0, "MANU"))
        self.assertTrue(w["battery"]["low"])
        self.assertEqual(k["valve"], 21.0)
        self.assertIsNone(k["mode"])
        self.assertEqual((wk["controllable"], wk["setpoint"], wk["humidity"]), (False, None, 59.0))

    def test_dead_room_does_not_block_others(self):
        c = FakeClient(bulk_fail=True, dead={"K_Set", "K_Temp", "K_Level"})
        st = heating.read_status(self.h, c)
        self.assertTrue(st["ok"])
        by = {r["id"]: r for r in st["rooms"]}
        self.assertEqual(by["wohnen"]["temp"], 18.4)
        self.assertIsNone(by["wohnen"]["error"])
        self.assertIsNone(by["kind"]["temp"])
        self.assertIn("nicht erreichbar", by["kind"]["error"])
        self.assertEqual(by["wasch"]["temp"], 20.5)

    def test_openhab_down(self):
        st = heating.read_status(self.h, FakeClient(items={}, bulk_fail=True))
        self.assertFalse(st["ok"])
        self.assertEqual(st["error"], "openHAB nicht erreichbar")

    def test_missing_item_reported(self):
        items = {k: v for k, v in ITEMS.items() if k != "K_Level"}
        st = heating.read_status(self.h, FakeClient(items=items))
        self.assertIn("K_Level", st["rooms"][1]["error"])

    def test_no_client(self):
        self.assertFalse(heating.read_status(self.h, None)["ok"])


class SetpointTest(unittest.TestCase):
    def setUp(self):
        self.h = heating.parse_heating(raw_cfg())

    def test_send(self):
        c = FakeClient()
        self.assertEqual(heating.set_setpoint(self.h, c, "kind", 21.5), 21.5)
        self.assertEqual(c.sent, [("K_Set", "21.5")])
        heating.set_setpoint(self.h, c, "kind", 20)
        self.assertEqual(c.sent[-1], ("K_Set", "20"))

    def test_limits_step_and_display_only(self):
        c = FakeClient()
        for v in (4.5, 26.5, 21.3, "21", True):
            with self.subTest(v=v), self.assertRaises(ValueError):
                heating.set_setpoint(self.h, c, "kind", v)
        with self.assertRaises(ValueError):
            heating.set_setpoint(self.h, c, "wasch", 20)
        with self.assertRaises(KeyError):
            heating.set_setpoint(self.h, c, "nope", 20)
        self.assertEqual(c.sent, [])


class ClientTest(unittest.TestCase):
    def test_token_url_and_command(self):
        c = OpenHabClient("http://oh:8080", timeout=3, token="oh.abc")
        self.assertEqual(c.rest_url, "http://oh:8080/rest")
        self.assertEqual(c.session.headers["Authorization"], "Bearer oh.abc")
        with mock.patch.object(c.session, "post") as post:
            post.return_value.raise_for_status.return_value = None
            c.send_command("K_Set", "21")
        args, kw = post.call_args
        self.assertEqual(args[0], "http://oh:8080/rest/items/K_Set")
        self.assertEqual(kw["data"], b"21")
        self.assertEqual(kw["headers"]["Content-Type"], "text/plain")
        self.assertEqual(kw["timeout"], 3)

    def test_rest_suffix_not_doubled(self):
        self.assertEqual(OpenHabClient("http://oh:8080/rest/").rest_url, "http://oh:8080/rest")

    def test_errors_wrapped(self):
        import requests
        c = OpenHabClient("http://oh:8080")
        with mock.patch.object(c.session, "get", side_effect=requests.ConnectTimeout("x")):
            with self.assertRaises(OpenHabError):
                c.get_items(["a"])

    def test_history_parses_numbers(self):
        c = OpenHabClient("http://oh:8080")
        with mock.patch.object(c, "_get", return_value={"data": [
                {"time": 1, "state": "17.1"}, {"time": 2, "state": "NULL"}, {"time": 3, "state": "18 °C"}]}):
            from datetime import datetime
            self.assertEqual(c.get_history("x", datetime(2026, 10, 12)), [(1, 17.1), (3, 18.0)])

    def test_downsample(self):
        pts = [(0, 1.0), (60_000, 3.0), (600_000, 5.0)]
        self.assertEqual(heating.downsample(pts, 600_000), [[0, 2.0], [600_000, 5.0]])


class WebTest(unittest.TestCase):
    """Endpunkte direkt aufrufen (ohne httpx) gegen eine temporäre config.json."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        example = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        example["runtime"]["log_file"] = str(self.dir / "app.log")
        example["runtime"]["dry_run"] = False
        example.update(raw_cfg())
        self.cfg_path.write_text(json.dumps(example), encoding="utf-8")
        self.fake = FakeClient()
        self.patches = [mock.patch.object(web, "CONFIG_PATH", self.cfg_path),
                        mock.patch.object(heating, "make_client", lambda raw: self.fake)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)

    def _set_dry_run(self, v):
        c = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        c["runtime"]["dry_run"] = v
        self.cfg_path.write_text(json.dumps(c), encoding="utf-8")

    def test_status(self):
        st = web.heating_status(web.Response())
        self.assertTrue(st["ok"])
        self.assertEqual(len(st["rooms"]), 3)
        self.assertFalse(st["dry_run"])

    def test_setpoint_sends_and_logs(self):
        res = web.heating_setpoint(web._HeatingSetpoint(room_id="kind", setpoint=21))
        self.assertTrue(res["ok"])
        self.assertEqual(self.fake.sent, [("K_Set", "21")])
        self.assertIn("Kind Soll → 21 °C (manuell)", (self.dir / "app.log").read_text(encoding="utf-8"))

    def test_setpoint_dry_run_sends_nothing(self):
        self._set_dry_run(True)
        res = web.heating_setpoint(web._HeatingSetpoint(room_id="kind", setpoint=21))
        self.assertTrue(res["dry_run"])
        self.assertEqual(self.fake.sent, [])
        self.assertIn("DRY-RUN", (self.dir / "app.log").read_text(encoding="utf-8"))

    def test_setpoint_rejected(self):
        for body, code in (({"room_id": "kind", "setpoint": 35}, 400),
                           ({"room_id": "wasch", "setpoint": 20}, 400),
                           ({"room_id": "nope", "setpoint": 20}, 404)):
            with self.subTest(body=body), self.assertRaises(web.HTTPException) as cm:
                web.heating_setpoint(web._HeatingSetpoint(**body))
            self.assertEqual(cm.exception.status_code, code)
        self.assertEqual(self.fake.sent, [])

    def test_setpoint_openhab_down(self):
        self.fake.dead = {"K_Set"}
        with self.assertRaises(web.HTTPException) as cm:
            web.heating_setpoint(web._HeatingSetpoint(room_id="kind", setpoint=21))
        self.assertEqual(cm.exception.status_code, 502)

    def test_config_get_masks_token(self):
        self.assertEqual(web.api_config_get()["openhab"]["token"], web._SECRET_MASK)
        hc = web.heating_config_get()
        self.assertTrue(hc["openhab"]["token_set"])
        self.assertNotIn("token", hc["openhab"])

    def test_config_put_validates_and_keeps_token(self):
        body = web._HeatingConfigUpdate(enabled=True, setpoint_min=5, setpoint_max=24,
                                        rooms=[{"id": "bad", "name": "Bad", "temp_item": "B_T",
                                                "setpoint_item": "B_S", "valve_item": ""}])
        with mock.patch.object(web, "_write_config", side_effect=lambda c: self.cfg_path.write_text(json.dumps(c))):
            web.heating_config_put(body)
        saved = json.loads(self.cfg_path.read_text())
        self.assertEqual(saved["heating"]["rooms"], [{"id": "bad", "name": "Bad", "temp_item": "B_T", "setpoint_item": "B_S"}])
        self.assertEqual(saved["openhab"]["token"], "secret")
        bad = web._HeatingConfigUpdate(enabled=True, setpoint_min=5, setpoint_max=24,
                                       rooms=[{"id": "bad", "name": "Bad"}])
        with self.assertRaises(web.HTTPException):
            web.heating_config_put(bad)


class ProdWithoutHeatingTest(unittest.TestCase):
    """Prod-config.json ohne openhab-/heating-Block: Deploy-Prüfung läuft, Heizung gilt als aus."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        raw.pop("openhab", None)
        raw.pop("heating", None)
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        self.cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def test_deploy_config_check_passes(self):
        # wie deploy.sh: load_config(prod config.json) mit dem neuen Code
        from config import load_config
        load_config(self.cfg_path)
        self.assertFalse(heating.parse_heating(json.loads(self.cfg_path.read_text())).enabled)
        self.assertIsNone(heating.make_client(json.loads(self.cfg_path.read_text())))

    def test_status_disabled_with_hint_flag(self):
        st = web.heating_status(web.Response())
        self.assertFalse(st["enabled"])
        self.assertFalse(st["openhab_configured"])
        self.assertEqual(st["rooms"], [])

    def test_status_enabled_but_no_openhab_does_not_crash(self):
        c = json.loads(self.cfg_path.read_text())
        c["heating"] = raw_cfg()["heating"]
        self.cfg_path.write_text(json.dumps(c))
        st = web.heating_status(web.Response())
        self.assertFalse(st["openhab_configured"])
        self.assertEqual(st["rooms"], [])

    def test_other_endpoints_refuse_cleanly(self):
        with self.assertRaises(web.HTTPException) as cm:
            web.heating_setpoint(web._HeatingSetpoint(room_id="kind", setpoint=20))
        self.assertEqual(cm.exception.status_code, 409)
        with self.assertRaises(web.HTTPException) as cm:
            web.heating_items()
        self.assertEqual(cm.exception.status_code, 409)
        cfg = web.heating_config_get()
        self.assertFalse(cfg["heating"]["enabled"])
        self.assertEqual(cfg["openhab"], {"url": "", "token_set": False})


if __name__ == "__main__":
    unittest.main()
