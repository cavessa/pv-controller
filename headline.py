"""Überschrift „Aktuelle Entscheidung“ aus dem Kaskaden-Ergebnis (reine Logik, keine I/O, nur Anzeige).

Quelle ist der Entscheidungs-Schnappschuss des letzten Cron-Laufs (``decisions.py``: Codes und Zahlen je
Gerät) und die Reihenfolge des aktiven Profils (``cascade_plan``). Die Regelung selbst bleibt unberührt.

- Kein Überschuss und keine Stufe aktiv → „Kein Überschuss“ + Netzbezug (bzw. Batterie deckt).
- Sonst die erste Stufe der Reihenfolge, die gerade Leistung bekommt („Batterie priorisiert“,
  „Wasserspeicher bis Min/Max“, „Wallbox“, Gerätename); bekommt keine etwas, die erste, die wartet.
- Alle Stufen voll / nicht bereit, aber Überschuss → „Einspeisung“.
Der Text enthält nur Sätze, die gerade zutreffen.
"""
from __future__ import annotations

from typing import Any, Optional

from cascade_plan import BATTERY, HEATER_MAX, HEATER_MIN

ACTIVE, WAITING, DONE = "active", "waiting", "done"
SURPLUS_MIN_W = 50            # darunter gilt „kein Überschuss“
ON_ACTIONS = ("turn_on", "keep_on", "skip_min_on")
PROFILE_DE = {"winter": "Winterprofil", "summer": "Sommerprofil"}


def _w(w: Optional[float]) -> str:
    w = float(w or 0)
    return f"{w / 1000:.2f} kW".replace(".", ",") if abs(w) >= 1000 else f"{w:.0f} W"


def _t(t: Optional[float]) -> str:
    return "?" if t is None else f"{t:.1f}".replace(".", ",") + " °C"


def _stage(key: str, devs: dict[str, dict], e: dict, temp: Optional[float], comfort: Optional[float],
           tmax: float, wallbox_connected: Optional[bool], heater_full: bool = False) -> Optional[tuple[str, str, str]]:
    """(Zustand, Name, Satz) einer Profilstufe; None = Stufe nicht vorhanden."""
    if key == BATTERY:
        d = devs.get(BATTERY)
        if d is None or d.get("reason") == "not_in_cascade":
            return None
        data, soc = d.get("data") or {}, e.get("battery_soc")
        soc_s = f" (SoC {soc} %)" if soc is not None else ""
        ch = e.get("battery_charge_w") or data.get("limit_w") or 0
        if d.get("action") == "charge" and ch > SURPLUS_MIN_W:
            return ACTIVE, "Batterie priorisiert", f"Batterie lädt mit {_w(ch)} aus PV-Überschuss{soc_s}."
        if d.get("reason") == "soc_target_reached":
            return DONE, "Batterie", f"Batterie voll{soc_s}."
        return WAITING, "Batterie", f"Batterie lädt noch nicht{soc_s}."
    if key in (HEATER_MIN, HEATER_MAX):
        d = devs.get("heizstab")
        if d is None:
            return None
        data = d.get("data") or {}
        below_min = comfort is not None and temp is not None and temp < comfort
        if key == HEATER_MIN:
            name = f"Wasserspeicher bis {comfort:.0f} °C" if comfort is not None else "Wasserspeicher bis Min"
            if not below_min:
                return DONE, name, ""
        else:
            name = f"Wasserspeicher bis Max {tmax:.0f} °C"
            if below_min:
                return DONE, name, ""                     # Heizstab zählt gerade zur Stufe „bis Min“
            if heater_full:
                return DONE, name, f"Wasserspeicher praktisch voll (Thermostat im Heizstab hat abgeschaltet, {_t(temp)})."
            if d.get("reason") in ("max_temp_reached", "storage_practically_full") or (
                    temp is not None and temp >= tmax):
                return DONE, name, f"Wasserspeicher voll ({_t(temp)}, Max {tmax:.0f} °C)."
            if d.get("reason") == "hysteresis_band":
                return DONE, name, (f"Wasserspeicher {_t(temp)} – kein neuer Heizzyklus bis zur "
                                    f"Wiedereinschalt-Temperatur.")
        actual = data.get("actual_w") or e.get("heater_w") or 0
        phases = data.get("target_phases") or 0
        if actual > SURPLUS_MIN_W or phases > 0:
            ph = f", {phases} Phase(n)" if phases else ""
            return ACTIVE, name, f"Heizstab heizt mit {_w(actual)}{ph}; Speicher {_t(temp)}, Ziel {name.split('bis ')[-1]}."
        thr = data.get("threshold_w")
        avail = data.get("budget_w", data.get("raw_w"))
        why = (f" – Überschuss {_w(avail)} unter der Startschwelle {_w(thr)}"
               if thr and avail is not None else "")
        return WAITING, name, f"Heizstab wartet{why}; Speicher {_t(temp)}."
    d = devs.get(key)
    if d is None:
        return None
    name = d.get("name") or key
    if d.get("type") == "wallbox":
        name = "Wallbox"
        if wallbox_connected is False:
            return DONE, name, ""
    if d.get("reason") in ("device_unreachable", "auto_disabled"):
        return DONE, name, f"{name} nicht erreichbar."
    data = d.get("data") or {}
    if d.get("action") in ON_ACTIONS:
        pw = data.get("power_w")
        return ACTIVE, name, f"{name} läuft" + (f" ({_w(pw)})." if pw else ".")
    thr = data.get("threshold_w")
    rest = data.get("rest_w")
    why = f": Überschuss {_w(rest)} unter {_w(thr)}" if thr and rest is not None else ""
    return WAITING, name, f"{name} wartet{why}."


def cascade_headline(snap: dict[str, Any], order: list[str], temp: Optional[float], comfort: Optional[float],
                     tmax: float, heater_full: bool = False) -> dict[str, Any]:
    """Überschrift, Text und Profil aus einem Entscheidungs-Schnappschuss."""
    e = snap.get("energy") or {}
    devs = {d["id"]: d for d in snap.get("devices") or []}
    wb = devs.get("wallbox_control")
    wb_conn = None if wb is None else wb.get("reason") != "not_connected"
    surplus = e.get("surplus_w") or 0
    grid = e.get("grid_w")
    stages = [s for k in order if (s := _stage(k, devs, e, temp, comfort, tmax, wb_conn, heater_full)) is not None]
    profile = PROFILE_DE.get(snap.get("profile"), snap.get("profile") or "")

    active = [s for s in stages if s[0] == ACTIVE]
    waiting = [s for s in stages if s[0] == WAITING]
    sentences: list[str] = []
    if active:
        state = active[0][1]
        sentences += [s[2] for s in active if s[2]]
        if surplus > SURPLUS_MIN_W and waiting:
            sentences.append(waiting[0][2])
    elif surplus <= SURPLUS_MIN_W:
        state = "Kein Überschuss"
        if grid is not None and grid > SURPLUS_MIN_W:
            sentences.append(f"Netzbezug {_w(grid)}.")
        dis = e.get("battery_discharge_w") or 0
        soc = e.get("battery_soc")
        bat = devs.get(BATTERY) or {}
        if dis > SURPLUS_MIN_W:
            sentences.append(f"Batterie entlädt {_w(dis)} und deckt den Grundbedarf"
                             + (f" (SoC {soc} %)." if soc is not None else "."))
        elif bat.get("reason") == "battery_empty":
            sentences.append(f"Batterie leer (SoC {soc} %)." if soc is not None else "Batterie leer.")
        full = [s[2] for s in stages if s[0] == DONE and s[2]]
        sentences += full[:1]
    elif waiting:
        state = f"{waiting[0][1]} wartet"
        sentences.append(f"Überschuss {_w(surplus)}.")
        sentences.append(waiting[0][2])
    else:
        state = "Einspeisung"
        sentences.append(f"Alle Stufen voll oder nicht bereit – {_w(surplus)} gehen ins Netz.")
        sentences += [s[2] for s in stages if s[0] == DONE and s[2]][:2]
    return {"state": state, "reason": " ".join(s for s in sentences if s), "severity": "ok", "profile": profile}
