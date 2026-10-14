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
import os
import sqlite3
import json
import logging
import re
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

import heating as _heating
import heating_schedule as _hsched
import explain as _explain
import heating_chat as _hchat
import redact as _redact
import shutters as _shutters
import shutter_schedule as _ssched
import shutter_chat as _schat
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
# API-Keys (sk-…, auch gekürzt) in keiner Log-Zeile: Root-Handler und die eigenen Handler von uvicorn
_redact.install()
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _redact.install(*logging.getLogger(_name).handlers)

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
        text = f"{label} seit {a['since']} Uhr nicht erreichbar"
        if a["id"] == "heater_meter":
            text = f"Heizstab-Zähler ({cfg.shelly.heater_meter_url}) seit {a['since']} Uhr nicht erreichbar – Ersatzwert aus Phasen".replace("http://", "")
        out.append({**a, "label": label, "text": text})
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
            "heater_power_source": r.heater_power_source,   # "meter" | "phase_sum" (Ersatz aus Phasen)
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


# Geheime Felder (Passwörter, Tokens, API-Keys) gehen nie im Klartext ans Frontend.
# Maßgeblich ist der Feldname, egal in welchem Block: auth_password, solax.pwd,
# openhab.token, assistant.api_key, … Gesetzte Werte kommen als _SECRET_MASK zurück,
# leere bleiben leer (die UI sieht so, ob etwas gesetzt ist). Schickt die UI die Maske
# zurück, bleibt der gespeicherte Wert erhalten (_drop_masked).
_SECRET_MASK = "********"
_SECRET_KEY_RE = re.compile(r"token|key|password|passwd|pwd|secret", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _masked(obj: Any) -> Any:
    """Kopie mit maskierten Secrets (rekursiv durch Blöcke und Listen)."""
    if isinstance(obj, dict):
        return {k: (_SECRET_MASK if _is_secret_key(k) and isinstance(v, str) and v else _masked(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_masked(v) for v in obj]
    return obj


def _drop_masked(obj: Any) -> Any:
    """Patch ohne Felder, deren Wert die Maske ist (= unverändert lassen)."""
    if isinstance(obj, dict):
        return {k: _drop_masked(v) for k, v in obj.items() if v != _SECRET_MASK}
    if isinstance(obj, list):
        return [_drop_masked(v) for v in obj]
    return obj


@app.get("/api/config")
def api_config_get() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return _masked(json.load(f))


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
    "winter_comfort_min_temp",
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
    "storage_url", "main_meter_url", "heater_meter_url", "main_meter_type",
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
    patch = ConfigUpdate(**_drop_masked(patch.model_dump()))
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
            if k in ("comfort_min_temp", "winter_comfort_min_temp") and v is None:
                pass  # leer = Default (Sommer: Zieltemperatur Sommermodus) bzw. aus (Winter)
            elif k in ("comfort_min_temp", "winter_comfort_min_temp") and (
                not isinstance(v, (int, float)) or isinstance(v, bool) or not 20 <= v <= 90
            ):
                raise HTTPException(400, f"heater.{k} must be a number between 20 and 90 (or null)")
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
            elif k == "main_meter_type":
                from config import METER_TYPES
                if v not in METER_TYPES:
                    raise HTTPException(400, f"shelly.main_meter_type must be one of {', '.join(METER_TYPES)}")
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
    backup_name = _write_config(new_cfg)
    _poll_wakeup.set()
    return {"ok": True, "backup": backup_name, "config": _masked(new_cfg)}


def _write_config(new_cfg: dict[str, Any]) -> str:
    """Backup der aktuellen config.json, dann atomar ersetzen. Gibt den Backup-Namen zurück."""
    # Config und Backups enthalten Secrets (Tokens, API-Keys): nur für den Besitzer lesbar (0600),
    # unabhängig von umask und den Rechten der bisherigen Datei.
    backup_name = f"config.backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    backup_path = CONFIG_PATH.with_name(backup_name)
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as dst, open(CONFIG_PATH, "rb") as src:
        os.fchmod(dst.fileno(), 0o600)
        shutil.copyfileobj(src, dst)

    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)                # falls eine alte .tmp mit 644 liegen geblieben ist
        json.dump(new_cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(CONFIG_PATH)

    log.info("config updated, backup written to %s", backup_path.name)
    return backup_path.name


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
        return SolaxClient(cfg.solax2.url, cfg.solax2.pwd, cfg.solax2_timeout).get_realtime_data()
    except Exception:
        log.warning("Pergola-WR: Lesefehler", exc_info=True)
        return None


PV_NOW_CACHE_S = 60.0          # Web-Cache der Wechselrichter gilt für /api/pv/now so lange
PV_NOW_SNAPSHOT_S = 180.0      # Entscheidungs-Schnappschuss des Cron-Laufs gilt so lange


@app.get("/api/pv/now")
def api_pv_now(response: Response) -> dict[str, Any]:
    """Gesamt-PV für die Kopfzeile – nie eine eigene Abfrage am Wechselrichter.

    Quelle: Prozess-Cache der Wechselrichter-Clients (gefüllt vom Dashboard-Polling), sonst der letzte
    Entscheidungs-Schnappschuss des Cron-Laufs. Pergola fehlt (nachts erwartet offline) → nur Haupt-WR;
    Haupt-WR fehlt → ``pv_w`` null."""
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()
    from clients.solax_client import SolaxClient
    main = SolaxClient(cfg.solax.url, cfg.solax.pwd, cfg.runtime.request_timeout_seconds)
    md = main.cached_realtime_data(PV_NOW_CACHE_S)
    if md is not None:
        pg = None
        if cfg.solax2_active:
            pg = SolaxClient(cfg.solax2.url, cfg.solax2.pwd, cfg.solax2_timeout).cached_realtime_data(PV_NOW_CACHE_S)
        pg_w = pg.pv_total_w if pg is not None else None
        return {"pv_w": md.pv_total_w + (pg_w or 0.0), "main_w": md.pv_total_w, "pergola_w": pg_w,
                "source": "cache", "age_s": round(main.cache_age() or 0.0)}
    import decisions as _dec
    try:
        snap = _dec.latest()
    except Exception:                                   # noqa: BLE001 – Kopfzeile nie am Lesen scheitern lassen
        snap = None
    if snap:
        age = (datetime.now().astimezone() - datetime.fromisoformat(snap["ts"])).total_seconds()
        if age <= PV_NOW_SNAPSHOT_S:
            e = snap.get("energy") or {}
            return {"pv_w": e.get("pv_w"), "main_w": None if e.get("pv_w") is None else
                    e["pv_w"] - (e.get("pv_pergola_w") or 0), "pergola_w": e.get("pv_pergola_w"),
                    "source": "snapshot", "age_s": round(age)}
    return {"pv_w": None, "main_w": None, "pergola_w": None, "source": None, "age_s": None}


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


class MainMeterTestRequest(BaseModel):
    url: str
    type: str = "shelly_3em_gen1"


@app.post("/api/main-meter/test")
def api_main_meter_test(req: MainMeterTestRequest, response: Response) -> dict[str, Any]:
    """Verbindungstest Hauptzähler mit den (noch ungespeicherten) Formularwerten: Gerät, Leistung, Zähler.
    Nur lesend; meldet, wenn der gewählte Typ nicht zum Gerät passt."""
    response.headers["Cache-Control"] = "no-store"
    from config import METER_TYPES
    from clients.shelly_client import Shelly3EMClient
    if not (req.url.startswith("http://") or req.url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if req.type not in METER_TYPES:
        raise HTTPException(400, f"type must be one of {', '.join(METER_TYPES)}")
    c = Shelly3EMClient(req.url, 5, name="main_meter_test", type_=req.type)
    try:
        info = c.get_device_info()
    except Exception:                                    # noqa: BLE001
        raise HTTPException(503, "Gerät nicht erreichbar")
    want_gen = 2 if c.is_pro else 1
    if info["generation"] != want_gen:
        raise HTTPException(409, f"Typ passt nicht: Gerät {info['model']} ist Gen{info['generation']}, "
                                 f"gewählt ist {'Pro 3EM (Gen2)' if c.is_pro else '3EM (Gen1)'}")
    power = c.get_total_power_w()
    energy = c.get_energy_wh()
    if power is None:
        raise HTTPException(502, "Gerät erreichbar, aber Leistung nicht lesbar")
    return {"ok": True, "model": info["model"], "mac": info["mac"], "generation": info["generation"],
            "power_w": power, "import_kwh": None if energy is None else round(energy[0] / 1000, 2),
            "export_kwh": None if energy is None else round(energy[1] / 1000, 2)}


@app.post("/api/solax2/test")
def api_solax2_test(req: Solax2TestRequest, response: Response) -> dict[str, Any]:
    """Verbindungstest mit den (noch ungespeicherten) Formularwerten."""
    response.headers["Cache-Control"] = "no-store"
    if not (req.url.startswith("http://") or req.url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")
    if not req.pwd.strip():
        raise HTTPException(400, "pwd must not be empty")
    from clients.solax_client import SolaxClient
    cfg = _load_cfg()
    pwd = req.pwd
    if pwd == _SECRET_MASK:  # Formular zeigt das gespeicherte Passwort nur maskiert
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            pwd = (json.load(f).get("solax2") or {}).get("pwd") or ""
        if not pwd:
            raise HTTPException(400, "kein gespeichertes Passwort – bitte eintragen")
    timeout = max(cfg.runtime.request_timeout_seconds, 8)
    d = SolaxClient(req.url, pwd, timeout).get_realtime_data()
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
        min_stage = _load_cfg().comfort_min_for(body.profile) is not None
        valid = {d["id"] for d in get_cascade_devices()} - {"heizstab"} | {HEATER_MIN, HEATER_MAX, BATTERY}
        missing = [i for i in body.deviceIds if i not in valid]
        if missing:
            raise HTTPException(404, f"Unbekannte Einträge: {missing}")
        if len(body.deviceIds) != len(set(body.deviceIds)):
            raise HTTPException(400, "deviceIds enthält Duplikate")
        return {"profile": body.profile, "order": set_cascade_profile(body.profile, body.deviceIds, min_stage)}
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
    # Aktuelle Entscheidung samt Grund je Gerät (wird jede Minute überschrieben; cascade_log enthält
    # nur noch Ereignisse). Fallback auf das Ereignis-Log für Geräte ohne Entscheidung.
    from db import get_cascade_decisions
    decisions = get_cascade_decisions()
    last_action: dict[str, str] = {k: v["action"] for k, v in decisions.items()}
    last_reason: dict[str, str] = {k: v["reason"] for k, v in decisions.items()}
    for entry in reversed(get_cascade_log(limit=50)):
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
    comfort, tmax = cfg.comfort_min_for(season), cfg.heater.storage_max_temp
    entries: list[dict[str, Any]] = []
    for key in get_cascade_profile(season, comfort is not None):
        if key in (HEATER_MIN, HEATER_MAX):
            base = by_id.get("heizstab")
            if base is None:
                continue
            e = dict(base)
            e["device_id"] = "heizstab"
            e["id"] = e["stage"] = key
            below = storage_temp is not None and comfort is not None and storage_temp < comfort
            if key == HEATER_MIN:
                e["name"] = f"Wasserspeicher (bis {comfort:.0f} °C)" if comfort is not None else "Wasserspeicher (bis Min)"
                e["is_on"] = bool(base["is_on"]) and below
                e["skipped"] = storage_temp is not None and not below
            else:
                e["name"] = (f"Wasserspeicher (bis {tmax:.0f} °C)" if season == "summer"
                             else f"Wasserspeicher (bis Max {tmax:.0f} °C)")
                e["is_on"] = bool(base["is_on"]) and (comfort is None or not below)
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


# ── Heizung (openHAB-Thermostate) ────────────────────────────────────────────


_HEATING_HISTORY_BUCKET_MS = 10 * 60 * 1000


def _heating_ctx() -> tuple[dict[str, Any], "_heating.HeatingConfig", Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    try:
        hcfg = _heating.parse_heating(raw)
    except ValueError as e:
        raise HTTPException(500, f"heating config ungültig: {e}")
    return raw, hcfg, _heating.make_client(raw)


def _heating_dry_run(raw: dict[str, Any]) -> bool:
    return bool((raw.get("runtime") or {}).get("dry_run", False))


def _app_log(msg: str, level: int = logging.INFO, area: str = "heating") -> None:
    """Zeile ins Controller-Log (runtime.log_file), damit sie im Log-Tab erscheint."""
    msg = _redact.redact(msg)
    log.log(level, msg)
    try:
        path = Path(_load_cfg().runtime.log_file)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{logging.getLevelName(level)}] pv-controller.{area}: {msg}\n")
    except Exception as e:  # Log darf das Schalten nie verhindern
        log.warning("%s: Log-Datei nicht beschreibbar: %s", area, e)


@app.get("/api/heating/status")
def heating_status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    raw, hcfg, client = _heating_ctx()
    base = {
        "enabled": hcfg.enabled,
        "setpoint_min": hcfg.setpoint_min,
        "setpoint_max": hcfg.setpoint_max,
        "setpoint_step": _heating.SETPOINT_STEP,
        "dry_run": _heating_dry_run(raw),
        "openhab_configured": client is not None,
        "schedules_enabled": hcfg.schedules_enabled,
        "chat": _chat_status(raw, hcfg),
    }
    if not hcfg.enabled or client is None:
        return {**base, "ok": True, "error": None, "rooms": []}
    st = {**base, **_heating.read_status(hcfg, client)}
    try:
        nxt = _hsched.next_changes(_hsched.list_schedules(), datetime.now(_hsched.TZ), loc=_ssched.location(raw))
        alerts = _hsched.alerts()
    except Exception as e:  # Zeitplan-DB darf die Anzeige nicht blockieren
        log.warning("heating: Zeitpläne nicht lesbar: %s", e)
        nxt, alerts = {}, {}
    for r in st["rooms"]:
        r["next"] = nxt.get(r["id"])
        a = alerts.get(r["id"])
        if a and a["status"] == "mismatch" and r.get("setpoint") is not None \
                and abs(r["setpoint"] - a["setpoint"]) < 0.01:
            _hsched.clear_verify(r["id"])        # Gerät meldet inzwischen den gesendeten Wert
            a = None
        r["alert"] = a["message"] if a else None
    return st


class _HeatingSetpoint(BaseModel):
    room_id: str
    setpoint: float


def _heating_send(raw, hcfg, client, room, value: float, source: str) -> dict[str, Any]:
    if _heating_dry_run(raw):
        _app_log(f"DRY-RUN: {room.name} Soll → {value:g} °C ({source}, nicht gesendet)")
        return {"room_id": room.id, "ok": True, "dry_run": True, "setpoint": value}
    try:
        _heating.set_setpoint(hcfg, client, room.id, value)
    except _heating.OpenHabError as e:
        _app_log(f"FEHLER: {room.name} Soll → {value:g} °C ({source}): {e}", logging.WARNING)
        return {"room_id": room.id, "ok": False, "error": str(e)}
    _app_log(f"{room.name} Soll → {value:g} °C ({source})")
    try:
        _hsched.clear_verify(room.id)          # neuer Sollwert ersetzt eine alte Abweichungsmeldung
    except Exception as e:
        log.warning("heating: Rückprüfung nicht zurückgesetzt: %s", e)
    return {"room_id": room.id, "ok": True, "dry_run": False, "setpoint": value}


@app.post("/api/heating/setpoint")
def heating_setpoint(body: _HeatingSetpoint) -> dict[str, Any]:
    raw, hcfg, client = _heating_ctx()
    if not hcfg.enabled or client is None:
        raise HTTPException(409, "Heizung nicht aktiv oder openHAB nicht konfiguriert")
    room = hcfg.room(body.room_id)
    if room is None:
        raise HTTPException(404, f"Raum {body.room_id!r} unbekannt")
    if not room.controllable:
        raise HTTPException(400, f"{room.name} hat keinen Sollwert (nur Anzeige)")
    try:
        value = _heating.validate_setpoint(hcfg, body.setpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res = _heating_send(raw, hcfg, client, room, value, "manuell")
    if not res["ok"]:
        raise HTTPException(502, res["error"])
    return res


@app.get("/api/heating/history/{room_id}")
def heating_history(room_id: str, response: Response,
                    hours: int = Query(default=24, ge=1, le=168)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    raw, hcfg, client = _heating_ctx()
    room = hcfg.room(room_id)
    if room is None:
        raise HTTPException(404, f"Raum {room_id!r} unbekannt")
    if client is None:
        raise HTTPException(409, "openHAB nicht konfiguriert")
    start = datetime.now() - timedelta(hours=hours)
    svc = (raw.get("openhab") or {}).get("persistence") or None
    out: dict[str, Any] = {"room_id": room_id, "hours": hours}
    for key, item in (("temp", room.temp_item), ("setpoint", room.setpoint_item)):
        if not item:
            out[key] = []
            continue
        try:
            out[key] = _heating.downsample(client.get_history(item, start, service_id=svc),
                                           _HEATING_HISTORY_BUCKET_MS)
        except _heating.OpenHabError as e:
            out[key] = []
            out["error"] = str(e)
    return out


@app.get("/api/heating/items")
def heating_items() -> dict[str, Any]:
    """openHAB-Items für die Raum-Auswahl in den Settings (ohne Gruppen)."""
    _raw, _hcfg, client = _heating_ctx()
    if client is None:
        raise HTTPException(409, "openHAB nicht konfiguriert")
    try:
        items = client.list_items()
    except _heating.OpenHabError as e:
        raise HTTPException(502, str(e))
    items = [{"name": i["name"], "label": i.get("label") or "", "type": i.get("type", ""),
              "state": i.get("state")} for i in items if i.get("type") != "Group"]
    items.sort(key=lambda i: i["name"].lower())
    return {"items": items}


@app.get("/api/heating/config")
def heating_config_get() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    oh = raw.get("openhab") or {}
    a = raw.get("assistant") or {}
    return {
        "heating": raw.get("heating") or {"enabled": False, "setpoint_min": 5, "setpoint_max": 26, "rooms": []},
        "openhab": {"url": oh.get("url", ""), "token_set": bool(oh.get("token"))},
        "assistant": {"enabled": bool(a.get("enabled", False)),
                      "provider": a.get("provider") or _hchat.ANTHROPIC,
                      "model": a.get("model") or _hchat.DEFAULT_MODELS.get(a.get("provider") or _hchat.ANTHROPIC,
                                                                            _hchat.DEFAULT_MODEL),
                      "default_models": _hchat.DEFAULT_MODELS,
                      "max_requests_per_day": a.get("max_requests_per_day", _hchat.DEFAULT_LIMIT),
                      "keys_set": {p: bool(a.get(f)) for p, f in _hchat._KEY_FIELD.items()}},
    }


class _HeatingConfigUpdate(BaseModel):
    enabled: bool
    setpoint_min: float
    setpoint_max: float
    rooms: list[dict[str, Any]]
    schedules_enabled: Optional[bool] = None    # None = unverändert lassen
    assistant: Optional[dict[str, Any]] = None   # {enabled, provider, model, max_requests_per_day}; Keys nie


@app.put("/api/heating/config")
def heating_config_put(body: _HeatingConfigUpdate) -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        current = json.load(f)
    old_se = bool((current.get("heating") or {}).get("schedules_enabled", False))
    block = {
        "enabled": body.enabled,
        "schedules_enabled": old_se if body.schedules_enabled is None else body.schedules_enabled,
        "setpoint_min": body.setpoint_min,
        "setpoint_max": body.setpoint_max,
        "rooms": [
            {k: v for k, v in r.items() if k in ("id", "name", *_heating.ITEM_KEYS) and v not in (None, "")}
            for r in body.rooms
        ],
    }
    new_cfg = copy.deepcopy(current)
    new_cfg["heating"] = block
    old_chat = bool((current.get("assistant") or {}).get("enabled", False))
    if body.assistant is not None:
        a = dict(current.get("assistant") or {})          # Keys bleiben, kommen nie vom Frontend
        for k in ("enabled", "provider", "model", "max_requests_per_day"):
            if k in body.assistant:
                a[k] = body.assistant[k]
        new_cfg["assistant"] = a
    try:
        _heating.parse_heating(new_cfg)
        _hchat.parse_assistant(new_cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if block["enabled"] and not (new_cfg.get("openhab") or {}).get("url"):
        raise HTTPException(400, "openhab.url fehlt in config.json")
    backup = _write_config(new_cfg)
    if block["schedules_enabled"] != old_se:
        _app_log(f"Zeitpläne {'aktiviert' if block['schedules_enabled'] else 'deaktiviert'} (Settings)")
    new_chat = bool((new_cfg.get("assistant") or {}).get("enabled", False))
    if new_chat != old_chat:
        _app_log(f"Chat {'aktiviert' if new_chat else 'deaktiviert'} (Settings)")
    return {"ok": True, "backup": backup, "heating": block}


# ── Heizung: Zeitpläne ──────────────────────────────────────────────────────


class _HeatingScheduleCreate(BaseModel):
    room_id: str
    time: Optional[str] = None           # HH:MM bei kind = "time"
    setpoint: float
    days: Optional[list[str]] = None
    date: Optional[str] = None
    note: Optional[str] = None
    kind: str = "time"                   # time | sunrise | sunset
    offset_min: int = 0
    not_before: Optional[str] = None
    not_after: Optional[str] = None


def _with_today(items: list[dict[str, Any]], inst, now: datetime, loc) -> list[dict[str, Any]]:
    """Berechnete Uhrzeit für heute je Eintrag („Sonnenuntergang −15 min (18:22)“ in der Liste)."""
    for e in items:
        at = inst(e, now.date(), loc) if (e.get("kind") or "time") != "time" else None
        e["at_today"] = f"{at:%H:%M}" if at else None
    return items


def _heating_sched_fields(hcfg, body: "_HeatingScheduleCreate", raw: dict[str, Any]) -> dict[str, Any]:
    try:
        return _hsched.validate_entry(hcfg, body.room_id, body.setpoint, body.time, days=body.days,
                                      date=body.date or None, kind=body.kind, offset_min=body.offset_min,
                                      not_before=body.not_before, not_after=body.not_after,
                                      loc=_ssched.location(raw))
    except ValueError as e:
        raise HTTPException(400, str(e))


class _HeatingSchedulePatch(BaseModel):
    enabled: bool


def _sun_hm(day, loc) -> dict[str, Any]:
    """Sonnenauf-/-untergang (HH:MM) für einen Tag aus suntime.py; {} ohne Standort."""
    if not loc:
        return {}
    rise, sset = _ssched.sun_times(day, *loc)
    return {"sunrise": f"{rise:%H:%M}" if rise else None, "sunset": f"{sset:%H:%M}" if sset else None}


@app.get("/api/heating/schedules")
def heating_schedules(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    raw, hcfg, _client = _heating_ctx()
    now = datetime.now(_hsched.TZ)
    loc = _ssched.location(raw)
    items = _with_today(_hsched.list_schedules(), _hsched.instant, now, loc)
    sun = {}
    if loc:
        rise, sset = _ssched.sun_times(now.date(), *loc)
        sun = {"sunrise": f"{rise:%H:%M}" if rise else None, "sunset": f"{sset:%H:%M}" if sset else None}
    return {
        "schedules_enabled": hcfg.schedules_enabled,
        "heating_enabled": hcfg.enabled,
        "dry_run": _heating_dry_run(raw),
        "now": now.isoformat(timespec="minutes"),
        "items": items,
        "sun_today": sun,
        "sun_tomorrow": _sun_hm(now.date() + timedelta(days=1), loc),
        "next": _hsched.next_changes(items, now, loc=loc),
    }


@app.post("/api/heating/schedules")
def heating_schedule_create(body: _HeatingScheduleCreate) -> dict[str, Any]:
    raw, hcfg, _client = _heating_ctx()
    fields = _heating_sched_fields(hcfg, body, raw)
    entry = _hsched.create_schedule(fields, source="ui", note=(body.note or "").strip()[:200])
    room = hcfg.room(body.room_id)
    when = entry["date"] or ",".join(entry["days"])
    _app_log(f"Zeitplan angelegt: {room.name} {entry['when']} → {entry['setpoint']:g} °C ({when})")
    return entry


@app.put("/api/heating/schedules/{sid}")
def heating_schedule_update(sid: int, body: _HeatingScheduleCreate) -> dict[str, Any]:
    """Zeitplan bearbeiten (gleiche Prüfung wie beim Anlegen)."""
    raw, hcfg, _client = _heating_ctx()
    if _hsched.get_schedule(sid) is None:
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    entry = _hsched.update_schedule(sid, _heating_sched_fields(hcfg, body, raw))
    when = entry["date"] or ",".join(entry["days"])
    _app_log(f"Zeitplan geändert: {hcfg.room(body.room_id).name} {entry['when']} → {entry['setpoint']:g} °C ({when})")
    return entry


@app.patch("/api/heating/schedules/{sid}")
def heating_schedule_patch(sid: int, body: _HeatingSchedulePatch) -> dict[str, Any]:
    entry = _hsched.set_enabled(sid, body.enabled)
    if entry is None:
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    return entry


@app.delete("/api/heating/schedules/{sid}")
def heating_schedule_delete(sid: int) -> dict[str, Any]:
    entry = _hsched.get_schedule(sid)
    if entry is None or not _hsched.delete_schedule(sid):
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    _raw, hcfg, _client = _heating_ctx()
    room = hcfg.room(entry["room_id"])
    _app_log(f"Zeitplan gelöscht: {room.name if room else entry['room_id']} {entry['when']} → {entry['setpoint']:g} °C")
    return {"ok": True, "id": sid}



# ── Heizung: Chat (Phase 3) ─────────────────────────────────────────────────


def _shutters_enabled(raw: dict[str, Any]) -> bool:
    try:
        return _shutters.parse_shutters(raw).enabled
    except ValueError:
        return False


def _chat_status(raw: dict[str, Any], hcfg: "_heating.HeatingConfig") -> dict[str, Any]:
    try:
        return _hchat.status(hcfg, _hchat.parse_assistant(raw), shutters_enabled=_shutters_enabled(raw))
    except ValueError as e:
        return {"enabled": False, "ready": False, "hint": f"assistant-Config ungültig: {e}",
                "used_today": 0, "limit": 0, "key_set": False, "model": "", "provider": "", "provider_name": ""}


class _ChatMessage(BaseModel):
    role: str
    content: str


class _ChatRequest(BaseModel):
    messages: list[_ChatMessage]


class _ChatApply(BaseModel):
    proposal_id: str
    confirm_doors: bool = False          # Tür offen/ohne Kontakt: Schließen ausdrücklich bestätigt


def _chat_ctx() -> tuple[dict[str, Any], "_heating.HeatingConfig", Any, "_hchat.AssistantConfig"]:
    raw, hcfg, client = _heating_ctx()
    try:
        acfg = _hchat.parse_assistant(raw)
    except ValueError as e:
        raise HTTPException(500, f"assistant-Config ungültig: {e}")
    if not acfg.enabled:
        raise HTTPException(409, "Chat ist aus (Settings → Heizung → „Chat aktiv“).")
    if not (hcfg.enabled or _shutters_enabled(raw)) or client is None:
        raise HTTPException(409, "Heizung nicht aktiv oder openHAB nicht konfiguriert")
    return raw, hcfg, client, acfg


def _chat_shutters(raw: dict[str, Any]) -> "_shutters.ShutterConfig":
    try:
        return _shutters.parse_shutters(raw)
    except ValueError as e:
        raise HTTPException(500, f"shutters config ungültig: {e}")


@app.get("/api/heating/chat/status")
def heating_chat_status(response: Response) -> dict[str, Any]:
    """Chat-Status für die Leiste auf allen Tabs (ohne openHAB-Abfrage): an/aus, bereit, Zähler."""
    response.headers["Cache-Control"] = "no-store"
    raw, hcfg, _client = _heating_ctx()
    return _chat_status(raw, hcfg)


@app.post("/api/heating/chat")
def heating_chat(body: _ChatRequest) -> dict[str, Any]:
    """Text → Antwort: Abfrage aus der DB, Rückfrage oder Vorschlag (nichts wird gespeichert).
    Ein Chat für Heizung und Rollläden."""
    raw, hcfg, client, acfg = _chat_ctx()
    scfg = _chat_shutters(raw)
    hist = [{"role": m.role, "content": m.content.strip()[:_hchat.MAX_TEXT]}
            for m in body.messages[-_hchat.MAX_HISTORY:] if m.role in ("user", "assistant") and m.content.strip()]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    if not hist or hist[-1]["role"] != "user":
        raise HTTPException(400, "Leere Nachricht")
    now = datetime.now(_hsched.TZ)
    loc = _ssched.location(raw)
    entries = _hsched.list_schedules()
    base = {"chat": None}
    st_now = lambda: _hchat.status(hcfg, acfg, now, shutters_enabled=scfg.enabled)  # noqa: E731
    text = hist[-1]["content"]
    why = _explain.is_why(hist)                                # Warum-/Statusfrage: nur lesen, s. explain.py
    if not why and scfg.enabled and _schat.is_query(text, scfg):   # Rollläden zuerst: „was fahren die Rollläden …“
        rules = _schat.cached_rules(client) if any(sh.automation == "openhab" for sh in scfg.rooms) else []
        answer = _schat.local_answer(text, scfg, _ssched.list_schedules(), now, loc, rules, scfg.schedules_enabled)
        if answer is not None:
            return {**base, "type": "answer", "text": answer, "chat": st_now()}
    answer = _hchat.local_answer(text, hcfg, entries, now, loc) if hcfg.enabled and not why else None
    if answer is not None:
        return {**base, "type": "answer", "text": answer, "chat": st_now()}
    st = st_now()
    if not st["ready"]:
        kind = "limit" if st["key_set"] and st["used_today"] >= st["limit"] else "unavailable"
        return {**base, "type": kind, "text": st["hint"], "chat": st}
    if not _hchat.try_consume(acfg.max_requests_per_day, now):
        st = st_now()
        return {**base, "type": "limit", "text": st["hint"], "chat": st}
    rooms = _heating.read_status(hcfg, client).get("rooms", []) if hcfg.enabled else []
    context = _hchat.build_context(hcfg, rooms, entries, now, loc)
    sh_status: list[dict[str, Any]] = []
    if scfg.enabled:
        sh_status = _shutters.read_status(scfg, client, now).get("rooms", [])
        context += "\n" + _schat.build_context(scfg, sh_status, _ssched.list_schedules(), now, loc)
    ectx = None
    if why:
        svc = (raw.get("openhab") or {}).get("persistence") or "rrd4j"
        hist_fn = (lambda item, start, end: client.get_history(item, start, end, service_id=svc)) if client else None
        ectx = _explain.build(text, now, _load_cfg(), hcfg, scfg, rooms, sh_status, history=hist_fn)
        context += "\n\n" + ectx.text
    try:
        inp = _hchat.call_model(acfg, context, hist, explain=True) if why else _hchat.call_model(acfg, context, hist)
    except _hchat.ChatError as e:
        return {**base, "type": "error", "kind": e.kind, "text": str(e), "chat": st_now()}
    st = st_now()
    usage = inp.pop("_usage", None)
    expl = None
    if why:
        base = {**base, "basis": ectx.basis, "context_tokens": ectx.tokens, "usage": usage}
        e = inp.get("explanation")
        expl = _redact.redact(e.strip())[:_hchat.MAX_EXPLANATION] if isinstance(e, str) and e.strip() else None
        if not _explain.has_command(text):          # reine Frage: nie ein Vorschlag, was immer das Modell liefert
            for k in _hchat.WORK_KEYS:
                inp[k] = []
    q = inp.get("question")
    has_work = any(inp.get(k) for k in _hchat.WORK_KEYS)
    if why and not has_work:
        if expl is None and isinstance(q, str) and q.strip():
            return {**base, "type": "question", "text": q.strip()[:400], "chat": st}
        return {**base, "type": "explain", "chat": st,
                "text": expl or "Das kann ich nicht sehen – dazu habe ich keine Antwort bekommen. Bitte anders fragen."}
    if expl:
        base = {**base, "explanation": expl}
    if isinstance(q, str) and q.strip() and not has_work:
        return {**base, "type": "question", "text": q.strip()[:400], "chat": st}
    proposal, errors = _hchat.validate_proposal(hcfg, inp, now, loc)
    errors += _schat.validate(scfg, proposal, inp, now, loc)
    amb = _schat.ambiguity(text, scfg, _schat.touched(proposal, inp)) if scfg.enabled else None
    if amb:                                                      # Rückfrage statt raten
        return {**base, "type": "question", "text": amb, "chat": st}
    if errors:
        log.info("Chat: Vorschlag abgelehnt: %s", "; ".join(errors))
        return {**base, "type": "invalid", "text": "Das kann ich so nicht übernehmen:\n" + "\n".join(errors),
                "chat": st}
    if proposal.empty:
        if proposal.declined:
            return {**base, "type": "answer", "text": "\n".join(proposal.declined), "chat": st}
        return {**base, "type": "question", "text": "Ich habe nichts zum Planen gefunden. Was soll geschehen?",
                "chat": st}
    proposal.note = text[:200]
    _hchat.store(proposal)
    pv = _hchat.preview(hcfg, proposal, now, loc)
    pv["shutters"] = _schat.preview(scfg, proposal, now, loc, sh_status) if scfg.enabled else None
    return {**base, "type": "proposal", "proposal": pv, "schedules_enabled": hcfg.schedules_enabled,
            "shutter_schedules_enabled": scfg.schedules_enabled, "dry_run": _heating_dry_run(raw), "chat": st}


@app.post("/api/heating/chat/apply")
def heating_chat_apply(body: _ChatApply) -> dict[str, Any]:
    """Bestätigter Vorschlag: noch einmal prüfen, dann speichern/löschen/sofort setzen bzw. fahren."""
    raw, hcfg, client, _acfg = _chat_ctx()
    scfg = _chat_shutters(raw)
    p = _hchat.peek(body.proposal_id)
    if p is None:
        raise HTTPException(410, "Vorschlag abgelaufen – bitte neu fragen.")
    now = datetime.now(_hsched.TZ)
    loc = _ssched.location(raw)
    if p.shutter_immediate:                      # gleiche Regeln wie bei den Buttons – vor dem Verbrauchen
        st = _shutters.read_status(scfg, client, now)
        warn = _schat.door_warnings(scfg, p, st.get("rooms", []))
        if warn and not body.confirm_doors:
            raise HTTPException(409, "; ".join(sorted(set(warn.values()))) + " – Schließen nur mit Bestätigung.")
        dc = st.get("duty_cycle")
        if dc and dc["high"] and len(p.shutter_immediate) > 1:
            raise HTTPException(409, f"Duty Cycle {dc['value']:g} % über {dc['max']:g} % – mehrere Rollläden "
                                     f"gerade nicht, bitte einzeln.")
    p = _hchat.take(body.proposal_id)
    if p is None:
        raise HTTPException(410, "Vorschlag abgelaufen – bitte neu fragen.")
    try:                                                  # Zeit kann seit der Vorschau vergangen sein
        for e in p.entries:
            _hsched.validate_entry(hcfg, e["room_id"], e["setpoint"], e["time"] or None,
                                   days=e["days"].split(",") if e["days"] else None, date=e["date"], now=now,
                                   kind=e.get("kind") or "time", offset_min=e.get("offset_min") or 0,
                                   not_before=e.get("not_before"), not_after=e.get("not_after"), loc=loc)
        for e in p.shutter_entries:
            _ssched.validate_entry(scfg, e["shutter_id"], e["action"], e["kind"], position=e["position"],
                                   time=e["time"], offset_min=e["offset_min"], not_before=e["not_before"],
                                   not_after=e["not_after"], days=e["days"].split(",") if e["days"] else None,
                                   date=e["date"], loc=loc, now=now)
    except ValueError as e:
        raise HTTPException(400, f"Vorschlag nicht mehr gültig: {e}")
    created = [_hsched.create_schedule(e, source="chat", note=p.note) for e in p.entries]
    for e in created:
        room = hcfg.room(e["room_id"])
        _app_log(f"Zeitplan angelegt (Chat): {room.name} {e['when']} → {e['setpoint']:g} °C "
                 f"({e['date'] or ','.join(e['days'])})")
    deleted = []
    for d in p.deletes:
        if _hsched.delete_schedule(d["id"]):
            deleted.append(d["id"])
            room = hcfg.room(d["room_id"])
            _app_log(f"Zeitplan gelöscht (Chat): {room.name if room else d['room_id']} {d['when']} → "
                     f"{d['setpoint']:g} °C")
    sent = [_heating_send(raw, hcfg, client, hcfg.room(i["room_id"]), i["setpoint"], "Chat") for i in p.immediate]
    out = {"ok": all(r["ok"] for r in sent), "created": [e["id"] for e in created], "deleted": deleted,
           "immediate": sent, "schedules_enabled": hcfg.schedules_enabled}
    if not (p.shutter_entries or p.shutter_deletes or p.shutter_immediate):
        return out
    s_created = [_ssched.create_schedule(e, source="chat", note=p.note) for e in p.shutter_entries]
    for e in s_created:
        _app_log("Rollladen-Zeitplan angelegt (Chat): " + _sched_text(scfg.shutter(e["shutter_id"]).name, e),
                 area="shutters")
    s_deleted = []
    for d in p.shutter_deletes:
        if _ssched.delete_schedule(d["id"]):
            s_deleted.append(d["id"])
            sh = scfg.shutter(d["shutter_id"])
            _app_log("Rollladen-Zeitplan gelöscht (Chat): " + _sched_text(sh.name if sh else d["shutter_id"], d),
                     area="shutters")
    jobs = []
    for im in p.shutter_immediate:
        sh = scfg.shutter(im["shutter_id"])
        jobs.append((sh, lambda sh=sh, im=im: _shutter_send(raw, client, sh, im["action"], im["target"], "Chat")))
    try:
        s_sent = _shutters.send_sequence(scfg, jobs)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    for im, r in zip(p.shutter_immediate, s_sent):
        if r["ok"]:
            try:
                _ssched.clear_wait(im["shutter_id"])
            except sqlite3.Error:
                pass
    out.update(ok=out["ok"] and all(r["ok"] for r in s_sent), shutter_created=[e["id"] for e in s_created],
               shutter_deleted=s_deleted, shutter_immediate=s_sent,
               shutter_schedules_enabled=scfg.schedules_enabled)
    return out


# ── Rollläden (openHAB-Rollershutter) ───────────────────────────────────────

_SHUTTER_CMD_LABEL = {"up": "▲ Auf", "down": "▼ Ab", "stop": "■ Stop"}


def _shutters_ctx() -> tuple[dict[str, Any], "_shutters.ShutterConfig", Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    try:
        scfg = _shutters.parse_shutters(raw)
    except ValueError as e:
        raise HTTPException(500, f"shutters config ungültig: {e}")
    return raw, scfg, _heating.make_client(raw)


def _shutter_label(command: str, target: Optional[int]) -> str:
    return f"→ {target} %" if command == "position" else _SHUTTER_CMD_LABEL[command]


def _shutter_send(raw, client, sh: "_shutters.Shutter", command: str, target: Optional[int],
                  source: str) -> dict[str, Any]:
    """Einen Befehl senden (dry_run: nur loggen) und für die Rückprüfung merken."""
    what = f"{sh.name} {_shutter_label(command, target)} ({source}"
    if _heating_dry_run(raw):
        _app_log(f"DRY-RUN: {what}, nicht gesendet)", area="shutters")
        return {"shutter_id": sh.id, "ok": True, "dry_run": True, "command": command, "target": target}
    try:
        client.send_command(sh.item, _shutters.to_openhab(command, target))
    except _heating.OpenHabError as e:
        _app_log(f"FEHLER: {what}): {e}", logging.WARNING, area="shutters")
        return {"shutter_id": sh.id, "ok": False, "error": str(e)}
    _app_log(f"{what})", area="shutters")
    try:
        _shutters.record_command(sh, command, target, source, datetime.now(_shutters.TZ))
    except sqlite3.Error as e:
        log.warning("shutters: Befehl nicht gespeichert: %s", e)
    return {"shutter_id": sh.id, "ok": True, "dry_run": False, "command": command, "target": target}


@app.get("/api/shutters/status")
def shutters_status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    raw, scfg, client = _shutters_ctx()
    base = {
        "enabled": scfg.enabled,
        "dry_run": _heating_dry_run(raw),
        "openhab_configured": client is not None,
        "groups": scfg.groups(),
        "command_spacing_s": scfg.command_spacing_s,
    }
    base["schedules_enabled"] = scfg.schedules_enabled
    try:                                                 # ein Chat für Heizung + Rollläden: Status auch hier
        base["chat"] = _chat_status(raw, _heating.parse_heating(raw))
    except ValueError:
        base["chat"] = None
    if not scfg.enabled or client is None:
        return {**base, "ok": True, "error": None, "rooms": [], "duty_cycle": None}
    st = {**base, **_shutters.read_status(scfg, client)}
    now = datetime.now(_ssched.TZ)
    try:
        nxt = _ssched.next_rides(_ssched.list_schedules(), now, _ssched.location(raw))
        hints = _ssched.hints(now)
    except Exception as e:  # Zeitplan-DB darf die Anzeige nicht blockieren
        log.warning("shutters: Zeitpläne nicht lesbar: %s", e)
        nxt, hints = {}, {}
    rule_next = _rule_next(scfg, client, now)
    for r in st["rooms"]:
        auto = r["automation"] == "pvcontroller"
        r["next"] = nxt.get(r["id"]) if auto else None
        r["hint"] = hints.get(r["id"]) if auto else None
        r["rule_next"] = None if auto else rule_next.get(r["name"])
    return st


_RULE_FAIL = {"t": 0.0}


def _rule_next(scfg, client, now: datetime) -> dict[str, dict[str, Any]]:
    """Nächste Fahrt je openHAB-Rollladen (Name → {at, target, source}) aus den aktiven openHAB-Regeln.
    Nur lesen; dieselben 10 min zwischengespeicherten Regeldaten wie die Chat-Abfrage. Nie fatal."""
    if not any(sh.automation == "openhab" for sh in scfg.rooms):
        return {}
    if time.time() - _RULE_FAIL["t"] < _schat.RULES_TTL_S:      # nicht lesbar: erst nach 10 min erneut versuchen
        return {}
    try:
        rules = _schat.cached_rules(client)
        if rules is None:
            _RULE_FAIL["t"] = time.time()
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ride in sorted(_schat.openhab_rule_rides(rules, scfg, now, now + timedelta(days=2)), key=lambda x: x["at"]):
            out.setdefault(ride["shutter"], {"at": ride["at"].isoformat(timespec="minutes"), "target": ride["target"],
                                             "source": ride["source"]})
        return out
    except Exception as e:  # noqa: BLE001 – Anzeige-Zusatz, darf den Status nie blockieren
        log.warning("shutters: nächste openHAB-Regelzeit nicht bestimmbar: %s", e)
        return {}


class _ShutterCommand(BaseModel):
    shutter_id: str
    command: str                       # up | down | stop | position
    position: Optional[float] = None
    confirm: bool = False              # Tür offen/ohne Kontakt: Schließen ausdrücklich bestätigt


@app.post("/api/shutters/command")
def shutters_command(body: _ShutterCommand) -> dict[str, Any]:
    raw, scfg, client = _shutters_ctx()
    if not scfg.enabled or client is None:
        raise HTTPException(409, "Rollläden nicht aktiv oder openHAB nicht konfiguriert")
    sh = scfg.shutter(body.shutter_id)
    if sh is None:
        raise HTTPException(404, f"Rollladen {body.shutter_id!r} unbekannt")
    try:
        target = _shutters.validate_command(sh, body.command, body.position)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if sh.is_door and body.command in ("down", "position"):
        st = next((r for r in _shutters.read_status(scfg, client)["rooms"] if r["id"] == sh.id), None)
        current = st["position"] if st and st["position_reliable"] else None     # unsicher → unbekannt
        why = st["door_guard"] if st else _shutters.door_guard(sh, None)
        if why and _shutters.is_closing(body.command, target, current) and not body.confirm:
            raise HTTPException(409, why + " – Schließen nur mit Bestätigung.")
    res = _shutter_send(raw, client, sh, body.command, target, "manuell")
    if not res["ok"]:
        raise HTTPException(502, res["error"])
    try:
        _ssched.clear_wait(sh.id)                # Bedienung von Hand beendet ein Warten auf die Tür
    except sqlite3.Error as e:
        log.warning("shutters: Warten nicht beendet: %s", e)
    return res


class _ShutterGroupCommand(BaseModel):
    group: str                         # "all" oder Gruppenname
    command: str                       # up | down


@app.post("/api/shutters/group")
def shutters_group(body: _ShutterGroupCommand) -> dict[str, Any]:
    raw, scfg, client = _shutters_ctx()
    if not scfg.enabled or client is None:
        raise HTTPException(409, "Rollläden nicht aktiv oder openHAB nicht konfiguriert")
    if body.command not in ("up", "down"):
        raise HTTPException(400, "Gruppenbefehl nur auf/ab")
    members = scfg.members(body.group)
    if not members:
        raise HTTPException(404, f"Gruppe {body.group!r} unbekannt oder leer")
    st = _shutters.read_status(scfg, client)
    dc = st.get("duty_cycle")
    if dc and dc["high"]:
        raise HTTPException(409, f"Duty Cycle {dc['value']:g} % über {dc['max']:g} % – Gruppenbefehle pausiert.")
    contacts = {r["id"]: r["contact"] for r in st["rooms"]}
    label = "alle" if body.group == "all" else body.group.upper()
    try:
        results = _shutters.send_group(
            scfg, members, body.command,
            lambda sh: _shutter_send(raw, client, sh, body.command, _shutters.target_of(body.command),
                                     f"Gruppe {label}"),
            contacts)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    for r in results:
        if r.get("skipped"):
            _app_log(f"Gruppe {label} {_SHUTTER_CMD_LABEL[body.command]}: übersprungen – {r['reason']}",
                     area="shutters")
    return {"ok": all(r["ok"] for r in results), "results": results, "dry_run": _heating_dry_run(raw)}


@app.get("/api/shutters/config")
def shutters_config_get() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {"shutters": raw.get("shutters") or {"enabled": False, "command_spacing_s": 2, "duty_cycle_item": "",
                                                "duty_cycle_max": 80, "rooms": []},
            "openhab_configured": bool((raw.get("openhab") or {}).get("url"))}


class _ShuttersConfigUpdate(BaseModel):
    enabled: bool
    command_spacing_s: float = 2
    duty_cycle_item: str = ""
    duty_cycle_max: float = 80
    rooms: list[dict[str, Any]]
    schedules_enabled: Optional[bool] = None     # None = unverändert lassen
    frost_temp_c: Optional[float] = None
    frost_temp_item: Optional[str] = None
    manual_hold_min: Optional[int] = None


_SHUTTER_ROOM_KEYS = ("id", "name", "item", "contact_item", "is_door", "group", "automation", "device",
                      "can_position", "retry_when_closed", "auto_close_max", "position_source", "shelly_url")


@app.put("/api/shutters/config")
def shutters_config_put(body: _ShuttersConfigUpdate) -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        current = json.load(f)
    block = dict(current.get("shutters") or {})          # unbekannte Felder bleiben
    old_se = bool(block.get("schedules_enabled", False))
    for k in ("schedules_enabled", "frost_temp_c", "manual_hold_min"):
        if getattr(body, k) is not None:
            block[k] = getattr(body, k)
    if body.frost_temp_item is not None:
        block["frost_temp_item"] = body.frost_temp_item.strip()
    block.update({
        "enabled": body.enabled,
        "command_spacing_s": body.command_spacing_s,
        "duty_cycle_item": body.duty_cycle_item.strip(),
        "duty_cycle_max": body.duty_cycle_max,
        "rooms": [{k: v for k, v in r.items() if k in _SHUTTER_ROOM_KEYS and v not in (None, "")}
                  for r in body.rooms],
    })
    new_cfg = copy.deepcopy(current)
    new_cfg["shutters"] = block
    try:
        _shutters.parse_shutters(new_cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if block["enabled"] and not (new_cfg.get("openhab") or {}).get("url"):
        raise HTTPException(400, "openhab.url fehlt in config.json")
    backup = _write_config(new_cfg)
    if bool(block.get("schedules_enabled", False)) != old_se:
        _app_log(f"Rollladen-Zeitpläne {'aktiviert' if block['schedules_enabled'] else 'deaktiviert'} (Settings)",
                 area="shutters")
    return {"ok": True, "backup": backup, "shutters": block}


# ── Rollläden: Zeitpläne ────────────────────────────────────────────────────


class _ShutterScheduleCreate(BaseModel):
    shutter_id: str
    action: str                          # up | down | position
    kind: str                            # time | sunrise | sunset
    position: Optional[float] = None
    time: Optional[str] = None
    offset_min: int = 0
    not_before: Optional[str] = None
    not_after: Optional[str] = None
    days: Optional[list[str]] = None
    date: Optional[str] = None
    note: Optional[str] = None


class _ShutterSchedulePatch(BaseModel):
    enabled: bool


def _sched_text(sh_name: str, e: dict[str, Any]) -> str:
    return (f"{sh_name} {_ssched.describe(e)} → {_ssched.target_label(e['action'], e['position'])} "
            f"({e['date'] or ','.join(e['days']) if isinstance(e['days'], list) else e['days']})")


@app.get("/api/shutters/schedules")
def shutters_schedules(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    raw, scfg, _client = _shutters_ctx()
    now = datetime.now(_ssched.TZ)
    loc = _ssched.location(raw)
    items = _with_today(_ssched.list_schedules(), _ssched.instant, now, loc)
    today = {}
    if loc:
        rise, sset = _ssched.sun_times(now.date(), *loc)
        today = {"sunrise": rise.strftime("%H:%M") if rise else None, "sunset": sset.strftime("%H:%M") if sset else None}
    return {"schedules_enabled": scfg.schedules_enabled, "dry_run": _heating_dry_run(raw),
            "now": now.isoformat(timespec="minutes"), "items": items, "sun_today": today,
            "sun_tomorrow": _sun_hm(now.date() + timedelta(days=1), loc),
            "next": _ssched.next_rides(items, now, loc)}


@app.post("/api/shutters/schedules")
def shutters_schedule_create(body: _ShutterScheduleCreate) -> dict[str, Any]:
    raw, scfg, _client = _shutters_ctx()
    entry = _ssched.create_schedule(_shutter_sched_fields(scfg, body, raw), source="ui",
                                    note=(body.note or "").strip()[:200])
    _app_log("Rollladen-Zeitplan angelegt: " + _sched_text(scfg.shutter(body.shutter_id).name, entry), area="shutters")
    return entry


def _shutter_sched_fields(scfg, body: "_ShutterScheduleCreate", raw: dict[str, Any]) -> dict[str, Any]:
    """Formular im Tab: an jedem Rollladen erlaubt; bei „Automatik: openHAB-Regel“ ruht der Eintrag."""
    pos = body.position
    if isinstance(pos, float) and pos.is_integer():
        pos = int(pos)
    try:
        return _ssched.validate_entry(scfg, body.shutter_id, body.action, body.kind, position=pos,
                                      time=body.time, offset_min=body.offset_min, not_before=body.not_before,
                                      not_after=body.not_after, days=body.days, date=body.date or None,
                                      loc=_ssched.location(raw), any_automation=True)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/shutters/schedules/{sid}")
def shutters_schedule_update(sid: int, body: _ShutterScheduleCreate) -> dict[str, Any]:
    """Rollladen-Zeitplan bearbeiten (gleiche Prüfung wie beim Anlegen)."""
    raw, scfg, _client = _shutters_ctx()
    if _ssched.get_schedule(sid) is None:
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    entry = _ssched.update_schedule(sid, _shutter_sched_fields(scfg, body, raw))
    _app_log("Rollladen-Zeitplan geändert: " + _sched_text(scfg.shutter(body.shutter_id).name, entry), area="shutters")
    return entry


@app.patch("/api/shutters/schedules/{sid}")
def shutters_schedule_patch(sid: int, body: _ShutterSchedulePatch) -> dict[str, Any]:
    entry = _ssched.set_enabled(sid, body.enabled)
    if entry is None:
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    return entry


@app.delete("/api/shutters/schedules/{sid}")
def shutters_schedule_delete(sid: int) -> dict[str, Any]:
    entry = _ssched.get_schedule(sid)
    if entry is None or not _ssched.delete_schedule(sid):
        raise HTTPException(404, f"Zeitplan {sid} unbekannt")
    _raw, scfg, _client = _shutters_ctx()
    sh = scfg.shutter(entry["shutter_id"])
    _app_log("Rollladen-Zeitplan gelöscht: " + _sched_text(sh.name if sh else entry["shutter_id"], entry),
             area="shutters")
    return {"ok": True, "id": sid}
