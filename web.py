"""FastAPI-App: lokale Weboberfläche für den PV-Controller.

Wichtig:
- /api/status ist *read-only* und schaltet niemals. Es liefert den letzten
  Snapshot eines Hintergrund-Pollers (kein Geräte-Roundtrip im Request-Pfad).
- /api/run-once führt einen NORMAL-Lauf aus (respektiert runtime.enabled +
  runtime.dry_run wie `python main.py`).
- Optionale Basic Auth via auth_enabled/auth_user/auth_password in config.json.
  /api/health und /api/status sind immer offen (Docker Healthcheck).
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clients.goe_client import GoeClient, GoeError
from config import Config, load_config
from controller import ControllerMode, run_controller
from db import (
    PROTECTED_CASCADE_IDS,
    VALID_CASCADE_TYPES,
    clear_cascade_override,
    create_cascade_device,
    delete_cascade_device,
    get_cascade_device,
    get_cascade_devices,
    get_cascade_log,
    get_cascade_settings,
    get_phase_logs,
    reorder_cascade_devices,
    set_cascade_override,
    update_cascade_device,
    update_cascade_settings,
)
from models import (
    ControllerResult,
    PhaseDecision,
    TempStatus,
    WallboxAction,
    WallboxDecision,
    WallboxStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
WEB_DIR = PROJECT_ROOT / "web"
TEMP_HISTORY_PATH = PROJECT_ROOT / "temp_history.json"

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
log = logging.getLogger("pv-controller.web")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    threading.Thread(target=_status_poll_loop, name="status-poller", daemon=True).start()
    yield


app = FastAPI(title="PV-Controller Web", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ---- Auth middleware -------------------------------------------------------

_AUTH_OPEN_PATHS = {"/api/health", "/api/status"}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if request.url.path in _AUTH_OPEN_PATHS:
        return await call_next(request)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
            _raw = json.load(_f)
    except Exception:
        return await call_next(request)
    if not _raw.get("auth_enabled", False):
        return await call_next(request)
    expected_user = _raw.get("auth_user", "admin")
    expected_pass = _raw.get("auth_password", "")
    if not expected_pass:
        log.warning("auth_enabled=true but auth_password is empty — skipping auth")
        return await call_next(request)
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            if (secrets.compare_digest(user.encode(), expected_user.encode())
                    and secrets.compare_digest(pw.encode(), expected_pass.encode())):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="PV Controller"'},
    )


# ---- Helpers --------------------------------------------------------------


def _load_cfg() -> Config:
    return load_config(CONFIG_PATH)


def _decision_to_dict(d: PhaseDecision) -> dict[str, Any]:
    return {
        "phase": d.name,
        "current_state": d.current_state,
        "action": d.action.value,
        "reason": d.reason,
        "executed": d.executed,
        "execution_error": d.execution_error,
    }


def _wallbox_status_to_dict(s: Optional[WallboxStatus]) -> Optional[dict[str, Any]]:
    if s is None:
        return None
    return {
        "fup": s.pv_surplus_active,
        "lmo": s.logic_mode,
        "frc": s.force_state,
        "alw": s.allowed,
        "car": s.car_state,
        "amp": s.amp,
        "psm": s.psm,
        "power_w": s.power_w,
        "charging": s.car_state == 2,
        "energy_session_wh": s.energy_session_wh,
        "energy_total_wh": s.energy_total_wh,
        "charge_duration_s": s.charge_duration_s,
    }


def _wallbox_decision_to_dict(
    d: Optional[WallboxDecision],
) -> Optional[dict[str, Any]]:
    if d is None:
        return None
    return {
        "action": d.action.value,
        "target_force_state": d.target_force_state,
        "reason": d.reason,
        "executed": d.executed,
        "execution_error": d.execution_error,
        "target_logic_mode": d.target_logic_mode,
        "logic_mode_executed": d.logic_mode_executed,
        "logic_mode_execution_error": d.logic_mode_execution_error,
        "target_amp": d.target_amp,
        "amp_executed": d.amp_executed,
        "amp_execution_error": d.amp_execution_error,
        "target_psm": d.target_psm,
        "psm_executed": d.psm_executed,
        "psm_execution_error": d.psm_execution_error,
    }


def _summary(result: ControllerResult, cfg: Config) -> dict[str, str]:
    """Eine kompakte UI-Zusammenfassung des aktuellen Zustands."""
    r = result.readings
    wd = result.wallbox_decision
    ws = result.wallbox_status
    temp = r.storage_temp_c

    if not result.controller_active:
        return {
            "state": "Beobachtungsmodus",
            "reason": "Controller ist deaktiviert (runtime.enabled=false). "
            "Werte werden nur gelesen.",
            "severity": "warn",
        }

    if result.fail_safe_active:
        return {
            "state": "Lesefehler",
            "reason": "Lesefehler bei: " + ", ".join(r.errors)
            + ". Fail-safe – keine Schaltbefehle.",
            "severity": "error",
        }
    if r.unreachable_phases:
        names = ", ".join(p.upper().replace("PH", "Phase ") for p in r.unreachable_phases)
        return {
            "state": "Phase nicht erreichbar",
            "reason": f"{names} antwortet nicht und wird nicht geschaltet; die übrigen Phasen "
            "laufen normal weiter. Maximaltemperatur wird weiter erzwungen.",
            "severity": "warn",
        }
    if r.errors:
        return {
            "state": "Lesefehler (transient)",
            "reason": "Kurzer Lesefehler bei: " + ", ".join(r.errors)
            + ". Letzter bekannter Wert wird verwendet.",
            "severity": "warn",
        }

    if wd is not None and wd.action is WallboxAction.ERROR:
        return {
            "state": "Wallbox unerreichbar",
            "reason": wd.reason,
            "severity": "error",
        }

    if result.summer_mode_heating:
        sm = cfg.summer_mode
        state = "Netzbezug-Heizung"
        reason = (
            result.summer_mode_reason
            or f"Sommermodus: Heizstab aus Netz "
               f"(Minimum {sm.min_temp:.0f} °C, Ziel {sm.target_temp:.0f} °C)."
        )
        severity = "warn"
    elif result.temp_status is TempStatus.AT_OR_ABOVE_MAX:
        state = "Wasserspeicher voll"
        reason = (
            f"Wasserspeicher {temp:.1f} °C ≥ {cfg.heater.storage_max_temp:.0f} °C. "
            "Heizstab aus."
        )
        severity = "ok"
    elif result.temp_status is TempStatus.HYSTERESIS_BAND:
        state = "Hysterese"
        reason = (
            f"Wasserspeicher {temp:.1f} °C im Hysteresefenster "
            f"({cfg.heater.heat_resume_temp:.0f}–{cfg.heater.storage_max_temp:.0f} °C). "
            "Keine neuen Phasen."
        )
        severity = "ok"
    else:  # BELOW_RESUME
        state = "Wasserspeicher priorisiert"
        reason = (
            f"Wasserspeicher {temp:.1f} °C unter "
            f"{cfg.heater.heat_resume_temp:.0f} °C. "
            "Heizstab darf per PV-Überschuss laden."
        )
        severity = "ok"

    if wd is not None:
        if wd.action is WallboxAction.SKIPPED and ws is not None and not ws.pv_surplus_active:
            reason += " Wallbox läuft im manuellen Modus und wird nicht angefasst."
        elif wd.action in (WallboxAction.PAUSE, WallboxAction.UNCHANGED) and (
            ws is not None and ws.force_state == 1
        ):
            reason += " Wallbox ist im Eco-Modus pausiert."
        elif wd.action is WallboxAction.RELEASE or (
            ws is not None and ws.force_state == 0 and ws.pv_surplus_active
        ):
            reason += " Wallbox ist freigegeben."

    # Batterie-Entscheidung (Phase 2) an die Begründung hängen
    if cfg.zendure_active and cfg.zendure.control_enabled:
        try:
            from db import get_zendure_ctl
            zc = get_zendure_ctl()
            if zc["decision"] and zc["decided_at"] and (
                datetime.now() - datetime.fromisoformat(zc["decided_at"])
            ).total_seconds() < 600:
                reason += " " + zc["decision"] + ("." if not zc["decision"].endswith((".", ")")) else "")
        except Exception:
            log.debug("Batterie-Entscheidung nicht lesbar", exc_info=True)

    return {"state": state, "reason": reason, "severity": severity}


from meter import compute_meter_today  # noqa: E402


def _zendure_status_block(cfg: Config, r) -> dict[str, Any]:
    """Batterie für das Dashboard; enabled=False wenn nicht konfiguriert."""
    z = cfg.zendure
    if not cfg.zendure_active:
        return {"enabled": False}
    from db import get_zendure_ctl, get_zendure_energy
    energy = get_zendure_energy(datetime.now().strftime("%Y-%m-%d"))
    ctl = get_zendure_ctl()
    st = r.zendure
    return {
        "enabled": True,
        "name": z.name,
        "reachable": st is not None,
        "state": st.state if st is not None else "unreachable",
        "soc": st.soc if st is not None else None,
        "charge_w": st.charge_w if st is not None else None,
        "discharge_w": st.discharge_w if st is not None else None,
        "grid_in_w": st.grid_in_w if st is not None else None,
        "home_out_w": st.home_out_w if st is not None else None,
        "temp_c": st.temp_c if st is not None else None,
        "min_soc": z.min_soc,
        "target_soc": z.target_soc,
        "capacity_wh": z.capacity_wh,
        "control_enabled": z.control_enabled,
        "decision": ctl["decision"] if z.control_enabled else None,
        "ctl_mode": ctl["mode"] if z.control_enabled else None,
        "limit_w": ctl["limit_w"] if z.control_enabled else None,
        **energy,
    }


def _device_alerts(cfg: Config) -> list[dict[str, Any]]:
    """Geräte, die seit ≥ 10 min nicht antworten (für das Dashboard-Banner)."""
    from db import get_device_alerts
    names = {
        "ph1": f"Phase 1 ({cfg.shelly.ph1_url})", "ph2": f"Phase 2 ({cfg.shelly.ph2_url})",
        "ph3": f"Phase 3 ({cfg.shelly.ph3_url})",
        "storage_temp": f"Speichertemperatur-Shelly ({cfg.shelly.storage_url})",
        "heater_meter": f"Heizstab-3EM ({cfg.shelly.heater_meter_url})",
        "main_meter": f"Hauptzähler ({cfg.shelly.main_meter_url})",
    }
    out = []
    off = {f"ph{i + 1}" for i, x in enumerate(cfg.heater.disabled_phases) if x}
    for a in get_device_alerts(10.0):
        if a["id"] in off:       # stillgelegte Phasen melden nichts
            continue
        label = names.get(a["id"], a["id"]).replace("http://", "")
        out.append({**a, "label": label})
    return out


def _house_peak_today() -> dict[str, Any]:
    from db import get_house_peak
    return get_house_peak(datetime.now().strftime("%Y-%m-%d"))


def _serialize_status(result: ControllerResult, cfg: Config) -> dict[str, Any]:
    r = result.readings
    hp = _house_peak_today()
    return {
        "timestamp": result.timestamp.isoformat(timespec="seconds"),
        "runtime": {
            "enabled": cfg.runtime.enabled,
            "dry_run": cfg.runtime.dry_run,
            "simulation": cfg.simulation.enabled,
        },
        "heater": {
            "storage_temp": r.storage_temp_c,
            "storage_max_temp": cfg.heater.storage_max_temp,
            "storage_temp_hysteresis": cfg.heater.storage_temp_hysteresis,
            "resume_temp": cfg.heater.heat_resume_temp,
            "temperature_status": result.temp_status.value if result.temp_status else None,
            "pv_power": r.pv_power_w,  # Summe Haupt-WR + Pergola
            "pv1_power": r.pv1_power_w,
            "pv2_power": r.pv2_power_w,
            "pv2_today_kwh": r.pv2_today_kwh,
            "pv2_name": cfg.solax2.name if cfg.solax2 is not None else None,
            "pv2_enabled": cfg.solax2_active,
            "main_meter_power": r.main_meter_power_w,
            "heater_meter_power": r.heater_meter_power_w,
            "surplus_without_heater": r.surplus_without_heater_w,
            "true_surplus": r.true_surplus_w,
            "wallbox_power": r.wallbox_power_w,
            "phases": {
                "ph1": r.ph1_on,
                "ph2": r.ph2_on,
                "ph3": r.ph3_on,
            },
            "phases_disabled": {
                f"ph{i + 1}": bool(x) for i, x in enumerate(cfg.heater.disabled_phases)
            },
            "decisions": [_decision_to_dict(d) for d in result.decisions],
            "errors": list(r.errors),
        },
        "wallbox": {
            "enabled": cfg.wallbox.enabled,
            "url": cfg.wallbox.url,
            "pause_below_storage_temp": cfg.wallbox.pause_below_storage_temp,
            "release_above_storage_temp": cfg.wallbox.release_above_storage_temp,
            "only_control_when_pv_surplus_active": cfg.wallbox.only_control_when_pv_surplus_active,
            "status": _wallbox_status_to_dict(result.wallbox_status),
            "decision": _wallbox_decision_to_dict(result.wallbox_decision),
        },
        "summer_mode": {
            "enabled": cfg.summer_mode.enabled,
            "min_temp": cfg.summer_mode.min_temp,
            "target_temp": cfg.summer_mode.target_temp,
            "heating": result.summer_mode_heating,
            "reason": result.summer_mode_reason,
        },
        "summary": _summary(result, cfg),
        "meter_today": compute_meter_today(r),
        "zendure": _zendure_status_block(cfg, r),
        "device_alerts": _device_alerts(cfg),
        "house_peak_watts": hp["peak_w"],
        "house_peak_time": hp["peak_time"],
    }


# ---- Temperature history ---------------------------------------------------


def _append_history(
    temp: float | None, wb_w: float | None, heater_w: float | None = None
) -> None:
    from db import append_temp_history
    append_temp_history(temp, wb_w, heater_w)


# ---- Status cache (Hintergrund-Poller) -------------------------------------
#
# Ein READ_ONLY-Controllerlauf braucht 8 Geräte-Roundtrips (Solax, Shellys,
# go-e) und mehrere Sekunden, sobald ein Gerät nicht antwortet. Damit
# /api/status und /api/cascade/status sofort antworten, läuft der Lesezyklus
# in einem Hintergrund-Thread und die Endpunkte liefern den letzten Snapshot.

_POLL_ACTIVE_S = 10.0          # solange ein Client den Status abfragt
_POLL_IDLE_S = 60.0            # ohne Client-Anfragen
_CLIENT_ACTIVE_WINDOW_S = 60.0
_STATUS_FIRST_WAIT_S = 30.0

_status_lock = threading.Lock()
_status_ready = threading.Event()
_poll_wakeup = threading.Event()
_status_result: Optional[ControllerResult] = None
_status_cfg: Optional[Config] = None
_status_error: Optional[str] = None
_status_ts: float = 0.0
_last_status_request: float = 0.0


def _set_status_cache(result: ControllerResult, cfg: Config) -> None:
    global _status_result, _status_cfg, _status_error, _status_ts
    with _status_lock:
        _status_result, _status_cfg, _status_error = result, cfg, None
        _status_ts = time.monotonic()
    _status_ready.set()


def _poll_status_once() -> None:
    global _status_error
    cfg = _load_cfg()
    try:
        result = run_controller(cfg, mode=ControllerMode.READ_ONLY)
    except Exception as e:
        log.exception("read-only controller run failed")
        with _status_lock:
            _status_error = f"controller error: {e}"
        _status_ready.set()
        return
    ws = result.wallbox_status
    _append_history(
        result.readings.storage_temp_c,
        (ws.power_w or 0.0) if ws is not None else None,
        result.readings.heater_meter_power_w,
    )
    _set_status_cache(result, cfg)


def _status_poll_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            _poll_status_once()
        except Exception:
            log.exception("status poller failed")
        client_active = (time.monotonic() - _last_status_request) < _CLIENT_ACTIVE_WINDOW_S
        interval = _POLL_ACTIVE_S if client_active else _POLL_IDLE_S
        _poll_wakeup.wait(max(1.0, interval - (time.monotonic() - started)))
        _poll_wakeup.clear()


def _cached_status() -> tuple[ControllerResult, Config]:
    global _last_status_request
    _last_status_request = time.monotonic()
    if not _status_ready.is_set():
        _poll_wakeup.set()
        if not _status_ready.wait(_STATUS_FIRST_WAIT_S):
            raise HTTPException(status_code=503, detail="status snapshot not ready yet")
    with _status_lock:
        result, cfg, err, ts = _status_result, _status_cfg, _status_error, _status_ts
    if result is None or cfg is None:
        raise HTTPException(status_code=500, detail=err or "status unavailable")
    if time.monotonic() - ts > _POLL_ACTIVE_S:
        _poll_wakeup.set()
    return result, cfg


# ---- Routes: static --------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# ---- Routes: API -----------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "pv-controller-web"}


@app.get("/api/status")
def api_status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    result, cfg = _cached_status()
    return _serialize_status(result, cfg)


@app.post("/api/run-once")
def api_run_once(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()
    try:
        result = run_controller(cfg, mode=ControllerMode.NORMAL)
    except Exception as e:
        log.exception("normal controller run failed")
        raise HTTPException(status_code=500, detail=f"controller error: {e}")
    ws = result.wallbox_status
    _append_history(
        result.readings.storage_temp_c,
        (ws.power_w or 0.0) if ws is not None else None,
        result.readings.heater_meter_power_w,
    )
    _set_status_cache(result, cfg)
    return _serialize_status(result, cfg)


class ZendureTestRequest(BaseModel):
    ip: str
    sn: str = ""


@app.post("/api/zendure/test")
def api_zendure_test(req: ZendureTestRequest, response: Response) -> dict[str, Any]:
    """Verbindungstest mit den (noch ungespeicherten) Formularwerten."""
    response.headers["Cache-Control"] = "no-store"
    if not req.ip.strip():
        raise HTTPException(400, "ip must not be empty")
    from clients.zendure_client import ZendureClient
    res = ZendureClient(req.ip, req.sn, timeout=5.0).test_connection()
    if not res.get("ok"):
        raise HTTPException(503, res.get("error", "Zendure nicht erreichbar"))
    return res


@app.get("/api/zendure/history")
def api_zendure_history(response: Response, date: Optional[str] = Query(default=None)) -> dict[str, Any]:
    """SoC- und Leistungsverlauf eines Tages in Minutenwerten: [Minute, SoC, Laden W, Entladen W]."""
    response.headers["Cache-Control"] = "no-store"
    import re
    day = date or datetime.now().strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(400, "Format muss YYYY-MM-DD sein")
    from db import get_zendure_day, get_zendure_energy
    return {"date": day, "points": get_zendure_day(day), **get_zendure_energy(day)}


@app.get("/api/history/zendure/month/{year_month}")
def api_zendure_month(year_month: str, response: Response) -> dict[str, Any]:
    """Batterie: geladen/entladen (kWh) pro Tag + Zyklen-Schätzung (Σ geladen / Kapazität)."""
    response.headers["Cache-Control"] = "no-store"
    import re, sqlite3
    if not re.match(r"^\d{4}-\d{2}$", year_month):
        raise HTTPException(400, "Format muss YYYY-MM sein")
    from db import DB_PATH
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT date, zendure_charged_kwh, zendure_discharged_kwh FROM pv_daily_log
               WHERE date LIKE ? AND (zendure_charged_kwh IS NOT NULL OR zendure_discharged_kwh IS NOT NULL)
               ORDER BY date""",
            (year_month + "-%",),
        ).fetchall()
    cfg = _load_cfg()
    cap_kwh = (cfg.zendure.capacity_wh / 1000.0) if cfg.zendure is not None else None
    days = [{"date": r[0], "charged_kwh": r[1] or 0.0, "discharged_kwh": r[2] or 0.0} for r in rows]
    charged = sum(d["charged_kwh"] for d in days)
    return {
        "month": year_month, "days": days,
        "charged_kwh": round(charged, 2),
        "discharged_kwh": round(sum(d["discharged_kwh"] for d in days), 2),
        "cycles": round(charged / cap_kwh, 2) if cap_kwh else None,
        "capacity_kwh": cap_kwh,
    }


@app.get("/api/speicher/history")
def api_speicher_history(
    response: Response,
    range: str = Query(default="24h", pattern="^(1h|24h|7d)$"),
) -> dict[str, Any]:
    """Wasserspeicher-Verlauf (Temperatur, Heizstab-Leistung, Ereignisse, Rate, ETA).

    24h: 5-Minuten-Buckets des Tages + Stundenfenster; 1h: nur Stundenfenster
    (1-Minuten-Werte); 7d: Tageswerte (Min/Max, Heizstab-kWh).
    """
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()
    import storage_history
    return storage_history.build(
        range, cfg.heater.storage_max_temp, cfg.heater.heat_resume_temp
    )


@app.get("/api/temp-history")
def api_temp_history(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now().strftime("%Y-%m-%d")
    fine: list[dict] = []
    if TEMP_HISTORY_PATH.exists():
        try:
            with open(TEMP_HISTORY_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            fine = [e for e in raw if e["t"].startswith(today)]
        except Exception:
            pass
    # Stundendaten aus pv_hourly_log als Lückenfüller
    try:
        from db import get_hourly_data
        hourly = get_hourly_data(today)
        fine_hours = {e["t"][11:13] for e in fine}  # "HH" der bereits vorhandenen Minuten
        for h in hourly:
            if h.get("storage_temp_c") is None:
                continue
            ts = h["ts"]  # "2026-05-12 07:00"
            hour = ts[11:13]
            if hour in fine_hours:
                continue  # Minutendaten vorhanden → Stundenpunkt überspringen
            fine.append({
                "t": ts[:10] + "T" + ts[11:16],
                "temp": round(h["storage_temp_c"], 1),
                "wb_w": round(h.get("wallbox_w") or 0.0, 0),
            })
    except Exception:
        pass
    fine.sort(key=lambda e: e["t"])
    return {"entries": fine}


@app.get("/api/config")
def api_config_get() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- Config update --------------------------------------------------------


_ALLOWED_RUNTIME_KEYS = {"enabled", "dry_run"}
_ALLOWED_HEATER_KEYS = {
    "storage_max_temp",
    "storage_temp_hysteresis",
    "min_pv_power_ph1",
    "min_pv_power_ph2",
    "min_pv_power_ph3",
    "grid_assist_w",
    "phase_min_on_s",
    "phase_min_off_s",
    "phase_down_hyst",
    "budget_smooth_min",
    "budget_smooth_down_min",
    "phase1_disabled",
    "phase2_disabled",
    "phase3_disabled",
    "comfort_min_temp",
}
_BOOL_HEATER_KEYS = {"phase1_disabled", "phase2_disabled", "phase3_disabled"}
_ALLOWED_WALLBOX_KEYS = {
    "enabled",
    "only_control_when_pv_surplus_active",
    "pause_below_storage_temp",
    "release_above_storage_temp",
    "fail_safe",
    "url",
    "eco_phases",
    "eco_amps",
    "basic_phases",
    "basic_amps",
    "auto_reset_on_unplug",
}
_ALLOWED_SOLAX_KEYS = {"url", "pwd"}
_ALLOWED_SOLAX2_KEYS = {"enabled", "url", "pwd", "name"}
_ALLOWED_SHELLY_KEYS = {
    "ph1_url", "ph2_url", "ph3_url",
    "ph1_type", "ph2_type", "ph3_type",
    "ph1_channel", "ph2_channel", "ph3_channel",
    "storage_url", "main_meter_url", "heater_meter_url",
}
_SHELLY_TYPE_KEYS = {"ph1_type", "ph2_type", "ph3_type"}
_SHELLY_CHANNEL_KEYS = {"ph1_channel", "ph2_channel", "ph3_channel"}
_ALLOWED_AUTH_KEYS = {"enabled", "user", "password"}
_ALLOWED_LOCATION_KEYS = {"latitude", "longitude", "name"}
_ALLOWED_SUMMER_MODE_KEYS = {"enabled", "min_temp", "target_temp"}


class ConfigUpdate(BaseModel):
    runtime: dict[str, Any] | None = None
    heater: dict[str, Any] | None = None
    wallbox: dict[str, Any] | None = None
    solax: dict[str, Any] | None = None
    solax2: dict[str, Any] | None = None
    shelly: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    summer_mode: dict[str, Any] | None = None
    house: dict[str, Any] | None = None
    zendure: dict[str, Any] | None = None


def _validated_update(current: dict[str, Any], patch: ConfigUpdate) -> dict[str, Any]:
    new_cfg = copy.deepcopy(current)

    if patch.runtime:
        bad = set(patch.runtime) - _ALLOWED_RUNTIME_KEYS
        if bad:
            raise HTTPException(400, f"runtime keys not allowed: {sorted(bad)}")
        for k, v in patch.runtime.items():
            if not isinstance(v, bool):
                raise HTTPException(400, f"runtime.{k} must be boolean")
            new_cfg["runtime"][k] = v

    if patch.heater:
        bad = set(patch.heater) - _ALLOWED_HEATER_KEYS
        if bad:
            raise HTTPException(400, f"heater keys not allowed: {sorted(bad)}")
        for k, v in patch.heater.items():
            if k == "comfort_min_temp" and v is None:
                pass  # leer = Default (Sommermodus-Zieltemperatur)
            elif k == "comfort_min_temp" and (
                not isinstance(v, (int, float)) or isinstance(v, bool) or not 20 <= v <= 90
            ):
                raise HTTPException(400, "heater.comfort_min_temp must be a number between 20 and 90 (or null)")
            elif k in _BOOL_HEATER_KEYS:
                if not isinstance(v, bool):
                    raise HTTPException(400, f"heater.{k} must be boolean")
            elif not isinstance(v, (int, float)) or isinstance(v, bool):
                raise HTTPException(400, f"heater.{k} must be a number")
            new_cfg["heater"][k] = v

    if patch.wallbox:
        bad = set(patch.wallbox) - _ALLOWED_WALLBOX_KEYS
        if bad:
            raise HTTPException(400, f"wallbox keys not allowed: {sorted(bad)}")
        for k, v in patch.wallbox.items():
            if k in {"enabled", "only_control_when_pv_surplus_active", "auto_reset_on_unplug"}:
                if not isinstance(v, bool):
                    raise HTTPException(400, f"wallbox.{k} must be boolean")
            elif k in {"pause_below_storage_temp", "release_above_storage_temp"}:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"wallbox.{k} must be a number")
            elif k in {"eco_phases", "basic_phases"}:
                if not isinstance(v, int) or isinstance(v, bool) or v not in (1, 2):
                    raise HTTPException(400, f"wallbox.{k} must be 1 or 2")
            elif k in {"eco_amps", "basic_amps"}:
                if not isinstance(v, int) or isinstance(v, bool) or not 6 <= v <= 32:
                    raise HTTPException(400, f"wallbox.{k} must be an integer between 6 and 32")
            elif k == "fail_safe":
                if v != "no_change":
                    raise HTTPException(
                        400, "wallbox.fail_safe currently only supports 'no_change'"
                    )
            elif k == "url":
                if not isinstance(v, str) or not (
                    v.startswith("http://") or v.startswith("https://")
                ):
                    raise HTTPException(
                        400, "wallbox.url must start with http:// or https://"
                    )
            new_cfg["wallbox"][k] = v

    if patch.solax:
        bad = set(patch.solax) - _ALLOWED_SOLAX_KEYS
        if bad:
            raise HTTPException(400, f"solax keys not allowed: {sorted(bad)}")
        for k, v in patch.solax.items():
            if not isinstance(v, str) or not v.strip():
                raise HTTPException(400, f"solax.{k} must be a non-empty string")
            if k == "url" and not (v.startswith("http://") or v.startswith("https://")):
                raise HTTPException(400, "solax.url must start with http:// or https://")
            new_cfg["solax"][k] = v

    if patch.solax2:
        bad = set(patch.solax2) - _ALLOWED_SOLAX2_KEYS
        if bad:
            raise HTTPException(400, f"solax2 keys not allowed: {sorted(bad)}")
        block = new_cfg.setdefault("solax2", {"enabled": False, "url": "", "pwd": "", "name": "Pergola"})
        for k, v in patch.solax2.items():
            if k == "enabled":
                if not isinstance(v, bool):
                    raise HTTPException(400, "solax2.enabled must be boolean")
            else:
                if not isinstance(v, str):
                    raise HTTPException(400, f"solax2.{k} must be a string")
                if k == "url" and v and not (v.startswith("http://") or v.startswith("https://")):
                    raise HTTPException(400, "solax2.url must start with http:// or https://")
                if k == "name" and not v.strip():
                    raise HTTPException(400, "solax2.name must not be empty")
            block[k] = v
        if block.get("enabled") and not (block.get("url") and block.get("pwd")):
            raise HTTPException(400, "solax2: url und pwd erforderlich wenn aktiviert")

    if patch.shelly:
        bad = set(patch.shelly) - _ALLOWED_SHELLY_KEYS
        if bad:
            raise HTTPException(400, f"shelly keys not allowed: {sorted(bad)}")
        for k, v in patch.shelly.items():
            if k in _SHELLY_TYPE_KEYS:
                if v not in ("shelly_gen1", "shelly_gen2"):
                    raise HTTPException(
                        400, f"shelly.{k} must be 'shelly_gen1' or 'shelly_gen2'"
                    )
            elif k in _SHELLY_CHANNEL_KEYS:
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise HTTPException(400, f"shelly.{k} must be a non-negative integer")
            else:
                if not isinstance(v, str) or not (v.startswith("http://") or v.startswith("https://")):
                    raise HTTPException(400, f"shelly.{k} must start with http:// or https://")
            new_cfg["shelly"][k] = v

    if patch.auth:
        bad = set(patch.auth) - _ALLOWED_AUTH_KEYS
        if bad:
            raise HTTPException(400, f"auth keys not allowed: {sorted(bad)}")
        if "enabled" in patch.auth:
            if not isinstance(patch.auth["enabled"], bool):
                raise HTTPException(400, "auth.enabled must be boolean")
            new_cfg["auth_enabled"] = patch.auth["enabled"]
        if "user" in patch.auth:
            if not isinstance(patch.auth["user"], str) or not patch.auth["user"].strip():
                raise HTTPException(400, "auth.user must be a non-empty string")
            new_cfg["auth_user"] = patch.auth["user"]
        if "password" in patch.auth:
            if not isinstance(patch.auth["password"], str):
                raise HTTPException(400, "auth.password must be a string")
            new_cfg["auth_password"] = patch.auth["password"]

    if patch.location:
        bad = set(patch.location) - _ALLOWED_LOCATION_KEYS
        if bad:
            raise HTTPException(400, f"location keys not allowed: {sorted(bad)}")
        if "latitude" in patch.location:
            v = patch.location["latitude"]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise HTTPException(400, "location.latitude must be a number")
            new_cfg["latitude"] = float(v)
        if "longitude" in patch.location:
            v = patch.location["longitude"]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise HTTPException(400, "location.longitude must be a number")
            new_cfg["longitude"] = float(v)
        if "name" in patch.location:
            v = patch.location["name"]
            if not isinstance(v, str):
                raise HTTPException(400, "location.name must be a string")
            new_cfg["location_name"] = v

    if patch.zendure:
        allowed = {"enabled", "ip", "sn", "name", "capacity_wh", "max_charge_w", "max_discharge_w",
                   "min_soc", "target_soc", "control_enabled",
                   "block_discharge_while_wallbox_charging"}
        bad = set(patch.zendure) - allowed
        if bad:
            raise HTTPException(400, f"zendure keys not allowed: {sorted(bad)}")
        block = new_cfg.setdefault("zendure", {})
        for k, v in patch.zendure.items():
            if k in {"enabled", "control_enabled", "block_discharge_while_wallbox_charging"}:
                if not isinstance(v, bool):
                    raise HTTPException(400, f"zendure.{k} must be boolean")
            elif k in {"ip", "sn", "name"}:
                if not isinstance(v, str) or (k == "name" and not v.strip()):
                    raise HTTPException(400, f"zendure.{k} must be a string")
            else:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"zendure.{k} must be a number")
                lim = {"capacity_wh": (100, 100000), "max_charge_w": (0, 2400), "max_discharge_w": (0, 2400),
                       "min_soc": (0, 50), "target_soc": (70, 100)}[k]
                if not lim[0] <= v <= lim[1]:
                    raise HTTPException(400, f"zendure.{k} must be between {lim[0]} and {lim[1]}")
            block[k] = v
        if block.get("enabled") and not block.get("ip", "").strip():
            raise HTTPException(400, "zendure: ip erforderlich wenn aktiviert")
        if block.get("control_enabled") and not block.get("enabled"):
            raise HTTPException(400, "zendure.control_enabled setzt zendure.enabled voraus")

    if patch.house:
        bad = set(patch.house) - {"max_load_kw"}
        if bad:
            raise HTTPException(400, f"house keys not allowed: {sorted(bad)}")
        v = patch.house.get("max_load_kw")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 1 <= v <= 100:
            raise HTTPException(400, "house.max_load_kw must be a number between 1 and 100")
        new_cfg["house_max_load_kw"] = float(v)

    if patch.summer_mode:
        bad = set(patch.summer_mode) - _ALLOWED_SUMMER_MODE_KEYS
        if bad:
            raise HTTPException(400, f"summer_mode keys not allowed: {sorted(bad)}")
        if "summer_mode" not in new_cfg:
            new_cfg["summer_mode"] = {}
        for k, v in patch.summer_mode.items():
            if k == "enabled":
                if not isinstance(v, bool):
                    raise HTTPException(400, "summer_mode.enabled must be boolean")
            elif k in {"min_temp", "target_temp"}:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"summer_mode.{k} must be a number")
            new_cfg["summer_mode"][k] = v

    # Final-Validierung über die echten dataclasses (wirft ValueError -> 400):
    try:
        load_config_from_dict(new_cfg)
    except ValueError as e:
        raise HTTPException(400, f"validation failed: {e}")

    return new_cfg


def load_config_from_dict(raw: dict[str, Any]) -> Config:
    """Wie load_config, aber aus dict statt Datei (für Validierung)."""
    from config import (
        HeaterConfig,
        RuntimeConfig,
        ShellyConfig,
        SimulationConfig,
        SolaxConfig,
        SummerModeConfig,
        WallboxConfig,
        parse_solax2,
        parse_zendure,
    )

    return Config(
        solax=SolaxConfig(**raw["solax"]),
        shelly=ShellyConfig(**raw["shelly"]),
        heater=HeaterConfig(**raw["heater"]),
        runtime=RuntimeConfig(
            enabled=raw["runtime"].get("enabled", True),
            dry_run=raw["runtime"]["dry_run"],
            request_timeout_seconds=raw["runtime"]["request_timeout_seconds"],
            log_file=raw["runtime"]["log_file"],
        ),
        simulation=SimulationConfig(**raw.get("simulation", {})),
        wallbox=WallboxConfig(**raw.get("wallbox", {})),
        summer_mode=SummerModeConfig(**{
            k: v for k, v in raw.get("summer_mode", {}).items()
            if k != "max_phases"
        }),
        solax2=parse_solax2(raw),
        house_max_load_kw=float(raw.get("house_max_load_kw", 11.0)),
        zendure=parse_zendure(raw),
    )


@app.put("/api/config")
def api_config_put(patch: ConfigUpdate) -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        current = json.load(f)

    new_cfg = _validated_update(current, patch)

    backup_name = f"config.backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    backup_path = CONFIG_PATH.with_name(backup_name)
    shutil.copy2(CONFIG_PATH, backup_path)

    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(CONFIG_PATH)

    log.info("config updated, backup written to %s", backup_path.name)
    _poll_wakeup.set()
    return {"ok": True, "backup": backup_path.name, "config": new_cfg}


@app.get("/api/heizstab/phase-logs")
def api_phase_logs(
    response: Response,
    since: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    since_dt: Optional[datetime] = None
    if since == "today":
        since_dt = datetime.combine(datetime.now().date(), datetime.min.time())
    return {"entries": get_phase_logs(since_dt)}


def _read_pergola(cfg: Config):
    """SolaxRealtimeData des Pergola-WR oder None (deaktiviert / nicht erreichbar)."""
    if not cfg.solax2_active:
        return None
    from clients.solax_client import SolaxClient
    try:
        return SolaxClient(
            cfg.solax2.url, cfg.solax2.pwd, cfg.runtime.request_timeout_seconds
        ).get_realtime_data()
    except Exception:
        log.warning("Pergola-WR: Lesefehler", exc_info=True)
        return None


@app.get("/api/solax")
def api_solax(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()
    from clients.solax_client import SolaxClient
    client = SolaxClient(cfg.solax.url, cfg.solax.pwd, cfg.runtime.request_timeout_seconds)
    data = client.get_realtime_data()
    if data is None:
        raise HTTPException(status_code=503, detail="Solax device not reachable")
    bk = _read_pergola(cfg)
    bk_w = bk.pv_total_w if bk is not None else 0.0
    bk_kwh = bk.yield_today_kwh if bk is not None else 0.0
    # Der Pergola-WR speist hinter dem Hauszähler ein: Gesamt-PV, Tagesertrag und
    # Hausverbrauch (Haupt-WR sieht den Pergola-Anteil nicht) = Summe.
    pv_total_w = data.pv_total_w + bk_w
    yield_today = round(data.yield_today_kwh + bk_kwh, 2)
    grid_out, grid_in = data.grid_out_today_kwh, data.grid_in_today_kwh
    # Tageswerte bevorzugt aus dem Hauptzähler (Shelly); Zählerstände liefert der
    # zuletzt gecachte Status-Lauf (kein zusätzlicher Geräte-Abruf).
    from models import Readings
    with _status_lock:
        cached = _status_result
    mr = Readings(
        solax_yield_today_kwh=yield_today,
        solax_grid_out_kwh=grid_out,
        solax_grid_in_kwh=grid_in,
        main_import_wh=cached.readings.main_import_wh if cached else None,
        main_export_wh=cached.readings.main_export_wh if cached else None,
    )
    meter = compute_meter_today(mr, pv_kwh=yield_today)
    if meter is not None and meter["source"] == "shelly":
        grid_out, grid_in = meter["einspeisung_kwh"], meter["bezug_kwh"]
    selfuse = round(max(0.0, yield_today - grid_out), 2)
    from db import update_pv_peak
    peak = update_pv_peak(pv_total_w)
    from db import get_house_peak
    house_peak = get_house_peak(datetime.now().strftime("%Y-%m-%d"))
    return {
        "pv_peak_w": peak["peak_w"],
        "pv_peak_time": peak["peak_time"],
        "pv1_power_w": data.pv1_power_w,
        "pv2_power_w": data.pv2_power_w,
        "pv_total_w": pv_total_w,
        "main_total_w": data.pv_total_w,
        "pv1_voltage_v": data.pv1_voltage_v,
        "pv2_voltage_v": data.pv2_voltage_v,
        "pv1_current_a": data.pv1_current_a,
        "pv2_current_a": data.pv2_current_a,
        "feed_in_w": data.feed_in_w,
        # Hausverbrauch inkl. Pergola; Batterie: Entladung dazu, Ladung ist kein Verbrauch
        "consumption_w": data.consumption_w + bk_w - (cached.readings.battery_net_w if cached else 0.0),
        "yield_today_kwh": yield_today,
        "main_yield_today_kwh": data.yield_today_kwh,
        "grid_out_today_kwh": grid_out,
        "grid_in_today_kwh": grid_in,
        "self_consumption_kwh": selfuse,
        "meter_today": meter,
        "house_peak_w": house_peak["peak_w"],
        "house_peak_time": house_peak["peak_time"],
        "house_max_load_kw": cfg.house_max_load_kw,
        "mode": data.mode,
        "str1_max_w": cfg.solax.str1_max_w,
        "str2_max_w": cfg.solax.str2_max_w,
        "pergola_max_w": cfg.solax2.max_w if cfg.solax2 is not None else None,
        "pergola_enabled": cfg.solax2_active,
        "pergola_name": cfg.solax2.name if cfg.solax2 is not None else None,
        "pergola_ok": bk is not None,
        "pergola_power_w": bk.pv_total_w if bk is not None else None,
        "pergola_yield_today_kwh": bk.yield_today_kwh if bk is not None else None,
        "pergola_voltage_v": bk.pv1_voltage_v if bk is not None else None,
        "pergola_current_a": bk.pv1_current_a if bk is not None else None,
    }


class Solax2TestRequest(BaseModel):
    url: str
    pwd: str


@app.post("/api/solax2/test")
def api_solax2_test(req: Solax2TestRequest, response: Response) -> dict[str, Any]:
    """Verbindungstest mit den (noch ungespeicherten) Formularwerten."""
    response.headers["Cache-Control"] = "no-store"
    if not (req.url.startswith("http://") or req.url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if not req.pwd.strip():
        raise HTTPException(400, "pwd must not be empty")
    from clients.solax_client import SolaxClient
    timeout = max(_load_cfg().runtime.request_timeout_seconds, 8)
    d = SolaxClient(req.url, req.pwd, timeout).get_realtime_data()
    if d is None:
        raise HTTPException(503, "Solax nicht erreichbar oder Passwort falsch")
    return {
        "ok": True,
        "power_w": d.pv_total_w,
        "yield_today_kwh": d.yield_today_kwh,
        "model": d.model,
    }


@app.get("/api/history/hourly")
def api_hourly_history(
    response: Response,
    for_date: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_hourly_data, get_weather
    date_str = for_date or datetime.now().strftime("%Y-%m-%d")
    w = get_weather(date_str) or {}
    # Sonnenzeiten (HH:MM) für die X-Achse der Tages-Charts; None -> Fallback im Frontend
    return {
        "date": date_str,
        "entries": get_hourly_data(date_str),
        "sunrise": w.get("sunrise"),
        "sunset": w.get("sunset"),
    }


@app.get("/api/history/house/day/{date_str}")
def api_house_day(date_str: str, response: Response) -> dict[str, Any]:
    """Hauptzähler-Leistung (Shelly) eines Tages in Minutenwerten + Spitzen.

    points: [[Minute des Tages, Mittel W, Max W, Min W], …]; + Bezug, − Einspeisung.
    """
    response.headers["Cache-Control"] = "no-store"
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(400, "Format muss YYYY-MM-DD sein")
    from db import get_main_meter_day
    pts = get_main_meter_day(date_str)
    peak_w = peak_time = feed_w = feed_time = None
    if pts:
        top = max(pts, key=lambda p: p[2])
        low = min(pts, key=lambda p: p[3])
        if top[2] > 0:
            peak_w, peak_time = top[2], f"{top[0] // 60:02d}:{top[0] % 60:02d}"
        if low[3] < -50:
            feed_w, feed_time = -low[3], f"{low[0] // 60:02d}:{low[0] % 60:02d}"
    avg = round(sum(p[1] for p in pts) / len(pts)) if pts else None
    return {
        "date": date_str, "points": pts, "avg_w": avg,
        "peak_w": peak_w, "peak_time": peak_time,
        "feed_w": feed_w, "feed_time": feed_time,
    }


@app.get("/api/history/house/month/{year_month}")
def api_house_month(year_month: str, response: Response) -> dict[str, Any]:
    """Lastspitze pro Tag eines Monats."""
    response.headers["Cache-Control"] = "no-store"
    import re
    if not re.match(r"^\d{4}-\d{2}$", year_month):
        raise HTTPException(400, "Format muss YYYY-MM sein")
    from db import get_house_peaks_month
    days = get_house_peaks_month(year_month)
    top = max(days, key=lambda d: d["peak_w"]) if days else None
    avg = round(sum(d["peak_w"] for d in days) / len(days)) if days else None
    return {
        "month": year_month,
        "days": days,
        "max": top,
        "avg_peak_w": avg,
        "max_load_kw": _load_cfg().house_max_load_kw,
    }


@app.get("/api/history/daily")
def api_daily_history(
    response: Response,
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_daily_history
    return {"entries": get_daily_history(days)}


@app.get("/api/history/peak")
def api_peak_history(
    response: Response,
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_daily_peak
    return {"entries": get_daily_peak(days)}


@app.get("/api/history/month/{year_month}")
def api_monthly_history(year_month: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    import re
    if not re.match(r"^\d{4}-\d{2}$", year_month):
        raise HTTPException(400, "Format muss YYYY-MM sein")
    from db import get_daily_for_month
    return {"month": year_month, "entries": get_daily_for_month(year_month)}


@app.get("/api/history/year/{year}")
def api_yearly_history(year: int, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if year < 2020 or year > 2100:
        raise HTTPException(400, "Ungültiges Jahr")
    from db import get_monthly_totals
    return {"year": year, "entries": get_monthly_totals(year)}


@app.get("/api/forecast")
def api_forecast(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
            _raw = json.load(_f)
        lat = float(_raw.get("latitude", 0.0))
        lon = float(_raw.get("longitude", 0.0))
    except Exception:
        lat, lon = 0.0, 0.0
    if lat != 0.0 or lon != 0.0:
        try:
            from weather import fetch_and_store
            fetch_and_store(lat, lon)
        except Exception:
            log.warning("weather fetch_and_store failed in /api/forecast")
    from weather import calculate_forecast
    result = calculate_forecast()
    # Prognose für morgen sofort in heutigen DB-Eintrag schreiben,
    # damit /api/history/forecast sie auch ohne 23:55-Cron findet.
    tm = result.get("tomorrow")
    if tm and tm.get("predicted_kwh") is not None:
        try:
            from db import upsert_daily_forecast
            today_str = datetime.now().strftime("%Y-%m-%d")
            upsert_daily_forecast(today_str, tm["predicted_kwh"], tm.get("ghi_kwh_m2"))
        except Exception:
            log.warning("upsert_daily_forecast failed", exc_info=True)
    return result


@app.get("/api/history/forecast")
def api_forecast_accuracy(
    response: Response,
    days: int = Query(default=30, ge=7, le=365),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_forecast_accuracy_data
    return {"entries": get_forecast_accuracy_data(days)}


@app.get("/api/strings/alerts")
def api_strings_alerts(
    response: Response,
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_string_alerts
    return {"entries": get_string_alerts(days)}


@app.get("/api/history/grid/today")
def api_grid_today(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now().strftime("%Y-%m-%d")
    from db import get_hourly_data
    entries = get_hourly_data(today)
    return {
        "date": today,
        "entries": [
            {
                "ts": e["ts"],
                "einspeisung_w": max(0, e.get("feed_in_w") or 0),
                "bezug_w": max(0, -(e.get("feed_in_w") or 0)),
            }
            for e in entries
        ],
    }


@app.get("/api/history/grid/day/{date_str}")
def api_grid_day(date_str: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=400, detail="Format muss YYYY-MM-DD sein")
    from db import get_hourly_data
    entries = get_hourly_data(date_str)
    return {
        "date": date_str,
        "entries": [
            {
                "ts": e["ts"],
                "einspeisung_w": max(0, e.get("feed_in_w") or 0),
                "bezug_w": max(0, -(e.get("feed_in_w") or 0)),
            }
            for e in entries
        ],
    }


@app.get("/api/history/grid/week")
def api_grid_week(
    response: Response,
    end_date: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    import re as _re
    if end_date is not None and not _re.match(r"^\d{4}-\d{2}-\d{2}$", end_date):
        raise HTTPException(status_code=400, detail="Format muss YYYY-MM-DD sein")
    from db import get_grid_week_data
    return get_grid_week_data(end_date)


@app.get("/api/history/grid/month/{year_month}")
def api_grid_month(year_month: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}$", year_month):
        raise HTTPException(status_code=400, detail="Format muss YYYY-MM sein")
    from db import get_daily_for_month
    entries = get_daily_for_month(year_month)
    return {
        "month": year_month,
        "entries": [
            {
                "date": e["date"],
                "einspeisung_kwh": round(e.get("feed_out_kwh") or 0, 2),
                "bezug_kwh": round(e.get("feed_in_kwh") or 0, 2),
            }
            for e in entries
        ],
    }


@app.get("/api/history/grid/year/{year}")
def api_grid_year(year: int, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if year < 2020 or year > 2100:
        raise HTTPException(status_code=400, detail="Ungültiges Jahr")
    from db import get_monthly_totals
    entries = get_monthly_totals(year)
    return {
        "year": year,
        "entries": [
            {
                "month": e["month"],
                "einspeisung_kwh": round(e.get("feed_out_kwh") or 0, 2),
                "bezug_kwh": round(e.get("feed_in_kwh") or 0, 2),
            }
            for e in entries
        ],
    }


@app.get("/api/logs")
def api_logs(lines: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    cfg = _load_cfg()
    log_file = Path(cfg.runtime.log_file)
    if not log_file.exists():
        return {
            "lines": [],
            "note": f"log file does not exist: {log_file}",
            "log_file": str(log_file),
        }
    try:
        with open(log_file, "rb") as f:
            try:
                f.seek(-256 * 1024, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError as e:
        raise HTTPException(500, f"could not read log file: {e}")

    return {
        "lines": tail[-lines:],
        "log_file": str(log_file),
        "total_returned": min(lines, len(tail)),
    }


# ── Kaskade ──────────────────────────────────────────────────────────────────


class _CascadeDeviceCreate(BaseModel):
    id: str
    name: str
    type: str
    power_watts: int
    priority: int
    ip_address: Optional[str] = None
    shelly_channel: int = 0
    min_on_minutes: int = 5
    min_off_minutes: int = 3
    hysteresis_watts: int = 100


class _CascadeDeviceUpdate(BaseModel):
    name: Optional[str] = None
    power_watts: Optional[int] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    ip_address: Optional[str] = None
    shelly_channel: Optional[int] = None
    min_on_minutes: Optional[int] = None
    min_off_minutes: Optional[int] = None
    hysteresis_watts: Optional[int] = None


class _CascadeReorder(BaseModel):
    deviceIds: list[str]
    profile: Optional[str] = None   # "summer" | "winter": Reihenfolge dieses Profils (sonst Legacy-Priorität)


class _CascadeOverride(BaseModel):
    action: str   # 'on' | 'off'
    minutes: int  # wie lange die Übersteuerung gilt


class _ShellyTestRequest(BaseModel):
    ip: str
    type: str
    channel: int = 0


@app.post("/api/cascade/devices/test")
def cascade_device_test(body: _ShellyTestRequest) -> dict[str, Any]:
    if body.type not in ("shelly_gen1", "shelly_gen2"):
        raise HTTPException(400, f"type muss 'shelly_gen1' oder 'shelly_gen2' sein, nicht '{body.type}'")
    from clients.shelly_cascade_client import ShellyCascadeClient
    client = ShellyCascadeClient()
    return client.test_connection(body.ip, body.type, body.channel)


def _visible_cascade_devices() -> list[dict]:
    """Kaskaden-Geräte; die Batterie nur bei aktivem Gerät mit Steuerung."""
    cfg = _load_cfg()
    show_zd = cfg.zendure_active
    if show_zd:
        from db import ensure_zendure_cascade_device
        ensure_zendure_cascade_device(cfg.zendure.name, int(cfg.zendure.max_charge_w))
    return [d for d in get_cascade_devices() if d["type"] != "zendure" or show_zd]


@app.get("/api/cascade/devices")
def cascade_devices_get(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if _load_cfg().zendure_active and _load_cfg().zendure.control_enabled:
        from db import ensure_zendure_cascade_device
        c = _load_cfg().zendure
        ensure_zendure_cascade_device(c.name, int(c.max_charge_w))
    return {"devices": _visible_cascade_devices()}


@app.post("/api/cascade/devices", status_code=201)
def cascade_devices_post(body: _CascadeDeviceCreate) -> dict[str, Any]:
    if body.type not in VALID_CASCADE_TYPES:
        raise HTTPException(400, f"Ungültiger Gerätetyp: {body.type}. "
                            f"Erlaubt: {sorted(VALID_CASCADE_TYPES)}")
    if body.power_watts <= 0:
        raise HTTPException(400, "power_watts muss > 0 sein")
    if body.type in ("shelly_gen1", "shelly_gen2") and not body.ip_address:
        raise HTTPException(400, f"ip_address ist für Typ {body.type} erforderlich")
    if get_cascade_device(body.id) is not None:
        raise HTTPException(409, f"Gerät mit id='{body.id}' existiert bereits")
    existing = get_cascade_devices()
    priorities = {d["priority"] for d in existing}
    if body.priority in priorities:
        raise HTTPException(
            400,
            f"priority={body.priority} wird bereits verwendet. "
            "Verwende PATCH /api/cascade/reorder um Prioritäten neu zu setzen.",
        )
    device = create_cascade_device(
        device_id=body.id,
        name=body.name,
        device_type=body.type,
        power_watts=body.power_watts,
        priority=body.priority,
        ip_address=body.ip_address,
        shelly_channel=body.shelly_channel,
        min_on_minutes=body.min_on_minutes,
        min_off_minutes=body.min_off_minutes,
        hysteresis_watts=body.hysteresis_watts,
    )
    return {"device": device}


@app.put("/api/cascade/devices/{device_id}")
def cascade_devices_put(device_id: str, body: _CascadeDeviceUpdate) -> dict[str, Any]:
    if get_cascade_device(device_id) is None:
        raise HTTPException(404, f"Gerät '{device_id}' nicht gefunden")
    if body.power_watts is not None and body.power_watts <= 0:
        raise HTTPException(400, "power_watts muss > 0 sein")
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    device = update_cascade_device(device_id, **kwargs)
    return {"device": device}


@app.delete("/api/cascade/devices/{device_id}")
def cascade_devices_delete(device_id: str) -> dict[str, Any]:
    if device_id in PROTECTED_CASCADE_IDS:
        raise HTTPException(400, f"Gerät '{device_id}' darf nicht gelöscht werden")
    if get_cascade_device(device_id) is None:
        raise HTTPException(404, f"Gerät '{device_id}' nicht gefunden")
    delete_cascade_device(device_id)
    return {"ok": True, "deleted": device_id}


@app.patch("/api/cascade/reorder")
def cascade_reorder(body: _CascadeReorder) -> dict[str, Any]:
    if not body.deviceIds:
        raise HTTPException(400, "deviceIds darf nicht leer sein")
    if body.profile is not None:
        from cascade_plan import BATTERY, HEATER_MAX, HEATER_MIN, SEASONS
        from db import set_cascade_profile
        if body.profile not in SEASONS:
            raise HTTPException(400, "profile muss 'summer' oder 'winter' sein")
        valid = {d["id"] for d in get_cascade_devices()} - {"heizstab"} | {HEATER_MIN, HEATER_MAX, BATTERY}
        missing = [i for i in body.deviceIds if i not in valid]
        if missing:
            raise HTTPException(404, f"Unbekannte Einträge: {missing}")
        if len(body.deviceIds) != len(set(body.deviceIds)):
            raise HTTPException(400, "deviceIds enthält Duplikate")
        return {"profile": body.profile, "order": set_cascade_profile(body.profile, body.deviceIds)}
    existing_ids = {d["id"] for d in get_cascade_devices()}
    missing = [i for i in body.deviceIds if i not in existing_ids]
    if missing:
        raise HTTPException(404, f"Unbekannte Geräte-IDs: {missing}")
    if len(body.deviceIds) != len(set(body.deviceIds)):
        raise HTTPException(400, "deviceIds enthält Duplikate")
    reorder_cascade_devices(body.deviceIds)
    return {"devices": get_cascade_devices()}


@app.get("/api/cascade/status")
def cascade_status(response: Response, profile: Optional[str] = None) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"

    # Echte Messwerte aus dem Status-Snapshot des Hintergrund-Pollers
    heater_power_w: Optional[float] = None
    wb_charging: bool = False
    wb_power_w: Optional[float] = None
    surplus_w: Optional[int] = None
    zd_live = None
    storage_temp: Optional[float] = None
    try:
        result, _ = _cached_status()
        r = result.readings
        zd_live = r.zendure
        heater_power_w = r.heater_meter_power_w
        # main_meter ist negativ bei Einspeisung → Überschuss = max(0, -main_meter)
        # batterieneutral: Ladeleistung der Batterie zählt als Überschuss
        if r.main_meter_power_w is not None:
            surplus_w = max(0, round(-(r.main_meter_power_w - r.battery_net_w)))
        storage_temp = r.storage_temp_c
        ws = result.wallbox_status
        if ws is not None:
            wb_charging = (ws.car_state == 2)
            wb_power_w = ws.power_w
    except Exception:
        log.warning("cascade/status: Controller-Lesefehler", exc_info=True)

    devices = _visible_cascade_devices()
    recent_log = get_cascade_log(limit=50)
    # Letzten Logeintrag pro Gerät ermitteln
    last_action: dict[str, str] = {}
    last_reason: dict[str, str] = {}
    for entry in reversed(recent_log):
        did = entry["device_id"]
        if did not in last_action:
            last_action[did] = entry["action"]
            last_reason[did] = entry.get("reason", "")

    def _enrich(d: dict) -> dict:
        entry: dict[str, Any] = {
            "id": d["id"],
            "name": d["name"],
            "type": d["type"],
            "is_on": d["is_on"],
            "power_watts": d["power_watts"],
            "priority": d["priority"],
            "enabled": d["enabled"],
            "last_action": last_action.get(d["id"]),
            "last_reason": last_reason.get(d["id"], ""),
            "turned_on_at": d["turned_on_at"],
            "turned_off_at": d["turned_off_at"],
            "manual_override_action": d["manual_override_action"],
            "manual_override_until": d["manual_override_until"],
            "last_status_power_w": d.get("last_status_power_w"),
            "last_status_ok": d.get("last_status_ok", True),
            "consecutive_errors": d.get("consecutive_errors", 0),
        }
        # Echten Betriebszustand für heizstab/wallbox übernehmen
        if d["id"] == "heizstab" and heater_power_w is not None:
            entry["is_on"] = heater_power_w > 50
            entry["last_status_power_w"] = round(heater_power_w)
        elif d["id"] == "zendure" and zd_live is not None:
            entry["is_on"] = zd_live.state != "standby"
            entry["last_status_power_w"] = round(max(zd_live.charge_w, zd_live.discharge_w))
        elif d["id"] == "wallbox":
            entry["is_on"] = wb_charging
            if wb_power_w is not None:
                entry["last_status_power_w"] = round(wb_power_w)
        return entry

    cfg = _load_cfg()
    from cascade_plan import HEATER_MAX, HEATER_MIN, SEASONS
    from db import get_cascade_profile
    active = "summer" if cfg.summer_mode.enabled else "winter"
    season = profile if profile in SEASONS else active
    by_id = {d["id"]: _enrich(d) for d in devices}
    comfort, tmax = cfg.comfort_min_temp, cfg.heater.storage_max_temp
    entries: list[dict[str, Any]] = []
    for key in get_cascade_profile(season):
        if key in (HEATER_MIN, HEATER_MAX):
            base = by_id.get("heizstab")
            if base is None:
                continue
            e = dict(base)
            e["device_id"] = "heizstab"
            e["id"] = e["stage"] = key
            below = storage_temp is not None and storage_temp < comfort
            if key == HEATER_MIN:
                e["name"] = f"Wasserspeicher (bis {comfort:.0f} °C)"
                e["is_on"] = bool(base["is_on"]) and below
                e["skipped"] = storage_temp is not None and not below
            else:
                e["name"] = (f"Wasserspeicher (bis {tmax:.0f} °C)" if season == "summer"
                             else f"Wasserspeicher (bis Max {tmax:.0f} °C)")
                e["is_on"] = bool(base["is_on"]) and (season != "summer" or not below)
                e["skipped"] = storage_temp is not None and storage_temp >= tmax
        elif key in by_id:
            e = dict(by_id[key])
            e["device_id"] = key
        else:
            continue            # z. B. Batterie ohne aktives Gerät
        entries.append(e)
    for i, e in enumerate(entries, start=1):
        e["priority"] = i
    return {
        "surplus_watts": surplus_w, "devices": entries,
        "profile": season, "active_profile": active, "storage_temp": storage_temp,
        "comfort_min_temp": comfort, "storage_max_temp": tmax,
    }


@app.post("/api/cascade/devices/{device_id}/override")
def cascade_override(device_id: str, body: _CascadeOverride) -> dict[str, Any]:
    device = get_cascade_device(device_id)
    if device is None:
        raise HTTPException(404, f"Gerät '{device_id}' nicht gefunden")
    if body.action not in ("on", "off"):
        raise HTTPException(400, "action muss 'on' oder 'off' sein")
    if body.minutes <= 0:
        raise HTTPException(400, "minutes muss > 0 sein")
    until = datetime.now() + timedelta(minutes=body.minutes)
    set_cascade_override(device_id, body.action, until)
    # Shelly sofort schalten, nicht auf den nächsten Cron-Lauf warten
    if device["type"] in ("shelly_gen1", "shelly_gen2") and device.get("ip_address"):
        from clients.shelly_cascade_client import ShellyCascadeClient, ShellyCascadeError
        log = logging.getLogger(__name__)
        try:
            client = ShellyCascadeClient()
            if body.action == "on":
                client.turn_on(device["ip_address"], device["type"], device["shelly_channel"])
            else:
                client.turn_off(device["ip_address"], device["type"], device["shelly_channel"])
            log.info("Override: Shelly %s sofort %s", device["name"], "eingeschaltet" if body.action == "on" else "ausgeschaltet")
        except ShellyCascadeError as e:
            log.error("Override: Shelly %s Schaltfehler — %s", device["name"], e)
    return {
        "ok": True,
        "device_id": device_id,
        "override_action": body.action,
        "override_until": until.isoformat(timespec="seconds"),
    }


@app.delete("/api/cascade/devices/{device_id}/override")
def cascade_override_delete(device_id: str) -> dict[str, Any]:
    if get_cascade_device(device_id) is None:
        raise HTTPException(404, f"Gerät '{device_id}' nicht gefunden")
    clear_cascade_override(device_id)
    return {"ok": True, "device_id": device_id}


@app.get("/api/cascade/log")
def cascade_log_get(
    response: Response,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"entries": get_cascade_log(limit)}


class _CascadeSettingsUpdate(BaseModel):
    cascade_enabled: Optional[bool] = None
    polling_interval_s: Optional[int] = None
    min_surplus_watts: Optional[int] = None
    allow_grid_draw: Optional[bool] = None


@app.get("/api/cascade/settings")
def cascade_settings_get(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return get_cascade_settings()


@app.put("/api/cascade/settings")
def cascade_settings_put(body: _CascadeSettingsUpdate) -> dict[str, Any]:
    if body.polling_interval_s is not None and body.polling_interval_s not in (15, 30, 60):
        raise HTTPException(400, "polling_interval_s muss 15, 30 oder 60 sein")
    if body.min_surplus_watts is not None and body.min_surplus_watts < 0:
        raise HTTPException(400, "min_surplus_watts muss >= 0 sein")
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    return update_cascade_settings(**kwargs)


@app.post("/api/wallbox/set-mode")
def wallbox_set_mode(mode: str = Query(..., pattern="^(basic|eco)$")) -> dict[str, Any]:
    cfg = _load_cfg()
    url = cfg.wallbox.url if cfg.wallbox else None
    if not url:
        raise HTTPException(400, "Wallbox nicht konfiguriert")
    if mode == "basic":
        lmo, psm, amp = 3, cfg.wallbox.basic_phases, cfg.wallbox.basic_amps
    else:
        lmo, psm, amp = 4, cfg.wallbox.eco_phases, cfg.wallbox.eco_amps
    client = GoeClient(url)
    try:
        client.set_psm(psm)
        client.set_amp(amp)
        client.set_logic_mode(lmo)
    except GoeError as e:
        raise HTTPException(502, f"go-e Fehler: {e}")
    return {"ok": True, "lmo": lmo, "psm": psm, "amp": amp, "mode": mode}
