"""Heizstab-PV-Überschuss-Controller (Phase 1) + Wallbox (Phase 2).

Ein einzelner run() liest alle Werte, entscheidet pro Phase AN/AUS/UNCHANGED
und führt die Schaltbefehle aus, sofern der Controller aktiv und kein
Dry-Run ist.

Modes (siehe ControllerMode):
- normal:    altes Verhalten, schaltet wenn enabled=true und dry_run=false
- dry_run:   wie normal, erzwingt aber dry_run=true
- read_only: liest und entscheidet, schaltet niemals (für Web-Status)

Temperaturlogik (vereinfacht):
- storage_max_temp           -> harte Obergrenze, alles AUS
- heat_resume_temp           -> = storage_max_temp - storage_temp_hysteresis
- Hysteresebereich           -> dazwischen, keine NEUEN Phasen einschalten,
                                aber AUSschalten bei zu wenig Überschuss erlaubt
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from clients.goe_client import GoeClient, GoeError
from clients.solax_client import SolaxClient
from clients.shelly_client import (
    Shelly3EMClient,
    ShellyPlugClient,
    ShellyStorageTempClient,
)
from config import Config
from db import get_cascade_permission, log_phase_change
from models import (
    ControllerResult,
    PhaseAction,
    PhaseDecision,
    Readings,
    TempStatus,
    WallboxAction,
    WallboxDecision,
    WallboxStatus,
)

log = logging.getLogger(__name__)

# Modul-level Zähler für aufeinanderfolgende Lesefehler (prozesslokal).
# Fail-safe wird erst nach _FAIL_SAFE_THRESHOLD konsekutiven Fehlern aktiviert,
# um sporadische Timeouts des Pocket-WiFi-Loggers zu tolerieren.
_consecutive_errors: int = 0
_FAIL_SAFE_THRESHOLD: int = 2


class ControllerMode(str, Enum):
    NORMAL = "normal"
    DRY_RUN = "dry_run"
    READ_ONLY = "read_only"


@dataclass
class _PhaseInput:
    name: str
    plug: ShellyPlugClient
    current_state: Optional[bool]
    surplus_threshold_w: float  # negativ; z. B. -1500
    min_pv_power_w: float


class Controller:
    def __init__(self, config: Config):
        self.cfg = config
        self._read_only = False
        self.solax = SolaxClient(
            config.solax.url, config.solax.pwd, config.runtime.request_timeout_seconds
        )
        timeout = config.runtime.request_timeout_seconds
        self.ph1 = ShellyPlugClient(config.shelly.ph1_url, timeout, name="PH1")
        self.ph2 = ShellyPlugClient(config.shelly.ph2_url, timeout, name="PH2")
        self.ph3 = ShellyPlugClient(config.shelly.ph3_url, timeout, name="PH3")
        self.storage = ShellyStorageTempClient(
            config.shelly.storage_url, timeout, name="storage"
        )
        self.main_meter = Shelly3EMClient(
            config.shelly.main_meter_url, timeout, name="main_meter"
        )
        self.heater_meter = Shelly3EMClient(
            config.shelly.heater_meter_url, timeout, name="heater_meter"
        )
        self.goe: Optional[GoeClient] = None
        if config.wallbox.enabled and config.wallbox.url:
            self.goe = GoeClient(
                config.wallbox.url, config.wallbox.request_timeout_seconds
            )

    # ---- Lesen ---------------------------------------------------------

    def _read_simulated(self) -> Readings:
        s = self.cfg.simulation
        return Readings(
            pv_power_w=float(s.pv_power),
            main_meter_power_w=float(s.main_meter_power),
            heater_meter_power_w=float(s.heater_meter_power),
            storage_temp_c=float(s.storage_temp),
            ph1_on=bool(s.ph1_on),
            ph2_on=bool(s.ph2_on),
            ph3_on=bool(s.ph3_on),
        )

    def _read_all(self) -> Readings:
        if self.cfg.simulation.enabled:
            return self._read_simulated()

        r = Readings()

        r.pv_power_w = self.solax.get_pv_power_w()
        if r.pv_power_w is None:
            r.errors.append("pv_power")

        r.main_meter_power_w = self.main_meter.get_total_power_w()
        if r.main_meter_power_w is None:
            r.errors.append("main_meter")

        r.heater_meter_power_w = self.heater_meter.get_total_power_w()
        if r.heater_meter_power_w is None:
            r.errors.append("heater_meter")

        r.storage_temp_c = self.storage.get_temperature_c()
        if r.storage_temp_c is None:
            r.errors.append("storage_temp")

        r.ph1_on = self.ph1.get_relay_state()
        if r.ph1_on is None:
            r.errors.append("ph1_state")
        r.ph2_on = self.ph2.get_relay_state()
        if r.ph2_on is None:
            r.errors.append("ph2_state")
        r.ph3_on = self.ph3.get_relay_state()
        if r.ph3_on is None:
            log.warning("ph3_state Lesefehler – Phase 3 bleibt unverändert, System läuft weiter.")

        return r

    # ---- Temperatur ----------------------------------------------------

    def _classify_temp(self, temp: float) -> TempStatus:
        h = self.cfg.heater
        if temp >= h.storage_max_temp:
            return TempStatus.AT_OR_ABOVE_MAX
        if temp <= h.heat_resume_temp:
            return TempStatus.BELOW_RESUME
        return TempStatus.HYSTERESIS_BAND

    # ---- Entscheidung --------------------------------------------------

    def _decide_phase(
        self,
        phase: _PhaseInput,
        readings: Readings,
        temp_status: TempStatus,
    ) -> PhaseDecision:
        """Entscheide AN/AUS/UNCHANGED für eine Phase.

        Reihenfolge:
        - Unvollständige Werte           -> UNCHANGED (Fail-safe)
        - storage_temp >= storage_max_temp
                                          -> AUS
        - storage_temp <= heat_resume_temp
                                          -> normale PV-Überschusslogik
        - sonst (Hysteresebereich)        -> nur AUSschalten erlaubt,
                                             keine neuen Einschaltungen

        PV-Überschusslogik:
          surplus = main_meter - heater_meter   (negativ = Einspeisung)
          AN, wenn surplus <= -min_pv_power UND pv_power >= min_pv_power
          AUS, wenn surplus > -min_pv_power
        """
        cur = phase.current_state
        temp = readings.storage_temp_c
        pv = readings.pv_power_w
        surplus = readings.surplus_without_heater_w
        h = self.cfg.heater

        def reason(tail: str) -> str:
            head = (
                f"needs surplus <= {phase.surplus_threshold_w:.0f} W "
                f"and pv_power >= {phase.min_pv_power_w:.0f} W"
            )
            actual = f"actual surplus={surplus:.0f} W, pv_power={pv:.0f} W"
            return f"{head}; {actual}; {tail}."

        if temp is None or pv is None or surplus is None or cur is None:
            return PhaseDecision(
                phase.name,
                cur,
                PhaseAction.UNCHANGED,
                "incomplete readings – fail-safe, no change.",
            )

        if temp_status is TempStatus.AT_OR_ABOVE_MAX:
            tail = (
                f"storage_temp={temp:.1f} °C >= storage_max_temp="
                f"{h.storage_max_temp:.1f} °C; "
                + ("switching OFF" if cur else "already OFF")
            )
            return PhaseDecision(
                phase.name,
                cur,
                PhaseAction.TURN_OFF if cur else PhaseAction.UNCHANGED,
                reason(tail),
            )

        want_off = surplus > phase.surplus_threshold_w
        want_on = (
            surplus <= phase.surplus_threshold_w and pv >= phase.min_pv_power_w
        )

        if temp_status is TempStatus.HYSTERESIS_BAND:
            # Im Hysteresebereich: nie eine neue Phase einschalten,
            # aber laufende Phasen dürfen bei zu wenig Überschuss aus.
            if want_off and cur:
                tail = (
                    f"hysteresis band ({h.heat_resume_temp:.1f} °C < "
                    f"storage_temp={temp:.1f} °C < {h.storage_max_temp:.1f} °C); "
                    f"surplus too small; switching OFF"
                )
                return PhaseDecision(
                    phase.name, cur, PhaseAction.TURN_OFF, reason(tail)
                )
            if not cur:
                tail = (
                    f"hysteresis band ({h.heat_resume_temp:.1f} °C < "
                    f"storage_temp={temp:.1f} °C < {h.storage_max_temp:.1f} °C); "
                    f"not switching new phase ON"
                )
                return PhaseDecision(
                    phase.name, cur, PhaseAction.UNCHANGED, reason(tail)
                )
            tail = (
                f"hysteresis band ({h.heat_resume_temp:.1f} °C < "
                f"storage_temp={temp:.1f} °C < {h.storage_max_temp:.1f} °C); "
                f"already ON, surplus sufficient"
            )
            return PhaseDecision(phase.name, cur, PhaseAction.UNCHANGED, reason(tail))

        # TempStatus.BELOW_RESUME -> volle PV-Überschusslogik
        if want_off:
            tail = "surplus too small; " + ("switching OFF" if cur else "already OFF")
            return PhaseDecision(
                phase.name,
                cur,
                PhaseAction.TURN_OFF if cur else PhaseAction.UNCHANGED,
                reason(tail),
            )

        if want_on:
            if not cur:
                return PhaseDecision(
                    phase.name,
                    cur,
                    PhaseAction.TURN_ON,
                    reason("conditions met; switching ON"),
                )
            return PhaseDecision(
                phase.name, cur, PhaseAction.UNCHANGED, reason("conditions met; already ON")
            )

        return PhaseDecision(
            phase.name,
            cur,
            PhaseAction.UNCHANGED,
            reason(
                f"pv_power below min ({pv:.0f} W < {phase.min_pv_power_w:.0f} W); "
                + ("staying ON" if cur else "staying OFF")
            ),
        )

    def _check_summer_mode(
        self,
        readings: Readings,
        temp_status: Optional[TempStatus],
    ) -> tuple[bool, bool, str]:
        """Prüft ob Sommermodus Netz-Heizung starten, fortführen oder stoppen soll.

        Gibt (heating_needed, stop_needed, reason) zurück.
        """
        sm = self.cfg.summer_mode
        if not sm.enabled:
            return False, False, ""

        temp = readings.storage_temp_c
        if temp is None:
            return False, False, ""

        # Sicherheits-Obergrenze hat absoluten Vorrang
        if temp_status is TempStatus.AT_OR_ABOVE_MAX:
            return False, False, ""

        # Deutlicher PV-Überschuss (Einspeisung > 200 W): PV übernimmt, kein Netzbezug nötig
        main_meter = readings.main_meter_power_w
        significant_pv = main_meter is not None and main_meter < -200

        any_phase_on = any(
            x is True for x in (readings.ph1_on, readings.ph2_on, readings.ph3_on)
        )

        if temp >= sm.target_temp and any_phase_on and not significant_pv:
            reason = (
                f"Sommermodus: Zieltemperatur {sm.target_temp:.0f} °C erreicht "
                f"({temp:.1f} °C) – Heizstab aus"
            )
            log.info("SOMMERMODUS: %s", reason)
            return False, True, reason

        if significant_pv:
            # PV liefert Überschuss; Kaskade/normale Logik übernimmt
            return False, False, ""

        if temp < sm.min_temp:
            reason = (
                f"Sommermodus: Speicher {temp:.1f} °C < Minimum {sm.min_temp:.0f} °C "
                f"– alle 3 Phasen aus Netz"
            )
            log.info("SOMMERMODUS: %s", reason)
            return True, False, reason

        if temp < sm.target_temp and any_phase_on:
            reason = (
                f"Sommermodus: Heize bis {sm.target_temp:.0f} °C "
                f"(aktuell {temp:.1f} °C)"
            )
            log.info("SOMMERMODUS: %s", reason)
            return True, False, reason

        return False, False, ""

    def _execute(self, decision: PhaseDecision, plug: ShellyPlugClient) -> None:
        if decision.action == PhaseAction.UNCHANGED:
            return
        if self._read_only:
            decision.executed = False
            decision.execution_error = None
            log.info(
                "[READ-ONLY] %s: would %s (no switching)",
                decision.name,
                decision.action.value,
            )
            return
        if self.cfg.runtime.dry_run:
            decision.executed = False
            decision.execution_error = None
            log.info(
                "[DRY-RUN] %s: würde %s ausführen", decision.name, decision.action.value
            )
            return
        target = decision.action == PhaseAction.TURN_ON
        ok = plug.set_relay(target)
        decision.executed = ok
        if not ok:
            decision.execution_error = "Schaltbefehl fehlgeschlagen"

    # ---- Wallbox -------------------------------------------------------

    @staticmethod
    def _parse_wallbox_status(raw: dict) -> WallboxStatus:
        nrg = raw.get("nrg")
        power_w: Optional[float] = None
        if isinstance(nrg, list) and len(nrg) >= 12 and isinstance(nrg[11], (int, float)):
            # go-e API v2: nrg[11] = total power in W (sum of L1+L2+L3+N).
            power_w = float(nrg[11])

        def _num(v: object) -> Optional[float]:
            return float(v) if isinstance(v, (int, float)) else None

        # cdi.value is the rbt-timestamp (ms since boot) when the current
        # charging session started; cdi.type=0 means "currently charging".
        # Duration in seconds = (rbt - cdi.value) / 1000.
        charge_duration_s: Optional[float] = None
        cdi = raw.get("cdi")
        rbt = _num(raw.get("rbt"))
        if (
            isinstance(cdi, dict)
            and cdi.get("type") == 0
            and isinstance(cdi.get("value"), (int, float))
            and rbt is not None
        ):
            delta_ms = rbt - float(cdi["value"])
            if delta_ms >= 0:
                charge_duration_s = delta_ms / 1000.0

        lmo = raw.get("lmo")
        # lmo=4 means eco/PV surplus mode; lmo=3 is Standard/Basic mode.
        # fup only reflects whether eco mode is currently allowing a charge, not whether
        # eco mode is selected at all — so lmo is the reliable indicator.
        pv_surplus_active = lmo == 4

        return WallboxStatus(
            pv_surplus_active=pv_surplus_active,
            force_state=raw.get("frc"),
            allowed=raw.get("alw"),
            car_state=raw.get("car"),
            amp=raw.get("amp"),
            power_w=power_w,
            energy_session_wh=_num(raw.get("wh")),
            energy_total_wh=_num(raw.get("eto")),
            charge_duration_s=charge_duration_s,
            logic_mode=int(lmo) if isinstance(lmo, (int, float)) else None,
        )

    def _decide_wallbox(
        self,
        storage_temp_c: Optional[float],
        all_phases_on: Optional[bool],
    ) -> tuple[Optional[WallboxStatus], WallboxDecision]:
        cfg = self.cfg.wallbox

        if not cfg.enabled:
            log.info("Wallbox disabled in config. Skipping wallbox control.")
            return None, WallboxDecision(
                action=WallboxAction.SKIPPED,
                target_force_state=None,
                reason="Wallbox disabled in config.",
            )

        if self.goe is None:
            return None, WallboxDecision(
                action=WallboxAction.ERROR,
                target_force_state=None,
                reason="Wallbox client not initialised. fail_safe=no_change.",
            )

        try:
            raw = self.goe.get_status()
        except GoeError as e:
            log.error("go-e read failed: %s", e)
            return None, WallboxDecision(
                action=WallboxAction.ERROR,
                target_force_state=None,
                reason="Could not read go-e status. fail_safe=no_change.",
            )

        status = self._parse_wallbox_status(raw)
        log.info(
            "Wallbox-Status: lmo=%s(eco=%s) frc=%s alw=%s car=%s amp=%s",
            status.logic_mode,
            str(status.pv_surplus_active).lower(),
            status.force_state,
            str(status.allowed).lower() if status.allowed is not None else "n/a",
            status.car_state,
            status.amp,
        )

        if cfg.only_control_when_pv_surplus_active and not status.pv_surplus_active:
            decision = WallboxDecision(
                action=WallboxAction.SKIPPED,
                target_force_state=None,
                reason=(
                    "go-e PV surplus mode is not active. "
                    "Manual/normal charging will not be touched."
                ),
            )
            if status.car_state == 1:
                decision.target_logic_mode = 4
                decision.target_amp = 7
                decision.reason += " Auto-restore: car unplugged, switching to Eco mode and resetting amp to 7A."
            return status, decision

        if storage_temp_c is None:
            return status, WallboxDecision(
                action=WallboxAction.ERROR,
                target_force_state=None,
                reason="Storage temperature unavailable. fail_safe=no_change.",
            )

        release_above = cfg.release_above_storage_temp

        # Speicher voll: Wallbox freigeben
        if storage_temp_c >= release_above:
            if status.force_state != 0:
                return status, WallboxDecision(
                    action=WallboxAction.RELEASE,
                    target_force_state=0,
                    reason=(
                        f"Speicher voll ({storage_temp_c:.1f} °C >= "
                        f"{release_above:.1f} °C) – Wallbox freigegeben."
                    ),
                )
            return status, WallboxDecision(
                action=WallboxAction.UNCHANGED,
                target_force_state=0,
                reason=(
                    f"Speicher voll ({storage_temp_c:.1f} °C >= "
                    f"{release_above:.1f} °C) – Wallbox bereits freigegeben."
                ),
            )

        # Alle 3 Phasen AN: Überschuss über Heizstab-Volllast geht in die Wallbox
        if all_phases_on is True:
            if status.force_state != 0:
                return status, WallboxDecision(
                    action=WallboxAction.RELEASE,
                    target_force_state=0,
                    reason=(
                        f"Alle 3 Heizstab-Phasen aktiv – Überschuss über "
                        f"Heizstab-Volllast wird in Wallbox geleitet "
                        f"(Speicher {storage_temp_c:.1f} °C)."
                    ),
                )
            return status, WallboxDecision(
                action=WallboxAction.UNCHANGED,
                target_force_state=0,
                reason=(
                    f"Alle 3 Heizstab-Phasen aktiv – Wallbox bereits freigegeben "
                    f"(Speicher {storage_temp_c:.1f} °C)."
                ),
            )

        # Nicht alle Phasen AN (oder Phasen-Status unbekannt): Speicher hat Priorität
        phases_info = (
            "Phasen-Status unbekannt" if all_phases_on is None else "Nicht alle Phasen aktiv"
        )
        if status.force_state != 1:
            return status, WallboxDecision(
                action=WallboxAction.PAUSE,
                target_force_state=1,
                reason=(
                    f"{phases_info} – Speicher priorisiert, Wallbox pausiert "
                    f"(Speicher {storage_temp_c:.1f} °C)."
                ),
            )
        return status, WallboxDecision(
            action=WallboxAction.UNCHANGED,
            target_force_state=1,
            reason=(
                f"{phases_info} – Speicher priorisiert, Wallbox bereits pausiert "
                f"(Speicher {storage_temp_c:.1f} °C)."
            ),
        )

    def _execute_wallbox(self, decision: WallboxDecision) -> None:
        if decision.action in (WallboxAction.PAUSE, WallboxAction.RELEASE):
            target = decision.target_force_state
            if target not in (0, 1):
                decision.execution_error = (
                    f"refusing to set forceState={target} (only 0 or 1 allowed)"
                )
                log.error(decision.execution_error)
            elif self._read_only:
                log.info(
                    "READ-ONLY: would set go-e forceState to %d (no switching).",
                    target,
                )
                decision.executed = False
            elif not self.cfg.runtime.enabled:
                log.info(
                    "Controller disabled (runtime.enabled=false): not setting go-e "
                    "forceState to %d.",
                    target,
                )
                decision.executed = False
            elif self.cfg.runtime.dry_run:
                log.info("DRY-RUN: would set go-e forceState to %d.", target)
                decision.executed = False
            elif self.goe is None:
                decision.execution_error = "wallbox client not initialised"
                log.error(decision.execution_error)
            else:
                try:
                    self.goe.set_force_state(target)
                    decision.executed = True
                    log.info("go-e forceState set to %d.", target)
                except (GoeError, ValueError) as e:
                    decision.execution_error = str(e)
                    log.error("go-e set frc=%d failed: %s", target, e)

    def _execute_amp(self, decision: WallboxDecision) -> None:
        target = decision.target_amp
        if target is None:
            return
        if self._read_only:
            log.info("READ-ONLY: would set go-e amp to %d.", target)
            return
        if not self.cfg.runtime.enabled:
            log.info("Controller disabled: not setting go-e amp to %d.", target)
            return
        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN: would set go-e amp to %d.", target)
            return
        if self.goe is None:
            decision.amp_execution_error = "wallbox client not initialised"
            log.error(decision.amp_execution_error)
            return
        try:
            self.goe.set_amp(target)
            decision.amp_executed = True
            log.info("go-e amp set to %d.", target)
        except GoeError as e:
            decision.amp_execution_error = str(e)
            log.error("go-e set amp=%d failed: %s", target, e)

    def _execute_lmo(self, decision: WallboxDecision) -> None:
        target = decision.target_logic_mode
        if target is None:
            return
        if self._read_only:
            log.info("READ-ONLY: would set go-e lmo to %d.", target)
            return
        if not self.cfg.runtime.enabled:
            log.info("Controller disabled: not setting go-e lmo to %d.", target)
            return
        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN: would set go-e lmo to %d.", target)
            return
        if self.goe is None:
            decision.logic_mode_execution_error = "wallbox client not initialised"
            log.error(decision.logic_mode_execution_error)
            return
        try:
            self.goe.set_logic_mode(target)
            decision.logic_mode_executed = True
            log.info("go-e lmo set to %d.", target)
        except GoeError as e:
            decision.logic_mode_execution_error = str(e)
            log.error("go-e set lmo=%d failed: %s", target, e)

    # ---- Hauptlauf -----------------------------------------------------

    def run(
        self,
        now: Optional[datetime] = None,
        mode: ControllerMode = ControllerMode.NORMAL,
    ) -> ControllerResult:
        now = now or datetime.now()
        log.info("=== PV-Controller Start (%s) ===", now.isoformat(timespec="seconds"))

        if mode is ControllerMode.READ_ONLY:
            self._read_only = True
            log.info("Mode: READ-ONLY (no switching, even if enabled).")
        elif mode is ControllerMode.DRY_RUN:
            self.cfg.runtime.dry_run = True
            log.info("Mode: DRY-RUN (forced via mode).")

        if self.cfg.simulation.enabled:
            log.info(
                "Simulation mode active. No real devices will be read or switched."
            )
            self.cfg.runtime.dry_run = True

        log.info(
            "Runtime: enabled=%s dry_run=%s simulation=%s",
            str(self.cfg.runtime.enabled).lower(),
            str(self.cfg.runtime.dry_run).lower(),
            str(self.cfg.simulation.enabled).lower(),
        )

        h = self.cfg.heater
        log.info(
            "Temperaturlogik: max=%.1f °C, hysteresis=%.1f °C, resume<=%.1f °C",
            h.storage_max_temp,
            h.storage_temp_hysteresis,
            h.heat_resume_temp,
        )

        if not self.cfg.runtime.enabled:
            log.info(
                "Controller disabled by config.runtime.enabled=false. "
                "Observing only. Existing phase states were not changed."
            )
            readings = self._read_all()
            self._log_readings(readings, None)
            result = ControllerResult(
                timestamp=now,
                controller_active=False,
                dry_run=self.cfg.runtime.dry_run,
                temp_status=None,
                storage_max_temp=h.storage_max_temp,
                heat_resume_temp=h.heat_resume_temp,
                readings=readings,
                summary="Controller disabled (observation only)",
            )
            log.info("=== PV-Controller Ende ===")
            return result

        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN aktiv – es werden keine Schaltbefehle gesendet.")

        readings = self._read_all()

        temp_status: Optional[TempStatus] = None
        if readings.storage_temp_c is not None:
            temp_status = self._classify_temp(readings.storage_temp_c)

        self._log_readings(readings, temp_status)

        global _consecutive_errors

        sm_heating = False
        sm_stop = False
        sm_reason = ""

        decisions: list[PhaseDecision] = []
        if readings.errors or temp_status is None:
            _consecutive_errors += 1
            error_names = ", ".join(readings.errors) if readings.errors else "storage_temp"
            if _consecutive_errors < _FAIL_SAFE_THRESHOLD:
                log.warning(
                    "Lesefehler bei: %s (Fehler %d/%d) – warte auf weiteren Fehler "
                    "vor Fail-safe-Aktivierung.",
                    error_names, _consecutive_errors, _FAIL_SAFE_THRESHOLD,
                )
            else:
                log.warning(
                    "Lesefehler bei: %s (%d konsekutiv) – Fail-safe, keine Schaltbefehle.",
                    error_names, _consecutive_errors,
                )
            for name, cur in (
                ("PH1", readings.ph1_on),
                ("PH2", readings.ph2_on),
                ("PH3", readings.ph3_on),
            ):
                reason = (
                    f"Fail-safe wegen Lesefehlern ({_consecutive_errors}x)"
                    if _consecutive_errors >= _FAIL_SAFE_THRESHOLD
                    else f"Lesefehler ({_consecutive_errors}/{_FAIL_SAFE_THRESHOLD}) – noch kein Fail-safe"
                )
                decisions.append(
                    PhaseDecision(
                        name,
                        cur,
                        PhaseAction.UNCHANGED,
                        reason,
                    )
                )
        else:
            _consecutive_errors = 0  # Erfolgreicher Read → Zähler zurücksetzen
            if temp_status is TempStatus.HYSTERESIS_BAND:
                log.info(
                    "Storage temperature within hysteresis band. "
                    "Avoiding new phase activation."
                )
            elif temp_status is TempStatus.AT_OR_ABOVE_MAX:
                log.info("Storage temperature >= max temperature. All phases off.")

            phases = [
                _PhaseInput(
                    name="PH1",
                    plug=self.ph1,
                    current_state=readings.ph1_on,
                    surplus_threshold_w=-float(h.min_pv_power_ph1),
                    min_pv_power_w=float(h.min_pv_power_ph1),
                ),
                _PhaseInput(
                    name="PH2",
                    plug=self.ph2,
                    current_state=readings.ph2_on,
                    surplus_threshold_w=-float(h.min_pv_power_ph2),
                    min_pv_power_w=float(h.min_pv_power_ph2),
                ),
                _PhaseInput(
                    name="PH3",
                    plug=self.ph3,
                    current_state=readings.ph3_on,
                    surplus_threshold_w=-float(h.min_pv_power_ph3),
                    min_pv_power_w=float(h.min_pv_power_ph3),
                ),
            ]
            cascade_heizstab = get_cascade_permission("heizstab")

            # Sommermodus: Prüfung VOR den Phasen-Entscheidungen
            sm_heating, sm_stop, sm_reason = self._check_summer_mode(readings, temp_status)

            for i, p in enumerate(phases):
                in_summer_heating = sm_heating
                in_summer_stop = sm_stop

                if in_summer_stop and readings.main_meter_power_w is not None \
                        and readings.main_meter_power_w >= -200:
                    # Zieltemperatur erreicht, kein PV-Überschuss → Netz-Heizung beenden
                    action = PhaseAction.TURN_OFF if p.current_state else PhaseAction.UNCHANGED
                    d = PhaseDecision(p.name, p.current_state, action, sm_reason)
                elif in_summer_heating:
                    # Sommermodus: Phase aus Netz einschalten (überstimmt Kaskade-Sperre)
                    action = PhaseAction.UNCHANGED if p.current_state else PhaseAction.TURN_ON
                    d = PhaseDecision(p.name, p.current_state, action, sm_reason)
                else:
                    d = self._decide_phase(p, readings, temp_status)
                    if cascade_heizstab is False:
                        # Kaskade hat Heizstab abgeschaltet:
                        # - laufende Phase aktiv ausschalten
                        # - Einschalten blockieren
                        if d.action is PhaseAction.TURN_ON:
                            d.action = PhaseAction.UNCHANGED
                            d.reason += " [Kaskade: Einschalten blockiert]"
                        elif d.action is PhaseAction.UNCHANGED and p.current_state:
                            d.action = PhaseAction.TURN_OFF
                            d.reason += " [Kaskade: Ausschalten erzwungen]"

                self._execute(d, p.plug)
                decisions.append(d)
                if d.executed and d.action in (PhaseAction.TURN_ON, PhaseAction.TURN_OFF):
                    ph_num = int(p.name[2])
                    state = "on" if d.action == PhaseAction.TURN_ON else "off"
                    try:
                        log_phase_change(ph_num, state)
                    except Exception:
                        log.warning("Phase-Log konnte nicht geschrieben werden", exc_info=True)

        for d in decisions:
            state_str = (
                "ON" if d.current_state else "OFF" if d.current_state is False else "?"
            )
            log.info("Decision %s: state=%s -> %s", d.name, state_str, d.action.value)
            err = "" if not d.execution_error else f" [ERR: {d.execution_error}]"
            log.info("Reason: %s%s", d.reason, err)

        wallbox_status, wallbox_decision = self._handle_wallbox(readings)

        # Sommermodus-Status für das Result ermitteln (aus den ausgeführten Decisions)
        _sm_heating_result = False
        _sm_reason_result = None
        if self.cfg.summer_mode.enabled and sm_heating:
            _sm_heating_result = True
            _sm_reason_result = sm_reason

        result = ControllerResult(
            timestamp=now,
            controller_active=True,
            dry_run=self.cfg.runtime.dry_run,
            temp_status=temp_status,
            storage_max_temp=h.storage_max_temp,
            heat_resume_temp=h.heat_resume_temp,
            readings=readings,
            decisions=decisions,
            wallbox_status=wallbox_status,
            wallbox_decision=wallbox_decision,
            summary="ok",
            fail_safe_active=bool(readings.errors or temp_status is None)
                and _consecutive_errors >= _FAIL_SAFE_THRESHOLD,
            summer_mode_heating=_sm_heating_result,
            summer_mode_reason=_sm_reason_result,
        )
        log.info("=== PV-Controller Ende ===")
        return result

    def _handle_wallbox(
        self, readings: Readings
    ) -> tuple[Optional[WallboxStatus], WallboxDecision]:
        log.info(
            "Wallbox: enabled=%s url=%s",
            str(self.cfg.wallbox.enabled).lower(),
            self.cfg.wallbox.url or "n/a",
        )
        # Alle 3 Phasen AN → Speicher läuft auf Volllast, Überschuss darüber in Wallbox
        ph = (readings.ph1_on, readings.ph2_on, readings.ph3_on)
        all_phases_on: Optional[bool] = (
            True if all(p is True for p in ph)
            else None if any(p is None for p in ph)
            else False
        )
        log.info(
            "Heizstab-Phasen: PH1=%s PH2=%s PH3=%s → all_phases_on=%s",
            "AN" if readings.ph1_on else "AUS" if readings.ph1_on is False else "?",
            "AN" if readings.ph2_on else "AUS" if readings.ph2_on is False else "?",
            "AN" if readings.ph3_on else "AUS" if readings.ph3_on is False else "?",
            all_phases_on,
        )
        status, decision = self._decide_wallbox(readings.storage_temp_c, all_phases_on)

        # Kaskaden-Gate: Kaskade kann nur blockieren, nicht das Temp-Gate überstimmen.
        # Das Temp-Gate (pause_below_storage_temp) ist autoritativ für "Speicher priorisiert".
        # Wenn Kaskade False → Wallbox pausieren (zu wenig Überschuss).
        # Wenn Kaskade True  → keine Aktion: Temp-Gate bleibt entscheidend.
        cascade_wallbox = get_cascade_permission("wallbox")
        if cascade_wallbox is False:
            if decision.action is WallboxAction.RELEASE:
                decision.action = WallboxAction.UNCHANGED
                decision.target_force_state = None
                decision.reason = "[Kaskade blockiert] " + decision.reason
            elif decision.action is WallboxAction.UNCHANGED and (
                status is not None and status.force_state == 0
            ):
                decision.action = WallboxAction.PAUSE
                decision.target_force_state = 1
                decision.reason = "[Kaskade pausiert] " + decision.reason

        self._execute_wallbox(decision)
        self._execute_lmo(decision)
        self._execute_amp(decision)
        log.info(
            "Wallbox Decision: action=%s target_force_state=%s target_lmo=%s",
            decision.action.value,
            decision.target_force_state,
            decision.target_logic_mode,
        )
        err = "" if not decision.execution_error else f" [ERR: {decision.execution_error}]"
        lmo_err = "" if not decision.logic_mode_execution_error else f" [LMO-ERR: {decision.logic_mode_execution_error}]"
        log.info("Reason: %s%s%s", decision.reason, err, lmo_err)
        return status, decision

    # ---- Logging-Helfer ------------------------------------------------

    def _log_readings(
        self, r: Readings, temp_status: Optional[TempStatus]
    ) -> None:
        def fmt(v, unit=""):
            return f"{v:.1f}{unit}" if isinstance(v, (int, float)) else "n/a"

        log.info("PV-Leistung:        %s", fmt(r.pv_power_w, " W"))
        log.info("Hauptzähler:        %s", fmt(r.main_meter_power_w, " W"))
        log.info("Heizstab 3EM:       %s", fmt(r.heater_meter_power_w, " W"))
        log.info(
            "Überschuss o. Heizstab: %s",
            fmt(r.surplus_without_heater_w, " W"),
        )
        log.info("Speicher-Temp:      %s", fmt(r.storage_temp_c, " °C"))
        log.info(
            "Temperaturstatus:   %s",
            temp_status.value if temp_status is not None else "n/a",
        )
        log.info(
            "Phasen-Status:      PH1=%s PH2=%s PH3=%s",
            "ON" if r.ph1_on else "OFF" if r.ph1_on is False else "?",
            "ON" if r.ph2_on else "OFF" if r.ph2_on is False else "?",
            "ON" if r.ph3_on else "OFF" if r.ph3_on is False else "?",
        )


def run_controller(
    config: Config, mode: ControllerMode = ControllerMode.NORMAL
) -> ControllerResult:
    """Convenience wrapper: build a Controller and execute one run in the
    requested mode. The web layer should always pass READ_ONLY for status
    snapshots so that the UI cannot accidentally switch hardware."""
    return Controller(config).run(mode=mode)
