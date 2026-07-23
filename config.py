"""Konfiguration laden und validieren."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SolaxConfig:
    url: str
    pwd: str


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

    def __post_init__(self) -> None:
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

    def __post_init__(self) -> None:
        if self.storage_max_temp <= 0:
            raise ValueError(
                f"heater.storage_max_temp must be > 0, got {self.storage_max_temp}"
            )
        if self.storage_temp_hysteresis < 0:
            raise ValueError(
                "heater.storage_temp_hysteresis must be >= 0, "
                f"got {self.storage_temp_hysteresis}"
            )

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
class Config:
    solax: SolaxConfig
    shelly: ShellyConfig
    heater: HeaterConfig
    runtime: RuntimeConfig
    simulation: SimulationConfig
    wallbox: WallboxConfig
    summer_mode: SummerModeConfig = field(default_factory=SummerModeConfig)


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
    )
