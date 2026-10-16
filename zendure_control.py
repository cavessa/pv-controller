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
- Laden <-> Entladen wechselt direkt (Gerät stellt in < 5 s um, geprüft 16.10.2026);
  Flattern verhindern die Schwellen (Laden ab 100 W + Hysterese, Entladen ab 50 W Bezug).
  Die frühere 2-min-Pause ließ die Batterie bei Netzbezug bzw. Einspeisung im Standby
  (``MIN_DIRECTION_S`` = 0, Mechanismus bleibt); Limits werden nur bei > 50 W Änderung
  geschrieben.
- 3 Lesefehler in Folge -> neutral (inputLimit = outputLimit = 0).
- Ladesollwert höchstens „jetzige Ladeleistung + jetzige Einspeisung“ (`export_cap_w`):
  die Batterie nimmt nur, was tatsächlich ins Netz geht – braucht der Heizstab die Leistung
  noch (Mindestlaufzeit), wartet sie. Absenkungen werden immer geschrieben (Wolke → höchstens
  bis zum nächsten Lauf aus dem Netz).
- Fail-safe (Hauptzähler/WR nicht lesbar, Kaskade abgestürzt) → sofort Standby (Limits 0).
- Watchdog (`zendure_watchdog.py`, eigener Cron): Regelung seit 5 min nicht gelaufen → Standby.
- Meldet das Gerät andere Sollwerte als den Regelzustand (App/HEMS, Neustart:
  smartMode=1 schreibt nur ins RAM), wird der Zustand erneut geschrieben.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
MIN_DIRECTION_S = 0             # Mindestzeit vor Laden <-> Entladen (0 = direkt)
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
    code: str = ""             # Grund-Code (reasons.py) – nur Protokoll, ohne Einfluss auf die Regelung
    data: dict = field(default_factory=dict)
    want_w: Optional[int] = None   # gewünschte Ladeleistung vor dem Einspeise-Deckel (für das Heizstab-Budget)


def _w(v: Optional[float]) -> Optional[int]:
    """Zahl fürs Protokoll (Grund-Code); unbekannt/unbegrenzt → None, nie ein Fehler."""
    try:
        return int(round(v))
    except (TypeError, ValueError, OverflowError):
        return None


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
    failsafe: Optional[str] = None,
    export_cap_w: Optional[float] = None,
) -> Plan:
    """Gewünschte Betriebsart bestimmen (siehe Modul-Docstring).

    ``failsafe``: Text, was nicht lesbar ist → Standby (hart). ``export_cap_w``: jetzige
    Ladeleistung + jetzige Einspeisung am Hauptzähler – Obergrenze für den Ladesollwert."""
    max_c = int(min(zc.max_charge_w, DEVICE_MAX_W))
    max_d = int(min(zc.max_discharge_w, DEVICE_MAX_W))
    soc = st.soc if st.soc is not None else 0.0

    blocked_reason = None
    blocked_code = ""
    if override == "off":
        blocked_reason, blocked_code = "Entladen gesperrt (manuell, 30 min)", "manual_override"
    elif zc.block_discharge_while_wallbox_charging and wallbox_charging:
        blocked_reason, blocked_code = "Batterie-Entladung gesperrt: Wallbox lädt", "wallbox_lock"
    nums = {"soc": _w(soc), "avail_w": _w(avail_w), "import_w": _w(import_w)}

    # --- Laden --------------------------------------------------------------
    target_c = 0
    reason_c = ""
    code_c, data_c = "", {}
    if override == "on" and soc < 100:
        target_c, reason_c = max_c, "Laden erzwungen (manuell, 30 min)"
        code_c, data_c = "manual_override", {"mode": "Laden"}
    elif soc >= zc.target_soc:
        reason_c = f"SoC {soc:.0f} % ≥ Ziel {zc.target_soc:.0f} % – kein Laden"
        code_c, data_c = "soc_target_reached", {"target_soc": zc.target_soc}
    else:
        need = MIN_START_CHARGE_W + (0 if ctl_mode == "charge" else hysteresis_w)
        if avail_w >= need:
            target_c = max(0, min(_round10(avail_w), max_c))
            reason_c = f"Batterie lädt mit {target_c / 1000:.2f} kW aus PV-Überschuss"
            code_c, data_c = "charging_surplus", {"watts": target_c}
        else:
            reason_c = f"Überschuss {avail_w:.0f} W < {need} W – kein Laden"
            code_c, data_c = "below_start_threshold", {"rest_w": _w(avail_w), "threshold_w": need}

    want_c = target_c
    if target_c > 0 and override != "on" and export_cap_w is not None:
        cap = max(0, int(export_cap_w // 10 * 10))
        if cap < target_c:
            target_c = cap
            reason_c = (f"Batterie lädt mit {target_c / 1000:.2f} kW (wartet auf {want_c} W – "
                        f"nur was gerade eingespeist wird)")
            data_c = {**data_c, "watts": target_c, "want_w": want_c}

    if failsafe:
        desired = Plan("idle", 0, False, f"Fail-safe: {failsafe} nicht lesbar – Batterie im Standby", "blocked",
                       hard=True, code="failsafe_global", data={**nums, "unreadable": failsafe})
    elif want_c > 0:
        desired = Plan("charge", target_c, False, reason_c, "override" if override == "on" else "charge",
                       hard=override == "on", code=code_c, data={**nums, **data_c}, want_w=want_c)
    else:
        # --- Entladen -------------------------------------------------------
        if blocked_reason:
            desired = Plan("idle", 0, False, blocked_reason, "blocked", hard=True, code=blocked_code,
                           data={**nums, "mode": "Entladen gesperrt"})
        elif soc <= zc.min_soc:
            desired = Plan("idle", 0, False,
                           f"Batterie leer (SoC {soc:.0f} % ≤ Mindest-SoC {zc.min_soc:.0f} %)"
                           + (", Netzbezug" if import_w > DISCHARGE_START_IMPORT_W else ""), "idle",
                           code="battery_empty", data={**nums, "min_soc": zc.min_soc})
        elif base_load_w is None:
            desired = Plan("idle", 0, False, "Grundlast unbekannt (PV/Hauszähler nicht lesbar) – kein Entladen", "idle",
                           code="base_load_unknown", data=dict(nums))
        elif import_w > DISCHARGE_START_IMPORT_W or ctl_mode == "discharge":
            # Entladen nur für die Grundlast (Haus ohne Heizstab/Wallbox), nie in den Heizstab
            cap_w = min(max_d, base_load_w)
            target_d = max(0, min(_round10(st.home_out_w + import_w), cap_w)) \
                if ctl_mode == "discharge" else max(0, min(_round10(import_w), cap_w))
            target_d = _round10(target_d)
            if target_d >= DISCHARGE_START_IMPORT_W:
                limited = " (auf Grundlast begrenzt)" if base_load_w < min(max_d, import_w + st.home_out_w) else ""
                desired = Plan("discharge", target_d, False,
                               f"Batterie deckt Grundlast ({target_d} W{limited}), SoC {soc:.0f} %", "discharge",
                               code="discharging_base_load",
                               data={**nums, "watts": target_d, "base_load_w": _w(base_load_w)})
            elif import_w > DISCHARGE_START_IMPORT_W and base_load_w < DISCHARGE_START_IMPORT_W:
                desired = Plan("idle", 0, False,
                               f"Netzbezug {import_w:.0f} W gehört zu Heizstab/Wallbox (Grundlast {base_load_w:.0f} W) – kein Entladen",
                               "idle", code="no_discharge_controlled_load",
                               data={**nums, "base_load_w": _w(base_load_w)})
            else:
                desired = Plan("idle", 0, False, "Kein Netzbezug – Batterie im Standby", "idle",
                               code="standby_no_import", data=dict(nums))
        else:
            desired = Plan("idle", 0, False, reason_c or "Kein Netzbezug – Batterie im Standby", "idle",
                           code=code_c or "standby_no_import", data={**nums, **data_c})

    # --- Wechsel-/Schreibsperren ---------------------------------------------
    # Ein neuer Lade-/Entlademodus startet frühestens MIN_DIRECTION_S nach dem letzten
    # Moduswechsel. Bis dahin Standby (nie "halten": ein weiterlaufendes Laden würde
    # ohne Überschuss aus dem Netz ziehen, ein weiterlaufendes Entladen gegen die Sperre).
    if (desired.mode != ctl_mode and desired.mode in ("charge", "discharge")
            and not desired.hard and seconds_since_change < MIN_DIRECTION_S):
        desired = Plan("idle", 0, False,
                       f"Pause nach Moduswechsel ({seconds_since_change:.0f}/{MIN_DIRECTION_S} s) – "
                       f"{desired.mode} danach", "hold", code="mode_change_pause",
                       data={**nums, "remaining_s": int(round(MIN_DIRECTION_S - seconds_since_change)),
                             "next_mode": "Laden" if desired.mode == "charge" else "Entladen"})

    changed = desired.mode != ctl_mode or abs(desired.limit_w - ctl_limit_w) > WRITE_DELTA_W
    if desired.mode == "charge" == ctl_mode and desired.limit_w < ctl_limit_w:
        changed = True                       # weniger Laden sofort schreiben (sonst Netzbezug)
    if desired.mode == "idle" and ctl_limit_w != 0:
        changed = True
    desired.write = changed
    if not changed:
        desired.limit_w = ctl_limit_w if ctl_mode == desired.mode else desired.limit_w
        drift = device_drift(st, desired.mode, desired.limit_w)
        if drift:
            # App/HEMS oder Geräte-Neustart (smartMode=1 schreibt nur ins RAM) hat die Werte geändert
            log.warning("Zendure: Gerät weicht vom Regelzustand ab (%s) – schreibe %s %d W erneut",
                        drift, desired.mode, desired.limit_w)
            desired.write = True
            desired.data = {**desired.data, "drift": drift}
    return desired


def device_drift(st: ZendureStatus, mode: str, limit_w: int) -> str:
    """Abweichung der am Gerät gemeldeten Sollwerte (acMode/inputLimit/outputLimit) vom
    Regelzustand; leer = passt oder Gerät meldet die Werte nicht."""
    props = st.props or {}
    want_in, want_out = (limit_w, 0) if mode == "charge" else (0, limit_w) if mode == "discharge" else (0, 0)
    out = []
    for key, want in (("inputLimit", want_in), ("outputLimit", want_out)):
        try:
            have = float(props[key])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(have - want) > WRITE_DELTA_W:
            out.append(f"{key} {have:.0f} statt {want}")
    want_mode = {"charge": AC_MODE_CHARGE, "discharge": AC_MODE_DISCHARGE}.get(mode)
    if want_mode is not None and props.get("acMode") is not None and props.get("acMode") != want_mode:
        out.append(f"acMode {props.get('acMode')} statt {want_mode}")
    return ", ".join(out)


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
        failsafe: Optional[str] = None,
        export_cap_w: Optional[float] = None,
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
            return Plan(ctl["mode"], ctl["limit_w"], False, text, "unreachable", code="device_unreachable",
                        data={"fails": fails})

        if ctl["fail_count"]:
            db.set_zendure_ctl(fail_count=0)

        changed_at = datetime.fromisoformat(ctl["changed_at"]) if ctl["changed_at"] else None
        since = (now - changed_at).total_seconds() if changed_at else 1e9
        p = plan(
            self.zc, st, avail_w=avail_w, import_w=import_w, wallbox_charging=wallbox_charging,
            override=override, ctl_mode=ctl["mode"], ctl_limit_w=ctl["limit_w"],
            seconds_since_change=since, hysteresis_w=hysteresis_w, base_load_w=base_load_w,
            failsafe=failsafe, export_cap_w=export_cap_w,
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
                    p.code, p.data = "write_failed", {**p.data, "wanted": p.code}
        db.set_zendure_ctl(decision=reason, decided_at=now.isoformat(timespec="seconds"))
        p.reason = reason
        return p


def force_standby(client: ZendureClient, reason: str, dry_run: bool = False) -> bool:
    """Batterie sofort in Standby (inputLimit = outputLimit = 0) und Regelzustand auf idle.

    Für Fail-safe außerhalb der normalen Regelung (Kaskade abgestürzt, Watchdog, Pi-Start).
    Bei dry_run nur Log. Liefert True, wenn geschrieben (oder dry_run)."""
    now = datetime.now().isoformat(timespec="seconds")
    if dry_run:
        log.warning("[Dry-Run] Zendure: würde Standby schreiben – %s", reason)
        return True
    ok = client.set_mode(None, input_limit=0, output_limit=0)
    try:
        if ok:
            db.set_zendure_ctl(mode="idle", limit_w=0, changed_at=now)
        # decided_at bleibt: das ist der Herzschlag der Regelung, den der Watchdog prüft
        db.set_zendure_ctl(decision=f"Standby: {reason}" if ok else f"Standby fehlgeschlagen: {reason}")
    except Exception:
        log.warning("Zendure: Regelzustand nicht speicherbar", exc_info=True)
    if ok:
        log.warning("Zendure: Standby geschrieben (Limits 0) – %s", reason)
    else:
        log.error("Zendure: Standby konnte nicht geschrieben werden – %s", reason)
    return ok


def control_age_s(now: Optional[datetime] = None) -> Optional[float]:
    """Sekunden seit dem letzten Lauf der Regelung (zendure_ctl.decided_at); None = nie/unlesbar."""
    try:
        ts = db.get_zendure_ctl().get("decided_at")
        if not ts:
            return None
        return ((now or datetime.now()) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        log.warning("Zendure: Regelzustand nicht lesbar", exc_info=True)
        return None
