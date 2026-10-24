#!/usr/bin/env python3
"""Nur lesend: Zeitumstellung prüfen. Aufruf: check_dst.py [YYYY-MM-DD] (Default: heute)."""
import json, re, sqlite3, sys, urllib.request
from datetime import date, datetime, timedelta
D = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
from pathlib import Path
PROD = str(Path.home() / 'pvcontroller-prod')
print(f"=== Prüfung {D} ===")

# 1) Cron-Läufe 00:00–04:59 (Dateireihenfolge = echte Reihenfolge; Labels sind lokale Zeit)
labels = []
pat = re.compile(r'=== PV-Controller Start \((\d{4}-\d\d-\d\dT[\d:]+)\)')
with open(f'{PROD}/cron.log', errors='replace') as f:
    for l in f:
        m = pat.search(l)
        if m and m.group(1).startswith(D):
            labels.append(datetime.fromisoformat(m.group(1)))
win = [t for t in labels if 0 <= t.hour < 5]
steps = {}
bad = []
for a, b in zip(win, win[1:]):
    d = round((b - a).total_seconds() / 60)
    steps[d] = steps.get(d, 0) + 1
    if d != 1 and not (d == -59 and a.hour == 2 and a.minute == 59):
        bad.append((a.strftime('%H:%M:%S'), b.strftime('%H:%M:%S'), d))
real_min = 5 * 60 if D >= '2026-10-25' and D == '2026-10-25' else 5 * 60 - (60 if D == '2027-03-28' else 0)
print(f"1) Cron-Läufe 00–05 Uhr: {len(win)} (Soll ≈ {real_min}; Sprung 02:59→02:00 einmal nur am Umstellungstag)")
print("   Schritte in Minuten (Label-Differenz):", dict(sorted(steps.items())))
print("   Auffälligkeiten:", bad or "keine")
per_label = {}
for t in win: per_label[t.strftime('%H:%M')] = per_label.get(t.strftime('%H:%M'), 0) + 1
dup = {k: v for k, v in per_label.items() if v > 2 or (v > 1 and not k.startswith('02:'))}
print("   Labels mit >2 Läufen (bzw. >1 außerhalb 02:xx):", dup or "keine")

c = sqlite3.connect(f'file:{PROD}/pvcontroller.db?mode=ro', uri=True)
# 2) Zeitpläne
print("2) Heizung/Rollläden-Zeitpläne 00:00–05:00:")
rows = c.execute("select ts,kind,schedule_id,target_id,code,json_extract(data,'$.when') from schedule_result where ts>=? and ts<? order by ts",
                 (D + 'T00:00', (date.fromisoformat(D) + timedelta(days=1)).isoformat() + 'T00:00')).fetchall()
for r in rows:
    if '00:00' <= r[0][11:16] < '05:00': print('  ', r)
seen = {}
for r in rows:
    seen[(r[2], r[1])] = seen.get((r[2], r[1]), 0) + 1
print("   Mehrfach ausgeführte (schedule_id,kind) an diesem Tag:", {k: v for k, v in seen.items() if v > 1} or "keine")
# 3) Geräte-Aussetzer
print("3) Gerätestatus-Ereignisse 00:30–03:30:")
ev = c.execute("select ts,device_id,event from device_health_event where ts>=? and ts<? order by ts", (D + 'T00:30', D + 'T03:30')).fetchall()
flap = {}
for r in ev: flap[r[1]] = flap.get(r[1], 0) + (r[2] == 'offline_ab')
print("   Ausfälle je Gerät:", flap)
mass = {}
for r in ev:
    if r[2] == 'offline_ab': mass.setdefault(r[0][:19], []).append(r[1])
for k, v in mass.items():
    if len(v) >= 4: print(f"   Sammelausfall {k} ({len(v)} Geräte: {', '.join(v[:6])}…)")
# 4) openHAB
print("4) openHAB ONLINE→OFFLINE (Shelly 3EM etc.) 00:00–04:00:")
n = 0
with open('/var/log/openhab/events.log', errors='replace') as f:
    for l in f:
        if l.startswith(D) and 'ONLINE to OFFLINE' in l and '00:00' <= l[11:16] < '04:00':
            n += 1; print('  ', l[:23], re.sub(r".*Thing '([^']+)'.*", r"\1", l.strip())[:80])
print("   Anzahl:", n, "→ Aussetzer um 01:01 (statt 02:01) = UTC-Mitternacht-Theorie bestätigt" if n else "")
# 5) Sonnenzeiten (Config nur Koordinaten)
sys.path.insert(0, PROD)
import suntime
cfg = json.load(open(f'{PROD}/config.json'))
loc = suntime.location(cfg)
print("5) Sonnenzeiten (suntime, lokale Zeit):", loc)
for off in (-1, 0, 1):
    d = date.fromisoformat(D) + timedelta(days=off)
    sr, ss = suntime.sun_times(d, *loc)
    f = lambda x: x.astimezone().strftime('%H:%M') if x else '–'
    print(f"   {d}: Aufgang {f(sr)}  Untergang {f(ss)}")
try:
    j = json.load(urllib.request.urlopen('http://localhost:8000/api/heating/schedules', timeout=10))
    print("   Dashboard-API sun_today:", j.get('sun_today'), " sun_tomorrow:", j.get('sun_tomorrow'))
except Exception as e:
    print("   Dashboard-API nicht lesbar:", e)
