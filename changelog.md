# Changelog

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
