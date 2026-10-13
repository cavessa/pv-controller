"""Rollläden (Phase 1): Config, Normalisierung, Status, Aussperrschutz, Staffelung, Duty Cycle, Rückprüfung, Web."""
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import heating
import shutters
import web
from clients.openhab_client import OpenHabError

TZ = shutters.TZ
ROOMS = [
    {"id": "wohnen", "name": "Wohnen", "item": "W_Roll", "device": "homematic", "group": "eg"},
    {"id": "terrasse", "name": "Terrasse", "item": "T_Roll", "contact_item": "T_Door", "is_door": True,
     "group": "eg", "device": "shelly", "automation": "pvcontroller"},
    {"id": "hinten", "name": "Hintereingang", "item": "H_Roll", "is_door": True, "device": "homematic"},
    {"id": "bad", "name": "Bad", "item": "B_Roll", "contact_item": "B_Win", "group": "og", "can_position": False},
]
ITEMS = {
    "W_Roll": {"name": "W_Roll", "type": "Rollershutter", "state": "0"},
    "T_Roll": {"name": "T_Roll", "type": "Rollershutter", "state": "65"},
    "T_Door": {"name": "T_Door", "type": "Contact", "state": "CLOSED"},
    "H_Roll": {"name": "H_Roll", "type": "Rollershutter", "state": "100"},
    "B_Roll": {"name": "B_Roll", "type": "Rollershutter", "state": "NULL"},
    "B_Win": {"name": "B_Win", "type": "Contact", "state": "OPEN"},
    "CCU_DC": {"name": "CCU_DC", "type": "Number", "state": "12"},
}


def raw_cfg(**extra):
    return {"openhab": {"url": "http://oh:8080", "token": "secret"},
            "shutters": {"enabled": True, "command_spacing_s": 2, "rooms": ROOMS, **extra}}


class FakeClient:
    def __init__(self, items=ITEMS, bulk_fail=False, dead=()):
        self.items, self.bulk_fail, self.dead = {k: dict(v) for k, v in items.items()}, bulk_fail, set(dead)
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


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_rooms_groups_defaults(self):
        c = shutters.parse_shutters(raw_cfg())
        self.assertEqual([s.id for s in c.rooms], ["wohnen", "terrasse", "hinten", "bad"])
        self.assertEqual(c.groups(), ["eg", "og"])
        self.assertEqual([s.id for s in c.members("eg")], ["wohnen", "terrasse"])
        self.assertEqual(len(c.members("all")), 4)
        h = c.shutter("hinten")
        self.assertEqual((h.automation, h.verify_after_s), ("openhab", 360))
        self.assertEqual(c.shutter("terrasse").verify_after_s, 60)
        self.assertFalse(c.shutter("bad").can_position)
        self.assertEqual(c.command_spacing_s, 2.0)

    def test_missing_block_is_disabled(self):
        c = shutters.parse_shutters({})
        self.assertFalse(c.enabled)
        self.assertEqual(c.rooms, [])

    def test_invalid(self):
        ok = {"id": "a", "name": "A", "item": "X"}
        for bad in (
            {"rooms": [{**ok, "id": "Bad Id"}]},
            {"rooms": [{**ok, "item": ""}]},
            {"rooms": [ok, ok]},
            {"rooms": [{**ok, "automation": "homekit"}]},
            {"rooms": [{**ok, "device": "knx"}]},
            {"rooms": [{**ok, "is_door": "ja"}]},
            {"rooms": [{**ok, "group": "all"}]},
            {"command_spacing_s": 0},
            {"duty_cycle_max": 150},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                shutters.parse_shutters({"shutters": {"enabled": True, **bad}})


class NormalizeTest(unittest.TestCase):
    def test_position(self):
        self.assertEqual(shutters.norm_position({"state": "65"}), 65)
        self.assertEqual(shutters.norm_position({"state": "99.6"}), 100)
        self.assertIsNone(shutters.norm_position({"state": "NULL"}))
        self.assertIsNone(shutters.norm_position({"state": "UP"}))
        self.assertIsNone(shutters.norm_position(None))

    def test_contact(self):
        self.assertEqual(shutters.norm_contact({"state": "OPEN"}), "open")
        self.assertEqual(shutters.norm_contact({"state": "CLOSED"}), "closed")
        self.assertEqual(shutters.norm_contact({"state": "ON"}), "open")
        self.assertIsNone(shutters.norm_contact({"state": "NULL"}))

    def test_duty_cycle(self):
        self.assertEqual(shutters.norm_duty_cycle({"type": "Number", "state": "12"}), 12.0)
        self.assertEqual(shutters.norm_duty_cycle({"type": "Number:Dimensionless", "state": "0.85"}), 85.0)
        self.assertIsNone(shutters.norm_duty_cycle({"type": "Number", "state": "UNDEF"}))

    def test_door_guard(self):
        c = shutters.parse_shutters(raw_cfg())
        self.assertIsNone(shutters.door_guard(c.shutter("wohnen"), None))         # kein Tür
        self.assertIsNone(shutters.door_guard(c.shutter("bad"), "open"))          # Fenster offen: kein Sperrgrund
        self.assertIsNone(shutters.door_guard(c.shutter("terrasse"), "closed"))
        self.assertIn("offen", shutters.door_guard(c.shutter("terrasse"), "open"))
        self.assertIn("unbekannt", shutters.door_guard(c.shutter("terrasse"), None))
        self.assertIn("ohne Kontakt", shutters.door_guard(c.shutter("hinten"), None))

    def test_closing_and_targets(self):
        self.assertTrue(shutters.is_closing("down", 100, 0))
        self.assertTrue(shutters.is_closing("position", 50, 20))
        self.assertTrue(shutters.is_closing("position", 50, None))
        self.assertFalse(shutters.is_closing("position", 20, 50))
        self.assertFalse(shutters.is_closing("up", 0, 100))
        self.assertFalse(shutters.is_closing("stop", None, 50))
        self.assertEqual(shutters.to_openhab("position", 40), "40")
        self.assertEqual(shutters.to_openhab("down", 100), "DOWN")

    def test_validate_command(self):
        c = shutters.parse_shutters(raw_cfg())
        self.assertEqual(shutters.validate_command(c.shutter("wohnen"), "position", 42.4), 42)
        self.assertEqual(shutters.validate_command(c.shutter("wohnen"), "down"), 100)
        self.assertIsNone(shutters.validate_command(c.shutter("wohnen"), "stop"))
        for sid, cmd, pos in (("wohnen", "position", 101), ("wohnen", "position", True), ("wohnen", "jump", None),
                              ("bad", "position", 50)):
            with self.subTest(cmd=cmd, pos=pos), self.assertRaises(ValueError):
                shutters.validate_command(c.shutter(sid), cmd, pos)


class StatusTest(DbCase):
    def setUp(self):
        super().setUp()
        self.c = shutters.parse_shutters(raw_cfg(duty_cycle_item="CCU_DC"))

    def test_bulk(self):
        st = shutters.read_status(self.c, FakeClient())
        self.assertTrue(st["ok"])
        by = {r["id"]: r for r in st["rooms"]}
        self.assertEqual(by["terrasse"]["position"], 65)
        self.assertEqual(by["terrasse"]["contact"], "closed")
        self.assertIsNone(by["terrasse"]["door_guard"])
        self.assertIn("ohne Kontakt", by["hinten"]["door_guard"])
        self.assertEqual(by["bad"]["contact"], "open")
        self.assertIsNone(by["bad"]["position"])
        self.assertEqual(st["duty_cycle"], {"value": 12.0, "max": 80.0, "high": False})

    def test_dead_shutter_does_not_block_others(self):
        st = shutters.read_status(self.c, FakeClient(bulk_fail=True, dead={"T_Roll", "T_Door"}))
        self.assertTrue(st["ok"])
        by = {r["id"]: r for r in st["rooms"]}
        self.assertEqual(by["wohnen"]["position"], 0)
        self.assertIsNone(by["wohnen"]["error"])
        self.assertIn("nicht erreichbar", by["terrasse"]["error"])
        self.assertIn("unbekannt", by["terrasse"]["door_guard"])     # Kontakt nicht lesbar → Bestätigung nötig

    def test_openhab_down(self):
        st = shutters.read_status(self.c, FakeClient(items={}, bulk_fail=True))
        self.assertFalse(st["ok"])
        self.assertEqual(st["error"], "openHAB nicht erreichbar")

    def test_missing_item_reported(self):
        items = {k: v for k, v in ITEMS.items() if k != "B_Win"}
        st = shutters.read_status(self.c, FakeClient(items=items))
        self.assertIn("B_Win", st["rooms"][3]["error"])

    def test_duty_cycle_high(self):
        items = {**ITEMS, "CCU_DC": {"name": "CCU_DC", "type": "Number", "state": "85"}}
        self.assertTrue(shutters.read_status(self.c, FakeClient(items=items))["duty_cycle"]["high"])

    def test_no_client(self):
        self.assertFalse(shutters.read_status(self.c, None)["ok"])


class VerifyTest(DbCase):
    """HmIP: Rückprüfung nach 6 min, Shelly nach 60 s; autoupdate-/Zwischenwerte davor zählen nicht."""

    def setUp(self):
        super().setUp()
        self.c = shutters.parse_shutters(raw_cfg())
        self.t0 = datetime(2026, 10, 13, 18, 40, tzinfo=TZ)

    def _status(self, client, minutes):
        st = shutters.read_status(self.c, client, now=self.t0 + timedelta(minutes=minutes))
        return {r["id"]: r for r in st["rooms"]}

    def test_homematic_waits_6_min_then_ok(self):
        shutters.record_command(self.c.shutter("wohnen"), "down", 100, "manuell", self.t0)
        fake = FakeClient()                                  # Aktor meldet noch 0 (alte Position)
        r = self._status(fake, 1)["wohnen"]
        self.assertEqual(r["pending"]["target"], 100)
        self.assertIsNone(r["alert"])
        r = self._status(fake, 5.9)["wohnen"]
        self.assertIsNotNone(r["pending"])
        fake.items["W_Roll"]["state"] = "99"                 # HmIP: 99 % gilt als zu
        r = self._status(fake, 6)["wohnen"]
        self.assertIsNone(r["pending"])
        self.assertIsNone(r["alert"])

    def test_mismatch_then_recovers(self):
        shutters.record_command(self.c.shutter("terrasse"), "position", 30, "manuell", self.t0)
        fake = FakeClient()                                  # meldet 65
        r = self._status(fake, 1)["terrasse"]                # Shelly: nach 60 s fällig
        self.assertIn("65 % statt 30 %", r["alert"])
        self.assertIsNone(r["pending"])
        fake.items["T_Roll"]["state"] = "30"
        self.assertIsNone(self._status(fake, 3)["terrasse"]["alert"])

    def test_openhab_down_keeps_pending_then_gives_up(self):
        shutters.record_command(self.c.shutter("wohnen"), "up", 0, "manuell", self.t0)
        down = FakeClient(items={}, bulk_fail=True)
        self.assertIsNotNone(self._status(down, 10)["wohnen"]["pending"])
        r = self._status(down, 31)["wohnen"]
        self.assertIn("Rückmeldung fehlt", r["alert"])

    def test_stop_is_not_verified(self):
        shutters.record_command(self.c.shutter("wohnen"), "stop", None, "manuell", self.t0)
        r = self._status(FakeClient(), 10)["wohnen"]
        self.assertIsNone(r["pending"])
        self.assertIsNone(r["alert"])

    def test_new_command_replaces_old_alert(self):
        shutters.record_command(self.c.shutter("terrasse"), "position", 30, "manuell", self.t0)
        self.assertIsNotNone(self._status(FakeClient(), 2)["terrasse"]["alert"])
        shutters.record_command(self.c.shutter("terrasse"), "up", 0, "manuell", self.t0 + timedelta(minutes=3))
        r = self._status(FakeClient(), 3.5)["terrasse"]
        self.assertIsNone(r["alert"])
        self.assertEqual(r["pending"]["command"], "up")


SHELLY_STOP = {"state": "stop", "source": "http", "current_pos": 47, "positioning": True, "is_valid": True,
               "calibrating": False}


class PositionSourceTest(DbCase):
    """Ist-Position: Shelly direkt / Lese-Item statt Steuer-Item (autoupdate)."""

    def setUp(self):
        super().setUp()
        rooms = [dict(ROOMS[0], position_source="item:W_Read"),
                 dict(ROOMS[1], position_source="shelly", shelly_url="http://192.168.2.51/")]
        self.c = shutters.parse_shutters(raw_cfg(rooms=rooms))
        self.items = {**ITEMS, "W_Read": {"name": "W_Read", "type": "Rollershutter", "state": "40"}}
        self.now = datetime(2026, 10, 13, 14, 0, tzinfo=TZ)

    def test_parse(self):
        w, t = self.c.shutter("wohnen"), self.c.shutter("terrasse")
        self.assertEqual((w.position_item, w.history_item), ("W_Read", "W_Read"))
        self.assertIn("W_Read", w.items())
        self.assertEqual((t.position_source, t.shelly_url, t.history_item), ("shelly", "http://192.168.2.51", "T_Roll"))
        for bad in ({"position_source": "shelly"}, {"position_source": "shelly", "shelly_url": "192.168.2.51"},
                    {"position_source": "item:"}, {"position_source": "mqtt"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                shutters.parse_shutters(raw_cfg(rooms=[dict(ROOMS[0], **bad)]))

    def test_shelly_mapping(self):
        self.assertEqual(shutters._from_shelly(SHELLY_STOP), (53, None))            # 47 % offen = 53 % zu
        self.assertEqual(shutters._from_shelly({**SHELLY_STOP, "state": "close", "current_pos": 80}), (20, "down"))
        self.assertEqual(shutters._from_shelly({**SHELLY_STOP, "state": "open"})[1], "up")
        self.assertEqual(shutters._from_shelly({**SHELLY_STOP, "positioning": False}), (None, None))
        self.assertEqual(shutters._from_shelly({**SHELLY_STOP, "calibrating": True})[0], None)

    def test_shelly_beats_stale_item_after_stop(self):
        """13.10.2026: ▼ Ab + ■ Stop → openHAB 100 (autoupdate), Shelly 47 % offen."""
        sh = self.c.shutter("terrasse")
        shutters.record_command(sh, "stop", None, "manuell", self.now - timedelta(minutes=2))
        items = {**self.items, "T_Roll": {"name": "T_Roll", "state": "100"}}
        p = shutters.resolve_position(sh, items, shutters.last_commands()["terrasse"], self.now, SHELLY_STOP)
        self.assertEqual((p.value, p.reliable, p.origin, p.note), (53, True, "shelly", None))

    def test_shelly_unreachable_falls_back_unreliable(self):
        sh = self.c.shutter("terrasse")
        p = shutters.resolve_position(sh, self.items, None, self.now, shutters.ShellyError("timeout"))
        self.assertEqual((p.value, p.reliable, p.origin), (65, False, "item"))
        self.assertIn("Shelly nicht erreichbar", p.note)

    def test_read_item(self):
        sh = self.c.shutter("wohnen")
        p = shutters.resolve_position(sh, self.items, None, self.now)
        self.assertEqual((p.value, p.reliable, p.origin), (40, True, "item:W_Read"))
        shutters.record_command(sh, "down", 100, "manuell", self.now - timedelta(minutes=1))
        p = shutters.resolve_position(sh, self.items, shutters.last_commands()["wohnen"], self.now)
        self.assertEqual((p.value, p.reliable), (40, False))        # HmIP-Kanal 3 hinkt bis zur Rückprüfung
        items = {k: v for k, v in self.items.items() if k != "W_Read"}
        p = shutters.resolve_position(sh, items, None, self.now)
        self.assertEqual((p.value, p.reliable), (0, False))
        self.assertIn("W_Read ohne Wert", p.note)

    def test_read_shelly_http(self):
        with mock.patch.object(shutters.requests, "get") as get:
            get.return_value.json.return_value = SHELLY_STOP
            get.return_value.raise_for_status.return_value = None
            self.assertEqual(shutters.read_shelly("http://192.168.2.51/")["current_pos"], 47)
        self.assertEqual(get.call_args[0][0], "http://192.168.2.51/roller/0")
        import requests
        with mock.patch.object(shutters.requests, "get", side_effect=requests.ConnectTimeout("x")):
            with self.assertRaises(shutters.ShellyError):
                shutters.read_shelly("http://192.168.2.51")

    def test_status_uses_shelly_and_moving(self):
        with mock.patch.object(shutters, "read_shelly", return_value={**SHELLY_STOP, "state": "close"}):
            st = shutters.read_status(self.c, FakeClient(items=self.items), now=self.now)
        t = next(r for r in st["rooms"] if r["id"] == "terrasse")
        self.assertEqual((t["position"], t["moving"], t["position_origin"]), (53, "down", "shelly"))
        w = next(r for r in st["rooms"] if r["id"] == "wohnen")
        self.assertEqual((w["position"], w["position_origin"]), (40, "item:W_Read"))

    def test_verify_waits_while_moving_then_uses_shelly(self):
        sh = self.c.shutter("terrasse")
        shutters.record_command(sh, "down", 100, "manuell", self.now - timedelta(minutes=2))   # fällig (60 s)
        with mock.patch.object(shutters, "read_shelly", return_value={**SHELLY_STOP, "state": "close", "current_pos": 30}):
            st = shutters.read_status(self.c, FakeClient(items=self.items), now=self.now)
        self.assertIsNone(next(r for r in st["rooms"] if r["id"] == "terrasse")["alert"])     # fährt noch
        with mock.patch.object(shutters, "read_shelly", return_value={**SHELLY_STOP, "current_pos": 0}):
            st = shutters.read_status(self.c, FakeClient(items=self.items), now=self.now)
        t = next(r for r in st["rooms"] if r["id"] == "terrasse")
        self.assertIsNone(t["alert"])
        self.assertIsNone(t["pending"])                                                        # 100 % = Ziel


class GroupTest(unittest.TestCase):
    def setUp(self):
        self.c = shutters.parse_shutters(raw_cfg())

    def _run(self, members, command, contacts, results=None):
        calls, sleeps = [], []

        def send(sh):
            calls.append(sh.id)
            return (results or {}).get(sh.id, {"shutter_id": sh.id, "ok": True, "dry_run": False})
        out = shutters.send_group(self.c, members, command, send, contacts, sleep=sleeps.append)
        return out, calls, sleeps

    def test_staggered(self):
        out, calls, sleeps = self._run(self.c.members("all"), "up", {})
        self.assertEqual(calls, ["wohnen", "terrasse", "hinten", "bad"])
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])             # Abstand nur zwischen zwei Sendungen

    def test_down_skips_guarded_doors(self):
        out, calls, sleeps = self._run(self.c.members("all"), "down", {"terrasse": "open"})
        self.assertEqual(calls, ["wohnen", "bad"])
        skipped = [r for r in out if r.get("skipped")]
        self.assertEqual([r["shutter_id"] for r in skipped], ["terrasse", "hinten"])
        self.assertEqual(sleeps, [2.0])

    def test_down_sends_closed_door(self):
        _out, calls, _ = self._run(self.c.members("eg"), "down", {"terrasse": "closed"})
        self.assertEqual(calls, ["wohnen", "terrasse"])

    def test_up_never_skips_doors(self):
        _out, calls, _ = self._run(self.c.members("all"), "up", {"terrasse": "open"})
        self.assertIn("terrasse", calls)
        self.assertIn("hinten", calls)

    def test_dry_run_no_sleep(self):
        res = {s.id: {"shutter_id": s.id, "ok": True, "dry_run": True} for s in self.c.rooms}
        _out, _calls, sleeps = self._run(self.c.members("all"), "up", {}, res)
        self.assertEqual(sleeps, [])

    def test_one_group_at_a_time(self):
        gate, done = threading.Event(), threading.Event()

        def slow(sh):
            gate.wait(2)
            return {"shutter_id": sh.id, "ok": True, "dry_run": False}
        t = threading.Thread(target=lambda: (shutters.send_group(self.c, self.c.members("og"), "up", slow, {},
                                                                 sleep=lambda s: None), done.set()))
        t.start()
        try:
            with self.assertRaises(RuntimeError):
                shutters.send_group(self.c, self.c.members("og"), "up", slow, {}, sleep=lambda s: None)
        finally:
            gate.set()
            t.join(3)
        self.assertTrue(done.is_set())


class WebTest(DbCase):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        example = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        example["runtime"]["log_file"] = str(self.dir / "app.log")
        example["runtime"]["dry_run"] = False
        example.update(raw_cfg(duty_cycle_item="CCU_DC"))
        self.cfg_path.write_text(json.dumps(example), encoding="utf-8")
        self.fake = FakeClient()
        self.patches = [mock.patch.object(web, "CONFIG_PATH", self.cfg_path),
                        mock.patch.object(heating, "make_client", lambda raw: self.fake),
                        mock.patch.object(shutters._time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir)
        super().tearDown()

    def _set(self, path, value):
        c = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        node = c
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        self.cfg_path.write_text(json.dumps(c), encoding="utf-8")

    def _log(self):
        return (self.dir / "app.log").read_text(encoding="utf-8")

    def cmd(self, **kw):
        return web.shutters_command(web._ShutterCommand(**kw))

    def test_status(self):
        st = web.shutters_status(web.Response())
        self.assertTrue(st["ok"])
        self.assertEqual(st["groups"], ["eg", "og"])
        self.assertEqual(len(st["rooms"]), 4)

    def test_command_sends_logs_and_records(self):
        res = self.cmd(shutter_id="wohnen", command="down")
        self.assertFalse(res["dry_run"])
        self.assertEqual(self.fake.sent, [("W_Roll", "DOWN")])
        self.assertIn("pv-controller.shutters: Wohnen ▼ Ab (manuell)", self._log())
        r = next(x for x in web.shutters_status(web.Response())["rooms"] if x["id"] == "wohnen")
        self.assertEqual(r["pending"]["target"], 100)
        self.cmd(shutter_id="terrasse", command="position", position=40)
        self.assertEqual(self.fake.sent[-1], ("T_Roll", "40"))

    def test_dry_run_never_sends(self):
        self._set(["runtime", "dry_run"], True)
        self.cmd(shutter_id="wohnen", command="down")
        self.cmd(shutter_id="hinten", command="down", confirm=True)
        web.shutters_group(web._ShutterGroupCommand(group="all", command="up"))
        self.assertEqual(self.fake.sent, [])
        self.assertIn("DRY-RUN: Wohnen ▼ Ab", self._log())
        self.assertEqual(shutters.last_commands(), {})

    def test_open_door_requires_confirmation(self):
        self.fake.items["T_Door"]["state"] = "OPEN"
        for kw in ({"command": "down"}, {"command": "position", "position": 90}):
            with self.subTest(kw=kw), self.assertRaises(web.HTTPException) as cm:
                self.cmd(shutter_id="terrasse", **kw)
            self.assertEqual(cm.exception.status_code, 409)
            self.assertIn("Tür ist offen", cm.exception.detail)
        self.assertEqual(self.fake.sent, [])
        self.cmd(shutter_id="terrasse", command="position", position=20)     # öffnen: nie gesperrt
        self.cmd(shutter_id="terrasse", command="up")
        self.cmd(shutter_id="terrasse", command="stop")
        self.cmd(shutter_id="terrasse", command="down", confirm=True)
        self.assertEqual([v for _n, v in self.fake.sent], ["20", "UP", "STOP", "DOWN"])

    def test_door_without_contact_requires_confirmation(self):
        with self.assertRaises(web.HTTPException) as cm:
            self.cmd(shutter_id="hinten", command="down")
        self.assertEqual(cm.exception.status_code, 409)
        self.cmd(shutter_id="hinten", command="down", confirm=True)
        self.assertEqual(self.fake.sent, [("H_Roll", "DOWN")])

    def test_door_check_uses_reliable_position(self):
        """Position offen laut Shelly, Item sagt veraltet 100 → „Position 90“ ist Schließen → Bestätigung."""
        c = json.loads(self.cfg_path.read_text())
        for r in c["shutters"]["rooms"]:
            if r["id"] == "terrasse":
                r.update(position_source="shelly", shelly_url="http://shelly")
        self.cfg_path.write_text(json.dumps(c))
        self.fake.items["T_Roll"]["state"] = "100"
        self.fake.items["T_Door"]["state"] = "OPEN"
        with mock.patch.object(shutters, "read_shelly", return_value={**SHELLY_STOP, "current_pos": 100}):
            with self.assertRaises(web.HTTPException) as cm:
                self.cmd(shutter_id="terrasse", command="position", position=90)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.fake.sent, [])

    def test_window_open_does_not_block(self):
        self.cmd(shutter_id="bad", command="down")            # Fenster (kein is_door): offen ist egal
        self.assertEqual(self.fake.sent, [("B_Roll", "DOWN")])

    def test_command_rejected(self):
        for kw, code in (({"shutter_id": "nope", "command": "up"}, 404),
                         ({"shutter_id": "wohnen", "command": "jump"}, 400),
                         ({"shutter_id": "bad", "command": "position", "position": 50}, 400),
                         ({"shutter_id": "wohnen", "command": "position", "position": 120}, 400)):
            with self.subTest(kw=kw), self.assertRaises(web.HTTPException) as cm:
                self.cmd(**kw)
            self.assertEqual(cm.exception.status_code, code)
        self.assertEqual(self.fake.sent, [])

    def test_command_openhab_down(self):
        self.fake.dead = {"W_Roll"}
        with self.assertRaises(web.HTTPException) as cm:
            self.cmd(shutter_id="wohnen", command="up")
        self.assertEqual(cm.exception.status_code, 502)
        self.assertIn("FEHLER", self._log())

    def test_group_down_skips_doors_and_staggers(self):
        sleeps = []
        with mock.patch.object(shutters._time, "sleep", sleeps.append):
            res = web.shutters_group(web._ShutterGroupCommand(group="all", command="down"))
        self.assertEqual(self.fake.sent, [("W_Roll", "DOWN"), ("T_Roll", "DOWN"), ("B_Roll", "DOWN")])
        self.assertEqual(sleeps, [2.0, 2.0])
        self.assertEqual([r["shutter_id"] for r in res["results"] if r.get("skipped")], ["hinten"])
        self.assertIn("übersprungen – Hintereingang: Tür ohne Kontakt", self._log())

    def test_group_paused_on_high_duty_cycle(self):
        self.fake.items["CCU_DC"]["state"] = "85"
        with self.assertRaises(web.HTTPException) as cm:
            web.shutters_group(web._ShutterGroupCommand(group="all", command="up"))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("Duty Cycle", cm.exception.detail)
        self.assertEqual(self.fake.sent, [])
        self.cmd(shutter_id="wohnen", command="up")           # Einzelbefehl bleibt möglich
        self.assertEqual(self.fake.sent, [("W_Roll", "UP")])

    def test_group_rejected(self):
        for body, code in (({"group": "keller", "command": "up"}, 404), ({"group": "all", "command": "stop"}, 400)):
            with self.subTest(body=body), self.assertRaises(web.HTTPException) as cm:
                web.shutters_group(web._ShutterGroupCommand(**body))
            self.assertEqual(cm.exception.status_code, code)

    def test_config_put_validates_keeps_unknown(self):
        self._set(["shutters", "frost_temp_c"], 0)            # späteres Feld bleibt erhalten
        body = web._ShuttersConfigUpdate(enabled=True, command_spacing_s=3, rooms=[
            {"id": "w", "name": "W", "item": "W_Roll", "contact_item": "", "is_door": False, "group": "eg",
             "automation": "openhab", "device": "homematic", "_open": True}])
        with mock.patch.object(web, "_write_config", side_effect=lambda c: self.cfg_path.write_text(json.dumps(c))):
            web.shutters_config_put(body)
        saved = json.loads(self.cfg_path.read_text())
        self.assertEqual(saved["shutters"]["rooms"], [{"id": "w", "name": "W", "item": "W_Roll", "is_door": False,
                                                       "group": "eg", "automation": "openhab", "device": "homematic"}])
        self.assertEqual(saved["shutters"]["frost_temp_c"], 0)
        self.assertEqual(saved["openhab"]["token"], "secret")
        bad = web._ShuttersConfigUpdate(enabled=True, rooms=[{"id": "w", "name": "W"}])
        with self.assertRaises(web.HTTPException):
            web.shutters_config_put(bad)


class ProdWithoutShuttersTest(unittest.TestCase):
    """Prod-config.json ohne shutters-Block: Deploy-Prüfung läuft, Tab gilt als aus."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = self.dir / "config.json"
        raw = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        raw.pop("shutters", None)
        raw["runtime"]["log_file"] = str(self.dir / "app.log")
        self.cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        self.p = mock.patch.object(web, "CONFIG_PATH", self.cfg_path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir)

    def test_disabled(self):
        from config import load_config
        load_config(self.cfg_path)
        st = web.shutters_status(web.Response())
        self.assertFalse(st["enabled"])
        self.assertEqual(st["rooms"], [])
        with self.assertRaises(web.HTTPException) as cm:
            web.shutters_command(web._ShutterCommand(shutter_id="wohnen", command="up"))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertFalse(web.shutters_config_get()["shutters"]["enabled"])


if __name__ == "__main__":
    unittest.main()
