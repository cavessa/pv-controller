# Steuerung – Ist-Analyse (Wallbox, Wasserspeicher, Kaskade)

Stand 12.10.2026, vor dem Scharfschalten der Zendure-Batterie. Reine Analyse, am Code und an den Daten wurde nichts geändert.

**Datenbasis:** Controller-Log `/home/openhabian/pvueberschuss/pv-controller.log*` (Cron-Läufe jede Minute): 06.10. 19:30 – 12.10. 13:29 = 8 279 Läufe, ≈ 5,75 Tage. Dazu `cascade_log`, `HeizstabPhaseLog`, `pv_hourly_log`, `pv_daily_log` (05.–12.10.). Grenzen: Wallbox-Leistung im Basic-Modus steht nicht im Minuten-Log (nur Stundenwerte), der echte Ladestrom `amp` im Eco-Modus ist nicht beobachtbar (Log zeigt nur den Maximalwert 16 A), Datenbank-Zeitstempel sind uneinheitlich (`cascade_log` UTC, `HeizstabPhaseLog` lokal).

## Wichtigste Befunde

1. **Phase-1-Shelly (192.168.2.53) ist seit 10.10. unzuverlässig/offline.** Jeder Lesefehler setzt *alle* drei Phasen auf „unverändert" (`controller.py:853-884`). 11.10.: 1 142 von 1 439 Minuten (79 %) im Fail-safe, 12.10.: 738 von 810. Im Fail-safe wird auch die Maximaltemperatur nicht mehr erzwungen. Läufe dauern wegen der Timeouts statt 1,4 s jetzt ≈ 7,6 s (p90 12 s).
2. **Zwei Ebenen steuern den Heizstab**, die Kaskade (an/aus, nominal 1 500 W, Mindestzeiten) und der Controller (je Phase, *ohne* Mindestzeiten). Folge: Flackern, bis zu 39 Phasenwechsel in einer Stunde (09.10. 12 Uhr).
3. **`grid_assist_w=400` wirkt für Phase 1 nicht.** Controller würde ab 1 100 W starten, die Kaskade verlangt 1 500 W (nominal, Hysterese 0).
4. **Das Kaskaden-Gerät „Wallbox" hat keine Wirkung**, `is_on` wird nie gelesen. Ebenso sind `wallbox.pause_below/release_above_storage_temp` tot (UI-Felder ohne Funktion).
5. **Eco-Laden bezieht im Mittel ≈ 33 % aus dem Netz** (5,8 von 17,5 kWh).
6. **Doppelabzug für Shelly-Geräte** (eBike 180 W) im Kaskaden-Budget (`cascade_service.py:165`).

## 1 Entscheidungslogik aus dem Code

### 1.1 Ein Zyklus (Cron, jede Minute: `main.py:63` Kaskade, danach `main.py:68` Controller)

| # | Schritt | Funktion |
|---|---|---|
| 1 | Shelly-Kaskadengeräte pollen (eBike) | `CascadeService._poll_shelly_devices` |
| 2 | Brutto-Überschuss = −Hauptzähler + Festlasten (Heizstab-3EM, Wallbox nur Eco) [+ Batterie-Korrektur] | `run_cascade` `cascade_service.py:117` |
| 3 | Budget je Gerät nach Priorität verteilen; setzt nur `is_on`-Flags in der DB (Shelly zusätzlich geschaltet) | `_decide` `:272`, `_dispatch` |
| 4 | Alle Messwerte neu lesen (Solax, Pergola, Zendure, Hauptzähler, Heizstab-3EM, Temperatur, PH1-3) | `Controller._read_all` `controller.py:176` |
| 5 | go-e-Status lesen | `_read_wallbox_status` `:540` |
| 6 | Temperatur klassifizieren | `_classify_temp` `:252` |
| 7 | Lesefehler? → alle Phasen unverändert | `run` `:853` |
| 8 | Sommermodus, dann je Phase `_decide_phase`, danach Kaskaden-Sperre | `:924-949` |
| 9 | Phasen schalten (Shelly-Plugs) | `_execute` `:466` |
| 10 | Wallbox: frc=0 freigeben bzw. Auto-Reset | `_handle_wallbox` `:1000` |
| 11 | Logging, Temp-Historie, Stundensnapshot | `pv_logger.maybe_log` |

Parallel liest der Web-Poller alle 10 s (Dashboard offen) bzw. 60 s (`web.py:391-392`) im **READ_ONLY**-Modus: schaltet nie, schreibt aber Minutenwerte (Hauptzähler, Batterie). Die Kaskade läuft nur im Cron, ihre Daten sind 1–3 s älter als die des Controllers (zwei getrennte Hauptzähler-Lesungen pro Minute).

### 1.2 Wasserspeicher / Heizstab

Aktuelle Werte (`config.json`): Max 64 °C, Hysterese 2 K (Resume 62 °C), Schwellen 1 500 / 3 000 / 4 500 W abzüglich `grid_assist_w` 400 → **1 100 / 2 600 / 4 100 W**, je Phase 1 500 W (`config.py:77-82`, `effective_min_pv`).

| Aspekt | Regel |
|---|---|
| Einschalten Phase n | `surplus ≤ −Schwelle_n` **und** `pv ≥ Schwelle_n` (`controller.py:326-329`), `surplus = Hauptzähler − Heizstab − Wallbox(Eco)` (`models.py:67`) |
| Ausschalten | `surplus > −Schwelle_n` → **je Phase einzeln**; `temp ≥ 64` → alle aus |
| Hysteresebereich 62–64 °C | nie eine neue Phase, *außer* es läuft schon eine (aktiver Zyklus), Ausschalten bleibt erlaubt (`:331-369`) |
| Mindestlaufzeit/-pause | **keine** auf Controller-Ebene |
| Kaskade | Gerät `heizstab` (Prio 2, 1 500 W nominal, Hysterese 0, an ≥ 5 min, aus ≥ 2 min). Aus → Controller blockiert Einschalten und schaltet alles laufende **auf einmal** aus (`:940-949`). An → Controller entscheidet allein |
| Wer gewinnt | die restriktivere Seite: Einschalten braucht beide, Ausschalten genügt eine |
| Sommermodus (aktuell **aus**) | Prüfung *vor* den Phasen, überstimmt die Kaskaden-Sperre: unter `min_temp` alle 3 Phasen aus dem Netz, bis `target_temp`. Bei Überschuss < −200 W übernimmt die normale Logik (`:403-464`) |
| Fehler | jeder Lesefehler → alle Phasen unverändert (siehe Befund 1) |

### 1.3 Wallbox (go-e)

- **Eco (lmo=4):** Der Controller schreibt *nur* `frc=0`, wenn es nicht schon 0 ist (`_decide_wallbox` `:575-642`). Der go-e regelt Strom (1-phasig, 6–16 A ≈ 1,4–3,7 kW) selbst über seinen eigenen Hauszähler. Kein `pause`/`frc=1` mehr (im Log `frc=0` in 8 213 von 8 213 Läufen).
- **Basic (lmo=3):** `SKIPPED`, nichts wird geschrieben. Einzige Ausnahme: **Auto-Reset beim Abstecken** (`car=1`, `:619-626`): setzt `lmo=4`, `amp=eco_amps` (16), `psm=eco_phases` (1). Im Log 2× in 7 Tagen (09.10. 07:37, 12.10. 07:34).
- **Kaskade:** Wallbox-Gerät (Prio 3, 1 610 W) zieht nur die *gemessene* Eco-Leistung vom Budget ab (`cascade_service.py:479-487`), schaltet aber nichts.
- **Reihenfolge:** erst Kaskade, dann Controller, dann Wallbox. Überstimmen kann keine Seite die andere, sie berühren verschiedene Stellgrößen.

### 1.4 Kaskade

- **Überschuss:** `raw_feed_in = −Hauptzähler` (Fallback Solax-Register 34), dazu Heizstab-Ist und Wallbox-Eco-Ist = Brutto (`:117`). `allow_grid_draw=1`, `min_surplus_watts=0` (`:128-134`).
- **Budget:** laufende Geräte ziehen **gemessene** Leistung ab (Heizstab, Wallbox, Batterie), Shelly-Geräte aber `power_watts`, und ihre Last wird nicht zuvor addiert → doppelt gezählt (`:165`, `:195`).
- **DB-Werte:** eBike Prio 1 (180 W, Hyst. 100, an 5 / aus 3 min), Heizstab Prio 2 (1 500 W, Hyst. 0, 5 / 2), Wallbox Prio 3 (1 610 W, Hyst. 100, 5 / 3).

### 1.5 Hausverbrauch

Drei Definitionen: (a) PV-Card/Stundenlog: Solax-Register 47 (inverterseitig berechnet) + Pergola – enthält Heizstab und Wallbox (13:00 am 09.10.: 5 388 W bei 4 500 W Heizstab); (b) Energiefluss-Knoten „Haus": `PV + Netz − Heizstab` (`app.js:363`), also **ohne** Heizstab; (c) Tagesbilanz: `PV + Bezug − Einspeisung` (`meter.py`).

## 2 Was die Logs sagen

**Tagesbilanz (Minutenlog, volle Tage 07.–11.10.):**

| Tag | PV kWh | Einsp. | Bezug | Heizstab kWh / min / Ø Ph. | T max | Fail-safe-Min. |
|---|---|---|---|---|---|---|
| 07.10. | 22,1 | 2,4 | 19,3 | 6,3 / 116 / 2,0 | 64,4 | 2 |
| 08.10. | 9,5 | 2,1 | 23,8 | 0,9 / 26 / 1,3 | 54,9 | 6 |
| 09.10. | 25,2 | 11,2 | 29,7 | 7,0 / 110 / 2,5 | 65,1 | 2 |
| 10.10. | 12,8 | 0,9 | 29,3 | 2,0 / 41 / 1,8 | 58,1 | 436 |
| 11.10. | 17,3 | 1,9 | 56,2 | 6,0 / 115 / 1,9 | 59,1 | 1 142 |

**Schaltvorgänge (DB, 05.–12.10.):** Phasen: PH1 56 Läufe / Ø 12 min, PH2 48 / Ø 9 min, PH3 33 / Ø 8 min, kürzester Lauf **1 min**. Spitzen: 39 Wechsel (09.10. 12 Uhr), 34 (05.10. 12 Uhr). Kaskade Heizstab 72× an / 71× aus (max. 10/h), Wallbox-Flag 48/48 (wirkungslos), eBike 49/48. Mehr als 10 Phasenwechsel pro Stunde (Flackern) gab es an **5 von 8 Tagen** (05., 06., 09., 10., 11.10.; insgesamt 10 Stunden).

**Wallbox:** 1 307 min Laden: 412 min Eco, 895 min Basic. Eco-Sitzungen 17,5 kWh, davon **5,8 kWh (33 %) aus dem Netz** (07.10. 27 %, 10.10. 48 %, 11.10. 36 %); in 182 von 211 Eco-Minuten am 07.10. lag der Bezug über 200 W. Basic: 11.10. 17:30 → 12.10. 07:33 (834 min, ≈ 62 kWh nachts) und 09.10. 06:36–07:36 (Stundenwert 10,8 kW, Netzbezug 14,3 kW). Eine Pause/Freigabe durch den Controller fand nie statt.

**Heizstab bei Netzbezug:** 5,3 kWh in 5,7 Tagen (davon 4,9 bei PV > 1 kW); 11.10.: 107 min mit Ø 1,4 kW Bezug bei Ø 3,2 kW Heizstab. Zieltemperatur 64 °C erreicht am 07.10. (64,4) und 09.10. (65,1).

**Verschenkter Überschuss** (Einspeisung 21,0 kWh, 07.–12.10.):

| Kategorie | kWh | Anmerkung |
|---|---|---|
| Speicher voll (≥ 64 °C) | 5,2 | nur 09.10. (4,8) und 07.10. |
| Unterhalb der nächsten Phasenschwelle | 4,9 | systembedingt (Stufen 1,1 / 2,6 / 4,1 kW) |
| 3 Phasen laufen schon | 2,5 | – |
| **Schwelle erreicht, Phase fehlt** | **5,3** | 137 min; 58 min lagen zwischen 1 100 und 1 500 W (Kaskaden-Schwelle), 63 min ≥ 1 500 W (vermutlich Mindestpause/Trägheit, nicht einzeln belegt) |
| **Fail-safe (Lesefehler)** | **3,0** | 102 min, fast nur 12.10. |

**Netzbezug tagsüber (PV > 1 kW):** 16,4 kWh in 754 min: Heizstab 4,9 (30 %), Wallbox Eco 4,6 (28 %), Haushalt/Sonstiges 6,9 (42 %). **Nachts** (PV < 20 W, ohne Auto-Laden): Ø 1 088 W, Median 666 W, p90 2,8 kW, p99 4,6 kW ≈ 12 kWh je Nacht.

**Beispieltag 09.10. (PV 25 kWh):**
06:36–07:36 Wallbox Basic (5–8 kW Bezug), 07:37 Auto-Reset → 08–10 Uhr PV 0,6–0,8 kW, Heizstab aus → ≈ 11:00 Heizstab startet (150 W), 12 Uhr PH1 an 12:12:14, aus 12:13:13, an 12:14:13, PH2 12:19 – 39 Wechsel in der Stunde → 13 Uhr PV 6,6 kW, Heizstab 3,7 kW, Speicher 50,5 → 60,1 °C → 14 Uhr Einspeisung 4,2 kW, Speicher 65,1 °C, Heizstab fällt auf 1,4 kW → ab 15 Uhr Speicher voll, Einspeisung 1,5 / 0,5 kW ohne Abnehmer → 18 Uhr Bezug 3,0 kW (Haushalt).

## 3 Bewertung für die Batterie

**3.1 Position.** Vorschlag: **am Ende** (Prio 4 hinter eBike, Heizstab, Wallbox), wie das Gerät jetzt angelegt wird. Begründung: Die Restmengen, die die bestehende Logik liegen lässt (unter Schwelle 4,9 + Speicher voll 5,2 + 3 Phasen 2,5 = 12,5 kWh in 6 Tagen ≈ **2,1 kWh/Tag**), entsprechen fast genau der Batteriekapazität (2,4 kWh). Die Batterie füllt sich also aus Resten, ohne den Heizstab zu verdrängen. Der Haken sind die Messwerte (3.3).

**3.2 Konflikte.**

| Fall | Befund | Maßnahme (Optionen in 4) |
|---|---|---|
| Entladen speist den Heizstab | Heizstab läuft an 11.10. 107 min mit Netzbezug: die Batterie würde das Wasser heizen | Entladen sperren, solange Heizstab > 100 W |
| Wallbox Eco ↔ Batterie | Batterie (Prio 4) nimmt 2,4 kW Überschuss, bevor der go-e (Start ab ≈ 1,4 kW) ihn sieht → Wallbox startet bei leerer Batterie nie; Prioritätsinversion. Entladen deckt Eco-Bezug (33 %) nur, wenn nicht gesperrt (Sperre `car=2 & > 100 W` greift während Ladung) | Laden der Batterie drosseln, solange Auto angesteckt und wartend |
| Sommermodus Netz-Heizung | Bezug durch Netz-Heizung würde entladen | Entladen sperren bei `summer_mode_heating` (aktuell aus) |
| Oszillation Wallbox-Pause → Entladen → Überschuss | Der Controller pausiert die Wallbox nicht mehr (frc immer 0), die Schleife ist nur über den go-e möglich. Entladen erzeugt nie Einspeisung → keine Rückkopplung | – |

**3.3 Messwerte, die die Batterie verfälscht (zu korrigieren):**

1. `models.py:61,67`: `surplus_without_heater_w` / `true_surplus_w` (Controller-Phasenlogik, Sommermodus `significant_pv`). **Muss** um Ladung (−) / Entladung (+) bereinigt werden, sonst nimmt die Batterie dem Heizstab Überschuss weg und die Phasen kippen.
2. `cascade_service.py:117-122`: bereits korrigiert (Phase 2).
3. `web.py` Überschuss-Anzeigen (`cascade/status`, Wallbox-Banner) und `app.js:363` (Knoten „Haus").
4. Hausverbrauch: Solax-Reg. 47 + Pergola enthält die Batterie nicht → `+ Entladung − Ladung` (`solax_client.py:209`, `web.py` api_solax, `pv_logger.py` Stundenlog).
5. Tagesbilanz `meter.py` und `pv_daily_log`: Verbrauch = PV + Bezug − Einsp. + entladen − geladen.

**3.4 Timing.** Zwischen zwei Minuten ändert sich der Hauptzähler tagsüber bei 9 % der Minuten um > 500 W (p90 359 W), nachts nur bei 4,8 % um > 300 W (p50 14 W). 1×/min reicht für die Grundlast, nicht für Wasserkocher/Herd: Bezugsspitzen von 1 Minute plus Nachregelung. Gegen die Ablage im 10-s-Poller: Er läuft nur lesend, mit wechselndem Takt (10/60 s) und blockiert bei Timeouts (PH1 offline ≈ 7,6 s je Lauf). Ein eigener Regel-Thread/Dienst wäre nötig; `smartMode=1` verhindert Flash-Verschleiß.

## 4 Offene Entscheidungen

1. **Priorität der Batterie:** (A) letzte (Empfehlung), (B) vor der Wallbox, (C) ganz vorn (setzt Korrektur 3.3.1 *und* Heizstab-Rückrechnung voraus).
2. **Batterie vs. Wallbox Eco:** (A) Batterie lädt immer aus Resten (Wallbox kann verhungern), (B) Laden pausiert, solange Auto wartend/angesteckt, (C) Wallbox-Start per eigener Logik statt go-e-Eco.
3. **Entladen bei Heizstab/Sommermodus:** (A) sperren, (B) erlauben (Batterie wärmt Wasser), (C) nur oberhalb eines SoC.
4. **Regel-Takt:** (A) 1 min (Cron), (B) eigener 10-s-Dienst, (C) Zendure-eigene Nulleinspeise-Logik nutzen.
5. **Heizstab-Doppelsteuerung** (unabhängig von der Batterie): Controller-Mindestzeiten einführen, Kaskadenschwelle an `grid_assist_w` angleichen, oder Kaskade auf phasenweises Gerät umbauen.
6. **PH1-Shelly und Fail-safe:** erst Hardware beheben; danach klären, ob Max-Temperatur auch im Fail-safe erzwungen werden soll (nur die erreichbaren Phasen abschalten).
7. **Tote Einstellungen** `wallbox.pause_below/release_above_storage_temp` entfernen oder reaktivieren; Kaskaden-Wallbox-Gerät streichen oder anbinden.

## Anhang A – Wallbox-Sitzungen (Auto lädt, Lücken < 10 min zusammengefasst)

| Beginn | Ende | Modus | Dauer | Eco-Energie | Bezug > 200 W |
|---|---|---|---|---|---|
| 07.10. 13:26 | 17:06 | Eco | 211 min | 10,0 kWh | 182 min |
| 08.10. 15:41 | 15:47 | Eco | 7 min | 0,2 kWh | 7 min |
| 09.10. 06:36 | 07:36 | Basic | 61 min | – | – |
| 10.10. (4 Sitzungen) | – | Eco | 74 min | 2,6 kWh | 68 min |
| 11.10. (4 Sitzungen) | – | Eco | 120 min | 4,7 kWh | 109 min |
| 11.10. 17:30 | 12.10. 07:33 | Basic (10 A) | 834 min | ≈ 62 kWh (Stundenwerte) | – |

## Anhang B – Methodik

- Minutenwerte aus den `Hauptzähler`/`PV-Leistung`/`Heizstab 3EM`/`Wallbox-Leistung`-Zeilen; kWh = Σ W ÷ 60 000.
- „Netz" = Hauptzähler > 0 (Bezug), Einspeisung = < −50 W. Überschuss für die Schwellenprüfung = −(Hauptzähler − Heizstab − Wallbox(Eco)).
- „Schwelle erreicht, Phase fehlt": Einspeisung, Speicher < 64 °C, weniger als 3 Phasen, Überschuss ≥ Schwelle der nächsten Phase (1 100 / 2 600 / 4 100 W), kein Lesefehler.
- Nachts = PV < 20 W; Wallbox-Anteile im Basic-Modus nur über Stundenwerte (`pv_hourly_log.wallbox_w`) schätzbar.
- Phasen-Anzahl aus Leistung: `round(P / 1 500)`, max. 3.
