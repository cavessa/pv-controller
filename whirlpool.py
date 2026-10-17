"""Whirlpool-Leistung aus seinem eigenen Shelly 3EM (nur lesen, nichts wird geschaltet).

Läuft eine Heizstab-Phase durch den Whirlpool-Zähler (``whirlpool_meter.subtract_heater_phase``,
z. B. "L2"), wird ihre Leistung abgezogen: Whirlpool = Summe der 3 Phasen − Heizstab-3EM auf dieser
Phase (gleicher Zeitpunkt), nicht unter 0. Ist der Heizstab-3EM dann nicht lesbar, ist der Wert
**unsicher** (``power_w`` = None, ``uncertain``) – lieber kein Wert als ein falscher.
Whirlpool-Zähler offline → None, kein Fail-safe (der Whirlpool wird nicht geregelt).
"""
from __future__ import annotations

from typing import Optional

PHASE_INDEX = {"L1": 0, "L2": 1, "L3": 2}


def compute(whirl_phases: Optional[list[float]], heater_phases: Optional[list[float]],
            subtract: str = "") -> dict:
    """{"power_w", "raw_w", "heater_phase_w", "uncertain", "reachable"} für eine Messung."""
    if not whirl_phases:
        return {"power_w": None, "raw_w": None, "heater_phase_w": None, "uncertain": False, "reachable": False}
    raw = float(sum(whirl_phases))
    out = {"power_w": raw, "raw_w": raw, "heater_phase_w": None, "uncertain": False, "reachable": True}
    idx = PHASE_INDEX.get(subtract or "")
    if idx is None:
        return out
    if not heater_phases or len(heater_phases) <= idx:
        return {**out, "power_w": None, "uncertain": True}
    hp = max(0.0, float(heater_phases[idx]))
    return {**out, "power_w": max(0.0, raw - hp), "heater_phase_w": hp}
