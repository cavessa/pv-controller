"""Zeitplan-Formular (Heizung + Rollläden, gemeinsamer Baustein in web/heating.js):
UI-Zustand → Request-Body → Server-Validierung, vor allem der Wechsel Sonnenuntergang → Uhrzeit → Sonnenuntergang."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import web
from tests import test_shutters as ts

JS = Path(web.PROJECT_ROOT) / "web" / "heating.js"

# Führt den echten Formular-Code aus heating.js in node aus. Die Schritte entsprechen den Event-Handlern
# (Radio → f.kind, Eingabefeld → f[feld]); nach jedem Schritt: sichtbare Felder, Fehlertext, Request-Body.
HARNESS = r"""
const escapeHtml = s => String(s);
%(code)s
const fields = (f, attr) => [...(_whenFields(f, attr, "k", {sunrise: "07:37", sunset: "18:37"})
  .matchAll(new RegExp(`data-${attr}="(time|offset|not_before|not_after)"`, "g")))].map(m => m[1]);
const checked = (f, attr) => /value="(\w+)" checked/.exec(_whenFields(f, attr, "k", {}))[1];
const out = [];
for (const attr of ["sf", "f"]) {
  const f = attr === "sf" ? { kind: "sunset", time: "", offset: 0, not_before: "", not_after: "" }
                          : { kind: "time", time: "", offset: 0, not_before: "", not_after: "" };
  const snap = step => out.push({ attr, step, checked: checked(f, attr), fields: fields(f, attr), error: _whenError(f), body: _whenBody(f) });
  f.kind = "sunset"; f.offset = "-20"; f.not_before = "17:00"; f.not_after = "21:00"; snap("sunset_filled");
  f.kind = "time"; snap("time_empty");
  f.time = "06:45"; snap("time_filled");
  f.kind = "sunset"; snap("back_to_sunset");
  f.kind = "sunrise"; f.offset = ""; f.not_before = ""; f.not_after = ""; snap("sunrise_blank");
  f.kind = "time"; f.time = ""; snap("time_cleared");
  f.time = "23:59"; snap("time_again");
  const e = { id: 7, kind: "sunset", time: null, offset_min: 15, not_before: "17:00", not_after: null, days: ["mo"], date: null };
  const g = _whenFromEntry(e); g.kind = "time"; g.time = "07:00";
  out.push({ attr, step: "edit_sun_to_time", checked: checked(g, attr), fields: fields(g, attr), error: _whenError(g), body: _whenBody(g) });
}
console.log(JSON.stringify(out));
"""


def run_form_flow():
    src = JS.read_text(encoding="utf-8")
    hq = next(l for l in src.splitlines() if l.startswith("const _hq"))
    code = src[src.index("const SCHED_KINDS"):src.index("function _schedForm")]
    r = subprocess.run(["node", "-e", HARNESS % {"code": hq + "\n" + code}], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise AssertionError(r.stderr)
    return {(s["attr"], s["step"]): s for s in json.loads(r.stdout)}


@unittest.skipUnless(shutil.which("node"), "node fehlt")
class ScheduleFormFlowTest(ts.WebTest):
    for _n in [n for n in dir(ts.WebTest) if n.startswith("test_")]:      # nur Fixture erben, nicht die Tests
        locals()[_n] = None
    del _n

    @classmethod
    def setUpClass(cls):
        cls.flow = run_form_flow()

    def test_fields_follow_the_radio(self):
        sun, clock = ["offset", "not_before", "not_after"], ["time"]
        for attr in ("sf", "f"):
            for step, kind, want in (("sunset_filled", "sunset", sun), ("time_empty", "time", clock),
                                     ("time_filled", "time", clock), ("back_to_sunset", "sunset", sun),
                                     ("sunrise_blank", "sunrise", sun), ("time_again", "time", clock),
                                     ("edit_sun_to_time", "time", clock)):
                s = self.flow[attr, step]
                with self.subTest(form=attr, step=step):
                    self.assertEqual((s["checked"], s["fields"]), (kind, want))

    def test_body_has_no_leftovers(self):
        for attr in ("sf", "f"):
            with self.subTest(form=attr):
                # Offset und Grenzen vom Sonnenuntergang dürfen bei „Uhrzeit“ nicht mitgeschickt werden
                self.assertEqual(self.flow[attr, "time_filled"]["body"], {"kind": "time", "time": "06:45"})
                self.assertEqual(self.flow[attr, "edit_sun_to_time"]["body"], {"kind": "time", "time": "07:00"})
                # … und bleiben erhalten, wenn man zurückwechselt
                self.assertEqual(self.flow[attr, "back_to_sunset"]["body"],
                                 {"kind": "sunset", "offset_min": -20, "not_before": "17:00", "not_after": "21:00"})
                self.assertEqual(self.flow[attr, "sunrise_blank"]["body"], {"kind": "sunrise", "offset_min": 0})
                # leere Uhrzeit: Hinweis im Formular statt Request
                self.assertTrue(self.flow[attr, "time_empty"]["error"])
                self.assertTrue(self.flow[attr, "time_cleared"]["error"])
                for step in ("sunset_filled", "time_filled", "back_to_sunset", "sunrise_blank", "time_again"):
                    self.assertEqual(self.flow[attr, step]["error"], "")

    def test_server_accepts_every_body_for_shutters(self):
        """Rollladen mit „PV Controller“ (terrasse) und mit openHAB-Regel (hinten): jeder Body wird angenommen."""
        self._set(("latitude",), 50.0)
        self._set(("longitude",), 8.0)
        for shutter in ("terrasse", "hinten"):
            for step in ("sunset_filled", "time_filled", "back_to_sunset", "sunrise_blank", "time_again"):
                body = self.flow["sf", step]["body"]
                with self.subTest(shutter=shutter, step=step):
                    e = web.shutters_schedule_create(web._ShutterScheduleCreate(
                        shutter_id=shutter, action="down", days=["mo", "di"], **body))
                    self.assertEqual(e["kind"], body["kind"])
                    if body["kind"] == "time":
                        self.assertEqual((e["time"], e["offset_min"], e["not_before"], e["not_after"]),
                                         (body["time"], 0, None, None))
            # Bearbeiten: Sonnenuntergang mit Grenze → Uhrzeit (PUT) räumt Offset/Grenzen ab
            sun = web.shutters_schedule_create(web._ShutterScheduleCreate(
                shutter_id=shutter, action="up", days=["sa"], kind="sunset", offset_min=15, not_before="17:00"))
            u = web.shutters_schedule_update(sun["id"], web._ShutterScheduleCreate(
                shutter_id=shutter, action="up", days=["sa"], **self.flow["sf", "edit_sun_to_time"]["body"]))
            self.assertEqual((u["kind"], u["time"], u["offset_min"], u["not_before"]), ("time", "07:00", 0, None))
        # ohne den Hinweis im Formular würde der Server die leere Uhrzeit ablehnen
        with self.assertRaises(web.HTTPException) as cm:
            web.shutters_schedule_create(web._ShutterScheduleCreate(
                shutter_id="hinten", action="down", days=["mo"], **self.flow["sf", "time_empty"]["body"]))
        self.assertEqual((cm.exception.status_code, cm.exception.detail), (400, "Uhrzeit muss HH:MM sein"))


if __name__ == "__main__":
    unittest.main()
