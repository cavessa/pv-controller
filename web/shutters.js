// ── Rollläden: openHAB-Rollershutter (Tab "Rollläden" + Settings-Block) ──────
// Nutzt $, $$, api, escapeHtml aus app.js und _hq, _setSlot aus heating.js.

const SHUT = {
  status: null,
  timer: null,
  roomsSig: null,       // Liste der gerenderten Cards (Neuaufbau nur bei Änderung)
  loadSeq: 0,
  drag: {},             // shutter_id → Slider wird gerade bedient (Poll überschreibt ihn nicht)
  busy: false,          // Gruppenbefehl läuft
};
const SHUT_POLL_MS = 30_000;
const SHUT_CMD = { up: "▲ Auf", down: "▼ Ab", stop: "■ Stop" };

const _shutVisible = () => !document.hidden && $(".tab.active")?.dataset.tab === "rolllaeden";

function shutActivate() {
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
    const s = await api("/api/shutters/status");
    if (seq !== SHUT.loadSeq) return;
    SHUT.status = s;
    banner.classList.add("hidden");
    if (!s.enabled || !s.openhab_configured) {
      const why = !s.openhab_configured
        ? "openHAB ist nicht konfiguriert: in config.json fehlt der Block „openhab“ (url, token)."
        : "Rollläden sind nicht aktiv. In den Settings unter „Rollläden“ eintragen und aktivieren.";
      $("#shut-dry").classList.add("hidden");
      $("#shut-groups").innerHTML = "";
      $("#shut-updated").textContent = "";
      $("#shut-rooms").innerHTML = `<div class="card"><p class="note">${why}</p></div>`;
      SHUT.roomsSig = null;
      return;
    }
    if (!s.ok) { banner.textContent = s.error || "openHAB nicht erreichbar"; banner.classList.remove("hidden"); }
    $("#shut-dry").classList.toggle("hidden", !s.dry_run);
    _renderShutGroups(s);
    renderShutRooms(s);
    const stand = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    $("#shut-updated").textContent = "Stand " + stand;
    pillTimes.shutters = stand;
    updatePillTime();
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
  const sig = s.rooms.map(r => `${r.id}|${r.name}|${r.can_position}`).join(";");
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
        <div data-slot="alert"></div><div data-slot="err"></div>
      </div>`;
}

// api() wirft "409 Conflict — {\"detail\": …}" → nur den Text
function _errText(e) {
  const m = /— (.*)$/s.exec(e.message);
  try { return JSON.parse(m[1]).detail || e.message; } catch (_) { return e.message; }
}

const _hhmm = iso => iso ? iso.slice(11, 16) : "";

function updateShutRoom(id) {
  const r = SHUT.status?.rooms.find(x => x.id === id);
  const card = document.querySelector(`.shut-room[data-shut="${CSS.escape(id)}"]`);
  if (!r || !card) return;
  const slot = n => card.querySelector(`[data-slot="${n}"]`);
  card.classList.toggle("has-error", !!r.error);
  const pos = r.position;
  const fill = slot("fill");
  const w = pos == null ? "0%" : pos + "%";
  if (fill.style.width !== w) fill.style.width = w;
  fill.classList.toggle("stale", !!r.pending);
  const val = pos == null ? "–" : pos === 0 ? "offen" : pos >= 100 ? "zu" : `${pos} % zu`;
  if (slot("val").textContent !== val) slot("val").textContent = val;
  slot("val").classList.toggle("stale", !!r.pending);
  const kind = r.is_door ? "🚪 Tür" : "🪟 Fenster";
  _setSlot(slot("contact"), r.has_contact
    ? `<span class="shut-contact${r.contact === "open" ? " open" : ""}">${kind} · ${r.contact === "open" ? "offen" : r.contact === "closed" ? "zu" : "?"}</span>`
    : r.is_door ? `<span class="shut-contact">🚪 Tür · ohne Kontakt</span>` : "");
  const down = card.querySelector('[data-shut-cmd="down"]');
  down.classList.toggle("guarded", !!r.door_guard);
  down.title = r.door_guard ? r.door_guard + " – Schließen nur mit Bestätigung" : "";
  const sl = card.querySelector("[data-pos]");
  if (sl && !SHUT.drag[id]) {
    const v = String(r.pending?.target ?? pos ?? 0);
    if (sl.value !== v) sl.value = v;
    _setSlot(slot("posval"), `${sl.value} %`);
  }
  _setSlot(slot("pending"), r.pending
    ? `<div class="heat-meta shut-pending">${r.pending.command === "position" ? `→ ${r.pending.target} %` : SHUT_CMD[r.pending.command]} gesendet ${_hhmm(r.pending.sent_at)} · Rückmeldung ab ${_hhmm(r.pending.verify_at)}</div>` : "");
  _setSlot(slot("auto"), `<div class="heat-meta heat-next">${r.automation === "openhab"
    ? "Automatik: openHAB-Regel" : "Automatik: PV Controller (Zeitpläne folgen in Phase 2)"}</div>`);
  _setSlot(slot("alert"), r.alert ? `<div class="heat-alert">⚠️ ${escapeHtml(r.alert)}</div>` : "");
  _setSlot(slot("err"), r.error ? `<div class="heat-err">${escapeHtml(r.error)}</div>` : "");
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
    SS_ROOMS = (s.rooms || []).map(r => ({ ...r }));
    renderSsRooms();
  } catch (e) {
    msg.textContent = "Fehler: " + e.message; msg.className = "form-msg error";
  }
}

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
      <label>Gerät (Rückprüfung)${_ssSel("device", r.device, [["other", "anderes (2 min)"], ["homematic", "Homematic (6 min)"], ["shelly", "Shelly (60 s)"]])}</label>
      <label>Automatik${_ssSel("automation", r.automation, [["openhab", "openHAB-Regel (PV Controller plant nichts)"], ["pvcontroller", "PV Controller"]])}</label>
      <label class="check"><input type="checkbox" data-k="is_door" ${r.is_door ? "checked" : ""}> Tür (Aussperrschutz)</label>
      <label class="check"><input type="checkbox" data-k="can_position" ${r.can_position !== false ? "checked" : ""}> kann Position (%)</label>
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
      r[inp.dataset.k] = inp.type === "checkbox" ? inp.checked : inp.value.trim();
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
    rooms: SS_ROOMS.map(({ _open, ...r }) => ({ ...r, group: (r.group || "").toLowerCase() })),
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
    if (b) shutSend(b.dataset.id, b.dataset.shutCmd);
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
