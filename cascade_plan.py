"""Kaskaden-Profile (Sommer/Winter) und Verteilung des Überschusses (reine Logik, keine I/O).

Ein Profil ist eine Reihenfolge von Einträgen:
- ``heizstab_min``  Wasserspeicher „bis Komfort-Minimum“ (aktiv, solange Temp < comfort_min)
- ``zendure``       Batterie (stufenloses Laden)
- ``heizstab_max``  Wasserspeicher „bis Max“ (aktiv, solange Temp < storage_max_temp)
- sonst die ID eines Kaskaden-Geräts (eBike-Shelly, Wallbox …)

Defaults: Winter (Öl heizt) ``[Batterie, Wasserspeicher bis Max]``, Sommer (Öl aus)
``[Wasserspeicher bis Min, Batterie, Wasserspeicher bis Max]``; bestehende Geräte behalten
ihre Position (der Heizstab-Block ersetzt den bisherigen Heizstab-Eintrag).

Es gibt nur **einen** Heizstab; ``walk`` fasst beide Stufen zu **einem** Budget für den
Controller zusammen: Budget = Rest-Überschuss an der ersten aktiven Heizstab-Stufe minus
das, was die Batterie danach (zwischen den Stufen) aufnimmt. Eine vorgelagerte Stufe
(``heizstab_min``) beansprucht nur ihre ganzzahligen Phasen – was darunter liegt,
reicht sie an die Batterie weiter (kleiner Überschuss unter der Startschwelle).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

HEATER_MIN = "heizstab_min"
HEATER_MAX = "heizstab_max"
BATTERY = "zendure"
HEATER_ID = "heizstab"
HEATER_STAGES = (HEATER_MIN, HEATER_MAX)
SEASONS = ("summer", "winter")


def heater_block(season: str) -> list[str]:
    return [HEATER_MIN, BATTERY, HEATER_MAX] if season == "summer" else [BATTERY, HEATER_MAX]


def default_profile(season: str, device_ids_by_priority: Iterable[str]) -> list[str]:
    """Aktuelle Geräteliste (nach Priorität) → Profil; Heizstab-/Batterie-Eintrag wird zum Block."""
    out: list[str] = []
    placed = False
    for did in device_ids_by_priority:
        if did in (HEATER_ID, BATTERY):
            if not placed:
                out.extend(heater_block(season))
                placed = True
            continue
        out.append(did)
    if not placed:
        out = heater_block(season) + out
    return out


def normalize_profile(season: str, order: Optional[Iterable[str]],
                      device_ids_by_priority: Iterable[str]) -> list[str]:
    """Gespeicherte Reihenfolge bereinigen: unbekannte/doppelte Einträge entfernen, fehlende
    (neue Geräte) hinten anhängen, Heizstab-Stufen und Batterie sicherstellen.

    Winter hat nur die Stufe „bis Max“ (Öl sichert das Minimum); Sommer beide, „bis Min“ vor „bis Max“.
    """
    ids = [d for d in device_ids_by_priority if d not in (HEATER_ID, BATTERY)]
    valid = set(ids) | {HEATER_MIN, HEATER_MAX, BATTERY}
    if season != "summer":
        valid.discard(HEATER_MIN)
    seen: set[str] = set()
    out: list[str] = []
    for k in (order or []):
        if k in valid and k not in seen:
            out.append(k)
            seen.add(k)
    if not out:
        return default_profile(season, device_ids_by_priority)
    # Pflichteinträge an sinnvoller Stelle ergänzen
    if HEATER_MAX not in seen:
        out.append(HEATER_MAX)
        seen.add(HEATER_MAX)
    if season == "summer" and HEATER_MIN not in seen:
        out.insert(out.index(HEATER_MAX), HEATER_MIN)
        seen.add(HEATER_MIN)
    if BATTERY not in seen:
        out.insert(out.index(HEATER_MAX), BATTERY)
        seen.add(BATTERY)
    # „bis Min“ muss vor „bis Max“ stehen (sonst würde Max das Minimum überholen)
    if HEATER_MIN in out and out.index(HEATER_MIN) > out.index(HEATER_MAX):
        out.remove(HEATER_MIN)
        out.insert(out.index(HEATER_MAX), HEATER_MIN)
    for d in ids:
        if d not in seen:
            out.append(d)
    return out


def phases_for(budget_w: float, phase_w: float, grid_assist_w: float, n_phases: int) -> int:
    """Phasen, die ein Budget trägt (Startschwelle phase_w − grid_assist_w je Phase)."""
    return max(0, min(n_phases, math.floor((budget_w + grid_assist_w) / phase_w)))


@dataclass
class HeaterCtx:
    phase_w: float = 1500.0
    grid_assist_w: float = 0.0
    n_phases: int = 3
    temp: Optional[float] = None          # None = unbekannt (Controller schaltet ab)
    comfort_min: float = 52.0
    max_temp: float = 63.0
    actual_w: float = 0.0                 # gemessene Heizstab-Leistung (für Folge-Geräte)

    def stage_active(self, key: str) -> bool:
        if self.temp is None:
            return key == HEATER_MAX
        if key == HEATER_MIN:
            return self.temp < self.comfort_min
        return self.temp < self.max_temp


@dataclass
class WalkResult:
    heater_budget_w: int = 0
    heater_phases: int = 0
    battery_w: int = 0
    remaining_w: int = 0
    heater_active: bool = False
    trace: list[str] = field(default_factory=list)


def walk(
    order: list[str],
    surplus_w: float,
    heater: HeaterCtx,
    battery_fn: Callable[[float], float],
    device_fn: Callable[[str, float], float],
    heater_fn: Optional[Callable[[int, int, int], None]] = None,
) -> WalkResult:
    """Überschuss entlang der Profil-Reihenfolge verteilen.

    ``battery_fn(avail_w)`` → geplante Ladeleistung der Batterie; ``device_fn(key, remaining)`` →
    Restbudget nach einem sonstigen Gerät. Die Heizstab-Phasenzahl ist nur die Budget-Obergrenze
    (der Controller entscheidet mit Mindestzeiten/Hysterese). ``heater_fn(budget_w, battery_w,
    remaining_w)`` wird an der Position der letzten Heizstab-Stufe aufgerufen (Budget steht fest).
    """
    res = WalkResult()
    remaining = float(surplus_w)
    stage_idx = [i for i, k in enumerate(order) if k in HEATER_STAGES]
    last_stage = stage_idx[-1] if stage_idx else -1
    first_r: Optional[float] = None
    battery_after_first = 0.0
    budget: Optional[float] = None

    for i, key in enumerate(order):
        if key == BATTERY:
            b = max(0.0, float(battery_fn(max(0.0, remaining))))
            remaining -= b
            res.battery_w = int(round(b))
            if first_r is not None:
                battery_after_first += b
            res.trace.append(f"Batterie {b:.0f} W")
        elif key in HEATER_STAGES:
            active = heater.stage_active(key)
            if active and first_r is None:
                first_r = remaining
            if i == last_stage:
                if first_r is not None:
                    budget = first_r - battery_after_first
                    remaining = first_r - battery_after_first - heater.actual_w
                else:
                    budget = remaining                       # Speicher voll: Budget nur informativ
                    remaining -= heater.actual_w
                res.trace.append(f"Heizstab-Budget {budget:.0f} W")
                if heater_fn is not None:
                    heater_fn(int(round(budget)), res.battery_w, int(round(remaining)))
            elif active:
                # vorgelagerte Stufe: beansprucht nur ihre ganzen Phasen, Rest geht weiter
                n = phases_for(remaining, heater.phase_w, heater.grid_assist_w, heater.n_phases)
                claim = n * heater.phase_w
                remaining -= claim
                res.trace.append(f"{key}: {n} Phase(n) = {claim:.0f} W, weiter {remaining:.0f} W")
        else:
            remaining = float(device_fn(key, remaining))
    if budget is None:
        budget = 0.0
    res.heater_budget_w = int(round(budget))
    res.heater_active = first_r is not None
    res.heater_phases = phases_for(budget, heater.phase_w, heater.grid_assist_w, heater.n_phases)
    res.remaining_w = int(round(remaining))
    return res
