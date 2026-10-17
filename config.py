"""Konfiguration laden und validieren."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SolaxConfig:
    url: str
    pwd: str
    enabled: bool = True
    name: str = "Dach"
    # Skalenende der PV-Balken (W): `max_w` gilt für das ganze Gerät (solax2),
    # bei `solax` je String über str1_max_w / str2_max_w
    max_w: float = 10000.0
    str1_max_w: float = 11500.0
    str2_max_w: float = 6500.0
    # Eigenes HTTP-Timeout (s) für dieses Gerät; None = runtime.request_timeout_seconds.
    # Der Pergola-WR (solax2) antwortet langsam (≈ 1–1,5 s), ist nachts aber nicht erreichbar:
    # ein kurzes, eigenes Timeout hält den Lauf kurz (2 Versuche je Lesen).
    timeout_s: Optional[float] = None


@dataclass
class ZendureConfig:
    """Zendure SolarFlow 2400 AC (lokale zenSDK-API)."""
    enabled: bool = False
    ip: str = ""
    sn: str = ""                  # leer = aus dem Report übernehmen
    name: str = "Batterie"
    capacity_wh: float = 2400.0
    max_charge_w: float = 2400.0
    max_discharge_w: float = 2400.0
    min_soc: float = 10.0
    target_soc: float = 100.0
    control_enabled: bool = False  # Master-Schalter für Phase 2 (Steuerung)
    # Entladen regelt auf diesen Netzbezug statt auf 0 W: Puffer gegen Einspeisung aus der Batterie,
    # wenn eine Last abschaltet (der Wächter senkt erst ab > 100 W Einspeisung)
    discharge_target_grid_w: float = 40.0
    # Entladen ist fest gesperrt, solange die Wallbox lädt (früherer Schalter
    # block_discharge_while_wallbox_charging entfällt; alter Config-Eintrag wird ignoriert)


METER_TYPES = ("shelly_3em_gen1", "shelly_pro_3em")


@dataclass
class ShellyConfig:
    ph1_url: str
    ph2_url: str
    ph3_url: str
    storage_url: str
    main_meter_url: str
    heater_meter_url: str
    # Gen1 (Plug S, 1, 1PM …, ein Kanal) oder shelly_gen2 (Plus, Pro …).
    # Ein Gen2-Gerät mit mehreren Kanälen (z. B. Pro 2PM) kann für zwei
    # Phasen mit gleicher url und unterschiedlichem channel (0/1) genutzt
    # werden. Default = altes Verhalten (Gen1, Kanal 0).
    ph1_type: str = "shelly_gen1"
    ph1_channel: int = 0
    ph2_type: str = "shelly_gen1"
    ph2_channel: int = 0
    ph3_type: str = "shelly_gen1"
    ph3_channel: int = 0
    # Hauptzähler: Shelly 3EM Gen1 (/status) oder Shelly Pro 3EM (Gen2, /rpc/EM…)
    main_meter_type: str = "shelly_3em_gen1"

    def __post_init__(self) -> None:
        if self.main_meter_type not in METER_TYPES:
            raise ValueError(
                f"shelly.main_meter_type must be one of {', '.join(METER_TYPES)}, got {self.main_meter_type!r}"
            )
        for name, type_ in (
            ("ph1_type", self.ph1_type),
            ("ph2_type", self.ph2_type),
            ("ph3_type", self.ph3_type),
        ):
            if type_ not in ("shelly_gen1", "shelly_gen2"):
                raise ValueError(
                    f"shelly.{name} must be 'shelly_gen1' or 'shelly_gen2', got {type_!r}"
                )


@dataclass
class HeaterConfig:
    phase_power_w: int = 1500
    storage_max_temp: float = 63.0
    storage_temp_hysteresis: float = 1.0
    min_pv_power_ph1: float = 1500.0
    min_pv_power_ph2: float = 3000.0
    min_pv_power_ph3: float = 4500.0
    # Erlaubter Netzbezug (W): wird von den min_pv_power_phX-Schwellen abgezogen,
    # d.h. der Heizstab startet schon mit (min_pv_power - grid_assist_w) Überschuss.
    grid_assist_w: float = 0.0
    # Phasenlogik (heater_phases.py): Mindestzeiten je Phase, Halte-Hysterese beim
    # Runterschalten (Anteil von phase_power_w) und Glättung des Budgets (Mittel, Minuten)
    phase_min_on_s: float = 420.0
    phase_min_off_s: float = 120.0
    phase_down_hyst: float = 0.15
    budget_smooth_min: int = 10        # Zuschalten: Mittel über so viele Minuten
    budget_smooth_down_min: int = 2    # Abschalten: kürzeres Mittel (schneller reagieren)
    # Einzelne Phase stilllegen (defekter Shelly/Heizstab-Phase): wird nicht gelesen,
    # nicht geschaltet und zählt nicht im Budget
    phase1_disabled: bool = False
    phase2_disabled: bool = False
    phase3_disabled: bool = False
    # Komfort-Mindesttemperatur des Wasserspeichers: bis hierher hat der Heizstab in der
    # Kaskade (Sommerprofil) Vorrang vor der Batterie. None = summer_mode.target_temp.
    comfort_min_temp: Optional[float] = None
    # Winterprofil: Untergrenze, bis zu der der Wasserspeicher VOR der Batterie geheizt wird
    # (Öl sichert die Wärme, aber ein ausgekühlter Speicher soll nicht leer bleiben). None = aus.
    winter_comfort_min_temp: Optional[float] = 40.0

    @property
    def disabled_phases(self) -> tuple[bool, bool, bool]:
        return (bool(self.phase1_disabled), bool(self.phase2_disabled), bool(self.phase3_disabled))

    @property
    def enabled_phase_count(self) -> int:
        return 3 - sum(self.disabled_phases)

    def __post_init__(self) -> None:
        if self.budget_smooth_down_min < 1:
            raise ValueError("heater.budget_smooth_down_min must be >= 1")
        if self.phase_min_on_s < 0 or self.phase_min_off_s < 0:
            raise ValueError("heater.phase_min_on_s / phase_min_off_s must be >= 0")
        if not 0 <= self.phase_down_hyst < 1:
            raise ValueError("heater.phase_down_hyst must be in [0, 1)")
        if self.budget_smooth_min < 1:
            raise ValueError("heater.budget_smooth_min must be >= 1")
        if self.grid_assist_w < 0:
            raise ValueError(
                f"heater.grid_assist_w must be >= 0, got {self.grid_assist_w}"
            )
        if self.storage_max_temp <= 0:
            raise ValueError(
                f"heater.storage_max_temp must be > 0, got {self.storage_max_temp}"
            )
        if self.storage_temp_hysteresis < 0:
            raise ValueError(
                "heater.storage_temp_hysteresis must be >= 0, "
                f"got {self.storage_temp_hysteresis}"
            )

    def effective_min_pv(self, min_pv: float) -> float:
        return float(max(0.0, min_pv - self.grid_assist_w))

    @property
    def heat_resume_temp(self) -> float:
        return self.storage_max_temp - self.storage_temp_hysteresis


@dataclass
class RuntimeConfig:
    enabled: bool
    dry_run: bool
    request_timeout_seconds: int
    log_file: str


@dataclass
class SimulationConfig:
    enabled: bool = False
    pv_power: float = 0.0
    main_meter_power: float = 0.0
    heater_meter_power: float = 0.0
    storage_temp: float = 0.0
    ph1_on: bool = False
    ph2_on: bool = False
    ph3_on: bool = False


@dataclass
class WallboxConfig:
    enabled: bool = False
    url: str = ""
    only_control_when_pv_surplus_active: bool = True
    pause_below_storage_temp: float = 58.0
    release_above_storage_temp: float = 62.0
    request_timeout_seconds: int = 5
    fail_safe: str = "no_change"
    eco_phases: int = 1
    eco_amps: int = 16
    basic_phases: int = 2
    basic_amps: int = 10
    auto_reset_on_unplug: bool = True

    def __post_init__(self) -> None:
        if self.release_above_storage_temp <= self.pause_below_storage_temp:
            raise ValueError(
                "wallbox.release_above_storage_temp must be > "
                "wallbox.pause_below_storage_temp "
                f"(got release={self.release_above_storage_temp}, "
                f"pause={self.pause_below_storage_temp})"
            )
        if self.enabled and not self.url:
            raise ValueError(
                "wallbox.url must be set when wallbox.enabled=true"
            )
        for name, value in (
            ("eco_phases", self.eco_phases),
            ("basic_phases", self.basic_phases),
        ):
            if value not in (1, 2):
                raise ValueError(f"wallbox.{name} must be 1 or 2 (got {value})")
        for name, value in (
            ("eco_amps", self.eco_amps),
            ("basic_amps", self.basic_amps),
        ):
            if not 6 <= value <= 32:
                raise ValueError(f"wallbox.{name} must be between 6 and 32 (got {value})")


@dataclass
class SummerModeConfig:
    enabled: bool = False
    min_temp: float = 45.0
    target_temp: float = 52.0

    def __post_init__(self) -> None:
        if self.min_temp >= self.target_temp:
            raise ValueError(
                f"summer_mode.min_temp must be < target_temp "
                f"(got min={self.min_temp}, target={self.target_temp})"
            )


@dataclass
class CascadeConfig:
    # Debug: keep_on/keep_off/budget wie früher jede Minute in cascade_log schreiben (wächst stark)
    log_verbose: bool = False


@dataclass
class Config:
    solax: SolaxConfig
    shelly: ShellyConfig
    heater: HeaterConfig
    runtime: RuntimeConfig
    simulation: SimulationConfig
    wallbox: WallboxConfig
    summer_mode: SummerModeConfig = field(default_factory=SummerModeConfig)
    # Zweiter Wechselrichter (Pergola-PV); None = nicht konfiguriert
    solax2: Optional[SolaxConfig] = None
    # Warnmarke für die Hausanschluss-Belastung (kW), Lastspitzen-Anzeige
    house_max_load_kw: float = 11.0
    zendure: Optional[ZendureConfig] = None
    cascade: CascadeConfig = field(default_factory=CascadeConfig)

    @property
    def comfort_min_temp(self) -> float:
        """Wasserspeicher-Komfortminimum (Kaskade, Stufe „bis Min“); Default = Sommermodus-Zieltemperatur."""
        t = self.heater.comfort_min_temp
        t = self.summer_mode.target_temp if t is None else float(t)
        return min(t, self.heater.storage_max_temp)

    def comfort_min_for(self, season: str) -> Optional[float]:
        """Untergrenze der Stufe „Wasserspeicher bis Min“ je Profil; None = Stufe gibt es nicht."""
        if season == "summer":
            return self.comfort_min_temp
        t = self.heater.winter_comfort_min_temp
        return None if t is None else min(float(t), self.heater.storage_max_temp)

    @property
    def zendure_active(self) -> bool:
        return self.zendure is not None and self.zendure.enabled and bool(self.zendure.ip)

    @property
    def solax2_timeout(self) -> float:
        """HTTP-Timeout für den Pergola-WR (eigener Wert, sonst Standard 3 s)."""
        t = self.solax2.timeout_s if self.solax2 is not None else None
        return float(SOLAX2_TIMEOUT_S if t is None else t)

    @property
    def solax2_active(self) -> bool:
        return self.solax2 is not None and self.solax2.enabled and bool(self.solax2.url)


def parse_zendure(raw: dict) -> Optional[ZendureConfig]:
    block = raw.get("zendure")
    if not block:
        return None
    return ZendureConfig(**{k: v for k, v in block.items() if k in ZendureConfig.__dataclass_fields__})


SOLAX2_TIMEOUT_S = 3.0      # Default für den Pergola-WR (solax2.timeout_s)


def parse_solax2(raw: dict) -> Optional[SolaxConfig]:
    """solax2-Block aus der Roh-Config; fehlend -> None, enabled-Default False."""
    block = raw.get("solax2")
    if not block:
        return None
    return SolaxConfig(
        url=block.get("url", ""),
        pwd=block.get("pwd", ""),
        enabled=bool(block.get("enabled", False)),
        name=block.get("name", "Pergola"),
        max_w=float(block.get("max_w", 1000.0)),
        timeout_s=float(block.get("timeout_s", SOLAX2_TIMEOUT_S)),
    )


def load_config(path: str | Path) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

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
        cascade=CascadeConfig(**{
            k: v for k, v in raw.get("cascade", {}).items() if k in CascadeConfig.__dataclass_fields__
        }),
    )
