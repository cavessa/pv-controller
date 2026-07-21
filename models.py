"""Datenstrukturen für Sensorwerte und Controller-Entscheidungen.

Diese Strukturen werden bewusst getrennt vom Controller gehalten,
damit Phase 2 (Weboberfläche, Wallbox) sie weiterverwenden kann.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PhaseAction(str, Enum):
    TURN_ON = "TURN_ON"
    TURN_OFF = "TURN_OFF"
    UNCHANGED = "UNCHANGED"


class TempStatus(str, Enum):
    BELOW_RESUME = "BELOW_RESUME"
    HYSTERESIS_BAND = "HYSTERESIS_BAND"
    AT_OR_ABOVE_MAX = "AT_OR_ABOVE_MAX"


class WallboxAction(str, Enum):
    PAUSE = "pause"
    RELEASE = "release"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class Readings:
    """Alle Messwerte eines Durchlaufs. Felder sind optional, damit Teilfehler
    nicht den ganzen Snapshot ungültig machen."""

    pv_power_w: Optional[float] = None
    main_meter_power_w: Optional[float] = None
    heater_meter_power_w: Optional[float] = None
    storage_temp_c: Optional[float] = None
    ph1_on: Optional[bool] = None
    ph2_on: Optional[bool] = None
    ph3_on: Optional[bool] = None
    wallbox_power_w: Optional[float] = None
    errors: list[str] = field(default_factory=list)

    @property
    def surplus_without_heater_w(self) -> Optional[float]:
        if self.main_meter_power_w is None or self.heater_meter_power_w is None:
            return None
        return self.main_meter_power_w - self.heater_meter_power_w

    @property
    def true_surplus_w(self) -> Optional[float]:
        """PV-Überschuss unabhängig von Heizstab UND Wallbox.

        Beide sind steuerbare Lasten, die vom Controller selbst ein-/ausgeschaltet
        werden. Würde man die Wallbox-Leistung nicht herausrechnen, sieht die
        Heizstab-Logik (inkl. Sommermodus) den eigenen Wallbox-Ladestrom als
        "kein PV-Überschuss mehr" an, schaltet den Heizstab ab, was die Wallbox
        pausiert, wodurch der Überschuss wieder auftaucht und der Heizstab erneut
        einschaltet – ein Endloskreis (Wallbox-Leistung unbekannt → wie 0 W).
        """
        if self.main_meter_power_w is None or self.heater_meter_power_w is None:
            return None
        return (
            self.main_meter_power_w
            - self.heater_meter_power_w
            - (self.wallbox_power_w or 0.0)
        )

    def is_complete(self) -> bool:
        return all(
            v is not None
            for v in (
                self.pv_power_w,
                self.main_meter_power_w,
                self.heater_meter_power_w,
                self.storage_temp_c,
                self.ph1_on,
                self.ph2_on,
                self.ph3_on,
            )
        )


@dataclass
class PhaseDecision:
    name: str
    current_state: Optional[bool]
    action: PhaseAction
    reason: str
    executed: bool = False
    execution_error: Optional[str] = None


@dataclass
class WallboxStatus:
    pv_surplus_active: bool
    force_state: Optional[int] = None
    allowed: Optional[bool] = None
    car_state: Optional[int] = None
    amp: Optional[int] = None
    power_w: Optional[float] = None
    energy_session_wh: Optional[float] = None
    energy_total_wh: Optional[float] = None
    charge_duration_s: Optional[float] = None
    logic_mode: Optional[int] = None  # go-e lmo: 3=eco/PV, 4=next-trip, other=standard


@dataclass
class WallboxDecision:
    action: WallboxAction
    target_force_state: Optional[int]
    reason: str
    executed: bool = False
    execution_error: Optional[str] = None
    target_logic_mode: Optional[int] = None
    logic_mode_executed: bool = False
    logic_mode_execution_error: Optional[str] = None
    target_amp: Optional[int] = None
    amp_executed: bool = False
    amp_execution_error: Optional[str] = None


@dataclass
class ControllerResult:
    timestamp: datetime
    controller_active: bool
    dry_run: bool
    temp_status: Optional[TempStatus]
    storage_max_temp: Optional[float]
    heat_resume_temp: Optional[float]
    readings: Readings
    decisions: list[PhaseDecision] = field(default_factory=list)
    wallbox_status: Optional[WallboxStatus] = None
    wallbox_decision: Optional[WallboxDecision] = None
    summary: str = ""
    fail_safe_active: bool = False  # True erst nach _FAIL_SAFE_THRESHOLD konsekutiven Fehlern
    summer_mode_heating: bool = False  # Heizstab läuft gerade wegen Sommermodus aus Netz
    summer_mode_reason: Optional[str] = None
