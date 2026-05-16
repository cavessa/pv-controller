# Changelog

## 2026-05-16 – Prognose-Trefferquote: heutiger Tag + Schwellwert

**Problem 1:** Der laufende Tag wurde in der Prognose-Tabelle und im "Letzte Tage"-Widget mit ✅/❌ bewertet, obwohl der Tagesertrag noch nicht final ist.

**Problem 2:** Schwellwert ±20% war zu streng für Wetterprognosen.

**Fix:**
- Backend (`db.py`): Heutiger Tag bekommt `hit = None` statt True/False. Schwellwert `<= 20` → `<= 25`.
- Frontend "Letzte Tage"-Widget (`app.js`): Heute zeigt ⏳ (grau) statt ✅/❌; Label "Heute" statt Datum.
- Frontend Prognose-Tabelle: `hit === null` → ⏳, kein roter Zeilenhintergrund für heute.
- KPI-Label: "Treffer ±20%" → "Treffer ±25%".

---

## 2026-05-16 – Kaskade: Automatischer Retry nach Auto-Deaktivierung

**Problem:** Wenn ein Shelly-Gerät (z.B. eBike) 5 aufeinanderfolgende Kommunikationsfehler produzierte, wurde es dauerhaft deaktiviert (`enabled=0`) ohne je wieder automatisch reaktiviert zu werden.

**Fix:**
- Neue DB-Spalte `retry_after` in `cascade_devices` (mit Migration für bestehende Tabellen).
- `_auto_disable` setzt jetzt `retry_after = now + 1 Stunde` (konfigurierbar via `_AUTO_RETRY_HOURS`).
- `_poll_shelly_devices` prüft zu Beginn jedes Zyklus ob Geräte fällig sind und reaktiviert sie automatisch (Fehlerzähler zurückgesetzt, Aktion `auto_reenabled` ins Log).
- Bestehendes blockiertes eBike (`shelly_test`) sofort per `auto_reenable_cascade_device` reaktiviert.

---

## 2026-05-14 – Heizstab Phase 3: Lesefehler nicht mehr fatal

**Problem:** Konnte `ph3_state` nicht gelesen werden, wurde der Fehler in `readings.errors` eingetragen.
Das löste den globalen Fail-safe aus und blockierte PH1/PH2 ebenfalls.

**Fix:** `ph3_state`-Lesefehler wird nur noch als Warning geloggt, nicht mehr in `errors` eingetragen.
Phase 3 bleibt bei unbekanntem Zustand UNCHANGED (`_decide_phase` gibt bereits UNCHANGED zurück wenn `cur is None`).
PH1 und PH2 laufen weiter normal.

---

## 2026-05-14 – Tab-Leiste: nur horizontal scrollbar

`overflow-y: hidden` und `white-space: nowrap` zur bestehenden `.tabs`-Regel hinzugefügt.
Tabs bleiben in einer Zeile und scrollen nur seitwärts, nicht vertikal.

---

## 2026-05-14 – Bugfix: Netz Sub-Tabs Woche/Monat/Jahr zeigten leere Seite

**Problem:** Klick auf Woche/Monat/Jahr im Netz-Sub-Tab rief `showVerlaufSection(undefined)` auf,
weil die Sub-Tab-Buttons die Klasse `verlauf-nav-btn` tragen und vom globalen Click-Handler
erfasst wurden. Das versteckte den gesamten `verlauf-netz`-Container bevor der Chart gerendert wurde.

**Fix:** Selector in `boot()` auf `.verlauf-nav-btn[data-vsec]` eingeschränkt –
identisch mit der bestehenden Guard in `showVerlaufSection` selbst.

---

## 2026-05-14 – Verlauf: Netz-Tab (Einspeisung vs. Bezug)

Neuer Sub-Tab **NETZ** im Verlauf, nur sichtbar wenn `shelly.main_meter_url` konfiguriert ist.

**Features:**
- Vier Ansichten: Tag (stündlich), Woche (7 Tage), Monat, Jahr
- Bi-direktionale SVG-Balkendiagramme: Einspeisung (grün, nach oben) / Bezug (orange, nach unten)
- Navigation mit Pfeilen (Tag/Woche/Monat/Jahr)
- KPI-Boxen: Einspeisung kWh, Bezug kWh, Saldo (grün=netto Einspeisung, rot=netto Bezug)

**Datenquellen:**
- Stündlich: `feed_in_w` aus `pv_hourly_log` (positiv=Einspeisung, negativ=Bezug)
- Täglich/monatlich: `feed_out_kwh` / `feed_in_kwh` aus `pv_daily_log`

**Neue API-Endpunkte:**
- `GET /api/history/grid/today`
- `GET /api/history/grid/day/:date`
- `GET /api/history/grid/week?end_date=...`
- `GET /api/history/grid/month/:YYYY-MM`
- `GET /api/history/grid/year/:YYYY`

**Neue DB-Funktion:** `get_grid_week_data(end_date)` für navigierbare Wochenansicht.

## 2026-05-13 – Kaskade: Überschuss aus Hauptzähler; Wallbox im Basic-Modus ignoriert

**Problem:** Die Kaskade hat das eBike-Shelly eingeschaltet obwohl die Wallbox im Basic-Modus 4,86 kW zog und der Haushalt 4,37 kW aus dem Netz bezog. Ursache: `_get_controlled_loads_w` addierte die Wallbox-Leistung zum Feed-in zurück, auch wenn die Wallbox nicht kaskaden-gesteuert war (lmo=3 / Basic). Das ergab einen falschen Brutto-Überschuss von +490 W.

**Fix:**
- `CascadeService` liest die Einspeisung jetzt primär vom **Hauptzähler** (Shelly 3EM, `config.shelly.main_meter_url`) statt vom Solax. Der Hauptzähler sieht alle Lasten inkl. Wallbox im Basic-Modus. Solax bleibt als Fallback bei Lesefehler.
- **Wallbox im Basic-Modus** (`lmo != 4`): `_get_controlled_loads_w` addiert die Wallbox-Leistung **nicht mehr** zum kontrollierten-Lasten-Budget. Da der Hauptzähler die Wallbox bereits erfasst, würde doppeltes Addieren den Überschuss fälschlich aufblasen.
- Bei aktivem Netzbezug (gross_feed_in < 0) und `allow_grid_draw=False` erzwingt die bestehende Logik weiterhin die Abschaltung aller Kaskaden-Geräte.

## 2026-05-13 – Wallbox: Stromstärke auf 7A zurücksetzen beim Abstecken

Wenn das Auto abgesteckt wird (`car=1`), setzt der Controller `amp=7A` (zusammen mit dem Eco-Restore).

## 2026-05-13 – Wallbox: Basic/Eco-Button im Dashboard

Unter der Wallbox-Card gibt es jetzt zwei Buttons ("Basic" / "Eco") zum direkten Umschalten des Lademodus. Der aktive Modus wird hervorgehoben. Außerdem zeigt "Technische Details" jetzt den Modus (lmo) statt des entfernten acs-Felds.

## 2026-05-13 – Wallbox: Auto-Restore Eco-Modus beim Abstecken

Wenn das Auto abgesteckt wird (`car=1`) und die Wallbox im Basic-Modus läuft (`lmo=3`), setzt der Controller automatisch `lmo=4` (Eco). Beim nächsten Einstecken steht die Wallbox dann wieder im PV-Überschussmodus.

## 2026-05-13 – Wallbox: Zugangskontrolle (acs) entfernt

Auto-Unlock-Logik (`_maybe_set_unlock`, `_execute_unlock`) und alle acs-bezogenen Felder aus Controller, Models, Client und Web entfernt. Die go-e Wallbox läuft dauerhaft im Basic-Modus ohne Zugangskontrolle.

## 2026-05-13 – Wallbox: lmo-Wert für Eco-Modus korrigiert

`lmo=3` ist Standard/Basic-Modus (nicht Eco). Eco/PV-Überschuss ist `lmo=4`. Bugfix: Controller greift jetzt nur noch bei `lmo=4` ein.

- **clients/goe_client.py**: `lmo` in STATUS_FILTER ergänzt
- **models.py**: `WallboxStatus.logic_mode` Feld hinzugefügt
- **controller.py**: `pv_surplus_active` basiert jetzt auf `lmo == 3`; Log zeigt `lmo` und eco-Flag

## 2026-05-12 – PV Erzeugung: Tageshoch anzeigen

Unter dem aktuellen PV-Wert in der Card "PV Erzeugung" wird jetzt das bisherige Tageshoch angezeigt ("Tageshoch: 10823 W um 12:34"). Der Peak-Wert wird im Frontend pro Polling-Zyklus aktualisiert und bei Mitternacht zurückgesetzt. Nachts (PV = 0 und kein Peak erfasst) erscheint "Tageshoch: –".

- **web/app.js**: Globale Vars `pvPeakToday` / `pvPeakTime`; Peak-Tracking und Mitternachts-Reset in `renderSolax`; `pv-peak-display`-Element wird befüllt
- **web/index.html**: `<div id="pv-peak-display">` unter `pv-live-total` eingefügt

## 2026-05-12 – Sommermodus: Max-Phasen-Einstellung entfernt

Netz-Heizung nutzt immer alle 3 Phasen (~4,5 kW) damit das Wasser so schnell wie möglich warm wird. Das Feld "Max. Phasen bei Netzbezug" aus Settings-UI, Config und API entfernt. Alte `config.json` mit `max_phases`-Feld laden weiterhin fehlerfrei (Feld wird ignoriert).

- **config.py** `SummerModeConfig`: `max_phases` entfernt; `load_config` filtert altes Feld heraus
- **controller.py** `_check_summer_mode` + `run()`: `i < max_phases` → alle 3 Phasen
- **web.py**: `max_phases` aus `_ALLOWED_SUMMER_MODE_KEYS`, Serialisierung und Validierung entfernt
- **web/index.html**: Dropdown + Hinweistext entfernt
- **web/app.js**: Feld aus `loadSettings` und `saveSettings` entfernt

## 2026-05-12 – Sommermodus: Heizstab als Backup aus dem Netz

Im Sommer (Ölheizung aus) kann der Heizstab jetzt die Warmwasserbereitung vollständig übernehmen – primär per PV-Überschuss, bei Bedarf aus dem Netz.

### Neue Einstellungen (`config.json` / Settings-UI)
- **`summer_mode.enabled`** – Schalter für den Sommermodus (Default: `false`)
- **`summer_mode.min_temp`** – Unter dieser Temperatur startet die Netz-Heizung (Default: 45 °C)
- **`summer_mode.target_temp`** – Bis hierhin heizen wenn aus Netz (Default: 52 °C)
- **`summer_mode.max_phases`** – Maximale Phasen beim Netzbezug: 1 (~1,5 kW), 2 (~3 kW), 3 (~4,5 kW) (Default: 1)

### Logik (`controller.py`, `config.py`, `models.py`)
- `SummerModeConfig` Dataclass mit Validierung (min_temp < target_temp, max_phases ∈ {1,2,3})
- `Controller._check_summer_mode()`: Prüft vor den Phasen-Entscheidungen ob Netz-Heizung nötig ist
  - Heizt wenn `temp < min_temp` und kein PV-Überschuss (Einspeisung ≤ 200 W)
  - Heizt weiter wenn `min_temp ≤ temp < target_temp` und Phasen bereits an
  - Stoppt wenn `temp ≥ target_temp` und Phasen an ohne PV
  - Wird von `temp_status AT_OR_ABOVE_MAX` und signifikantem PV-Überschuss overruled
- Sommermodus überstimmt die Kaskaden-Sperre (Kaskade verwaltet PV-Verteilung; Sommermodus ist Notfall-Backup)
- `ControllerResult` um `summer_mode_heating` und `summer_mode_reason` erweitert

### API (`web.py`)
- `/api/status` liefert jetzt `summer_mode`-Objekt mit `enabled`, `min_temp`, `target_temp`, `max_phases`, `heating`, `reason`
- `_summary()`: Wenn Netz-Heizung aktiv → `state="Netzbezug-Heizung"`, `severity="warn"`
- `/api/config` PUT: `summer_mode`-Sektion vollständig konfigurierbar

### Dashboard (`web/index.html`, `web/app.js`)
- Neues Badge `[Sommermodus: aktiv]` / `[Sommermodus: Netz-Heizung]` in der Toolbar (orange)
- Hinweis-Box in der Speicher-Card (zeigt Mindest- und Zieltemperatur, hebt Netz-Heizung hervor)
- Settings-Tab: neue Sektion "Sommermodus" zwischen Heizstab & Speicher und Wallbox

## 2026-05-12 – Prognose-Einschätzung: Glockenkurven-Modell statt linearem Durchschnitt

- **app.js** `getForecastAssessment`: Frühmorgens ist `avgPerHour` niedrig (normale Morgen-Sonne), der alte `* 0.5`-Abschlag führte fälschlicherweise zu "Wird nicht mehr erreicht"
- Neu: Halbsinus-Modell (`(1 − cos(π · t)) / 2`) schätzt welcher Anteil der Tagesenergie bis jetzt erwartet wird; Hochrechnung auf Tagesgesamt ergibt realistische Einschätzung
- Morgens (< 15 % der Tagesenergie erwartet, ca. bis 09:45): Hochrechnung zu unzuverlässig → "Auf Kurs" wenn irgendeine Produktion vorhanden, "Tag hat gerade begonnen" wenn noch 0 kWh

## 2026-05-12 – Temperaturchart: Cron-Lauf schreibt jetzt auch temp_history.json

- **db.py**: Funktion `append_temp_history` aus `web.py` hierhin verschoben, damit sie ohne FastAPI-Import nutzbar ist
- **web.py** `_append_history`: delegiert jetzt an `db.append_temp_history`
- **main.py**: ruft nach jedem Controller-Lauf `append_temp_history` auf — damit wird `temp_history.json` jede Minute per Cron befüllt, unabhängig davon ob jemand das Dashboard geöffnet hat

## 2026-05-12 – Temperaturchart: Stundendaten als Fallback wenn Minutendaten fehlen

- **web.py** `api_temp_history`: Wenn `temp_history.json` für eine Stunde keine Minutendaten enthält (z. B. nach Dienst-Neustart), werden fehlende Stunden aus `pv_hourly_log` (`storage_temp_c`, `wallbox_w`) ergänzt. Der Chart zeigt damit auch nach einem Neustart den vollen Tagesverlauf.

## 2026-05-10 – "Aktualisieren"-Button entfernt

- **index.html**: Button `#btn-refresh` entfernt (Dashboard pollt automatisch)
- **app.js**: Event-Listener für `#btn-refresh` entfernt

## 2026-05-10 – Bugfix: String-Leistung Chart (täglich) war leer

- **app.js** `renderStringsDayChart`: `e.timestamp` → `e.ts` korrigiert; API liefert das Feld als `ts`, deshalb wurde `new Date(undefined)` aufgerufen → alle Stunden wurden als `NaN` berechnet und der Chart blieb leer

## 2026-05-10 – Browser-Cache bust für app.js

- **index.html**: `app.js` Version-Parameter auf `v=20260510` hochgezählt (war `v=70`)

## 2026-05-10 – Prognose-Einschätzung berücksichtigt Sonnenuntergang

- **app.js**: Neue Funktion `getForecastAssessment` ersetzt die inline-Logik; berechnet verbleibenden möglichen Ertrag (Durchschnitt × Restzeit × 0.5 für Abendsonne) statt nur Fortschritts-Prozentsatz; Texte zeigen verbleibende kWh und Restzeit bei orangenem/rotem Status

## 2026-05-10 – Settings: Ortsname-Autovervollständigung – Bugfix Hausnummer

- **app.js**: `selectLocation` auf Index-basiert umgestellt; Ortsname wird jetzt aus `address.city/town/village/municipality/county` gelesen statt `display_name.split(',')[0]`; Results in `locationSearchResults[]` gecacht

## 2026-05-10 – Settings: Ortsname-Autovervollständigung via Nominatim

- **index.html**: Ortsname-`<label>` in `<div style="position:relative">` gewrapped; `autocomplete="off"` und `oninput="searchLocation()"` hinzugefügt; Dropdown-`<div id="location-suggestions">` eingefügt
- **app.js**: Funktionen `searchLocation` (Nominatim-Debounce 300ms, User-Agent gesetzt), `selectLocation` (Koordinaten + Kurzname übernehmen) und Click-outside-Listener ergänzt

## 2026-05-10 – Settings: Standort-Button mit IP-Geolocation-Fallback

- **app.js**: `getDeviceLocation` auf `async` umgestellt; nutzt Browser Geolocation nur über HTTPS, fällt sonst auf ip-api.com (Versuch 2) und ipapi.co (Versuch 3) zurück; neue Hilfsfunktion `setLocation` setzt Koordinaten und trägt Stadtname in `location.name` ein, falls noch leer
- **index.html**: Hinweistext angepasst – HTTPS-Einschränkung entfernt, IP-Genauigkeit (~Stadtebene) kommuniziert

## 2026-05-10 – Settings: Standort vom Gerät übernehmen

- **index.html**: Button "📍 Standort vom Gerät übernehmen" in der Standort-Fieldset eingefügt; Hinweistext um HTTPS-Einschränkung ergänzt
- **app.js**: Funktion `getDeviceLocation(event)` hinzugefügt – übernimmt GPS-Koordinaten via Browser Geolocation API in die Felder `location.latitude` / `location.longitude`

## 2026-05-10 – Heute-Card: Gestapelte Fluss-Balken-Redesign

- **index.html**: `<h2>Heute</h2>` aus der Card entfernt; `renderToday` rendert jetzt Titelzeile + kWh-Wert selbst
- **app.js**: `renderToday` komplett neu – zwei gestapelte Fluss-Balken ("Wohin ging der PV-Strom?" / "Woher kam der Hausstrom?") mit Segment-Labels ab 20% Breite; drei KPI-Boxen (Eigenverbrauch %, Autarkie %, kWh verbraucht); neue Hilfsfunktion `kpiBox()`; Edge-Cases PV=0 und Normalbetrieb abgedeckt

## 2026-05-10 – PV & Netz Card: Fluss-Balken-Redesign

- **index.html**: PV & Netz Card auf `<div id="pv-netz-body">` reduziert; alte `.kv`-Liste und Note entfernt
- **app.js**: `renderStatus` ruft jetzt `renderPvNetz(h, s.wallbox)` auf statt direkte DOM-Updates; neue Funktion `renderPvNetz(h, wb)` rendert PV-Erzeugung (groß), Verteilungsbalken (Heizstab | Wallbox | Eigenverbr. | Netz), Werte-Zeile und Hauptzähler-Fußzeile; Wallbox-Segment (#f5782a, orange) erscheint nur wenn Wallbox lädt (>50 W); Segment-Labels nur bei >15%; Netz-Segment rot bei Bezug, gold bei Einspeisung

## 2026-05-10 – Hausverbrauch-Card: Sparkline-Redesign

- **index.html**: Hausverbrauch-Card neu strukturiert – Flex-Layout mit großer Zahl links und Canvas-Sparkline rechts; neues Element `#hv-avg` für Ø kW/h; CSS-Version auf v50
- **styles.css**: Neue Klassen `.hv-body`, `.hv-left`, `.hv-unit`, `.hv-avg`, `.hv-sparkline`; `.hv-val` und `.hv-today` auf linksbündiges Layout umgestellt
- **app.js**: `renderHausverbrauch()` zeigt Einheit als styled `<span class="hv-unit">` und berechnet Ø kW/h; neue Funktion `drawHausverbrauchSparkline()` zeichnet Verlaufsgraph (Linie + Gradient-Fläche + Endpunkt) aus `/api/history/hourly`; wird bei jedem `refreshStatus()`-Zyklus neu gezeichnet

## 2026-05-10 – Sonnenuntergang: Uhrzeit + verbleibende Zeit

- **app.js**: `fpSunset` zeigt jetzt "20:50 (noch 7h 12min)" tagsüber bzw. "20:50 (vorbei)" nach Sonnenuntergang; v70

## 2026-05-10 – Heute vs. Prognose: Einschätzung "Wird Prognose erreicht?"

- **index.html**: `#forecast-assessment` div direkt unter `#forecast-percent`
- **app.js**: Einschätzungslogik nach Sunset-Definition – vergleicht Ist-% mit erwartetem Tagesfortschritt (lineare Interpolation 6 Uhr → Sonnenuntergang); 4 Zustände: übertroffen / auf Kurs / unter Plan / deutlich verfehlt; nach Sonnenuntergang: Endstand-Bewertung
- v49/v69

## 2026-05-10 – Dashboard: Heute-vs-Prognose Card Redesign mit Fortschrittsbalken

- **index.html**: `#forecast-progress-card` – neuer 32px-Balken mit drei Ebenen (gold Hintergrund/Prognose-Ziel, grüner Fill mit kWh-Label, "noch ~X"-Text rechts); Label-Zeile über dem Balken ("0 kWh" / "Prognose: X kWh")
- **styles.css**: `.forecast-bar-container` ersetzt durch `.forecast-bar-track` + `.forecast-bar-goal` + `.forecast-bar-actual-label` + `.forecast-bar-remaining`; `.forecast-percent` von 24px → 13px (Zeilentext)
- **app.js**: `renderForecastProgress()` befüllt neue Bar-Elemente (`fp-bar-forecast`, `fp-bar-actual`, `fp-bar-remaining`); Prozent-Label zeigt "X% der Prognose erreicht" / "X% – besser als erwartet! 🎉"; Letzte-Tage-Zeilen zeigen "Gestern"/"Vorgestern" statt Datum + Format "32.4 kWh (30.1) ✅ +8%"
- **CSS-Version**: v47 → v48, **JS-Version**: v67 → v68

## 2026-05-10 – Dashboard: Prognose-Card aufgeteilt in zwei Cards

- **index.html**: `#forecast-card` ("Prognose Morgen") bleibt kompakt ohne "Heute:"-Zeile; neue `#forecast-progress-card` ("Heute vs. Prognose") danach eingefügt
- **app.js**: `renderForecastCard()` vereinfacht (nur Morgen-Daten); neues `renderForecastProgress(data, histEntries)` rendert Fortschrittsbalken + KV-Details + Letzte-Tage-Tabelle
  - `loadForecast()` lädt jetzt parallel `/api/forecast` + `/api/history/forecast?days=7`
  - Fortschrittsbalken: grüner Gradient, `transition: width 2s ease`; über 100%: gold Endgradient + 🎉
  - "Verbleibend": zeigt "noch ~X kWh" oder "Endstand" nach Sonnenuntergang
  - "Letzte Tage": letzte 3 Tage mit Prognose vs. Ist + ✅/❌ (±20%)
- **styles.css**: `.forecast-bar-container`, `.forecast-bar-fill` (inkl. `.over`), `.forecast-percent`, `.forecast-last-days`, `.forecast-day-row`, `.fdr-*` Spalten-Styles
- **db.py**: `weather_log` bekommt `sunset TEXT`-Spalte (Migration); `upsert_weather` + `get_weather` erweitert
- **weather.py**: Open-Meteo-Request um `sunset` erweitert; `fetch_and_store` speichert Uhrzeit-Substring (HH:MM); `calculate_forecast` gibt `today.sunset` zurück
- `styles.css?v=47`, `app.js?v=67`


## 2026-05-10 – Prognose-Tab: forecast_kwh persistieren + retroaktiver Backfill

- **Ursache:** `pv_daily_log.forecast_kwh` war bei allen historischen Einträgen NULL — die Speicherlogik in `pvlog.py` wurde erst heute deployed, ältere Cron-Läufe hatten den Code noch nicht.
- **db.py** `upsert_daily_forecast(date_str, forecast_kwh, forecast_ghi)`: neuer INSERT-OR-REPLACE der nur Prognose-Spalten überschreibt, Tageswerte bleiben erhalten.
- **web.py** `/api/forecast`: ruft nach `calculate_forecast()` sofort `upsert_daily_forecast` auf — Prognose für morgen wird in den heutigen Eintrag geschrieben, kein Warten auf den 23:55-Cron.
- **db.py** `backfill_historical_forecasts()`: retroaktiver Backfill (nur wo `forecast_kwh IS NULL`): Regression auf Vortags-Daten + GHI aus `weather_log` → 5 historische Zeilen befüllt, sofort 4 Vergleichspunkte im Tab sichtbar.
- Backfill wird einmalig beim Modulimport ausgeführt (idempotent).

## 2026-05-10 – Verlauf: neuer Sub-Tab "Prognose"

- **db.py**: `get_forecast_accuracy_data(days)` – LEFT JOIN `pv_daily_log` auf Vortag: holt `prev.forecast_kwh` (Prognose vom Vortag für diesen Tag) vs. `d.pv_kwh` (tatsächlicher Ertrag), berechnet `diff_kwh`, `diff_percent`, `hit` (±20%)
- **web.py**: `GET /api/history/forecast?days=30` – neuer Endpunkt, gibt Genauigkeitsdaten zurück
- **pvlog.py**: `daily_job` berechnet Prognose für morgen (lineare Regression GHI→PV, letzten 30 Tage) und speichert sie in `pv_daily_log.forecast_kwh` des heutigen Eintrags
- **index.html**: Verlauf Sub-Tab `[Prognose]` hinzugefügt; Section `#verlauf-prognose` mit KPI-Row, grouped-Bar-Chart (Prognose halbtransparent / Ist voll), Tageswert-Tabelle
- **app.js**: `loadVerlaufPrognose()`, `renderPrognoseKPIs()` (Trefferquote ±20%, Ø Abweichung, Tendenz-Box), `renderPrognoseChart()` (grouped bar chart), `renderPrognoseTable()` (Tabelle mit ✅/❌)
- **styles.css**: `.verlauf-kpi-sub`, `.forecast-table` + Spalten-Styles für die Prognose-Tabelle
- `styles.css?v=46`, `app.js?v=66`

## 2026-05-10 – Kaskade Heizstab: Toleranz 100→200 W, Mindestpause 3→2 min

- **DB** (`cascade_devices`): `hysteresis_watts` Heizstab: 100 → 200 W (weniger empfindlich gegen kurze Mess-Dips)
- **DB** (`cascade_devices`): `min_off_minutes` Heizstab: 3 → 2 min (schnellere Reaktivierung nach Abschaltung)

## 2026-05-10 – Wallbox: Freigabe wenn alle 3 Heizstab-Phasen aktiv

- **controller.py**: Neue Wallbox-Freigabelogik: Wallbox darf laden wenn alle 3 Heizstab-Phasen AN sind (Heizstab läuft auf Volllast, Überschuss darüber geht in die Wallbox) ODER Speicher voll (≥ `release_above_storage_temp`). Vorher: temperaturbasierte `pause_below_storage_temp`-Grenze.
- `_decide_wallbox` bekommt `all_phases_on: Optional[bool]` Parameter; `_handle_wallbox` berechnet diesen aus den Readings.
- `pause_below_storage_temp` in der Config wird nicht mehr ausgewertet (kann für zukünftige Verwendung bleiben).

## 2026-05-10 – Wallbox Kaskaden-Override entfernt (Speicher-Priorität Fix)

- **controller.py**: `cascade_wallbox is True` überstimmte das Temp-Gate (`pause_below_storage_temp`) und gab die Wallbox frei, selbst wenn der Speicher kalt war (z.B. 49°C). Dadurch lud die Wallbox mit ~5 kW und der Heizstab blieb mangels Überschuss aus — das Gegenteil von "Speicher priorisiert".
- Fix: Kaskade kann die Wallbox nur noch *blockieren* (zu wenig Überschuss → `cascade_wallbox=False`), aber nicht mehr über das Temp-Gate hinaus *freigeben*. Das Temp-Gate (`pause_below=62°C` / `release_above=63°C`) ist jetzt autoritativ.

## 2026-05-07 – PV & Netz Card verschoben

- **index.html**: Card "PV & Netz" nach "Heizstab Phasen" verschoben — neue Reihenfolge: PV Erzeugung → Heizstab Phasen → PV & Netz → Heute → …

## 2026-05-07 – Heizstab Phasen Card verschoben

- **index.html**: Card "Heizstab Phasen" direkt nach "PV Erzeugung" verschoben (war zuvor nach "Prognose Morgen")

## 2026-05-07 – String-Verhältnis Banner Abstand

- **styles.css**: `.string-dash-alert` margin-bottom `2px` → `12px` — verhindert das Anquetschen des Banners an den STR1-Balken darunter

## 2026-05-07 – Loading-Overlay beim Seitenstart

- **index.html**: Fullscreen-Overlay (`#loading-overlay`) direkt nach `<body>` — gleiche Hintergrundfarbe wie Dashboard (#1a1d23), Spinner in #2ed8a3, z-index 9999
- **app.js**: `hideLoadingOverlay()` entfernt das Overlay mit 300 ms Fade-out; wird einmalig nach dem ersten `refreshStatus()`-Aufruf in `boot()` via `.finally()` aufgerufen

## 2026-05-06 – Chart Dark-Theme Styling

- **CSS**: Grid lines `rgba(255,255,255,0.06)` statt `var(--line)`, Achsenschriften `#a0a3ab` / 9 px statt 8 px
- **CSS**: Temp-Linie stroke-width 1.8 → 2.5, temp-area opacity 0.15 → 0.25, now-line heller
- **CSS**: Neuer Block `.chart-svg` (Verlauf/Strings/Wetter-SVGs) mit gleichen Grid- und Textregeln
- **Tagesverlauf** (`renderDailyChart`): Füllbereiche unter PV / Einspeisung / Verbrauch; Farben `#e8a435` / `#2ed8a3` / `#e0e2e6`, stroke-width 2–2.5
- **String-Tag** (`renderStringChart`): Füllbereiche + Farben `#e8a435` (STR1) / `#f5782a` (STR2), stroke-width 2.5
- **Woche** (`renderWeekChart`): Eigenverbrauch `#2ed8a3`, Einspeisung `rgba(232,164,53,0.7)`
- **Monat** (`renderMonthChart`): Balkenfarbe `#e8a435` statt `var(--c-pv)`
- **Jahr** (`renderYearChart`): Balkenfarbe `#e8a435`, opacity 0.85
- **Strings-Tag** (`renderStringsDayChart`): Füllbereiche + Farben `#e8a435` / `#f5782a`, stroke-width 2.5
- **String-Verhältnis** (`renderStringRatioChart`): Linie `#2ed8a3` stroke-width 2.5, Band opacity 0.05, Anomalie-Dots `#ff6b6b` r=4
- **Wetter-Korrelation** (`renderScatterPlot`): Punkte `#e8a435` r=5 opacity 0.75, Trendlinie `rgba(46,216,163,0.6)` width 2, Prognose-Stern `#ff6b6b` font-size 18

## 2026-05-10 – Fix: Sonnenuntergang war leer

- **weather.py**: `fetch_and_store` hat übersprungen wenn morgen in der DB war – dadurch wurde heute's `sunset`-Spalte (später hinzugefügt) nie nachgefüllt. Skip-Bedingung geändert: Abruf läuft wenn heute's `sunset` fehlt, auch wenn morgen schon vorhanden.
