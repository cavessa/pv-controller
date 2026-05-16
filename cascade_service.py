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
from datetime import datetime, timedelta
from typing import Optional

from clients.goe_client import GoeClient, GoeError
from clients.shelly_cascade_client import ShellyCascadeClient, ShellyCascadeError
from clients.shelly_client import Shelly3EMClient
from clients.solax_client import SolaxClient
from config import Config
from db import (
    auto_reenable_cascade_device,
    clear_cascade_override,
    get_cascade_device,
    get_cascade_devices,
    get_cascade_devices_due_for_retry,
    get_cascade_settings,
    increment_cascade_device_errors,
    log_cascade_action,
    reset_cascade_device_errors,
    set_cascade_device_retry_after,
    update_cascade_device,
    update_cascade_device_live_status,
    update_cascade_device_state,
)

_MAX_ERRORS = 5
_AUTO_RETRY_HOURS = 1

log = logging.getLogger(__name__)


class CascadeService:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        timeout = config.runtime.request_timeout_seconds
        self.solax = SolaxClient(config.solax.url, config.solax.pwd, timeout)
        self.shelly = ShellyCascadeClient()
        self.heater_meter = Shelly3EMClient(
            config.shelly.heater_meter_url, timeout, name="heater_meter"
        )
        self.main_meter = Shelly3EMClient(
            config.shelly.main_meter_url, timeout, name="main_meter"
        )
        self.goe: Optional[GoeClient] = None
        if config.wallbox.enabled and config.wallbox.url:
            self.goe = GoeClient(config.wallbox.url, config.wallbox.request_timeout_seconds)

    # ── Hauptmethode ─────────────────────────────────────────────────────────

    def run_cascade(self) -> dict:
        """Liest Überschuss, iteriert durch Geräte (nach priority) und entscheidet."""
        settings = get_cascade_settings()

        if not settings["cascade_enabled"]:
            log.info("Kaskade: deaktiviert (cascade_enabled=False)")
            return {"surplus_watts": 0, "devices": [], "disabled": True}

        self._poll_shelly_devices()
        devices = [d for d in get_cascade_devices() if d["enabled"]]

        # Primärquelle: Hauptzähler (sieht alle Lasten inkl. Wallbox im Basic-Modus).
        # Fallback auf Solax wenn Hauptzähler nicht erreichbar.
        main_feed_in = self._get_main_meter_feed_in_w()
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
        gross_feed_in = raw_feed_in + int(controlled_loads_w)
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
            "=== Kaskade Start: Überschuss=%d W (effektiv=%d W), %d aktive Geräte ===",
            surplus,
            effective_surplus,
            len(devices),
        )

        remaining = effective_surplus
        results: list[dict] = []

        for device in devices:
            override = self._active_override(device)

            if override is not None:
                if device["is_on"]:
                    # Gemessene statt nominale Leistung abziehen (Phantom-Budget vermeiden)
                    remaining -= int(actual_device_w.get(device["id"], device["power_watts"]))
                log_cascade_action(
                    surplus,
                    device["id"],
                    "manual_override",
                    f"Manuelle Übersteuerung: {override} aktiv bis {device['manual_override_until']}",
                    remaining,
                )
                log.info(
                    "Kaskade: %s → manual_override (%s), remaining=%d W",
                    device["name"],
                    override,
                    remaining,
                )
                # Shelly in den gewünschten Zustand bringen (DB-Flag allein reicht nicht)
                self._dispatch(device, "turn_on" if override == "on" else "turn_off")
                results.append(self._result(device, f"manual_override ({override})"))
                continue

            action, reason = self._decide(device, effective_surplus, remaining)

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

            log_cascade_action(surplus, device["id"], action, reason, remaining)
            log.info(
                "Kaskade: %s → %s, remaining=%d W | %s",
                device["name"],
                action,
                remaining,
                reason,
            )

            self._dispatch(device, action)
            results.append(self._result(device, reason))

        log.info("=== Kaskade Ende: remaining=%d W ===", remaining)
        return {"surplus_watts": surplus, "devices": results}

    # ── Entscheidungslogik ───────────────────────────────────────────────────

    def _decide(
        self, device: dict, surplus: int, remaining: int
    ) -> tuple[str, str]:
        """Gibt (action, reason) zurück; Zustandsänderungen passieren beim Aufrufer."""
        power = device["power_watts"]
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
            log_cascade_action(0, device["id"], "auto_reenabled", "Automatischer Retry", 0)

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
        log_cascade_action(0, device["id"], "auto_disabled", reason, 0)
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
            heizstab_actual: float | None = None
            try:
                power = self.heater_meter.get_total_power_w()
                if power is not None:
                    heizstab_actual = max(0.0, power)
                    total += heizstab_actual
                    log.debug("Kaskade: Heizstab-Ist %.0f W", heizstab_actual)
                else:
                    raise ValueError("3EM returned None")
            except Exception:
                log.warning("Kaskade: Heizstab-Meter-Lesefehler – nutze DB-Wert", exc_info=True)
                for d in devices:
                    if d["type"] == "heizstab" and d["is_on"]:
                        total += d["power_watts"]
                        heizstab_actual = float(d["power_watts"])
            for d in devices:
                if d["type"] == "heizstab":
                    per_device[d["id"]] = heizstab_actual if heizstab_actual is not None else 0.0

        if any(d["type"] == "wallbox" for d in devices) and self.goe is not None:
            wallbox_actual: float | None = None
            try:
                raw = self.goe.get_status()
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
    def _result(device: dict, reason: str) -> dict:
        return {
            "id": device["id"],
            "name": device["name"],
            "is_on": device["is_on"],
            "reason": reason,
        }
