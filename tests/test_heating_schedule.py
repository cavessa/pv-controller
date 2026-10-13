"""Heizung Phase 2: Zeitpläne im Minuten-Cron (Fälligkeit, Nachholen, Zeitumstellung, Rückprüfung,
openHAB tot/hängt, Isolation von der PV-Steuerung) und die Web-Endpunkte."""
import json
import logging
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db
import heating
import heating_schedule as hs
import main as main_mod
import web
from clients.openhab_client import OpenHabError
from tests import test_heating as th

TZ = hs.TZ
UTC = timezone.utc
TUE = date(2026, 10, 13)           # Dienstag


def at(d: date, hhmm: str, sec: int = 30) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, sec, tzinfo=TZ)


def raw(schedules=True, **extra):
    r = th.raw_cfg(schedules_enabled=schedules, **extra)
    r["runtime"] = {"dry_run": False}
    return r


class SchedClient:
    """openHAB-Attrappe: zählt Requests, Sollwerte änderbar, optional tot."""

    def __init__(self, setpoints=None, dead=False):
        self.sp = {"W_Set": 17.5, "K_Set": 19.0, **(setpoints or {})}
        self.dead = dead
        self.calls = []          # ("get", names) / ("send", item, value)
        self.timeout = 3.0
        self.echo = True         # Gerät übernimmt gesendeten Wert

    def get_items(self, names):
        self.calls.append(("get", sorted(names)))
        if self.dead:
            raise OpenHabError("GET /items: timeout")
        return {n: {"name": n, "type": "Number:Temperature", "state": f"{self.sp[n]} °C"}
                for n in names if n in self.sp}

    def send_command(self, name, value):
        self.calls.append(("send", name, value))
        if self.dead:
            raise OpenHabError(f"command {name}={value}: timeout")
        if self.echo:
            self.sp[name] = float(value)

    @property
    def sends(self):
        return [c[1:] for c in self.calls if c[0] == "send"]


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        hs.init_tables()
        self.cfg = heating.parse_heating(raw())
        self.logs = []

    def tearDown(self):
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def add(self, room="wohnen", sp=21.0, time="07:00", days=None, date_=None, created=None):
        fields = {"room_id": room, "setpoint": sp, "time": time,
                  "days": ",".join(days) if days else None, "date": date_}
        return hs.create_schedule(fields, now=created or datetime(2026, 1, 1, tzinfo=TZ))

    def run_at(self, now, client, dry_run=False, r=None, **kw):
        return hs.run(r or raw(), dry_run=dry_run, now=now, client=client,
                      app_log=lambda m, level=logging.INFO: self.logs.append((level, m)), **kw)


class DueTest(DbCase):
    def test_due_once_per_day_and_weekday(self):
        self.add(days=["mo", "di", "mi", "do", "fr"])
        c = SchedClient()
        self.run_at(at(TUE, "06:59"), c)
        self.assertEqual(c.calls, [])                                   # noch nicht fällig
        res = self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertEqual(res.sent, 1)
        c.sp["W_Set"] = 17.0                                            # jemand dreht zurück
        self.run_at(at(TUE, "07:01"), c)                                # Rückprüfung: …
        c.calls.clear()
        self.run_at(at(TUE, "07:05"), c)
        self.assertEqual(c.sends, [])                                   # … aber heute kein zweites Mal
        c.sp["W_Set"] = 17.5
        c.calls.clear()
        self.run_at(at(TUE + timedelta(days=1), "07:00"), c)            # Mittwoch wieder
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_weekday_mismatch(self):
        self.add(days=["sa", "so"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(c.calls, [])

    def test_once_runs_then_disabled(self):
        e = self.add(date_=TUE.isoformat())
        tomorrow = self.add(time="07:00", date_=(TUE + timedelta(days=1)).isoformat(), sp=22.0)
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertFalse(hs.get_schedule(e["id"])["enabled"])
        self.assertTrue(hs.get_schedule(tomorrow["id"])["enabled"])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SENT)

    def test_unchanged_setpoint_is_not_sent(self):
        self.add(sp=17.5, days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(c.sends, [])
        self.assertEqual(hs.list_schedules()[0]["last_result"], hs.UNCHANGED)
        self.assertTrue(any("bereits eingestellt" in m for _, m in self.logs))

    def test_created_after_time_is_not_caught_up(self):
        self.add(days=["di"], created=at(TUE, "07:05"))
        c = SchedClient()
        self.run_at(at(TUE, "07:06"), c)
        self.assertEqual(c.calls, [])
        self.assertEqual(self.logs, [])                                 # auch kein „übersprungen“


class CatchUpTest(DbCase):
    def test_catch_up_within_15_min(self):
        self.add(days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:15", sec=0), c)                         # genau 15 min verspätet
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_skip_when_later_than_15_min(self):
        e = self.add(days=["di"])
        once = self.add(room="kind", sp=22.0, time="06:00", date_=TUE.isoformat())
        c = SchedClient()
        res = self.run_at(at(TUE, "07:16"), c)
        self.assertEqual(c.calls, [])                                   # nur DB, kein openHAB
        self.assertEqual(res.skipped, 2)
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SKIPPED)
        self.assertFalse(hs.get_schedule(once["id"])["enabled"])
        self.assertTrue(any("übersprungen" in m and "max. 15" in m for _, m in self.logs))
        self.logs.clear()
        self.run_at(at(TUE, "07:17"), c)                                # nur einmal geloggt
        self.assertEqual(self.logs, [])


class SeveralPerRoomTest(DbCase):
    def test_only_latest_per_room(self):
        a = self.add(sp=18.0, time="06:00", days=["di"])
        b = self.add(sp=21.0, time="06:05", days=["di"])
        k = self.add(room="kind", sp=20.0, time="06:02", days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "06:10"), c)
        self.assertEqual(sorted(c.sends), [("K_Set", "20"), ("W_Set", "21")])
        self.assertEqual(hs.get_schedule(a["id"])["last_result"], hs.SUPERSEDED)
        self.assertEqual(hs.get_schedule(b["id"])["last_result"], hs.SENT)
        self.assertEqual(hs.get_schedule(k["id"])["last_result"], hs.SENT)
        self.assertEqual(len([x for x in c.calls if x[0] == "get"]), 1)   # ein Sammel-Request


class DstTest(DbCase):
    """Zeitumstellung 2026: Sommerzeit endet So 25.10. (03:00 CEST → 02:00 CET), Mo 26.10. normal."""

    END = date(2026, 10, 25)
    START = date(2026, 3, 29)

    def test_due_instants(self):
        self.assertEqual(hs.due_instant(self.END, "07:00").astimezone(UTC).hour, 6)       # CET
        self.assertEqual(hs.due_instant(self.END - timedelta(days=1), "07:00").astimezone(UTC).hour, 5)  # CEST
        self.assertEqual(hs.due_instant(self.END, "02:30").astimezone(UTC),               # erstes 02:30
                         datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        gap = hs.due_instant(self.START, "02:30")                                         # gibt es nicht
        self.assertEqual(gap.astimezone(UTC), datetime(2026, 3, 29, 1, 0, tzinfo=UTC))   # = 03:00 CEST

    def test_doubled_hour_runs_once(self):
        self.add(time="02:30", days=list(hs.DAYS))
        c = SchedClient()
        first = datetime(2026, 10, 25, 0, 30, 20, tzinfo=UTC)                  # 02:30 CEST
        second = datetime(2026, 10, 25, 1, 30, 20, tzinfo=UTC)                 # 02:30 CET
        self.run_at(first, c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        c.sp["W_Set"] = 17.5
        c.calls.clear()
        for minute in range(0, 61):                                             # Minuten-Cron durch die doppelte Stunde
            self.run_at(first + timedelta(minutes=minute + 2), c)
        self.run_at(second, c)
        self.assertEqual(c.sends, [("W_Set", "21")])   # nur der eine Retry der Rückprüfung, kein 2. Zeitplanlauf
        self.assertFalse(any("übersprungen" in m for _, m in self.logs))

    def test_cron_minutes_across_fall_back(self):
        """Wiederkehrend 07:00 täglich: Sa (CEST), So (CET), Mo je genau einmal, zur richtigen Uhrzeit."""
        self.add(time="07:00", days=list(hs.DAYS))
        c = SchedClient()
        c.echo = False
        sent_at = []
        t = datetime(2026, 10, 24, 0, 0, 10, tzinfo=UTC)
        while t < datetime(2026, 10, 27, 0, 0, tzinfo=UTC):
            n = len(c.sends)
            self.run_at(t, c, dry_run=False)
            if len(c.sends) > n and c.sends[-1] == ("W_Set", "21"):
                sent_at.append(t.astimezone(TZ).strftime("%d. %H:%M"))
            hs.clear_verify("wohnen")                     # Rückprüfung hier nicht Thema
            c.sp["W_Set"] = 17.5
            t += timedelta(minutes=1)
        self.assertEqual(sent_at, ["24. 07:00", "25. 07:00", "26. 07:00"])

    def test_spring_forward_gap_runs_at_three(self):
        self.add(time="02:30", days=list(hs.DAYS))
        c = SchedClient()
        self.run_at(datetime(2026, 3, 29, 0, 59, 30, tzinfo=UTC), c)            # 01:59 CET
        self.assertEqual(c.sends, [])
        self.run_at(datetime(2026, 3, 29, 1, 0, 20, tzinfo=UTC), c)             # 03:00 CEST
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_late_across_fall_back_is_skipped_not_sent_an_hour_later(self):
        """02:50 CEST fällig, openHAB bis 02:09 CET (= 19 min später) tot → übersprungen.

        Vorher: Python vergleicht/zieht aware datetimes mit derselben tzinfo als Wanduhrzeit ab (fold wird
        ignoriert) → um 02:09 CET galt 02:50 als „noch nicht fällig“, um 02:50 CET wurde gesendet (1 h zu spät).
        """
        e = self.add(time="02:50", days=list(hs.DAYS))
        c = SchedClient(dead=True)
        t = datetime(2026, 10, 25, 0, 40, 20, tzinfo=UTC)              # 02:40 CEST
        while t < datetime(2026, 10, 25, 2, 0, tzinfo=UTC):             # bis 03:00 CET
            c.dead = t < datetime(2026, 10, 25, 1, 9, tzinfo=UTC)       # wieder da ab 02:09 CET
            self.run_at(t, c)
            t += timedelta(minutes=1)
        self.assertEqual(c.sends, [])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SKIPPED)
        self.assertTrue(any("15 min verspätet" in m for _, m in self.logs))   # 15:20 min (02:05:20 CET)

    def test_late_within_window_across_fall_back_is_caught_up(self):
        self.add(time="02:55", days=list(hs.DAYS))
        c = SchedClient(dead=True)
        self.run_at(datetime(2026, 10, 25, 0, 55, 20, tzinfo=UTC), c)  # 02:55 CEST, tot
        c.dead = False
        self.run_at(datetime(2026, 10, 25, 1, 5, 20, tzinfo=UTC), c)   # 02:05 CET = 10 min später
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_verify_steps_in_real_minutes_across_fall_back(self):
        """Rückprüfung 5 min nach dem Senden – auch wenn dazwischen die Uhr zurückgestellt wird."""
        self.add(time="02:58", days=list(hs.DAYS))
        c = SchedClient()
        c.echo = False
        self.run_at(datetime(2026, 10, 25, 0, 58, 20, tzinfo=UTC), c)  # 02:58 CEST gesendet
        c.calls.clear()
        self.run_at(datetime(2026, 10, 25, 1, 3, 30, tzinfo=UTC), c)   # 02:03 CET = 5 min später
        self.assertEqual([x[0] for x in c.calls], ["get", "send"])      # geprüft + einmal erneut gesendet

    def test_stale_verify_given_up_after_30_real_minutes(self):
        hs._verify_start("wohnen", 21.0, datetime(2026, 10, 25, 2, 40, tzinfo=TZ))          # CEST
        c = SchedClient(dead=True)
        self.run_at(datetime(2026, 10, 25, 1, 15, 20, tzinfo=UTC), c)  # 02:15 CET = 35 min später
        self.assertEqual(hs.alerts()["wohnen"]["status"], "mismatch")

    def test_next_change_across_dst(self):
        e = self.add(time="07:00", days=["so"])
        nxt = hs.next_changes([hs.get_schedule(e["id"])], at(date(2026, 10, 24), "12:00"))
        self.assertEqual(nxt["wohnen"]["at"], "2026-10-25T07:00+01:00")


class OpenHabDownTest(DbCase):
    def test_dead_openhab_keeps_entry_due_then_catches_up(self):
        e = self.add(days=["di"])
        c = SchedClient(dead=True)
        res = self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(res.sent, 0)
        self.assertIsNone(hs.get_schedule(e["id"])["last_run"])         # bleibt fällig
        self.assertTrue(any("nicht erreichbar" in m for _, m in self.logs))
        c.dead = False
        self.run_at(at(TUE, "07:10"), c)                                # innerhalb 15 min nachgeholt
        self.assertEqual(c.sends, [("W_Set", "21")])

    def test_dead_openhab_longer_than_15_min_skips(self):
        e = self.add(days=["di"])
        c = SchedClient(dead=True)
        for m in range(0, 20):
            self.run_at(at(TUE, f"07:{m:02d}"), c)
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SKIPPED)
        c.dead = False
        c.calls.clear()
        self.run_at(at(TUE, "07:30"), c)
        self.assertEqual(c.calls, [])

    def test_send_error_retried_once_then_given_up(self):
        e = self.add(days=["di"])
        c = SchedClient()
        c.send_command = mock.Mock(side_effect=OpenHabError("400 Bad Request"))
        self.run_at(at(TUE, "07:00"), c)
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.RETRY)
        self.assertIsNone(hs.get_schedule(e["id"])["last_run"])         # nächste Minute fällig
        self.run_at(at(TUE, "07:01"), c)
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.FAILED)
        self.run_at(at(TUE, "07:02"), c)
        self.assertEqual(c.send_command.call_count, 2)                  # 1 Versuch + 1 Retry, keine Schleife
        self.assertTrue(any("aufgegeben" in m for _, m in self.logs))

    def test_send_timeout_but_applied_is_not_sent_twice(self):
        """Lese-Timeout beim Senden, openHAB hat den Befehl aber übernommen."""
        e = self.add(days=["di"])
        c = SchedClient()

        def applied_then_timeout(name, value):
            c.calls.append(("send", name, value))
            c.sp[name] = float(value)
            raise OpenHabError("read timeout")
        c.send_command = applied_then_timeout
        self.run_at(at(TUE, "07:00"), c)
        self.run_at(at(TUE, "07:01"), c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.UNCHANGED)


class AbortTest(DbCase):
    """Lauf bricht ab (5-s-Limit, Prozessende): nächste Minute erneut fällig, nie doppelt gesendet."""

    def test_cut_off_after_send_before_db_write(self):
        e = self.add(days=["di"])
        c = SchedClient()
        with mock.patch.object(hs, "_mark_sent", lambda *a, **kw: None):        # Thread endet nach dem POST
            self.run_at(at(TUE, "07:00"), c)
        self.assertIsNone(hs.get_schedule(e["id"])["last_run"])
        self.run_at(at(TUE, "07:01"), c)                                # fällig, liest, sendet nicht noch mal
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.UNCHANGED)

    def test_cut_off_before_send_runs_next_minute(self):
        e = self.add(days=["di"])
        now = [0.0]
        c = SchedClient()
        orig = c.get_items

        def slow(names):
            now[0] += 4.8
            return orig(names)
        c.get_items = slow
        self.run_at(at(TUE, "07:00"), c, clock=lambda: now[0])
        self.assertEqual(c.sends, [])
        c.get_items = orig
        self.run_at(at(TUE, "07:01"), c)
        self.assertEqual(c.sends, [("W_Set", "21")])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.SENT)

    def test_mark_and_verify_are_one_transaction(self):
        e = self.add(days=["di"])
        with mock.patch.object(hs, "_verify_start", side_effect=sqlite3.OperationalError("abgebrochen")):
            with self.assertRaises(sqlite3.OperationalError):
                hs._mark_sent(e["id"], "wohnen", 21.0, at(TUE, "07:00"))
        self.assertIsNone(hs.get_schedule(e["id"])["last_run"])         # nichts halb geschrieben


class NoRequestWithoutDueTest(DbCase):
    def test_nothing_due_means_no_openhab_request(self):
        self.add(days=["di"], time="22:00")
        with mock.patch.object(heating, "make_client") as mk:
            res = hs.run(raw(), dry_run=False, now=at(TUE, "07:00"))
        mk.assert_not_called()
        self.assertTrue(res.active)
        self.assertEqual(res.requests, 0)

    def test_switch_off_does_nothing(self):
        e = self.add(days=["di"])
        c = SchedClient()
        res = self.run_at(at(TUE, "07:00"), c, r=raw(schedules=False))
        self.assertFalse(res.active)
        self.assertEqual(c.calls, [])
        self.assertIsNone(hs.get_schedule(e["id"])["last_run"])

    def test_default_is_off(self):
        r = th.raw_cfg()
        self.assertFalse(heating.parse_heating(r).schedules_enabled)
        r["heating"]["schedules_enabled"] = "ja"
        with self.assertRaises(ValueError):
            heating.parse_heating(r)


class DryRunTest(DbCase):
    def test_dry_run_only_logs(self):
        e = self.add(days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c, dry_run=True)
        self.assertEqual(c.sends, [])
        self.assertEqual(hs.get_schedule(e["id"])["last_result"], hs.DRY_RUN)
        self.assertTrue(any(m.startswith("DRY-RUN: Wohnen Soll → 21 °C") for _, m in self.logs))
        c.calls.clear()
        self.run_at(at(TUE, "07:01"), c, dry_run=True)
        self.assertEqual(c.calls, [])                                   # keine Rückprüfung offen


class VerifyTest(DbCase):
    """Rückprüfung 5/10/15 min nach dem Senden (openHAB-autoupdate setzt das Item sofort auf den
    gesendeten Wert – erst die spätere Meldung des Geräts zählt)."""

    def calls_at(self, c, hhmm):
        c.calls.clear()
        self.run_at(at(TUE, hhmm), c)
        return list(c.calls)

    def test_no_request_between_checks_and_done_after_15_min(self):
        self.add(days=["di"])
        c = SchedClient()                                               # autoupdate: Item = gesendeter Wert
        self.run_at(at(TUE, "07:00"), c)
        requests = {f"07:{m:02d}": len(self.calls_at(c, f"07:{m:02d}")) for m in range(1, 16)}
        self.assertEqual([k for k, n in requests.items() if n], ["07:05", "07:10", "07:15"])   # nur dann
        self.assertEqual(hs._pending_verifies(), [])                    # nach 15 min stabil: fertig
        self.assertEqual(self.calls_at(c, "07:20"), [])
        self.assertEqual(hs.alerts(), {})

    def test_jump_back_warns_and_resends_once(self):
        self.add(days=["di"])
        c = SchedClient()                                               # vorher 17,5
        self.run_at(at(TUE, "07:00"), c)
        self.run_at(at(TUE, "07:05"), c)                                # passt (autoupdate)
        c.sp["W_Set"] = 17.5                                            # Gerät meldet den alten Wert
        self.run_at(at(TUE, "07:10"), c)
        self.assertEqual(c.sends, [("W_Set", "21")] * 2)
        warn = [m for lvl, m in self.logs if lvl == logging.WARNING]
        self.assertIn("Wohnen: 21 °C gesendet, Gerät meldet wieder 17,5 °C (zurückgesprungen) – sende einmal erneut",
                      warn)
        self.assertIn("erneut gesendet", hs.alerts()["wohnen"]["message"])
        # nach dem Retry neues 15-min-Fenster ab 07:10; springt es erneut zurück: nur Meldung
        c.sp["W_Set"] = 17.5
        self.run_at(at(TUE, "07:15"), c)
        self.assertEqual(len(c.sends), 2)
        self.assertEqual(hs.alerts()["wohnen"]["message"], "Wohnen: 21 °C gesendet, Gerät meldet wieder 17,5 °C")
        c.calls.clear()
        for m in range(16, 60):
            self.run_at(at(TUE, f"07:{m:02d}"), c)
        self.assertEqual(c.calls, [])                                   # keine Schleife

    def test_retry_holds_then_cleared(self):
        self.add(days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        c.sp["W_Set"] = 17.5
        self.run_at(at(TUE, "07:05"), c)                                # zurückgesprungen → Retry (echo: 21)
        for m in ("07:10", "07:15", "07:20"):
            self.run_at(at(TUE, m), c)
        self.assertEqual(hs.alerts(), {})                               # Meldung weg, sobald es passt
        self.assertEqual(hs._pending_verifies(), [])
        self.assertEqual(len(c.sends), 2)

    def test_other_value_warns_without_resend(self):
        """z. B. Fenster-offen (4,5 °C) oder am Gerät verstellt: melden, nicht dagegen anschalten."""
        self.add(days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        c.sp["W_Set"] = 4.5
        self.run_at(at(TUE, "07:05"), c)
        self.assertEqual(len(c.sends), 1)
        self.assertEqual(hs.alerts()["wohnen"]["message"],
                         "Wohnen: 21 °C gesendet, Gerät meldet 4,5 °C (am Gerät, Fenster-offen oder Wochenprofil geändert?)")
        c.calls.clear()
        self.run_at(at(TUE, "07:10"), c)
        self.assertEqual(c.calls, [])

    def test_null_item_gives_up_after_30_min(self):
        self.add(days=["di"])
        c = SchedClient()
        self.run_at(at(TUE, "07:00"), c)
        del c.sp["W_Set"]                                               # Item liefert keinen Wert
        for m in range(5, 32):
            self.run_at(at(TUE, f"07:{m:02d}"), c)
        self.assertIn("nicht prüfbar", hs.alerts()["wohnen"]["message"])
        c.calls.clear()
        self.run_at(at(TUE, "07:40"), c)
        self.assertEqual(c.calls, [])

    def test_old_verify_table_is_migrated(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("DROP TABLE heating_verify")
            conn.execute("CREATE TABLE heating_verify (room_id TEXT PRIMARY KEY, setpoint REAL NOT NULL, "
                         "sent_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, "
                         "status TEXT NOT NULL DEFAULT 'pending', reported REAL, message TEXT)")
            conn.execute("INSERT INTO heating_verify (room_id, setpoint, sent_at) VALUES ('wohnen', 21, ?)",
                         (at(TUE, "06:58").isoformat(),))
        hs.init_tables()
        c = SchedClient({"W_Set": 21.0})
        self.run_at(at(TUE, "07:03"), c)                                # alte Zeile: checks 0 → fällig ab 07:03
        self.assertEqual(hs._pending_verifies()[0]["checks"], 1)


class BudgetTest(DbCase):
    def test_requests_respect_time_budget(self):
        self.add(days=["di"])
        self.add(room="kind", sp=22.0, days=["di"])
        now = [0.0]
        c = SchedClient()
        orig = c.get_items

        def slow_get(names):
            now[0] += 4.8                                               # Lesen frisst fast das ganze Budget
            return orig(names)
        c.get_items = slow_get
        res = self.run_at(at(TUE, "07:00"), c, clock=lambda: now[0])
        self.assertTrue(res.timed_out)
        self.assertEqual(c.sends, [])                                   # kein Request mehr gestartet
        self.assertEqual([e["last_run"] for e in hs.list_schedules()], [None, None])   # bleiben fällig

    def test_request_timeout_shrinks_to_remaining_budget(self):
        self.add(days=["di"])
        now = [0.0]
        seen = []
        c = SchedClient()
        orig = c.get_items

        def get(names):
            seen.append(c.timeout)
            now[0] += 3.5
            return orig(names)
        orig_send = c.send_command

        def send(n, v):
            seen.append(c.timeout)
            orig_send(n, v)
        c.get_items, c.send_command = get, send
        self.run_at(at(TUE, "07:00"), c, clock=lambda: now[0])
        self.assertEqual(seen[0], 3.0)
        self.assertAlmostEqual(seen[1], 1.5)


class ValidateAndPruneTest(DbCase):
    def test_validate(self):
        now = at(TUE, "10:00")
        ok = hs.validate_entry(self.cfg, "wohnen", 21, "06:30", days=["fr", "mo", "mo"], now=now)
        self.assertEqual(ok["days"], "mo,fr")
        ok = hs.validate_entry(self.cfg, "wohnen", 21, "11:00", date="2026-10-13", now=now)
        self.assertEqual(ok["date"], "2026-10-13")
        for kw, msg in (
            (dict(room_id="nix", setpoint=21, time="06:00", days=["mo"]), "unbekannt"),
            (dict(room_id="wasch", setpoint=21, time="06:00", days=["mo"]), "nur Anzeige"),
            (dict(room_id="wohnen", setpoint=35, time="06:00", days=["mo"]), "außerhalb"),
            (dict(room_id="wohnen", setpoint=21.3, time="06:00", days=["mo"]), "Schritten"),
            (dict(room_id="wohnen", setpoint=21, time="24:00", days=["mo"]), "HH:MM"),
            (dict(room_id="wohnen", setpoint=21, time="06:00", days=["xx"]), "Wochentage"),
            (dict(room_id="wohnen", setpoint=21, time="06:00"), "angeben"),
            (dict(room_id="wohnen", setpoint=21, time="06:00", days=["mo"], date="2026-10-14"), "nicht beides"),
            (dict(room_id="wohnen", setpoint=21, time="09:00", date="2026-10-13"), "Vergangenheit"),
            (dict(room_id="wohnen", setpoint=21, time="09:00", date="2026-10-12"), "Vergangenheit"),
        ):
            with self.assertRaises(ValueError) as cm:
                hs.validate_entry(self.cfg, now=now, **kw)
            self.assertIn(msg, str(cm.exception), kw)

    def test_prune_once_after_7_days(self):
        db.init_cascade_settings_table()
        old = self.add(date_="2026-10-05")
        keep = self.add(date_="2026-10-07")
        rec = self.add(days=["mo"])
        out = db.prune(now=datetime(2026, 10, 13, 3, 0))
        self.assertEqual(out["heating_schedule (einmalig)"], 1)
        self.assertEqual([e["id"] for e in hs.list_schedules()], sorted([keep["id"], rec["id"]]))
        self.assertIsNone(hs.get_schedule(old["id"]))


class MainIsolationTest(unittest.TestCase):
    """Zeitpläne laufen nach der PV-Steuerung, isoliert und zeitbegrenzt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._old_db = db.DB_PATH
        db.DB_PATH = d / "t.db"
        db.init_db()
        db.init_cascade_tables()
        db.init_cascade_settings_table()
        hs.init_tables()
        cfg = json.loads((Path(web.PROJECT_ROOT) / "config.example.json").read_text(encoding="utf-8"))
        cfg["runtime"]["log_file"] = str(d / "app.log")
        cfg["runtime"]["dry_run"] = False
        cfg.update({k: v for k, v in raw().items() if k != "runtime"})
        self.cfg_path = d / "config.json"
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        now = datetime.now(TZ)
        hs.create_schedule({"room_id": "wohnen", "setpoint": 21.0, "time": now.strftime("%H:%M"),
                            "days": ",".join(hs.DAYS), "date": None}, now=now - timedelta(hours=1))
        self.events = []
        self.release = threading.Event()
        self.hanging = None                            # Thread, der im hängenden openHAB-Request steckt
        self.clock = 1000.0                            # simulierte Uhr (s) statt time.monotonic in main.py
        self.threads = []
        self._root = logging.getLogger().handlers[:]

        test = self

        class FakeController:
            def __init__(self, config):
                pass

            def run(self):
                test.events.append(("pv_start", test.clock))
                test.clock += 0.05                     # „normale“ Laufzeit der PV-Steuerung (simuliert)
                test.events.append(("pv_end", test.clock))
                return SimpleNamespace(wallbox_status=None,
                                       readings=SimpleNamespace(storage_temp_c=50.0, heater_meter_power_w=0.0))

        class HangingClient(SchedClient):
            def get_items(self, names):
                test.events.append(("openhab_start", test.clock))
                test.hanging = threading.current_thread()
                test.release.wait(60)                  # openHAB hängt, bis der Test endet
                test.events.append(("openhab_end", test.clock))
                return super().get_items(names)

        class SimThread(threading.Thread):
            """join(timeout) ohne Echtzeit-Grenze: wartet, bis der Thread fertig ist **oder** im hängenden
            openHAB-Request steckt, und stellt dann die simulierte Uhr um ``timeout`` vor. Unter Last dauert
            das nur länger, das Ergebnis bleibt gleich."""

            def start(self):
                test.threads.append(self)
                super().start()

            def join(self, timeout=None):
                while self.is_alive() and test.hanging is not self:
                    super().join(0.01)
                if self.is_alive() and timeout is not None:
                    test.clock += timeout              # Zeitlimit „abgelaufen“

        self.patches = [
            mock.patch.object(main_mod, "Controller", FakeController),
            mock.patch.object(main_mod, "CascadeService"),
            mock.patch("pv_logger.maybe_log"),
            mock.patch.object(db, "append_temp_history"),
            mock.patch.object(heating, "make_client", lambda r: HangingClient()),
            mock.patch.object(main_mod, "time", SimpleNamespace(monotonic=lambda: test.clock)),
            mock.patch.object(main_mod.threading, "Thread", SimThread),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        self.release.set()
        for p in self.patches:
            p.stop()
        for th_ in self.threads:                       # hängende Attrappe sauber beenden (echtes join)
            threading.Thread.join(th_, 5)
        root = logging.getLogger()
        for h in root.handlers:
            h.close()
        root.handlers[:] = self._root
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def test_openhab_hangs_pv_control_unaffected(self):
        """openHAB hängt unbegrenzt: PV-Steuerung läuft vorher komplett und in normaler Zeit, der Cron-Lauf
        endet nach dem Zeitlimit (simulierte Uhr), nicht erst wenn openHAB antwortet."""
        t0 = self.clock
        rc = main_mod.main(["--config", str(self.cfg_path)])
        took = self.clock - t0
        ev = dict(self.events)
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in self.events], ["pv_start", "pv_end", "openhab_start"])  # PV zuerst, komplett
        self.assertAlmostEqual(ev["pv_end"] - ev["pv_start"], 0.05)            # PV in normaler Zeit
        self.assertTrue(self.hanging.is_alive())                               # openHAB hängt noch – main ist zurück
        self.assertAlmostEqual(took, 0.05 + main_mod.HEATING_BUDGET_S + 0.5)   # Zeitlimit, nicht die Hängedauer
        log_text = (Path(self.tmp.name) / "app.log").read_text(encoding="utf-8")
        self.assertIn(f"Heizungs-Zeitpläne: nach {main_mod.HEATING_BUDGET_S + 0.5:.1f} s abgebrochen", log_text)

    def test_heating_exception_does_not_change_rc(self):
        with mock.patch.object(hs, "run", side_effect=RuntimeError("kaputt")):
            rc = main_mod.main(["--config", str(self.cfg_path)])
        self.assertEqual(rc, 0)
        self.assertEqual([e[0] for e in self.events], ["pv_start", "pv_end"])
        self.assertIn("Heizungs-Zeitpläne: Fehler", (Path(self.tmp.name) / "app.log").read_text(encoding="utf-8"))


class WebScheduleTest(unittest.TestCase):
    setUp_web = th.WebTest.setUp
    tearDown_web = th.WebTest.tearDown

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        hs.init_tables()
        self.setUp_web()

    def tearDown(self):
        self.tearDown_web()
        db.DB_PATH = self._old_db
        self.tmp.cleanup()

    def test_create_list_toggle_delete(self):
        e = web.heating_schedule_create(web._HeatingScheduleCreate(room_id="wohnen", time="06:30", setpoint=21,
                                                                   days=["mo", "di"]))
        self.assertEqual((e["days"], e["source"]), (["mo", "di"], "ui"))
        lst = web.heating_schedules(web.Response())
        self.assertFalse(lst["schedules_enabled"])
        self.assertEqual(len(lst["items"]), 1)
        self.assertIn("wohnen", lst["next"])
        self.assertFalse(web.heating_schedule_patch(e["id"], web._HeatingSchedulePatch(enabled=False))["enabled"])
        self.assertTrue(web.heating_schedule_delete(e["id"])["ok"])
        with self.assertRaises(web.HTTPException):
            web.heating_schedule_delete(e["id"])
        with self.assertRaises(web.HTTPException) as cm:
            web.heating_schedule_create(web._HeatingScheduleCreate(room_id="wohnen", time="06:30", setpoint=40,
                                                                   days=["mo"]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_status_has_next_and_alert_and_clears_when_device_agrees(self):
        web.heating_schedule_create(web._HeatingScheduleCreate(room_id="wohnen", time="23:59", setpoint=21,
                                                               days=list(hs.DAYS)))
        hs._verify_start("wohnen", 21.0, datetime.now(TZ))
        hs._verify_update("wohnen", status="mismatch", reported=17.5,
                          message="Wohnen: 21 °C gesendet, Gerät meldet 17,5 °C")
        st = web.heating_status(web.Response())
        w = next(r for r in st["rooms"] if r["id"] == "wohnen")
        self.assertEqual(w["alert"], "Wohnen: 21 °C gesendet, Gerät meldet 17,5 °C")
        self.assertEqual(w["next"]["setpoint"], 21.0)
        self.fake.items["W_Set"] = {"name": "W_Set", "type": "Number:Temperature", "state": "21 °C"}
        st = web.heating_status(web.Response())
        self.assertIsNone(next(r for r in st["rooms"] if r["id"] == "wohnen")["alert"])

    def test_manual_setpoint_clears_alert(self):
        hs._verify_start("wohnen", 21.0, datetime.now(TZ))
        hs._verify_update("wohnen", status="mismatch", reported=15.0, message="x")
        web.heating_setpoint(web._HeatingSetpoint(room_id="wohnen", setpoint=20))
        self.assertEqual(hs.alerts(), {})

    def test_config_switch_kept_unless_sent(self):
        body = dict(enabled=True, setpoint_min=5, setpoint_max=26, rooms=th.ROOMS)
        web.heating_config_put(web._HeatingConfigUpdate(**body, schedules_enabled=True))
        self.assertTrue(web.heating_config_get()["heating"]["schedules_enabled"])
        web.heating_config_put(web._HeatingConfigUpdate(**body))                  # alter Client ohne Feld
        self.assertTrue(web.heating_config_get()["heating"]["schedules_enabled"])
        web.heating_config_put(web._HeatingConfigUpdate(**body, schedules_enabled=False))
        self.assertFalse(web.heating_status(web.Response())["schedules_enabled"])
        self.assertIn("Zeitpläne deaktiviert", (self.dir / "app.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
