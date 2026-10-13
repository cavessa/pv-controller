"""Heizung Phase 3: Chat → Zeitplan-Vorschlag über ein Sprachmodell (Claude oder ChatGPT, nur Backend).

Ein Chat für Heizung **und Rollläden**: dasselbe Tool trägt zusätzlich ``shutter_entries``,
``shutter_immediate`` und ``shutter_delete_ids`` (geprüft in ``shutter_chat``).

Ablauf: Text + Kontext (Datum/Uhrzeit Europe/Berlin, Räume mit aktuellen Werten, Grenzen, Zeitpläne der
nächsten 7 Tage) → Anthropic Messages API bzw. OpenAI Chat Completions mit genau einem Tool
``propose_schedule`` (strict, gleiches Schema für beide) → der Server prüft alles
(Raum, Grenzen, Uhrzeit, Datum nicht in der Vergangenheit, Lösch-IDs) → Vorschlag mit ID zurück an die UI →
erst „Übernehmen“ speichert/schaltet. Ungültiges → Fehlermeldung, nichts wird gespeichert.

Config (config.json):
  "assistant": {"enabled": false,              # Settings-Schalter „Chat aktiv“ (Default aus)
                "provider": "openai",           # "openai" (ChatGPT) | "anthropic" (Claude, Default)
                "openai_api_key": "sk-…",       # Keys nur in config.json, nie ins Git/Frontend/Log
                "api_key": "sk-ant-…",          # Anthropic-Key
                "model": "gpt-4.1-mini",        # Default je Anbieter, s. DEFAULT_MODELS
                "max_requests_per_day": 100,
                "workspace_id": "wrkspc_…"}     # optional, nur Anthropic-Keys ohne Workspace-Zuordnung

Abfragen wie „was ist für morgen geplant?“ beantwortet ``local_answer`` aus der DB, ohne API.
Die SDKs (``openai``, ``anthropic``) werden erst beim Aufruf importiert: ohne Paket ist nur der Chat aus.
"""

from __future__ import annotations

import json
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

ANTHROPIC, OPENAI = "anthropic", "openai"
PROVIDERS = {ANTHROPIC: "Claude", OPENAI: "ChatGPT"}
DEFAULT_MODELS = {ANTHROPIC: "claude-haiku-5-5", OPENAI: "gpt-4.1-mini"}
DEFAULT_MODEL = DEFAULT_MODELS[ANTHROPIC]
_KEY_FIELD = {ANTHROPIC: "api_key", OPENAI: "openai_api_key"}
_SDK = {ANTHROPIC: "anthropic", OPENAI: "openai"}
_HOST = {ANTHROPIC: "api.anthropic.com", OPENAI: "api.openai.com"}
DEFAULT_LIMIT = 100
MAX_TOKENS = 8000
API_TIMEOUT_S = 20.0                  # harte Obergrenze je Chat-Anfrage (kein Retry, s. _default_client)
API_CONNECT_TIMEOUT_S = 5.0
MAX_HISTORY = 12                      # Nachrichten (Rückfragen + Antworten)
MAX_TEXT = 500                        # Zeichen je Nachricht
PROPOSAL_TTL_S = 15 * 60
HORIZON_DAYS = 7
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,63}$")
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_\-]{4,80}$")
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
    api_key: str = ""                 # Anthropic
    model: str = DEFAULT_MODEL
    max_requests_per_day: int = DEFAULT_LIMIT
    workspace_id: str = ""            # Header anthropic-workspace-id (Key ohne Workspace-Zuordnung)
    provider: str = ANTHROPIC
    openai_api_key: str = ""

    @property
    def key(self) -> str:
        """Key des gewählten Anbieters."""
        return self.openai_api_key if self.provider == OPENAI else self.api_key

    @property
    def key_set(self) -> bool:
        return bool(self.key)

    @property
    def key_field(self) -> str:
        return _KEY_FIELD[self.provider]


def parse_assistant(raw: dict) -> AssistantConfig:
    a = raw.get("assistant") or {}
    if not isinstance(a, dict):
        raise ValueError("assistant must be an object")
    enabled = a.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("assistant.enabled must be true/false")
    provider = a.get("provider") or ANTHROPIC
    if provider not in PROVIDERS:
        raise ValueError("assistant.provider must be 'openai' or 'anthropic'")
    keys = {}
    for field_ in _KEY_FIELD.values():
        v = a.get(field_) or ""
        if not isinstance(v, str):
            raise ValueError(f"assistant.{field_} must be a string")
        keys[field_] = v.strip()
    model = a.get("model") or DEFAULT_MODELS[provider]
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise ValueError("assistant.model must look like 'gpt-4.1-mini' or 'claude-haiku-5-5'")
    limit = a.get("max_requests_per_day", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("assistant.max_requests_per_day must be 1…10000")
    ws = a.get("workspace_id") or ""
    if not isinstance(ws, str) or (ws and not _WORKSPACE_RE.match(ws.strip())):
        raise ValueError("assistant.workspace_id must look like 'wrkspc_…'")
    return AssistantConfig(enabled=enabled, api_key=keys["api_key"], openai_api_key=keys["openai_api_key"],
                           provider=provider, model=model, max_requests_per_day=limit, workspace_id=ws.strip())


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


def sdk_available(provider: str = ANTHROPIC) -> bool:
    try:
        __import__(_SDK[provider])
    except ImportError:
        return False
    return True


def status(hcfg: heating.HeatingConfig, acfg: AssistantConfig, now: Optional[datetime] = None,
           shutters_enabled: bool = False) -> dict[str, Any]:
    used = used_today(now)
    hint = None
    if not acfg.enabled:
        hint = "Chat ist aus. Einschalten unter Settings → Heizung → „Chat aktiv“."
    elif not hcfg.enabled and not shutters_enabled:
        hint = "Heizung ist nicht aktiv."
    elif not acfg.key_set:
        hint = f"API-Key fehlt: assistant.{acfg.key_field} in config.json eintragen."
    elif not sdk_available(acfg.provider):
        hint = f"Python-Paket „{_SDK[acfg.provider]}“ fehlt (pip install -r requirements.txt)."
    elif used >= acfg.max_requests_per_day:
        hint = f"Tageslimit erreicht ({used}/{acfg.max_requests_per_day}). Morgen geht es weiter."
    return {"enabled": acfg.enabled, "key_set": acfg.key_set, "model": acfg.model,
            "provider": acfg.provider, "provider_name": PROVIDERS[acfg.provider],
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


def upcoming(entries: list[dict[str, Any]], now: datetime, days: int = HORIZON_DAYS,
             loc: Optional[tuple[float, float]] = None) -> list[dict[str, Any]]:
    """Schaltpunkte ab jetzt für ``days`` Tage: [{date, time, room_id, setpoint, id, kind}].
    ``time`` ist bei Sonnen-Einträgen die berechnete Uhrzeit, ``when`` der Zeittyp („Sonnenuntergang −15 min“)."""
    now = now.astimezone(hs.TZ)
    out = []
    for off in range(days + 1):
        d = now.date() + timedelta(days=off)
        for e in entries:
            if not e["enabled"] or not hs.applies_on(e, d):
                continue
            at = hs.instant(e, d, loc)
            if at is None or hs._utc(at) <= hs._utc(now):
                continue
            sun = (e.get("kind") or "time") != "time"
            out.append({"date": d, "time": f"{at:%H:%M}" if sun else e["time"], "room_id": e["room_id"],
                        "setpoint": e["setpoint"], "id": e["id"], "when": hs.when(e) if sun else None,
                        "kind": "einmalig" if e["date"] else "wiederkehrend"})
    out.sort(key=lambda x: (x["date"], x["time"], x["room_id"]))
    return out


def build_context(hcfg: heating.HeatingConfig, rooms: list[dict[str, Any]], entries: list[dict[str, Any]],
                  now: datetime, loc: Optional[tuple[float, float]] = None) -> str:
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
        lines.append(f"- #{e['id']} {e['room_id']} {hs.when(e)} {_fmt_c(e['setpoint'])} {when}"
                     f"{'' if e['enabled'] else ' (aus)'}")
    lines += ["", f"Schaltpunkte der nächsten {HORIZON_DAYS} Tage:"]
    up = upcoming(entries, now, loc=loc)
    if not up:
        lines.append("- keine")
    for u in up:
        lines.append(f"- {u['date'].isoformat()} {WD[u['date'].weekday()]} {u['time']} {u['room_id']} → "
                     f"{_fmt_c(u['setpoint'])} (#{u['id']}, {u['kind']}"
                     + (f", {u['when']}" if u.get("when") else "") + ")")
    return "\n".join(lines)


SYSTEM_PROMPT = """Du setzt Wünsche zur Heizungssteuerung eines Wohnhauses in Zeitpläne um.
Antworte immer mit genau einem Aufruf des Tools propose_schedule und schreibe keinen anderen Text.

Regeln für propose_schedule:
- entries: neue Schaltpunkte. Einmalig: date (YYYY-MM-DD) setzen und days null lassen. Wiederkehrend: days
  setzen (mo, di, mi, do, fr, sa, so; „werktags“ = mo bis fr, „am Wochenende“ = sa, so) und date null lassen.
- Uhrzeiten als HH:MM im 24-Stunden-Format. „um 7“ ohne weitere Angabe heißt 07:00, „um 7 abends“ heißt 19:00.
- Zeittyp je Eintrag: kind = "time" (time HH:MM; offset_min 0, not_before/not_after null) oder
  "sunrise"/"sunset" (time null; offset_min = Minuten danach, negativ = davor: „30 min vor Sonnenaufgang“ =
  sunrise, -30; „bei Sonnenuntergang“ = sunset, 0). not_before / not_after (HH:MM oder null) für
  „aber nicht vor 6 Uhr“ / „spätestens 22 Uhr“.
- Wochentage wie „Freitag“ meinen den nächsten solchen Tag laut Kalender.
- immediate: Änderungen, die sofort gelten sollen („jetzt“, „sofort“).
- delete_ids: IDs bestehender Zeitpläne aus der Liste, die der Nutzer löschen will. Meint der Nutzer nur einen
  Tag eines wiederkehrenden Plans, lösche ihn nicht, sondern stelle eine Rückfrage.
- Nur room_id aus der Raumliste und nur steuerbare Räume. Sollwerte innerhalb der Grenzen.
  Keine Zeitpunkte in der Vergangenheit.
- question: eine kurze Rückfrage auf Deutsch, wenn Raum, Zeit, Tag oder Sollwert nicht eindeutig sind
  (z. B. zwei Räume passen). Dann entries, immediate und delete_ids leer lassen. Sonst null.
- summary: ein kurzer deutscher Satz, was geplant wird, oder null.

Rollläden (nur wenn eine Rollladenliste im Kontext steht; sonst die shutter_-Felder leer lassen):
- shutter_entries: Zeitpläne. kind = "time" (time HH:MM) oder "sunrise"/"sunset" (time null,
  offset_min = Minuten danach, negativ = davor, 0 = genau; „30 min nach Sonnenuntergang“ = sunset, 30).
  not_before / not_after (HH:MM oder null) für „aber nicht vor 18 Uhr“ / „spätestens 22 Uhr“.
  action: "up" (hoch, auf, öffnen), "down" (runter, zu, schließen) oder "position" mit position in % ZU
  (0 = ganz offen, 100 = ganz zu; „halb“ = 50). date/days wie bei der Heizung.
  Zeitpläne nur für Rollläden mit „Automatik: PV Controller“ – trotzdem auch für openHAB-Rollläden eintragen,
  wenn der Nutzer sie nennt (der Server lehnt sie freundlich ab und erklärt es).
- shutter_immediate: sofort fahren („jetzt“, „sofort“) – für alle Rollläden erlaubt. action "up", "down",
  "stop" oder "position" (dann position in % zu, sonst null).
- shutter_delete_ids: IDs bestehender Rollladen-Zeitpläne, die gelöscht werden sollen.
- „alle Rollläden“ = jeder Rollladen der Liste einzeln. Passt ein Name auf mehrere Rollläden (z. B.
  „Wohnen“ → Wohnen links und Wohnen rechts) und sagt der Nutzer nicht „beide“ oder „alle“, stelle eine
  Rückfrage (question) statt zu raten.
- „morgen früh … um 7“ ohne Wochentag = einmalig morgen."""

_DAY_ENUM = list(hs.DAYS)
_NULL_STR = {"type": ["string", "null"]}
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
                    "required": ["room_id", "setpoint", "kind", "time", "offset_min", "not_before", "not_after",
                                 "date", "days"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "setpoint": {"type": "number"},
                        "kind": {"type": "string", "enum": ["time", "sunrise", "sunset"]},
                        "time": {"type": ["string", "null"], "description": "HH:MM, 24 h, Europe/Berlin; null bei Sonnenzeit"},
                        "offset_min": {"type": ["integer", "null"], "description": "Minuten nach (−: vor) der Sonnenzeit"},
                        "not_before": {"type": ["string", "null"], "description": "HH:MM oder null"},
                        "not_after": {"type": ["string", "null"], "description": "HH:MM oder null"},
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
            "shutter_entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shutter_id", "action", "position", "kind", "time", "offset_min",
                                 "not_before", "not_after", "date", "days"],
                    "properties": {
                        "shutter_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["up", "down", "position"]},
                        "position": {"type": ["number", "null"], "description": "% zu bei action=position"},
                        "kind": {"type": "string", "enum": ["time", "sunrise", "sunset"]},
                        "time": {**_NULL_STR, "description": "HH:MM bei kind=time, sonst null"},
                        "offset_min": {"type": ["integer", "null"], "description": "Minuten nach (−: vor) der Sonnenzeit"},
                        "not_before": {**_NULL_STR, "description": "HH:MM oder null"},
                        "not_after": {**_NULL_STR, "description": "HH:MM oder null"},
                        "date": {**_NULL_STR, "description": "YYYY-MM-DD bei einmalig, sonst null"},
                        "days": {"type": ["array", "null"], "items": {"type": "string", "enum": _DAY_ENUM}},
                    },
                },
            },
            "shutter_immediate": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["shutter_id", "action", "position"],
                    "properties": {
                        "shutter_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["up", "down", "stop", "position"]},
                        "position": {"type": ["number", "null"]},
                    },
                },
            },
            "shutter_delete_ids": {"type": "array", "items": {"type": "integer"}},
        },
    },
}
TOOL["input_schema"]["required"] += ["shutter_entries", "shutter_immediate", "shutter_delete_ids"]
TOOL["description"] = ("Schlägt neue Heizungs- und Rollladen-Zeitpläne, sofortige Sollwerte bzw. Rollladen-Fahrten "
                       "und zu löschende Zeitpläne vor, oder stellt eine Rückfrage. Der Nutzer bestätigt den "
                       "Vorschlag, bevor etwas gespeichert oder gefahren wird.")


# ── API-Aufruf ──────────────────────────────────────────────────────────────


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _default_client(acfg: AssistantConfig) -> Any:
    """SDK-Client des gewählten Anbieters. Kein automatischer Retry (sonst bis (1 + Retries) × Timeout);
    der Endpunkt ist synchron (FastAPI-Threadpool) – ein hängender Aufruf blockiert keine anderen Requests."""
    sdk = _SDK[acfg.provider]
    try:
        mod = __import__(sdk)
    except ImportError:
        raise ChatError(f"Python-Paket „{sdk}“ fehlt – Chat nicht verfügbar.", "unavailable")
    if acfg.provider == OPENAI:
        return mod.OpenAI(api_key=acfg.key, max_retries=0,
                          timeout=mod.Timeout(API_TIMEOUT_S, connect=API_CONNECT_TIMEOUT_S))
    headers = {"anthropic-workspace-id": acfg.workspace_id} if acfg.workspace_id else None
    return mod.Anthropic(api_key=acfg.key, max_retries=0, default_headers=headers,
                         timeout=mod.Timeout(API_TIMEOUT_S, connect=API_CONNECT_TIMEOUT_S))


def _translate(e: Exception, acfg: AssistantConfig) -> ChatError:
    """SDK-Fehler → ChatError mit verständlichem Text (typisierte Klassen, spezifischste zuerst).

    Beide SDKs haben dieselben Fehlerklassen (NotFoundError, AuthenticationError, …)."""
    sdk = __import__(_SDK[acfg.provider])
    _log_api_error(e, acfg)
    msg = _api_message(e, acfg) if getattr(e, "status_code", None) else ""
    low = msg.lower()
    code = str(getattr(e, "code", "") or "")
    name = PROVIDERS[acfg.provider]
    billing = ("Kein Guthaben auf dem API-Konto – bei OpenAI unter „Billing“ aufladen." if acfg.provider == OPENAI
               else "Kein Guthaben auf dem API-Konto – in der Anthropic Console unter „Plans & Billing“ aufladen.")
    if isinstance(e, sdk.NotFoundError):
        return ChatError(f"Modell „{acfg.model}“ nicht verfügbar – in Settings ändern.", "model")
    if isinstance(e, sdk.AuthenticationError):
        return ChatError(f"API-Key ungültig – assistant.{acfg.key_field} in config.json prüfen.", "auth")
    if isinstance(e, sdk.PermissionDeniedError):
        return ChatError("API-Key hat keine Berechtigung für dieses Modell.", "auth")
    if isinstance(e, sdk.RateLimitError):
        if code == "insufficient_quota" or "quota" in low or "billing" in low:
            return ChatError(billing, "billing")
        return ChatError(f"{name} ist gerade ausgelastet (Rate-Limit). Bitte gleich noch einmal.", "busy")
    if isinstance(e, sdk.BadRequestError):
        if "credit balance" in low or code == "insufficient_quota":
            return ChatError(billing, "billing")
        if "workspace" in low:
            return ChatError("API-Key ist keinem Workspace zugeordnet: in der Console einen Key in einem "
                             "Workspace anlegen oder assistant.workspace_id in config.json eintragen.", "auth")
        if code == "model_not_found" or (re.search(r"\bmodel\b", msg, re.IGNORECASE)
                                         and not re.search(r"tool|schema|function", msg, re.IGNORECASE)):
            return ChatError(f"Modell „{acfg.model}“ nicht verfügbar – in Settings ändern.", "model")
        return ChatError("Die API hat die Anfrage abgelehnt (400)" + (f": {msg}" if msg else "."))
    if isinstance(e, sdk.APIStatusError):
        return ChatError(f"API-Fehler ({e.status_code}). Bitte später noch einmal.", "busy")
    if isinstance(e, sdk.APITimeoutError):
        return ChatError(f"{name} antwortet nicht (Zeitüberschreitung nach {API_TIMEOUT_S:g} s). "
                         "Bitte später noch einmal.", "timeout")
    if isinstance(e, sdk.APIConnectionError):
        return ChatError(f"Keine Verbindung zu {_HOST[acfg.provider]} (Internet?).", "offline")
    return ChatError("Unerwarteter Fehler beim Chat.")


def _no_tool(text: str) -> dict[str, Any]:
    """Kein Tool-Aufruf: Text nur anzeigen, schaltet nichts."""
    return {"entries": [], "immediate": [], "delete_ids": [], "summary": None,
            "shutter_entries": [], "shutter_immediate": [], "shutter_delete_ids": [],
            "question": text.strip()[:400] or "Das habe ich nicht verstanden. Bitte anders formulieren."}


def _request_anthropic(client: Any, acfg: AssistantConfig, system: str, messages: list[dict]) -> dict[str, Any]:
    resp = client.messages.create(
        model=acfg.model,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=[TOOL],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        messages=messages,
    )
    if resp.stop_reason == "refusal":
        raise ChatError("Die Anfrage wurde abgelehnt. Bitte anders formulieren.")
    for block in resp.content:
        if block.type == "tool_use" and block.name == "propose_schedule":
            return dict(block.input)
    if resp.stop_reason == "max_tokens":
        raise ChatError("Antwort unvollständig. Bitte kürzer formulieren.")
    return _no_tool(" ".join(b.text for b in resp.content if b.type == "text"))


OPENAI_TOOL = {"type": "function", "function": {
    "name": TOOL["name"], "description": TOOL["description"], "parameters": TOOL["input_schema"], "strict": True}}


def _request_openai(client: Any, acfg: AssistantConfig, system: str, messages: list[dict]) -> dict[str, Any]:
    resp = client.chat.completions.create(
        model=acfg.model,
        max_completion_tokens=MAX_TOKENS,
        messages=[{"role": "system", "content": system}, *messages],
        tools=[OPENAI_TOOL],
        tool_choice={"type": "function", "function": {"name": TOOL["name"]}},
        parallel_tool_calls=False,
    )
    choice = resp.choices[0]
    msg = choice.message
    if getattr(msg, "refusal", None):
        raise ChatError("Die Anfrage wurde abgelehnt. Bitte anders formulieren.")
    for call in msg.tool_calls or []:
        if getattr(call, "type", "function") == "function" and call.function.name == TOOL["name"]:
            try:
                return json.loads(call.function.arguments)
            except (TypeError, ValueError):
                raise ChatError("Antwort nicht lesbar. Bitte noch einmal.")
    if choice.finish_reason == "length":
        raise ChatError("Antwort unvollständig. Bitte kürzer formulieren.")
    return _no_tool(msg.content or "")


def call_model(acfg: AssistantConfig, context: str, history: list[dict[str, str]],
               client_factory: Optional[Callable[[AssistantConfig], Any]] = None) -> dict[str, Any]:
    """Ein API-Aufruf beim gewählten Anbieter → Eingabe des Tools ``propose_schedule`` (dict).

    Fehler werden zu ChatError mit verständlichem Text; der API-Key erscheint nie (auch nicht im Log).
    """
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    system = SYSTEM_PROMPT + "\n\n" + context
    client = (client_factory or _default_client)(acfg)
    request = _request_openai if acfg.provider == OPENAI else _request_anthropic
    try:
        return request(client, acfg, system, messages)
    except ChatError:
        raise
    except Exception as e:  # noqa: BLE001 – SDK-Fehler typisiert in _translate
        try:
            raise _translate(e, acfg) from None
        except ImportError:
            log.warning("Chat: Fehler %s", type(e).__name__)
            raise ChatError("Unerwarteter Fehler beim Chat.") from None


def _api_message(e: Exception, acfg: AssistantConfig, limit: int = 300) -> str:
    """Fehlertext der API (aus dem JSON-Body), ohne API-Key, gekürzt."""
    body = getattr(e, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message", "") if isinstance(err, dict) else str(body.get("message", ""))
    msg = msg or getattr(e, "message", "") or ""
    return _scrub(_scrub(str(msg), acfg.api_key), acfg.openai_api_key)[:limit]


def _log_api_error(e: Exception, acfg: AssistantConfig) -> None:
    status = getattr(e, "status_code", None)
    rid = getattr(getattr(e, "response", None), "headers", {}) or {}
    rid = (rid.get("request-id") or rid.get("x-request-id")) if hasattr(rid, "get") else None
    msg = _api_message(e, acfg) if status else ""
    log.warning("Chat: API-Fehler %s %s%s (Modell %s)%s%s", PROVIDERS[acfg.provider], type(e).__name__,
                f" {status}" if status else "", acfg.model, f", request-id {rid}" if rid else "",
                f": {msg}" if msg else "")


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
    shutter_entries: list[dict[str, Any]] = field(default_factory=list)    # validierte DB-Felder
    shutter_immediate: list[dict[str, Any]] = field(default_factory=list)  # {shutter_id, action, target}
    shutter_deletes: list[dict[str, Any]] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)                      # freundlich abgelehnt (Info)

    @property
    def empty(self) -> bool:
        return not (self.entries or self.immediate or self.deletes or self.shutter_entries
                    or self.shutter_immediate or self.shutter_deletes)


_PROPOSALS: dict[str, Proposal] = {}


def validate_proposal(hcfg: heating.HeatingConfig, inp: dict[str, Any], now: datetime,
                      loc: Optional[tuple[float, float]] = None) -> tuple[Proposal, list[str]]:
    """Alles prüfen; Fehlerliste leer = gültig. Nichts wird gespeichert."""
    errors: list[str] = []
    p = Proposal(id=uuid.uuid4().hex, created=_time.time())
    for i, e in enumerate(inp.get("entries") or []):
        try:
            days = e.get("days") or None
            p.entries.append(hs.validate_entry(
                hcfg, e.get("room_id"), e.get("setpoint"), e.get("time"), days=days, date=e.get("date") or None,
                now=now, kind=e.get("kind") or "time", offset_min=e.get("offset_min") or 0,
                not_before=e.get("not_before"), not_after=e.get("not_after"), loc=loc))
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


def peek(pid: str) -> Optional[Proposal]:
    """Vorschlag ansehen, ohne ihn zu verbrauchen (Tür-Bestätigung vor dem Übernehmen)."""
    p = _PROPOSALS.get(pid)
    if p is None or p.created < _time.time() - PROPOSAL_TTL_S:
        return None
    return p


def take(pid: str) -> Optional[Proposal]:
    p = _PROPOSALS.pop(pid, None)
    if p is None or p.created < _time.time() - PROPOSAL_TTL_S:
        return None
    return p


def _sun_row(e: dict[str, Any], today: Date, now: datetime, loc: Optional[tuple[float, float]]) -> dict[str, Any]:
    """Zusatzfelder einer Vorschau-Zeile mit Sonnenzeit: Zeittyp und berechnete Uhrzeit(en)."""
    ee = {**e, "days": e["days"].split(",") if e["days"] else []}
    if e["date"]:
        at = hs.instant(ee, Date.fromisoformat(e["date"]), loc)
        times = f"{at:%H:%M}" if at else "–"
        first = times
    else:
        parts = []
        for off, lbl in ((0, "heute"), (1, "morgen")):
            d = today + timedelta(days=off)
            at = hs.instant(ee, d, loc) if hs.applies_on(ee, d) else None
            if at is not None and not (off == 0 and hs._utc(at) <= hs._utc(now)):
                parts.append(f"{lbl} {at:%H:%M}")
        times = ", ".join(parts)
        first = parts[0].split(" ")[1] if parts else "☀"
    return {"time": first, "when": hs.when(ee), "at": times}


def preview(hcfg: heating.HeatingConfig, p: Proposal, now: datetime,
            loc: Optional[tuple[float, float]] = None) -> dict[str, Any]:
    """Für die UI: Gruppen (Datum bzw. Wochentage) mit Zeilen, Sofort-Änderungen, Löschungen."""
    today = now.astimezone(hs.TZ).date()
    name = lambda rid: (hcfg.room(rid).name if hcfg.room(rid) else rid)  # noqa: E731
    groups: dict[str, dict[str, Any]] = {}
    for e in sorted(p.entries, key=lambda x: (x["date"] or "9", x["days"] or "", x["time"])):
        if e["date"]:
            key, label = e["date"], _day_label(Date.fromisoformat(e["date"]), today)
        else:
            key, label = "w:" + e["days"], _days_label(e["days"].split(","))
        row = {"time": e["time"], "room": name(e["room_id"]), "setpoint": e["setpoint"]}
        if (e.get("kind") or "time") != "time":          # Sonnenzeit: berechnete Uhrzeit + Zeittyp
            row.update(_sun_row(e, today, now.astimezone(hs.TZ), loc))
        groups.setdefault(key, {"label": label, "rows": []})["rows"].append(row)
    return {
        "id": p.id,
        "summary": p.summary,
        "groups": list(groups.values()),
        "immediate": [{"room": name(i["room_id"]), "setpoint": i["setpoint"]} for i in p.immediate],
        "deletes": [{"id": d["id"], "room": name(d["room_id"]), "time": hs.when(d), "setpoint": d["setpoint"],
                     "when": (_day_label(Date.fromisoformat(d["date"]), today) if d["date"]
                              else _days_label(d["days"]) + " (ganzer Plan)")} for d in p.deletes],
    }


# ── Abfragen ohne API ───────────────────────────────────────────────────────

_QUERY_RE = re.compile(r"\b(was|welche|wann)\b.*\b(geplant|plan|pläne|zeitpläne|schaltet)\b"
                       r"|\b(zeig|liste)\w*\b.*\b(pläne|zeitpläne|plan)\b", re.IGNORECASE)


def local_answer(text: str, hcfg: heating.HeatingConfig, entries: list[dict[str, Any]],
                 now: datetime, loc: Optional[tuple[float, float]] = None) -> Optional[str]:
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
    up = [u for u in upcoming(entries, now, (first - today).days + days, loc)
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
        out.append(f"  {u['time']}  {room.name if room else u['room_id']} → {_fmt_c(u['setpoint'])}"
                   + (f" · {u['when']}" if u.get("when") else ""))
    return "\n".join(out)
