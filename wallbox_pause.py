"""Wallbox-Pause im PV-/Eco-Modus: die Wallbox steht im Profil hinter Batterie, eBike und Wasser.

Reine Logik (testbar, keine I/O). ``budget_w`` ist, was der Wallbox nach den Stufen davor zusteht
(Kaskaden-Rest an ihrer Position, ihre eigene Ladeleistung ist darin schon zurückgerechnet).

- Budget ≥ Mindestleistung (6 A × Phasen × 230 V) → laden (Strom 6–16 A setzt die Kaskade).
- Budget darunter **5 min am Stück** → pausieren (go-e ``frc=1``), Code ``wallbox_paused_priority``.
- Wieder starten erst nach **5 min am Stück** Budget ≥ Mindestleistung; danach mindestens **10 min** laden.
- Wenige Umschaltungen sind Absicht: häufiges ``frc``-Umschalten ließ das Auto früher (Juli 2026) am
  nächsten Tag erst nach Aus-/Einstecken wieder laden. Beim Abstecken setzt der Controller ``frc=0``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

PAUSE_AFTER_S = 300      # so lange am Stück zu wenig, bis pausiert wird
RESUME_AFTER_S = 300     # so lange am Stück genug, bis wieder gestartet wird
MIN_RUN_S = 600          # nach einem Start mindestens so lange laden
STALE_S = 180            # älterer Zustand gilt nicht (Kaskade läuft nicht) → Controller gibt frei


def _since(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


def step(state: dict, now: datetime, budget_w: float, min_w: float) -> tuple[dict, str, dict]:
    """Zustand fortschreiben → (neuer Zustand, Grund-Code, Zahlen).

    ``state``: ``paused``, ``below_since``, ``above_since``, ``run_since`` (ISO-Zeiten oder None)."""
    st = {k: state.get(k) for k in ("paused", "below_since", "above_since", "run_since")}
    st["paused"] = bool(st["paused"])
    iso = now.isoformat(timespec="seconds")
    enough = budget_w >= min_w
    data = {"rest_w": int(round(budget_w)), "min_w": int(round(min_w))}
    if not st["paused"]:
        st["above_since"] = None
        if enough:
            st["below_since"] = None
            return st, "wallbox_charging_budget", data
        st["below_since"] = st["below_since"] or iso
        below = _since(st["below_since"], now) or 0.0
        run = _since(st["run_since"], now)
        if below >= PAUSE_AFTER_S and (run is None or run >= MIN_RUN_S):
            st.update(paused=True, below_since=None, run_since=None)
            return st, "wallbox_paused_priority", data
        if below < PAUSE_AFTER_S:
            data["remaining_s"] = int(round(PAUSE_AFTER_S - below))
        else:
            data["remaining_s"] = int(round(MIN_RUN_S - (run or 0.0)))
        return st, "wallbox_pause_pending", data
    st["below_since"] = None
    if not enough:
        st["above_since"] = None
        return st, "wallbox_paused_priority", data
    st["above_since"] = st["above_since"] or iso
    above = _since(st["above_since"], now) or 0.0
    if above >= RESUME_AFTER_S:
        st.update(paused=False, above_since=None, run_since=iso)
        return st, "wallbox_charging_budget", data
    data["remaining_s"] = int(round(RESUME_AFTER_S - above))
    return st, "wallbox_resume_pending", data
