// ── Heizung: openHAB-Thermostate (Tab "Heizung" + Settings-Block) ─────────────
// Nutzt $, $$, api, escapeHtml aus app.js.

const HEAT = {
  status: null,
  pending: {},          // room_id → {value, timer}  (−/+ wird gesammelt und nach kurzer Pause gesendet)
  history: {},          // room_id → {temp, setpoint, t}
  sched: [],            // alle Zeitpläne (GET /api/heating/schedules)
  sun: {},              // heute: {sunrise, sunset}
  sun2: {},             // morgen
  form: {},             // room_id → Eingaben im Zeitplan-Sheet (überleben das 30-s-Neuzeichnen)
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

// Ein Chat für Heizung + Rollläden: eine Leiste unten auf allen Tabs. Der Status kommt mit dem Heizungs-/
// Rollladen-Status; auf den anderen Tabs (und beim Start) über den schlanken Endpoint ohne openHAB-Abfrage.
async function loadChatStatus() {
  try { renderChatStatus(await api("/api/heating/chat/status")); } catch (_) { /* Leiste bleibt, wie sie ist */ }
}

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
    if (sch) { HEAT.sched = sch.items; HEAT.sun = sch.sun_today || {}; HEAT.sun2 = sch.sun_tomorrow || {}; }
    renderChatStatus(s.chat);
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Heizung ist nicht aktiv. In den Settings unter „Heizung“ Räume eintragen und aktivieren.";
      _setSlot($("#heat-head"), "");
      $("#heat-updated").textContent = "";
      $("#heat-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      HEAT.roomsSig = null;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    _renderHeatHead(s);
    renderHeatRooms(s);
    renderSheet();                                   // offenes Sheet (Zeitpläne/Verlauf) bleibt offen und aktuell
    const stand = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#heat-updated").textContent = "Stand " + stand;
    pillTimes.heating = stand;                       // Kopfzeilen-Pill zeigt auf dem Heizung-Tab diese Zeit
    updatePillTime();
    _loadSparks(s.rooms);
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

// Schalter „Zeitpläne an/aus“ in der Kopfzeile (Heizung + Rollläden)
function _headSwitch(on, what) {
  return `<label class="head-switch" title="${_hq(what)} ${on ? "werden ausgeführt" : "werden gespeichert, aber nicht ausgeführt"}">
      <span>Zeitpläne</span><span class="heat-switch"><input type="checkbox" data-sched-master ${on ? "checked" : ""} aria-label="${_hq(what)} aktiv"><span></span></span>
      <small class="${on ? "" : "off"}">${on ? "an" : "aus"}</small></label>`;
}

// Kopfzeile: Ø-Temperatur · wie viele Räume heizen · nächste Änderung · Zeitpläne-Schalter
function _renderHeatHead(s) {
  const temps = s.rooms.filter(r => r.temp != null).map(r => r.temp);
  const parts = [];
  if (temps.length) parts.push(`Ø ${fmtC(temps.reduce((a, b) => a + b, 0) / temps.length)}`);
  if (s.rooms.some(r => r.has.valve)) {
    const n = s.rooms.filter(r => r.has.valve && r.valve > 0).length;
    parts.push(n === 1 ? "1 Raum heizt" : `${n} Räume heizen`);
  }
  const nx = s.rooms.filter(r => r.next).sort((a, b) => new Date(a.next.at) - new Date(b.next.at))[0];
  if (nx) parts.push(`nächste: ${escapeHtml(nx.name)} ${_fmtNext(nx.next)} → ${fmtC(nx.next.setpoint)}`);
  _setSlot($("#heat-head"), `<div class="tab-head-main"><h1>Heizung</h1><span class="tab-head-sum">${parts.join('<span class="sep">·</span>')}</span></div>
    <div class="tab-head-actions">${s.dry_run ? `<span class="cb warn" title="Dry-Run: Änderungen werden nur geloggt, nicht an openHAB gesendet">Dry-Run</span>` : ""}${_headSwitch(!!s.schedules_enabled, "Heizungs-Zeitpläne")}</div>`);
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
  if (!r.controllable) {                              // nur Anzeige (z. B. Waschküche): halbe Höhe, Ist + Feuchte
    return `
      <div class="card heat-room hcard mini" data-room="${id}">
        <div class="hcard-head"><h2>${escapeHtml(r.name)}</h2><span class="card-badges" data-slot="badges"></span></div>
        <div class="hcard-main"><div class="hcard-temp"><span class="heat-temp" data-slot="temp"></span></div><div class="hcard-meta" data-slot="meta"></div></div>
        <div data-slot="err"></div>
      </div>`;
  }
  return `
      <div class="card heat-room hcard" data-room="${id}">
        <div class="hcard-head"><h2>${escapeHtml(r.name)}</h2><span class="card-badges" data-slot="badges"></span></div>
        <div class="hcard-main">
          <div class="hcard-temp"><span class="heat-temp" data-slot="temp"></span><small class="hcard-delta" data-slot="delta"></small></div>
          <div class="heat-set">
            <button type="button" class="heat-step" data-room="${id}" data-step="-1" aria-label="${_hq(r.name)} kälter">−</button>
            <span class="heat-set-val" data-slot="sp" title="Soll"></span>
            <button type="button" class="heat-step" data-room="${id}" data-step="1" aria-label="${_hq(r.name)} wärmer">+</button>
          </div>
        </div>
        <button type="button" class="hcard-spark" data-hist="${id}" data-slot="spark" aria-label="${_hq(r.name)}: Verlauf 24 h öffnen" title="Verlauf 24 h"></button>
        <div class="hcard-meta" data-slot="meta"></div>
        <div data-slot="alert"></div><div data-slot="err"></div>
        <button type="button" class="card-link" data-sched="${id}" data-slot="schedlink"></button>
      </div>`;
}

function _typingIn(el) {
  const a = document.activeElement;
  return !!a && el.contains(a) && a.matches(".heat-sched-form input:not([type=checkbox]):not([type=radio]), .heat-sched-form select, .heat-sched-form textarea");
}

function _setSlot(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}

const _fmtDelta = d => (d > 0 ? "+" : "−") + Math.abs(d).toFixed(1).replace(".", ",");

// Zeile am Fuß der Card: öffnet das Zeitplan-Sheet
function _schedLink(n, dormant = false) {
  return n ? `<span>☰ ${n} ${n === 1 ? "Zeitplan" : "Zeitpläne"}${dormant ? " · ruhen" : ""}</span><span class="chev">›</span>`
           : `<span>+ Zeitplan</span><span class="chev">›</span>`;
}

// Einen Raum in place aktualisieren (die Card selbst wird nie neu aufgebaut).
function updateHeatRoom(roomId) {
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
  const dl = slot("delta");
  if (dl) {                                          // Abweichung als Text statt eingefärbter Zahl
    const d = r.temp != null && r.setpoint != null ? r.temp - r.setpoint : 0;
    const txt = Math.abs(d) >= 0.5 ? `${_fmtDelta(d)} ${d > 0 ? "über" : "unter"} Soll` : "";
    if (dl.textContent !== txt) dl.textContent = txt;
  }
  const v = slot("sp");
  if (v) {                                           // −/+ gesammelt: angezeigter Wert bleibt der eigene
    if (v.textContent !== fmtC(sp)) v.textContent = fmtC(sp);
    v.classList.toggle("pending", !!pend);
    card.querySelectorAll(".heat-step").forEach(b => { b.disabled = sp == null; });
  }
  // Badges oben rechts: nur Warnungen in Warn-Orange (Batterie schwach, AUTO-Modus, Hinweis)
  const badges = [];
  if (r.alert) badges.push(`<span class="cb warn" title="${_hq(r.alert)}">⚠ Hinweis</span>`);
  if (r.battery?.low) badges.push(`<span class="cb warn" title="Batterie schwach">⚠ Batterie</span>`);
  else if (r.battery) badges.push(`<span class="cb plain" title="Batterie OK">🔋${r.battery.percent != null ? " " + r.battery.percent + " %" : ""}</span>`);
  if (r.mode === "AUTO") badges.push(`<span class="cb warn" title="Gerät folgt eigenem Wochenprofil">AUTO</span>`);
  else if (r.mode != null) badges.push(`<span class="cb">${_hq(r.mode)}</span>`);
  _setSlot(slot("badges"), badges.join(""));
  const meta = [
    r.has.humidity ? `<span title="Luftfeuchte">💧 ${fmtPct(r.humidity)}</span>` : "",
    r.has.valve ? `<span title="Ventil">Ventil ${fmtPct(r.valve)}</span>` : "",
    r.next ? `<span title="Nächste Änderung">⏱ ${_fmtNext(r.next)} → ${fmtC(r.next.setpoint)}${s.schedules_enabled ? "" : ` <span class="heat-off">(aus)</span>`}</span>` : "",
  ].filter(Boolean).join("");
  _setSlot(slot("meta"), meta);
  _setSlot(slot("alert"), r.alert ? `<div class="heat-alert">${escapeHtml(r.alert)}</div>` : "");
  _setSlot(slot("err"), r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : "");
  _setSlot(slot("spark"), _sparkSvg(r.id));
  _setSlot(slot("schedlink"), _schedLink(HEAT.sched.filter(e => e.room_id === r.id).length));
}

// ── Sheet (Handy: von unten, Desktop: Modal 480 px) ─────────────────────────
// Ein Element für Zeitpläne (Heizung/Rollläden) und den großen Verlauf. Bleibt beim Poll offen;
// neu gezeichnet wird nur, wenn sich der Inhalt geändert hat und gerade nicht getippt wird.

const SHEET = { kind: null, id: null, form: false };
const SHEET_VIEWS = {};                               // kind → id => {title, html} (null = schließen)
const SCHED = {};                                     // "heat" / "shut" → Zugriff auf Zeitpläne des Tabs

function _sheetRoot() {
  let el = $("#sheet");
  if (!el) {
    el = document.createElement("div");
    el.id = "sheet"; el.className = "sheet hidden";
    el.innerHTML = `<div class="sheet-backdrop" data-sheet-close="1"></div>
      <div class="sheet-panel" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
        <div class="sheet-head"><h2 id="sheet-title"></h2><button type="button" class="sheet-x" data-sheet-close="1" aria-label="Schließen">✕</button></div>
        <div class="sheet-body" id="sheet-body"></div>
      </div>`;
    document.body.appendChild(el);
    _bindSheet(el);
  }
  return el;
}

function openSheet(kind, id, form = false) {
  SHEET.kind = kind; SHEET.id = id; SHEET.form = form;
  const el = _sheetRoot();
  el.classList.remove("hidden");
  document.body.classList.add("sheet-open");
  renderSheet(true);
}

function closeSheet() {
  SHEET.kind = null;
  $("#sheet")?.classList.add("hidden");
  document.body.classList.remove("sheet-open");
}

function renderSheet(force = false) {
  if (!SHEET.kind) return;
  const body = $("#sheet-body");
  if (!body || (!force && _typingIn(body))) return;
  const v = SHEET_VIEWS[SHEET.kind]?.(SHEET.id);
  if (!v) { closeSheet(); return; }
  const t = $("#sheet-title");
  if (t.textContent !== v.title) t.textContent = v.title;
  _setSlot(body, v.html);
}

const _schedDom = () => SCHED[{ hsched: "heat", ssched: "shut" }[SHEET.kind]];

// ── Chat ───────────────────────────────────────────────────────────────────

function renderChatStatus(st) {
  HEAT.chat.status = st;
  const bar = $("#heat-chat");
  if (!bar) return;
  const on = !!(st && st.enabled);                   // Chat in den Settings aus → Leiste überall ausgeblendet
  bar.classList.toggle("hidden", !on);
  document.documentElement.classList.toggle("has-chatbar", on);   // Platz unter dem Inhalt, damit nichts verdeckt wird
  const ready = !!(st && st.ready);
  const inp = $("#heat-chat-input");
  inp.disabled = !ready || HEAT.chat.busy;
  $("#heat-chat-send").disabled = !ready || HEAT.chat.busy;
  bar.classList.toggle("disabled", !ready);
  const ph = st?.hint || "z. B. „morgen Bad um 7 auf 21 Grad“ oder „Schlafzimmer jetzt auf 50 %“";
  if (inp.placeholder !== ph) inp.placeholder = ph;
  const count = st && st.enabled ? `${st.used_today}/${st.limit}` : "";
  const cEl = $("#heat-chat-count");
  if (cEl.textContent !== count) cEl.textContent = count;
  cEl.title = st && st.enabled ? `${st.provider_name ? st.provider_name + " · " : ""}${st.used_today} von ${st.limit} Anfragen heute` +
    (st.warning ? `\n⚠ ${st.warning}` : "") : "";
  cEl.classList.toggle("warn", !!(st && st.enabled && st.warning));   // Modell eingestellt → Ersatzmodell aktiv
}

// Antwort/Vorschau als Sheet über der Eingabezeile (Handy: Bottom-Sheet, Desktop: Popover)
function _chatPop(open) {
  $("#heat-chat-pop")?.classList.toggle("hidden", !open);
}

function _chatAdd(role, text, cls = "") {
  const log = $("#heat-chat-log");
  const div = document.createElement("div");
  div.className = `heat-msg ${role}${cls ? " " + cls : ""}`;
  div.textContent = text;
  log.appendChild(div);
  _chatPop(true);
  log.scrollTop = log.scrollHeight;
}

// Warum-Antwort: Text wie gewohnt, darunter klein aufklappbar die Fakten, die das Modell bekommen hat
function _chatAddExplain(text, basis) {
  const log = $("#heat-chat-log");
  const div = document.createElement("div");
  div.className = "heat-msg assistant explain";
  const p = document.createElement("div");
  p.textContent = text;
  div.appendChild(p);
  if (Array.isArray(basis) && basis.length) {
    const det = document.createElement("details");
    det.className = "chat-basis";
    const sum = document.createElement("summary");
    sum.textContent = "Grundlage";
    det.appendChild(sum);
    for (const sec of basis) {
      const h = document.createElement("div");
      h.className = "chat-basis-title";
      h.textContent = sec.title;
      det.appendChild(h);
      const ul = document.createElement("ul");
      for (const line of sec.lines || []) {
        const li = document.createElement("li");
        li.textContent = String(line).replace(/^- /, "");
        ul.appendChild(li);
      }
      det.appendChild(ul);
    }
    div.appendChild(det);
  }
  log.appendChild(div);
  _chatPop(true);
  log.scrollTop = log.scrollHeight;
}

function _fmtSp(v) { return String(v).replace(".", ",") + " °C"; }

function _shutPreview(sp, res) {
  if (!sp) return "";
  const warn = w => w ? `<span class="heat-prev-warnmark" title="${_hq(w)}">⚠️ ${escapeHtml(w)}</span>` : "";
  const groups = sp.groups.map(g => `<div class="heat-prev-group"><div class="heat-prev-label">🪟 Rollläden · ${escapeHtml(g.label)}</div>
      ${g.rows.map(r => `<div class="heat-prev-row shut-prev-row"><span>${escapeHtml(r.shutter)} ${escapeHtml(r.target)}</span><span class="shut-prev-when">${escapeHtml(r.when)}${r.at ? ` → ${escapeHtml(r.at)}` : ""}</span>${warn(r.warn)}</div>`).join("")}</div>`).join("");
  const imm = sp.immediate.length ? `<div class="heat-prev-group"><div class="heat-prev-label">🪟 Rollläden · Sofort</div>
      ${sp.immediate.map(i => `<div class="heat-prev-row"><span>jetzt</span><span>${escapeHtml(i.shutter)} ${escapeHtml(i.target)}${warn(i.warn)}</span></div>`).join("")}</div>` : "";
  const del = sp.deletes.length ? `<div class="heat-prev-group del"><div class="heat-prev-label">🪟 Rollläden · Löschen</div>
      ${sp.deletes.map(d => `<div class="heat-prev-row"><span>${escapeHtml(d.when)}</span><span>${escapeHtml(d.shutter)} ${escapeHtml(d.target)} · ${escapeHtml(d.days)}</span></div>`).join("")}</div>` : "";
  const dec = sp.declined.map(t => `<p class="heat-prev-info">ℹ️ ${escapeHtml(t)}</p>`).join("");
  const off = sp.groups.length && !res.shutter_schedules_enabled
    ? `<p class="heat-prev-warn">„Rollladen-Zeitpläne aktiv“ ist aus: die Pläne werden gespeichert, aber nicht ausgeführt.</p>` : "";
  const dry = sp.immediate.length && res.dry_run ? `<p class="heat-prev-warn">Dry-Run: Rollläden fahren nicht, nur Log.</p>` : "";
  return groups + imm + del + dec + off + dry;
}

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
      ${groups}${imm}${del}${warn}${_shutPreview(p.shutters, res)}
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
  c.busy = true; renderChatStatus(c.status);
  const wait = document.createElement("div");
  wait.className = "heat-msg assistant pending"; wait.textContent = "…";
  $("#heat-chat-log").appendChild(wait);
  _chatPop(true);
  try {
    const res = await api("/api/heating/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: c.history }) });
    wait.remove();
    if (res.chat) c.status = res.chat;
    if (res.fallback) _chatAdd("assistant", "⚠ " + res.fallback, "info");
    if (res.type === "question") {
      _chatAdd("assistant", res.text);
      c.history.push({ role: "assistant", content: res.text });
    } else if (res.type === "explain") {                 // reine Warum-Frage: nur Text, nichts zu bestätigen
      _chatAddExplain(res.text, res.basis);
      c.history.push({ role: "assistant", content: res.text });   // Nachfragen („und um 15 Uhr?“) möglich
    } else if (res.type === "proposal") {
      if (res.explanation) _chatAddExplain(res.explanation, res.basis);   // Mischfrage: erst die Erklärung
      _chatAdd("assistant", "Vorschlag – bitte prüfen:");
      c.proposal = res.proposal; _renderProposal(res);
      const lg = $("#heat-chat-log"); lg.scrollTop = lg.scrollHeight;   // Vorschau verkleinert den Verlauf: ans Ende
    } else {
      const cls = { error: "err", invalid: "err", limit: "info", unavailable: "info", answer: "" }[res.type] ?? "";
      _chatAdd("assistant", res.text, cls);
      c.history = [];
    }
  } catch (e) {
    wait.remove();
    _chatAdd("assistant", "Fehler: " + e.message, "err");
  } finally {
    c.busy = false; renderChatStatus(c.status);
    $("#heat-chat-input").focus();
  }
}

function _chatErr(e) {                                 // api() wirft "409 Conflict — {\"detail\": …}"
  const m = /— (.*)$/s.exec(e.message);
  try { return JSON.parse(m[1]).detail || e.message; } catch (_) { return e.message; }
}

async function chatApply(id) {
  const c = HEAT.chat;
  const doors = c.proposal?.shutters?.door_confirm || [];
  // gleiche Regel wie bei den Buttons: Tür offen/ohne Kontakt → erst bestätigen
  if (doors.length && !confirm(doors.join("\n") + "\n\nTrotzdem schließen?")) {
    _chatAdd("assistant", "Nicht übernommen – Vorschlag bleibt stehen.", "info");
    const b = $("#heat-chat-preview [data-chat-apply]"); if (b) b.disabled = false;
    return;
  }
  const send = confirm_doors => api("/api/heating/chat/apply", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proposal_id: id, confirm_doors }) });
  try {
    let r;
    try { r = await send(doors.length > 0); } catch (e) {
      const why = _chatErr(e);
      if (!doors.length && /Bestätigung/.test(why) && confirm(why.replace(/ – Schließen nur mit Bestätigung\.?/, "") + "\n\nTrotzdem schließen?")) r = await send(true);
      else throw e;
    }
    const parts = [];
    if (r.created.length) parts.push(`${r.created.length} ${r.created.length > 1 ? "Zeitpläne" : "Zeitplan"} angelegt`);
    if (r.deleted.length) parts.push(`${r.deleted.length} gelöscht`);
    if (r.immediate.length) parts.push(`${r.immediate.filter(x => x.ok).length}/${r.immediate.length} sofort gesetzt`);
    if (r.shutter_created?.length) parts.push(`${r.shutter_created.length} Rollladen-${r.shutter_created.length > 1 ? "Zeitpläne" : "Zeitplan"} angelegt`);
    if (r.shutter_deleted?.length) parts.push(`${r.shutter_deleted.length} Rollladen-Zeitplan gelöscht`);
    if (r.shutter_immediate?.length) parts.push(`${r.shutter_immediate.filter(x => x.ok).length}/${r.shutter_immediate.length} Rollläden ${r.shutter_immediate.some(x => x.dry_run) ? "(Dry-Run) " : ""}gefahren`);
    _chatAdd("assistant", "✓ Übernommen: " + (parts.join(", ") || "nichts zu tun"), r.ok ? "ok" : "err");
  } catch (e) {
    _chatAdd("assistant", "Nicht übernommen: " + _chatErr(e), "err");
  }
  c.proposal = null; c.history = []; _renderProposal({});
  const tab = $(".tab.active")?.dataset.tab;           // Anzeige nur auf dem Tab nachladen, der gerade offen ist
  if (tab === "rolllaeden") { loadShutters(); setTimeout(loadShutters, 3000); }
  else if (tab === "heizung") loadHeating();
  else loadChatStatus();
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

// "heute 18:22" bzw. mit Zeittyp kurz: "heute ☀↓ −15 min (18:22)"
function _fmtNext(n) {
  const at = _fmtWhen(n.at);
  if (!n.when || /^\d\d:\d\d$/.test(n.when)) return at;
  const i = at.lastIndexOf(" ");
  return `${escapeHtml(at.slice(0, i))} ${escapeHtml(_whenShort(n.when).replace(/ \(.*$/, ""))} (${at.slice(i + 1)})`;
}

// Zeittyp + berechnete Uhrzeit für heute: "Sonnenuntergang −15 min (18:22)"
function _whenToday(e) {
  return escapeHtml(e.when) + (e.at_today ? ` <span class="sched-at">(${_hq(e.at_today)})</span>` : "");
}

// Gemeinsamer Formularteil „Wann“ (Heizung + Rollläden): Segmented Control Uhrzeit | Sonnenaufgang |
// Sonnenuntergang; je nach Typ Uhrzeit-Feld oder ±Minuten-Stepper, „nicht vor / nicht nach“ unter „Grenzen“.
// attr = data-Attribut der Felder ("f" bzw. "sf").
const SCHED_KINDS = [["time", "Uhrzeit"], ["sunrise", "Sonnenaufgang"], ["sunset", "Sonnenuntergang"]];
function _whenCalc(f, sun) {                         // Vorschau der Uhrzeit für heute (Server rechnet genau)
  const base = f.kind !== "time" && sun && sun[f.kind] ? sun[f.kind] : null;
  if (!base) return null;
  const [h, m] = base.split(":").map(Number);
  let t = h * 60 + m + (Math.round(Number(f.offset)) || 0);
  const lim = v => { const [a, b] = v.split(":").map(Number); return a * 60 + b; };
  if (f.not_before) t = Math.max(t, lim(f.not_before));
  if (f.not_after) t = Math.min(t, lim(f.not_after));
  t = ((t % 1440) + 1440) % 1440;
  return { at: `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`, base };
}
function _whenFields(f, attr, name, sun) {
  const isSun = f.kind !== "time";
  const c = _whenCalc(f, sun);
  const calc = c ? `heute ${c.at} (${f.kind === "sunrise" ? "Aufgang" : "Untergang"} ${c.base})` : "";
  return `<div class="seg sched-kind" role="radiogroup" aria-label="Zeitart">
      ${SCHED_KINDS.map(([v, l]) => `<label class="seg-opt"><input type="radio" name="${_hq(name)}" data-${attr}="kind" value="${v}"${f.kind === v ? " checked" : ""}><span>${l}</span></label>`).join("")}
    </div>
    ${isSun ? `<div class="heat-form-row">
        <label>± Minuten<span class="stepper"><button type="button" data-off-step="-5" aria-label="5 Minuten früher">−</button><input type="number" data-${attr}="offset" step="5" min="-180" max="180" value="${_hq(f.offset)}" inputmode="numeric"><button type="button" data-off-step="5" aria-label="5 Minuten später">+</button></span></label>
        <small class="note" data-calc="1">${_hq(calc)}</small>
      </div>
      <details class="sch-limits"${f.limits || f.not_before || f.not_after ? " open" : ""}><summary>Grenzen</summary>
        <div class="heat-form-row">
          <label>nicht vor<input type="time" lang="de" data-${attr}="not_before" value="${_hq(f.not_before)}"></label>
          <label>nicht nach<input type="time" lang="de" data-${attr}="not_after" value="${_hq(f.not_after)}"></label>
        </div>
      </details>`
    : `<div class="heat-form-row"><label>Uhrzeit<input type="time" lang="de" data-${attr}="time" value="${_hq(f.time)}"></label></div>`}`;
}

// Formularwerte → Felder für die API
function _whenBody(f) {
  if (f.kind === "time") return { kind: "time", time: f.time };
  const b = { kind: f.kind, offset_min: Math.round(Number(f.offset) || 0) };
  if (f.not_before) b.not_before = f.not_before;
  if (f.not_after) b.not_after = f.not_after;
  return b;
}

// Vor dem Absenden: bei „Uhrzeit“ muss das Feld vollständig sein (leer/halb getippt liefert der Browser als "")
function _whenError(f) {
  return f.kind === "time" && !/^\d\d:\d\d$/.test(f.time || "") ? "Bitte eine Uhrzeit eintragen (Stunde und Minute)." : "";
}

// Eintrag → Formularwerte (Bearbeiten)
function _whenFromEntry(e) {
  return { kind: e.kind || "time", time: e.time || "", offset: e.offset_min || 0, not_before: e.not_before || "",
           not_after: e.not_after || "", days: [...(e.days || [])], date: e.date || "", editId: e.id };
}

function _schedForm(roomId, r) {
  const f = HEAT.form[roomId] ||= { kind: "time", time: "", offset: 0, not_before: "", not_after: "",
                                    setpoint: r?.setpoint ?? 20, days: [], date: "", once: false, editId: null };
  return f;
}

// "Mo–Fr", "täglich", "Mo, Mi, Fr", "einmalig Do 16.10."
function _daysText(e) {
  if (e.date) return _fmtDate(e.date);
  const idx = HEAT_DAYS.map(([k], i) => (e.days || []).includes(k) ? i : -1).filter(i => i >= 0);
  if (!idx.length) return "kein Tag";
  if (idx.length === 7) return "täglich";
  const runs = [];
  for (const i of idx) { const l = runs[runs.length - 1]; if (l && l[1] === i - 1) l[1] = i; else runs.push([i, i]); }
  const L = i => HEAT_DAYS[i][1];
  return runs.map(([a, b]) => a === b ? L(a) : b === a + 1 ? `${L(a)}, ${L(b)}` : `${L(a)}–${L(b)}`).join(", ");
}

// "Sonnenuntergang −15 min (nicht nach 22:00)" → "☀↓ −15 min (nicht nach 22:00)"
const _whenShort = w => String(w || "").replace("Sonnenuntergang", "☀↓").replace("Sonnenaufgang", "☀↑");

// Live-Vorschau unter dem Formular: "Mo–Fr bei Sonnenuntergang −15 min → heute 18:22, morgen 18:20 · → 21,0 °C"
function _schedPreviewText(D, f, it) {
  const days = f.once ? (f.date ? _fmtDate(f.date) : "einmalig (Datum fehlt)") : _daysText({ days: f.days });
  const off = Math.round(Number(f.offset)) || 0;
  const c = _whenCalc(f, D.sun()), c2 = _whenCalc(f, D.sun2());      // Sonnenzeiten heute/morgen vom Server (suntime.py)
  const at = [c ? `heute ${c.at}` : "", c2 ? `morgen ${c2.at}` : ""].filter(Boolean).join(", ");
  const when = f.kind === "time" ? (f.time ? `um ${f.time}` : "Uhrzeit fehlt")
    : `bei ${f.kind === "sunrise" ? "Sonnenaufgang" : "Sonnenuntergang"}${off ? ` ${off > 0 ? "+" : "−"}${Math.abs(off)} min` : ""}${at ? ` → ${at}` : ""}`;
  return `${days} ${when} · ${D.formTarget(f, it)}`;
}

// Zeitplan-Sheet: kompakte Liste + „+ Zeitplan“ → Formular im selben Sheet
function _schedSheet(D, id) {
  const it = D.item(id);
  if (!it) return null;
  const f = D.form(id, it);
  const list = D.list(id).sort((a, b) => (a.date || "").localeCompare(b.date || "") || (a.time || a.kind).localeCompare(b.time || b.kind));
  const rows = list.map(e => {
    const last = e.last_run ? `<small class="heat-last" title="${_hq(e.last_message || e.last_run)}">zuletzt ${_hq(e.last_run.slice(5, 16).replace("T", " "))} · ${D.result[e.last_result] || _hq(e.last_result || "")}</small>` : "";
    const sun = e.kind && e.kind !== "time";
    return `<div class="sch-row${e.enabled ? "" : " off"}${SHEET.form && f.editId === e.id ? " editing" : ""}">
        <div class="sch-main"><span class="sch-days">${_hq(_daysText(e))}</span><span class="sep">·</span><span class="sch-when">${_hq(_whenShort(e.when))}${sun && e.at_today ? ` <span class="sched-at">(${_hq(e.at_today)})</span>` : ""}</span><span class="sep">·</span><span class="sch-target">${D.target(e)}</span>${last}</div>
        <label class="heat-switch" title="${e.enabled ? "aktiv" : "inaktiv"}"><input type="checkbox" data-sched-toggle="${e.id}" ${e.enabled ? "checked" : ""} aria-label="Zeitplan aktiv"><span></span></label>
        <button type="button" class="sch-btn" data-sched-edit="${e.id}" aria-label="Zeitplan bearbeiten" title="bearbeiten">✎</button>
        <button type="button" class="sch-btn del" data-sched-del="${e.id}" aria-label="Zeitplan löschen" title="löschen">🗑</button>
      </div>`;
  }).join("") || `<p class="note">Noch keine Zeitpläne.</p>`;
  const wk = "mo,di,mi,do,fr", all = "mo,di,mi,do,fr,sa,so";
  const cur = HEAT_DAYS.map(x => x[0]).filter(k => f.days.includes(k)).join(",");
  const form = !SHEET.form ? `<button type="button" class="btn sch-add" data-form-open="1">+ Zeitplan</button>`
    : `<div class="heat-sched-form" data-owner="${_hq(id)}">
        <div class="sched-editing">${f.editId ? "Zeitplan bearbeiten" : "Neuer Zeitplan"}</div>
        ${_whenFields(f, D.attr, D.attr + "kind-" + id, D.sun())}
        ${D.targetFields(f, it)}
        <div class="seg seg-days" role="group" aria-label="Tage">
          <button type="button" class="seg-btn${!f.once && cur === wk ? " on" : ""}" data-days="${wk}">Werktags</button>
          <button type="button" class="seg-btn${!f.once && cur === all ? " on" : ""}" data-days="${all}">Täglich</button>
          <button type="button" class="seg-btn${f.once ? " on" : ""}" data-once="1">Einmalig</button>
        </div>
        ${f.once ? `<div class="heat-form-row"><label>Datum<input type="date" lang="de" data-${D.attr}="date" value="${_hq(f.date)}"></label></div>`
          : `<div class="heat-chips pick">${HEAT_DAYS.map(([k, l]) => `<button type="button" class="heat-chip${f.days.includes(k) ? " on" : ""}" data-day="${k}" aria-pressed="${f.days.includes(k)}">${l}</button>`).join("")}</div>`}
        <div class="sch-preview" data-preview="1">${_hq(_schedPreviewText(D, f, it))}</div>
        <div class="sheet-actions">
          <button type="button" class="btn primary" data-sched-add="1">${f.editId ? "Speichern" : "Zeitplan anlegen"}</button>
          <button type="button" class="btn" data-sched-cancel="1">Abbrechen</button>
        </div>
        <span class="form-msg" id="sched-msg"></span>
      </div>`;
  return { title: `Zeitpläne · ${it.name}`, html: `${D.banner(it)}<div class="sch-list">${rows}</div>${form}` };
}

SCHED.heat = {
  attr: "f",
  item: id => HEAT.status?.rooms.find(r => r.id === id && r.controllable),
  form: (id, r) => _schedForm(id, r),
  store: () => HEAT.form,
  list: id => HEAT.sched.filter(e => e.room_id === id),
  find: sid => HEAT.sched.find(e => e.id === sid),
  owner: e => e.room_id,
  fromEntry: e => ({ ..._whenFromEntry(e), once: !!e.date, setpoint: e.setpoint }),
  sun: () => HEAT.sun,
  sun2: () => HEAT.sun2,
  result: _RESULT,
  banner: () => "",
  target: e => `→ ${fmtC(e.setpoint)}`,
  formTarget: f => `→ ${fmtC(Number(f.setpoint))}`,
  targetFields: f => {
    const s = HEAT.status;
    return `<div class="heat-form-row"><label>Soll (°C)<span class="stepper"><button type="button" data-sp-step="-1" aria-label="kälter">−</button><input type="number" data-f="setpoint" step="${s.setpoint_step}" min="${s.setpoint_min}" max="${s.setpoint_max}" value="${_hq(f.setpoint)}" inputmode="decimal"><button type="button" data-sp-step="1" aria-label="wärmer">+</button></span></label></div>`;
  },
  save: id => _schedAdd(id),
  toggle: (sid, on) => _schedToggle(sid, on),
  del: sid => _schedDelete(sid),
};
SHEET_VIEWS.hsched = id => _schedSheet(SCHED.heat, id);

// Meldung im offenen Formular (Fehler vom Server oder fehlende Eingabe)
function _schedMsg(text) {
  const msg = $("#sched-msg");
  if (msg) { msg.textContent = text; msg.className = "form-msg error"; }
}

// Tage/Datum aus dem Formular; liefert null und zeigt den Hinweis, wenn etwas fehlt
function _schedDaysBody(f) {
  if (f.once) {
    if (!f.date) { _schedMsg("Bitte ein Datum wählen."); return null; }
    return { date: f.date };
  }
  if (!f.days.length) { _schedMsg("Bitte mindestens einen Wochentag wählen."); return null; }
  return { days: f.days };
}

async function _schedAdd(roomId) {
  const f = HEAT.form[roomId];
  const err = _whenError(f);
  if (err) { _schedMsg(err); return; }
  const when = _schedDaysBody(f);
  if (!when) return;
  const body = { room_id: roomId, setpoint: Number(f.setpoint), ..._whenBody(f), ...when };
  try {
    await api(f.editId ? `/api/heating/schedules/${f.editId}` : "/api/heating/schedules",
      { method: f.editId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    HEAT.form[roomId] = { ...f, time: "", date: "", once: false, offset: 0, not_before: "", not_after: "", editId: null };
    SHEET.form = false;
    document.activeElement?.blur();
    await loadHeating();
  } catch (e) {
    _schedMsg(_chatErr(e));
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
  if (!confirm(`Zeitplan ${e ? e.when + " → " + fmtC(e.setpoint) : id} löschen?`)) return;
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
  if (!force && h && Date.now() - h.t < HEAT_HISTORY_TTL_MS) return;
  try {
    const d = await api(`/api/heating/history/${encodeURIComponent(roomId)}?hours=24`);
    HEAT.history[roomId] = { temp: d.temp || [], setpoint: d.setpoint || [], t: Date.now(), error: d.error };
  } catch (e) {
    HEAT.history[roomId] = { temp: [], setpoint: [], t: Date.now(), error: e.message };
  }
  updateHeatRoom(roomId);
  if (SHEET.kind === "hist" && SHEET.id === roomId) renderSheet(true);
}

// Sparklines: 24-h-Verlauf je Raum nacheinander laden (höchstens alle 5 min, nur solange der Tab sichtbar ist)
async function _loadSparks(rooms) {
  if (HEAT.sparkBusy) return;
  HEAT.sparkBusy = true;
  try {
    for (const r of rooms) {
      if (!r.controllable || !_heatVisible()) continue;
      await loadHeatHistory(r.id);
    }
  } finally { HEAT.sparkBusy = false; }
}

function _histScale(h, W, H, pad) {
  const all = [...h.temp, ...h.setpoint];
  const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
  let lo = Math.floor(Math.min(...all.map(p => p[1])) - 0.5), hi = Math.ceil(Math.max(...all.map(p => p[1])) + 0.5);
  if (hi - lo < 4) { const m = (hi + lo) / 2; lo = Math.floor(m - 2); hi = Math.ceil(m + 2); }
  const x = t => pad.l + (t - t0) / Math.max(1, t1 - t0) * (W - pad.l - pad.r);
  const y = v => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);
  const line = pts => pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const step = pts => pts.map((p, i) => i ? `H${x(p[0]).toFixed(1)}V${y(p[1]).toFixed(1)}` : `M${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("")
    + (pts.length ? `H${x(t1).toFixed(1)}` : "");
  return { t0, t1, lo, hi, x, y, line, step };
}

// Sparkline in der Card: Ist als Linie, Soll fein gestrichelt, 40 px hoch, ohne Achsen
function _sparkSvg(roomId) {
  const h = HEAT.history[roomId];
  if (!h || !h.temp.length) return `<span class="spark-empty">${h ? "kein Verlauf" : ""}</span>`;
  const W = 300, H = 40;
  const sc = _histScale(h, W, H, { l: 1, r: 1, t: 4, b: 4 });
  return `<svg viewBox="0 0 ${W} ${H}" class="spark" preserveAspectRatio="none" aria-hidden="true">
    ${h.setpoint.length ? `<path d="${sc.step(h.setpoint)}" class="sp-set"/>` : ""}<path d="${sc.line(h.temp)}" class="sp-temp"/></svg>`;
}

// Großer Verlauf im Sheet: in echten Pixeln gezeichnet (Breite des Sheets, 200 px hoch, Schrift 11,5 px)
function _heatChartSvg(roomId, W, H = 200) {
  const h = HEAT.history[roomId];
  if (!h) return `<div class="chart-empty">Lade…</div>`;
  if (!h.temp.length && !h.setpoint.length) return `<div class="chart-empty">${h.error ? escapeHtml(h.error) : "Keine Verlaufsdaten"}</div>`;
  const pad = { l: 36, r: 8, t: 10, b: 24 };
  const { t0, t1, lo, hi, x, y, line, step } = _histScale(h, W, H, pad);
  let g = "";
  const ticks = hi - lo <= 6 ? 1 : 2;
  for (let v = lo; v <= hi; v += ticks) {
    g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}" class="hc-grid"/>`;
    g += `<text x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end" class="hc-lbl">${v}°</text>`;
  }
  // Stundenmarken alle 6 h
  const d0 = new Date(t0); d0.setMinutes(0, 0, 0);
  for (let t = d0.getTime(); t <= t1; t += 3600_000) {
    const hr = new Date(t).getHours();
    if (t < t0 || hr % 6) continue;
    g += `<line x1="${x(t)}" x2="${x(t)}" y1="${pad.t}" y2="${H - pad.b}" class="hc-grid"/>`;
    g += `<text x="${x(t)}" y="${H - 7}" text-anchor="middle" class="hc-lbl">${String(hr).padStart(2, "0")}h</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" class="hist-svg" role="img" aria-label="Verlauf 24 h">
    ${g}
    ${h.setpoint.length ? `<path d="${step(h.setpoint)}" class="hc-set"/>` : ""}
    ${h.temp.length ? `<path d="${line(h.temp)}" class="hc-temp"/>` : ""}
  </svg>
  <div class="heat-legend"><span><i class="hc-temp-k"></i>Ist</span>${h.setpoint.length ? `<span><i class="hc-set-k"></i>Soll</span>` : ""}</div>`;
}

SHEET_VIEWS.hist = id => {
  const r = HEAT.status?.rooms.find(x => x.id === id);
  if (!r) return null;
  const W = Math.max(260, Math.round(($("#sheet-body")?.clientWidth || 360) - 36));
  return { title: `Verlauf 24 h · ${r.name}`, html: `<div class="hist-now"><span class="heat-temp">${fmtC(r.temp)}</span><span class="note">Soll ${fmtC(r.setpoint)}</span></div>${_heatChartSvg(id, W)}` };
};

// Zeitpläne an/aus in der Kopfzeile: gleicher Endpoint wie Settings → Heizung (PUT /api/heating/config)
async function _heatSchedMaster(on, box) {
  if (on && !confirm("Heizungs-Zeitpläne einschalten? Gespeicherte Pläne werden ab jetzt ausgeführt.")) { box.checked = false; return; }
  box.disabled = true;
  try {
    const c = await api("/api/heating/config");
    const h = c.heating, a = c.assistant || {};
    await api("/api/heating/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      enabled: !!h.enabled, schedules_enabled: on,
      assistant: { enabled: !!a.enabled, provider: a.provider || "anthropic", model: a.model || (a.default_models || {})[a.provider] || "",
                   max_requests_per_day: a.max_requests_per_day ?? 100 },
      setpoint_min: h.setpoint_min ?? 5, setpoint_max: h.setpoint_max ?? 26, rooms: h.rooms || [],
    }) });
  } catch (e) {
    alert("Zeitpläne nicht umgeschaltet: " + _chatErr(e));
  }
  $("#heat-head")._html = undefined;                 // Schalter von Hand umgestellt → neu zeichnen
  loadHeating();
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
    loadChatStatus();                                // „Chat aktiv“ geändert → Leiste sofort ein-/ausblenden
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
}

// berechnete Uhrzeit und Vorschau-Zeile aktualisieren, ohne das Formular neu zu zeichnen (Fokus bleibt)
function _refreshCalc(form, f, D, it) {
  const el = form.querySelector("[data-calc]");
  if (el) {
    const c = _whenCalc(f, D.sun());
    el.textContent = c ? `heute ${c.at} (${f.kind === "sunrise" ? "Aufgang" : "Untergang"} ${c.base})` : "";
  }
  const pv = form.querySelector("[data-preview]");
  if (pv) pv.textContent = _schedPreviewText(D, f, it);
}

// Sheet: ein Satz Handler für Zeitpläne beider Tabs (D = SCHED.heat / SCHED.shut) und den Verlauf
function _bindSheet(el) {
  el.addEventListener("click", e => {
    if (e.target.closest("[data-sheet-close]")) { closeSheet(); return; }
    const D = _schedDom(), id = SHEET.id;
    if (!D) return;
    const it = D.item(id), f = D.form(id, it);
    if (e.target.closest("[data-form-open]")) {
      Object.assign(f, { editId: null, time: "", date: "", once: false });
      SHEET.form = true; renderSheet(true); return;
    }
    const day = e.target.closest("[data-day]"), quick = e.target.closest("[data-days]");
    if (day || quick) {
      if (quick) f.days = quick.dataset.days.split(",");
      else f.days = f.days.includes(day.dataset.day) ? f.days.filter(d => d !== day.dataset.day) : [...f.days, day.dataset.day];
      f.once = false; f.date = "";
      renderSheet(true); return;
    }
    if (e.target.closest("[data-once]")) { f.once = true; f.days = []; renderSheet(true); return; }
    const os = e.target.closest("[data-off-step]");
    if (os) {                                          // ±5 min, Feld bleibt stehen (kein Neuzeichnen)
      f.offset = Math.max(-180, Math.min(180, (Math.round(Number(f.offset)) || 0) + Number(os.dataset.offStep)));
      const inp = os.parentElement.querySelector("input");
      inp.value = f.offset;
      _refreshCalc(os.closest(".heat-sched-form"), f, D, it); return;
    }
    const ss = e.target.closest("[data-sp-step]");
    if (ss) {
      const s = HEAT.status, st = s.setpoint_step;
      const v = Math.round(((Number(f.setpoint) || 0) + Number(ss.dataset.spStep) * st) / st) * st;
      f.setpoint = Math.min(s.setpoint_max, Math.max(s.setpoint_min, v));
      ss.parentElement.querySelector("input").value = f.setpoint;
      _refreshCalc(ss.closest(".heat-sched-form"), f, D, it); return;
    }
    if (e.target.closest("[data-sched-add]")) { D.save(id); return; }
    const ed = e.target.closest("[data-sched-edit]");
    if (ed) {                                          // Eintrag ins Formular laden
      const en = D.find(Number(ed.dataset.schedEdit));
      if (en) { D.store()[id] = D.fromEntry(en); SHEET.form = true; renderSheet(true); }
      return;
    }
    if (e.target.closest("[data-sched-cancel]")) {
      Object.assign(f, { editId: null, time: "", date: "", once: false });
      SHEET.form = false; renderSheet(true); return;
    }
    const del = e.target.closest("[data-sched-del]");
    if (del) D.del(Number(del.dataset.schedDel));
  });
  el.addEventListener("change", e => {
    const D = _schedDom(), id = SHEET.id;
    if (!D) return;
    const t = e.target.closest("[data-sched-toggle]");
    if (t) { D.toggle(Number(t.dataset.schedToggle), t.checked); return; }
    const k = e.target.closest(`.heat-sched-form input[type="radio"], .heat-sched-form select`);
    if (k) {                                           // Zeitart/Ziel gewechselt → andere Felder
      D.form(id, D.item(id))[k.dataset[D.attr]] = k.value;
      document.activeElement?.blur();
      renderSheet(true);
    }
  });
  el.addEventListener("input", e => {
    const D = _schedDom(), id = SHEET.id;
    if (!D) return;
    const inp = e.target.closest(`.heat-sched-form input[data-${D.attr}]`);
    if (!inp || inp.type === "radio") return;
    const it = D.item(id), f = D.form(id, it);
    f[inp.dataset[D.attr]] = inp.value;
    _refreshCalc(inp.closest(".heat-sched-form"), f, D, it);
  });
  // <details> „Grenzen“: Zustand merken, damit es beim Neuzeichnen offen bleibt (toggle steigt nicht auf)
  el.addEventListener("toggle", e => {
    const D = _schedDom();
    if (D && e.target.matches?.(".sch-limits")) D.form(SHEET.id, D.item(SHEET.id)).limits = e.target.open;
  }, true);
}

function bootHeating() {
  loadChatStatus();
  document.addEventListener("keydown", e => { if (e.key === "Escape" && SHEET.kind) closeSheet(); });
  $("#heat-rooms")?.addEventListener("click", e => {
    const b = e.target.closest("[data-step]");
    if (b) { heatStep(b.dataset.room, Number(b.dataset.step)); return; }
    const t = e.target.closest("[data-hist]");
    if (t) { openSheet("hist", t.dataset.hist); loadHeatHistory(t.dataset.hist); return; }
    const sc = e.target.closest("[data-sched]");
    if (sc) openSheet("hsched", sc.dataset.sched, !HEAT.sched.some(x => x.room_id === sc.dataset.sched));
  });
  $("#heat-head")?.addEventListener("change", e => {
    const m = e.target.closest("[data-sched-master]");
    if (m) _heatSchedMaster(m.checked, m);
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
    if (e.target.closest("[data-chat-close]")) { _chatPop(false); return; }
    if (e.target.closest("[data-chat-toggle]")) { _chatPop($("#heat-chat-pop").classList.contains("hidden")); return; }
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
