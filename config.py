"""Konfiguration laden und validieren."""

from __future__ import annotations

import json
import re
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
HEATER_PHASES = ("", "L1", "L2", "L3")


@dataclass
class WhirlpoolMeterConfig:
    """Whirlpool-Zähler (eigener Shelly 3EM, nur lesen – nichts wird geschaltet).

    ``subtract_heater_phase``: läuft eine Heizstab-Phase durch diesen Zähler ("L1"/"L2"/"L3"), wird
    deren Leistung (Heizstab-3EM, gleiche Phase, gleicher Zeitpunkt) abgezogen; "" = nichts abziehen.
    """
    enabled: bool = False
    url: str = ""
    type: str = "shelly_3em_gen1"
    name: str = "Whirlpool"
    subtract_heater_phase: str = ""

    def __post_init__(self) -> None:
        if self.type not in METER_TYPES:
            raise ValueError(f"whirlpool_meter.type must be one of {METER_TYPES}")
        if self.subtract_heater_phase not in HEATER_PHASES:
            raise ValueError("whirlpool_meter.subtract_heater_phase must be '', 'L1', 'L2' or 'L3'")
        if self.enabled and not self.url.strip():
            raise ValueError("whirlpool_meter.url required when enabled")


@dataclass
class WaterheaterMeterConfig:
    """Durchlauferhitzer-Zähler (eigener Shelly 3EM / Pro 3EM, nur lesen – nichts wird geschaltet)."""
    enabled: bool = False
    url: str = ""
    type: str = "shelly_pro_3em"
    name: str = "Durchlauferhitzer"

    def __post_init__(self) -> None:
        if self.type not in METER_TYPES:
            raise ValueError(f"waterheater_meter.type must be one of {METER_TYPES}")
        if self.enabled and not self.url.strip():
            raise ValueError("waterheater_meter.url required when enabled")


ITEM_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass
class ConsumerDevice:
    """Ein Verbraucher mit Leistungs-Item in openHAB (nur lesen).

    ``parent``: Item des vorgeschalteten Zählers ("" = direkt am Hauptzähler). Wird der Zähler gezählt,
    zählt das Gerät nicht noch einmal. ``in_sum`` False: nur anzeigen, nicht in Summe/Rest (z. B. ein
    Zähler, dessen Zuordnung noch offen ist).
    """
    item: str
    name: str = ""
    parent: str = ""
    in_sum: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.item, str) or not ITEM_RE.match(self.item):
            raise ValueError(f"consumers: ungültiger Item-Name {self.item!r}")
        if not isinstance(self.name, str) or not isinstance(self.parent, str):
            raise ValueError(f"consumers.{self.item}: name/parent müssen Text sein")
        if not isinstance(self.in_sum, bool):
            raise ValueError(f"consumers.{self.item}: in_sum muss true/false sein")
        self.name = self.name.strip() or self.item
        self.parent = self.parent.strip()


@dataclass
class ConsumersConfig:
    """Verbraucher-Card: Leistungs-Items aus openHAB, nur lesen (es wird nichts geschaltet).

    ``small_avg_w``: Geräte mit kleinerem 24-h-Mittel stehen in der Card unter „Sonstige“.
    openHAB-Zugang kommt aus dem Block ``openhab`` (url, token, timeout_s).
    """
    enabled: bool = False
    small_avg_w: float = 20.0
    devices: list = field(default_factory=list)
    openhab_url: str = ""
    openhab_token: str = field(default="", repr=False)
    openhab_timeout_s: float = 3.0

    def __post_init__(self) -> None:
        self.devices = [d if isinstance(d, ConsumerDevice) else ConsumerDevice(**d) for d in self.devices]
        if not isinstance(self.small_avg_w, (int, float)) or isinstance(self.small_avg_w, bool) \
                or not 0 <= self.small_avg_w <= 1000:
            raise ValueError("consumers.small_avg_w muss zwischen 0 und 1000 liegen")
        items = [d.item for d in self.devices]
        if len(items) != len(set(items)):
            raise ValueError("consumers: Item doppelt eingetragen")
        by = {d.item: d for d in self.devices}
        for d in self.devices:
            seen, p = {d.item}, d.parent
            while p:
                if p not in by:
                    raise ValueError(f"consumers.{d.item}: parent {p!r} ist kein eingetragenes Gerät")
                if p in seen:
                    raise ValueError(f"consumers.{d.item}: Zähler-Zuordnung im Kreis")
                seen.add(p)
                p = by[p].parent
        if self.enabled and not self.openhab_url.strip():
            raise ValueError("consumers: openhab.url erforderlich")


DW_ITEM_KEYS = ("remote_start", "operation_state", "selected_program", "door", "remaining", "action")
DW_MODES = ("latest_start", "finish_by")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class DishwasherConfig:
    """Geschirrspüler (Home Connect über openHAB): startet bei PV-Überschuss, spätestens zur Frist.

    ``deadline_mode``: "latest_start" = spätestens um ``latest_start`` starten; "finish_by" = so starten,
    dass er bis ``finish_by`` fertig ist (gelernte Programmdauer). ``dry_run``: nie wirklich starten
    (zusätzlich zu runtime.dry_run). openHAB-Zugang aus dem Block ``openhab``.
    """
    enabled: bool = False
    dry_run: bool = False
    items: dict = field(default_factory=dict)
    start_surplus_w: float = 1500.0
    surplus_minutes: int = 5
    deadline_mode: str = "latest_start"
    latest_start: str = "14:00"
    finish_by: str = "18:00"
    assumed_power_w: float = 2000.0
    # Heizstab in der Stufe „bis Max“ (Speicher schon über der Min-Temperatur) steckt für den Spüler zurück:
    # seine Leistung zählt beim Start-Überschuss mit
    heater_yields: bool = True
    openhab_url: str = ""
    openhab_token: str = field(default="", repr=False)
    openhab_timeout_s: float = 3.0

    def __post_init__(self) -> None:
        if not isinstance(self.items, dict) or set(self.items) - set(DW_ITEM_KEYS):
            raise ValueError(f"dishwasher.items: erlaubt sind {DW_ITEM_KEYS}")
        for k, v in self.items.items():
            if not isinstance(v, str) or (v and not ITEM_RE.match(v)):
                raise ValueError(f"dishwasher.items.{k}: ungültiger Item-Name")
        for k in ("enabled", "dry_run", "heater_yields"):
            if not isinstance(getattr(self, k), bool):
                raise ValueError(f"dishwasher.{k} muss true/false sein")
        for k, lo, hi in (("start_surplus_w", 100, 10000), ("assumed_power_w", 0, 5000)):
            v = getattr(self, k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(f"dishwasher.{k} muss zwischen {lo} und {hi} liegen")
        if not isinstance(self.surplus_minutes, int) or isinstance(self.surplus_minutes, bool) \
                or not 1 <= self.surplus_minutes <= 60:
            raise ValueError("dishwasher.surplus_minutes muss zwischen 1 und 60 liegen")
        if self.deadline_mode not in DW_MODES:
            raise ValueError(f"dishwasher.deadline_mode muss {DW_MODES} sein")
        for k in ("latest_start", "finish_by"):
            if not isinstance(getattr(self, k), str) or not _HHMM.match(getattr(self, k)):
                raise ValueError(f"dishwasher.{k} muss HH:MM sein")
        if self.enabled:
            missing = [k for k in DW_ITEM_KEYS if not self.items.get(k)]
            if missing:
                raise ValueError(f"dishwasher: Items fehlen: {missing}")
            if not self.openhab_url.strip():
                raise ValueError("dishwasher: openhab.url erforderlich")


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
    # Puffer für die Batterie: so viel weniger bekommt die Wallbox (Eco), solange die Batterie im Profil
    # davor nicht voll ist und noch nicht mit voller Ladeleistung lädt
    battery_buffer_w: int = 400

    @property
    def eco_phase_count(self) -> int:
        """Leiter im Eco-Modus (go-e psm: 1 = einphasig, 2 = dreiphasig)."""
        return 3 if self.eco_phases == 2 else 1

    def __post_init__(self) -> None:
        if not 0 <= self.battery_buffer_w <= 3000:
            raise ValueError(f"wallbox.battery_buffer_w must be between 0 and 3000 (got {self.battery_buffer_w})")
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
    # Standort (Breite, Länge) für Sonnenzeiten; None = nicht gesetzt
    location: Optional[tuple[float, float]] = None
    zendure: Optional[ZendureConfig] = None
    cascade: CascadeConfig = field(default_factory=CascadeConfig)
    whirlpool_meter: Optional[WhirlpoolMeterConfig] = None
    waterheater_meter: Optional[WaterheaterMeterConfig] = None
    consumers: Optional[ConsumersConfig] = None
    dishwasher: Optional[DishwasherConfig] = None

    @property
    def dishwasher_active(self) -> bool:
        return self.dishwasher is not None and self.dishwasher.enabled

    @property
    def consumers_active(self) -> bool:
        return self.consumers is not None and self.consumers.enabled and bool(self.consumers.devices)

    @property
    def whirlpool_active(self) -> bool:
        return self.whirlpool_meter is not None and self.whirlpool_meter.enabled

    @property
    def waterheater_active(self) -> bool:
        return self.waterheater_meter is not None and self.waterheater_meter.enabled

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
    def solax2_connect_timeout(self) -> float:
        """Kurzes Limit für den Verbindungsaufbau zum Pergola-WR (ohne Sonne offline)."""
        return SOLAX2_CONNECT_TIMEOUT_S

    @property
    def solax2_active(self) -> bool:
        return self.solax2 is not None and self.solax2.enabled and bool(self.solax2.url)


def parse_whirlpool_meter(raw: dict) -> Optional[WhirlpoolMeterConfig]:
    block = raw.get("whirlpool_meter")
    if not block:
        return None
    return WhirlpoolMeterConfig(**{k: v for k, v in block.items()
                                   if k in WhirlpoolMeterConfig.__dataclass_fields__})


def parse_waterheater_meter(raw: dict) -> Optional[WaterheaterMeterConfig]:
    block = raw.get("waterheater_meter")
    if not block:
        return None
    return WaterheaterMeterConfig(**{k: v for k, v in block.items()
                                     if k in WaterheaterMeterConfig.__dataclass_fields__})


def parse_consumers(raw: dict) -> Optional[ConsumersConfig]:
    block = raw.get("consumers")
    if not block:
        return None
    oh = raw.get("openhab") or {}
    return ConsumersConfig(
        **{k: v for k, v in block.items() if k in ("enabled", "small_avg_w", "devices")},
        openhab_url=str(oh.get("url", "")), openhab_token=str(oh.get("token", "")),
        openhab_timeout_s=float(oh.get("timeout_s", 3)),
    )


def parse_dishwasher(raw: dict) -> Optional[DishwasherConfig]:
    block = raw.get("dishwasher")
    if not block:
        return None
    oh = raw.get("openhab") or {}
    keep = set(DishwasherConfig.__dataclass_fields__) - {"openhab_url", "openhab_token", "openhab_timeout_s"}
    return DishwasherConfig(
        **{k: v for k, v in block.items() if k in keep},
        openhab_url=str(oh.get("url", "")), openhab_token=str(oh.get("token", "")),
        openhab_timeout_s=float(oh.get("timeout_s", 3)),
    )


def parse_zendure(raw: dict) -> Optional[ZendureConfig]:
    block = raw.get("zendure")
    if not block:
        return None
    return ZendureConfig(**{k: v for k, v in block.items() if k in ZendureConfig.__dataclass_fields__})


SOLAX2_TIMEOUT_S = 3.0      # Default für den Pergola-WR (solax2.timeout_s)
SOLAX2_CONNECT_TIMEOUT_S = 1.0   # Verbindungsaufbau Pergola-WR: offline (ohne Sonne) soll schnell auffallen


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


def _location(raw: dict) -> Optional[tuple[float, float]]:
    import suntime
    return suntime.location(raw)


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
        location=_location(raw),
        zendure=parse_zendure(raw),
        whirlpool_meter=parse_whirlpool_meter(raw),
        waterheater_meter=parse_waterheater_meter(raw),
        consumers=parse_consumers(raw),
        dishwasher=parse_dishwasher(raw),
        cascade=CascadeConfig(**{
            k: v for k, v in raw.get("cascade", {}).items() if k in CascadeConfig.__dataclass_fields__
        }),
    )
