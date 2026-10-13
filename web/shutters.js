// ── Rollläden: openHAB-Rollershutter (Tab "Rollläden" + Settings-Block) ──────
// Nutzt $, $$, api, escapeHtml aus app.js und _hq, _setSlot aus heating.js.

const SHUT = {
  status: null,
  timer: null,
  roomsSig: null,       // Liste der gerenderten Cards (Neuaufbau nur bei Änderung)
  loadSeq: 0,
  drag: {},             // shutter_id → Slider wird gerade bedient (Poll überschreibt ihn nicht)
  busy: false,          // Gruppenbefehl läuft
  movingTimer: null,    // schnelles Nachladen während einer Fahrt
  sched: [],            // alle Zeitpläne (GET /api/shutters/schedules)
  sun: {},              // heute: {sunrise, sunset}
  schedOpen: {},        // shutter_id → Zeitpläne aufgeklappt
  form: {},             // shutter_id → Eingaben „+ Zeitplan“
};
const SHUT_POLL_MS = 30_000;
const SHUT_MOVING_POLL_MS = 3_000;      // solange ein Shelly fährt: Position alle 3 s
const SHUT_CMD = { up: "▲ Auf", down: "▼ Ab", stop: "■ Stop" };

const _shutVisible = () => !document.hidden && $(".tab.active")?.dataset.tab === "rolllaeden";

function shutActivate() {
  placeChat("rolllaeden");                              // aus heating.js: ein Chat für beide Tabs
  loadShutters();
  clearInterval(SHUT.timer);
  // alle 30 s; pausiert, solange ein anderer Tab aktiv oder die Seite verdeckt ist
  SHUT.timer = setInterval(() => {
    if (_shutVisible()) loadShutters();
    else if ($(".tab.active")?.dataset.tab !== "rolllaeden") { clearInterval(SHUT.timer); SHUT.timer = null; }
  }, SHUT_POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(SHUT.timer); SHUT.timer = null; }
  else if (_shutVisible()) shutActivate();
});

async function loadShutters() {
  const banner = $("#shut-banner");
  const seq = ++SHUT.loadSeq;
  try {
    const [s, sch] = await Promise.all([
      api("/api/shutters/status"),
      api("/api/shutters/schedules").catch(() => null),
    ]);
    if (seq !== SHUT.loadSeq) return;
    SHUT.status = s;
    if (sch) { SHUT.sched = sch.items; SHUT.sun = sch.sun_today || {}; }
    if (s.chat) renderChatStatus(s.chat, !!(s.enabled && s.openhab_configured));
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Rollläden sind nicht aktiv. In den Settings unter „Rollläden“ eintragen und aktivieren.";
      $("#shut-dry").classList.add("hidden");
      $("#shut-sched-off").classList.add("hidden");
      $("#shut-groups").innerHTML = "";
      $("#shut-updated").textContent = "";
      $("#shut-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      SHUT.roomsSig = null;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    $("#shut-dry").classList.toggle("hidden", !s.dry_run);
    $("#shut-sched-off").classList.toggle("hidden", !!s.schedules_enabled || !s.rooms.some(r => r.automation === "pvcontroller"));
    _renderShutGroups(s);
    renderShutRooms(s);
    const stand = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#shut-updated").textContent = "Stand " + stand;
    pillTimes.shutters = stand;
    updatePillTime();
    // fährt ein Rollladen (Shelly meldet open/close) → in 3 s erneut, bis er steht
    clearTimeout(SHUT.movingTimer);
    if (s.rooms.some(r => r.moving)) SHUT.movingTimer = setTimeout(() => { if (_shutVisible()) loadShutters(); }, SHUT_MOVING_POLL_MS);
  } catch (e) {
    banner.textContent = "Rollläden: " + e.message;
    banner.classList.remove("hidden");
  }
}

function _groupName(g) { return g.toUpperCase(); }

function _renderShutGroups(s) {
  const dc = s.duty_cycle;
  const dcHtml = dc ? `<span class="shut-dc${dc.high ? " high" : ""}" title="Funk-Auslastung der CCU (Homematic Duty Cycle)">
      Duty Cycle ${dc.value == null ? "–" : Math.round(dc.value) + " %"}${dc.high ? ` · über ${dc.max} %: Gruppenbefehle pausiert` : ""}</span>` : "";
  const btn = (g, cmd, label) => `<button type="button" class="btn shut-gbtn" data-group="${_hq(g)}" data-gcmd="${cmd}"
      ${SHUT.busy || dc?.high ? "disabled" : ""}>${label}</button>`;
  const rows = [["all", "Alle"], ...s.groups.map(g => [g, _groupName(g)])].map(([g, l]) =>
    `<div class="shut-grow"><span class="shut-glabel">${escapeHtml(l)}</span>${btn(g, "up", "▲ Auf")}${btn(g, "down", "▼ Zu")}</div>`).join("");
  _setSlot($("#shut-groups"), `<div class="card shut-gcard">
      <div class="heat-room-head"><h2>Gruppen</h2>${dcHtml}</div>
      ${rows}
      ${SHUT.busy ? `<p class="note">Sende gestaffelt (${String(s.command_spacing_s).replace(".", ",")} s Abstand)…</p>` : ""}
    </div>`);
}

function renderShutRooms(s) {
  const wrap = $("#shut-rooms");
  const sig = s.rooms.map(r => `${r.id}|${r.name}|${r.can_position}|${r.automation}`).join(";");
  if (sig !== SHUT.roomsSig || !wrap.querySelector(".shut-room")) {
    wrap.innerHTML = s.rooms.map(_shutSkeleton).join("");
    SHUT.roomsSig = sig;
  }
  for (const r of s.rooms) updateShutRoom(r.id);
}

function _shutSkeleton(r) {
  const id = _hq(r.id);
  const slider = r.can_position ? `
        <label class="shut-pos">Position
          <input type="range" min="0" max="100" step="5" data-pos="${id}" aria-label="${_hq(r.name)} Position (% zu)">
          <span data-slot="posval"></span>
        </label>` : "";
  return `
      <div class="card heat-room shut-room" data-shut="${id}">
        <div class="heat-room-head"><h2>${escapeHtml(r.name)}</h2><span data-slot="contact"></span></div>
        <div class="shut-bar-row"><div class="shut-bar"><div class="shut-fill" data-slot="fill"></div></div>
          <span class="shut-val" data-slot="val"></span></div>
        <div class="shut-ctrl">
          <button type="button" class="btn shut-btn" data-shut-cmd="up" data-id="${id}">▲ Auf</button>
          <button type="button" class="btn shut-btn" data-shut-cmd="stop" data-id="${id}">■ Stop</button>
          <button type="button" class="btn shut-btn" data-shut-cmd="down" data-id="${id}">▼ Ab</button>
          ${slider}
        </div>
        <div data-slot="pending"></div><div data-slot="auto"></div>
        <div data-slot="hint"></div><div data-slot="alert"></div><div data-slot="err"></div>
        <div class="heat-toggles" data-slot="toggles"></div>
        <div data-slot="sched"></div>
      </div>`;
}

// api() wirft "409 Conflict — {\"detail\": …}" → nur den Text
function _errText(e) {
  const m = /— (.*)$/s.exec(e.message);
  try { return JSON.parse(m[1]).detail || e.message; } catch (_) { return e.message; }
}

const _hhmm = iso => iso ? iso.slice(11, 16) : "";

function updateShutRoom(id, force = false) {
  const r = SHUT.status?.rooms.find(x => x.id === id);
  const card = document.querySelector(`.shut-room[data-shut="${CSS.escape(id)}"]`);
  if (!r || !card) return;
  const slot = n => card.querySelector(`[data-slot="${n}"]`);
  card.classList.toggle("has-error", !!r.error);
  const pos = r.position;
  const fill = slot("fill");
  const w = pos == null ? "0%" : pos + "%";
  if (fill.style.width !== w) fill.style.width = w;
  const stale = !r.moving && (!r.position_reliable || !!r.pending);
  fill.classList.toggle("stale", stale);
  const val = pos == null ? "–"
    : r.moving ? `fährt ${r.moving === "down" ? "▼" : "▲"} ${pos} %`
    : pos === 0 ? "offen" : pos >= 100 ? "zu" : `${pos} % zu`;
  if (slot("val").textContent !== val) slot("val").textContent = val;
  slot("val").classList.toggle("stale", stale);
  slot("val").classList.toggle("moving", !!r.moving);
  slot("val").title = r.position_origin === "shelly" ? "Ist-Position direkt vom Shelly"
    : r.position_origin.startsWith("item:") ? `Ist-Position aus ${r.position_origin.slice(5)}` : "Ist-Position aus dem Steuer-Item";
  const kind = r.is_door ? "🚪 Tür" : "🪟 Fenster";
  _setSlot(slot("contact"), r.has_contact
    ? `<span class="shut-contact${r.contact === "open" ? " open" : ""}">${kind} · ${r.contact === "open" ? "offen" : r.contact === "closed" ? "zu" : "?"}</span>`
    : r.is_door ? `<span class="shut-contact">🚪 Tür · ohne Kontakt</span>` : "");
  const down = card.querySelector('[data-shut-cmd="down"]');
  down.classList.toggle("guarded", !!r.door_guard);
  down.title = r.door_guard ? r.door_guard + " – Schließen nur mit Bestätigung" : "";
  const sl = card.querySelector("[data-pos]");
  if (sl && !SHUT.drag[id]) {
    const v = String(r.moving ? pos ?? 0 : r.pending?.target ?? pos ?? 0);
    if (sl.value !== v) sl.value = v;
    _setSlot(slot("posval"), `${sl.value} %`);
  }
  _setSlot(slot("pending"), r.pending && !r.moving
    ? `<div class="heat-meta shut-pending">${r.pending.command === "position" ? `→ ${r.pending.target} %` : SHUT_CMD[r.pending.command]} gesendet ${_hhmm(r.pending.sent_at)} · Rückmeldung ab ${_hhmm(r.pending.verify_at)}</div>`
    : r.uncertain ? `<div class="heat-meta shut-pending">${escapeHtml(r.uncertain)}</div>` : "");
  const auto = r.automation === "pvcontroller";
  _setSlot(slot("auto"), `<div class="heat-meta heat-next">${!auto ? "Automatik: openHAB-Regel"
    : r.next ? `Nächste Fahrt: ${_fmtWhenShut(r.next)}${SHUT.status.schedules_enabled ? "" : ` <span class="heat-off">(Zeitpläne aus)</span>`}`
    : "Automatik: PV Controller · keine Fahrt geplant"}</div>`);
  _setSlot(slot("hint"), r.hint ? `<div class="heat-alert">ℹ️ ${escapeHtml(r.hint)}</div>` : "");
  _setSlot(slot("alert"), r.alert ? `<div class="heat-alert">⚠️ ${escapeHtml(r.alert)}</div>` : "");
  _setSlot(slot("err"), r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : "");
  // Zeitpläne an jedem Rollladen; bei „openHAB-Regel“ gespeichert, aber ruhend
  const nSched = SHUT.sched.filter(e => e.shutter_id === r.id).length;
  _setSlot(slot("toggles"), `<button type="button" class="heat-hist-toggle" data-ssched="${_hq(r.id)}">${SHUT.schedOpen[r.id] ? "▾" : "▸"} Zeitpläne (${nSched})${!auto && nSched ? " · ruhen" : ""}</button>`);
  const sch = slot("sched");
  if (force || !_typingIn(sch)) _setSlot(sch, SHUT.schedOpen[r.id] ? _shutSchedPanel(r) : "");
}

// ── Zeitpläne ──────────────────────────────────────────────────────────────

const _SHUT_TARGET = { up: "▲ auf", down: "▼ zu" };
const _shutTarget = (a, p) => _SHUT_TARGET[a] || `→ ${p} %`;

// "heute Sonnenuntergang +15 min (18:47) → zu" / "morgen 07:00 → auf"
function _fmtWhenShut(n) {
  const at = _fmtWhen(n.at);                      // aus heating.js: "heute 18:47"
  const [day, hm] = [at.slice(0, at.lastIndexOf(" ")), at.slice(at.lastIndexOf(" ") + 1)];
  const what = /^\d\d:\d\d$/.test(n.when) ? hm : `${n.when} (${hm})`;
  return `${escapeHtml(day)} ${escapeHtml(what)} ${_shutTarget(n.action, n.position)}`;
}

function _shutForm(id) {
  return SHUT.form[id] ||= { kind: "sunset", time: "", offset: 0, not_before: "", not_after: "", action: "down",
                             position: 50, days: [], date: "", editId: null };
}

function _shutSchedPanel(r) {
  const list = SHUT.sched.filter(e => e.shutter_id === r.id)
    .sort((a, b) => (a.date || "").localeCompare(b.date || "") || (a.time || a.kind).localeCompare(b.time || b.kind));
  const f = _shutForm(r.id);
  const rows = list.map(e => {
    const when = e.date ? `<span class="heat-chip once">${_fmtDate(e.date)}</span>`
      : HEAT_DAYS.map(([k, l]) => `<span class="heat-chip${e.days.includes(k) ? " on" : ""}">${l}</span>`).join("");
    const last = e.last_run ? `<small class="heat-last" title="${_hq(e.last_message || e.last_run)}">zuletzt ${_hq(e.last_run.slice(5, 16).replace("T", " "))} · ${_SHUT_RESULT[e.last_result] || _hq(e.last_result || "")}</small>` : "";
    return `<div class="heat-sched-row shut-sched-row${e.enabled ? "" : " off"}${f.editId === e.id ? " editing" : ""}">
        <span class="shut-sched-when">${_whenToday(e)}</span>
        <span class="heat-sched-sp">${_shutTarget(e.action, e.position)}</span>
        <label class="heat-switch" title="${e.enabled ? "aktiv" : "inaktiv"}"><input type="checkbox" data-ssched-toggle="${e.id}" ${e.enabled ? "checked" : ""}><span></span></label>
        <button type="button" class="heat-del" data-ssched-edit="${e.id}" aria-label="Zeitplan bearbeiten" title="bearbeiten">✎</button>
        <button type="button" class="heat-del" data-ssched-del="${e.id}" aria-label="Zeitplan löschen">✕</button>
        <span class="heat-chips">${when}</span>
        ${last}
      </div>`;
  }).join("") || `<p class="note">Noch keine Zeitpläne.</p>`;
  const dormant = r.automation !== "pvcontroller"
    ? `<p class="heat-prev-warn">Automatik: openHAB-Regel – Zeitpläne werden gespeichert, aber erst ausgeführt, wenn dieser Rollladen in den Settings auf „PV Controller“ umgestellt ist.</p>` : "";
  return `<div class="heat-sched" data-room="${_hq(r.id)}">
      ${dormant}
      ${rows}
      <div class="heat-sched-form" data-shut="${_hq(r.id)}">
        ${f.editId ? `<div class="sched-editing">Zeitplan bearbeiten</div>` : ""}
        ${_whenFields(f, "sf", "skind-" + r.id, SHUT.sun)}
        <div class="heat-form-row">
          <label>Ziel<select data-sf="action">
            ${[["up", "▲ auf"], ["down", "▼ zu"], ...(r.can_position ? [["position", "Position"]] : [])].map(([v, l]) => `<option value="${v}"${f.action === v ? " selected" : ""}>${l}</option>`).join("")}
          </select></label>
          ${f.action === "position" ? `<label>% zu<input type="number" data-sf="position" step="5" min="0" max="100" value="${_hq(f.position)}"></label>` : ""}
        </div>
        <div class="heat-chips pick">${HEAT_DAYS.map(([k, l]) => `<button type="button" class="heat-chip${f.days.includes(k) ? " on" : ""}" data-sday="${k}" aria-pressed="${f.days.includes(k)}">${l}</button>`).join("")}
          <button type="button" class="heat-chip quick" data-sdays="mo,di,mi,do,fr">Werktags</button>
          <button type="button" class="heat-chip quick" data-sdays="mo,di,mi,do,fr,sa,so">Täglich</button></div>
        <div class="heat-form-row">
          <label>oder einmalig am<input type="date" data-sf="date" value="${_hq(f.date)}"></label>
          <button type="button" class="btn primary" data-ssched-add="${_hq(r.id)}">${f.editId ? "Speichern" : "+ Zeitplan"}</button>
          ${f.editId ? `<button type="button" class="btn" data-ssched-cancel="${_hq(r.id)}">Abbrechen</button>` : ""}
        </div>
        <span class="form-msg" id="shut-sched-msg-${_hq(r.id)}"></span>
      </div>
    </div>`;
}

const _SHUT_RESULT = { sent: "gefahren", unchanged: "stand schon dort", dry_run: "Dry-Run", skipped: "verpasst",
  superseded: "durch späteren ersetzt", failed: "Fehler", door_open: "Tür offen – übersprungen",
  waiting_door: "wartet auf Tür", frost: "Frost – übersprungen", manual_hold: "von Hand bewegt – übersprungen",
  no_sun: "Sonnenzeit unbekannt" };

async function _shutSchedAdd(id) {
  const f = SHUT.form[id];
  const msg = document.getElementById("shut-sched-msg-" + id);
  const body = { shutter_id: id, action: f.action, ..._whenBody(f) };
  if (f.action === "position") body.position = Number(f.position);
  if (f.date) body.date = f.date; else body.days = f.days;
  try {
    await api(f.editId ? `/api/shutters/schedules/${f.editId}` : "/api/shutters/schedules",
      { method: f.editId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    SHUT.form[id] = { ...f, time: "", date: "", offset: 0, not_before: "", not_after: "", editId: null };
    document.activeElement?.blur();
    await loadShutters();
  } catch (e) {
    if (msg) { msg.textContent = _errText(e); msg.className = "form-msg error"; }
  }
}

async function _shutSchedToggle(id, enabled) {
  try {
    await api(`/api/shutters/schedules/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
  } catch (e) { alert("Zeitplan: " + _errText(e)); }
  loadShutters();
}

async function _shutSchedDelete(id) {
  const e = SHUT.sched.find(x => x.id === id);
  if (!confirm(`Zeitplan ${e ? e.when + " " + _shutTarget(e.action, e.position) : id} löschen?`)) return;
  try { await api(`/api/shutters/schedules/${id}`, { method: "DELETE" }); } catch (err) { alert("Zeitplan: " + _errText(err)); }
  loadShutters();
}

function _closing(r, cmd, target) {
  if (cmd === "down") return true;
  if (cmd === "position") return r.position == null || target > r.position;
  return false;
}

async function shutSend(id, cmd, position = null) {
  const r = SHUT.status?.rooms.find(x => x.id === id);
  if (!r) return;
  let confirmDoor = false;
  if (r.door_guard && _closing(r, cmd, position)) {
    if (!confirm(`${r.door_guard} – trotzdem schließen?`)) { updateShutRoom(id); return; }
    confirmDoor = true;
  }
  const body = { shutter_id: id, command: cmd, confirm: confirmDoor };
  if (cmd === "position") body.position = position;
  try {
    let res;
    try {
      res = await api("/api/shutters/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      // Kontakt hat sich seit dem letzten Poll geändert → Server verlangt die Bestätigung
      const why = _errText(e);
      if (!confirmDoor && /Bestätigung/.test(why) && confirm(why.replace(/ – Schließen nur mit Bestätigung\.?/, "") + " – trotzdem schließen?")) {
        body.confirm = true;
        res = await api("/api/shutters/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      } else throw e;
    }
    if (res?.dry_run) _shutToast(`Dry-Run: ${r.name} ${cmd === "position" ? "→ " + position + " %" : SHUT_CMD[cmd]} nur geloggt`);
  } catch (e) {
    alert("Rollladen: " + _errText(e));
  } finally {
    loadShutters();
    setTimeout(loadShutters, 3000);
  }
}

async function shutGroup(group, cmd) {
  const s = SHUT.status;
  const members = s.rooms.filter(r => group === "all" || r.group === group);
  const label = group === "all" ? "Alle" : _groupName(group);
  const skipped = cmd === "down" ? members.filter(r => r.door_guard) : [];
  const lines = [`${label} ${cmd === "up" ? "öffnen" : "schließen"}? (${members.length} Rollläden, gestaffelt im ${String(s.command_spacing_s).replace(".", ",")}-s-Abstand)`];
  if (skipped.length) lines.push("", "Übersprungen:", ...skipped.map(r => "• " + r.door_guard));
  if (!confirm(lines.join("\n"))) return;
  SHUT.busy = true; _renderShutGroups(s);
  try {
    const res = await api("/api/shutters/group", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group, command: cmd }) });
    const bad = res.results.filter(x => !x.ok);
    if (bad.length) alert("Nicht gesendet:\n" + bad.map(x => `• ${x.shutter_id}: ${x.error}`).join("\n"));
    else if (res.dry_run) _shutToast(`Dry-Run: ${label} ${cmd === "up" ? "▲ Auf" : "▼ Zu"} nur geloggt`);
  } catch (e) {
    alert("Gruppe: " + _errText(e));
  } finally {
    SHUT.busy = false;
    loadShutters();
    setTimeout(loadShutters, 3000);
  }
}

function _shutToast(text) {
  const el = $("#shut-toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 4000);
}

// ── Settings ───────────────────────────────────────────────────────────────

let SS_ROOMS = [];

async function loadShutterSettings() {
  const msg = $("#ss-msg");
  try {
    const c = await api("/api/shutters/config");
    const s = c.shutters;
    $("#ss-enabled").checked = !!s.enabled;
    $("#ss-spacing").value = s.command_spacing_s ?? 2;
    $("#ss-dc-item").value = s.duty_cycle_item || "";
    $("#ss-dc-max").value = s.duty_cycle_max ?? 80;
    $("#ss-sched").checked = !!s.schedules_enabled;
    $("#ss-frost").value = s.frost_temp_c ?? 0;
    $("#ss-frost-item").value = s.frost_temp_item || "";
    $("#ss-hold").value = s.manual_hold_min ?? 60;
    SS_ROOMS = (s.rooms || []).map(r => ({ ...r }));
    renderSsRooms();
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
}

const _psrcKind = v => !v || v === "item" ? "item" : v === "shelly" ? "shelly" : "item:";
const _ssSel = (k, val, opts) => `<select data-k="${k}">${opts.map(([v, l]) => `<option value="${v}"${(val || opts[0][0]) === v ? " selected" : ""}>${l}</option>`).join("")}</select>`;

function renderSsRooms() {
  $("#ss-rooms").innerHTML = SS_ROOMS.map((r, i) => `
    <details class="hs-room" ${r._open ? "open" : ""} data-i="${i}">
      <summary>${escapeHtml(r.name || "(neu)")} <small>${escapeHtml(r.id || "")}${r.is_door ? " · Tür" : ""}${r.group ? " · " + escapeHtml(r.group.toUpperCase()) : ""} · ${r.automation === "pvcontroller" ? "PV Controller" : "openHAB-Regel"}</small></summary>
      <label>ID (a–z, 0–9, _ -)<input type="text" data-k="id" value="${_hq(r.id)}" placeholder="z. B. wohnen_sued"></label>
      <label>Name<input type="text" data-k="name" value="${_hq(r.name)}"></label>
      <label>Rollladen-Item *<input type="text" list="hs-items" data-k="item" value="${_hq(r.item)}" autocomplete="off" placeholder="Rollershutter-Item"></label>
      <label>Kontakt-Item (Fenster/Tür)<input type="text" list="hs-items" data-k="contact_item" value="${_hq(r.contact_item)}" autocomplete="off" placeholder="optional"></label>
      <label>Gruppe (z. B. eg, og)<input type="text" data-k="group" value="${_hq(r.group)}" placeholder="optional"></label>
      <label>Ist-Position aus${_ssSel("psrc", _psrcKind(r.position_source), [["item", "Steuer-Item (Standard)"], ["item:", "eigenem Lese-Item (z. B. HmIP-Kanal 3)"], ["shelly", "Shelly direkt (Gen1 /roller/0)"]])}</label>
      <label>Lese-Item (bei „eigenem Lese-Item“)<input type="text" list="hs-items" data-k="position_item" value="${_hq((r.position_source || "").startsWith("item:") ? r.position_source.slice(5) : "")}" autocomplete="off" placeholder="z. B. Rollladen_Wohnen_Level"></label>
      <label>Shelly-Adresse (bei „Shelly direkt“)<input type="text" data-k="shelly_url" value="${_hq(r.shelly_url)}" autocomplete="off" placeholder="http://192.168.2.51"></label>
      <label>Gerät (Rückprüfung)${_ssSel("device", r.device, [["other", "anderes (2 min)"], ["homematic", "Homematic (6 min)"], ["shelly", "Shelly (60 s)"]])}</label>
      <label>Automatik${_ssSel("automation", r.automation, [["openhab", "openHAB-Regel (PV Controller plant nichts)"], ["pvcontroller", "PV Controller"]])}</label>
      <label class="check"><input type="checkbox" data-k="is_door" ${r.is_door ? "checked" : ""}> Tür (Aussperrschutz)</label>
      <label class="check"><input type="checkbox" data-k="can_position" ${r.can_position !== false ? "checked" : ""}> kann Position (%)</label>
      <label class="check"><input type="checkbox" data-k="retry_when_closed" ${r.retry_when_closed ? "checked" : ""}> Tür offen: Schließen nachholen, sobald die Tür zu ist</label>
      <label>Automatisch höchstens schließen bis (%) – leer = Standard (Tür ohne Kontakt 80, sonst 100)<input type="number" data-k="auto_close_max" min="0" max="100" step="5" value="${_hq(r.auto_close_max ?? "")}"></label>
      <div class="hs-room-actions">
        <button type="button" class="btn" data-move="-1">↑</button>
        <button type="button" class="btn" data-move="1">↓</button>
        <button type="button" class="btn" data-del="1">Rollladen entfernen</button>
      </div>
    </details>`).join("");
}

function _ssCollect() {
  $$("#ss-rooms .hs-room").forEach(el => {
    const r = SS_ROOMS[+el.dataset.i];
    r._open = el.open;
    el.querySelectorAll("[data-k]").forEach(inp => {
      r[inp.dataset.k] = inp.type === "checkbox" ? inp.checked
        : inp.type === "number" ? (inp.value.trim() === "" ? null : Math.round(Number(inp.value))) : inp.value.trim();
    });
  });
}

async function saveShutterSettings() {
  _ssCollect();
  const msg = $("#ss-msg");
  msg.textContent = "…"; msg.className = "form-msg";
  const body = {
    enabled: $("#ss-enabled").checked,
    command_spacing_s: Number($("#ss-spacing").value),
    duty_cycle_item: $("#ss-dc-item").value.trim(),
    duty_cycle_max: Number($("#ss-dc-max").value),
    schedules_enabled: $("#ss-sched").checked,
    frost_temp_c: Number($("#ss-frost").value),
    frost_temp_item: $("#ss-frost-item").value.trim(),
    manual_hold_min: Math.round(Number($("#ss-hold").value)),
    rooms: SS_ROOMS.map(({ _open, psrc, position_item, ...r }) => ({
      ...r, group: (r.group || "").toLowerCase(),
      position_source: psrc === "item:" ? "item:" + (position_item || "") : psrc || r.position_source || "item",
      shelly_url: psrc === "shelly" ? r.shelly_url : "",
    })),
  };
  try {
    const res = await api("/api/shutters/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    msg.textContent = "Gespeichert (Backup: " + res.backup + ")"; msg.className = "form-msg ok";
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
}

function bootShutters() {
  $("#shut-rooms")?.addEventListener("click", e => {
    const b = e.target.closest("[data-shut-cmd]");
    if (b) { shutSend(b.dataset.id, b.dataset.shutCmd); return; }
    const t = e.target.closest("[data-ssched]");
    if (t) { SHUT.schedOpen[t.dataset.ssched] = !SHUT.schedOpen[t.dataset.ssched]; updateShutRoom(t.dataset.ssched, true); return; }
    const day = e.target.closest("[data-sday]"), quick = e.target.closest("[data-sdays]");
    if (day || quick) {
      const id = e.target.closest(".heat-sched-form").dataset.shut;
      const f = _shutForm(id);
      if (quick) f.days = quick.dataset.sdays.split(",");
      else f.days = f.days.includes(day.dataset.sday) ? f.days.filter(d => d !== day.dataset.sday) : [...f.days, day.dataset.sday];
      f.date = "";
      updateShutRoom(id, true);
      return;
    }
    const add = e.target.closest("[data-ssched-add]");
    if (add) { _shutSchedAdd(add.dataset.sschedAdd); return; }
    const ed = e.target.closest("[data-ssched-edit]");
    if (ed) {
      const en = SHUT.sched.find(x => x.id === Number(ed.dataset.sschedEdit));
      if (en) { SHUT.form[en.shutter_id] = { ..._whenFromEntry(en), action: en.action, position: en.position ?? 50 }; updateShutRoom(en.shutter_id, true); }
      return;
    }
    const ca = e.target.closest("[data-ssched-cancel]");
    if (ca) { SHUT.form[ca.dataset.sschedCancel] = { ..._shutForm(ca.dataset.sschedCancel), editId: null, time: "", date: "" }; updateShutRoom(ca.dataset.sschedCancel, true); return; }
    const del = e.target.closest("[data-ssched-del]");
    if (del) _shutSchedDelete(Number(del.dataset.sschedDel));
  });
  $("#shut-rooms")?.addEventListener("input", e => {
    const inp = e.target.closest('.heat-sched-form input[data-sf]:not([type="radio"])');
    if (inp) {
      const f = _shutForm(inp.closest(".heat-sched-form").dataset.shut);
      f[inp.dataset.sf] = inp.value;
      _refreshCalc(inp.closest(".heat-sched-form"), f, SHUT.sun);
      if (inp.dataset.sf === "date" && inp.value) {
        f.days = [];
        inp.closest(".heat-sched-form").querySelectorAll("[data-sday]").forEach(b => { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); });
      }
      return;
    }
    const sl = e.target.closest("[data-pos]");
    if (!sl) return;
    SHUT.drag[sl.dataset.pos] = true;
    const card = sl.closest(".shut-room");
    _setSlot(card.querySelector('[data-slot="posval"]'), `${sl.value} %`);
  });
  $("#shut-rooms")?.addEventListener("change", e => {
    const tg = e.target.closest("[data-ssched-toggle]");
    if (tg) { _shutSchedToggle(Number(tg.dataset.sschedToggle), tg.checked); return; }
    const sel = e.target.closest('.heat-sched-form select[data-sf], .heat-sched-form input[type="radio"][data-sf="kind"]');
    if (sel) {                                         // Zeitart/Ziel ändern die Felder → neu zeichnen
      const id = sel.closest(".heat-sched-form").dataset.shut;
      _shutForm(id)[sel.dataset.sf] = sel.value;
      document.activeElement?.blur();
      updateShutRoom(id, true);
      return;
    }
    const sl = e.target.closest("[data-pos]");
    if (!sl) return;
    const id = sl.dataset.pos;
    delete SHUT.drag[id];
    shutSend(id, "position", Number(sl.value));
  });
  $("#shut-groups")?.addEventListener("click", e => {
    const b = e.target.closest("[data-gcmd]");
    if (b && !b.disabled) shutGroup(b.dataset.group, b.dataset.gcmd);
  });
  $("#ss-save")?.addEventListener("click", saveShutterSettings);
  $("#ss-add")?.addEventListener("click", () => {
    _ssCollect();
    SS_ROOMS.push({ id: "", name: "", automation: "openhab", device: "other", _open: true });
    renderSsRooms();
  });
  $("#ss-rooms")?.addEventListener("click", e => {
    const el = e.target.closest(".hs-room");
    if (!el) return;
    const i = +el.dataset.i;
    if (e.target.closest("[data-del]")) {
      _ssCollect();
      if (!confirm(`Rollladen „${SS_ROOMS[i].name || SS_ROOMS[i].id}“ entfernen?`)) return;
      SS_ROOMS.splice(i, 1); renderSsRooms();
    } else if (e.target.closest("[data-move]")) {
      _ssCollect();
      const j = i + Number(e.target.closest("[data-move]").dataset.move);
      if (j < 0 || j >= SS_ROOMS.length) return;
      [SS_ROOMS[i], SS_ROOMS[j]] = [SS_ROOMS[j], SS_ROOMS[i]]; renderSsRooms();
    }
  });
}

document.addEventListener("DOMContentLoaded", bootShutters);
