"""Grund-Codes für Entscheidungen (Kaskade, Heizstab, Batterie, Wallbox, Zeitpläne, Geräte).

Jede Entscheidung bekommt **zusätzlich** zum bestehenden Freitext einen festen Code und Zahlen
(``data``). Der Code steht im Entscheidungs-Schnappschuss (``decisions.py``) und im Zeitplan-Verlauf
(``schedule_log.py``); die Kurztexte hier sind die deutsche Übersetzung für Anzeige und Chat.

Die Codes sind eine feste Liste: ein unbekannter Code ist ein Programmfehler (Test prüft, dass jeder
vergebene Code hier steht). Platzhalter ``{name}`` werden aus ``data`` gefüllt; fehlt ein Wert,
steht dort „?“ – nie ein Absturz.
"""
from __future__ import annotations

from typing import Any, Optional

REASONS: dict[str, str] = {
    # ── Überschuss / Budget (Kaskade, Heizstab) ──────────────────────────────
    "surplus_sufficient": "Überschuss reicht ({rest_w} W verfügbar, {threshold_w} W nötig)",
    "keep_running": "Bleibt an: nach Abzug der eigenen {power_w} W bleiben {rest_w} W "
                    "(abgeschaltet wird erst unter −{hysteresis_w} W)",
    "below_start_threshold": "Überschuss {rest_w} W unter der Startschwelle {threshold_w} W",
    "below_stop_threshold": "Budget {budget_w} W reicht nicht mehr für {running} Phase(n) – Abschalten",
    "budget_exhausted": "Überschuss reicht nicht mehr: nach Abzug der eigenen {power_w} W bleiben {rest_w} W",
    "not_reached": "Kein Überschuss mehr übrig – Geräte davor in der Reihenfolge haben alles verbraucht",
    "no_surplus": "Kein PV-Überschuss",
    "smoothing_wait": "Überschuss jetzt {raw_w} W würde reichen, das {smooth_min}-min-Mittel {budget_w} W "
                      "liegt noch unter der Startschwelle {threshold_w} W",
    "budget_available": "Budget {budget_w} W erlaubt bis zu {target_phases} Phase(n)",
    "grid_assist": "Budget {budget_w} W erlaubt {target_phases} Phase(n) mit bis zu {grid_assist_w} W Netz-Zuschuss",
    "priority_first": "Batterie hat im Profil Vorrang vor dem Heizstab und lädt mit {watts} W",
    "min_surplus_not_reached": "Überschuss {surplus_w} W unter dem Mindestüberschuss {min_surplus_w} W",
    "grid_draw_forbidden": "Netzbezug nicht erlaubt und brutto {surplus_w} W – alles aus",
    # ── Zeiten ───────────────────────────────────────────────────────────────
    "min_on_time": "Mindestlaufzeit läuft noch ({remaining_s} s)",
    "min_off_time": "Mindestpause läuft noch ({remaining_s} s)",
    "one_phase_per_run": "Pro Minute wird nur eine Phase zugeschaltet – folgt im nächsten Lauf",
    "mode_change_pause": "Pause nach Moduswechsel ({remaining_s} s), danach {next_mode}",
    # ── Heizstab: Temperatur, Sommer, Phasen ─────────────────────────────────
    "max_temp_reached": "Speicher {temp_c} °C hat die Maximaltemperatur {max_temp_c} °C erreicht",
    "hysteresis_band": "Speicher {temp_c} °C liegt über der Wiedereinschalt-Temperatur {resume_temp_c} °C – "
                       "kein neuer Heizzyklus",
    "temp_unknown": "Speichertemperatur nicht lesbar – Heizstab aus",
    "heater_thermostat_open": "Thermostat im Heizstab hat abgeschaltet (Relais an, 0 W seit {open_min} min, "
                              "Speicher {temp_c} °C) – Überschuss geht an Batterie/Wallbox",
    "storage_practically_full": "Speicher praktisch voll: Thermostat im Heizstab {open_min} min offen bei "
                                "{temp_c} °C – Heizstab aus, Freigabe ab {release_c} °C",
    "summer_mode_grid_heat": "Sommermodus: Speicher {temp_c} °C unter Mindesttemperatur – Heizen aus dem Netz",
    "summer_mode_stop": "Sommermodus: Netz-Heizen beendet",
    "phase_disabled": "Phase ist in den Einstellungen stillgelegt",
    "phase_unknown": "Phase nicht erreichbar – Zustand unbekannt, wird vorsichtshalber als laufend gezählt",
    "phase_unknown_off_sent": "Phase nicht erreichbar – AUS wird vorsichtshalber gesendet",
    "failsafe_global": "Fail-safe: {unreadable} nicht lesbar – keine überschussbasierten Schaltbefehle",
    "failsafe_phase": "Phase nicht erreichbar – AN wird nie ohne bekannten Zustand gesendet",
    "manual_override": "Manuelle Übersteuerung ({mode}) aktiv",
    "controller_disabled": "Steuerung ist ausgeschaltet (nur Beobachten)",
    "cascade_disabled": "Kaskade ist ausgeschaltet",
    # ── Batterie ─────────────────────────────────────────────────────────────
    "charging_surplus": "Batterie lädt mit {watts} W aus PV-Überschuss",
    "soc_target_reached": "Batterie hat das Ziel erreicht (SoC {soc} % ≥ {target_soc} %) – kein Laden",
    "battery_empty": "Batterie leer (SoC {soc} % ≤ Mindest-SoC {min_soc} %)",
    "discharging_base_load": "Batterie deckt die Grundlast mit {watts} W",
    "no_discharge_controlled_load": "Netzbezug {import_w} W gehört zu Heizstab/Wallbox – Batterie entlädt nicht",
    "standby_no_import": "Kein Netzbezug – Batterie im Standby",
    "base_load_unknown": "Grundlast unbekannt (PV oder Hauszähler nicht lesbar) – kein Entladen",
    "wallbox_lock": "Entladen gesperrt, weil die Wallbox lädt",
    "write_failed": "Befehl an die Batterie fehlgeschlagen – neuer Versuch im nächsten Lauf",
    "not_in_cascade": "Nicht in der Kaskade eingerichtet",
    # ── Wallbox ──────────────────────────────────────────────────────────────
    "wallbox_disabled": "Wallbox-Steuerung ist in den Einstellungen aus",
    "not_connected": "Kein Auto angesteckt",
    "wallbox_not_eco_mode": "Wallbox ist nicht im PV-Überschussmodus (Eco) – manuelles Laden wird nicht angefasst",
    "car_full": "Laden abgeschlossen – Auto ist voll oder hat das Laden beendet",
    "waiting_for_car": "Wallbox freigegeben, wartet aber: zu wenig Überschuss für den Mindeststrom, "
                       "Freigabe in der App fehlt oder das Auto nimmt nichts",
    "charging": "Auto lädt mit {power_w} W im PV-Überschussmodus (Eco)",
    "charging_manual": "Auto lädt mit {power_w} W im normalen Modus – nicht PV-geführt, wird nicht angefasst",
    "wallbox_released": "Wallbox freigegeben – sie regelt den Ladestrom selbst nach Überschuss",
    # ── Geräte ───────────────────────────────────────────────────────────────
    "device_unreachable": "Gerät nicht erreichbar – keine Änderung",
    "auto_disabled": "Nach wiederholten Fehlern automatisch deaktiviert",
    "expected_offline": "Erwartet offline (keine Sonne)",
    # ── Zeitpläne ────────────────────────────────────────────────────────────
    "sent": "Gesendet",
    "already_set": "War schon eingestellt – nichts gesendet",
    "dry_run": "Testbetrieb (Dry-Run) – nicht gesendet",
    "superseded": "Durch einen späteren fälligen Eintrag ersetzt",
    "skipped_late": "Übersprungen: {late_min} min verspätet (höchstens {max_min})",
    "skipped_no_sun": "Übersprungen: Sonnenzeit nicht berechenbar (Koordinaten fehlen)",
    "skipped_schedules_off": "Nicht ausgeführt: Zeitpläne sind ausgeschaltet",
    "skipped_automation_openhab": "Nicht ausgeführt: Rollladen wird von openHAB gesteuert",
    "skipped_duty_cycle": "Pausiert: Funk-Duty-Cycle {duty_cycle} % über {max} %",
    "skipped_door_open": "Übersprungen: Tür {door} – automatisches Schließen wird nicht nachgeholt",
    "waiting_door": "Wartet: Tür {door} – schließt, sobald die Tür zu ist",
    "door_wait_expired": "Warten auf die geschlossene Tür nach {hours} h beendet – nicht geschlossen",
    "skipped_frost": "Übersprungen: Außentemperatur {outdoor_c} °C – Öffnen bei Frost gesperrt",
    "skipped_manual_hold": "Übersprungen: von Hand bewegt ({manual_at}), Vorrang {hold_min} min",
    "history_unreadable": "Übersprungen: Verlauf nicht lesbar – manuelle Bedienung nicht prüfbar",
    "limited_door_gap": "Tür ohne Kontakt: automatisch nur bis {limited_to} % statt {target} %",
    "deferred_moving": "Rollladen fährt gerade – nächste Minute erneut",
    "deferred_time_budget": "Zeitbudget des Laufs erschöpft – nächste Minute erneut",
    "openhab_error": "openHAB-Fehler beim Senden",
    "openhab_retry": "openHAB-Fehler beim Senden – ein weiterer Versuch in der nächsten Minute",
    "openhab_unreachable": "openHAB nicht erreichbar – Eintrag bleibt fällig",
    "readback_mismatch": "Rückprüfung: Gerät meldet {reported} statt {expected}",
    "readback_retry": "Rückprüfung: Wert zurückgesprungen ({reported} statt {expected}) – einmal erneut gesendet",
}


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def _fmt(v: Any) -> Any:
    if isinstance(v, float):
        return f"{v:g}".replace(".", ",")
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return v


def is_known(code: str) -> bool:
    return code in REASONS


def text(code: Optional[str], data: Optional[dict[str, Any]] = None) -> str:
    """Deutscher Kurztext zu einem Code; unbekannter Code → der Code selbst."""
    if not code:
        return ""
    tpl = REASONS.get(code)
    if tpl is None:
        return str(code)
    try:
        return tpl.format_map(_Missing({k: _fmt(v) for k, v in (data or {}).items()}))
    except (ValueError, IndexError):
        return tpl
