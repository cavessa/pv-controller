"""Ringpuffer der Hauptzähler-Leistung (3-s-Werte des Zendure-Wächters, ~2,5 min).

Der Wächter (zendure_watchdog.py) schreibt jeden gelesenen Wert; der Minutenlauf nimmt für die
Batterie-Entscheidung das Mittel der letzten 60 s statt der Einzelmessung (pulsierende Lasten wie
Herd/Backofen ließen die Batterie sonst jede Minute zwischen Laden und Entladen wechseln).

Datei im RAM (/dev/shm, tmpfs) – kein SD-Schreiben alle 3 s; nach einem Neustart leer (dann gilt
die Einzelmessung, bis wieder genug Werte da sind). Eine Datei je Datenbank (Dev/Prod/Tests getrennt).
Format: eine Zeile je Takt ``<unix-zeit> <zähler-watt> <batterie-watt>`` (Zähler + = Bezug,
− = Einspeisung; Batterie + = Entladen, − = Laden). Gemittelt wird „Netz ohne Batterie“
(Zähler + Batterie), damit der Minutenlauf nicht auf die eigenen Drosselungen des Wächters
reagiert. Nur Werte der letzten 60 s zählen (Zeitstempel); hängt der Wächter, sind es zu wenige
→ Einzelmessung.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import db

log = logging.getLogger(__name__)

RING_PATH: Optional[str] = None      # None → /dev/shm/pvc-meter-<Hash des DB-Pfads>.txt
KEEP_S = 150.0
PRUNE_BYTES = 6000                   # ab dieser Größe alte Werte entfernen (~250 Zeilen)
WINDOW_S = 60.0
MIN_VALUES = 10


def path() -> Path:
    if RING_PATH:
        return Path(RING_PATH)
    h = hashlib.md5(str(db.DB_PATH).encode()).hexdigest()[:8]
    base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path("/tmp")
    return base / f"pvc-meter-{h}.txt"


def _read(p: Path) -> list[tuple[float, float, float]]:
    out = []
    try:
        for line in p.read_text().splitlines():
            parts = line.split()
            if len(parts) == 3:                      # ältere Zeilen ohne Batteriewert zählen nicht
                try:
                    out.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def append(meter_w: float, battery_w: float, ts: Optional[float] = None) -> None:
    """Takt anhängen (Zähler, Batterie-Ist: + Entladen / − Laden); wird die Datei zu groß, alte
    Werte (> KEEP_S) entfernen."""
    ts = time.time() if ts is None else ts
    p = path()
    try:
        with open(p, "a") as f:
            f.write(f"{ts:.1f} {meter_w:.0f} {battery_w:.0f}\n")
        if p.stat().st_size > PRUNE_BYTES:
            keep = [r for r in _read(p) if r[0] >= ts - KEEP_S]
            tmp = p.with_suffix(".tmp")
            tmp.write_text("".join(f"{t:.1f} {m:.0f} {b:.0f}\n" for t, m, b in keep))
            os.replace(tmp, p)
    except OSError:
        log.warning("Zähler-Ringpuffer nicht schreibbar (%s)", p, exc_info=True)


def mean(window_s: float = WINDOW_S, now: Optional[float] = None,
         min_values: int = MIN_VALUES) -> Optional[tuple[float, int]]:
    """(Mittel „Netz ohne Batterie“, Anzahl) der Werte mit Zeitstempel in den letzten ``window_s``;
    None bei weniger als ``min_values`` frischen Werten (Wächter hängt, Neustart)."""
    now = time.time() if now is None else now
    vals = [m + b for t, m, b in _read(path()) if now - window_s <= t <= now + 5]
    if len(vals) < min_values:
        return None
    return sum(vals) / len(vals), len(vals)
