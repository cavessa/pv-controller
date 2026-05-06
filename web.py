"""FastAPI-App: lokale Weboberfläche für den PV-Controller.

Wichtig:
- /api/status ist *read-only* und schaltet niemals.
- /api/run-once führt einen NORMAL-Lauf aus (respektiert runtime.enabled +
  runtime.dry_run wie `python main.py`).
- Keine Auth — nur im lokalen Netz betreiben (siehe README).
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

app = FastAPI(title="PV-Controller Web", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


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
        "frc": s.force_state,
        "alw": s.allowed,
        "car": s.car_state,
        "amp": s.amp,
        "acs": s.access_control_state,
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
        "target_access_state": d.target_access_state,
        "access_executed": d.access_executed,
        "access_execution_error": d.access_execution_error,
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

    if result.temp_status is TempStatus.AT_OR_ABOVE_MAX:
        state = "Speicher voll"
        reason = (
            f"Speicher {temp:.1f} °C ≥ {cfg.heater.storage_max_temp:.0f} °C. "
            "Heizstab aus."
        )
        severity = "ok"
    elif result.temp_status is TempStatus.HYSTERESIS_BAND:
        state = "Hysterese"
        reason = (
            f"Speicher {temp:.1f} °C im Hysteresefenster "
            f"({cfg.heater.heat_resume_temp:.0f}–{cfg.heater.storage_max_temp:.0f} °C). "
            "Keine neuen Phasen."
        )
        severity = "ok"
    else:  # BELOW_RESUME
        state = "Speicher priorisiert"
        reason = (
            f"Speicher {temp:.1f} °C unter "
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

    return {"state": state, "reason": reason, "severity": severity}


def _serialize_status(result: ControllerResult, cfg: Config) -> dict[str, Any]:
    r = result.readings
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
            "pv_power": r.pv_power_w,
            "main_meter_power": r.main_meter_power_w,
            "heater_meter_power": r.heater_meter_power_w,
            "surplus_without_heater": r.surplus_without_heater_w,
            "phases": {
                "ph1": r.ph1_on,
                "ph2": r.ph2_on,
                "ph3": r.ph3_on,
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
        "summary": _summary(result, cfg),
    }


# ---- Temperature history ---------------------------------------------------


def _append_history(temp: float | None, wb_w: float | None) -> None:
    now = datetime.now()
    entry: dict = {"t": now.isoformat(timespec="minutes")}
    if temp is not None:
        entry["temp"] = round(temp, 1)
    if wb_w is not None:
        entry["wb_w"] = round(wb_w, 0)
    if len(entry) == 1:
        return
    data: list[dict] = []
    if TEMP_HISTORY_PATH.exists():
        try:
            with open(TEMP_HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    if data and data[-1]["t"] == entry["t"]:
        # merge new fields into existing minute entry
        data[-1].update(entry)
    else:
        cutoff = (now - timedelta(hours=25)).isoformat(timespec="minutes")
        data = [e for e in data if e["t"] >= cutoff]
        data.append(entry)
    try:
        with open(TEMP_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


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
    cfg = _load_cfg()
    try:
        result = run_controller(cfg, mode=ControllerMode.READ_ONLY)
    except Exception as e:
        log.exception("read-only controller run failed")
        raise HTTPException(status_code=500, detail=f"controller error: {e}")
    ws = result.wallbox_status
    _append_history(result.readings.storage_temp_c, (ws.power_w or 0.0) if ws is not None else None)
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
    _append_history(result.readings.storage_temp_c, (ws.power_w or 0.0) if ws is not None else None)
    return _serialize_status(result, cfg)


@app.get("/api/temp-history")
def api_temp_history(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if not TEMP_HISTORY_PATH.exists():
        return {"entries": []}
    try:
        with open(TEMP_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"entries": []}
    today = datetime.now().strftime("%Y-%m-%d")
    return {"entries": [e for e in data if e["t"].startswith(today)]}


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
}
_ALLOWED_WALLBOX_KEYS = {
    "enabled",
    "only_control_when_pv_surplus_active",
    "pause_below_storage_temp",
    "release_above_storage_temp",
    "fail_safe",
    "url",
}
_ALLOWED_SOLAX_KEYS = {"url", "pwd"}
_ALLOWED_SHELLY_KEYS = {
    "ph1_url", "ph2_url", "ph3_url",
    "storage_url", "main_meter_url", "heater_meter_url",
}


class ConfigUpdate(BaseModel):
    runtime: dict[str, Any] | None = None
    heater: dict[str, Any] | None = None
    wallbox: dict[str, Any] | None = None
    solax: dict[str, Any] | None = None
    shelly: dict[str, Any] | None = None


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
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise HTTPException(400, f"heater.{k} must be a number")
            new_cfg["heater"][k] = v

    if patch.wallbox:
        bad = set(patch.wallbox) - _ALLOWED_WALLBOX_KEYS
        if bad:
            raise HTTPException(400, f"wallbox keys not allowed: {sorted(bad)}")
        for k, v in patch.wallbox.items():
            if k in {"enabled", "only_control_when_pv_surplus_active"}:
                if not isinstance(v, bool):
                    raise HTTPException(400, f"wallbox.{k} must be boolean")
            elif k in {"pause_below_storage_temp", "release_above_storage_temp"}:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise HTTPException(400, f"wallbox.{k} must be a number")
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

    if patch.shelly:
        bad = set(patch.shelly) - _ALLOWED_SHELLY_KEYS
        if bad:
            raise HTTPException(400, f"shelly keys not allowed: {sorted(bad)}")
        for k, v in patch.shelly.items():
            if not isinstance(v, str) or not (v.startswith("http://") or v.startswith("https://")):
                raise HTTPException(400, f"shelly.{k} must start with http:// or https://")
            new_cfg["shelly"][k] = v

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
        WallboxConfig,
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


@app.get("/api/solax")
def api_solax(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()
    from clients.solax_client import SolaxClient
    client = SolaxClient(cfg.solax.url, cfg.solax.pwd, cfg.runtime.request_timeout_seconds)
    data = client.get_realtime_data()
    if data is None:
        raise HTTPException(status_code=503, detail="Solax device not reachable")
    selfuse = round(max(0.0, data.yield_today_kwh - data.grid_out_today_kwh), 2)
    return {
        "pv1_power_w": data.pv1_power_w,
        "pv2_power_w": data.pv2_power_w,
        "pv_total_w": data.pv_total_w,
        "pv1_voltage_v": data.pv1_voltage_v,
        "pv2_voltage_v": data.pv2_voltage_v,
        "pv1_current_a": data.pv1_current_a,
        "pv2_current_a": data.pv2_current_a,
        "feed_in_w": data.feed_in_w,
        "consumption_w": data.consumption_w,
        "yield_today_kwh": data.yield_today_kwh,
        "grid_out_today_kwh": data.grid_out_today_kwh,
        "grid_in_today_kwh": data.grid_in_today_kwh,
        "self_consumption_kwh": selfuse,
        "mode": data.mode,
    }


@app.get("/api/history/hourly")
def api_hourly_history(
    response: Response,
    for_date: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_hourly_data
    date_str = for_date or datetime.now().strftime("%Y-%m-%d")
    return {"date": date_str, "entries": get_hourly_data(date_str)}


@app.get("/api/history/daily")
def api_daily_history(
    response: Response,
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    from db import get_daily_history
    return {"entries": get_daily_history(days)}


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


@app.get("/api/cascade/devices")
def cascade_devices_get(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"devices": get_cascade_devices()}


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
    existing_ids = {d["id"] for d in get_cascade_devices()}
    missing = [i for i in body.deviceIds if i not in existing_ids]
    if missing:
        raise HTTPException(404, f"Unbekannte Geräte-IDs: {missing}")
    if len(body.deviceIds) != len(set(body.deviceIds)):
        raise HTTPException(400, "deviceIds enthält Duplikate")
    reorder_cascade_devices(body.deviceIds)
    return {"devices": get_cascade_devices()}


@app.get("/api/cascade/status")
def cascade_status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    cfg = _load_cfg()

    # Echte Messwerte via Read-Only-Controllerlauf (Shelly-Meter + Wallbox)
    heater_power_w: Optional[float] = None
    wb_charging: bool = False
    wb_power_w: Optional[float] = None
    surplus_w: Optional[int] = None
    try:
        result = run_controller(cfg, mode=ControllerMode.READ_ONLY)
        r = result.readings
        heater_power_w = r.heater_meter_power_w
        # main_meter ist negativ bei Einspeisung → Überschuss = max(0, -main_meter)
        if r.main_meter_power_w is not None:
            surplus_w = max(0, round(-r.main_meter_power_w))
        ws = result.wallbox_status
        if ws is not None:
            wb_charging = (ws.car_state == 2)
            wb_power_w = ws.power_w
    except Exception:
        log.warning("cascade/status: Controller-Lesefehler", exc_info=True)

    devices = get_cascade_devices()
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
        elif d["id"] == "wallbox":
            entry["is_on"] = wb_charging
            if wb_power_w is not None:
                entry["last_status_power_w"] = round(wb_power_w)
        return entry

    device_status = [_enrich(d) for d in devices]
    return {"surplus_watts": surplus_w, "devices": device_status}


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
