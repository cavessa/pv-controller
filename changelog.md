# Changelog

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
