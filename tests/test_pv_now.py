"""Gesamt-PV für die Kopfzeile (/api/pv/now): nur Cache bzw. Schnappschuss, nie eine Geräteabfrage."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import db
import decisions
import web
from clients import solax_client
from tests.test_solax_timeout import cfg_with

MAIN, PERGOLA = "http://192.168.2.195", "http://192.168.2.141"


def raw_x3(pv1, pv2):
    raw = [0] * 100
    raw[14], raw[15] = pv1, pv2
    return raw


def raw_x1(pv1, pv2):
    raw = [0] * 100
    raw[7], raw[8] = pv1, pv2
    return raw


class PvNowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        cfg = cfg_with()
        cfg.solax.url = MAIN
        self.patches = [mock.patch.object(web, "_load_cfg", lambda: cfg),
                        mock.patch.object(solax_client.requests, "post",
                                          side_effect=AssertionError("keine Geräteabfrage erlaubt"))]
        for p in self.patches:
            p.start()
        self._cache = dict(solax_client._cache)
        solax_client._cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        solax_client._cache.clear()
        solax_client._cache.update(self._cache)
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def now(self):
        return web.api_pv_now(web.Response())

    def cache(self, url, raw, dev_type, age=5.0):
        solax_client._cache[url] = (raw, time.monotonic() - age, dev_type)

    def snap(self, age_s=30, **energy):
        ts = datetime.now().astimezone() - timedelta(seconds=age_s)
        decisions.save({"ts": ts.isoformat(timespec="seconds"), "energy": energy, "devices": [], "health": []})

    def test_from_the_web_cache_main_plus_pergola(self):
        self.cache(MAIN, raw_x3(5000, 2374), 14)
        self.cache(PERGOLA, raw_x1(400, 207), 4)
        d = self.now()
        self.assertEqual((d["pv_w"], d["main_w"], d["pergola_w"], d["source"]), (7981.0, 7374.0, 607.0, "cache"))

    def test_pergola_offline_counts_only_the_main_inverter(self):
        self.cache(MAIN, raw_x3(300, 0), 14)
        d = self.now()
        self.assertEqual((d["pv_w"], d["pergola_w"]), (300.0, None))

    def test_stale_cache_falls_back_to_the_snapshot(self):
        self.cache(MAIN, raw_x3(5000, 0), 14, age=web.PV_NOW_CACHE_S + 5)
        self.snap(pv_w=3000, pv_pergola_w=500)
        d = self.now()
        self.assertEqual((d["pv_w"], d["main_w"], d["pergola_w"], d["source"]), (3000, 2500, 500, "snapshot"))

    def test_main_inverter_missing_or_snapshot_too_old_is_null(self):
        self.snap(pv_pergola_w=500)                             # Fail-safe-Lauf: Haupt-WR nicht lesbar
        self.assertIsNone(self.now()["pv_w"])
        self.snap(age_s=-1, pv_w=4000)
        self.assertEqual(self.now()["pv_w"], 4000)
        db.DB_PATH = Path(self.tmp.name) / "leer.db"
        self.snap(age_s=web.PV_NOW_SNAPSHOT_S + 60, pv_w=4000)
        self.assertEqual(self.now(), {"pv_w": None, "main_w": None, "pergola_w": None, "source": None, "age_s": None})

    def test_cached_read_never_fetches(self):
        c = solax_client.SolaxClient(MAIN, "x")
        self.assertIsNone(c.cached_realtime_data(60))
        self.assertIsNone(c.cache_age())
        self.cache(MAIN, raw_x3(1, 2), 14)
        self.assertEqual(c.cached_realtime_data(60).pv_total_w, 3.0)


class HeaderMarkupTest(unittest.TestCase):
    def test_pill_in_the_header_and_sum_in_the_flow(self):
        root = Path(web.PROJECT_ROOT) / "web"
        html, js, css = ((root / f).read_text(encoding="utf-8") for f in ("index.html", "app.js", "styles.css"))
        top = html[html.index('<header class="topbar">'):html.index("</header>")]
        self.assertLess(top.index('id="pill-pv"'), top.index('id="pill-time"'))   # links neben „aktualisiert“
        self.assertIn('api("/api/pv/now")', js)
        self.assertIn("if (document.hidden || _statusWanted()) return;", js)        # pausiert, kein Doppel-Polling
        self.assertIn("PV_PILL_MS = 30000", js)
        self.assertIn('"Σ "', js)
        self.assertIn("#pill-time .pt-label { display: none; }", css)               # Handy: nur Uhrzeit
