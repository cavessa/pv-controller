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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from clients.goe_client import GoeClient, GoeError
from clients.solax_client import SolaxClient
from clients.zendure_client import ZendureClient
from clients.shelly_client import (
    Shelly3EMClient,
    ShellyPlugClient,
    ShellyStorageTempClient,
)
from config import Config
from db import (
    device_health_clear,
    device_health_update,
    get_cascade_budget,
    get_heater_thermostat,
    get_last_phase_changes,
    get_last_phase_states,
    log_phase_change,
    mark_device_alerted,
    note_blind_off,
    set_heater_thermostat,
    smooth_heater_budgets,
)
from heater_phases import HeaterParams, PhaseIn, decide_phases, thermostat_step
from heater_power import PhaseRead
from heater_power import resolve as resolve_heater_power
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

# Lesen darf kurz warten: ein toter Shelly bremst sonst jeden Lauf (Schalten: runtime-Timeout).
SHELLY_READ_TIMEOUT_S = 1.5
# Ab so vielen Minuten ohne Antwort gilt ein Gerät als „dauerhaft tot" (Banner + einmaliger Log).
DEVICE_ALERT_MIN = 10


def wallbox_reason_code(enabled: bool, status: Optional[WallboxStatus], action: WallboxAction) -> tuple[str, dict]:
    """Grund-Code zur Wallbox (reasons.py) – nur Protokoll, die Entscheidung fällt in ``_decide_wallbox``.

    go-e ``car``: 1 = kein Auto, 2 = lädt, 3 = wartet aufs Auto, 4 = Laden beendet.
    """
    if not enabled:
        return "wallbox_disabled", {}
    if status is None:
        return "device_unreachable", {}
    data = {"car_state": status.car_state, "eco_mode": bool(status.pv_surplus_active), "amp": status.amp,
            "phases": {1: 1, 2: 3}.get(status.psm), "power_w": None if status.power_w is None else int(status.power_w)}
    if status.car_state == 1:
        return "not_connected", data
    charging = status.car_state == 2 and (status.power_w or 0) > 100
    if not status.pv_surplus_active:
        return ("charging_manual" if charging else "wallbox_not_eco_mode"), data
    if charging:
        return "charging", data
    if status.car_state == 4:
        return "car_full", data
    if action is WallboxAction.RELEASE:
        return "wallbox_released", data
    return "waiting_for_car", data


class ControllerMode(str, Enum):
    NORMAL = "normal"
    DRY_RUN = "dry_run"
    READ_ONLY = "read_only"


class Controller:
    def __init__(self, config: Config):
        self.cfg = config
        self._read_only = False
        self.solax = SolaxClient(
            config.solax.url, config.solax.pwd, config.runtime.request_timeout_seconds
        )
        self.solax2: Optional[SolaxClient] = None
        if config.solax2_active:
            self.solax2 = SolaxClient(config.solax2.url, config.solax2.pwd, config.solax2_timeout)
        self.zendure: Optional[ZendureClient] = None
        if config.zendure_active:
            self.zendure = ZendureClient(config.zendure.ip, config.zendure.sn)
        timeout = config.runtime.request_timeout_seconds
        self.ph1 = ShellyPlugClient(
            config.shelly.ph1_url,
            timeout,
            name="PH1",
            type_=config.shelly.ph1_type,
            channel=config.shelly.ph1_channel,
            read_timeout=SHELLY_READ_TIMEOUT_S,
        )
        self.ph2 = ShellyPlugClient(
            config.shelly.ph2_url,
            timeout,
            name="PH2",
            type_=config.shelly.ph2_type,
            channel=config.shelly.ph2_channel,
            read_timeout=SHELLY_READ_TIMEOUT_S,
        )
        self.ph3 = ShellyPlugClient(
            config.shelly.ph3_url,
            timeout,
            name="PH3",
            type_=config.shelly.ph3_type,
            channel=config.shelly.ph3_channel,
            read_timeout=SHELLY_READ_TIMEOUT_S,
        )
        self.storage = ShellyStorageTempClient(
            config.shelly.storage_url, SHELLY_READ_TIMEOUT_S, name="storage"
        )
        self.main_meter = Shelly3EMClient(
            config.shelly.main_meter_url, SHELLY_READ_TIMEOUT_S, name="main_meter",
            type_=config.shelly.main_meter_type
        )
        self.heater_meter = Shelly3EMClient(
            config.shelly.heater_meter_url, SHELLY_READ_TIMEOUT_S, name="heater_meter"
        )
        # Whirlpool-3EM: nur lesen (Anzeige, Nachtbedarf), kein Fail-safe, nichts schalten
        self.whirlpool_meter: Optional[Shelly3EMClient] = None
        if config.whirlpool_active:
            wm = config.whirlpool_meter
            self.whirlpool_meter = Shelly3EMClient(wm.url, SHELLY_READ_TIMEOUT_S, name="whirlpool_meter",
                                                   type_=wm.type)
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

    def _read_pergola(self, r: Readings) -> None:
        """Pergola-PV (Solax 2) lesen und auf die Gesamt-PV-Leistung addieren.

        Nicht kritisch: Lesefehler landen nur im Log, nicht in r.errors
        (kein Fail-safe). Fällt der Haupt-WR aus, bleibt pv_power_w None.
        """
        if self.solax2 is None:
            return
        try:
            d = self.solax2.get_realtime_data()
        except Exception:
            log.warning("Pergola-WR: Lesefehler", exc_info=True)
            return
        if d is None:
            log.warning("Pergola-WR nicht erreichbar (n/a)")
            return
        r.solax_pergola = d
        r.pv2_power_w = d.pv_ac_w
        r.pv2_today_kwh = d.yield_today_kwh
        if r.pv_power_w is not None:
            r.pv_power_w += d.pv_ac_w

    def _read_zendure(self, r: Readings) -> None:
        """Batterie lesen (nicht kritisch: Fehler -> None, kein Fail-safe)."""
        if self.zendure is None:
            return
        try:
            st = self.zendure.get_status()
            if st is None:
                return
            r.zendure = st
            from db import record_zendure
            record_zendure(st.soc, st.charge_w, st.discharge_w)
        except Exception:
            log.warning("Zendure-Lesen fehlgeschlagen", exc_info=True)

    def _read_inverters(self, r: Readings) -> None:
        """Haupt-WR (+ Pergola, Tageszähler). Ein Thread: das Pocket-WiFi mag nur eine Verbindung."""
        # PV-Leistung = AC-Ausgang (ein Read; Strings DC nur als Detail in solax_main)
        try:
            sd = self.solax.get_realtime_data()
        except Exception:
            log.warning("Solax: Lesewerte ungültig", exc_info=True)
            sd = None
        if sd is not None:
            r.solax_main = sd
            r.pv1_power_w = sd.pv_ac_w
            r.solax_yield_today_kwh = sd.yield_today_kwh
            r.solax_grid_out_kwh = sd.grid_out_today_kwh
            r.solax_grid_in_kwh = sd.grid_in_today_kwh
        r.pv_power_w = r.pv1_power_w
        self._read_pergola(r)

    def _read_main_meter(self, r: Readings) -> None:
        r.main_meter_power_w = self.main_meter.get_total_power_w()
        if r.main_meter_power_w is None:
            return
        try:
            from db import record_main_meter
            record_main_meter(r.main_meter_power_w)
        except Exception:
            log.warning("Hauptzähler-Leistung nicht gespeichert", exc_info=True)
        # Tagesenergie ist nicht kritisch: Fehler -> Solax-Fallback, kein Fail-safe
        try:
            energy = self.main_meter.get_energy_wh()
            if energy is not None:
                r.main_import_wh, r.main_export_wh = energy
                from db import track_meter
                # Gerät = Typ + Adresse: ein Tausch (oder neue IP) wird erkannt und der Tagesstand übertragen
                track_meter(*energy, meter_id=f"{self.cfg.shelly.main_meter_type}|{self.cfg.shelly.main_meter_url}")
        except Exception:
            log.warning("Hauptzähler-Energie/Snapshot fehlgeschlagen", exc_info=True)

    def _track_health(self, key: str, label: str, ok: bool) -> None:
        """Dauerhaft tote Geräte: einmal loggen (nach DEVICE_ALERT_MIN) und beim Wiederkommen."""
        try:
            h = device_health_update(key, ok)
            if ok:
                if h["recovered"]:
                    log.info("%s wieder erreichbar.", label)
            elif h["down_min"] >= DEVICE_ALERT_MIN and not h["alerted"]:
                log.error("%s seit %s Uhr nicht erreichbar.", label, h["since"])
                mark_device_alerted(key)
        except Exception:
            log.debug("Geräte-Status nicht fortgeschrieben", exc_info=True)

    def _apply_whirlpool(self, r: Readings, whirl_phases: Optional[list[float]]) -> None:
        """Whirlpool-Leistung (minus mitlaufende Heizstab-Phase); Heizstab-3EM nicht lesbar → unsicher.
        Die Heizstab-Phasen kommen aus demselben /status-Abruf wie heater_meter_power_w (Cache)."""
        import whirlpool
        sub = self.cfg.whirlpool_meter.subtract_heater_phase
        heater_phases = None
        if sub and r.heater_meter_power_w is not None:       # 3EM offline → nicht erneut anfragen
            heater_phases = self.heater_meter.get_phase_powers_w()
        w = whirlpool.compute(whirl_phases, heater_phases, sub)
        r.whirlpool_power_w, r.whirlpool_raw_w = w["power_w"], w["raw_w"]
        r.whirlpool_uncertain, r.whirlpool_reachable = w["uncertain"], w["reachable"]
        if w["uncertain"]:
            log.info("Whirlpool: Heizstab-3EM nicht lesbar – Wert unsicher (Rohwert %.0f W inkl. %s)",
                     w["raw_w"], sub)

    def _read_consumers(self) -> Optional[list]:
        """Verbraucher aus openHAB (nur lesen); Fehler nie weiterreichen – die Regelung braucht sie nicht."""
        try:
            import consumers
            return consumers.read(self.cfg.consumers)
        except Exception:
            log.warning("Verbraucher nicht lesbar", exc_info=True)
            return None

    def _read_all(self) -> Readings:
        """Liest alle Geräte parallel (Lauf ≈ langsamstes Gerät, nicht Summe der Timeouts)."""
        if self.cfg.simulation.enabled:
            return self._read_simulated()

        r = Readings()
        with ThreadPoolExecutor(max_workers=9, thread_name_prefix="read") as ex:
            f_inv = ex.submit(self._read_inverters, r)
            f_zd = ex.submit(self._read_zendure, r)
            f_main = ex.submit(self._read_main_meter, r)
            f_heat = ex.submit(self.heater_meter.get_total_power_w)
            f_temp = ex.submit(self.storage.get_temperature_c)
            f_whirl = ex.submit(self.whirlpool_meter.get_phase_powers_w) if self.whirlpool_meter else None
            f_cons = ex.submit(self._read_consumers) if self.cfg.consumers_active else None
            disabled = self.cfg.heater.disabled_phases
            f_ph = [
                None if disabled[i] else ex.submit(p.get_status)
                for i, p in enumerate((self.ph1, self.ph2, self.ph3))
            ]
            for f in (f_inv, f_zd, f_main):
                f.result()
            r.heater_meter_power_w = f_heat.result()
            r.storage_temp_c = f_temp.result()
            ph_reads = [None if f is None else PhaseRead(*f.result()) for f in f_ph]
            whirl_phases = f_whirl.result() if f_whirl is not None else None
            r.consumers = f_cons.result() if f_cons is not None else None
        ph_states = [None if ph is None else ph.on for ph in ph_reads]
        if self.whirlpool_meter is not None:
            self._apply_whirlpool(r, whirl_phases)

        r.ph1_on, r.ph2_on, r.ph3_on = ph_states

        if r.pv_power_w is None:
            r.errors.append("pv_power")
        if r.main_meter_power_w is None:
            r.errors.append("main_meter")
        if r.heater_meter_power_w is None:
            r.errors.append("heater_meter")
        if r.storage_temp_c is None:
            r.errors.append("storage_temp")
        urls = (self.cfg.shelly.ph1_url, self.cfg.shelly.ph2_url, self.cfg.shelly.ph3_url)
        for i, (state, url) in enumerate(zip(ph_states, urls), start=1):
            if disabled[i - 1]:
                # stillgelegt: kein Fehler, kein Banner, alter Fehlerzustand wird gelöscht
                r.disabled_phases.append(f"ph{i}")
                device_health_clear(f"ph{i}")
                continue
            if state is None:
                r.errors.append(f"ph{i}_state")
                r.unreachable_phases.append(f"ph{i}")
            self._track_health(f"ph{i}", f"Phase {i} ({url.replace('http://', '')})", state is not None)
        self._track_health("storage_temp", "Speichertemperatur-Shelly "
                           f"({self.cfg.shelly.storage_url.replace('http://', '')})", r.storage_temp_c is not None)
        self._track_health("heater_meter", "Heizstab-3EM "
                           f"({self.cfg.shelly.heater_meter_url.replace('http://', '')})",
                           r.heater_meter_power_w is not None)
        self._track_health("main_meter", "Hauptzähler "
                           f"({self.cfg.shelly.main_meter_url.replace('http://', '')})",
                           r.main_meter_power_w is not None)

        # Fallback: heater_meter nicht erreichbar → Summe der Phasen-Shellys (gemessen bzw. Nennleistung,
        # siehe heater_power). Nie stillschweigend 0 W; die Quelle steht in heater_power_source.
        if r.heater_meter_power_w is None:
            hp = resolve_heater_power(None, ph_reads, self.cfg.heater.phase_power_w, get_last_phase_states())
            r.heater_meter_power_w = hp.watts
            r.heater_power_source = hp.source
            log.warning("heater_meter nicht erreichbar – Ersatzwert aus Phasen: %d W (%s)", hp.watts, hp.detail)
            r.errors = [e for e in r.errors if e != "heater_meter"]

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
    # Die Phasenentscheidung (Budget → Phasen, Mindestzeiten, Max-Temp) liegt in
    # heater_phases.decide_phases; hier nur Sommermodus und Ausführung.

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

        # Deutlicher PV-Überschuss: Heizstab UND Wallbox laufen auf PV-Energie, kein
        # Netzbezug nötig. true_surplus_w zeigt den wahren Überschuss unabhängig davon
        # ob Heizstab oder Wallbox gerade laufen (sonst hält sich die Wallbox-Ladeleistung
        # selbst für "kein Überschuss" und schaltet den Heizstab ab -> Wallbox pausiert
        # -> Überschuss taucht wieder auf -> Heizstab an -> Wallbox wieder frei -> Flattern).
        surplus = readings.true_surplus_w
        significant_pv = surplus is not None and surplus < -200

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
                f"Sommermodus: Wasserspeicher {temp:.1f} °C < Minimum {sm.min_temp:.0f} °C "
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
        if decision.blind:
            # AUS an eine Phase mit unbekanntem Zustand: kurzer Timeout, jeden Lauf erneut, Fehler nur
            # beim ersten Fehlschlag einer Serie loggen (kein Fehlertext im Decision-Log jede Minute)
            ok = plug.set_relay(False, timeout=SHELLY_READ_TIMEOUT_S, quiet=True)
            decision.executed = ok
            try:
                phase_no = int(decision.name[2:])
                if note_blind_off(phase_no, ok):
                    log.error(
                        "%s: AUS-Befehl ohne Antwort (Zustand unbekannt, Gerät nicht erreichbar) – "
                        "wird in jedem Lauf erneut gesendet; weitere Fehlschläge nur im Debug-Log.",
                        decision.name,
                    )
            except Exception:
                log.debug("Blind-AUS-Merker nicht fortgeschrieben", exc_info=True)
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
            psm=int(raw["psm"]) if isinstance(raw.get("psm"), (int, float)) else None,
        )

    def _read_wallbox_status(self) -> tuple[Optional[WallboxStatus], Optional[str]]:
        """Liest den go-e Status einmal pro Lauf.

        Wird sowohl für die Wallbox-Entscheidung als auch für den PV-Überschuss
        der Heizstab-/Sommermodus-Logik gebraucht (readings.wallbox_power_w),
        damit beide auf demselben Snapshot arbeiten statt zwei leicht
        unterschiedliche go-e-Reads im selben Durchlauf zu machen.

        Rückgabe: (status, error_reason). (None, None) wenn Wallbox deaktiviert
        oder Client nicht initialisiert; (None, reason) bei Lesefehler.
        """
        cfg = self.cfg.wallbox
        if not cfg.enabled:
            return None, None
        if self.goe is None:
            return None, "Wallbox-Client nicht eingerichtet."

        try:
            raw = self.goe.get_status()
        except GoeError as e:
            log.error("go-e: Lesen fehlgeschlagen: %s", e)
            return None, "go-e-Status nicht lesbar."

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
        return status, None

    def _decide_wallbox(
        self,
        status: Optional[WallboxStatus],
        read_error: Optional[str],
    ) -> tuple[Optional[WallboxStatus], WallboxDecision]:
        """Der Controller pausiert die Wallbox nicht mehr aktiv. Sobald sie
        laden darf (PV-Überschussmodus aktiv), bleibt sie dauerhaft
        freigegeben (frc=0) – der go-e im Eco-Modus (lmo=4) sieht über
        seinen eigenen Hauszähler den echten Hausverbrauch inkl. Heizstab
        und drosselt/pausiert seinen Ladestrom von selbst, sobald der
        Heizstab eine Phase zuschaltet und dadurch weniger Überschuss
        übrig bleibt. Priorität Wasserspeicher vor Wallbox ergibt sich also
        automatisch daraus, dass der Heizstab zuerst zuschaltet – ohne
        dass der Controller dafür extra frc=1 setzen muss (das
        `frc`-Umschalten war genau der Grund für die Ladeprobleme am
        nächsten Tag).
        """
        cfg = self.cfg.wallbox

        if not cfg.enabled:
            log.info("Wallbox in der Config deaktiviert – keine Wallbox-Steuerung.")
            return None, WallboxDecision(
                action=WallboxAction.SKIPPED,
                target_force_state=None,
                reason="Wallbox in der Config deaktiviert.",
            )

        if status is None:
            reason = read_error or "Wallbox-Client nicht eingerichtet."
            return None, WallboxDecision(
                action=WallboxAction.ERROR,
                target_force_state=None,
                reason=f"{reason} Fail-safe: keine Änderung.",
            )

        if cfg.only_control_when_pv_surplus_active and not status.pv_surplus_active:
            decision = WallboxDecision(
                action=WallboxAction.SKIPPED,
                target_force_state=None,
                reason=(
                    "go-e ist nicht im PV-Überschussmodus (Eco). "
                    "Manuelles/normales Laden wird nicht angefasst."
                ),
            )
            if status.car_state == 1 and cfg.auto_reset_on_unplug:
                decision.target_logic_mode = 4
                decision.target_amp = cfg.eco_amps
                decision.target_psm = cfg.eco_phases
                decision.reason += (
                    " Automatisch zurück: Auto abgesteckt, wechsle in den Eco-Modus "
                    f"({cfg.eco_phases}-phasig, {cfg.eco_amps}A)."
                )
            return status, decision

        if status.force_state != 0:
            return status, WallboxDecision(
                action=WallboxAction.RELEASE,
                target_force_state=0,
                reason=(
                    "PV-Überschussmodus aktiv – Wallbox freigegeben, "
                    "go-e regelt Ladestrom über eigenen Hauszähler selbst."
                ),
            )
        return status, WallboxDecision(
            action=WallboxAction.UNCHANGED,
            target_force_state=0,
            reason="PV-Überschussmodus aktiv – Wallbox bereits freigegeben.",
        )

    def _execute_wallbox(self, decision: WallboxDecision) -> None:
        if decision.action in (WallboxAction.PAUSE, WallboxAction.RELEASE):
            target = decision.target_force_state
            if target not in (0, 1):
                decision.execution_error = (
                    f"forceState={target} wird nicht gesetzt (nur 0 oder 1 erlaubt)"
                )
                log.error(decision.execution_error)
            elif self._read_only:
                log.info(
                    "READ-ONLY: würde go-e forceState auf %d setzen (kein Schalten).",
                    target,
                )
                decision.executed = False
            elif not self.cfg.runtime.enabled:
                log.info(
                    "Steuerung aus (runtime.enabled=false): go-e forceState wird nicht "
                    "auf %d gesetzt.",
                    target,
                )
                decision.executed = False
            elif self.cfg.runtime.dry_run:
                log.info("DRY-RUN: würde go-e forceState auf %d setzen.", target)
                decision.executed = False
            elif self.goe is None:
                decision.execution_error = "Wallbox-Client nicht eingerichtet"
                log.error(decision.execution_error)
            else:
                try:
                    self.goe.set_force_state(target)
                    decision.executed = True
                    log.info("go-e forceState auf %d gesetzt.", target)
                except (GoeError, ValueError) as e:
                    decision.execution_error = str(e)
                    log.error("go-e: frc=%d setzen fehlgeschlagen: %s", target, e)

    def _execute_amp(self, decision: WallboxDecision) -> None:
        target = decision.target_amp
        if target is None:
            return
        if self._read_only:
            log.info("READ-ONLY: würde go-e amp auf %d setzen.", target)
            return
        if not self.cfg.runtime.enabled:
            log.info("Steuerung aus: go-e amp wird nicht auf %d gesetzt.", target)
            return
        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN: würde go-e amp auf %d setzen.", target)
            return
        if self.goe is None:
            decision.amp_execution_error = "Wallbox-Client nicht eingerichtet"
            log.error(decision.amp_execution_error)
            return
        try:
            self.goe.set_amp(target)
            decision.amp_executed = True
            log.info("go-e amp auf %d gesetzt.", target)
        except GoeError as e:
            decision.amp_execution_error = str(e)
            log.error("go-e: amp=%d setzen fehlgeschlagen: %s", target, e)

    def _execute_psm(self, decision: WallboxDecision) -> None:
        target = decision.target_psm
        if target is None:
            return
        if self._read_only:
            log.info("READ-ONLY: würde go-e psm auf %d setzen.", target)
            return
        if not self.cfg.runtime.enabled:
            log.info("Steuerung aus: go-e psm wird nicht auf %d gesetzt.", target)
            return
        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN: würde go-e psm auf %d setzen.", target)
            return
        if self.goe is None:
            decision.psm_execution_error = "Wallbox-Client nicht eingerichtet"
            log.error(decision.psm_execution_error)
            return
        try:
            self.goe.set_psm(target)
            decision.psm_executed = True
            log.info("go-e psm auf %d gesetzt.", target)
        except (GoeError, ValueError) as e:
            decision.psm_execution_error = str(e)
            log.error("go-e: psm=%d setzen fehlgeschlagen: %s", target, e)

    def _execute_lmo(self, decision: WallboxDecision) -> None:
        target = decision.target_logic_mode
        if target is None:
            return
        if self._read_only:
            log.info("READ-ONLY: würde go-e lmo auf %d setzen.", target)
            return
        if not self.cfg.runtime.enabled:
            log.info("Steuerung aus: go-e lmo wird nicht auf %d gesetzt.", target)
            return
        if self.cfg.runtime.dry_run:
            log.info("DRY-RUN: würde go-e lmo auf %d setzen.", target)
            return
        if self.goe is None:
            decision.logic_mode_execution_error = "Wallbox-Client nicht eingerichtet"
            log.error(decision.logic_mode_execution_error)
            return
        try:
            self.goe.set_logic_mode(target)
            decision.logic_mode_executed = True
            log.info("go-e lmo auf %d gesetzt.", target)
        except GoeError as e:
            decision.logic_mode_execution_error = str(e)
            log.error("go-e: lmo=%d setzen fehlgeschlagen: %s", target, e)

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

        # Wallbox-Status einmal pro Lauf lesen (nicht simuliert): wird für die
        # Wallbox-Entscheidung UND für den PV-Überschuss der Heizstab-Logik
        # gebraucht (readings.wallbox_power_w), damit Heizstab und Wallbox sich
        # nicht gegenseitig über den Hauptzähler ein-/ausschalten.
        wallbox_status: Optional[WallboxStatus] = None
        wallbox_read_error: Optional[str] = None
        if not self.cfg.simulation.enabled:
            wallbox_status, wallbox_read_error = self._read_wallbox_status()
            if wallbox_status is not None:
                # Nur im Eco-Modus (lmo=4) regelt die Wallbox ihren Ladestrom
                # selbst nach PV-Überschuss – ihre Leistung darf dann aus dem
                # Hauptzähler herausgerechnet werden (true_surplus_w), damit
                # die Heizstab-Logik den eigenen Wallbox-Ladestrom nicht als
                # zusätzlichen Überschuss missversteht. Im Standard-/manuellen
                # Modus lädt sie mit fest eingestelltem Strom unabhängig von
                # der PV – dieser Verbrauch ist bereits regulär im Hauptzähler
                # enthalten und darf NICHT herausgerechnet werden, sonst denkt
                # die Heizstab-Logik fälschlich, es gäbe mehr Überschuss.
                readings.wallbox_power_w = (
                    wallbox_status.power_w if wallbox_status.pv_surplus_active else 0.0
                )
        # Große Verbraucher pro Minute für die Nachtbedarf-Auswertung (echte Wallbox-Leistung, auch
        # außerhalb des Eco-Modus); nicht kritisch, nicht in der Simulation
        if not self.cfg.simulation.enabled:
            try:
                from db import record_loads
                record_loads(wallbox_status.power_w if wallbox_status is not None else None,
                             readings.heater_meter_power_w, readings.whirlpool_power_w)
            except Exception:
                log.warning("Wallbox/Heizstab-Minutenwerte nicht gespeichert", exc_info=True)
            if readings.consumers is not None:
                try:
                    from db import record_consumers
                    record_consumers(readings.consumers)
                except Exception:
                    log.warning("Verbraucher-Minutenwerte nicht gespeichert", exc_info=True)

        temp_status: Optional[TempStatus] = None
        if readings.storage_temp_c is not None:
            temp_status = self._classify_temp(readings.storage_temp_c)

        self._log_readings(readings, temp_status)

        h = self.cfg.heater
        decisions: list[PhaseDecision] = []
        critical = readings.critical_errors
        if critical:
            # Ohne Hauptzähler bzw. Wechselrichter ist kein Überschuss bekannt: keine
            # überschussbasierten Schaltbefehle. Sicherheitsabschaltungen (Max-Temp,
            # Temperatur unbekannt) gelten weiter, siehe decide_phases.
            log.warning(
                "Lesefehler bei: %s – Fail-safe: keine überschussbasierten Schaltbefehle.",
                ", ".join(critical),
            )

        # Sommermodus: Prüfung VOR den Phasen-Entscheidungen
        sm_heating = sm_stop = False
        sm_reason = ""
        if not critical:
            sm_heating, sm_stop, sm_reason = self._check_summer_mode(readings, temp_status)

        # Budget: kommt von der Kaskade (geglättet); ohne aktuelles Kaskaden-Budget
        # (Kaskade aus / veraltet) eigene Berechnung aus dem Überschuss, gleich geglättet.
        budget_w: Optional[float] = None
        budget_down_w: Optional[float] = None
        force: Optional[str] = None
        info = get_cascade_budget("heizstab")
        if info is not None:
            budget_w, budget_down_w, force = float(info["budget_w"]), float(info["budget_down_w"]), info["force"]
            log.info("Heizstab-Budget (Kaskade): %.0f W zu / %.0f W ab (roh %s W)%s", budget_w,
                     budget_down_w, info["raw_w"], f", manuell {force}" if force else "")
        elif readings.true_surplus_w is not None:
            raw = -readings.true_surplus_w
            budget_w, budget_down_w = smooth_heater_budgets(
                raw, h.budget_smooth_min, h.budget_smooth_down_min,
                record=not self._read_only and not self.cfg.runtime.dry_run,
            )
            log.info("Heizstab-Budget (Eigenberechnung, kein Kaskaden-Budget): %.0f W zu / %.0f W ab (roh %.0f W)",
                     budget_w, budget_down_w, raw)

        last = get_last_phase_changes()
        plugs = (self.ph1, self.ph2, self.ph3)
        states = (readings.ph1_on, readings.ph2_on, readings.ph3_on)

        # Thermostat im Heizstab: Relais an, Heizstab zieht nichts (Schichtung) → Relais halten bzw.
        # nach 15 min nahe Max „praktisch voll“ (siehe heater_phases.thermostat_step)
        thermo_open = thermo_full = None
        try:
            upd, thermo_open, thermo_full, msg = thermostat_step(
                get_heater_thermostat(), now,
                any(st is True for i, st in enumerate(states) if not h.disabled_phases[i]),
                readings.heater_meter_power_w, readings.storage_temp_c, h.storage_max_temp,
            )
            if msg:
                log.info(msg)
            if upd and not self._read_only:
                set_heater_thermostat(**upd)
        except Exception:
            log.warning("Heizstab-Thermostat-Zustand nicht verfügbar", exc_info=True)
        outs = decide_phases(
            [PhaseIn(f"PH{i + 1}", states[i], last.get(i + 1), disabled=h.disabled_phases[i])
             for i in range(3)],
            readings.storage_temp_c,
            budget_w,
            HeaterParams(
                phase_w=h.phase_power_w, grid_assist_w=h.grid_assist_w,
                max_temp=h.storage_max_temp, resume_temp=h.heat_resume_temp,
                min_on_s=h.phase_min_on_s, min_off_s=h.phase_min_off_s,
                down_hyst_frac=h.phase_down_hyst,
            ),
            now,
            budget_down_w=budget_down_w,
            force=force,
            summer="stop" if sm_stop else "heat" if sm_heating else None,
            summer_reason=sm_reason,
            surplus_fail_safe=bool(critical),
            thermostat_open=thermo_open,
            practically_full=thermo_full,
        )
        for i, out in enumerate(outs):
            d = PhaseDecision(f"PH{i + 1}", states[i], out.action, out.reason, blind=out.blind,
                              code=out.code, data=out.data)
            self._execute(d, plugs[i])
            decisions.append(d)
            if d.executed and not d.blind and d.action in (PhaseAction.TURN_ON, PhaseAction.TURN_OFF):
                try:
                    log_phase_change(i + 1, "on" if d.action == PhaseAction.TURN_ON else "off")
                except Exception:
                    log.warning("Phase-Log konnte nicht geschrieben werden", exc_info=True)

        for d in decisions:
            state_str = (
                "ON" if d.current_state else "OFF" if d.current_state is False else "?"
            )
            log.info("Decision %s: state=%s -> %s", d.name, state_str, d.action.value)
            err = "" if not d.execution_error else f" [ERR: {d.execution_error}]"
            log.info("Reason: %s%s", d.reason, err)

        wallbox_status, wallbox_decision = self._handle_wallbox(
            wallbox_status, wallbox_read_error
        )

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
            fail_safe_active=bool(critical),
            summer_mode_heating=_sm_heating_result,
            summer_mode_reason=_sm_reason_result,
        )
        log.info("=== PV-Controller Ende ===")
        return result

    def _handle_wallbox(
        self,
        wallbox_status: Optional[WallboxStatus],
        wallbox_read_error: Optional[str],
    ) -> tuple[Optional[WallboxStatus], WallboxDecision]:
        log.info(
            "Wallbox: enabled=%s url=%s",
            str(self.cfg.wallbox.enabled).lower(),
            self.cfg.wallbox.url or "n/a",
        )
        status, decision = self._decide_wallbox(
            wallbox_status,
            wallbox_read_error,
        )
        decision.code, decision.data = wallbox_reason_code(self.cfg.wallbox.enabled, status, decision.action)

        self._execute_wallbox(decision)
        self._execute_lmo(decision)
        self._execute_amp(decision)
        self._execute_psm(decision)
        log.info(
            "Wallbox-Entscheidung: action=%s target_force_state=%s target_lmo=%s",
            decision.action.value,
            decision.target_force_state,
            decision.target_logic_mode,
        )
        err = "" if not decision.execution_error else f" [FEHLER: {decision.execution_error}]"
        lmo_err = "" if not decision.logic_mode_execution_error else f" [LMO-FEHLER: {decision.logic_mode_execution_error}]"
        log.info("Grund: %s%s%s", decision.reason, err, lmo_err)
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
        log.info("Wallbox-Leistung:   %s", fmt(r.wallbox_power_w, " W"))
        log.info(
            "Überschuss o. Heizstab: %s",
            fmt(r.surplus_without_heater_w, " W"),
        )
        log.info(
            "Überschuss o. Heizstab+Wallbox: %s",
            fmt(r.true_surplus_w, " W"),
        )
        log.info("Wasserspeicher-Temp:      %s", fmt(r.storage_temp_c, " °C"))
        log.info(
            "Temperaturstatus:   %s",
            temp_status.value if temp_status is not None else "n/a",
        )
        log.info(
            "Phasen-Status:      PH1=%s PH2=%s PH3=%s",
            "deaktiviert" if "ph1" in r.disabled_phases else "ON" if r.ph1_on else "OFF" if r.ph1_on is False else "?",
            "deaktiviert" if "ph2" in r.disabled_phases else "ON" if r.ph2_on else "OFF" if r.ph2_on is False else "?",
            "deaktiviert" if "ph3" in r.disabled_phases else "ON" if r.ph3_on else "OFF" if r.ph3_on is False else "?",
        )


def run_controller(
    config: Config, mode: ControllerMode = ControllerMode.NORMAL
) -> ControllerResult:
    """Convenience wrapper: build a Controller and execute one run in the
    requested mode. The web layer should always pass READ_ONLY for status
    snapshots so that the UI cannot accidentally switch hardware."""
    return Controller(config).run(mode=mode)
