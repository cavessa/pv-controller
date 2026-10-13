// ── Heizung: openHAB-Thermostate (Tab "Heizung" + Settings-Block) ─────────────
// Nutzt $, $$, api, escapeHtml aus app.js.

const HEAT = {
  status: null,
  pending: {},          // room_id → {value, timer}  (−/+ wird gesammelt und nach kurzer Pause gesendet)
  history: {},          // room_id → {temp, setpoint, t}
  open: {},             // room_id → Verlauf aufgeklappt
  sched: [],            // alle Zeitpläne (GET /api/heating/schedules)
  schedOpen: {},        // room_id → Zeitpläne aufgeklappt
  form: {},             // room_id → Eingaben „+ Zeitplan“ (überleben das 30-s-Neuzeichnen)
  timer: null,
};
const HEAT_DAYS = [["mo", "Mo"], ["di", "Di"], ["mi", "Mi"], ["do", "Do"], ["fr", "Fr"], ["sa", "Sa"], ["so", "So"]];
const HEAT_SEND_DELAY_MS = 900;
const HEAT_POLL_MS = 30_000;
const HEAT_HISTORY_TTL_MS = 10 * 60_000;

const _hq = s => String(s ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const fmtC = v => v == null ? "–" : v.toFixed(1).replace(".", ",") + " °C";
const fmtPct = v => v == null ? "–" : Math.round(v) + " %";

function heatActivate() {
  loadHeating();
  clearInterval(HEAT.timer);
  HEAT.timer = setInterval(() => {
    if ($(".tab.active")?.dataset.tab === "heizung") loadHeating(); else { clearInterval(HEAT.timer); HEAT.timer = null; }
  }, HEAT_POLL_MS);
}

async function loadHeating() {
  const banner = $("#heat-banner");
  // nicht neu zeichnen, während im Zeitplan-Formular getippt wird
  if (document.activeElement?.closest?.(".heat-sched-form")) return;
  try {
    const [s, sch] = await Promise.all([
      api("/api/heating/status"),
      api("/api/heating/schedules").catch(() => null),
    ]);
    HEAT.status = s;
    if (sch) HEAT.sched = sch.items;
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Heizung ist nicht aktiv. In den Settings unter „Heizung“ Räume eintragen und aktivieren.";
      $("#heat-dry").classList.add("hidden");
      $("#heat-sched-off").classList.add("hidden");
      $("#heat-updated").textContent = "";
      $("#heat-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    $("#heat-dry").classList.toggle("hidden", !s.dry_run);
    $("#heat-sched-off").classList.toggle("hidden", !!s.schedules_enabled);
    renderHeatRooms(s);
    $("#heat-updated").textContent = "Stand " + new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    for (const r of s.rooms) if (HEAT.open[r.id]) loadHeatHistory(r.id);
  } catch (e) {
    banner.textContent = "Heizung: " + e.message;
    banner.classList.remove("hidden");
  }
}

function _tempClass(r) {
  if (r.temp == null || r.setpoint == null) return "";
  if (r.temp < r.setpoint - 1) return "cold";
  if (r.temp > r.setpoint + 1) return "warm";
  return "";
}

function renderHeatRooms(s) {
  const wrap = $("#heat-rooms");
  wrap.innerHTML = s.rooms.map(r => {
    const pend = HEAT.pending[r.id];
    const sp = pend ? pend.value : r.setpoint;
    const bat = r.battery == null ? "" : r.battery.low
      ? `<span class="heat-bat low" title="Batterie schwach">⚠️ Batterie</span>`
      : `<span class="heat-bat" title="Batterie OK">🔋 ${r.battery.percent != null ? r.battery.percent + " %" : "OK"}</span>`;
    const mode = r.mode == null ? "" : r.mode === "AUTO"
      ? `<span class="heat-mode auto" title="Gerät folgt eigenem Wochenprofil">AUTO · Gerät folgt eigenem Wochenprofil</span>`
      : `<span class="heat-mode">${_hq(r.mode)}</span>`;
    const meta = [
      r.has.valve ? `Ventil ${fmtPct(r.valve)}` : "",
      r.has.humidity ? `Luftfeuchte ${fmtPct(r.humidity)}` : "",
    ].filter(Boolean).join('<span class="sep">·</span>');
    const ctrl = r.controllable ? `
        <div class="heat-set">
          <span class="heat-set-label">Soll</span>
          <button type="button" class="heat-step" data-room="${_hq(r.id)}" data-step="-1" aria-label="${_hq(r.name)} kälter" ${sp == null ? "disabled" : ""}>−</button>
          <span class="heat-set-val${pend ? " pending" : ""}">${fmtC(sp)}</span>
          <button type="button" class="heat-step" data-room="${_hq(r.id)}" data-step="1" aria-label="${_hq(r.name)} wärmer" ${sp == null ? "disabled" : ""}>+</button>
        </div>` : `<div class="heat-set"><span class="heat-set-label">nur Anzeige</span></div>`;
    return `
      <div class="card heat-room${r.error ? " has-error" : ""}" data-room="${_hq(r.id)}">
        <div class="heat-room-head">
          <h2>${escapeHtml(r.name)}</h2>
          ${bat}
        </div>
        <div class="heat-room-main">
          <div class="heat-temp ${_tempClass(r)}">${fmtC(r.temp)}</div>
          ${ctrl}
        </div>
        ${meta ? `<div class="heat-meta">${meta}</div>` : ""}
        ${mode ? `<div class="heat-meta">${mode}</div>` : ""}
        ${r.next ? `<div class="heat-meta heat-next">Nächste Änderung: ${_fmtWhen(r.next.at)} → ${fmtC(r.next.setpoint)}${s.schedules_enabled ? "" : " <span class=\"heat-off\">(Zeitpläne aus)</span>"}</div>` : ""}
        ${r.alert ? `<div class="heat-alert">⚠️ ${escapeHtml(r.alert)}</div>` : ""}
        ${r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : ""}
        <div class="heat-toggles">
          <button type="button" class="heat-hist-toggle" data-hist="${_hq(r.id)}">${HEAT.open[r.id] ? "▾" : "▸"} Verlauf 24 h</button>
          ${r.controllable ? `<button type="button" class="heat-hist-toggle" data-sched="${_hq(r.id)}">${HEAT.schedOpen[r.id] ? "▾" : "▸"} Zeitpläne (${HEAT.sched.filter(e => e.room_id === r.id).length})</button>` : ""}
        </div>
        <div class="heat-chart" id="heat-chart-${_hq(r.id)}">${HEAT.open[r.id] ? _heatChartSvg(r.id) : ""}</div>
        ${r.controllable && HEAT.schedOpen[r.id] ? _schedPanel(r, s) : ""}
      </div>`;
  }).join("");
}

// ── Zeitpläne ──────────────────────────────────────────────────────────────

const _WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

function _fmtWhen(iso) {
  // "2026-10-13T22:00+02:00" → "heute 22:00" / "morgen 06:00" / "Do 16.10. 06:00"
  const d = new Date(iso);
  const hm = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  const day0 = new Date(); day0.setHours(0, 0, 0, 0);
  const diff = Math.round((new Date(d).setHours(0, 0, 0, 0) - day0) / 86400_000);
  if (diff === 0) return "heute " + hm;
  if (diff === 1) return "morgen " + hm;
  return `${_WD[d.getDay()]} ${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}. ${hm}`;
}

function _fmtDate(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `einmalig ${_WD[dt.getDay()]} ${String(d).padStart(2, "0")}.${String(m).padStart(2, "0")}.`;
}

const _RESULT = { sent: "gesendet", unchanged: "war schon eingestellt", dry_run: "Dry-Run", skipped: "verpasst",
  superseded: "durch späteren ersetzt", failed: "Fehler" };

function _schedForm(roomId, r) {
  const f = HEAT.form[roomId] ||= { time: "", setpoint: r.setpoint ?? 20, days: [], date: "" };
  return f;
}

function _schedPanel(r, s) {
  const list = HEAT.sched.filter(e => e.room_id === r.id)
    .sort((a, b) => (a.date || "").localeCompare(b.date || "") || a.time.localeCompare(b.time));
  const f = _schedForm(r.id, r);
  const rows = list.map(e => {
    const when = e.date ? `<span class="heat-chip once">${_fmtDate(e.date)}</span>`
      : HEAT_DAYS.map(([k, l]) => `<span class="heat-chip${e.days.includes(k) ? " on" : ""}">${l}</span>`).join("");
    const last = e.last_run ? `<small class="heat-last" title="${_hq(e.last_run)}">zuletzt ${_hq(e.last_run.slice(5, 16).replace("T", " "))} · ${_RESULT[e.last_result] || _hq(e.last_result || "")}</small>` : "";
    return `<div class="heat-sched-row${e.enabled ? "" : " off"}">
        <span class="heat-sched-time">${_hq(e.time)}</span>
        <span class="heat-sched-sp">${fmtC(e.setpoint)}</span>
        <span class="heat-chips">${when}</span>
        <label class="heat-switch" title="${e.enabled ? "aktiv" : "inaktiv"}"><input type="checkbox" data-sched-toggle="${e.id}" ${e.enabled ? "checked" : ""}><span></span></label>
        <button type="button" class="heat-del" data-sched-del="${e.id}" aria-label="Zeitplan löschen">✕</button>
        ${last}
      </div>`;
  }).join("") || `<p class="note">Noch keine Zeitpläne.</p>`;
  return `<div class="heat-sched" data-room="${_hq(r.id)}">
      ${rows}
      <div class="heat-sched-form" data-room="${_hq(r.id)}">
        <div class="heat-form-row">
          <label>Uhrzeit<input type="time" data-f="time" value="${_hq(f.time)}" required></label>
          <label>Soll (°C)<input type="number" data-f="setpoint" step="${s.setpoint_step}" min="${s.setpoint_min}" max="${s.setpoint_max}" value="${_hq(f.setpoint)}"></label>
        </div>
        <div class="heat-chips pick">${HEAT_DAYS.map(([k, l]) => `<button type="button" class="heat-chip${f.days.includes(k) ? " on" : ""}" data-day="${k}" aria-pressed="${f.days.includes(k)}">${l}</button>`).join("")}
          <button type="button" class="heat-chip quick" data-days="mo,di,mi,do,fr">Werktags</button>
          <button type="button" class="heat-chip quick" data-days="mo,di,mi,do,fr,sa,so">Täglich</button></div>
        <div class="heat-form-row">
          <label>oder einmalig am<input type="date" data-f="date" value="${_hq(f.date)}"></label>
          <button type="button" class="btn primary" data-sched-add="${_hq(r.id)}">+ Zeitplan</button>
        </div>
        <span class="form-msg" id="heat-sched-msg-${_hq(r.id)}"></span>
      </div>
    </div>`;
}

async function _schedAdd(roomId) {
  const f = HEAT.form[roomId];
  const msg = document.getElementById("heat-sched-msg-" + roomId);
  const body = { room_id: roomId, time: f.time, setpoint: Number(f.setpoint) };
  if (f.date) body.date = f.date; else body.days = f.days;
  try {
    await api("/api/heating/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    HEAT.form[roomId] = { time: "", setpoint: f.setpoint, days: f.days, date: "" };
    document.activeElement?.blur();
    await loadHeating();
  } catch (e) {
    if (msg) { msg.textContent = e.message; msg.className = "form-msg error"; }
  }
}

async function _schedToggle(id, enabled) {
  try {
    await api(`/api/heating/schedules/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
  } catch (e) { alert("Zeitplan: " + e.message); }
  loadHeating();
}

async function _schedDelete(id) {
  const e = HEAT.sched.find(x => x.id === id);
  if (!confirm(`Zeitplan ${e ? e.time + " → " + fmtC(e.setpoint) : id} löschen?`)) return;
  try { await api(`/api/heating/schedules/${id}`, { method: "DELETE" }); } catch (err) { alert("Zeitplan: " + err.message); }
  loadHeating();
}

function heatStep(roomId, dir) {
  const s = HEAT.status;
  const r = s?.rooms.find(x => x.id === roomId);
  if (!r) return;
  const cur = HEAT.pending[roomId]?.value ?? r.setpoint;
  if (cur == null) return;
  const step = s.setpoint_step;
  // auf das Raster runden (HM-Thermostate können z. B. 7,5 haben, das passt; Fremdwerte werden eingerastet)
  let v = Math.round((cur + dir * step) / step) * step;
  v = Math.min(s.setpoint_max, Math.max(s.setpoint_min, v));
  const p = HEAT.pending[roomId] || {};
  clearTimeout(p.timer);
  p.value = v;
  p.timer = setTimeout(() => _heatSend(roomId), HEAT_SEND_DELAY_MS);
  HEAT.pending[roomId] = p;
  renderHeatRooms(s);
}

async function _heatSend(roomId) {
  const p = HEAT.pending[roomId];
  if (!p) return;
  try {
    await api("/api/heating/setpoint", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room_id: roomId, setpoint: p.value }),
    });
    const r = HEAT.status.rooms.find(x => x.id === roomId);
    if (r && !HEAT.status.dry_run) r.setpoint = p.value;
  } catch (e) {
    alert("Sollwert nicht gesetzt: " + e.message);
  } finally {
    delete HEAT.pending[roomId];
    renderHeatRooms(HEAT.status);
    // openHAB/CCU brauchen einen Moment, bis der neue Wert zurückgemeldet ist
    setTimeout(loadHeating, 3000);
  }
}

async function loadHeatHistory(roomId, force = false) {
  const h = HEAT.history[roomId];
  if (!force && h && Date.now() - h.t < HEAT_HISTORY_TTL_MS) { _paintHeatChart(roomId); return; }
  try {
    const d = await api(`/api/heating/history/${encodeURIComponent(roomId)}?hours=24`);
    HEAT.history[roomId] = { temp: d.temp || [], setpoint: d.setpoint || [], t: Date.now(), error: d.error };
  } catch (e) {
    HEAT.history[roomId] = { temp: [], setpoint: [], t: Date.now(), error: e.message };
  }
  _paintHeatChart(roomId);
}

function _paintHeatChart(roomId) {
  const el = document.getElementById("heat-chart-" + roomId);
  if (el && HEAT.open[roomId]) el.innerHTML = _heatChartSvg(roomId);
}

function _heatChartSvg(roomId) {
  const h = HEAT.history[roomId];
  if (!h) return `<div class="chart-empty">Lade…</div>`;
  if (!h.temp.length && !h.setpoint.length) return `<div class="chart-empty">${h.error ? escapeHtml(h.error) : "Keine Verlaufsdaten"}</div>`;
  const W = 320, H = 110, pad = { l: 30, r: 6, t: 8, b: 18 };
  const all = [...h.temp, ...h.setpoint];
  const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
  let lo = Math.floor(Math.min(...all.map(p => p[1])) - 0.5), hi = Math.ceil(Math.max(...all.map(p => p[1])) + 0.5);
  if (hi - lo < 4) { const m = (hi + lo) / 2; lo = Math.floor(m - 2); hi = Math.ceil(m + 2); }
  const x = t => pad.l + (t - t0) / Math.max(1, t1 - t0) * (W - pad.l - pad.r);
  const y = v => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);
  const line = pts => pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const step = pts => pts.map((p, i) => i ? `H${x(p[0]).toFixed(1)}V${y(p[1]).toFixed(1)}` : `M${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  let g = "";
  const ticks = hi - lo <= 6 ? 1 : 2;
  for (let v = lo; v <= hi; v += ticks) {
    g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" class="hc-grid"/>`;
    g += `<text x="${pad.l - 4}" y="${y(v) + 3}" text-anchor="end" class="hc-lbl">${v}°</text>`;
  }
  // Stundenmarken alle 6 h
  const d0 = new Date(t0); d0.setMinutes(0, 0, 0);
  for (let t = d0.getTime(); t <= t1; t += 3600_000) {
    const hr = new Date(t).getHours();
    if (t < t0 || hr % 6) continue;
    g += `<line x1="${x(t)}" x2="${x(t)}" y1="${pad.t}" y2="${H - pad.b}" class="hc-grid"/>`;
    g += `<text x="${x(t)}" y="${H - 5}" text-anchor="middle" class="hc-lbl">${String(hr).padStart(2, "0")}h</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" class="heat-svg" role="img" aria-label="Verlauf 24 h">
    ${g}
    ${h.setpoint.length ? `<path d="${step(h.setpoint)}" class="hc-set"/>` : ""}
    ${h.temp.length ? `<path d="${line(h.temp)}" class="hc-temp"/>` : ""}
  </svg>
  <div class="heat-legend"><span><i class="hc-temp-k"></i>Ist</span>${h.setpoint.length ? `<span><i class="hc-set-k"></i>Soll</span>` : ""}</div>`;
}

// ── Settings: Räume ────────────────────────────────────────────────────────

const HS_FIELDS = [
  ["setpoint_item", "Sollwert"], ["temp_item", "Ist-Temperatur *"], ["valve_item", "Ventil"],
  ["battery_item", "Batterie"], ["humidity_item", "Luftfeuchte"], ["mode_item", "Modus"],
];
let HS_ROOMS = [];

async function loadHeatingSettings() {
  const msg = $("#hs-msg");
  try {
    const c = await api("/api/heating/config");
    const h = c.heating;
    $("#hs-enabled").checked = !!h.enabled;
    $("#hs-sched").checked = !!h.schedules_enabled;
    $("#hs-min").value = h.setpoint_min ?? 5;
    $("#hs-max").value = h.setpoint_max ?? 26;
    HS_ROOMS = (h.rooms || []).map(r => ({ ...r }));
    $("#hs-openhab").textContent = c.openhab.url
      ? `openHAB: ${c.openhab.url} · Token ${c.openhab.token_set ? "gesetzt" : "FEHLT"} (nur in config.json)`
      : "openHAB: openhab.url fehlt in config.json";
    renderHsRooms();
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
  // Items für die Auswahl (Dropdown mit Suche über <datalist>)
  try {
    const d = await api("/api/heating/items");
    $("#hs-items").innerHTML = d.items.map(i =>
      `<option value="${_hq(i.name)}">${_hq((i.label || "") + " · " + i.type + " · " + (i.state ?? ""))}</option>`).join("");
  } catch (_) { /* ohne openHAB: freie Eingabe */ }
}

function renderHsRooms() {
  $("#hs-rooms").innerHTML = HS_ROOMS.map((r, i) => `
    <details class="hs-room" ${r._open ? "open" : ""} data-i="${i}">
      <summary>${escapeHtml(r.name || "(neu)")} <small>${escapeHtml(r.id || "")}${r.setpoint_item ? "" : " · nur Anzeige"}</small></summary>
      <label>ID (a–z, 0–9, _ -)<input type="text" data-k="id" value="${_hq(r.id)}" placeholder="z. B. bad"></label>
      <label>Name<input type="text" data-k="name" value="${_hq(r.name)}"></label>
      ${HS_FIELDS.map(([k, lbl]) => `<label>${lbl}<input type="text" list="hs-items" data-k="${k}" value="${_hq(r[k])}" autocomplete="off" placeholder="openHAB-Item"></label>`).join("")}
      <div class="hs-room-actions">
        <button type="button" class="btn" data-move="-1">↑</button>
        <button type="button" class="btn" data-move="1">↓</button>
        <button type="button" class="btn" data-del="1">Raum entfernen</button>
      </div>
    </details>`).join("");
}

function _hsCollect() {
  $$("#hs-rooms .hs-room").forEach(el => {
    const r = HS_ROOMS[+el.dataset.i];
    r._open = el.open;
    el.querySelectorAll("input[data-k]").forEach(inp => { r[inp.dataset.k] = inp.value.trim(); });
  });
}

async function saveHeatingSettings() {
  _hsCollect();
  const msg = $("#hs-msg");
  msg.textContent = "…"; msg.className = "form-msg";
  const body = {
    enabled: $("#hs-enabled").checked,
    schedules_enabled: $("#hs-sched").checked,
    setpoint_min: Number($("#hs-min").value),
    setpoint_max: Number($("#hs-max").value),
    rooms: HS_ROOMS.map(({ _open, ...r }) => r),
  };
  try {
    const res = await api("/api/heating/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    msg.textContent = "Gespeichert (Backup: " + res.backup + ")"; msg.className = "form-msg ok";
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
}

function bootHeating() {
  $("#heat-rooms")?.addEventListener("click", e => {
    const b = e.target.closest("[data-step]");
    if (b) { heatStep(b.dataset.room, Number(b.dataset.step)); return; }
    const t = e.target.closest("[data-hist]");
    if (t) {
      const id = t.dataset.hist;
      HEAT.open[id] = !HEAT.open[id];
      t.textContent = (HEAT.open[id] ? "▾" : "▸") + " Verlauf 24 h";
      const el = document.getElementById("heat-chart-" + id);
      if (!HEAT.open[id]) el.innerHTML = ""; else loadHeatHistory(id);
      return;
    }
    const sc = e.target.closest("[data-sched]");
    if (sc) { HEAT.schedOpen[sc.dataset.sched] = !HEAT.schedOpen[sc.dataset.sched]; renderHeatRooms(HEAT.status); return; }
    const day = e.target.closest("[data-day]"), quick = e.target.closest("[data-days]");
    if (day || quick) {
      const f = HEAT.form[e.target.closest(".heat-sched-form").dataset.room];
      if (quick) f.days = quick.dataset.days.split(",");
      else f.days = f.days.includes(day.dataset.day) ? f.days.filter(d => d !== day.dataset.day) : [...f.days, day.dataset.day];
      f.date = "";
      renderHeatRooms(HEAT.status);
      return;
    }
    const add = e.target.closest("[data-sched-add]");
    if (add) { _schedAdd(add.dataset.schedAdd); return; }
    const del = e.target.closest("[data-sched-del]");
    if (del) { _schedDelete(Number(del.dataset.schedDel)); return; }
  });
  $("#heat-rooms")?.addEventListener("change", e => {
    const t = e.target.closest("[data-sched-toggle]");
    if (t) _schedToggle(Number(t.dataset.schedToggle), t.checked);
  });
  $("#heat-rooms")?.addEventListener("input", e => {
    const inp = e.target.closest(".heat-sched-form [data-f]");
    if (!inp) return;
    const f = HEAT.form[inp.closest(".heat-sched-form").dataset.room];
    f[inp.dataset.f] = inp.value;
    if (inp.dataset.f === "date" && inp.value) {
      f.days = [];
      inp.closest(".heat-sched-form").querySelectorAll("[data-day]").forEach(b => { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); });
    }
  });

  $("#hs-save")?.addEventListener("click", saveHeatingSettings);
  $("#hs-add")?.addEventListener("click", () => {
    _hsCollect();
    HS_ROOMS.push({ id: "", name: "", _open: true });
    renderHsRooms();
  });
  $("#hs-rooms")?.addEventListener("click", e => {
    const el = e.target.closest(".hs-room");
    if (!el) return;
    const i = +el.dataset.i;
    if (e.target.closest("[data-del]")) {
      _hsCollect();
      if (!confirm(`Raum „${HS_ROOMS[i].name || HS_ROOMS[i].id}“ entfernen?`)) return;
      HS_ROOMS.splice(i, 1); renderHsRooms();
    } else if (e.target.closest("[data-move]")) {
      _hsCollect();
      const j = i + Number(e.target.closest("[data-move]").dataset.move);
      if (j < 0 || j >= HS_ROOMS.length) return;
      [HS_ROOMS[i], HS_ROOMS[j]] = [HS_ROOMS[j], HS_ROOMS[i]]; renderHsRooms();
    }
  });
}

document.addEventListener("DOMContentLoaded", bootHeating);
