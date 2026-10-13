"""Rollläden: openHAB-Rollershutter anzeigen und manuell fahren (Phase 1).

Config (config.json):
  "shutters": {"enabled": true, "command_spacing_s": 2,
               "duty_cycle_item": "", "duty_cycle_max": 80,
               "schedules_enabled": false, "frost_temp_c": 0, "frost_temp_item": "", "manual_hold_min": 60,
               "rooms": [{"id", "name", "item", "contact_item", "is_door", "group",
                          "automation", "device", "can_position", "retry_when_closed", "auto_close_max"}]}

- ``item``: Rollershutter-Item (0 = offen, 100 = zu), nimmt UP/DOWN/STOP/0..100.
- ``automation``: "openhab" = eine openHAB-Regel fährt den Rollladen (der PV Controller plant dann
  nichts, Anzeige „Automatik: openHAB-Regel“); "pvcontroller" = eigene Zeitpläne (ab Phase 2).
- ``device``: "homematic" | "shelly" | "other" → Wartezeit der Rückprüfung (HmIP meldet die Position
  nur im 5-min-Raster, Shelly sofort).
- Tür (``is_door``) mit offenem / unbekanntem Kontakt oder ganz ohne Kontakt-Item: Schließen nur mit
  ausdrücklicher Bestätigung; Gruppenbefehle überspringen solche Türen.
- Ist-Position (``position_source``): "item" (Default, Steuer-Item – nach Befehlen durch openHAB-autoupdate
  verfälscht), "shelly" (``shelly_url``, Gen1 ``/roller/0`` direkt, auch während der Fahrt) oder
  "item:<Name>" (eigenes Lese-Item, z. B. HmIP-Kanal 3 LEVEL ohne autoupdate). Befehle gehen immer an ``item``.
  Ohne verlässliche Quelle gilt die Position nach STOP / bei ausstehender Rückmeldung als unbekannt.
- Zeitpläne (``shutter_schedule``): ``retry_when_closed`` = bei offener Tür übersprungenes Schließen nachholen,
  sobald die Tür zu ist; ``auto_close_max`` = automatisches Schließen höchstens bis hier (Default 80 % bei
  einer Tür ohne Kontakt-Item, sonst 100).

Rückprüfung: jeder echte Befehl landet in ``shutter_command``. Nach ``verify_after_s`` wird die gemeldete
Position mit dem Ziel verglichen (beim Statusabruf, kein eigener Thread). openHAB-autoupdate setzt das
Item sofort auf das Ziel und der Aktor meldet kurz danach noch die alte Position – vor Ablauf der Wartezeit
ist der Wert daher nicht aussagekräftig.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import requests

import db
from clients.openhab_client import OpenHabClient, OpenHabError
from heating import _num

log = logging.getLogger("pv-controller.shutters")

TZ = ZoneInfo("Europe/Berlin")
_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_GROUP_RE = re.compile(r"^[a-z0-9_-]{0,16}$")
AUTOMATIONS = ("openhab", "pvcontroller")
DEVICES = ("homematic", "shelly", "other")
VERIFY_AFTER_S = {"homematic": 360, "shelly": 60, "other": 120}
VERIFY_TOLERANCE = 5            # HmIP meldet nach „zu“ gern 98/99 %
VERIFY_GIVE_UP_MIN = 30         # openHAB so lange weg → Rückprüfung aufgeben (Hinweis)
COMMANDS = ("up", "down", "stop", "position")
SHELLY_TIMEOUT_S = 2.0
_URL_RE = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$")


@dataclass
class Shutter:
    id: str
    name: str
    item: str
    contact_item: str = ""
    is_door: bool = False
    group: str = ""
    automation: str = "openhab"
    device: str = "other"
    can_position: bool = True
    retry_when_closed: bool = False
    auto_close_max: int = 100
    position_source: str = "item"        # "item" | "shelly" | "item:<Name>" (wie in der Config)
    shelly_url: str = ""

    @property
    def verify_after_s(self) -> int:
        return VERIFY_AFTER_S[self.device]

    @property
    def position_item(self) -> str:
        """Eigenes Lese-Item der Ist-Position ("item:<Name>"), sonst ""."""
        return self.position_source[5:] if self.position_source.startswith("item:") else ""

    @property
    def history_item(self) -> str:
        """Item, dessen Persistence-Verlauf „von Hand bewegt“ zeigt: Lese-Item, sonst Steuer-Item
        (der Shelly-Binding meldet Taster-Fahrten dort; autoupdate-Sprünge liegen im Fenster eigener Befehle)."""
        return self.position_item or self.item

    def items(self) -> list[str]:
        return [n for n in (self.item, self.position_item, self.contact_item) if n]


@dataclass
class ShutterConfig:
    enabled: bool = False
    command_spacing_s: float = 2.0
    duty_cycle_item: str = ""
    duty_cycle_max: float = 80.0
    schedules_enabled: bool = False      # Schalter „Rollladen-Zeitpläne aktiv“ (Default aus)
    frost_temp_c: float = 0.0
    frost_temp_item: str = ""
    manual_hold_min: int = 60
    rooms: list[Shutter] = field(default_factory=list)

    def shutter(self, sid: str) -> Optional[Shutter]:
        return next((s for s in self.rooms if s.id == sid), None)

    def groups(self) -> list[str]:
        out: list[str] = []
        for s in self.rooms:
            if s.group and s.group not in out:
                out.append(s.group)
        return out

    def members(self, group: str) -> list[Shutter]:
        return list(self.rooms) if group == "all" else [s for s in self.rooms if s.group == group]


def _str(v: Any, where: str) -> str:
    v = v if v is not None else ""
    if not isinstance(v, str):
        raise ValueError(f"{where} must be a string")
    return v.strip()


def _bool(v: Any, where: str, default: bool) -> bool:
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ValueError(f"{where} must be true/false")
    return v


def parse_shutters(raw: dict) -> ShutterConfig:
    """shutters-Block aus der rohen Config; wirft ValueError bei ungültigen Werten."""
    s = raw.get("shutters") or {}
    if not isinstance(s, dict):
        raise ValueError("shutters must be an object")
    spacing = s.get("command_spacing_s", 2)
    if not isinstance(spacing, (int, float)) or isinstance(spacing, bool) or not 0.5 <= spacing <= 30:
        raise ValueError("shutters.command_spacing_s must be a number between 0.5 and 30")
    dmax = s.get("duty_cycle_max", 80)
    if not isinstance(dmax, (int, float)) or isinstance(dmax, bool) or not 10 <= dmax <= 100:
        raise ValueError("shutters.duty_cycle_max must be a number between 10 and 100")
    rooms: list[Shutter] = []
    seen: set[str] = set()
    for i, r in enumerate(s.get("rooms") or []):
        w = f"shutters.rooms[{i}]"
        if not isinstance(r, dict):
            raise ValueError(f"{w} must be an object")
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            raise ValueError(f"{w}.id must match [a-z0-9_-]{{1,32}}")
        if rid in seen:
            raise ValueError(f"shutters.rooms: duplicate id {rid!r}")
        seen.add(rid)
        name = _str(r.get("name"), f"{w}.name")
        item = _str(r.get("item"), f"{w}.item")
        if not name or not item:
            raise ValueError(f"{w}: name and item are required")
        group = _str(r.get("group"), f"{w}.group").lower()
        if not _GROUP_RE.match(group) or group == "all":
            raise ValueError(f"{w}.group must match [a-z0-9_-]{{0,16}} and not be 'all'")
        automation = r.get("automation") or "openhab"
        if automation not in AUTOMATIONS:
            raise ValueError(f"{w}.automation must be one of {', '.join(AUTOMATIONS)}")
        device = r.get("device") or "other"
        if device not in DEVICES:
            raise ValueError(f"{w}.device must be one of {', '.join(DEVICES)}")
        contact_item = _str(r.get("contact_item"), f"{w}.contact_item")
        is_door = _bool(r.get("is_door"), f"{w}.is_door", False)
        acm = r.get("auto_close_max")
        if acm is None:
            acm = 80 if is_door and not contact_item else 100
        if not isinstance(acm, int) or isinstance(acm, bool) or not 0 <= acm <= 100:
            raise ValueError(f"{w}.auto_close_max must be an integer 0–100")
        psrc = _str(r.get("position_source"), f"{w}.position_source") or "item"
        shelly_url = _str(r.get("shelly_url"), f"{w}.shelly_url").rstrip("/")
        if psrc == "shelly":
            if not _URL_RE.match(shelly_url):
                raise ValueError(f"{w}.shelly_url must be http(s)://… when position_source is 'shelly'")
        elif psrc != "item" and not (psrc.startswith("item:") and psrc[5:].strip()):
            raise ValueError(f"{w}.position_source must be 'item', 'shelly' or 'item:<Name>'")
        psrc = psrc if not psrc.startswith("item:") else "item:" + psrc[5:].strip()
        rooms.append(Shutter(
            id=rid, name=name, item=item, contact_item=contact_item, is_door=is_door, group=group,
            automation=automation, device=device,
            can_position=_bool(r.get("can_position"), f"{w}.can_position", True),
            retry_when_closed=_bool(r.get("retry_when_closed"), f"{w}.retry_when_closed", False),
            auto_close_max=acm, position_source=psrc, shelly_url=shelly_url))
    se = s.get("schedules_enabled", False)
    if not isinstance(se, bool):
        raise ValueError("shutters.schedules_enabled must be true/false")
    frost = s.get("frost_temp_c", 0)
    if not isinstance(frost, (int, float)) or isinstance(frost, bool) or not -20 <= frost <= 10:
        raise ValueError("shutters.frost_temp_c must be a number between -20 and 10")
    hold = s.get("manual_hold_min", 60)
    if not isinstance(hold, int) or isinstance(hold, bool) or not 0 <= hold <= 600:
        raise ValueError("shutters.manual_hold_min must be an integer 0–600")
    return ShutterConfig(enabled=bool(s.get("enabled", False)), command_spacing_s=float(spacing),
                         duty_cycle_item=_str(s.get("duty_cycle_item"), "shutters.duty_cycle_item"),
                         duty_cycle_max=float(dmax), schedules_enabled=se, frost_temp_c=float(frost),
                         frost_temp_item=_str(s.get("frost_temp_item"), "shutters.frost_temp_item"),
                         manual_hold_min=hold, rooms=rooms)


# ── Zustände normalisieren ──────────────────────────────────────────────────


def norm_position(item: Optional[dict]) -> Optional[int]:
    """Rollershutter/Dimmer '65' → 65 (0 = offen, 100 = zu); NULL/UNDEF → None."""
    v = _num(item.get("state")) if item else None
    if v is None or not 0 <= v <= 100:
        return None
    return int(round(v))


def norm_contact(item: Optional[dict]) -> Optional[str]:
    """Contact OPEN/CLOSED (auch Switch ON/OFF, String open/closed) → 'open' | 'closed' | None."""
    if not item:
        return None
    s = str(item.get("state", "")).strip().upper()
    if s in ("OPEN", "ON", "TRUE", "1", "TILTED"):
        return "open"
    if s in ("CLOSED", "OFF", "FALSE", "0"):
        return "closed"
    return None


def norm_duty_cycle(item: Optional[dict]) -> Optional[float]:
    """DUTY_CYCLE_RATIO der CCU in Prozent (Anteil 0..1 bei Dimensionless → ×100)."""
    if not item:
        return None
    raw = str(item.get("state", ""))
    v = _num(raw)
    if v is None or v < 0:
        return None
    if "%" not in raw and str(item.get("type", "")).startswith("Number:Dimensionless") and v <= 1.0:
        v *= 100
    return round(v, 1)


def door_guard(sh: Shutter, contact: Optional[str]) -> Optional[str]:
    """Grund, warum Schließen eine Bestätigung braucht (nur Türen), sonst None."""
    if not sh.is_door:
        return None
    if not sh.contact_item:
        return f"{sh.name}: Tür ohne Kontakt – ist niemand draußen?"
    if contact == "open":
        return f"{sh.name}: Tür ist offen"
    if contact != "closed":
        return f"{sh.name}: Türkontakt unbekannt"
    return None


def target_of(command: str, position: Optional[int] = None) -> Optional[int]:
    return {"up": 0, "down": 100}.get(command, position if command == "position" else None)


def is_closing(command: str, target: Optional[int], current: Optional[int]) -> bool:
    if command == "down":
        return True
    if command == "position" and target is not None:
        return current is None or target > current
    return False


# ── DB: gesendete Befehle + Rückprüfung ─────────────────────────────────────


_initialized: set[str] = set()


def _conn() -> sqlite3.Connection:
    if str(db.DB_PATH) not in _initialized:
        init_tables()
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    _initialized.add(str(db.DB_PATH))
    with sqlite3.connect(db.DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shutter_command (
                id          INTEGER PRIMARY KEY,
                shutter_id  TEXT NOT NULL,
                command     TEXT NOT NULL,
                target      INTEGER,
                source      TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                verify_at   TEXT,
                status      TEXT NOT NULL,
                reported    INTEGER,
                message     TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_shutter_command_sid ON shutter_command (shutter_id, id)")


def record_command(sh: Shutter, command: str, target: Optional[int], source: str, now: datetime) -> int:
    """Gesendeten Befehl merken; STOP wird nicht geprüft (Ziel unbekannt)."""
    verify_at = None if target is None else now + timedelta(seconds=sh.verify_after_s)
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO shutter_command (shutter_id, command, target, source, sent_at, verify_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sh.id, command, target, source, now.isoformat(timespec="seconds"),
             verify_at.isoformat(timespec="seconds") if verify_at else None,
             "pending" if target is not None else "none"))
        conn.execute("DELETE FROM shutter_command WHERE sent_at < ?",
                     ((now - timedelta(days=30)).isoformat(timespec="seconds"),))
        return int(cur.lastrowid)


def last_commands() -> dict[str, dict[str, Any]]:
    """Letzter Befehl je Rollladen."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM shutter_command WHERE id IN (SELECT MAX(id) FROM shutter_command GROUP BY shutter_id)")
        return {r["shutter_id"]: dict(r) for r in rows}


def _update(cid: int, status: str, reported: Optional[int], message: Optional[str]) -> None:
    with _conn() as conn:
        conn.execute("UPDATE shutter_command SET status = ?, reported = ?, message = ? WHERE id = ?",
                     (status, reported, message, cid))


STOP_UNCERTAIN_MIN = 60        # nach STOP über openHAB bleibt beim Shelly die autoupdate-Vorhersage stehen


def position_reliable(cmd: Optional[dict[str, Any]], now: datetime) -> bool:
    """Ist die gemeldete Position verlässlich? Nein, solange ein eigener Befehl auf Rückmeldung wartet
    (autoupdate setzt sofort das Ziel) und bis 60 min nach einem STOP (openHAB sagt dann z. B. 100,
    der Rollladen steht aber irgendwo dazwischen – am Shelly 2.5 am 13.10.2026 beobachtet)."""
    if not cmd:
        return True
    if cmd["status"] == "pending":
        return False
    if cmd["command"] == "stop":
        return now - datetime.fromisoformat(cmd["sent_at"]) > timedelta(minutes=STOP_UNCERTAIN_MIN)
    return True


def _hm(iso: str) -> str:
    return iso[11:16]


# ── Ist-Position ────────────────────────────────────────────────────────────


class ShellyError(Exception):
    pass


def read_shelly(url: str, timeout: float = SHELLY_TIMEOUT_S) -> dict[str, Any]:
    """Shelly Gen1 (2.5) ``GET <url>/roller/0`` → state ("stop"|"open"|"close"), current_pos (% **offen**), …"""
    try:
        r = requests.get(url.rstrip("/") + "/roller/0", timeout=timeout)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ShellyError(f"Shelly {url}: {e}") from e
    if not isinstance(d, dict) or "state" not in d:
        raise ShellyError(f"Shelly {url}: unerwartete Antwort")
    return d


@dataclass
class Position:
    value: Optional[int]               # 0 = offen, 100 = zu
    reliable: bool                     # für Entscheidungen verwendbar
    origin: str                        # "shelly" | "item:<Name>" | "item"
    moving: Optional[str] = None       # "down" | "up" während der Fahrt (nur Shelly)
    note: Optional[str] = None         # Hinweis für die Card (Fallback, Stop)


def _from_shelly(d: dict[str, Any]) -> tuple[Optional[int], Optional[str]]:
    """Shelly-Status → (Position 0 = offen … 100 = zu, Fahrtrichtung). Ohne Kalibrierung keine Position."""
    moving = {"close": "down", "open": "up"}.get(str(d.get("state")))
    cp = d.get("current_pos")
    if not d.get("positioning", True) or d.get("calibrating") or d.get("is_valid") is False \
            or not isinstance(cp, (int, float)) or isinstance(cp, bool) or not 0 <= cp <= 100:
        return None, moving
    return int(round(100 - cp)), moving


def resolve_position(sh: Shutter, items: dict[str, dict], cmd: Optional[dict[str, Any]], now: datetime,
                     shelly: Any = None) -> Position:
    """Ist-Position aus der konfigurierten Quelle; ohne verlässliche Quelle Fallback auf das Steuer-Item
    mit der „unbekannt“-Logik (``position_reliable``). ``shelly``: Antwort von ``read_shelly`` oder Exception."""
    fallback_note = None
    if sh.position_source == "shelly":
        if isinstance(shelly, dict):
            v, moving = _from_shelly(shelly)
            if v is not None:
                return Position(v, True, "shelly", moving)
            fallback_note = "Shelly meldet keine Position (nicht kalibriert?) – Position aus openHAB"
        else:
            fallback_note = "Shelly nicht erreichbar – Position aus openHAB"
    elif sh.position_item:
        v = norm_position(items.get(sh.position_item))
        if v is not None:
            # Lese-Kanal (HmIP: 5-min-Raster) hinkt nach einem Befehl → bis zur Rückprüfung nicht verwenden
            pending = bool(cmd and cmd["status"] == "pending")
            return Position(v, not pending, "item:" + sh.position_item)
        fallback_note = f"{sh.position_item} ohne Wert – Position aus dem Steuer-Item"
    v = norm_position(items.get(sh.item))
    reliable = position_reliable(cmd, now)
    note = fallback_note
    if not reliable and cmd and cmd["command"] == "stop":
        note = (note + " · " if note else "") + \
            f"■ Stop {_hm(cmd['sent_at'])} – angezeigte Position evtl. ungenau (openHAB-autoupdate)"
    # Eigene Quelle ausgefallen → das Steuer-Item ist für dieses Gerät bekanntermaßen unzuverlässig
    return Position(v, reliable and fallback_note is None, "item", None, note)


def _note_mismatch(cmd: dict[str, Any], position: Optional[int], msg: str, now: datetime) -> None:
    """Abweichung nach einer Zeitplan-Fahrt in den Zeitplan-Verlauf (reines Protokoll)."""
    if str(cmd.get("source") or "").startswith("Zeitplan"):
        import schedule_log
        schedule_log.record(schedule_log.SHUTTER, None, cmd["shutter_id"], "readback_mismatch", now,
                            {"expected": cmd.get("target"), "reported": position}, msg)


def verify(cmd: dict[str, Any], position: Optional[int], reachable: bool, now: datetime) -> dict[str, Any]:
    """Rückprüfung eines Befehls (nur wenn fällig); gibt den ggf. aktualisierten Datensatz zurück."""
    if cmd["status"] == "mismatch" and position is not None and cmd["target"] is not None \
            and abs(position - cmd["target"]) <= VERIFY_TOLERANCE:
        _update(cmd["id"], "ok", position, None)          # Gerät meldet inzwischen das Ziel
        return {**cmd, "status": "ok", "reported": position, "message": None}
    if cmd["status"] != "pending" or not cmd["verify_at"] or now < datetime.fromisoformat(cmd["verify_at"]):
        return cmd
    if position is None:
        if now - datetime.fromisoformat(cmd["sent_at"]) < timedelta(minutes=VERIFY_GIVE_UP_MIN):
            return cmd                                      # openHAB gerade weg: später noch einmal
        msg = f"Rückmeldung fehlt (Befehl {_hm(cmd['sent_at'])}{'' if reachable else ', openHAB nicht erreichbar'})"
        _update(cmd["id"], "mismatch", None, msg)
        _note_mismatch(cmd, None, msg, now)
        return {**cmd, "status": "mismatch", "message": msg}
    if abs(position - cmd["target"]) <= VERIFY_TOLERANCE:
        _update(cmd["id"], "ok", position, None)
        return {**cmd, "status": "ok", "reported": position, "message": None}
    msg = f"Rückmeldung {position} % statt {cmd['target']} % (Befehl {_hm(cmd['sent_at'])})"
    _update(cmd["id"], "mismatch", position, msg)
    _note_mismatch(cmd, position, msg, now)
    log.warning("Rollladen %s: %s", cmd["shutter_id"], msg)
    return {**cmd, "status": "mismatch", "reported": position, "message": msg}


def verify_with(cmd: dict[str, Any], pos: Position, reachable: bool, now: datetime) -> dict[str, Any]:
    """Rückprüfung mit der Ist-Position: während der Fahrt (Shelly) noch nicht; Steuer-Item zählt nur,
    wenn es keine eigene Quelle gibt (bzw. diese ausfällt)."""
    if pos.moving:
        return cmd
    return verify(cmd, pos.value, reachable, now)


# ── Status ──────────────────────────────────────────────────────────────────


def _read_items(cfg: ShutterConfig, client: OpenHabClient) -> tuple[dict[str, dict], bool, dict[str, str]]:
    """Alle Items in einem Request; schlägt er fehl, einzeln (ein toter Rollladen blockiert nichts)."""
    names = [n for s in cfg.rooms for n in s.items()] + ([cfg.duty_cycle_item] if cfg.duty_cycle_item else [])
    try:
        return client.get_items(names), True, {}
    except OpenHabError as e:
        log.warning("shutters: Sammelabfrage fehlgeschlagen (%s), lese einzeln", e)
    items: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for n in names:
        try:
            items[n] = client.get_item(n)
        except OpenHabError as e:
            errors[n] = str(e)
    return items, bool(items), errors


def shutter_status(sh: Shutter, items: dict[str, dict], cmd: Optional[dict], now: datetime,
                   error: Optional[str] = None, pos: Optional[Position] = None) -> dict[str, Any]:
    pos = pos or resolve_position(sh, items, cmd, now)
    contact = norm_contact(items.get(sh.contact_item)) if sh.contact_item else None
    missing = [n for n in sh.items() if n not in items]
    pending = None
    alert = None
    uncertain = pos.note
    if cmd:
        if cmd["status"] == "pending":
            pending = {"command": cmd["command"], "target": cmd["target"], "sent_at": cmd["sent_at"],
                       "verify_at": cmd["verify_at"]}
        elif cmd["status"] == "mismatch":
            alert = cmd["message"]
    return {
        "id": sh.id,
        "name": sh.name,
        "group": sh.group,
        "is_door": sh.is_door,
        "has_contact": bool(sh.contact_item),
        "contact": contact,
        "door_guard": door_guard(sh, contact),
        "automation": sh.automation,
        "device": sh.device,
        "can_position": sh.can_position,
        "position": pos.value,
        "position_reliable": pos.reliable,
        "position_origin": pos.origin,
        "moving": pos.moving,
        "pending": pending,
        "uncertain": uncertain,
        "alert": alert,
        "error": error or (f"Items fehlen in openHAB: {', '.join(missing)}" if missing else None),
    }


def read_status(cfg: ShutterConfig, client: Optional[OpenHabClient], now: Optional[datetime] = None) -> dict[str, Any]:
    if client is None:
        return {"ok": False, "error": "openhab.url fehlt in config.json", "rooms": [], "duty_cycle": None}
    now = now or datetime.now(TZ)
    items, ok, errors = _read_items(cfg, client)
    try:
        cmds = last_commands()
    except sqlite3.Error as e:              # DB darf die Anzeige nicht blockieren
        log.warning("shutters: Befehls-DB nicht lesbar: %s", e)
        cmds = {}
    rooms = []
    for sh in cfg.rooms:
        cmd = cmds.get(sh.id)
        shelly = None
        if sh.position_source == "shelly":
            try:
                shelly = read_shelly(sh.shelly_url)
            except ShellyError as e:
                log.warning("shutters: %s", e)
                shelly = e
        pos = resolve_position(sh, items, cmd, now, shelly)
        if cmd:
            try:
                cmd = verify_with(cmd, pos, ok, now)
            except sqlite3.Error as e:
                log.warning("shutters: Rückprüfung nicht gespeichert: %s", e)
        pos = resolve_position(sh, items, cmd, now, shelly)      # Rückprüfung kann „pending“ beendet haben
        err = None
        if sh.item in errors:
            err = f"openHAB nicht erreichbar: {errors[sh.item]}"
        rooms.append(shutter_status(sh, items, cmd, now, err, pos))
    dc = None
    if cfg.duty_cycle_item:
        v = norm_duty_cycle(items.get(cfg.duty_cycle_item))
        dc = {"value": v, "max": cfg.duty_cycle_max, "high": v is not None and v > cfg.duty_cycle_max}
    return {"ok": ok, "error": None if ok else "openHAB nicht erreichbar", "rooms": rooms, "duty_cycle": dc}


# ── Fahren ──────────────────────────────────────────────────────────────────


def to_openhab(command: str, position: Optional[int]) -> str:
    if command == "position":
        return str(int(position))
    return command.upper()


def validate_command(sh: Shutter, command: str, position: Any = None) -> Optional[int]:
    """Prüft Befehl/Position; gibt die Zielposition zurück (None bei STOP)."""
    if command not in COMMANDS:
        raise ValueError(f"Befehl {command!r} unbekannt")
    if command == "position":
        if not sh.can_position:
            raise ValueError(f"{sh.name} kann keine Position anfahren")
        if not isinstance(position, (int, float)) or isinstance(position, bool) or not 0 <= position <= 100:
            raise ValueError("Position muss 0–100 sein")
        return int(round(position))
    return target_of(command)


_group_lock = threading.Lock()


def send_sequence(cfg: ShutterConfig, jobs: list[tuple[Shutter, Callable[[], dict[str, Any]]]],
                  sleep: Optional[Callable[[float], None]] = None) -> list[dict[str, Any]]:
    """Mehrere Befehle gestaffelt (``command_spacing_s`` zwischen zwei echten Sendungen, Funk-Duty-Cycle).
    Immer nur eine Folge gleichzeitig; läuft schon eine, wirft RuntimeError."""
    sleep = sleep or _time.sleep
    if not _group_lock.acquire(blocking=False):
        raise RuntimeError("Es läuft schon ein Gruppenbefehl")
    try:
        out = []
        sent_any = False
        for _sh, fn in jobs:
            if sent_any:
                sleep(cfg.command_spacing_s)
            res = fn()
            sent_any = sent_any or not res.get("dry_run")
            out.append(res)
        return out
    finally:
        _group_lock.release()


def send_group(cfg: ShutterConfig, members: list[Shutter], command: str,
               send_one: Callable[[Shutter], dict[str, Any]], contacts: dict[str, Optional[str]],
               sleep: Optional[Callable[[float], None]] = None) -> list[dict[str, Any]]:
    """Gruppenbefehl gestaffelt. Türen, deren Schließen eine Bestätigung bräuchte, werden bei „zu“
    übersprungen. Läuft schon ein Gruppenbefehl, wirft RuntimeError."""
    skipped: dict[str, dict[str, Any]] = {}
    jobs = []
    for sh in members:
        why = door_guard(sh, contacts.get(sh.id)) if command == "down" else None
        if why:
            skipped[sh.id] = {"shutter_id": sh.id, "ok": True, "skipped": True, "reason": why}
        else:
            jobs.append((sh, lambda sh=sh: send_one(sh)))
    sent = iter(send_sequence(cfg, jobs, sleep))
    return [skipped[sh.id] if sh.id in skipped else next(sent) for sh in members]
