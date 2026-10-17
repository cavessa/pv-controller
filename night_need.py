"""Batterie-Größe: Strombedarf pro Nacht und PV-Überschuss pro Tag (aus den Minutenwerten).

Grundlage sind main_meter_min (Hauptzähler, + Bezug / − Einspeisung), zendure_min (Laden/Entladen) und
load_min (Wallbox, Heizstab). Je Minute: Haus-Netto = Zähler + Entladen − Laden (= Hausverbrauch − PV).
Positiv heißt, das Haus braucht Strom, den die PV gerade nicht liefert; negativ ist Überschuss.

Nacht D→D+1: vom letzten Überschuss am Nachmittag/Abend von D bis zum ersten Überschuss am Morgen von
D+1 (Haus-Netto über 15 min gemittelt, damit kurze Spitzen die Grenze nicht verschieben). Gab es keinen
Überschuss (trüber Tag), gelten Sonnenunter- bzw. -aufgang (``estimated``).

Wallbox und Heizstab zählen nicht zum Bedarf: das Auto lädt nie aus der Batterie, der Heizstab läuft
nachts nur im Sommermodus aus dem Netz. Ihre Leistung kommt aus load_min; Minuten davor (vor Einführung
der Tabelle) nehmen den Stundenwert aus pv_hourly_log (``loads_estimated``). Der Whirlpool bleibt im
Bedarf; sein Anteil (``whirlpool_wh``) und sein Anteil an den Spitzen über der Entladeleistung
(``whirlpool_peak_wh``) kommen aus load_min.whirlpool_w (None ohne Messwerte).

- ``need_wh``: Bedarf in der Nacht ohne Wallbox/Heizstab.
- ``capacity_wh``: davon, was ein Speicher mit der Entladegrenze (``max_discharge_w``) hätte liefern
  können – Spitzen darüber kommen immer aus dem Netz, egal wie groß der Speicher ist.
- ``battery_wh``: was tatsächlich aus der Batterie kam; ``excluded_wh``: Wallbox + Heizstab.
- Tag D: ``surplus_wh`` = PV-Überschuss zwischen Morgen- und Abendgrenze (geladen + eingespeist),
  ``charged_wh`` = davon in die Batterie geladen. Mehr als der Überschuss kann kein Speicher füllen.
"""
from __future__ import annotations

import sqlite3
from datetime import date as Date, datetime, timedelta
from typing import Any, Optional

import db
import suntime

SMOOTH_MIN = 15
EVENING_FROM = 12 * 60          # Abendgrenze frühestens ab 12:00 suchen
MORNING_FROM = 4 * 60           # Morgengrenze ab 04:00 …
MORNING_UNTIL = 14 * 60         # … bis spätestens 14:00 suchen
CUT_MIN = 30                    # Daten beginnen/enden > 30 min nach/vor der Grenze → Nacht angeschnitten, weglassen
MIN_COVERAGE = 0.9              # Anteil Minuten mit Zählerwert, damit Nacht/Tag als vollständig gilt
OPEN = 10 ** 9                  # Grenze noch nicht erreicht (laufender Tag)
EFFICIENCY = 0.95               # nutzbar (AC) / Nennkapazität × SoC-Fenster (gemessen 16./17.10.: 7,5 von 7,8 kWh)


def _minutes(day: Date) -> list[str]:
    base = datetime(day.year, day.month, day.day)
    return [(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M") for i in range(1440)]


def load_data(first: Date, last: Date) -> dict[str, dict]:
    """Minutenwerte (Zähler, Batterie, Verbraucher) und Stundenwerte der Verbraucher, ``first``–``last``."""
    lo, hi = f"{first:%Y-%m-%d} 00:00", f"{last:%Y-%m-%d} 23:59"
    with sqlite3.connect(db.DB_PATH) as conn:
        meter = dict(conn.execute("SELECT ts, avg_w FROM main_meter_min WHERE ts BETWEEN ? AND ?", (lo, hi)))
        bat = {r[0]: (r[1] or 0.0, r[2] or 0.0) for r in conn.execute(
            "SELECT ts, charge_w, discharge_w FROM zendure_min WHERE ts BETWEEN ? AND ?", (lo, hi))}
        rows = conn.execute(
            "SELECT ts, wallbox_w, heater_w, whirlpool_w FROM load_min WHERE ts BETWEEN ? AND ?", (lo, hi)).fetchall()
        loads = {r[0]: (r[1] or 0.0) + (r[2] or 0.0) for r in rows}
        whirl = {r[0]: r[3] for r in rows if r[3] is not None}
        hourly = {r[0][:13]: (r[1] or 0.0) + (r[2] or 0.0) for r in conn.execute(
            "SELECT timestamp, wallbox_w, heater_w FROM pv_hourly_log WHERE timestamp BETWEEN ? AND ?", (lo, hi))}
    return {"meter": meter, "bat": bat, "loads": loads, "hourly": hourly, "whirl": whirl}


def _series(days: list[Date], data: dict) -> tuple[list[str], list, list]:
    """Minutenreihe: ts, Haus-Netto (oder None) und (Zähler, Laden, Entladen, Verbraucher, geschätzt)."""
    meter, bat, loads, hourly = data["meter"], data["bat"], data.get("loads", {}), data.get("hourly", {})
    whirl = data.get("whirl", {})
    ts_all, net, raw = [], [], []
    for d in days:
        for ts in _minutes(d):
            ts_all.append(ts)
            m = meter.get(ts)
            if m is None:
                net.append(None)
                raw.append(None)
                continue
            ch, dis = bat.get(ts, (0.0, 0.0))
            if ts in loads:
                ld, est = loads[ts], False
            else:
                ld = hourly.get(ts[:13], 0.0)
                est = ld > 0
            net.append(m + dis - ch)
            raw.append((m, ch, dis, ld, est, whirl.get(ts)))
    return ts_all, net, raw


def _smooth(net: list[Optional[float]], w: int = SMOOTH_MIN) -> list[Optional[float]]:
    """Gleitendes Mittel über ``w`` Minuten (zentriert); None bei zu wenigen Werten."""
    out, half = [], w // 2
    s, n = 0.0, 0
    vals = net + [None] * (half + 1)
    for i in range(-half, half):                          # Fenster für i = 0 vorbereiten
        if 0 <= i < len(net) and net[i] is not None:
            s, n = s + net[i], n + 1
    for i in range(len(net)):
        add, drop = vals[i + half], (net[i - half - 1] if i - half - 1 >= 0 else None)
        if add is not None:
            s, n = s + add, n + 1
        if drop is not None:
            s, n = s - drop, n - 1
        out.append(s / n if n >= w // 3 else None)
    return out


def _sun_index(day: Date, which: int, loc, day_offset: int, fallback_min: int) -> int:
    """Index (Minute ab Reihenbeginn) von Sonnenauf-/-untergang an ``day``; ohne Standort Festwert."""
    minute = fallback_min
    if loc:
        t = suntime.sun_times(day, *loc)[which]
        if t is not None:
            minute = t.hour * 60 + t.minute
    return day_offset * 1440 + minute


def compute(days_back: int = 14, now: Optional[datetime] = None, loc=None,
            max_discharge_w: float = 2400.0, data: Optional[dict] = None) -> dict[str, Any]:
    """Nächte und Tage der letzten ``days_back`` Tage (älteste zuerst); die laufende Nacht mit ``ongoing``."""
    now = now or datetime.now()
    today = now.date()
    first = today - timedelta(days=days_back)
    days = [first + timedelta(days=i) for i in range(days_back + 1)]
    if data is None:
        data = load_data(first, today)
    ts_all, net, raw = _series(days, data)
    sm = _smooth(net)
    now_idx = (today - first).days * 1440 + now.hour * 60 + now.minute

    def surplus_at(i: int) -> bool:
        return i <= now_idx and sm[i] is not None and sm[i] < 0

    # Grenzen je Tag: Morgen = erster Überschuss, Abend = Ende des letzten Überschusses
    morning, evening = {}, {}
    for k, d in enumerate(days):
        base = k * 1440
        m = next((base + i for i in range(MORNING_FROM, MORNING_UNTIL) if surplus_at(base + i)), None)
        if m is not None:
            morning[k] = (m, False)
        elif now_idx < base + MORNING_UNTIL:        # heute, noch kein Überschuss: Nacht läuft noch
            morning[k] = (OPEN, False)
        else:
            morning[k] = (_sun_index(d, 0, loc, k, 8 * 60), True)
        sunset = _sun_index(d, 1, loc, k, 18 * 60)
        if now_idx < sunset:                        # heute vor Sonnenuntergang: noch keine Nacht
            evening[k] = (OPEN, False)
            continue
        e = next((base + i + 1 for i in range(1439, EVENING_FROM - 1, -1) if surplus_at(base + i)), None)
        evening[k] = (e, False) if e is not None else (sunset, True)

    nights, day_rows = [], []
    for k in range(len(days)):
        # Tag k: Überschuss zwischen Morgen- und Abendgrenze
        a, b = morning[k][0], min(evening[k][0], now_idx + 1)
        if a != OPEN and b > a:
            sur = sum(-v for v in net[a:b] if v is not None and v < 0) / 60
            chg = sum(r[1] for r in raw[a:b] if r is not None) / 60
            have = sum(1 for v in net[a:b] if v is not None)
            if have:
                day_rows.append({"date": days[k].isoformat(), "surplus_wh": round(sur), "charged_wh": round(chg),
                                 "complete": evening[k][0] <= now_idx and have >= MIN_COVERAGE * (b - a)})
        if k + 1 >= len(days) or evening[k][0] == OPEN:
            continue
        start, est_s = evening[k]
        end, est_e = morning[k + 1]
        ongoing = end > now_idx
        stop = min(end, now_idx + 1)
        if stop <= start:
            continue
        need = cap = batt = excl = wh_all = wh_peak = 0.0
        have = wh_min = 0
        first_i = last_i = None
        loads_est = False
        for i in range(start, stop):
            v = net[i]
            if v is None:
                continue
            have += 1
            first_i = i if first_i is None else first_i
            last_i = i
            _m, _ch, dis, ld, est, wp = raw[i]
            batt += dis
            if wp is not None:
                wh_min += 1
            if v > 0:
                base_w = max(0.0, v - ld)
                excl += v - base_w
                need += base_w
                cap += min(base_w, max_discharge_w)
                loads_est = loads_est or (est and ld > 0)
                if wp is not None and wp > 0:            # Whirlpool ist Teil des Bedarfs (nicht herausgerechnet)
                    w_in = min(wp, base_w)
                    wh_all += w_in
                    wh_peak += min(w_in, max(0.0, base_w - max_discharge_w))
        if not have or first_i - start > CUT_MIN or (not ongoing and stop - 1 - last_i > CUT_MIN):
            continue                                # keine Daten oder angeschnitten (Datenbeginn/-ende mitten in der Nacht)
        nights.append({
            "date": days[k].isoformat(),
            "start": ts_all[start][11:], "end": ts_all[stop - 1][11:] if ongoing else ts_all[end][11:],
            "need_wh": round(need / 60), "capacity_wh": round(cap / 60),
            "battery_wh": round(batt / 60), "excluded_wh": round(excl / 60),
            # Whirlpool-Anteil am Bedarf / an den Spitzen über der Entladeleistung; None ohne Messwerte
            "whirlpool_wh": round(wh_all / 60) if wh_min else None,
            "whirlpool_peak_wh": round(wh_peak / 60) if wh_min else None,
            "whirlpool_coverage": round(wh_min / have, 2),
            "estimated": est_s or est_e, "loads_estimated": loads_est, "ongoing": ongoing,
            "complete": not ongoing and have >= MIN_COVERAGE * (stop - start),
        })
    return {"nights": nights, "days": day_rows}


def summary(res: dict[str, Any], capacity_wh: float, min_soc: float, target_soc: float = 100.0) -> dict[str, Any]:
    """Kennzahlen über die vollständigen Nächte: ø/max Bedarf, nötige Größe, was die PV hätte füllen können."""
    window = max(0.05, (target_soc - min_soc) / 100.0)
    usable_now = capacity_wh * window * EFFICIENCY
    full = [n for n in res["nights"] if n["complete"]]
    surplus = {d["date"]: d["surplus_wh"] for d in res["days"] if d["complete"]}
    out: dict[str, Any] = {"nights": len(full), "usable_now_wh": round(usable_now),
                           "capacity_now_wh": round(capacity_wh), "window": window, "efficiency": EFFICIENCY}
    if not full:
        return out
    caps = [n["capacity_wh"] for n in full]
    # was ein beliebig großer Speicher hätte liefern können: Bedarf, höchstens der Überschuss am Tag davor
    fillable = [min(n["capacity_wh"], surplus[n["date"]]) for n in full if n["date"] in surplus]
    out.update({
        "need_avg_wh": round(sum(n["need_wh"] for n in full) / len(full)),
        "capacity_avg_wh": round(sum(caps) / len(caps)),
        "capacity_max_wh": max(caps),
        "peak_avg_wh": round(sum(n["need_wh"] - n["capacity_wh"] for n in full) / len(full)),
        "size_for_max_wh": round(max(caps) / window / EFFICIENCY),
        "fillable_max_wh": max(fillable) if fillable else None,
        "size_fillable_wh": round(max(fillable) / window / EFFICIENCY) if fillable else None,
        "nights_covered": sum(1 for c in caps if c <= usable_now),
    })
    return out
