"""Rollläden Phase 3: der Heizungs-Chat lernt Rollläden (ein Chat-Feld, ein Tool ``propose_schedule``).

- Kontext für das Modell: Rollläden (id, Name, Automatik, Tür/Kontakt, Ist-Position), Sonnenzeiten heute/morgen,
  bestehende Rollladen-Zeitpläne.
- Prüfung (``validate``): Zeitpläne nur für „Automatik: PV Controller“ – für openHAB-Rollläden freundlich
  abgelehnt (Info in der Vorschau, der Rest bleibt gültig). Sofort-Befehle für alle Rollläden.
- Mehrdeutige Namen („Wohnen“ → links + rechts) → Rückfrage, auch wenn das Modell geraten hat (``ambiguity``).
- Vorschau: berechnete Uhrzeit heute/morgen, ⚠️ für Türen ohne Kontakt bzw. mit offenem Kontakt.
- Abfragen („was fahren die Rollläden heute Abend?“) lokal aus der DB, ohne Modell-Aufruf, inkl.
  openHAB-Regelzeiten der openHAB-Rollläden als Info („Wohnen links: 18:40 openHAB-Regel“).
"""

from __future__ import annotations

import logging
import re
import time as _time
from datetime import date as Date, datetime, timedelta
from typing import Any, Optional

import shutter_schedule as ss
import shutters
from heating_chat import Proposal, _day_label, _days_label

log = logging.getLogger("pv-controller.shutters")

TZ = ss.TZ
ARROW = {"up": "▲ auf", "down": "▼ zu", "stop": "■ Stop"}


def target_text(action: str, target: Optional[int]) -> str:
    return ARROW.get(action) or f"→ {target} %"


# ── Kontext ─────────────────────────────────────────────────────────────────


def build_context(scfg: shutters.ShutterConfig, statuses: list[dict[str, Any]], entries: list[dict[str, Any]],
                  now: datetime, loc: Optional[tuple[float, float]]) -> str:
    now = now.astimezone(TZ)
    today = now.date()
    by_id = {r["id"]: r for r in statuses}
    lines = ["", "Rollläden (shutter_id: Name – Automatik, Tür, Ist-Position; 0 % = offen, 100 % = zu):"]
    for sh in scfg.rooms:
        st = by_id.get(sh.id, {})
        auto = "PV Controller (Zeitpläne möglich)" if sh.automation == "pvcontroller" \
            else "openHAB-Regel (nur Sofort-Befehle, keine Zeitpläne)"
        door = ""
        if sh.is_door:
            door = ", Tür " + ({"open": "offen", "closed": "zu"}.get(st.get("contact") or "", "ohne Kontakt")
                               if sh.contact_item else "ohne Kontakt")
        pos = st.get("position")
        lines.append(f"- {sh.id}: {sh.name} – Automatik {auto}{door}, Position "
                     f"{'unbekannt' if pos is None else f'{pos} %'}" + (f", Gruppe {sh.group}" if sh.group else ""))
    if loc:
        for d in (today, today + timedelta(days=1)):
            rise, sset = ss.sun_times(d, *loc)
            lines.append(f"Sonne {d.isoformat()}: Aufgang {rise:%H:%M}, Untergang {sset:%H:%M}" if rise and sset
                         else f"Sonne {d.isoformat()}: keine Auf-/Untergangszeit")
    lines += ["", "Bestehende Rollladen-Zeitpläne (id, Rollladen, wann, Ziel, Tage, aktiv):"]
    if not entries:
        lines.append("- keine")
    for e in sorted(entries, key=lambda x: (x["shutter_id"], x["id"])):
        when = f"einmalig {e['date']}" if e["date"] else "wiederkehrend " + ",".join(e["days"])
        lines.append(f"- #{e['id']} {e['shutter_id']} {ss.describe(e)} {target_text(e['action'], e['position'])} "
                     f"{when}{'' if e['enabled'] else ' (aus)'}")
    return "\n".join(lines)


# ── Mehrdeutige Namen ───────────────────────────────────────────────────────


_WORD_RE = re.compile(r"[a-zäöüß]+")
_BOTH_RE = re.compile(r"\b(beide|beiden|alle|allen|sämtliche|roll(l)?äden|rollos|jalousien)\b")   # Plural = alle


def _words(name: str) -> list[str]:
    return [w for w in _WORD_RE.findall(name.lower()) if len(w) >= 3]


def ambiguity(text: str, scfg: shutters.ShutterConfig, touched: set[str]) -> Optional[str]:
    """Rückfrage, wenn ein Wort des Nutzers auf mehrere Rollläden passt und nichts unterscheidet.

    „Wohnen runter“ bei „Wohnen links“ + „Wohnen rechts (Straße)“ → Rückfrage; „Wohnen links“, „beide“,
    „alle“ → eindeutig. Nur relevant, wenn der Vorschlag einen dieser Rollläden betrifft.
    """
    t = text.lower()
    if _BOTH_RE.search(t):
        return None
    said = set(_WORD_RE.findall(t))
    for w in sorted(said, key=len, reverse=True):
        if len(w) < 4:
            continue
        hits = [sh for sh in scfg.rooms if any(x.startswith(w) or w.startswith(x) and len(x) >= 4
                                                for x in _words(sh.name))]
        if len(hits) < 2 or not touched & {sh.id for sh in hits}:
            continue
        common = set.intersection(*(set(_words(sh.name)) for sh in hits))
        distinguishing = {x for sh in hits for x in _words(sh.name)} - common
        if any(d in said or any(s.startswith(d[:5]) for s in said if len(s) >= 4) for d in distinguishing):
            continue
        names = [sh.name for sh in hits]
        return (f"„{w.capitalize()}“ passt auf {', '.join(names[:-1])} und {names[-1]}. "
                f"Welchen meinst du – oder beide?")
    return None


# ── Prüfen ──────────────────────────────────────────────────────────────────


def openhab_decline(sh: shutters.Shutter) -> str:
    return f"{sh.name} wird über eine openHAB-Regel gesteuert – Zeitplan erst nach Umstellung."


def validate(scfg: shutters.ShutterConfig, p: Proposal, inp: dict[str, Any], now: datetime,
             loc: Optional[tuple[float, float]]) -> list[str]:
    """Rollladen-Teil des Tool-Aufrufs prüfen und in ``p`` eintragen; gibt Fehler zurück."""
    errors: list[str] = []
    items = inp.get("shutter_entries") or []
    imm = inp.get("shutter_immediate") or []
    dels = inp.get("shutter_delete_ids") or []
    if not (items or imm or dels):
        return errors
    if not scfg.enabled:
        return ["Rollläden sind nicht aktiv (Settings → Rollläden)."]
    declined: set[str] = set()
    for i, e in enumerate(items):
        sh = scfg.shutter(e.get("shutter_id") or "")
        if sh is not None and sh.automation != "pvcontroller":
            if sh.id not in declined:
                declined.add(sh.id)
                p.declined.append(openhab_decline(sh))
            continue
        try:
            off = e.get("offset_min")
            p.shutter_entries.append(ss.validate_entry(
                scfg, e.get("shutter_id"), e.get("action"), e.get("kind"), position=e.get("position"),
                time=e.get("time"), offset_min=0 if off is None else off, not_before=e.get("not_before"),
                not_after=e.get("not_after"), days=e.get("days") or None, date=e.get("date") or None,
                loc=loc, now=now))
        except (ValueError, TypeError, AttributeError) as ex:
            errors.append(f"Rollladen-Zeitplan {i + 1}: {ex}")
    for i, im in enumerate(imm):
        sh = scfg.shutter(im.get("shutter_id") or "")
        try:
            if sh is None:
                raise ValueError(f"Rollladen {im.get('shutter_id')!r} unbekannt")
            pos = im.get("position")
            if isinstance(pos, float) and pos.is_integer():
                pos = int(pos)
            target = shutters.validate_command(sh, im.get("action"), pos)
            p.shutter_immediate.append({"shutter_id": sh.id, "action": im["action"], "target": target})
        except (ValueError, TypeError) as ex:
            errors.append(f"Rollladen sofort {i + 1}: {ex}")
    for sid in dels:
        row = ss.get_schedule(sid) if isinstance(sid, int) and not isinstance(sid, bool) else None
        if row is None:
            errors.append(f"Rollladen-Zeitplan #{sid} gibt es nicht")
        else:
            p.shutter_deletes.append(row)
    return errors


def touched(p: Proposal, inp: dict[str, Any]) -> set[str]:
    """Alle Rollläden, die der Modellvorschlag nennt (auch abgelehnte)."""
    ids = {e.get("shutter_id") for e in inp.get("shutter_entries") or []}
    ids |= {e.get("shutter_id") for e in inp.get("shutter_immediate") or []}
    ids |= {d["shutter_id"] for d in p.shutter_deletes}
    return {i for i in ids if isinstance(i, str)}


# ── Vorschau ────────────────────────────────────────────────────────────────


def _closing(action: str, target: Optional[int], current: Optional[int]) -> bool:
    return shutters.is_closing(action, target, current)


def door_warnings(scfg: shutters.ShutterConfig, p: Proposal, statuses: list[dict[str, Any]]) -> dict[int, str]:
    """Sofort-Befehle, die eine Tür mit offenem/unbekanntem Kontakt oder ohne Kontakt schließen würden →
    {Index in shutter_immediate: Grund}. Gleiche Regel wie bei den Buttons (Bestätigung nötig)."""
    by_id = {r["id"]: r for r in statuses}
    out = {}
    for n, im in enumerate(p.shutter_immediate):
        sh = scfg.shutter(im["shutter_id"])
        st = by_id.get(sh.id, {})
        cur = st.get("position") if st.get("position_reliable", True) else None
        why = st.get("door_guard") if st else shutters.door_guard(sh, None)
        if why and _closing(im["action"], im["target"], cur):
            out[n] = why
    return out


def _times(e: dict[str, Any], today: Date, loc: Optional[tuple[float, float]], now: datetime) -> str:
    """'heute 19:07, morgen 19:05' (wiederkehrend) bzw. 'Do 15.10. 07:00' (einmalig)."""
    if e["date"]:                                 # Tag steht schon in der Gruppe → nur die berechnete Uhrzeit
        if e["kind"] == "time":
            return ""
        at = ss.instant(e, Date.fromisoformat(e["date"]), loc)
        return f"{at:%H:%M}" if at else "–"
    out = []
    for off, lbl in ((0, "heute"), (1, "morgen")):
        d = today + timedelta(days=off)
        if not ss.applies_on({**e, "days": e["days"].split(",") if isinstance(e["days"], str) else e["days"]}, d):
            continue
        at = ss.instant(e, d, loc)
        if at is None or (off == 0 and ss._utc(at) <= ss._utc(now)):
            continue
        out.append(f"{lbl} {at:%H:%M}")
    return ", ".join(out)


def preview(scfg: shutters.ShutterConfig, p: Proposal, now: datetime, loc: Optional[tuple[float, float]],
            statuses: list[dict[str, Any]]) -> dict[str, Any]:
    now = now.astimezone(TZ)
    today = now.date()
    name = lambda sid: (scfg.shutter(sid).name if scfg.shutter(sid) else sid)  # noqa: E731
    groups: dict[str, dict[str, Any]] = {}
    for e in sorted(p.shutter_entries, key=lambda x: (x["date"] or "9", x["days"] or "", x["shutter_id"])):
        if e["date"]:
            key, label = e["date"], _day_label(Date.fromisoformat(e["date"]), today)
        else:
            key, label = "w:" + e["days"], _days_label(e["days"].split(","))
        sh = scfg.shutter(e["shutter_id"])
        note = None
        if sh.is_door and not sh.contact_item and e["action"] != "up" and sh.auto_close_max < 100:
            note = f"Tür ohne Kontakt: automatisch nur bis {sh.auto_close_max} %"
        elif sh.is_door and sh.contact_item and e["action"] != "up":
            note = "Tür: bei offenem Kontakt wird nicht geschlossen"
        groups.setdefault(key, {"label": label, "rows": []})["rows"].append({
            "when": ss.describe(e), "at": _times(e, today, loc, now), "shutter": name(e["shutter_id"]),
            "target": target_text(e["action"], e["position"]), "warn": note})
    warn = door_warnings(scfg, p, statuses)
    return {
        "groups": list(groups.values()),
        "immediate": [{"shutter": name(i["shutter_id"]), "target": target_text(i["action"], i["target"]),
                       "warn": warn.get(n)} for n, i in enumerate(p.shutter_immediate)],
        "deletes": [{"id": d["id"], "shutter": name(d["shutter_id"]), "when": ss.describe(d),
                     "target": target_text(d["action"], d["position"]),
                     "days": (_day_label(Date.fromisoformat(d["date"]), today) if d["date"]
                              else _days_label(d["days"]) + " (ganzer Plan)")} for d in p.shutter_deletes],
        "declined": list(p.declined),
        "door_confirm": sorted(set(warn.values())),
    }


# ── Abfragen ohne Modell ────────────────────────────────────────────────────


_SHUTTER_WORD_RE = re.compile(r"roll(l)?(a|ä)den|rollo|jalousie|markise", re.IGNORECASE)
_Q_RE = re.compile(r"^\s*(was|wann|welche\w*|fahren|fährt|gehen|geht|zeig\w*|liste)\b", re.IGNORECASE)
_ACT_RE = re.compile(r"\b(lösch\w*|entfern\w*|stell\w*|setz\w*|mach\w*|leg\w* an|plan\w* ein)\b", re.IGNORECASE)


def is_query(text: str, scfg: shutters.ShutterConfig) -> bool:
    t = text.strip()
    mentions = bool(_SHUTTER_WORD_RE.search(t)) or any(
        w in t.lower() for sh in scfg.rooms for w in _words(sh.name) if len(w) >= 5)
    return mentions and bool(_Q_RE.search(t)) and not _ACT_RE.search(t)


def _window(t: str, now: datetime) -> tuple[datetime, datetime, str]:
    today = now.date()
    day0 = lambda d, h=0: datetime(d.year, d.month, d.day, h, tzinfo=TZ)  # noqa: E731
    tom = today + timedelta(days=1)
    if "übermorgen" in t:
        d = today + timedelta(days=2)
        return day0(d), day0(d + timedelta(days=1)), _day_label(d, today)
    if "morgen früh" in t or "morgen frueh" in t:
        return day0(tom), day0(tom, 12), f"{_day_label(tom, today)} früh"
    if "morgen abend" in t:
        return day0(tom, 17), day0(tom + timedelta(days=1)), f"{_day_label(tom, today)} Abend"
    if "morgen" in t:
        return day0(tom), day0(tom + timedelta(days=1)), _day_label(tom, today)
    if "heute abend" in t or t.endswith("abend?") or "heute nacht" in t:
        return max(now, day0(today, 17)), day0(tom), "Heute Abend"
    if "heute früh" in t or "heute morgen" in t:
        return now, day0(today, 12), "Heute früh"
    if "heute" in t:
        return now, day0(tom), _day_label(today, today)
    return now, now + timedelta(hours=24), "Nächste 24 Stunden"


# openHAB-Regeln (Quartz-Cron / TimeOfDay) → Uhrzeiten an einem Tag
_QUARTZ_DOW = {"SUN": 1, "MON": 2, "TUE": 3, "WED": 4, "THU": 5, "FRI": 6, "SAT": 7}


def _field_values(f: str, lo: int, hi: int, names: Optional[dict[str, int]] = None) -> Optional[list[int]]:
    """Quartz-Feld → Werte; None bei nicht unterstützter Syntax."""
    if f in ("*", "?"):
        return list(range(lo, hi + 1))
    out: list[int] = []
    for part in f.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            if not st.isdigit():
                return None
            step = int(st)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = _num_or_name(a_s, names), _num_or_name(b_s, names)
            if a is None or b is None:
                return None
        else:
            a = _num_or_name(part, names)
            if a is None:
                return None
            b = a if step == 1 else hi
        rng = list(range(a, b + 1)) if a <= b else list(range(a, hi + 1)) + list(range(lo, b + 1))  # 22-05
        out += rng[::step]
    return sorted(set(out))


def _num_or_name(v: str, names: Optional[dict[str, int]]) -> Optional[int]:
    if v.isdigit():
        return int(v)
    return (names or {}).get(v.upper())


def cron_times(expr: str, day: Date) -> Optional[list[str]]:
    """Uhrzeiten (HH:MM) an ``day`` für einen Quartz-Cron 'sec min hour dom month dow [year]'."""
    f = expr.split()
    if len(f) < 6:
        return None
    mins, hours = _field_values(f[1], 0, 59), _field_values(f[2], 0, 23)
    dom, mon = _field_values(f[3], 1, 31), _field_values(f[4], 1, 12)
    dow = _field_values(f[5], 1, 7, _QUARTZ_DOW)
    if None in (mins, hours, dom, mon, dow):
        return None
    if day.day not in dom or day.month not in mon or (day.isoweekday() % 7) + 1 not in dow:
        return []
    return [f"{h:02d}:{m:02d}" for h in hours for m in mins]


_SEND_RE = re.compile(r"getItem\(['\"]([\w]+)['\"]\)\.sendCommand\(['\"]?(\w+)['\"]?\)")


def openhab_rule_rides(rules: list[dict[str, Any]], scfg: shutters.ShutterConfig, start: datetime,
                       end: datetime) -> list[dict[str, Any]]:
    """Fahrten der openHAB-Rollläden aus aktiven openHAB-Regeln im Zeitraum [start, end)."""
    targets = {sh.item: sh for sh in scfg.rooms if sh.automation == "openhab"}
    out = []
    for r in rules:
        st = (r.get("status") or {}).get("status")
        if st in ("UNINITIALIZED", "DISABLED"):
            continue
        cmds: list[tuple[shutters.Shutter, str]] = []
        conditional = bool(r.get("conditions"))
        for a in r.get("actions") or []:
            c = a.get("configuration") or {}
            if c.get("itemName") in targets:
                cmds.append((targets[c["itemName"]], str(c.get("command", ""))))
            script = c.get("script") or ""
            for item, cmd in _SEND_RE.findall(script):
                if item in targets:
                    cmds.append((targets[item], cmd))
                    conditional = conditional or bool(re.search(r"\bif\s*\(", script))
        if not cmds:
            continue
        d = start.date()
        while d <= (end - timedelta(seconds=1)).date():
            times: list[str] = []
            for t in r.get("triggers") or []:
                cfg = t.get("configuration") or {}
                if t.get("type") == "timer.GenericCronTrigger":
                    times += cron_times(cfg.get("cronExpression", ""), d) or []
                elif t.get("type") == "timer.TimeOfDayTrigger" and cfg.get("time"):
                    times.append(cfg["time"][:5])
            in_range = []
            for hhmm in sorted(set(times)):
                at = ss.due_instant(d, hhmm)
                if ss._utc(start) <= ss._utc(at) < ss._utc(end):
                    in_range.append(at)
            span = None
            if len(in_range) > 4:                       # z. B. alle 15 min 22–05 Uhr → eine Zeile mit Spanne
                span = f"{in_range[0]:%H:%M}–{in_range[-1]:%H:%M} mehrfach"
                in_range = [in_range[0]]
            for at in in_range:
                seen = set()
                for sh, cmd in cmds:
                    if sh.id in seen:
                        continue
                    seen.add(sh.id)
                    out.append({"at": at, "shutter": sh.name, "target": {"UP": "▲ auf", "DOWN": "▼ zu",
                                "STOP": "■ Stop"}.get(cmd.upper(), f"→ {cmd} %" if cmd.isdigit() else cmd),
                                "source": f"openHAB-Regel „{r.get('name') or r.get('uid')}“"
                                          + (f" ({span}, mit Bedingungen)" if span and conditional
                                             else f" ({span})" if span
                                             else " (mit Bedingungen)" if conditional else "")})
            d += timedelta(days=1)
    return out


_RULES_CACHE: dict[str, Any] = {"t": 0.0, "rules": None}
RULES_TTL_S = 600


def cached_rules(client: Any) -> Optional[list[dict[str, Any]]]:
    """openHAB-Regeln (10 min zwischengespeichert); None wenn nicht lesbar."""
    if _RULES_CACHE["rules"] is not None and _time.time() - _RULES_CACHE["t"] < RULES_TTL_S:
        return _RULES_CACHE["rules"]
    try:
        rules = client.get_rules()
    except Exception as e:  # noqa: BLE001 – Info, nie fatal
        log.warning("Chat: openHAB-Regeln nicht lesbar: %s", e)
        return None
    _RULES_CACHE.update(t=_time.time(), rules=rules)
    return rules


def local_answer(text: str, scfg: shutters.ShutterConfig, entries: list[dict[str, Any]], now: datetime,
                 loc: Optional[tuple[float, float]], rules: Optional[list[dict[str, Any]]],
                 schedules_enabled: bool) -> Optional[str]:
    """„Was fahren die Rollläden heute Abend?“ → Antwort aus DB + openHAB-Regeln. None = keine Abfrage."""
    if not scfg.enabled or not is_query(text, scfg):
        return None
    now = now.astimezone(TZ)
    t = text.strip().lower()
    start, end, scope = _window(t, now)
    # genannte Rollläden: die mit den meisten passenden Namenswörtern („Wohnen links“ → nur links)
    score = {sh.id: sum(1 for w in _words(sh.name) if len(w) >= 4 and w in t) for sh in scfg.rooms}
    best = max(score.values(), default=0)
    only = [sh for sh in scfg.rooms if best and score[sh.id] == best]
    only_ids = {sh.id for sh in only}
    rides = []
    d = start.date()
    while d <= (end - timedelta(seconds=1)).date():
        for e in entries:
            sh = scfg.shutter(e["shutter_id"])
            if not sh or sh.automation != "pvcontroller" or not e["enabled"] or not ss.applies_on(e, d):
                continue
            if d == now.date() and ss._ran_on(e, d):
                continue
            at = ss.instant(e, d, loc)
            if at is None or not (ss._utc(start) <= ss._utc(at) < ss._utc(end)):
                continue
            rides.append({"at": at, "shutter": sh.name, "target": target_text(e["action"], e["position"]),
                          "source": f"Zeitplan ({ss.describe(e)})" + ("" if schedules_enabled
                                                                      else " – Zeitpläne aus")})
        d += timedelta(days=1)
    rule_note = ""
    if rules is None and any(sh.automation == "openhab" for sh in scfg.rooms):
        rule_note = "\n(openHAB-Regeln nicht lesbar – deren Zeiten fehlen.)"
    elif rules:
        rides += openhab_rule_rides(rules, scfg, start, end)
    if only:
        rides = [r for r in rides if r["shutter"] in {sh.name for sh in only}]
    if re.search(r"\b(hoch|rauf|öffn\w*)\b|\bauf\b(?!\s*\d)", t):         # „wann geht … hoch?“
        rides = [r for r in rides if r["target"].startswith("▲")]
    elif re.search(r"\b(runter|schließ\w*|zu)\b", t):
        rides = [r for r in rides if r["target"].startswith("▼")]
    rides.sort(key=lambda r: (ss._utc(r["at"]), r["shutter"]))
    head = scope + (" · " + ", ".join(sh.name for sh in only) if only_ids else "")
    if not rides:
        return f"{head}: keine Fahrten geplant.{rule_note}"
    out, cur = [f"{head}:"], None
    multi_day = start.date() != (end - timedelta(seconds=1)).date()
    for r in rides:
        if multi_day and r["at"].date() != cur:
            cur = r["at"].date()
            out.append(_day_label(cur, now.date()))
        out.append(f"  {r['at']:%H:%M}  {r['shutter']} {r['target']} · {r['source']}")
    return "\n".join(out) + rule_note
