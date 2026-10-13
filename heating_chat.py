"""Heizung Phase 3: Chat → Zeitplan-Vorschlag über die Claude API (nur Backend).

Ablauf: Text + Kontext (Datum/Uhrzeit Europe/Berlin, Räume mit aktuellen Werten, Grenzen, Zeitpläne der
nächsten 7 Tage) → Messages API mit genau einem Tool ``propose_schedule`` (strict) → der Server prüft alles
(Raum, Grenzen, Uhrzeit, Datum nicht in der Vergangenheit, Lösch-IDs) → Vorschlag mit ID zurück an die UI →
erst „Übernehmen“ speichert/schaltet. Ungültiges → Fehlermeldung, nichts wird gespeichert.

Config (config.json):
  "assistant": {"enabled": false,              # Settings-Schalter „Chat aktiv“ (Default aus)
                "api_key": "…",                 # nur in config.json, nie ins Git/Frontend/Log
                "model": "claude-haiku-5-5",
                "max_requests_per_day": 100}

Abfragen wie „was ist für morgen geplant?“ beantwortet ``local_answer`` aus der DB, ohne API.
Das SDK (``anthropic``) wird erst beim Aufruf importiert: ohne Paket ist nur der Chat aus.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import date as Date, datetime, timedelta
from typing import Any, Callable, Optional

import db
import heating
import heating_schedule as hs

log = logging.getLogger("pv-controller.heating")

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_LIMIT = 100
MAX_TOKENS = 8000
API_TIMEOUT_S = 20.0                  # harte Obergrenze je Chat-Anfrage (kein Retry, s. _default_client)
API_CONNECT_TIMEOUT_S = 5.0
MAX_HISTORY = 12                      # Nachrichten (Rückfragen + Antworten)
MAX_TEXT = 500                        # Zeichen je Nachricht
PROPOSAL_TTL_S = 15 * 60
HORIZON_DAYS = 7
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,63}$")
WD = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WD_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


class ChatError(Exception):
    """Fehler mit Text für den Chat (ohne Secrets). ``kind`` steuert die Darstellung."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


# ── Config / Limit ──────────────────────────────────────────────────────────


@dataclass
class AssistantConfig:
    enabled: bool = False
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_requests_per_day: int = DEFAULT_LIMIT

    @property
    def key_set(self) -> bool:
        return bool(self.api_key)


def parse_assistant(raw: dict) -> AssistantConfig:
    a = raw.get("assistant") or {}
    if not isinstance(a, dict):
        raise ValueError("assistant must be an object")
    enabled = a.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("assistant.enabled must be true/false")
    key = a.get("api_key") or ""
    if not isinstance(key, str):
        raise ValueError("assistant.api_key must be a string")
    model = a.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise ValueError("assistant.model must look like 'claude-haiku-5-5'")
    limit = a.get("max_requests_per_day", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("assistant.max_requests_per_day must be 1…10000")
    return AssistantConfig(enabled=enabled, api_key=key.strip(), model=model, max_requests_per_day=limit)


def _usage_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS assistant_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL)")
    return conn


def used_today(now: Optional[datetime] = None) -> int:
    day = (now or datetime.now(hs.TZ)).astimezone(hs.TZ).date().isoformat()
    with _usage_conn() as conn:
        row = conn.execute("SELECT count FROM assistant_usage WHERE day = ?", (day,)).fetchone()
    return int(row[0]) if row else 0


def try_consume(limit: int, now: Optional[datetime] = None) -> bool:
    """Zähler +1, wenn das Tageslimit noch nicht erreicht ist (atomar)."""
    day = (now or datetime.now(hs.TZ)).astimezone(hs.TZ).date().isoformat()
    with _usage_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO assistant_usage (day, count) VALUES (?, 0)", (day,))
        n = conn.execute("UPDATE assistant_usage SET count = count + 1 WHERE day = ? AND count < ?",
                         (day, limit)).rowcount
    return n == 1


def sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def status(hcfg: heating.HeatingConfig, acfg: AssistantConfig, now: Optional[datetime] = None) -> dict[str, Any]:
    used = used_today(now)
    hint = None
    if not acfg.enabled:
        hint = "Chat ist aus. Einschalten unter Settings → Heizung → „Chat aktiv“."
    elif not hcfg.enabled:
        hint = "Heizung ist nicht aktiv."
    elif not acfg.key_set:
        hint = "API-Key fehlt: assistant.api_key in config.json eintragen."
    elif not sdk_available():
        hint = "Python-Paket „anthropic“ fehlt (pip install -r requirements.txt)."
    elif used >= acfg.max_requests_per_day:
        hint = f"Tageslimit erreicht ({used}/{acfg.max_requests_per_day}). Morgen geht es weiter."
    return {"enabled": acfg.enabled, "key_set": acfg.key_set, "model": acfg.model,
            "used_today": used, "limit": acfg.max_requests_per_day,
            "ready": hint is None, "hint": hint}


# ── Kontext ─────────────────────────────────────────────────────────────────


def _fmt_c(v: Optional[float]) -> str:
    return "–" if v is None else f"{v:g}".replace(".", ",") + " °C"


def _day_label(d: Date, today: Date) -> str:
    base = f"{WD[d.weekday()]} {d:%d.%m.}"
    if d == today:
        return f"Heute ({base})"
    if d == today + timedelta(days=1):
        return f"Morgen ({base})"
    if d == today + timedelta(days=2):
        return f"Übermorgen ({base})"
    return base


def _days_label(days: list[str]) -> str:
    if days == list(hs.DAYS):
        return "Täglich"
    if days == list(hs.DAYS[:5]):
        return "Jeden Mo–Fr"
    if days == ["sa", "so"]:
        return "Jedes Wochenende"
    return "Jeden " + ", ".join(WD[hs.DAYS.index(d)] for d in days)


def upcoming(entries: list[dict[str, Any]], now: datetime, days: int = HORIZON_DAYS) -> list[dict[str, Any]]:
    """Schaltpunkte ab jetzt für ``days`` Tage: [{date, time, room_id, setpoint, id, kind}]."""
    now = now.astimezone(hs.TZ)
    out = []
    for off in range(days + 1):
        d = now.date() + timedelta(days=off)
        for e in entries:
            if not e["enabled"] or not hs.applies_on(e, d):
                continue
            if hs.due_instant(d, e["time"]) <= now:
                continue
            out.append({"date": d, "time": e["time"], "room_id": e["room_id"], "setpoint": e["setpoint"],
                        "id": e["id"], "kind": "einmalig" if e["date"] else "wiederkehrend"})
    out.sort(key=lambda x: (x["date"], x["time"], x["room_id"]))
    return out


def build_context(hcfg: heating.HeatingConfig, rooms: list[dict[str, Any]], entries: list[dict[str, Any]],
                  now: datetime) -> str:
    now = now.astimezone(hs.TZ)
    today = now.date()
    lines = [f"Jetzt: {WD_LONG[today.weekday()]}, {today:%d.%m.%Y} {now:%H:%M} (Europe/Berlin)",
             "Kalender: " + "; ".join(
                 f"{WD[(today + timedelta(days=i)).weekday()]} = {(today + timedelta(days=i)).isoformat()}"
                 + (" (heute)" if i == 0 else " (morgen)" if i == 1 else "")
                 for i in range(8)),
             f"Grenzen: Sollwert {_fmt_c(hcfg.setpoint_min)} bis {_fmt_c(hcfg.setpoint_max)}, "
             f"in {heating.SETPOINT_STEP:g}-°C-Schritten",
             "", "Räume (room_id: Name – aktuelle Werte):"]
    by_id = {r["id"]: r for r in rooms}
    for room in hcfg.rooms:
        st = by_id.get(room.id, {})
        if not room.controllable:
            lines.append(f"- {room.id}: {room.name} – nur Anzeige, nicht steuerbar")
            continue
        lines.append(f"- {room.id}: {room.name} – Ist {_fmt_c(st.get('temp'))}, Soll {_fmt_c(st.get('setpoint'))}")
    lines += ["", "Bestehende Zeitpläne (id, Raum, Uhrzeit, Soll, wann, aktiv):"]
    if not entries:
        lines.append("- keine")
    for e in sorted(entries, key=lambda x: (x["room_id"], x["time"])):
        when = f"einmalig {e['date']}" if e["date"] else "wiederkehrend " + ",".join(e["days"])
        lines.append(f"- #{e['id']} {e['room_id']} {e['time']} {_fmt_c(e['setpoint'])} {when}"
                     f"{'' if e['enabled'] else ' (aus)'}")
    lines += ["", f"Schaltpunkte der nächsten {HORIZON_DAYS} Tage:"]
    up = upcoming(entries, now)
    if not up:
        lines.append("- keine")
    for u in up:
        lines.append(f"- {u['date'].isoformat()} {WD[u['date'].weekday()]} {u['time']} {u['room_id']} → "
                     f"{_fmt_c(u['setpoint'])} (#{u['id']}, {u['kind']})")
    return "\n".join(lines)


SYSTEM_PROMPT = """Du setzt Wünsche zur Heizungssteuerung eines Wohnhauses in Zeitpläne um.
Antworte immer mit genau einem Aufruf des Tools propose_schedule und schreibe keinen anderen Text.

Regeln für propose_schedule:
- entries: neue Schaltpunkte. Einmalig: date (YYYY-MM-DD) setzen und days null lassen. Wiederkehrend: days
  setzen (mo, di, mi, do, fr, sa, so; „werktags“ = mo bis fr, „am Wochenende“ = sa, so) und date null lassen.
- Uhrzeiten als HH:MM im 24-Stunden-Format. „um 7“ ohne weitere Angabe heißt 07:00, „um 7 abends“ heißt 19:00.
- Wochentage wie „Freitag“ meinen den nächsten solchen Tag laut Kalender.
- immediate: Änderungen, die sofort gelten sollen („jetzt“, „sofort“).
- delete_ids: IDs bestehender Zeitpläne aus der Liste, die der Nutzer löschen will. Meint der Nutzer nur einen
  Tag eines wiederkehrenden Plans, lösche ihn nicht, sondern stelle eine Rückfrage.
- Nur room_id aus der Raumliste und nur steuerbare Räume. Sollwerte innerhalb der Grenzen.
  Keine Zeitpunkte in der Vergangenheit.
- question: eine kurze Rückfrage auf Deutsch, wenn Raum, Zeit, Tag oder Sollwert nicht eindeutig sind
  (z. B. zwei Räume passen). Dann entries, immediate und delete_ids leer lassen. Sonst null.
- summary: ein kurzer deutscher Satz, was geplant wird, oder null."""

_DAY_ENUM = list(hs.DAYS)
TOOL = {
    "name": "propose_schedule",
    "description": "Schlägt neue Heizungs-Zeitpläne, sofortige Sollwerte und zu löschende Zeitpläne vor, "
                   "oder stellt eine Rückfrage. Der Nutzer bestätigt den Vorschlag, bevor etwas gespeichert wird.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["entries", "immediate", "delete_ids", "question", "summary"],
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["room_id", "setpoint", "time", "date", "days"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "setpoint": {"type": "number"},
                        "time": {"type": "string", "description": "HH:MM, 24 h, Europe/Berlin"},
                        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD bei einmalig, sonst null"},
                        "days": {"type": ["array", "null"], "items": {"type": "string", "enum": _DAY_ENUM},
                                 "description": "Wochentage bei wiederkehrend, sonst null"},
                    },
                },
            },
            "immediate": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["room_id", "setpoint"],
                    "properties": {"room_id": {"type": "string"}, "setpoint": {"type": "number"}},
                },
            },
            "delete_ids": {"type": "array", "items": {"type": "integer"}},
            "question": {"type": ["string", "null"]},
            "summary": {"type": ["string", "null"]},
        },
    },
}


# ── API-Aufruf ──────────────────────────────────────────────────────────────


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _default_client(acfg: AssistantConfig) -> Any:
    try:
        import anthropic
    except ImportError:
        raise ChatError("Python-Paket „anthropic“ fehlt – Chat nicht verfügbar.", "unavailable")
    # Kein automatischer Retry: sonst bis zu (1 + max_retries) × Timeout Wartezeit im Chat.
    # Der Endpunkt ist synchron (FastAPI-Threadpool) – ein hängender Aufruf blockiert keine anderen Requests.
    return anthropic.Anthropic(api_key=acfg.api_key, max_retries=0,
                               timeout=anthropic.Timeout(API_TIMEOUT_S, connect=API_CONNECT_TIMEOUT_S))


def _translate(e: Exception, acfg: AssistantConfig) -> ChatError:
    """SDK-Fehler → ChatError mit verständlichem Text (typisierte Klassen, spezifischste zuerst)."""
    import anthropic
    _log_api_error(e, acfg)
    if isinstance(e, anthropic.NotFoundError):
        return ChatError(f"Modell „{acfg.model}“ nicht verfügbar – in Settings ändern.", "model")
    if isinstance(e, anthropic.AuthenticationError):
        return ChatError("API-Key ungültig – assistant.api_key in config.json prüfen.", "auth")
    if isinstance(e, anthropic.PermissionDeniedError):
        return ChatError("API-Key hat keine Berechtigung für dieses Modell.", "auth")
    if isinstance(e, anthropic.RateLimitError):
        return ChatError("Die API ist gerade ausgelastet (Rate-Limit). Bitte gleich noch einmal.", "busy")
    if isinstance(e, anthropic.BadRequestError):
        msg = _scrub(getattr(e, "message", "") or "", acfg.api_key)
        if "model" in msg.lower():
            return ChatError(f"Modell „{acfg.model}“ nicht verfügbar – in Settings ändern.", "model")
        return ChatError("Die API hat die Anfrage abgelehnt (400).")
    if isinstance(e, anthropic.APIStatusError):
        return ChatError(f"API-Fehler ({e.status_code}). Bitte später noch einmal.", "busy")
    if isinstance(e, anthropic.APITimeoutError):
        return ChatError(f"Die API antwortet nicht (Zeitüberschreitung nach {API_TIMEOUT_S:g} s). "
                         "Bitte später noch einmal.", "timeout")
    if isinstance(e, anthropic.APIConnectionError):
        return ChatError("Keine Verbindung zu api.anthropic.com (Internet?).", "offline")
    return ChatError("Unerwarteter Fehler beim Chat.")


def call_model(acfg: AssistantConfig, context: str, history: list[dict[str, str]],
               client_factory: Optional[Callable[[AssistantConfig], Any]] = None) -> dict[str, Any]:
    """Ein Aufruf der Messages API → Eingabe des Tools ``propose_schedule`` (dict).

    Fehler werden zu ChatError mit verständlichem Text; der API-Key erscheint nie (auch nicht im Log).
    """
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    client = (client_factory or _default_client)(acfg)
    try:
        resp = client.messages.create(
            model=acfg.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT + "\n\n" + context,
            tools=[TOOL],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=messages,
        )
    except ChatError:
        raise
    except Exception as e:  # noqa: BLE001 – SDK-Fehler typisiert in _translate
        try:
            raise _translate(e, acfg) from None
        except ImportError:
            log.warning("Chat: Fehler %s", type(e).__name__)
            raise ChatError("Unerwarteter Fehler beim Chat.") from None

    if resp.stop_reason == "refusal":
        raise ChatError("Die Anfrage wurde abgelehnt. Bitte anders formulieren.")
    for block in resp.content:
        if block.type == "tool_use" and block.name == "propose_schedule":
            return dict(block.input)
    text = " ".join(b.text for b in resp.content if b.type == "text").strip()
    if resp.stop_reason == "max_tokens":
        raise ChatError("Antwort unvollständig. Bitte kürzer formulieren.")
    # Kein Tool-Aufruf: Text nur anzeigen, schaltet nichts
    return {"entries": [], "immediate": [], "delete_ids": [], "summary": None,
            "question": text[:400] or "Das habe ich nicht verstanden. Bitte anders formulieren."}


def _log_api_error(e: Exception, acfg: AssistantConfig) -> None:
    status = getattr(e, "status_code", None)
    rid = getattr(getattr(e, "response", None), "headers", {}) or {}
    rid = rid.get("request-id") if hasattr(rid, "get") else None
    log.warning("Chat: API-Fehler %s%s (Modell %s)%s", type(e).__name__,
                f" {status}" if status else "", acfg.model, f", request-id {rid}" if rid else "")


# ── Vorschlag prüfen ────────────────────────────────────────────────────────


@dataclass
class Proposal:
    id: str
    entries: list[dict[str, Any]] = field(default_factory=list)      # validierte DB-Felder
    immediate: list[dict[str, Any]] = field(default_factory=list)    # {room_id, setpoint}
    deletes: list[dict[str, Any]] = field(default_factory=list)      # bestehende Zeilen
    summary: Optional[str] = None
    note: str = ""
    created: float = 0.0


_PROPOSALS: dict[str, Proposal] = {}


def validate_proposal(hcfg: heating.HeatingConfig, inp: dict[str, Any], now: datetime) -> tuple[Proposal, list[str]]:
    """Alles prüfen; Fehlerliste leer = gültig. Nichts wird gespeichert."""
    errors: list[str] = []
    p = Proposal(id=uuid.uuid4().hex, created=_time.time())
    for i, e in enumerate(inp.get("entries") or []):
        try:
            days = e.get("days") or None
            p.entries.append(hs.validate_entry(hcfg, e.get("room_id"), e.get("setpoint"), e.get("time"),
                                               days=days, date=e.get("date") or None, now=now))
        except (ValueError, TypeError, AttributeError) as ex:
            errors.append(f"Eintrag {i + 1}: {ex}")
    for i, im in enumerate(inp.get("immediate") or []):
        room = hcfg.room(im.get("room_id") or "")
        try:
            if room is None:
                raise ValueError(f"Raum {im.get('room_id')!r} unbekannt")
            if not room.controllable:
                raise ValueError(f"{room.name} hat keinen Sollwert (nur Anzeige)")
            p.immediate.append({"room_id": room.id, "setpoint": heating.validate_setpoint(hcfg, im.get("setpoint"))})
        except (ValueError, TypeError) as ex:
            errors.append(f"Sofort {i + 1}: {ex}")
    for sid in inp.get("delete_ids") or []:
        row = hs.get_schedule(sid) if isinstance(sid, int) and not isinstance(sid, bool) else None
        if row is None:
            errors.append(f"Zeitplan #{sid} gibt es nicht")
        else:
            p.deletes.append(row)
    s = inp.get("summary")
    p.summary = s.strip()[:200] if isinstance(s, str) and s.strip() else None
    return p, errors


def store(p: Proposal) -> None:
    cutoff = _time.time() - PROPOSAL_TTL_S
    for k in [k for k, v in _PROPOSALS.items() if v.created < cutoff]:
        del _PROPOSALS[k]
    _PROPOSALS[p.id] = p


def take(pid: str) -> Optional[Proposal]:
    p = _PROPOSALS.pop(pid, None)
    if p is None or p.created < _time.time() - PROPOSAL_TTL_S:
        return None
    return p


def preview(hcfg: heating.HeatingConfig, p: Proposal, now: datetime) -> dict[str, Any]:
    """Für die UI: Gruppen (Datum bzw. Wochentage) mit Zeilen, Sofort-Änderungen, Löschungen."""
    today = now.astimezone(hs.TZ).date()
    name = lambda rid: (hcfg.room(rid).name if hcfg.room(rid) else rid)  # noqa: E731
    groups: dict[str, dict[str, Any]] = {}
    for e in sorted(p.entries, key=lambda x: (x["date"] or "9", x["days"] or "", x["time"])):
        if e["date"]:
            key, label = e["date"], _day_label(Date.fromisoformat(e["date"]), today)
        else:
            key, label = "w:" + e["days"], _days_label(e["days"].split(","))
        groups.setdefault(key, {"label": label, "rows": []})["rows"].append(
            {"time": e["time"], "room": name(e["room_id"]), "setpoint": e["setpoint"]})
    return {
        "id": p.id,
        "summary": p.summary,
        "groups": list(groups.values()),
        "immediate": [{"room": name(i["room_id"]), "setpoint": i["setpoint"]} for i in p.immediate],
        "deletes": [{"id": d["id"], "room": name(d["room_id"]), "time": d["time"], "setpoint": d["setpoint"],
                     "when": (_day_label(Date.fromisoformat(d["date"]), today) if d["date"]
                              else _days_label(d["days"]) + " (ganzer Plan)")} for d in p.deletes],
    }


# ── Abfragen ohne API ───────────────────────────────────────────────────────

_QUERY_RE = re.compile(r"\b(was|welche|wann)\b.*\b(geplant|plan|pläne|zeitpläne|schaltet)\b"
                       r"|\b(zeig|liste)\w*\b.*\b(pläne|zeitpläne|plan)\b", re.IGNORECASE)


def local_answer(text: str, hcfg: heating.HeatingConfig, entries: list[dict[str, Any]],
                 now: datetime) -> Optional[str]:
    """„Was ist für morgen geplant?“ → Antwort aus der DB (kein API-Aufruf). None = keine Abfrage."""
    t = text.strip().lower()
    if not _QUERY_RE.search(t) or re.search(r"\b(lösch|entfern|stell|setz|mach|auf \d)", t):
        return None
    now = now.astimezone(hs.TZ)
    today = now.date()
    days = HORIZON_DAYS
    first = today
    if "übermorgen" in t:
        first, days = today + timedelta(days=2), 0
    elif "morgen" in t:
        first, days = today + timedelta(days=1), 0
    elif "heute" in t:
        days = 0
    rooms = [r for r in hcfg.rooms if r.controllable and (r.name.lower() in t or r.id in t)]
    up = [u for u in upcoming(entries, now, (first - today).days + days)
          if u["date"] >= first and (not rooms or u["room_id"] in {r.id for r in rooms})]
    single = days == 0
    scope = (_day_label(first, today) if single else f"Nächste {HORIZON_DAYS} Tage") + \
            (" · " + ", ".join(r.name for r in rooms) if rooms else "")
    if not up:
        return f"{scope}: nichts geplant."
    out, cur = [f"{scope}:"], None
    for u in up:
        if not single and u["date"] != cur:
            cur = u["date"]
            out.append(_day_label(cur, today))
        room = hcfg.room(u["room_id"])
        out.append(f"  {u['time']}  {room.name if room else u['room_id']} → {_fmt_c(u['setpoint'])}")
    return "\n".join(out)
