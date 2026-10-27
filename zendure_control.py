"""Zendure-Regelung (Phase 2): Laden aus PV-Überschuss, Entladen bei Netzbezug.

`plan()` ist eine reine Funktion (testbar, keine I/O): sie bekommt Zustand und
Messwerte und liefert die gewünschte Betriebsart samt Limit. `ZendureControl`
liest/schreibt den persistierten Regelzustand (Tabelle zendure_ctl) und sendet
die Befehle über den ZendureClient.

Regeln:
- Laden nur aus dem Überschuss, der der Batterie in der Kaskade zusteht
  (`avail_w`); nie aus dem Netz. Stufenlos, Start ab 100 W + Hysterese.
- Entladen nur ohne Laden, mit SoC > Mindest-SoC und Netzbezug; geregelt auf einen kleinen
  Rest-Bezug ``discharge_target_grid_w`` (Default 40 W; Ziel = aktuelle Abgabe + Bezug − Rest,
  Puffer gegen Einspeisung aus der Batterie, wenn eine Last abschaltet), begrenzt auf die **Grundlast**
  (Haus ohne Heizstab und Wallbox) – die Batterie heizt nie das Wasser.
- Wallbox lädt (go-e car=2 oder > 100 W, nicht lesbar = lädt) -> Entladen hart gesperrt
  (fest, nicht abschaltbar). Vor jedem Entlade-Befehl wird go-e unter dem Schreib-Lock neu
  gelesen; der Wächter (zendure_watchdog.py) prüft beim Entladen alle 3 s.
- Mindest-SoC: Entladen stoppt bei `min_soc`, Freigabe erst ab `min_soc` + 2 %; SoC
  unbekannt -> kein Entladen. Am Gerät zusätzlich `minSoc` (Flash) als Schutz ohne Pi.
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
- Fremdregler (App-Energieplan/HEMS, 16.10.2026 mehrfach beobachtet): Gerätewerte ändern sich
  ohne eigenen Schreibbefehl (``FOREIGN_GRACE_S`` nach dem letzten eigenen Befehl) oder das Gerät
  entlädt gegen den Regelzustand → ``record_foreign`` (Log, Dashboard-Hinweis); der Wächter
  schreibt dann sofort Standby. Weicht ``minSoc`` am Gerät von der Config ab, wird er
  nachgeschrieben (Flash, höchstens einmal pro Stunde).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

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
EMPTY_SHOW_CHARGE_REASON_W = 20  # Anzeige: leere Batterie nennt ab so viel Überschuss den Lade-Grund statt „leer“
MIN_SOC_HYST = 2                # Entladen nach Erreichen des Mindest-SoC erst ab Mindest-SoC + 2 % wieder
MIN_SOC_CHOICES = (5, 10, 15, 20)
WRITE_LOCK_PATH = "/tmp/pvc-zendure-write.lock"   # gemeinsam für Minutenlauf und Wächter
WRITE_LOCK_WAIT_S = 10.0


@contextlib.contextmanager
def write_guard(wait_s: float = WRITE_LOCK_WAIT_S) -> Iterator[bool]:
    """Exklusives Schreib-Lock für alle Zendure-Befehle (Minutenlauf, Wächter, Settings).

    Liefert True, wenn das Lock gehalten wird; nach ``wait_s`` ohne Lock False (der Aufrufer
    schreibt dann trotzdem nur Standby, nie Entladen)."""
    f = open(WRITE_LOCK_PATH, "a+")
    got = False
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    log.warning("Zendure: Schreib-Lock nach %.0f s nicht erhalten", wait_s)
                    break
                time.sleep(0.1)
        yield got
    finally:
        if got:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
DEVICE_MAX_W = 2400
FOREIGN_GRACE_S = 10.0          # so lange nach einem eigenen Befehl keine Fremdregler-Prüfung
FOREIGN_ACTIVE_S = 15 * 60      # Hinweis bleibt so lange nach der letzten Erkennung stehen
MIN_SOC_REFIX_S = 3600          # minSoc höchstens einmal pro Stunde nachschreiben (Flash)
UNLIMITED = float("inf")        # Grundlast nicht begrenzt (Tests/Aufrufer ohne Grundlast-Angabe)

# Aufnahmegrenze: Die Batterie nimmt weniger auf als geschrieben (BMS – Zelle voll/Ausgleich eines Packs,
# Temperatur; z. B. 27.10.2026 ab 80 % nur ~505 W bei inputLimit 2400). Nach ACCEPT_RUNS Läufen in Folge
# mit ≥ ACCEPT_GAP_W Lücke reserviert die Kaskade nur noch Ist + ACCEPT_HEADROOM_W statt des Wunschwerts.
# Der Puffer bleibt für die Batterie frei: nimmt sie ihn wieder auf, schließt sich die Lücke und es gilt
# sofort wieder der volle Wunschwert (Heizstab macht Platz).
ACCEPT_GAP_W = 200
ACCEPT_RUNS = 3
ACCEPT_HEADROOM_W = 300
ACCEPT_RISE_W = 150             # so viel über dem Plateau = nimmt wieder mehr auf → Grenze aufheben
ACCEPT_RECHECK_S = 15 * 60      # spätestens dann neu prüfen (volle Anforderung, Heizstab macht Platz)
ACCEPT_STATE_NAME = "battery_accept_state.json"   # neben der DB (Tests: eigene Temp-DB → eigener Zustand)


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
    elif wallbox_charging:                      # fest, nicht abschaltbar: nie ins Auto entladen
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
        elif st.soc is None:
            desired = Plan("idle", 0, False, "SoC unbekannt – kein Entladen", "idle",
                           code="battery_empty", data={**nums, "min_soc": zc.min_soc})
        elif (soc <= zc.min_soc or (ctl_mode != "discharge" and soc < zc.min_soc + MIN_SOC_HYST)) \
                and code_c == "below_start_threshold" and avail_w > EMPTY_SHOW_CHARGE_REASON_W:
            # Nur Anzeige: Überschuss da, aber unter der Lade-Startschwelle – das ist der Grund, nicht „leer“
            desired = Plan("idle", 0, False, f"{reason_c} (Batterie leer, SoC {soc:.0f} %)", "idle",
                           code="below_start_threshold", data={**nums, **data_c, "min_soc": zc.min_soc})
        elif soc <= zc.min_soc or (ctl_mode != "discharge" and soc < zc.min_soc + MIN_SOC_HYST):
            # stoppt bei Mindest-SoC, Freigabe erst wieder ab Mindest-SoC + Hysterese
            again = "" if soc <= zc.min_soc else f", Freigabe ab {zc.min_soc + MIN_SOC_HYST:.0f} %"
            desired = Plan("idle", 0, False,
                           f"Batterie leer (SoC {soc:.0f} % ≤ Mindest-SoC {zc.min_soc:.0f} %{again})"
                           if not again else f"Batterie fast leer (SoC {soc:.0f} %{again})", "idle",
                           code="battery_empty", data={**nums, "min_soc": zc.min_soc})
        elif base_load_w is None:
            desired = Plan("idle", 0, False, "Grundlast unbekannt (PV/Hauszähler nicht lesbar) – kein Entladen", "idle",
                           code="base_load_unknown", data=dict(nums))
        elif import_w > DISCHARGE_START_IMPORT_W or ctl_mode == "discharge":
            # Entladen nur für die Grundlast (Haus ohne Heizstab/Wallbox), nie in den Heizstab
            cap_w = min(max_d, base_load_w)
            keep_w = max(0.0, float(zc.discharge_target_grid_w or 0.0))     # Rest-Bezug als Puffer
            target_d = max(0, min(_round10(st.home_out_w + import_w - keep_w), cap_w)) \
                if ctl_mode == "discharge" else max(0, min(_round10(import_w - keep_w), cap_w))
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



def charge_acceptance(st: Optional[ZendureStatus], ctl_mode: str, ctl_limit_w: int,
                      state_path: Optional[Path] = None, now: Optional[datetime] = None) -> Optional[int]:
    """Einmal pro Kaskadenlauf (vor dem Schreiben) aufrufen: ``ctl_*`` ist der Sollwert, der seit dem
    letzten Lauf am Gerät gilt. Liefert die Leistung, die für die Batterie reserviert werden soll,
    wenn sie seit ACCEPT_RUNS Läufen deutlich weniger aufnimmt als angeboten; sonst None.

    Ist die Grenze erkannt und senkt danach der eigene Einspeise-Deckel den Sollwert (Lücke scheinbar
    weg), bleibt sie bestehen – bis die Batterie wirklich mehr aufnimmt (Plateau + ACCEPT_RISE_W) oder
    ACCEPT_RECHECK_S ohne bestätigte Lücke vergangen sind (dann neu prüfen)."""
    path = state_path or Path(db.DB_PATH).resolve().parent / ACCEPT_STATE_NAME
    now = now or datetime.now()
    try:
        old = json.loads(path.read_text()) if path.exists() else {}
        runs, plateau = int(old.get("runs", 0)), float(old.get("plateau_w") or 0.0)
        confirmed = datetime.fromisoformat(old["confirmed_at"]) if old.get("confirmed_at") else None
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        runs, plateau, confirmed = 0, 0.0, None
    charging = st is not None and ctl_mode == "charge" and ctl_limit_w > 0
    if charging and ctl_limit_w - st.charge_w >= ACCEPT_GAP_W:
        runs, plateau, confirmed = runs + 1, st.charge_w, now
    elif (charging and runs >= ACCEPT_RUNS and st.charge_w < plateau + ACCEPT_RISE_W
          and confirmed is not None and (now - confirmed).total_seconds() < ACCEPT_RECHECK_S):
        pass                                  # eigener Deckel verdeckt die Lücke – Grenze gilt weiter
    else:
        runs, plateau, confirmed = 0, 0.0, None
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"runs": runs, "plateau_w": round(plateau), "limit_w": ctl_limit_w,
                                   "confirmed_at": confirmed.isoformat(timespec="seconds") if confirmed else None}))
        os.replace(tmp, path)
    except OSError:
        log.warning("Zendure: Aufnahme-Zustand nicht speicherbar", exc_info=True)
    if runs >= ACCEPT_RUNS:
        return int(round(plateau)) + ACCEPT_HEADROOM_W
    return None

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


def _mode_text(mode: str, limit_w: int) -> str:
    return {"charge": f"Laden {limit_w} W", "discharge": f"Entladen {limit_w} W"}.get(mode, "Standby")


def foreign_deviation(st: ZendureStatus, mode: str, limit_w: int, *, target_soc: float = 100.0,
                      min_soc: float = 0.0) -> str:
    """Hat jemand anderes die Sollwerte gesetzt? Leer = nein (oder Gerät meldet sie nicht).

    Anders als ``device_drift`` zählt acMode allein nicht, und ein niedrigeres Limit nicht dort,
    wo das Gerät selbst stoppt (Laden bei SoC ≥ Ziel − 1, Entladen bei SoC ≤ Mindest-SoC + 1)."""
    props = st.props or {}
    try:
        have_in, have_out = float(props["inputLimit"]), float(props["outputLimit"])
    except (KeyError, TypeError, ValueError):
        return ""
    want_in = limit_w if mode == "charge" else 0
    want_out = limit_w if mode == "discharge" else 0
    soc = st.soc
    out = []
    if have_out - want_out > WRITE_DELTA_W:
        out.append(f"Gerät entlädt bis {have_out:.0f} W" if mode != "discharge"
                   else f"outputLimit {have_out:.0f} statt {want_out}")
    elif want_out - have_out > WRITE_DELTA_W and not (soc is not None and soc <= min_soc + 1):
        out.append(f"outputLimit {have_out:.0f} statt {want_out}")
    if have_in - want_in > WRITE_DELTA_W:
        out.append(f"inputLimit {have_in:.0f} statt {want_in}")
    elif want_in - have_in > WRITE_DELTA_W and not (soc is not None and soc >= target_soc - 1):
        out.append(f"inputLimit {have_in:.0f} statt {want_in}")
    if not out:
        return ""
    return ", ".join(out) + f" (Regelung: {_mode_text(mode, limit_w)})"


def _age_s(ts: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not ts:
        return None
    try:
        return ((now or datetime.now()) - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return None


def record_foreign(detail: str, source: str) -> bool:
    """Fremdregler-Erkennung speichern (Dashboard) und loggen; True = neue Episode."""
    now = datetime.now()
    ctl = db.get_zendure_ctl()
    age = _age_s(ctl.get("foreign_at"), now)
    new = age is None or age > FOREIGN_ACTIVE_S
    iso = now.isoformat(timespec="seconds")
    upd = {"foreign_at": iso, "foreign_detail": f"{detail} – {source}"}
    if new:
        upd["foreign_since"] = iso
        log.error("Zendure: Fremdregler aktiv – %s (%s). HEMS / Energieplan in der Zendure-App prüfen", detail, source)
    else:
        log.info("Zendure: Fremdregler weiter aktiv – %s (%s)", detail, source)
    db.set_zendure_ctl(**upd)
    return new


def foreign_status(ctl: Optional[dict] = None) -> dict:
    """Für das Dashboard: active (letzte Erkennung < FOREIGN_ACTIVE_S), since, last, detail."""
    ctl = ctl if ctl is not None else db.get_zendure_ctl()
    age = _age_s(ctl.get("foreign_at"))
    return {"active": age is not None and age <= FOREIGN_ACTIVE_S, "since": ctl.get("foreign_since"),
            "last": ctl.get("foreign_at"), "detail": ctl.get("foreign_detail")}


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
        wallbox_check: Optional[Callable[[], bool]] = None,
    ) -> Plan:
        """``wallbox_check``: liest go-e frisch (True = lädt oder nicht lesbar); wird unter dem
        Schreib-Lock unmittelbar vor jedem Entlade-Befehl aufgerufen."""
        ctl = db.get_zendure_ctl()
        now = datetime.now()

        if st is None:
            fails = ctl["fail_count"] + 1
            db.set_zendure_ctl(fail_count=fails)
            text = f"Batterie nicht erreichbar ({min(fails, MAX_FAILS)}/{MAX_FAILS})"
            if wallbox_charging and ctl["mode"] == "discharge":
                # Wallbox lädt: nicht auf den 3. Fehler warten – Standby blind schreiben
                force_standby(self.client, "Wallbox lädt, Batterie nicht lesbar", self.dry_run)
                text += " – Wallbox lädt: Standby geschrieben"
            elif fails >= MAX_FAILS and (ctl["mode"] != "idle" or ctl["limit_w"] != 0):
                # neutral stellen; schlägt das fehl, wird beim nächsten Lauf erneut versucht
                with write_guard():
                    ok = not self.dry_run and self.client.set_mode(None, input_limit=0, output_limit=0)
                if ok:
                    db.set_zendure_ctl(mode="idle", limit_w=0, changed_at=now.isoformat(timespec="seconds"),
                                       written_at=datetime.now().isoformat(timespec="seconds"))
                    text += " – neutral gestellt (Limits 0)"
            db.set_zendure_ctl(decision=text, decided_at=now.isoformat(timespec="seconds"))
            return Plan(ctl["mode"], ctl["limit_w"], False, text, "unreachable", code="device_unreachable",
                        data={"fails": fails})

        if ctl["fail_count"]:
            db.set_zendure_ctl(fail_count=0)
        if not self.dry_run:
            try:
                self._check_foreign(st, ctl, now)
            except Exception:
                log.warning("Zendure: Fremdregler-Prüfung fehlgeschlagen", exc_info=True)

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
                with write_guard() as locked:
                    if p.mode == "discharge" and (not locked or (wallbox_check is not None and wallbox_check())):
                        # Auto hat zwischen Planung und Schreiben angefangen (oder go-e nicht lesbar,
                        # oder kein Lock): nie entladen – Standby
                        why = "Wallbox lädt (vor dem Schreiben geprüft)" if locked else "Schreib-Lock belegt"
                        log.warning("Zendure: Entladen verworfen – %s → Standby", why)
                        p = Plan("idle", 0, True, f"Batterie-Entladung gesperrt: {why}", "blocked", hard=True,
                                 code="wallbox_lock", data={**p.data, "mode": "Entladen gesperrt"})
                        reason = p.reason
                    if p.mode == "charge":
                        ok = self.client.set_mode(AC_MODE_CHARGE, input_limit=p.limit_w, output_limit=0)
                    elif p.mode == "discharge":
                        ok = self.client.set_mode(AC_MODE_DISCHARGE, input_limit=0, output_limit=p.limit_w)
                    else:
                        ok = self.client.set_mode(None, input_limit=0, output_limit=0)
                if ok:
                    upd = dict(mode=p.mode, limit_w=p.limit_w, written_at=datetime.now().isoformat(timespec="seconds"))
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

    def _check_foreign(self, st: ZendureStatus, ctl: dict, now: datetime) -> None:
        """Fremdregler (Sollwerte ohne eigenen Befehl geändert) und minSoc am Gerät prüfen."""
        age = _age_s(ctl.get("written_at"), now)
        if age is None or age >= FOREIGN_GRACE_S:
            dev = foreign_deviation(st, ctl["mode"], ctl["limit_w"], target_soc=self.zc.target_soc,
                                    min_soc=self.zc.min_soc)
            if dev:
                record_foreign(dev, "Minutenlauf")
        have = st.props.get("minSoc") if st.props else None
        try:
            have = int(float(have))
        except (TypeError, ValueError):
            return
        want = int(round(float(self.zc.min_soc) * 10))
        if have == want:
            return
        record_foreign(f"Mindest-SoC am Gerät {have / 10:g} % statt {self.zc.min_soc:g} %", "Minutenlauf")
        fixed = _age_s(ctl.get("min_soc_fixed_at"), now)
        if fixed is None or fixed >= MIN_SOC_REFIX_S:
            db.set_zendure_ctl(min_soc_fixed_at=now.isoformat(timespec="seconds"))
            apply_min_soc(self.client, self.zc.min_soc)


def force_standby(client: ZendureClient, reason: str, dry_run: bool = False) -> bool:
    """Batterie sofort in Standby (inputLimit = outputLimit = 0) und Regelzustand auf idle.

    Für Fail-safe außerhalb der normalen Regelung (Kaskade abgestürzt, Watchdog, Pi-Start).
    Bei dry_run nur Log. Liefert True, wenn geschrieben (oder dry_run)."""
    now = datetime.now().isoformat(timespec="seconds")
    if dry_run:
        log.warning("[Dry-Run] Zendure: würde Standby schreiben – %s", reason)
        return True
    with write_guard():
        ok = client.set_mode(None, input_limit=0, output_limit=0)
    try:
        if ok:
            db.set_zendure_ctl(mode="idle", limit_w=0, changed_at=now, written_at=now)
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


def apply_min_soc(client: ZendureClient, pct: float) -> dict:
    """Mindest-SoC ans Gerät (``minSoc`` in 0,1 %, in den Flash: smartMode 0) und zurücklesen.

    Nur bei Änderung in den Settings aufrufen (Flash-Schreibzyklen). Liefert
    {"ok": bool, "written": int, "read": int|None, "error": str|None}."""
    want = int(round(float(pct) * 10))
    with write_guard():
        ok = client.write_props({"smartMode": 0, "minSoc": want})
    if not ok:
        return {"ok": False, "written": want, "read": None, "error": "Schreiben fehlgeschlagen"}
    time.sleep(READBACK_WAIT_S)
    st = client.get_status(fresh=True)
    have = None
    if st is not None:
        try:
            have = int(float(st.props.get("minSoc")))
        except (TypeError, ValueError):
            have = None
    res = {"ok": have == want, "written": want, "read": have,
           "error": None if have == want else f"Gerät meldet minSoc={have} statt {want}"}
    (log.info if res["ok"] else log.error)("Zendure: Mindest-SoC %s %% ans Gerät – %s", pct,
                                           "bestätigt" if res["ok"] else res["error"])
    return res


READBACK_WAIT_S = 3.0
