"""Tageswerte (Verbrauch, Netzbezug, Einspeisung) aus dem Shelly-Hauptzähler.

Der Shelly 3EM liefert Lifetime-Zähler (Wh). Tageswert = aktueller Stand minus
Mitternachts-Snapshot (Tabelle meter_snapshots). Ohne gültigen Snapshot oder
bei nicht lesbarem Shelly gelten die Solax-Tageszähler wie bisher.
"""
from __future__ import annotations

import logging
from typing import Optional

import db
from models import Readings

log = logging.getLogger(__name__)

# Ein Snapshot zählt nur als „Mitternacht", wenn er in diesem Zeitfenster nach
# 00:00 entstand – sonst würde der Tageswert erst ab Neustart/Start zählen.
SNAPSHOT_MAX_TAKEN_AT = "00:30:00"


def pv_today_kwh(r: Readings) -> Optional[float]:
    """Tages-PV-Ertrag beider Wechselrichter (Haupt-WR + Pergola)."""
    if r.solax_yield_today_kwh is None:
        return None
    return r.solax_yield_today_kwh + (r.pv2_today_kwh or 0.0)


def compute_meter_today(r: Readings, pv_kwh: Optional[float] = None) -> Optional[dict]:
    """{source, bezug_kwh, einspeisung_kwh, verbrauch_kwh} oder None, wenn nichts bekannt.

    verbrauch = PV-Ertrag (Summe) + Bezug − Einspeisung.
    """
    if pv_kwh is None:
        pv_kwh = pv_today_kwh(r)
    if pv_kwh is None:
        return None

    bezug = einsp = None
    source = "solax"
    if r.main_import_wh is not None and r.main_export_wh is not None:
        try:
            snap = db.ensure_meter_snapshot(r.main_import_wh, r.main_export_wh)
            d_in = r.main_import_wh - snap["import_wh"]
            d_out = r.main_export_wh - snap["export_wh"]
            # Negative Differenz = Zähler wurde zurückgesetzt -> Snapshot unbrauchbar
            if snap["taken_at"] <= SNAPSHOT_MAX_TAKEN_AT and d_in >= 0 and d_out >= 0:
                bezug, einsp, source = d_in / 1000.0, d_out / 1000.0, "shelly"
        except Exception:
            log.warning("meter_today: Snapshot nicht lesbar", exc_info=True)

    if source == "solax":
        if r.solax_grid_in_kwh is None or r.solax_grid_out_kwh is None:
            return None
        bezug, einsp = r.solax_grid_in_kwh, r.solax_grid_out_kwh

    return {
        "source": source,
        "bezug_kwh": round(bezug, 2),
        "einspeisung_kwh": round(einsp, 2),
        "verbrauch_kwh": round(max(0.0, pv_kwh + bezug - einsp), 2),
    }
