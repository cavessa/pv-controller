"""Zeitpunkte für Zeitpläne (Heizung und Rollläden): feste Uhrzeit oder Sonnenauf-/-untergang ± Offset.

- ``due_instant``: Wanduhrzeit HH:MM (Europe/Berlin) an einem Tag → Zeitpunkt, mit Zeitumstellung.
- ``sun_times``: Sonnenauf-/-untergang lokal aus latitude/longitude (NOAA-Formel, ±1 min, keine externe API).
- ``instant``: Schaltzeitpunkt eines Eintrags (kind = "time" | "sunrise" | "sunset", offset_min,
  not_before / not_after) an einem Tag.
- ``validate_when`` / ``describe``: gemeinsame Prüfung und Beschriftung („Sonnenuntergang −15 min“).
"""

from __future__ import annotations

import math
import re
from datetime import date as Date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
KINDS = ("time", "sunrise", "sunset")
OFFSET_MAX = 180
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def utc(dt: datetime) -> datetime:
    """Für Vergleiche und Abstände: aware datetimes mit derselben tzinfo vergleicht/subtrahiert Python als
    Wanduhrzeit (fold wird ignoriert) – in der doppelten Stunde im Oktober 1 h daneben. In UTC stimmt es."""
    return dt.astimezone(timezone.utc)


def due_instant(day: Date, hhmm: str) -> datetime:
    """Zeitpunkt (aware, Europe/Berlin) von HH:MM an ``day``.

    Zeitumstellung: doppelte Stunde im Oktober → erstes Auftreten (fold=0);
    fehlende Stunde im März (02:00–02:59) → Moment der Umstellung (03:00 Sommerzeit).
    """
    h, m = map(int, hhmm.split(":"))
    wall = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)
    back = wall.astimezone(timezone.utc).astimezone(TZ)
    if (back.hour, back.minute) == (h, m):
        return wall
    # Lücke: ab dem Instant vor der Lücke minutenweise vor bis die Wanduhr HH:MM erreicht
    t = wall.replace(fold=1).astimezone(timezone.utc)
    for _ in range(180):
        local = t.astimezone(TZ)
        if (local.hour, local.minute) >= (h, m) or local.date() > day:
            return local
        t += timedelta(minutes=1)
    return wall


# ── Sonnenzeiten (NOAA) ─────────────────────────────────────────────────────


def sun_times(day: Date, lat: float, lon: float) -> tuple[Optional[datetime], Optional[datetime]]:
    """Sonnenauf- und -untergang (Zenit 90,833°) an ``day`` als aware datetime in Europe/Berlin.

    None bei Polartag/-nacht. Genauigkeit etwa ±1 min.
    """
    out = []
    for rising in (True, False):
        # Iteration über die Sonnenposition zum Ereigniszeitpunkt (2 Durchläufe reichen für ±1 min)
        t_utc_min = 720 - 4 * lon            # Startwert: Sonnenmittag UTC in Minuten
        ok = True
        for _ in range(3):
            jd = _julian(day) + t_utc_min / 1440.0
            decl, eqtime = _sun_decl_eqtime(jd)
            cos_ha = (math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl))
                      - math.tan(math.radians(lat)) * math.tan(decl))
            if not -1 <= cos_ha <= 1:
                ok = False
                break
            ha = math.degrees(math.acos(cos_ha))
            t_utc_min = 720 - 4 * (lon + (ha if rising else -ha)) - eqtime
        if not ok:
            out.append(None)
            continue
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        out.append((base + timedelta(minutes=t_utc_min)).astimezone(TZ))
    return out[0], out[1]


def _julian(day: Date) -> float:
    """Julianisches Datum um 0:00 UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() / 86400.0 + 2440587.5


def _sun_decl_eqtime(jd: float) -> tuple[float, float]:
    """Deklination (rad) und Zeitgleichung (min) nach NOAA."""
    t = (jd - 2451545.0) / 36525.0
    l0 = math.radians((280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360)
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * m) * (0.019993 - 0.000101 * t)
         + math.sin(3 * m) * 0.000289)
    true_long = l0 + math.radians(c)
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = true_long - math.radians(0.00569 + 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(app_long))
    y = math.tan(eps / 2) ** 2
    eqtime = 4 * math.degrees(y * math.sin(2 * l0) - 2 * e * math.sin(m) + 4 * e * y * math.sin(m) * math.cos(2 * l0)
                              - 0.5 * y * y * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m))
    return decl, eqtime


def location(raw: dict) -> Optional[tuple[float, float]]:
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180:
        return float(lat), float(lon)
    return None


def instant(entry: dict[str, Any], day: Date, loc: Optional[tuple[float, float]]) -> Optional[datetime]:
    """Schaltzeitpunkt des Eintrags an ``day`` (auf die Minute), None wenn nicht berechenbar."""
    if entry["kind"] == "time":
        return due_instant(day, entry["time"])
    if loc is None:
        return None
    rise, sset = sun_times(day, *loc)
    base = rise if entry["kind"] == "sunrise" else sset
    if base is None:
        return None
    at = (base + timedelta(minutes=int(entry.get("offset_min") or 0))).replace(second=0, microsecond=0)
    if entry.get("not_before"):
        at = max(at, due_instant(day, entry["not_before"]))
    if entry.get("not_after"):
        at = min(at, due_instant(day, entry["not_after"]))
    return at


def describe(entry: dict[str, Any]) -> str:
    """'07:00' / 'Sonnenuntergang +15 min (nicht nach 22:00)'."""
    if entry["kind"] == "time":
        return entry["time"]
    off = int(entry.get("offset_min") or 0)
    s = "Sonnenaufgang" if entry["kind"] == "sunrise" else "Sonnenuntergang"
    if off:
        s += f" {'+' if off > 0 else '−'}{abs(off)} min"
    lim = [f"nicht vor {entry['not_before']}" if entry.get("not_before") else "",
           f"nicht nach {entry['not_after']}" if entry.get("not_after") else ""]
    lim = [x for x in lim if x]
    return s + (f" ({', '.join(lim)})" if lim else "")


def hhmm(v: Any, what: str) -> Optional[str]:
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not _TIME_RE.match(v):
        raise ValueError(f"{what} muss HH:MM sein")
    return v


def validate_when(kind: Any, time: Any = None, offset_min: Any = 0, not_before: Any = None,
                  not_after: Any = None, loc: Optional[tuple[float, float]] = None) -> dict[str, Any]:
    """Zeit-Teil eines Eintrags prüfen → {kind, time, offset_min, not_before, not_after}; ValueError mit Klartext."""
    if kind not in KINDS:
        raise ValueError("Zeitart muss Uhrzeit, Sonnenaufgang oder Sonnenuntergang sein")
    out: dict[str, Any] = {"kind": kind, "time": None, "offset_min": 0, "not_before": None, "not_after": None}
    if kind == "time":
        if not isinstance(time, str) or not _TIME_RE.match(time):
            raise ValueError("Uhrzeit muss HH:MM sein")
        out["time"] = time
        return out
    if loc is None:
        raise ValueError("Sonnenzeiten brauchen latitude/longitude in der Config")
    offset_min = 0 if offset_min is None else offset_min
    if not isinstance(offset_min, int) or isinstance(offset_min, bool) or abs(offset_min) > OFFSET_MAX:
        raise ValueError(f"Offset muss eine ganze Zahl zwischen −{OFFSET_MAX} und {OFFSET_MAX} min sein")
    out["offset_min"] = offset_min
    out["not_before"] = hhmm(not_before, "„nicht vor“")
    out["not_after"] = hhmm(not_after, "„nicht nach“")
    if out["not_before"] and out["not_after"] and out["not_before"] >= out["not_after"]:
        raise ValueError("„nicht vor“ muss vor „nicht nach“ liegen")
    return out
