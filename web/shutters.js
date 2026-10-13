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
  sun2: {},             // morgen
  form: {},             // shutter_id → Eingaben im Zeitplan-Sheet
};
const SHUT_POLL_MS = 30_000;
const SHUT_MOVING_POLL_MS = 3_000;      // solange ein Shelly fährt: Position alle 3 s
const SHUT_CMD = { up: "▲ Auf", down: "▼ Ab", stop: "■ Stop" };
const SHUT_ARROW = { up: "▲", down: "▼" };

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
    if (sch) { SHUT.sched = sch.items; SHUT.sun = sch.sun_today || {}; SHUT.sun2 = sch.sun_tomorrow || {}; }
    if (s.chat) renderChatStatus(s.chat, !!(s.enabled && s.openhab_configured));
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Rollläden sind nicht aktiv. In den Settings unter „Rollläden“ eintragen und aktivieren.";
      _setSlot($("#shut-head"), "");
      _setSlot($("#shut-groups"), "");
      $("#shut-updated").textContent = "";
      $("#shut-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      SHUT.roomsSig = null;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    _renderShutHead(s);
    _renderShutGroups(s);
    renderShutRooms(s);
    renderSheet();                                      // offenes Zeitplan-Sheet bleibt offen und aktuell
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

// "heute 18:40 ▼" bzw. "morgen ☀↑ +10 (07:48) ▲" – nächste geplante Fahrt, kurz
function _nextShort(n) {
  const at = _fmtWhen(n.at);                            // aus heating.js: "heute 18:47"
  const arrow = SHUT_ARROW[n.action] || `→ ${n.position} %`;
  return `${escapeHtml(at)} ${arrow}`;
}

// Kopfzeile: offen/zu · nächste Fahrt · [Alle ▲] [Alle ▼] · Zeitpläne-Schalter
function _renderShutHead(s) {
  const known = s.rooms.filter(r => r.position != null);
  const closed = known.filter(r => r.position >= 100).length, open = known.length - closed;
  const parts = [`${open} offen`, `${closed} zu`];
  const nx = s.rooms.filter(r => r.next).sort((a, b) => new Date(a.next.at) - new Date(b.next.at))[0];
  if (nx) parts.push(`nächste: ${escapeHtml(nx.name)} ${_nextShort(nx.next)}${s.schedules_enabled ? "" : ` <span class="heat-off">(aus)</span>`}`);
  const dc = s.duty_cycle;
  if (dc) parts.push(`<span class="${dc.high ? "heat-off" : ""}" title="Funk-Auslastung der CCU (Homematic Duty Cycle)">Duty Cycle ${dc.value == null ? "–" : Math.round(dc.value) + " %"}${dc.high ? ` · über ${dc.max} %: Gruppenbefehle pausiert` : ""}</span>`);
  if (SHUT.busy) parts.push(`sende gestaffelt (${String(s.command_spacing_s).replace(".", ",")} s Abstand)…`);
  const off = SHUT.busy || dc?.high ? "disabled" : "";
  _setSlot($("#shut-head"), `<div class="tab-head-main"><h1>Rollläden</h1><span class="tab-head-sum">${parts.join('<span class="sep">·</span>')}</span></div>
    <div class="tab-head-actions">${s.dry_run ? `<span class="cb warn" title="Dry-Run: Befehle werden nur geloggt, kein Rollladen fährt">Dry-Run</span>` : ""}
      <button type="button" class="btn shut-gbtn" data-group="all" data-gcmd="up" ${off}>Alle ▲</button>
      <button type="button" class="btn shut-gbtn" data-group="all" data-gcmd="down" ${off}>Alle ▼</button>
      ${_headSwitch(!!s.schedules_enabled, "Rollladen-Zeitpläne")}</div>`);
}

// Benannte Gruppen (z. B. EG/OG) als schlanke Zeile; „Alle“ steht in der Kopfzeile
function _renderShutGroups(s) {
  const off = SHUT.busy || s.duty_cycle?.high ? "disabled" : "";
  _setSlot($("#shut-groups"), s.groups.length ? `<div class="shut-groups-row">${s.groups.map(g =>
    `<span class="shut-grp"><span class="shut-glabel">${escapeHtml(_groupName(g))}</span>
       <button type="button" class="btn shut-gbtn" data-group="${_hq(g)}" data-gcmd="up" ${off} aria-label="${_hq(_groupName(g))} auf">▲</button>
       <button type="button" class="btn shut-gbtn" data-group="${_hq(g)}" data-gcmd="down" ${off} aria-label="${_hq(_groupName(g))} zu">▼</button></span>`).join("")}</div>` : "");
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
  const id = _hq(r.id), nm = _hq(r.name);
  const slider = r.can_position ? `
        <label class="shut-pos"><input type="range" min="0" max="100" step="5" data-pos="${id}" aria-label="${nm} Position (% zu)">
          <span data-slot="posval"></span></label>` : "";
  return `
      <div class="card heat-room shut-room scard" data-shut="${id}">
        <div class="hcard-head"><h2>${escapeHtml(r.name)}</h2><span class="card-badges" data-slot="badges"></span></div>
        <div class="scard-main">
          <div class="scard-win" data-slot="win"></div>
          <div class="scard-side">
            <div class="shut-val" data-slot="val"></div>
            <div class="shut-ctrl">
              <button type="button" class="btn shut-btn" data-shut-cmd="up" data-id="${id}" aria-label="${nm} auf" title="Auf">▲</button>
              <button type="button" class="btn shut-btn" data-shut-cmd="stop" data-id="${id}" aria-label="${nm} Stop" title="Stop">■</button>
              <button type="button" class="btn shut-btn" data-shut-cmd="down" data-id="${id}" aria-label="${nm} ab" title="Ab">▼</button>
            </div>
          </div>
        </div>
        ${slider}
        <div data-slot="pending"></div>
        <div data-slot="hint"></div><div data-slot="alert"></div><div data-slot="err"></div>
        <button type="button" class="card-link" data-ssched="${id}" data-slot="schedlink"></button>
      </div>`;
}

// Fenster-Symbol (56×64): die Lamellen zeigen die Position (0 = offen, 100 = zu)
function _winSvg(pos, moving, stale) {
  const x = 7, y = 7, w = 42, h = 50;
  const p = pos == null ? 0 : Math.max(0, Math.min(100, pos));
  const bh = Math.round(h * p / 100);
  let slats = "";
  for (let yy = y + 5; yy < y + bh - 1; yy += 5) slats += `<line x1="${x}" x2="${x + w}" y1="${yy}" y2="${yy}" class="win-slat"/>`;
  return `<svg viewBox="0 0 56 64" class="win${moving ? " moving" : ""}${stale ? " stale" : ""}" role="img" aria-label="${pos == null ? "Position unbekannt" : p + " % zu"}">
      <rect x="${x}" y="${y}" width="${w}" height="${h}" class="win-glass"/>
      <line x1="${x + w / 2}" x2="${x + w / 2}" y1="${y}" y2="${y + h}" class="win-bar"/>
      <g class="win-blind">${bh ? `<rect x="${x}" y="${y}" width="${w}" height="${bh}" class="win-fill"/>${slats}<line x1="${x}" x2="${x + w}" y1="${y + bh}" y2="${y + bh}" class="win-end"/>` : ""}</g>
      <rect x="3" y="3" width="50" height="58" rx="5" class="win-frame"/>
      ${pos == null ? `<text x="28" y="38" text-anchor="middle" class="win-q">?</text>` : ""}
    </svg>`;
}

// Automatik als Badge: "openHAB ▼ 18:40" (bzw. "openHAB" ohne lesbare Regelzeit) / "Auto ▼ 18:40" / "Auto ☀↓ −15" / "Auto"
function _autoBadge(r, s) {
  if (r.automation !== "pvcontroller") {
    const n = r.rule_next;                              // nächste Zeit aus den openHAB-Regeln (Server, 10 min gecacht)
    if (!n) return `<span class="cb" title="Automatik: openHAB-Regel (PV Controller plant nichts) · keine aktive Regel mit Uhrzeit in den nächsten 48 h gefunden">openHAB</span>`;
    const when = _fmtWhen(n.at), hm = when.split(" ").pop();
    const arrow = n.target.startsWith("▼") ? "▼" : n.target.startsWith("▲") ? "▲" : n.target;
    return `<span class="cb" title="Automatik: ${_hq(n.source)} · ${_hq(when)} ${_hq(n.target)}">openHAB ${_hq(arrow)} ${_hq(hm)}</span>`;
  }
  if (!r.next) return `<span class="cb" title="Automatik: PV Controller · keine Fahrt geplant">Auto</span>`;
  const n = r.next, arrow = SHUT_ARROW[n.action] || `${n.position} %`;
  const hm = _fmtWhen(n.at).split(" ").pop();
  const sun = /^\d\d:\d\d$/.test(n.when) ? "" : _whenShort(n.when).replace(/ min.*$/, "").replace(/ \(.*$/, "");
  return `<span class="cb${s.schedules_enabled ? " auto" : " dim"}" title="Nächste Fahrt: ${_hq(_fmtWhenShut(n).replace(/<[^>]+>/g, ""))}${s.schedules_enabled ? "" : " (Zeitpläne aus)"}">Auto ${sun ? _hq(sun) + " " : ""}${arrow} ${_hq(hm)}</span>`;
}

function updateShutRoom(id) {
  const s = SHUT.status;
  const r = s?.rooms.find(x => x.id === id);
  const card = document.querySelector(`.shut-room[data-shut="${CSS.escape(id)}"]`);
  if (!r || !card) return;
  const slot = n => card.querySelector(`[data-slot="${n}"]`);
  card.classList.toggle("has-error", !!r.error);
  const pos = r.position;
  const stale = !r.moving && (!r.position_reliable || !!r.pending);
  _setSlot(slot("win"), _winSvg(pos, !!r.moving, stale));
  // unsichere Position: ausgegraut mit „≈“
  const val = pos == null ? "–"
    : r.moving ? `fährt ${r.moving === "down" ? "▼" : "▲"} ${pos} %`
    : (stale ? "≈ " : "") + (pos === 0 ? "offen" : pos >= 100 ? "zu" : `${pos} % zu`);
  if (slot("val").textContent !== val) slot("val").textContent = val;
  slot("val").classList.toggle("stale", stale);
  slot("val").classList.toggle("moving", !!r.moving);
  slot("val").title = r.position_origin === "shelly" ? "Ist-Position direkt vom Shelly"
    : r.position_origin.startsWith("item:") ? `Ist-Position aus ${r.position_origin.slice(5)}` : "Ist-Position aus dem Steuer-Item";
  // Badges: Tür/Fenster-Kontakt (offen = Warnung), Automatik
  const badges = [];
  if (r.has_contact) badges.push(`<span class="cb${r.contact === "open" ? " warn" : ""}" title="${r.is_door ? "Tür" : "Fenster"}-Kontakt">${r.is_door ? "🚪" : "🪟"} ${r.contact === "open" ? "offen" : r.contact === "closed" ? "zu" : "?"}</span>`);
  else if (r.is_door) badges.push(`<span class="cb" title="Tür ohne Kontakt – Schließen nur mit Bestätigung">🚪 Tür</span>`);
  badges.push(_autoBadge(r, s));
  _setSlot(slot("badges"), badges.join(""));
  const down = card.querySelector('[data-shut-cmd="down"]');
  down.classList.toggle("guarded", !!r.door_guard);
  down.title = r.door_guard ? r.door_guard + " – Schließen nur mit Bestätigung" : "Ab";
  const sl = card.querySelector("[data-pos]");
  if (sl && !SHUT.drag[id]) {
    const v = String(r.moving ? pos ?? 0 : r.pending?.target ?? pos ?? 0);
    if (sl.value !== v) sl.value = v;
    _setSlot(slot("posval"), `${sl.value} %`);
  }
  _setSlot(slot("pending"), r.pending && !r.moving
    ? `<div class="heat-meta shut-pending">${r.pending.command === "position" ? `→ ${r.pending.target} %` : SHUT_CMD[r.pending.command]} gesendet ${_hhmm(r.pending.sent_at)} · Rückmeldung ab ${_hhmm(r.pending.verify_at)}</div>`
    : r.uncertain ? `<div class="heat-meta shut-pending">${escapeHtml(r.uncertain)}</div>` : "");
  _setSlot(slot("hint"), r.hint ? `<div class="heat-meta">ℹ️ ${escapeHtml(r.hint)}</div>` : "");
  _setSlot(slot("alert"), r.alert ? `<div class="heat-alert">⚠ ${escapeHtml(r.alert)}</div>` : "");
  _setSlot(slot("err"), r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : "");
  // Zeitpläne an jedem Rollladen; bei „openHAB-Regel“ gespeichert, aber ruhend
  const nSched = SHUT.sched.filter(e => e.shutter_id === r.id).length;
  _setSlot(slot("schedlink"), _schedLink(nSched, r.automation !== "pvcontroller"));
}

// api() wirft "409 Conflict — {\"detail\": …}" → nur den Text
function _errText(e) {
  const m = /— (.*)$/s.exec(e.message);
  try { return JSON.parse(m[1]).detail || e.message; } catch (_) { return e.message; }
}

const _hhmm = iso => iso ? iso.slice(11, 16) : "";

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
                             position: 50, days: [], date: "", once: false, editId: null };
}

const _SHUT_RESULT = { sent: "gefahren", unchanged: "stand schon dort", dry_run: "Dry-Run", skipped: "verpasst",
  superseded: "durch späteren ersetzt", failed: "Fehler", door_open: "Tür offen – übersprungen",
  waiting_door: "wartet auf Tür", frost: "Frost – übersprungen", manual_hold: "von Hand bewegt – übersprungen",
  no_sun: "Sonnenzeit unbekannt" };

// Zeitpläne im Sheet (Liste + Formular kommen aus heating.js: _schedSheet)
SCHED.shut = {
  attr: "sf",
  item: id => SHUT.status?.rooms.find(r => r.id === id),
  form: id => _shutForm(id),
  store: () => SHUT.form,
  list: id => SHUT.sched.filter(e => e.shutter_id === id),
  find: sid => SHUT.sched.find(e => e.id === sid),
  owner: e => e.shutter_id,
  fromEntry: e => ({ ..._whenFromEntry(e), once: !!e.date, action: e.action, position: e.position ?? 50 }),
  sun: () => SHUT.sun,
  sun2: () => SHUT.sun2,
  result: _SHUT_RESULT,
  banner: r => r.automation !== "pvcontroller"
    ? `<p class="sheet-banner">Automatik: openHAB-Regel – Zeitpläne werden gespeichert, aber erst ausgeführt, wenn dieser Rollladen in den Settings auf „PV Controller“ umgestellt ist.</p>` : "",
  target: e => _shutTarget(e.action, e.position),
  formTarget: f => _shutTarget(f.action, f.position),
  targetFields: (f, r) => `<div class="heat-form-row sch-target-row">
      <div class="seg" role="radiogroup" aria-label="Ziel">
        ${[["up", "▲ Auf"], ["down", "▼ Zu"], ...(r.can_position ? [["position", "Position"]] : [])].map(([v, l]) => `<label class="seg-opt"><input type="radio" name="sact-${_hq(r.id)}" data-sf="action" value="${v}"${f.action === v ? " checked" : ""}><span>${l}</span></label>`).join("")}
      </div>
      ${f.action === "position" ? `<label>% zu<input type="number" data-sf="position" step="5" min="0" max="100" value="${_hq(f.position)}" inputmode="numeric"></label>` : ""}
    </div>`,
  save: id => _shutSchedAdd(id),
  toggle: (sid, on) => _shutSchedToggle(sid, on),
  del: sid => _shutSchedDelete(sid),
};
SHEET_VIEWS.ssched = id => _schedSheet(SCHED.shut, id);

async function _shutSchedAdd(id) {
  const f = SHUT.form[id];
  const err = _whenError(f);
  if (err) { _schedMsg(err); return; }
  const when = _schedDaysBody(f);
  if (!when) return;
  const body = { shutter_id: id, action: f.action, ..._whenBody(f), ...when };
  if (f.action === "position") body.position = Number(f.position);
  try {
    await api(f.editId ? `/api/shutters/schedules/${f.editId}` : "/api/shutters/schedules",
      { method: f.editId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    SHUT.form[id] = { ...f, time: "", date: "", once: false, offset: 0, not_before: "", not_after: "", editId: null };
    SHEET.form = false;
    document.activeElement?.blur();
    await loadShutters();
  } catch (e) {
    _schedMsg(_errText(e));
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
  SHUT.busy = true; _renderShutHead(s); _renderShutGroups(s);
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

// Zeitpläne an/aus in der Kopfzeile: gleicher Endpoint wie Settings → Rollläden (PUT /api/shutters/config)
async function _shutSchedMaster(on, box) {
  if (on && !confirm("Rollladen-Zeitpläne einschalten? Rollläden mit „Automatik: PV Controller“ fahren dann nach Plan.")) { box.checked = false; return; }
  box.disabled = true;
  try {
    const s = (await api("/api/shutters/config")).shutters;
    await api("/api/shutters/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      enabled: !!s.enabled, command_spacing_s: s.command_spacing_s ?? 2, duty_cycle_item: s.duty_cycle_item || "",
      duty_cycle_max: s.duty_cycle_max ?? 80, schedules_enabled: on, frost_temp_c: s.frost_temp_c ?? 0,
      frost_temp_item: s.frost_temp_item || "", manual_hold_min: s.manual_hold_min ?? 60, rooms: s.rooms || [],
    }) });
  } catch (e) {
    alert("Zeitpläne nicht umgeschaltet: " + _errText(e));
  }
  $("#shut-head")._html = undefined;                   // Schalter von Hand umgestellt → neu zeichnen
  loadShutters();
}

function bootShutters() {
  $("#shut-rooms")?.addEventListener("click", e => {
    const b = e.target.closest("[data-shut-cmd]");
    if (b) { shutSend(b.dataset.id, b.dataset.shutCmd); return; }
    const t = e.target.closest("[data-ssched]");
    if (t) openSheet("ssched", t.dataset.ssched, !SHUT.sched.some(x => x.shutter_id === t.dataset.ssched));
  });
  $("#shut-rooms")?.addEventListener("input", e => {
    const sl = e.target.closest("[data-pos]");
    if (!sl) return;
    SHUT.drag[sl.dataset.pos] = true;
    const card = sl.closest(".shut-room");
    _setSlot(card.querySelector('[data-slot="posval"]'), `${sl.value} %`);
  });
  $("#shut-rooms")?.addEventListener("change", e => {
    const sl = e.target.closest("[data-pos]");
    if (!sl) return;
    const id = sl.dataset.pos;
    delete SHUT.drag[id];
    shutSend(id, "position", Number(sl.value));
  });
  $("#shut-head")?.addEventListener("click", e => {
    const b = e.target.closest("[data-gcmd]");
    if (b && !b.disabled) shutGroup(b.dataset.group, b.dataset.gcmd);
  });
  $("#shut-head")?.addEventListener("change", e => {
    const m = e.target.closest("[data-sched-master]");
    if (m) _shutSchedMaster(m.checked, m);
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
