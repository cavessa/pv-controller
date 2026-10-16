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
Speichertemperatur ≥ Max, Speichertemperatur unbekannt, Sommermodus-Stopp, manuelles AUS,
„Speicher praktisch voll“ (``practically_full``: Thermostat im Heizstab ≥ 15 min offen nahe Max).

Thermostat offen (``thermostat_open``: Relais an, Heizstab zieht seit ≥ 2 Messungen nichts): die
Relais bleiben wie sie sind – keine neue Phase (zöge ja nichts), kein Abschalten übers Budget (keine
Schaltspiele; der Heizstab kommt von selbst wieder). Die Leistung bucht die Kaskade nach Messung.
Eine nicht erreichbare Phase (``state is None``) wird im Budget vorsichtshalber als laufend gezählt.
**AUS darf auch an eine Phase mit unbekanntem Zustand gesendet werden („blind“), AN nie**: bei allen
Abschaltgründen (Max-Temp, Temperatur unbekannt, Sommermodus-Stopp, manuell AUS) und wenn das
Budget-Ziel weniger Phasen als laufend vorsieht. Der Befehl wird in jedem Lauf erneut gesendet,
solange der Zustand unbekannt bleibt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
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
    code: str = ""                      # Grund-Code (reasons.py) – nur Protokoll, ohne Einfluss auf die Entscheidung
    data: dict = field(default_factory=dict)


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
    thermostat_open: Optional[dict] = None,   # {"open_min", "temp_c"}: Relais an, Heizstab zieht nichts
    practically_full: Optional[dict] = None,  # {"open_min", "temp_c", "release_c"}
) -> list[PhaseOut]:
    n_active = sum(1 for ph in phases if not ph.disabled)
    p = replace(p, n_phases=min(p.n_phases, n_active))

    def all_reachable(action: PhaseAction, text: str, code: str, data: Optional[dict] = None) -> list[PhaseOut]:
        out = []
        d = dict(data or {})
        for ph in phases:
            if ph.disabled:
                out.append(PhaseOut(PhaseAction.UNCHANGED, "deaktiviert", code="phase_disabled"))
            elif ph.state is None:
                if action is PhaseAction.TURN_OFF:
                    out.append(PhaseOut(PhaseAction.TURN_OFF, f"{text}; Zustand unbekannt – AUS wird trotzdem gesendet",
                                        blind=True, code=code, data={**d, "blind_off": True}))
                else:
                    out.append(PhaseOut(PhaseAction.UNCHANGED, "nicht erreichbar – AN nie ohne bekannten Zustand",
                                        code="failsafe_phase", data={"wanted": code}))
            elif action is PhaseAction.TURN_OFF:
                out.append(PhaseOut(PhaseAction.TURN_OFF if ph.state else PhaseAction.UNCHANGED, text,
                                    code=code, data=dict(d)))
            else:
                out.append(PhaseOut(PhaseAction.TURN_ON if not ph.state else PhaseAction.UNCHANGED, text,
                                    code=code, data=dict(d)))
        return out

    # ---- Vorrang: Sicherheit --------------------------------------------------
    if temp is None:
        return all_reachable(PhaseAction.TURN_OFF, "Speichertemperatur unbekannt – Heizstab aus", "temp_unknown")
    status = classify_temp(temp, p)
    if status is TempStatus.AT_OR_ABOVE_MAX:
        return all_reachable(
            PhaseAction.TURN_OFF,
            f"storage_temp={temp:.1f} °C >= storage_max_temp={p.max_temp:.1f} °C – Heizstab aus",
            "max_temp_reached", {"temp_c": round(temp, 1), "max_temp_c": p.max_temp},
        )
    if summer == "stop":
        return all_reachable(PhaseAction.TURN_OFF, summer_reason, "summer_mode_stop", {"temp_c": round(temp, 1)})
    if force == "off":
        return all_reachable(PhaseAction.TURN_OFF, "Manuell AUS (Kaskade)", "manual_override", {"mode": "aus"})
    if practically_full is not None and force != "on":
        pf = practically_full
        return all_reachable(
            PhaseAction.TURN_OFF,
            f"Speicher praktisch voll (Thermostat im Heizstab {pf.get('open_min')} min offen bei "
            f"{pf.get('temp_c')} °C) – Heizstab aus, Freigabe ab {pf.get('release_c')} °C",
            "storage_practically_full", dict(pf),
        )
    if summer == "heat":
        return all_reachable(PhaseAction.TURN_ON, summer_reason, "summer_mode_grid_heat", {"temp_c": round(temp, 1)})

    # ---- ab hier Budgetlogik --------------------------------------------------
    if surplus_fail_safe or budget_w is None:
        return [PhaseOut(PhaseAction.UNCHANGED,
                         "deaktiviert" if ph.disabled
                         else "Hauptzähler/Wechselrichter nicht lesbar – Fail-safe, keine Änderung",
                         code="phase_disabled" if ph.disabled else "failsafe_global")
                for ph in phases]
    if thermostat_open is not None and force != "on":
        to = {"open_min": thermostat_open.get("open_min"), "temp_c": round(temp, 1)}
        text = (f"Thermostat im Heizstab hat abgeschaltet (Relais an, 0 W seit {to['open_min']} min) – "
                "Relais bleiben, keine neue Phase")
        return [PhaseOut(PhaseAction.UNCHANGED, "deaktiviert", code="phase_disabled") if ph.disabled
                else PhaseOut(PhaseAction.UNCHANGED, text + ("; bleibt EIN" if ph.state else "; bleibt AUS"),
                              code="heater_thermostat_open", data=dict(to))
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

    # Grund-Codes (nur Protokoll): gemeinsame Zahlen; Schwelle = Budget, ab dem eine weitere Phase dazukäme
    base = {"budget_w": int(round(budget_w)), "target_phases": target, "running": n_cur,
            "threshold_w": int(round((target + 1) * p.phase_w - p.grid_assist_w))}
    if budget_down_w is not None and budget_down_w != budget_w:
        base["budget_down_w"] = int(round(budget_down_w))
    def tag(o: PhaseOut, code: str, **extra) -> PhaseOut:
        o.code, o.data = code, {**base, **extra}
        return o

    def rest(ph: PhaseIn, limit: float) -> int:
        return int(round(limit - elapsed(ph)))

    on_code = "manual_override" if force == "on" else "budget_available"
    off_code = "hysteresis_band" if band_note else "below_start_threshold"
    if band_note:
        base.update(temp_c=round(temp, 1), resume_temp_c=p.resume_temp)
    if force == "on":
        base["mode"] = "an"
    base["rest_w"] = base["budget_w"]
    out: list[PhaseOut] = []
    turned_on = 0
    for i, ph in enumerate(phases):
        if ph.disabled:
            out.append(PhaseOut(PhaseAction.UNCHANGED, "deaktiviert", code="phase_disabled"))
        elif ph.state is None and i in off_unknown:
            if elapsed(ph) < p.min_on_s:
                out.append(tag(PhaseOut(PhaseAction.UNCHANGED,
                                        f"{head}; nicht erreichbar, Mindestlaufzeit {elapsed(ph):.0f}/{p.min_on_s:.0f} s"),
                               "min_on_time", remaining_s=rest(ph, p.min_on_s), unreachable=True))
            else:
                out.append(tag(PhaseOut(PhaseAction.TURN_OFF,
                                        f"{head}; Zustand unbekannt – AUS wird gesendet", blind=True),
                               "phase_unknown_off_sent"))
        elif ph.state is None:
            out.append(tag(PhaseOut(PhaseAction.UNCHANGED,
                                    f"{head}; nicht erreichbar – wird als laufend gezählt"), "phase_unknown"))
        elif i in wanted and not ph.state:
            if turned_on >= 1:
                out.append(tag(PhaseOut(PhaseAction.UNCHANGED, f"{head}; nur 1 Phase pro Lauf zuschalten"),
                               "one_phase_per_run"))
            elif elapsed(ph) < p.min_off_s:
                out.append(tag(PhaseOut(PhaseAction.UNCHANGED,
                                        f"{head}; Mindestpause {elapsed(ph):.0f}/{p.min_off_s:.0f} s"),
                               "min_off_time", remaining_s=rest(ph, p.min_off_s)))
            else:
                turned_on += 1
                out.append(tag(PhaseOut(PhaseAction.TURN_ON, f"{head}; schalte EIN"), on_code))
        elif i not in wanted and ph.state:
            if elapsed(ph) < p.min_on_s:
                out.append(tag(PhaseOut(PhaseAction.UNCHANGED,
                                        f"{head}; Mindestlaufzeit {elapsed(ph):.0f}/{p.min_on_s:.0f} s"),
                               "min_on_time", remaining_s=rest(ph, p.min_on_s)))
            else:
                out.append(tag(PhaseOut(PhaseAction.TURN_OFF, f"{head}; schalte AUS"), "below_stop_threshold"))
        else:
            out.append(tag(PhaseOut(PhaseAction.UNCHANGED,
                                    f"{head}; " + ("bleibt EIN" if ph.state else "bleibt AUS")),
                           on_code if ph.state else off_code))
    return out


# ---- Thermostat im Heizstab ------------------------------------------------------------
THERMOSTAT_OPEN_W = 50.0        # Relais an, Heizstab-3EM darunter → Thermostat offen
THERMOSTAT_MIN_TEMP = 50.0      # nur bei warmem Speicher (kalt: eher Defekt, nicht Thermostat)
FULL_AFTER_S = 15 * 60          # so lange offen …
FULL_MARGIN_C = 2.0             # … und Speicher ≥ Max − 2 °C → „praktisch voll“
RELEASE_DROP_C = 2.0            # Freigabe, wenn der Speicher 2 °C unter den Wert beim Abschalten fällt


def thermostat_step(
    st: dict, now: datetime, relay_on: bool, heater_w: Optional[float], temp: Optional[float], max_temp: float,
) -> tuple[dict, Optional[dict], Optional[dict], str]:
    """Thermostat-Zustand fortschreiben (reine Logik).

    ``st``: gespeicherter Zustand (open_since, open_temp, full_since, full_temp). Liefert
    (DB-Updates, thermostat_open-Info oder None, practically_full-Info oder None, Log-Text).
    Offen gilt erst ab der **zweiten** Messung in Folge (erste setzt nur open_since) – kein
    Fehlalarm, wenn ein Relais gerade erst geschaltet hat."""
    upd: dict = {}
    msg = ""
    full_since, full_temp = st.get("full_since"), st.get("full_temp")
    if full_since and temp is not None and full_temp is not None and temp <= full_temp - RELEASE_DROP_C:
        upd.update(full_since=None, full_temp=None)
        msg = (f"Heizstab-Thermostat: Speicher {temp:.1f} °C ist {RELEASE_DROP_C:.0f} °C unter {full_temp:.1f} °C "
               "gefallen – „praktisch voll“ aufgehoben, Heizstab wieder frei")
        full_since = full_temp = None
    is_open = (relay_on and heater_w is not None and heater_w < THERMOSTAT_OPEN_W
               and temp is not None and temp >= THERMOSTAT_MIN_TEMP)
    open_info: Optional[dict] = None
    since = st.get("open_since")
    if full_since:
        if since:
            upd.update(open_since=None, open_temp=None)
    elif is_open:
        if not since:
            upd.update(open_since=now.isoformat(timespec="seconds"), open_temp=round(temp, 1))
        else:
            age = (now - datetime.fromisoformat(since)).total_seconds()
            open_info = {"open_min": max(1, int(round(age / 60))), "temp_c": round(temp, 1)}
            if age >= FULL_AFTER_S - 30 and temp >= max_temp - FULL_MARGIN_C:
                full_since, full_temp = now.isoformat(timespec="seconds"), round(temp, 1)
                upd.update(full_since=full_since, full_temp=full_temp, open_since=None, open_temp=None)
                msg = (f"Heizstab-Thermostat seit {open_info['open_min']} min offen bei {temp:.1f} °C "
                       f"(≥ Max − {FULL_MARGIN_C:.0f} °C) – Speicher praktisch voll, Heizstab aus bis "
                       f"{temp - RELEASE_DROP_C:.1f} °C")
                open_info = None
            elif open_info["open_min"] == 1:
                msg = (f"Heizstab-Thermostat offen: Relais an, Heizstab zieht {heater_w:.0f} W bei "
                       f"{temp:.1f} °C – Relais bleiben, Überschuss geht an Batterie/Wallbox")
    elif since:
        upd.update(open_since=None, open_temp=None)
        if relay_on and heater_w is not None and heater_w >= THERMOSTAT_OPEN_W:
            age = (now - datetime.fromisoformat(since)).total_seconds()
            msg = f"Heizstab zieht wieder ({heater_w:.0f} W) nach {age / 60:.0f} min Thermostat-Pause"
    full_info = None
    if full_since:
        full_info = {"open_min": int(FULL_AFTER_S // 60), "temp_c": full_temp,
                     "release_c": round(float(full_temp) - RELEASE_DROP_C, 1)}
    return upd, open_info, full_info, msg
