# Changelog

## 2026-10-17 – Verbraucher aus openHAB (nur lesen): Card, Minutenwerte, Nacht-Anteil, Warum-Chat

Anlass: In openHAB gibt es viele Geräte mit Leistungsmessung (Shelly-Plugs/1PM, Homematic); sie sollen im Dashboard und in der Nacht-Auswertung erscheinen. Es wird **nichts geschaltet**.
- **Config `consumers`** (`enabled`, `small_avg_w` = 20, `devices`: `item`, `name`, `parent` = vorgeschalteter Zähler, `in_sum`), in den Settings als Tabelle editierbar. openHAB-Zugang aus dem Block `openhab` (Token nie im repr/Log). Prüfung: Item-Namen, doppelte Items, unbekannter/kreisförmiger `parent`.
- **Lesen** (`consumers.py`): pro Lauf 3 REST-Abrufe (Item-Zustände, Thing-Status, Kanal-Links – Links 10 min gemerkt), ≈ 0,4 s, parallel zu den übrigen Geräten. **Veraltet**, wenn das Thing nicht ONLINE ist (OFFLINE/UNINITIALIZED/fehlt/kein Link) oder der Zustand keine Leistung ist; veraltete Werte werden grau angezeigt und nie in Summe/Rest gerechnet. openHAB nicht erreichbar → nur die Card fehlt, Regelung unberührt.
- **Summe/Rest:** ein Gerät zählt, wenn aktuell, `in_sum` und kein vorgeschalteter Zähler schon zählt (kein Doppelzählen). Rest = Hausverbrauch − Heizstab/Wallbox/Whirlpool − gezählte Geräte.
- **Card „Verbraucher“** (rechte Spalte über dem Hauptzähler): Balken je Gerät, Geräte hinter einem Zähler eingerückt, Geräte mit Ø 24 h < `small_avg_w` unter „Sonstige“ (aufklappbar), Rest-Zeile, Fußzeile Haus = Lasten + Geräte + Rest.
- **Minutenwerte** in neuer Tabelle `consumer_min` (nur aktuelle Werte ≥ 1 W + Marker `__read__` je gelesener Minute; 35 Tage wie die übrigen Minutentabellen, auch in `prune_once.py`).
- **Nacht-Card (Tooltip):** Top-5-Verbraucher der Nacht (kWh, mit Abdeckung) und Spitzen über der Entladeleistung je Gerät: jede Spitzen-Minute geht an das stärkste gemessene Einzelgerät (inkl. Whirlpool), wenn es allein den Überschuss zieht, sonst „nicht gemessen“; nur Minuten mit Verbraucher-Lesung.
- **Warum-Chat:** die 5 stärksten aktuellen Einzelgeräte im Erklär-Kontext (`consumers_top` im Schnappschuss).
- Whirlpool kommt weiter nur vom eigenen 3EM (.164), nicht aus openHAB.
- Eingetragen (Dev): 22 Geräte. **FeWo02-Zähler** = ganze FeWo02 außer dem Dusch-Durchlauferhitzer, zählt in der Summe (keine der übrigen Geräte hängen dahinter). **SHEM-3 (192.168.3.29)** misst das andere Haus FeHa.01 und ist deshalb nicht eingetragen.
- Tests `tests/test_consumers.py` (20).

## 2026-10-17 – Dashboard: Hauptzähler und „Bedarf pro Nacht“ in der rechten Spalte

Anlass: Die beiden unteren Cards liefen auf Tablet/Desktop über die volle Breite, die Charts wurden dadurch sehr breit gezogen; rechts blieb neben der langen linken Spalte eine große Lücke.
- „Hauptzähler“ und „Batterie – Bedarf pro Nacht“ stehen jetzt am Ende der rechten Spalte (`.dashboard-bottom` innerhalb `.dashboard-right`), gleiche Breite wie die übrigen Cards, keine Lücke mehr. Auf dem Handy unveränderte Reihenfolge (ganz unten). `styles.css?v=93`.
- **Energiefluss:** Wert- und Unterzeile der unteren Kacheln werden passend zur Kachelbreite verkleinert, damit z. B. „entlädt 2.40 kW“ (Batterie), „freigeg.“ (Wallbox) oder „unter Resume“ im Rahmen bleiben. `app.js?v=20261017e`.

## 2026-10-17 – Whirlpool-Zähler (eigener Shelly 3EM, nur lesen)

Anlass: Der Whirlpool hängt an einem eigenen Shelly 3EM (SHEM-3, MAC 244CAB41ACF6, jetzt 192.168.2.164; das Balboa-Skript verliert zu oft die Verbindung). Er soll im Dashboard, in der Nacht-Auswertung und im Warum-Chat erscheinen.
- **Config `whirlpool_meter`** (`enabled`, `url`, `type` Gen1/Pro, `name`, `subtract_heater_phase` "" | L1 | L2 | L3), in den Settings editierbar. Es wird **nichts geschaltet**, auch nicht das Relais des 3EM.
- **Rechnung** (`whirlpool.py`): Whirlpool = Summe der 3 Phasen; mit `subtract_heater_phase` minus Heizstab-3EM auf dieser Phase (gleicher /status-Abruf), nicht unter 0. Heizstab-3EM nicht lesbar → Wert **unsicher** (None, Hinweis), nicht falsch gerechnet. Whirlpool-3EM offline → None, kein Fail-safe.
- **Phasentest 17.10.** (je 30 s mit Shelly-Auto-Aus, 3-s-Messreihe beider 3EM): PH1 (.93) → Heizstab-3EM L2 ~1555 W (10:58:40–10:59:01), PH2 (.231) → L3 ~1550 W (11:00:47–11:01:02), PH3 (.200) → L1 ~1570 W (11:02:53–11:03:02). Der Whirlpool-3EM blieb dabei auf allen Phasen bei −6 … +5 W: **keine Heizstab-Phase läuft durch seine Messung** → `subtract_heater_phase` bleibt leer.
- `Shelly3EMClient.get_phase_powers_w()` (Gen1 emeters[i].power, Pro a/b/c_act_power).
- **Minutenwerte** in `load_min.whirlpool_w` (Spalte wird bei bestehenden Datenbanken ergänzt; NULL = offline/unsicher).
- **Dashboard:** Kachel „Whirlpool“ im Energiefluss (unten, antippbar) und kleine Card (Leistung, Rohwert/Abzug, Hinweis unsicher/offline).
- **Nacht-Card:** Whirlpool-Anteil am Bedarf und an den Spitzen über der Entladeleistung im Tooltip (`whirlpool_wh`, `whirlpool_peak_wh`, Abdeckung der Messwerte); der Whirlpool bleibt im Bedarf.
- **Warum-Chat:** Whirlpool-Leistung im Erklär-Kontext (Schnappschuss `whirlpool_w`/`whirlpool_uncertain`).
- Tests `tests/test_whirlpool.py` (20): Abzug, nie unter 0, unsicher ohne Heizstab-3EM (kein zweiter Abruf, kein Fail-safe), offline, Config-Prüfung, Client Gen1/Pro, Migration der Spalte, Nachtanteil inkl. Spitzen, Settings, Status-Block, Warum-Kontext.

## 2026-10-17 – Batterie: Entlade-Puffer 40 W Rest-Netzbezug statt Regelung auf 0 W

Anlass: Beim Entladen auf genau 0 W ging bei jedem Lastabfall kurz Batterie-Strom ins Netz (16./17.10. 17–02 Uhr: ~270 Wh, rund 4 % der entladenen 6,9 kWh), bis Minutenlauf oder Wächter nachregelten.
- **Neues Config-Feld `zendure.discharge_target_grid_w`** (Default 40 W, 0–300, in den Settings unter Batterie editierbar): Die Batterie deckt beim Entladen nur bis auf diesen Netzbezug. Sollwert = jetzige Abgabe + Bezug − Puffer, beim Start Bezug − Puffer; Grundlast-Deckel, 2400 W, Startschwelle (50 W Bezug, Sollwert ≥ 50 W) und Schreib-Schwelle (> 50 W Änderung) bleiben. 0 = wie bisher auf 0 W.
- Wächter-Entlade-Drosselung unverändert (Einspeisung > 100 W in zwei 3-s-Takten → senken, < 50 W → Standby).
- Ohne Eintrag in der Prod-Config gilt nach dem Deploy sofort der Default 40 W.
- **Replay 16.10. 16:53 – 17.10. 02:01** (20-s-Messreihe bis 21:00, danach Minutenwerte; Zähler beim Entladen um 40 W Richtung Bezug verschoben, außer die Batterie lief an der 2400-W-Grenze):
  - Entladung ins Netz: 270 → 148 Wh (−122 Wh, −45 %).
  - Netzbezug während des Entladens: 2535 → 2744 Wh (+209 Wh). Diese Energie bleibt in der Batterie und deckt später in der Nacht Bezug, solange die Batterie vor dem Morgen leer wird (wie am 17.10. um 02:01). Netto über die Nacht ≈ 120 Wh weniger aus dem Netz.
  - Näherung: Minutenmittel ab 21:00 glätten kurze Einspeisungen (echte Einsparung eher etwas höher); weniger Wächter-Drosselungen nicht mitgerechnet.
- Tests: Puffer im laufenden Entladen, Einspeisung → Puffer mit abgezogen, Puffer konfigurierbar, Start unter der Mindestschwelle, Grundlast-Deckel weiter gültig, Settings (0–300, kein Geräte-Schreiben); bestehende Erwartungen um die 40 W angepasst, „regelt auf 0 W“ mit Puffer 0.

## 2026-10-17 – Dashboard: „Batterie – Bedarf pro Nacht“ (wie groß müsste der Speicher sein?)

Anlass: Die Batterie (8,64 kWh, ~7,5 kWh nutzbar) war in der Nacht 16./17.10. um 02:02 leer. Gesucht ist, wie groß sie sein müsste, um die ganze Nacht zu überbrücken, und ob die PV einen größeren Speicher überhaupt füllen könnte.
- **Berechnung** (`night_need.py`, aus den vorhandenen Minutenwerten, keine neuen Messungen):
  - Je Minute: Haus-Netto = Zähler + Entladen − Laden (= Verbrauch − PV).
  - Nacht = vom letzten Überschuss am Abend bis zum ersten Überschuss am Morgen (15-min-Mittel). Ohne Überschuss gelten Sonnenunter-/-aufgang (`estimated`).
  - Bedarf = positives Haus-Netto in der Nacht, **ohne Wallbox und Heizstab** (Auto lädt nie aus der Batterie). Aufgeteilt in: aus Batterie, Netz weil Speicher zu klein, Netz über der Entladeleistung (2,4 kW; kommt immer aus dem Netz).
  - Tag = Überschuss zwischen Morgen- und Abendgrenze (eingespeist + geladen). Mehr kann kein Speicher füllen.
  - Laufende Nacht/heutiger Tag als `ongoing` bzw. unvollständig. Nächte mit Lücken (< 90 % Minuten) zählen nicht in die Kennzahlen.
  - Speichergröße = nutzbarer Bedarf ÷ SoC-Fenster (Mindest-SoC bis Ziel-SoC) ÷ 0,95 (gemessen 16./17.10.: 7,5 von 7,8 kWh).
- **Neue Minutentabelle `load_min`** (Wallbox- und Heizstab-Leistung je Minutenlauf, echte Wallbox-Leistung auch außerhalb des Eco-Modus, nicht in der Simulation; Aufbewahrung 35 Tage wie die anderen Minutentabellen). Für Minuten davor wird der Stundenwert aus `pv_hourly_log` genommen und die Nacht als „geschätzt“ markiert.
- **API** `GET /api/history/battery-nights?days=14` (2–34): Nächte, Tage, Kennzahlen (`summary`), `enabled`.
- **Dashboard ganz unten**, nur bei aktivierter Batterie: Balken pro Nacht (Batterie / Netz: Speicher zu klein / Netz: über 2,4 kW), Raute = PV-Überschuss am Vortag, gestrichelte Linie = jetzt nutzbare Kapazität (Beschriftung oberhalb des Diagramms, überdeckt keinen Balken), y-Achse in kWh, kompakte Legende (mobil 1–2 Zeilen). Angeschnittene Nächte (Daten beginnen oder enden > 30 min nach/vor der Nachtgrenze, z. B. Datenbeginn mitten in der Nacht) werden gar nicht angezeigt. Darunter ø/max Bedarf, nötige Speichergröße, wie oft der jetzige gereicht hätte und wie viel die PV höchstens hätte füllen können. Antippen eines Balkens zeigt die Details.
- Erster Stand mit Prod-Daten (5 vollständige Nächte, 11.–15.10., noch ohne Batterie): ø 17,7 kWh Bedarf pro Nacht, ø 3,2 kWh davon über 2,4 kW; für jede Nacht ≈ 19,6 kWh Speicher, der Überschuss am Vortag hätte aber höchstens 12,8 kWh gefüllt ≈ 15 kWh Speicher.
- Tests `tests/test_night_need.py` (21): Grenzen, angeschnittene Nächte (Datenbeginn/-ende), trüber Tag, laufende Nacht, vor Sonnenuntergang keine Nacht, Spitze über Entladeleistung, Batterie-Entladen/-Laden, Wallbox aus Minuten- und Stundenwerten, Last größer als Netto, Lücken, Kennzahlen, `record_loads`, Aufbewahrung, API.

## 2026-10-16 – Batterie: Entlade-Drosselung im Wächter, 60-s-Mittel statt Einzelmessung (kein Minuten-Hin-und-Her)

Anlass: Nach dem Deploy um 15:51 wechselte die Batterie jede Minute zwischen Laden und Entladen, 15:51–16:01 sechsmal. Pulsierende Lasten (Herd/Backofen: ~2 kW für ~7 s, Spitzen bis 10 kW) bestimmten, was der Minutenlauf gerade sah; zeitweise entlud die Batterie ins Netz (15:52:31: 1530 W Entladung bei −3398 W am Zähler).
- **Entlade-Drosselung** (`zendure_watchdog.throttle_tick`, jetzt für Laden und Entladen):
  - Entlädt die Batterie und speist das Haus zwei 3-s-Takte in Folge > 100 W ein → Entladung sofort um die Einspeisung senken (kleinerer der beiden Werte); unter 50 W Standby.
  - Nur senken, nie erhöhen. Zähler zwei Takte nicht lesbar → Standby (nie ins Blaue entladen); beim Laden bleibt „nicht lesbar → nichts tun“.
  - Log: „Zendure-Wächter: Einspeisung … W beim Entladen – sofort von … auf … W gesenkt“ bzw. „Standby geschrieben … Entladen gestoppt“.
- **Ringpuffer** (`meter_ring.py`): Der Wächter liest den Hauptzähler jetzt in jedem Takt, auch ohne Laden/Entladen. Er legt ihn **zusammen mit der Batterie-Ist-Leistung** desselben Takts in eine RAM-Datei (`/dev/shm/pvc-meter-<Hash des DB-Pfads>.txt`, ~2,5 min, kein SD-Schreiben alle 3 s). Ist die Batterie nicht lesbar, gibt es keinen Eintrag. Nach einem Neustart ist der Puffer leer.
- **Gemittelt wird „Netz ohne Batterie“** (Zähler + Entladung − Ladung). Der Minutenlauf setzt das Mittel mit der jetzigen Batterie-Leistung wieder zusammen. Eigene Drosselungen des Wächters mitten in der Minute verändern die Entscheidung so nicht.
- **Nur frische Werte:** Es zählen nur Werte mit Zeitstempel in den letzten 60 s. Hängt der Wächter oder ist er tot, gibt es weniger als 10 frische Werte → Einzelmessung. Zeilen im alten Format ohne Batteriewert zählen nicht.
- **Minutenlauf, nur Batterie**: Einspeisung/Bezug kommen aus dem Mittel der letzten 60 s, für Laden/Entladen, Höhe, Grundlast und Einspeise-Deckel. Unter 10 Werten gilt die Einzelmessung. Die Schwellen bleiben gleich (Laden ab 100 W + 100 W Hysterese, Entladen ab 50 W Bezug), gelten aber für das Mittel. Ist an der Batterie-Position laut Kaskade nichts übrig, lädt sie auch bei besserem Mittel nicht. Heizstab und übrige Kaskade nutzen weiter die Einzelmessung. Log „Kaskade: Batterie nutzt Einspeisung … W (Mittel 60 s, n Werte) statt jetzt … W“; Schnappschuss-Felder `battery_feed_in_avg_w`/`_n`.
- **Replay 15:51–16:01** (20-s-Messreihe + Minutenwerte, Batterie herausgerechnet, echte `plan()`-Funktion):
  - Gemessen (wirklich): 6 Wechsel, ~62 Wh ins Netz entladen, ~21 Wh aus dem Netz geladen.
  - Nachgerechnet „alt“: 6 Wechsel, 44 Wh ins Netz, 52 Wh aus dem Netz geladen.
  - „neu“ (Mittel): 1 Wechsel, 18 Wh / 23 Wh.
  - „neu + Drosselung“: 1 Wechsel, 13 Wh / 15 Wh.
  - Näherung: nur ~4 Werte pro Minute statt 20, Drosselung auf 20-s-Proben (echter 3-s-Takt reagiert schneller).
- Tests (`tests/test_heater_thermostat.py`, jetzt 71; neu u. a.: veralteter Puffer → Einzelmessung, Mittel ohne Batterie, altes Zeilenformat, Drosselung mitten in der Minute ändert die Entscheidung nicht): Laden-Drosselung angepasst (Zählerwert pro Takt). Neu:
  - Entladen: Einspeisung → senken, größer als Sollwert → Standby, Bezug → nie erhöhen, Zähler 2× unlesbar → Standby, Einzel-Ausfall ignoriert.
  - Moduswechsel setzt den Zähler zurück; Leerlauf ohne Aktion.
  - Ringpuffer: jeder Takt auch im Leerlauf, Fenster 60 s, mindestens 10 Werte, Kürzen, fehlende Datei.
  - Kaskade: Herd-Puls beim Messen → kein Entladen, Einspeise-Delle beim Entladen → kein Laden, zu wenige Werte → Einzelmessung, Heizstab-Budget weiter aus der Einzelmessung.

## 2026-10-16 – Heizstab-Thermostat erkennen (Relais an, 0 W), „Speicher praktisch voll“; schnelle Lade-Drosselung im Wächter

Anlass: Der Heizstab schaltet über seinen eigenen Thermostat zwischendurch auf 0 W, obwohl das Relais an ist (Drehregler 70 °C, vermutlich Schichtung). Auswertung Prod-Log 11.–16.10.: 8 Fälle, zusammen 37 min (1–10 min, Median 4,5), immer 13:38–15:45, fast nur ≥ 59 °C (einmal 55,8 °C). Bei ~59 °C kam er nach 2–10 min von selbst wieder; bei 63,5–64 °C schaltete er vor unserer Max-Grenze ab. Der Überschuss (zusammen ~2 kWh) ging ins Netz.
- **Erkennung** (`heater_phases.thermostat_step`, Zustand in Tabelle `heater_thermostat`): Relais an, Heizstab-3EM < 50 W, Speicher ≥ 50 °C. Gilt erst ab der zweiten Messung in Folge, damit kein Fehlalarm direkt nach dem Einschalten kommt. Log beim Erkennen und „Heizstab zieht wieder (… W) nach … min“.
- **Relais bleiben** (`decide_phases(thermostat_open=…)`): keine neue Phase, kein Abschalten übers Budget (keine Schaltspiele). Max-Temperatur, Temperatur unbekannt, Sommer-Stopp und manuell AUS gelten weiter. Grund-Code `heater_thermostat_open`.
- **Kaskade nach Messung**: Eine vorgelagerte Stufe „bis Min“ beansprucht bei offenem Thermostat nur die gemessene Leistung statt ihrer Relais-Phasen, der Rest geht an Batterie und Wallbox. Geräte hinter dem Heizstab bekamen schon bisher „Budget − gemessene Leistung“. Zieht der Heizstab wieder, senken Lade-Deckel bzw. Wächter-Drosselung die Batterie.
- **„Speicher praktisch voll“**: Thermostat ≥ 15 min offen und Speicher ≥ Max − 2 °C → alle Relais aus (auch während der Mindestlaufzeit; manuell AN hat Vorrang), Heizstab-Stufen in der Kaskade inaktiv. Erneute Freigabe erst, wenn der Speicher 2 °C unter den Wert beim Abschalten gefallen ist. Grund-Code `storage_practically_full`. Überschrift „Wasserspeicher praktisch voll“.
- **Dashboard**: Hinweis in der Card „Heizstab Phasen“ (offen seit … min bzw. praktisch voll, Freigabe ab … °C). Bei offenem Thermostat hängt die Begründung oben einen Satz an. API `heater.thermostat {state: open|full, open_min, temp_c, release_c}`.
- **Schnelle Lade-Drosselung** (`zendure_watchdog.throttle_tick`, 3-s-Takt):
  - Auslöser: Batterie lädt und Hauptzähler misst zwei Takte in Folge > 100 W Bezug → Ladesollwert sofort um den kleineren der beiden Bezugswerte senken (bis 0), danach 6 s Pause. Log: „Zendure-Wächter: Netzbezug … W beim Laden – Ladung sofort von … auf … W gesenkt“.
  - Nur senken, nie erhöhen (das macht der Minutenlauf). Zähler nicht lesbar → nichts tun. Unter dem Schreib-Lock wird der Regelzustand neu gelesen; hat der Minutenlauf inzwischen geschrieben, unterbleibt die Senkung. `written_at` wird gesetzt, damit die Fremdregler-Erkennung die eigene Senkung nicht meldet.
  - Der Hauptzähler wird nur gelesen, solange die Batterie lädt. Gemessen (Shelly Gen1 .183, 1 Abfrage/s über 2 min): Median 39 ms, p95 111 ms, max. 173 ms, 0 Fehler.
  - Der Wächter läuft jetzt auch ohne `wallbox.url` (der Wallbox-Teil gilt dann als „lädt nicht“).
- Tests `tests/test_heater_thermostat.py` (41): Thermostat-Zustand (zweite Messung, Wiederkommen, Relais aus, kalt/unbekannt, praktisch voll nach 15 min nahe Max, Freigabe nach 2 °C, lange offen ohne Max-Nähe), Phasen (halten ohne Budget, keine neue Phase, Max-Temp gewinnt, praktisch voll schaltet in der Mindestlaufzeit ab, manuell AN), Kaskade (Min-Stufe nach Messung, praktisch voll inaktiv, Sommer: Überschuss an die Batterie, Heizstab zieht wieder), Web-Status, Drosselung (2 Takte, Einzelspitze, bis 0, Zähler unlesbar, nicht ladend, Einspeisung, Pause und erneute Senkung, dry_run, Minutenlauf schrieb dazwischen, Schleife ohne Fremdregler-Fehlalarm, ohne Wallbox). Suite: 757 grün. `app.js?v=20261016e`, `styles.css?v=89`.

## 2026-10-16 – Batterie: Fremdregler (App/HEMS) erkennen → Standby + roter Hinweis; Heizstab weicht der wartenden Batterie

Anlass: In der Zendure-App schaltete sich HEMS wieder ein. Der App-Energieplan regelte dann mit Hauptzähler auf 0 W: am 16.10. 13:01–14:45 und 15:14–15:30 änderten sich `inputLimit` (alle 20 s, z. B. 1260 → 2253 → 1429 W) und `outputLimit` (Entladen 193–1963 W gegen den Regelzustand, teils in den Heizstab) ohne Befehl von pvcontroller. Zusätzlich stand `minSoc` am Gerät auf 50 (5 %) statt 100.
- **Erkennung** (`zendure_control.foreign_deviation`): Gerätewerte weichen > 50 W vom Regelzustand ab, ohne dass pvcontroller in den letzten 10 s geschrieben hat (`zendure_ctl.written_at`, neu, bei jedem eigenen Befehl). Fälle: Gerät entlädt gegen den Regelzustand, `inputLimit`/`outputLimit` anders als geschrieben. acMode allein zählt nicht. Kein Fehlalarm, wo das Gerät selbst stoppt: Laden bei SoC ≥ Ziel − 1, Entladen bei SoC ≤ Mindest-SoC + 1.
- **Wächter** (`zendure_watchdog.py`): liest das Gerät jetzt in jedem 3-s-Takt (bisher nur beim Minutenstart; damit sieht auch der Wallbox-Wächter Entladen nach App-Eingriff sofort). Zwei Takte hintereinander Abweichung → `record_foreign` + **sofort Standby** (Log „Standby geschrieben … Fremdregler aktiv (…) – HEMS in der Zendure-App prüfen“). Standby höchstens alle 30 s, solange der Fremdregler weiter eingreift. Bei dry_run nur beobachten.
- **Minutenlauf**: prüft dasselbe vor der Planung (Log „Fremdregler aktiv … (Minutenlauf)“, schreibt dann wie bisher den Regelzustand neu). `minSoc` am Gerät ≠ Config → Hinweis und **nachschreiben** (Flash, mit Rücklesen, höchstens einmal pro Stunde, `min_soc_fixed_at`).
- **Log**: neue Episode = ERROR „Zendure: Fremdregler aktiv – … HEMS / Energieplan in der Zendure-App prüfen“, weitere Erkennungen INFO.
- **Dashboard**: roter Hinweis oben („Fremdregler aktiv – HEMS in der Zendure-App prüfen (seit 15:14, zuletzt 15:29)“) und in der Batterie-Card mit Detail (z. B. „Gerät entlädt bis 337 W (Regelung: Laden 0 W) – Wächter“), dazu der Mindest-SoC am Gerät. Steht 15 min nach der letzten Erkennung. API: `zendure.foreign {active, since, last, detail, device_min_soc}`.
- HEMS selbst ist über die lokale API nicht direkt lesbar. Die Properties enthalten kein `hems*`-Feld, und `smartMode`, `IOTState`, `bindstate`, `oldMode` sind bei HEMS aus dieselben wie vorher unbekannt. Ein Property-Protokoll (alle 5 s) läuft, um ein Merkmal zu finden, falls HEMS wieder angeht.
- **Heizstab weicht der wartenden Batterie** (Fall 14:06): Bekommt die Batterie wegen des Einspeise-Deckels weniger, als ihr zusteht (Wunsch − Sollwert > 50 W), gilt auch fürs Abschalten der jetzige Überschuss statt des 2-min-Mittels (`budget_down = min(Mittel, jetzt) − Batterie`). Der Heizstab gibt seine Phase so im selben Lauf ab. Die Mindestlaufzeit (420 s) bleibt (User-Entscheidung). Begründung im Log: „Batterie wartet → ab ohne Mittel“.
- **Überschrift „Batterie priorisiert“** statt „Wasserspeicher priorisiert“, wenn die Batterie lädt oder wartet und im aktiven Profil vor dem gerade zählenden Heizstab-Schritt steht („bis Min“ unterhalb Min, sonst „bis Max“).
- Gemessen (Prod-Gerät, nur lesen): `/properties/report` antwortet in 32 ms (Median), 130 ms (p95), max. 200 ms bei 2 Abfragen/s, Werte live. 3-s-Takt = ~20 Abfragen/min. `app.js?v=20261016d`, `styles.css?v=88`.
- Mindest-SoC am echten Gerät geprüft: 50 → 150 → 100 geschrieben und zurückgelesen (Einheit 0,1 % bestätigt).
- Tests `tests/test_zendure_foreign.py` (35): Abweichung (Entladen gegen Laden, inputLimit, kleine Differenzen, acMode allein, Gerät voll/leer), Wächter (2 Takte → Standby, Schonfrist nach eigenem Befehl, Einzelausreißer, dauerhafter Fremdregler → Standby höchstens alle 30 s, dry_run, passendes Gerät), Minutenlauf (Hinweis, written_at, minSoc einmal pro Stunde, dry_run), Status-Block, Heizstab bei wartender/voller Batterie, Überschrift (Winter, Sommer kalt/warm, Batterie idle). Suite: 716 grün.

## 2026-10-16 – Energiefluss: Kacheln antippbar → Card

- Jede Kachel im Energiefluss-Diagramm ist antippbar und scrollt weich zur passenden Card im Dashboard: PV/Pergola → „PV Erzeugung“, Netz → „Hauptzähler“, Haus → „Hausverbrauch“, Heizstab → „Heizstab Phasen“, Wasserspeicher → „Wasserspeicher“, Batterie → „Batterie“, Wallbox → „Wallbox (go-e)“, eBike/Shelly → „Kaskade“ (dort stehen die Geräte). Die Card pulsiert 1 s mit grünem Rahmen.
- Sticky Kopfzeile: `scroll-margin-top` aus ihrer echten Höhe (+12 px, CSS-Variable `--sticky-h`, bei Größenänderung neu). Bei „Bewegung reduzieren“ ohne Animation.
- Tap-Feedback: Zeiger-Cursor, Rahmen wird beim Hover/Fokus/Antippen kräftiger; per Tastatur (Tab + Enter/Leertaste) bedienbar (`role="button"`, `aria-label`). Ein Handler auf dem Container, ohne neue Library. Neue Card-IDs: `pv-card`, `house-card`, `heater-card`, `storage-card`, `meter-card`. `app.js?v=20261016c`, `styles.css?v=87`.

## 2026-10-16 – Batterie: nie entladen, solange die Wallbox lädt (Wächter 3 s); Mindest-Ladestand einstellbar

Regel (User): Solange die Wallbox lädt, wird nie aus der Batterie entladen – Auto und Haus kommen dann aus dem Netz. Laden aus PV-Überschuss bleibt erlaubt.
- **Erkennung „Wallbox lädt“** (`clients/goe_client.car_charging`): go-e `car=2` **oder** Ladeleistung `nrg[11]` > 100 W (vorher beides gleichzeitig – in der Anlauframpe mit car=2 und < 100 W war Entladen noch erlaubt). Nicht lesbar = lädt.
- **Auch ohne Wallbox-Steuerung:** Die Batterie-Sperre liest go-e, sobald `wallbox.url` gesetzt ist (eigener Client, Timeout 2 s), auch bei `wallbox.enabled=false` (Auto lädt z. B. im Basic-Modus).
- **Sperre fest:** Schalter `zendure.block_discharge_while_wallbox_charging` entfällt (Settings-Checkbox weg; alter Config-Eintrag wird ignoriert und beim nächsten Speichern der Settings entfernt).
- **Zendure nicht lesbar + Wallbox lädt:** sofort Standby (blind geschrieben), nicht erst nach dem 3. Lesefehler.
- **Wettlauf:** Alle Zendure-Schreibbefehle (Minutenlauf, Wächter, Watchdog, Settings) laufen über ein gemeinsames Schreib-Lock (`/tmp/pvc-zendure-write.lock`, `zendure_control.write_guard`). Der Minutenlauf liest go-e unter dem Lock unmittelbar vor jedem Entlade-Befehl erneut; „lädt“/nicht lesbar → Standby statt Entladen (Log „Entladen verworfen – Wallbox lädt (vor dem Schreiben geprüft)“). Ohne Lock (10 s belegt) wird nie entladen.
- **Wallbox-Wächter** in `zendure_watchdog.py`: nach der Watchdog-Prüfung Schleife bis Sekunde 58 der Minute, alle 3 s: entlädt die Batterie (Regelzustand „discharge“ oder Gerät meldete beim Minutenstart `outputLimit` > 0) und lädt die Wallbox oder go-e ist nicht lesbar → sofort Standby (Log „Wächter: Wallbox lädt – Entladen sofort gestoppt“). Reaktionszeit beim Ladestart: ≤ 3 s Takt + ≤ 2 s go-e + < 5 s Gerät (vorher bis ~65 s). Lädt die Batterie, tut der Wächter nichts (liest dann auch go-e nicht).
- **Prozess:** Cron mit eigenem Lock `flock -n /tmp/pvc-zendure-watch.lock` (nie zwei Instanzen; neue Zeile in `deploy/cron.example`). `pvc-code.lock` hält das Skript nur beim Start (Import + Config, geteilt); jeder Takt prüft, ob deploy.sh den Lock exklusiv hält oder sich der Code-Stand (git HEAD) geändert hat → Schleife endet, der nächste Cron-Lauf startet mit neuem Code. deploy.sh wird so nicht blockiert, keine Änderung an deploy.sh nötig. Leichtgewichtiges Logging ohne main.py-Import (schnellerer Start).
- **Mindest-Ladestand** (Settings „Mindest-Ladestand“: Auswahl 5 / 10 / 15 / 20 %, Default 10 %; API nimmt nur diese Werte):
  - Software: Entladen stoppt bei `min_soc`, Freigabe erst wieder ab `min_soc` + 2 % (läuft eine Entladung, geht sie bis zur Grenze weiter); SoC unbekannt → kein Entladen.
  - Gerät: zenSDK hat `minSoc` (RW, Gerät rechnet in 0,1 %: gelesen 100 = 10 %). Bei Änderung in den Settings wird `minSoc` = Wert × 10 mit `smartMode 0` (Flash) geschrieben und nach 3 s zurückgelesen; die Settings-Meldung zeigt „Mindest-Ladestand am Gerät bestätigt (15 %)“ bzw. den Fehler. Nur bei Änderung (Flash-Zyklen), nicht bei dry_run. So entlädt das Gerät auch ohne Pi nicht unter die Grenze.
  - Dashboard: Mindest-SoC-Linie im Chart sowie „Nutzbar“/„Reicht noch“ nutzen den eingestellten Wert (kam schon aus der Config).
- `ZendureClient`: `write_props()` (allgemein, erkennt `success: false` als Fehler), `get_status(fresh=True)` (ohne Fehler-Pause, für das Rücklesen).
- Beobachtung: Das Gerät meldet `smartMode` nur ~3 s nach einem Schreibbefehl als 1, danach wieder 0 – passt zu „gilt für den einen Befehl (RAM)“, kein Hinweis auf Flash-Schreiben im Minutentakt.
- Tests `tests/test_zendure_wallbox.py` (34): Erkennung (Rampe 50 W, > 100 W ohne car=2, unlesbar), Kaskade (Auto lädt + Bezug → 0/0, go-e unlesbar → 0/0, Rampe → 0/0, Auto fertig → Entladen wieder, Ablauf entladen → Auto → Standby → fertig → entladen, **Wettlauf**: Auto startet zwischen Planung und Schreiben → nie acMode 2, Laden aus Überschuss trotz Auto erlaubt, Sperre mit `wallbox.enabled=false`), Zendure unlesbar + Wallbox → sofort Standby, Wächter (Auto startet → Standby im selben Takt, go-e unlesbar, Batterie lädt → nichts, Zendure unlesbar, Gerät entlädt nach App-Eingriff, Schleife 20 Takte à 3 s ohne Doppelschreiben, Stopp bei Deploy, exklusiver Code-Lock erkannt, Code-Stand-Wechsel), Schreib-Lock exklusiv / ohne Lock kein Entladen, Mindest-SoC (Grenze, Hysterese, unbekannt, 20 %), Gerät schreiben + Rücklesen (ok, Abweichung, Schreibfehler), Settings (Änderung → Gerät, unverändert → nichts, nur 5/10/15/20, alter Schalter entfernt, dry_run). `test_wallbox_block_can_be_disabled` → `test_wallbox_block_cannot_be_disabled`; Kaskaden-Tests mit Fake-go-e (keine Netzanfragen). Suite: 681 grün.
- Dev-Lauf (dry_run, temporäre Config mit `control_enabled`, echte Geräte nur gelesen): Wächter 13:23:00–13:23:58, zweite Instanz per `flock -n` abgewiesen.
- **Reihenfolge beim Ausrollen:** zuerst die Crontab-Zeile des Watchdogs auf die neue Form umstellen (alte Zeile hielte `pvc-code.lock` 58 s), dann deployen.

## 2026-10-16 – Batterie: direkter Wechsel Laden/Entladen, Winterprofil, Charts im Dashboard

Anlass: Erste 15 min Regelbetrieb in Prod (12:00–12:15). Die 2-min-Pause nach jedem Moduswechsel ließ die Batterie im Standby, obwohl 400–740 W aus dem Netz kamen (12:06–12:08) bzw. 3 kW eingespeist wurden (12:13–12:15).
- **Kein Standby mehr zwischen Laden und Entladen** (`zendure_control.MIN_DIRECTION_S` 120 → 0): Das Gerät stellt in < 5 s um (Schreibtest). Flattern verhindern weiter die Schwellen: Laden ab 100 W + Hysterese (100 W) Überschuss, Entladen ab 50 W Bezug. Tests: `test_direct_switch_without_pause`, `test_small_surplus_no_flapping` (ersetzen `test_pause_after_mode_change`), `test_direct_switch_respects_wallbox_lock` (Laden → Auto lädt + Bezug → Standby), `test_direct_switch_never_feeds_heater_or_car` (Laden → Bezug: Entladen nur bis zur Grundlast bzw. gar nicht); `test_decisions` prüft `mode_change_pause` mit gepatchtem `MIN_DIRECTION_S`.
- **Winterprofil (Prod, DB):** `eBike → Batterie → Wasserspeicher bis 40 °C → Wasserspeicher bis Max → Wallbox` (Batterie vor dem Wasserspeicher, im Winter heizt das Öl). Sommer unverändert: `eBike → Wasserspeicher bis 52 °C → Batterie → Wasserspeicher bis Max → Wallbox`.
- **Dashboard-Card „Batterie“:** Chart heute mit Füllstand (Fläche, Mindest-SoC gestrichelt) und darunter Laden (grün, nach oben) / Entladen (orange, nach unten) als 5-Minuten-Balken mit Tooltip (Uhrzeit, Leistung); Legende. Neue Zeilen „Nutzbar“ (Inhalt über Mindest-SoC, z. B. „4.6 von 8.6 kWh“) und „Reicht noch“ (beim Entladen, bis Mindest-SoC) bzw. „Voll in“ (beim Laden, bis Ziel-SoC) bei der aktuellen Leistung. Verlauf → Tag nutzt dieselbe Zeichenfunktion (`zendureChartSvg`), jetzt auch mit Mindest-SoC-Linie und Tooltips. `app.js?v=20261016a`, `styles.css?v=86`.
- Dev: `zendure` in der Dev-Config nur lesend eingetragen (`control_enabled=false`, `dry_run`), heutige Minutenwerte aus Prod in die Dev-DB kopiert – zum Ansehen auf Port 8001.

## 2026-10-16 – Zendure SolarFlow 2400 AC+: am echten Gerät geprüft, Abgleich, Fail-safe, Watchdog

Anlass: Der SolarFlow 2400 AC+ (192.168.2.134, 3 Akkupacks, 8,64 kWh) ist eingebaut. Im Lieferzustand entlud er per App fest 200 W – auch bei 1,5 kW Einspeisung, also Akku → Netz.
- **Schreibtest am Gerät** (beaufsichtigt, je ~25 s, danach Ausgangszustand wiederhergestellt): `acMode 1` + `inputLimit 300` → lädt 300 W aus dem Hausnetz (`gridInputPower` = `outputPackPower` = 300), `acMode 2` + `outputLimit 100` → entlädt 100 W (`packInputPower` = `outputHomePower`), `inputLimit = outputLimit = 0` → Standby; Umstellung jeweils < 5 s, Antwort `success: true`. `packState` 0 Standby / 1 lädt / 2 entlädt. Mapping in `clients/zendure_client.py` als geprüft vermerkt (vorher „unverifiziert“). `socSet`/`minSoc` meldet das Gerät in 0,1 % (1000/100 = 100 %/10 %); werden weiterhin nicht geschrieben.
- **Abgleich mit dem Gerät** (`zendure_control.device_drift`): Meldet das Gerät andere Sollwerte (`acMode`, `inputLimit`, `outputLimit` > 50 W daneben) als der gespeicherte Regelzustand, schreibt die Regelung den Zustand erneut und warnt im Log („Gerät weicht vom Regelzustand ab (outputLimit 200 statt 0) – schreibe idle 0 W erneut“). Ursachen: App/HEMS stellt um, Geräte-Neustart (`smartMode=1` schreibt nur ins RAM, danach gelten wieder die App-Werte). Ohne den Abgleich wäre die feste 200-W-Entladung beim Einschalten der Steuerung stehen geblieben (Regelzustand „idle“ = kein Schreibbedarf).
- **Fail-safe → Standby:** Hauptzähler oder Wechselrichter nicht lesbar → die Batterie geht sofort in Standby (Limits 0, hart, auch während der Wechselpause und bei „Laden erzwingen“), statt den letzten Wert zu halten (Grund-Code `failsafe_global`, Text „Fail-safe: Hauptzähler nicht lesbar – Batterie im Standby“). Die übrigen Kaskaden-Geräte nutzen weiter den Solax-Fallback. Bricht der Kaskaden-Lauf mit einer Ausnahme ab, schreibt `main.py` ebenfalls Standby.
- **Laden nur aus echter Einspeisung:** Der Ladesollwert ist jede Minute höchstens „jetzige Ladeleistung + jetzige Einspeisung am Hauptzähler“. Kommt eine Wolke, wird er im nächsten Lauf auf den tatsächlichen Überschuss gesenkt (bei Bezug auf 0). Absenkungen werden immer geschrieben, nicht erst ab 50 W (vorher konnten bis zu 50 W dauerhaft aus dem Netz geladen werden); Erhöhungen weiter erst ab > 50 W. Steht die Batterie vor dem Heizstab und läuft der Heizstab noch (Mindestlaufzeit), wartet die Batterie („wartet auf 1500 W – nur was gerade eingespeist wird“); das Heizstab-Budget rechnet aber mit dem gewünschten Wert, damit der Heizstab Platz macht. „Laden erzwingen“ ist ausgenommen.
- **Watchdog** `zendure_watchdog.py` (eigener Cron jede Minute + `@reboot`, siehe `deploy/cron.example`): zenSDK kennt laut Doku keinen Heartbeat und keine Gültigkeitsdauer (ein Sollwert gilt, bis ein neuer kommt; `smartMode=1` → nach Geräte-Neustart wieder die Flash-Werte). Ist die Regelung (`zendure_ctl.decided_at`) älter als 5 min oder nie gelaufen (main.py hängt, Kaskade aus, Absturz) → Standby schreiben, sofern das Gerät nicht schon steht; Log „Zendure: Standby geschrieben (Limits 0) – Watchdog: Regelung läuft nicht (seit 6 min)“. Beim Pi-Start sofort Standby (bis zu 12 Versuche à 15 s). Nur mit `control_enabled`; bei `dry_run` nur Log. Der Standby setzt den Herzschlag nicht neu. Grenze: Ist der Pi ganz aus oder hängt das System komplett, kann nichts schreiben – dann gilt der letzte Sollwert weiter.
- **Profile in Prod gespeichert** (vorher nur Default, gleiche Reihenfolge): Winter und Sommer `eBike → Wasserspeicher bis Min (Winter 40 °C / Sommer 52 °C) → Batterie → Wasserspeicher bis Max → Wallbox`.
- Kapazität: wird nur für die Zyklen-Schätzung (Verlauf → Monat) genutzt und kommt aus der Config (8640 Wh); keine Simulation/Prognose rechnet mit 2,4 kWh (2400 nur als Default ohne Config).
- Tests: `DriftTest` (4) in `tests/test_zendure_control.py`, `tests/test_zendure_safety.py` (19): Fail-safe (Planung und Kaskade mit Hauptzähler/WR nicht lesbar), Einspeise-Deckel (Wolke, Bezug, kleine Absenkung, Heizstab läuft noch, Override), Watchdog (Cron 6 min weg → Standby, danach kein Doppelschreiben; 4 min → ok; nie gelaufen + Gerät nicht lesbar; dry_run; Pi-Start mit Wiederholung; Herzschlag bleibt), Kaskaden-Absturz → Standby.
- Prod-Config: `zendure.enabled=true`, `ip`, `sn`, `capacity_wh=8640` (nur Lesen, `control_enabled` bleibt aus bis zur Freigabe).

## 2026-10-16 – CLAUDE.md: Abschnitt „Offene Prüfungen“

- Neuer Abschnitt in `CLAUDE.md`. Erster Punkt: Am 26.10.2026 prüfen, ob der tägliche ~1-min-Aussetzer der Shelly 3EM (Gen1) nach der Zeitumstellung um 01:01 statt 02:01 kommt (Theorie: Firmware-Vorgang um 00:00 UTC). Analyse 15./16.10.: um 02:01 fallen nur die 3EM aus (Hauptzähler, Heizstab, Whirlpool, SHEM-3), alle anderen Geräte laufen durch; Shelly-Cloud ist nicht die Ursache (Heizstab-3EM fiel auch ohne Cloud aus). Kein Handlungsbedarf.

## 2026-10-15 – Rollladen-Zeitpläne: Endlagen immer senden

Anlass: Nach dem Stromausfall am 15.10. (15:15–16:22) meldeten die Homematic-Aktoren ohne Fahrt 100 % (Wohnen links/rechts um 16:23, Esszimmer und Hintereingang um 17:40), die Rollläden waren aber offen. Der Zeitplan „Sonnenuntergang zu“ um 18:33 hat bei Wohnen rechts und Esszimmer „steht schon dort, nichts gesendet“ gemeldet.
- **Zeitgesteuerte Fahrten auf 0 % oder 100 %** werden jetzt immer gesendet, auch wenn openHAB die Zielposition schon meldet (an den Anschlag fahren ist harmlos). Die Richtung gilt dann wie bei unbekannter Position, d. h. alle Schutzregeln greifen weiter: Tür offen/Kontakt unbekannt (Aussperrschutz, Warten auf Tür), Tür ohne Kontakt nur bis zum Spalt, Frost beim Öffnen, Vorrang manueller Bedienung, Duty Cycle, „fährt gerade“, Zeitbudget, dry_run.
- **Zwischenpositionen** (z. B. 70 %) weiterhin nur bei Abweichung > 2 %.
- **Tür ohne Kontakt (z. B. Hintereingang, schließt automatisch nur bis zum Spalt):** Meldet das Steuer-Item eine Endlage (100 %), gilt sie wie bei den anderen als unsicher → der Zu-Plan fährt den Spalt (80 %) an, auch wenn das einen wirklich geschlossenen Rollladen leicht anhebt (lieber 20 % offen als ganz offen). Nur bei verlässlicher Position – eigenes Lese-Item/Shelly oder eigene Fahrt dorthin mit bestätigter Rückmeldung (`status ok`, gleiche Position) – fährt „zu“ nie hoch: steht er schon auf dem Spalt oder weiter zu, bleibt er. Zwischenwerte aus dem Steuer-Item (z. B. 90 %) bleiben ebenfalls (vorher fuhr „zu“ von 90 % auf den Spalt hoch).
- **Log eindeutig:** „…: Befehl gesendet“ (bei Endlage mit gleichem gemeldetem Wert: „(openHAB meldete schon 100 % – Endlage wird trotzdem angefahren)“, beim Spalt: „(openHAB meldete 100 % – Endlage unsicher, Spalt wird angefahren)“) bzw. „…: unverändert – steht schon bei X %, kein Befehl gesendet“. Verlauf (`schedule_log`): `sent` mit `forced_end: true`.
- Unverändert: manuelle Bedienung (Buttons), Chat-Sofortbefehle, Rollläden mit Automatik „openHAB-Regel“, Heizungs-Zeitpläne. Ein Zeitplan-Ereignis sendet weiterhin höchstens einmal (`last_run`).
- Tests: `EndPositionTest` (23) in `tests/test_shutter_schedule.py`: falsche 100 % nach Stromausfall, falsche 0 %, unbekannte Position (NULL), Zwischenposition gleich/abweichend, dasselbe Ereignis über das Nachhol-Fenster und zwei Einträge in derselben Minute → ein Befehl, Tür offen/zu, Tür ohne Kontakt (Spalt; gemeldete 100 % ohne bzw. nach anderer bestätigter Fahrt → Spalt; bestätigte 100 %, Lese-Item 100/90 %, Steuer-Item 90 % → bleibt), Frost, manuelle Bedienung, Duty Cycle, dry_run, „fährt gerade“, openHAB-Regel-Rollladen. 4 bestehende Tests auf Zwischenpositionen umgestellt (prüften „unverändert“ an der Endlage). Suite: 621 grün.
- Dev-Abnahme gegen Kopie der Prod-DB und echte openHAB-Werte (nur gelesen, dry_run, Senden gesperrt), 18:33 nachgestellt: Prod-Code „steht schon dort“ ×3, Dev-Code DRY-RUN „zu“ für Wohnen links, Wohnen rechts, Esszimmer; Folgeminuten nichts doppelt; Hintereingang 22:01 (Tür ohne Kontakt, meldet unsicher 100 %) → DRY-RUN 80 % (Spalt).

## 2026-10-14 – Chat: Ersatzmodell (`assistant.fallback_model`) und neue GPT-Modelle

Anlass: OpenAI stellt `gpt-4.1` und `gpt-4.1-mini` am 14.01.2027 ein (empfohlener Nachfolger laut Deprecations-Seite: `gpt-6-luna`).
- **`assistant.fallback_model`** (neu, optional, nur config.json): Meldet die API für `model` bzw. `explain_model` „Modell nicht gefunden/eingestellt“ (Fehlerart `model`: 404 oder 400 `model_not_found`), wiederholt der Chat die Anfrage einmal mit dem Ersatzmodell. Warnung ins Log, das Modell wird in der DB gemerkt (Tabelle `assistant_model_unavailable`); die nächsten 24 h gehen Anfragen direkt an das Ersatzmodell (kein unnötiger 404), danach wird das eigentliche Modell wieder versucht – antwortet es, verschwindet die Warnung. Andere Fehler (Key, Guthaben, Rate-Limit, Zeitüberschreitung) lösen keinen Wechsel aus. Ohne `fallback_model` bleibt alles wie bisher.
- **Chat-Status:** neues Feld `warning` („Modell „gpt-4.1-mini“ nicht verfügbar (seit …) – Chat nutzt Ersatzmodell „gpt-6-luna“. Bitte assistant.model in config.json umstellen.“) und `fallback_model`; der Zähler in der Chat-Leiste zeigt dann ⚠ (Text im Tooltip). Die Antwort, die das Ersatzmodell gegeben hat, trägt `fallback` → Hinweiszeile im Chat. Die Warnung gilt nur, solange die Config das gemerkte Modell noch nennt.
- **`reasoning_effort="none"` für neuere GPT-Modelle** (`gpt-5.1` ff., `gpt-6-*`): Diese denken per Default nach und können in Chat Completions nur mit `none` Tools aufrufen. `gpt-4.1`/`gpt-4o` bekommen den Parameter nicht. **Ohne diese Änderung funktionieren `gpt-6-luna`/`gpt-6-sol` im deployten Code nicht** – Config in Prod erst nach Deploy umstellen. `gpt-6.1-sol` kann in Chat Completions gar keine Tools (nur Responses API) und ist damit nicht nutzbar.
- **Eingebauter OpenAI-Standard** (ohne `assistant.model` in der Config): `gpt-6-luna` statt `gpt-4.1-mini`; `config.example.json` ebenso.
- Tests: `tests/test_chat_fallback.py` (12): Config, reasoning_effort je Modell, Wechsel bei 404 inkl. Warum-Frage (eigenes Tool-Schema), direkt an Ersatz nach Merken und erneuter Versuch nach 24 h, ohne Ersatz unverändert, Ersatz ebenfalls weg, andere Fehler ohne Wechsel, Warnung weg nach Config-Änderung, Web-Antwort/Status mit Warnung, Key nie in Log/Antwort. Suite: 598 grün.
- Vergleich mit echten Anfragen (6, Kopie der Prod-DB, nichts übernommen): `gpt-6-luna` setzt beide Zeitplan-Befehle aus Prod genauso um wie damals `gpt-4.1-mini` und den Rollladen-Befehl mit Sonnenbezug richtig; Warum-Frage Heizstab (`gpt-6-sol`) und Kinderzimmer (`gpt-6-sol` und `gpt-6-luna`) stimmen mit der Grundlage und nennen beim Kinderzimmer erstmals beide Abschnitte (vor/nach Wechsel 07:01).

## 2026-10-14 – Hauptzähler: Shelly Pro 3EM (Gen2) und Zählertausch ohne Sprung

Vorbereitung auf den Tausch Shelly 3EM (Gen1) → Shelly Pro 3EM (Gen2). Vorher las der Code die Leistung des Hauptzählers nur über Gen1 `/status` (ein Pro 3EM hätte sofort Fail-safe ausgelöst); ein Typ-Feld gab es nur für die Phasen-Shellys.
- **`shelly.main_meter_type`** (neu, optional): `shelly_3em_gen1` (Default, bisheriges Verhalten) oder `shelly_pro_3em`. Fehlt das Feld, bleibt alles wie bisher.
- **Pro 3EM im Client (`Shelly3EMClient`, `type_`):** Leistung über `/rpc/EM.GetStatus?id=0` → `total_act_power` (fehlt es: Summe `a/b/c_act_power`), Energiezähler über `/rpc/EMData.GetStatus?id=0` → `total_act` / `total_act_ret`. **Vorzeichen:** wie bisher Bezug positiv, Einspeisung negativ – Gen1 `total_power` und Pro `act_power` zählen gleich, es wird nichts umgerechnet (am Gen1-Zähler geprüft: −525 W bei PV-Einspeisung). Controller und Kaskade nutzen den Typ; der Heizstab-Zähler bleibt Gen1.
- **Zählertausch ohne Sprung (`db.track_meter`, `meter_snapshots` um `meter_id`, `last_*`, `carry_*` ergänzt):** Jeder Controller-Lauf schreibt den Stand fort. Erkennt er ein anderes Gerät (Typ|Adresse) oder einen rückwärts laufenden Zähler (neues Gerät startet bei 0, Reset; Toleranz 100 Wh), übernimmt er den bisherigen Tagesanteil und setzt den neuen Stand als Basis. Tageswert = Übertrag + (aktuell − Basis): am Tauschtag kein negativer Wert, kein Sprung, die Shelly-Werte des Tages bleiben erhalten (vorher fiel der ganze Tag auf die Solax-Zähler zurück). Es fehlt nur der Verbrauch in der Umbauzeit. Ein IP-Wechsel desselben Geräts läuft ebenso nahtlos weiter. Zusätzlich gilt ein Tageswert über 1 000 kWh als falscher Zählerstand (z. B. Web liest kurz nach dem Tausch noch den alten) → Solax. Tag/Woche/Monat/Jahr lesen die so gespeicherten Tageswerte (`pv_daily_log`), die Leistungskurven kommen aus der Momentanleistung – dort gibt es keine Zählerstände.
- **Settings:** Auswahl „Hauptzähler Typ“ unter Shelly IPs und Knopf **„Hauptzähler testen“** (`POST /api/main-meter/test`, ungespeicherte Formularwerte, nur lesend): Modell, Generation, MAC, Leistung mit Richtung, Zählerstände; meldet, wenn der Typ nicht zum Gerät passt (über `/shelly`, gibt es bei Gen1 und Gen2).
- `config.example.json`: `shelly.main_meter_type`.
- Tests: `tests/test_main_meter.py` (13): Config/Default, Controller+Kaskade nutzen den Typ, Pro-3EM-Leistung/-Zähler/Phasensumme/Fehler, Gen1 unverändert (ein Abruf), Tausch auf neuen Zähler bei 0, neuer Zähler mit größerem Stand, Reset und kleiner Rücksprung, neue IP, alter Stand im Web nach dem Tausch, Migration der alten Tabelle, Settings speichern/prüfen, Verbindungstest inkl. Typ-Fehler. Suite: 586 grün.

## 2026-10-14 – Gesamt-PV in der Kopfzeile und Σ im Energiefluss

- **Kopfzeile (alle Tabs):** kleine Pill links neben „aktualisiert“ mit der Gesamt-PV (Hauptwechselrichter + Pergola): „☀ 7,5 kW“, unter 1 kW in W, 0 W und „☀ –“ gedimmt. Pergola nachts offline zählt einfach nicht mit (kein Fehlerzustand); fehlt der Hauptwechselrichter → „☀ –“. Antippen → Dashboard. Auf dem Handy zeigt die Zeit-Pill nur die Uhrzeit, damit Titel, PV und Zeit in eine Zeile passen (geprüft bei 320, 390 und 1440 px, kein horizontales Scrollen).
- **Datenquelle ohne zusätzliche Wechselrichter-Abfrage:** Auf Dashboard/Prioritäten aus dem vorhandenen Dashboard-Polling (`/api/solax`). Auf den anderen Tabs neuer Endpoint **`GET /api/pv/now`**: liest nur den Prozess-Cache der Wechselrichter-Clients (`SolaxClient.cached_realtime_data`, höchstens 60 s alt), sonst den letzten Entscheidungs-Schnappschuss des Cron-Laufs (höchstens 180 s alt); nie ein HTTP-Request am Gerät. Abfrage alle 30 s, pausiert bei verdeckter Seite und auf Dashboard/Prioritäten (kein Doppel-Polling), beim Tabwechsel und Zurückkehren sofort.
- **Energiefluss:** zwischen den Kästen PV und Pergola dezent „Σ 7,52 kW“ (Summe beider Wechselrichter), gleiche Schrift und Farbe wie die Kasten-Labels; nur wenn die Pergola eingerichtet ist. Sonst unverändert.
- `clients/solax_client.py`: `get_realtime_data` parst über `_parse`; neu `cached_realtime_data(max_age)` und `cache_age()` (nur Cache).
- Tests: `tests/test_pv_now.py` (6): Cache Haupt-WR + Pergola, Pergola offline, Fallback Schnappschuss, Haupt-WR fehlt / Schnappschuss zu alt → null, Cache-Lesen ohne Geräteabfrage (HTTP im Test verboten), Markup. Im Testbrowser: Werte/Dimmen für –, 0, 0,4, 654, 999,6 und 7520 W, keine Abfrage bei verdeckter Seite, Klick → Dashboard, keine JS-Fehler. Suite: 573 grün.

## 2026-10-14 – „Warum?“: Prompt-Beispiel mit Platzhaltern

- **`EXPLAIN_PROMPT`:** Das Beispiel zur Regel „vor/nach Wechsel“ nutzt keine echten Messwerte mehr, sondern Platzhalter („Schon vor <Uhrzeit> war es <X> °C, weil das Ventil bei Soll <A> °C bis <Y> % offen war; danach heizte es mit Soll <B> °C zusätzlich bis <Z> °C.“), dazu „Setze nur Werte aus dem Kontext ein.“ – damit das Modell keine Beispielzahlen übernimmt.
- Test: Das Beispiel im Prompt enthält keine Ziffer. Suite: 567 grün.

## 2026-10-14 – „Warum?“: Regel für „vor/nach Wechsel“ im Prompt

- **`EXPLAIN_PROMPT`:** Enthält der Erklär-Kontext Raum-Verlaufszeilen „vor Wechsel HH:MM“ und „nach Wechsel HH:MM“, nennt die Antwort beide Abschnitte, wenn sie zur Frage beitragen („Schon vor 7 Uhr war es 20,2 °C, weil das Ventil bei Soll 19 °C bis 84 % offen war; danach heizte es mit Soll 22 °C zusätzlich …“), und schreibt die Wirkung nicht allein dem Wechsel zu. Anlass: Die Antwort auf „Warum war es im Kinderzimmer heute früh so warm?“ (gpt-4.1) nannte nur den Zeitplan um 07:01, obwohl es schon um 06:20 20,2 °C waren.
- Tests (API gemockt): Regel im Prompt; Endpunkt-Test mit Raum-Verlauf über den openHAB-Client – Regel und beide Abschnitte gehen zusammen ans Modell. Die Web-Testklassen nutzen jetzt eine gemeinsame Basis (`WebBase`), dadurch laufen die Endpunkt-Tests nicht mehr doppelt. Suite: 567 grün.

## 2026-10-14 – „Warum?“ Phase 2, Nachbesserung nach den echten Beispielen

Fünf echte Beispielfragen (gpt-4.1-mini, Kopie der Prod-Schnappschüsse) zeigten zwei Fehler: „Heizstab läuft nicht“, obwohl Phase 1 per Mindestlaufzeit lief, und „die Heizung hatte angehoben“ ohne Raumtemperaturen vom Morgen im Kontext.
- **„Läuft gerade“-Zeile** (`explain.running_line`) direkt unter den Messwerten jedes Schnappschusses im Kontext: Heizstab AN/AUS mit Phasen und gemessener Leistung (zu Beginn des Laufs), Phasen, die nur wegen Mindestlaufzeit/Fail-safe o. Ä. an sind, mit Grund; in diesem Lauf eingeschaltete Phasen; Kaskaden-Geräte AN/aus; Auto lädt/lädt nicht mit Grund (kein Auto, Laden beendet, nicht im Eco-Modus) und die Überschuss-Freigabe der Kaskade für die Wallbox.
- **Budget-Zeile des Heizstabs** zeigt nur noch „Budget“ (nie „schaltet EIN/AUS“); Aktionswechsel dieser Zeile zählen nicht mehr als Wechsel.
- **Raum-Verlauf aus rrd4j:** Nennt eine Warum-Frage einen Raum und einen Zeitbezug, kommen Ist-Temperatur (Mittelwerte, höchstens 12 je Zeitraum, mit min/max) und Soll-Wechsel (Anfangswert + Wechsel) des Zeitraums in den Kontext – über `openhab.persistence` bzw. `rrd4j`, nur lesend, höchstens 3 ausdrücklich genannte Räume. Ohne Daten steht ausdrücklich „unbekannt“ (keine gespeicherten Werte / Persistenz nicht lesbar / openHAB nicht verbunden), Fehlertexte nie im Kontext.
- **Soll-Wechsel und Ventil im Raum-Verlauf:** Zusätzlich der Ventil-Verlauf des Raums (Mittelwerte in %, mit max) und – wenn der Sollwert im Zeitraum wechselt – je Abschnitt eine Zeile „vor Wechsel HH:MM“ / „nach Wechsel HH:MM“ mit Soll, Ist Anfang → Ende, min/max und Ventil max. Damit ist ablesbar, ob es schon vor dem Zeitplan warm war bzw. geheizt wurde (Beispiel Kind heute: vor 07:01 bei Soll 19 °C bis 20,2 °C, Ventil bis 84 %).
- **Prompt (`EXPLAIN_PROMPT`):** Hauptgrund zuerst (Auto: kein Auto/kein Eco-Modus vor Überschuss); ob etwas läuft, steht in „Läuft gerade“, die Budget-Zeile ist kein Schaltzustand; Werte, die nicht im Kontext stehen, nicht schätzen oder folgern.
- **`assistant.explain_model` (optional, neu):** eigenes Modell nur für Warum-Fragen; leer/fehlend = `assistant.model`. Wird geprüft wie `model`, bleibt beim Speichern der Settings erhalten, `chat/status` meldet es. In Prod noch nicht gesetzt.
- Tests: +12 in `tests/test_explain.py` („Läuft gerade“, Budget-Zeile, Raum-Verlauf mit/ohne Daten, vor/nach Soll-Wechsel, Ventil, Fehler, kein Abruf ohne Zeitbezug oder ohne genannten Raum, rrd4j über den openHAB-Client, `explain_model`). Suite: 575 grün.

## 2026-10-13 – „Warum?“-Antworten im Chat (Phase 2)

Der Chat (unten auf allen Tabs) beantwortet Warum-/Statusfragen („Warum heizt der Heizstab gerade nicht?“, „Warum ging um 14 Uhr Phase 2 aus?“, „Warum ist der Rollladen Hintereingang nicht ganz zu?“). **Nur lesen:** eine reine Frage erzeugt nie einen Vorschlag, Schaltvorgang oder DB-Eintrag außer dem Tageszähler. Bestehendes Chat-Verhalten für Zeitpläne/Sofortbefehle und die Abfragen ohne API sind unverändert (gleicher Prompt, gleiches Tool).
- **Erkennen (`explain.is_why`):** „warum/wieso/weshalb/woran liegt/was ist los …“ in der letzten Nachricht, oder eine kurze Nachfrage („und um 12 Uhr?“) direkt nach einer Warum-Frage. Mischfrage („warum ist Bad kalt? stell es auf 21“, `explain.has_command`): Erklärung **und** normaler Vorschlag mit Bestätigung. Liefert das Modell bei einer reinen Frage trotzdem Schaltwünsche, verwirft der Server sie.
- **Erklär-Kontext (`explain.py`, vom Server gebaut):** aktueller Entscheidungs-Schnappschuss, die letzten 60 min verdichtet (nur Wechsel von Aktion/Grund je Gerät, dazu Messwerte in Abständen), bei Zeitbezug („um 14 Uhr“, „vor 20 Minuten“ → ±15 min; „heute früh“, „gestern Abend“, „heute“, „gestern“ → Zeitraum) der passende Schnappschuss mit Wechseln, Geräte-Erreichbarkeit (jetzt offline + Ereignisse), Einstellungen als Zahlen (Startschwellen, Mindestzeiten, Glättung, Max-Temp, Profil-Reihenfolge, Sommermodus, Kaskaden-Geräte, Batterie, Wallbox). Nennt die Frage einen Raum oder Rollladen: dessen Status, Zeitpläne, Tür-/Frost-/Hand-Regeln, letzter Befehl und die Zeitplan-Ergebnisse des Tages. Fehlendes steht ausdrücklich als „unbekannt“ im Kontext (kein Schnappschuss, Zeitraum außerhalb der 48 h, letzter Lauf älter als 5 min).
- **Keine Secrets, IPs, MACs:** es werden nur ausgewählte Zahlen und Namen gelesen; Fehlertexte von Schaltbefehlen kommen gar nicht in den Kontext; der fertige Text läuft zusätzlich durch `redact.redact` und `redact.scrub_addresses` (Test durchsucht Kontext und „Grundlage“).
- **Größe:** Obergrenze 6 000 Token (geschätzt mit 3 Zeichen/Token), lange Wechsel-/Ergebnislisten werden in der Mitte gekürzt. Die Antwort enthält `context_tokens` (Schätzung) und `usage` (echte Token der API, wenn sie sie meldet).
- **Modell-Aufruf (`heating_chat`):** Bei Warum-Fragen dasselbe Tool `propose_schedule` mit dem zusätzlichen Textfeld `explanation` (`TOOL_EXPLAIN`) und einem Zusatz zum System-Prompt (`EXPLAIN_PROMPT`: nur Fakten aus dem Kontext, sonst „Das kann ich nicht sehen“; 2–5 Sätze, Deutsch, Zahlen mit Einheit, „Stand HH:MM“; Codes in Alltagssprache; was sich ändern müsste; keine Empfehlung, Einstellungen zu ändern). Gilt für ChatGPT und Claude. Warum-Fragen zählen zum Tageslimit.
- **Endpunkt:** `POST /api/heating/chat` liefert bei einer reinen Warum-Frage `type: "explain"` mit `text` und `basis` (Abschnitte mit den verwendeten Fakten); bei einer Mischfrage `type: "proposal"` mit zusätzlich `explanation` und `basis`.
- **UI (`web/heating.js`, `web/styles.css`):** Antwort im Chat-Sheet wie gewohnt, darunter klein aufklappbar **„Grundlage“** (Uhrzeit, Werte, Grund-Codes). Nachfragen behalten den Verlauf. Nach einem Vorschlag scrollt der Verlauf ans Ende (die Vorschau verkleinert ihn sonst und verdeckt die letzte Nachricht). Gleichlautende Phasen-Zeilen stehen im Kontext zusammengefasst („Heizstab Phase 1–3: …“). Im Testbrowser geprüft (390 px und 1440 px, API gemockt): Antwort, „Grundlage“ zu-/aufklappbar (2 Abschnitte, 12 px), Nachfrage sendet den Verlauf mit, Mischfrage zeigt Erklärung + Vorschlag mit „Übernehmen“/„Abbrechen“, Seite nicht breiter als das Fenster, keine JS-Fehler.
- Tests: `tests/test_explain.py` (33, API gemockt): Erkennen, Zeitbezug („jetzt“, „um 14 Uhr“, „heute früh“, gestern, Zukunft → gestern), Kontextbau, keine Secrets/IPs/MACs, Größe, nur lesen, Warum-Frage ohne Schaltwirkung/DB-Eintrag außer Zähler, Mischfrage mit Bestätigung, fehlende Daten → „unbekannt“, Tageslimit, normales Verhalten unverändert. Suite: 554 grün.

## 2026-10-13 – „Warum?“ Phase 1, Nachträge: schlankere Phasen-Einträge, openHAB-Check alle 5 min, Wallbox-Texte deutsch

- **Schnappschuss entschlackt:** Die Budget-Zahlen der drei Heizstab-Phasen (`budget_w`, `budget_down_w`, `target_phases`, `running`, `threshold_w`) stehen einmal in `snap["phases"]` statt in jeder Phase; an der Phase bleibt nur Eigenes (z. B. `remaining_s`). `rest_w` entfällt, wo es gleich `budget_w` ist. `decisions.device_data()` / `reason_text()` fügen beim Lesen wieder zusammen. Größe mit Prod-Geräten ~1,45 KB statt ~1,65 KB je Lauf (~2,1 MB/Tag).
- **openHAB-Gesundheit:** `decisions.check_openhab` – im Cron alle 5 min (Minute durch 5 teilbar) ein `GET /rest/` mit 2 s Timeout, ohne Token, nur lesend; Ergebnis als `offline_ab` / `wieder_da` im Ereignislog. Damit werden Ausfälle auch ohne fälligen Zeitplan erfasst. Fehler werden abgefangen; im Detail steht nur die Fehlerart.
- **Keine Adressen in den neuen Protokollen:** `redact.scrub_addresses` entfernt URLs, Hostangaben, IPs und MACs aus `device_health_event.detail` und `schedule_result.message` (openHAB-Fehlertexte enthalten Host und Port). Das Textlog bleibt unverändert.
- **Wallbox-Texte deutsch** (`controller.py`): Begründungen in `/api/status` und Log („go-e ist nicht im PV-Überschussmodus (Eco). Manuelles/normales Laden wird nicht angefasst.“, „Wallbox in der Config deaktiviert.“, „… Fail-safe: keine Änderung.“) sowie die go-e-Logzeilen (DRY-RUN/READ-ONLY/gesetzt/fehlgeschlagen, „Wallbox-Entscheidung“, „Grund“). Entscheidung und Schaltbefehle unverändert.
- Die Kaskaden-ID `shelly_test` (Gerät „eBike“) bleibt, wie sie ist.
- Tests: +6 in `tests/test_decisions.py` (Phasen-Zahlen, openHAB-Check, Adress-Bereinigung, Wallbox-Texte). Suite: 521 grün.

## 2026-10-13 – „Warum?“-Antworten, Phase 1: Grund-Codes, Entscheidungs-Schnappschuss, Zeitplan-Verlauf, Geräte-Ereignislog

Reines Protokoll als Grundlage für Warum-Fragen im Chat (Phase 2). **Entscheidungslogik, Schaltverhalten, bestehende Texte und Logs unverändert** – die 452 bestehenden Tests laufen unverändert grün.
- **`reasons.py` (neu):** feste Liste von Grund-Codes mit deutschem Kurztext-Template (`reasons.text(code, data)`; fehlende Zahl → „?“, nie ein Fehler).
- **Grund-Code neben dem bestehenden Text** (zusätzliches Feld `code` + Zahlen `data`): `heater_phases.decide_phases` (`below_start_threshold`, `budget_available`, `one_phase_per_run`, `min_on_time`, `min_off_time`, `below_stop_threshold`, `max_temp_reached`, `temp_unknown`, `hysteresis_band`, `summer_mode_grid_heat`, `summer_mode_stop`, `manual_override`, `failsafe_global`, `failsafe_phase`, `phase_unknown`, `phase_unknown_off_sent`, `phase_disabled`), `zendure_control.plan` (`charging_surplus`, `soc_target_reached`, `below_start_threshold`, `battery_empty`, `discharging_base_load`, `no_discharge_controlled_load`, `standby_no_import`, `base_load_unknown`, `wallbox_lock`, `manual_override`, `mode_change_pause`, `device_unreachable`, `write_failed`), Kaskade (`surplus_sufficient`, `keep_running`, `budget_exhausted`, `min_on_time`, `min_off_time`, `below_start_threshold`, `not_reached`, `no_surplus`, `manual_override`; Heizstab-Budget: `budget_available`, `grid_assist`, `smoothing_wait`, `below_start_threshold`; Batterie vor dem Heizstab: `priority_first`), Wallbox (`controller.wallbox_reason_code`: `not_connected`, `charging`, `charging_manual`, `wallbox_not_eco_mode`, `car_full`, `waiting_for_car`, `wallbox_released`, `wallbox_disabled`, `device_unreachable`). `run_cascade()` liefert je Gerät zusätzlich `action`/`code`/`data` und die Zahlen des Laufs.
- **Entscheidungs-Schnappschuss (`decisions.py`, Tabelle `decision_snapshot`):** `main.py` schreibt nach Kaskade und Controller eine JSON-Zeile je Lauf (Messwerte, Profil, Fail-safe mit Gerät, je Gerät/Phase Aktion + Code + Zahlen, ausgefallene Geräte, Ersatzwerte). Batterie ist immer vorgesehen (`not_in_cascade`, solange sie nicht in der Kaskade ist). Keine Adressen/Zugangsdaten (Test durchsucht den Schnappschuss). Aufbewahrung 48 h über `db.prune` (läuft täglich ab 03:00, kurz davor liegen bis zu ~3 Tage). Größe mit Prod-Geräten (Heizstab, Wallbox, eBike, 3 Phasen): ~1,65 KB/Lauf → ~2,4 MB/Tag, im Mittel ~5 MB, höchstens ~7 MB. Fehler beim Schreiben werden abgefangen und nur geloggt.
- **Zeitplan-Verlauf (`schedule_log.py`, Tabelle `schedule_result`, 14 Tage):** Heizungs- und Rollladen-Ausführung schreiben bei jedem fälligen Eintrag ein Ergebnis mit Code, Zahlen und der Log-Meldung: `sent`, `already_set`, `dry_run`, `superseded`, `skipped_late`, `skipped_no_sun`, `skipped_door_open`, `waiting_door`, `door_wait_expired`, `skipped_frost`, `skipped_manual_hold`, `history_unreadable`, `deferred_moving`, `deferred_time_budget`, `openhab_unreachable`, `openhab_retry`, `openhab_error`, `readback_mismatch`, `readback_retry`; die Kürzung „Tür ohne Kontakt → 80 %“ steht als `limit: limited_door_gap` am Ergebnis. **Erstmals mit Eintrag** die drei stillen Fälle: `skipped_schedules_off` (Schalter aus), `skipped_automation_openhab` (Rollladen in openHAB-Automatik), `skipped_duty_cycle` – jeweils **einmal je Eintrag und Tag** (nur im 15-min-Nachholfenster; weiterhin kein openHAB-Request, der Eintrag selbst bleibt unberührt). Schreiben wirft nie – ein Fehler im Verlauf verhindert keine Fahrt (Test).
- **Geräte-Ereignislog (`device_health_event`, 30 Tage):** `offline_ab` / `wieder_da` bei jedem Wechsel für Solax, Pergola, Hauptzähler, Heizstab-Zähler (offline = Ersatzwert aus den Phasen), Speichertemperatur, Phasen 1–3, Batterie, Wallbox, Kaskaden-Shellys (`cascade:<id>`) und openHAB. Quelle ist der Cron-Lauf (nicht der Web-Poller); openHAB wird nur geprüft, wenn ein Zeitplan-Lauf openHAB anspricht. **Pergola „erwartet offline“** (`expected = 1`), wenn der Haupt-WR unter 100 W liefert bzw. – falls er selbst nicht lesbar ist – zwischen Sonnenunter- und -aufgang; ist sie bei Sonne weiter weg, folgt ein zweites `offline_ab` ohne `expected`. Die bestehende Tabelle `device_health` (Banner) bleibt unverändert.
- **`db.prune`:** drei neue Regeln (`decision_snapshot` 2 Tage, `schedule_result` 14, `device_health_event` 30).
- Bestehende englische Wallbox-Texte (`WallboxDecision.reason`) sind bewusst nicht umformuliert (Steuerungstests prüfen sie nicht, aber Log/Status bleiben so unverändert); Schnappschuss und Chat nutzen nur Code + deutsches Template.
- Tests: `tests/test_decisions.py` (63): unter Startschwelle, Mindestlaufzeit/-pause, Max-Temp, Temp unbekannt, Hystereseband, Batterie-Vorrang Winter, Sommer Wasser < Mindesttemp, globaler Fail-safe, Phase unbekannt, Wallbox-Sperre, Glättung, Tür offen, Frost, manuell bewegt, Duty Cycle, Schalter aus, Automatik openHAB, Rückprüfung, Retention, kein unbekannter Code im Quelltext. Suite: 515 grün. In Dev zwei echte Dry-Run-Läufe geschrieben (Kaskade ist in Dev aus → dort ohne Kaskaden-Geräte, 1,1 KB).

## 2026-10-13 – Verlauf: Unternavigation auf dem Handy horizontal scrollbar

Die acht Knöpfe (Tag … Netz) ragten bei 390 px über den Rand (Seite 446 px breit). `web/styles.css`: `.verlauf-subnav` scrollt horizontal wie die Haupt-Tabs (ohne sichtbare Scrollleiste); bis 768 px behalten die Knöpfe ihre Textbreite. Geprüft bei 390 px: Seite 390 px, Leiste scrollt (358 von 558 px), „Netz“ nach Scrollen anklickbar, kein Text abgeschnitten. Desktop unverändert (gleich breite Knöpfe).

## 2026-10-13 – Chat-Leiste auf allen Tabs; iOS: kein Zoom mehr beim Antippen von Eingabefeldern

**Chat-Leiste überall:** Die Leiste liegt jetzt auf Seitenebene (nicht mehr im Heizung-/Rollladen-Tab) und ist auf Dashboard, Heizung, Rollläden, Verlauf, Prioritäten, Settings und Logs dieselbe – gleicher Verlauf über alle Tabs. Ist der Chat in den Settings aus, wird sie überall ausgeblendet (statt ausgegraut); nach „Speichern“ in Settings → Heizung sofort. Solange sie sichtbar ist, bekommt die Seite unten 64 px + Safe-Area Platz (`html.has-chatbar .app-shell`) und `scroll-padding-bottom`, damit weder Inhalt noch angesprungene/fokussierte Felder unter der Leiste liegen. Neuer Lese-Endpoint `GET /api/heating/chat/status` (an/aus, bereit, Zähler; ohne openHAB-Abfrage) für die Tabs, die keinen Heizungs-/Rollladen-Status laden. Dashboard sonst unverändert.
**iOS-Zoom:** iOS Safari zoomt in die Seite, wenn ein Feld mit Schrift < 16 px den Fokus bekommt, und bleibt gezoomt (Chat-Leiste 14 px, Formulare 13–14 px). Bis 768 px Breite haben alle `input` (außer Checkbox/Radio/Range), `textarea` und `select` 16 px – Chat-Leiste, Zeitplan-Sheet (Uhrzeit, ±Minuten, Datum, Soll, Position), Settings, Logs, Verlauf. Viewport unverändert (kein `maximum-scale`/`user-scalable=no`, Zoomen per Geste bleibt möglich).
**Settings auf dem Handy nicht mehr breiter als der Bildschirm:** Die Settings-Blöcke Heizung/Rollläden waren bei 390 px schon vorher 476 px breit (breite Auswahlfelder dehnten das Raster), mit 16 px wären es 559 px geworden. Auf Mobil jetzt `minmax(0, 1fr)`, Felder höchstens 100 % breit → 390 px.
**Geprüft im Testbrowser** (390 px und 1440 px, Chat an/aus): Leiste auf allen 7 Tabs, Fußzeile endet 3 px über der Leiste, kein Bedienelement verdeckt (auch „Rollläden speichern“), Chat-Verlauf bleibt beim Tab-Wechsel, bei „Chat aus“ keine Leiste und kein Zusatz-Abstand; alle 203 Felder bei 390/768 px ≥ 16 px; keine JS-Fehler. Tests: `ChatBarStatusTest` (2), Suite 452. Nicht prüfbar ohne iPhone: das Zoom-Verhalten selbst. Weiter vorhanden (war vorher schon so): die Unter-Navigation im Verlauf ragt bei 390 px über den Rand.

## 2026-10-13 – Betrieb (Prod): Rollläden 1–4 auf „PV Controller“ umgestellt, Zeitpläne aktiv

Kein Code. Auf Anweisung des Users um 17:45: Wohnen links, Wohnen rechts, Esszimmer, Hintereingang in den Settings (`PUT /api/shutters/config`, Backup `config.backup-20261013-174500.json`) auf Automatik „PV Controller“; „Rollladen-Zeitpläne aktiv“ eingeschaltet. Neu angelegt: Hintereingang ▲ 06:20 und ▼ 22:01 täglich (Tür ohne Kontakt → schließt automatisch nur bis 80 %). Bestehend: Wohnen links/rechts, Esszimmer je ▼ Sonnenuntergang und ▲ Sonnenaufgang täglich. Schlafzimmer hat keinen Zeitplan. In openHAB die Regel „Rollladen automatisch runter wenn abends nicht zuhause“ (75088ccfb7) wieder aktiviert; die fünf anderen Rollladen-Regeln bleiben deaktiviert. Kein Rollladen wurde von Hand bewegt. Danach `af28697` deployt.

## 2026-10-13 – Redesign Heizung- und Rollladen-Tab

Nur `web/heating.js`, `web/shutters.js`, `web/styles.css`, `web/index.html` (Abschnitte der beiden Tabs). Logik, Zeitplan-Ausführung und Sicherheitsregeln (Tür-Bestätigung, Duty Cycle, Dry-Run) unverändert; API nur um zwei Lese-Felder ergänzt (`rule_next`, `sun_tomorrow`); andere Tabs nicht angefasst.
- **Grid:** Cards in `repeat(auto-fill, minmax(340px, 1fr))` – iPhone 1, iPad (1024 px) 2, Desktop (1440 px) 3 Spalten. Cards haben eine feste Struktur und klappen nicht mehr auf.
- **Kopfzeile je Tab** statt Hinweiszeilen und Gruppen-Card: Heizung „Ø · n Räume heizen · nächste: …“, Rollläden „n offen · n zu · nächste: …“ mit [Alle ▲] [Alle ▼]; Dry-Run als Badge; **Zeitpläne-Schalter** (gleicher Endpoint wie Settings: `PUT /api/heating/config` bzw. `/api/shutters/config`, Einschalten mit Rückfrage). Benannte Gruppen (EG/OG) als schlanke Zeile darunter.
- **Heizungs-Card:** Ist groß, Abweichung als Text („+4,4 über Soll“) statt eingefärbter Zahl (kalt blau-grau, warm gedecktes Amber); Sparkline 24 h (Ist, Soll gestrichelt, 40 px), Tippen öffnet den großen Verlauf im Sheet (echte Pixel, 200 px hoch, Schrift 11,5 px); Warnungen (Batterie, AUTO, Hinweis) als Badge in Warn-Orange – Soll-Linie ist jetzt blau. Nur-Anzeige-Räume (Waschküche) als halbe Card.
- **Rollladen-Card:** Fenster-Symbol (SVG 56×64) mit Lamellen statt Balken, Slider kartenbreit, Buttons ▲ ■ ▼ 52×44 px, Automatik als Badge („openHAB“ / „Auto ☀↓ −15 ▼ 18:22“), 🚪-Badge, „fährt ▼ 47 %“ pulsiert, unsichere Position ausgegraut mit „≈“.
- **Zeitpläne im Sheet** (Handy von unten, Desktop Modal 480 px): kompakte Liste (Tage · Zeit · Ziel · Schalter · ✎ · 🗑), „+ Zeitplan“ → Formular im selben Sheet: Segmented Control Uhrzeit | Sonnenaufgang | Sonnenuntergang, ±Minuten-Stepper (5er-Schritte), „Grenzen“ eingeklappt, Soll-Stepper bzw. Auf/Zu/Position, Werktags | Täglich | Einmalig + Wochentag-Chips (Datum nur bei Einmalig), Live-Vorschau-Zeile. Banner bei openHAB-Rollläden. Sheet bleibt beim Poll offen.
- **Chat:** schlanke Eingabezeile unten fixiert (Safe-Area beachtet), Zähler klein rechts; Antwort/Vorschau als Sheet darüber (Handy Bottom-Sheet, Desktop Popover).
- **openHAB-Badge mit nächster Regelzeit** („openHAB ▼ 18:40“): `/api/shutters/status` liefert je openHAB-Rollladen `rule_next` (`at`, `target`, `source`) aus den aktiven openHAB-Regeln – dieselben 10 min gecachten Regeldaten wie die Chat-Abfrage, nur lesend; nicht lesbar → kein Fehler, erneuter Versuch erst nach 10 min. Ohne aktive Regel mit Uhrzeit bleibt das Badge „openHAB“.
- **Vorschau mit morgen** („→ heute 18:22, morgen 18:20“): beide Zeitplan-Endpunkte liefern zusätzlich `sun_tomorrow` (aus `suntime.py`).
- Zeit-Inputs mit `lang="de"`.
- Geprüft im Browser (1440 / 1024 / 390 px): keine JS-Fehler, kein horizontales Scrollen, Anlegen/Bearbeiten/Umschalten/Löschen im Sheet, Zeitpläne-Schalter an/aus, Tür-Bestätigung. Tests: `tests/test_shutter_chat.RuleNextTest` (4). Suite: 450 grün.
- Neu gegenüber vorher: Für die Sparklines wird der 24-h-Verlauf jedes regelbaren Raums geladen (nacheinander, höchstens alle 5 min, nur bei sichtbarem Tab).

## 2026-10-13 – CLAUDE.md: Abschnitt „Sicherheit“

Verbindliche Regel des Users nach zwei Pannen (Türöffner-Zugangsdaten und Dropbox-Zugang landeten im Sitzungsprotokoll): Dateien mit möglichen Zugangsdaten nie anzeigen, nur Pfad, Größe, Rechte, Variablennamen. Zusätzlich in `~/.claude/settings.json` gesperrt (deny): Lesen von `~/.secrets/**`, `**/config.json`, `**/config*.json.bak*`, `~/.dropbox_uploader`.

## 2026-10-13 – Charts in echten Pixeln (alle Charts, zentral)

**Ursache:** Alle Charts sind SVGs mit `viewBox` und `width: 100 %; height: auto` – sie wuchsen proportional mit der Card-Breite. In den breiten Cards (Hauptzähler im Dashboard und Verlauf, Heizungsverlauf 24 h, alle Verlauf-Unterseiten) wurden bei 1440 px aus 9 px Schrift 34 px und aus 150 Einheiten 564 px Höhe, bei 2000 px 48 px Schrift und 798 px Höhe.
- **Zentral in `web/app.js` (`fitChart` + Beobachter):** bis Faktor 1,5 skaliert ein Chart wie bisher; darüber wird es nur noch in die Breite gestreckt. Höhe dann fest (Hauptzähler 225 px, „Letzte Stunde“ 165 px), Schrift 11,7 px, Punkte r = 4 px, Linienstärke wie im Code angegeben (1,6–2 px; CSS `svg.chart-fit`). Greift automatisch für jedes Chart mit den Klassen `temp-chart`, `chart-svg`, `heat-svg`, `phase-tl-svg` – auch nach Neuzeichnen, Tab-Wechsel und Fenstergröße.
- **Hauptzähler-Y-Achse (`meterChartSvg`):** Bereich schließt Einspeisung (negativ) und die Min/Max-Punkte ein, plus 6 % Rand oben und unten; Null-Linie bleibt.
- **Unverändert:** Handy (Faktor < 1,5: pixelgleich wie vorher) und schmale Dashboard-Charts bei 1440 px (Tageshoch, Wallbox). Der Wasserspeicher-Chart im Dashboard lag bei 1440 px über 1,5 (Faktor 1,8) und ist jetzt 285 statt 342 px hoch mit 11,7 statt 16 px Schrift.
- Geprüft per Screenshot vorher/nachher in 1440 px, 2000 px und iPhone (390 px): 20 Charts je Größe. In Dev ohne Daten und deshalb nicht gesehen: Tagesverlauf (Stundenwerte), String-Charts, Zendure, Phasen-Zeitleiste, Speicher 7 Tage – sie nutzen dieselben Klassen.
- Card-Breite nicht geändert: Im Verlauf ist die Hauptzähler-Card so breit wie die Cards darüber; im Dashboard liegt sie als eigene Zeile unter den zwei Spalten.

## 2026-10-13 – Zeitplan-Formular: Meldung „Uhrzeit“ geprüft, Hinweis bei leerer Uhrzeit, Ablauf-Test

**Meldung:** In Prod ließen sich Rollladen-Zeitpläne mit Zeittyp „Uhrzeit“ nicht anlegen. **In Dev nicht nachstellbar** (Chromium, 390–2000 px, Hintereingang/Wohnen links = openHAB, Schlafzimmer = PV Controller, Heizung Wohnen): Radio „Uhrzeit“ blendet das Uhrzeit-Feld ein und ±Minuten/Grenzen aus, Wechsel Sonnenuntergang → Uhrzeit → Sonnenuntergang lässt keine alten Werte im Request (`{"kind":"time","time":"06:45",…}`), Server antwortet 200, keine JS-Fehler. Im Prod-Log steht für den Zeitraum kein abgelehnter und kein „Uhrzeit“-Request – der Fehler entsteht also im Browser vor dem Absenden.
- Einzige gefundene Schwäche: Bei leerem oder halb getipptem Uhrzeit-Feld ging der Request mit `time: ""` raus und der Server antwortete 400 „Uhrzeit muss HH:MM sein“. Jetzt prüft das Formular vorher (`_whenError` in `web/heating.js`, für Heizung und Rollläden) und zeigt „Bitte eine Uhrzeit eintragen (Stunde und Minute).“ ohne Request.
- **Test** `tests/test_schedule_form_flow.py` (3): führt den echten Formular-Code aus `heating.js` in node aus (Zustand wie nach den Klicks: Sonnenuntergang mit Offset/Grenzen → Uhrzeit → zurück → Sonnenaufgang → Uhrzeit, Bearbeiten Sonne → Uhrzeit), prüft sichtbare Felder und Request-Body und schickt jeden Body durch die Server-Validierung (Rollladen mit PV Controller und mit openHAB-Regel, POST und PUT). Suite: 446.

## 2026-10-13 – System (außerhalb des Repos): Home, `~/backup`, `~/.ssh` und `backup.sh` abgesichert

Kein Code geändert. Vorab gelesen: Samba gibt nur openHAB-Verzeichnisse frei (nichts unter `/home/openhabian`), kein Docker, einzige systemd-Unit mit Home-Pfad ist `pvcontroller-web` (`User=openhabian`), alle Cron-Jobs laufen aus der Crontab von `openhabian`. Einziger fremder Zugriff: openHAB (Benutzer `openhab`) führt `/home/openhabian/opendoor.sh` und `gds3710-feha/tuerskript.sh` aus (Türöffner) – braucht nur Lesen/Durchgehen, deshalb `755` statt `750`.
- `/home/openhabian` `777` → `755`; `~/backup` samt Unterordnern `700`, Archive/Snapshots/Crontab-Sicherungen `600`; `~/.ssh` `777` → `700`, `authorized_keys` (leer) `777` → `600`.
- `~/backup/backup.sh`: `umask 077` am Anfang (Kopie `backup.sh.bak`). Archive entstehen mit `600`.
- Geprüft: manueller Backup-Lauf (beide Tar-Archive `600`, Upload nach Dropbox ok), Cron-Lauf 15:22 ohne Fehler, Prod-/Dev-Web 200.
- **Backups verschlüsselt (age):** `backup.sh` (Kopie `backup.sh.pre-age`) verschlüsselt alle drei Archive mit dem öffentlichen age-Schlüssel des Users (`AGE_RECIPIENT`), `tar` schreibt direkt in `age`; nach Dropbox gehen nur `.age`-Dateien, bei Verschlüsselungsfehler kein Upload, `trap` räumt Tagesarchive immer ab. `openhab-cli backup` läuft über `sudo -n` (sudoers-Eintrag vom User, ohne `--noninteractive`, damit keine Kopie unter `/static/` entsteht). Fünf nicht mehr existierende Pfade entfernt. Neu: `~/backup/backup.log`. Manueller Lauf in Cron-Umgebung: drei `.age`-Dateien hochgeladen, lokal nichts übrig.
**Nur gemeldet:** Homegear-Archiv bleibt unvollständig (`/var/lib/homegear` und drei Dateien in `/etc/homegear` für `openhabian` nicht lesbar); alte unverschlüsselte Archive liegen weiter in Dropbox. Stand vor der Verschlüsselung: Backups gehen nach Dropbox (`/Privat/Backup/OpenHab3/`) inkl. Prod-`config.json` mit Keys; `openhab-cli backup` und Teile des Homegear-Backups scheitern seit jeher an fehlenden Root-Rechten; fünf Pfade im Tar existieren nicht mehr; im Home sind weiter ca. 5100 Dateien/Ordner für alle beschreibbar (`777`), darunter die Türöffner-Skripte.

## 2026-10-13 – Secrets härten: Keys nie im Log, Config-Dateien 600; zweiter Echtzeit-Test auf simulierte Uhr

**Keys im Log (`redact.py`, neu):** Alles, was wie ein `sk-…`-Key aussieht – OpenAI (`sk-proj-…`) und Anthropic (`sk-ant-…`), auch **gekürzt** (`sk-proj-AbCd****…wxyz`, `sk-…abcd`) – wird zu `sk-***`, unabhängig davon, ob es der konfigurierte Key ist. Vorher wurde nur der vollständige konfigurierte Key entfernt; zitiert die API einen ungültigen Key gekürzt (OpenAI 401), wäre das Fragment im Journal gelandet. Greift an allen Wegen: Log-Filter an den Handlern des Cron-Laufs (`main.setup_logging`), des Web-Dienstes (Root + uvicorn-Handler, inkl. Tracebacks), `_app_log` (direktes Schreiben ins Controller-Log) und in den API-Fehlertexten des Chats (`_scrub`). Normale Wörter („task-123“, „risk-free“) bleiben.
**Config-Dateien nur für den Besitzer (600):** `config.json` und Backups enthalten openHAB-Token und API-Keys im Klartext und waren `644`. Geprüft: Web-Dienst (`User=openhabian`), Minuten-Cron (Crontab von `openhabian`) und Dev-Web laufen als Besitzer der Dateien; openHAB greift nicht darauf zu. Gesetzt: Dev 27, Prod 11 Dateien auf `600` (`config.example.json` bleibt `644`); danach Prod-/Dev-Web `/api/config` 200 und ein Cron-Lauf ohne Fehler.
- `web._write_config`: Backup und neue Config entstehen direkt mit `0600` (unabhängig von umask und den Rechten der alten Datei; vorher wurde die Config über eine Temp-Datei mit Standardrechten ersetzt – ein `chmod` allein wäre beim nächsten Speichern in den Settings verloren gegangen).
- `deploy/deploy.sh`: `secure_configs` setzt bei jedem Deploy `config*.json`, `config.json.*` (Backups, Temp) auf `600` und bricht ab, wenn `config.json` für den ausführenden Benutzer nicht lesbar ist.
- `deploy/make-dev.sh`: `umask 077`, Backup per `install -m 600`, `config.json` danach `600`.
**Nicht geändert, nur gemeldet:** `~/backup/backup.sh` (außerhalb des Repos) packt die Prod-`config.json` nachts in ein Tar-Archiv; `~/backup` und das Home-Verzeichnis sind für alle beschreibbar (`drwxrwxrwx`).
**Tests:** `tests/test_secrets_hardening.py` (9: volle/gekürzte Keys beider Anbieter, Log-Filter inkl. Argumente und Traceback, Cron-Logging, `_app_log`, API-Fehlertexte, Rechte nach `_write_config` bei umask 022, `secure_configs` aus `deploy.sh`).
**Zweiter Echtzeit-Test:** `test_controller_phases.test_dead_device_does_not_slow_down_the_run` maß 3 × 1 s parallel gegen eine 2-s-Grenze. Jetzt simulierte Uhr + Barriere: alle drei Geräte müssen gleichzeitig im Lese-Aufruf stecken, simulierte Dauer 1,0 s statt 3,0 s. Gegenprobe mit erzwungen sequenziellem Lesen schlägt fehl („1 != 3“). `controller.py` unverändert. Suite (443) normal und unter 8 Last-Prozessen grün; es gibt keine Echtzeit-Grenzen mehr in den Tests.

## 2026-10-13 – Zeitplan-Formular: Uhrzeit / Sonnenaufgang / Sonnenuntergang ± Offset – für Heizung und Rollläden

**Formular (beide Tabs, gleicher Baustein):** Auswahl ( ) Uhrzeit ( ) Sonnenaufgang ( ) Sonnenuntergang; bei Sonnenzeiten „± Minuten“ (−180…+180, negativ = davor) und optional „nicht vor“ / „nicht nach“; daneben die berechnete Uhrzeit für heute („heute 18:22 (Untergang 18:37)“). Liste und „Nächste Änderung/Fahrt“ zeigen Zeittyp plus berechnete Uhrzeit, z. B. „Sonnenuntergang −15 min (18:22)“. Neu: **Bearbeiten** (✎) – lädt den Eintrag ins Formular, „Speichern“ ersetzt ihn (gilt wie neu angelegt: heute kein Nachholen, wenn der neue Zeitpunkt schon vorbei ist). Nach dem Speichern werden Offset und Grenzen im Formular zurückgesetzt.
**Rollläden – Bestand:** negative/positive Offsets und die Grenzen gab es schon (API + Formular); neu sind Auswahlknöpfe statt Dropdown, die berechnete Uhrzeit in der Liste, Bearbeiten und das **Formular an jedem Rollladen**. Bei „Automatik: openHAB-Regel“ wird der Zeitplan gespeichert, aber nicht ausgeführt (Hinweis im Formular, „· ruhen“ am Zähler, keine „Nächste Fahrt“); der Cron fährt weiterhin nur „PV Controller“-Rollläden, der Chat lehnt Zeitpläne für openHAB-Rollläden weiter freundlich ab.
**Heizung – neu:** `heating_schedule` um `kind` (`time` | `sunrise` | `sunset`), `offset_min`, `not_before`, `not_after` erweitert. Migration per `ALTER TABLE` beim ersten Zugriff; **bestehende Einträge bleiben `kind = 'time'`** und laufen unverändert (bei Sonnen-Einträgen ist `time` leer). Fälligkeit, Nachholen (15 min), „spätester gewinnt“, Rückprüfung wie bisher; ohne `latitude`/`longitude` wird ein Sonnen-Eintrag übersprungen und gemeldet.
**Gemeinsame Sonnenberechnung:** neues Modul `suntime.py` (`sun_times`, `instant`, `describe`, `validate_when`, `due_instant`) – von Heizung und Rollläden genutzt; `heating_schedule.due_instant`/`TZ` und `shutter_schedule.sun_times` bleiben als Namen erhalten.
**Chat:** Heizungs-Einträge im Tool mit `kind`/`offset_min`/`not_before`/`not_after` („Bad 30 min vor Sonnenaufgang auf 21“); Vorschau mit Zeittyp und berechneter Uhrzeit („heute 18:22, morgen 18:20“); lokale Abfragen und Kontext zeigen die berechnete Zeit. Uhrzeit-Zeilen der Vorschau unverändert.
**API:** `POST`/`PUT /api/heating/schedules[/{id}]` und `PUT /api/shutters/schedules/{id}`; Listen liefern `when`, `at_today`, `sun_today`.
**Tests:** `tests/test_heating_sun.py` (21: Migration/Altdaten, Offsets ±, Grenzen, Cron-Lauf minutenweise über die Nacht 24./25.10.2026 – 24.: 07:24 MESZ, 25.: 06:25 MEZ, je genau einmal –, Grenze in der doppelten Stunde, Web anlegen/bearbeiten, Chat), Rollläden: ruhende Einträge und Bearbeiten. Bestehende Heizungs-Testdateien unverändert; alle 434 grün. Im Browser geprüft (Dev): Anlegen aller drei Zeitarten, Bearbeiten, Liste, „Nächste“, Formular an 5 von 5 Rollläden, Handy ohne Querscrollen, keine JS-Fehler.
**OpenAI/ChatGPT:** bleibt (Entscheidung des Users: „Wir brauchen nur openai“); der begonnene Revert von `6e0386e` wurde verworfen, nichts entfernt.

## 2026-10-13 – Tests: Heizungs-Cron-Test ohne Echtzeit-Grenzen (simulierte Uhr)

**Warum:** `MainIsolationTest.test_openhab_hangs_10s_pv_control_unaffected` prüfte echte Laufzeiten (PV < 0,5 s, Cron-Lauf < 6,5 s). Unter Last (ein Lauf während einer Browser-Installation) kann das kippen – im Deploy-Vorabtest würde `deploy.sh` dann grundlos abbrechen.
**Jetzt (`tests/test_heating_schedule.py`, nur der Test; `main.py` unverändert):** simulierte Uhr statt `time.monotonic` in `main.py`; der `join` des Cron-Threads wartet, bis der Thread fertig ist oder im hängenden openHAB-Request steckt, und stellt dann die Uhr um das Zeitlimit vor. Geprüft wird unverändert: PV zuerst und komplett, PV-Dauer normal (0,05 s simuliert), der Cron-Lauf kehrt nach Budget + 0,5 s zurück, während openHAB noch hängt, Log „Heizungs-Zeitpläne: nach 5.5 s abgebrochen“. Der Test heißt jetzt `test_openhab_hangs_pv_control_unaffected` und braucht ~0,4 s statt 5,5 s. Unter 8 CPU-Last-Prozessen auf 4 Kernen 5× grün, gesamte Suite (412) grün.
**Nicht geändert, nur gemeldet:** `test_controller_phases.test_dead_device_does_not_slow_down_the_run` misst ebenfalls Echtzeit (3 × 1 s parallel, Grenze 2 s).

## 2026-10-13 – Rollläden Phase 3: ein Chat für Heizung und Rollläden

**Neu:** Der Heizungs-Chat versteht Rollläden – ein Chat-Feld (die Karte wandert mit in den aktiven Tab Heizung bzw. Rollläden, Verlauf bleibt), ein Tool `propose_schedule` mit zusätzlich `shutter_entries`, `shutter_immediate`, `shutter_delete_ids` (strict, gleiches Schema für Claude und ChatGPT). Wie bei der Heizung: nur Vorschläge, der Server prüft alles, gespeichert/gefahren wird erst nach „Übernehmen“.
- Zeitpläne per Chat nur für „Automatik: PV Controller“. Für openHAB-Rollläden lehnt die Vorschau freundlich ab („Wohnen links wird über eine openHAB-Regel gesteuert – Zeitplan erst nach Umstellung.“), der Rest des Vorschlags bleibt gültig. Sofort-Befehle für alle Rollläden.
- Sonnenzeiten: „bei Sonnenuntergang“, „30 min nach Sonnenuntergang“, „aber nicht vor 18 Uhr“ → `kind`/`offset_min`/`not_before`/`not_after`; die Vorschau zeigt die berechnete Uhrzeit („heute 19:07, morgen 19:05“).
- „alle Rollläden“ = jeder einzeln in der Vorschau. Tür ohne Kontakt (bzw. offen/unbekannt) beim Schließen → ⚠️ in der Vorschau; „Übernehmen“ fragt wie die Buttons nach, der Server lehnt ohne Bestätigung mit 409 ab, ohne den Vorschlag zu verbrauchen. Mehrere Sofort-Befehle gestaffelt (`command_spacing_s`), Duty Cycle über der Grenze → nur einzeln.
- Mehrdeutige Namen („Wohnen“ → links + rechts) → Rückfrage, auch wenn das Modell geraten hat (serverseitige Prüfung); „beide“, „alle“ oder Plural („Rollläden Wohnen“) gelten als eindeutig.
- Abfragen lokal ohne Sprachmodell: „Was fahren die Rollläden heute Abend?“, „wann geht der Hintereingang morgen früh hoch?“ – PV-Controller-Zeitpläne (berechnete Uhrzeit) plus **openHAB-Regelzeiten** als Info („18:40 Wohnen links ▼ zu · openHAB-Regel „Rollladen Wohnen runter Automatik““). Regeln werden nur gelesen (`GET /rest/rules`, 10 min zwischengespeichert); Quartz-Cron und Tageszeit-Trigger, deaktivierte Regeln ausgelassen, Regeln mit `if` als „mit Bedingungen“, häufige Trigger als Spanne („22:00–23:45 mehrfach“). Geprüft gegen die echten Regeln in Dev.
- Chat geht auch, wenn nur Rollläden aktiv sind.
**Code:** `shutter_chat.py` (neu), `heating_chat.py` (Tool, Prompt, `Proposal` + `peek`), `web.py` (gemeinsamer Chat-Endpunkt, Apply mit `confirm_doors`, Chat-Status auch im Rollladen-Status), `shutters.send_sequence` (Staffelung, auch für Gruppen), `OpenHabClient.get_rules`, `web/heating.js` (Vorschau, Bestätigung), `web/shutters.js`. Tests: `tests/test_shutter_chat.py` (29, API gemockt wie bei der Heizung); Heizungs-Testdateien unverändert, alle grün.

## 2026-10-13 – Rollläden Phase 2: Zeitpläne (Uhrzeit / Sonnenauf- und -untergang) mit Sicherheitsregeln

**Neu:** Zeitpläne je Rollladen, ausgeführt im Minuten-Cron nach PV-Steuerung und Heizungs-Zeitplänen (eigener Thread, Zeitbudget 20 s, Fehler isoliert). Eigener Schalter „Rollladen-Zeitpläne aktiv“ (Default aus). **Nur Rollläden mit „Automatik: PV Controller“** (aktuell Schlafzimmer); für „openHAB-Regel“ lehnt der Server Zeitpläne ab und der Cron fährt sie nie.
- Zeitarten: feste Uhrzeit; Sonnenaufgang/-untergang ± Offset (−180…+180 min) mit optional „nicht vor“ / „nicht nach“. Ziel: auf / zu / Position %. Wochentage oder einmalig.
- Sonnenzeiten lokal aus `latitude`/`longitude` (NOAA-Formel, keine Abhängigkeit, keine externe API). Abgleich mit Open-Meteo für 13.10./24.10./21.06./21.12.: ±1 min. **Zeitumstellung 25.10.2026:** lokal korrekt 06:55 MEZ; Open-Meteo liefert für den 25.10. 07:55 (rechnet über die Umstellung mit dem alten Offset, betrifft nur die Anzeige in `weather_log`).
- Card: „Nächste Fahrt: heute Sonnenuntergang +15 min (18:52) ▼ zu“, aufklappbare Zeitplan-Liste mit „+ Zeitplan“ (wie Heizung), Hinweise zu übersprungenen Fahrten (12 h).
**Sicherheitsregeln (mit Tests):**
1. Aussperrschutz: Tür mit offenem/unbekanntem Kontakt → automatisches Schließen übersprungen (Log + Hinweis), kein Nachholen. Pro Rollladen `retry_when_closed`: nachholen, sobald der Kontakt „zu“ meldet (höchstens 12 h, ein neuer Eintrag ersetzt das Warten, Bedienung von Hand beendet es).
2. Tür ohne Kontakt-Item → automatisch höchstens bis `auto_close_max` (Default 80 %, pro Rollladen änderbar).
3. Frost: Außentemperatur unter `frost_temp_c` (Default 0 °C) oder unbekannt → automatisches Öffnen übersprungen; Schließen erlaubt. Quelle: `frost_temp_item` (optional), sonst Tages-Minimum der Prognose (`weather_log`).
4. Gestaffelt (`command_spacing_s`), Duty Cycle über der Grenze → nichts senden, Einträge bleiben fällig.
5. Nachholen nur bis 15 min; mehrere fällige je Rollladen → nur der späteste.
6. Manueller Vorrang: Positionsänderung (≥ 5 %) in der openHAB-Persistence ohne eigenen Befehl in den letzten `manual_hold_min` (Default 60) → übersprungen. Eigene Befehle (Tab, Gruppe, Zeitplan) gelten bis `verify_after` + 3 min. Verlauf nicht lesbar → übersprungen.
- `dry_run` → nur loggen. Senden scheitert → nächste Minute einmal erneut, dann „Fehler“. openHAB nicht erreichbar → bleibt fällig.
**Ist-Position aus eigener Quelle (Phase-1-Fix):** Prod-Test 13:38 (Schlafzimmer ▼ Ab + ■ Stop): openHAB zeigt danach dauerhaft 100 (autoupdate-Vorhersage, das Shelly-Binding korrigiert nicht), real stand der Rollladen bei 53 % zu (Shelly `current_pos` 47 = % offen).
- Neu je Rollladen `position_source`: `"item"` (Default, Steuer-Item), `"shelly"` + `shelly_url` (Gen1 `GET /roller/0`, `100 − current_pos`, ohne Kalibrierung keine Position), `"item:<Name>"` (eigenes Lese-Item, z. B. HmIP-Kanal 3 `LEVEL` ohne autoupdate). Befehle gehen weiter an das Steuer-Item über openHAB.
- Anzeige, Rückprüfung, Tür-Check bei manuellen Befehlen, „von Hand bewegt“ (Verlauf des Lese-Items, sonst Steuer-Item) und alle Zeitplan-Regeln nutzen diese Ist-Position.
- Fallback ohne verlässliche Quelle: Position gilt als **unbekannt** (Steuer-Item nach STOP 60 min, während einer ausstehenden Rückmeldung, Lese-Item bis zur Rückprüfung, Shelly nicht erreichbar) → kein „steht schon dort“, Aussperrschutz und Frost greifen in beide Richtungen; die Card zeigt den Grund.
- Während der Fahrt (Shelly `state` open/close): Card „fährt ▼ 53 %“, Tab lädt alle 3 s nach, bis er steht; Rückprüfung wartet; ein fälliger Zeitplan wartet eine Minute (bleibt im Nachhol-Fenster).
- Settings: „Ist-Position aus“ (Steuer-Item / eigenes Lese-Item / Shelly direkt) mit Lese-Item bzw. Shelly-Adresse.
- Dev-Config: Schlafzimmer `shelly` (http://192.168.2.51), Wohnen links `item:Rollladen_Wohnen_Level`, `frost_temp_item` = `Aussentemperatur_Temperature` (Prognose-Minimum bleibt Fallback). Für Wohnen rechts, Esszimmer, Hintereingang existiert Kanal 3 `LEVEL`, ist aber mit keinem Item verlinkt.
**Code:** `shutter_schedule.py` (neu), Config-Felder in `shutters.py` (`schedules_enabled`, `frost_temp_c`, `frost_temp_item`, `manual_hold_min`; je Rollladen `retry_when_closed`, `auto_close_max`), Endpunkte `/api/shutters/schedules`, `main.py`: gemeinsame Hilfsfunktion `_run_isolated` für Heizungs- und Rollladen-Zeitpläne (Heizung: gleiche Log-Texte, gleiches Verhalten). Tests: `tests/test_shutter_schedule.py` (64: Sonnenzeiten inkl. 25.10. und 29.03., Lauf durch die Nacht 24./25.10., Offsets/Grenzen, Aussperrschutz, Frost, Staffelung/Budget, Duty Cycle, manueller Vorrang, openHAB weg, unsichere Position, Shelly-/Lese-Item-Quelle), `tests/test_shutters.py` +10 (Positionsquellen). Alle 383 Tests grün (Heizung nur um die DST-Tests aus dem separaten Fix ergänzt).
**Zeitumstellung:** Vergleiche und Abstände zweier Zeitpunkte mit derselben `ZoneInfo` rechnet Python als Wanduhrzeit (`fold` ignoriert, in der doppelten Stunde 1 h daneben) – Fälligkeit, Verspätung, nächste Fahrt, 12-h-Warten und „von Hand bewegt“ rechnen über UTC (wie im separaten Heizungs-Fix).

## 2026-10-13 – Fix Heizungs-Zeitpläne: Zeitumstellung im Oktober (doppelte Stunde)

**Ursache:** Python vergleicht und subtrahiert aware datetimes mit derselben `tzinfo` als Wanduhrzeit, `fold` wird ignoriert. In der doppelten Stunde (So 25.10.2026, 02:00–03:00) war ein Zeitpunkt aus der ersten Stunde (CEST) daher „später“ als einer aus der zweiten (CET).
**Folgen vorher (mit Tests nachgestellt):** 02:50 fällig, openHAB bis 02:09 CET weg → nicht übersprungen, sondern um 02:50 CET gesendet (1 h zu spät); Nachholen innerhalb 15 min über die Umstellung klappte nicht; Rückprüfung nach 02:58 CEST erst 1 h später statt nach 5 min; „Rückmeldung nicht prüfbar“ nach 30 min ebenso verzögert.
**Fix (`heating_schedule.py`):** alle Vergleiche und Abstände (Fälligkeit, „erst danach angelegt“, Nachhol-Fenster, Reihenfolge, nächste Änderung, Rückprüf-Takt, Aufgeben, Datum in der Vergangenheit) laufen über UTC (`_utc`). Sonst unverändert. Tests: 4 neue in `DstTest` (Nacht 24./25.10.2026; die Umstellung ist 2026 am Sonntag 25.10. um 03:00), alle Heizungstests grün.

## 2026-10-13 – Rollläden Phase 1: Tab „Rollläden“ mit Anzeige und manueller Steuerung

**Neu:** Tab „Rollläden“ (nach Heizung), gleiches Muster wie der Heizung-Tab (Cards in place aktualisiert, Polling alle 30 s, pausiert bei anderem Tab oder verdeckter Seite). Je Rollladen: Positionsbalken (0 = offen, 100 = zu), ▲ Auf / ■ Stop / ▼ Ab, Positions-Slider (nur wenn das Gerät Position kann), Kontaktanzeige (🚪 Tür / 🪟 Fenster · offen/zu), „Automatik: openHAB-Regel“ bzw. „PV Controller“. Gruppen-Card: „Alle“ und je Gruppe Auf/Zu, jeweils mit Bestätigung.
**Sicherheit:**
- Tür (`is_door`) mit offenem oder unbekanntem Kontakt oder ganz ohne Kontakt-Item: Schließen (Ab, Position weiter zu) nur nach Bestätigungsdialog; der Server lehnt ohne `confirm` mit 409 ab (auch wenn sich der Kontakt seit dem letzten Poll geändert hat). Öffnen und Stop sind nie gesperrt.
- Gruppenbefehle werden gestaffelt gesendet (`command_spacing_s`, Default 2 s), immer nur einer gleichzeitig; Türen, die eine Bestätigung bräuchten, werden bei „Zu“ übersprungen (Log + Hinweis im Dialog).
- Duty Cycle (optional `duty_cycle_item`): Anzeige in der Gruppen-Card; über `duty_cycle_max` (Default 80 %) → Warnung, Gruppenbefehle pausiert (Einzelbefehle bleiben möglich).
- `runtime.dry_run`: nichts wird an openHAB gesendet, nur geloggt (Dev).
**Rückprüfung:** Jeder echte Befehl wird in `shutter_command` (pvcontroller.db, 30 Tage) gespeichert. Homematic nach 6 min, Shelly nach 60 s, sonst 2 min wird die gemeldete Position mit dem Ziel verglichen (Toleranz ±5 %, HmIP meldet „zu“ als 98/99 %). Bis dahin zeigt die Card „▼ Ab gesendet 18:40 · Rückmeldung ab 18:46“ und den Wert gedimmt (openHAB-autoupdate setzt das Item sofort auf das Ziel, der Aktor meldet danach kurz die alte Position). Abweichung → ⚠️ auf der Card, verschwindet, sobald das Gerät das Ziel meldet oder ein neuer Befehl kommt. openHAB 30 min nicht erreichbar → „Rückmeldung fehlt“. Stop wird nicht geprüft.
**Config:** neuer Block `shutters` (`enabled`, `command_spacing_s`, `duty_cycle_item`, `duty_cycle_max`, `rooms[]` mit `id`, `name`, `item`, `contact_item`, `is_door`, `group`, `automation` = `openhab`|`pvcontroller`, `device` = `homematic`|`shelly`|`other`, `can_position`), editierbar unter Settings → Rollläden. Ohne Block ist der Tab aus (Prod-Config bleibt gültig). Beim Speichern bleiben unbekannte Felder im Block erhalten.
**Code:** `shutters.py` (neu), Endpunkte `/api/shutters/{status,command,group,config}` in `web.py`, `web/shutters.js` (neu), Tab/Settings in `web/index.html`, Pill-Zeit + Tab-Aktivierung in `web/app.js`, CSS. `_app_log` hat einen optionalen Bereich (`pv-controller.shutters`); für die Heizung unverändert. Tests: `tests/test_shutters.py` (39 Tests); alle 306 Tests grün, Heizungs-Tests unverändert.
**Dev-Config:** fünf Rollläden aus der Bestandsaufnahme (Wohnen links/rechts, Esszimmer, Hintereingang: Homematic, Automatik openHAB-Regel; Schlafzimmer: Shelly, Automatik PV Controller). Hintereingang vorläufig als Tür ohne Kontakt, keine Gruppen, kein Duty-Cycle-Item (Entscheidungen C/D/F/E offen).

## 2026-10-13 – Fix: Zeitplan-Liste blieb nach Löschen/Umschalten stehen (seit d83c81f)

**Ursache:** Der Schutz „Formular nicht überschreiben“ griff, sobald irgendein Element im Zeitplan-Bereich den Fokus hatte. Nach einem Klick auf ✕ oder den An/Aus-Schalter behält genau dieser Button den Fokus → die Liste wurde beim Neuladen nicht ersetzt (Zähler und „Nächste Änderung“ schon). Im Browser reproduziert; der Chat war betroffen, wenn der Fokus vorher in der Liste lag.
**Fix (`web/heating.js`):** Schutz nur noch, wenn ein Eingabefeld des Formulars (Uhrzeit, Soll, Datum) fokussiert ist. Überlappende Ladevorgänge: nur das zuletzt gestartete Laden wird angezeigt (ein älterer Poll kann ein Laden nach einer Änderung nicht mehr überschreiben). −/+ lädt sofort nach dem Senden neu und zusätzlich nach 3 s (Rückmeldung des Geräts). Löschen, An/Aus, Anlegen und Chat „Übernehmen“ laden bereits sofort neu.
**Geprüft im Browser (Dev):** Löschen → sofort weg; Toggle aus/an → sofort grau/normal, „Nächste Änderung“ passt; Anlegen → sofort da; Chat „Übernehmen“ (löschen) mit Fokus in der Liste → Liste und „Nächste Änderung“ aktualisiert; 30-s-Poll übernimmt Änderungen von außen in Liste und „Nächste Änderung“; während des Tippens bleibt das Formular, danach wird aktualisiert; −/+ lädt sofort.

## 2026-10-13 – „aktualisiert“-Pill zeigt die Zeit des aktiven Tabs

Seit das Dashboard-Polling auf anderen Tabs pausiert, stand die Pill dort auf der letzten Dashboard-Zeit. Jetzt: Heizung → Zeit des letzten Heizungs-Updates (wie „Stand …“ unten im Tab), Settings → ausgeblendet (`visibility: hidden`, Kopfzeile behält ihr Layout), übrige Tabs → Dashboard-Status wie bisher. Gleiche Klasse, Größe und Position (im Browser geprüft).

## 2026-10-13 – Dashboard, Prioritäten, Logs: Polling pausiert, nur Geändertes wird ersetzt (Optik unverändert)

**Vorher (gemessen, Headless-Browser je 30 s):** Das Dashboard-Polling (`refreshStatus`, 10 s) lief auf jedem Tab und bei verdeckter Seite: ≈ 78–90 Requests/min, ≈ 420 ersetzte DOM-Knoten je 30 s (Charts, Kaskaden-Mini, Phasen-Timeline per `innerHTML`). Im Heizung-Tab sichtbar: `#pill-time` („aktualisiert …“ in der Kopfzeile) alle 10 s – das gemeldete Neuladen „alle paar Sekunden“. Prioritäten baute die Geräteliste alle 10 s komplett neu (≈ 480 Knoten je 30 s), Logs ersetzte alle 5 s den ganzen Text und sprang ans Ende.
**Jetzt (`web/app.js`):**
- Polling nur auf Dashboard und Prioritäten bei sichtbarer Seite; Logs-Auto-Refresh nur auf dem Logs-Tab. Beim Zurückkommen (Tab oder Seite) sofort ein Update.
- Dashboard: alle `innerHTML`/`textContent`/`className`-Zuweisungen der vom Polling aufgerufenen Render-Funktionen laufen über `htmlIfChanged`/`textIfChanged`/`classIfChanged` (`setHTML`): gleiches HTML wie bisher, ersetzt wird nur bei Änderung – Charts (Wallbox, Strings, Speicher, Haus, Batterie), Kaskaden-Mini und Phasen-Timeline nur bei geänderten Daten. Skeleton-Platzhalter mit gleichem Text werden trotzdem ersetzt.
- Prioritäten: nie während Drag & Drop oder offenem Aktionsmenü; gleiche Geräte in gleicher Reihenfolge → nur geänderte Cards ersetzt (Handler nur an diese), sonst Neuaufbau; nach Drag/Umschalten von Hand wird der Cache verworfen.
- Logs: neue Zeilen werden angehängt (Abgleich über die letzten 3 Zeilen); ans Ende nur, wenn man unten war; anderer Ausschnitt oder mehr als das Doppelte des Limits → einmal komplett neu.
**Nachher (gleiche Messung):** Dashboard 78 Req/min, ≈ 87 ersetzte Knoten je 30 s (nur geänderte Werte/Charts); Heizung-Tab 4 Req/min, Kopfzeile unverändert; Logs 12 Req/min, 0 ersetzte Knoten (nur angehängt); Settings und verdeckte Seite 0 Requests.
**Optik geprüft:** Screenshots mit fest aufgezeichneten API-Antworten (zwei Datensätze A/B) und fester Uhr, Animationen angehalten; Verfahren vorab zweimal gleich = pixelgleich. Vorher/Nachher: Dashboard frisch A, frisch B und Update A→B pixelgleich; Prioritäten Update A→B pixelgleich, frisch 26 Pixel mit max. 1/255 Farbabweichung (Kantenglättung, dieselbe Abweichung zeigte der alte Code zwischen seinen eigenen Pfaden); Logs Update pixelgleich, frisch 13 Pixel mit max. 3/255 am Rand der Kopfzeilen-Pill (Kantenglättung). Verhalten im Browser geprüft: keine Card bei gleichen Daten ersetzt, nur geänderte Card ersetzt, Menü und Drag unberührt, Schalter-Handler nicht doppelt, Logs anhängen/stehen bleiben, Pause und Sofort-Update.

## 2026-10-13 – Heizung-Tab: kein Flackern mehr (Werte in place statt Neuaufbau)

**Vorher:** `renderHeatRooms` ersetzte bei jedem Poll (30 s), nach jedem −/+ und jedem Klick in den Zeitplänen alle Cards per `innerHTML` – inkl. SVG-Verlauf und Zeitplan-Formular (gemessen: 8 Cards je Poll ersetzt). 24-h-Verlauf alle 10 min neu geladen und bei jedem Poll neu gezeichnet. Polling lief bei verdeckter Seite weiter.
**Jetzt (`web/heating.js`):** Cards werden einmal aufgebaut (neu nur, wenn sich die Raumliste ändert); `updateHeatRoom` aktualisiert je Card nur geänderte Slots (Ist + Farbe, Soll, Ventil/Feuchte, Batterie, Modus, „Nächste Änderung“, Warnung, Fehler, Zeitplan-Zähler/-Liste). Ein gesammelter −/+-Wert bleibt sichtbar, der Zeitplan-Bereich wird nicht ersetzt, solange darin getippt wird; Chat-Eingabe und Vorschau fasst das Polling nicht an. Aufgeklappte Verläufe/Zeitpläne und die Scrollposition bleiben. Polling alle 30 s, pausiert bei anderem Tab und bei `document.hidden`, beim Zurückkommen sofort ein Update. 24-h-Verlauf höchstens alle 5 min geladen und nur bei geänderten Daten neu gezeichnet.
**Gemessen (Dev, Headless-Browser, 65 s Heizung-Tab mit offenem Verlauf + Zeitplänen):** Heizungs-Requests unverändert 4/min (status + schedules je 30 s); ersetzte Cards 8 → 0; Verlauf/Zeitpläne offen, Scroll unverändert; 0 Heizungs-Requests bei anderem Tab oder verdeckter Seite. Geprüft: Chat-Eingabe, gesammelter Sollwert und Fokus/Wert im Zeitplan-Formular überstehen einen Poll.
**Andere Tabs (nur berichtet, nicht geändert):** Das Dashboard-Polling (`refreshStatus`, alle 10 s, ≈ 78 Requests/min) läuft unabhängig vom aktiven Tab und auch bei verdeckter Seite weiter; es ersetzt Charts/Listen per `innerHTML` (Wallbox-Chart, Strings-Chart, Kaskaden-Mini, Phasen-Timeline: ≈ 400 DOM-Knoten je 30 s). Prioritäten baut die Kaskaden-Liste zusätzlich alle 10 s neu (≈ 470 Knoten je 30 s). Logs: Auto-Refresh alle 5 s ersetzt den Text und springt ans Ende.

## 2026-10-13 – Chat: ChatGPT (OpenAI) als Anbieter, wählbar in den Settings

**Warum:** Der User hat API-Zugang und Guthaben bei OpenAI, nicht bei Anthropic.
**Config:** `assistant.provider` = `openai` (ChatGPT) | `anthropic` (Claude, Default wenn das Feld fehlt), `assistant.openai_api_key` (nur config.json; beide Keys können nebeneinander stehen), `model` mit Default je Anbieter (`gpt-4.1-mini` bzw. `claude-haiku-5-5`). Settings: Auswahl „Chat-Anbieter“; beim Umschalten wird ein Default-Modell des anderen Anbieters durch das passende ersetzt; Key-Status je Anbieter („assistant.openai_api_key gesetzt/fehlt“). Chat-Kopf zeigt den Anbieter („ChatGPT · 3/100 heute“).
**OpenAI-Aufruf:** offizielles SDK `openai` (neu in requirements.txt, Import erst beim Aufruf), Chat Completions mit demselben Tool-Schema als Function (`strict`), erzwungenem Aufruf von `propose_schedule`, `parallel_tool_calls=false`, 20 s Timeout ohne Retry; Argumente per `json.loads`, Text ohne Tool-Aufruf nur als Rückfrage, `refusal`/`length` mit eigener Meldung. Validierung, Vorschau, Bestätigung, Limit, lokale Abfragen und Ausführung unverändert.
**Fehler:** gleiche Klassen wie bei Anthropic; `insufficient_quota` (429) → „Kein Guthaben … bei OpenAI unter „Billing“ aufladen“, 401 nennt `assistant.openai_api_key`, `model_not_found`/404 → „in Settings ändern“, 400 mit API-Fehlertext; beide Keys werden in Meldungen und Log maskiert.
**Tests:** OpenAI-Request-Form, Parsing, Rückfrage/Refusal/Length, kaputte Argumente, Fehler (Quota, Rate-Limit, Key, Modell, 400, Timeout, offline), Client-Konfiguration, kompletter Web-Ablauf mit `provider=openai`; gesamt 267 (ohne SDKs: 18 übersprungen, 0 Fehler – wie im Deploy-Vorabtest).

## 2026-10-13 – Chat: API-Key ohne Workspace (400) – optionales assistant.workspace_id, API-Fehlertext sichtbar

**Befund (Prod, erster echter Aufruf):** „Die API hat die Anfrage abgelehnt (400)“. Diagnose mit einem vom User ausgeführten Skript (Key nur aus config.json gelesen, maskiert): „This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header …“ – unabhängig vom Tool-Schema (beide Varianten gleich).
**Danach** (User hat einen Key im Default-Workspace angelegt): „Your credit balance is too low …“ – Konto ohne Guthaben, kein Code-Problem; dafür jetzt eine eigene Chat-Meldung („Kein Guthaben … Plans & Billing“).
**Fix:** optionales Feld `assistant.workspace_id` (z. B. `wrkspc_…`, nur in config.json); gesetzt → Header `anthropic-workspace-id` bei jedem Aufruf. Alternativ einen Key innerhalb eines Workspaces anlegen (dann bleibt das Feld leer). Dieser Fehler hat eine eigene Meldung im Chat. Bei 400 steht der Fehlertext der API jetzt im Chat und im Log (Key maskiert, gekürzt), damit die Ursache ohne Nachstellen sichtbar ist.
**Tests:** Workspace-Header nur wenn gesetzt, Validierung, Workspace- und Guthaben-Meldung, 400-Grund ohne Key; gesamt 247.

## 2026-10-13 – Tab „Heizung“ direkt nach „Dashboard“

Reihenfolge der Haupt-Tabs: Dashboard, Heizung, Verlauf, Prioritäten, Settings, Logs (nur `web/index.html`).

## 2026-10-13 – .gitignore: alle config*.json außer config.example.json

`config*.json` + `!config.example.json` statt einzelner Muster (`config.json`, `config.backup-*.json`): auch Arbeitskopien wie `config.edit.json` können nicht mehr versehentlich committet werden. `config.json.bak-*` bleibt. Geprüft mit `git check-ignore`: config.json, config.edit.json, config.backup-*.json ignoriert; config.example.json nicht ignoriert und weiter versioniert.

## 2026-10-13 – deploy.sh: Wegwerf-Worktree wirklich entfernen, verwaiste Verweise aufräumen

Der EXIT-Trap rief `git worktree remove "$TMP"` auf, der Worktree lag aber unter `$TMP/wt` – das Entfernen schlug still fehl, `rm -rf` löschte nur das Verzeichnis, und je Deploy blieb ein verwaister Verweis in `.git/worktrees` (Prod: 11 Stück). Jetzt entfernt der Trap `$TMP/wt` und ruft danach `git worktree prune` auf – bei jedem Ende (ok, Rollback, Abbruch). Einmalig in `~/pvcontroller-prod` ausgeführt.

## 2026-10-13 – Chat: 20-s-Timeout ohne Retry (Dev)

Der API-Aufruf hatte 45 s Timeout mit 1 automatischem Retry (bis ≈ 90 s Wartezeit im Chat). Jetzt `anthropic.Timeout(20 s, connect 5 s)` und `max_retries=0`; eine Zeitüberschreitung meldet der Chat als „Die API antwortet nicht (Zeitüberschreitung nach 20 s)“ (eigene Meldung statt „keine Verbindung“). Die Chat-Endpunkte sind synchron und laufen im FastAPI-Threadpool – geprüft mit einer hängenden Mock-API in Dev: Chat-Antwort nach 21,5 s, währenddessen Dashboard, `/api/status` und `/api/heating/status` normal in 3–540 ms. Tests: Timeout-Konfiguration, Timeout-Meldung, Endpunkte nicht `async`; gesamt 243.

## 2026-10-13 – Heizung Phase 3: Chat → Zeitplan-Vorschlag über die Claude API (Dev)

**Ablauf (`heating_chat.py`, nur Backend):** Text + Kontext an die Messages API (Python-SDK `anthropic`, neu in `requirements.txt`, Import erst beim Aufruf). Kontext: Datum/Uhrzeit Europe/Berlin mit Wochentag und Kalender der nächsten 8 Tage, Räume mit IDs und aktuellen Ist/Soll-Werten (Anzeige-Räume markiert), Grenzen, alle Zeitpläne mit ID und die Schaltpunkte der nächsten 7 Tage. Genau ein Tool `propose_schedule` (`strict`, `tool_choice: auto` + Anweisung, ein Aufruf) mit `entries` (einmalig `date` oder wiederkehrend `days`), `immediate`, `delete_ids`, `question`, `summary`. Kein freier Text-Pfad schaltet etwas: Text ohne Tool-Aufruf wird nur angezeigt.
**Server prüft alles** (gleiche Regeln wie Phase 2: Raum existiert und ist steuerbar, Sollwert in Grenzen/0,5-Raster, HH:MM, Datum/Zeit nicht in der Vergangenheit, Lösch-IDs existieren). Ungültig → Meldung im Chat, nichts gespeichert. Gültig → Vorschau mit Gruppen („Morgen (Mi 14.10.)“, „Jeden Mo–Fr“), Sofort-Änderungen und Löschungen („ganzer Plan“ bei wiederkehrenden), Hinweis wenn „Zeitpläne aktiv“ aus ist bzw. Dry-Run. Erst „Übernehmen“ speichert: Vorschlag liegt serverseitig (ID, 15 min, nur einmal), wird beim Übernehmen erneut geprüft; Zeitpläne mit `source='chat'` und dem Text als `note`, Sofort-Werte über denselben Weg wie die Buttons (`dry_run` respektiert), alles im Log. Rückfragen gehen mit dem Verlauf (max. 12 Nachrichten à 500 Zeichen) zurück an die API. „Was ist (morgen/heute/im Bad) geplant?“ wird lokal aus der DB beantwortet – ohne API, zählt nicht zum Limit. Ausführung der Pläne unverändert (Phase 2, nur mit „Zeitpläne aktiv“).
**Freigabe/Config:** `assistant` {`enabled` = Settings-Schalter „Chat aktiv“ (Default aus), `api_key` (nur config.json, nie Frontend/Log/Git; `/api/config` maskiert, Settings senden ihn nie), `model` (Default `claude-haiku-5-5`), `max_requests_per_day` (Default 100)}. Ohne Schalter/Key/SDK oder bei erreichtem Limit: Chat-Feld ausgegraut mit Hinweis, Rest des Tabs unverändert. Zähler je Tag in `assistant_usage`, im Status (`/api/heating/status` → `chat`) und im Chat-Kopf („3/100 heute“); Limit → freundliche Meldung, kein Fehler.
**Fehler:** 404/ungültiges Modell → „Modell „…“ nicht verfügbar – in Settings ändern.“; ungültiger Key, Rate-Limit, 5xx, offline jeweils mit eigenem Text (typisierte SDK-Fehlerklassen). Log nur Fehlerklasse, Status, Modell, request-id – nie Key oder Fehlertext.
**Netz:** Pi erreicht `api.anthropic.com` (HEAD /v1/messages → HTTP 405 = erreichbar, TLS nach 75 ms, 160.79.104.10).
**Deploy:** `deploy.sh` testet vor dem `pip install`; die Tests, die das SDK brauchen, werden dann übersprungen (geprüft: ohne `anthropic` 240 Tests, 4 übersprungen, 0 Fehler).
**Tests:** `tests/test_heating_chat.py` (26, API gemockt): Kontext, Request-Form (ein Tool, strict, kein Key), „morgen“, „übermorgen“, „Freitag“, „jeden Werktag“, Rückfrage mit Verlauf, 35 °C und Vergangenheit abgelehnt, unbekannter/Anzeige-Raum, Löschen, Übernehmen (source chat, nur einmal), Sofort nach Klick, Abfrage ohne API, Tageslimit, Schalter aus, Key/SDK fehlt, 404/401/offline/400-Modell, Key taucht nirgends auf, Settings behalten den Key; gesamt 240.

## 2026-10-13 – Zeitpläne: Rückprüfung 5/10/15 min statt nach 1 min (openHAB-autoupdate); Verlauf-Zeitraum (Dev)

**Befund (nur gelesen, openHAB unverändert):** Die 7 Sollwert-Items haben keine `autoupdate`-Metadaten, die Homematic-Kanaltypen (`…_SET_TEMPERATURE`, `…_SET_POINT_TEMPERATURE`) keine `autoUpdatePolicy` → openHAB-Default: autoupdate aktiv. Belegt durch den Test des Users in Prod (Zeitpläne Schlafen 07:57 → 21 °C, 07:58 → 17 °C): `events.log` zeigt 2 ms nach dem Kommando „changed … (source: org.openhab.core.autoupdate.optimistic)“ – die bisherige Prüfung nach 1 min las also den von openHAB selbst gesetzten Wert. Das Gerät bestätigte nach 1 s bzw. 8 s. Meldetakt (12 min SSE `stateupdated` mitgeschnitten): HM (Wandthermostate, CC-RT-DN Bad/Schlafen/Gäste-WC) alle 2–3 min, HmIP (Kind, Katzenzimmer) alle 5 min. rrd4j speichert minütlich und zeigt daher nur Wertwechsel: Schlafen springt zwischen 4,5 °C (AUS/Fenster-offen) und 19,5/20 °C (11.–12.10. sechsmal), Wohnen folgt einem Wochenprofil (06:01 → 21, 22:02 → 17). Nebenbefund: openHAB-JVM läuft mit `-Duser.timezone=Europe/London` (events.log 1 h hinter Ortszeit), Regionaleinstellung ist Europe/Berlin.
**Rückprüfung neu:** Prüfungen 5, 10 und 15 min nach dem Senden (dazwischen kein openHAB-Request). Gesendeter Wert → weiter beobachten, nach 15 min fertig. Gerät meldet wieder den **alten** Wert (Zurückspringen, `heating_verify.previous`) → Warnung „Bad: 21 °C gesendet, Gerät meldet wieder 15 °C (zurückgesprungen) – sende einmal erneut“, ein Retry mit neuem 15-min-Fenster, danach nur Meldung. **Anderer** Wert (z. B. 4,5 °C Fenster-offen, am Gerät verstellt, Wochenprofil) → Warnung + Card, **kein** erneutes Senden (nicht gegen Fenster-offen anschalten); die Meldung verschwindet, sobald das Gerät wieder den gesendeten Wert meldet. Item ohne Wert → nach 30 min „nicht prüfbar“. Migration der Tabelle (`previous`, `checks`) per `ALTER TABLE`.
**Verlauf 24 h:** `get_history` schickte `starttime` ohne Zeitzone; openHAB ignorierte das und lieferte immer seinen Default (≈ 25 h), `hours` war wirkungslos. Jetzt ISO mit Offset.
**Tests:** Rückprüfung neu (Requests nur bei 5/10/15 min, Zurückspringen + 1 Retry, Retry hält, anderer Wert ohne Retry, Item ohne Wert, Migration), Zeitzone im Verlauf; gesamt 214.

## 2026-10-13 – Kaskade respektiert dry_run beim Schalten; Zeitpläne: Abbruch sicher (Dev)

**Kaskade:** `cascade_service._dispatch` schaltete Shelly-Geräte auch bei `runtime.dry_run`. Jetzt nur Log „DRY-RUN: Kaskade würde Shelly X einschalten (IP, Kanal n) – nicht geschaltet“; gilt auch für manuelle Übersteuerungen. Entscheidung, DB-Zustand (`is_on`) und `cascade_log` unverändert, Status-Lesen bleibt. Test `tests/test_cascade_dry_run.py` (3; zwei schlagen mit dem alten Code fehl).
**Zeitpläne, Abbruch mitten im Lauf:** `last_run` und Rückprüfung werden nach erfolgreichem Senden in **einer** Transaktion geschrieben (`_mark_sent`) – bricht der Daemon-Thread dazwischen ab, gilt beides oder keins. Ein Sendefehler (z. B. Lese-Timeout, Befehl evtl. doch angekommen) wird nicht mehr sofort `failed`, sondern ist in der nächsten Minute **einmal** erneut fällig (`last_result = retry`), danach `failed`. Doppeltes Senden verhindert das Lesen vor dem Senden (schon eingestellt → `unchanged`). Tests: Abbruch nach dem POST vor dem DB-Eintrag, Abbruch vor dem Senden, Transaktion, Retry/aufgegeben, Timeout-aber-übernommen; gesamt 210.

## 2026-10-13 – Heizung Phase 2: Zeitpläne im Minuten-Cron (Dev)

**Tabelle `heating_schedule`** (wie im Prompt, zusätzlich `last_result`: sent/unchanged/dry_run/skipped/superseded/failed) und `heating_verify` (Rückprüfung je Raum), angelegt beim ersten Zugriff. Wiederkehrend = `days` („mo,di,…“), einmalig = `date`; Uhrzeiten Europe/Berlin.
**Ausführung (`heating_schedule.py`, aus `main.py`):** läuft **nach** Kaskade, Controller, PV-Log und Temperaturverlauf in einem eigenen Daemon-Thread mit hartem Limit (`HEATING_BUDGET_S` = 5 s, `join`); Fehler werden geloggt, der Rückgabewert von `main.py` bleibt der des Controllers. Innerhalb des Budgets bekommt jeder openHAB-Request nur die Restzeit als Timeout, unter 0,5 s Rest startet kein Request mehr (Eintrag bleibt fällig). Ohne fällige Einträge und ohne offene Rückprüfung: nur ein DB-Zugriff, **kein** openHAB-Request. Sollwerte aller betroffenen Räume in **einem** Request. Fällig = Uhrzeit erreicht, heute noch nicht gelaufen, Wochentag/Datum passt; nach der Uhrzeit angelegte Einträge werden heute nicht nachgeholt. Nachholen bis 15 min Verspätung, sonst „Zeitplan übersprungen … min verspätet“ (einmal). Mehrere fällige Einträge je Raum → nur der späteste (`superseded`). Sollwert schon eingestellt → nichts senden. Einmalige Einträge danach `enabled=0`, `db.prune` löscht sie 7 Tage nach ihrem Datum. Zeitumstellung: doppelte Stunde (25.10.) → nur das erste Auftreten; fehlende Stunde (29.03.) → 02:xx läuft um 03:00.
**`dry_run`:** Dev loggt nur („DRY-RUN: … nicht gesendet“), sendet nie, keine Rückprüfung.
**Rückprüfung:** im Lauf nach dem Senden wird der Sollwert gelesen. Abweichung → Warnung ins Log und auf die Card („Bad: 21 °C gesendet, Gerät meldet 15 °C“), **einmal** erneut senden; weicht er danach weiter ab, bleibt nur die Meldung (keine Schleife). Die Meldung verschwindet, wenn das Gerät den Wert meldet oder ein neuer Sollwert (Button) gesetzt wird. openHAB länger als 30 min weg → „Rückmeldung nicht prüfbar“.
**Freigabe:** neuer Settings-Schalter „Zeitpläne aktiv“ (`heating.schedules_enabled`, Default **aus**). Aus: Pläne lassen sich anlegen, nichts wird ausgeführt (auch kein DB-Zugriff im Cron), Tab zeigt den Hinweis, „Nächste Änderung“ mit „(Zeitpläne aus)“. `PUT /api/heating/config` lässt den Schalter unverändert, wenn das Feld fehlt; Umschalten wird geloggt.
**API:** `GET/POST /api/heating/schedules`, `PATCH /api/heating/schedules/{id}` (an/aus), `DELETE /api/heating/schedules/{id}`; `/api/heating/status` liefert je Raum `next` und `alert` sowie `schedules_enabled`. Anlegen/Löschen steht im Log.
**UI:** je Raum „Nächste Änderung: heute 22:00 → 18 °C“, aufklappbar „Zeitpläne (n)“: Uhrzeit, Soll, Wochentage als Chips bzw. „einmalig Mi 14.10.“, Schalter, Löschen, letzte Ausführung; „+ Zeitplan“ mit Uhrzeit, Soll, Wochentagen (Werktags/Täglich) oder Datum. Keine Wochenansicht (optional, offen).
**Unverändert:** PV-/Heizstab-/Wallbox-/Batterie-Logik, openHAB-Regeln (Bad/Schlafen bleiben deaktiviert).
**Laufzeit:** Heizungsteil mit Schalter aus 0,2–0,5 ms, an ohne Fälliges 1–26 ms (DB), mit fälligem Eintrag 250–300 ms (ein Sammel-GET gegen openHAB, dry-run). Dev-`main.py` gesamt vorher 4,3/7,0 s, nachher 6,3–9,0 s (Streuung durch Geräte-Netzwerk, der Heizungsteil ist darin < 1 ms).
**Dev-Config:** `shelly.ph1_type/ph2_type/ph3_type` = `shelly_gen2` wie Prod (Backup `config.json.bak-20261013-072551`).
**Tests:** `tests/test_heating_schedule.py` (33: Fälligkeit, Wochentage, einmalig, unverändert, Nachholen ≤ 15 min/übersprungen, mehrere je Raum, Zeitumstellung 25./26.10. und 29.03. inkl. Minuten-Cron über die doppelte Stunde, openHAB tot/Sendefehler, kein Request ohne Fälliges, Schalter aus, dry_run, Rückprüfung mit 1 Retry, Zeitbudget, Validierung, Prune, **openHAB hängt 10 s → `main.py` endet nach ≈ 5,5 s, PV-Steuerung vorher vollständig**, Web-Endpunkte), gesamt 203.

## 2026-10-13 – Heizstab-Zähler tot: Ersatzwert aus den Phasen-Shellys statt DB-Nennwert (Dev)

**Vorfall (Prod):** Der Heizstab-3EM (`heater_meter_url` `.21`) war ab 12.10. 18:43 nicht erreichbar. Er hatte um 18:41 neu gebootet und per DHCP `192.168.2.148` (WLAN002) bekommen; auf `.21` antwortet seitdem ein anderes Gerät nur per ARP (MAC `98:cc:f3:2a:94:8b`, kein HTTP). Gefunden per Shelly-Scan (`/shelly`, `/settings`, `/status`; MAC `48:55:19:DB:E5:1B`). Der User hat am Gerät statisch `.148` eingestellt; `shelly.heater_meter_url` in Prod- und Dev-`config.json` auf `http://192.168.2.148` (Backups `config.json.bak-20261013-031332`). Ab dem Lauf 03:14 wieder gemessen, `device_health` leer.
**Tatsächliche Folgen 18:43–03:14:** keine. Die PV lag schon bei ≈ 0 (Sonnenuntergang), alle drei Phasen waren aus (letzter Phasenwechsel 14:47, Speicher ab 14 Uhr auf Max-Temperatur), kein Kaskaden-Ereignis nach 15:48, `heater_w` = 0 war korrekt (Controller-Schätzung aus Phasenzuständen). Die 6,1 kW Bezug um 19 Uhr kamen nicht vom Heizstab (Phasen aus, Speicher fiel 63,5 → 62,4 °C) und nicht von der Wallbox (0 W).
**Risiko bei Sonne:** Die Kaskade nahm bei totem Zähler `power_watts` (1500 W) aus der DB, sobald `is_on` (= Budget erlaubt ≥ 1 Phase) gesetzt war – unabhängig davon, wie viele Phasen wirklich liefen. Mit 2–3 laufenden Phasen fehlten 1,5–3 kW im Brutto-Überschuss (Budget zu klein → Abschalten/Flattern), bei Max-Temperatur entstand ein Phantom-Budget von 1,5 kW (Einschalten aus dem Netz). Betroffen: Kaskaden-Budget und Glättung (`heater_budget_log`), Grundlast fürs Batterie-Entladen. Controller (`surplus_without_heater`, `true_surplus`), Energiefluss, Hausverbrauch und `pv_hourly_log.heater_w`/Temperaturverlauf nutzten die Controller-Schätzung (Phasen an/unbekannt × 1500 W).

**Fix:** neues Modul `heater_power.py`, von Controller und Kaskade gemeinsam genutzt. Ist der Zähler nicht lesbar, gilt die Summe der Phasen: gemessene `apower` (Gen2/Gen3 `Switch.GetStatus`, Gen1 `/status` meters[]), ohne Messung Nennleistung wenn an; eine **unlesbare** Phase zählt mit Nennleistung, falls sie an sein könnte (letzter Eintrag in `HeizstabPhaseLog` „on“ oder keiner), 0 W wenn sie zuletzt nachweislich ausgeschaltet wurde; stillgelegte Phasen zählen nicht. Der DB-Pfad `is_on × power_watts` in der Kaskade entfällt. `ShellyPlugClient.get_status()` liefert Zustand + Leistung in einem Abruf (Controller liest dadurch nicht mehr). `Readings.heater_power_source` = `meter` | `phase_sum`, im Status als `heater.heater_power_source`; Dashboard zeigt „≈… W (aus Phasen)“ bzw. im Energiefluss „aus Phasen“. Banner nach ≥ 10 min: „Heizstab-Zähler (…) seit HH:MM Uhr nicht erreichbar – Ersatzwert aus Phasen“ (`device_alerts[].text`).
**Tests:** `tests/test_heater_power.py` (14: Schätzung, Controller und Kaskade mit totem Zähler, Phasen an / eine Phase unbekannt, kein Phantom-Budget, Banner-Text; die Kaskaden-Tests schlagen mit dem alten Code fehl), gesamt 170.

## 2026-10-13 – Neuer Tab „Heizung“: openHAB-Thermostate anzeigen und Sollwert setzen (Phase 1, Dev)

**Bestandsaufnahme (Phase 0, nur gelesen):** openHAB enthält neben Zuhause auch FeHa01, FeWo01/02/04/05 und Büro. Zuhause = Sitemap `home` → „Heizung Zuhause“: 7 steuerbare Räume (Wohnen & Büro, Essen & Küche: HM-Wandthermostate; Bad, Schlafen, Gäste-WC: HM CC-RT-DN; Kind: HmIP eTRV-E; Katzenzimmer: HmIP eTRV-2) und Waschküche (nur Sensor). Regeln für diese Items: nur „Heizung Bad an/aus“ und „Heizung Schlafen an/aus“, alle **deaktiviert** (bleiben so). rrd4j persistiert Ist/Soll minütlich. `Kinderzimmer_Control_Mode`/`Katzenzimmer_Control_Mode` hängen am HmIP-Kanal `CONTROL_MODE` (Umschalt-Kanal, meldet konstant 0 = „AUTO“, obwohl die CCU „Manu“ zeigt); der echte Modus steht in `SET_POINT_MODE`, der Kanal ist nicht verlinkt → Modus bei Kind/Katzenzimmer wird nicht angezeigt. Bei Bad/Schlafen/Gäste-WC existiert `4#CONTROL_MODE` (AUTO-MODE/MANU-MODE/…), ebenfalls ohne Item.

**Config:** `openhab` {`url`, `token` (API-Token, nur in `config.json`), `timeout_s` (Default 3), optional `persistence`} und `heating` {`enabled`, `setpoint_min` (5), `setpoint_max` (26), `rooms`: `id`, `name`, `temp_item` (Pflicht), optional `setpoint_item`, `valve_item`, `battery_item`, `humidity_item`, `mode_item`}. Raum ohne `setpoint_item` = nur Anzeige. Beispiel in `config.example.json`.
**Client** `clients/openhab_client.py`: Bearer-Token, `get_item`, `get_items` (ein Request für alle Items), `send_command` (POST text/plain), `get_history` (Persistence). `base_url` mit oder ohne `/rest`.
**`heating.py`:** Config-Prüfung, Normalisierung (Feuchte/Ventil: `Number:Dimensionless` als Anteil → %, Dimmer/Number schon %; Batterie: Switch → OK/niedrig, Zahl → %; Modus: `AUTO-MODE`/`MANU-MODE` bzw. 0/1/2), Status mit Fehler-Isolation (Sammelabfrage schlägt fehl → je Raum einzeln), Sollwert-Prüfung (Grenzen, 0,5-°C-Raster).
**API:** `GET /api/heating/status`, `POST /api/heating/setpoint`, `GET /api/heating/history/{room}` (24 h, 10-min-Mittel), `GET /api/heating/items` (Auswahl in den Settings), `GET/PUT /api/heating/config`. Schreiben respektiert `runtime.dry_run` (nur Log-Zeile „DRY-RUN …“) – Dev und Prod nutzen dasselbe openHAB. Jede Schaltung steht im Controller-Log (Log-Tab).
**Secrets in `/api/config`:** Bisher wurden `solax.pwd`, `solax2.pwd`, `auth_password` (in Dev/Prod aktuell leer) und `openhab.token` im Klartext ausgeliefert. Jetzt maskiert `GET`/`PUT /api/config` zentral jedes gesetzte Feld, dessen Name `token`, `key`, `password`, `passwd`, `pwd` oder `secret` enthält (rekursiv, deckt auch das künftige `assistant.api_key` ab) als `********`; leere Felder bleiben leer. Schickt die UI `********` zurück, bleibt der gespeicherte Wert erhalten. Das Passwortfeld (Zugriffsschutz) wird jetzt mit der Maske vorbelegt – vorher schickte jedes Speichern der Settings `password: ""` und hätte einen gesetzten Passwortschutz still gelöscht; leer eintragen deaktiviert ihn weiterhin. `/api/solax2/test` nimmt bei maskiertem Passwort das gespeicherte.
**Prod ohne `openhab`/`heating`-Block:** Heizung gilt als deaktiviert, der Tab zeigt den Hinweis „openHAB ist nicht konfiguriert …“ (`openhab_configured` im Status); `load_config` (Deploy-Vorabprüfung) und `main.py` sind unberührt. Mit der echten Prod-`config.json` gegen den neuen Code geprüft.
**UI:** Tab „Heizung“ zwischen Prioritäten und Settings (`web/heating.js`): Card je Raum mit Ist (blau < Soll − 1, orange > Soll + 1), Soll mit −/+ (0,5 °C, wird nach 0,9 s Pause gesendet), Ventil, Luftfeuchte, Batterie (🔋 OK / ⚠️), Modus (AUTO orange: „Gerät folgt eigenem Wochenprofil“), Verlauf 24 h (Ist + Soll) aufklappbar; Aktualisierung alle 30 s. Kein „Alle Räume“-Block (auf Wunsch entfernt). Settings: Block „Heizung“ mit Räumen (Item-Auswahl aus openHAB, Reihenfolge, entfernen), eigener Speichern-Button.
**Tests:** `tests/test_heating.py` (30, inkl. Prod ohne Heizung-Blöcke), `tests/test_config_secrets.py` (5), gesamt 156.

## 2026-10-12 – Heizstab-Shellys getauscht/umgestellt, eigenes Timeout für den Pergola-WR

**Betrieb (Prod, nicht im Code):** PH1-Shelly ersetzt (jetzt Shelly Plus 1PM Gen2, FW 1.4.0, feste IP `192.168.2.93` per FRITZ!Box-Reservierung; vorher IP-Konflikt auf `.53` mit unbekannter MAC `44:bd:8d:15:f0:00`, Ursache der sporadischen „Connection refused“-Lesefehler). `config.json` (Backups `config.backup-20261012-163717.json`, `…-165557.json`): `shelly.ph1_type/ph2_type/ph3_type` = `shelly_gen2` (PH2 = Plus 1PM Gen2 FW 1.7.1, PH3 = 1PM Gen3 FW 1.2.2; liefen bisher nur über den Gen1-Kompatibilitätsmodus). Auf allen drei Shellys `Switch.SetConfig`: `in_mode: detached`, `initial_state: off` (vorher `follow`/`match_input`: Relais folgte nach Stromausfall dem Eingang) – per `Switch.GetConfig` bestätigt. Alle Geräte der Config antworten stabil unter ihrer IP, keine weiteren MAC-Wechsel. Cron-Läufe danach: PH1/PH2/PH3 gelesen (kein `?`), keine Fehler.

**Pergola-WR (solax2) eigenes HTTP-Timeout:** neues Feld `solax2.timeout_s` (Default **3 s**), `Config.solax2_timeout`; verwendet in Controller, Kaskade und Web. Der Haupt-WR bleibt bei `runtime.request_timeout_seconds` (5 s). Hinweis: Der Pergola-Client lief bisher mit 5 s (nicht 1,5 s – das ist das Shelly-Lese-Timeout); der WR antwortet in 1–1,5 s, nachts gar nicht (Pocket-WLAN schläft): mit 2 Versuchen je Lesen sinkt die Wartezeit bei Ausfall von bis zu 10 s auf 6 s. Test `tests/test_solax_timeout.py` (121 Tests).

## 2026-10-12 – cascade_log nur noch Ereignisse, Retention, AUS auch an unbekannte Phasen, Anzeige-Fix (Dev)

**cascade_log (war 96,7 von 97,7 MB der DB; 96,8 % `keep_*`, ≈ 0,6 MB/Tag):**
- Dauerhaft geschrieben werden nur Ereignisse: `turn_on`/`turn_off` (auch Heizstab-Budget erlaubt/verbietet), `auto_(re)enabled`; Mindestzeit-Sperren (`skip_min_*`), `manual_override` und Batterie-Betriebsarten nur beim **Wechsel** (erste Minute eines Zustands). `keep_on`/`keep_off`/`budget` nur mit `cascade.log_verbose=true` (Default false, `config.json`-Block `cascade`).
- Die aktuelle Entscheidung samt Grund steht je Gerät in `cascade_devices.last_action/last_reason/last_decision_at` (wird überschrieben) und speist `/api/cascade/status`. Verlauf-Ansicht unverändert; leere Liste → „Keine Schaltvorgänge.“.
- `db.prune()` (täglich, erster Minuten-Lauf ab 03:00, Merker `cascade_settings.last_prune`, kein neuer Cron, Batches à 5000, kein VACUUM; löst das stündliche `cleanup_old_hourly` im Minuten-Lauf ab): `cascade_log` Ereignisse 365 Tage, `keep_*`/`budget` 2 Tage, `pv_hourly_log` 90 Tage, `main_meter_min`/`zendure_min` 35 Tage (Tagesverlauf-Ansicht, bewusst nicht 14), `heater_budget_log` 14 Tage; unbegrenzt: `pv_daily_log`, `weather_log`, `house_peak_day`, `storage_day`. Erster automatischer Lauf nach dem Deploy löscht ≈ 656k Zeilen (≈ 23 s) – besser vorher `deploy/maintenance/prune_once.py` ausführen.
- `deploy/maintenance/prune_once.py` (Dry-Run Default; `--apply`: Snapshot `~/backup/pvcontroller-snapshot/pre-prune-<Zeit>.db`, exklusiver flock auf `/tmp/pvc-code.lock`, alle `keep_*`/`budget` löschen, `prune()`, VACUUM, `integrity_check`). Probe auf Kopie der Prod-DB: 97,69 → 4,39 MB, `integrity_check` ok, Verlauf-API liefert weiter die Ereignisse.

**Heizstab, Sicherheit:** `decide_phases` sendet AUS jetzt auch an Phasen mit **unbekanntem Zustand** („blind“) – bei Max-Temp, Temperatur unlesbar, Sommermodus-Stopp, manuell AUS und wenn das Budget-Ziel weniger Phasen als laufend vorsieht (dann Mindestlaufzeit beachtet); AN nie. Jeden Lauf erneut, Timeout 1,5 s, Fehler nur einmal je Serie im Log (Merker `blind_off_fail_phN`), kein Eintrag in `HeizstabPhaseLog`. Anlass: PH1-Shelly antwortet nur sporadisch; bei 64,6 °C zeigte das Relais gelegentlich EIN, der Controller konnte es nicht abschalten.
**Anzeige:** Energiefluss zeigt „heizt/lädt“ nur noch bei gemessener Heizstab-Leistung (> 50 W), nicht schon bei eingeschaltetem Relais (`app.js?v=20261012i`).
**Tests:** 117 (neu: `tests/test_db_retention.py`, Blind-AUS in `test_heater_phases`/`test_controller_phases`, cascade_log ohne keep/budget).

## 2026-10-12 – Fix: `log_daily_summary` nullte Prognose und Wetter (Dev, nicht deployt)

`pv_logger._log_daily` → `db.log_daily_summary` schrieb mit `INSERT OR REPLACE` in `pv_daily_log`. REPLACE löscht die ganze Zeile und nullte damit jede Minute die Spalten, die der Aufruf nicht kennt (`forecast_kwh`, `forecast_ghi`; bei fehlendem Wetterabruf auch `sunshine_h`, `ghi_kwh_m2`, `cloud_cover_pct`, `temp_avg_c`; Pergola-/Batterie-Spalten). Folge: Die Prognose von `/api/forecast` hielt höchstens bis zum nächsten Minuten-Lauf; sichtbar wurde sie erst, wenn `backfill_historical_forecasts()` sie nach Mitternacht nachtrug. Jetzt `INSERT … ON CONFLICT(date) DO UPDATE`: Kennzahlen (PV, Einspeisung, Bezug, Eigenverbrauch, Autarkie) werden immer neu gesetzt, optionale Spalten (Wetter, Prognose, Pergola, Batterie) nur bei übergebenem Wert (`COALESCE`).
Das Tageshoch (`house_peak_day`) war nicht betroffen: eigene Tabelle. Geprüft, kein Handlungsbedarf: `pv_hourly_log` (schreibt alle 18 Spalten), `weather_log` (alle 8), `storage_day` (alle 5), `cascade_settings`/`heater_budget_log` (Schlüssel/Wert). Neue Tests (`tests/test_pvlog.py`, schlagen mit dem alten Code fehl): Prognose und Tageshoch überleben `_log_daily`; Wetter bleibt ohne Neuwert erhalten, ein Neuwert überschreibt. 96 Tests.

## 2026-10-12 – Batterie: Winter-Untergrenze, Glättung vor Batterie, pvlog.py (Dev, nicht deployt)

**1. Winter-Untergrenze `heater.winter_comfort_min_temp`** (Default **40 °C**, leer = aus; Einstellung unter Heizstab & Wasserspeicher). Das Winterprofil bekommt die Stufe „Wasserspeicher (bis 40 °C)“ **vor** der Batterie: `Wasserspeicher bis 40 °C → Batterie → Wasserspeicher bis Max`; sie nimmt nur ganze Phasen, der Rest geht wie gehabt an die Batterie. Nur PV-Überschuss, kein Netz-Heizen. Ohne Batterie ändert sich nichts. `Config.comfort_min_for(season)`, `cascade_plan.normalize_profile(..., min_stage)`; API/UI zeigen die Stufe im Winter nur, wenn gesetzt. Hinweis: Fehlt der Schlüssel in der `config.json`, gilt der Default 40 °C.

**2. Glättung:** Geglättet wird jetzt der **Gesamt-Überschuss** (vor der Batterie), die **aktuelle** Batterie-Ladung wird danach ungeglättet abgezogen (`_store_heater_budget`, `walk(..., heater_fn)` liefert `gross_w` und `battery_w`). Wird die Batterie voll, steigt das Budget im selben Zyklus. Ergänzung gegenüber der Vorgabe („Deckel immer“): Das Zuschalt-Mittel wird **immer** auf den jetzt freien Überschuss gedeckelt (`min(Mittel, Überschuss jetzt)`, auch ohne Batterie), sonst startet der Heizstab nach einem Einbruch auf dem noch hohen Mittel aus dem Netz. Simulation 07.–12.10. ohne Batterie: Heizstab-Energie nicht aus Überschuss 3,54 → 2,08 kWh, Netzbezug −1,7 kWh, Phasenwechsel 4,6 → 3,9/h. Abschalten nutzt weiter das kurze Mittel. **Das ändert auch das Verhalten ohne Batterie** (Start nur noch bei aktuell vorhandenem Überschuss).

**3. `pvlog.py`** nutzt jetzt dieselben Logger wie der Minuten-Lauf (`pv_logger._log_hourly/_log_daily`, Messwerte per `Controller._read_all()`): Gesamt-PV und Hausverbrauch inkl. Pergola und Batterie (+ Entladung, − Ladung), Eigenverbrauch/Autarkie aus Gesamt-PV, Einspeisung/Bezug aus dem Hauptzähler. Die alte Prognose-Berechnung entfiel: Der Wert (Prognose für morgen in der heutigen Zeile) wurde schon bisher vom Minuten-Lauf (`INSERT OR REPLACE` in `log_daily_summary`) sofort wieder überschrieben; die Prognose schreiben `web.api_forecast` → `db.upsert_daily_forecast` und `db.backfill_historical_forecasts()` (läuft bei jedem `import db`, füllt NULL-Werte vergangener Tage). **Die beiden pvlog-Cron-Jobs wurden aus der Crontab entfernt** (Sicherung `~/backup/crontab/crontab.pre-pvlog-removal`).

Beobachtung (nicht geändert): `log_daily_summary` überschreibt die Prognose-Spalten der heutigen Zeile jede Minute mit NULL; sichtbar wird die Prognose eines Tages erst, wenn der Backfill sie nach Mitternacht nachträgt. Für 13.10. (steht in der Zeile 12.10.) ist daher noch kein Wert gespeichert; Wetterdaten für den 13.10. liegen vor.

**Tests:** 94 (vorher 80; neu u. a. Winter-Untergrenze, Glättung „Batterie wird voll → Budget steigt im selben Zyklus“, Überschuss-Einbruch, `tests/test_pvlog.py`).

**Simulation** (07.–12.10., Methode wie oben; neu: Speichertemperatur wird je Tag auf die Messung zurückgesetzt):

### Heizstab-kWh je Tag (gemessen | Sim ohne Bat. | Winter | Winter+40 °C | Sommer)
| Tag | gemessen | Sim ohne Bat. | Winter | Winter + 40 °C | Sommer |
|---|---|---|---|---|---|
| 2026-10-07 | 6.3 | 6.3 | 6.2 | 6.2 | 6.2 |
| 2026-10-08 | 0.9 | 1.1 | 0.2 | 0.2 | 0.9 |
| 2026-10-09 | 7.0 | 7.0 | 6.7 | 6.7 | 6.9 |
| 2026-10-10 | 2.0 | 2.5 | 1.4 | 1.4 | 1.6 |
| 2026-10-11 | 6.0 | 7.5 | 4.0 | 4.0 | 5.5 |
| 2026-10-12 | 2.5 | 2.2 | 0.4 | 1.9 | 1.9 |
| Σ | 24.6 | 26.6 | 19.0 | 20.5 | 23.0 |

### Batterie geladen kWh (Winter | Winter+40 | Sommer) und Netzbezug Σ
| | Winter | Winter + 40 °C | Sommer |
|---|---|---|---|
| Batterie geladen Σ | 17.1 | 15.6 | 12.2 |
| Einspeisung Σ | 14.8 | 14.9 | 14.8 |
| Netzbezug Σ | 190.4 | 190.5 | 193.0 |

### Glättung (Σ 6 Tage): alt = Budget nach Batterie glätten | wörtlich = Gesamt-Überschuss glätten, Batterie danach | umgesetzt = wörtlich + Start nie über dem jetzt freien Überschuss
| Profil / Variante | Einspeisung | Netzbezug | Heizstab | davon nicht aus Überschuss | Batterie geladen |
|---|---|---|---|---|---|
| Winter alt | 14.99 | 189.0 | 17.3 | 2.35 | 16.7 |
| Winter wörtlich | 14.71 | 191.0 | 19.6 | 4.27 | 17.1 |
| Winter umgesetzt | 14.75 | 190.4 | 19.0 | 3.75 | 17.1 |
| Sommer alt | 15.41 | 193.2 | 22.5 | 2.84 | 11.8 |
| Sommer wörtlich | 15.08 | 193.5 | 23.2 | 3.60 | 12.2 |
| Sommer umgesetzt | 14.85 | 193.0 | 23.0 | 3.15 | 12.2 |
| (Sim ohne Batterie) | 18.78 | 200.8 | 26.6 | 3.54 | 0 |

### Phasenwechsel (Σ / Stunden / je Stunde)
- ohne: 96 / 21 / 4.6
- winter_alt: 68 / 20 / 3.4
- winter_lit: 93 / 20 / 4.7
- winter: 87 / 20 / 4.3
- winter40: 92 / 20 / 4.6
- summer_alt: 94 / 21 / 4.5
- summer_lit: 106 / 21 / 5.0
- summer: 102 / 21 / 4.9

### Speichertemperatur 12.10. (gemessen | Winter | Winter + 40 °C | Sommer)
| Uhrzeit | gemessen | Winter | Winter + 40 °C | Sommer |
|---|---|---|---|---|
| 00:00 | 51.1 | 51.1 | 51.1 | 51.1 |
| 01:00 | 50.8 | 50.8 | 50.8 | 50.8 |
| 02:00 | 50.5 | 50.5 | 50.5 | 50.5 |
| 03:00 | 50.2 | 50.2 | 50.2 | 50.2 |
| 04:00 | 49.8 | 49.8 | 49.8 | 49.8 |
| 05:00 | 49.5 | 49.5 | 49.5 | 49.5 |
| 06:00 | 49.2 | 49.2 | 49.2 | 49.2 |
| 07:00 | 46.8 | 46.8 | 46.8 | 46.8 |
| 08:00 | 36.3 | 36.3 | 36.3 | 36.3 |
| 09:00 | 34.9 | 34.9 | 34.9 | 34.9 |
| 10:00 | 35.6 | 35.7 | 35.7 | 35.7 |
| 11:00 | 36.1 | 36.1 | 36.1 | 36.1 |
| 12:00 | 33.1 | 32.9 | 32.9 | 32.9 |
| 13:00 | 36.6 | 33.1 | 34.3 | 34.3 |
- Winter: min 31.4 °C, 285 min unter 40 °C
- Winter + 40 °C: min 32.1 °C, 285 min unter 40 °C
- Sommer: min 32.1 °C, 285 min unter 40 °C
- gemessen: min 32.4 °C, 364 min unter 40 °C

## 2026-10-12 – Batterie in der Kaskade: Sommer-/Winterprofil, Grundlast-Entladen, batterieneutraler Überschuss (Dev, nicht deployt)

`zendure.control_enabled` bleibt aus; alles Neue wirkt erst mit aktiver Batterie. Entwickelt im Dev-Verzeichnis (dry_run, Kaskade aus).

**1. Profile (neu `cascade_plan.py`):** Der Sommermodus-Schalter wählt das Profil (kein zweiter Schalter). Reihenfolge je Profil in `cascade_settings` (`profile_summer` / `profile_winter`, JSON); fehlt sie, wird sie aus der bisherigen Geräteliste gebaut (Heizstab-Eintrag wird zum Block, eBike/Wallbox behalten ihre Position, neue Geräte hinten):
- Winter (Öl heizt): `Batterie → Wasserspeicher bis Max`
- Sommer (Öl aus): `Wasserspeicher bis Komfort-Min → Batterie → Wasserspeicher bis Max`

**2. Heizstab als zwei Stufen:** `heizstab_min` (aktiv solange Temp < `heater.comfort_min_temp`, Default = `summer_mode.target_temp` = 52 °C, neue Einstellung „Komfort-Mindesttemperatur“ unter Heizstab & Wasserspeicher) und `heizstab_max` (Temp < `storage_max_temp`). Ein Heizstab, **ein** Budget an den Controller: Rest-Überschuss an der ersten aktiven Stufe minus Batterie-Ladung dazwischen. Die Min-Stufe beansprucht nur ihre ganzen Phasen (`floor((Rest + grid_assist) / 1500 W)`), der Rest geht an die Batterie – kleiner Überschuss unter der Startschwelle landet so in der Batterie, beide laden gleichzeitig, sobald genug da ist. Phasenlogik des Controllers unverändert.

**3. Grundlast-Entladen (`zendure_control.plan`):** `Entladen = min(Netzbezug, Grundlast, max_discharge)`, Grundlast = Haus − Heizstab − Wallbox (Haus = PV + Netz + Entladung − Ladung, PV inkl. Pergola; die Kaskade liest dafür PV und go-e-Leistung mit, Solax-Lesewerte sind prozessweit gecacht). Kein Entladen in den Heizstab (grid_assist, Sommermodus-Netzheizung), nicht ins Auto (Wallbox lädt → Sperre wie in Phase 2), nicht im selben Zyklus wie Laden; Grundlast unbekannt (PV nicht lesbar) → kein Entladen.

**4. Überschuss batterieneutral:** `Readings.battery_net_w` (+ lädt / − entlädt); `true_surplus_w` und `surplus_without_heater_w` rechnen Ladung zum Überschuss, Entladung nicht. Kaskade war bereits korrekt (Phase 2). Neu `Readings.house_consumption_w`. Hausverbrauch: Energiefluss-Knoten „Haus“ (app.js), `/api/solax` (`consumption_w`), Stundenlog (`pv_logger`), Tagesbilanz (`meter.compute_meter_today`: + entladen − geladen kWh); `/api/cascade/status` Überschuss. Lastspitzen (`house_peak_day`, Hauptzähler) bleiben bewusst Hausanschluss-Leistung am Zähler. Nicht angefasst: `pvlog.py` (Legacy-Cron, kennt weder Pergola noch Batterie).

**5. UI:** Prioritäten-Seite mit Tabs `☀ Sommer | ❄ Winter` (aktives Profil markiert, beide per Drag&Drop sortierbar, Einträge „Wasserspeicher (bis 52 °C)“ / „(bis 63 °C)“, übersprungene Stufe ausgegraut), Kaskade-Card mit „· Sommerprofil/Winterprofil“, Batterie-Eintrag nur bei `zendure.enabled` (+ IP). API: `PATCH /api/cascade/reorder` mit `profile`, `GET /api/cascade/status?profile=`; Antwort enthält `profile`, `active_profile`, `comfort_min_temp`. `app.js?v=20261012g`, `styles.css?v=64`.

**Tests:** 80 (vorher 48): Verteilung (alle Fälle der Vorgabe), Entladen, batterieneutraler Überschuss, Profil-Normalisierung, `run_cascade` mit Fake-Geräten je Profil. Außerdem einmal read-only gegen die echten Geräte (Kopie der Prod-DB, `_dispatch` aus).

**Simulation** (Minutenlogs 07.–12.10., 12.10. nur bis 13:29; fiktive Batterie 2,4 kWh, Start-SoC 50 %, ideal 100 % Wirkungsgrad, Wallbox/eBike wie gemessen; Speichertemperatur = Messung + zusätzliche Heizstab-Energie / 0,375 kWh/K (aus den Daten geschätzt); „Sim ohne Bat.“ = dieselbe Simulation ohne Batterie als Referenz). Skript: `scratchpad/sim/sim.py`.

| Tag | Bat. geladen W/S | Bat. entladen W/S | Heizstab gemessen | Heizstab Sim ohne Bat. | Heizstab Winter | Heizstab Sommer |
|---|---|---|---|---|---|---|
| 2026-10-07 | 2.20 / 2.22 | 3.16 / 3.18 | 6.3 | 6.3 | 6.2 | 6.2 |
| 2026-10-08 | 2.76 / 1.74 | 2.76 / 1.74 | 0.9 | 1.1 | 0.0 | 1.1 |
| 2026-10-09 | 2.62 / 2.49 | 2.62 / 2.49 | 7.0 | 6.8 | 7.6 | 6.8 |
| 2026-10-10 | 2.35 / 1.41 | 2.35 / 1.41 | 2.0 | 2.5 | 0.9 | 1.8 |
| 2026-10-11 | 4.64 / 2.90 | 4.64 / 2.90 | 6.0 | 7.5 | 3.3 | 5.2 |
| 2026-10-12 | 2.16 / 0.74 | 0.00 / 0.03 | 2.5 | 2.2 | 0.2 | 1.6 |
| Σ | 16.7 / 11.5 | 15.5 / 11.8 | 24.6 | 26.4 | 18.2 | 22.7 |

| Tag | Einspeisung gemessen | Sim ohne Bat. | Winter | Sommer |
|---|---|---|---|---|
| 2026-10-07 | 2.4 | 2.3 | 1.2 | 1.3 |
| 2026-10-08 | 2.1 | 1.9 | 1.0 | 0.9 |
| 2026-10-09 | 11.2 | 11.3 | 8.6 | 9.5 |
| 2026-10-10 | 0.9 | 1.3 | 0.9 | 0.9 |
| 2026-10-11 | 1.9 | 1.0 | 1.8 | 1.8 |
| 2026-10-12 | 1.4 | 1.2 | 0.7 | 0.9 |
| Σ | 19.9 | 19.0 | 14.2 | 15.4 |

| Tag | Netzbezug gemessen | Sim ohne Bat. | Winter | Sommer |
|---|---|---|---|---|
| 2026-10-07 | 19.3 | 19.2 | 17.0 | 17.1 |
| 2026-10-08 | 23.8 | 23.8 | 21.8 | 22.8 |
| 2026-10-09 | 29.7 | 29.6 | 27.7 | 27.8 |
| 2026-10-10 | 29.3 | 30.1 | 28.1 | 29.0 |
| 2026-10-11 | 56.2 | 56.8 | 53.5 | 55.4 |
| 2026-10-12 | 41.8 | 41.3 | 40.9 | 41.2 |
| Σ | 200.0 | 200.8 | 189.1 | 193.3 |

| Phasenwechsel | gesamt | Stunden mit Heizstab | je Stunde |
|---|---|---|---|
| tatsächlich (Schaltbefehle im Log) | 178 | 19 | 9.4 |
| Sim ohne Batterie | 96 | 21 | 4.6 |
| Sim Winter | 74 | 20 | 3.7 |
| Sim Sommer | 98 | 21 | 4.7 |

## 2026-10-12 – Umzug: Produktion läuft aus `~/pvcontroller-prod`, `~/pvcontroller` ist Dev

- Cutover 14:08–14:10: Clone von GitHub (`32aec2c`), Venv, 48 Tests grün; DB per Python-`sqlite3.backup` kopiert (`integrity_check` ok, `cascade_log` 686.772 / `HeizstabPhaseLog` 12.943 Zeilen identisch), `config.json`/`temp_history.json` kopiert. Cron `main.py` zeigt auf Prod (`flock -s /tmp/pvc-code.lock`), alte Zeile als `#CUTOVER#` auskommentiert; Web-Dienst (Unit aus `deploy/`) läuft aus Prod, sudoers-Eintrag für den Neustart angelegt. Erster Prod-Lauf 14:10:03 ohne Fehler, `deploy.sh` meldet „Bereits aktuell".
- Auch `pvlog.py --mode hourly/daily` (Cron) auf Prod umgestellt (liefen sonst weiter in die Dev-DB). Crontab-Sicherungen: `~/backup/crontab/crontab.pre-cutover`, `crontab.pre-pvlog-move`.
- `~/pvcontroller` per `deploy/make-dev.sh` zur Dev-Umgebung: `dry_run=true`, Log `dev.log`, `cascade_enabled=0`, Zendure aus; Dev-Web über `deploy/dev-web.sh` (Port 8001).
- Hinweis: Das `sqlite3`-CLI ist auf dem Pi nicht installiert (README: Python-Variante statt CLI; `make-dev.sh` hat bereits einen Python-Fallback).
- Ablauf ab jetzt: Dev → Commit → Push (nach Freigabe) → `deploy/deploy.sh` in `~/pvcontroller-prod`.

## 2026-10-12 – Deploy-Werkzeuge (Produktion ↔ Entwicklung trennen)

Neu in `deploy/`: `deploy.sh` (fetch → Wegwerf-Worktree mit Unit-Tests und Prüfung der produktiven `config.json` gegen den neuen Code → Umschalten unter exklusivem Lock → Neustart über sudoers → Smoke-Test mit automatischem Rollback), `rollback.sh`, `smoke_check.py`, `sudoers.pvcontroller` (nur `systemctl restart pvcontroller-web`), `cron.example` (Shared-Lock), `pvcontroller-web.service` (Produktion `~/pvcontroller-prod`), `make-dev.sh`/`dev-web.sh` (Dry-Run, Zendure aus, Kaskade aus, Port 8001), `README.md` mit Umzugsablauf. `.gitignore`: `deploy.log`, `.deploy_history`.
Getestet in einem Wegwerf-Aufbau (Fake-Origin, Fake-Health-Server, echter `main.py`-Cron mit Dry-Run und unerreichbaren Geräten): Deploy ok, roter Test → Abbruch ohne Änderung, Laufzeitfehler bei grünen Tests → automatischer Rollback, `rollback.sh`, ungültige `config.json` → Abbruch. Der produktive Umzug steht noch aus.
**Auffälligkeit:** `/home/openhabian/backup/backup.sh` sichert `~/pvcontroller` (Config, Datenbank) nicht.

## 2026-10-12 – Defaults angepasst, PH1 wieder aktiv

- `heater.phase_min_on_s` Default **180 → 420 s** (`config.py`, `heater_phases.HeaterParams`, `config.example.json`, Settings-Fallback); `budget_smooth_min` 10, `budget_smooth_down_min` 2, `phase_min_off_s` 120 und `phase_down_hyst` 0,15 bleiben. Simulation 09.10. 11–15 Uhr mit diesen Werten: Ø 6,0 Wechsel/h (max. Stunde 8), Netzanteil 0,72 kWh. `app.js?v=20261012f`.
- In der produktiven `config.json` wurde `heater.phase1_disabled` auf **false** zurückgesetzt (User prüft PH1 am Heizstab-3EM; Backup `config.backup-20261012-*.json`).

## 2026-10-12 – Asymmetrische Budget-Glättung, Phasen stilllegen, Regeln für Git/Produktion

**1. Asymmetrische Glättung:** Zuschalten nutzt `heater.budget_smooth_min` (Default 10 min Mittel), Abschalten/Halten das kürzere `heater.budget_smooth_down_min` (Default 2 min). Kaskade speichert beide Mittel (`cascade_devices.budget_w`/`budget_down_w`), `heater_phases.target_phases(budget, n_cur, p, budget_down_w)`; Controller-Eigenberechnung (ohne Kaskaden-Budget) ebenso (`db.smooth_heater_budgets`).
Simulation 09.10. 11–15 Uhr (Netzanteil = Σ max(0, Heizstab_sim − max(Budget, 0)); real 0,68 kWh):

| Variante | Wechsel/h Ø (max Stunde) | Netzanteil |
|---|---|---|
| symmetrisch 10/10 (vorher) | 3,0 (5) | 1,07 kWh |
| **10 / 2 (neuer Default)** | **8,0 (12)** | **0,59 kWh** |
| 10 / 3 | 7,0 (12) | 0,76 kWh |
| 10 / 2, `phase_min_on_s` 420 | 6,0 (8) | 0,72 kWh |
| 10 / 2, `phase_min_on_s` 420, `phase_min_off_s` 300, `phase_down_hyst` 0,5 | 5,0 (8) | 0,71 kWh |

→ Ziel „Netzanteil ≤ 0,8 kWh" erfüllt, „≤ 6 Wechsel/h" mit den Defaults (10/2, 180 s/120 s) **nicht** (Ø 8,0/h, schlechteste Stunde 12). In einem Raster aus 162 Varianten erfüllen 107 beide Ziele im Stundenmittel, keine hält 6 in der schlechtesten Stunde (kleinstes Maximum 7–8). Defaults bleiben wie vorgegeben, Entscheidung offen.

**2. Phasen stilllegen:** `heater.phase1_disabled` / `phase2_disabled` / `phase3_disabled` (Settings → Heizstab). Eine deaktivierte Phase wird nicht gelesen, nicht geschaltet, zählt weder als laufend noch im Budget (Ziel-Phasen ≤ Zahl aktiver Phasen; Kaskade-Override „EIN" = aktive Phasen × `phase_power_w`), erzeugt keinen Fehler und kein Banner (alter Fehlerzustand wird gelöscht). Dashboard: Phasen-Karte und Zeitstrahl grau mit „deaktiviert", Energiefluss-Punkt gestrichelt. In der produktiven `config.json` ist **PH1 deaktiviert** (Backup `config.backup-20261012-*.json`). `/api/status` → `heater.phases_disabled`.
**Achtung:** Ein deaktivierter Shelly wird nicht mehr geschaltet. Läuft die Phase physisch noch (am 12.10. zog der Heizstab ≈ 4,9 kW bei bekannten PH2+PH3), bleibt sie unkontrolliert an und die Max-Temperatur kann sie nicht abschalten.

**3. Regeln:** `CLAUDE.md`: Pushen nur nach ausdrücklicher Freigabe; Cron und Web-Dienst laufen aus dem Arbeitsverzeichnis, Änderungen sind sofort produktiv (Entwicklung in einer Kopie, dann gesammelt übernehmen). Konzept zur Trennung Produktion/Entwicklung: noch nicht umgesetzt (Vorschlag liegt dem User vor).

**Tests:** 48 (`python -m unittest discover -s tests -t .`), neu u. a. asymmetrische Mittel und stillgelegte Phasen. Cron läuft seit 13:49 mit diesem Stand; Web-Dienst braucht einen Neustart.

## 2026-10-12 – Heizstab: Fail-safe pro Phase, Kaskade = Budget, Controller = Phasen

**Anlass (User):** Grundlage `docs/steuerung-ist-analyse.md`: Ein ausgefallener Phasen-Shelly legte die ganze Steuerung lahm (inkl. Max-Temperatur), der Heizstab wurde von zwei Ebenen ohne gemeinsame Mindestzeiten geschaltet (bis 39 Wechsel/h).

**A – Fail-safe granular**
- Lesefehler einer Phase → Phase `unknown` (wird nicht geschaltet, zählt im Budget als laufend); die anderen Phasen arbeiten normal weiter. `Readings.unreachable_phases`, Fehlerliste enthält weiter `phN_state`.
- **Globaler Fail-safe nur noch** bei nicht lesbarem Hauptzähler oder Wechselrichter (`Readings.critical_errors`): dann keine überschussbasierten Schaltbefehle und kein Sommermodus-Start.
- **Maximaltemperatur hat absoluten Vorrang** und gilt immer (auch im globalen Fail-safe): ≥ Max → alle erreichbaren Phasen AUS; Speichertemperatur nicht lesbar → alle erreichbaren Phasen AUS („Speichertemperatur unbekannt – Heizstab aus"). Gilt ebenso für Sommermodus-Stopp und manuelles AUS – auch innerhalb der Mindestlaufzeit.
- **Paralleles Lesen** (`ThreadPoolExecutor`) mit 1,5 s Lese-Timeout für Shelly-Reads (`SHELLY_READ_TIMEOUT_S`; Schalten behält `runtime.request_timeout_seconds`); gleiches Lese-Timeout für die Zähler der Kaskade. Lauf mit totem PH1: ≈ 2 s statt 7,6 s.
- **Dauerhaft tote Geräte:** Tabelle `device_health`; ab 10 min ohne Antwort ein einmaliger Log-Eintrag („Phase 1 (192.168.2.53) seit HH:MM Uhr nicht erreichbar"), ein weiterer beim Wiederkommen; `/api/status` → `device_alerts`, Dashboard-Banner. Die Einzel-Lesefehler der Phasen werden nicht mehr pro Lauf geloggt.

**B – Eine Zuständigkeit**
- **Kaskade** (`cascade_service._handle_heater`): legt nur das Budget fest („Heizstab darf X W haben" = Rest-Überschuss an seiner Priorität, brutto) und speichert es (`cascade_devices.budget_*`); vom Restbudget der nachfolgenden Geräte wird die **gemessene** Heizstab-Leistung abgezogen. Manuelle Übersteuerung: AUS = Budget 0 (sofort), EIN = volle Leistung. Die Kaskaden-Mindestzeiten gelten für den Heizstab nicht mehr.
- **Controller** (neu: `heater_phases.decide_phases`, reine Funktion): Ziel-Phasen = `floor((Budget + grid_assist_w) / phase_power_w)`, 0–3; erst ab Schwelle starten, Abschalten erst unter Schwelle − `phase_down_hyst` × Phase (15 %); Mindestlaufzeit 180 s / Mindestpause 120 s je Phase (aus `HeizstabPhaseLog`), höchstens 1 Phase pro Lauf zuschalten; Hysteresebereich 62–64 °C wie bisher (keine neue Phase ohne laufenden Zyklus). Fehlt ein aktuelles Kaskaden-Budget (Kaskade aus/veraltet > 3 min), rechnet der Controller selbst (`-true_surplus_w`, gleich geglättet).
- `grid_assist_w` gilt nur noch hier; die Kaskade zeigt als Startschwelle `phase_power_w − grid_assist_w` (statt fix 1 500 W). Die Schwellen `min_pv_power_ph1..3` sind **ohne Wirkung** (Phasenschwelle = n × `phase_power_w`); die UI-Felder sind als „veraltet" markiert.
- **Zusätzlich (Abweichung):** Budget-Glättung `heater.budget_smooth_min` (Default 10): Mittel der letzten 10 Minuten. Ohne sie erreichte die neue Logik im Nachspielen die Zielmarke nicht (Wolken: Budget schwankt um mehrere kW pro Minute).
- Neue Config-Felder `heater.phase_min_on_s`, `phase_min_off_s`, `phase_down_hyst`, `budget_smooth_min` (Settings → Heizstab & Wasserspeicher, `config.example.json`).

**Nachgespielt (Minutenlog, Phasenwechsel/h, alt → neu mit 10-min-Glättung):** 07.10. max 10 → 5; 09.10. max 39 → 5 (88 → 12 Wechsel/Tag); 10.10. max 24 → 7; 11.10. max 11 → 4. Ohne Glättung (Mindestzeiten allein): 09.10. max 20/h. Die Simulation kennt keine Rückkopplung des Heizstabs auf die Temperatur; Netzanteil des Heizstabs (09.10. 11–15 Uhr): real 0,7 kWh, simuliert 1,1 kWh (längere Mittelung ⇒ etwas später abgeschaltet).

**Tests:** `tests/test_heater_phases.py` (18), `tests/test_controller_phases.py` (10, Fake-Geräte, eigene Test-DB), `tests/test_zendure_control.py` (12): `python -m unittest discover -s tests -t .`.

**Hinweis:** Der **Cron-Lauf** (`main.py`) nutzt den Code sofort, nur der Web-Dienst braucht einen Neustart (Dashboard-Banner, neue Settings-Felder, `device_alerts`). Zendure-Code und Wallbox-Logik unverändert.

## 2026-10-12 – Ist-Analyse der Steuerungslogik (nur Dokumentation)

Neuer Bericht `docs/steuerung-ist-analyse.md`: wie Heizstab, Wallbox und Kaskade heute tatsächlich entscheiden (mit Code-Referenzen), Auswertung von 5,75 Tagen Controller-Log und 7 Tagen DB-Daten sowie Bewertung für die Batterie-Integration mit offenen Entscheidungen. **Kein Code geändert.** Wichtigste Befunde: Phase-1-Shelly offline → Fail-safe an 79 % der Minuten am 11.10.; Heizstab wird von zwei Ebenen ohne gemeinsame Mindestzeiten gesteuert (Flackern an 5 von 8 Tagen); `grid_assist_w` wirkt für Phase 1 nicht; Kaskaden-Gerät „Wallbox" und die Wallbox-Temperatur-Einstellungen haben keine Wirkung.

## 2026-10-12 – Heizstab-Punkte: Schätzung bei nicht lesbarem Phasenstatus

**Anlass (User):** Der Heizstab lief (1,59 kW), aber alle drei Punkte im Energiefluss waren leer.

**Ursache:** Der Shelly von Phase 1 (192.168.2.53) ist nicht erreichbar → `ph1_on = null` („Lesefehler bei: ph1_state", Fail-safe). Die Punkte zeigten nur Phasen mit Status `true`.

**Fix:** In `renderEnergyFlow` (`web/app.js`) wird bei unbekanntem Phasenstatus (`null`) und Heizstab-Leistung > 50 W die Zahl der laufenden Phasen aus der Leistung geschätzt (≈ 1,5 kW je Phase, mindestens 1, höchstens 3) und die ersten unbekannten Phasen gefüllt dargestellt. Bekannte Zustände (`true`/`false`) gelten unverändert. `app.js?v=20261012c`.

**Hinweis:** Das behebt nur die Anzeige. Solange der Phase-1-Shelly offline ist, bleibt der Controller im Fail-safe und schaltet nichts.

## 2026-10-12 – Zendure SolarFlow: Phase 2 (Steuerung)

**Anlass (User):** Nach Phase 1 soll die Batterie gesteuert werden: Laden aus PV-Überschuss, Entladen bei Netzbezug, Wallbox-Schutz, Verlauf.

**Umsetzung:**
- **Master-Schalter** `zendure.control_enabled` (Default aus; setzt `zendure.enabled` voraus). Ohne ihn bleibt es bei Phase 1 (nur lesen). Bei `runtime.dry_run` wird nur geloggt (`[Dry-Run] … würde … schreiben`), nichts geschrieben.
- **`zendure_control.py`:** reine Planungsfunktion `plan()` (12 Unit-Tests: `python -m unittest tests.test_zendure_control`) + `ZendureControl` mit persistiertem Zustand (Tabelle `zendure_ctl`: Modus, Limit, letzter Moduswechsel, Fehlerzähler, Entscheidungstext).
  - *Laden:* nur aus dem Überschuss, der der Batterie in der Kaskade zusteht; stufenlos bis `max_charge_w`; Start ab 100 W + Hysterese des Geräts, danach weiter ab 100 W; nicht über `target_soc`; Limit wird nur bei > 50 W Änderung geschrieben.
  - *Entladen:* nur ohne Laden, mit SoC > `min_soc` und Netzbezug > 50 W; Regelung auf Bezug ≈ 0 (Ziel = aktuelle Abgabe + Bezug, gedeckelt auf `max_discharge_w`), fällt bei Einspeisung wieder zurück.
  - *Wallbox-Schutz:* lädt ein Auto (go-e `car = 2` und > 100 W, alle Lademodi), wird das Entladen sofort hart gesperrt (`outputLimit = 0`), Laden bleibt davon unberührt. Setting `block_discharge_while_wallbox_charging` (Default an). Ist der Wallbox-Status nicht lesbar, wird vorsichtshalber ebenfalls gesperrt.
  - *Wechselsperre:* ein neuer Lade-/Entlademodus startet frühestens 2 min nach dem letzten Moduswechsel (dazwischen Standby, nie „weiterlaufen lassen"). `smartMode = 1` bei jedem Write.
  - *Ausfall:* Lesefehler → letzter Befehl bleibt stehen; nach 3 Fehlern in Folge wird neutral gestellt (`inputLimit = outputLimit = 0`), sonst alle 1 min erneut versucht.
- **Kaskade:** Batterie als Gerät `zendure` (wird beim ersten Lauf mit aktiver Steuerung am Ende der Prioritätenliste angelegt, nicht löschbar, per Drag & Drop sortierbar, nur sichtbar bei aktiver Steuerung). Für den Überschuss wird die Ladeleistung der Batterie zurückgerechnet und die Entladeleistung abgezogen, damit weder Laden noch Entladen den Überschuss verfälscht. Menü: „Laden erzwingen (30 Min)" / „Entladen sperren (30 Min)".
- **UI:** Settings (Checkbox „Steuerung aktiv"), Batterie-Card mit Regelungs-Entscheidung, „Aktuelle Entscheidung" um den Batterie-Text ergänzt („Batterie lädt mit 1.20 kW aus PV-Überschuss", „Batterie deckt Hausverbrauch (420 W), SoC 73 %", „Batterie leer …", „Batterie-Entladung gesperrt: Wallbox lädt"), Verlauf → Tag: Card „Batterie" (SoC-Linie + Laden/Entladen als Balken), Verlauf → Monat: geladen/entladen pro Tag + Zyklen-Schätzung (Σ geladen / Kapazität). API: `GET /api/history/zendure/month/{monat}`. Cache-Busting `app.js?v=20261012b`, `styles.css?v=61`; `config.example.json` um den Block `zendure` ergänzt.

**Test:** In einer Projektkopie mit eigener Datenbank gegen eine Fake-Welt (Zendure mit Schreibzugriff, Hauptzähler, go-e) durchgespielt: Entladen am Abend, Wallbox-Sperre und Freigabe, Laden aus Überschuss, Überschuss bricht ein, SoC 100 %, Mindest-SoC, Override ein/aus, Ausfall (3 Fehler), Dry-Run. Dabei gefunden und behoben: Der Netzbezug musste um die eigene Ladeleistung bereinigt werden, sonst hat die Batterie nach dem Moduswechsel Laden → Entladen zunächst überschossen. **Nicht gegen das echte Gerät getestet.**

**Abweichungen / Hinweise:**
- Die Setting `zendure_grid_charge_enabled` (Laden aus dem Netz) ist nicht umgesetzt: geladen wird ausschließlich aus Überschuss (oder per „Laden erzwingen").
- Die Regelung läuft im Kaskaden-Durchlauf (Cron, 1×/min); bei deaktivierter Kaskade (`cascade_enabled = false`) wird die Batterie nicht geregelt.
- Vor dem ersten Einschalten der Steuerung am echten Gerät: `acMode`-Mapping (1 = Laden, 2 = Entladen) und die Skalen von `socSet`/`minSoc` prüfen (Zendure liefert diese teils in 0,1 %); `socSet`/`minSoc` werden noch nicht geschrieben.
- Dienst neu starten.

## 2026-10-11 – Wasserspeicher „Letzte Stunde": Y-Achse mit mindestens 4 °C Spanne

**Anlass (User):** Bei konstanter Temperatur standen auf der Y-Achse mehrmals dieselben Werte.

**Umsetzung:** In `renderStorageChart` (`web/app.js`) gilt für das Stundenfenster `min = floor(minTemp) − 2`, `max = ceil(maxTemp) + 2` (Spanne ≥ 4 °C). Das Tageschart behält die bisherige Skala. Cache-Busting `app.js?v=20261011i`.

## 2026-10-11 – Zendure SolarFlow 2400 AC: Phase 1 (nur lesen, Anzeige)

**Anlass (User):** Ein Zendure SolarFlow 2400 AC (Batteriespeicher) soll lokal über die zenSDK-HTTP-API eingebunden werden. Phase 1 = Client + Anzeige, Steuerung folgt in Phase 2 nach dem Test am echten Gerät.

**Umsetzung:**
- `clients/zendure_client.py`: `get_status()` (GET `/properties/report`, Timeout 3 s, 20 s Backoff nach Fehler, `None` = nicht erreichbar, kein Fail-safe), `test_connection()`, `set_mode()` (mit `smartMode=1`, vorbereitet, noch ungenutzt). Laden = `outputPackPower`, Entladen = `packInputPower`; der Zustand wird aus den Leistungen abgeleitet, nicht aus `packState`. Temperatur: `hyperTmp` wird als Kelvin×10 gedeutet (> 1000 → (v − 2731)/10). `acMode`-Konstanten (1 = Laden, 2 = Entladen) sind unverifiziert.
- Config: Block `zendure` (`enabled`, `ip`, `sn`, `name`, `capacity_wh`, `max_charge_w`, `max_discharge_w`, `min_soc`, `target_soc`, `control_enabled`, `block_discharge_while_wallbox_charging`) statt flacher `zendure_*`-Schlüssel; Standard aus. `control_enabled=true` wird noch abgelehnt (Phase 2). Settings → „Batteriespeicher (Zendure SolarFlow)" mit „Verbindung testen" (SoC, Leistung, SN, Produkt, Version, `packState`).
- Daten: Tabelle `zendure_min` (Minutenwerte SoC/Laden/Entladen); `pv_hourly_log` + `zendure_soc/charge_w/discharge_w`; `pv_daily_log` + `zendure_charged_kwh/discharged_kwh`. Tagesenergie = Minutenmittel × 1 min aufsummiert. API: `/api/status` → `zendure`, `GET /api/zendure/history`, `POST /api/zendure/test`.
- Dashboard: Card „Batterie" unter „Wasserspeicher" (SoC groß, Balken rot→grün mit Mindest-SoC-Marke, Status-Badge, geladen/entladen heute, Temperatur, SoC-Verlauf) und Energiefluss-Knoten „BATTERIE" (zwischen Wasserspeicher und Wallbox; Laden Haus → Batterie, Entladen Batterie → Haus, Standby gedimmt). Beide nur sichtbar bei `zendure.enabled`.
- Zusatzregel (Phase 2) schon als Setting angelegt: `block_discharge_while_wallbox_charging` (Default an) – Entladen sperren, solange die Wallbox lädt (alle Lademodi).

**Abweichungen:** SoC-Chart als eigenes SVG (keine Chart.js, wie die übrigen Charts); Knoten zeigt „73 %" mit Untertitel „lädt 1,20 kW" statt „73 % · 420 W".

**Test:** Gegen einen Fake-zenSDK-Server (laden 1200 W / entladen 420 W / Standby) in einer Projektkopie auf Port 8001 im Headless-Browser geprüft. Dabei aufgefallen und behoben: die Card-Funktionen fehlten wegen der Umbenennung „Speicher → Wasserspeicher" an der Einfügemarke zunächst in `app.js`. **Nicht gegen das echte Gerät getestet.** Cache-Busting `app.js?v=20261011h`, `styles.css?v=60`.

**Hinweis:** Dienst neu starten (neue Config-Felder, API, Tabellen).

## 2026-10-11 – „Speicher" heißt überall „Wasserspeicher"

**Anlass (User):** Alles, was „Speicher" heißt, soll in „Wasserspeicher" umbenannt werden (Abgrenzung zu einem Batteriespeicher).

**Umsetzung:** Nur sichtbare Texte: Dashboard und Settings (`web/index.html`, `web/app.js`: Karten-Titel „Wasserspeicher · heute/· 7 Tage/· letzte Stunde", Energiefluss-Knoten „WASSERSPEICHER" – bei langen Labels etwas kleinere Schrift –, Status „Wasserspeicher lädt/bereit", Settings-Labels), die Entscheidungstexte (`web.py`: „Wasserspeicher priorisiert/voll …"), die Sommermodus-Begründung und das Log (`controller.py`), die Kaskaden-Voreinstellung „Wasserspeicher (Heizstab)" (`db.py`; der vorhandene Eintrag in `cascade_devices` wurde umbenannt) sowie `README.md`. Nicht geändert: Config-Schlüssel (`storage_*`, `shelly.storage_url`), API-Pfade (`/api/speicher/history`), Tabellen und Code-Bezeichner sowie ältere Changelog-Einträge. `app.js?v=20261011f`.

**Hinweis:** Dienst neu starten (Texte in `web.py`/`controller.py`).

## 2026-10-11 – PV-Balken: eigene Skala je String (STR1 11,5 kW, STR2 6,5 kW)

**Anlass (User):** Maximalwerte der Strings sind STR1 11500 W und STR2 6500 W; die Balken sollen darauf skaliert werden (statt beide auf 10 kW).

**Umsetzung:** `SolaxConfig` hat neu `str1_max_w` (Default 11500) und `str2_max_w` (Default 6500), in `config.json`/`config.example.json` gesetzt. `/api/solax` liefert `str1_max_w`/`str2_max_w` (statt `str_max_w`), `renderSolax` skaliert und beschriftet damit („/ 11.5 kW", „/ 6.5 kW"). `solax.max_w` entfällt für die Strings, `solax2.max_w` (Pergola, 1 kW) bleibt. `app.js?v=20261011e`.

**Hinweis:** Dienst neu starten (Config-Feld, API).

## 2026-10-11 – Lastspitzen jetzt vom Shelly-Hauptzähler, Verlauf in Minutenwerten

**Anlass (User):** Die Spitzen sollen vom Shelly-Hauptzähler kommen (nicht aus dem Solax-Verbrauch), und der Verlauf soll wie im openHAB-Chart aussehen (Tag mit Spitzen, Zoom auf die letzte Stunde, Bezug positiv, Einspeisung negativ).

**Umsetzung:**
- `db.py`: neue Tabelle `main_meter_min` (Minutenwerte: Mittel/Max/Min/Anzahl der Hauptzähler-Leistung, 35 Tage Aufbewahrung). `record_main_meter()` rechnet jede Messung ein und schreibt das Tageshoch (max. Netzbezug) in `house_peak_day` fort. Aufgerufen aus `Controller._read_all` – also vom Web-Poller (alle 10 s bei offenem Dashboard) und vom Cron (jede Minute). Der bisherige Solax-Verbrauch speist das Tageshoch nicht mehr (`pv_logger`, `/api/solax`).
- `web.py`: `/api/history/house/day/{datum}` liefert die Minutenwerte (`points` = [Minute, Mittel, Max, Min]) plus Spitze Bezug / max. Einspeisung (nur ab 50 W) und Ø; die Monatsübersicht liest weiter `house_peak_day`.
- Web-UI: Card „Hauptzähler" im Verlauf → Tag: 0–24 h mit Nulllinie (Bezug hell, Einspeisung grün unterlegt), roter Punkt = Spitze Bezug, grüner Punkt = max. Einspeisung, darunter „Letzte Stunde" (nur für heute). Cache-Busting `app.js?v=20261011b`.
- Dashboard: dieselbe Card „Hauptzähler" (Tag + letzte Stunde) steht auch auf dem Dashboard ganz unten (volle Breite unter beiden Spalten) und wird mit jedem Refresh aktualisiert (`loadHouseDash`, `app.js?v=20261011d`, `styles.css?v=59`).
- Die zuvor angelegten Test-Spitzen in `house_peak_day` (aus dem Solax-Verbrauch) wurden gelöscht.

**Hinweise:**
- Das Tageshoch ist der **Netzbezug** am Hauptzähler (relevant für den Hausanschluss), nicht der Hausverbrauch: Was die PV direkt deckt, zählt nicht mit.
- Kurze Spitzen werden nur erfasst, wenn eine Messung in diese Zeit fällt (Cron: 1/min, Dashboard offen: alle 10 s). Aufzeichnung beginnt mit dem Neustart; ältere Tage zeigen „Noch keine Messwerte".
- Dienst neu starten.

## 2026-10-11 – Hausverbrauch: Lastspitzen (Tageshoch, Tagesverlauf, Monatsübersicht)

**Anlass (User):** Sehen, wie hoch die Spitzenlast im Haus ist – heute und über die letzten Wochen (Hausanschluss-Grenze).

**Umsetzung:**
- **Persistenz:** neue Tabelle `house_peak_day` (`date`, `peak_w`, `peak_time`), fortgeschrieben bei jedem Cron-Lauf (`pv_logger._log_hourly`, minütlich) und bei jedem `/api/solax`-Abruf (`db.update_house_peak`). Hausverbrauch = Solax-Verbrauch + Pergola-Leistung (wie bisher im Stunden-Log). Nach Mitternacht beginnt automatisch ein neuer Tag.
- **API:** `/api/solax` liefert `house_peak_w`, `house_peak_time`, `house_max_load_kw`; `/api/status` liefert `house_peak_watts`/`house_peak_time`; neu `GET /api/history/house/day/{YYYY-MM-DD}` (stündliche Werte, Ø, Tageshoch) und `GET /api/history/house/month/{YYYY-MM}` (Spitze pro Tag, höchste Spitze, Ø Tagesspitze, Warnmarke).
- **Config:** neuer Schlüssel `house_max_load_kw` (Default 11), einstellbar unter Settings → „Hausanschluss" (`PUT /api/config` Sektion `house.max_load_kw`).
- **Hausverbrauch-Card:** Zeile „Tageshoch: X kW um HH:MM"; Wert grau, ab 8 kW orange, ab `house_max_load_kw` rot.
- **Verlauf → Tag:** neue Card „Hausverbrauch" (0–24 h, weiße Linie + Fläche, roter Max-Punkt mit Tooltip, „Ø … · Max … um …").
- **Verlauf → Monat:** neue Card „Lastspitzen pro Tag" (Balken grau < 8 kW, gold ab 8 kW, rot ab Warnmarke, gestrichelte Warnmarke, „Höchste Spitze" und „Ø Tagesspitze"). Cache-Busting `app.js?v=20261011a`, `styles.css?v=58`.

**Abweichungen / Hinweise:**
- Die Spitzen liegen in einer eigenen Tabelle statt in `daily_log` (`pv_daily_log`): dessen `INSERT OR REPLACE` würde die Spalten jede Minute überschreiben. Die Stundenwerte stammen aus `pv_hourly_log.consumption_w`.
- Das Tageshoch kommt vom Server; ein zusätzliches Tracking im Browser entfällt (überlebt so Reload und Neustart). Spitzen gibt es erst ab dem Neustart – frühere Tage zeigen im Tag-Chart das Maximum der Stundenwerte, im Monat fehlen sie.
- Die Monats-Navigation ist die bestehende des Sub-Tabs (nicht in der Card). Der Max-Punkt im Tag-Chart liegt auf der echten Spitzenminute und kann daher über der Stundenlinie liegen.
- Dienst neu starten (neue Endpunkte, neue Tabelle).

## 2026-10-10 – HEUTE-Card: Tagesertrag groß, Aufteilung PV-Anlage / Balkonkraftwerk

**Anlass (User):** „10.8 kWh erzeugt" stand klein im Header; mit zwei Anlagen fehlte die Aufteilung.

**Umsetzung:** `renderToday` (`web/app.js`): Header nur noch „HEUTE"; darunter der Gesamtertrag groß und zentriert mit „ERZEUGT". Bei aktivem zweiten Wechselrichter zwei Zeilen „☀ PV-Anlage" (gold) und „☀ Balkonkraftwerk" (grün) mit kWh, Balken und Prozentanteil am Gesamtertrag; ohne zweiten WR nur der Gesamtwert. Der Rest ab „Wohin ging der PV-Strom?" ist unverändert. Cache-Busting `app.js?v=20261010n`.

**Hinweise:** Die Werte kommen aus `/api/solax` (`main_yield_today_kwh`, `pergola_yield_today_kwh`, Summe `yield_today_kwh`), das die HEUTE-Card ohnehin nutzt, nicht aus `/api/status`. Die Zeilenbeschriftung ist fest „PV-Anlage"/„Balkonkraftwerk" (wie in der PV-Card), nicht der Config-Name (`Dach`/`Pergola`). Ist der Balkon-WR nicht erreichbar, zeigt seine Zeile „–".

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
