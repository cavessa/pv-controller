"""Zendure-Regelung (Phase 2): Laden aus PV-Überschuss, Entladen bei Netzbezug.

`plan()` ist eine reine Funktion (testbar, keine I/O): sie bekommt Zustand und
Messwerte und liefert die gewünschte Betriebsart samt Limit. `ZendureControl`
liest/schreibt den persistierten Regelzustand (Tabelle zendure_ctl) und sendet
die Befehle über den ZendureClient.

Regeln:
- Laden nur aus dem Überschuss, der der Batterie in der Kaskade zusteht
  (`avail_w`); nie aus dem Netz. Stufenlos, Start ab 100 W + Hysterese.
- Entladen nur ohne Laden, mit SoC > Mindest-SoC und Netzbezug; geregelt auf
  Bezug ≈ 0 (Ziel = aktuelle Abgabe + Bezug), begrenzt auf die **Grundlast**
  (Haus ohne Heizstab und Wallbox) – die Batterie heizt nie das Wasser.
- Wallbox lädt (Auto im Zustand „lädt", > 100 W) -> Entladen hart gesperrt,
  wenn `block_discharge_while_wallbox_charging` (Default an).
- Ein neuer Lade-/Entlademodus startet frühestens 2 min nach dem letzten
  Moduswechsel (dazwischen Standby); Limits werden nur bei > 50 W Änderung
  geschrieben.
- 3 Lesefehler in Folge -> neutral (inputLimit = outputLimit = 0).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import db
from clients.zendure_client import (
    AC_MODE_CHARGE,
    AC_MODE_DISCHARGE,
    ZendureClient,
    ZendureStatus,
)
from config import ZendureConfig

log = logging.getLogger(__name__)

MIN_START_CHARGE_W = 100        # + Hysterese des Kaskaden-Geräts
WRITE_DELTA_W = 50              # Limit nur bei größerer Änderung schreiben
DISCHARGE_START_IMPORT_W = 50   # Netzbezug, ab dem entladen wird
MIN_DIRECTION_S = 120           # Mindestzeit vor Laden <-> Entladen
MAX_FAILS = 3
DEVICE_MAX_W = 2400
UNLIMITED = float("inf")        # Grundlast nicht begrenzt (Tests/Aufrufer ohne Grundlast-Angabe)


@dataclass
class Plan:
    mode: str                  # idle | charge | discharge
    limit_w: int
    write: bool
    reason: str
    action: str                # charge | discharge | idle | hold | blocked | override
    hard: bool = False         # Sicherheits-/Override-Fall, ignoriert Wechselsperre


def _round10(w: float) -> int:
    return int(round(w / 10.0) * 10)


def plan(
    zc: ZendureConfig,
    st: ZendureStatus,
    *,
    avail_w: float,
    import_w: float,
    wallbox_charging: bool,
    override: Optional[str],
    ctl_mode: str,
    ctl_limit_w: int,
    seconds_since_change: float,
    hysteresis_w: int = 100,
    base_load_w: Optional[float] = UNLIMITED,
) -> Plan:
    """Gewünschte Betriebsart bestimmen (siehe Modul-Docstring)."""
    max_c = int(min(zc.max_charge_w, DEVICE_MAX_W))
    max_d = int(min(zc.max_discharge_w, DEVICE_MAX_W))
    soc = st.soc if st.soc is not None else 0.0

    blocked_reason = None
    if override == "off":
        blocked_reason = "Entladen gesperrt (manuell, 30 min)"
    elif zc.block_discharge_while_wallbox_charging and wallbox_charging:
        blocked_reason = "Batterie-Entladung gesperrt: Wallbox lädt"

    # --- Laden --------------------------------------------------------------
    target_c = 0
    reason_c = ""
    if override == "on" and soc < 100:
        target_c, reason_c = max_c, "Laden erzwungen (manuell, 30 min)"
    elif soc >= zc.target_soc:
        reason_c = f"SoC {soc:.0f} % ≥ Ziel {zc.target_soc:.0f} % – kein Laden"
    else:
        need = MIN_START_CHARGE_W + (0 if ctl_mode == "charge" else hysteresis_w)
        if avail_w >= need:
            target_c = max(0, min(_round10(avail_w), max_c))
            reason_c = f"Batterie lädt mit {target_c / 1000:.2f} kW aus PV-Überschuss"
        else:
            reason_c = f"Überschuss {avail_w:.0f} W < {need} W – kein Laden"

    if target_c > 0:
        desired = Plan("charge", target_c, False, reason_c, "override" if override == "on" else "charge",
                       hard=override == "on")
    else:
        # --- Entladen -------------------------------------------------------
        if blocked_reason:
            desired = Plan("idle", 0, False, blocked_reason, "blocked", hard=True)
        elif soc <= zc.min_soc:
            desired = Plan("idle", 0, False,
                           f"Batterie leer (SoC {soc:.0f} % ≤ Mindest-SoC {zc.min_soc:.0f} %)"
                           + (", Netzbezug" if import_w > DISCHARGE_START_IMPORT_W else ""), "idle")
        elif base_load_w is None:
            desired = Plan("idle", 0, False, "Grundlast unbekannt (PV/Hauszähler nicht lesbar) – kein Entladen", "idle")
        elif import_w > DISCHARGE_START_IMPORT_W or ctl_mode == "discharge":
            # Entladen nur für die Grundlast (Haus ohne Heizstab/Wallbox), nie in den Heizstab
            cap_w = min(max_d, base_load_w)
            target_d = max(0, min(_round10(st.home_out_w + import_w), cap_w)) \
                if ctl_mode == "discharge" else max(0, min(_round10(import_w), cap_w))
            target_d = _round10(target_d)
            if target_d >= DISCHARGE_START_IMPORT_W:
                limited = " (auf Grundlast begrenzt)" if base_load_w < min(max_d, import_w + st.home_out_w) else ""
                desired = Plan("discharge", target_d, False,
                               f"Batterie deckt Grundlast ({target_d} W{limited}), SoC {soc:.0f} %", "discharge")
            elif import_w > DISCHARGE_START_IMPORT_W and base_load_w < DISCHARGE_START_IMPORT_W:
                desired = Plan("idle", 0, False,
                               f"Netzbezug {import_w:.0f} W gehört zu Heizstab/Wallbox (Grundlast {base_load_w:.0f} W) – kein Entladen",
                               "idle")
            else:
                desired = Plan("idle", 0, False, "Kein Netzbezug – Batterie im Standby", "idle")
        else:
            desired = Plan("idle", 0, False, reason_c or "Kein Netzbezug – Batterie im Standby", "idle")

    # --- Wechsel-/Schreibsperren ---------------------------------------------
    # Ein neuer Lade-/Entlademodus startet frühestens MIN_DIRECTION_S nach dem letzten
    # Moduswechsel. Bis dahin Standby (nie "halten": ein weiterlaufendes Laden würde
    # ohne Überschuss aus dem Netz ziehen, ein weiterlaufendes Entladen gegen die Sperre).
    if (desired.mode != ctl_mode and desired.mode in ("charge", "discharge")
            and not desired.hard and seconds_since_change < MIN_DIRECTION_S):
        desired = Plan("idle", 0, False,
                       f"Pause nach Moduswechsel ({seconds_since_change:.0f}/{MIN_DIRECTION_S} s) – "
                       f"{desired.mode} danach", "hold")

    changed = desired.mode != ctl_mode or abs(desired.limit_w - ctl_limit_w) > WRITE_DELTA_W
    if desired.mode == "idle" and ctl_limit_w != 0:
        changed = True
    desired.write = changed
    if not changed:
        desired.limit_w = ctl_limit_w if ctl_mode == desired.mode else desired.limit_w
    return desired


class ZendureControl:
    def __init__(self, cfg, client: ZendureClient, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.zc: ZendureConfig = cfg.zendure
        self.client = client
        self.dry_run = dry_run

    def run(
        self,
        st: Optional[ZendureStatus],
        *,
        avail_w: float,
        import_w: float,
        wallbox_charging: bool,
        override: Optional[str],
        hysteresis_w: int = 100,
        base_load_w: Optional[float] = UNLIMITED,
    ) -> Plan:
        ctl = db.get_zendure_ctl()
        now = datetime.now()

        if st is None:
            fails = ctl["fail_count"] + 1
            db.set_zendure_ctl(fail_count=fails)
            text = f"Batterie nicht erreichbar ({min(fails, MAX_FAILS)}/{MAX_FAILS})"
            if fails >= MAX_FAILS and (ctl["mode"] != "idle" or ctl["limit_w"] != 0):
                # neutral stellen; schlägt das fehl, wird beim nächsten Lauf erneut versucht
                if not self.dry_run and self.client.set_mode(None, input_limit=0, output_limit=0):
                    db.set_zendure_ctl(mode="idle", limit_w=0, changed_at=now.isoformat(timespec="seconds"))
                    text += " – neutral gestellt (Limits 0)"
            db.set_zendure_ctl(decision=text, decided_at=now.isoformat(timespec="seconds"))
            return Plan(ctl["mode"], ctl["limit_w"], False, text, "unreachable")

        if ctl["fail_count"]:
            db.set_zendure_ctl(fail_count=0)

        changed_at = datetime.fromisoformat(ctl["changed_at"]) if ctl["changed_at"] else None
        since = (now - changed_at).total_seconds() if changed_at else 1e9
        p = plan(
            self.zc, st, avail_w=avail_w, import_w=import_w, wallbox_charging=wallbox_charging,
            override=override, ctl_mode=ctl["mode"], ctl_limit_w=ctl["limit_w"],
            seconds_since_change=since, hysteresis_w=hysteresis_w, base_load_w=base_load_w,
        )

        reason = p.reason
        if p.write:
            if self.dry_run:
                reason = f"[Dry-Run] {reason} (würde {p.mode} {p.limit_w} W schreiben)"
            else:
                if p.mode == "charge":
                    ok = self.client.set_mode(AC_MODE_CHARGE, input_limit=p.limit_w, output_limit=0)
                elif p.mode == "discharge":
                    ok = self.client.set_mode(AC_MODE_DISCHARGE, input_limit=0, output_limit=p.limit_w)
                else:
                    ok = self.client.set_mode(None, input_limit=0, output_limit=0)
                if ok:
                    upd = dict(mode=p.mode, limit_w=p.limit_w)
                    if p.mode != ctl["mode"]:
                        upd["changed_at"] = now.isoformat(timespec="seconds")
                    db.set_zendure_ctl(**upd)
                    log.info("Zendure: %s %d W – %s", p.mode, p.limit_w, p.reason)
                else:
                    reason = f"Schreiben fehlgeschlagen ({p.mode} {p.limit_w} W) – neuer Versuch im nächsten Lauf"
                    p.write = False
        db.set_zendure_ctl(decision=reason, decided_at=now.isoformat(timespec="seconds"))
        p.reason = reason
        return p
