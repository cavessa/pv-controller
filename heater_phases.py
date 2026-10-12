"""Phasenentscheidung des Heizstabs (reine Logik, keine I/O).

Aufteilung der Zuständigkeiten:
- Die Kaskade legt nur das **Budget** fest („Heizstab darf X W haben": Rest-Überschuss
  an seiner Prioritätsposition, brutto inkl. seiner eigenen Leistung).
- Hier wird entschieden, **welche Phasen** innerhalb dieses Budgets laufen.

Ziel-Phasenzahl: ``floor((budget + grid_assist) / phase_w)``, begrenzt 0..3 (bzw. Zahl der
aktiven Phasen). Zuschalten nutzt das **längere** Mittel (``budget_w``), Abschalten das
**kürzere** (``budget_down_w``). Runterschalten erst, wenn das Budget um mehr als
``phase_w × down_hyst_frac`` unter der Schwelle der laufenden Phase liegt. Deaktivierte
Phasen (``disabled``) werden ignoriert und zählen weder im Budget noch als laufend. Mindestlaufzeit/-pause je Phase; hochgeschaltet wird
höchstens eine Phase pro Lauf.

Über allem stehen (dürfen jederzeit abschalten, auch innerhalb der Mindestlaufzeit):
Speichertemperatur ≥ Max, Speichertemperatur unbekannt, Sommermodus-Stopp, manuelles AUS.
Eine nicht erreichbare Phase (``state is None``) wird im Budget vorsichtshalber als laufend gezählt.
**AUS darf auch an eine Phase mit unbekanntem Zustand gesendet werden („blind“), AN nie**: bei allen
Abschaltgründen (Max-Temp, Temperatur unbekannt, Sommermodus-Stopp, manuell AUS) und wenn das
Budget-Ziel weniger Phasen als laufend vorsieht. Der Befehl wird in jedem Lauf erneut gesendet,
solange der Zustand unbekannt bleibt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from models import PhaseAction, TempStatus


@dataclass
class HeaterParams:
    phase_w: float = 1500.0
    grid_assist_w: float = 0.0
    max_temp: float = 64.0
    resume_temp: float = 62.0
    min_on_s: float = 420.0
    min_off_s: float = 120.0
    down_hyst_frac: float = 0.15
    n_phases: int = 3


@dataclass
class PhaseIn:
    name: str
    state: Optional[bool]               # None = nicht erreichbar
    last_change: Optional[datetime] = None
    disabled: bool = False              # in der Config stillgelegt


@dataclass
class PhaseOut:
    action: PhaseAction
    reason: str
    blind: bool = False                 # AUS an eine Phase mit unbekanntem Zustand


def classify_temp(temp: float, p: HeaterParams) -> TempStatus:
    if temp >= p.max_temp:
        return TempStatus.AT_OR_ABOVE_MAX
    if temp <= p.resume_temp:
        return TempStatus.BELOW_RESUME
    return TempStatus.HYSTERESIS_BAND


def target_phases(
    budget_w: float, n_cur: int, p: HeaterParams, budget_down_w: Optional[float] = None
) -> int:
    """Ziel-Phasenzahl mit Start-/Halte-Hysterese (n_cur = aktuell laufende inkl. unbekannter).

    Start: ``budget_w`` (langes Mittel); Halten/Abschalten: ``budget_down_w`` (kurzes Mittel,
    Default = budget_w)."""
    bd = budget_w if budget_down_w is None else budget_down_w
    t_up = max(0, min(p.n_phases, math.floor((budget_w + p.grid_assist_w) / p.phase_w)))
    t_keep = max(0, min(p.n_phases, math.floor(
        (bd + p.grid_assist_w + p.down_hyst_frac * p.phase_w) / p.phase_w)))
    if t_up > n_cur:
        return t_up
    if t_keep >= n_cur:
        return n_cur
    return t_keep


def decide_phases(
    phases: list[PhaseIn],
    temp: Optional[float],
    budget_w: Optional[float],
    p: HeaterParams,
    now: datetime,
    *,
    budget_down_w: Optional[float] = None,  # kürzeres Mittel fürs Abschalten
    force: Optional[str] = None,          # "off" | "on" (manuelle Übersteuerung der Kaskade)
    summer: Optional[str] = None,         # "heat" | "stop"
    summer_reason: str = "",
    surplus_fail_safe: bool = False,      # Hauptzähler/Wechselrichter nicht lesbar
) -> list[PhaseOut]:
    n_active = sum(1 for ph in phases if not ph.disabled)
    p = replace(p, n_phases=min(p.n_phases, n_active))

    def all_reachable(action: PhaseAction, text: str) -> list[PhaseOut]:
        out = []
        for ph in phases:
            if ph.disabled:
                out.append(PhaseOut(PhaseAction.UNCHANGED, "deaktiviert"))
            elif ph.state is None:
                if action is PhaseAction.TURN_OFF:
                    out.append(PhaseOut(PhaseAction.TURN_OFF, f"{text}; Zustand unbekannt – AUS wird trotzdem gesendet",
                                        blind=True))
                else:
                    out.append(PhaseOut(PhaseAction.UNCHANGED, "nicht erreichbar – AN nie ohne bekannten Zustand"))
            elif action is PhaseAction.TURN_OFF:
                out.append(PhaseOut(PhaseAction.TURN_OFF if ph.state else PhaseAction.UNCHANGED, text))
            else:
                out.append(PhaseOut(PhaseAction.TURN_ON if not ph.state else PhaseAction.UNCHANGED, text))
        return out

    # ---- Vorrang: Sicherheit --------------------------------------------------
    if temp is None:
        return all_reachable(PhaseAction.TURN_OFF, "Speichertemperatur unbekannt – Heizstab aus")
    status = classify_temp(temp, p)
    if status is TempStatus.AT_OR_ABOVE_MAX:
        return all_reachable(
            PhaseAction.TURN_OFF,
            f"storage_temp={temp:.1f} °C >= storage_max_temp={p.max_temp:.1f} °C – Heizstab aus",
        )
    if summer == "stop":
        return all_reachable(PhaseAction.TURN_OFF, summer_reason)
    if force == "off":
        return all_reachable(PhaseAction.TURN_OFF, "Manuell AUS (Kaskade)")
    if summer == "heat":
        return all_reachable(PhaseAction.TURN_ON, summer_reason)

    # ---- ab hier Budgetlogik --------------------------------------------------
    if surplus_fail_safe or budget_w is None:
        return [PhaseOut(PhaseAction.UNCHANGED,
                         "deaktiviert" if ph.disabled
                         else "Hauptzähler/Wechselrichter nicht lesbar – Fail-safe, keine Änderung")
                for ph in phases]
    if force == "on":
        budget_w = budget_down_w = p.n_phases * p.phase_w

    n_unknown = sum(1 for ph in phases if ph.state is None and not ph.disabled)
    on_idx = [i for i, ph in enumerate(phases) if ph.state is True and not ph.disabled]
    n_cur = len(on_idx) + n_unknown            # unbekannte Phasen zählen als laufend

    target = target_phases(budget_w, n_cur, p, budget_down_w)
    band_note = ""
    if status is TempStatus.HYSTERESIS_BAND and n_cur == 0:
        target, band_note = 0, " Hysteresebereich: keine neue Phase ohne laufenden Zyklus."

    # unbekannte Phasen gelten als laufend; sieht das Ziel weniger vor, bekommen die überzähligen AUS
    unknown_idx = [i for i, ph in enumerate(phases) if ph.state is None and not ph.disabled]
    off_unknown = set(unknown_idx[min(n_unknown, target):])
    slots = max(0, target - n_unknown)         # Phasen, die erreichbare Phasen belegen dürfen
    # bevorzugt bereits laufende Phasen behalten (niedrigster Index zuerst), dann auffüllen
    reachable = [i for i, ph in enumerate(phases) if ph.state is not None and not ph.disabled]
    keep = [i for i in on_idx][:slots]
    fill = [i for i in reachable if i not in keep][: max(0, slots - len(keep))]
    wanted = set(keep) | set(fill)

    down = "" if budget_down_w is None or budget_down_w == budget_w else f", ab {budget_down_w:.0f} W"
    head = (f"Budget {budget_w:.0f} W{down} (+{p.grid_assist_w:.0f} W Netzbezug) → Ziel {target} Phase(n){band_note}")

    def elapsed(ph: PhaseIn) -> float:
        return (now - ph.last_change).total_seconds() if ph.last_change else 1e9

    out: list[PhaseOut] = []
    turned_on = 0
    for i, ph in enumerate(phases):
        if ph.disabled:
            out.append(PhaseOut(PhaseAction.UNCHANGED, "deaktiviert"))
        elif ph.state is None and i in off_unknown:
            if elapsed(ph) < p.min_on_s:
                out.append(PhaseOut(PhaseAction.UNCHANGED,
                                    f"{head}; nicht erreichbar, Mindestlaufzeit {elapsed(ph):.0f}/{p.min_on_s:.0f} s"))
            else:
                out.append(PhaseOut(PhaseAction.TURN_OFF,
                                    f"{head}; Zustand unbekannt – AUS wird gesendet", blind=True))
        elif ph.state is None:
            out.append(PhaseOut(PhaseAction.UNCHANGED,
                                f"{head}; nicht erreichbar – wird als laufend gezählt"))
        elif i in wanted and not ph.state:
            if turned_on >= 1:
                out.append(PhaseOut(PhaseAction.UNCHANGED, f"{head}; nur 1 Phase pro Lauf zuschalten"))
            elif elapsed(ph) < p.min_off_s:
                out.append(PhaseOut(PhaseAction.UNCHANGED,
                                    f"{head}; Mindestpause {elapsed(ph):.0f}/{p.min_off_s:.0f} s"))
            else:
                turned_on += 1
                out.append(PhaseOut(PhaseAction.TURN_ON, f"{head}; schalte EIN"))
        elif i not in wanted and ph.state:
            if elapsed(ph) < p.min_on_s:
                out.append(PhaseOut(PhaseAction.UNCHANGED,
                                    f"{head}; Mindestlaufzeit {elapsed(ph):.0f}/{p.min_on_s:.0f} s"))
            else:
                out.append(PhaseOut(PhaseAction.TURN_OFF, f"{head}; schalte AUS"))
        else:
            out.append(PhaseOut(PhaseAction.UNCHANGED,
                                f"{head}; " + ("bleibt EIN" if ph.state else "bleibt AUS")))
    return out
