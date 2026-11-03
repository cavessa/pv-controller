"""Akku-Kalibrierung: volle Zyklen mit Laden aus dem Netz (Empfehlung Zendure-Support, 11/2026).

Anlass: Das BMS begrenzt schon bei angezeigten ~80 % auf ~500 W (SoC-Offset, Pack 1 mit 160 mV
Zelldifferenz oben, unten ausgeglichen). Abhilfe: mehrere Zyklen niedrig → 100 % mit Haltezeit (Zellausgleich).
Im November reicht die PV dafür nicht – deshalb lädt die Kalibrierung gezielt aus dem Netz.

Ablauf (pro Zyklus):
1. **Laden** (``charge``): Sollwert = Kalibrier-Leistung + PV-Überschuss (höchstens Gerätemaximum und
   ``max_grid_w`` minus Hausbezug ohne Batterie) bis SoC 100 % und das Gerät nichts mehr aufnimmt
   (< ``FULL_W`` in ``FULL_RUNS`` Läufen; nimmt es ab SoC ≥ ``STUCK_SOC`` ``STUCK_S`` lang nichts mehr: auch voll).
2. **Halten** (``hold``): Haltezeit bei 100 %, Laden bleibt freigegeben (BMS gleicht aus), nie entladen.
3. **Entladen** (``discharge``): normale Regelung mit Mindest-SoC ``CALIB_MIN_SOC`` (5 %, am Gerät minSoc 50).
4. SoC ≤ 5 % → nächster Zyklus; nach dem letzten: aus, Mindest-SoC zurück (Gerät + Readback), Bericht.

In Lade- und Haltephase gelten Lade-Deckel, Wächter-Drosselung und Wallbox-Puffer nicht (Kennzeichen
``grid_charge = calib`` im Regelzustand). Lädt die Wallbox (``_wallbox_charging``: car=2 oder > 100 W, nicht
lesbar = lädt) → Pause (Standby). Abbruch (Standby, Hinweis, Mindest-SoC zurück) bei Zellspannung
> ``CELL_MAX_MV``, Pack-Temperatur > ``TEMP_MAX_C``, Fremdregler, Zendure bzw. Hauptzähler ``FAIL_RUNS``
Läufe in Folge nicht lesbar.

``step()`` ist rein (keine I/O): Zustand + Messwerte → neuer Zustand, Vorgabe für die Regelung, Ereignisse
(Mindest-SoC schreiben, Zyklus speichern, Log). Ausführen und Speichern macht ``zendure_control``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

CALIB_MIN_SOC = 5.0
FULL_W = 50                 # darunter nimmt das Gerät „nichts“ mehr auf
FULL_RUNS = 3
STUCK_SOC = 98.0            # SoC-Anzeige bleibt knapp unter 100 hängen (Offset) …
STUCK_S = 30 * 60           # … und nimmt so lange nichts mehr auf → als voll werten
CELL_MAX_MV = 3650
TEMP_MAX_C = 45.0
FAIL_RUNS = 3
MIN_START_W = 100
BMS_GAP_W = 200             # Ist so weit unter dem Sollwert …
BMS_RUNS = 3                # … so viele Läufe in Folge = BMS begrenzt
DIFF80_SOC = (78.0, 82.0)
MIN_SOC_RETRY_S = 600
MAX_STEP_S = 180            # Energie-Integration: längere Lücken nicht hochrechnen
SHOW_FINISHED_S = 7 * 86400

ACTIVE_PHASES = ("wait", "charge", "hold", "discharge")
GRID_PHASES = ("charge", "hold")
PHASE_DE = {"wait": "wartet auf Start", "charge": "Laden aus dem Netz", "hold": "Halten bei 100 %",
            "discharge": "Entladen", "done": "abgeschlossen", "aborted": "abgebrochen", "stopped": "gestoppt"}

LIMITS = {"cycles": (1, 5), "charge_w": (500, 2400), "hold_min": (0, 240), "max_grid_w": (1000, 30000)}


def validate_params(raw: dict) -> dict:
    """Parameter aus Settings/Config prüfen; ValueError mit deutschem Text bei Fehlern."""
    out: dict[str, Any] = {}
    for k, (lo, hi) in LIMITS.items():
        v = raw.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"{k} muss zwischen {lo} und {hi} liegen")
        out[k] = int(v)
    start = raw.get("start", "now")
    if start not in ("now", "at"):
        raise ValueError("start muss 'now' oder 'at' sein")
    out["start"] = start
    t = str(raw.get("start_time") or "")
    if start == "at":
        try:
            hh, mm = (int(x) for x in t.split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
        except ValueError:
            raise ValueError("start_time muss HH:MM sein")
        t = f"{hh:02d}:{mm:02d}"
    out["start_time"] = t
    return out


def params_from_config(zc) -> dict:
    return {"cycles": zc.calib_cycles, "charge_w": zc.calib_charge_w, "hold_min": zc.calib_hold_min,
            "max_grid_w": zc.calib_max_grid_w, "start": zc.calib_start, "start_time": zc.calib_start_time}


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _dt(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _new_cycle(n: int, now: datetime) -> dict:
    return {"cycle": n, "started_at": _iso(now), "full_at": None, "grid_wh": 0.0, "ac_wh": 0.0,
            "bms_soc": None, "bms_runs": 0, "bms_first_soc": None, "diff80": {}, "diff100": {}}


def begin(params: dict, now: datetime, prev_min_soc: float) -> dict:
    """Neuen Kalibrier-Zustand anlegen (Anfrage „start“ aus den Settings)."""
    s = {"active": True, "run_id": _iso(now), "params": params, "phase": "wait", "phase_since": _iso(now),
         "cycle": 0, "prev_min_soc": prev_min_soc, "min_soc_ok": False, "min_soc_try": None,
         "restore_pending": False, "fails_zd": 0, "fails_meter": 0, "full_runs": 0, "low_since": None,
         "hold_since": None, "paused": None, "last_at": None, "last_limit": 0, "cur": None,
         "abort_reason": None, "finished_at": None, "report": None, "notes": [], "wait_until": None}
    if params.get("start") == "at":
        hh, mm = (int(x) for x in params["start_time"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        s["wait_until"] = _iso(at)
    return s


@dataclass
class Inputs:
    soc: Optional[float]
    charge_w: float = 0.0
    grid_in_w: float = 0.0              # AC-Eingang des Geräts (Netz + PV)
    packs: list = field(default_factory=list)
    avail_w: float = 0.0                # PV-Überschuss an der Batterie-Position der Kaskade
    house_import_w: float = 0.0         # Netzbezug ohne Batterie (− = Einspeisung)
    meter_import_w: Optional[float] = None   # Hauptzähler jetzt inkl. Batterie (+ = Bezug)
    wallbox_charging: bool = False
    meter_ok: bool = True
    foreign_at: Optional[str] = None
    max_charge_w: int = 2400
    capacity_wh: float = 2400.0


@dataclass
class Directive:
    """Vorgabe für die Regelung: ``charge`` (fester Sollwert, aus dem Netz erlaubt), ``idle`` (Standby, hart)
    oder ``normal`` (normale Regelung, ggf. mit ``min_soc``)."""
    mode: str
    limit_w: int = 0
    code: str = ""
    reason: str = ""
    data: dict = field(default_factory=dict)
    min_soc: Optional[float] = None


def _enter(s: dict, phase: str, now: datetime, ev: list) -> None:
    s["phase"], s["phase_since"] = phase, _iso(now)
    s["full_runs"], s["low_since"], s["paused"] = 0, None, None
    ev.append(("phase", phase))
    ev.append(("log", "info", f"Kalibrierung: Zyklus {s['cycle']}/{s['params']['cycles']} – {PHASE_DE[phase]}"))


def _finish(s: dict, kind: str, now: datetime, ev: list, why: str = "") -> None:
    cur = s.get("cur")
    if cur and s["phase"] in GRID_PHASES and kind != "done":
        ev.append(("cycle", _cycle_row(s, cur, now, "aborted")))
    s.update(active=False, phase=kind, phase_since=_iso(now), finished_at=_iso(now), paused=None,
             abort_reason=why or None, cur=None)
    s["restore_pending"] = True                 # Mindest-SoC am Gerät zurück (auch wenn noch nicht geschrieben)
    s["min_soc_try"] = _iso(now)
    ev.append(("min_soc_restore", None))        # gleich in diesem Lauf, bei Fehler alle 10 min erneut
    ev.append(("phase", kind))
    if kind == "done":
        ev.append(("report", None))
        ev.append(("log", "info", "Kalibrierung abgeschlossen – Mindest-SoC zurück, Bericht erstellt"))
    elif kind == "aborted":
        ev.append(("standby", why))
        ev.append(("log", "error", f"Kalibrierung abgebrochen: {why}"))
    else:
        ev.append(("log", "info", "Kalibrierung gestoppt (Settings)"))


def _cycle_row(s: dict, cur: dict, now: datetime, kind: str) -> dict:
    st = _dt(cur.get("started_at"))
    full = _dt(cur.get("full_at"))
    return {"run_id": s["run_id"], "cycle": cur["cycle"], "started_at": cur.get("started_at"),
            "full_at": cur.get("full_at"), "ended_at": _iso(now), "end_kind": kind,
            "grid_wh": round(cur.get("grid_wh") or 0.0, 1), "ac_wh": round(cur.get("ac_wh") or 0.0, 1),
            "charge_s": int(((full or now) - st).total_seconds()) if st else None,
            "bms_soc": cur.get("bms_soc"), "diff80": cur.get("diff80") or None, "diff100": cur.get("diff100") or None}


def _pack_diffs(packs: list) -> dict:
    out = {}
    for p in packs or []:
        mx, mn = p.get("max_mv"), p.get("min_mv")
        if mx is not None and mn is not None:
            out[str(p.get("idx"))] = int(mx - mn)
    return out


def _keep_max(dst: dict, diffs: dict) -> None:
    for k, v in diffs.items():
        if v > dst.get(k, -1):
            dst[k] = v


def _safety(s: dict, inp: Inputs) -> Optional[str]:
    """Abbruchgrund in Lade-/Haltephase oder None."""
    cells = [p.get("max_mv") for p in inp.packs or [] if p.get("max_mv") is not None]
    if cells and max(cells) > CELL_MAX_MV:
        return f"Zellspannung {max(cells) / 1000:.2f} V > {CELL_MAX_MV / 1000:.2f} V"
    temps = [p.get("temp_c") for p in inp.packs or [] if p.get("temp_c") is not None]
    if temps and max(temps) > TEMP_MAX_C:
        return f"Pack-Temperatur {max(temps):.0f} °C > {TEMP_MAX_C:.0f} °C"
    fa, started = _dt(inp.foreign_at), _dt(s.get("run_id"))
    if fa is not None and started is not None and fa >= started:
        return "Fremdregler erkannt (App/HEMS)"
    if not inp.meter_ok:
        s["fails_meter"] = s.get("fails_meter", 0) + 1
        if s["fails_meter"] >= FAIL_RUNS:
            return f"Hauptzähler {FAIL_RUNS} Läufe in Folge nicht lesbar"
    else:
        s["fails_meter"] = 0
    return None


def _data(s: dict, inp: Inputs, **kw) -> dict:
    return {"cycle": s["cycle"], "cycles": s["params"]["cycles"],
            "soc": None if inp.soc is None else int(round(inp.soc)), **kw}


def _allowed_w(s: dict, inp: Inputs) -> int:
    """Netzbezug gesamt begrenzen: Batterie höchstens max_grid − Hausbezug ohne Batterie."""
    return int(s["params"]["max_grid_w"] - inp.house_import_w)


def _target_w(s: dict, inp: Inputs, base_w: float) -> int:
    t = min(base_w + max(0.0, inp.avail_w), _allowed_w(s, inp), inp.max_charge_w)
    return max(0, int(round(t / 10.0) * 10))


def step(s: Optional[dict], inp: Inputs, now: datetime) -> tuple[Optional[dict], Optional[Directive], list]:
    """Ein Minutenlauf. Liefert (Zustand, Vorgabe oder None = Kalibrierung nicht aktiv, Ereignisse)."""
    ev: list = []
    if not s:
        return s, None, ev
    if s.get("restore_pending"):
        last = _dt(s.get("min_soc_try"))
        if last is None or (now - last).total_seconds() >= MIN_SOC_RETRY_S:
            s["min_soc_try"] = _iso(now)
            ev.append(("min_soc_restore", None))
    if not s.get("active"):
        return s, None, ev
    p = s["params"]
    last_at = _dt(s.get("last_at"))
    dt_s = min(MAX_STEP_S, max(0.0, (now - last_at).total_seconds())) if last_at else 0.0
    s["last_at"] = _iso(now)
    soc = inp.soc

    if s["phase"] == "wait":
        wu = _dt(s.get("wait_until"))
        if wu is not None and now < wu:
            return s, Directive("normal", code="", reason=f"Kalibrierung startet um {wu.strftime('%H:%M')}",
                                data=_data(s, inp, start_time=wu.strftime("%H:%M"))), ev
        s["cycle"] = 1
        s["cur"] = _new_cycle(1, now)
        _enter(s, "charge", now, ev)

    # Mindest-SoC 5 % ans Gerät (einmal, bei Fehler alle 10 min erneut)
    if not s.get("min_soc_ok"):
        last = _dt(s.get("min_soc_try"))
        if last is None or (now - last).total_seconds() >= MIN_SOC_RETRY_S:
            s["min_soc_try"] = _iso(now)
            ev.append(("min_soc", CALIB_MIN_SOC))

    ph = s["phase"]
    if ph in GRID_PHASES:
        why = _safety(s, inp)
        if why:
            _finish(s, "aborted", now, ev, why)
            return s, Directive("idle", code="calib_aborted", reason=f"Kalibrierung abgebrochen: {why}",
                                data=_data(s, inp, why=why)), ev
        cur = s["cur"] = s.get("cur") or _new_cycle(s["cycle"], now)
        # Energie seit dem letzten Lauf: Netzanteil = min(AC-Eingang, Hauptzähler-Bezug)
        if dt_s and not s.get("paused"):
            cur["ac_wh"] += max(0.0, inp.grid_in_w) * dt_s / 3600.0
            if inp.meter_import_w is not None:
                cur["grid_wh"] += max(0.0, min(inp.grid_in_w, inp.meter_import_w)) * dt_s / 3600.0
        diffs = _pack_diffs(inp.packs)
        if ph == "charge":
            if soc is not None and DIFF80_SOC[0] <= soc <= DIFF80_SOC[1]:
                _keep_max(cur["diff80"], diffs)
            # BMS-Begrenzung (erstmals in diesem Zyklus): Ist deutlich unter dem Sollwert, noch nicht voll
            if cur.get("bms_soc") is None and not s.get("paused") and soc is not None and soc < 99 \
                    and s.get("last_limit", 0) - inp.charge_w >= BMS_GAP_W:
                if cur.get("bms_runs", 0) == 0:
                    cur["bms_first_soc"] = soc
                cur["bms_runs"] = cur.get("bms_runs", 0) + 1
                if cur["bms_runs"] >= BMS_RUNS:
                    cur["bms_soc"] = cur["bms_first_soc"]
                    ev.append(("log", "info", f"Kalibrierung: BMS begrenzt ab SoC {cur['bms_soc']:.0f} % "
                                              f"(Ist {inp.charge_w:.0f} W, Sollwert {s['last_limit']} W)"))
            elif cur.get("bms_soc") is None:
                cur["bms_runs"] = 0
            # voll?
            if not s.get("paused") and soc is not None and inp.charge_w < FULL_W:
                if soc >= 100:
                    s["full_runs"] = s.get("full_runs", 0) + 1
                else:
                    s["full_runs"] = 0
                if soc >= STUCK_SOC and s.get("low_since") is None:
                    s["low_since"] = _iso(now)
                stuck = soc >= STUCK_SOC and (now - _dt(s["low_since"])).total_seconds() >= STUCK_S
                if s["full_runs"] >= FULL_RUNS or stuck:
                    if stuck and s["full_runs"] < FULL_RUNS:
                        s.setdefault("notes", []).append(
                            f"Zyklus {s['cycle']}: SoC blieb bei {soc:.0f} %, Gerät nahm {STUCK_S // 60} min "
                            f"nichts mehr auf – als voll gewertet")
                    cur["full_at"] = _iso(now)
                    s["hold_since"] = _iso(now)
                    _keep_max(cur["diff100"], diffs)
                    _enter(s, "hold", now, ev)
                    ph = "hold"
            elif not s.get("paused"):
                s["full_runs"], s["low_since"] = 0, None
        if ph == "hold":
            _keep_max(cur["diff100"], diffs)
            left = p["hold_min"] * 60 - (now - _dt(s["hold_since"])).total_seconds()
            if left <= 0:
                ev.append(("cycle", _cycle_row(s, cur, now, "full")))
                s["cur"] = None
                _enter(s, "discharge", now, ev)
                ph = "discharge"
        if ph in GRID_PHASES:
            if inp.wallbox_charging:
                s["paused"] = "wallbox"
                s["last_limit"] = 0
                return s, Directive("idle", code="calib_paused_wallbox",
                                    reason="Kalibrierung pausiert: Wallbox lädt (nicht beides aus dem Netz)",
                                    data=_data(s, inp, phase=PHASE_DE[ph])), ev
            base = p["charge_w"]
            target = _target_w(s, inp, base)
            if target < MIN_START_W:
                s["paused"] = "grid"
                s["last_limit"] = 0
                return s, Directive("idle", code="calib_paused_grid",
                                    reason=f"Kalibrierung pausiert: Haus zieht schon {inp.house_import_w:.0f} W "
                                           f"(Grenze {p['max_grid_w']} W gesamt)",
                                    data=_data(s, inp, house_w=int(inp.house_import_w),
                                               max_grid_w=p["max_grid_w"])), ev
            s["paused"] = None
            s["last_limit"] = target
            grid_w = max(0, target - int(max(0.0, inp.avail_w)))
            if ph == "charge":
                eta = None
                if soc is not None and target > 0:
                    eta = int(round(max(0.0, 100.0 - soc) / 100.0 * inp.capacity_wh / target * 60))
                reduced = " (Netzgrenze)" if target < min(base + max(0.0, inp.avail_w), inp.max_charge_w) else ""
                return s, Directive("charge", target, "calib_charge_grid",
                                    f"Kalibrierung Zyklus {s['cycle']}/{p['cycles']}: lädt mit {target} W "
                                    f"(Netz {grid_w} W{reduced}) bis 100 %",
                                    _data(s, inp, watts=target, grid_w=grid_w, eta_min=eta)), ev
            left_min = max(0, int(round(left / 60)))
            return s, Directive("charge", target, "calib_hold_full",
                                f"Kalibrierung Zyklus {s['cycle']}/{p['cycles']}: hält bei 100 % (noch {left_min} min)",
                                _data(s, inp, remaining_min=left_min, watts=target)), ev

    # Entladen bis 5 %
    if soc is not None and soc <= CALIB_MIN_SOC:
        if s["cycle"] < p["cycles"]:
            s["cycle"] += 1
            s["cur"] = _new_cycle(s["cycle"], now)
            _enter(s, "charge", now, ev)
            s, d, ev2 = step(s, inp, now)          # gleicher Lauf: gleich die Lade-Vorgabe
            return s, d, ev + ev2
        _finish(s, "done", now, ev)
        return s, None, ev
    return s, Directive("normal", code="calib_discharge",
                        reason=f"Kalibrierung Zyklus {s['cycle']}/{p['cycles']}: Entladen bis "
                               f"{CALIB_MIN_SOC:.0f} %",
                        data=_data(s, inp, min_soc=int(CALIB_MIN_SOC)), min_soc=CALIB_MIN_SOC), ev


def on_unreadable(s: Optional[dict], now: datetime) -> tuple[Optional[dict], list]:
    """Zendure in diesem Lauf nicht lesbar: in Lade-/Haltephase nach FAIL_RUNS Läufen abbrechen."""
    ev: list = []
    if not s or not s.get("active"):
        return s, ev
    s["fails_zd"] = s.get("fails_zd", 0) + 1
    if s["phase"] in GRID_PHASES and s["fails_zd"] >= FAIL_RUNS:
        _finish(s, "aborted", now, ev, f"Zendure {FAIL_RUNS} Läufe in Folge nicht lesbar")
    return s, ev


def device_min_soc(s: Optional[dict], config_min_soc: float) -> float:
    """Mindest-SoC, den das Gerät gerade haben soll (für die Fremdregler-Prüfung)."""
    if s and ((s.get("active") and s.get("min_soc_ok")) or s.get("restore_pending")):
        return CALIB_MIN_SOC
    return config_min_soc


def is_active(s: Optional[dict]) -> bool:
    return bool(s and s.get("active"))


def status(s: Optional[dict], cycles_rows: Optional[list] = None, now: Optional[datetime] = None,
           pending: Optional[dict] = None) -> dict:
    """Für Dashboard/Settings: aktiv, Phase, Zyklus, Text; nach dem Ende Bericht/Abbruchgrund (7 Tage)."""
    now = now or datetime.now()
    out: dict[str, Any] = {"active": False, "show": False, "pending": (pending or {}).get("action")}
    if not s:
        return out
    fin = _dt(s.get("finished_at"))
    out.update(active=bool(s.get("active")), phase=s.get("phase"), phase_de=PHASE_DE.get(s.get("phase"), ""),
               cycle=s.get("cycle"), cycles=s["params"]["cycles"], params=s["params"], run_id=s.get("run_id"),
               paused=s.get("paused"), abort_reason=s.get("abort_reason"), report=s.get("report"),
               finished_at=s.get("finished_at"), notes=s.get("notes") or [], wait_until=s.get("wait_until"),
               min_soc_ok=bool(s.get("min_soc_ok")), restore_pending=bool(s.get("restore_pending")),
               hold_since=s.get("hold_since"), last_limit=s.get("last_limit"),
               cycles_done=cycles_rows or [])
    out["show"] = out["active"] or (fin is not None and (now - fin).total_seconds() < SHOW_FINISHED_S)
    return out


def _fmt_diffs(d: Optional[dict]) -> str:
    if not d:
        return "–"
    return "/".join(str(d.get(k, "–")) for k in sorted(d, key=lambda x: int(x))) + " mV"


def report(rows: list[dict], baseline_soc: Optional[float], notes: Optional[list] = None) -> str:
    """Kurzbericht nach Abschluss: BMS-Begrenzungspunkt und Zelldifferenz Pack 1, erster vs. letzter Zyklus."""
    if not rows:
        return "Keine abgeschlossenen Zyklen."
    lines = []
    for r in rows:
        bms = "–" if r.get("bms_soc") is None else f"{r['bms_soc']:.0f} %"
        lines.append(f"Zyklus {r['cycle']}: Netz {(r.get('grid_wh') or 0) / 1000:.2f} kWh, "
                     f"Laden {round((r.get('charge_s') or 0) / 3600, 1)} h, "
                     f"BMS ab {bms}, "
                     f"Differenz bei ~80 % {_fmt_diffs(r.get('diff80'))}, bei 100 % {_fmt_diffs(r.get('diff100'))}"
                     + (" (abgebrochen)" if r.get("end_kind") == "aborted" else ""))
    full = [r for r in rows if r.get("end_kind") == "full"]
    first, last = (full[0], full[-1]) if full else (rows[0], rows[-1])
    verdict = []
    b0 = baseline_soc if baseline_soc is not None else first.get("bms_soc")
    b1 = last.get("bms_soc")
    if b1 is None:
        verdict.append("BMS-Begrenzung im letzten Zyklus nicht mehr erkannt (vor 99 %) – Ziel erreicht")
    elif b0 is not None:
        trend = "nach oben verschoben" if b1 > b0 + 2 else "unverändert" if abs(b1 - b0) <= 2 else "nach unten verschoben"
        verdict.append(f"BMS-Begrenzung {trend}: vorher ab {b0:.0f} %, im letzten Zyklus ab {b1:.0f} %"
                       + (" – Ziel (kurz vor 100 %) erreicht" if b1 >= 95 else ""))
    for key, label in (("diff80", "~80 %"), ("diff100", "100 %")):
        d0, d1 = (first.get(key) or {}).get("1"), (last.get(key) or {}).get("1")
        if d0 is not None and d1 is not None and first is not last:
            verdict.append(f"Pack 1 Differenz bei {label}: {d0} → {d1} mV "
                           + ("(kleiner)" if d1 < d0 - 10 else "(größer)" if d1 > d0 + 10 else "(gleich)"))
    return "\n".join(lines + verdict + list(notes or []))
