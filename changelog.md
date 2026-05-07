# Changelog

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
