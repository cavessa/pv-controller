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
  roomsSig: null,       // Raumliste der gerenderten Cards (Neuaufbau nur bei Änderung)
  loadSeq: 0,           // nur das zuletzt gestartete Laden wird angezeigt (Poll vs. Laden nach Änderung)
  chat: { history: [], busy: false, proposal: null, status: null },
};
const HEAT_DAYS = [["mo", "Mo"], ["di", "Di"], ["mi", "Mi"], ["do", "Do"], ["fr", "Fr"], ["sa", "Sa"], ["so", "So"]];
const HEAT_SEND_DELAY_MS = 900;
const HEAT_POLL_MS = 30_000;
const HEAT_HISTORY_TTL_MS = 5 * 60_000;     // 24-h-Verlauf höchstens alle 5 min neu laden

const _hq = s => String(s ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const fmtC = v => v == null ? "–" : v.toFixed(1).replace(".", ",") + " °C";
const fmtPct = v => v == null ? "–" : Math.round(v) + " %";

const _heatVisible = () => !document.hidden && $(".tab.active")?.dataset.tab === "heizung";

function heatActivate() {
  loadHeating();
  clearInterval(HEAT.timer);
  // alle 30 s; pausiert, solange ein anderer Tab aktiv oder die Seite verdeckt ist
  HEAT.timer = setInterval(() => {
    if (_heatVisible()) loadHeating();
    else if ($(".tab.active")?.dataset.tab !== "heizung") { clearInterval(HEAT.timer); HEAT.timer = null; }
  }, HEAT_POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(HEAT.timer); HEAT.timer = null; }
  else if (_heatVisible()) heatActivate();            // zurück: einmal sofort aktualisieren
});

async function loadHeating() {
  const banner = $("#heat-banner");
  const seq = ++HEAT.loadSeq;
  try {
    const [s, sch] = await Promise.all([
      api("/api/heating/status"),
      api("/api/heating/schedules").catch(() => null),
    ]);
    if (seq !== HEAT.loadSeq) return;                // inzwischen neuer geladen (z. B. nach Löschen) → verwerfen
    HEAT.status = s;
    if (sch) HEAT.sched = sch.items;
    renderChatStatus(s.chat, s.enabled && s.openhab_configured);
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Heizung ist nicht aktiv. In den Settings unter „Heizung“ Räume eintragen und aktivieren.";
      $("#heat-dry").classList.add("hidden");
      $("#heat-sched-off").classList.add("hidden");
      $("#heat-updated").textContent = "";
      $("#heat-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      HEAT.roomsSig = null;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    $("#heat-dry").classList.toggle("hidden", !s.dry_run);
    $("#heat-sched-off").classList.toggle("hidden", !!s.schedules_enabled);
    renderHeatRooms(s);
    const stand = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#heat-updated").textContent = "Stand " + stand;
    pillTimes.heating = stand;                       // Kopfzeilen-Pill zeigt auf dem Heizung-Tab diese Zeit
    updatePillTime();
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

// Cards: einmal aufbauen (bzw. wenn sich die Raumliste ändert), danach nur geänderte Slots ersetzen.
function renderHeatRooms(s) {
  const wrap = $("#heat-rooms");
  const sig = s.rooms.map(r => `${r.id}|${r.name}|${r.controllable}`).join(";");
  if (sig !== HEAT.roomsSig || !wrap.querySelector(".heat-room")) {
    wrap.innerHTML = s.rooms.map(_roomSkeleton).join("");
    HEAT.roomsSig = sig;
  }
  for (const r of s.rooms) updateHeatRoom(r.id);
}

function _roomSkeleton(r) {
  const id = _hq(r.id);
  const ctrl = r.controllable ? `
        <div class="heat-set">
          <span class="heat-set-label">Soll</span>
          <button type="button" class="heat-step" data-room="${id}" data-step="-1" aria-label="${_hq(r.name)} kälter">−</button>
          <span class="heat-set-val" data-slot="sp"></span>
          <button type="button" class="heat-step" data-room="${id}" data-step="1" aria-label="${_hq(r.name)} wärmer">+</button>
        </div>` : `<div class="heat-set"><span class="heat-set-label">nur Anzeige</span></div>`;
  return `
      <div class="card heat-room" data-room="${id}">
        <div class="heat-room-head"><h2>${escapeHtml(r.name)}</h2><span data-slot="bat"></span></div>
        <div class="heat-room-main"><div class="heat-temp" data-slot="temp"></div>${ctrl}</div>
        <div data-slot="meta"></div><div data-slot="mode"></div><div data-slot="next"></div>
        <div data-slot="alert"></div><div data-slot="err"></div>
        <div class="heat-toggles" data-slot="toggles"></div>
        <div class="heat-chart" id="heat-chart-${id}"></div>
        <div data-slot="sched"></div>
      </div>`;
}

function _typingIn(el) {
  const a = document.activeElement;
  return !!a && el.contains(a) && a.matches(".heat-sched-form input:not([type=checkbox]), .heat-sched-form select, .heat-sched-form textarea");
}

function _setSlot(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}

// Einen Raum in place aktualisieren. force: Zeitplan-Bereich auch bei Fokus darin neu zeichnen (eigener Klick).
function updateHeatRoom(roomId, force = false) {
  const s = HEAT.status;
  const r = s?.rooms.find(x => x.id === roomId);
  const card = document.querySelector(`.heat-room[data-room="${CSS.escape(roomId)}"]`);
  if (!r || !card) return;
  const slot = name => card.querySelector(`[data-slot="${name}"]`);
  const pend = HEAT.pending[r.id];
  const sp = pend ? pend.value : r.setpoint;
  card.classList.toggle("has-error", !!r.error);
  const t = slot("temp");
  const tCls = "heat-temp " + _tempClass(r);
  if (t.className.trim() !== tCls.trim()) t.className = tCls;
  if (t.textContent !== fmtC(r.temp)) t.textContent = fmtC(r.temp);
  const v = slot("sp");
  if (v) {                                           // −/+ gesammelt: angezeigter Wert bleibt der eigene
    if (v.textContent !== fmtC(sp)) v.textContent = fmtC(sp);
    v.classList.toggle("pending", !!pend);
    card.querySelectorAll(".heat-step").forEach(b => { b.disabled = sp == null; });
  }
  _setSlot(slot("bat"), r.battery == null ? "" : r.battery.low
    ? `<span class="heat-bat low" title="Batterie schwach">⚠️ Batterie</span>`
    : `<span class="heat-bat" title="Batterie OK">🔋 ${r.battery.percent != null ? r.battery.percent + " %" : "OK"}</span>`);
  const meta = [r.has.valve ? `Ventil ${fmtPct(r.valve)}` : "", r.has.humidity ? `Luftfeuchte ${fmtPct(r.humidity)}` : ""]
    .filter(Boolean).join('<span class="sep">·</span>');
  _setSlot(slot("meta"), meta ? `<div class="heat-meta">${meta}</div>` : "");
  _setSlot(slot("mode"), r.mode == null ? "" : `<div class="heat-meta">${r.mode === "AUTO"
    ? `<span class="heat-mode auto" title="Gerät folgt eigenem Wochenprofil">AUTO · Gerät folgt eigenem Wochenprofil</span>`
    : `<span class="heat-mode">${_hq(r.mode)}</span>`}</div>`);
  _setSlot(slot("next"), r.next ? `<div class="heat-meta heat-next">Nächste Änderung: ${_fmtWhen(r.next.at)} → ${fmtC(r.next.setpoint)}${s.schedules_enabled ? "" : ` <span class="heat-off">(Zeitpläne aus)</span>`}</div>` : "");
  _setSlot(slot("alert"), r.alert ? `<div class="heat-alert">⚠️ ${escapeHtml(r.alert)}</div>` : "");
  _setSlot(slot("err"), r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : "");
  _setSlot(slot("toggles"), `<button type="button" class="heat-hist-toggle" data-hist="${_hq(r.id)}">${HEAT.open[r.id] ? "▾" : "▸"} Verlauf 24 h</button>`
    + (r.controllable ? `<button type="button" class="heat-hist-toggle" data-sched="${_hq(r.id)}">${HEAT.schedOpen[r.id] ? "▾" : "▸"} Zeitpläne (${HEAT.sched.filter(e => e.room_id === r.id).length})</button>` : ""));
  const sch = slot("sched");
  // nur während im Formular getippt wird nicht ersetzen (Fokus in einem Eingabefeld) – ein fokussierter
  // Button/Schalter (✕, An/Aus, Wochentag) blockiert nichts
  if (force || !_typingIn(sch)) _setSlot(sch, r.controllable && HEAT.schedOpen[r.id] ? _schedPanel(r, s) : "");
}

// ── Chat ───────────────────────────────────────────────────────────────────

function renderChatStatus(st, heatingOn) {
  HEAT.chat.status = st;
  const card = $("#heat-chat");
  if (!card) return;
  card.classList.toggle("hidden", !heatingOn);
  const ready = !!(st && st.ready);
  const inp = $("#heat-chat-input");
  inp.disabled = !ready || HEAT.chat.busy;
  $("#heat-chat-send").disabled = !ready || HEAT.chat.busy;
  card.classList.toggle("disabled", !ready);
  const hint = $("#heat-chat-hint");
  hint.textContent = st?.hint || "";
  hint.classList.toggle("hidden", !st?.hint);
  $("#heat-chat-count").textContent = st && st.enabled
    ? `${st.provider_name ? st.provider_name + " · " : ""}${st.used_today}/${st.limit} heute` : "";
}

function _chatAdd(role, text, cls = "") {
  const log = $("#heat-chat-log");
  const div = document.createElement("div");
  div.className = `heat-msg ${role}${cls ? " " + cls : ""}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function _fmtSp(v) { return String(v).replace(".", ",") + " °C"; }

function _renderProposal(res) {
  const p = res.proposal;
  const box = $("#heat-chat-preview");
  if (!p) { box.innerHTML = ""; return; }
  const groups = p.groups.map(g => `<div class="heat-prev-group"><div class="heat-prev-label">${escapeHtml(g.label)}</div>
      ${g.rows.map(r => `<div class="heat-prev-row"><span>${_hq(r.time)}</span><span>${escapeHtml(r.room)} → ${_fmtSp(r.setpoint)}</span></div>`).join("")}</div>`).join("");
  const imm = p.immediate.length ? `<div class="heat-prev-group"><div class="heat-prev-label">Sofort</div>
      ${p.immediate.map(i => `<div class="heat-prev-row"><span>jetzt</span><span>${escapeHtml(i.room)} → ${_fmtSp(i.setpoint)}</span></div>`).join("")}</div>` : "";
  const del = p.deletes.length ? `<div class="heat-prev-group del"><div class="heat-prev-label">Löschen</div>
      ${p.deletes.map(d => `<div class="heat-prev-row"><span>${_hq(d.time)}</span><span>${escapeHtml(d.room)} → ${_fmtSp(d.setpoint)} · ${escapeHtml(d.when)}</span></div>`).join("")}</div>` : "";
  const warn = [
    p.groups.length && !res.schedules_enabled ? "„Zeitpläne aktiv“ ist aus: die Pläne werden gespeichert, aber nicht ausgeführt." : "",
    p.immediate.length && res.dry_run ? "Dry-Run: Sofort-Änderungen werden nur geloggt." : "",
  ].filter(Boolean).map(t => `<p class="heat-prev-warn">${t}</p>`).join("");
  box.innerHTML = `<div class="heat-prev">
      ${p.summary ? `<div class="heat-prev-sum">${escapeHtml(p.summary)}</div>` : ""}
      ${groups}${imm}${del}${warn}
      <div class="heat-prev-actions">
        <button type="button" class="btn primary" data-chat-apply="${_hq(p.id)}">Übernehmen</button>
        <button type="button" class="btn" data-chat-cancel="1">Abbrechen</button>
      </div></div>`;
}

async function chatSend(text) {
  const c = HEAT.chat;
  if (!text || c.busy) return;
  c.proposal = null; _renderProposal({});
  _chatAdd("user", text);
  c.history.push({ role: "user", content: text });
  c.busy = true; renderChatStatus(c.status, true);
  const wait = document.createElement("div");
  wait.className = "heat-msg assistant pending"; wait.textContent = "…";
  $("#heat-chat-log").appendChild(wait);
  try {
    const res = await api("/api/heating/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: c.history }) });
    wait.remove();
    if (res.chat) c.status = res.chat;
    if (res.type === "question") {
      _chatAdd("assistant", res.text);
      c.history.push({ role: "assistant", content: res.text });
    } else if (res.type === "proposal") {
      _chatAdd("assistant", "Vorschlag – bitte prüfen:");
      c.proposal = res.proposal; _renderProposal(res);
    } else {
      const cls = { error: "err", invalid: "err", limit: "info", unavailable: "info", answer: "" }[res.type] ?? "";
      _chatAdd("assistant", res.text, cls);
      c.history = [];
    }
  } catch (e) {
    wait.remove();
    _chatAdd("assistant", "Fehler: " + e.message, "err");
  } finally {
    c.busy = false; renderChatStatus(c.status, true);
    $("#heat-chat-input").focus();
  }
}

async function chatApply(id) {
  const c = HEAT.chat;
  try {
    const r = await api("/api/heating/chat/apply", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposal_id: id }) });
    const parts = [];
    if (r.created.length) parts.push(`${r.created.length} ${r.created.length > 1 ? "Zeitpläne" : "Zeitplan"} angelegt`);
    if (r.deleted.length) parts.push(`${r.deleted.length} gelöscht`);
    if (r.immediate.length) parts.push(`${r.immediate.filter(x => x.ok).length}/${r.immediate.length} sofort gesetzt`);
    _chatAdd("assistant", "✓ Übernommen: " + (parts.join(", ") || "nichts zu tun"), r.ok ? "ok" : "err");
  } catch (e) {
    _chatAdd("assistant", "Nicht übernommen: " + e.message, "err");
  }
  c.proposal = null; c.history = []; _renderProposal({});
  loadHeating();
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
  updateHeatRoom(roomId);
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
    updateHeatRoom(roomId);
    loadHeating();                                   // sofort: Soll, Hinweise, „Nächste Änderung“
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
  // nur zeichnen, wenn aufgeklappt – und nur ersetzen, wenn sich das SVG (= die Daten) geändert hat
  const el = document.getElementById("heat-chart-" + roomId);
  if (el && HEAT.open[roomId]) _setSlot(el, _heatChartSvg(roomId));
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
let HS_ASSIST = {};
const HS_KEY_FIELD = { openai: "openai_api_key", anthropic: "api_key" };

function _hsKeyNote() {
  const p = $("#hs-provider").value;
  const set = (HS_ASSIST.keys_set || {})[p];
  $("#hs-chat-key").textContent = set
    ? `API-Key (${HS_KEY_FIELD[p]}): gesetzt – nur in config.json, wird nie angezeigt`
    : `API-Key fehlt: assistant.${HS_KEY_FIELD[p]} in config.json eintragen`;
}

async function loadHeatingSettings() {
  const msg = $("#hs-msg");
  try {
    const c = await api("/api/heating/config");
    const h = c.heating;
    $("#hs-enabled").checked = !!h.enabled;
    $("#hs-sched").checked = !!h.schedules_enabled;
    const a = c.assistant || {};
    HS_ASSIST = a;
    $("#hs-chat").checked = !!a.enabled;
    $("#hs-provider").value = a.provider || "anthropic";
    $("#hs-model").value = a.model || (a.default_models || {})[a.provider] || "";
    $("#hs-limit").value = a.max_requests_per_day ?? 100;
    _hsKeyNote();
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
    assistant: { enabled: $("#hs-chat").checked, provider: $("#hs-provider").value, model: $("#hs-model").value.trim(),
                 max_requests_per_day: Number($("#hs-limit").value) },
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
      updateHeatRoom(id);
      const el = document.getElementById("heat-chart-" + id);
      if (!HEAT.open[id]) _setSlot(el, ""); else { _setSlot(el, _heatChartSvg(id)); loadHeatHistory(id); }
      return;
    }
    const sc = e.target.closest("[data-sched]");
    if (sc) { HEAT.schedOpen[sc.dataset.sched] = !HEAT.schedOpen[sc.dataset.sched]; updateHeatRoom(sc.dataset.sched, true); return; }
    const day = e.target.closest("[data-day]"), quick = e.target.closest("[data-days]");
    if (day || quick) {
      const f = HEAT.form[e.target.closest(".heat-sched-form").dataset.room];
      if (quick) f.days = quick.dataset.days.split(",");
      else f.days = f.days.includes(day.dataset.day) ? f.days.filter(d => d !== day.dataset.day) : [...f.days, day.dataset.day];
      f.date = "";
      updateHeatRoom(e.target.closest(".heat-sched-form").dataset.room, true);
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
  $("#hs-provider")?.addEventListener("change", () => {
    // Modell auf den Default des neuen Anbieters setzen, wenn noch das des anderen eingetragen ist
    const defs = HS_ASSIST.default_models || {};
    const m = $("#hs-model").value.trim();
    if (!m || Object.values(defs).includes(m)) $("#hs-model").value = defs[$("#hs-provider").value] || "";
    _hsKeyNote();
  });
  $("#heat-chat-form")?.addEventListener("submit", e => {
    e.preventDefault();
    const inp = $("#heat-chat-input");
    const t = inp.value.trim();
    inp.value = "";
    chatSend(t);
  });
  $("#heat-chat")?.addEventListener("click", e => {
    const a = e.target.closest("[data-chat-apply]");
    if (a) { a.disabled = true; chatApply(a.dataset.chatApply); return; }
    if (e.target.closest("[data-chat-cancel]")) {
      HEAT.chat.proposal = null; HEAT.chat.history = []; _renderProposal({});
      _chatAdd("assistant", "Verworfen – nichts gespeichert.", "info");
    }
  });
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
