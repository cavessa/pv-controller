"""Dashboard-Ladezeit: Pergola-Ausfall bremst nicht, gzip/Cache für Assets, WAL, keine externen Requests."""
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

import db
import web
from clients import solax_client as sc

URL = "http://192.168.2.141"
ROOT = Path(web.PROJECT_ROOT)


def raw_x1(ac=300):
    raw = [0] * 100
    raw[2] = ac
    return raw


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(sc._cache), dict(sc._fail_until))
        sc._cache.clear()
        sc._fail_until.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self._state = [mock.patch.object(sc, "_OFFLINE_STATE_PATH", Path(self._tmp.name) / "o.json"),
                       mock.patch.object(sc, "_offline_state", None)]
        for p in self._state:
            p.start()

    def tearDown(self):
        for p in self._state:
            p.stop()
        self._tmp.cleanup()
        sc._cache.clear(); sc._cache.update(self._saved[0])
        sc._fail_until.clear(); sc._fail_until.update(self._saved[1])


class FailureBackoffTest(_Isolated):
    def test_offline_device_is_asked_once_then_not_for_the_backoff_period(self):
        c = sc.SolaxClient(URL, "x", 3)
        with mock.patch.object(sc.requests, "post", side_effect=requests.ConnectTimeout("t")) as post, \
                mock.patch.object(sc.time, "sleep"):
            self.assertIsNone(c.get_realtime_data())
            self.assertEqual(post.call_count, 2)             # Versuch + Retry
            for _ in range(5):
                self.assertIsNone(c.get_realtime_data())
            self.assertEqual(post.call_count, 2)             # danach kein HTTP mehr

    def test_connect_timeout_short_only_for_pergola_main_inverter_keeps_its_timeout(self):
        for client, want in ((sc.SolaxClient(URL, "x", 5), (5, 5)), (sc.SolaxClient(URL, "x", 3, 1.0), (1.0, 3))):
            sc._fail_until.clear()
            with mock.patch.object(sc.requests, "post", side_effect=requests.ConnectTimeout("t")) as post, \
                    mock.patch.object(sc.time, "sleep"):
                client.get_realtime_data()
            self.assertEqual(post.call_args.kwargs["timeout"], want)

    def test_clients_built_from_config_use_the_short_connect_timeout_for_pergola(self):
        from controller import Controller
        from cascade_service import CascadeService
        from tests.test_solax_timeout import cfg_with
        c = cfg_with(timeout=5)
        for obj in (Controller(c), CascadeService(c)):
            self.assertEqual(obj.solax2.connect_timeout, 1.0)
            self.assertEqual(obj.solax.connect_timeout, 5)

    def test_backoff_ends_and_success_clears_it(self):
        c = sc.SolaxClient(URL, "x", 3)
        sc._fail_until[URL] = time.monotonic() - 1
        ok = mock.Mock(); ok.json.return_value = {"Data": raw_x1(), "type": 4}
        with mock.patch.object(sc.requests, "post", return_value=ok) as post:
            self.assertEqual(c.get_realtime_data().pv_ac_w, 300.0)
        self.assertEqual(post.call_count, 1)
        self.assertNotIn(URL, sc._fail_until)

    def test_reset_backoff_for_the_connection_test(self):
        c = sc.SolaxClient(URL, "x", 3)
        sc._fail_until[URL] = time.monotonic() + 100
        c.reset_backoff()
        ok = mock.Mock(); ok.json.return_value = {"Data": raw_x1(), "type": 4}
        with mock.patch.object(sc.requests, "post", return_value=ok):
            self.assertIsNotNone(c.get_realtime_data())


class StaleLimitTest(_Isolated):
    def fail(self, c):
        with mock.patch.object(sc.requests, "post", side_effect=requests.ConnectTimeout("t")), \
                mock.patch.object(sc.time, "sleep"):
            return c.get_realtime_data()

    def test_recent_cache_still_serves_as_fallback(self):
        sc._cache[URL] = (raw_x1(250), time.monotonic() - 100, 4)
        self.assertEqual(self.fail(sc.SolaxClient(URL, "x", 3)).pv_ac_w, 250.0)

    def test_hours_old_cache_is_no_longer_current_data(self):
        sc._cache[URL] = (raw_x1(250), time.monotonic() - 12193, 4)      # wie im Prod-Log
        self.assertIsNone(self.fail(sc.SolaxClient(URL, "x", 3)))

    def test_web_pergola_is_none_when_offline_for_hours(self):
        sc._cache[URL] = (raw_x1(250), time.monotonic() - 12193, 4)
        cfg = mock.Mock(); cfg.solax2_active = True; cfg.solax2.url = URL; cfg.solax2.pwd = "x"; cfg.solax2_timeout = 3; cfg.solax2_connect_timeout = 1.0
        with mock.patch.object(sc.requests, "post", side_effect=requests.ConnectTimeout("t")), \
                mock.patch.object(sc.time, "sleep"):
            self.assertIsNone(web._read_pergola(cfg))


class WalTest(unittest.TestCase):
    def test_init_db_switches_to_wal_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = db.DB_PATH
            db.DB_PATH = Path(tmp) / "t.db"
            try:
                sqlite3.connect(db.DB_PATH).execute("CREATE TABLE x(a)").connection.close()
                self.assertEqual(sqlite3.connect(db.DB_PATH).execute("PRAGMA journal_mode").fetchone()[0], "delete")
                db.init_db()
                self.assertEqual(sqlite3.connect(db.DB_PATH).execute("PRAGMA journal_mode").fetchone()[0], "wal")
                db.init_db()                                  # idempotent
                self.assertEqual(sqlite3.connect(db.DB_PATH).execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                db.DB_PATH = old

    def test_backup_api_snapshot_is_consistent_while_wal_has_unmerged_pages(self):
        """Wie backup.sh: Snapshot per Backup-API enthält auch, was nur im -wal steht."""
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "s.db", Path(tmp) / "snap.db"
            w = sqlite3.connect(src)
            w.execute("PRAGMA journal_mode=WAL"); w.execute("PRAGMA wal_autocheckpoint=0")
            w.execute("CREATE TABLE t(a)"); w.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(500)]); w.commit()
            self.assertGreater((Path(str(src) + "-wal")).stat().st_size, 0)
            s = sqlite3.connect(src); d = sqlite3.connect(dst); s.backup(d); d.close(); s.close()
            snap = sqlite3.connect(dst)
            self.assertEqual(snap.execute("SELECT count(*) FROM t").fetchone()[0], 500)
            self.assertEqual(snap.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            w.close()


class _Resp:
    def __init__(self, status, headers, body):
        self.status_code, self.content = status, body
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}

    @property
    def text(self):
        return self.content.decode("utf-8")


class _Client:
    """Minimaler ASGI-Aufruf (httpx/TestClient ist im venv nicht installiert); Middleware läuft mit."""

    def get(self, path, headers=None):
        import asyncio
        path, _, query = path.partition("?")
        hdrs = [(b"host", b"t")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
                 "server": ("t", 80), "client": ("127.0.0.1", 1), "root_path": ""}
        out = {"status": None, "headers": [], "body": b""}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"], out["headers"] = m["status"], m["headers"]
            elif m["type"] == "http.response.body":
                out["body"] += m.get("body", b"")

        asyncio.run(web.app(scope, receive, send))
        return _Resp(out["status"], out["headers"], out["body"])


class AssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = _Client()

    def test_gzip_for_big_text_responses(self):
        r = self.c.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertGreater(len((ROOT / "web/app.js").read_bytes()), 3 * len(r.content))

    def test_versioned_assets_and_fonts_are_immutable_unversioned_revalidate(self):
        imm = "public, max-age=31536000, immutable"
        self.assertEqual(self.c.get("/static/app.js?v=abc").headers["cache-control"], imm)
        self.assertEqual(self.c.get("/static/fonts/dm-mono-400-latin.woff2").headers["cache-control"], imm)
        self.assertEqual(self.c.get("/static/app.js").headers["cache-control"], "no-cache")

    def test_index_gets_versions_from_the_file_dates_and_revalidates(self):
        r = self.c.get("/")
        self.assertEqual(r.headers["cache-control"], "no-cache")
        want = {f"/static/{n}": f"{(ROOT / 'web' / n).stat().st_mtime_ns // 1_000_000_000:x}"
                for n in ("styles.css", "app.js", "heating.js", "shutters.js")}
        for path, ver in want.items():
            self.assertIn(f'{path}?v={ver}', r.text)
        r2 = self.c.get("/", headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual(r2.status_code, 304)

    def test_index_without_manual_version_bump_changes_url_with_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            web_dir = Path(tmp)
            (web_dir / "app.js").write_text("1")
            (web_dir / "index.html").write_text('<script src="/static/app.js?v=1"></script>')
            with mock.patch.object(web, "WEB_DIR", web_dir), mock.patch.dict(web._index_cache, {"key": None}):
                a = web._render_index()[0]
                import os; os.utime(web_dir / "app.js", (2_000_000_000, 2_000_000_000))
                b = web._render_index()[0]
        self.assertNotEqual(a, b)
        self.assertIn(f"?v={2_000_000_000:x}", b)


class NoExternalRequestsTest(unittest.TestCase):
    def test_page_load_has_no_third_party_requests(self):
        files = {n: (ROOT / "web" / n).read_text(encoding="utf-8") for n in ("index.html", "styles.css")}
        loads = {"index.html": r"""(?:src|href)\s*=\s*["']https?://[^"']+""",
                 "styles.css": r"(?:url\(|@import)\s*[\"']?https?://[^)\"']+"}
        for name, text in files.items():
            self.assertEqual(re.findall(loads[name], text), [], name)
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        for dead in ("ip-api.com", "ipapi.co", "fonts.googleapis", "fonts.gstatic"):
            self.assertNotIn(dead, js + files["index.html"] + files["styles.css"])
        for f in ("dm-mono-400-latin", "dm-mono-500-latin", "dm-mono-400-latin-ext", "dm-mono-500-latin-ext"):
            self.assertTrue((ROOT / f"web/fonts/{f}.woff2").stat().st_size > 1000)
            self.assertIn(f"/static/fonts/{f}.woff2", files["styles.css"])

    def test_dashboard_renders_before_slow_solax_answers(self):
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        body = js[js.index("async function refreshStatus()"):js.index("async function runOnce()")]
        self.assertNotIn("Promise.all([statusP, solaxP])", body)
        self.assertLess(body.index("renderStatus(data)"), body.index("await solaxP"))
        alert = js[js.index("async function _checkLiveStringAlert"):js.index("// ── Wetter sub-tab")]
        self.assertNotIn('"/api/solax"', alert)


if __name__ == "__main__":
    unittest.main()
