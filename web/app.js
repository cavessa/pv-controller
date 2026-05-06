// PV-Controller Dashboard — vanilla JS, kein Framework, kein CDN.

const REFRESH_MS = 10_000;
let refreshTimer = null;
let logTimer = null;
let lastConfig = null;
let phaseLogs = [];
let lastSolaxData = null;

// ── Verlauf sub-tab state ─────────────────────────────
let _vsec        = "tag";
let _vTagDate    = new Date().toISOString().slice(0, 10);
let _vMonth      = new Date().toISOString().slice(0, 7);
let _vYear       = new Date().getFullYear();
let _vStringsDate = new Date().toISOString().slice(0, 10);
let _cachedNormalRatio = null;


const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---------- helpers ----------
function fmtPower(w, { kw = false } = {}) {
  if (w == null || isNaN(w)) return "–";
  if (kw) return (w / 1000).toFixed(2) + " kW";
  return Math.round(w) + " W";
}
function fmtTemp(c) {
  return (c == null || isNaN(c)) ? "–" : c.toFixed(1) + " °C";
}
function fmtEnergy(wh) {
  if (wh == null || isNaN(wh)) return "–";
  if (Math.abs(wh) >= 1000) return (wh / 1000).toFixed(2) + " kWh";
  return Math.round(wh) + " Wh";
}
function fmtDuration(s) {
  if (s == null || isNaN(s) || s < 0) return "–";
  const total = Math.floor(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (h > 0) return `${h}h ${m}min`;
  if (m > 0) return `${m}min ${sec}s`;
  return `${sec}s`;
}
function fmtTime(iso) {
  if (!iso) return "–";
  try { return new Date(iso).toLocaleTimeString("de-DE"); } catch { return iso; }
}
function pillClass(value) {
  if (value === true) return "ok";
  if (value === false) return "warn";
  return "subtle";
}
function showError(msg) {
  const b = $("#error-banner");
  b.textContent = msg;
  b.classList.remove("hidden");
}
function hideError() {
  $("#error-banner").classList.add("hidden");
}

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${text}`);
  }
  return res.json();
}

// ---------- render ----------
function renderStatus(s) {
  hideError();

  // pills
  $("#pill-enabled").textContent = "Automatik: " + (s.runtime.enabled ? "aktiv" : "aus");
  $("#pill-enabled").className = "pill " + (s.runtime.enabled ? "ok" : "warn");

  $("#pill-dry").textContent = "Dry-Run: " + (s.runtime.dry_run ? "an" : "aus");
  $("#pill-dry").className = "pill " + (s.runtime.dry_run ? "warn" : "ok");

  $("#pill-wb").textContent = "Wallbox: " + (s.wallbox.enabled ? "aktiv" : "aus");
  $("#pill-wb").className = "pill " + (s.wallbox.enabled ? "ok" : "warn");

  $("#pill-time").textContent = "aktualisiert " + fmtTime(s.timestamp);

  // summary
  $("#summary-state").textContent = s.summary?.state ?? "–";
  $("#summary-reason").textContent = s.summary?.reason ?? "";
  const sev = s.summary?.severity ?? "ok";
  const badge = $("#summary-severity");
  badge.textContent = sev;
  badge.className = "severity-badge " + sev;

  // PV & Netz
  const h = s.heater;
  $("#m-pv").textContent      = fmtPower(h.pv_power);
  $("#m-grid").textContent    = fmtPower(h.main_meter_power);
  $("#m-heater").textContent  = fmtPower(h.heater_meter_power);
  $("#m-surplus").textContent = fmtPower(h.surplus_without_heater);

  // Speicher
  const heaterAktiv = (h.heater_meter_power ?? 0) > 50
    || ["ph1","ph2","ph3"].some(k => h.phases?.[k] === true);
  $("#m-temp").textContent       = fmtTemp(h.storage_temp);
  $("#m-temp-max").textContent   = fmtTemp(h.storage_max_temp);
  $("#m-temp-resume").textContent = fmtTemp(h.resume_temp);
  const status = h.temperature_status ?? "–";
  const tempBadge = $("#m-temp-status");
  const { label: storageLabel, cls: storageCls } = storageBadge(status, heaterAktiv);
  tempBadge.textContent = storageLabel;
  tempBadge.className = "badge " + storageCls;
  const pct = (h.storage_temp != null && h.storage_max_temp)
    ? Math.max(0, Math.min(100, (h.storage_temp / h.storage_max_temp) * 100))
    : 0;
  $("#m-temp-bar").style.width = pct + "%";

  const storageHeating = heaterAktiv;
  const storageDot = $("#storage-dot");
  storageDot.classList.toggle("hidden", !storageHeating);
  storageDot.classList.toggle("on", storageHeating);

  // resume-temp marker (blau), max ist CSS-fixed bei left:100%
  const marker = $("#m-temp-marker-resume");
  if (marker && h.resume_temp != null && h.storage_max_temp) {
    const pctR = Math.max(0, Math.min(100, (h.resume_temp / h.storage_max_temp) * 100));
    marker.style.left = pctR + "%";
    marker.style.display = "block";
  }

  // active card glow
  storageDot.closest?.(".card")?.classList.toggle("active-storage", storageHeating);

  // Phasen — drei Dots
  const phEl = $("#phases");
  const decisions = h.decisions ?? [];
  if (decisions.length > 0) {
    phEl.innerHTML = `<div class="phases-dots">${
      decisions.map(d => `
        <div class="phase-dot-item">
          <div class="phase-dot ${d.current_state === true ? "on" : ""}"></div>
          <div class="phase-dot-label">${escapeHtml(d.phase)}</div>
        </div>
      `).join("")
    }</div>`;
  } else {
    phEl.innerHTML = `<div class="phases-dots">
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH1</div></div>
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH2</div></div>
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH3</div></div>
    </div>`;
  }

  // Wallbox
  const wb = s.wallbox;
  const wbStatus = wb.status ?? {};
  const wbDec    = wb.decision ?? {};
  const wbCharging = wbStatus.charging === true;
  const wallboxDot = $("#wallbox-dot");
  wallboxDot.classList.toggle("hidden", !wbCharging);
  wallboxDot.classList.toggle("on", wbCharging);

  // Charging indicator
  const ci = $("#charge-indicator");
  if (wb.enabled && wbStatus.car != null) {
    ci.classList.remove("hidden");
    ci.classList.toggle("charging", wbCharging);
    let stateLabel, mode;
    if (wbCharging) {
      stateLabel = "lädt";
      mode = "ok";
    } else if (wbStatus.car === 1) {
      stateLabel = "kein Auto angesteckt";
      mode = "idle";
    } else if (wbStatus.car === 3) {
      stateLabel = "wartet (Freigabe?)";
      mode = "warn";
    } else if (wbStatus.car === 4) {
      stateLabel = "fertig geladen";
      mode = "done";
    } else {
      stateLabel = `car=${wbStatus.car}`;
      mode = "idle";
    }
    ci.dataset.mode = mode;
    $("#charge-state").textContent = stateLabel;
    const p = wbStatus.power_w;
    $("#charge-power").textContent = (p != null && !isNaN(p) && p > 50)
      ? fmtPower(p, { kw: true })
      : (wbCharging ? "0,00 kW" : "");
  } else {
    ci.classList.add("hidden");
  }

  // Energy values (session + lifetime)
  const ce = $("#charge-energy");
  if (wb.enabled && (wbStatus.energy_session_wh != null || wbStatus.energy_total_wh != null || wbStatus.charge_duration_s != null)) {
    ce.classList.remove("hidden");
    $("#charge-session").textContent  = fmtEnergy(wbStatus.energy_session_wh);
    $("#charge-total").textContent    = fmtEnergy(wbStatus.energy_total_wh);
    $("#charge-duration").textContent = fmtDuration(wbStatus.charge_duration_s);
  } else {
    ce.classList.add("hidden");
  }

  $("#wallbox-kv").innerHTML = `
    <div><span>Eco/PV (fup)</span><b>${wbStatus.fup === true ? "ja" : wbStatus.fup === false ? "nein" : "–"}</b></div>
    <div><span>forceState (frc)</span><b>${wbStatus.frc ?? "–"}</b></div>
    <div><span>allowed (alw)</span><b>${wbStatus.alw === true ? "ja" : wbStatus.alw === false ? "nein" : "–"}</b></div>
    <div><span>access (acs)</span><b>${wbStatus.acs === 0 ? "frei" : wbStatus.acs === 1 ? "wartet" : (wbStatus.acs ?? "–")}</b></div>
    <div><span>car</span><b>${wbStatus.car ?? "–"}</b></div>
    <div><span>amp</span><b>${wbStatus.amp ?? "–"} A</b></div>
  `;
  const wbDecEl = $("#wallbox-decision");
  if (!wb.enabled) {
    wbDecEl.innerHTML = `<b>Wallbox in Config deaktiviert.</b>`;
  } else if (wbDec && wbDec.action) {
    const t = (wbDec.target_force_state == null) ? "–" : wbDec.target_force_state;
    const r = wbDec.reason ?? "";
    wbDecEl.innerHTML = `
      <div><b>Entscheidung:</b> ${wbDec.action} (target frc=${t})</div>
      <div>${escapeHtml(r)}</div>
    `;
  } else {
    wbDecEl.innerHTML = "";
  }

  // active card glow – wallbox
  $("#wallbox-dot").closest?.(".card")?.classList.toggle("active-wallbox", wbCharging);

  // wb-banner mit konkreten Werten
  const banner = $("#wb-banner");
  if (banner) {
    const surplus = h.surplus_without_heater;
    const surplusKw = (surplus != null) ? ((-surplus) / 1000).toFixed(1) + " kW" : "–";
    if (!wb.enabled) {
      banner.className = "wb-banner hidden";
    } else if (wbDec.action === "error") {
      banner.className = "wb-banner error";
      banner.textContent = "Wallbox nicht erreichbar";
    } else if (wbDec.action === "release" || (wbStatus.frc === 0 && wbStatus.fup)) {
      if (wbCharging) {
        banner.className = "wb-banner ok";
        banner.textContent = `Wallbox lädt — Überschuss: ${surplusKw}`;
      } else {
        banner.className = "wb-banner";
        banner.textContent = `Wallbox freigegeben — kein Auto lädt (Überschuss: ${surplusKw})`;
      }
    } else if (wbDec.action === "skipped") {
      banner.className = "wb-banner";
      banner.textContent = "Wallbox manueller Modus — kein Eingriff";
    } else {
      banner.className = "wb-banner warn";
      banner.textContent = `Wallbox pausiert — Überschuss: ${surplusKw} (zu gering)`;
    }
  }

  const wbCard = document.getElementById("wallbox-card");
  if (wbCard) wbCard.style.display = wb.enabled ? "" : "none";
  renderEnergyFlow(h, wb, lastCascadeStatus);
}

// ══════════════════════════════════════════════════════════════════
// Energiefluss – haus-zentriertes Layout
// ══════════════════════════════════════════════════════════════════
function renderEnergyFlow(heater, wb, cs) {
  const el = document.getElementById("flow-diagram");
  if (!el) return;

  const W = 360, ACTIVE = "#2ed8a3", PVC = "#fbbf24";
  const NW = 88, NH = 52, H_W = 100;

  // data
  const pvW     = heater.pv_power ?? 0;
  const gridW   = heater.main_meter_power ?? 0;
  const heaterW = heater.heater_meter_power ?? 0;
  const phases  = ["ph1","ph2","ph3"].filter(k => heater.phases?.[k] === true).length;
  const wbSt    = wb.status ?? {};
  const wbChrg  = wbSt.charging === true;
  const wbPwrW  = wbSt.power_w ?? 0;

  // flow state
  const pvOn    = pvW >= 100;
  const feeding = gridW < -50;
  const drawing = gridW > 50;
  const gridOn  = feeding || drawing;
  const heatOn  = heaterW > 50;
  const wbOn    = wbChrg && wbPwrW > 50;

  // Shelly cascade devices
  const shellys = (cs?.devices ?? []).filter(d => d.enabled && d.type?.startsWith("shelly"));

  // Hausverbrauch estimate: PV + net grid - heizstab
  const hvW   = Math.max(0, pvW + gridW - heaterW);
  const hvStr = hvW >= 1000 ? (hvW / 1000).toFixed(2) + " kW" : Math.round(hvW) + " W";

  // node geometry (SVG coordinates, viewBox 0 0 360 H)
  const TOP_Y = 4, MID_Y = 74, BUS_Y = 138, BOT_Y = 148;
  const pv = { x: (W - NW) / 2, y: TOP_Y, cx: W / 2, by: TOP_Y + NH };
  const hs = {
    x: (W - H_W) / 2, y: MID_Y, cx: W / 2,
    ty: MID_Y, by: MID_Y + NH,
    lx: (W - H_W) / 2, ly: MID_Y + NH / 2,
    rx: (W + H_W) / 2, ry: MID_Y + NH / 2,
  };
  const nz = { x: 4, y: MID_Y, rx: 4 + NW, ry: MID_Y + NH / 2 };
  const hz = { x: W - 4 - NW, y: MID_Y, lx: W - 4 - NW, ly: MID_Y + NH / 2 };

  // bottom nodes
  const bots = [];
  const speicherStatusLabel = st => translateStatus(st);
  bots.push({
    icon: "🌡️", label: "SPEICHER",
    value: heater.storage_temp != null ? fmtTemp(heater.storage_temp) : "–",
    sub: speicherStatusLabel(heater.temperature_status),
    aktiv: heatOn, flow: heatOn,
  });
  if (wb.enabled) {
    let wbV = "–", wbS = "";
    if (wbSt.frc != null) {
      if (wbChrg) {
        wbV = wbPwrW > 50 ? (wbPwrW / 1000).toFixed(2) + " kW" : "lädt";
        wbS = "Auto lädt";
      } else {
        wbV = wbSt.frc === 1 ? "pausiert" : wbSt.frc === 0 ? "freigeg." : `frc=${wbSt.frc}`;
        wbS = wbSt.fup ? "Eco/PV" : "manuell";
      }
    }
    bots.push({ icon: "🚗", label: "WALLBOX", value: wbV, sub: wbS, aktiv: wbOn, flow: wbOn });
  }
  shellys.forEach(d => {
    const pw    = d.last_status_power_w ?? 0;
    const flow  = d.is_on;
    const aktiv = d.is_on;
    const pwStr = pw >= 1000 ? (pw / 1000).toFixed(2) + " kW" : Math.round(pw) + " W";
    bots.push({
      icon: "🔌",
      label: (d.name || "Shelly").toUpperCase().slice(0, 11),
      value: d.is_on ? pwStr : "aus",
      sub: d.is_on ? "aktiv" : "wartet",
      aktiv, flow,
    });
  });

  // layout bottom row
  const n  = bots.length;
  const BW = n <= 2 ? 88 : n === 3 ? 84 : n === 4 ? 76 : n === 5 ? 64 : 54;
  const BG = n <= 4 ? 8 : 6;
  const totW = n * BW + Math.max(0, n - 1) * BG;
  const bx0  = Math.max(4, (W - totW) / 2);
  bots.forEach((b, i) => { b.x = bx0 + i * (BW + BG); b.y = BOT_Y; b.w = BW; b.cx = b.x + BW / 2; });

  const SVG_H = n > 0 ? BOT_Y + NH + 6 : MID_Y + NH + 6;

  // helpers
  const esc  = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const rgba = (hex, a) => hex === PVC ? `rgba(251,191,36,${a})` : `rgba(46,216,163,${a})`;
  const L    = (x1, y1, x2, y2, d) =>
    `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="ef-line ef-${d}"/>`;

  function node(x, y, w, nh, { icon, label, value, sub, aktiv, accent = ACTIVE, always = false }) {
    const op  = !aktiv && !always ? ' opacity="0.35"' : "";
    const bdr = aktiv || always ? accent : "rgba(255,255,255,0.12)";
    const bg  = aktiv || always ? rgba(accent, 0.07) : "rgba(255,255,255,0.04)";
    const vc  = aktiv || always ? accent : "rgba(255,255,255,0.6)";
    return `<g transform="translate(${x},${y})"${op}>
  <rect width="${w}" height="${nh}" rx="10" fill="${bg}" stroke="${bdr}" stroke-width="1.5"/>
  <text x="${w/2}" y="16" text-anchor="middle" font-size="14" font-family="'DM Mono',monospace">${icon}</text>
  <text x="${w/2}" y="27" text-anchor="middle" font-size="6.5" fill="rgba(255,255,255,0.5)" letter-spacing="1" font-family="'DM Mono',monospace">${esc(label)}</text>
  <text x="${w/2}" y="39" text-anchor="middle" font-size="12" font-weight="700" fill="${vc}" font-family="'DM Mono',monospace">${esc(value)}</text>
  <text x="${w/2}" y="${nh-5}" text-anchor="middle" font-size="7.5" fill="rgba(255,255,255,0.4)" font-family="'DM Mono',monospace">${esc(sub)}</text>
</g>`;
  }

  function hausNode() {
    return `<g transform="translate(${hs.x},${hs.y})">
  <rect width="${H_W}" height="${NH}" rx="10" fill="rgba(46,216,163,0.05)" stroke="${ACTIVE}55" stroke-width="2"/>
  <text x="${H_W/2}" y="16" text-anchor="middle" font-size="14" font-family="'DM Mono',monospace">🏠</text>
  <text x="${H_W/2}" y="27" text-anchor="middle" font-size="6.5" fill="rgba(255,255,255,0.5)" letter-spacing="1" font-family="'DM Mono',monospace">HAUS</text>
  <text x="${H_W/2}" y="39" text-anchor="middle" font-size="12" font-weight="700" fill="${ACTIVE}" font-family="'DM Mono',monospace">${esc(hvStr)}</text>
  <text x="${H_W/2}" y="${NH-5}" text-anchor="middle" font-size="7.5" fill="rgba(255,255,255,0.4)" font-family="'DM Mono',monospace">Verbrauch</text>
</g>`;
  }

  // connections
  const conn = [];
  if (pvOn)   conn.push(L(pv.cx, pv.by, hs.cx, hs.ty, "down"));
  if (gridOn) conn.push(L(nz.rx, nz.ry, hs.lx, hs.ly, drawing ? "right" : "left"));
  if (heatOn) conn.push(L(hs.rx, hs.ry, hz.lx, hz.ly, "right"));

  const fb = bots.filter(b => b.flow);
  fb.forEach(b => {
    if (b.cx === hs.cx) {
      conn.push(L(hs.cx, hs.by, b.cx, b.y, "down"));
    } else {
      conn.push(`<path d="M ${hs.cx} ${hs.by} L ${hs.cx} ${BUS_Y} L ${b.cx} ${BUS_Y} L ${b.cx} ${b.y}" class="ef-line ef-down"/>`);
    }
  });

  // display values
  const absKw = (Math.abs(gridW) / 1000).toFixed(2) + " kW";
  const nzVal = Math.abs(gridW) > 50 ? absKw : "~0 W";
  const nzSub = feeding ? "Einsp." : drawing ? "Bezug" : "";
  const hzKw  = (heaterW / 1000).toFixed(2) + " kW";

  el.innerHTML = `<svg viewBox="0 0 ${W} ${SVG_H}" width="100%" style="display:block;margin-top:8px">
${conn.join("\n")}
${node(pv.x, pv.y, NW, NH, { icon:"☀️", label:"PV", value:(pvW/1000).toFixed(2)+" kW", sub:"", aktiv:pvOn, accent:PVC, always:true })}
${node(nz.x, nz.y, NW, NH, { icon:"⚡", label:"NETZ", value:nzVal, sub:nzSub, aktiv:gridOn })}
${hausNode()}
${node(hz.x, hz.y, NW, NH, { icon:"🔥", label:"HEIZSTAB", value:hzKw, sub:`${phases}/3 Ph.`, aktiv:heatOn })}
${bots.map(b => node(b.x, b.y, b.w, NH, { icon:b.icon, label:b.label, value:b.value, sub:b.sub, aktiv:b.aktiv })).join("\n")}
</svg>`;
}

// ---------- solax / PV live + heute ----------
function renderSolax(d) {
  const totalEl       = document.getElementById("pv-live-total");
  const stringsEl     = document.getElementById("pv-live-strings");
  const feedinEl      = document.getElementById("pv-live-feedin");
  const consumptionEl = document.getElementById("pv-live-consumption");

  if (d == null) {
    console.log("[renderSolax] d=null – no data available, showing n/a");
    if (totalEl) { totalEl.textContent = "n/a"; totalEl.style.color = "var(--text-dim)"; }
    if (stringsEl) stringsEl.innerHTML = "";
    if (feedinEl) feedinEl.textContent = "–";
    if (consumptionEl) consumptionEl.textContent = "–";
    renderHausverbrauch(null);
    renderToday(null);
    return;
  }

  renderKpiGauges(d);

  if (totalEl) {
    totalEl.textContent = Math.round(d.pv_total_w) + " W";
    totalEl.style.color = d.pv_total_w > 50 ? "var(--c-pv)" : "var(--text-dim)";
  }

  if (stringsEl) {
    const maxW = Math.max(d.pv_total_w, 1);
    stringsEl.innerHTML = [
      { n: "STR1", w: d.pv1_power_w, v: d.pv1_voltage_v, a: d.pv1_current_a },
      { n: "STR2", w: d.pv2_power_w, v: d.pv2_voltage_v, a: d.pv2_current_a },
    ].map(s => {
      const pct = Math.max(2, Math.min(100, s.w / maxW * 100)).toFixed(1);
      return `<div class="pv-string">
        <div class="pv-string-label">${s.n}</div>
        <div class="pv-string-bar-wrap"><div class="pv-string-bar" style="width:${pct}%"></div></div>
        <div class="pv-string-val">${Math.round(s.w)} W</div>
        <div class="pv-string-sub">${s.v.toFixed(0)} V · ${s.a.toFixed(1)} A</div>
      </div>`;
    }).join("");
  }

  if (feedinEl) {
    const fi = d.feed_in_w;
    feedinEl.textContent = (fi >= 0 ? "+" : "") + Math.round(fi) + " W";
    feedinEl.style.color = fi >= 0 ? "var(--ok)" : "var(--error)";
  }

  if (consumptionEl) {
    consumptionEl.textContent = Math.round(d.consumption_w) + " W";
  }

  renderHausverbrauch(d);
  renderToday(d);
}

function renderHausverbrauch(d) {
  const valEl   = document.getElementById("hv-val");
  const todayEl = document.getElementById("hv-today");
  if (!valEl || !todayEl) return;
  if (d == null) {
    valEl.textContent   = "–";
    valEl.style.color   = "var(--text-dim)";
    todayEl.textContent = "Heute: –";
    return;
  }
  const w = Math.round(d.consumption_w);
  valEl.textContent = w >= 1000 ? (w / 1000).toFixed(2) + " kW" : w + " W";
  valEl.style.color = w > 3000 ? "var(--warn)" : "var(--text)";
  const kwh = d.self_consumption_kwh;
  todayEl.textContent = "Heute: " + (kwh != null ? kwh.toFixed(1) + " kWh" : "–");
}

function renderToday(d) {
  const el = document.getElementById("sol-today");
  if (!el) return;

  if (!d) {
    el.innerHTML = '<div class="chart-empty">Keine Daten verfügbar.</div>';
    return;
  }

  const maxVal = Math.max(d.yield_today_kwh, 0.01);
  const rows = [
    { label: "Erzeugt",      val: d.yield_today_kwh,       color: "var(--c-pv)" },
    { label: "Eingespeist",  val: d.grid_out_today_kwh,    color: "var(--ok)" },
    { label: "Netzbezug",    val: d.grid_in_today_kwh,     color: "var(--c-grid)" },
    { label: "Eigenverbr.",  val: d.self_consumption_kwh,  color: "var(--c-storage)" },
  ];

  el.innerHTML = rows.map(r => {
    const pct  = Math.max(0, Math.min(100, r.val / maxVal * 100)).toFixed(1);
    const disp = r.val >= 10 ? r.val.toFixed(1) : r.val.toFixed(2);
    return `<div class="today-row">
      <div class="today-label">${r.label}</div>
      <div class="today-bar-wrap"><div class="today-bar" style="width:${pct}%;background:${r.color}"></div></div>
      <div class="today-val">${disp} kWh</div>
    </div>`;
  }).join("");
}

// ---------- KPI Gauges ----------
function renderKpiGauges(d) {
  const el = document.getElementById("kpi-gauges");
  if (!el) return;

  const hasData = d != null && (d.yield_today_kwh || 0) >= 0.01;
  const selfuse = hasData ? (d.self_consumption_kwh || 0) : 0;
  const feedin  = hasData ? (d.grid_in_today_kwh    || 0) : 0;
  const yield_  = hasData ? d.yield_today_kwh : 1;
  const selfusePct = hasData && yield_ > 0 ? Math.min(100, Math.round(selfuse / yield_ * 100)) : 0;
  const autarkyPct = hasData && (selfuse + feedin) > 0
    ? Math.min(100, Math.round(selfuse / (selfuse + feedin) * 100)) : 0;

  const gaugeColor = p => p >= 70 ? "var(--ok)" : p >= 40 ? "var(--warn)" : "var(--error)";

  function gauge(pct, label, placeholder) {
    const color = placeholder ? "rgba(255,255,255,0.18)" : gaugeColor(pct);
    const cx = 80, cy = 70, r = 50;
    const bgPath = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
    let fgPath = "";
    if (!placeholder && pct > 0) {
      const angle = (pct / 100) * Math.PI;
      const ex = (cx - r * Math.cos(angle)).toFixed(2);
      const ey = (cy - r * Math.sin(angle)).toFixed(2);
      fgPath = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${ex} ${ey}`;
    }
    const txt = placeholder ? "–" : pct + "%";
    return `<div class="kpi-gauge-wrap">
      <svg viewBox="0 0 160 120" class="kpi-gauge" style="display:block;overflow:visible">
        <path d="${bgPath}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="10" stroke-linecap="round"/>
        ${fgPath ? `<path d="${fgPath}" fill="none" stroke="${color}" stroke-width="10" stroke-linecap="round"/>` : ""}
        <text x="80" y="55" text-anchor="middle" dominant-baseline="middle" class="gauge-val" fill="${color}">${txt}</text>
        <text x="80" y="95" text-anchor="middle" class="gauge-label">${escapeHtml(label)}</text>
      </svg>
    </div>`;
  }
  el.innerHTML = `<div class="kpi-gauges-row">
    ${gauge(selfusePct, "Eigenverbrauch", !hasData)}
    ${gauge(autarkyPct, "Autarkie",       !hasData)}
  </div>`;
}

// ---------- Verlauf / History Charts ----------
function renderDailyChart(entries) {
  const wrap = document.getElementById("chart-daily-wrap");
  if (!wrap) return;
  if (!entries || entries.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Stundendaten. Logging läuft ab nächster vollen Stunde.</div>';
    return;
  }
  const VW = 480, VH = 90;
  const pad = { t: 6, r: 6, b: 22, l: 40 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;

  const pts = entries.map(e => {
    const h = parseInt((e.ts || "").split(" ")[1]?.split(":")[0] || "0", 10);
    return { h, pv: e.pv_w || 0, feed: Math.max(0, e.feed_in_w || 0), cons: Math.abs(e.consumption_w || 0) };
  }).sort((a, b) => a.h - b.h);

  const maxW = Math.max(...pts.flatMap(p => [p.pv, p.feed, p.cons]), 100);
  const sx = h => pad.l + (h / 23) * cw;
  const sy = w => pad.t + ch - (w / maxW * ch);

  let yElems = "", xElems = "";
  const yStep = niceStep(maxW, 3);
  for (let yv = 0; yv <= maxW + 1; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv >= 1000 ? (yv/1000).toFixed(0)+"k" : yv}</text>`;
  }
  for (let h = 0; h <= 23; h += 3) {
    const xv = sx(h).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${pad.t+ch}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH-2}" text-anchor="middle">${h}h</text>`;
  }
  const now = new Date();
  const nowX = sx(now.getHours() + now.getMinutes() / 60).toFixed(1);
  const path = key => pts.map((p, i) => `${i===0?"M":"L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`).join(" ");

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}${xElems}</g>
    <line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${pad.t+ch}" class="now-line"/>
    <path d="${path('cons')}" fill="none" stroke="rgba(255,255,255,0.25)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('feed')}" fill="none" stroke="var(--ok)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('pv')}" fill="none" stroke="var(--c-pv)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function renderStringChart(entries) {
  const wrap = document.getElementById("chart-strings-wrap");
  if (!wrap) return;
  if (!entries || entries.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Stundendaten.</div>';
    return;
  }
  const VW = 480, VH = 90;
  const pad = { t: 6, r: 6, b: 22, l: 40 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;

  const pts = entries.map(e => {
    const h = parseInt((e.ts || "").split(" ")[1]?.split(":")[0] || "0", 10);
    return { h, s1: e.str1_w || 0, s2: e.str2_w || 0 };
  }).sort((a, b) => a.h - b.h);

  const maxW = Math.max(...pts.flatMap(p => [p.s1, p.s2]), 100);
  const sx = h => pad.l + (h / 23) * cw;
  const sy = w => pad.t + ch - (w / maxW * ch);

  let yElems = "", xElems = "";
  const yStep = niceStep(maxW, 3);
  for (let yv = 0; yv <= maxW + 1; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv >= 1000 ? (yv/1000).toFixed(0)+"k" : yv}</text>`;
  }
  for (let h = 0; h <= 23; h += 3) {
    const xv = sx(h).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${pad.t+ch}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH-2}" text-anchor="middle">${h}h</text>`;
  }
  const now = new Date();
  const nowX = sx(now.getHours() + now.getMinutes() / 60).toFixed(1);
  const path = key => pts.map((p, i) => `${i===0?"M":"L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`).join(" ");

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}${xElems}</g>
    <line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${pad.t+ch}" class="now-line"/>
    <path d="${path('s2')}" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('s1')}" fill="none" stroke="var(--c-pv)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function renderWeekChart(dailyData) {
  const wrap = document.getElementById("chart-week-wrap");
  if (!wrap) return;
  const data = dailyData.slice(-7);
  if (!data.length) { wrap.innerHTML = '<div class="chart-empty">Noch keine Tagesdaten.</div>'; return; }

  const VW = 480, VH = 90;
  const pad = { t: 6, r: 6, b: 22, l: 36 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const n = Math.max(data.length, 7);
  const barW = cw / n * 0.62, barGap = cw / n;
  const offset = (7 - data.length) * barGap / 2;

  const maxVal = Math.max(...data.map(d => (d.selfuse_kwh||0) + (d.feed_out_kwh||0)), 0.5);
  const sy = v => pad.t + ch - (v / maxVal * ch);
  const x0 = i => pad.l + offset + i * barGap + (barGap - barW) / 2;

  let yElems = "";
  const yStep = niceStep(maxVal, 3);
  for (let yv = 0; yv <= maxVal + 0.01; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(1)}</text>`;
  }

  const weekdays = ["So","Mo","Di","Mi","Do","Fr","Sa"];
  let bars = "";
  data.forEach((d, i) => {
    const selfuse = d.selfuse_kwh || 0, feedout = d.feed_out_kwh || 0;
    const x = x0(i), bot = pad.t + ch;
    const feedH = Math.max(0, feedout / maxVal * ch);
    const selfH = Math.max(0, selfuse / maxVal * ch);
    bars += `<rect x="${x.toFixed(1)}" y="${(bot-feedH).toFixed(1)}" width="${barW.toFixed(1)}" height="${feedH.toFixed(1)}" rx="2" fill="var(--ok)" opacity="0.7"/>`;
    bars += `<rect x="${x.toFixed(1)}" y="${(bot-feedH-selfH).toFixed(1)}" width="${barW.toFixed(1)}" height="${selfH.toFixed(1)}" rx="2" fill="var(--c-storage)" opacity="0.85"/>`;
    const dayIdx = new Date(d.date + "T12:00:00").getDay();
    bars += `<text x="${(x+barW/2).toFixed(1)}" y="${VH-3}" text-anchor="middle">${weekdays[dayIdx]}</text>`;
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}</g>${bars}
  </svg>`;
}

function renderMonthChart(dailyData) {
  const wrap = document.getElementById("chart-month-wrap");
  if (!wrap) return;
  if (!dailyData.length) { wrap.innerHTML = '<div class="chart-empty">Noch keine Tagesdaten.</div>'; return; }

  const VW = 480, VH = 90;
  const pad = { t: 6, r: 6, b: 22, l: 36 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const n = dailyData.length;
  const barW = Math.max(2, cw / n * 0.78), barGap = cw / n;
  const maxVal = Math.max(...dailyData.map(d => d.pv_kwh || 0), 0.5);
  const sy = v => pad.t + ch - (v / maxVal * ch);
  const x0 = i => pad.l + i * barGap + (barGap - barW) / 2;

  let yElems = "";
  const yStep = niceStep(maxVal, 3);
  for (let yv = 0; yv <= maxVal + 0.01; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(1)}</text>`;
  }

  let bars = "";
  dailyData.forEach((d, i) => {
    const val = d.pv_kwh || 0, h = Math.max(1, val / maxVal * ch);
    const x = x0(i);
    const opacity = (0.3 + (val / maxVal) * 0.65).toFixed(2);
    bars += `<rect x="${x.toFixed(1)}" y="${(pad.t+ch-h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="1" fill="var(--c-pv)" opacity="${opacity}"/>`;
    if (i === 0 || i % 5 === 0 || i === n - 1) {
      const day = parseInt((d.date || "").split("-")[2] || "0");
      bars += `<text x="${(x+barW/2).toFixed(1)}" y="${VH-3}" text-anchor="middle">${day}.</text>`;
    }
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}</g>${bars}
  </svg>`;
}

function checkStringAlert(entries) {
  const el = document.getElementById("string-alert");
  if (!el) return;
  const daytime = (entries || []).filter(e => (e.pv_w || 0) > 200);
  if (daytime.length < 2) { el.className = "string-alert hidden"; return; }
  const latest = daytime[daytime.length - 1];
  if ((latest.str1_w || 0) < 10) {
    el.textContent = "⚠️ String 1 liefert keine Leistung – Verschattung oder Ausfall prüfen.";
    el.className = "string-alert error"; return;
  }
  if ((latest.str2_w || 0) < 10) {
    el.textContent = "⚠️ String 2 liefert keine Leistung – Verschattung oder Ausfall prüfen.";
    el.className = "string-alert error"; return;
  }
  const valid = daytime.filter(e => (e.str2_w || 0) > 50);
  if (valid.length >= 3 && latest.str2_w > 50) {
    const avg = valid.reduce((s, e) => s + e.str1_w / e.str2_w, 0) / valid.length;
    const cur = latest.str1_w / latest.str2_w;
    const dev = Math.abs(cur - avg) / Math.max(avg, 0.01);
    if (dev > 0.3) {
      el.textContent = `⚠️ String-Verhältnis weicht ${Math.round(dev*100)}% vom Tagesdurchschnitt ab – Verschattung oder Verschmutzung prüfen.`;
      el.className = "string-alert warn"; return;
    }
  }
  el.className = "string-alert hidden";
}

// ── Verlauf helpers ───────────────────────────────────

function _dateAddDays(iso, n) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
function _monthAdd(ym, n) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}
function _fmtDate(iso) {
  try {
    return new Date(iso + "T12:00:00").toLocaleDateString("de-DE",
      { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  } catch { return iso; }
}
function _fmtMonth(ym) {
  try {
    return new Date(ym + "-01T12:00:00").toLocaleDateString("de-DE",
      { month: "long", year: "numeric" });
  } catch { return ym; }
}

function showVerlaufSection(name) {
  _vsec = name;
  $$(".verlauf-nav-btn").forEach(b => b.classList.toggle("active", b.dataset.vsec === name));
  $$(".verlauf-section").forEach(s => s.classList.toggle("hidden", s.id !== "verlauf-" + name));
  _loadCurrentVerlauf();
}

function _loadCurrentVerlauf() {
  if (_vsec === "tag")     loadVerlaufTag(_vTagDate);
  if (_vsec === "woche")   loadVerlaufWoche();
  if (_vsec === "monat")   loadVerlaufMonat(_vMonth);
  if (_vsec === "jahr")    loadVerlaufJahr(_vYear);
  if (_vsec === "strings") loadVerlaufStrings(_vStringsDate);
}

async function loadVerlauf() {
  try {
    const [cascadeLog, cascadeDevs] = await Promise.all([
      api("/api/cascade/log?limit=200").catch(() => ({ entries: [] })),
      api("/api/cascade/devices").catch(() => ({ devices: [] })),
    ]);
    renderCascadeHistory(cascadeLog.entries || [], cascadeDevs.devices || []);
  } catch (e) {
    console.error("Cascade history error:", e);
  }
  _loadCurrentVerlauf();
}

async function loadVerlaufTag(dateStr) {
  _vTagDate = dateStr;
  const today = new Date().toISOString().slice(0, 10);
  const labelEl = document.getElementById("verlauf-tag-label");
  if (labelEl) labelEl.textContent = dateStr === today ? "Heute" : _fmtDate(dateStr);
  const nextBtn = document.getElementById("verlauf-tag-next");
  if (nextBtn) nextBtn.disabled = dateStr >= today;
  try {
    const hourly = await api(`/api/history/hourly?for_date=${dateStr}`);
    renderDailyChart(hourly.entries || []);
    renderStringChart(hourly.entries || []);
    checkStringAlert(hourly.entries || []);
  } catch (e) {
    showError("Tagesverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufWoche() {
  try {
    const daily = await api("/api/history/daily?days=7");
    renderWeekChart(daily.entries || []);
  } catch (e) {
    showError("Wochenverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufMonat(monthStr) {
  _vMonth = monthStr;
  const labelEl = document.getElementById("verlauf-monat-label");
  if (labelEl) labelEl.textContent = _fmtMonth(monthStr);
  const thisMonth = new Date().toISOString().slice(0, 7);
  const nextBtn = document.getElementById("verlauf-monat-next");
  if (nextBtn) nextBtn.disabled = monthStr >= thisMonth;
  try {
    const data = await api(`/api/history/month/${monthStr}`);
    renderMonthChart(data.entries || []);
    _renderKpiRow("verlauf-monat-kpis", data.entries || []);
  } catch (e) {
    showError("Monatsverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufJahr(year) {
  _vYear = year;
  const labelEl = document.getElementById("verlauf-jahr-label");
  if (labelEl) labelEl.textContent = String(year);
  const thisYear = new Date().getFullYear();
  const nextBtn = document.getElementById("verlauf-jahr-next");
  if (nextBtn) nextBtn.disabled = year >= thisYear;
  try {
    const data = await api(`/api/history/year/${year}`);
    renderYearChart(data.entries || []);
    _renderKpiRow("verlauf-jahr-kpis", data.entries || [], true);
  } catch (e) {
    showError("Jahresverlauf konnte nicht geladen werden: " + e.message);
  }
}

function _renderKpiRow(elemId, entries, round0 = false) {
  const wrap = document.getElementById(elemId);
  if (!wrap) return;
  if (!entries.length) { wrap.innerHTML = ""; return; }
  const total = entries.reduce((s, e) => ({
    pv:      s.pv      + (e.pv_kwh      || 0),
    feed:    s.feed    + (e.feed_out_kwh || 0),
    selfuse: s.selfuse + (e.selfuse_kwh  || 0),
  }), { pv: 0, feed: 0, selfuse: 0 });
  const fmt = v => round0 ? v.toFixed(0) : v.toFixed(1);
  wrap.innerHTML = `
    <div class="verlauf-kpi"><div class="verlauf-kpi-label">PV gesamt</div><div class="verlauf-kpi-val">${fmt(total.pv)} kWh</div></div>
    <div class="verlauf-kpi"><div class="verlauf-kpi-label">Einspeisung</div><div class="verlauf-kpi-val">${fmt(total.feed)} kWh</div></div>
    <div class="verlauf-kpi"><div class="verlauf-kpi-label">Eigenverbr.</div><div class="verlauf-kpi-val">${fmt(total.selfuse)} kWh</div></div>
  `;
}

function renderYearChart(entries) {
  const wrap = document.getElementById("chart-year-wrap");
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Jahresdaten vorhanden.</div>';
    return;
  }
  const MONTHS_DE = ["Jan","Feb","Mär","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"];
  const VW = 460, VH = 100;
  const pad = { t: 12, r: 8, b: 24, l: 36 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;
  const n = entries.length;
  const maxVal = Math.max(...entries.map(e => e.pv_kwh || 0), 1);
  const gap = 3;
  const barW = (cw - (n - 1) * gap) / n;
  const sy = v => pad.t + ch - (v / maxVal) * ch;
  const x0 = i => pad.l + i * (barW + gap);

  let yElems = "";
  const yStep = niceStep(maxVal, 3);
  for (let yv = 0; yv <= maxVal + 0.01; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l + cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l - 4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(0)}</text>`;
  }

  let bars = "";
  entries.forEach((d, i) => {
    const val = d.pv_kwh || 0;
    const h = Math.max(1, val / maxVal * ch);
    const x = x0(i);
    bars += `<rect x="${x.toFixed(1)}" y="${(pad.t + ch - h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="1" fill="var(--c-pv)" opacity="0.8"/>`;
    const mi = parseInt((d.month || "").split("-")[1] || "1") - 1;
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${VH - 4}" text-anchor="middle">${MONTHS_DE[mi] ?? ""}</text>`;
    if (val >= 1) {
      bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${(pad.t + ch - h - 3).toFixed(1)}" text-anchor="middle" font-size="7" fill="rgba(255,255,255,0.5)">${val.toFixed(0)}</text>`;
    }
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}</g>${bars}
  </svg>`;
}

// ── Strings sub-tab ──────────────────────────────────────────────────────────

async function loadVerlaufStrings(dateStr) {
  _vStringsDate = dateStr;
  const labelEl = document.getElementById("verlauf-strings-label");
  if (labelEl) labelEl.textContent = dateStr;
  const nextBtn = document.getElementById("verlauf-strings-next");
  const today = new Date().toISOString().slice(0, 10);
  if (nextBtn) nextBtn.disabled = (_vStringsDate >= today);
  try {
    const [dayData, ratioData] = await Promise.all([
      api(`/api/history/hourly?for_date=${dateStr}`),
      api("/api/strings/ratio?days=30"),
    ]);
    _cachedNormalRatio = ratioData.normal_ratio;
    renderStringsDayChart(dayData.entries || []);
    renderStringRatioChart(ratioData.entries || [], ratioData.normal_ratio);
    const alertData = await api("/api/strings/alerts?days=30");
    renderStringAlertsList(alertData.entries || []);
  } catch (e) {
    showError("Strings-Verlauf konnte nicht geladen werden: " + e.message);
  }
}

function renderStringsDayChart(entries) {
  const wrap = document.getElementById("chart-strings-day-wrap");
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Daten für diesen Tag.</div>';
    return;
  }
  const VW = 460, VH = 100;
  const pad = { t: 12, r: 8, b: 22, l: 40 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;
  const pts = entries.map(e => ({
    h: new Date(e.timestamp).getHours(),
    s1: e.str1_w || 0,
    s2: e.str2_w || 0,
  }));
  const maxV = Math.max(...pts.flatMap(p => [p.s1, p.s2]), 1);
  const sx = h => pad.l + (h / 23) * cw;
  const sy = v => pad.t + ch * (1 - v / maxV);

  let yElems = "";
  const yStep = niceStep(maxV, 3);
  for (let yv = 0; yv <= maxV + 1; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l + cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l - 4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${(yv / 1000).toFixed(1)}</text>`;
  }

  const mkPath = (key, color) => {
    if (!pts.length) return "";
    const d = pts.map((p, i) =>
      `${i === 0 ? "M" : "L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`
    ).join(" ");
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  };

  const xLabels = [0, 6, 12, 18, 23].map(h =>
    `<text x="${sx(h).toFixed(1)}" y="${VH - 2}" text-anchor="middle">${String(h).padStart(2, "0")}</text>`
  ).join("");

  wrap.innerHTML = `<svg viewBox="0 0 ${VW} ${VH}" class="spark-svg chart-svg">
    <g class="grid-g">${yElems}</g>
    ${mkPath("s1", "var(--c-pv)")}
    ${mkPath("s2", "var(--c-str2)")}
    <g class="axis-g">${xLabels}</g>
  </svg>`;
}

function renderStringRatioChart(entries, normalRatio) {
  const wrap = document.getElementById("chart-strings-ratio-wrap");
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Verhältnis-Daten vorhanden.</div>';
    return;
  }
  const VW = 460, VH = 100;
  const pad = { t: 12, r: 8, b: 22, l: 36 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;
  const n = entries.length;
  const allRatios = entries.map(e => e.avg).filter(v => v !== null);
  const minR = Math.min(...allRatios, normalRatio != null ? normalRatio * 0.8 : Infinity);
  const maxR = Math.max(...allRatios, normalRatio != null ? normalRatio * 1.2 : 0, 0.1);
  const rRange = Math.max(maxR - minR, 0.3);
  const sy = v => pad.t + ch * (1 - (v - minR) / rRange);
  const sx = i => pad.l + (n > 1 ? (i / (n - 1)) * cw : cw / 2);

  let bandElems = "";
  if (normalRatio != null) {
    const bandTop = sy(normalRatio * 1.15).toFixed(1);
    const bandBot = sy(normalRatio * 0.85).toFixed(1);
    bandElems = `<rect x="${pad.l}" y="${bandTop}" width="${cw}" height="${(parseFloat(bandBot) - parseFloat(bandTop)).toFixed(1)}" fill="rgba(255,255,255,0.07)"/>`;
    const ny = sy(normalRatio).toFixed(1);
    bandElems += `<line x1="${pad.l}" y1="${ny}" x2="${pad.l + cw}" y2="${ny}" stroke="rgba(255,255,255,0.2)" stroke-width="1" stroke-dasharray="4,3"/>`;
  }

  let line = "";
  if (n >= 2) {
    const segs = entries.map((e, i) => e.avg !== null
      ? `${i === 0 ? "M" : "L"}${sx(i).toFixed(1)},${sy(e.avg).toFixed(1)}`
      : null
    ).filter(Boolean);
    if (segs.length) {
      line = `<path d="${segs.join(" ")}" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-linejoin="round"/>`;
    }
  }

  let anomalyDots = "";
  if (normalRatio != null) {
    entries.forEach((e, i) => {
      if (e.avg === null) return;
      if (Math.abs(e.avg - normalRatio) / normalRatio * 100 > 30) {
        anomalyDots += `<circle cx="${sx(i).toFixed(1)}" cy="${sy(e.avg).toFixed(1)}" r="3" fill="#f87171" opacity="0.9"/>`;
      }
    });
  }

  const step = Math.max(1, Math.floor(n / 6));
  const xLabels = entries.map((e, i) => {
    if (i % step !== 0 && i !== n - 1) return "";
    return `<text x="${sx(i).toFixed(1)}" y="${VH - 2}" text-anchor="middle">${(e.day || "").slice(5)}</text>`;
  }).join("");

  wrap.innerHTML = `<svg viewBox="0 0 ${VW} ${VH}" class="spark-svg chart-svg">
    ${bandElems}
    ${line}
    ${anomalyDots}
    <g class="axis-g">${xLabels}</g>
  </svg>`;
}

function renderStringAlertsList(alerts) {
  const el = document.getElementById("strings-alerts-list");
  if (!el) return;
  if (!alerts.length) {
    el.innerHTML = '<div class="chart-empty">Keine Anomalie-Meldungen in den letzten 30 Tagen.</div>';
    return;
  }
  el.innerHTML = alerts.map(a => {
    const cls = a.level === "error" ? "string-alert error" : "string-alert warn";
    const ts = (a.ts || "").replace("T", " ").slice(0, 16);
    const ratioStr = a.ratio != null ? ` · Ratio: ${(+a.ratio).toFixed(2)}` : "";
    return `<div class="${cls}" style="margin-bottom:8px;">
      <div style="font-size:10px;color:var(--text-muted);margin-bottom:3px;">${ts}</div>
      ${a.message}
      <div style="font-size:10px;color:var(--text-muted);margin-top:3px;">STR1: ${a.str1_w ?? "–"} W · STR2: ${a.str2_w ?? "–"} W${ratioStr}</div>
    </div>`;
  }).join("");
}

async function _checkLiveStringAlert() {
  const el = document.getElementById("string-dash-alert");
  if (!el) return;
  try {
    const data = await api("/api/strings/alerts?days=1");
    const active = (data.entries || []);
    if (!active.length) { el.className = "string-dash-alert hidden"; return; }
    const top = active[0];
    el.className = `string-dash-alert ${top.level === "error" ? "error" : "warn"}`;
    el.textContent = "⚡ " + top.message;
  } catch {
    // ignore — non-critical
  }
}

function fmtPhaseAction(a) {
  const map = { "TURN_ON": "einschalten", "TURN_OFF": "ausschalten", "UNCHANGED": "unverändert" };
  return map[a] ?? a ?? "–";
}

function translateStatus(status) {
  const labels = {
    'AT_OR_ABOVE_MAX':  'Max erreicht',
    'ABOVE_MAX':        'über Max',
    'BELOW_RESUME':     'unter Resume',
    'HYSTERESIS_BAND':  'Hysterese',
    'MAX_REACHED':      'Max erreicht',
    'HEATING':          'lädt',
    'HYSTERESIS':       'Hysterese',
    'IDLE':             'Standby',
    'COOLDOWN':         'Abkühlung',
    'WARMING_UP':       'heizt auf',
    'BELOW_MIN':        'unter Minimum',
  };
  return labels[status] || status || '–';
}

function storageBadge(tempStatus, heaterActive) {
  if (heaterActive) return { label: "Speicher lädt", cls: "ok" };
  switch (tempStatus) {
    case "AT_OR_ABOVE_MAX": return { label: "Max erreicht",    cls: "warn" };
    case "HYSTERESIS_BAND": return { label: "Speicher bereit", cls: "ok" };
    case "BELOW_RESUME":    return { label: "Wartet auf PV",   cls: "" };
    default:                return { label: translateStatus(tempStatus) ?? "–", cls: "" };
  }
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;");
}

// ---------- temperature chart ----------
function niceStep(range, targetTicks) {
  const rough = range / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const n = rough / mag;
  const nice = n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10;
  return nice * mag;
}

function xTickStep(rangeMin) {
  if (rangeMin <= 120) return 15;
  if (rangeMin <= 360) return 30;
  return 60;
}
function xTickLabel(m) {
  const h = Math.floor(m / 60);
  const min = m % 60;
  return min === 0 ? `${h}h` : `${h}:${String(min).padStart(2, "0")}`;
}

function renderTempChart(entries, maxTemp) {
  const wrap = $("#temp-chart-wrap");
  if (!entries || entries.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Verlaufsdaten für heute.</div>';
    return;
  }

  const VW = 480, VH = 82;
  const pad = { t: 6, r: 6, b: 20, l: 34 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;

  const pts = entries.map(e => {
    const d = new Date(e.t);
    return { x: d.getHours() * 60 + d.getMinutes(), y: e.temp };
  }).sort((a, b) => a.x - b.x);

  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const xLo = Math.max(0, pts[0].x - 15);
  const xHi = Math.max(pts[pts.length - 1].x, nowMin) + 15;
  const xRange = xHi - xLo;

  const allTemps = pts.map(p => p.y);
  if (maxTemp != null) allTemps.push(maxTemp);
  const yDataMin = Math.min(...allTemps);
  const yDataMax = Math.max(...allTemps);
  const yRange = Math.max(yDataMax - yDataMin, 5);
  const yLo = yDataMin - yRange * 0.15;
  const yHi = yDataMax + yRange * 0.15;

  const sx = x => pad.l + ((x - xLo) / xRange) * cw;
  const sy = y => pad.t + ch - ((y - yLo) / (yHi - yLo)) * ch;

  const linePts = pts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  const areaPath = linePts
    + ` L${sx(last.x).toFixed(1)},${(pad.t + ch).toFixed(1)}`
    + ` L${sx(pts[0].x).toFixed(1)},${(pad.t + ch).toFixed(1)} Z`;

  // x grid + labels
  const step = xTickStep(xRange);
  let xElems = "";
  for (let m = Math.ceil(xLo / step) * step; m <= xHi; m += step) {
    const xv = sx(m).toFixed(1);
    const yb = (pad.t + ch).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${yb}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH - 3}" text-anchor="middle">${xTickLabel(m)}</text>`;
  }

  // y grid + labels
  let yElems = "";
  const yStep = niceStep(yHi - yLo, 3);
  for (let yv = Math.ceil(yLo / yStep) * yStep; yv <= yHi; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l + cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l - 4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(0)}°</text>`;
  }

  // max temp threshold
  let maxLine = "";
  if (maxTemp != null) {
    const ypx = sy(maxTemp).toFixed(1);
    maxLine = `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l + cw).toFixed(1)}" y2="${ypx}" class="max-line"/>`;
  }

  // current time indicator
  const nowX = sx(nowMin).toFixed(1);
  const nowLine = `<line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${(pad.t + ch).toFixed(1)}" class="now-line"/>`;

  const lastDot = `<circle cx="${sx(last.x).toFixed(1)}" cy="${sy(last.y).toFixed(1)}" r="3" class="last-dot"/>`;

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
  <defs>
    <linearGradient id="tcg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#a78bfa" stop-opacity="1"/>
      <stop offset="100%" stop-color="#a78bfa" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <g>${xElems}${yElems}</g>
  ${maxLine}${nowLine}
  <path d="${areaPath}" fill="url(#tcg)" class="temp-area"/>
  <path d="${linePts}" fill="none" class="temp-line"/>
  ${lastDot}
</svg>`;
}

function renderWbChart(entries) {
  const wrap = $("#wb-chart-wrap");
  const pts = (entries || [])
    .filter(e => e.wb_w != null)
    .map(e => {
      const d = new Date(e.t);
      return { x: d.getHours() * 60 + d.getMinutes(), y: e.wb_w / 1000 };
    })
    .sort((a, b) => a.x - b.x);

  if (pts.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Ladeverlaufsdaten für heute.</div>';
    return;
  }

  const VW = 480, VH = 82;
  const pad = { t: 6, r: 6, b: 20, l: 34 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;

  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const xLo = Math.max(0, pts[0].x - 15);
  const xHi = Math.max(pts[pts.length - 1].x, nowMin) + 15;
  const xRange = xHi - xLo;

  const maxKw = Math.max(...pts.map(p => p.y));
  const yLo = 0;
  const yHi = Math.max(maxKw * 1.2, 2.5);

  const sx = x => pad.l + ((x - xLo) / xRange) * cw;
  const sy = y => pad.t + ch - ((y - yLo) / (yHi - yLo)) * ch;

  const linePts = pts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  const areaPath = linePts
    + ` L${sx(last.x).toFixed(1)},${(pad.t + ch).toFixed(1)}`
    + ` L${sx(pts[0].x).toFixed(1)},${(pad.t + ch).toFixed(1)} Z`;

  const step = xTickStep(xRange);
  let xElems = "";
  for (let m = Math.ceil(xLo / step) * step; m <= xHi; m += step) {
    const xv = sx(m).toFixed(1);
    const yb = (pad.t + ch).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${yb}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH - 3}" text-anchor="middle">${xTickLabel(m)}</text>`;
  }

  let yElems = "";
  const yStep = niceStep(yHi, 3);
  for (let yv = 0; yv <= yHi; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l + cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l - 4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(1)}</text>`;
  }

  const nowX = sx(nowMin).toFixed(1);
  const nowLine = `<line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${(pad.t + ch).toFixed(1)}" class="now-line"/>`;
  const lastDot = `<circle cx="${sx(last.x).toFixed(1)}" cy="${sy(last.y).toFixed(1)}" r="3" class="wb-dot"/>`;

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
  <defs>
    <linearGradient id="wbg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#34d399" stop-opacity="1"/>
      <stop offset="100%" stop-color="#34d399" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <g>${xElems}${yElems}</g>
  ${nowLine}
  <path d="${areaPath}" fill="url(#wbg)" class="temp-area"/>
  <path d="${linePts}" fill="none" class="wb-line"/>
  ${lastDot}
</svg>`;
}

// ---------- phase timeline ----------
function computePhaseSpans(logs) {
  const spans = { 1: [], 2: [], 3: [] };
  const pending = {};
  for (const entry of logs) {
    const ph = entry.phase;
    if (!(ph in spans)) continue;
    const ts = new Date(entry.timestamp).getTime();
    if (entry.state === 'on') {
      if (pending[ph] == null) pending[ph] = ts;
    } else if (entry.state === 'off' && pending[ph] != null) {
      spans[ph].push({ start: pending[ph], end: ts });
      delete pending[ph];
    }
  }
  const now = Date.now();
  for (const [ph, start] of Object.entries(pending)) {
    spans[Number(ph)].push({ start, end: now });
  }
  return spans;
}

function renderPhaseTimeline(logs) {
  const wrap = document.getElementById('phase-timeline');
  if (!logs || logs.length === 0) {
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');

  const spans = computePhaseSpans(logs);
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const nowMs = now.getTime();

  // 4-Stunden-Fenster, frühestens ab 7:00 Uhr
  const winEnd = nowMs;
  const winStart = Math.max(todayStart + 7 * 3600000, winEnd - 4 * 3600000);
  const winMs = winEnd - winStart;

  const VW = 480, VH = 82;
  const labelW = 32, barH = 14, barGap = 8, padT = 6;
  const trackW = VW - labelW - 4;
  const toX = ts => labelW + Math.max(0, Math.min(1, (ts - winStart) / winMs)) * trackW;
  const nowX = toX(nowMs).toFixed(1);
  const lineEndY = padT + 3 * barH + 2 * barGap;

  let rows = '';
  [1, 2, 3].forEach((ph, i) => {
    const y = padT + i * (barH + barGap);
    rows += `<rect x="${labelW}" y="${y}" width="${trackW}" height="${barH}" rx="2" fill="rgba(255,255,255,0.06)"/>`;
    rows += `<text x="${labelW - 4}" y="${(y + barH / 2).toFixed(1)}" text-anchor="end" dominant-baseline="middle">PH${ph}</text>`;
    for (const span of spans[ph]) {
      const x1 = toX(span.start), x2 = toX(span.end), w = Math.max(0, x2 - x1);
      if (w > 0) rows += `<rect x="${x1.toFixed(1)}" y="${y}" width="${w.toFixed(1)}" height="${barH}" rx="2" fill="#34d399"/>`;
    }
  });

  // Ticks alle 30 Minuten innerhalb des Fensters
  let ticks = '';
  const stepMs = 30 * 60000;
  const firstTickMs = Math.ceil(winStart / stepMs) * stepMs;
  for (let tickMs = firstTickMs; tickMs <= winEnd; tickMs += stepMs) {
    const x = toX(tickMs).toFixed(1);
    const td = new Date(tickMs);
    const h = td.getHours();
    const min = td.getMinutes();
    const label = min === 0 ? `${h}h` : `${h}:${String(min).padStart(2, '0')}`;
    ticks += `<text x="${x}" y="${lineEndY + 12}" text-anchor="middle">${label}</text>`;
  }

  wrap.innerHTML = `<svg class="phase-tl-svg" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    ${rows}
    <line x1="${nowX}" y1="${padT}" x2="${nowX}" y2="${lineEndY}" stroke="rgba(255,255,255,0.3)" stroke-width="1"/>
    ${ticks}
  </svg>`;
}

async function loadPhaseLogs() {
  try {
    const data = await api('/api/heizstab/phase-logs?since=today');
    phaseLogs = Array.isArray(data.entries) ? data.entries : [];
    renderPhaseTimeline(phaseLogs);
  } catch {
    // silent – timeline bleibt versteckt
  }
}

// ---------- refresh ----------
async function refreshStatus() {
  try {
    const activeTab = document.querySelector(".tab.active")?.dataset.tab;
    const [data, history, solax, cascadeStatus] = await Promise.all([
      api("/api/status"),
      api("/api/temp-history").catch(() => ({ entries: [] })),
      api("/api/solax").catch(() => null),
      api("/api/cascade/status").catch(() => null),
    ]);
    if (solax !== null) {
      lastSolaxData = solax;
    } else {
      console.log("[refreshStatus] /api/solax failed – using cached data:", lastSolaxData ? "available" : "none");
    }
    const effectiveSolax = solax !== null ? solax : lastSolaxData;
    // Single source of truth: pv_total_w aus dem Solax-Call überschreibt pv_power
    // im Status-Result, damit Energiefluss, PV&Netz und PV-Erzeugung identische
    // Werte zeigen (beide Endpunkte nutzen ohnehin denselben Server-Cache).
    if (effectiveSolax != null && data?.heater != null) {
      data.heater.pv_power = effectiveSolax.pv_total_w;
    }
    if (cascadeStatus) lastCascadeStatus = cascadeStatus;
    renderStatus(data);
    renderSolax(effectiveSolax);
    renderTempChart(history.entries, data.heater?.storage_max_temp);
    renderWbChart(history.entries);
    loadPhaseLogs();
    if (cascadeStatus) {
      renderCascadeMini(cascadeStatus);
      checkCascadeAlerts(cascadeStatus);
    }
    if (activeTab === "prioritaeten") loadCascade();
    _checkLiveStringAlert();
  } catch (e) {
    showError("Status-Abfrage fehlgeschlagen: " + e.message);
  }
}

async function runOnce() {
  hideError();
  const btn = $("#btn-run");
  btn.disabled = true;
  btn.textContent = "läuft …";
  try {
    const [data, solax] = await Promise.all([
      api("/api/run-once", { method: "POST" }),
      api("/api/solax").catch(() => null),
    ]);
    if (solax !== null) lastSolaxData = solax;
    const effectiveSolax = solax !== null ? solax : lastSolaxData;
    if (effectiveSolax != null && data?.heater != null) {
      data.heater.pv_power = effectiveSolax.pv_total_w;
    }
    renderStatus(data);
    renderSolax(effectiveSolax);
    api("/api/temp-history").then(h => {
      renderTempChart(h.entries, data.heater?.storage_max_temp);
      renderWbChart(h.entries);
    }).catch(() => {});
    loadPhaseLogs();
  } catch (e) {
    showError("Lauf fehlgeschlagen: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Controller jetzt ausführen";
  }
}

// ---------- settings ----------
function setFormValue(form, name, value) {
  const el = form.elements.namedItem(name);
  if (!el) return;
  if (el.type === "checkbox") el.checked = !!value;
  else el.value = (value ?? "");
}
async function loadSettings() {
  try {
    lastConfig = await api("/api/config");
    const f = $("#settings-form");
    setFormValue(f, "runtime.enabled", lastConfig.runtime?.enabled);
    setFormValue(f, "runtime.dry_run", lastConfig.runtime?.dry_run);
    setFormValue(f, "solax.url", lastConfig.solax?.url ?? "");
    setFormValue(f, "solax.pwd", lastConfig.solax?.pwd ?? "");
    setFormValue(f, "shelly.ph1_url", lastConfig.shelly?.ph1_url ?? "");
    setFormValue(f, "shelly.ph2_url", lastConfig.shelly?.ph2_url ?? "");
    setFormValue(f, "shelly.ph3_url", lastConfig.shelly?.ph3_url ?? "");
    setFormValue(f, "shelly.storage_url", lastConfig.shelly?.storage_url ?? "");
    setFormValue(f, "shelly.main_meter_url", lastConfig.shelly?.main_meter_url ?? "");
    setFormValue(f, "shelly.heater_meter_url", lastConfig.shelly?.heater_meter_url ?? "");
    setFormValue(f, "heater.storage_max_temp", lastConfig.heater?.storage_max_temp);
    setFormValue(f, "heater.storage_temp_hysteresis", lastConfig.heater?.storage_temp_hysteresis);
    setFormValue(f, "heater.min_pv_power_ph1", lastConfig.heater?.min_pv_power_ph1);
    setFormValue(f, "heater.min_pv_power_ph2", lastConfig.heater?.min_pv_power_ph2);
    setFormValue(f, "heater.min_pv_power_ph3", lastConfig.heater?.min_pv_power_ph3);
    setFormValue(f, "wallbox.enabled", lastConfig.wallbox?.enabled);
    setFormValue(f, "wallbox.url", lastConfig.wallbox?.url ?? "");
    setFormValue(f, "wallbox.only_control_when_pv_surplus_active", lastConfig.wallbox?.only_control_when_pv_surplus_active);
    setFormValue(f, "wallbox.pause_below_storage_temp", lastConfig.wallbox?.pause_below_storage_temp);
    setFormValue(f, "wallbox.release_above_storage_temp", lastConfig.wallbox?.release_above_storage_temp);
    setFormValue(f, "wallbox.fail_safe", lastConfig.wallbox?.fail_safe ?? "no_change");
    setFormValue(f, "auth.enabled", lastConfig.auth_enabled ?? false);
    setFormValue(f, "auth.user", lastConfig.auth_user ?? "admin");
    // Passwort-Feld bewusst leer lassen
  } catch (e) {
    showError("Konnte Settings nicht laden: " + e.message);
  }
}
async function saveSettings(e) {
  e.preventDefault();
  const f = e.target;
  const msg = $("#settings-msg");

  const input = prompt('Einstellungen wirklich speichern?\nZum Bestätigen bitte "speichern" eintippen:');
  if (input !== "speichern") {
    msg.textContent = "Abgebrochen.";
    msg.className = "form-msg";
    return;
  }

  msg.textContent = "…";
  msg.className = "form-msg";

  const numField = (name) => {
    const el = f.elements.namedItem(name);
    if (el == null || el.value === "") return undefined;
    const n = Number(el.value);
    return isNaN(n) ? undefined : n;
  };
  const boolField = (name) => f.elements.namedItem(name)?.checked;
  const strField = (name) => {
    const v = f.elements.namedItem(name)?.value?.trim();
    return v || undefined;
  };

  const patch = {
    runtime: {
      enabled: boolField("runtime.enabled"),
      dry_run: boolField("runtime.dry_run"),
    },
    solax: {
      url: strField("solax.url"),
      pwd: strField("solax.pwd"),
    },
    shelly: {
      ph1_url: strField("shelly.ph1_url"),
      ph2_url: strField("shelly.ph2_url"),
      ph3_url: strField("shelly.ph3_url"),
      storage_url: strField("shelly.storage_url"),
      main_meter_url: strField("shelly.main_meter_url"),
      heater_meter_url: strField("shelly.heater_meter_url"),
    },
    heater: {
      storage_max_temp: numField("heater.storage_max_temp"),
      storage_temp_hysteresis: numField("heater.storage_temp_hysteresis"),
      min_pv_power_ph1: numField("heater.min_pv_power_ph1"),
      min_pv_power_ph2: numField("heater.min_pv_power_ph2"),
      min_pv_power_ph3: numField("heater.min_pv_power_ph3"),
    },
    wallbox: {
      enabled: boolField("wallbox.enabled"),
      url: strField("wallbox.url"),
      only_control_when_pv_surplus_active: boolField("wallbox.only_control_when_pv_surplus_active"),
      pause_below_storage_temp: numField("wallbox.pause_below_storage_temp"),
      release_above_storage_temp: numField("wallbox.release_above_storage_temp"),
      fail_safe: f.elements.namedItem("wallbox.fail_safe")?.value,
    },
    auth: {
      enabled: boolField("auth.enabled"),
      user: strField("auth.user"),
      password: f.elements.namedItem("auth.password")?.value ?? "",
    },
  };
  // undefined entfernen
  for (const sec of Object.keys(patch)) {
    for (const k of Object.keys(patch[sec])) {
      if (patch[sec][k] === undefined) delete patch[sec][k];
    }
    if (Object.keys(patch[sec]).length === 0) delete patch[sec];
  }

  try {
    const res = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    msg.textContent = "Gespeichert (Backup: " + res.backup + ")";
    msg.className = "form-msg ok";
    await refreshStatus();
  } catch (e) {
    msg.textContent = "Fehler: " + e.message;
    msg.className = "form-msg error";
  }
}

// ---------- logs ----------
async function refreshLogs() {
  const lines = parseInt($("#log-lines").value, 10) || 200;
  try {
    const res = await api(`/api/logs?lines=${lines}`);
    const out = $("#log-output");
    if (res.note && (!res.lines || res.lines.length === 0)) {
      out.textContent = res.note;
    } else {
      out.textContent = res.lines.join("\n");
    }
    out.scrollTop = out.scrollHeight;
  } catch (e) {
    $("#log-output").textContent = "Konnte Logs nicht laden: " + e.message;
  }
}
function setupLogAuto() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if ($("#log-auto").checked) logTimer = setInterval(refreshLogs, 5_000);
}

// ---------- tabs ----------
function activateTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "verlauf")      loadVerlauf();
  if (name === "settings")     { loadSettings(); loadCascadeSettings(); }
  if (name === "logs")         refreshLogs();
  if (name === "prioritaeten") loadCascade();
}

// ---------- boot ----------
function boot() {
  $$(".tab").forEach(t => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  $("#btn-refresh").addEventListener("click", refreshStatus);
  $("#btn-run").addEventListener("click", runOnce);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#btn-logs-refresh").addEventListener("click", refreshLogs);
  $("#log-lines").addEventListener("change", refreshLogs);
  $("#log-auto").addEventListener("change", setupLogAuto);

  // Cascade modal
  $("#cascade-edit-form")?.addEventListener("submit", _submitCascadeEdit);
  $("#modal-cancel")?.addEventListener("click", _closeEditModal);
  $("#cascade-modal")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) _closeEditModal();
  });

  // Cascade mini-card click → open tab
  $("#cascade-mini")?.addEventListener("click", () => activateTab("prioritaeten"));

  // Cascade settings
  $("#btn-cascade-settings-save")?.addEventListener("click", saveCascadeSettings);

  // Shelly add modal
  $("#btn-shelly-add")?.addEventListener("click", _openShellyAddModal);
  $("#sa-cancel")?.addEventListener("click", _closeShellyAddModal);
  $("#shelly-add-modal")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) _closeShellyAddModal();
  });
  $("#btn-sa-test")?.addEventListener("click", _testShellyConnection);
  $("#shelly-add-form")?.addEventListener("submit", _submitShellyAdd);

  // Verlauf sub-navigation
  $$(".verlauf-nav-btn").forEach(b => b.addEventListener("click", () => showVerlaufSection(b.dataset.vsec)));

  // Tag navigation
  document.getElementById("verlauf-tag-prev")?.addEventListener("click", () => {
    loadVerlaufTag(_dateAddDays(_vTagDate, -1));
  });
  document.getElementById("verlauf-tag-next")?.addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    if (_vTagDate < today) loadVerlaufTag(_dateAddDays(_vTagDate, 1));
  });

  // Monat navigation
  document.getElementById("verlauf-monat-prev")?.addEventListener("click", () => {
    loadVerlaufMonat(_monthAdd(_vMonth, -1));
  });
  document.getElementById("verlauf-monat-next")?.addEventListener("click", () => {
    const thisMonth = new Date().toISOString().slice(0, 7);
    if (_vMonth < thisMonth) loadVerlaufMonat(_monthAdd(_vMonth, 1));
  });

  // Jahr navigation
  document.getElementById("verlauf-jahr-prev")?.addEventListener("click", () => {
    loadVerlaufJahr(_vYear - 1);
  });
  document.getElementById("verlauf-jahr-next")?.addEventListener("click", () => {
    if (_vYear < new Date().getFullYear()) loadVerlaufJahr(_vYear + 1);
  });

  // Strings navigation
  document.getElementById("verlauf-strings-prev")?.addEventListener("click", () => {
    loadVerlaufStrings(_dateAddDays(_vStringsDate, -1));
  });
  document.getElementById("verlauf-strings-next")?.addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    if (_vStringsDate < today) loadVerlaufStrings(_dateAddDays(_vStringsDate, 1));
  });

  refreshStatus();
  refreshTimer = setInterval(refreshStatus, REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", boot);

// ══════════════════════════════════════════════════════
// Kaskade — Prioritäten Tab
// ══════════════════════════════════════════════════════

let cascadeDevicesData = [];  // merged status + full device data
let cascadeStatusData  = null;
let lastCascadeStatus  = null;  // latest status for cross-widget use
let _cDrag             = null;  // active drag state
let _activeMenu        = null;  // open dropdown element

// ── helpers ──────────────────────────────────────────

function _cascadeActionText(action) {
  const map = {
    turn_on:        "eingeschaltet",
    turn_off:       "ausgeschaltet",
    keep_on:        "läuft",
    keep_off:       "Überschuss reicht nicht",
    skip_min_on:    "Mindestlaufzeit",
    skip_min_off:   "Mindestpause",
    manual_override:"Manuell",
  };
  return map[action] || "–";
}

function _cascadeIcon(type) {
  return { heizstab: "⚡", wallbox: "🚗", shelly_gen1: "🔌", shelly_gen2: "🔌" }[type] || "🔌";
}

// ── load ─────────────────────────────────────────────

async function loadCascade() {
  try {
    const [statusData, devData] = await Promise.all([
      api("/api/cascade/status"),
      api("/api/cascade/devices"),
    ]);
    const devMap = Object.fromEntries((devData.devices || []).map(d => [d.id, d]));
    cascadeDevicesData = (statusData.devices || []).map(sd => ({
      ...(devMap[sd.id] || {}),
      ...sd,
    }));
    cascadeStatusData = statusData;
    renderCascadeTab(cascadeDevicesData, statusData.surplus_watts);
    renderCascadeMini(statusData);
  } catch (err) {
    const el = document.getElementById("cascade-device-list");
    if (el) el.innerHTML = `<div class="card"><p class="note" style="color:var(--error)">Fehler: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ── render tab ───────────────────────────────────────

function renderCascadeTab(devices, surplusW) {
  const listEl = document.getElementById("cascade-device-list");
  if (!listEl) return;

  const assignedW = devices.filter(d => d.is_on && d.enabled).reduce((s, d) => s + d.power_watts, 0);
  const fillPct   = surplusW > 0 ? Math.min(100, assignedW / surplusW * 100) : 0;

  const el = id => document.getElementById(id);
  if (el("cs-surplus"))  el("cs-surplus").textContent  = surplusW != null ? (surplusW / 1000).toFixed(2) + " kW" : "–";
  if (el("cs-assigned")) el("cs-assigned").textContent = (assignedW / 1000).toFixed(2) + " kW";
  if (el("cs-fill"))     el("cs-fill").style.width     = fillPct.toFixed(1) + "%";

  listEl.innerHTML = devices.map((d, i) => {
    const isShelly    = d.type.startsWith("shelly");
    const isProtected = d.id === "heizstab" || d.id === "wallbox";
    const statusText  = d.manual_override_action
      ? "Manuell " + (d.manual_override_action === "on" ? "EIN" : "AUS")
      : (!d.is_on && d.last_reason ? d.last_reason : _cascadeActionText(d.last_action));
    const overrideDot = d.manual_override_action
      ? `<span class="cascade-override-dot" title="Manuelle Übersteuerung">●</span>` : "";

    const warnIcon = isShelly && d.last_status_ok === false
      ? `<span class="cascade-warn-icon" title="Gerät nicht erreichbar">⚠️</span>` : "";

    let pwrText = `${d.power_watts} W`;
    if (d.last_status_power_w != null) {
      pwrText = `${Math.round(d.last_status_power_w)} W <span class="cascade-power-actual">(von ${d.power_watts} W)</span>`;
    }

    return `<div class="cascade-card${d.enabled ? "" : " disabled-device"}"
        data-id="${escapeHtml(d.id)}"
        data-is-shelly="${isShelly}"
        data-is-protected="${isProtected}">
      <div class="cascade-drag-handle" title="Ziehen zum Sortieren">
        <svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor">
          <circle cx="3" cy="2.5" r="1.3"/><circle cx="7" cy="2.5" r="1.3"/>
          <circle cx="3" cy="7"   r="1.3"/><circle cx="7" cy="7"   r="1.3"/>
          <circle cx="3" cy="11.5" r="1.3"/><circle cx="7" cy="11.5" r="1.3"/>
        </svg>
      </div>
      <div class="cascade-priority-num">${d.priority}</div>
      <div class="cascade-icon">${_cascadeIcon(d.type)}</div>
      <div class="cascade-info">
        <div class="cascade-name">${escapeHtml(d.name)}${overrideDot}${warnIcon}</div>
        <div class="cascade-sub">${pwrText} · ${escapeHtml(statusText)}</div>
      </div>
      <div class="cascade-right">
        <span class="cascade-badge ${d.is_on ? "on" : "off"}">${d.is_on ? "AN" : "AUS"}</span>
        <label class="toggle-switch" title="${d.enabled ? "Deaktivieren" : "Aktivieren"}">
          <input type="checkbox" class="device-enabled-toggle"
            data-device-id="${escapeHtml(d.id)}" ${d.enabled ? "checked" : ""}>
          <span class="toggle-track"></span>
        </label>
        <button class="cascade-menu-btn" title="Aktionen"
          data-device-id="${escapeHtml(d.id)}">⋮</button>
      </div>
    </div>`;
  }).join("");

  _attachCardHandlers(listEl);
}

function _attachCardHandlers(listEl) {
  listEl.querySelectorAll(".cascade-drag-handle").forEach(handle => {
    handle.addEventListener("pointerdown", e => {
      if (e.button !== 0 && e.pointerType !== "touch") return;
      _cascadeStartDrag(e, handle.closest(".cascade-card"));
    }, { passive: false });
  });

  listEl.querySelectorAll(".device-enabled-toggle").forEach(chk => {
    chk.addEventListener("change", () => {
      _cascadeToggleEnabled(chk.dataset.deviceId, chk.checked);
    });
  });

  listEl.querySelectorAll(".cascade-menu-btn").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      _openCascadeMenu(btn);
    });
  });
}

// ── dropdown menu ────────────────────────────────────

document.addEventListener("click", () => { _closeMenu(); });

function _closeMenu() {
  if (_activeMenu) { _activeMenu.remove(); _activeMenu = null; }
}

function _openCascadeMenu(btn) {
  _closeMenu();
  const card      = btn.closest(".cascade-card");
  const id        = card.dataset.id;
  const isShelly  = card.dataset.isShelly === "true";
  const isProt    = card.dataset.isProtected === "true";
  const device    = cascadeDevicesData.find(d => d.id === id) || {};
  const enabled   = device.enabled ?? true;

  const menu = document.createElement("div");
  menu.className = "cascade-menu-dropdown";
  menu.innerHTML = `
    <button class="cascade-menu-item" data-action="edit">Bearbeiten</button>
    <button class="cascade-menu-item" data-action="toggle">${enabled ? "Deaktivieren" : "Aktivieren"}</button>
    ${isShelly ? `
    <button class="cascade-menu-item" data-action="on">Manuell einschalten (30 Min)</button>
    <button class="cascade-menu-item" data-action="off">Manuell ausschalten (30 Min)</button>` : ""}
    <button class="cascade-menu-item danger" data-action="delete" ${isProt ? "disabled" : ""}>Löschen</button>
  `;

  // An body hängen (nicht an Card), damit disabled-device opacity: 0.42 nicht vererbt wird
  const btnRect = btn.getBoundingClientRect();
  menu.style.top   = (btnRect.bottom + 4) + "px";
  menu.style.right = (window.innerWidth - btnRect.right) + "px";
  document.body.appendChild(menu);
  _activeMenu = menu;

  menu.querySelectorAll("button[data-action]").forEach(item => {
    item.addEventListener("click", e => {
      e.stopPropagation();
      _closeMenu();
      const a = item.dataset.action;
      if (a === "edit")   _openEditModal(id);
      if (a === "toggle") _cascadeToggleEnabled(id, !enabled);
      if (a === "on")     _cascadeSetOverride(id, "on");
      if (a === "off")    _cascadeSetOverride(id, "off");
      if (a === "delete") _cascadeDelete(id);
    });
  });
}

// ── device actions ───────────────────────────────────

async function _cascadeToggleEnabled(id, enabled) {
  const card = document.querySelector(`.cascade-card[data-id="${id}"]`);
  const chk  = card?.querySelector(".device-enabled-toggle");
  if (card) card.classList.toggle("disabled-device", !enabled);
  if (chk)  chk.checked = enabled;
  try {
    await api(`/api/cascade/devices/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const d = cascadeDevicesData.find(x => x.id === id);
    if (d) d.enabled = enabled;
  } catch (err) {
    if (card) card.classList.toggle("disabled-device", enabled);
    if (chk)  chk.checked = !enabled;
    showError("Gerät konnte nicht aktualisiert werden: " + err.message);
  }
}

async function _cascadeSetOverride(id, action) {
  try {
    await api(`/api/cascade/devices/${id}/override`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, minutes: 30 }),
    });
    loadCascade();
  } catch (err) {
    showError("Override konnte nicht gesetzt werden: " + err.message);
  }
}

async function _cascadeDelete(id) {
  if (!confirm("Gerät wirklich löschen?")) return;
  try {
    await api(`/api/cascade/devices/${id}`, { method: "DELETE" });
    loadCascade();
  } catch (err) {
    showError("Gerät konnte nicht gelöscht werden: " + err.message);
  }
}

// ── edit modal ───────────────────────────────────────

function _openEditModal(id) {
  const d = cascadeDevicesData.find(x => x.id === id);
  if (!d) return;
  const modal = document.getElementById("cascade-modal");
  if (!modal) return;
  document.getElementById("modal-device-id").value   = id;
  document.getElementById("modal-title").textContent = "Bearbeiten: " + d.name;
  document.getElementById("modal-name").value        = d.name || "";
  document.getElementById("modal-power").value       = d.power_watts || "";
  document.getElementById("modal-min-on").value      = d.min_on_minutes  ?? 5;
  document.getElementById("modal-min-off").value     = d.min_off_minutes ?? 3;
  document.getElementById("modal-hysteresis").value  = d.hysteresis_watts ?? 100;
  document.getElementById("modal-msg").textContent   = "";
  modal.classList.remove("hidden");
}

function _closeEditModal() {
  document.getElementById("cascade-modal")?.classList.add("hidden");
}

async function _submitCascadeEdit(e) {
  e.preventDefault();
  const id  = document.getElementById("modal-device-id").value;
  const msg = document.getElementById("modal-msg");
  const body = {
    name:           document.getElementById("modal-name").value.trim(),
    power_watts:    parseInt(document.getElementById("modal-power").value, 10),
    min_on_minutes: parseInt(document.getElementById("modal-min-on").value, 10),
    min_off_minutes:parseInt(document.getElementById("modal-min-off").value, 10),
    hysteresis_watts:parseInt(document.getElementById("modal-hysteresis").value, 10),
  };
  if (!body.name || isNaN(body.power_watts) || body.power_watts <= 0) {
    msg.textContent = "Name und Leistung (>0) sind erforderlich.";
    msg.className = "form-msg error";
    return;
  }
  msg.textContent = "…"; msg.className = "form-msg";
  try {
    await api(`/api/cascade/devices/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    _closeEditModal();
    loadCascade();
  } catch (err) {
    msg.textContent = "Fehler: " + err.message;
    msg.className = "form-msg error";
  }
}

// ── drag & drop (Pointer Events, desktop + touch) ────

function _cascadeStartDrag(e, card) {
  if (!card) return;
  e.preventDefault();
  const list = document.getElementById("cascade-device-list");
  if (!list) return;

  const cards = Array.from(list.querySelectorAll(".cascade-card"));
  const rect  = card.getBoundingClientRect();

  // Ghost clone
  const ghost = card.cloneNode(true);
  ghost.classList.add("cascade-ghost");
  ghost.style.width = rect.width + "px";
  ghost.style.top   = rect.top + "px";
  ghost.style.left  = rect.left + "px";
  ghost.querySelectorAll("input,button").forEach(el => { el.style.pointerEvents = "none"; });
  document.body.appendChild(ghost);

  // Placeholder
  const ph = document.createElement("div");
  ph.className = "cascade-placeholder";
  ph.style.height = rect.height + "px";
  list.insertBefore(ph, card);
  card.classList.add("is-dragging");  // visibility:hidden, keeps layout space

  _cDrag = {
    card, ghost, ph, list,
    cards,
    startIdx: cards.indexOf(card),
    offsetY:  e.clientY - rect.top,
  };

  document.addEventListener("pointermove",  _cdMove,  { passive: false });
  document.addEventListener("pointerup",    _cdEnd);
  document.addEventListener("pointercancel",_cdCancel);
}

function _cdMove(e) {
  if (!_cDrag) return;
  e.preventDefault();
  const { ghost, ph, list, card, offsetY } = _cDrag;

  ghost.style.top = (e.clientY - offsetY) + "px";

  const others = Array.from(list.querySelectorAll(".cascade-card:not(.is-dragging)"));
  let insertBefore = null;
  for (const other of others) {
    const r = other.getBoundingClientRect();
    if (e.clientY < r.top + r.height / 2) { insertBefore = other; break; }
  }
  if (insertBefore) list.insertBefore(ph, insertBefore);
  else              list.insertBefore(ph, document.querySelector(".cascade-add-btn") || null);
}

function _cdEnd(e) {
  if (!_cDrag) return;
  const { card, ghost, ph, list, cards, startIdx } = _cDrag;
  _cDrag = null;
  document.removeEventListener("pointermove",  _cdMove);
  document.removeEventListener("pointerup",    _cdEnd);
  document.removeEventListener("pointercancel",_cdCancel);

  list.insertBefore(card, ph);  // place card at placeholder position
  ph.remove();
  ghost.remove();
  card.classList.remove("is-dragging");

  const newCards = Array.from(list.querySelectorAll(".cascade-card"));
  const newIdx   = newCards.indexOf(card);

  // Update priority numbers in DOM
  newCards.forEach((c, i) => {
    const n = c.querySelector(".cascade-priority-num");
    if (n) n.textContent = i + 1;
  });

  if (newIdx !== startIdx) {
    const deviceIds = newCards.map(c => c.dataset.id);
    api("/api/cascade/reorder", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deviceIds }),
    }).catch(err => showError("Reihenfolge konnte nicht gespeichert werden: " + err.message));
  }
}

function _cdCancel() {
  if (!_cDrag) return;
  const { card, ghost, ph, list, cards, startIdx } = _cDrag;
  _cDrag = null;
  document.removeEventListener("pointermove",  _cdMove);
  document.removeEventListener("pointerup",    _cdEnd);
  document.removeEventListener("pointercancel",_cdCancel);
  // Restore to original position
  const ref = cards[startIdx + 1] || null;
  list.insertBefore(card, ref);
  ph.remove(); ghost.remove();
  card.classList.remove("is-dragging");
}

// ── dashboard mini-card ──────────────────────────────

function renderCascadeMini(status) {
  if (!status) return;
  const devices  = status.devices || [];
  const surplusW = status.surplus_watts ?? 0;
  const assignedW = devices.filter(d => d.is_on && d.enabled).reduce((s, d) => s + d.power_watts, 0);
  const freeW    = Math.max(0, surplusW - assignedW);

  const surplusEl = document.getElementById("cascade-mini-surplus");
  if (surplusEl) {
    surplusEl.innerHTML = `Überschuss: <b>${surplusW > 0 ? (surplusW / 1000).toFixed(1) + " kW" : "–"}</b>`;
  }

  const devEl = document.getElementById("cascade-mini-devices");
  if (devEl) {
    const barScale = devices.length ? Math.max(...devices.map(d => d.power_watts)) || 1 : 1;
    devEl.innerHTML = devices.map(d => {
      const on = d.is_on && d.enabled;
      const isShelly = d.type && d.type.startsWith("shelly");
      const warn = isShelly && d.last_status_ok === false ? " ⚠️" : "";
      let pwrLabel;
      if (on) {
        pwrLabel = d.last_status_power_w != null
          ? Math.round(d.last_status_power_w) + " W"
          : Math.round(d.power_watts) + " W";
      } else {
        pwrLabel = d.power_watts + " W";
      }
      const barPct = Math.min(100, d.power_watts / barScale * 100).toFixed(1);
      return `<div class="cascade-mini-row">
        <span class="cascade-mini-icon">${on ? "✅" : "❌"}</span>
        <span class="cascade-mini-name">${escapeHtml(d.name)}${warn}</span>
        <div class="cascade-mini-bar-wrap">
          <div class="cascade-mini-bar${on ? "" : " off"}" style="width:${barPct}%"></div>
        </div>
        <span class="cascade-mini-pwr">${pwrLabel}</span>
      </div>`;
    }).join("");
  }

  const footerEl = document.getElementById("cascade-mini-footer");
  if (footerEl) {
    footerEl.innerHTML = `
      <span>Frei: <b>${(freeW / 1000).toFixed(1)} kW</b></span>
      <button class="cascade-mini-link" id="cascade-mini-link">→ Prioritäten</button>
    `;
    document.getElementById("cascade-mini-link")?.addEventListener("click", e => {
      e.stopPropagation();
      activateTab("prioritaeten");
    });
  }
}

// ══════════════════════════════════════════════════════
// Shelly Add Modal — Phase 3
// ══════════════════════════════════════════════════════

function _openShellyAddModal() {
  const form = $("#shelly-add-form");
  if (form) form.reset();
  const testResult = $("#sa-test-result");
  if (testResult) { testResult.className = "sa-test-result hidden"; testResult.innerHTML = ""; }
  const msg = $("#sa-msg");
  if (msg) msg.textContent = "";
  // Restore gen2 as default
  const gen2 = $("#sa-gen2");
  if (gen2) gen2.checked = true;
  _setShellyAddModal(true);
}

function _closeShellyAddModal() {
  _setShellyAddModal(false);
}

function _setShellyAddModal(open) {
  const el = $("#shelly-add-modal");
  if (el) el.classList.toggle("hidden", !open);
}

async function _testShellyConnection() {
  const ip      = ($("#sa-ip")?.value || "").trim();
  const genVal  = document.querySelector("input[name='sa-gen']:checked")?.value || "shelly_gen2";
  const channel = parseInt($("#sa-channel")?.value || "0", 10);
  const resultEl = $("#sa-test-result");

  if (!ip) {
    if (resultEl) {
      resultEl.className = "sa-test-result error";
      resultEl.textContent = "Bitte IP-Adresse eingeben.";
    }
    return;
  }

  if (resultEl) {
    resultEl.className = "sa-test-result";
    resultEl.textContent = "Verbinde …";
  }

  try {
    const data = await api("/api/cascade/devices/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, type: genVal, channel }),
    });

    if (data.success) {
      if (resultEl) {
        resultEl.className = "sa-test-result success";
        resultEl.innerHTML =
          `✓ Verbindung erfolgreich<br>` +
          `Modell: ${escapeHtml(data.model || "–")}<br>` +
          `Firmware: ${escapeHtml(data.firmware || "–")}<br>` +
          `Status: ${data.is_on ? "EIN" : "AUS"} · ${Math.round(data.power ?? 0)} W`;
      }
    } else {
      if (resultEl) {
        resultEl.className = "sa-test-result error";
        resultEl.innerHTML = `✗ Verbindung fehlgeschlagen<br>${escapeHtml(data.error || "Unbekannter Fehler")}`;
      }
    }
  } catch (err) {
    if (resultEl) {
      resultEl.className = "sa-test-result error";
      resultEl.textContent = "Fehler: " + err.message;
    }
  }
}

async function _submitShellyAdd(e) {
  e.preventDefault();
  const msgEl = $("#sa-msg");
  if (msgEl) msgEl.textContent = "";

  const name       = ($("#sa-name")?.value || "").trim();
  const ip         = ($("#sa-ip")?.value || "").trim();
  const genVal     = document.querySelector("input[name='sa-gen']:checked")?.value || "shelly_gen2";
  const channel    = parseInt($("#sa-channel")?.value || "0", 10);
  const powerWatts = parseInt($("#sa-power")?.value || "0", 10);
  const minOn      = parseInt($("#sa-min-on")?.value || "5", 10);
  const minOff     = parseInt($("#sa-min-off")?.value || "3", 10);
  const hysteresis = parseInt($("#sa-hysteresis")?.value || "100", 10);

  if (!name || !ip || !powerWatts) {
    if (msgEl) msgEl.textContent = "Bitte alle Pflichtfelder ausfüllen.";
    return;
  }

  // Generate a slug-style ID from name
  const deviceId = "shelly_" + name.toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .substring(0, 30);

  // Determine next priority (after all existing devices)
  const nextPriority = (cascadeDevicesData.length > 0
    ? Math.max(...cascadeDevicesData.map(d => d.priority)) + 1
    : 1);

  try {
    await api("/api/cascade/devices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: deviceId,
        name,
        type: genVal,
        ip_address: ip,
        shelly_channel: channel,
        power_watts: powerWatts,
        priority: nextPriority,
        min_on_minutes: minOn,
        min_off_minutes: minOff,
        hysteresis_watts: hysteresis,
      }),
    });
    _closeShellyAddModal();
    loadCascade();
  } catch (err) {
    if (msgEl) msgEl.textContent = "Fehler beim Speichern: " + err.message;
  }
}

// ══════════════════════════════════════════════════════
// Phase 4 — Cascade Alerts
// ══════════════════════════════════════════════════════

function checkCascadeAlerts(cascadeStatus) {
  const strip = document.getElementById("cascade-alert-strip");
  if (!strip) return;
  const devices = cascadeStatus?.devices || [];
  const alerts = [];

  // Auto-disabled devices
  devices.filter(d => !d.enabled && (d.consecutive_errors ?? 0) >= 5).forEach(d => {
    alerts.push({ cls: "error", icon: "🚫", text: `<b>${escapeHtml(d.name)}</b> wurde automatisch deaktiviert (${d.consecutive_errors} Fehler).` });
  });

  // Unreachable Shelly devices
  devices.filter(d => d.enabled && d.type?.startsWith("shelly") && d.last_status_ok === false).forEach(d => {
    alerts.push({ cls: "warn", icon: "⚠️", text: `<b>${escapeHtml(d.name)}</b> ist nicht erreichbar.` });
  });

  // All enabled devices on → full utilization
  const enabledDevices = devices.filter(d => d.enabled);
  if (enabledDevices.length > 0 && enabledDevices.every(d => d.is_on)) {
    alerts.push({ cls: "ok", icon: "✅", text: `Volle Auslastung — alle ${enabledDevices.length} Geräte eingeschaltet.` });
  }

  // Power anomaly: actual > 130% or < 30% of configured (only when on and has live data)
  devices.filter(d => d.enabled && d.is_on && d.type?.startsWith("shelly") && d.last_status_power_w != null && d.power_watts > 0).forEach(d => {
    const ratio = d.last_status_power_w / d.power_watts;
    if (ratio > 1.3) {
      alerts.push({ cls: "warn", icon: "📈", text: `<b>${escapeHtml(d.name)}</b>: ${Math.round(d.last_status_power_w)} W (${Math.round(ratio * 100)}% der konfigurierten ${d.power_watts} W).` });
    } else if (d.last_status_power_w > 10 && ratio < 0.3) {
      alerts.push({ cls: "warn", icon: "📉", text: `<b>${escapeHtml(d.name)}</b>: nur ${Math.round(d.last_status_power_w)} W (${Math.round(ratio * 100)}% der konfigurierten ${d.power_watts} W).` });
    }
  });

  if (alerts.length === 0) {
    strip.classList.add("hidden");
    return;
  }
  strip.classList.remove("hidden");
  strip.innerHTML = alerts.map(a =>
    `<div class="cascade-alert ${a.cls}">
      <span class="cascade-alert-icon">${a.icon}</span>
      <span class="cascade-alert-text">${a.text}</span>
    </div>`
  ).join("");
}

// ══════════════════════════════════════════════════════
// Phase 4 — Cascade History (Verlauf Tab)
// ══════════════════════════════════════════════════════

const _cascadeHistActionLabel = {
  turn_on:        { label: "eingeschaltet",  cls: "cascade-hist-on" },
  turn_off:       { label: "ausgeschaltet",  cls: "cascade-hist-off" },
  keep_on:        { label: "läuft weiter",   cls: "cascade-hist-other" },
  keep_off:       { label: "bleibt aus",     cls: "cascade-hist-other" },
  skip_min_on:    { label: "Mindestlaufzeit",cls: "cascade-hist-other" },
  skip_min_off:   { label: "Mindestpause",   cls: "cascade-hist-other" },
  manual_override:{ label: "Manuell",        cls: "cascade-hist-other" },
  auto_disabled:  { label: "deaktiviert",    cls: "cascade-hist-off" },
};

function _cascadeHistIcon(action) {
  if (action === "turn_on")      return "✅";
  if (action === "turn_off")     return "❌";
  if (action === "auto_disabled")return "🚫";
  if (action === "manual_override") return "🔧";
  return "·";
}

let _cascadeHistAllEntries = [];
let _cascadeHistDeviceMap  = {};

function renderCascadeHistory(entries, devices) {
  _cascadeHistAllEntries = entries;
  _cascadeHistDeviceMap  = Object.fromEntries(devices.map(d => [d.id, d.name]));

  const filterEl = document.getElementById("cascade-hist-filter");
  if (filterEl) {
    const deviceIds = [...new Set(entries.map(e => e.device_id))];
    const currentVal = filterEl.value;
    filterEl.innerHTML = '<option value="">Alle Geräte</option>' +
      deviceIds.map(id =>
        `<option value="${escapeHtml(id)}"${id === currentVal ? " selected" : ""}>${escapeHtml(_cascadeHistDeviceMap[id] || id)}</option>`
      ).join("");
    filterEl.onchange = () => _renderCascadeHistFiltered(filterEl.value);
  }
  _renderCascadeHistFiltered(filterEl?.value || "");
}

function _renderCascadeHistFiltered(filterDeviceId) {
  const listEl = document.getElementById("cascade-hist-list");
  if (!listEl) return;
  const filtered = filterDeviceId
    ? _cascadeHistAllEntries.filter(e => e.device_id === filterDeviceId)
    : _cascadeHistAllEntries;

  if (filtered.length === 0) {
    listEl.innerHTML = '<div class="chart-empty">Keine Schaltvorgänge.</div>';
    return;
  }

  listEl.innerHTML = filtered.slice(0, 100).map(e => {
    const info  = _cascadeHistActionLabel[e.action] || { label: e.action, cls: "cascade-hist-other" };
    const icon  = _cascadeHistIcon(e.action);
    const devName = escapeHtml(_cascadeHistDeviceMap[e.device_id] || e.device_id);
    const ts = e.timestamp ? e.timestamp.slice(11, 19) : "–";
    const meta = e.surplus_watts != null
      ? `${e.surplus_watts} W → ${e.remaining_watts ?? 0} W frei`
      : "";
    return `<div class="cascade-hist-entry">
      <span class="cascade-hist-time">${ts}</span>
      <span class="cascade-hist-icon">${icon}</span>
      <span class="cascade-hist-action ${info.cls}"><b>${devName}</b> ${info.label}</span>
      <span class="cascade-hist-meta">${meta}</span>
    </div>`;
  }).join("");
}

// ══════════════════════════════════════════════════════
// Phase 4 — Cascade Settings (Settings Tab)
// ══════════════════════════════════════════════════════

async function loadCascadeSettings() {
  try {
    const s = await api("/api/cascade/settings");
    const setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };
    const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val ?? ""; };
    setChk("cs-enabled",     s.cascade_enabled);
    setVal("cs-polling",     s.polling_interval_s);
    setVal("cs-min-surplus", s.min_surplus_watts);
    setChk("cs-grid-draw",   s.allow_grid_draw);
  } catch (e) {
    console.warn("Kaskaden-Settings konnten nicht geladen werden:", e.message);
  }
}

async function saveCascadeSettings() {
  const msgEl = document.getElementById("cs-settings-msg");
  if (msgEl) { msgEl.textContent = "…"; msgEl.className = "form-msg"; }
  const getChk = id => document.getElementById(id)?.checked ?? true;
  const getNum = id => {
    const v = document.getElementById(id)?.value;
    return v !== "" && v != null ? Number(v) : undefined;
  };
  const body = {
    cascade_enabled:    getChk("cs-enabled"),
    polling_interval_s: getNum("cs-polling"),
    min_surplus_watts:  getNum("cs-min-surplus"),
    allow_grid_draw:    getChk("cs-grid-draw"),
  };
  for (const k of Object.keys(body)) {
    if (body[k] === undefined) delete body[k];
  }
  try {
    await api("/api/cascade/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (msgEl) { msgEl.textContent = "Gespeichert"; msgEl.className = "form-msg ok"; }
    setTimeout(() => { if (msgEl) msgEl.textContent = ""; }, 3000);
  } catch (e) {
    if (msgEl) { msgEl.textContent = "Fehler: " + e.message; msgEl.className = "form-msg error"; }
  }
}
