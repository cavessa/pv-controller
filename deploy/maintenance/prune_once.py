#!/usr/bin/env python3
"""Einmaliges Aufräumen der Produktions-Datenbank (cascade_log & Retention).

  python3 deploy/maintenance/prune_once.py            # Dry-Run (Default): zeigt, was gelöscht würde
  python3 deploy/maintenance/prune_once.py --apply    # wirklich aufräumen

--apply:
  1. Snapshot (SQLite-Backup-API) nach ~/backup/pvcontroller-snapshot/pre-prune-<Zeit>.db
  2. exklusiver flock auf /tmp/pvc-code.lock (dasselbe Lock wie Cron `flock -s` und deploy.sh):
     während des Löschens und VACUUMs läuft kein main.py
  3. alle keep_on/keep_off/budget-Zeilen aus cascade_log löschen (jedes Alter), dann db.prune()
  4. VACUUM (einmalig, unter dem Lock), integrity_check, Größe vorher/nachher

Die Datenbank ist die des Verzeichnisses, in dem das Skript liegt (Prod: ~/pvcontroller-prod);
mit --db kann eine andere Datei angegeben werden (Tests).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

LOCK_FILE = "/tmp/pvc-code.lock"
VERBOSE = tuple(sorted(db.CASCADE_LOG_VERBOSE_ONLY))


def mb(n: float) -> str:
    return f"{n / 1e6:7.2f} MB"


def table_sizes(conn: sqlite3.Connection) -> dict[str, int]:
    """Bytes je Tabelle inkl. ihrer Indizes (dbstat)."""
    owner = {r[0]: r[1] for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
    out: dict[str, int] = {}
    for name, size in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
        t = owner.get(name)
        if t and not t.startswith("sqlite_"):
            out[t] = out.get(t, 0) + size
    return out


def plan() -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
    """(Löschzahlen je Regel inkl. einmaligem keep_*-Löschen, {Tabelle: (vorher, nachher)})."""
    with sqlite3.connect(db.DB_PATH) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        ph = ",".join("?" for _ in VERBOSE)
        once = conn.execute(f"SELECT COUNT(*) FROM cascade_log WHERE action IN ({ph})", VERBOSE).fetchone()[0]
    rules = db.prune(dry_run=True)
    deletes = {"cascade_log: keep_*/budget (einmalig, alle)": once}
    # prune-Regel „keep_*/budget älter 2 Tage“ ist in „alle“ enthalten
    for name, n in rules.items():
        if name != "cascade_log (keep_*/budget)":
            deletes[name] = n
    after = dict((t, (b, b)) for t, b in before.items())
    per_table = {"cascade_log": once + rules["cascade_log (Ereignisse)"], "pv_hourly_log": rules["pv_hourly_log"],
                 "main_meter_min": rules["main_meter_min"], "zendure_min": rules["zendure_min"],
                 "load_min": rules["load_min"],
                 "heater_budget_log": rules["heater_budget_log"]}
    for t, n in per_table.items():
        if t in after:
            after[t] = (before[t], max(0, before[t] - n))
    return deletes, after


def report(deletes: dict[str, int], rows: dict[str, tuple[int, int]]) -> None:
    with sqlite3.connect(db.DB_PATH) as conn:
        sizes = table_sizes(conn)
        free = conn.execute("PRAGMA freelist_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    print(f"Datenbank: {db.DB_PATH}  ({mb(os.path.getsize(db.DB_PATH))}, frei in Datei: {mb(free)})\n")
    print("Zu löschende Zeilen:")
    for name, n in deletes.items():
        print(f"  {name:48s} {n:10d}")
    print("\nTabelle                    Zeilen vorher → nachher     Größe jetzt   erwartet danach")
    est_after = 0
    for t, (b, a) in rows.items():
        s = sizes.get(t, 0)
        e = s * (a / b) if b else s
        est_after += e
        mark = "  *" if a != b else ""
        print(f"  {t:22s} {b:10d} → {a:10d} {mb(s)}    {mb(e)}{mark}")
    print(f"\nErwartete Dateigröße nach VACUUM: ≈ {mb(est_after)} (jetzt {mb(os.path.getsize(db.DB_PATH))})")


def take_lock(wait_s: int):
    fh = open(LOCK_FILE, "a")
    deadline = time.time() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.time() > deadline:
                raise SystemExit(f"Lock {LOCK_FILE} nach {wait_s} s nicht frei – Abbruch, nichts geändert.")
            time.sleep(0.5)


def apply(backup_dir: Path, use_lock: bool) -> None:
    size_before = os.path.getsize(db.DB_PATH)
    backup_dir.mkdir(parents=True, exist_ok=True)
    need = size_before * 3
    free = shutil.disk_usage(backup_dir).free
    if free < need:
        raise SystemExit(f"Zu wenig freier Platz ({mb(free)} frei, ≥ {mb(need)} nötig) – Abbruch, nichts geändert.")
    snap = backup_dir / f"pre-prune-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    lock = take_lock(120) if use_lock else None
    try:
        print(f"Lock {LOCK_FILE} gehalten – kein main.py-Lauf parallel." if lock else "(ohne Lock)")
        src = sqlite3.connect(db.DB_PATH, timeout=60)
        dst = sqlite3.connect(snap)
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close(); src.close()
        print(f"Snapshot: {snap}  ({mb(os.path.getsize(snap))}, integrity_check: {ok})")
        if ok != "ok":
            raise SystemExit("Snapshot nicht in Ordnung – Abbruch, nichts geändert.")

        ph = ",".join("?" for _ in VERBOSE)
        total = 0
        while True:
            with sqlite3.connect(db.DB_PATH, timeout=60) as conn:
                n = conn.execute(
                    f"DELETE FROM cascade_log WHERE rowid IN (SELECT rowid FROM cascade_log WHERE action IN ({ph}) LIMIT {db.PRUNE_BATCH})",
                    VERBOSE,
                ).rowcount
            total += n
            if n < db.PRUNE_BATCH:
                break
        print(f"cascade_log: {total} keep_*/budget-Zeilen gelöscht")
        for name, n in db.prune().items():
            print(f"prune: {name}: {n}")

        t = time.time()
        conn = sqlite3.connect(db.DB_PATH, timeout=120, isolation_level=None)
        conn.execute("VACUUM")
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        print(f"VACUUM ({time.time() - t:.1f} s), integrity_check: {res}")
        size_after = os.path.getsize(db.DB_PATH)
        print(f"Größe: {mb(size_before)} → {mb(size_after)}  (−{(1 - size_after / size_before) * 100:.0f} %)")
        if res != "ok":
            raise SystemExit(f"integrity_check fehlgeschlagen – Snapshot zurückspielen: {snap}")
    finally:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="wirklich aufräumen (Default: Dry-Run)")
    ap.add_argument("--db", help="andere Datenbank-Datei (Tests)")
    ap.add_argument("--backup-dir", default=str(Path.home() / "backup" / "pvcontroller-snapshot"))
    ap.add_argument("--no-lock", action="store_true", help="ohne flock (nur Tests)")
    a = ap.parse_args()
    if a.db:
        db.DB_PATH = Path(a.db)
    if not Path(db.DB_PATH).exists():
        raise SystemExit(f"Datenbank nicht gefunden: {db.DB_PATH}")
    deletes, rows = plan()
    report(deletes, rows)
    if not a.apply:
        print("\nDry-Run – nichts geändert. Mit --apply ausführen.")
        return 0
    print("\n--apply:")
    apply(Path(a.backup_dir), use_lock=not a.no_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
