"""Erklär-Kontext für Warum-Fragen im Chat (nur lesen).

Der Server baut aus dem Entscheidungs-Schnappschuss (``decisions``), dem Zeitplan-Verlauf
(``schedule_log``), dem Geräte-Ereignislog und der Konfiguration (nur Zahlen und Namen) einen Text aus
Abschnitten; bei Fragen zu einem Raum mit Zeitbezug kommt der Ist/Soll-Verlauf aus der openHAB-Persistenz
(rrd4j) verdichtet dazu. Das Sprachmodell bekommt genau diesen Text und darf nur ihn verwenden; die UI zeigt ihn
unter „Grundlage“. Fehlt etwas, steht dort ausdrücklich „unbekannt“.

Nichts hier schaltet, sendet oder schreibt. In den Kontext kommen keine Adressen und keine
Zugangsdaten: es werden nur ausgewählte Zahlen/Namen gelesen, und der fertige Text läuft zum Schluss
noch durch ``redact`` (Keys) und ``redact.scrub_addresses`` (URLs, IPs, MACs).
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import db
import decisions
import reasons
import redact
import schedule_log
import suntime

TZ = suntime.TZ
MAX_TOKENS = 6000                 # Obergrenze für den Erklär-Kontext (geschätzt, s. est_tokens)
CHARS_PER_TOKEN = 3.0             # vorsichtig für deutschen Text mit vielen Zahlen (gemessen: ~3,2–3,6)
MAX_CHARS = int(MAX_TOKENS * CHARS_PER_TOKEN) - 1500
RECENT_MIN = 60                   # „die letzten 60 min“
POINT_MIN = 15                    # Zeitpunkt in der Frage: ± 15 min
STALE_MIN = 5                     # letzter Schnappschuss älter → Hinweis
MAX_CHANGES = 40                  # Wechsel je Abschnitt
MAX_WINDOWS = 2
ROOM_HISTORY_ROOMS = 3            # Raum-Verlauf nur für ausdrücklich genannte Räume, höchstens so viele
ROOM_HISTORY_POINTS = 12          # Ist-Verlauf: höchstens so viele Mittelwerte je Zeitraum
SETPOINT_LOOKBACK_H = 6           # Soll-Verlauf: so weit vor dem Zeitraum nach dem Anfangswert suchen

# (Item, Start, Ende) → [(epoch_ms, Wert)] aus der openHAB-Persistenz; wirft bei Fehlern
HistoryFn = Callable[[str, datetime, datetime], list[tuple[int, float]]]

_WHY_RE = re.compile(r"\b(warum|wieso|weshalb|weswegen|woran liegt|aus welchem grund|wie kommt es|"
                     r"was ist (?:da |denn )?(?:los|passiert)|was war (?:da |denn )?(?:los|passiert))\b", re.IGNORECASE)
_COMMAND_RE = re.compile(r"\b(stell\w*|setz\w*|mach\w*|fahr\w*|schalt\w*|lösch\w*|entfern\w*|plan\w*|öffne\w*|"
                         r"schließ\w*|dreh\w*|bitte)\b|\bauf \d", re.IGNORECASE)
_SHUTTER_WORDS = re.compile(r"rolll?[aä]d|rollo|jalousie", re.IGNORECASE)
_HEATING_WORDS = re.compile(r"heizung|heizkörper|thermostat|zimmer|raum\b|räume|sollwert|soll\b", re.IGNORECASE)

ACTION_DE = {
    "turn_on": "schaltet EIN", "turn_off": "schaltet AUS", "keep_on": "bleibt an", "keep_off": "bleibt aus",
    "skip_min_on": "bleibt an", "skip_min_off": "bleibt aus", "budget": "Budget", "charge": "lädt",
    "discharge": "entlädt", "idle": "Standby", "hold": "Pause", "blocked": "gesperrt", "override": "manuell",
    "manual_override": "manuell", "unreachable": "nicht erreichbar", "none": "nicht aktiv",
    "unknown": "Zustand unbekannt", "unchanged": "keine Änderung", "skipped": "nicht gesteuert",
    "release": "wird freigegeben", "pause": "pausiert", "error": "Fehler", "auto_disabled": "deaktiviert",
    "auto_reenabled": "reaktiviert",
}
DEVICE_NAMES = {"zendure": "Batterie", "wallbox_control": "Wallbox (Ladesteuerung)",
                "heizstab_phase1": "Heizstab Phase 1", "heizstab_phase2": "Heizstab Phase 2",
                "heizstab_phase3": "Heizstab Phase 3"}
_CAR = {1: "kein Auto angesteckt", 2: "lädt", 3: "wartet aufs Auto", 4: "Laden beendet"}
_ON = {"turn_on", "keep_on", "skip_min_on"}       # Gerät ist nach diesem Lauf an
_ACTIVE_REASONS = {"budget_available", "grid_assist", "keep_running", "surplus_sufficient"}


# ── Erkennen ────────────────────────────────────────────────────────────────


def _segments(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[?!.;\n]+", text) if s.strip()]


def is_why_text(text: str) -> bool:
    return bool(_WHY_RE.search(text or ""))


def has_command(text: str) -> bool:
    """Enthält die Nachricht neben der Frage einen Auftrag (Mischfrage)? Ein Satz ohne Fragewort, der wie
    ein Auftrag klingt („stell es auf 21“). Nur dann darf aus einer Warum-Frage ein Vorschlag werden."""
    return any(not _WHY_RE.search(s) and _COMMAND_RE.search(s) for s in _segments(text or ""))


def is_why(history: list[dict[str, str]]) -> bool:
    """Warum-/Statusfrage? Letzte Nutzer-Nachricht – oder eine kurze Nachfrage („und um 15 Uhr?“) nach einer."""
    users = [m["content"] for m in history if m.get("role") == "user"]
    if not users:
        return False
    last = users[-1]
    if is_why_text(last):
        return True
    return (len(users) > 1 and is_why_text(users[-2]) and len(last) <= 80 and last.rstrip().endswith("?")
            and not has_command(last))


# ── Zeitbezug ───────────────────────────────────────────────────────────────


@dataclass
class Window:
    label: str
    start: datetime
    end: datetime
    point: Optional[datetime] = None      # Zeitpunkt (± POINT_MIN) statt Zeitraum


_PARTS = (
    (r"heute\s+nacht|letzte\s+nacht|in\s+der\s+nacht|nachts", "nachts", (-2, 0), (6, 0)),
    (r"früh\b|morgens|am\s+morgen|(?:heute|gestern)\s+morgen", "früh", (5, 0), (9, 0)),
    (r"vormittag", "vormittags", (9, 0), (12, 0)),
    (r"nachmittag", "nachmittags", (13, 0), (17, 0)),
    (r"mittag", "mittags", (11, 30), (13, 30)),
    (r"abend", "abends", (17, 0), (22, 0)),
)
_CLOCK_RE = re.compile(r"\b(?:(um|gegen|ab|seit|kurz\s+nach|kurz\s+vor)\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(uhr)?\b"
                       r"(?!\s*(?:stunde|std|min|tag|grad|°|%|k?w\b|phase|prozent))", re.IGNORECASE)
_AGO_RE = re.compile(r"\bvor\s+(\d{1,3}|einer|einem|zwei|drei)\s*(min\w*|std\w*|stunden?|h)\b", re.IGNORECASE)
_NUM = {"einer": 1, "einem": 1, "zwei": 2, "drei": 3}


def _at(day: datetime, hh: int, mm: int) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hh, minutes=mm)


def time_windows(text: str, now: datetime) -> list[Window]:
    """Zeitbezüge der Frage → Fenster („um 14 Uhr“ → 13:45–14:15, „heute früh“ → 05:00–09:00, „gestern“)."""
    t = (text or "").lower()
    now = now.astimezone(TZ)
    off = -2 if "vorgestern" in t else -1 if "gestern" in t else 0
    day = now + timedelta(days=off)
    day_lbl = {0: "heute", -1: "gestern", -2: "vorgestern"}[off]
    out: list[Window] = []

    for m in _AGO_RE.finditer(t):
        n = _NUM.get(m.group(1)) or int(m.group(1))
        mins = n if m.group(2).startswith("min") else n * 60
        p = now - timedelta(minutes=mins)
        out.append(Window(f"vor {n} {'min' if m.group(2).startswith('min') else 'h'} ({p:%H:%M})",
                          p - timedelta(minutes=POINT_MIN), min(now, p + timedelta(minutes=POINT_MIN)), p))
    for m in _CLOCK_RE.finditer(t):
        prep, hh, mm, uhr = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if not (uhr or mm or prep) or hh > 23 or (mm and int(mm) > 59):
            continue
        p = _at(day, hh, int(mm or 0))
        if off == 0 and p > now:                             # „um 22 Uhr“ am Vormittag gefragt: gestern
            p -= timedelta(days=1)
        lbl = ("gestern" if p.date() < now.date() and off == 0 else day_lbl) + f" {p:%H:%M}"
        out.append(Window(lbl, p - timedelta(minutes=POINT_MIN), min(now, p + timedelta(minutes=POINT_MIN)), p))
    if not out:
        for pat, lbl, (h1, m1), (h2, m2) in _PARTS:
            if re.search(pat, t):
                start, end = _at(day, 0, 0) + timedelta(hours=h1, minutes=m1), _at(day, h2, m2)
                if off == 0 and start > now:                 # „abends“ am Mittag gefragt: gestern Abend
                    start, end, lab = start - timedelta(days=1), end - timedelta(days=1), "gestern"
                else:
                    lab = day_lbl
                out.append(Window(f"{lab} {lbl} ({start:%H:%M}–{end:%H:%M})", start, min(now, end)))
                break
    if not out and re.search(r"\bvorhin\b", t):
        out.append(Window("vorhin (letzte 2 h)", now - timedelta(hours=2), now))
    if not out and (off != 0 or re.search(r"\bheute\b", t)):
        start = _at(day, 0, 0)
        out.append(Window(f"{day_lbl} (ganzer Tag)", start, min(now, start + timedelta(days=1))))
    return out[:MAX_WINDOWS]


# ── Darstellung ─────────────────────────────────────────────────────────────


def _w(v: Any) -> str:
    return "unbekannt" if v is None else f"{int(round(v))} W"


def _hm(ts: str) -> str:
    return ts[11:16]


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _c(v: Any) -> str:
    return "unbekannt" if v is None else f"{float(v):g}".replace(".", ",") + " °C"


def energy_line(snap: dict[str, Any]) -> str:
    e = snap.get("energy") or {}
    parts = [f"PV {_w(e.get('pv_w'))}"]
    g = e.get("grid_w")
    parts.append("Netz unbekannt" if g is None else f"Netzbezug {int(g)} W" if g > 0 else f"Einspeisung {int(-g)} W")
    parts.append(f"Hausverbrauch {_w(e.get('house_w'))}")
    parts.append(f"PV-Überschuss für die Kaskade {_w(e.get('surplus_w'))}")
    parts.append(f"Heizstab {_w(e.get('heater_w'))}")
    if "wallbox_w" in e:
        parts.append(f"Wallbox {_w(e.get('wallbox_w'))}")
    if "battery_soc" in e:
        parts.append(f"Batterie {e['battery_soc']} % (lädt {_w(e.get('battery_charge_w'))}, "
                     f"entlädt {_w(e.get('battery_discharge_w'))})")
    parts.append(f"Wasserspeicher {_c(e.get('storage_temp_c'))}")
    return ", ".join(parts)


def _name(d: dict[str, Any]) -> str:
    return DEVICE_NAMES.get(d["id"]) or d.get("name") or d["id"]


def _is_budget(d: dict[str, Any]) -> bool:
    """Kaskaden-Eintrag des Heizstabs: verteilt nur das Budget, geschaltet wird über die Phasen."""
    return d.get("type") == "heizstab" or d.get("id") == "heizstab"


def _action(d: dict[str, Any]) -> str:
    if _is_budget(d):
        return "Budget"
    return ACTION_DE.get(d.get("action"), d.get("action") or "–")


def running_line(snap: dict[str, Any]) -> str:
    """Was nach diesem Lauf tatsächlich läuft – ausdrücklich, damit das nicht aus den Grund-Zeilen erschlossen
    werden muss (z. B. Phase an trotz 0 W Überschuss wegen Mindestlaufzeit)."""
    devs = snap.get("devices") or []
    e = snap.get("energy") or {}
    parts: list[str] = []
    phases = [d for d in devs if str(d["id"]).startswith("heizstab_phase")]
    if phases:
        on = [d for d in phases if d.get("action") in _ON]
        meas = f"zu Beginn des Laufs gemessen {_w(e.get('heater_w'))}"
        if on:
            why = [f"Phase {d['id'][-1]} in diesem Lauf eingeschaltet" for d in on if d.get("action") == "turn_on"]
            for d in on:
                if d.get("reason") not in _ACTIVE_REASONS:
                    t = f"Phase {d['id'][-1]}: {decisions.reason_text(snap, d)}"
                    why.append(t)
            parts.append(f"Heizstab AN mit Phase {'+'.join(d['id'][-1] for d in on)} ({meas})"
                         + (f" – {'; '.join(why)}" if why else ""))
        elif any(d.get("action") == "unknown" for d in phases):
            parts.append(f"Heizstab: Zustand unbekannt ({meas})")
        else:
            parts.append(f"Heizstab AUS ({meas})")
    if snap.get("summer_mode_heating"):
        parts.append("Sommermodus heizt aus dem Netz")
    wb = None
    for d in devs:
        if d["id"] in ("zendure", "wallbox_control") or str(d["id"]).startswith("heizstab_phase") or _is_budget(d):
            continue
        if d.get("type") == "wallbox":
            wb = d
            continue
        a = d.get("action")
        parts.append(f"{_name(d)} " + ("AN" if a in _ON else "AUS (in diesem Lauf ausgeschaltet)" if a == "turn_off"
                                       else "aus" if a in ("keep_off", "skip_min_off") else "Zustand unbekannt"))
    wc = next((d for d in devs if d["id"] == "wallbox_control"), None)
    if wc is not None or wb is not None or "wallbox_w" in e:
        data = decisions.device_data(snap, wc) if wc is not None else {}
        car, w = data.get("car_state"), e.get("wallbox_w")
        charging = car == 2 or (w or 0) > 100
        s = f"Auto lädt ({_w(w)})" if charging else f"Auto lädt nicht (Wallbox {_w(w)})"
        if car in _CAR and car != 2:
            s += f" – {_CAR[car]}"
        if data.get("eco_mode") is False:
            s += " – Wallbox nicht im Eco-Modus, die PV-Steuerung fasst sie nicht an"
        if wb is not None:
            s += (f"; Überschuss-Freigabe der Kaskade für die Wallbox: {'an' if wb.get('action') in _ON else 'aus'} "
                  "(lädt nur mit angestecktem Auto im Eco-Modus)")
        parts.append(s)
    return "Läuft gerade (nach diesem Lauf): " + ("; ".join(parts) if parts else "unbekannt")


def device_line(snap: dict[str, Any], d: dict[str, Any]) -> str:
    extra = ""
    data = decisions.device_data(snap, d)
    if d["id"] == "wallbox_control" and data.get("car_state") in _CAR:
        extra = f" (Auto: {_CAR[data['car_state']]}, Eco-Modus {'an' if data.get('eco_mode') else 'aus'})"
    if d.get("error"):
        extra += " – Schaltbefehl fehlgeschlagen"        # Fehlertext bewusst nicht (kann Adressen enthalten)
    return (f"{_name(d)}: {_action(d)} [{d.get('reason') or '–'}] "
            f"{decisions.reason_text(snap, d)}{extra}")


def _merge_phases(lines: list[tuple[str, str]]) -> list[str]:
    """[(Gerät-ID, Zeile)] → Zeilen; Heizstab-Phasen mit identischem Text werden zu „Heizstab Phase 1–3“
    bzw. „Phase 1+3“ zusammengefasst (spart zwei Drittel der Phasen-Zeilen)."""
    out: list[str] = []
    groups: dict[str, list[int]] = {}
    slot: dict[str, int] = {}
    for dev, line in lines:
        m = re.match(r"heizstab_phase(\d)$", dev)
        if not m:
            out.append(line)
            continue
        rest = line.split(": ", 1)[1]
        if rest not in groups:
            groups[rest] = []
            slot[rest] = len(out)
            out.append("")
        groups[rest].append(int(m.group(1)))
    for rest, nums in groups.items():
        label = (f"Phase {nums[0]}" if len(nums) == 1 else
                 f"Phase {nums[0]}–{nums[-1]}" if nums == list(range(nums[0], nums[-1] + 1)) else
                 "Phase " + "+".join(map(str, nums)))
        out[slot[rest]] = f"Heizstab {label}: {rest}"
    return out


def snapshot_lines(snap: dict[str, Any]) -> list[str]:
    lines = [f"Stand {_hm(snap['ts'])} ({'Sommer' if snap.get('profile') == 'summer' else 'Winter'}profil"
             + (", Dry-Run: es wird nichts geschaltet" if snap.get("dry_run") else "") + "): " + energy_line(snap),
             running_line(snap)]
    fs = snap.get("failsafe")
    if fs:
        lines.append(f"Fail-safe aktiv [failsafe_global]: {', '.join(fs.get('names') or fs.get('unreadable') or [])} "
                     f"nicht lesbar – keine überschussbasierten Schaltbefehle")
    if snap.get("cascade"):
        lines.append(f"Kaskade [{snap['cascade']}]: {reasons.text(snap['cascade'])}")
    if snap.get("controller"):
        lines.append(f"Steuerung [{snap['controller']}]: {reasons.text(snap['controller']) or 'Fehler im Lauf'}")
    if snap.get("summer_mode_heating"):
        lines.append("Sommermodus heizt gerade aus dem Netz")
    lines += ["- " + x for x in _merge_phases([(d["id"], device_line(snap, d)) for d in snap.get("devices") or []])]
    for h in snap.get("health") or []:
        lines.append(f"- Nicht erreichbar: {h.get('name') or h['id']} seit {_stamp(h.get('offline_since'), snap['ts'])}"
                     + (" (erwartet, keine Sonne)" if h.get("expected") else ""))
    if snap.get("substitutes"):
        lines.append("- Ersatzwert: Heizstab-Leistung aus den Phasen geschätzt (Zähler nicht lesbar)")
    return lines


def _stamp(ts: Optional[str], ref: str) -> str:
    if not ts:
        return "unbekannt"
    return _hm(ts) if ts[:10] == ref[:10] else f"{ts[8:10]}.{ts[5:7]}. {_hm(ts)}"


def changes(snaps: list[dict[str, Any]], limit: int = MAX_CHANGES) -> list[str]:
    """Verdichtet: nur Wechsel von Aktion/Grund je Gerät (nicht jede Minute), dazu Fail-safe-Wechsel."""
    last: dict[str, tuple] = {}
    out: list[str] = []
    fs_prev: Any = "start"
    for i, s in enumerate(snaps):
        fs = tuple((s.get("failsafe") or {}).get("names") or [])
        if fs_prev != "start" and fs != fs_prev:
            out.append(f"{_hm(s['ts'])} Fail-safe " + (f"aktiv: {', '.join(fs)} nicht lesbar" if fs else "beendet"))
        fs_prev = fs
        changed: list[tuple[str, str]] = []
        for d in s.get("devices") or []:
            key = ("budget" if _is_budget(d) else d.get("action"), d.get("reason"))
            if last.get(d["id"]) != key:
                if i > 0:
                    changed.append((d["id"], device_line(s, d)))
                last[d["id"]] = key
        out += [f"{_hm(s['ts'])} {x}" for x in _merge_phases(changed)]
    if len(out) > limit:
        head, tail = limit // 3, limit - limit // 3
        out = out[:head] + [f"… {len(out) - limit} weitere Wechsel ausgelassen …"] + out[-tail:]
    return out


def samples(snaps: list[dict[str, Any]], max_lines: int = 7) -> list[str]:
    """Messwerte in gleichmäßigen Abständen (höchstens ``max_lines`` Zeilen)."""
    if not snaps:
        return []
    step = max(1, math.ceil(len(snaps) / max_lines))
    picked = snaps[::step]
    if picked[-1] is not snaps[-1]:
        picked.append(snaps[-1])
    return [f"{_hm(s['ts'])} {energy_line(s)}" for s in picked]


# ── Kontext ─────────────────────────────────────────────────────────────────


@dataclass
class Context:
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)

    def add(self, title: str, lines: list[str]) -> None:
        if lines:
            self.sections.append((title, lines))

    @property
    def text(self) -> str:
        body = "\n\n".join(f"## {t}\n" + "\n".join(lines) for t, lines in self.sections)
        return redact.scrub_addresses(redact.redact("ERKLÄR-KONTEXT (nur diese Fakten verwenden)\n\n" + body))

    @property
    def basis(self) -> list[dict[str, Any]]:
        """Für die UI („Grundlage“): dieselben Abschnitte, ebenfalls bereinigt."""
        clean = lambda s: redact.scrub_addresses(redact.redact(s))  # noqa: E731
        return [{"title": t, "lines": [clean(x) for x in lines]} for t, lines in self.sections]

    @property
    def tokens(self) -> int:
        return est_tokens(self.text)


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _settings_lines(config: Any) -> list[str]:
    h = config.heater
    n = h.enabled_phase_count
    season = "summer" if config.summer_mode.enabled else "winter"
    lines = [
        f"Heizstab: {n} Phase(n) je {int(h.phase_power_w)} W; erlaubter Netz-Zuschuss {int(h.grid_assist_w)} W; "
        "Startschwelle (Budget) " + ", ".join(f"{k} Phase(n) ab {int(k * h.phase_power_w - h.grid_assist_w)} W"
                                              for k in range(1, n + 1)),
        f"Heizstab: Mindestlaufzeit {int(h.phase_min_on_s)} s, Mindestpause {int(h.phase_min_off_s)} s je Phase; "
        f"pro Minute wird höchstens eine Phase zugeschaltet; Budget = Mittel über {h.budget_smooth_min} min "
        f"(Zuschalten) bzw. {h.budget_smooth_down_min} min (Abschalten); Abschalten erst "
        f"{int(h.phase_power_w * h.phase_down_hyst)} W unter der Schwelle",
        f"Wasserspeicher: Maximaltemperatur {_c(h.storage_max_temp)}, neuer Heizzyklus erst unter "
        f"{_c(h.storage_max_temp - h.storage_temp_hysteresis)}",
    ]
    disabled = [f"Phase {i + 1}" for i, x in enumerate(h.disabled_phases) if x]
    if disabled:
        lines.append("Stillgelegt: " + ", ".join(disabled))
    sm = config.summer_mode
    lines.append(f"Sommermodus: {'an' if sm.enabled else 'aus'} (heizt unter {_c(sm.min_temp)} aus dem Netz bis "
                 f"{_c(sm.target_temp)})")
    try:
        devs = db.get_cascade_devices()
        st = db.get_cascade_settings()
        by_id = {d["id"]: d for d in devs}
        comfort = config.comfort_min_for(season)
        names = {"heizstab_min": f"Wasserspeicher bis {_c(comfort)}", "heizstab_max": "Wasserspeicher bis Maximaltemperatur",
                 "zendure": "Batterie"}
        order = [names.get(k) or (by_id[k]["name"] if k in by_id else k)
                 for k in db.get_cascade_profile(season, comfort is not None)
                 if k in names or (k in by_id and by_id[k]["enabled"])]
        lines.append(f"Aktives Profil: {'Sommer' if season == 'summer' else 'Winter'}; Reihenfolge der Verteilung: "
                     + " → ".join(order))
        lines.append(f"Kaskade: {'an' if st['cascade_enabled'] else 'aus'}; Mindestüberschuss "
                     f"{int(st['min_surplus_watts'])} W; Netzbezug für laufende Geräte "
                     f"{'erlaubt' if st['allow_grid_draw'] else 'nicht erlaubt'}")
        for d in devs:
            if d["type"] in ("heizstab", "zendure"):
                continue
            lines.append(f"Kaskaden-Gerät {d['name']}: {int(d['power_watts'])} W, startet ab "
                         f"{int(d['power_watts'] + d['hysteresis_watts'])} W Rest-Überschuss, Mindestlaufzeit "
                         f"{d['min_on_minutes']} min, Mindestpause {d['min_off_minutes']} min"
                         + ("" if d["enabled"] else " (deaktiviert)"))
    except sqlite3.Error:
        lines.append("Kaskaden-Einstellungen: unbekannt (nicht lesbar)")
    zc = config.zendure
    if config.zendure_active:
        lines.append(f"Batterie: Regelung {'an' if zc.control_enabled else 'aus'}; Mindest-SoC {zc.min_soc:g} %, "
                     f"Ziel-SoC {zc.target_soc:g} %, Laden bis {int(zc.max_charge_w)} W, Entladen bis "
                     f"{int(zc.max_discharge_w)} W nur für die Grundlast; Entladen gesperrt, während die Wallbox lädt: "
                     f"{'ja' if zc.block_discharge_while_wallbox_charging else 'nein'}")
    else:
        lines.append("Batterie: nicht eingerichtet")
    wb = config.wallbox
    lines.append(f"Wallbox: Steuerung {'an' if wb.enabled else 'aus'}; PV-Laden nur im Eco-Modus der Wallbox "
                 f"({wb.eco_phases}-phasig, {wb.eco_amps} A); im normalen Modus lädt sie unabhängig von der PV und "
                 "wird nicht angefasst")
    rt = config.runtime
    lines.append(f"Steuerung: {'an' if rt.enabled else 'aus (nur Beobachten)'}"
                 + ("; Dry-Run: es wird nichts geschaltet" if rt.dry_run else ""))
    return lines


def _health_lines(now: datetime, since: datetime) -> list[str]:
    lines = []
    try:
        for o in decisions.offline_devices():
            lines.append(f"Jetzt nicht erreichbar: {o['name']} seit {_stamp(o['since'], now.isoformat())}"
                         + (" (erwartet, keine Sonne)" if o["expected"] else ""))
        ev = list(reversed(decisions.health_events(since, limit=30)))
        for e in ev:
            lines.append(f"{_stamp(e['ts'], now.isoformat())} {e['name']}: "
                         + ("nicht mehr erreichbar" if e["event"] == decisions.OFFLINE else "wieder erreichbar")
                         + (" (erwartet, keine Sonne)" if e["expected"] else "")
                         + (f" – {e['detail']}" if e["detail"] and not e["expected"] else ""))
    except sqlite3.Error:
        return ["Geräte-Ereignisse: unbekannt (nicht lesbar)"]
    return lines or [f"Alle Geräte erreichbar; keine Ausfälle seit {_stamp(since.isoformat(), now.isoformat())}"]


def _mentioned(text: str, items: list[Any], generic: re.Pattern, prefix: bool = True) -> list[Any]:
    """In der Frage genannte Räume/Rollläden: voller Name oder ID; sonst (``prefix``) das erste Wort des Namens
    („Wohnen“ → Wohnen links und Wohnen rechts); sonst alle, wenn nur allgemein danach gefragt ist."""
    t = text.lower()
    hit = [x for x in items if x.name.lower() in t or re.search(rf"\b{re.escape(x.id.lower())}\b", t)]
    if not hit and prefix:
        hit = [x for x in items if len(x.name.split()[0]) >= 3
               and re.search(rf"\b{re.escape(x.name.split()[0].lower())}", t)]
    return hit or (list(items) if generic.search(t) else [])


def _result_lines(kind: str, ids: set[str], names: dict[str, str], since: datetime, now: datetime) -> list[str]:
    try:
        rows = [r for r in schedule_log.results(kind, since=since, limit=300) if r["target_id"] in ids]
    except sqlite3.Error:
        return ["Zeitplan-Ergebnisse: unbekannt (nicht lesbar)"]
    out = []
    for r in reversed(rows[:40]):
        when = r["data"].get("when")
        tgt = r["data"].get("target") or (_c(r["data"]["setpoint"]) if r["data"].get("setpoint") is not None else "")
        out.append(f"{_stamp(r['ts'], now.isoformat())} {names.get(r['target_id'], r['target_id'])}"
                   + (f" (Zeitplan {when} → {tgt})" if when else "")
                   + f": [{r['code']}] {reasons.text(r['code'], r['data'])}"
                   + (f" – Rollladen danach bei {r['data']['target_pct']} % zu" if r["data"].get("target_pct") is not None
                      and r["code"] == "sent" else "")
                   + (f"; {reasons.text('limited_door_gap', {'limited_to': r['data'].get('limited_to'), 'target': r['data'].get('limited_from')})}"
                      if r["data"].get("limit") == "limited_door_gap" else ""))
    return out


def _bucket_min(span_min: float) -> int:
    for b in (5, 10, 15, 20, 30, 60, 120):
        if span_min / b <= ROOM_HISTORY_POINTS:
            return b
    return 240


def _tc(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def _fetch(history: Optional[HistoryFn], item: str, start: datetime, end: datetime) -> tuple[list, str]:
    """→ (Punkte als [(datetime, Wert)], Grund wenn leer). Fehlertexte bewusst fest (keine Adressen)."""
    if history is None:
        return [], "unbekannt (openHAB nicht verbunden)"
    try:
        pts = history(item, start, end)
    except Exception:                                    # noqa: BLE001 – Verlauf ist nur Zusatz
        return [], "unbekannt (Persistenz nicht lesbar)"
    out = sorted((datetime.fromtimestamp(ms / 1000, TZ), v) for ms, v in pts or [])
    return out, "" if out else "unbekannt (keine gespeicherten Werte)"


def _buckets(pts: list[tuple[datetime, float]], b: int) -> list[tuple[datetime, float]]:
    groups: dict[datetime, list[float]] = {}
    for t, v in pts:
        m = (t.hour * 60 + t.minute) // b * b
        groups.setdefault(t.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0), []).append(v)
    return [(k, sum(g) / len(g)) for k, g in sorted(groups.items())]


def _pct(v: float) -> str:
    return f"{int(round(v))}"


def room_history_lines(room: Any, windows: list[Window], history: Optional[HistoryFn]) -> list[str]:
    """Je Zeitfenster: Soll-Wechsel, Ist-Verlauf (Mittelwerte je Zeitblock), Ventil-Verlauf und – wenn der Sollwert
    im Fenster wechselt – Ist/Ventil vor bzw. nach jedem Wechsel (damit „war es schon vorher warm?“ ablesbar ist)."""
    lines: list[str] = []
    for w in windows:
        span = (w.end - w.start).total_seconds() / 60
        if span <= 0:
            continue
        b = _bucket_min(span)
        head = f"  Verlauf {w.label}"
        # Soll zuerst: die Wechselzeiten teilen den Ist-/Ventil-Verlauf in „vor/nach Wechsel“
        changes_: list[tuple[datetime, float, float]] = []          # (Zeit, Soll vorher, Soll danach)
        sp_line = None
        if room.setpoint_item:
            pts, why = _fetch(history, room.setpoint_item, w.start - timedelta(hours=SETPOINT_LOOKBACK_H), w.end)
            if not pts:
                sp_line = f"{head} – Soll: {why}"
            else:
                before = [v for t, v in pts if t <= w.start]
                last = round(before[-1], 1) if before else None
                seq = [f"{w.start:%H:%M} {_tc(last)}"] if last is not None else []
                for t, v in pts:
                    if w.start < t <= w.end and round(v, 1) != last:
                        if last is not None:
                            changes_.append((t, last, round(v, 1)))
                        last = round(v, 1)
                        seq.append(f"{t:%H:%M} {_tc(last)}")
                sp_line = f"{head} – Soll (Wechsel, °C): " + (", ".join(seq) if seq else "unbekannt")
        if sp_line:
            lines.append(sp_line)
        temp: list[tuple[datetime, float]] = []
        if not room.temp_item:
            lines.append(f"{head} – Ist: unbekannt (kein Temperatur-Item)")
        else:
            pts, why = _fetch(history, room.temp_item, w.start, w.end)
            temp = [(t, v) for t, v in pts if w.start <= t < w.end]
            if not temp:
                lines.append(f"{head} – Ist: {why or 'unbekannt (keine gespeicherten Werte)'}")
            else:
                vals = [v for _, v in temp]
                lines.append(f"{head} – Ist (Mittel je {b} min, °C): "
                             + ", ".join(f"{k:%H:%M} {_tc(v)}" for k, v in _buckets(temp, b))
                             + f" (min {_tc(min(vals))}, max {_tc(max(vals))})")
        valve: list[tuple[datetime, float]] = []
        if room.valve_item:
            pts, why = _fetch(history, room.valve_item, w.start, w.end)
            valve = [(t, v) for t, v in pts if w.start <= t < w.end]
            if not valve:
                lines.append(f"{head} – Ventil: {why or 'unbekannt (keine gespeicherten Werte)'}")
            else:
                lines.append(f"{head} – Ventil (Mittel je {b} min, %): "
                             + ", ".join(f"{k:%H:%M} {_pct(v)}" for k, v in _buckets(valve, b))
                             + f" (max {_pct(max(v for _, v in valve))})")
        if changes_ and (temp or valve):
            bounds = [w.start] + [t for t, _, _ in changes_] + [w.end]
            sps = [changes_[0][1]] + [new for _, _, new in changes_]
            for i in range(len(bounds) - 1):
                a, z = bounds[i], bounds[i + 1]
                label = f"vor Wechsel {z:%H:%M}" if i == 0 else f"nach Wechsel {a:%H:%M}"
                seg = [v for t, v in temp if a <= t < z]
                vseg = [v for t, v in valve if a <= t < z]
                part = (f"Ist {_tc(seg[0])} → {_tc(seg[-1])} (min {_tc(min(seg))}, max {_tc(max(seg))})"
                        if seg else "Ist unbekannt")
                if room.valve_item:
                    part += f", Ventil max {_pct(max(vseg))} %" if vseg else ", Ventil unbekannt"
                lines.append(f"{head} – {label} ({a:%H:%M}–{z:%H:%M}, Soll {_tc(sps[i])} °C): {part}")
    return lines


def _heating_lines(text: str, hcfg: Any, rooms: list[dict], since: datetime, now: datetime,
                   windows: Optional[list[Window]] = None, history: Optional[HistoryFn] = None) -> list[str]:
    import heating_schedule as hs
    picked = _mentioned(text, [r for r in hcfg.rooms], _HEATING_WORDS)
    named = {r.id for r in _mentioned(text, [r for r in hcfg.rooms], re.compile(r"(?!x)x"))}
    if not picked:
        return []
    by_id = {r["id"]: r for r in rooms}
    lines = [f"Schalter „Heizungs-Zeitpläne aktiv“: {'an' if hcfg.schedules_enabled else 'AUS – Zeitpläne werden nicht ausgeführt'}"]
    try:
        entries, alerts = hs.list_schedules(), hs.alerts()
    except sqlite3.Error:
        entries, alerts = [], {}
    for room in picked:
        st = by_id.get(room.id) or {}
        lines.append(f"{room.name}: Ist {_c(st.get('temp'))}, Soll {_c(st.get('setpoint'))}"
                     + (f", Ventil {st['valve']:g} %" if st.get("valve") is not None else "")
                     + (f", Modus {st['mode']}" if st.get("mode") else "")
                     + ("" if room.controllable else " (nur Anzeige, nicht steuerbar)")
                     + (" – Status nicht lesbar" if st.get("error") or not st else ""))
        if room.id in alerts:
            lines.append(f"  Rückprüfung: {alerts[room.id]['message']}")
        if windows and room.id in named and len([r for r in picked if r.id in named]) <= ROOM_HISTORY_ROOMS:
            lines += room_history_lines(room, windows, history)
        elif windows:
            lines.append(f"  Temperatur-Verlauf {windows[0].label}: unbekannt (nur für ausdrücklich genannte Räume)")
        for e in [e for e in entries if e["room_id"] == room.id][:12]:
            lines.append(f"  Zeitplan #{e['id']}: {hs.when(e)} → {_c(e['setpoint'])}, "
                         + (f"einmalig {e['date']}" if e["date"] else ",".join(e["days"]))
                         + ("" if e["enabled"] else " (ausgeschaltet)")
                         + (f"; zuletzt {_stamp(e['last_run'], now.isoformat())}: {e['last_result']}"
                            if e.get("last_run") else "; noch nie gelaufen"))
    res = _result_lines(schedule_log.HEATING, {r.id for r in picked}, {r.id: r.name for r in picked}, since, now)
    lines += ["Zeitplan-Ergebnisse:"] + ["  " + x for x in res] if res else \
        [f"Zeitplan-Ergebnisse seit {_stamp(since.isoformat(), now.isoformat())}: keine Einträge"]
    return lines


def _shutter_lines(text: str, scfg: Any, sh_status: list[dict], since: datetime, now: datetime,
                   room_named: bool = False) -> list[str]:
    import shutter_schedule as ss
    import shutters
    # „Warum ist Wohnen kalt?“ meint den Raum, nicht „Wohnen links“: Teilnamen zählen bei Rollläden nur,
    # wenn von Rollläden die Rede ist oder kein Heizungsraum so heißt
    picked = _mentioned(text, list(scfg.rooms), _SHUTTER_WORDS,
                        prefix=bool(_SHUTTER_WORDS.search(text)) or not room_named)
    if not picked:
        return []
    by_id = {r["id"]: r for r in sh_status}
    lines = [f"Schalter „Rollladen-Zeitpläne aktiv“: {'an' if scfg.schedules_enabled else 'AUS – Zeitpläne werden nicht ausgeführt'}",
             f"Regeln: Position in % ZU (0 = offen, 100 = zu); Öffnen gesperrt unter {_c(scfg.frost_temp_c)} "
             f"Außentemperatur; nach Bedienung von Hand {scfg.manual_hold_min} min Vorrang vor Zeitplänen; "
             f"Zeitpläne pausieren über {scfg.duty_cycle_max:g} % Funk-Duty-Cycle; fällige Zeitpläne werden höchstens "
             f"{ss.CATCH_UP_MIN} min nachgeholt"]
    try:
        entries, cmds = ss.list_schedules(), shutters.last_commands()
    except sqlite3.Error:
        entries, cmds = [], {}
    for sh in picked:
        st = by_id.get(sh.id) or {}
        pos = st.get("position")
        door = ""
        if sh.is_door:
            door = (f"; Tür mit Kontakt ({ {'open': 'offen', 'closed': 'zu'}.get(st.get('contact'), 'unbekannt') }) – "
                    "schließt automatisch nur bei geschlossener Tür" if sh.contact_item else
                    f"; Tür ohne Kontakt – schließt automatisch nur bis {sh.auto_close_max} % (Aussperrschutz), "
                    "ganz zu nur von Hand")
        lines.append(f"{sh.name}: Position {'unbekannt' if pos is None else f'{pos} % zu'}"
                     + ("" if st.get("position_reliable", True) else " (unsicher)")
                     + (f", fährt gerade {'ab' if st['moving'] == 'down' else 'auf'}" if st.get("moving") else "")
                     + f"; Automatik: {'PV Controller (Zeitpläne hier)' if sh.automation == 'pvcontroller' else 'openHAB (Zeitpläne hier werden nicht ausgeführt)'}"
                     + door + (f"; Meldung: {st['alert']}" if st.get("alert") else ""))
        c = cmds.get(sh.id)
        if c:
            lines.append(f"  Letzter Befehl: {_stamp(c['sent_at'], now.isoformat())} "
                         + (f"→ {c['target']} % zu" if c.get("target") is not None else c["command"])
                         + f" ({c['source']}), Rückprüfung: "
                         + {"ok": f"ok (gemeldet {c.get('reported')} %)", "pending": "läuft noch", "none": "keine",
                            "mismatch": c.get("message") or "Abweichung"}.get(c["status"], c["status"]))
        for e in [e for e in entries if e["shutter_id"] == sh.id][:12]:
            lines.append(f"  Zeitplan #{e['id']}: {ss.describe(e)} → {ss.target_label(e['action'], e['position'])}, "
                         + (f"einmalig {e['date']}" if e["date"] else ",".join(e["days"] or []))
                         + ("" if e["enabled"] else " (ausgeschaltet)")
                         + (f"; zuletzt {_stamp(e['last_run'], now.isoformat())}: {e['last_result']}"
                            + (f" ({e['last_message']})" if e.get("last_message") else "")
                            if e.get("last_run") else "; noch nie gelaufen"))
    res = _result_lines(schedule_log.SHUTTER, {s.id for s in picked}, {s.id: s.name for s in picked}, since, now)
    lines += ["Zeitplan-Ergebnisse:"] + ["  " + x for x in res] if res else \
        [f"Zeitplan-Ergebnisse seit {_stamp(since.isoformat(), now.isoformat())}: keine Einträge"]
    return lines


def _available(now: datetime) -> str:
    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            lo, hi, n = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM decision_snapshot").fetchone()
    except sqlite3.Error:
        return "keine Schnappschüsse vorhanden"
    if not n:
        return "keine Schnappschüsse vorhanden"
    return f"Schnappschüsse vorhanden von {_stamp(lo, now.isoformat())} bis {_stamp(hi, now.isoformat())}"


def build(text: str, now: datetime, config: Any, hcfg: Any = None, scfg: Any = None,
          rooms: Optional[list[dict]] = None, sh_status: Optional[list[dict]] = None,
          history: Optional[HistoryFn] = None) -> Context:
    """Erklär-Kontext zur Frage ``text``. Liest nur; fehlende Daten stehen als „unbekannt“ im Text."""
    now = now.astimezone(TZ)
    ctx = Context(windows=time_windows(text, now))
    try:
        cur = decisions.latest()
    except sqlite3.Error:
        cur = None
    if cur is None:
        ctx.add("Aktueller Stand", ["unbekannt – es gibt noch keinen Entscheidungs-Schnappschuss der Steuerung"])
    else:
        age = (now - _dt(cur["ts"])).total_seconds() / 60
        head = [f"Jetzt ist es {now:%H:%M} am {now:%d.%m.%Y}."]
        if age > STALE_MIN:
            head.append(f"ACHTUNG: Der letzte Lauf der Steuerung ist {int(age)} min alt ({_stamp(cur['ts'], now.isoformat())}) "
                        "– aktuelle Werte sind unbekannt, die Steuerung läuft evtl. nicht.")
        ctx.add("Aktueller Stand (letzter Lauf der Steuerung)", head + snapshot_lines(cur))
        try:
            recent = decisions.snapshots(now - timedelta(minutes=RECENT_MIN), now)
        except sqlite3.Error:
            recent = []
        if len(recent) > 1:
            ch = changes(recent)
            ctx.add(f"Letzte {RECENT_MIN} min – Wechsel ({_hm(recent[0]['ts'])}–{_hm(recent[-1]['ts'])}, "
                    f"{len(recent)} Läufe)", ch or ["keine Wechsel – alle Geräte unverändert wie im aktuellen Stand"])
            ctx.add(f"Letzte {RECENT_MIN} min – Messwerte", samples(recent))

    earliest = now - timedelta(hours=24)
    for w in ctx.windows:
        earliest = min(earliest, w.start)
        try:
            snaps = decisions.snapshots(w.start, w.end)
        except sqlite3.Error:
            snaps = []
        if not snaps:
            ctx.add(f"Zeitraum {w.label}", [f"unbekannt – für diesen Zeitraum liegen keine Schnappschüsse vor "
                                            f"({_available(now)}; aufbewahrt werden 48 h)"])
            continue
        if w.point is not None:
            near = min(snaps, key=lambda s: abs((_dt(s["ts"]) - w.point).total_seconds()))
            ctx.add(f"Zeitpunkt {w.label} – nächster Lauf", snapshot_lines(near))
            ctx.add(f"Zeitpunkt {w.label} – Wechsel {_hm(snaps[0]['ts'])}–{_hm(snaps[-1]['ts'])}",
                    changes(snaps) or ["keine Wechsel in diesem Fenster"])
            ctx.add(f"Zeitpunkt {w.label} – Messwerte", samples(snaps))
        else:
            ctx.add(f"Zeitraum {w.label} – Beginn", snapshot_lines(snaps[0]))
            ctx.add(f"Zeitraum {w.label} – Wechsel ({len(snaps)} Läufe)",
                    changes(snaps) or ["keine Wechsel in diesem Zeitraum"])
            ctx.add(f"Zeitraum {w.label} – Messwerte", samples(snaps, 10))

    since = min(earliest, now.replace(hour=0, minute=0, second=0, microsecond=0))
    heat_on = hcfg is not None and getattr(hcfg, "enabled", False)
    room_named = heat_on and any(r.name.lower() in text.lower() for r in hcfg.rooms)
    if scfg is not None and getattr(scfg, "enabled", False):
        ctx.add("Rollläden", _shutter_lines(text, scfg, sh_status or [], since, now, room_named))
    if heat_on:
        ctx.add("Heizung (Räume)", _heating_lines(text, hcfg, rooms or [], since, now, ctx.windows, history))
    ctx.add("Geräte-Erreichbarkeit", _health_lines(now, since))
    try:
        ctx.add("Einstellungen (Zahlen)", _settings_lines(config))
    except Exception:                                    # noqa: BLE001 – Kontext nie am Config-Detail scheitern lassen
        ctx.add("Einstellungen (Zahlen)", ["unbekannt (nicht lesbar)"])
    _fit(ctx)
    return ctx


def _fit(ctx: Context) -> None:
    """Größe begrenzen: zuerst die langen Wechsel-/Ergebnislisten kürzen (Mitte auslassen)."""
    for _ in range(40):
        if len(ctx.text) <= MAX_CHARS:
            return
        idx = max(range(len(ctx.sections)), key=lambda i: len(ctx.sections[i][1]))
        title, lines = ctx.sections[idx]
        if len(lines) <= 8:
            return
        keep = max(8, len(lines) * 2 // 3)
        head = keep // 3
        ctx.sections[idx] = (title, lines[:head] + [f"… {len(lines) - keep} Zeilen ausgelassen …"]
                             + lines[-(keep - head):])
