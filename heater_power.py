"""Heizstab-Leistung: Zähler (3EM) oder Ersatzwert aus den Phasen-Shellys.

Ist der Heizstab-Zähler nicht lesbar, wird nie stillschweigend 0 W angenommen, sondern
die Summe der Phasen gebildet:
- Phase lesbar: gemessene Leistung (apower); misst der Shelly nicht, Nennleistung wenn an, sonst 0.
- Phase nicht lesbar: Nennleistung, falls sie an sein könnte (letzter protokollierter Zustand
  „on“ oder unbekannt), 0 W wenn sie zuletzt nachweislich ausgeschaltet wurde.
- Stillgelegte Phasen zählen nicht.
Controller und Kaskade nutzen dieselbe Rechnung (source = "meter" | "phase_sum").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

SOURCE_METER = "meter"
SOURCE_PHASE_SUM = "phase_sum"


@dataclass
class PhaseRead:
    on: Optional[bool]          # None = Shelly nicht lesbar
    power_w: Optional[float]    # gemessene Leistung, None = keine Messung


@dataclass
class HeaterPower:
    watts: float
    source: str
    detail: str = ""


def estimate_from_phases(
    phases: Sequence[Optional[PhaseRead]],
    phase_power_w: float,
    last_states: Optional[dict[int, str]] = None,
) -> tuple[float, str]:
    """Summe über die Phasen; ``phases[i]`` None = Phase stillgelegt. Liefert (W, Erklärung)."""
    last_states = last_states or {}
    total = 0.0
    parts: list[str] = []
    for i, ph in enumerate(phases, start=1):
        if ph is None:
            continue
        if ph.on is None:
            could_be_on = last_states.get(i, "on") != "off"
            w = phase_power_w if could_be_on else 0.0
            parts.append(f"PH{i} ? {'→ ' + str(int(w)) + ' W (evtl. an)' if could_be_on else '0 W (zuletzt aus)'}")
        elif ph.power_w is not None:
            w = max(0.0, ph.power_w)
            parts.append(f"PH{i} {int(round(w))} W")
        else:
            w = phase_power_w if ph.on else 0.0
            parts.append(f"PH{i} {'an' if ph.on else 'aus'} ≈ {int(w)} W")
        total += w
    return total, ", ".join(parts)


def resolve(
    meter_w: Optional[float],
    phases: Sequence[Optional[PhaseRead]],
    phase_power_w: float,
    last_states: Optional[dict[int, str]] = None,
) -> HeaterPower:
    if meter_w is not None:
        return HeaterPower(float(meter_w), SOURCE_METER)
    w, detail = estimate_from_phases(phases, phase_power_w, last_states)
    return HeaterPower(w, SOURCE_PHASE_SUM, detail)
