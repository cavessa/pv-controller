"""Speicher-Verlauf: Temperatur + Heizstab-Leistung aus temp_history.json.

Liefert fertig berechnete Daten (Buckets, Heizphasen, Min/Max, Steigung, ETA)
für /api/speicher/history; das Frontend rendert nur.

Persistenz: temp_history.json (1 Eintrag/Minute, 25 h) mit `temp` und
`heater_w` (Heizstab-Leistung am Zähler). Eine feinere Auflösung als eine
Minute wird nicht gespeichert – das Stunden-Fenster nutzt deshalb die
1-Minuten-Werte (nicht 30 s).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import db

log = logging.getLogger(__name__)

ON_THRESHOLD_W = 100.0       # Heizstab gilt als „an" ab dieser Leistung
MIN_RUN_S = 60               # kürzere An-Phasen sind Flattern und zählen nicht
MAX_GAP_S = 180              # größere Lücken zwischen Samples trennen Phasen
DAY_BUCKET_MIN = 5
RATE_WINDOW_MIN = 10


def _load_samples() -> list[tuple[datetime, Optional[float], Optional[float]]]:
    path = db.TEMP_HISTORY_PATH
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out = []
    for e in raw:
        try:
            out.append((datetime.fromisoformat(e["t"]), e.get("temp"), e.get("heater_w")))
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda s: s[0])
    return out


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _heating_runs(samples) -> tuple[list[tuple[datetime, datetime]], bool]:
    """([(Start, Ende)], läuft_noch): Ende = erster Sample nach der Phase (oder letzter an-Sample)."""
    runs: list[tuple[datetime, datetime]] = []
    start = last_on = None
    prev_t = None
    for t, _temp, w in samples:
        if w is None:
            continue
        on = w > ON_THRESHOLD_W
        if start is not None and prev_t is not None and (t - prev_t).total_seconds() > MAX_GAP_S:
            runs.append((start, last_on))   # Lücke: Phase endet beim letzten an-Sample
            start = last_on = None
        if on:
            if start is None:
                start = t
            last_on = t
        elif start is not None:
            runs.append((start, t))
            start = last_on = None
        prev_t = t
    ongoing = start is not None
    if ongoing:
        runs.append((start, last_on))
    return [(a, b) for (a, b) in runs if (b - a).total_seconds() >= MIN_RUN_S], ongoing


def _energy_kwh(samples) -> Optional[float]:
    """Heizstab-Energie (kWh): Rechteckregel über die Leistungswerte > Schwelle."""
    wh = 0.0
    seen = False
    prev = None
    for t, _temp, w in samples:
        if w is None:
            prev = None
            continue
        seen = True
        if prev is not None:
            pt, pw = prev
            dt = min((t - pt).total_seconds(), MAX_GAP_S)
            if pw > ON_THRESHOLD_W:
                wh += pw * dt / 3600.0
        prev = (t, w)
    return round(wh / 1000.0, 2) if seen else None


def _slope_per_h(samples, now: datetime) -> Optional[float]:
    """Lineare Regression der Temperatur über die letzten RATE_WINDOW_MIN Minuten (°C/h)."""
    pts = [((t - now).total_seconds() / 3600.0, v) for t, v, _ in samples
           if v is not None and (now - t) <= timedelta(minutes=RATE_WINDOW_MIN)]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den


def summarize(samples_day) -> dict:
    temps = [(t, v) for t, v, _ in samples_day if v is not None]
    out: dict = {"tmin": None, "tmax": None, "kwh": _energy_kwh(samples_day), "phases": None}
    if temps:
        lo = min(temps, key=lambda x: x[1])
        hi = max(temps, key=lambda x: x[1])
        out["tmin"], out["tmin_t"] = lo[1], _hhmm(lo[0])
        out["tmax"], out["tmax_t"] = hi[1], _hhmm(hi[0])
    if any(w is not None for _, _, w in samples_day):
        out["phases"] = len(_heating_runs(samples_day)[0])
    return out


def persist_today(all_samples=None) -> None:
    """Tageszusammenfassung für die 7-Tage-Ansicht in storage_day schreiben."""
    samples = all_samples if all_samples is not None else _load_samples()
    today = datetime.now().date()
    day = [s for s in samples if s[0].date() == today]
    if not day:
        return
    s = summarize(day)
    if s["tmin"] is None:
        return
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO storage_day (date, tmin, tmax, kwh, phases)
               VALUES (?,?,?,?,?)""",
            (today.isoformat(), s["tmin"], s["tmax"], s["kwh"], s["phases"]),
        )


def _sun_window(day: datetime) -> Optional[dict]:
    w = db.get_weather(day.strftime("%Y-%m-%d")) or {}
    sr, ss = w.get("sunrise"), w.get("sunset")
    if not sr or not ss:
        return None
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    def ts(hhmm: str) -> int:
        h, m = (int(x) for x in hhmm.split(":")[:2])
        return int(base.replace(hour=h, minute=m).timestamp())
    return {"rise": ts(sr), "set": ts(ss), "rise_label": sr, "set_label": ss}


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _events(runs, ongoing, samples, lo: datetime) -> list[dict]:
    temp_at = {t: v for t, v, _ in samples}
    def temp_near(dt):
        if dt in temp_at and temp_at[dt] is not None:
            return temp_at[dt]
        cand = [(abs((t - dt).total_seconds()), v) for t, v, _ in samples if v is not None]
        return min(cand)[1] if cand else None
    ev = []
    for i, (a, b) in enumerate(runs):
        if a >= lo:
            ev.append({"ts": _ts(a), "state": "on", "temp": temp_near(a)})
        last = i == len(runs) - 1
        if b >= lo and not (last and ongoing):
            ev.append({"ts": _ts(b), "state": "off", "temp": temp_near(b)})
    return ev


def _week(samples, today_summary: dict) -> list[dict]:
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    with sqlite3.connect(db.DB_PATH) as conn:
        rows = {
            r[0]: r for r in conn.execute(
                "SELECT date, tmin, tmax, kwh, phases FROM storage_day WHERE date >= ?",
                (days[0].isoformat(),),
            )
        }
        hourly = {}
        for r in conn.execute(
            """SELECT substr(timestamp,1,10), MIN(storage_temp_c), MAX(storage_temp_c),
                      SUM(heater_w), COUNT(heater_w)
               FROM pv_hourly_log WHERE timestamp >= ? AND storage_temp_c IS NOT NULL
               GROUP BY 1""",
            (days[0].isoformat(),),
        ):
            hourly[r[0]] = r
    out = []
    for d in days:
        key = d.isoformat()
        if d == today and today_summary.get("tmin") is not None:
            row = {"tmin": today_summary["tmin"], "tmax": today_summary["tmax"],
                   "kwh": today_summary["kwh"], "phases": today_summary["phases"]}
        elif key in rows:
            row = {"tmin": rows[key][1], "tmax": rows[key][2], "kwh": rows[key][3], "phases": rows[key][4]}
        elif key in hourly:
            h = hourly[key]   # Näherung aus stündlichen Snapshots (Leistung × 1 h)
            row = {"tmin": h[1], "tmax": h[2],
                   "kwh": round(h[3] / 1000.0, 2) if h[4] else None, "phases": None}
        else:
            row = {"tmin": None, "tmax": None, "kwh": None, "phases": None}
        row["date"] = key
        out.append(row)
    return out


def build(range_: str, max_temp: float, resume_temp: float) -> dict:
    now = datetime.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    samples = _load_samples()
    today = [s for s in samples if s[0] >= day_start]
    cur_s = next((s for s in reversed(samples) if s[1] is not None), None)
    cur = cur_s[1] if cur_s else None

    rate = _slope_per_h(samples, now)
    sun = _sun_window(now)
    eta_ts = eta_label = None
    if cur is not None and cur >= resume_temp:
        eta_label = "erreicht"
    elif rate is not None and rate > 0.05 and cur is not None:
        eta = now + timedelta(hours=(resume_temp - cur) / rate)
        eta_ts = _ts(eta)
        # Heizen geht nur mit Sonne: ETA nach Sonnenuntergang gilt als „heute nicht"
        if sun is not None and eta_ts > sun["set"]:
            eta_label = "heute nicht"
        else:
            eta_label = _hhmm(eta) + (" +1d" if eta.date() > now.date() else "")

    summ = summarize(today)
    resp: dict = {
        "range": range_,
        "now": _ts(now),
        "day_start": _ts(day_start),
        "day_end": _ts(day_start + timedelta(days=1)),
        "current": cur,
        "resume": resume_temp,
        "max": max_temp,
        "rate_c_per_h": round(rate, 2) if rate is not None else None,
        "eta_ts": eta_ts,
        "eta_label": eta_label,
        "min": {"v": summ.get("tmin"), "t": summ.get("tmin_t")},
        "maxday": {"v": summ.get("tmax"), "t": summ.get("tmax_t")},
        "heating": {"phases": summ["phases"], "kwh": summ["kwh"]},
        "sun": sun,
    }

    if range_ == "7d":
        resp["days"] = _week(samples, summ)
        return resp

    runs, ongoing = _heating_runs(today)

    if range_ == "24h":
        buckets: dict[int, list] = {}
        for t, v, w in today:
            b = ((t.hour * 60 + t.minute) // DAY_BUCKET_MIN) * DAY_BUCKET_MIN
            buckets.setdefault(b, []).append((v, w))
        temps, power = [], []
        for b in sorted(buckets):
            ts = _ts(day_start + timedelta(minutes=b))
            tv = [v for v, _ in buckets[b] if v is not None]
            pw = [w for _, w in buckets[b] if w is not None]
            if tv:
                temps.append([ts, round(sum(tv) / len(tv), 2)])
            if pw:
                power.append([ts, round(sum(pw) / len(pw))])
        resp["temps"] = temps
        resp["power"] = power
        resp["events"] = _events(runs, ongoing, today, day_start)

    # Stundenfenster (bei 24h und 1h)
    lo = now - timedelta(minutes=60)
    hour = [s for s in samples if s[0] >= lo]
    h_runs, h_ongoing = _heating_runs(hour)
    resp["hour"] = {
        "from": _ts(lo),
        "to": _ts(now),
        "temps": [[_ts(t), v] for t, v, _ in hour if v is not None],
        "power": [[_ts(t), round(w)] for t, _, w in hour if w is not None],
        "events": _events(h_runs, h_ongoing, hour, lo),
    }
    return resp
