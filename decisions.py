"""Entscheidungs-Schnappschuss und Geräte-Ereignislog (reines Protokoll).

Der Cron-Lauf (``main.py``) schreibt nach Kaskade und Controller **eine** JSON-Zeile je Lauf in
``decision_snapshot``: Messwerte, Profil, Fail-safe und je Gerät Aktion + Grund-Code (``reasons.py``)
mit Zahlen. Aufbewahrung 48 h (``db.prune``). Daneben ``device_health_event``: ein Ereignis, wenn ein
Gerät ausfällt (``offline_ab``) oder wiederkommt (``wieder_da``), 30 Tage.

Nichts hier entscheidet oder schaltet. ``record_run`` fängt alle Fehler ab – ein Problem beim
Protokollieren darf den Lauf nie stören. In den Schnappschuss kommen keine Adressen und keine
Zugangsdaten, nur Geräte-IDs, Namen und Zahlen.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

import db
import reasons
import redact
import suntime
from cascade_plan import BATTERY
from models import ControllerResult, PhaseAction

log = logging.getLogger(__name__)

OFFLINE, ONLINE = "offline_ab", "wieder_da"
PERGOLA_DARK_W = 100          # Haupt-WR unter 100 W: die Pergola darf ohne Sonne aus sein
OPENHAB_CHECK_MIN = 5         # openHAB-Erreichbarkeit alle 5 min prüfen …
OPENHAB_CHECK_TIMEOUT_S = 2.0 # … mit kurzem Timeout (der Cron-Lauf darf nicht hängen)
# Zahlen, die für alle drei Heizstab-Phasen gleich sind: einmal in snap["phases"]
PHASE_SHARED = ("budget_w", "budget_down_w", "target_phases", "running", "threshold_w", "rest_w")
_initialized: set[str] = set()

# Geräte-IDs im Ereignislog → Name für Anzeige/Chat (Kaskaden-Shellys: "cascade:<id>" mit eigenem Namen)
DEVICE_NAMES = {
    "solax": "Wechselrichter (Solax)", "pergola": "Pergola-Wechselrichter", "main_meter": "Hauptzähler",
    "heater_meter": "Heizstab-Zähler", "storage_temp": "Speichertemperatur", "ph1": "Heizstab Phase 1",
    "ph2": "Heizstab Phase 2", "ph3": "Heizstab Phase 3", "battery": "Batterie", "wallbox": "Wallbox",
    "openhab": "openHAB",
}


# Readings.CRITICAL_ERRORS → Name (für den Fail-safe-Text)
CRITICAL_NAMES = {"pv_power": "Wechselrichter", "main_meter": "Hauptzähler"}


def init_tables() -> None:
    _initialized.add(str(db.DB_PATH))
    with sqlite3.connect(db.DB_PATH, timeout=30) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS decision_snapshot (ts TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_health_event (
                id        INTEGER PRIMARY KEY,
                ts        TEXT NOT NULL,
                device_id TEXT NOT NULL,
                name      TEXT,
                event     TEXT NOT NULL,              -- offline_ab | wieder_da
                expected  INTEGER NOT NULL DEFAULT 0, -- 1 = erwartet offline (Pergola ohne Sonne)
                detail    TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_device_health_event_ts ON device_health_event (ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_device_health_event_dev ON device_health_event (device_id, id)")


def _conn() -> sqlite3.Connection:
    if str(db.DB_PATH) not in _initialized:
        init_tables()
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(suntime.TZ).replace(microsecond=0)


# ── Geräte-Ereignislog ──────────────────────────────────────────────────────


def _last_events(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM device_health_event WHERE id IN "
                        "(SELECT MAX(id) FROM device_health_event GROUP BY device_id)")
    return {r["device_id"]: r for r in rows}


def health_update(states: dict[str, dict[str, Any]], now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Erreichbarkeit fortschreiben; nur **Wechsel** erzeugen ein Ereignis.

    ``states``: {device_id: {"ok": bool, "name": str, "expected": bool, "detail": str}}.
    Ein Gerät, das „erwartet offline“ war und es nicht mehr sein dürfte (Pergola bei Sonne), bekommt
    ein zweites ``offline_ab`` ohne ``expected``. Liefert die geschriebenen Ereignisse.
    """
    now = now or _now()
    ts = now.isoformat(timespec="seconds")
    out = []
    with _conn() as conn:
        last = _last_events(conn)
        for dev, st in states.items():
            prev = last.get(dev)
            was_off = prev is not None and prev["event"] == OFFLINE
            expected = bool(st.get("expected"))
            if st["ok"]:
                if not was_off:
                    continue
                ev = {"event": ONLINE, "expected": bool(prev["expected"]), "detail": st.get("detail")}
            elif not was_off:
                ev = {"event": OFFLINE, "expected": expected, "detail": st.get("detail")}
            elif prev["expected"] and not expected:
                ev = {"event": OFFLINE, "expected": False, "detail": "nicht mehr erwartet – weiter offline"}
            else:
                continue
            name = st.get("name") or DEVICE_NAMES.get(dev, dev)
            if ev["detail"]:
                ev["detail"] = redact.scrub_addresses(redact.redact(str(ev["detail"])))[:200]
            conn.execute("INSERT INTO device_health_event (ts, device_id, name, event, expected, detail) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (ts, dev, name, ev["event"], int(ev["expected"]), ev["detail"]))
            out.append({"ts": ts, "device_id": dev, "name": name, **ev})
    return out


def note_health(device_id: str, ok: bool, now: Optional[datetime] = None, detail: Optional[str] = None) -> None:
    """Einzelnes Gerät fortschreiben (z. B. openHAB aus den Zeitplan-Läufen); wirft nie."""
    try:
        health_update({device_id: {"ok": ok, "detail": detail}}, now)
    except Exception:
        log.debug("Geräte-Ereignis nicht geschrieben", exc_info=True)


def offline_devices() -> list[dict[str, Any]]:
    """Aktuell ausgefallene Geräte: [{id, name, since, expected}]."""
    with _conn() as conn:
        last = _last_events(conn)
    return [{"id": d, "name": r["name"], "since": r["ts"], "expected": bool(r["expected"])}
            for d, r in sorted(last.items()) if r["event"] == OFFLINE]


def health_events(since: Optional[datetime] = None, limit: int = 200) -> list[dict[str, Any]]:
    """Ereignisse, neueste zuerst."""
    with _conn() as conn:
        if since is None:
            rows = conn.execute("SELECT * FROM device_health_event ORDER BY id DESC LIMIT ?", (int(limit),))
        else:
            rows = conn.execute("SELECT * FROM device_health_event WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                                (since.isoformat(timespec="seconds"), int(limit)))
        return [dict(r) for r in rows]


def pergola_expected_offline(pv1_w: Optional[float], now: datetime, loc: Optional[tuple[float, float]]) -> bool:
    """Darf der Pergola-Wechselrichter gerade aus sein? Ja ohne Sonne: Haupt-WR liefert (fast) nichts
    oder – wenn der Haupt-WR selbst nicht lesbar ist – es ist Nacht (Sonnenauf-/-untergang)."""
    if pv1_w is not None:
        return pv1_w < PERGOLA_DARK_W
    if loc is None:
        return False
    rise, sset = suntime.sun_times(now.astimezone(suntime.TZ).date(), *loc)
    if rise is None or sset is None:
        return False
    return not (suntime.utc(rise) <= suntime.utc(now) <= suntime.utc(sset))


def collect_health(config: Any, result: ControllerResult, now: datetime,
                   loc: Optional[tuple[float, float]] = None) -> dict[str, dict[str, Any]]:
    """Erreichbarkeit aller Geräte aus dem Controller-Lauf und der Kaskaden-Tabelle ableiten."""
    r = result.readings
    states: dict[str, dict[str, Any]] = {}
    if getattr(config.simulation, "enabled", False):
        return states
    states["solax"] = {"ok": r.pv1_power_w is not None}
    if config.solax2_active:
        expected = pergola_expected_offline(r.pv1_power_w, now, loc)
        states["pergola"] = {"ok": r.pv2_power_w is not None, "expected": expected,
                             "detail": "erwartet offline (keine Sonne)" if expected else None}
    states["main_meter"] = {"ok": r.main_meter_power_w is not None}
    substitute = r.heater_power_source != "meter"
    states["heater_meter"] = {"ok": not substitute,
                              "detail": "Ersatzwert aus den Phasen" if substitute else None}
    states["storage_temp"] = {"ok": r.storage_temp_c is not None}
    for i in (1, 2, 3):
        if f"ph{i}" not in r.disabled_phases:
            states[f"ph{i}"] = {"ok": f"ph{i}" not in r.unreachable_phases}
    if config.zendure_active:
        states["battery"] = {"ok": r.zendure is not None}
    if result.controller_active and config.wallbox.enabled and config.wallbox.url:
        states["wallbox"] = {"ok": result.wallbox_status is not None}
    for d in db.get_cascade_devices():
        if d["type"] in ("shelly_gen1", "shelly_gen2") and d["enabled"] and d.get("ip_address"):
            n = int(d.get("consecutive_errors") or 0)
            states[f"cascade:{d['id']}"] = {"ok": n == 0, "name": d["name"],
                                            "detail": f"{n} Fehler in Folge" if n else None}
    return states


# ── Schnappschuss ───────────────────────────────────────────────────────────


def _i(v: Any) -> Optional[int]:
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError, OverflowError):
        return None


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if v is not None}


def _entry(id_: str, action: str, code: str, data: Optional[dict] = None, **extra: Any) -> dict[str, Any]:
    e: dict[str, Any] = {"id": id_, "action": action, "reason": code}
    e.update(_clean(extra))
    data = _clean(data)
    if data:
        e["data"] = data
    return e


def _phase_action(d: Any) -> str:
    if d.action is PhaseAction.TURN_ON:
        return "turn_on"
    if d.action is PhaseAction.TURN_OFF:
        return "turn_off"
    return "keep_on" if d.current_state else "keep_off" if d.current_state is False else "unknown"


def build(config: Any, cascade: Optional[dict], result: Optional[ControllerResult],
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Schnappschuss eines Laufs aus dem Kaskaden-Ergebnis und dem Controller-Ergebnis bauen."""
    now = now or _now()
    cascade = cascade or {}
    snap: dict[str, Any] = {"ts": now.isoformat(timespec="seconds")}
    r = result.readings if result is not None else None
    zd = getattr(r, "zendure", None) if r is not None else None
    wb = result.wallbox_status if result is not None else None
    energy = {
        "pv_w": _i(r.pv_power_w) if r else None,
        "pv_pergola_w": _i(r.pv2_power_w) if r else None,
        "grid_w": _i(r.main_meter_power_w) if r else None,          # + Netzbezug, − Einspeisung
        "house_w": _i(r.house_consumption_w) if r else None,
        "heater_w": _i(r.heater_meter_power_w) if r else None,
        "wallbox_w": _i(wb.power_w) if wb else None,
        "whirlpool_w": _i(r.whirlpool_power_w) if r is not None and r.whirlpool_reachable else None,
        "whirlpool_uncertain": bool(r.whirlpool_uncertain) if r is not None else False,
        "battery_charge_w": _i(zd.charge_w) if zd is not None else None,
        "battery_discharge_w": _i(zd.discharge_w) if zd is not None else None,
        "battery_soc": _i(zd.soc) if zd is not None else None,
        "surplus_w": _i(cascade.get("surplus_watts")) if "surplus_watts" in cascade and not cascade.get("disabled")
        else None,
        "surplus_effective_w": _i(cascade.get("effective_surplus_w")),
        "storage_temp_c": None if r is None or r.storage_temp_c is None else round(r.storage_temp_c, 1),
    }
    snap["energy"] = _clean(energy)
    snap["profile"] = cascade.get("profile") or ("summer" if config.summer_mode.enabled else "winter")
    if result is not None and result.dry_run:
        snap["dry_run"] = True
    if cascade.get("disabled"):
        snap["cascade"] = "cascade_disabled"
    if result is None:
        snap["controller"] = "error"
    elif not result.controller_active:
        snap["controller"] = "controller_disabled"

    critical = list(r.critical_errors) if r is not None else []
    names = [CRITICAL_NAMES.get(c, c) for c in critical]
    snap["failsafe"] = {"reason": "failsafe_global", "unreadable": critical, "names": names} if critical else None

    devices: list[dict[str, Any]] = []
    seen = set()
    for d in cascade.get("devices") or []:
        seen.add(d["id"])
        data = dict(d.get("data") or {})
        if "budget_w" in data and data.get("rest_w") == data["budget_w"]:
            data.pop("rest_w")                        # gleich dem Budget: device_data ergänzt es beim Lesen
        devices.append(_entry(d["id"], d.get("action") or "none", d.get("code") or "", data,
                              name=d.get("name"), type=d.get("type"), on=bool(d.get("is_on"))))
    if BATTERY not in seen:
        devices.insert(0, _entry(BATTERY, "none", "not_in_cascade", name="Batterie", type="zendure"))
    if result is not None:
        # Budget-Zahlen der Phasen stehen einmal in snap["phases"], nicht in jeder Phase (device_data fügt sie
        # beim Lesen wieder zusammen); an der Phase bleibt nur, was sie selbst betrifft (z. B. remaining_s).
        shared: dict[str, Any] = {}
        for d in result.decisions:
            for k in PHASE_SHARED:
                if k in (d.data or {}) and k not in shared:
                    shared[k] = d.data[k]
        if shared.get("rest_w") == shared.get("budget_w"):
            shared.pop("rest_w", None)
        if critical:
            shared["unreadable"] = names
        if shared:
            snap["phases"] = _clean(shared)
        for i, d in enumerate(result.decisions, start=1):
            data = {k: v for k, v in (d.data or {}).items() if k not in PHASE_SHARED or shared.get(k) != v}
            data.pop("rest_w", None) if data.get("rest_w") == shared.get("budget_w") else None
            devices.append(_entry(f"heizstab_phase{i}", _phase_action(d), d.code, data,
                                  error=d.execution_error, blind=True if d.blind else None))
        wd = result.wallbox_decision
        if wd is not None:
            devices.append(_entry("wallbox_control", wd.action.value, wd.code, wd.data,
                                  error=wd.execution_error))
        if result.summer_mode_heating:
            snap["summer_mode_heating"] = True
    snap["devices"] = devices

    health = []
    try:
        for o in offline_devices():
            health.append(_clean({"id": o["id"], "name": o["name"], "offline_since": o["since"],
                                  "expected": True if o["expected"] else None}))
    except sqlite3.Error:
        log.debug("Geräte-Zustand nicht lesbar", exc_info=True)
    snap["health"] = health
    if r is not None and r.heater_power_source != "meter":
        snap["substitutes"] = {"heater_w": r.heater_power_source}
    return snap


def device_data(snap: dict[str, Any], dev: dict[str, Any]) -> dict[str, Any]:
    """Zahlen eines Geräte-Eintrags; bei Heizstab-Phasen ergänzt um die gemeinsamen aus ``snap["phases"]``."""
    data = dict(dev.get("data") or {})
    if str(dev.get("id", "")).startswith("heizstab_phase"):
        data = {**(snap.get("phases") or {}), **data}
    if "rest_w" not in data and "budget_w" in data:
        data["rest_w"] = data["budget_w"]
    return data


def reason_text(snap: dict[str, Any], dev: dict[str, Any]) -> str:
    """Deutscher Kurztext zum Grund eines Geräte-Eintrags."""
    return reasons.text(dev.get("reason"), device_data(snap, dev))


def check_openhab(raw: Optional[dict], now: Optional[datetime] = None, every_min: int = OPENHAB_CHECK_MIN,
                  timeout_s: float = OPENHAB_CHECK_TIMEOUT_S, get: Any = None) -> Optional[bool]:
    """Leichter Erreichbarkeits-Check für openHAB (``GET /rest/``, kurzes Timeout), alle ``every_min`` Minuten.

    Damit landen Ausfälle auch ohne fälligen Zeitplan im Ereignislog. Nur lesen. Liefert True/False,
    None wenn nicht geprüft (falsche Minute, keine URL). Wirft nie.
    """
    try:
        now = now or _now()
        url = ((raw or {}).get("openhab") or {}).get("url")
        if not url or now.minute % max(1, int(every_min)) != 0:
            return None
        base = str(url).rstrip("/")
        rest = base if base.endswith("/rest") else base + "/rest"
        detail = None
        try:
            if get is None:
                import requests
                get = requests.get
            r = get(rest + "/", timeout=timeout_s, headers={"Accept": "application/json"})
            ok = 200 <= r.status_code < 300
            if not ok:
                detail = f"HTTP {r.status_code}"
        except Exception as e:
            ok, detail = False, type(e).__name__          # nur die Fehlerart – keine URL im Ereignislog
        note_health("openhab", ok, now, detail)
        return ok
    except Exception:
        log.debug("openHAB-Check fehlgeschlagen", exc_info=True)
        return None


def save(snap: dict[str, Any]) -> int:
    """Schnappschuss speichern (ein Eintrag je Zeitstempel). Liefert die Größe in Bytes."""
    text = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO decision_snapshot (ts, data) VALUES (?, ?)", (snap["ts"], text))
    return len(text.encode("utf-8"))


def snapshots(since: Optional[datetime] = None, until: Optional[datetime] = None,
              limit: int = 3000) -> list[dict[str, Any]]:
    """Schnappschüsse im Zeitraum, älteste zuerst."""
    lo = (since or datetime(2000, 1, 1)).isoformat(timespec="seconds")
    hi = (until or datetime(2999, 1, 1)).isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute("SELECT data FROM decision_snapshot WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
                            (lo, hi, int(limit))).fetchall()
    return [json.loads(r[0]) for r in rows]


def latest() -> Optional[dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute("SELECT data FROM decision_snapshot ORDER BY ts DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def record_run(config: Any, raw: Optional[dict], cascade: Optional[dict], result: Optional[ControllerResult],
               now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Nach einem Cron-Lauf: Geräte-Ereignisse fortschreiben und den Schnappschuss speichern. Wirft nie."""
    try:
        now = now or _now()
        check_openhab(raw, now)
        if result is not None:
            try:
                health_update(collect_health(config, result, now, suntime.location(raw or {})), now)
            except Exception:
                log.warning("Geräte-Ereignislog nicht fortgeschrieben", exc_info=True)
        snap = build(config, cascade, result, now)
        save(snap)
        return snap
    except Exception:
        log.warning("Entscheidungs-Schnappschuss nicht geschrieben", exc_info=True)
        return None
