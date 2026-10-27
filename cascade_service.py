"""Kaskaden-Service: PV-Überschuss-Prioritätskaskade.

Entscheidet basierend auf dem Brutto-PV-Überschuss, welche Geräte (geordnet
nach priority) ein- oder ausgeschaltet werden sollen.

Brutto-Überschuss = Hauptzähler (Shelly 3EM, invertiert) + gemessene Leistung
aller kaskaden-gesteuerten Festlasten (Heizstab 3EM; Wallbox go-e nur wenn
im Eco-Modus lmo=4, nicht im Basic-Modus).  Der Hauptzähler ist die einzig
zuverlässige Quelle: er sieht alle Lasten inkl. Wallbox im Basic-Modus.

Für heizstab/wallbox setzt die Kaskade das is_on-Flag in der DB; der Controller
liest es als Freigabe.  Die Temperatur-Sicherheitslogik des Heizstabs und der
Eco-Modus der Wallbox bleiben als zusätzliche Bedingungen erhalten.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from clients.goe_client import BATTERY_CHECK_TIMEOUT_S, GoeClient, GoeError, car_charging, charging_now
from clients.shelly_cascade_client import ShellyCascadeClient, ShellyCascadeError
from cascade_plan import WALLBOX_MIN_A, WALLBOX_VOLT, wallbox_amp_cap, BATTERY, HEATER_MAX, HEATER_MIN, HEATER_STAGES, HeaterCtx, walk
from clients.shelly_client import Shelly3EMClient, ShellyPlugClient, ShellyStorageTempClient
from clients.solax_client import SolaxClient
from clients.zendure_client import ZendureClient
from zendure_control import DEVICE_MAX_W, WRITE_DELTA_W, charge_acceptance
from heater_phases import RELEASE_DROP_C, THERMOSTAT_OPEN_W
import meter_ring
import suntime
import wallbox_pause
from config import Config
from heater_power import PhaseRead
from heater_power import resolve as resolve_heater_power
from db import (
    log_cascade_action,
    ensure_zendure_cascade_device,
    auto_reenable_cascade_device,
    clear_cascade_override,
    get_cascade_device,
    get_cascade_devices,
    get_cascade_profile,
    get_cascade_devices_due_for_retry,
    get_cascade_settings,
    get_heater_thermostat,
    get_zendure_ctl,
    get_last_phase_states,
    increment_cascade_device_errors,
    reset_cascade_device_errors,
    set_cascade_budget,
    smooth_heater_budgets,
    set_cascade_device_retry_after,
    update_cascade_device,
    update_cascade_device_live_status,
    update_cascade_device_state,
    get_wallbox_pause,
    set_wallbox_pause,
)

_MAX_ERRORS = 5
_METER_READ_TIMEOUT_S = 1.5   # Lesen darf kurz warten; ein toter Zähler bremst sonst jeden Lauf
_AUTO_RETRY_HOURS = 1
NIGHT_MARGIN = timedelta(minutes=30)   # Haupt-WR offline gilt erst so lange nach Sonnenuntergang (vor Aufgang) als PV 0 W

log = logging.getLogger(__name__)


class CascadeService:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        timeout = config.runtime.request_timeout_seconds
        self.solax = SolaxClient(config.solax.url, config.solax.pwd, timeout)
        self.shelly = ShellyCascadeClient()
        self.heater_meter = Shelly3EMClient(
            config.shelly.heater_meter_url, _METER_READ_TIMEOUT_S, name="heater_meter"
        )
        self.main_meter = Shelly3EMClient(
            config.shelly.main_meter_url, _METER_READ_TIMEOUT_S, name="main_meter",
            type_=config.shelly.main_meter_type
        )
        self.storage = ShellyStorageTempClient(
            config.shelly.storage_url, _METER_READ_TIMEOUT_S, name="storage"
        )
        self.solax2: Optional[SolaxClient] = None
        if config.solax2_active:
            self.solax2 = SolaxClient(config.solax2.url, config.solax2.pwd, config.solax2_timeout,
                                       config.solax2_connect_timeout)
        self.goe: Optional[GoeClient] = None
        self._goe_cache: Optional[dict] = None
        if config.wallbox.enabled and config.wallbox.url:
            self.goe = GoeClient(config.wallbox.url, config.wallbox.request_timeout_seconds)
        # Batterie-Sperre liest go-e immer, wenn eine URL eingetragen ist (auch bei wallbox.enabled=false:
        # ein Auto kann z. B. im Basic-Modus laden), kurzes Timeout
        self.goe_battery: Optional[GoeClient] = (
            GoeClient(config.wallbox.url, BATTERY_CHECK_TIMEOUT_S) if config.wallbox.url else None)
        # Batterie-Regelung (Phase 2): nur mit aktivem Gerät UND Master-Schalter
        # Grund-Code der letzten Geräte-Entscheidung: (action, code, data) – nur Protokoll
        self._last_code: tuple[str, str, dict] = ("", "", {})
        self._last_plan = None
        self.zendure_ctl = None
        self.zendure_client: Optional[ZendureClient] = None
        if config.zendure_active and config.zendure.control_enabled:
            from zendure_control import ZendureControl
            self.zendure_client = ZendureClient(config.zendure.ip, config.zendure.sn)
            self.zendure_ctl = ZendureControl(
                config, self.zendure_client, dry_run=config.runtime.dry_run
            )

    def _log(self, surplus: int, device_id: str, action: str, reason: str, remaining: int) -> None:
        """Entscheidung festhalten (nur Ereignisse dauerhaft, siehe db.log_cascade_action)."""
        log_cascade_action(surplus, device_id, action, reason, remaining, verbose=self.cfg.cascade.log_verbose)

    # ── Hauptmethode ─────────────────────────────────────────────────────────

    def run_cascade(self) -> dict:
        """Liest Überschuss und verteilt ihn entlang des aktiven Profils (Sommer/Winter)."""
        settings = get_cascade_settings()

        if not settings["cascade_enabled"]:
            log.info("Kaskade: deaktiviert (cascade_enabled=False)")
            return {"surplus_watts": 0, "devices": [], "disabled": True}

        self._goe_cache = None
        self._poll_shelly_devices()
        if self.zendure_ctl is not None:
            ensure_zendure_cascade_device(self.cfg.zendure.name, int(self.cfg.zendure.max_charge_w))
        devices = [
            d for d in get_cascade_devices()
            if d["enabled"] and (d["type"] != "zendure" or self.zendure_ctl is not None)
        ]
        # Wallbox: Nennleistung nie unter dem, was der Wagen im Eco-Modus wirklich zieht (Phasen × A × 230 V).
        # Ein zu niedriger DB-Wert (z. B. 1610 W bei 3,7 kW) ließ Wallbox und Heizstab über das Budget laufen →
        # Netzbezug → der Wächter drosselte die Batterie statt der nachrangigen Lasten.
        wb_cfg = self.cfg.wallbox
        for d in devices:
            if d["type"] == "wallbox":
                d["power_watts"] = max(int(d["power_watts"]), int(wb_cfg.eco_phase_count * wb_cfg.eco_amps * 230))
        by_id = {d["id"]: d for d in devices}
        heater_dev = next((d for d in devices if d["type"] == "heizstab"), None)
        battery_dev = by_id.get(BATTERY)

        season = "summer" if self.cfg.summer_mode.enabled else "winter"
        comfort_min = self.cfg.comfort_min_for(season)       # None = keine „bis Min“-Stufe im Profil
        order = []
        for key in get_cascade_profile(season, comfort_min is not None):
            if key in HEATER_STAGES:
                if heater_dev is not None:
                    order.append(key)
            elif key in by_id:
                order.append(key)

        # Lesen: Speichertemperatur, Batterie und PV (für die Grundlast) parallel zum Hauptzähler
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="casc") as ex:
            f_temp = ex.submit(self._read_storage_temp) if heater_dev is not None else None
            f_zd = ex.submit(self.zendure_client.get_status) if battery_dev is not None else None
            f_pv = ex.submit(self._read_pv_total_w) if battery_dev is not None else None
            # Primärquelle: Hauptzähler (sieht alle Lasten inkl. Wallbox im Basic-Modus).
            # Fallback auf Solax wenn Hauptzähler nicht erreichbar.
            main_feed_in = self._get_main_meter_feed_in_w()
            temp = f_temp.result() if f_temp else None
            zd_status = f_zd.result() if f_zd else None
            pv_total_w = f_pv.result() if f_pv else None
        if main_feed_in is None:
            log.warning("Kaskade: Hauptzähler nicht verfügbar, Fallback zu Solax")
            raw_feed_in = self._get_raw_feed_in_w()
            feed_in_source = "Solax"
        else:
            raw_feed_in = main_feed_in
            feed_in_source = "Hauptzähler"

        controlled_loads_w, actual_device_w = self._get_controlled_loads_w(devices)
        # Brutto-Überschuss = Einspeisung + kaskaden-gesteuerte Festlasten.
        # Nur Lasten addieren die die Kaskade selbst steuert (Heizstab; Wallbox nur
        # im Eco-Modus). Wallbox im Basic-Modus wird vom Hauptzähler bereits erfasst
        # und darf nicht doppelt angerechnet werden.
        # Batterie: Ladeleistung ist eine kaskaden-gesteuerte Last (zurückrechnen);
        # Entladeleistung ist kein PV-Überschuss (abziehen).
        if zd_status is not None:
            controlled_loads_w += zd_status.charge_w
        gross_feed_in = raw_feed_in + int(controlled_loads_w)
        if zd_status is not None:
            gross_feed_in -= int(zd_status.discharge_w)
        surplus = max(0, gross_feed_in)

        log.info(
            "Kaskade: %s feed_in=%d W + Festlasten=%.0f W → Brutto=%d W",
            feed_in_source, raw_feed_in, controlled_loads_w, gross_feed_in,
        )

        if not settings["allow_grid_draw"] and gross_feed_in < -50:
            effective_surplus = gross_feed_in  # negative → cascade turns everything off
            log.info(
                "Kaskade: PV-Überschuss (brutto) %d W, allow_grid_draw=False → erzwinge Abschaltung",
                gross_feed_in,
            )
        else:
            effective_surplus = max(0, surplus - settings["min_surplus_watts"])
            if settings["min_surplus_watts"] > 0 and surplus < settings["min_surplus_watts"]:
                log.info(
                    "Kaskade: Brutto-Überschuss %d W < Mindestüberschuss %d W → kein Einschalten",
                    surplus,
                    settings["min_surplus_watts"],
                )

        log.info(
            "=== Kaskade Start (%sprofil): Überschuss=%d W (effektiv=%d W), %d Einträge ===",
            "Sommer" if season == "summer" else "Winter", surplus, effective_surplus, len(order),
        )

        # Batterie-Entscheidung auf das 60-s-Mittel des Hauptzählers ohne Batterie (Ringpuffer des
        # Wächters, 3-s-Werte; eigene Drosselungen zählen so nicht mit) statt der Einzelmessung: pulsierende Lasten (Herd, Backofen) ließen sie sonst jede Minute
        # zwischen Laden und Entladen wechseln. Unter 10 Werten: Einzelmessung. Heizstab/Kaskade unverändert.
        bat_feed_in = float(raw_feed_in)
        feed_avg: Optional[tuple[float, int]] = None
        if battery_dev is not None and main_feed_in is not None and zd_status is not None:
            try:
                m = meter_ring.mean()                 # Mittel „Netz ohne Batterie“
            except Exception:
                m = None
                log.warning("Zähler-Ringpuffer nicht lesbar", exc_info=True)
            if m is not None:
                # mit der jetzigen Batterie-Leistung wieder zusammensetzen (wie eine Zählermessung jetzt)
                bat_feed_in = -(m[0] + zd_status.charge_w - zd_status.discharge_w)
                feed_avg = (bat_feed_in, m[1])
                log.info("Kaskade: Batterie nutzt Einspeisung %.0f W (Mittel 60 s, %d Werte) statt jetzt %d W",
                         bat_feed_in, m[1], raw_feed_in)
        bat_delta = bat_feed_in - raw_feed_in

        # Grundlast für das Batterie-Entladen: Haus ohne Heizstab und Wallbox.
        # Haus = PV + Netzbezug + Entladung − Ladung (die Batterie ist selbst kein Verbraucher).
        base_load_w: Optional[float] = None
        if battery_dev is not None and pv_total_w is not None and zd_status is not None:
            wb_power = self._wallbox_power_w() or 0.0
            house_w = pv_total_w - bat_feed_in + zd_status.discharge_w - zd_status.charge_w
            heater_w = actual_device_w.get(heater_dev["id"], 0.0) if heater_dev else 0.0
            base_load_w = max(0.0, house_w - heater_w - wb_power)

        n_ph = self.cfg.heater.enabled_phase_count
        h = self.cfg.heater
        actual_heater_w = float(actual_device_w.get(heater_dev["id"], 0.0)) if heater_dev else 0.0
        # Thermostat im Heizstab (Zustand vom Controller des Vorlaufs + jetzige Messung): offen →
        # gemessene statt Relais-Leistung; praktisch voll → Heizstab-Stufen inaktiv
        thermo = {}
        try:
            thermo = get_heater_thermostat()
        except Exception:
            log.warning("Heizstab-Thermostat-Zustand nicht lesbar", exc_info=True)
        ctx = HeaterCtx(
            phase_w=h.phase_power_w, grid_assist_w=h.grid_assist_w, n_phases=n_ph, temp=temp,
            comfort_min=comfort_min if comfort_min is not None else 0.0, max_temp=h.storage_max_temp, actual_w=actual_heater_w,
            thermostat_open=bool(thermo.get("open_since")) and actual_heater_w < THERMOSTAT_OPEN_W,
            practically_full=bool(thermo.get("full_since")),
        )
        self._thermo = thermo
        results: list[dict] = []
        battery_plan: dict = {}

        # Fail-safe Batterie: ohne Hauptzähler oder WR kein Laden/Entladen (Standby, nicht halten)
        battery_failsafe = ", ".join(
            n for n, bad in (("Hauptzähler", main_feed_in is None), ("Wechselrichter", pv_total_w is None)) if bad
        ) or None
        # Laden höchstens bis „jetzige Ladung + jetzige Einspeisung“ (nie Leistung, die gerade aus dem Netz käme)
        export_cap_w = (zd_status.charge_w + bat_feed_in) if zd_status is not None else None

        def battery_fn(avail_w: float) -> float:
            # Mittel statt jetzt: Differenz auf den Rest an der Batterie-Position (walk liefert ≥ 0;
            # ist dort nichts übrig, bleibt es dabei – kein Laden auf ein besseres Mittel hin)
            if avail_w > 0:
                avail_w = max(0.0, avail_w + bat_delta)
            # Aufnahmegrenze: gegen den Sollwert prüfen, der seit dem letzten Lauf am Gerät gilt
            accept_w = None
            if zd_status is not None and not battery_failsafe:
                try:
                    ctl = get_zendure_ctl()
                    accept_w = charge_acceptance(zd_status, ctl["mode"], int(ctl["limit_w"]))
                except Exception:
                    log.warning("Kaskade: Aufnahmegrenze der Batterie nicht prüfbar", exc_info=True)
            charge_w, reason = self._handle_zendure(
                battery_dev, avail_w, surplus, zd_status, -bat_feed_in, base_load_w,
                self._active_override(battery_dev), failsafe=battery_failsafe, export_cap_w=export_cap_w,
            )
            p = self._last_plan
            code = p.code
            if code == "charging_surplus" and HEATER_MAX in order and order.index(BATTERY) < order.index(HEATER_MAX):
                code = "priority_first"               # Batterie steht im Profil vor dem Heizstab
            results.append(self._result(battery_dev, reason, p.action, code, {**p.data, "limit_w": p.limit_w}))
            # Batterie wartet (Einspeise-Deckel): bekommt weniger, als ihr zusteht
            waiting = (p.mode == "charge" and p.want_w is not None and p.want_w - p.limit_w > WRITE_DELTA_W)
            if accept_w is not None and p.mode == "charge" and charge_w > accept_w:
                # nimmt seit Minuten weniger auf als angeboten → nur Ist + Puffer reservieren, kein Warten
                log.info("Kaskade: Batterie nimmt nur %.0f W auf (Sollwert %d W) – reserviere %d W statt %.0f W",
                         zd_status.charge_w, p.limit_w, accept_w, charge_w)
                charge_w, waiting = float(accept_w), False
                results[-1]["data"] = {**(results[-1].get("data") or {}), "accept_w": accept_w}
            battery_plan["charge_w"] = charge_w
            battery_plan["waiting"] = waiting
            return charge_w

        def device_fn(key: str, remaining: float) -> float:
            device = by_id[key]
            if device["type"] == "wallbox":
                buffer_w = self._wallbox_battery_buffer_w(
                    battery_dev is not None and BATTERY in order and order.index(BATTERY) < order.index(key),
                    zd_status, battery_failsafe)
                handled = self._handle_wallbox_pv(device, int(remaining), surplus, actual_device_w, buffer_w)
                if handled is not None:
                    new_remaining, result = handled
                    results.append(result)
                    return new_remaining
            new_remaining, reason = self._handle_device(
                device, int(remaining), surplus, effective_surplus, actual_device_w
            )
            results.append(self._result(device, reason, *self._last_code))
            return new_remaining

        def heater_fn(gross_w: int, battery_w: int, remaining_w: int) -> None:
            reason = self._store_heater_budget(
                heater_dev, gross_w, battery_w, remaining_w, surplus, season, temp, ctx,
                actual_heater_w, self._active_override(heater_dev),
                battery_waiting=bool(battery_plan.get("waiting")),
            )
            results.append(self._result(heater_dev, reason, *self._last_code))

        res = walk(order, effective_surplus, ctx, battery_fn, device_fn,
                   heater_fn if heater_dev is not None else None)

        log.info("=== Kaskade Ende: remaining=%d W ===", res.remaining_w)
        return {
            "surplus_watts": surplus, "devices": results, "profile": season,
            # Zahlen für den Entscheidungs-Schnappschuss (decisions.py)
            "effective_surplus_w": int(effective_surplus), "feed_in_w": int(raw_feed_in),
            "battery_feed_in_avg_w": None if feed_avg is None else int(round(feed_avg[0])),
            "battery_feed_in_avg_n": None if feed_avg is None else feed_avg[1],
            "feed_in_source": feed_in_source, "controlled_loads_w": int(controlled_loads_w),
            "remaining_w": res.remaining_w, "order": list(order), "storage_temp_c": temp,
            "min_surplus_w": int(settings["min_surplus_watts"]), "allow_grid_draw": bool(settings["allow_grid_draw"]),
        }

    # ── Heizstab: Kaskade legt nur das Budget fest ───────────────────────────

    def _store_heater_budget(
        self, device: dict, gross_w: int, battery_w: int, remaining_w: int, surplus: int, season: str,
        temp: Optional[float], ctx: HeaterCtx, actual_w: float, override: Optional[str],
        battery_waiting: bool = False,
    ) -> str:
        """Heizstab-Budget für den Controller ablegen.

        Geglättet wird der Gesamt-Überschuss ``gross_w`` (vor der Batterie); die **aktuelle**
        Batterie-Ladung ``battery_w`` wird erst danach ungeglättet abgezogen. So steigt das Budget
        sofort, wenn die Batterie voll wird (kein Nachlaufen des Mittelwerts).
        Welche Phasen innerhalb des Budgets laufen, entscheidet allein der Controller
        (heater_phases.decide_phases; Mindestzeiten, Hysterese, Max-Temp).

        ``battery_waiting``: die Batterie (im Profil davor) bekommt wegen des Einspeise-Deckels
        weniger, als ihr zusteht → auch fürs Abschalten zählt der jetzige Überschuss statt des
        Mittels, damit der Heizstab im selben Lauf Platz macht (Mindestlaufzeit gilt weiter).
        """
        h = self.cfg.heater
        n_ph = h.enabled_phase_count           # stillgelegte Phasen zählen nicht im Budget
        if override == "off":
            budget = budget_down = 0
            force = "off"
        elif override == "on":
            budget = budget_down = int(h.phase_power_w * n_ph)
            force = "on"
        else:
            up, down = smooth_heater_budgets(gross_w, h.budget_smooth_min, h.budget_smooth_down_min)
            # Zuschalten nie über dem jetzt tatsächlich freien Überschuss: Bricht der Überschuss ein, würde
            # das noch hohe Mittel den Heizstab sonst aus dem Netz starten (mit Batterie nimmt sie zudem den
            # Rest). Abschalten nutzt weiter das kurze Mittel.
            up = min(up, gross_w)
            if battery_waiting:
                down = min(down, gross_w)
            budget, budget_down, force = int(round(up)) - battery_w, int(round(down)) - battery_w, None
        set_cascade_budget(device["id"], budget, gross_w, force, budget_down, shed_now=battery_waiting and not force)

        target = max(0, min(n_ph, math.floor((budget + h.grid_assist_w) / h.phase_power_w)))
        now = datetime.now()
        active = target >= 1
        action = "budget"                      # laufende Budget-Entscheidung (nur mit log_verbose in cascade_log)
        if active != device["is_on"]:
            action = "turn_on" if active else "turn_off"      # Budget erlaubt/verbietet den Heizstab: Ereignis
            update_cascade_device_state(
                device["id"], active,
                turned_on_at=now if active else None, turned_off_at=None if active else now,
            )

        stage = ""
        if temp is not None and ctx.comfort_min > 0:
            stage = " (bis Min)" if ctx.stage_active(HEATER_MIN) else " (Min erreicht)"
        reason = (
            f"Budget {budget} W zu / {budget_down} W ab (Überschuss roh {gross_w} W, Mittel "
            f"{h.budget_smooth_min}/{h.budget_smooth_down_min} min, abzüglich Batterie {battery_w} W jetzt"
            + ("; Batterie wartet → ab ohne Mittel" if battery_waiting else "") + ") "
            f"→ bis zu {target} Phase(n); Startschwelle {int(h.phase_power_w - h.grid_assist_w)} W, "
            f"Ist {int(actual_w)} W"
            + (f"; Speicher {temp:.1f} °C{stage}" if temp is not None else "; Speichertemperatur unbekannt")
            + (f"; manuell {force.upper()}" if force else "")
        )
        start_w = int(h.phase_power_w - h.grid_assist_w)
        data = {"budget_w": budget, "budget_down_w": budget_down, "raw_w": gross_w, "battery_w": battery_w,
                "rest_w": budget, "threshold_w": start_w, "target_phases": target, "actual_w": int(actual_w),
                "smooth_min": h.budget_smooth_min, "grid_assist_w": int(h.grid_assist_w),
                "temp_c": None if temp is None else round(temp, 1)}
        if battery_waiting:
            data["battery_waiting"] = True
        thermo = getattr(self, "_thermo", {}) or {}
        if force:
            code = "manual_override"
            data["mode"] = "an" if force == "on" else "aus"
        elif ctx.practically_full and thermo.get("full_temp") is not None:
            code = "storage_practically_full"
            data.update(open_min=15, temp_c=thermo["full_temp"],
                        release_c=round(float(thermo["full_temp"]) - RELEASE_DROP_C, 1))
            reason += f"; Speicher praktisch voll – Freigabe ab {data['release_c']} °C"
        elif ctx.thermostat_open:
            code = "heater_thermostat_open"
            try:
                age = (now - datetime.fromisoformat(thermo["open_since"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = 60.0
            data["open_min"] = max(1, int(round(age / 60)))
            reason += (f"; Thermostat im Heizstab offen (Relais an, {int(actual_w)} W seit {data['open_min']} min) "
                       "– Überschuss geht an Batterie/Wallbox")
        elif target >= 1:
            code = "grid_assist" if budget < target * h.phase_power_w else "budget_available"
        elif gross_w - battery_w >= start_w:
            code = "smoothing_wait"                 # jetzt würde es reichen, das Mittel liegt noch darunter
            data["raw_w"] = gross_w - battery_w
        else:
            code = "below_start_threshold"
        self._last_code = (action, code, data)
        self._log(surplus, device["id"], action, reason, remaining_w)
        log.info("Kaskade: %s → %s, remaining=%d W", device["name"], reason, remaining_w)
        return reason

    # ── Sonstige Geräte (Shelly, Wallbox) ────────────────────────────────────

    def _handle_device(
        self, device: dict, remaining: int, surplus: int, effective_surplus: int,
        actual_device_w: dict[str, float],
    ) -> tuple[int, str]:
        """Ein-/Ausschaltentscheidung für ein Festlast-Gerät; liefert (Restbudget, Begründung)."""
        override = self._active_override(device)
        if override is not None:
            if device["is_on"]:
                # Gemessene statt nominale Leistung abziehen (Phantom-Budget vermeiden)
                remaining -= int(actual_device_w.get(device["id"], device["power_watts"]))
            reason = f"manual_override ({override})"
            self._last_code = ("manual_override", "manual_override",
                               {"mode": "an" if override == "on" else "aus",
                                "until": device["manual_override_until"]})
            self._log(
                surplus, device["id"], "manual_override",
                f"Manuelle Übersteuerung: {override} aktiv bis {device['manual_override_until']}",
                remaining,
            )
            log.info("Kaskade: %s → manual_override (%s), remaining=%d W", device["name"], override, remaining)
            # Shelly in den gewünschten Zustand bringen (DB-Flag allein reicht nicht)
            self._dispatch(device, "turn_on" if override == "on" else "turn_off")
            return remaining, reason

        running_w = actual_device_w.get(device["id"]) if device["type"] == "wallbox" else None
        action, reason = self._decide(device, effective_surplus, remaining, running_w)
        self._last_code = (action, *self._reason_code(device, action, remaining, effective_surplus))

        # Zustand in DB aktualisieren und remaining korrekt anpassen:
        # - Laufendes Gerät: gemessene Ist-Leistung abziehen (nicht Nominalwert).
        #   Das verhindert Phantom-Reservierungen wenn z.B. der Controller den
        #   Heizstab wegen Temperatur blockiert (is_on=1 aber actual=0 W).
        # - Ausschalten: abgezogene Ist-Leistung wieder freigeben
        # - Einschalten: Nominalleistung reservieren (Gerät läuft noch nicht)
        now = datetime.now()
        if device["is_on"]:
            actual_w = int(actual_device_w.get(device["id"], device["power_watts"]))
            remaining -= actual_w
            if action == "turn_off":
                update_cascade_device_state(device["id"], False, turned_off_at=now)
                remaining += actual_w  # Gerät freigegeben, Budget zurück
                device = {**device, "is_on": False}
        elif action == "turn_on":
            update_cascade_device_state(device["id"], True, turned_on_at=now)
            remaining -= device["power_watts"]
            device = {**device, "is_on": True}

        self._log(surplus, device["id"], action, reason, remaining)
        log.info("Kaskade: %s → %s, remaining=%d W | %s", device["name"], action, remaining, reason)
        self._dispatch(device, action)
        return remaining, reason

    # ── Batterie (stufenlos) ─────────────────────────────────────────────────

    def _goe_raw(self) -> Optional[dict]:
        """go-e-Status, einmal pro Kaskadenlauf gelesen (None bei Fehler/ohne Wallbox)."""
        client = self.goe or self.goe_battery
        if client is None:
            return None
        if self._goe_cache is None:
            try:
                self._goe_cache = client.get_status() or {}
            except Exception:
                log.warning("Kaskade: Wallbox-Status nicht lesbar", exc_info=True)
                self._goe_cache = {"_error": True}
        return None if self._goe_cache.get("_error") else self._goe_cache

    def _wallbox_power_w(self) -> Optional[float]:
        raw = self._goe_raw()
        if raw is None:
            return None
        nrg = raw.get("nrg")
        return max(0.0, float(nrg[11])) if isinstance(nrg, list) and len(nrg) >= 12 else 0.0

    def _wallbox_battery_buffer_w(self, battery_before: bool, st, failsafe: Optional[str]) -> int:
        """Puffer für die Batterie im Wallbox-Budget (``wallbox.battery_buffer_w``).

        Nur wenn die Batterie im Profil vor der Wallbox steht, nicht voll ist (SoC < Ziel) und noch nicht
        mit voller Ladeleistung lädt – dann fängt der Puffer kurze Einbrüche ab, bevor der Wächter die
        Batterie drosselt. Batterie voll oder auf Max: kein Puffer."""
        if not battery_before or st is None or failsafe or self.zendure_ctl is None:
            return 0
        zc = self.cfg.zendure
        if st.soc is None or st.soc >= zc.target_soc:
            return 0
        max_c = int(min(zc.max_charge_w, DEVICE_MAX_W))
        if st.charge_w >= max_c - WRITE_DELTA_W:
            return 0
        return int(self.cfg.wallbox.battery_buffer_w)

    def _handle_wallbox_pv(
        self, device: dict, remaining: int, surplus: int, actual_device_w: dict[str, float],
        buffer_w: int = 0,
    ) -> Optional[tuple[int, dict]]:
        """Wallbox im PV-/Eco-Modus mit angestecktem Auto: Budget = Rest nach den Stufen davor.

        Laden mit passendem Strom (6–16 A) oder nach 5 min zu wenig pausieren (wallbox_pause.step; den
        go-e-``frc`` setzt der Controller). Sonst (kein Eco, kein Auto, go-e nicht lesbar) None → alter Weg,
        Pausen-Zustand zurückgesetzt."""
        raw = self._goe_raw()
        if self.goe is None or raw is None or raw.get("lmo") != 4 or raw.get("car") in (None, 1):
            if any(get_wallbox_pause().get(k) for k in ("paused", "below_since", "above_since", "run_since")):
                set_wallbox_pause({})
            if raw is not None and raw.get("lmo") is not None and raw.get("lmo") != 4:
                # Standard/Next Trip: manuell – nichts schalten. Ihre Leistung sieht der Hauptzähler schon
                # (nicht im Brutto, actual_device_w = 0); für die Anzeige die go-e-Messung.
                actual = float(actual_device_w.get(device["id"]) or 0.0)
                shown = self._wallbox_power_w() or 0.0
                action = "keep_on" if shown > 100 else "keep_off"
                reason = "Standard – manuell, PV Controller greift nicht ein"
                self._log(surplus, device["id"], action, reason, int(remaining - actual))
                log.info("Kaskade: Wallbox → %s, remaining=%d W | %s", action, remaining - actual, reason)
                return int(remaining - actual), self._result(
                    device, reason, action, "wallbox_not_eco_mode", {"lmo": raw.get("lmo"), "power_w": int(shown)})
            return None
        wb = self.cfg.wallbox
        min_w = WALLBOX_MIN_A * wb.eco_phase_count * WALLBOX_VOLT
        now = datetime.now()
        old = get_wallbox_pause()
        age = self._minutes_since(old.get("at")) * 60
        if age > wallbox_pause.STALE_S:
            old = {}                                     # Kaskade lief nicht: neu anfangen
        budget = remaining - buffer_w                    # Puffer: Batterie lädt noch nicht voll
        st, code, data = wallbox_pause.step(old, now, budget, min_w, charging=raw.get("car") == 2)
        data["buffer_w"] = buffer_w
        set_wallbox_pause({**st, "rest_w": data["rest_w"], "min_w": data["min_w"]})
        actual = float(actual_device_w.get(device["id"]) or 0.0)
        paused = st["paused"]
        if paused:
            action = "turn_off" if not old.get("paused") else "keep_off"
        else:
            action = "turn_on" if old.get("paused") else ("keep_off" if code == "wallbox_car_idle" else "keep_on")
            self._limit_wallbox_amp(budget)
        if bool(device["is_on"]) == paused:
            update_cascade_device_state(device["id"], not paused,
                                        turned_on_at=None if paused else now, turned_off_at=now if paused else None)
            device = {**device, "is_on": not paused}
        kw = lambda w: f"{w / 1000:.1f}".replace(".", ",") + " kW"
        rest, mn = data["rest_w"], data["min_w"]
        if code == "wallbox_paused_priority":
            reason = f"Wallbox pausiert – Batterie hat Vorrang (Rest {kw(rest)} < {kw(mn)})"
            if "remaining_s" in data:
                reason += f", Start frühestens in {data['remaining_s']} s"
        elif code == "wallbox_resume_pending":
            reason = f"Wallbox pausiert – Rest {kw(rest)} ≥ {kw(mn)}, Start in {data['remaining_s']} s"
        elif code == "wallbox_car_idle":
            reason = "Auto fertig/pausiert (Fahrzeug) – keine PV-Pause nötig"
        elif code == "wallbox_pause_pending":
            reason = f"Rest {kw(rest)} < {kw(mn)} – Wallbox lädt mit Mindeststrom, Pause in {data['remaining_s']} s"
        else:
            reason = f"Rest {kw(rest)} ≥ {kw(mn)} – Wallbox lädt mit passendem Strom"
        if buffer_w:
            reason += f" (Rest nach {buffer_w} W Puffer für die Batterie)"
        data["power_w"] = int(actual)
        self._log(surplus, device["id"], action, reason, int(remaining - actual))
        log.info("Kaskade: Wallbox → %s, remaining=%d W | %s", action, remaining - actual, reason)
        return int(remaining - actual), self._result(device, reason, action, code, data)

    def _limit_wallbox_amp(self, available_w: int) -> None:
        """Ladestrom des go-e (Eco, lmo=4) auf das begrenzen, was der Wallbox nach Batterie/Heizstab zusteht.

        Der go-e regelt im Eco-Modus selbst bis zu ``amp``; ohne Obergrenze nimmt er sich vor der Batterie,
        was eingespeist wird. Nie unter 6 A (darunter pausiert er selbst), kein frc-Umschalten. Nur wenn ein
        Auto angesteckt ist; ohne Schreibfreigabe (dry_run/enabled) nur Log."""
        raw = self._goe_raw()
        if self.goe is None or raw is None or raw.get("lmo") != 4 or raw.get("car") in (None, 1):
            return
        wb = self.cfg.wallbox
        try:
            current = int(raw.get("amp"))
        except (TypeError, ValueError):
            return
        cap = wallbox_amp_cap(available_w, wb.eco_phase_count, int(wb.eco_amps))
        if cap == current:
            return
        text = (f"Wallbox: Ladestrom {current} → {cap} A (zusteht {available_w} W, "
                f"{wb.eco_phase_count} Phase(n), Min {WALLBOX_MIN_A} A, Max {int(wb.eco_amps)} A)")
        if self.cfg.runtime.dry_run or not self.cfg.runtime.enabled:
            log.info("DRY-RUN/aus: %s – nicht gesetzt", text)
            return
        try:
            self.goe.set_amp(cap)
            self._goe_cache = None
            log.info("Kaskade: %s", text)
        except GoeError as e:
            log.warning("Kaskade: Wallbox-Ladestrom nicht gesetzt: %s", e)

    def _wallbox_charging(self) -> bool:
        """Lädt gerade ein Auto (go-e car=2 ODER > 100 W)? Bei Lesefehler konservativ True,
        damit die Batterie nicht ungeprüft entladen wird (Wert dieses Laufs, gecacht)."""
        if self.goe is None and self.goe_battery is None:
            return False
        raw = self._goe_raw()
        if raw is None:
            log.warning("Kaskade: Wallbox-Status für Batterie unbekannt – Entladen vorsichtshalber gesperrt")
            return True
        return car_charging(raw)

    def _read_storage_temp(self) -> Optional[float]:
        try:
            return self.storage.get_temperature_c()
        except Exception:
            log.warning("Kaskade: Speichertemperatur nicht lesbar", exc_info=True)
            return None

    def _now(self) -> datetime:
        return datetime.now(suntime.TZ)

    def _night_without_sun(self) -> bool:
        """Sonne sicher weg: nach Sonnenuntergang + NIGHT_MARGIN bzw. vor Sonnenaufgang − NIGHT_MARGIN."""
        if self.cfg.location is None:
            return False
        now = self._now()
        rise, sset = suntime.sun_times(now.date(), *self.cfg.location)
        if rise is None or sset is None:
            return False
        return now < rise - NIGHT_MARGIN or now > sset + NIGHT_MARGIN

    def _read_pv_total_w(self) -> Optional[float]:
        """Gesamt-PV als AC-Leistung (Haupt-WR + Pergola) – Solax-Lesewerte sind prozessweit gecacht.

        Haupt-WR nicht lesbar: nachts (Sonne sicher weg) zählt das als PV = 0 W, damit die Batterie mit dem
        Hauptzähler allein weiterregelt (Grundlast = Netzbezug + Entladung − Ladung) statt in den Fail-safe
        zu gehen. Tagsüber bleibt „nicht lesbar“ (None → Fail-safe): dort fehlt echte PV-Leistung."""
        pv = self.solax.get_pv_ac_w()
        if pv is None:
            if self._night_without_sun():
                log.debug("Kaskade: Haupt-WR nachts nicht lesbar – PV = 0 W (Batterie regelt mit dem Hauptzähler)")
                return 0.0
            return None
        if self.solax2 is not None:
            try:
                d = self.solax2.get_realtime_data()
                if d is not None:
                    pv += d.pv_ac_w
            except Exception:
                log.warning("Kaskade: Pergola-WR nicht lesbar", exc_info=True)
        return float(pv)

    def _handle_zendure(
        self, device: dict, avail_w: float, surplus: int, st, import_w: float,
        base_load_w: Optional[float], override: Optional[str],
        failsafe: Optional[str] = None, export_cap_w: Optional[float] = None,
    ) -> tuple[float, str]:
        """Batterie regeln; liefert (gewünschte Ladeleistung W, Begründung) – gewünscht, nicht
        gedeckelt, damit der Heizstab (dahinter) für die Batterie Platz macht."""
        wb = self._wallbox_charging()             # Sperre fest (nicht abschaltbar)
        # Netzbezug ohne die eigene Ladeleistung der Batterie (Laden zieht ja selbst am Zähler)
        if st is not None:
            import_w -= st.charge_w
        p = self.zendure_ctl.run(
            st, avail_w=max(0, avail_w), import_w=import_w, base_load_w=base_load_w,
            wallbox_charging=wb, override=override,
            hysteresis_w=int(device["hysteresis_watts"]), failsafe=failsafe, export_cap_w=export_cap_w,
            wallbox_check=lambda: charging_now(self.goe_battery),
        )
        charge_w = (p.want_w if p.want_w is not None else p.limit_w) if (p.mode == "charge" and st is not None) else 0

        now = datetime.now()
        active = p.mode != "idle" and st is not None
        if active != device["is_on"]:
            update_cascade_device_state(
                device["id"], active,
                turned_on_at=now if active else None, turned_off_at=None if active else now,
            )
        update_cascade_device_live_status(
            device["id"],
            (st.charge_w if st.state == "charging" else st.discharge_w) if st is not None else 0.0,
            st is not None,
        )
        self._last_plan = p
        self._log(surplus, device["id"], p.action, p.reason, int(avail_w - charge_w))
        log.info("Kaskade: %s → %s %d W, verfügbar %d W | %s", device["name"], p.mode, p.limit_w, avail_w, p.reason)
        return float(charge_w), p.reason

    # ── Entscheidungslogik ───────────────────────────────────────────────────

    def _decide(
        self, device: dict, surplus: int, remaining: int, running_w: Optional[float] = None
    ) -> tuple[str, str]:
        """Gibt (action, reason) zurück; Zustandsänderungen passieren beim Aufrufer.

        ``running_w``: gemessene Leistung eines laufenden Geräts (Wallbox) – zählt für das Weiterlaufen
        statt der Nennleistung, wenn sie höher ist."""
        power = device["power_watts"]
        if device["is_on"] and running_w is not None:
            power = max(int(power), int(running_w))
        hyst = device["hysteresis_watts"]

        if device["is_on"]:
            budget_after = remaining - power

            if budget_after < -hyst:
                on_min = self._minutes_since(device["turned_on_at"])
                if on_min >= device["min_on_minutes"]:
                    reason = (
                        f"Budget nach Abzug {power} W = {budget_after} W "
                        f"< -{hyst} W; Mindestlaufzeit {on_min:.0f}/{device['min_on_minutes']} min"
                    )
                    return "turn_off", reason
                else:
                    reason = (
                        f"Budget {budget_after} W < -{hyst} W, aber Mindestlaufzeit "
                        f"noch nicht erreicht ({on_min:.1f}/{device['min_on_minutes']} min)"
                    )
                    return "skip_min_on", reason
            else:
                return (
                    "keep_on",
                    f"Bleibt an: Budget nach Abzug {power} W = {budget_after} W >= -{hyst} W",
                )
        else:
            needed = power + hyst
            if remaining >= needed:
                off_min = self._minutes_since(device["turned_off_at"])
                if off_min >= device["min_off_minutes"]:
                    reason = (
                        f"Überschuss {remaining} W >= {needed} W "
                        f"({power} W + {hyst} W Hysterese)"
                    )
                    return "turn_on", reason
                else:
                    reason = (
                        f"Genug Überschuss ({remaining} W >= {needed} W), "
                        f"Mindestpause noch nicht erreicht "
                        f"({off_min:.1f}/{device['min_off_minutes']} min)"
                    )
                    return "skip_min_off", reason
            else:
                return (
                    "keep_off",
                    f"Zu wenig Überschuss: {remaining} W < {needed} W "
                    f"({power} W + {hyst} W Hysterese)",
                )

    def _reason_code(self, device: dict, action: str, remaining: int, surplus: int) -> tuple[str, dict]:
        """Grund-Code (reasons.py) zur Entscheidung aus ``_decide`` – nur Protokoll, entscheidet nichts."""
        power, hyst = device["power_watts"], device["hysteresis_watts"]
        if device["is_on"]:
            # läuft: Budget nach Abzug der eigenen Leistung; abgeschaltet wird erst unter −Hysterese
            data = {"rest_w": int(remaining - power), "power_w": power, "hysteresis_w": hyst}
            if action == "turn_off":
                return "budget_exhausted", data
            if action == "skip_min_on":
                on_min = self._minutes_since(device["turned_on_at"])
                data["remaining_s"] = int(round((device["min_on_minutes"] - on_min) * 60))
                return "min_on_time", data
            return "keep_running", data
        data = {"rest_w": int(remaining), "power_w": power, "threshold_w": power + hyst}
        if action == "skip_min_off":
            off_min = self._minutes_since(device["turned_off_at"])
            data["remaining_s"] = int(round((device["min_off_minutes"] - off_min) * 60))
            return "min_off_time", data
        if action == "turn_on":
            return "surplus_sufficient", data
        if surplus <= 0:
            return "no_surplus", data
        if remaining <= 0:
            return "not_reached", data
        return "below_start_threshold", data

    # ── Shelly-Polling ───────────────────────────────────────────────────────

    def _poll_shelly_devices(self) -> None:
        """Liest aktuellen Status aller aktiven Shelly-Geräte und speichert ihn."""
        for device in get_cascade_devices_due_for_retry():
            auto_reenable_cascade_device(device["id"])
            log.info(
                "Kaskade: %s nach %d h automatisch reaktiviert (Retry)",
                device["name"],
                _AUTO_RETRY_HOURS,
            )
            self._log(0, device["id"], "auto_reenabled", "Automatischer Retry", 0)

        shelly_devices = [
            d for d in get_cascade_devices()
            if d["enabled"] and d["type"] in ("shelly_gen1", "shelly_gen2")
        ]
        for device in shelly_devices:
            ip = device.get("ip_address")
            if not ip:
                continue
            try:
                status = self.shelly.get_status(ip, device["type"], device["shelly_channel"])
                update_cascade_device_live_status(device["id"], status["power"], True)
                reset_cascade_device_errors(device["id"])
                log.debug(
                    "Kaskade: Shelly %s erreichbar — %.0f W",
                    device["name"],
                    status["power"],
                )
            except ShellyCascadeError as e:
                errors = increment_cascade_device_errors(device["id"])
                log.warning(
                    "Kaskade: Shelly %s Fehler %d/%d — %s",
                    device["name"],
                    errors,
                    _MAX_ERRORS,
                    e,
                )
                if errors >= _MAX_ERRORS:
                    self._auto_disable(device)

    def _auto_disable(self, device: dict) -> None:
        """Deaktiviert ein Gerät nach zu vielen Fehlern; plant automatischen Retry."""
        retry_at = datetime.now() + timedelta(hours=_AUTO_RETRY_HOURS)
        update_cascade_device(device["id"], enabled=False)
        set_cascade_device_retry_after(device["id"], retry_at)
        reason = (
            f"Automatisch deaktiviert nach {_MAX_ERRORS} aufeinanderfolgenden Fehlern "
            f"(Retry um {retry_at.strftime('%H:%M')} Uhr)"
        )
        self._log(0, device["id"], "auto_disabled", reason, 0)
        log.error("Kaskade: %s — %s", device["name"], reason)

    # ── Gerätetyp-spezifische Aktionen ───────────────────────────────────────

    def _dispatch(self, device: dict, action: str) -> None:
        """Typ-spezifische Seiteneffekte nach einer Entscheidung."""
        if action not in ("turn_on", "turn_off"):
            return
        t = device["type"]
        if t in ("heizstab", "wallbox"):
            # Flag ist bereits in DB gesetzt; bestehender Controller liest es.
            pass
        elif t in ("shelly_gen1", "shelly_gen2"):
            ip = device.get("ip_address")
            if not ip:
                log.warning("Kaskade: Shelly %s hat keine IP-Adresse", device["name"])
                return
            if self.cfg.runtime.dry_run:
                # Entscheidung steht (DB/Log wie sonst), nur der Schaltbefehl entfällt
                log.info(
                    "DRY-RUN: Kaskade würde Shelly %s %s (%s, Kanal %s) – nicht geschaltet",
                    device["name"],
                    "einschalten" if action == "turn_on" else "ausschalten",
                    ip,
                    device["shelly_channel"],
                )
                return
            try:
                if action == "turn_on":
                    self.shelly.turn_on(ip, t, device["shelly_channel"])
                else:
                    self.shelly.turn_off(ip, t, device["shelly_channel"])
                log.info(
                    "Kaskade: Shelly %s %s",
                    device["name"],
                    "eingeschaltet" if action == "turn_on" else "ausgeschaltet",
                )
            except ShellyCascadeError as e:
                errors = increment_cascade_device_errors(device["id"])
                log.error(
                    "Kaskade: Shelly %s Schaltfehler %d/%d — %s",
                    device["name"],
                    errors,
                    _MAX_ERRORS,
                    e,
                )
                if errors >= _MAX_ERRORS:
                    self._auto_disable(device)

    # ── Hilfsmethoden ────────────────────────────────────────────────────────

    def _get_main_meter_feed_in_w(self) -> Optional[int]:
        """Hauptzähler als Einspeisung: positiv = Einspeisung, negativ = Bezug.
        Gibt None zurück bei Lesefehler (dann Fallback auf Solax).
        """
        try:
            val = self.main_meter.get_total_power_w()
            if val is not None:
                # Shelly 3EM: negativ = Einspeisung → invertieren für feed-in-Konvention
                return int(-val)
        except Exception:
            log.warning("Kaskade: Hauptzähler-Lesefehler", exc_info=True)
        return None

    def _get_raw_feed_in_w(self) -> int:
        """Fallback: feed_in_w aus Solax Data[34]; negativ bei Netzbezug."""
        try:
            data = self.solax.get_realtime_data()
            if data is not None:
                return int(data.feed_in_w)
        except Exception:
            log.warning("Kaskade: Solax-Lesefehler", exc_info=True)
        return 0

    def _heater_phase_reads(self) -> list[Optional[PhaseRead]]:
        """Zustand + Leistung der Heizstab-Phasen (stillgelegte = None), parallel, kurzes Timeout."""
        sh = self.cfg.shelly
        disabled = self.cfg.heater.disabled_phases
        plugs = [
            ShellyPlugClient(getattr(sh, f"ph{i}_url"), _METER_READ_TIMEOUT_S, name=f"PH{i}",
                             type_=getattr(sh, f"ph{i}_type"), channel=getattr(sh, f"ph{i}_channel"))
            for i in (1, 2, 3)
        ]
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="cascade-ph") as ex:
            futs = [None if disabled[i] else ex.submit(p.get_status) for i, p in enumerate(plugs)]
            return [None if f is None else PhaseRead(*f.result()) for f in futs]

    def _get_controlled_loads_w(self, devices: list[dict]) -> tuple[float, dict[str, float]]:
        """Liest aktuelle Leistung der kaskaden-gesteuerten Festlasten.

        Heizstab-Leistung wird vom Shelly 3EM gemessen; Wallbox vom go-e nrg[11].
        Bei Lesefehlern: Fallback auf power_watts aus DB wenn Gerät laut DB an ist.

        Gibt (total_w, per_device) zurück. per_device mappt device-id auf gemessene Watt.
        Damit kann der Budget-Loop echte statt nominale Leistung abziehen und verhindert
        so Phantom-Reservierungen (z.B. Heizstab is_on=1 aber Controller blockiert ihn).
        """
        total = 0.0
        per_device: dict[str, float] = {}

        if any(d["type"] == "heizstab" for d in devices):
            try:
                power = self.heater_meter.get_total_power_w()
            except Exception:
                log.warning("Kaskade: Heizstab-Meter-Lesefehler", exc_info=True)
                power = None
            if power is not None:
                heizstab_actual = max(0.0, power)
                log.debug("Kaskade: Heizstab-Ist %.0f W", heizstab_actual)
            else:
                # Zähler tot: Summe der Phasen-Shellys (wie der Controller), nie stillschweigend 0 W
                hp = resolve_heater_power(None, self._heater_phase_reads(), self.cfg.heater.phase_power_w,
                                          get_last_phase_states())
                heizstab_actual = hp.watts
                log.warning("Kaskade: Heizstab-Zähler nicht lesbar – Ersatzwert aus Phasen %.0f W (%s)",
                            hp.watts, hp.detail)
            total += heizstab_actual
            for d in devices:
                if d["type"] == "heizstab":
                    per_device[d["id"]] = heizstab_actual

        if any(d["type"] == "wallbox" for d in devices) and self.goe is not None:
            wallbox_actual: float | None = None
            try:
                raw = self._goe_raw()
                if raw is None:
                    raise GoeError("Wallbox-Status nicht lesbar")
                lmo = raw.get("lmo")
                if lmo != 4:
                    # Basic-Modus (lmo=3): Wallbox nicht kaskaden-gesteuert.
                    # Der Hauptzähler erfasst ihren Verbrauch bereits; hier nicht
                    # addieren, sonst wird der Überschuss fälschlich aufgeblasen.
                    log.debug(
                        "Kaskade: Wallbox im Basic-Modus (lmo=%s) – "
                        "nicht als Kaskadenlast gerechnet", lmo,
                    )
                    wallbox_actual = 0.0
                else:
                    nrg = raw.get("nrg")
                    if isinstance(nrg, list) and len(nrg) >= 12 and isinstance(nrg[11], (int, float)):
                        wallbox_actual = max(0.0, float(nrg[11]))
                        total += wallbox_actual
                        log.debug("Kaskade: Wallbox-Ist %.0f W (Eco-Modus)", wallbox_actual)
            except (GoeError, Exception):
                log.warning("Kaskade: Wallbox-Meter-Lesefehler – nutze DB-Wert", exc_info=True)
                for d in devices:
                    if d["type"] == "wallbox" and d["is_on"]:
                        total += d["power_watts"]
                        wallbox_actual = float(d["power_watts"])
            for d in devices:
                if d["type"] == "wallbox":
                    per_device[d["id"]] = wallbox_actual if wallbox_actual is not None else 0.0

        return total, per_device

    def _get_surplus_w(self) -> int:
        return max(0, self._get_raw_feed_in_w())

    def _active_override(self, device: dict) -> Optional[str]:
        """Gibt die aktive manuelle Übersteuerung zurück oder None wenn abgelaufen/keine."""
        action = device.get("manual_override_action")
        until_str = device.get("manual_override_until")
        if not action or not until_str:
            return None
        try:
            until = datetime.fromisoformat(until_str)
            if datetime.now() <= until:
                return action
        except ValueError:
            pass
        clear_cascade_override(device["id"])
        return None

    @staticmethod
    def _minutes_since(ts: Optional[str]) -> float:
        """Minuten seit einem ISO-Timestamp; 9999 wenn kein Timestamp vorhanden."""
        if not ts:
            return 9999.0
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
        except ValueError:
            return 9999.0

    @staticmethod
    def _result(device: dict, reason: str, action: str = "", code: str = "", data: Optional[dict] = None) -> dict:
        return {
            "id": device["id"],
            "name": device["name"],
            "type": device["type"],
            "is_on": device["is_on"],
            "reason": reason,
            "action": action,
            "code": code,            # Grund-Code (reasons.py) für den Entscheidungs-Schnappschuss
            "data": data or {},
        }
