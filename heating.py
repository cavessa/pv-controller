"""Heizung: Raum-Thermostate aus openHAB anzeigen und Sollwerte setzen.

Config (config.json):
  "openhab": {"url": "http://…:8080", "token": "oh.…", "timeout_s": 3},
  "heating": {"enabled": true, "setpoint_min": 5, "setpoint_max": 26,
              "rooms": [{"id", "name", "setpoint_item", "temp_item",
                         "valve_item", "battery_item", "humidity_item", "mode_item"}]}

Nur id/name/temp_item sind Pflicht. Ein Raum ohne setpoint_item ist eine reine
Anzeige (z. B. Waschküche: Ist + Feuchte), er bekommt weder Buttons noch Zeitpläne.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from clients.openhab_client import OpenHabClient, OpenHabError

log = logging.getLogger("pv-controller.heating")

ITEM_KEYS = ("setpoint_item", "temp_item", "valve_item", "battery_item", "humidity_item", "mode_item")
_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
SETPOINT_STEP = 0.5


@dataclass
class Room:
    id: str
    name: str
    temp_item: str
    setpoint_item: str = ""
    valve_item: str = ""
    battery_item: str = ""
    humidity_item: str = ""
    mode_item: str = ""

    @property
    def controllable(self) -> bool:
        return bool(self.setpoint_item)

    def items(self) -> list[str]:
        return [getattr(self, k) for k in ITEM_KEYS if getattr(self, k)]


@dataclass
class HeatingConfig:
    enabled: bool = False
    setpoint_min: float = 5.0
    setpoint_max: float = 26.0
    rooms: list[Room] = field(default_factory=list)

    def room(self, room_id: str) -> Optional[Room]:
        return next((r for r in self.rooms if r.id == room_id), None)


def parse_heating(raw: dict) -> HeatingConfig:
    """heating-Block aus der rohen Config; wirft ValueError bei ungültigen Werten."""
    h = raw.get("heating") or {}
    if not isinstance(h, dict):
        raise ValueError("heating must be an object")
    smin = h.get("setpoint_min", 5)
    smax = h.get("setpoint_max", 26)
    for k, v in (("setpoint_min", smin), ("setpoint_max", smax)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 4.5 <= v <= 30.5:
            raise ValueError(f"heating.{k} must be a number between 4.5 and 30.5")
    if smin >= smax:
        raise ValueError("heating.setpoint_min must be below setpoint_max")
    rooms: list[Room] = []
    seen: set[str] = set()
    for i, r in enumerate(h.get("rooms") or []):
        if not isinstance(r, dict):
            raise ValueError(f"heating.rooms[{i}] must be an object")
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID_RE.match(rid):
            raise ValueError(f"heating.rooms[{i}].id must match [a-z0-9_-]{{1,32}}")
        if rid in seen:
            raise ValueError(f"heating.rooms: duplicate id {rid!r}")
        seen.add(rid)
        name = r.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"heating.rooms[{i}].name must be a non-empty string")
        kw = {}
        for k in ITEM_KEYS:
            v = r.get(k) or ""
            if not isinstance(v, str):
                raise ValueError(f"heating.rooms[{i}].{k} must be a string")
            kw[k] = v.strip()
        if not kw["temp_item"]:
            raise ValueError(f"heating.rooms[{i}].temp_item is required")
        rooms.append(Room(id=rid, name=name.strip(), **kw))
    return HeatingConfig(enabled=bool(h.get("enabled", False)), setpoint_min=float(smin),
                         setpoint_max=float(smax), rooms=rooms)


def make_client(raw: dict) -> Optional[OpenHabClient]:
    oh = raw.get("openhab") or {}
    if not oh.get("url"):
        return None
    return OpenHabClient(oh["url"], timeout=float(oh.get("timeout_s", 3)), token=oh.get("token", ""))


# ── Zustände normalisieren ──────────────────────────────────────────────────


def _num(state: Any) -> Optional[float]:
    """'17.5 °C' / '0.5' / '59' → float; NULL/UNDEF/leer → None."""
    if state is None:
        return None
    s = str(state).strip()
    if s in ("", "NULL", "UNDEF"):
        return None
    try:
        return float(s.split(" ")[0])
    except ValueError:
        return None


def norm_temp(item: Optional[dict]) -> Optional[float]:
    v = _num(item.get("state")) if item else None
    return round(v, 2) if v is not None else None


def norm_percent(item: Optional[dict]) -> Optional[float]:
    """Feuchte/Ventil in Prozent: Anteil (0..1, Dimensionless) → ×100, sonst als Prozent.

    Number:Dimensionless kommt in openHAB als Anteil (0.5 = 50 %), Dimmer und
    einfache Number-Items als Prozentzahl (59 = 59 %). Ein Wert mit '%' im State ist
    immer schon Prozent.
    """
    if not item:
        return None
    raw = str(item.get("state", ""))
    v = _num(raw)
    if v is None:
        return None
    if "%" not in raw and str(item.get("type", "")).startswith("Number:Dimensionless") and v <= 1.0:
        v *= 100
    return round(v, 1)


def norm_battery(item: Optional[dict]) -> Optional[dict]:
    """Batterie: Switch (LOW_BAT) → ok/low; Zahl → Prozent (unter 20 % = low)."""
    if not item:
        return None
    s = str(item.get("state", "")).strip()
    if s in ("", "NULL", "UNDEF"):
        return None
    if s in ("ON", "OFF", "OPEN", "CLOSED"):
        return {"low": s in ("ON", "OPEN"), "percent": None}
    v = _num(s)
    if v is None:
        return None
    return {"low": v < 20, "percent": round(v)}


_MODE_NUM = {0: "AUTO", 1: "MANU", 2: "PARTY", 3: "BOOST"}


def norm_mode(item: Optional[dict]) -> Optional[str]:
    """'AUTO-MODE'/'MANU-MODE' (HM) bzw. 0/1/2 (HmIP SET_POINT_MODE) → AUTO/MANU/PARTY/BOOST."""
    if not item:
        return None
    s = str(item.get("state", "")).strip().upper()
    if s in ("", "NULL", "UNDEF"):
        return None
    for key in ("AUTO", "MANU", "PARTY", "BOOST"):
        if key in s:
            return key
    v = _num(s)
    if v is not None and int(v) == v:
        return _MODE_NUM.get(int(v))
    return None


# ── Status / Schalten ───────────────────────────────────────────────────────


def room_status(room: Room, items: dict[str, dict], error: Optional[str] = None) -> dict[str, Any]:
    def it(key: str) -> Optional[dict]:
        name = getattr(room, key)
        return items.get(name) if name else None

    missing = [getattr(room, k) for k in ITEM_KEYS if getattr(room, k) and getattr(room, k) not in items]
    return {
        "id": room.id,
        "name": room.name,
        "controllable": room.controllable,
        "temp": norm_temp(it("temp_item")),
        "setpoint": norm_temp(it("setpoint_item")),
        "valve": norm_percent(it("valve_item")),
        "humidity": norm_percent(it("humidity_item")),
        "battery": norm_battery(it("battery_item")),
        "mode": norm_mode(it("mode_item")),
        "has": {k.replace("_item", ""): bool(getattr(room, k)) for k in ITEM_KEYS},
        "error": error or (f"Items fehlen in openHAB: {', '.join(missing)}" if missing else None),
    }


def read_status(cfg: HeatingConfig, client: Optional[OpenHabClient]) -> dict[str, Any]:
    """Status aller Räume. Ein einziger Request für alle Items; schlägt er fehl,
    wird je Raum einzeln gelesen, damit ein toter Raum die anderen nicht blockiert."""
    if client is None:
        return {"ok": False, "error": "openhab.url fehlt in config.json", "rooms": []}
    names = [n for r in cfg.rooms for n in r.items()]
    try:
        items = client.get_items(names)
        rooms = [room_status(r, items) for r in cfg.rooms]
        return {"ok": True, "error": None, "rooms": rooms}
    except OpenHabError as e:
        log.warning("heating: Sammelabfrage fehlgeschlagen (%s), lese je Raum", e)
    rooms = []
    ok_any = False
    for r in cfg.rooms:
        items: dict[str, dict] = {}
        err = None
        for n in r.items():
            try:
                items[n] = client.get_item(n)
                ok_any = True
            except OpenHabError as e:
                err = f"openHAB nicht erreichbar: {e}"
        rooms.append(room_status(r, items, None if items else err))
    return {"ok": ok_any, "error": None if ok_any else "openHAB nicht erreichbar", "rooms": rooms}


def validate_setpoint(cfg: HeatingConfig, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Sollwert muss eine Zahl sein")
    v = float(value)
    if not cfg.setpoint_min <= v <= cfg.setpoint_max:
        raise ValueError(f"Sollwert {v:g} °C außerhalb {cfg.setpoint_min:g}–{cfg.setpoint_max:g} °C")
    if abs(v / SETPOINT_STEP - round(v / SETPOINT_STEP)) > 1e-6:
        raise ValueError(f"Sollwert muss in {SETPOINT_STEP:g}-°C-Schritten sein")
    return v


def set_setpoint(cfg: HeatingConfig, client: OpenHabClient, room_id: str, value: Any) -> float:
    room = cfg.room(room_id)
    if room is None:
        raise KeyError(room_id)
    if not room.controllable:
        raise ValueError(f"{room.name} hat keinen Sollwert (nur Anzeige)")
    v = validate_setpoint(cfg, value)
    client.send_command(room.setpoint_item, f"{v:g}")
    return v


def downsample(points: list[tuple[int, float]], bucket_ms: int) -> list[list[float]]:
    """Mittelwert je Zeitfenster → [[t_ms, wert], …] (24 h × 1/min → ~144 Punkte)."""
    out: list[list[float]] = []
    cur, acc = None, []
    for t, v in points:
        b = t - t % bucket_ms
        if cur is not None and b != cur:
            out.append([cur, round(sum(acc) / len(acc), 2)])
            acc = []
        cur = b
        acc.append(v)
    if acc:
        out.append([cur, round(sum(acc) / len(acc), 2)])
    return out
