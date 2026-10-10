# Changelog

## 2026-10-10 – Speicher-Card: Tagesverlauf, Zoom auf die letzte Stunde, 7-Tage-Ansicht

**Anlass (User):** Die Speicher-Card soll einen Tagesverlauf mit Heizstab-Leistung, Referenzlinien, Sonnenfenster, Schaltereignissen und einem Zoom auf die letzte Stunde zeigen, plus Umschalter 1 h / 24 h / 7 T.

**Umsetzung:**
- Neues Modul `storage_history.py` + Endpoint `GET /api/speicher/history?range=1h|24h|7d`: berechnet serverseitig 5-Minuten-Buckets (Tag), die 1-Minuten-Werte der letzten Stunde, Heizphasen (an = > 100 W, Mindestdauer 1 min, Lücken > 3 min trennen), Heizstab-kWh (Rechteckregel), Tagesmin/-max mit Uhrzeit, Steigung (lineare Regression über 10 min, °C/h), ETA bis Resume ((Resume − T) / Rate; „–" bei Rate ≤ 0) und Sonnenfenster (aus `weather_log`, kein suncalc nötig). 7 T liefert Min/Max und Heizstab-kWh pro Tag.
- Persistenz: `temp_history.json` speichert jetzt zusätzlich `heater_w` (Heizstab-Leistung am Zähler); `append_temp_history()` bekommt den Wert von `web.py` und `main.py`. Neue Tabelle `storage_day` (Tagesmin/-max, kWh, Heizphasen) wird jede Minute fortgeschrieben, für die 7-Tage-Ansicht; fehlende Tage werden aus `pv_hourly_log` genähert.
- ETA und Hochrechnung enden spätestens am Sonnenuntergang; liegt die ETA danach, zeigt die Card „ETA Resume: heute nicht" (Feld `eta_label`).
- „an"-Labels der Schaltereignisse stehen unter der Kurve, wenn der Punkt in der oberen Hälfte des Temperaturbereichs liegt, sonst darüber.
- 7 T: Tage ohne `heater_w`-Daten zeigen „–" statt 0,0 kWh (Summe nur über bekannte Tage; die Beschriftung „Heizstab kWh/Tag" im Chart entfiel, die Legende nennt sie).
- Frontend (`web/app.js`, `index.html`, `styles.css`): `renderStorageChart()` rendert Tag, letzte Stunde und 7 Tage mit eigenem SVG (keine neue Bibliothek). Tageschart: Temperaturkurve + Fläche, Heizstab-Balken mit eigener kW-Skala, Max-/Resume-Linien, Sonnenfenster, „an"-Punkte mit Uhrzeit, pulsierender Live-Punkt, gestrichelte Hochrechnung bis Resume (nur bei steigender Temperatur), Zoom-Rahmen. Darunter „Letzte Stunde" mit ETA, Legende. Der gewählte Zeitraum wird im Browser gemerkt. Das alte `renderTempChart` ist ersetzt. Cache-Busting `app.js?v=20261010m`, `styles.css?v=57`.

**Abweichungen / Hinweise:**
- Die Persistenz hat nur **1-Minuten-Auflösung**, die letzte Stunde nutzt daher 1-Minuten- statt 30-s-Werte.
- `heater_w` wird erst **ab dem Neustart** des Dienstes aufgezeichnet (und vom Cron-Lauf). Davor ist die Heizstab-Leistung unbekannt: heute zeigt die Card bis dahin „Heizstab –", keine Balken/Ereignisse.
- „Heizphasen" = Heizzyklen (Heizstab an/aus), nicht die elektrischen Phasen PH1–PH3.
- Dienst neu starten (neue API, Schema).

## 2026-10-10 – Heizstab-Punkte kleiner, mehr Abstand zum Rand

**Anlass (User):** Der Abstand der 3 Punkte zum unteren Rand des Heizstab-Knotens war zu gering.

**Umsetzung:** In `renderEnergyFlow` (`web/app.js`) Radius 3 → 2,3, Abstand 11 → 9, Mitte `nh-5` → `nh-8`; der Abstand zum Rand entspricht jetzt etwa dem der Textzeile in den anderen Knoten (z. B. „aktiv"). Cache-Busting `app.js?v=20261010j`.

## 2026-10-10 – Sommermodus-Pill immer sichtbar (an/aus), Heizstab-Punkte höher

**Anlass (User):** Oben soll stehen, ob der Sommermodus an oder aus ist (die Pill zeigte nur „Sommermodus …").

**Umsetzung:** `renderStatus` (`web/app.js`): Die Pill ist jetzt immer sichtbar – „Sommermodus: an" (grün), „Sommermodus: aus" (grau), bei laufender Netz-Heizung „Sommermodus: an · Netz-Heizung" (gelb). Vorher war sie bei „aus" versteckt und der Platzhalter „…" blieb stehen, wenn kein Status geladen war. Zusätzlich sitzen die 3 Heizstab-Punkte im Energiefluss tiefer (`nh-5`), damit sie den Wert „0 W" nicht überlappen. Cache-Busting `app.js?v=20261010i`.

## 2026-10-10 – Energiefluss: Heizstab zeigt 3 Phasen-Punkte

**Anlass (User):** Im Heizstab-Knoten des Energieflusses sollen 3 Punkte die aktiven Phasen anzeigen.

**Umsetzung:** In `renderEnergyFlow` (`web/app.js`) ersetzen drei Punkte den Text „N/3 Ph."; Punkt 1/2/3 ist gefüllt, wenn Phase 1/2/3 an ist (`heater.phases.ph1..ph3`), sonst nur ein Ring. `node()` hat dafür den optionalen Parameter `dots`. Cache-Busting `app.js?v=20261010h`.

## 2026-10-10 – PV-Card: Balken-Skala sichtbar, kein doppelter Wert, Label-Layout

**Anlass (User):** Die Balken hatten unterschiedliche Skalen ohne Hinweis, der Pergola-Wert stand doppelt, und „PERGOLA" überlappte die V/A-Zeile.

**Umsetzung:**
- **Max-Wert am Balkenende:** hinter dem Watt-Wert steht klein und grau „/ 10 kW" bzw. „/ 1 kW" (≥ 1000 W in kW, sonst in W). Die Skala kommt aus der Config: neues Feld `solax.max_w` (pro String, Default 10000) und `solax2.max_w` (Gerät, Default 1000); `/api/solax` liefert `str_max_w` und `pergola_max_w`. Dadurch sind jetzt auch STR1/STR2 auf 10 kW skaliert (vorher relativ zur Gesamt-PV). Die Felder sind nur in `config.json` einstellbar, nicht im Settings-UI; `config.example.json` ergänzt.
- **Doppelter Wert:** Der Summenwert in der Gruppenzeile erscheint nur bei mehr als einem String („PV-Anlage" ja, „Balkonkraftwerk" nein).
- **Label-Kollision:** Label-Spalte 70 px, linksbündig, Zeilenumbruch erlaubt (`line-height: 1.2`); Wertspalte 104 px ohne Umbruch. Das Label wird nicht mehr auf 7 Zeichen gekürzt. Cache-Busting `app.js?v=20261010g`, `styles.css?v=56`.

**Hinweis:** Backend-Änderung (`/api/solax`, `config.py`) → Dienst neu starten.

## 2026-10-10 – PV-Card: Pergola-Balken auf 1000 W skaliert

**Anlass (User):** Die Pergola-Anlage hat maximal 1000 W; der Balken war relativ zur Gesamt-PV skaliert und deshalb kaum sichtbar.

**Umsetzung:** In `renderSolax` (`web/app.js`) bekommt die Pergola-Zeile `max: 1000` – 1000 W entsprechen einem vollen Balken. STR1/STR2 bleiben relativ zur Gesamt-PV. Der Wert ist im Code fest (kein Config-Eintrag). Cache-Busting `app.js?v=20261010f`.

## 2026-10-10 – Hausverbrauch-Card: Zeile „davon aus PV"

**Anlass (User):** Neben „davon zugekauft" soll der PV-Anteil stehen. 

**Umsetzung:** Neue Zeile „davon aus PV: X kWh (Y%)" vor „davon zugekauft" (`web/index.html`, `renderHausverbrauch` in `web/app.js`). Wert = `meter_today.verbrauch_kwh − bezug_kwh` (identisch mit „Eigenverbr." in der HEUTE-Card). Wert grün (#2ed8a3), der Wert bei „davon zugekauft" jetzt rot (#ff6b6b) statt grau. Cache-Busting `app.js?v=20261010e`.

## 2026-10-10 – Tagesverbrauch und Netzbezug aus dem Shelly-Hauptzähler

**Anlass (User):** Mit dem Pergola-Wechselrichter ist der Shelly 3EM am Hauptzähler (`main_meter_url`) die verlässlichere Quelle für Netzbezug/Einspeisung – er sieht alles.

**Umsetzung:**
- `clients/shelly_client.py`: `Shelly3EMClient.get_energy_wh()` liest die Lifetime-Zähler (Gen1 `/status` → `emeters[i].total`/`total_returned`, über die Phasen summiert; Gen2 `/rpc/EMData.GetStatus` → `total_act`/`total_act_ret`). Leistung und Energie teilen sich pro Lauf einen `/status`-Abruf (2 s Cache). Erkennung automatisch (Gen1 zuerst), es gibt kein `main_meter_type` in der Config.
- `db.py`: Tabelle `meter_snapshots` (`date`, `import_wh_at_midnight`, `export_wh_at_midnight`, zusätzlich `taken_at`); `ensure_meter_snapshot()` legt den Snapshot beim ersten Lauf des Tages an (`INSERT OR IGNORE`, Web-Poller und Cron kollidieren nicht).
- Neues Modul `meter.py` (`compute_meter_today`): `bezug = Zähler − Snapshot`, `einspeisung` analog, `verbrauch = PV-Ertrag (Haupt-WR + Pergola) + Bezug − Einspeisung`. Fallback auf die Solax-Tageszähler (`source: "solax"`), wenn der Shelly nicht lesbar ist, der Snapshot fehlt/ungültig ist oder ein Zähler zurückgesetzt wurde.
- `controller.py`/`models.py`: `Readings` um Hauptzähler-Lifetime und Solax-Tageszähler erweitert. Fehler beim Energie-Lesen sind nicht kritisch (nur Log, kein Fail-safe).
- `web.py`: `/api/status` liefert `meter_today` `{source, bezug_kwh, einspeisung_kwh, verbrauch_kwh}`. `/api/solax` liefert dasselbe und setzt `grid_in_today_kwh`/`grid_out_today_kwh`/`self_consumption_kwh` auf die Shelly-Werte, damit HEUTE-Card, Eigenverbrauch/Autarkie-Gauges und Hausverbrauch konsistent sind.
- `pv_logger.py`: `pv_daily_log.feed_out_kwh`/`feed_in_kwh` kommen beim Tagesabschluss aus dem Shelly (wenn verfügbar), damit Verlauf, Netz-Tab und Autarkie konsistent sind.
- Web-UI: Hausverbrauch-Card „Heute: X kWh" = Gesamtverbrauch plus Zeile „davon zugekauft: Y kWh (Z%)"; HEUTE-Card mit Hinweis „Quelle: Hauptzähler" bzw. „Quelle: Wechselrichter". Cache-Busting `app.js?v=20261010d`, `styles.css?v=55`.

**Hinweise:**
- Ein Snapshot gilt nur als Mitternachtswert, wenn er bis 00:30 Uhr entstand (`taken_at`). Der heutige Snapshot entstand beim Test um 13:18 und wird deshalb **nicht** verwendet: **Heute zeigen die Karten noch die Solax-Werte, ab morgen 00:00 den Hauptzähler.** Fällt Cron/Dienst um Mitternacht aus, gilt der Tag ebenfalls mit Solax-Werten.
- Der 3EM zählt Bezug und Einspeisung **pro Phase**: Bezug und Einspeisung sind dadurch höher als beim Zähler des Netzbetreibers (der saldiert über die Phasen), die Differenz und damit der Verbrauch stimmen. 
- Dienst neu starten (Backend-Änderungen).

## 2026-10-10 – Tages-Charts: X-Achse auf Sonnenfenster begrenzt

**Anlass (User):** Die Charts zeigten immer 0h–24h; die Nachtstunden (flach 0) stauchten die eigentliche Kurve.

**Umsetzung:** X-Achse = Sonnenaufgang −1 h bis Sonnenuntergang +1 h, auf volle Stunden gerundet (heute 07:33/18:43 → 6h–20h); ohne Sonnenzeiten Fallback 5h–21h. Gilt für „PV1 / PV2 heute" (PV-Card), Verlauf → Tag („Tagesverlauf" und String-Chart) und Verlauf → Strings (Tagesverlauf). Speicher-Temperatur, Hausverbrauch-Sparkline und Netz-Tagesverlauf bleiben unverändert. Datenpunkte außerhalb des Fensters werden nicht gezeichnet, die „Jetzt"-Linie nur innerhalb des Fensters.
- `weather.py`/`db.py`: Open-Meteo-Abfrage um `sunrise` erweitert, neue Spalte `weather_log.sunrise` (automatische Migration). Der Abruf wird wiederholt, solange der Sonnenaufgang von heute fehlt. `calculate_forecast` liefert `today.sunrise` mit.
- `web.py`: `/api/history/hourly` liefert `sunrise`/`sunset` des angefragten Tages.
- `web/app.js`: `sunWindow()`, die drei Chart-Funktionen nehmen das Fenster als Parameter. Cache-Busting `app.js?v=20261010c`.

**Hinweis:** Nur ab heute ist `sunrise` gespeichert (Open-Meteo-Abruf holt gestern/heute/morgen). Ältere Tage haben keinen Sonnenaufgang in `weather_log` und zeigen deshalb das Fallback-Fenster 5h–21h. Dienst-Neustart nötig (Backend-Änderung an `/api/history/hourly`).

## 2026-10-10 – Pergola-PV (Solax 2, 1,5 kW) als zweite PV-Quelle

**Anlass (User):** Der zweite Solax-Wechselrichter (Balkonkraftwerk, 192.168.2.141) soll im Energiefluss, in der PV-Erzeugung-Card und im Verlauf erscheinen. Name im UI: **Pergola** (nicht „Balkon").

**Umsetzung:**
- `config.py`: `SolaxConfig` um `enabled`/`name` erweitert, neuer optionaler Block `solax2` (`enabled`, `url`, `pwd`, `name`; Default aus). Verschachtelt wie `solax`, nicht als flache `solax2_*`-Keys. `config.example.json` ergänzt.
- `clients/solax_client.py`: Cache jetzt **pro URL** (vorher modulweit – zwei Geräte hätten sich gegenseitig überschrieben). Das Pergola-Gerät (payload `type` 4, X1) hat eine **andere Register-Belegung** als der Haupt-WR: PV-Leistung `Data[7]+Data[8]`, Spannung `[3]/[4]`, Strom `[5]/[6]`, Tagesertrag `Data[13]/10`. Die Belegung `Data[14]/[15]`/`[82]` aus der Vorgabe liefert hier 0 und wurde nur für den Haupt-WR beibehalten. `pv_logger` liest weiter nur den Haupt-WR (`_cached_raw`).
- `controller.py`/`models.py`: `pv_power_w` = Haupt-WR + Pergola; neu `pv1_power_w`, `pv2_power_w`, `pv2_today_kwh`. Pergola-Lesefehler sind **nicht kritisch** (nur Log, kein Fail-safe). Fällt der Haupt-WR aus, bleibt `pv_power_w` None wie bisher.
- `web.py`: `/api/status` (`heater.pv1_power`, `pv2_power`, `pv2_today_kwh`, `pv2_name`, `pv2_enabled`; `pv_power` = Summe), `/api/solax` (`pv_total_w`, `yield_today_kwh`, `consumption_w` = Summe inkl. Pergola, dazu `main_total_w`, `pergola_*`), neuer `POST /api/solax2/test`, `PUT /api/config` akzeptiert `solax2`. Die Strings STR1/STR2 (`pv1_power_w`/`pv2_power_w` in `/api/solax`) bleiben die des Haupt-WR.
- `db.py`/`pv_logger.py`: neue Spalten `pv_hourly_log.pv2_w`, `pv_daily_log.pv2_kwh` (automatische Migration). `pv_w`/`pv_kwh` sind die Summen; der Hausverbrauch im Stunden-Log enthält jetzt die Pergola-Leistung. Der String-Check nutzt nur die Haupt-WR-Leistung.
- PV-Card: Gruppenüberschriften „PV-Anlage" (STR1 Dach, STR2 Wand) und „Balkonkraftwerk" (Zeile mit dem Standort, z. B. „Pergola"); vorher stand „Dach"/„Pergola" doppelt (`app.js?v=20261010b`).
- Web-UI: Einstellungen „Pergola (Solax 2)" mit „Verbindung testen"; Energiefluss mit zweiter PV-Box (Linie ab 50 W, Layout unverändert wenn deaktiviert); PV-Card mit Aufschlüsselung Dach/Pergola; dritte grüne Linie im Tages-Chart. Cache-Busting `app.js?v=20261010a`, `styles.css?v=54`.

**Hinweise:**
- Dienst muss neu gestartet werden (`pvcontroller-web`), `config.json` ist bereits mit `solax2.enabled=true`, `name=Pergola` angepasst.
- Der Pergola-**Tagesertrag** (`Data[13]`) ist nicht gegen die Solax-Doku verifiziert (aktuell 0,2 kWh) – abends mit dem Wert in der Solax-App vergleichen.
- Die Summe geht in die Überschuss-Schwellen (`pv >= min_pv_power`) ein; die Überschussberechnung selbst nutzt den Hauptzähler, der die Pergola schon enthält. Die Prognose-Regression (GHI → `pv_kwh`) mischt ab heute Dach+Pergola.
- Bei Pergola-Ausfall fällt `pv_power` kurz auf den Haupt-WR-Wert zurück; der Tagesertrag im Log behält den zuletzt gespeicherten Pergola-Wert.

## 2026-10-03 – Heizstab: einstellbare Netzbezug-Toleranz (`heater.grid_assist_w`)

**Anlass (User):** Im Winter werden 1,5 kW PV-Überschuss selten erreicht; der Heizstab soll schon ab z. B. 1 kW starten und die fehlenden 500 W aus dem Netz beziehen. Per Schalter einstellbar.

**Umsetzung:** Neue Einstellung `heater.grid_assist_w` (W, Default 0 = aus). Sie wird von `min_pv_power_ph1/2/3` abgezogen (`HeaterConfig.effective_min_pv`, genutzt in `controller.py`) – gilt für Ein- und Ausschaltschwelle sowie die PV-Mindestleistung. Beispiel 500 W: Phase 1 ab 1000 W, Phase 2 ab 2500 W, Phase 3 ab 4000 W. Einstellbar im Web-UI (Heizstab & Speicher), in `web.py` erlaubt, `config.json`/`config.example.json` ergänzt. Cache-Busting `app.js?v=20261003a`.

**Hinweis:** Dienst muss neu gestartet werden. Der Netzbezug entspricht höchstens dem eingestellten Wert pro laufender Phase-Stufe.

## 2026-09-29 – Tageshoch wird serverseitig gespeichert (war nach Reload falsch)

**Anlass (User):** Dashboard zeigte "Tageshoch: 2342 W um 15:47", obwohl heute über 7000 W erreicht wurden.

**Ursache:** Das Tageshoch wurde nur im Browser mitgezählt (`pvPeakToday` in `web/app.js`) und startete bei jedem Seiten-Reload bei 0 – es zeigte also nur das Maximum seit dem Laden der Seite.

**Fix:** Neue Tabelle `pv_peak_day` (`db.py`, `update_pv_peak()`); `/api/solax` schreibt das Tagesmaximum bei jedem Abruf fort und liefert `pv_peak_w`/`pv_peak_time`. Das Frontend zeigt nur noch diese Werte an. Cache-Busting `app.js?v=20260929d`.

**Nachtrag:** Tageshoch-Anzeige war leer, solange der Dienst noch nicht neu gestartet war (altes Backend ohne `pv_peak_w`). Frontend hat jetzt einen Fallback auf die lokale Zählung (`app.js?v=20260929e`).

**Hinweis:** Das Maximum gilt ab jetzt (Deploy ~15:50); die heutigen 7000 W vor dem Fix sind nicht nachgetragen, ab morgen stimmt der Wert komplett. Der Verlaufschart (7/30 Tage) nutzt weiter die stündlichen Snapshots und kann Spitzen zwischen den Stunden verpassen.

## 2026-09-29 – Fehlalarm "String 1 liefert 0 W" auf dem Dashboard behoben

**Anlass (User):** Dashboard zeigte "String 1 liefert 0 W während String 2 aktiv ist", obwohl beide Strings liefen (3677 W / 3091 W).

**Ursache:** Das Banner (`_checkLiveStringAlert` in `web/app.js`) zeigte den neuesten Eintrag aus `string_alerts` der letzten 24 h – ohne Prüfung, ob der Fehler noch besteht. Der Alarm war echt, aber von 13:39 (STR1 0 W, STR2 2364 W, vermutlich kurzzeitige Verschattung) und um 14:22 längst vorbei.

**Fix:** Banner erscheint nur noch, wenn die Live-Werte (`/api/solax`, `pv1_power_w`/`pv2_power_w`) den Fehler noch zeigen (ein String < 10 W, der andere > 200 W). Das Alert-Log im Verlauf-Tab bleibt unverändert. Cache-Busting `app.js?v=20260929c`.

**Hinweis (nicht geändert):** In `db.log_string_alert` wird `datetime('now')` (UTC) mit lokalen Timestamps verglichen – das 55-min-Dedup ist dadurch um 2 h verschoben.

## 2026-09-29 – Dashboard lädt sofort: Status-Cache im Backend, Skeleton-UI statt Loading-Overlay

**Anlass (User):** Der Fullscreen-Loading-Overlay (Sonne + Spinner) stand ~5 Sekunden, bevor das Dashboard erschien. Wunsch: Layout sofort anzeigen (Skeleton-Platzhalter), jede Karte einzeln füllen sobald ihre Daten da sind, und die Ursache der 5 Sekunden im Backend beheben.

**Messung vorher:** `/api/status` 5,6 s, `/api/cascade/status` 6,6 s, `/api/cascade/log` 1,5 s – alle anderen Endpunkte < 100 ms. Ursachen:
1. `/api/status` **und** `/api/cascade/status` führten jeweils einen kompletten READ_ONLY-Controllerlauf aus (8 HTTP-Roundtrips zu Solax, Shellys, go-e) – zweimal pro Dashboard-Refresh, alle 10 s.
2. Der Shelly-Heizstab-Zähler `heater_meter_url = http://192.168.2.21` ist nicht erreichbar → jeder Controllerlauf wartet 5 s auf den Connect-Timeout (der Controller fällt dann korrekt auf "0 W aus 0 aktiven Phasen" zurück). **Das Gerät sollte geprüft werden** – auch der minütliche Cron-Lauf (`main.py`) wartet dadurch bei jedem Durchlauf 5 s.
3. `cascade_log` hat ~630.000 Zeilen und keinen Index auf `timestamp` → `ORDER BY timestamp DESC LIMIT 50` scannt die ganze Tabelle.

**Backend:**
- `web.py`: Neuer Hintergrund-Poller (`_status_poll_loop`, Daemon-Thread, gestartet über FastAPI-`lifespan`). Er führt den READ_ONLY-Controllerlauf alle 10 s aus (60 s, wenn 60 s lang kein Client den Status abgefragt hat) und cached das `ControllerResult` + Config. `/api/status` und `/api/cascade/status` liefern nur noch den Snapshot (`_cached_status()`), kein Geräte-Roundtrip mehr im Request-Pfad. Vor dem ersten Snapshot wartet ein Request bis zu 30 s (danach 503). Ist der Snapshot beim Request älter als 10 s (Übergang idle→aktiv), wird der Poller sofort geweckt. `POST /api/run-once` schreibt sein NORMAL-Ergebnis direkt in den Cache; `PUT /api/config` weckt den Poller, damit geänderte Einstellungen sofort im Status auftauchen. `append_temp_history` wandert vom Request-Pfad in den Poller (gleiche 10-s-Rate wie bisher bei offenem Dashboard).
- `db.py`: `CREATE INDEX IF NOT EXISTS idx_cascade_log_timestamp ON cascade_log(timestamp)` in `init_db()`.

**Messung nachher:** `/api/status` 25 ms, `/api/cascade/status` 18 ms, `/api/cascade/log` 16 ms.

**Frontend:**
- `web/index.html`: `#loading-overlay` komplett entfernt. Alle Dashboard-Werte/-Container starten mit Skeleton-Platzhaltern (`<span class="skeleton">…</span>` bzw. `<div class="skeleton skeleton-chart">`); der Platzhaltertext (transparent) bestimmt die Balkenbreite. Da die Renderer die Elemente per `textContent`/`innerHTML` überschreiben, verschwindet das Skeleton automatisch mit dem ersten echten Wert – kein separates "Skeleton entfernen" nötig. Cache-Busting `styles.css?v=53`, `app.js?v=20260929b`.
- `web/styles.css`: `.skeleton` (Shimmer-Gradient, `color: transparent`, `pointer-events: none`), `.skeleton-chart`, `@keyframes shimmer`.
- `web/app.js`: `refreshStatus()` wartet nicht mehr per `Promise.all` auf alle fünf Calls, sondern rendert jeden Bereich, sobald seine Antwort da ist: Kaskade-Mini, String-Chart, Wallbox-Chart, Phasen-Logs, Prognose, Hausverbrauch-Sparkline laufen unabhängig; nur Status + Solax werden gemeinsam gerendert (gemeinsame PV-Leistung als Single Source of Truth), Temperatur-Chart danach (braucht `storage_max_temp` aus dem Status). `hideLoadingOverlay()` entfernt.

## 2026-09-29 – String-Verhältnis-Feature entfernt, Tageshoch-Chart auf kW umgestellt, Dashboard-Card-Layout iPad-Fix

**Anlass (User):** Das String-Verhältnis-Warnsystem (Dach vs. Wand) schlägt bei diesem System grundsätzlich immer an, da beide Strings unterschiedlich ausgerichtet sind und nie ein stabiles "Normalverhältnis" haben. Zudem sollte die Tageshoch-Karte im Verlauf Leistung (kW) statt Uhrzeit zeigen, und auf dem iPad wirkte die Tageshoch-Karte deutlich breiter als die übrigen Dashboard-Karten.

**String-Verhältnis entfernt** (die reine Ratio-Auswertung STR1/STR2 – die Erkennung "String liefert 0 W" bleibt erhalten, da sie orientierungsunabhängig ist):
- `pv_logger.py`: `_check_string_anomaly()` – Block, der bei >30 % Abweichung vom "Normalverhältnis" einen Alert loggt, entfernt.
- `web/app.js`: `checkStringAlert()` – Ratio-Abweichungs-Check (Tagesdurchschnitt) entfernt; `renderStringRatioChart()` sowie der Ratio-Fetch in `loadVerlaufStrings()` entfernt; `_cachedNormalRatio` entfernt.
- `web/index.html`: Karte "String-Verhältnis (30 Tage)" (Verlauf → Strings) entfernt.
- `web.py` / `db.py`: unbenutzt gewordenen Endpunkt `GET /api/strings/ratio` sowie `get_string_ratio_history()` entfernt (`get_normal_ratio()` bleibt, wird noch als Kontext-Info bei "String liefert 0 W"-Alerts geloggt).

**Tageshoch-Chart auf kW umgestellt:**
- `web/app.js`: `renderPeakTimeChart()` – Y-Achse zeigt jetzt die Tageshoch-Leistung in kW (`peak_w / 1000`, Achsenschritte über `niceStep()`) statt der Uhrzeit; Uhrzeit steht weiterhin im Hover-Tooltip. Cache-Busting `app.js?v=` auf `20260929a` angehoben.
- `web/index.html`: Karte umbenannt in "Tageshoch – Verlauf (kW)", Legende auf "Tageshoch" vereinfacht.

**iPad-Layout-Fix:** Die Tageshoch-Karte lag als eigenständige `.card` direkt unter `.dashboard-cols` und wurde ab 768 px Breite dadurch über die volle Breite gestreckt, während die übrigen Karten im Zwei-Spalten-Grid nur halb so breit waren.
- `web/index.html`: Tageshoch-Karte zunächst in das `.grid` von `.dashboard-right` verschoben (nach der Wallbox-Karte).

**Nachtrag (User):** Alte, bereits vor der Code-Änderung geloggte "String-Verhältnis weicht ... ab"-Alerts wurden weiterhin im Dashboard angezeigt (kamen aus der DB, nicht aus neu generiertem Code). Zudem war die rechte Dashboard-Spalte (PV Erzeugung, Heizstab, PV & Netz, Prognose, Wallbox, Tageshoch) auf dem iPad deutlich länger als die linke Spalte – die Tageshoch-Karte sollte stattdessen links stehen.

- `pvcontroller.db`: 145 veraltete `string_alerts`-Zeilen mit der Nachricht "String-Verhältnis weicht ... vom Normal ab" gelöscht (Restbestände aus der Zeit vor der Code-Änderung; die Zeilen zu "String liefert 0 W" blieben erhalten).
- `web/index.html`: Tageshoch-Karte von `.dashboard-right` nach `.dashboard-left` verschoben (letzte Karte, nach "Speicher"), um die beiden Spalten auf dem iPad in der Höhe anzugleichen.
- `pvcontroller-web.service` neu gestartet, damit die entfernte `/api/strings/ratio`-Route und der bereinigte `pv_logger.py`-Code aktiv sind (reine Web-/API-Schicht, keine Auswirkung auf die minütlich per Cron laufende Steuerungslogik in `main.py`).

## 2026-09-28 – Energiefluss-Grafik: Werte unter 1 kW in Watt statt kW

**Anlass (User):** In der Energiefluss-Grafik zeigte PV bei 90 W noch "0.09 kW" an – schwer lesbar bei kleinen Leistungen.

**Lösung:** Neuer Helper `kwOrW(w)` in `renderEnergyFlow()` (`web/app.js`) – ab 1000 W wird in kW (2 Nachkommastellen), darunter in ganzen Watt angezeigt. Auf PV-, NETZ-, HEIZSTAB- und Wallbox-Knoten angewendet (HAUS und Shelly-Geräte rundeten schon vorher so). Cache-Busting `app.js?v=` auf `20260928a` angehoben.

**Nachtrag (User):** Bei niedriger PV-Leistung (z. B. 73 W) fehlte die Verbindungslinie PV→HAUS, obwohl NETZ→HAUS angezeigt wurde.

- `web/app.js`: PV-Schwelle `pvOn` von 100 W auf 50 W gesenkt – jetzt konsistent mit den anderen Fluss-Schwellen (NETZ/HEIZSTAB ebenfalls 50 W). Cache-Busting `app.js?v=` auf `20260928b` angehoben.

## 2026-09-26 – Neue Verlauf-Sektion "Tageshoch" (PV-Spitzenleistung 7/30 Tage)

**Anlass (User):** Ganz unten sollte noch ein Chart mit dem Tageshoch (PV-Spitzenleistung pro Tag) für die letzten 7 und die letzten 30 Tage her.

**Lösung:** Neue Verlauf-Sektion "Tageshoch" (eigener Nav-Button, ganz unten im Verlauf-Tab) mit zwei Bar-Charts. Als Datenquelle dient `pv_hourly_log` (stündliche PV-Snapshots) – die Tages-Spitzenleistung wird per `MAX(pv_w)` gruppiert nach Tag ermittelt (Auflösung: stündlich, da keine minütliche Rohdatenhistorie vorgehalten wird).

- `db.py`: neue Funktion `get_daily_peak(days)` – `MAX(pv_w)` gruppiert nach Tag aus `pv_hourly_log`, letzte N Tage.
- `web.py`: neuer Endpunkt `GET /api/history/peak?days=` (Default 30, max 365).
- `web/index.html`: neuer Nav-Button "Tageshoch" im Verlauf-Tab (vor "Netz") + neue Sektion `verlauf-peak` mit zwei Karten ("letzte 7 Tage" / "letzte 30 Tage"); Cache-Busting `app.js?v=` auf `20260926c` angehoben.
- `web/app.js`: `loadVerlaufPeak()` lädt `/api/history/peak?days=30` einmal und rendert daraus beide Charts (7-Tage-Ausschnitt + volle 30 Tage) über neue generische `renderPeakChart(entries, wrapId, days)` (Balken in kW, Wochentags- bzw. Tag-des-Monats-Beschriftung wie bei `renderWeekChart`/`renderMonthChart`); Dispatch in `_loadCurrentVerlauf()` um `peak` erweitert.

**Nachtrag (User):** Doch nicht im Verlauf-Tab als Balken, sondern ganz unten im Dashboard (Übersicht) als Linienchart – im Fokus steht dabei weniger der Watt-Wert als die Uhrzeit, zu der das Tageshoch jeweils auftrat.

- `db.py`: `get_daily_peak()` liefert jetzt zusätzlich `peak_hour` (Stunde des Tageshochs, `substr(timestamp,12,5)` aus `pv_hourly_log`; SQLite liefert dank Bare-Column-Optimierung bei `MAX()` pro Gruppe automatisch die passende Zeile).
- `web/index.html`: Verlauf-Tab-Nav-Button + Sektion "Tageshoch" wieder entfernt; stattdessen neue Karte "Tageshoch – Uhrzeit im Verlauf" ganz unten im Dashboard-Tab (nach `.dashboard-cols`) mit zwei Chart-Wraps (7 Tage / 30 Tage).
- `web/app.js`: `renderPeakChart()`/`loadVerlaufPeak()` ersetzt durch `renderPeakTimeChart()`/`loadPeakTimeChart()` – Linienchart mit Y-Achse = Uhrzeit (statt Leistung), Punktgröße skaliert mit der Spitzenleistung, SVG-`<title>` als Hover-Tooltip (Datum · Uhrzeit · Watt). Wird einmalig in `boot()` und nach jedem manuellen Lauf (`runOnce()`) geladen, nicht bei jedem `refreshStatus()`-Poll (Daten ändern sich höchstens stündlich); Cache-Busting `app.js?v=` auf `20260926d` angehoben.

## 2026-09-26 – STR1/STR2-Balken in "PV Erzeugung" farblich an Legende angeglichen

**Anlass (User):** Die oberen Balken bei STR1/STR2 in der "PV Erzeugung"-Karte waren beide gelb – sollten wie die Legende darunter (STR1 gelb, STR2 blau) eingefärbt werden.

- `web/styles.css`: neue Klasse `.pv-string-bar.str2` mit `var(--accent)` (Blau); `.pv-string-bar` bleibt für STR1 bei `var(--c-pv)` (Gelb).
- `web/app.js`: Live-String-Balken (`stringsEl`) vergeben jetzt `str1`/`str2` als zusätzliche CSS-Klasse am Balken.
- `web/index.html`: Cache-Busting auf `20260926b` angehoben.

## 2026-09-26 – STR1/STR2-Legenden mit Standort ergänzt

**Anlass (User):** STR1 sollte als "Dach" und STR2 als "Wand" gekennzeichnet werden, damit die Zuordnung in den Grafiken/Anzeigen klarer ist.

- `web/index.html`: Legenden in den Karten "PV Erzeugung" (Übersicht), "String-Performance" und "String-Leistung (täglich)" zeigen jetzt "STR1 (Dach)" / "STR2 (Wand)"; Cache-Busting auf `20260926` angehoben.
- `web/app.js`: Live-String-Anzeige (`stringsEl`) und der String-Alert-Text zeigen ebenfalls "STR1 (Dach)" / "STR2 (Wand)".

## 2026-09-24 – PV1/PV2-Verlaufschart direkt in der Übersicht unter "PV Erzeugung"

**Anlass (User):** Ein Chart für PV1 und PV2 sollte auch direkt in der Dashboard-Übersicht unter "PV Erzeugung" sichtbar sein, nicht nur im Verlauf-Tab (dort existiert die "String-Performance"-Grafik bereits unter Verlauf → Tag).

**Lösung:** Die bestehende `renderStringChart()`-Funktion (bisher fest an `chart-strings-wrap` im Verlauf-Tab gebunden) nimmt jetzt eine `wrapId` als zweiten Parameter, damit sie für ein zweites Chart-Element wiederverwendet werden kann. In der "PV Erzeugung"-Karte der Übersicht gibt es jetzt einen neuen Chart-Container mit STR1/STR2-Legende, der bei jedem Status-Refresh (`refreshStatus()`, `runOnce()`) mit den Stundendaten des aktuellen Tages aus `/api/history/hourly` befüllt wird.

- `web/app.js`: `renderStringChart(entries, wrapId = "chart-strings-wrap")` generalisiert; `refreshStatus()` lädt zusätzlich `/api/history/hourly` (heute) und rendert damit `pv-strings-chart-wrap`; `runOnce()` ebenso nach dem manuellen Lauf.
- `web/index.html`: neuer Chart-Container `pv-strings-chart-wrap` inkl. Legende (STR1/STR2) in der "PV Erzeugung"-Karte der Übersicht; Cache-Busting `app.js?v=` auf `20260924` angehoben.

**Nachtrag (User):** STR2-Linie im Chart war orange (`#f5782a`), obwohl die Legende (`.cl-str2`) für STR2 Blau (`--accent`, `#5b8cff`) zeigt – Farbe im Chart auf Blau korrigiert, betrifft beide Charts (Übersicht + Verlauf → Tag → String-Performance), da sie dieselbe `renderStringChart()` nutzen. Cache-Busting auf `20260924b` angehoben.

## 2026-09-17 – Wallbox: feste Standardeinstellungen für Eco/Basic-Modus + Auto-Reset beim Abstecken

**Anlass (User):** Der go-eCharger soll beim Umschalten der Segmented-Control-Buttons feste Standardwerte setzen statt nur `lmo` zu ändern: Eco/PV-Modus = einphasig, 16 A (max. ~3,7 kW), Basic-Modus = dreiphasig, 10 A (max. ~6,9 kW). Zusätzlich soll die Wallbox beim Abstecken automatisch zurück auf Eco/PV wechseln, damit das nächste Laden immer im PV-Überschuss-Modus startet.

**Lösung:** `GoeClient` bekommt eine neue `set_psm()`-Methode (go-e API v2, `psm=1` einphasig / `psm=2` dreiphasig) und liest `psm` jetzt mit im Status. `WallboxConfig` bekommt neue, in den Settings konfigurierbare Felder `eco_phases`/`eco_amps`/`basic_phases`/`basic_amps`/`auto_reset_on_unplug` (Defaults 1/16/2/10/true). Der manuelle Moduswechsel (`POST /api/wallbox/set-mode`) setzt jetzt `psm`+`amp`+`lmo` passend zum Modus statt nur `lmo`. Die bereits vorhandene Auto-Restore-Logik beim Abstecken (`car_state==1` im Basic-Modus) nutzte bisher hart codiert `lmo=4, amp=7` – sie verwendet jetzt die konfigurierbaren Eco-Werte (Default 1-phasig/16 A) und setzt zusätzlich `psm`, ist per `auto_reset_on_unplug` abschaltbar.

- `clients/goe_client.py`: `set_psm()` neu; `STATUS_FILTER` um `psm` erweitert.
- `config.py` `WallboxConfig`: neue Felder `eco_phases`, `eco_amps`, `basic_phases`, `basic_amps`, `auto_reset_on_unplug` + Validierung (Phasen nur 1/2, Ampere 6–32).
- `models.py`: `WallboxStatus.psm`, `WallboxDecision.target_psm`/`psm_executed`/`psm_execution_error` neu.
- `controller.py`: `_parse_wallbox_status()` liest `psm`; Auto-Restore-Zweig in `_decide_wallbox()` nutzt `cfg.eco_phases`/`cfg.eco_amps` statt hart codiert `4`/`7` und respektiert `cfg.auto_reset_on_unplug`; neue `_execute_psm()` (Pendant zu `_execute_amp()`), aufgerufen in `_handle_wallbox()`.
- `web.py`: `POST /api/wallbox/set-mode` setzt jetzt `psm`+`amp` aus der Config mit; `_ALLOWED_WALLBOX_KEYS` + Validierung um die neuen Felder erweitert; `_wallbox_status_to_dict`/`_wallbox_decision_to_dict` geben `psm`/`target_psm` mit aus.
- `web/index.html` (Settings-Tab, Fieldset "Wallbox (go-e)"): neue Felder für Eco/Basic Phasen+Ampere und Checkbox "Nach Abstecken automatisch auf Eco/PV zurücksetzen"; Cache-Busting `app.js?v=` auf `20260917` angehoben.
- `web/app.js`: `loadSettings()`/`saveSettings()` um die neuen `wallbox.*`-Felder erweitert.
- `config.json`, `config.example.json`: neue Wallbox-Defaults (`eco_phases=1, eco_amps=16, basic_phases=2, basic_amps=10, auto_reset_on_unplug=true`).

## 2026-07-26 – Energiefluss-Grafik zeigte alte app.js aus dem Browser-Cache (Cache-Busting nachgezogen)

**Anlass (User, Screenshot):** Nach dem `heatOn`-Fix (siehe Eintrag darunter) blieb SPEICHER trotz 3/3 aktiver Phasen im Energiefluss-Diagramm weiterhin grau/inaktiv. Ursache: `web/index.html` band `app.js` mit `?v=20260525` ein – dieser Cache-Busting-Parameter wurde bei den letzten Änderungen nie hochgezählt, wodurch Browser (insb. als Homescreen-Webapp) weiterhin die alte, gecachte `app.js` ausgeliefert bekamen.

**Lösung:** Versionsparameter auf `?v=20260726` angehoben, damit der Browser die aktuelle `app.js` neu lädt.

- `web/index.html`: `<script src="/static/app.js?v=...">` Version 20260525 → 20260726.

## 2026-07-26 – Energiefluss-Grafik: Fluss zum Speicher folgt Phasenstatus, nicht nur Zählerleistung

**Anlass (User):** Im "Energiefluss"-Diagramm (Dashboard) stoppte die Fluss-Animation HEIZSTAB→SPEICHER, sobald der Heizstab-Zähler kurzzeitig 0 W meldete – obwohl die Phasen-Relais laut Status weiterhin an waren. Ursache dafür ist ein bekanntes Verhalten des Heizstabs: sein interner Thermostat schaltet die Heizpatrone manchmal selbst kurz ab, auch wenn das vorgeschaltete Relais an bleibt. User möchte, dass die Grafik in diesem Fall trotzdem "läuft", sobald Phasen an sind.

**Lösung:** `heatOn` in `renderEnergyFlow()` (web/app.js) berücksichtigt jetzt zusätzlich zum gemessenen `heater_meter_power` auch den Phasenstatus, analog zur bereits vorhandenen `heaterAktiv`-Logik weiter oben in derselben Datei.

- `web/app.js` `renderEnergyFlow()`: `const heatOn = heaterW > 50;` → `const heatOn = heaterW > 50 || phases > 0;`. Die angezeigte Leistung am HEIZSTAB-Knoten bleibt der reale Messwert, nur Aktiv/Fluss-Optik folgt jetzt zusätzlich den Relais-Zuständen.

## 2026-07-25 – Hysterese-Band blockierte Hochschalten weiterer Heizstab-Phasen

**Anlass (User):** "jetzt könnte er doch auch alle 3 heizstäbe an machen" – bei laufender PH1 und ausreichend PV-Überschuss schalteten PH2/PH3 nicht zu, solange die Speichertemperatur im Hysterese-Band lag (`heat_resume_temp` < temp < `storage_max_temp`, aktuell 62–64 °C). `_decide_phase()` verbot dort bisher grundsätzlich jedes Einschalten einer neuen Phase, unabhängig davon, ob überhaupt schon geheizt wurde.

**Lösung:** Im Hysterese-Band dürfen jetzt zusätzliche Phasen zugeschaltet werden, wenn bereits mindestens eine Phase läuft (aktiver Heizzyklus) und die PV-Überschussbedingung erfüllt ist. Ist noch gar keine Phase an, bleibt die bisherige Sperre bestehen – das verhindert weiterhin ein Neustart-Flattern direkt nach Erreichen von `storage_max_temp`.

- `controller.py` `_decide_phase()`: neuer Parameter `any_phase_on`; im `HYSTERESIS_BAND`-Zweig `TURN_ON` erlaubt, wenn `want_on and any_phase_on`.
- `controller.py` `run()`: `any_phase_on` vor der Phasen-Schleife aus dem Ausgangszustand (`ph1_on`/`ph2_on`/`ph3_on`) berechnet und an `_decide_phase()` übergeben.

## 2026-07-24 – Kaskade Heizstab: Hysterese 200→0 W

**Anlass (User):** Heizstab schaltete trotz ~1500–1700 W PV-Überschuss nicht ein, weil die Kaskade wegen der 200-W-Hysterese (Toleranz, siehe Eintrag 2026-05-10) `remaining >= 1500 W + 200 W` verlangte und der Überschuss knapp innerhalb dieser Totzone lag. User: kurzer Netzbezug ist kein Problem, Hysterese soll weg.

- **DB** (`cascade_devices`): `hysteresis_watts` Heizstab: 200 → 0 W. Einschalten jetzt schon ab genau 1500 W Überschuss, Ausschalten sofort bei Budget < 0 W. Kein Puffer mehr gegen kurze Mess-Dips – dafür reagiert der Heizstab jetzt direkter auf verfügbaren Überschuss.

## 2026-07-23 – Heizstab-Phasen: Shelly Gen2 mit Kanal-Unterstützung (Pro 2PM für 2 Phasen)

**Anlass (User):** Der Shelly 1PM für Heizstab-Phase 3 ist defekt gegangen. Ersatz ist ein Shelly Pro 2PM (Gen2, RPC-API, 2 Kanäle), der PH2 und PH3 zusammen übernehmen soll (eine URL, Kanal 0/1). PH1 bleibt ein einzelner Gen1-Plug. Die bestehende Konfiguration beim Schwiegervater (3× einzelne Gen1-Plugs) muss unverändert weiterlaufen.

**Lösung:** `ShellyPlugClient` (`clients/shelly_client.py`) unterstützt jetzt sowohl Gen1 (`/relay/<channel>`) als auch Gen2 (`/rpc/Switch.GetStatus` / `/rpc/Switch.Set` mit `id=<channel>`), analog zum bereits vorhandenen `ShellyCascadeClient`. Auswahl über neue optionale Config-Felder `shelly.ph[1-3]_type` (`shelly_gen1`/`shelly_gen2`) und `shelly.ph[1-3]_channel`, beide defaulten auf `shelly_gen1`/`0` – bestehende `config.json`-Dateien ohne diese Felder verhalten sich exakt wie vorher.

- `clients/shelly_client.py`: `ShellyPlugClient.__init__()` nimmt `type_`/`channel` entgegen; `get_relay_state()`/`set_relay()` verzweigen auf Gen1- oder Gen2-Endpunkt.
- `config.py`: `ShellyConfig` um `ph1_type`/`ph1_channel` … `ph3_type`/`ph3_channel` erweitert (Default `shelly_gen1`/`0`), Validierung der `type`-Werte in `__post_init__()`.
- `controller.py` `Controller.__init__()`: `ph1`/`ph2`/`ph3`-Clients bekommen `type_`/`channel` aus der Config.
- `web.py`: `_ALLOWED_SHELLY_KEYS` um die neuen Felder erweitert, eigene Validierung für `type` (Enum) und `channel` (Integer ≥ 0).
- `web/index.html`, `web/app.js`, `web/styles.css`: Typ-Auswahl + Kanal-Feld pro Phase im Settings-Formular.
- `config.example.json`, `README.md`: neue Felder dokumentiert.
- Zwei Phasen an einem Pro 2PM konfiguriert man mit gleicher `ph2_url`/`ph3_url`, `type=shelly_gen2`, `channel=0` bzw. `1`.

## 2026-07-22 – true_surplus_w zog Wallbox-Leistung auch im Standard-/manuellen Modus ab (Bugfix)

**Problem (User, beim Live-Test entdeckt):** `Readings.true_surplus_w` (Basis für Heizstab-Phasenentscheidung und Sommermodus) zog `wallbox_power_w` unconditional ab, unabhängig davon ob die Wallbox im Eco-Modus (lmo=4, PV-adaptiver Ladestrom) oder im Standard-/manuellen Modus (lmo≠4, fester Ladestrom egal wie viel PV da ist) lädt. Lädt die Wallbox mit festem Strom, ist dieser Verbrauch schon reell im Hauptzähler enthalten – zieht man ihn zusätzlich ab, denkt die Heizstab-Logik fälschlich, es gäbe mehr Überschuss als real vorhanden, und aktiviert ggf. zu viele Phasen. `cascade_service.py._get_controlled_loads_w()` hatte genau diese Unterscheidung (`lmo != 4` → nicht addieren) schon korrekt eingebaut, `controller.py` beim Befüllen von `readings.wallbox_power_w` aber nicht.

**Lösung:** `readings.wallbox_power_w` wird jetzt nur noch auf den gemessenen Wert gesetzt, wenn `wallbox_status.pv_surplus_active` (lmo=4) true ist; sonst auf `0.0` (Wallbox-Verbrauch bleibt dann Teil des ganz normal vom Hauptzähler erfassten Hausverbrauchs).

- `controller.py` `run()`: Zuweisung von `readings.wallbox_power_w` an `wallbox_status.pv_surplus_active` gebunden.

## 2026-07-22 – Wallbox-Pause komplett entfernt: dauerhaft freigegeben, go-e regelt alles selbst

**Problem (User):** Das häufige Umschalten der Wallbox zwischen pausiert/freigegeben (abhängig vom Live-Zustand der Heizstab-Phasen: `full`/`idle`/`partial`/`unknown`/`grace`, inkl. 90s-Debounce) führte am nächsten Tag dazu, dass die Wallbox erst nach Aus-/Einstecken des Fahrzeugs wieder lud. Trotz mehrerer Anti-Flatter-Fixes (Debounce, Grace-Periode, `idle`-Zustand, siehe Einträge vom 2026-07-21) blieb das `frc`-Kommando zu häufig.

Erster Versuch am selben Tag (reine Speichertemperatur-Hysterese `pause_below_storage_temp`/`release_above_storage_temp`) blockierte die Wallbox live trotz reichlich vorhandenem Kaskaden-Überschuss, weil die absolute Temp-Schwelle (63/64 °C) keine Rücksicht auf den tatsächlichen Überschuss nahm. Zweiter Versuch (Kaskade als alleinige Autorität für Pause/Freigabe) behob das, aber der User wies darauf hin, dass eine explizite Pause durch den Controller gar nicht nötig ist: Schaltet der Heizstab eine Phase zu, sieht der go-e das über seinen eigenen Hauszähler und drosselt seinen Ladestrom automatisch – ganz ohne dass der Controller `frc` anfassen muss.

**Lösung:** `_decide_wallbox()` pausiert die Wallbox nicht mehr aktiv. Sobald der PV-Überschussmodus-Gate (`only_control_when_pv_surplus_active`) passiert, wird `frc=0` gesetzt (falls nicht schon so) und bleibt dauerhaft so – keine Temperatur-Schwelle, keine Kaskaden-Kopplung, kein Pause-Zweig mehr. Priorität Speicher vor Wallbox ergibt sich automatisch daraus, dass der Heizstab zuschaltet und der go-e das über seinen eigenen Hauszähler sieht.

- `controller.py` `_decide_wallbox()`: komplett auf `cfg.enabled` → `status None` → `only_control_when_pv_surplus_active`-Gate (inkl. Auto-Restore bei `car_state==1`) → immer `RELEASE`/`UNCHANGED` mit `target_force_state=0` reduziert. Kein `heater_state`-, `temp_status`- oder Kaskaden-Parameter mehr.
- `controller.py` `_handle_wallbox()`: PH1/PH2/PH3-Auswertung, `_debounce_heater_state()`-Aufruf und das nachgelagerte Kaskaden-Gate (blockieren/pausieren) entfernt; `readings`-Parameter entfällt (nicht mehr gebraucht).
- `controller.py`: Methode `_debounce_heater_state()` und Konstante `_WALLBOX_PAUSE_DEBOUNCE_S` entfernt.
- `db.py`: `init_wallbox_debounce_table()`, `get_wallbox_not_all_phases_since()`, `set_wallbox_not_all_phases_since()` sowie der zugehörige Init-Aufruf entfernt (Tabelle `WallboxDebounce` wird nicht mehr geschrieben).
- `README.md`: Wallbox-Abschnitt an neue Logik angepasst; veraltete `acs`/Auto-Unlock-Beschreibung entfernt (die Zugangskontroll-Logik war bereits am 2026-05-13 aus dem Code entfernt worden, die Doku war seither stale).
- Live getestet: Wallbox blieb nach dem Einstecken durchgehend `frc=0`; go-e ist einmal von selbst auf `lmo=3` zurückgesprungen (Auto-Restore hat via `car_state==1` korrekt wieder auf `lmo=4` zurückgesetzt), kein `frc`-Flattern beobachtet.
- Bekannt/offen: `wallbox.pause_below_storage_temp` und `wallbox.release_above_storage_temp` in `config.py`/`web.py`/`web/index.html`/`web/app.js`/`config.example.json` werden jetzt nirgends mehr ausgewertet (nur noch Altlast in Config/UI, siehe TODO).

## 2026-07-21 – Wallbox pausiert nur noch, wenn Heizstab aktiv läuft (statt "alle 3 Phasen an")

**Idee (User):** go-e im Eco-Modus (lmo=4) lädt selbst nur bei PV-Überschuss. Ist der Heizstab komplett aus, gibt es keinen Verteilungskonflikt – der Controller muss die Wallbox dann nicht extra pausieren, go-e regelt das Überschussladen selbst.

**Vorher:** Die Wallbox wurde immer pausiert, sobald nicht *alle 3* Heizstab-Phasen an waren – auch wenn der Heizstab komplett inaktiv war (0 Phasen, z. B. nachts oder bei zu wenig PV für auch nur PH1). Das war strenger als nötig und sorgte für unnötige Pause-Befehle ohne echten Nutzen.

**Lösung:** `_decide_wallbox()` unterscheidet jetzt 5 Heizstab-Zustände statt nur "alle Phasen an oder nicht":
- `full` (alle 3 Phasen an) → Wallbox frei (wie bisher)
- `idle` (0 Phasen an) → **neu:** Wallbox frei, kein Verteilungskonflikt
- `partial` (läuft, will noch mehr Phasen) → Wallbox pausiert, Speicher priorisiert (wie bisher "nicht alle Phasen an")
- `unknown` (PH1/PH2 nicht lesbar) → Wallbox pausiert, fail-safe (wie bisher)
- `grace` (Wechsel zu partial/unknown, aber noch innerhalb der 90s-Schonfrist) → Wallbox bleibt frei

- `controller.py` `_decide_wallbox()`: Parameter `all_phases_on: Optional[bool]` durch `heater_state: str` ersetzt; neuer "idle"-Zweig gibt die Wallbox frei.
- `controller.py` `_handle_wallbox()`: berechnet `heater_state` aus PH1/PH2/PH3 statt nur `all_phases_on`.
- `controller.py` `_debounce_heater_state()` (vorher `_debounce_all_phases_on()`): debounced nur noch den Übergang zu `partial`/`unknown`, `full`/`idle` werden weiterhin sofort übernommen.

## 2026-07-21 – Wallbox-Pause um 90s verzögert (Debounce gegen kurze PV-Einbrüche)

**Problem:** Auch nach dem Fix von heute Vormittag (Wallbox-Leistung aus Überschuss herausrechnen, siehe unten) stoppte die Wallbox weiterhin häufig, z. B. 4 Minuten nachdem der User in der go-e-App manuell "Eco fortsetzen" geklickt hatte. Ursache diesmal: Die Wallbox-Freigabe hängt an "alle 3 Heizstab-Phasen an". Ein einzelner kurzer PV-Einbruch (z. B. 1 Messwert durch eine vorbeiziehende Wolke) reicht, damit PH2 ausgeht – und der Controller pausiert die Wallbox dann sofort, auch wenn die PV-Leistung eine Minute später schon wieder da ist.

**Lösung:** "Nicht alle Phasen an" wird jetzt erst nach 90 Sekunden ununterbrochenem Bestehen an die Wallbox-Entscheidung weitergereicht. "Alle Phasen an" (Freigabe) bleibt weiterhin sofort wirksam – nur das Pausieren einer laufenden Ladung wird verzögert. Da jeder Cron-Lauf ein frischer Prozess ist, wird der Zeitpunkt, seit wann "nicht alle Phasen an" gilt, in der DB gespeichert (Tabelle `WallboxDebounce`, eine Zeile).

- `db.py`: neue Tabelle `WallboxDebounce` + `get_wallbox_not_all_phases_since()` / `set_wallbox_not_all_phases_since()`.
- `controller.py`: neue Konstante `_WALLBOX_PAUSE_DEBOUNCE_S = 90.0`; neue Methode `_debounce_all_phases_on()`; `_handle_wallbox()` wendet sie auf `all_phases_on` an, bevor es an `_decide_wallbox()` geht.

## 2026-07-21 – Flattern Heizstab/Wallbox behoben: Wallbox-Leistung aus Überschuss herausgerechnet

**Problem:** Heizstab-Phasen und Wallbox schaukelten sich alle 2–3 Minuten gegenseitig hoch: Sobald die Wallbox freigegeben war (weil alle Heizstab-Phasen an waren) und Ladestrom zog, stieg der Hauptzähler (der Ladestrom zählt als Netzbezug). Der Sommermodus sah dadurch "keinen PV-Überschuss mehr" (`significant_pv` prüfte `surplus_without_heater_w`, das die Wallbox-Leistung nicht kennt) und schaltete den Heizstab sofort komplett ab. Dadurch war nicht mehr "alle 3 Phasen an", die Wallbox wurde pausiert – wodurch der echte Überschuss (5000–7000 W PV) wieder sichtbar wurde, der Heizstab erneut ansprang, alle Phasen wieder an waren und die Wallbox erneut freigegeben wurde. Nebeneffekt: Das go-e-`frc`-Kommando "pausiert" kam oft erst eine Minute nach dem Wiederanspringen der Wallbox an, sodass im Log "Speicher priorisiert, Wallbox pausiert" stand, während die Wallbox tatsächlich noch lud.

**Lösung:** Wallbox-Leistung wird jetzt einmal pro Durchlauf gelesen (`readings.wallbox_power_w`) und – wie schon beim Heizstab selbst – aus dem für Heizstab-Entscheidungen und Sommermodus verwendeten Überschuss herausgerechnet (`Readings.true_surplus_w = main_meter - heater_meter - wallbox`). Dadurch sieht die Heizstab-Logik den echten PV-Überschuss unabhängig davon, ob die Wallbox gerade lädt, und schaltet nicht mehr wegen des eigenen Wallbox-Ladestroms ab.

- `models.py` `Readings`: neues Feld `wallbox_power_w`; neue Property `true_surplus_w` (Überschuss ohne Heizstab UND ohne Wallbox).
- `controller.py` `_decide_phase()` und `_check_summer_mode()`: nutzen jetzt `true_surplus_w` statt `surplus_without_heater_w`.
- `controller.py`: neue Methode `_read_wallbox_status()` liest den go-e-Status einmal pro Lauf (statt separat in `_decide_wallbox()`); wird für `readings.wallbox_power_w` UND für die Wallbox-Entscheidung wiederverwendet – vermeidet zwei leicht unterschiedliche go-e-Reads pro Durchlauf.
- `controller.py` `_decide_wallbox()`/`_handle_wallbox()`: nehmen den vorab gelesenen Wallbox-Status als Parameter statt selbst zu lesen.
- `web.py`: `true_surplus` und `wallbox_power` im Status-JSON ergänzt (Sichtbarkeit für Debugging).

## 2026-07-16 – Wallbox blockiert nicht mehr bei unerreichbarem PH3-Shelly

**Problem:** Der Shelly für Heizstab-Phase 3 (192.168.2.36) war im Netzwerk nicht erreichbar. Dadurch war `ph3_on = None` (unbekannt), `all_phases_on` wurde `None`, und die Wallbox blieb dauerhaft in "Speicher priorisiert, Wallbox pausiert" – obwohl 5000–7000 W PV-Überschuss ungenutzt ins Netz gingen und der Speicher (62,4 °C) noch unter dem Hysterese-Band lag.

**Lösung:** Für das Wallbox-Gate "alle 3 Phasen an" zählt jetzt nur noch der bestätigte Status von PH1+PH2. Ist PH3 unbekannt (Sensor/Shelly nicht erreichbar), wird das ignoriert – PH3 wird ohnehin nicht geschaltet, solange der Shelly nicht erreichbar ist. Ist PH3 dagegen bestätigt AUS, gilt weiterhin "nicht alle Phasen an".

- `controller.py` `_handle_wallbox()`: `all_phases_on`-Berechnung geändert – `None` nur noch wenn PH1 oder PH2 unbekannt sind; PH3=`None` führt zu `True` (sofern PH1+PH2 an), PH3=`False` weiterhin zu `False`.

## 2026-07-11 – Sommermodus: Flattern bei laufendem Heizstab behoben

**Problem:** Wenn der Heizstab alle 3 Phasen (~4900 W) lief, verbrauchte er fast den gesamten PV-Strom. Der `Hauptzähler` zeigte dann nur noch -91 W bis -192 W – über dem Schwellwert von -200 W. Damit erkannte der Sommermodus keinen „signifikanten PV-Überschuss" mehr und schaltete den Heizstab ab, obwohl er gerade PV-Energie verbrauchte. Danach stand der volle Überschuss wieder am Netz, der Regler schaltete den Heizstab erneut ein – ein Endloskreis alle 2 Minuten. Nebenwirkung: Wallbox wurde 5×/13 min zwischen pause/release hin- und hergeschaltet.

**Lösung:** `_check_summer_mode()` verwendet jetzt `surplus_without_heater_w` statt `main_meter_power_w` für die `significant_pv`-Erkennung. `surplus_without_heater_w` zeigt den wahren PV-Überschuss unabhängig vom aktuellen Heizstab-Zustand (-4983 W statt -91 W). Die redundante `main_meter_power_w >= -200`-Prüfung im Phasen-Loop wurde ebenfalls entfernt.

- `controller.py` `_check_summer_mode()`: `significant_pv` nutzt `readings.surplus_without_heater_w < -200` statt `main_meter_power_w < -200`.
- `controller.py` Phase-Loop: redundante `main_meter_power_w`-Doppelprüfung bei `in_summer_stop` entfernt.

## 2026-05-29 – Wallbox-Freigabe im Hysterese-Band

**Problem:** Wenn der Heizstab im Hysterese-Band war (z. B. 63–64 °C), wurden keine neuen Phasen aktiviert – aber die Wallbox blieb trotzdem pausiert. Der PV-Überschuss ging ungenutzt ins Netz.

**Lösung:** `_decide_wallbox` kennt jetzt den `temp_status`. Bei `HYSTERESIS_BAND` wird die Wallbox freigegeben, da der Heizstab in diesem Zustand keine neuen Phasen aufschaltet. Die Wallbox läuft im Eco-Modus und nimmt sich ohnehin nur den verfügbaren Überschuss.

- `controller.py` `_decide_wallbox()`: neuer Parameter `temp_status`; Hysterese-Pfad gibt Wallbox frei (frc=0) bevor der Phasen-Check greift.
- `controller.py` `_handle_wallbox()`: übergibt `temp_status` an `_decide_wallbox`.
- `controller.py` `run()`: übergibt `temp_status` an `_handle_wallbox`.

## 2026-05-27 – heater_meter-Ausfall: Schätzung aus Phasenzuständen statt Fail-safe

**Problem:** Wenn der Shelly 3EM für die Heizstab-Messung (heater_meter, 192.168.2.21) nicht erreichbar war, ging das System in Fail-safe (UNCHANGED für alle Phasen) – obwohl zum Schalten kein Messwert benötigt wird.

**Lösung:** Wenn `heater_meter` nicht antwortet, wird der Verbrauch aus den bekannten Phasenzuständen geschätzt (je aktive Phase × 1500 W aus `heater.phase_power_w`). Kein Fehler mehr → System schaltet normal weiter. Ein `WARNING`-Log zeigt an, dass der Schätzwert verwendet wird.

- `controller.py` `_read_all()`: Fallback-Schätzung nach dem Lesen der Phasenzustände; "heater_meter" wird aus `r.errors` entfernt wenn Schätzwert vorhanden.

## 2026-05-25 – Prognose-Treffer: Nur noch Ertrag ≥ Prognose gilt als Treffer

**Änderung:** Die Treffer-Logik für Prognose-Vergleiche wurde verschärft. Bisher galt ein Tag als Treffer wenn die Abweichung ≤ ±25% war. Jetzt gilt nur noch `actual_kwh >= forecast_kwh` als Treffer – d.h. nur wenn der tatsächliche Ertrag die Prognose erreicht oder übertrifft.

- `db.py`: `hit = (actual >= fc)` statt `abs(diff_pct) <= 25`
- `app.js` Dashboard "Letzte Tage": `hit = e.actual_kwh >= e.forecast_kwh` statt `Math.abs(diff) <= 25`
- `app.js` Verlauf → Prognose KPI-Label: "Treffer ±25%" → "Treffer"
- `index.html`: Cache-Buster für `app.js` auf `v=20260525` erhöht

## 2026-05-25 – Temperatur-Chart: NaN-Crash durch fehlende temp-Felder behoben

**Problem:** `renderTempChart` in `app.js` baute das `pts`-Array aus ALLEN Einträgen von `/api/temp-history`, einschließlich solcher ohne `temp`-Feld (Einträge, bei denen nur `wb_w` gesetzt war, z. B. `{'t':'2026-05-25T07:03','wb_w':0.0}`). Das führte zu `undefined`-Werten im Array → `Math.min(...allTemps)` und `Math.max(...allTemps)` gaben `NaN` zurück → alle y-Koordinaten wurden `NaN` → die SVG-Kurve wurde nicht gerendert. Nur die Max-Linie (abhängig von einem festen `maxTemp`-Wert) und die Zeitachsen-Labels (nur x-abhängig) waren sichtbar.

**Fix (`web/app.js`):**
- `.filter(e => e.temp != null)` vor dem `.map()` eingefügt
- `entries.length < 2`-Check auf `pts.length < 2` verschoben (greift jetzt nach dem Filter)

## 2026-05-23 – Cron: flock-Schutz für solaxbb_local.py gegen Zombie-Prozesse

**Problem:** `solaxbb_local.py` lief 3× pro Minute per Cron ohne Schutz gegen Akkumulierung. Bei hängendem Prozess häuften sich Dutzende Instanzen an (zuletzt 62 gleichzeitig). Da das Solax Pocket-WiFi-Modul nur eine HTTP-Verbindung gleichzeitig verarbeitet, blockierten diese Zombies den pvcontroller → `pv_power = null` → Dashboard zeigte "n/a" statt PV-Leistung.

**Fix (Crontab):**
- Alle 3 aktiven `solaxbb_local.py`-Einträge mit `/usr/bin/flock -n <lockfile>` abgesichert.
- Jeder Eintrag hat eine eigene Lock-Datei (`/tmp/solax_local.lock`, `…5.lock`, `…30.lock`).
- Mit `-n` (non-blocking): Läuft bereits eine Instanz, wird der neue Aufruf sofort beendet statt zu warten.

---

## 2026-05-20 – Prognose: Bugfixes isEndstand + Letzte-Tage-Sektion + stabile Prognose-Basis

**Probleme:**
1. `isEndstand` prüfte nur die Stunde des Sonnenuntergangs (z. B. ab 20:00 statt 20:59) → "Endstand" erschien bis zu 59 Minuten zu früh.
2. "Letzte Tage"-Sektion war immer leer: JS verwendete `e.pv_kwh`, aber die API liefert `e.actual_kwh`.
3. "Prognose erreicht ✅" konnte bei 94% angezeigt bleiben, weil `predicted_kwh` live aus der Regression neu berechnet wird und schwanken kann.

**Fix (`web/app.js`):**
- `isEndstand`: exakter Minutenvergleich statt nur Stunden-Vergleich.
- "Letzte Tage": `e.pv_kwh` → `e.actual_kwh` (API-Schlüssel korrigiert).
- `renderForecastProgress`: Gespeicherte Prognose vom Vortag (`histEntries[heute].forecast_kwh`) als Nenner bevorzugen — stabil, kein Schwanken durch Regression. Fallback auf `today.predicted_kwh`.

---

## 2026-05-20 – Prognose-Tagesabschluss: Schwelle für "erreicht" auf 97% angehoben

**Problem:** Bei Tagesende (< 15 min bis Sonnenuntergang) wurde "Prognose erreicht ✅" bereits ab 90% angezeigt, obwohl z. B. 94% nicht wirklich "erreicht" ist.

**Fix:**
- `web/app.js`: Schwelle für "Prognose erreicht ✅" von 90% auf 97% angehoben.
- Schwelle für "Knapp verfehlt" von 70% auf 80% angehoben (80–96% gilt jetzt als knapp verfehlt).

---

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
