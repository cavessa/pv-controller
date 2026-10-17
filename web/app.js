// PV-Controller Dashboard — vanilla JS, kein Framework, kein CDN.

const REFRESH_MS = 10_000;
let refreshTimer = null;
let logTimer = null;
let lastConfig = null;
let phaseLogs = [];
let lastSolaxData = null;
let pvPeakToday = 0;
let pvPeakTime = '';

// ── Verlauf sub-tab state ─────────────────────────────
let _vsec        = "tag";
let _vTagDate    = new Date().toISOString().slice(0, 10);
let _vMonth      = new Date().toISOString().slice(0, 7);
let _vYear       = new Date().getFullYear();
let _vStringsDate = new Date().toISOString().slice(0, 10);

// ── Netz sub-tab state ────────────────────────────────
let _vNetzSub     = "tag";
let _vNetzTagDate = new Date().toISOString().slice(0, 10);
let _vNetzWeekEnd = new Date().toISOString().slice(0, 10);
let _vNetzMonth   = new Date().toISOString().slice(0, 7);
let _vNetzYear    = new Date().getFullYear();


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
// ---------- Aktualisieren ohne Neuaufbau ----------
// Das Polling erzeugt bei jedem Lauf dasselbe HTML wie bisher, ersetzt es aber nur, wenn es sich
// geändert hat (gleiche Optik, kein Flackern, keine ersetzten Charts bei unveränderten Daten).
// el.htmlIfChanged = html / el.textIfChanged = text statt innerHTML / textContent.
function setHTML(el, html) {
  if (!el) return false;
  html = html == null ? "" : String(html);
  if (el._html === html && el._first === el.firstChild && el._count === el.childNodes.length) return false;
  el.innerHTML = html;
  el._html = html; el._first = el.firstChild; el._count = el.childNodes.length;
  return true;
}
function invalidateHTML(el) { if (el) el._html = undefined; }   // DOM wurde von Hand geändert
Object.defineProperty(Element.prototype, "classIfChanged", {
  configurable: true, set(v) { const c = v == null ? "" : String(v); if (this.className !== c) this.className = c; },
});
Object.defineProperty(Element.prototype, "htmlIfChanged", { configurable: true, set(v) { setHTML(this, v); } });
Object.defineProperty(Element.prototype, "textIfChanged", {
  configurable: true,
  // nur überspringen, wenn bereits genau dieser reine Text drinsteht (nicht z. B. ein Skeleton-Platzhalter)
  set(v) { const t = v == null ? "" : String(v); if (this.childElementCount || this.textContent !== t) this.textContent = t; },
});

// ---------- Charts in echten Pixeln ----------
// Alle Charts sind SVGs mit viewBox und wuchsen mit der Card-Breite: in einer 1400 px breiten Card
// (Hauptzähler, Heizungsverlauf) wurden aus 9 px Schrift 44 px und aus 150 Einheiten 560 px Höhe.
// Jetzt zentral: bis Faktor CHART_MAX_SCALE wie bisher proportional, darüber wird nur noch in die Breite
// gestreckt. Höhe, Schrift, Punkte und Linienstärke (CSS: svg.chart-fit) bleiben dann fest.
// Bis Faktor 1,5 (Handy, schmale Cards) ändert sich nichts.
const CHART_SEL = "svg.temp-chart, svg.chart-svg, svg.heat-svg, svg.phase-tl-svg";
const CHART_MAX_SCALE = 1.5;      // Höhe: viewBox-Einheit → max. 1,5 px (150 Einheiten → 225 px)
const CHART_TEXT_SCALE = 1.3;     // Schrift 9 Einheiten → max. 11,7 px
const CHART_DOT_SCALE = 1.15;     // Punkte r=3,5 → max. 4 px
function fitChart(svg) {
  const vb = svg.viewBox.baseVal;
  const w = svg.getBoundingClientRect().width;
  if (!vb || !vb.width || !vb.height || w < 1) return;            // versteckter Tab: später per ResizeObserver
  const sx = w / vb.width, sy = Math.min(sx, CHART_MAX_SCALE);
  const key = `${Math.round(w)}|${svg.childElementCount}|${svg.firstElementChild?.tagName}`;
  if (svg._fit === key) return;
  svg._fit = key;
  const stretched = sx > sy + 0.001;
  svg.classList.toggle("chart-fit", stretched);                   // CSS: Linienstärke dann in echten Pixeln
  svg.setAttribute("preserveAspectRatio", stretched ? "none" : "xMidYMid meet");
  svg.style.height = stretched ? Math.round(vb.height * sy) + "px" : "";
  const around = (el, x, y, k) => {
    if (el._t0 === undefined) el._t0 = el.getAttribute("transform") || "";
    const t = stretched ? `${el._t0} translate(${x} ${y}) scale(${(k / sx).toFixed(4)} ${(k / sy).toFixed(4)}) translate(${-x} ${-y})`.trim() : el._t0;
    if (t) el.setAttribute("transform", t); else el.removeAttribute("transform");
  };
  const num = (el, a) => parseFloat(el.getAttribute(a)) || 0;
  for (const t of svg.querySelectorAll("text")) around(t, num(t, "x"), num(t, "y"), Math.min(sx, CHART_TEXT_SCALE));
  for (const c of svg.querySelectorAll("circle")) around(c, num(c, "cx"), num(c, "cy"), Math.min(sx, CHART_DOT_SCALE));
}
(function watchCharts() {
  if (typeof ResizeObserver === "undefined" || typeof MutationObserver === "undefined") return;
  const ro = new ResizeObserver(entries => { for (const e of entries) fitChart(e.target); });
  const add = node => {
    if (node.nodeType !== 1) return;
    const list = node.matches?.(CHART_SEL) ? [node] : node.querySelectorAll?.(CHART_SEL) || [];
    for (const svg of list) { ro.observe(svg); fitChart(svg); }
  };
  const start = () => {
    add(document.body);
    new MutationObserver(muts => { for (const m of muts) m.addedNodes.forEach(add); })
      .observe(document.body, { childList: true, subtree: true });
  };
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();

let lastZendure = null;
let lastPhasesDisabled = {};

function renderStatus(s) {
  hideError();
  lastZendure = s.zendure ?? null;
  lastPhasesDisabled = s.heater?.phases_disabled ?? {};
  renderZendureCard(lastZendure);

  // Geräte, die seit ≥ 10 min nicht antworten (z. B. ein Phasen-Shelly)
  const devBanner = $("#device-banner");
  if (devBanner) {
    const alerts = s.device_alerts ?? [];
    if (alerts.length) {
      devBanner.classIfChanged = "wb-banner warn";
      devBanner.textIfChanged = alerts.map(a => a.text ?? `${a.label} seit ${a.since} Uhr nicht erreichbar`).join(" · ");
    } else {
      devBanner.classIfChanged = "wb-banner warn hidden";
    }
  }

  // pills
  $("#pill-enabled").textIfChanged = "Automatik: " + (s.runtime.enabled ? "aktiv" : "aus");
  $("#pill-enabled").classIfChanged = "pill " + (s.runtime.enabled ? "ok" : "warn");

  $("#pill-dry").textIfChanged = "Dry-Run: " + (s.runtime.dry_run ? "an" : "aus");
  $("#pill-dry").classIfChanged = "pill " + (s.runtime.dry_run ? "warn" : "ok");

  $("#pill-wb").textIfChanged = "Wallbox: " + (s.wallbox.enabled ? "aktiv" : "aus");
  $("#pill-wb").classIfChanged = "pill " + (s.wallbox.enabled ? "ok" : "warn");

  const sm = s.summer_mode ?? {};
  const pillSummer = $("#pill-summer");
  if (pillSummer) {
    // immer sichtbar: an (grün) / aus (grau); bei Netz-Heizung Warnfarbe
    pillSummer.classList.remove("hidden");
    if (sm.enabled) {
      pillSummer.textIfChanged = sm.heating ? "Sommermodus: an · Netz-Heizung" : "Sommermodus: an";
      pillSummer.classIfChanged = "pill " + (sm.heating ? "warn" : "ok");
    } else {
      pillSummer.textIfChanged = "Sommermodus: aus";
      pillSummer.classIfChanged = "pill subtle";
    }
  }

  pillTimes.status = fmtTime(s.timestamp);
  updatePillTime();

  // summary
  $("#summary-state").textIfChanged = s.summary?.state ?? "–";
  $("#summary-reason").textIfChanged = s.summary?.reason ?? "";
  const sev = s.summary?.severity ?? "ok";
  const badge = $("#summary-severity");
  badge.textIfChanged = sev;
  badge.classIfChanged = "severity-badge " + sev;

  // PV & Netz
  const h = s.heater;
  renderPvNetz(h, s.wallbox);

  // Wasserspeicher
  const heaterAktiv = (h.heater_meter_power ?? 0) > 50
    || ["ph1","ph2","ph3"].some(k => h.phases?.[k] === true);
  $("#m-temp").textIfChanged       = fmtTemp(h.storage_temp);
  $("#m-temp-max").textIfChanged   = fmtTemp(h.storage_max_temp);
  $("#m-temp-resume").textIfChanged = fmtTemp(h.resume_temp);
  const status = h.temperature_status ?? "–";
  const tempBadge = $("#m-temp-status");
  const { label: storageLabel, cls: storageCls } = storageBadge(status, heaterAktiv);
  tempBadge.textIfChanged = storageLabel;
  tempBadge.classIfChanged = "badge " + storageCls;
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

  // Sommermodus-Hinweis in Wasserspeicher-Card
  const summerHint = $("#summer-mode-hint");
  if (summerHint) {
    if (sm.enabled) {
      summerHint.classList.remove("hidden");
      if (sm.heating) {
        summerHint.textIfChanged =
          `Sommermodus: Netz-Heizung aktiv – Ziel ${sm.target_temp ?? "–"} °C`;
      } else {
        summerHint.textIfChanged =
          `Sommermodus: Netz-Heizung ab ${sm.min_temp ?? "–"} °C, Ziel ${sm.target_temp ?? "–"} °C`;
      }
    } else {
      summerHint.classList.add("hidden");
    }
  }

  // Phasen — drei Dots
  const phEl = $("#phases");
  const decisions = h.decisions ?? [];
  if (decisions.length > 0) {
    phEl.htmlIfChanged = `<div class="phases-dots">${
      decisions.map(d => {
        const off = h.phases_disabled?.[d.phase.toLowerCase()] === true;
        return `
        <div class="phase-dot-item">
          <div class="phase-dot ${off ? "disabled" : d.current_state === true ? "on" : ""}"></div>
          <div class="phase-dot-label">${escapeHtml(d.phase)}${off ? '<small class="phase-off">deaktiviert</small>' : ""}</div>
        </div>`;
      }).join("")
    }</div>`;
  } else {
    phEl.htmlIfChanged = `<div class="phases-dots">
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH1</div></div>
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH2</div></div>
      <div class="phase-dot-item"><div class="phase-dot"></div><div class="phase-dot-label">PH3</div></div>
    </div>`;
  }

  // Thermostat im Heizstab (Relais an, 0 W) bzw. „Speicher praktisch voll“
  const th = $("#heater-thermo");
  if (th) {
    const t = h.thermostat ?? {};
    th.classList.toggle("hidden", !t.state);
    th.textIfChanged = t.state === "full"
      ? `Speicher praktisch voll – Thermostat im Heizstab ${t.open_min} min offen bei ${t.temp_c} °C, wieder frei ab ${t.release_c} °C`
      : t.state === "open"
        ? `Thermostat im Heizstab hat abgeschaltet – Relais an, 0 W seit ${t.open_min} min (${t.temp_c} °C). Überschuss geht an Batterie/Wallbox.`
        : "";
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
    $("#charge-state").textIfChanged = stateLabel;
    const p = wbStatus.power_w;
    $("#charge-power").textIfChanged = (p != null && !isNaN(p) && p > 50)
      ? fmtPower(p, { kw: true })
      : (wbCharging ? "0,00 kW" : "");
  } else {
    ci.classList.add("hidden");
  }

  // Energy values (session + lifetime)
  const ce = $("#charge-energy");
  if (wb.enabled && (wbStatus.energy_session_wh != null || wbStatus.energy_total_wh != null || wbStatus.charge_duration_s != null)) {
    ce.classList.remove("hidden");
    $("#charge-session").textIfChanged  = fmtEnergy(wbStatus.energy_session_wh);
    $("#charge-total").textIfChanged    = fmtEnergy(wbStatus.energy_total_wh);
    $("#charge-duration").textIfChanged = fmtDuration(wbStatus.charge_duration_s);
  } else {
    ce.classList.add("hidden");
  }

  const lmoLabel = wbStatus.lmo === 3 ? "Basic" : wbStatus.lmo === 4 ? "Eco" : (wbStatus.lmo ?? "–");
  $("#wallbox-kv").htmlIfChanged = `
    <div><span>Modus (lmo)</span><b>${lmoLabel}</b></div>
    <div><span>forceState (frc)</span><b>${wbStatus.frc ?? "–"}</b></div>
    <div><span>allowed (alw)</span><b>${wbStatus.alw === true ? "ja" : wbStatus.alw === false ? "nein" : "–"}</b></div>
    <div><span>car</span><b>${wbStatus.car ?? "–"}</b></div>
    <div><span>amp</span><b>${wbStatus.amp ?? "–"} A</b></div>
  `;
  const btnBasic = $("#btn-wb-basic");
  const btnEco   = $("#btn-wb-eco");
  if (btnBasic) {
    const on = wbStatus.lmo === 3;
    btnBasic.style.background  = on ? "#2ed8a3" : "transparent";
    btnBasic.style.color       = on ? "#1a1d23" : "#555";
    btnBasic.style.fontWeight  = on ? "500" : "";
  }
  if (btnEco) {
    const on = wbStatus.lmo === 4;
    btnEco.style.background = on ? "#e8a435" : "transparent";
    btnEco.style.color      = on ? "#1a1d23" : "#555";
    btnEco.style.fontWeight = on ? "500" : "";
  }
  const wbDecEl = $("#wallbox-decision");
  if (!wb.enabled) {
    wbDecEl.htmlIfChanged = `<b>Wallbox in Config deaktiviert.</b>`;
  } else if (wbDec && wbDec.action) {
    const t = (wbDec.target_force_state == null) ? "–" : wbDec.target_force_state;
    const r = wbDec.reason ?? "";
    wbDecEl.htmlIfChanged = `
      <div><b>Entscheidung:</b> ${wbDec.action} (target frc=${t})</div>
      <div>${escapeHtml(r)}</div>
    `;
  } else {
    wbDecEl.htmlIfChanged = "";
  }

  // active card glow – wallbox
  $("#wallbox-dot").closest?.(".card")?.classList.toggle("active-wallbox", wbCharging);

  // wb-banner mit konkreten Werten
  const banner = $("#wb-banner");
  if (banner) {
    const surplus = h.surplus_without_heater;
    const surplusKw = (surplus != null) ? ((-surplus) / 1000).toFixed(1) + " kW" : "–";
    if (!wb.enabled) {
      banner.classIfChanged = "wb-banner hidden";
    } else if (wbDec.action === "error") {
      banner.classIfChanged = "wb-banner error";
      banner.textIfChanged = "Wallbox nicht erreichbar";
    } else if (wbDec.action === "release" || (wbStatus.frc === 0 && wbStatus.fup)) {
      if (wbCharging) {
        banner.classIfChanged = "wb-banner ok";
        banner.textIfChanged = `Wallbox lädt — Überschuss: ${surplusKw}`;
      } else {
        banner.classIfChanged = "wb-banner";
        banner.textIfChanged = `Wallbox freigegeben — kein Auto lädt (Überschuss: ${surplusKw})`;
      }
    } else if (wbDec.action === "skipped") {
      banner.classIfChanged = "wb-banner";
      banner.textIfChanged = "Wallbox manueller Modus — kein Eingriff";
    } else {
      banner.classIfChanged = "wb-banner warn";
      banner.textIfChanged = `Wallbox pausiert — Überschuss: ${surplusKw} (zu gering)`;
    }
  }

  const wbCard = document.getElementById("wallbox-card");
  if (wbCard) wbCard.style.display = wb.enabled ? "" : "none";
  renderEnergyFlow(h, wb, lastCascadeStatus, lastZendure);
}

// ══════════════════════════════════════════════════════════════════
// Energiefluss – haus-zentriertes Layout
// ══════════════════════════════════════════════════════════════════
function renderEnergyFlow(heater, wb, cs, zd) {
  const el = document.getElementById("flow-diagram");
  if (!el) return;

  const W = 360, ACTIVE = "#2ed8a3", PVC = "#fbbf24";
  const NW = 88, NH = 52, H_W = 100;

  // data
  const pvW     = heater.pv_power ?? 0;          // Summe (Haus-Bilanz)
  const pergolaOn_  = heater.pv2_enabled === true;
  const pv1W    = pergolaOn_ ? (heater.pv1_power ?? 0) : pvW;
  const pgW     = heater.pv2_power ?? 0;
  const pgName  = (heater.pv2_name || "Pergola").toUpperCase().slice(0, 11);
  const gridW   = heater.main_meter_power ?? 0;
  const heaterW = heater.heater_meter_power ?? 0;
  const phases  = ["ph1","ph2","ph3"].filter(k => heater.phases?.[k] === true).length;
  const wbSt    = wb.status ?? {};
  const wbChrg  = wbSt.charging === true;
  const wbPwrW  = wbSt.power_w ?? 0;

  // flow state
  const pvOn    = pvW >= 50;
  const pv1On   = pv1W >= 50;
  const pgOn    = pergolaOn_ && pgW >= 50;
  const feeding = gridW < -50;
  const drawing = gridW > 50;
  const gridOn  = feeding || drawing;
  // „Heizt“ nur bei gemessener Leistung: ein eingeschaltetes Relais ohne Last (Speicher voll, Thermostat)
  // darf den Energiefluss nicht als Laden zeigen
  const heatOn  = heaterW > 50;
  // Phasen-Punkte; ist ein Phasenstatus nicht lesbar (Shelly offline, null), die Zahl der
  // laufenden Phasen aus der gemessenen Heizstab-Leistung schätzen (≈ 1,5 kW je Phase)
  const phaseOff = ["ph1", "ph2", "ph3"].map(k => heater.phases_disabled?.[k] === true);
  const phaseStates = ["ph1", "ph2", "ph3"].map(k => heater.phases?.[k]);
  const nActive = 3 - phaseOff.filter(Boolean).length;
  const estPhases = heaterW > 50 ? Math.min(nActive, Math.max(1, Math.round(heaterW / 1500))) : 0;
  let estLeft = estPhases;
  const phaseDots = phaseStates.map((st, i) => {
    if (phaseOff[i]) return "off";                     // deaktiviert
    if (st === true) { estLeft--; return true; }
    if (st == null && heaterW > 50 && estLeft > 0) { estLeft--; return true; }
    return false;
  });
  const wbOn    = wbChrg && wbPwrW > 50;

  // Shelly cascade devices
  const shellys = (cs?.devices ?? []).filter(d => d.enabled && d.type?.startsWith("shelly"));

  // Hausverbrauch estimate: PV + net grid - heizstab (Batterie: Entladung dazu, Ladung ist kein Verbrauch)
  const kwOrW = w => Math.abs(w) >= 1000 ? (w / 1000).toFixed(2) + " kW" : Math.round(w) + " W";
  const batNetW = zd?.enabled ? ((zd.discharge_w ?? 0) - (zd.charge_w ?? 0)) : 0;
  const hvW   = Math.max(0, pvW + gridW - heaterW + batNetW);
  const hvStr = kwOrW(hvW);

  // node geometry (SVG coordinates, viewBox 0 0 360 H)
  const TOP_Y = 4, MID_Y = 74, BUS_Y = 138, BOT_Y = 148;
  const PG_W = 76;
  const pv = pergolaOn_
    ? { x: W / 2 - 8 - NW, y: TOP_Y, cx: W / 2 - 8 - NW / 2, by: TOP_Y + NH }
    : { x: (W - NW) / 2, y: TOP_Y, cx: W / 2, by: TOP_Y + NH };
  const pg = { x: W / 2 + 8, y: TOP_Y + 4, cx: W / 2 + 8 + PG_W / 2, by: TOP_Y + 4 + NH - 4, nh: NH - 4 };
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
    icon: "🌡️", label: "WASSERSPEICHER",
    value: heater.storage_temp != null ? fmtTemp(heater.storage_temp) : "–",
    sub: speicherStatusLabel(heater.temperature_status),
    aktiv: heatOn, flow: heatOn, target: "storage-card",
  });
  if (zd?.enabled) {
    // Batterie: Laden = Linie Haus → Batterie, Entladen = Batterie → Haus
    const ch = zd.charge_w ?? 0, dis = zd.discharge_w ?? 0;
    const st = zd.state;
    const pw = st === "charging" ? ch : st === "discharging" ? dis : 0;
    bots.push({
      icon: "🔋", label: (zd.name || "Batterie").toUpperCase().slice(0, 12),
      value: !zd.reachable ? "n/a" : zd.soc != null ? Math.round(zd.soc) + " %" : "–",
      sub: !zd.reachable ? "offline" : st === "charging" ? `lädt ${kwOrW(pw)}` : st === "discharging" ? `entlädt ${kwOrW(pw)}` : "Standby",
      aktiv: st === "charging" || st === "discharging", flow: st === "charging" || st === "discharging",
      rev: st === "discharging", target: "zendure-card",
    });
  }
  if (wb.enabled) {
    let wbV = "–", wbS = "";
    if (wbSt.frc != null) {
      if (wbChrg) {
        wbV = wbPwrW > 50 ? kwOrW(wbPwrW) : "lädt";
        wbS = "Auto lädt";
      } else {
        wbV = wbSt.frc === 1 ? "pausiert" : wbSt.frc === 0 ? "freigeg." : `frc=${wbSt.frc}`;
        wbS = wbSt.fup ? "Eco/PV" : "manuell";
      }
    }
    bots.push({ icon: "🚗", label: "WALLBOX", value: wbV, sub: wbS, aktiv: wbOn, flow: wbOn, target: "wallbox-card" });
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
      aktiv, flow, target: "cascade-mini",          // eBike/Shelly: Kaskade-Card listet die Geräte
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

  function node(x, y, w, nh, { icon, label, value, sub, aktiv, accent = ACTIVE, always = false, dots = null, target = null }) {
    const op  = !aktiv && !always ? ' opacity="0.35"' : "";
    // antippbar: scrollt zur passenden Card (Klick-Handler einmalig auf #flow-diagram, siehe initFlowTaps)
    const tap = target ? ` class="ef-tap" data-target="${target}" role="button" tabindex="0" aria-label="${esc(label)} – Details"` : "";
    const bdr = aktiv || always ? accent : "rgba(255,255,255,0.12)";
    const bg  = aktiv || always ? rgba(accent, 0.07) : "rgba(255,255,255,0.04)";
    const vc  = aktiv || always ? accent : "rgba(255,255,255,0.6)";
    return `<g transform="translate(${x},${y})"${op}${tap}>
  <rect width="${w}" height="${nh}" rx="10" fill="${bg}" stroke="${bdr}" stroke-width="1.5"/>
  <text x="${w/2}" y="16" text-anchor="middle" font-size="14" font-family="'DM Mono',monospace">${icon}</text>
  <text x="${w/2}" y="27" text-anchor="middle" font-size="${label.length > 11 ? 5.4 : 6.5}" fill="rgba(255,255,255,0.5)" letter-spacing="${label.length > 11 ? 0.4 : 1}" font-family="'DM Mono',monospace">${esc(label)}</text>
  <text x="${w/2}" y="39" text-anchor="middle" font-size="12" font-weight="700" fill="${vc}" font-family="'DM Mono',monospace">${esc(value)}</text>
  ${dots
    ? dots.map((on, i) => on === "off"
        ? `<circle cx="${w/2 + (i - (dots.length - 1) / 2) * 9}" cy="${nh-8}" r="2.3" fill="none" stroke="rgba(255,255,255,0.18)" stroke-width="1" stroke-dasharray="1.2 1.2"><title>deaktiviert</title></circle>`
        : `<circle cx="${w/2 + (i - (dots.length - 1) / 2) * 9}" cy="${nh-8}" r="2.3" fill="${on ? accent : "none"}" stroke="${on ? accent : "rgba(255,255,255,0.4)"}" stroke-width="1"/>`).join("")
    : `<text x="${w/2}" y="${nh-5}" text-anchor="middle" font-size="7.5" fill="rgba(255,255,255,0.4)" font-family="'DM Mono',monospace">${esc(sub)}</text>`}
</g>`;
  }

  function hausNode() {
    return `<g transform="translate(${hs.x},${hs.y})" class="ef-tap" data-target="house-card" role="button" tabindex="0" aria-label="Haus – Details">
  <rect width="${H_W}" height="${NH}" rx="10" fill="rgba(46,216,163,0.05)" stroke="${ACTIVE}55" stroke-width="2"/>
  <text x="${H_W/2}" y="16" text-anchor="middle" font-size="14" font-family="'DM Mono',monospace">🏠</text>
  <text x="${H_W/2}" y="27" text-anchor="middle" font-size="6.5" fill="rgba(255,255,255,0.5)" letter-spacing="1" font-family="'DM Mono',monospace">HAUS</text>
  <text x="${H_W/2}" y="39" text-anchor="middle" font-size="12" font-weight="700" fill="${ACTIVE}" font-family="'DM Mono',monospace">${esc(hvStr)}</text>
  <text x="${H_W/2}" y="${NH-5}" text-anchor="middle" font-size="7.5" fill="rgba(255,255,255,0.4)" font-family="'DM Mono',monospace">Verbrauch</text>
</g>`;
  }

  // connections
  const conn = [];
  if (pergolaOn_) {
    // beide PV-Linien laufen über eine gemeinsame Schiene zum Haus
    const JY = pv.by + (hs.ty - pv.by) / 2;
    if (pv1On) conn.push(`<path d="M ${pv.cx} ${pv.by} L ${pv.cx} ${JY} L ${hs.cx} ${JY} L ${hs.cx} ${hs.ty}" class="ef-line ef-down"/>`);
    if (pgOn)  conn.push(`<path d="M ${pg.cx} ${pg.by} L ${pg.cx} ${JY} L ${hs.cx} ${JY} L ${hs.cx} ${hs.ty}" class="ef-line ef-down"/>`);
  } else if (pvOn) {
    conn.push(L(pv.cx, pv.by, hs.cx, hs.ty, "down"));
  }
  if (gridOn) conn.push(L(nz.rx, nz.ry, hs.lx, hs.ly, drawing ? "right" : "left"));
  if (heatOn) conn.push(L(hs.rx, hs.ry, hz.lx, hz.ly, "right"));

  const fb = bots.filter(b => b.flow);
  fb.forEach(b => {
    if (b.cx === hs.cx) {
      conn.push(b.rev ? L(b.cx, b.y, hs.cx, hs.by, "down") : L(hs.cx, hs.by, b.cx, b.y, "down"));
    } else if (b.rev) {
      // Entladen: Pfad von der Batterie zum Haus (Animation läuft in Pfadrichtung = nach oben)
      conn.push(`<path d="M ${b.cx} ${b.y} L ${b.cx} ${BUS_Y} L ${hs.cx} ${BUS_Y} L ${hs.cx} ${hs.by}" class="ef-line ef-down"/>`);
    } else {
      conn.push(`<path d="M ${hs.cx} ${hs.by} L ${hs.cx} ${BUS_Y} L ${b.cx} ${BUS_Y} L ${b.cx} ${b.y}" class="ef-line ef-down"/>`);
    }
  });

  // display values
  // Summe Haupt-WR + Pergola, dezent zwischen den beiden PV-Kästen
  const pvSumStr = "Σ " + (Math.abs(pvW) >= 1000 ? (pvW / 1000).toFixed(2).replace(".", ",") + " kW" : Math.round(pvW) + " W");
  const nzVal = Math.abs(gridW) > 50 ? kwOrW(Math.abs(gridW)) : "~0 W";
  const nzSub = feeding ? "Einsp." : drawing ? "Bezug" : "";
  const hzEst = heater.heater_power_source === "phase_sum";   // Zähler tot: Ersatzwert aus den Phasen
  const hzKw  = (hzEst ? "≈" : "") + kwOrW(heaterW);

  el.htmlIfChanged = `<svg viewBox="0 0 ${W} ${SVG_H}" width="100%" style="display:block;margin-top:8px">
${conn.join("\n")}
${node(pv.x, pv.y, NW, NH, { icon:"☀️", label:"PV", value:kwOrW(pv1W), sub:"", aktiv:pv1On, accent:PVC, always:true, target:"pv-card" })}
${pergolaOn_ ? node(pg.x, pg.y, PG_W, pg.nh, { icon:"☀️", label:pgName, value: heater.pv2_power == null ? "n/a" : kwOrW(pgW), sub:"", aktiv:pgOn, accent:PVC, always:true, target:"pv-card" }) : ""}
${pergolaOn_ ? `<text x="${W/2}" y="${pv.by + 6.5}" text-anchor="middle" font-size="6.5" fill="rgba(255,255,255,0.5)" letter-spacing="1" font-family="'DM Mono',monospace">${esc(pvSumStr)}</text>` : ""}
${node(nz.x, nz.y, NW, NH, { icon:"⚡", label:"NETZ", value:nzVal, sub:nzSub, aktiv:gridOn, target:"meter-card" })}
${hausNode()}
${node(hz.x, hz.y, NW, NH, { icon:"🔥", label:"HEIZSTAB", value:hzKw, sub:hzEst ? "aus Phasen" : `${phases}/3 Ph.`, aktiv:heatOn, dots:phaseDots, target:"heater-card" })}
${bots.map(b => node(b.x, b.y, b.w, NH, { icon:b.icon, label:b.label, value:b.value, sub:b.sub, aktiv:b.aktiv, target:b.target })).join("\n")}
</svg>`;
}

// Energiefluss: Kachel antippen → weich zur passenden Card scrollen und sie kurz hervorheben.
// Ein Handler auf dem Container (das SVG wird bei jedem Update neu gezeichnet).
function initFlowTaps() {
  const el = document.getElementById("flow-diagram");
  if (!el || el.dataset.taps) return;
  el.dataset.taps = "1";
  const go = (g) => {
    const card = document.getElementById(g.dataset.target);
    if (!card || card.classList.contains("hidden")) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    card.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    card.classList.remove("card-pulse");
    void card.offsetWidth;                                  // Animation neu starten
    card.classList.add("card-pulse");
    setTimeout(() => card.classList.remove("card-pulse"), 1100);
  };
  el.addEventListener("click", (e) => {
    const g = e.target.closest?.(".ef-tap");
    if (g) go(g);
  });
  el.addEventListener("keydown", (e) => {
    const g = e.target.closest?.(".ef-tap");
    if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); go(g); }
  });
  // Sticky-Kopfzeile: Abstand für scrollIntoView (scroll-margin-top) aus ihrer echten Höhe
  const setH = () => {
    const h = document.querySelector(".topbar")?.offsetHeight ?? 0;
    document.documentElement.style.setProperty("--sticky-h", `${h + 12}px`);
  };
  setH();
  window.addEventListener("resize", setH);
}

// ---------- solax / PV live + heute ----------
function renderSolax(d) {
  const totalEl       = document.getElementById("pv-live-total");
  const stringsEl     = document.getElementById("pv-live-strings");
  const feedinEl      = document.getElementById("pv-live-feedin");
  const consumptionEl = document.getElementById("pv-live-consumption");

  if (d == null) {
    console.log("[renderSolax] d=null – no data available, showing n/a");
    if (totalEl) { totalEl.textIfChanged = "n/a"; totalEl.style.color = "var(--text-dim)"; }
    if (stringsEl) stringsEl.htmlIfChanged = "";
    if (feedinEl) feedinEl.textIfChanged = "–";
    if (consumptionEl) consumptionEl.textIfChanged = "–";
    renderHausverbrauch(null);
    renderToday(null);
    return;
  }

  renderKpiGauges(d);

  if (totalEl) {
    totalEl.textIfChanged = Math.round(d.pv_total_w) + " W";
    totalEl.style.color = d.pv_total_w > 50 ? "var(--c-pv)" : "var(--text-dim)";
  }

  // Tageshoch kommt serverseitig aus der DB (überlebt Reload/Neustart)
  if (d.pv_peak_w > 0) {
    pvPeakToday = d.pv_peak_w; pvPeakTime = d.pv_peak_time;
  } else if (Math.round(d.pv_total_w) > pvPeakToday) {
    // Fallback, falls der Server kein Tageshoch liefert
    pvPeakToday = Math.round(d.pv_total_w);
    pvPeakTime = new Date().toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  }
  const peakEl = document.getElementById('pv-peak-display');
  if (peakEl) {
    peakEl.textIfChanged = pvPeakToday > 0 ? `Tageshoch: ${pvPeakToday} W um ${pvPeakTime}` : 'Tageshoch: –';
  }

  if (stringsEl) {
    const pergola = d.pergola_enabled === true;
    const fmtMax = w => w >= 1000 ? `${+(w / 1000).toFixed(1)} kW` : `${Math.round(w)} W`;
    const row = s => {
      const pct = Math.max(2, Math.min(100, s.w / s.max * 100)).toFixed(1);
      return `<div class="pv-string">
        <div class="pv-string-label">${s.n}</div>
        <div class="pv-string-bar-wrap"><div class="pv-string-bar ${s.cls}" style="width:${pct}%"></div></div>
        <div class="pv-string-val"><span style="color:#e0e2e6">${s.w == null ? "n/a" : Math.round(s.w) + " W"}</span><span style="color:#6a6d75;font-size:11px;margin-left:6px">/ ${fmtMax(s.max)}</span></div>
        <div class="pv-string-sub">${s.v == null ? "" : s.v.toFixed(0) + " V · " + s.a.toFixed(1) + " A"}</div>
      </div>`;
    };
    // Summenwert im Kopf nur bei mehr als einem String (sonst steht er schon im Balken)
    const group = (name, w, nStrings) => `<div class="pv-group"><span>${name}</span>${nStrings > 1 ? `<b>${w == null ? "n/a" : Math.round(w) + " W"}</b>` : ""}</div>`;
    const str1Max = d.str1_max_w ?? 11500, str2Max = d.str2_max_w ?? 6500;
    const strs = [
      { n: "STR1 (Dach)", w: d.pv1_power_w, v: d.pv1_voltage_v, a: d.pv1_current_a, cls: "str1", max: str1Max },
      { n: "STR2 (Wand)", w: d.pv2_power_w, v: d.pv2_voltage_v, a: d.pv2_current_a, cls: "str2", max: str2Max },
    ].map(row).join("");
    if (!pergola) {
      stringsEl.htmlIfChanged = strs;
    } else {
      const pgName = d.pergola_name || "Pergola";
      stringsEl.htmlIfChanged =
        group("PV-Anlage", d.main_total_w, 2) + strs +
        group("Balkonkraftwerk", d.pergola_ok ? d.pergola_power_w : null, 1) +
        row({ n: pgName, w: d.pergola_ok ? d.pergola_power_w : 0,
              v: d.pergola_ok ? d.pergola_voltage_v : null, a: d.pergola_current_a, cls: "str3",
              max: d.pergola_max_w ?? 1000 });
    }
    const lg = document.getElementById("legend-pergola");
    if (lg) {
      lg.classList.toggle("hidden", !pergola);
      if (pergola) lg.textIfChanged = d.pergola_name || "Pergola";
    }
  }

  if (feedinEl) {
    const fi = d.feed_in_w;
    feedinEl.textIfChanged = (fi >= 0 ? "+" : "") + Math.round(fi) + " W";
    feedinEl.style.color = fi >= 0 ? "var(--ok)" : "var(--error)";
  }

  if (consumptionEl) {
    consumptionEl.textIfChanged = Math.round(d.consumption_w) + " W";
  }

  renderHausverbrauch(d);
  renderToday(d);
}

// Tageshoch Hausverbrauch: grau, ab 8 kW orange, ab Hausanschluss-Warnmarke rot
function houseColor(w, maxLoadKw) {
  if (w >= (maxLoadKw ?? 11) * 1000) return "#ff6b6b";
  if (w >= 8000) return "#e8a435";
  return "#8a8d95";
}
function fmtKw(w) { return (w / 1000).toFixed(1) + " kW"; }
function renderHousePeak(d) {
  const el = document.getElementById("hv-peak");
  if (!el) return;
  if (d == null || !(d.house_peak_w > 0)) { el.htmlIfChanged = ""; return; }
  const w = d.house_peak_w;
  el.htmlIfChanged = `Tageshoch: <span style="color:${houseColor(w, d.house_max_load_kw)}">${fmtKw(w)}</span> um ${d.house_peak_time}`;
}

function renderHausverbrauch(d) {
  const valEl   = document.getElementById("hv-val");
  const todayEl = document.getElementById("hv-today");
  const avgEl   = document.getElementById("hv-avg");
  if (!valEl || !todayEl) return;
  if (d == null) {
    valEl.htmlIfChanged     = `–<span class="hv-unit">W</span>`;
    valEl.style.color   = "var(--text-dim)";
    todayEl.textIfChanged = "Heute: –";
    const b0 = document.getElementById("hv-bought");
    if (b0) b0.textIfChanged = "";
    const p0 = document.getElementById("hv-pv");
    if (p0) p0.textIfChanged = "";
    renderHousePeak(null);
    if (avgEl) avgEl.textIfChanged = "Ø – kW/h";
    return;
  }
  const w = Math.round(d.consumption_w);
  if (w >= 1000) {
    valEl.htmlIfChanged = `${(w / 1000).toFixed(2)}<span class="hv-unit">kW</span>`;
  } else {
    valEl.htmlIfChanged = `${w}<span class="hv-unit">W</span>`;
  }
  valEl.style.color = w > 3000 ? "var(--warn)" : "var(--text)";
  // Gesamtverbrauch heute (Hauptzähler bzw. Solax-Fallback); ohne meter_today alter Wert
  renderHousePeak(d);
  const mt = d.meter_today;
  const kwh = mt?.verbrauch_kwh ?? d.self_consumption_kwh;
  todayEl.textIfChanged = "Heute: " + (kwh != null ? kwh.toFixed(1) + " kWh" : "–");
  const pvShareEl = document.getElementById("hv-pv");
  const boughtEl = document.getElementById("hv-bought");
  const hasShare = mt && mt.verbrauch_kwh > 0;
  const pvKwh = hasShare ? Math.max(0, mt.verbrauch_kwh - mt.bezug_kwh) : 0;
  const pvPct = hasShare ? Math.round(Math.min(100, pvKwh / mt.verbrauch_kwh * 100)) : 0;
  if (pvShareEl) {
    pvShareEl.htmlIfChanged = hasShare
      ? `davon aus PV: <span style="color:#2ed8a3">${pvKwh.toFixed(1)} kWh (${pvPct}%)</span>`
      : "";
  }
  if (boughtEl) {
    boughtEl.htmlIfChanged = hasShare
      ? `davon zugekauft: <span style="color:#ff6b6b">${mt.bezug_kwh.toFixed(1)} kWh (${Math.round(Math.min(100, mt.bezug_kwh / mt.verbrauch_kwh * 100))}%)</span>`
      : "";
  }
  if (avgEl && kwh != null) {
    const hours = Math.max(1, new Date().getHours());
    avgEl.textIfChanged = "Ø " + (kwh / hours).toFixed(2) + " kW/h";
  }
}

async function drawHausverbrauchSparkline() {
  const canvas = document.getElementById("hausverbrauch-sparkline");
  if (!canvas) return;
  const today = new Date().toISOString().slice(0, 10);
  let entries;
  try {
    const res = await api(`/api/history/hourly?for_date=${today}`);
    entries = res.entries || [];
  } catch (e) {
    return;
  }
  const values = entries.map(e => e.consumption_w || 0);
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;
  const pad = 4;
  const max = Math.max(...values) * 1.1 || 1;
  const step = (w - pad * 2) / (values.length - 1);
  const pts = values.map((v, i) => ({
    x: pad + i * step,
    y: h - pad - (v / max) * (h - pad * 2),
  }));
  // Gradient fill
  const lastPt = pts[pts.length - 1];
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(46,216,163,0.25)");
  grad.addColorStop(1, "rgba(46,216,163,0)");
  ctx.beginPath();
  pts.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
  ctx.lineTo(lastPt.x, h);
  ctx.lineTo(pts[0].x, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  // Line
  ctx.beginPath();
  ctx.strokeStyle = "#2ed8a3";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  pts.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
  ctx.stroke();
  // Dot at current value
  ctx.beginPath();
  ctx.arc(lastPt.x, lastPt.y, 3, 0, Math.PI * 2);
  ctx.fillStyle = "#2ed8a3";
  ctx.fill();
}

function renderPvNetz(h, wb) {
  const el = document.getElementById("pv-netz-body");
  if (!el) return;

  const pvW       = Math.max(0, Math.round(h.pv_power ?? 0));
  const heizstabW = Math.max(0, Math.round(h.heater_meter_power ?? 0));
  const gridW     = Math.round(h.main_meter_power ?? 0);
  const wbSt      = wb?.status ?? {};
  const wallboxW  = wbSt.charging === true ? Math.max(0, Math.round(wbSt.power_w ?? 0)) : 0;
  const hausW     = Math.max(0, Math.round(lastSolaxData?.consumption_w ?? 0));
  const isEinsp   = gridW < 0;
  const zaehlerW  = Math.abs(gridW);

  // Segment-Prozente (alle positiv, Summe ≤ 100)
  const total = pvW || 1;
  const heizstabPct  = Math.max(0, Math.min(100, heizstabW / total * 100));
  const wallboxPct   = wallboxW > 50
    ? Math.max(0, Math.min(100 - heizstabPct, wallboxW / total * 100))
    : 0;
  const grundlastW   = Math.max(0, hausW - heizstabW - wallboxW);
  const grundlastPct = Math.max(0, Math.min(100 - heizstabPct - wallboxPct, grundlastW / total * 100));
  const netzPct      = Math.max(0, 100 - heizstabPct - wallboxPct - grundlastPct);

  const seg = (pct, bg, txt) => pct < 0.5 ? '' : `
    <div style="width:${pct.toFixed(1)}%;background:${bg};display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;overflow:hidden;flex-shrink:0">
      ${pct > 15 ? txt : ''}
    </div>`;

  const netzBg = !isEinsp && zaehlerW > 50 ? 'rgba(239,68,68,0.5)' : 'rgba(232,164,53,0.5)';
  const pvDisp = pvW >= 1000 ? (pvW / 1000).toFixed(2) + ' kW' : pvW + ' W';

  const bar = pvW > 0
    ? seg(heizstabPct, '#2ed8a3', 'Heizstab')
      + (wallboxPct > 0 ? seg(wallboxPct, '#f5782a', 'Wallbox') : '')
      + seg(grundlastPct, 'rgba(46,216,163,0.4)', 'Eigenverbr.')
      + seg(netzPct, netzBg, isEinsp ? 'Einsp.' : 'Bezug')
    : '<div style="width:100%;background:rgba(255,255,255,0.06)"></div>';

  const wbLabel = wallboxW > 50
    ? `<span>Wallbox: ${wallboxW} W</span>` : '';

  el.htmlIfChanged = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span style="font-size:12px;color:var(--text-muted)">☀ PV Erzeugung</span>
      <span style="font-size:18px;font-weight:500;color:#e8a435">${pvDisp}</span>
    </div>
    <div style="height:24px;background:#1a1d23;border-radius:6px;overflow:hidden;display:flex;margin-bottom:8px">
      ${bar}
    </div>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);margin-bottom:14px;flex-wrap:wrap;gap:4px">
      <span>Heizstab: ${h.heater_power_source === "phase_sum" ? `≈${heizstabW} W (aus Phasen)` : `${heizstabW} W`}</span>
      ${wbLabel}
      <span>Haus: ${grundlastW} W</span>
      <span>Netz: ${zaehlerW} W</span>
    </div>
    <div style="border-top:1px solid var(--line);padding-top:10px;display:flex;justify-content:space-between;font-size:12px">
      <span style="color:var(--text-muted)">Hauptzähler</span>
      <span style="color:${isEinsp ? 'var(--ok)' : 'var(--text)'}">
        ${isEinsp ? '+' : ''}${zaehlerW} W ${isEinsp ? 'Einsp.' : 'Bezug'}
      </span>
    </div>`;
}

function renderToday(d) {
  const el = document.getElementById("sol-today");
  if (!el) return;

  const H2 = `<span style="font-size:11px;letter-spacing:0.14em;color:rgba(255,255,255,0.65);text-transform:uppercase;font-weight:500">Heute</span>`;
  const seg = (pct, bg, txt) =>
    `<div style="width:${pct.toFixed(1)}%;background:${bg};display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;overflow:hidden;flex-shrink:0">${pct > 20 ? txt : ''}</div>`;
  const lbl = (txt) =>
    `<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">${txt}</div>`;
  // Großer Tagesertrag (zentriert) + optionale Aufteilung Dach / Balkonkraftwerk
  const bigTotal = (kwh) => `
    ${H2}
    <div style="text-align:center;margin:12px 0 4px">
      <span style="font-size:36px;font-weight:500;color:#e8a435">${kwh == null ? "–" : kwh.toFixed(1)}</span>
      <span style="font-size:16px;color:#8a8d95;margin-left:4px">kWh</span>
    </div>
    <div style="text-align:center;font-size:11px;color:#6a6d75;letter-spacing:1.5px;margin-bottom:14px">ERZEUGT</div>`;
  const splitRow = (label, kwh, pct, color) => `
    <div style="display:flex;align-items:center;gap:10px;font-size:12px">
      <span style="flex:0 0 118px;color:#8a8d95;white-space:nowrap">☀ ${label}</span>
      <span style="flex:0 0 62px;text-align:right;color:#e0e2e6;white-space:nowrap">${kwh == null ? "–" : kwh.toFixed(1)} kWh</span>
      <div style="flex:1;min-width:20px;height:6px;background:#1a1d23;border-radius:3px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${color}"></div>
      </div>
      <span style="flex:0 0 32px;text-align:right;color:#6a6d75">${pct}%</span>
    </div>`;
  const barWrap = (inner) =>
    `<div style="height:28px;background:#1a1d23;border-radius:6px;overflow:hidden;display:flex;margin-bottom:6px">${inner}</div>`;

  if (!d || (d.yield_today_kwh ?? 0) < 0.001) {
    const pvZero = barWrap(`<div style="width:100%;background:rgba(255,255,255,0.06);display:flex;align-items:center;justify-content:center;font-size:10px;color:rgba(255,255,255,0.3)">kein PV</div>`);
    const netzFull = barWrap(seg(100, 'rgba(255,107,107,0.5)', 'Netz 100%'));
    el.htmlIfChanged = `
      ${bigTotal(null)}
      ${lbl('Wohin ging der PV-Strom?')}${pvZero}
      ${lbl('Woher kam der Hausstrom?')}${netzFull}
      <div style="display:flex;gap:8px;margin-top:14px">
        ${kpiBox('–%', 'Eigenverbr.', '#2ed8a3')}
        ${kpiBox('–%', 'Autarkie', '#e8a435')}
        ${kpiBox('–', 'kWh verbr.', '#e0e2e6')}
      </div>`;
    return;
  }

  const pvKwh    = d.yield_today_kwh       ?? 0;
  const einspKwh = d.grid_out_today_kwh    ?? 0;
  const bezugKwh = d.grid_in_today_kwh     ?? 0;
  const eigenKwh = d.self_consumption_kwh  ?? Math.max(0, pvKwh - einspKwh);
  const gesamtKwh = eigenKwh + bezugKwh;

  const eigenPct  = pvKwh > 0 ? Math.min(100, eigenKwh / pvKwh * 100) : 0;
  const einspPct  = Math.max(0, 100 - eigenPct);
  const pvAntPct  = gesamtKwh > 0 ? Math.min(100, eigenKwh / gesamtKwh * 100) : 0;
  const bezugPct  = Math.max(0, 100 - pvAntPct);

  // Aufteilung nur mit aktivem zweiten Wechselrichter (Anteil am Gesamtertrag)
  let splitHtml = "";
  if (d.pergola_enabled === true) {
    const pv2Kwh = d.pergola_ok ? (d.pergola_yield_today_kwh ?? 0) : null;
    const pv1Kwh = d.main_yield_today_kwh ?? Math.max(0, pvKwh - (pv2Kwh ?? 0));
    const pv1Pct = pvKwh > 0 ? Math.min(100, Math.round(pv1Kwh / pvKwh * 100)) : 0;
    const pv2Pct = pv2Kwh == null ? 0 : 100 - pv1Pct;
    splitHtml = `<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px">
      ${splitRow("PV-Anlage", pv1Kwh, pv1Pct, "#e8a435")}
      ${splitRow("Balkonkraftwerk", pv2Kwh, pv2Pct, "#2ed8a3")}
    </div>`;
  }

  const evQuote = pvKwh > 0 ? Math.round(eigenKwh / pvKwh * 100) : 0;
  const autarkie = gesamtKwh > 0 ? Math.round(eigenKwh / gesamtKwh * 100) : 0;

  el.htmlIfChanged = `
    ${bigTotal(pvKwh)}
    ${splitHtml}
    ${lbl('Wohin ging der PV-Strom?')}
    ${barWrap(
      seg(eigenPct,  '#2ed8a3',               `Eigenverbr. ${eigenKwh.toFixed(1)}`) +
      seg(einspPct,  'rgba(232,164,53,0.5)',   `Einspeisung ${einspKwh.toFixed(1)}`)
    )}
    ${lbl('Woher kam der Hausstrom?')}
    ${barWrap(
      seg(pvAntPct,  '#2ed8a3',               `PV ${eigenKwh.toFixed(1)}`) +
      seg(bezugPct,  'rgba(255,107,107,0.5)', `Netz ${bezugKwh.toFixed(1)}`)
    )}
    <div style="display:flex;gap:8px;margin-top:14px">
      ${kpiBox(evQuote + '%',              'Eigenverbr.', '#2ed8a3')}
      ${kpiBox(autarkie + '%',             'Autarkie',    '#e8a435')}
      ${kpiBox(gesamtKwh.toFixed(1),       'kWh verbr.',  '#e0e2e6')}
    </div>
    <div style="font-size:10px;color:var(--text-muted);margin-top:8px;text-align:right">Quelle: ${d.meter_today?.source === "shelly" ? "Hauptzähler" : "Wechselrichter"}</div>`;
}

function kpiBox(val, label, color) {
  return `<div style="flex:1;background:#1a1d23;border-radius:8px;padding:10px;text-align:center">
    <div style="font-size:20px;font-weight:500;color:${color}">${val}</div>
    <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${label}</div>
  </div>`;
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
  el.htmlIfChanged = `<div class="kpi-gauges-row">
    ${gauge(selfusePct, "Eigenverbrauch", !hasData)}
    ${gauge(autarkyPct, "Autarkie",       !hasData)}
  </div>`;
}

// X-Achsen-Fenster der Tages-Charts: Sonnenaufgang−1h … Sonnenuntergang+1h
// (volle Stunden). Ohne Sonnenzeiten Fallback 5h–21h.
function sunWindow(res) {
  const toH = t => {
    const m = /^(\d{1,2}):(\d{2})/.exec(t || "");
    return m ? Number(m[1]) + Number(m[2]) / 60 : null;
  };
  const sr = toH(res?.sunrise), ss = toH(res?.sunset);
  if (sr == null || ss == null || ss <= sr) return [5, 21];
  const xMin = Math.max(0, Math.floor(sr) - 1);
  const xMax = Math.min(24, Math.ceil(ss) + 1);
  return xMax - xMin >= 2 ? [xMin, xMax] : [5, 21];
}

// ---------- Verlauf / History Charts ----------
function renderDailyChart(entries, win = [5, 21]) {
  const [xMin, xMax] = win;
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
  }).filter(p => p.h >= xMin && p.h <= xMax).sort((a, b) => a.h - b.h);
  if (pts.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Keine Daten im Sonnenfenster.</div>';
    return;
  }

  const maxW = Math.max(...pts.flatMap(p => [p.pv, p.feed, p.cons]), 100);
  const sx = h => pad.l + ((h - xMin) / (xMax - xMin)) * cw;
  const sy = w => pad.t + ch - (w / maxW * ch);

  let yElems = "", xElems = "";
  const yStep = niceStep(maxW, 3);
  for (let yv = 0; yv <= maxW + 1; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv >= 1000 ? (yv/1000).toFixed(0)+"k" : yv}</text>`;
  }
  const xStep = xMax - xMin > 12 ? 3 : 2;
  for (let h = xMin; h <= xMax; h += xStep) {
    const xv = sx(h).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${pad.t+ch}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH-2}" text-anchor="middle">${h}h</text>`;
  }
  const now = new Date();
  const nowH = now.getHours() + now.getMinutes() / 60;
  const nowX = sx(Math.min(Math.max(nowH, xMin), xMax)).toFixed(1);
  const nowLine = (nowH >= xMin && nowH <= xMax)
    ? `<line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${pad.t+ch}" class="now-line"/>` : "";
  const path = key => pts.map((p, i) => `${i===0?"M":"L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`).join(" ");
  const area = key => path(key) + ` L${sx(pts[pts.length-1].h).toFixed(1)},${(pad.t+ch).toFixed(1)} L${sx(pts[0].h).toFixed(1)},${(pad.t+ch).toFixed(1)} Z`;

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}${xElems}</g>
    ${nowLine}
    <path d="${area('cons')}" fill="rgba(224,226,230,0.1)" stroke="none"/>
    <path d="${area('feed')}" fill="rgba(46,216,163,0.15)" stroke="none"/>
    <path d="${area('pv')}" fill="rgba(232,164,53,0.2)" stroke="none"/>
    <path d="${path('cons')}" fill="none" stroke="#e0e2e6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('feed')}" fill="none" stroke="#2ed8a3" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('pv')}" fill="none" stroke="#e8a435" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function renderStringChart(entries, wrapId = "chart-strings-wrap", win = [5, 21]) {
  const [xMin, xMax] = win;
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  if (!entries || entries.length < 2) {
    wrap.htmlIfChanged = '<div class="chart-empty">Noch keine Stundendaten.</div>';
    return;
  }
  const VW = 480, VH = 90;
  const pad = { t: 6, r: 6, b: 22, l: 40 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;

  const pts = entries.map(e => {
    const h = parseInt((e.ts || "").split(" ")[1]?.split(":")[0] || "0", 10);
    return { h, s1: e.str1_w || 0, s2: e.str2_w || 0, s3: e.pv2_w || 0 };
  }).filter(p => p.h >= xMin && p.h <= xMax).sort((a, b) => a.h - b.h);
  if (pts.length < 2) {
    wrap.htmlIfChanged = '<div class="chart-empty">Keine Daten im Sonnenfenster.</div>';
    return;
  }
  // dritte Linie (Pergola) nur wenn der Tag Pergola-Werte enthält
  const hasPg = entries.some(e => e.pv2_w != null);

  const maxW = Math.max(...pts.flatMap(p => hasPg ? [p.s1, p.s2, p.s3] : [p.s1, p.s2]), 100);
  const sx = h => pad.l + ((h - xMin) / (xMax - xMin)) * cw;
  const sy = w => pad.t + ch - (w / maxW * ch);

  let yElems = "", xElems = "";
  const yStep = niceStep(maxW, 3);
  for (let yv = 0; yv <= maxW + 1; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv >= 1000 ? (yv/1000).toFixed(0)+"k" : yv}</text>`;
  }
  const xStep = xMax - xMin > 12 ? 3 : 2;
  for (let h = xMin; h <= xMax; h += xStep) {
    const xv = sx(h).toFixed(1);
    xElems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${pad.t+ch}" class="grid-line"/>`;
    xElems += `<text x="${xv}" y="${VH-2}" text-anchor="middle">${h}h</text>`;
  }
  const now = new Date();
  const nowH = now.getHours() + now.getMinutes() / 60;
  const nowX = sx(Math.min(Math.max(nowH, xMin), xMax)).toFixed(1);
  const nowLine = (nowH >= xMin && nowH <= xMax)
    ? `<line x1="${nowX}" y1="${pad.t}" x2="${nowX}" y2="${pad.t+ch}" class="now-line"/>` : "";
  const path = key => pts.map((p, i) => `${i===0?"M":"L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`).join(" ");
  const area = key => path(key) + ` L${sx(pts[pts.length-1].h).toFixed(1)},${(pad.t+ch).toFixed(1)} L${sx(pts[0].h).toFixed(1)},${(pad.t+ch).toFixed(1)} Z`;

  wrap.htmlIfChanged = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}${xElems}</g>
    ${nowLine}
    <path d="${area('s2')}" fill="rgba(91,140,255,0.15)" stroke="none"/>
    <path d="${area('s1')}" fill="rgba(232,164,53,0.15)" stroke="none"/>
    <path d="${path('s2')}" fill="none" stroke="#5b8cff" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${path('s1')}" fill="none" stroke="#e8a435" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    ${hasPg ? `<path d="${path('s3')}" fill="none" stroke="#2ed8a3" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>` : ""}
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
    bars += `<rect x="${x.toFixed(1)}" y="${(bot-feedH).toFixed(1)}" width="${barW.toFixed(1)}" height="${feedH.toFixed(1)}" rx="2" fill="rgba(232,164,53,0.7)"/>`;
    bars += `<rect x="${x.toFixed(1)}" y="${(bot-feedH-selfH).toFixed(1)}" width="${barW.toFixed(1)}" height="${selfH.toFixed(1)}" rx="2" fill="#2ed8a3"/>`;
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
    bars += `<rect x="${x.toFixed(1)}" y="${(pad.t+ch-h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="1" fill="#e8a435" opacity="${opacity}"/>`;
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
  $$(".verlauf-nav-btn[data-vsec]").forEach(b => b.classList.toggle("active", b.dataset.vsec === name));
  $$(".verlauf-section").forEach(s => s.classList.toggle("hidden", s.id !== "verlauf-" + name));
  _loadCurrentVerlauf();
}

function _loadCurrentVerlauf() {
  if (_vsec === "tag")     loadVerlaufTag(_vTagDate);
  if (_vsec === "woche")   loadVerlaufWoche();
  if (_vsec === "monat")   loadVerlaufMonat(_vMonth);
  if (_vsec === "jahr")    loadVerlaufJahr(_vYear);
  if (_vsec === "strings") loadVerlaufStrings(_vStringsDate);
  if (_vsec === "wetter")   loadVerlaufWetter();
  if (_vsec === "prognose") loadVerlaufPrognose();
  if (_vsec === "netz")    _loadCurrentNetz();
}

async function loadVerlauf() {
  try {
    const cfg = lastConfig || await api("/api/config").catch(() => null);
    const hasMainMeter = !!(cfg?.shelly?.main_meter_url?.trim());
    const netzBtn = document.getElementById("verlauf-netz-btn");
    if (netzBtn) netzBtn.style.display = hasMainMeter ? "" : "none";
    if (_vsec === "netz" && !hasMainMeter) showVerlaufSection("tag");
  } catch {}
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
    const win = sunWindow(hourly);
    renderDailyChart(hourly.entries || [], win);
    renderStringChart(hourly.entries || [], "chart-strings-wrap", win);
    checkStringAlert(hourly.entries || []);
    api(`/api/history/house/day/${dateStr}`).then(renderHouseDayChart).catch(() => {});
    loadZendureDay(dateStr).catch(() => {});
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
    api(`/api/history/house/month/${monthStr}`).then(renderHouseMonthChart).catch(() => {});
    loadZendureMonth(monthStr).catch(() => {});
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

async function loadPeakTimeChart() {
  try {
    const data = await api("/api/history/peak?days=30");
    const entries = (data.entries || []).filter(e => e.peak_hour && e.peak_w);
    renderPeakTimeChart(entries.slice(-7), "chart-peak-week-wrap", 7);
    renderPeakTimeChart(entries, "chart-peak-month-wrap", 30);
  } catch (e) {
    console.error("Tageshoch-Zeitverlauf konnte nicht geladen werden:", e);
  }
}

function renderPeakTimeChart(entries, wrapId, days) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Tagesdaten.</div>';
    return;
  }
  const n = entries.length;
  const kw = entries.map(e => (e.peak_w || 0) / 1000);
  const maxKw = Math.max(...kw, 0.1);

  const VW = 480, VH = 100;
  const pad = { t: 10, r: 6, b: 22, l: 40 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;

  const yStep = niceStep(maxKw, 4);
  const maxY = Math.ceil(maxKw / yStep) * yStep;
  const sy = v => pad.t + ch - (v / maxY * ch);
  const sx = i => n === 1 ? pad.l + cw / 2 : pad.l + (i / (n - 1)) * cw;

  let yElems = "";
  for (let yv = 0; yv <= maxY + 0.001; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(1)}</text>`;
  }

  const weekdays = ["So","Mo","Di","Mi","Do","Fr","Sa"];
  let points = "", dots = "", xLabels = "";
  entries.forEach((e, i) => {
    const x = sx(i), y = sy(kw[i]);
    points += `${x.toFixed(1)},${y.toFixed(1)} `;
    dots += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" fill="#fbbf24">`
          + `<title>${_fmtDate(e.date)} · ${e.peak_hour} Uhr · ${e.peak_w} W</title></circle>`;
    let label = "";
    if (days <= 7) {
      label = weekdays[new Date(e.date + "T12:00:00").getDay()];
    } else if (i === 0 || i % 5 === 0 || i === n - 1) {
      label = parseInt((e.date || "").split("-")[2] || "0") + ".";
    }
    if (label) xLabels += `<text x="${x.toFixed(1)}" y="${VH-3}" text-anchor="middle">${label}</text>`;
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}</g>
    <polyline points="${points.trim()}" fill="none" stroke="#fbbf24" stroke-width="1.5" opacity="0.6"/>
    ${dots}${xLabels}
  </svg>`;
}

// ---------- Batterie – Bedarf pro Nacht (Speichergröße) ----------
async function loadBatteryNights() {
  const card = document.getElementById("nights-card");
  if (!card) return;
  let d;
  try {
    d = await api("/api/history/battery-nights?days=14");
  } catch (e) {
    console.error("Nachtbedarf konnte nicht geladen werden:", e);
    return;
  }
  card.classList.toggle("hidden", !d.enabled);
  if (d.enabled) renderBatteryNights(d);
}

function renderBatteryNights(d) {
  const wrap = document.getElementById("chart-nights-wrap");
  const foot = document.getElementById("nights-foot");
  const nights = d.nights || [];
  const s = d.summary || {};
  document.getElementById("nights-range").textContent =
    s.nights ? `${s.nights} ${s.nights === 1 ? "Nacht" : "Nächte"}` : "";
  if (!nights.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Nachtdaten.</div>';
    foot.textContent = "";
    return;
  }
  const kwh = wh => (wh / 1000).toFixed(1).replace(".", ",");
  const maxKw = kwh(d.max_discharge_w || 2400);
  const peakLegend = document.getElementById("nights-legend-peak");
  if (peakLegend) peakLegend.textContent = `■ Netz: über ${maxKw} kW`;
  const surplus = Object.fromEntries((d.days || []).map(x => [x.date, x]));
  const usable = s.usable_now_wh || 0;
  const top = Math.max(1000, usable, ...nights.map(n => n.need_wh),
                       ...nights.map(n => surplus[n.date]?.surplus_wh || 0)) / 1000;

  const VW = 480, VH = 158;
  const pad = { t: 20, r: 6, b: 22, l: 30 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const yStep = niceStep(top, 4), yMax = Math.ceil(top / yStep) * yStep;
  const sy = v => pad.t + ch - (v / yMax) * ch;
  const slot = cw / nights.length, bw = Math.min(26, slot * 0.6);
  const weekdays = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

  let g = "";
  for (let v = 0; v <= yMax + 1e-9; v += yStep) {
    g += `<line x1="${pad.l}" y1="${sy(v).toFixed(1)}" x2="${pad.l + cw}" y2="${sy(v).toFixed(1)}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${sy(v).toFixed(1)}" text-anchor="end" dominant-baseline="middle">${+v.toFixed(1)}</text>`;
  }
  nights.forEach((n, i) => {
    const cx = pad.l + slot * (i + 0.5), x = (cx - bw / 2).toFixed(1);
    const bat = Math.min(n.battery_wh, n.capacity_wh);
    const segs = [
      [bat, "#e8a435", 0.85, "Batterie"],
      [n.capacity_wh - bat, "var(--c-grid)", 0.85, "Netz: Speicher zu klein"],
      [n.need_wh - n.capacity_wh, "var(--c-grid)", 0.35, `Netz: über ${maxKw} kW`],
    ];
    const faded = n.ongoing || !n.complete;
    const dt = new Date(n.date + "T12:00:00");
    const label = `${weekdays[dt.getDay()]} ${dt.getDate()}.`;
    const tip = `Nacht ${label} ${n.start}–${n.end}${n.ongoing ? " (läuft)" : n.complete ? "" : " (unvollständig)"}\n`
      + `Bedarf ${kwh(n.need_wh)} kWh` + segs.filter(sg => sg[0] > 0).map(sg => `\n· ${sg[3]}: ${kwh(sg[0])} kWh`).join("")
      + (n.excluded_wh > 0 ? `\nnicht eingerechnet (Wallbox/Heizstab): ${kwh(n.excluded_wh)} kWh${n.loads_estimated ? " (geschätzt)" : ""}` : "")
      + (surplus[n.date] ? `\nPV-Überschuss am Tag davor: ${kwh(surplus[n.date].surplus_wh)} kWh` : "");
    let acc = 0;
    g += `<g opacity="${faded ? 0.45 : 1}"><title>${tip}</title>`;
    for (const [wh, color, op] of segs) {
      if (wh <= 0) continue;
      const y0 = sy((acc + wh) / 1000), y1 = sy(acc / 1000);
      g += `<rect x="${x}" y="${y0.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5, y1 - y0).toFixed(1)}" fill="${color}" opacity="${op}"/>`;
      acc += wh;
    }
    g += `<rect x="${x}" y="${pad.t}" width="${bw.toFixed(1)}" height="${ch}" fill="transparent"/></g>`;
    const sur = surplus[n.date];
    if (sur) {
      const sy0 = sy(sur.surplus_wh / 1000), r = 3.2;
      g += `<path d="M${cx.toFixed(1)} ${(sy0 - r).toFixed(1)} l${r} ${r} l${-r} ${r} l${-r} ${-r} z" fill="var(--c-pv)" opacity="${sur.complete ? 0.95 : 0.5}"><title>${tip}</title></path>`;
    }
    g += `<text x="${cx.toFixed(1)}" y="${VH - 4}" text-anchor="middle">${label}</text>`;
  });
  g += `<text x="${pad.l - 4}" y="${pad.t - 9}" text-anchor="end">kWh</text>`;
  if (usable > 0) {
    // Linie über die Balken, Beschriftung oberhalb des Diagramms (überdeckt keinen Balken)
    const yu = sy(usable / 1000).toFixed(1), ly = pad.t - 9;
    g += `<line x1="${pad.l}" y1="${yu}" x2="${pad.l + cw}" y2="${yu}" stroke="#2ed8a3" stroke-width="1" stroke-dasharray="4 3"/>`;
    g += `<line x1="${pad.l + 2}" y1="${ly}" x2="${pad.l + 18}" y2="${ly}" stroke="#2ed8a3" stroke-width="1" stroke-dasharray="4 3"/>`;
    g += `<text x="${pad.l + 22}" y="${ly}" dominant-baseline="middle" fill="#2ed8a3">Akku nutzbar ${kwh(usable)} kWh</text>`;
  }
  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;

  if (!s.nights) {
    foot.innerHTML = "Noch keine vollständige Nacht – die erste Auswertung gibt es morgen früh.";
    return;
  }
  const lines = [
    `Ø Bedarf <b>${kwh(s.need_avg_wh)} kWh</b> pro Nacht, höchstens <b>${kwh(Math.max(...nights.filter(n => n.complete).map(n => n.need_wh)))} kWh</b>`
      + ` (ohne Wallbox und Heizstab; ø ${kwh(s.peak_avg_wh)} kWh davon über ${maxKw} kW – kommt immer aus dem Netz)`,
    `Für jede dieser Nächte bräuchte es <b>${kwh(s.capacity_max_wh)} kWh</b> nutzbar ≈ <b>${kwh(s.size_for_max_wh)} kWh</b> Speicher`
      + ` (jetzt ${kwh(s.capacity_now_wh)} kWh; gereicht hätte er in ${s.nights_covered} von ${s.nights} Nächten)`,
  ];
  if (s.fillable_max_wh != null) {
    lines.push(`Der PV-Überschuss am Tag davor hätte aber höchstens <b>${kwh(s.fillable_max_wh)} kWh</b> gefüllt ≈ <b>${kwh(s.size_fillable_wh)} kWh</b> Speicher – mehr bringt zurzeit nichts`);
  }
  if (nights.some(n => n.loads_estimated)) {
    lines.push(`<span style="opacity:.7">Wallbox/Heizstab in älteren Nächten aus Stundenwerten geschätzt (Details beim Antippen eines Balkens).</span>`);
  }
  foot.innerHTML = lines.join("<br>");
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
    bars += `<rect x="${x.toFixed(1)}" y="${(pad.t + ch - h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="1" fill="#e8a435" opacity="0.85"/>`;
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

// ── Forecast card ────────────────────────────────────────────────────────────

let _lastForecastData = null;

async function loadForecast() {
  try {
    const [data, histData] = await Promise.all([
      api("/api/forecast"),
      api("/api/history/forecast?days=7"),
    ]);
    _lastForecastData = data;
    renderForecastCard(data);
    renderForecastProgress(data, histData.entries || []);
  } catch {
    // non-critical – silently skip
  }
}

function renderForecastCard(data) {
  const card = document.getElementById("forecast-card");
  const kwhEl = document.getElementById("forecast-kwh");
  const badgeEl = document.getElementById("forecast-badge");
  const sunEl = document.getElementById("forecast-sun");
  const ghiEl = document.getElementById("forecast-ghi");
  if (!card) return;

  const t = data?.tomorrow;
  const today = data?.today;

  if (!t) {
    kwhEl.textIfChanged = "–";
    badgeEl.textIfChanged = today?.weather_icon ?? "";
    return;
  }

  kwhEl.textIfChanged = `~${t.predicted_kwh} kWh`;
  badgeEl.textIfChanged = `${t.weather_icon ?? ""} ${t.temp_max != null ? Math.round(t.temp_max) + "°C" : ""}`.trim();
  if (sunEl) sunEl.textIfChanged = t.sunshine_hours != null ? t.sunshine_hours.toFixed(1) + " h" : "–";
  if (ghiEl) ghiEl.textIfChanged = t.ghi_kwh_m2 != null ? t.ghi_kwh_m2.toFixed(1) + " kWh/m²" : "–";
}

function getForecastAssessment(actualKwh, forecastKwh, currentHour, currentMinute, sunsetHour, sunsetMinute) {
  const percent = forecastKwh > 0 ? (actualKwh / forecastKwh * 100) : 0;
  const now = currentHour + currentMinute / 60;
  const sunset = sunsetHour + sunsetMinute / 60;
  const hoursLeft = Math.max(0, sunset - now);
  const remaining = forecastKwh - actualKwh;

  if (hoursLeft <= 0.25) {
    if (percent >= 97) return { text: 'Prognose erreicht ✅', color: '#2ed8a3' };
    if (percent >= 80) return { text: 'Knapp verfehlt (' + Math.round(percent) + '%)', color: '#e8a435' };
    return { text: 'Deutlich unter Prognose (' + Math.round(percent) + '%)', color: '#ff6b6b' };
  }

  // Glockenkurven-Modell: PV-Ertrag folgt ~Halbsinus von Sonnenaufgang bis -untergang.
  // fractionExpected = Anteil der Tagesenergie der bis jetzt erwartet wird.
  const sunriseApprox = 6;
  const totalSunHours = Math.max(1, sunset - sunriseApprox);
  const sunHoursElapsed = Math.max(0, now - sunriseApprox);
  const fractionElapsed = Math.min(1, sunHoursElapsed / totalSunHours);
  const fractionExpected = (1 - Math.cos(Math.PI * fractionElapsed)) / 2;

  // Morgens (< 15 % der Tagesenergie erwartet, ca. bis 09:45): Hochrechnung wäre zu unzuverlässig.
  // Wenn bereits Produktion vorhanden → Auf Kurs, sonst Hinweis dass der Tag gerade beginnt.
  if (fractionExpected < 0.15) {
    if (actualKwh > 0) return { text: 'Auf Kurs – Prognose wird vermutlich erreicht', color: '#2ed8a3' };
    return { text: 'Tag hat gerade begonnen', color: '#aaa' };
  }

  // Hochrechnung: wenn aktuelle Produktion dem Kurvenanteil entspricht → Gesamtschätzung
  const estimatedTotal = actualKwh / fractionExpected;

  if (estimatedTotal >= forecastKwh * 0.9) {
    return { text: 'Auf Kurs – Prognose wird vermutlich erreicht', color: '#2ed8a3' };
  }
  if (estimatedTotal >= forecastKwh * 0.7) {
    return { text: 'Knapp – noch ' + remaining.toFixed(1) + ' kWh in ' + hoursLeft.toFixed(1) + 'h', color: '#e8a435' };
  }
  return { text: 'Wird nicht mehr erreicht – noch ' + remaining.toFixed(1) + ' kWh in ' + hoursLeft.toFixed(1) + 'h nötig', color: '#ff6b6b' };
}

function renderForecastProgress(data, histEntries) {
  const wrap = document.getElementById("forecast-progress-wrap");
  const empty = document.getElementById("forecast-progress-empty");
  if (!wrap) return;

  const today = data?.today;
  const actual = today?.actual_kwh;
  // Gespeicherte Prognose vom Vortag bevorzugen (stabil, keine Schwankungen durch Regressionsänderungen)
  const todayStr = new Date().toISOString().slice(0, 10);
  const todayHist = histEntries.find(e => e.date === todayStr);
  const predicted = (todayHist?.forecast_kwh ?? today?.predicted_kwh);

  if (!predicted || predicted <= 0) {
    wrap.classList.add("hidden");
    if (empty) empty.classList.remove("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  if (empty) empty.classList.add("hidden");

  const pct = actual != null ? Math.min(Math.round((actual / predicted) * 100), 150) : 0;
  const over100 = pct >= 100;
  const fill = document.getElementById("forecast-bar-fill");
  const pctEl = document.getElementById("forecast-percent");
  if (fill) {
    fill.style.width = Math.min(pct, 100) + "%";
    fill.classIfChanged = "forecast-bar-fill" + (over100 ? " over" : "");
  }
  if (pctEl) {
    if (actual == null) {
      pctEl.textIfChanged = "–";
    } else if (over100) {
      pctEl.textIfChanged = `${pct}% – besser als erwartet! 🎉`;
    } else {
      pctEl.textIfChanged = `${pct}% der Prognose erreicht`;
    }
  }

  const fpBarForecast = document.getElementById("fp-bar-forecast");
  const fpBarActual = document.getElementById("fp-bar-actual");
  const fpBarRemaining = document.getElementById("fp-bar-remaining");
  if (fpBarForecast) fpBarForecast.textIfChanged = predicted.toFixed(1);
  if (fpBarActual) fpBarActual.textIfChanged = actual != null ? `${actual.toFixed(1)} kWh` : "";

  const fpForecast = document.getElementById("fp-forecast");
  const fpActual = document.getElementById("fp-actual");
  const fpRemaining = document.getElementById("fp-remaining");
  const fpSunset = document.getElementById("fp-sunset");

  if (fpForecast) fpForecast.textIfChanged = `~${predicted.toFixed(1)} kWh`;
  if (fpActual) fpActual.textIfChanged = actual != null ? `${actual.toFixed(1)} kWh` : "–";

  const sunset = today?.sunset;
  const [sunsetH, sunsetM] = sunset ? sunset.split(":").map(Number) : [21, 0];
  const _nowD = new Date();
  const isEndstand = (_nowD.getHours() * 60 + _nowD.getMinutes()) >= (sunsetH * 60 + sunsetM);

  let remainingText = "–";
  if (actual != null) {
    if (isEndstand) {
      remainingText = "Endstand";
    } else {
      const rem = Math.max(0, predicted - actual);
      remainingText = rem > 0.05 ? `noch ~${rem.toFixed(1)} kWh` : "Ziel erreicht";
    }
  }
  if (fpRemaining) fpRemaining.textIfChanged = remainingText;
  if (fpBarRemaining) fpBarRemaining.textIfChanged = remainingText;
  if (fpSunset) {
    if (!sunset) {
      fpSunset.textIfChanged = "–";
    } else if (isEndstand) {
      fpSunset.textIfChanged = `${sunset} (vorbei)`;
    } else {
      const now = new Date();
      const [sh, sm] = sunset.split(":").map(Number);
      const diffMin = (sh * 60 + sm) - (now.getHours() * 60 + now.getMinutes());
      const hLeft = Math.floor(diffMin / 60);
      const mLeft = diffMin % 60;
      fpSunset.textIfChanged = `${sunset} (noch ${hLeft}h ${mLeft}min)`;
    }
  }

  const assessEl = document.getElementById("forecast-assessment");
  if (assessEl && actual != null) {
    const now = new Date();
    const [sh, sm] = sunset ? sunset.split(":").map(Number) : [21, 0];
    const a = getForecastAssessment(actual, predicted, now.getHours(), now.getMinutes(), sh, sm);
    assessEl.textIfChanged = a.text;
    assessEl.style.color = a.color;
  } else if (assessEl) {
    assessEl.textIfChanged = "";
  }

  const daysEl = document.getElementById("forecast-last-days");
  if (daysEl) {
    const recent = histEntries
      .filter(e => e.forecast_kwh != null && e.actual_kwh != null)
      .slice(-3)
      .reverse();
    if (recent.length === 0) {
      daysEl.htmlIfChanged = "";
    } else {
      const yd = new Date(); yd.setDate(yd.getDate() - 1);
      const db = new Date(); db.setDate(db.getDate() - 2);
      const yStr = yd.toISOString().slice(0, 10);
      const dbStr = db.toISOString().slice(0, 10);
      daysEl.htmlIfChanged =
        `<div class="forecast-last-days-title">Letzte Tage</div>` +
        recent.map(e => {
          const diff = Math.round((e.actual_kwh - e.forecast_kwh) / e.forecast_kwh * 100);
          const isToday = e.date === todayStr;
          const hit = !isToday && e.actual_kwh >= e.forecast_kwh;
          const sign = diff >= 0 ? "+" : "";
          let dateLabel;
          if (e.date === yStr) dateLabel = "Gestern";
          else if (e.date === dbStr) dateLabel = "Vorgestern";
          else if (isToday) dateLabel = "Heute";
          else dateLabel = e.date.slice(5).replace("-", ".");
          const icon  = isToday ? "⏳" : (hit ? "✅" : "❌");
          const color = isToday ? "#8a8d95" : (hit ? "#2ed8a3" : "#ff6b6b");
          return `<div class="forecast-day-row">` +
            `<span class="fdr-date">${dateLabel}</span>` +
            `<span class="fdr-actual">${e.actual_kwh.toFixed(1)} kWh</span>` +
            `<span class="fdr-forecast">(${e.forecast_kwh.toFixed(1)})</span>` +
            `<span class="fdr-icon">${icon}</span>` +
            `<span class="fdr-pct" style="color:${color}">${sign}${diff}%</span>` +
            `</div>`;
        }).join("");
    }
  }
}

// ── Netz sub-tab ─────────────────────────────────────────────────────────────

function _isoWeekNum(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  const jan4 = new Date(d.getFullYear(), 0, 4);
  const startW1 = new Date(jan4.getTime() - ((jan4.getDay() || 7) - 1) * 86400000);
  return Math.max(1, Math.round((d - startW1) / 604800000) + 1);
}

function showNetzSection(name) {
  _vNetzSub = name;
  $$(".netz-nav-btn").forEach(b => b.classList.toggle("active", b.dataset.nsec === name));
  $$(".netz-section").forEach(s => s.classList.toggle("hidden", s.id !== "netz-" + name));
  _loadCurrentNetz();
}

function _loadCurrentNetz() {
  if (_vNetzSub === "tag")   loadVerlaufNetzTag(_vNetzTagDate);
  if (_vNetzSub === "woche") loadVerlaufNetzWoche(_vNetzWeekEnd);
  if (_vNetzSub === "monat") loadVerlaufNetzMonat(_vNetzMonth);
  if (_vNetzSub === "jahr")  loadVerlaufNetzJahr(_vNetzYear);
}

async function loadVerlaufNetzTag(dateStr) {
  _vNetzTagDate = dateStr;
  const today = new Date().toISOString().slice(0, 10);
  const labelEl = document.getElementById("netz-tag-label");
  if (labelEl) labelEl.textContent = dateStr === today ? "Heute" : dateStr;
  const nextBtn = document.getElementById("netz-tag-next");
  if (nextBtn) nextBtn.disabled = dateStr >= today;
  try {
    const url = dateStr === today
      ? "/api/history/grid/today"
      : `/api/history/grid/day/${dateStr}`;
    const data = await api(url);
    renderNetzTagChart(data.entries || []);
    const totalEins = (data.entries || []).reduce((s, e) => s + (e.einspeisung_w || 0), 0) / 1000;
    const totalBez  = (data.entries || []).reduce((s, e) => s + (e.bezug_w || 0), 0) / 1000;
    renderNetzKpis("netz-tag-kpis", totalEins, totalBez);
  } catch (e) {
    showError("Netz Tagesverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufNetzWoche(endDate) {
  _vNetzWeekEnd = endDate;
  const today = new Date().toISOString().slice(0, 10);
  const kw = _isoWeekNum(endDate);
  const labelEl = document.getElementById("netz-woche-label");
  if (labelEl) labelEl.textContent = `KW ${kw}`;
  const nextBtn = document.getElementById("netz-woche-next");
  if (nextBtn) nextBtn.disabled = endDate >= today;
  try {
    const data = await api(`/api/history/grid/week?end_date=${endDate}`);
    renderNetzWocheChart(data.entries || []);
    const totalEins = (data.entries || []).reduce((s, e) => s + (e.einspeisung_kwh || 0), 0);
    const totalBez  = (data.entries || []).reduce((s, e) => s + (e.bezug_kwh || 0), 0);
    renderNetzKpis("netz-woche-kpis", totalEins, totalBez);
  } catch (e) {
    showError("Netz Wochenverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufNetzMonat(monthStr) {
  _vNetzMonth = monthStr;
  const thisMonth = new Date().toISOString().slice(0, 7);
  const labelEl = document.getElementById("netz-monat-label");
  if (labelEl) labelEl.textContent = _fmtMonth(monthStr);
  const nextBtn = document.getElementById("netz-monat-next");
  if (nextBtn) nextBtn.disabled = monthStr >= thisMonth;
  try {
    const data = await api(`/api/history/grid/month/${monthStr}`);
    renderNetzMonatChart(data.entries || []);
    const totalEins = (data.entries || []).reduce((s, e) => s + (e.einspeisung_kwh || 0), 0);
    const totalBez  = (data.entries || []).reduce((s, e) => s + (e.bezug_kwh || 0), 0);
    renderNetzKpis("netz-monat-kpis", totalEins, totalBez);
  } catch (e) {
    showError("Netz Monatsverlauf konnte nicht geladen werden: " + e.message);
  }
}

async function loadVerlaufNetzJahr(year) {
  _vNetzYear = year;
  const thisYear = new Date().getFullYear();
  const labelEl = document.getElementById("netz-jahr-label");
  if (labelEl) labelEl.textContent = String(year);
  const nextBtn = document.getElementById("netz-jahr-next");
  if (nextBtn) nextBtn.disabled = year >= thisYear;
  try {
    const data = await api(`/api/history/grid/year/${year}`);
    renderNetzJahrChart(data.entries || []);
    const totalEins = (data.entries || []).reduce((s, e) => s + (e.einspeisung_kwh || 0), 0);
    const totalBez  = (data.entries || []).reduce((s, e) => s + (e.bezug_kwh || 0), 0);
    renderNetzKpis("netz-jahr-kpis", totalEins, totalBez);
  } catch (e) {
    showError("Netz Jahresübersicht konnte nicht geladen werden: " + e.message);
  }
}

function renderNetzKpis(containerId, einspeisung, bezug) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  const saldo = einspeisung - bezug;
  const saldoColor = saldo >= 0 ? "#2ed8a3" : "#ff6b6b";
  const saldoStr = (saldo >= 0 ? "+" : "") + saldo.toFixed(1);
  wrap.innerHTML = `
    <div style="display:flex; gap:8px; margin-top:14px">
      <div style="flex:1; background:#1a1d23; border-radius:8px; padding:10px; text-align:center">
        <div style="font-size:18px; font-weight:500; color:#2ed8a3">${einspeisung.toFixed(1)} kWh</div>
        <div style="font-size:10px; color:#6a6d75; margin-top:2px">Einspeisung</div>
      </div>
      <div style="flex:1; background:#1a1d23; border-radius:8px; padding:10px; text-align:center">
        <div style="font-size:18px; font-weight:500; color:#e8a435">${bezug.toFixed(1)} kWh</div>
        <div style="font-size:10px; color:#6a6d75; margin-top:2px">Bezug</div>
      </div>
      <div style="flex:1; background:#1a1d23; border-radius:8px; padding:10px; text-align:center">
        <div style="font-size:18px; font-weight:500; color:${saldoColor}">${saldoStr} kWh</div>
        <div style="font-size:10px; color:#6a6d75; margin-top:2px">Saldo</div>
      </div>
    </div>`;
}

function renderNetzTagChart(entries) {
  const wrap = document.getElementById("chart-netz-tag-wrap");
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Stundendaten vorhanden.</div>';
    return;
  }
  const VW = 480, VH = 130;
  const pad = { t: 10, r: 8, b: 22, l: 52 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;

  const pts = entries.map(e => {
    const h = parseInt((e.ts || "").split(" ")[1]?.split(":")[0] || "0", 10);
    return { h, eins: e.einspeisung_w || 0, bez: e.bezug_w || 0 };
  }).sort((a, b) => a.h - b.h);

  const maxUp   = Math.max(...pts.map(p => p.eins), 100);
  const maxDown = Math.max(...pts.map(p => p.bez), 100);
  const total   = maxUp + maxDown;
  const zeroY   = pad.t + (maxUp / total) * ch;
  const barHalf = Math.max(3, cw / 24 * 0.35);

  const syUp   = v => zeroY - (v / maxUp) * (zeroY - pad.t);
  const syDown = v => zeroY + (v / maxDown) * ((pad.t + ch) - zeroY);
  const sx     = h => pad.l + (h / 23) * cw;

  let elems = `<line x1="${pad.l}" y1="${zeroY.toFixed(1)}" x2="${(pad.l+cw).toFixed(1)}" y2="${zeroY.toFixed(1)}" stroke="rgba(255,255,255,0.25)" stroke-width="1"/>`;
  elems += `<text x="${pad.l-4}" y="${zeroY.toFixed(1)}" text-anchor="end" dominant-baseline="middle" font-size="8" fill="#a0a3ab">0</text>`;

  const stepUp = niceStep(maxUp, 2);
  for (let yv = stepUp; yv <= maxUp + 1; yv += stepUp) {
    const ypx = syUp(yv).toFixed(1);
    elems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l+cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    const lbl = yv >= 1000 ? (yv/1000).toFixed(1)+"k" : yv;
    elems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle" fill="#a0a3ab">${lbl}</text>`;
  }
  const stepDown = niceStep(maxDown, 2);
  for (let yv = stepDown; yv <= maxDown + 1; yv += stepDown) {
    const ypx = syDown(yv).toFixed(1);
    elems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l+cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    const lbl = yv >= 1000 ? "-"+(yv/1000).toFixed(1)+"k" : "-"+yv;
    elems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle" fill="#a0a3ab">${lbl}</text>`;
  }
  for (let h = 0; h <= 23; h += 3) {
    const xv = sx(h).toFixed(1);
    elems += `<line x1="${xv}" y1="${pad.t}" x2="${xv}" y2="${(pad.t+ch).toFixed(1)}" class="grid-line"/>`;
    elems += `<text x="${xv}" y="${VH-2}" text-anchor="middle" fill="#a0a3ab" font-size="8">${h}h</text>`;
  }

  let bars = "";
  pts.forEach(p => {
    const cx = sx(p.h);
    if (p.eins > 0) {
      const top = syUp(p.eins);
      bars += `<rect x="${(cx-barHalf).toFixed(1)}" y="${top.toFixed(1)}" width="${(barHalf*2).toFixed(1)}" height="${(zeroY-top).toFixed(1)}" rx="1" fill="#2ed8a3"/>`;
    }
    if (p.bez > 0) {
      const bot = syDown(p.bez);
      bars += `<rect x="${(cx-barHalf).toFixed(1)}" y="${zeroY.toFixed(1)}" width="${(barHalf*2).toFixed(1)}" height="${(bot-zeroY).toFixed(1)}" rx="1" fill="rgba(232,164,53,0.75)"/>`;
    }
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${elems}</g>${bars}
  </svg>`;
}

function _renderNetzBidirChart(wrapId, entries, xLabelFn) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  if (!entries || !entries.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Netz-Daten vorhanden.</div>';
    return;
  }
  const VW = 480, VH = 130;
  const pad = { t: 10, r: 8, b: 22, l: 52 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const n = entries.length;

  const einsVals = entries.map(e => +(e.einspeisung_kwh || 0));
  const bezVals  = entries.map(e => +(e.bezug_kwh || 0));
  const maxUp    = Math.max(...einsVals, 0.1);
  const maxDown  = Math.max(...bezVals, 0.1);
  const total    = maxUp + maxDown;
  const zeroY    = pad.t + (maxUp / total) * ch;

  const syUp   = v => zeroY - (v / maxUp) * (zeroY - pad.t);
  const syDown = v => zeroY + (v / maxDown) * ((pad.t + ch) - zeroY);
  const barGap = cw / n;
  const barW   = Math.max(2, barGap * 0.65);
  const x0     = i => pad.l + i * barGap + (barGap - barW) / 2;

  let elems = `<line x1="${pad.l}" y1="${zeroY.toFixed(1)}" x2="${(pad.l+cw).toFixed(1)}" y2="${zeroY.toFixed(1)}" stroke="rgba(255,255,255,0.25)" stroke-width="1"/>`;
  elems += `<text x="${pad.l-4}" y="${zeroY.toFixed(1)}" text-anchor="end" dominant-baseline="middle" font-size="8" fill="#a0a3ab">0</text>`;

  const stepUp = niceStep(maxUp, 2);
  for (let yv = stepUp; yv <= maxUp + 0.01; yv += stepUp) {
    const ypx = syUp(yv).toFixed(1);
    elems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l+cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    elems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle" fill="#a0a3ab">${yv >= 100 ? yv.toFixed(0) : yv.toFixed(1)}</text>`;
  }
  const stepDown = niceStep(maxDown, 2);
  for (let yv = stepDown; yv <= maxDown + 0.01; yv += stepDown) {
    const ypx = syDown(yv).toFixed(1);
    elems += `<line x1="${pad.l}" y1="${ypx}" x2="${(pad.l+cw).toFixed(1)}" y2="${ypx}" class="grid-line"/>`;
    const lbl = yv >= 100 ? `-${yv.toFixed(0)}` : `-${yv.toFixed(1)}`;
    elems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle" fill="#a0a3ab">${lbl}</text>`;
  }

  let bars = "";
  entries.forEach((d, i) => {
    const x = x0(i);
    const einsV = einsVals[i], bezV = bezVals[i];
    if (einsV > 0) {
      const top = syUp(einsV);
      bars += `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${(zeroY-top).toFixed(1)}" rx="1" fill="#2ed8a3"/>`;
    }
    if (bezV > 0) {
      const bot = syDown(bezV);
      bars += `<rect x="${x.toFixed(1)}" y="${zeroY.toFixed(1)}" width="${barW.toFixed(1)}" height="${(bot-zeroY).toFixed(1)}" rx="1" fill="rgba(232,164,53,0.75)"/>`;
    }
    const lbl = xLabelFn(d, i, entries.length);
    if (lbl) bars += `<text x="${(x+barW/2).toFixed(1)}" y="${VH-3}" text-anchor="middle" fill="#a0a3ab" font-size="8">${lbl}</text>`;
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${elems}</g>${bars}
  </svg>`;
}

function renderNetzWocheChart(entries) {
  const WD = ["So","Mo","Di","Mi","Do","Fr","Sa"];
  _renderNetzBidirChart("chart-netz-woche-wrap", entries,
    d => WD[new Date(d.date + "T12:00:00").getDay()]);
}

function renderNetzMonatChart(entries) {
  _renderNetzBidirChart("chart-netz-monat-wrap", entries, (d, i, total) => {
    const day = parseInt((d.date || "").split("-")[2] || "0");
    return (i === 0 || day % 5 === 0 || i === total - 1) ? `${day}.` : "";
  });
}

function renderNetzJahrChart(entries) {
  const MONATE = ["Jan","Feb","Mär","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"];
  _renderNetzBidirChart("chart-netz-jahr-wrap", entries,
    d => MONATE[parseInt((d.month || "").split("-")[1] || "1") - 1] ?? "");
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
    const dayData = await api(`/api/history/hourly?for_date=${dateStr}`);
    renderStringsDayChart(dayData.entries || [], sunWindow(dayData));
    const alertData = await api("/api/strings/alerts?days=30");
    renderStringAlertsList(alertData.entries || []);
  } catch (e) {
    showError("Strings-Verlauf konnte nicht geladen werden: " + e.message);
  }
}

function renderStringsDayChart(entries, win = [5, 21]) {
  const [xMin, xMax] = win;
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
    h: parseInt((e.ts || "").split(" ")[1]?.split(":")[0] || "0", 10),
    s1: e.str1_w || 0,
    s2: e.str2_w || 0,
  })).filter(p => p.h >= xMin && p.h <= xMax);
  if (pts.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Keine Daten im Sonnenfenster.</div>';
    return;
  }
  const maxV = Math.max(...pts.flatMap(p => [p.s1, p.s2]), 1);
  const sx = h => pad.l + ((h - xMin) / (xMax - xMin)) * cw;
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
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  };
  const mkArea = (key, fillColor) => {
    if (!pts.length) return "";
    const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.h).toFixed(1)},${sy(p[key]).toFixed(1)}`).join(" ");
    const bottom = pad.t + ch;
    return `<path d="${d} L${sx(pts[pts.length-1].h).toFixed(1)},${bottom} L${sx(pts[0].h).toFixed(1)},${bottom} Z" fill="${fillColor}" stroke="none"/>`;
  };

  const xStep = xMax - xMin > 12 ? 3 : 2;
  const xLabels = Array.from({ length: Math.floor((xMax - xMin) / xStep) + 1 }, (_, i) => xMin + i * xStep).map(h =>
    `<text x="${sx(h).toFixed(1)}" y="${VH - 2}" text-anchor="middle">${String(h).padStart(2, "0")}</text>`
  ).join("");

  wrap.innerHTML = `<svg viewBox="0 0 ${VW} ${VH}" class="spark-svg chart-svg">
    <g class="grid-g">${yElems}</g>
    ${mkArea("s2", "rgba(245,120,42,0.15)")}
    ${mkArea("s1", "rgba(232,164,53,0.15)")}
    ${mkPath("s2", "#f5782a")}
    ${mkPath("s1", "#e8a435")}
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
      <div style="font-size:10px;color:var(--text-muted);margin-top:3px;">STR1 (Dach): ${a.str1_w ?? "–"} W · STR2 (Wand): ${a.str2_w ?? "–"} W${ratioStr}</div>
    </div>`;
  }).join("");
}

async function _checkLiveStringAlert() {
  const el = document.getElementById("string-dash-alert");
  if (!el) return;
  try {
    const data = await api("/api/strings/alerts?days=1");
    // Nur anzeigen, solange der Fehler live noch besteht (Alert-Log hält 24 h).
    const live = await api("/api/solax").catch(() => null);
    const p1 = live?.pv1_power_w, p2 = live?.pv2_power_w;
    const stillFaulty = p1 == null || p2 == null ||
      (p1 < 10 && p2 > 200) || (p2 < 10 && p1 > 200);
    const active = stillFaulty ? (data.entries || []) : [];
    if (!active.length) { el.classIfChanged = "string-dash-alert hidden"; return; }
    const top = active[0];
    el.classIfChanged = `string-dash-alert ${top.level === "error" ? "error" : "warn"}`;
    el.textIfChanged = "⚡ " + top.message;
  } catch {
    // ignore — non-critical
  }
}

// ── Wetter sub-tab ───────────────────────────────────────────────────────────

async function loadVerlaufWetter() {
  try {
    const data = await api("/api/forecast");
    _lastForecastData = data;
    const r2El = document.getElementById("weather-corr-r2");
    if (r2El) r2El.textContent = data.r2 != null ? `r² = ${data.r2.toFixed(2)}` : "–";
    renderScatterPlot(
      data.correlation || [],
      data.tomorrow,
      data.r2,
      data.slope,
      data.intercept,
    );
  } catch (e) {
    showError("Wetter-Korrelation konnte nicht geladen werden: " + e.message);
  }
}

// ── Prognose-Genauigkeit ─────────────────────────────

async function loadVerlaufPrognose() {
  try {
    const data = await api("/api/history/forecast?days=30");
    const entries = data.entries || [];
    renderPrognoseKPIs(entries);
    renderPrognoseChart(entries);
    renderPrognoseTable(entries);
  } catch (e) {
    showError("Prognose-Daten konnten nicht geladen werden: " + e.message);
  }
}

function renderPrognoseKPIs(entries) {
  const el = document.getElementById("verlauf-prognose-kpis");
  if (!el) return;

  const valid = entries.filter(e => e.hit !== null);
  const hits  = valid.filter(e => e.hit).length;
  const hitRate = valid.length > 0 ? Math.round(hits / valid.length * 100) : null;

  const withDiff = entries.filter(e => e.diff_kwh !== null);
  const avgAbsKwh = withDiff.length > 0
    ? withDiff.reduce((s, e) => s + Math.abs(e.diff_kwh), 0) / withDiff.length
    : null;
  const avgAbsPct = withDiff.length > 0
    ? withDiff.reduce((s, e) => s + Math.abs(e.diff_percent), 0) / withDiff.length
    : null;

  const meanDiff = withDiff.length > 0
    ? withDiff.reduce((s, e) => s + e.diff_kwh, 0) / withDiff.length
    : null;
  let tendenz = "–", tendenzSub = "";
  if (meanDiff !== null) {
    if (meanDiff > 1)       { tendenz = "eher konservativ"; }
    else if (meanDiff < -1) { tendenz = "eher optimistisch"; }
    else                    { tendenz = "gut kalibriert"; }
    tendenzSub = `Ø ${meanDiff >= 0 ? "+" : ""}${meanDiff.toFixed(1)} kWh`;
  }

  el.innerHTML = [
    { label: "Treffer",       val: hitRate !== null ? `${hitRate}%` : "–",              sub: hitRate !== null ? `(${hits}/${valid.length})` : "" },
    { label: "Ø Abweichung",  val: avgAbsKwh !== null ? `${avgAbsKwh.toFixed(1)} kWh` : "–", sub: avgAbsPct !== null ? `(${avgAbsPct.toFixed(0)}%)` : "" },
    { label: "Tendenz",       val: tendenz,                                               sub: tendenzSub },
  ].map(k => `<div class="verlauf-kpi">
    <div class="verlauf-kpi-label">${k.label}</div>
    <div class="verlauf-kpi-val">${k.val}</div>
    ${k.sub ? `<div class="verlauf-kpi-sub">${k.sub}</div>` : ""}
  </div>`).join("");
}

function renderPrognoseChart(entries) {
  const wrap = document.getElementById("chart-forecast-wrap");
  if (!wrap) return;

  const valid = entries.filter(e => e.forecast_kwh !== null);
  if (!valid.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Prognose-Daten vorhanden.</div>';
    return;
  }

  const VW = 480, VH = 120;
  const pad = { t: 8, r: 8, b: 28, l: 36 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const n = valid.length;
  const groupW = cw / n;
  const barW = Math.max(1.5, groupW * 0.38);
  const gap  = Math.max(0.5, groupW * 0.05);

  const maxVal = Math.max(
    ...valid.map(e => Math.max(e.forecast_kwh || 0, e.actual_kwh || 0)), 0.5
  );
  const sy = v => pad.t + ch - (v / maxVal * ch);
  const x0 = i => pad.l + i * groupW + (groupW - 2 * barW - gap) / 2;

  let yElems = "";
  const yStep = niceStep(maxVal, 3);
  for (let yv = 0; yv <= maxVal + 0.01; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l+cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l-4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(0)}</text>`;
  }

  let bars = "";
  valid.forEach((d, i) => {
    const fc  = d.forecast_kwh || 0;
    const ac  = d.actual_kwh   || 0;
    const xFc = x0(i), xAc = xFc + barW + gap;
    const bot = pad.t + ch;
    const fcH = Math.max(1, fc / maxVal * ch);
    const acH = Math.max(1, ac / maxVal * ch);
    bars += `<rect x="${xFc.toFixed(1)}" y="${(bot-fcH).toFixed(1)}" width="${barW.toFixed(1)}" height="${fcH.toFixed(1)}" rx="1" fill="rgba(232,164,53,0.3)"/>`;
    bars += `<rect x="${xAc.toFixed(1)}" y="${(bot-acH).toFixed(1)}" width="${barW.toFixed(1)}" height="${acH.toFixed(1)}" rx="1" fill="#e8a435"/>`;
    if (i === 0 || (i + 1) % 5 === 0 || i === n - 1) {
      const parts = (d.date || "").split("-");
      const lbl = `${parseInt(parts[2] || 0)}.${parseInt(parts[1] || 0)}.`;
      bars += `<text x="${(xFc + barW + gap / 2).toFixed(1)}" y="${VH-3}" text-anchor="middle">${lbl}</text>`;
    }
  });

  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
    <g>${yElems}</g>${bars}
  </svg>`;
}

function renderPrognoseTable(entries) {
  const wrap = document.getElementById("forecast-table-wrap");
  if (!wrap) return;

  const withFc = entries.filter(e => e.forecast_kwh !== null).slice().reverse();
  if (!withFc.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Prognose-Daten vorhanden.</div>';
    return;
  }

  const rows = withFc.map(e => {
    const parts = (e.date || "").split("-");
    const day   = `${parts[2]}.${parts[1]}.`;
    const sign  = e.diff_kwh >= 0 ? "+" : "";
    const color = e.diff_kwh >= 0 ? "var(--ok)" : "var(--error)";
    const icon  = e.hit === null ? "⏳" : (e.hit ? "✅" : "❌");
    const pct   = e.diff_percent !== null ? ` (${sign}${e.diff_percent}%)` : "";
    const bg    = (e.hit === false) ? ' style="background:rgba(248,113,113,0.08)"' : "";
    return `<tr${bg}>
      <td>${day}</td>
      <td>${e.forecast_kwh} kWh</td>
      <td>${e.actual_kwh} kWh</td>
      <td style="color:${color}">${sign}${e.diff_kwh} kWh</td>
      <td>${icon}<span style="color:${color}">${pct}</span></td>
    </tr>`;
  }).join("");

  wrap.innerHTML = `<table class="forecast-table">
    <thead><tr>
      <th>Datum</th><th>Prognose</th><th>Ist</th><th>Diff</th><th>Status</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderScatterPlot(corrData, forecastPt, r2, slope, intercept) {
  const wrap = document.getElementById("chart-weather-corr-wrap");
  if (!wrap) return;
  if (!corrData || !corrData.length) {
    const days = _lastForecastData?.data_days ?? 0;
    wrap.innerHTML = `<div class="chart-empty">${
      days < 7
        ? `Noch nicht genug Daten (${days}/7 Tage mit Wetter + PV-Ertrag benötigt).`
        : "Keine Korrelationsdaten verfügbar."
    }</div>`;
    return;
  }
  const VW = 460, VH = 150;
  const pad = { t: 14, r: 20, b: 28, l: 40 };
  const cw = VW - pad.l - pad.r;
  const ch = VH - pad.t - pad.b;

  const maxGhi = Math.max(...corrData.map(d => d.ghi_kwh_m2),
    forecastPt?.ghi_kwh_m2 ?? 0, 0.1);
  const maxPv = Math.max(...corrData.map(d => d.pv_kwh),
    forecastPt?.predicted_kwh ?? 0, 1);

  const sx = x => pad.l + (x / maxGhi) * cw;
  const sy = y => pad.t + ch * (1 - y / maxPv);

  let yElems = "";
  const yStep = niceStep(maxPv, 4);
  for (let yv = 0; yv <= maxPv + 0.01; yv += yStep) {
    const ypx = sy(yv).toFixed(1);
    yElems += `<line x1="${pad.l}" y1="${ypx}" x2="${pad.l + cw}" y2="${ypx}" class="grid-line"/>`;
    yElems += `<text x="${pad.l - 4}" y="${ypx}" text-anchor="end" dominant-baseline="middle">${yv.toFixed(0)}</text>`;
  }

  const xStep = niceStep(maxGhi, 5);
  let xElems = `<text x="${(pad.l + cw / 2).toFixed(0)}" y="${VH}" text-anchor="middle" font-size="8" fill="rgba(255,255,255,0.35)">GHI kWh/m²</text>`;
  for (let xv = 0; xv <= maxGhi + 0.01; xv += xStep) {
    xElems += `<text x="${sx(xv).toFixed(1)}" y="${VH - 10}" text-anchor="middle">${xv.toFixed(1)}</text>`;
  }

  let regLine = "";
  if (slope != null && intercept != null) {
    const x0 = 0, x1 = maxGhi;
    const y0 = Math.max(0, slope * x0 + intercept);
    const y1 = Math.max(0, slope * x1 + intercept);
    regLine = `<line x1="${sx(x0).toFixed(1)}" y1="${sy(y0).toFixed(1)}" x2="${sx(x1).toFixed(1)}" y2="${sy(y1).toFixed(1)}" stroke="rgba(46,216,163,0.6)" stroke-width="2" stroke-dasharray="5,3"/>`;
  }

  const dots = corrData.map(d =>
    `<circle cx="${sx(d.ghi_kwh_m2).toFixed(1)}" cy="${sy(d.pv_kwh).toFixed(1)}" r="5" fill="#e8a435" opacity="0.75"/>`
  ).join("");

  let starEl = "";
  if (forecastPt?.ghi_kwh_m2 != null && forecastPt?.predicted_kwh != null) {
    starEl = `<text x="${sx(forecastPt.ghi_kwh_m2).toFixed(1)}" y="${sy(forecastPt.predicted_kwh).toFixed(1)}" text-anchor="middle" dominant-baseline="middle" font-size="18" fill="#ff6b6b">★</text>`;
  }

  const r2label = r2 != null
    ? `<text x="${pad.l + cw - 2}" y="${pad.t + 10}" text-anchor="end" font-size="9" fill="rgba(255,255,255,0.45)" letter-spacing="0.05em">r²=${r2.toFixed(2)}</text>`
    : "";

  wrap.innerHTML = `<svg viewBox="0 0 ${VW} ${VH}" class="spark-svg chart-svg">
    <g class="grid-g">${yElems}</g>
    ${regLine}${dots}${starEl}${r2label}
    <g class="axis-g">${xElems}</g>
  </svg>`;
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
  if (heaterActive) return { label: "Wasserspeicher lädt", cls: "ok" };
  switch (tempStatus) {
    case "AT_OR_ABOVE_MAX": return { label: "Max erreicht",    cls: "warn" };
    case "HYSTERESIS_BAND": return { label: "Wasserspeicher bereit", cls: "ok" };
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

// ---------- Hausverbrauch: Tagesverlauf + Lastspitzen ----------
// Hauptzähler-Leistung (Shelly, Minutenwerte): + Bezug (hell), − Einspeisung (grün)
function meterChartSvg(pts, xMin, xMax, d, o) {
  const VW = 360, VH = o.h;
  const pad = { l: 34, r: 8, t: 10, b: 18 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const inWin = pts.filter(p => p[0] >= xMin && p[0] <= xMax);
  if (inWin.length < 2) return '<div class="chart-empty">Noch keine Messwerte.</div>';
  // Bereich schließt Einspeisung (negativ) und die Spitzen-Punkte ein, mit 6 % Rand oben und unten,
  // damit Linie und Punkte nie am Rand kleben oder abgeschnitten werden
  const hi0 = Math.max(1000, d.peak_w || 0, ...inWin.map(p => p[2]));
  const lo0 = Math.min(0, d.feed_w ? -d.feed_w : 0, ...inWin.map(p => p[3]));
  const margin = (hi0 - lo0) * 0.06;
  const hiW = hi0 + margin, loW = lo0 < 0 ? lo0 - margin : 0;
  const yStep = niceStep((hiW - loW) / 1000, 3) * 1000;
  const yTop = Math.ceil(hiW / yStep) * yStep, yBot = Math.floor(loW / yStep) * yStep;
  const sx = m => pad.l + ((m - xMin) / (xMax - xMin)) * cw;
  const sy = w => pad.t + ch - ((w - yBot) / (yTop - yBot)) * ch;
  const y0 = sy(0);
  let g = "";
  for (let v = yBot; v <= yTop + 1; v += yStep) {
    const y = sy(v).toFixed(1);
    g += `<line x1="${pad.l}" y1="${y}" x2="${pad.l + cw}" y2="${y}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${y}" text-anchor="end" dominant-baseline="middle" font-size="8">${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k</text>`;
  }
  for (const t of o.ticks) {
    const x = sx(t.m).toFixed(1);
    g += `<line x1="${x}" y1="${pad.t}" x2="${x}" y2="${pad.t + ch}" class="grid-line"/>`;
    g += `<text x="${x}" y="${VH - 4}" text-anchor="${t.anchor || "middle"}" font-size="8">${t.label}</text>`;
  }
  // Linie in Segmenten (Lücken > 5 min trennen)
  const segs = [];
  let cur = [];
  for (const p of inWin) {
    if (cur.length && p[0] - cur[cur.length - 1][0] > 5) { segs.push(cur); cur = []; }
    cur.push(p);
  }
  segs.push(cur);
  const cid = o.id;
  g += `<defs><clipPath id="${cid}-p"><rect x="${pad.l}" y="${pad.t}" width="${cw}" height="${Math.max(0, y0 - pad.t).toFixed(1)}"/></clipPath>`
     + `<clipPath id="${cid}-n"><rect x="${pad.l}" y="${y0.toFixed(1)}" width="${cw}" height="${Math.max(0, pad.t + ch - y0).toFixed(1)}"/></clipPath></defs>`;
  for (const sg of segs) {
    if (sg.length < 2) continue;
    const line = sg.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ");
    const area = `${line} L${sx(sg[sg.length - 1][0]).toFixed(1)},${y0.toFixed(1)} L${sx(sg[0][0]).toFixed(1)},${y0.toFixed(1)} Z`;
    g += `<path d="${area}" fill="rgba(224,226,230,0.1)" clip-path="url(#${cid}-p)"/>`;
    g += `<path d="${area}" fill="rgba(46,216,163,0.18)" clip-path="url(#${cid}-n)"/>`;
    g += `<path d="${line}" fill="none" stroke="#e0e2e6" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  g += `<line x1="${pad.l}" y1="${y0.toFixed(1)}" x2="${pad.l + cw}" y2="${y0.toFixed(1)}" stroke="rgba(255,255,255,0.25)" stroke-width="0.8"/>`;
  // Spitze Bezug (rot), Spitze Einspeisung (grün)
  const mark = (timeStr, w, color, txt) => {
    if (w == null || !timeStr) return "";
    const [h, mi] = timeStr.split(":").map(Number);
    const m = h * 60 + mi;
    if (m < xMin || m > xMax) return "";
    return `<circle cx="${sx(m).toFixed(1)}" cy="${sy(w).toFixed(1)}" r="3.5" fill="${color}" stroke="#14161b" stroke-width="1.5"><title>${txt}</title></circle>`;
  };
  g += mark(d.peak_time, d.peak_w, "#ff6b6b", `${fmtKw(d.peak_w)} Bezug um ${d.peak_time}`);
  g += mark(d.feed_time, d.feed_w != null ? -d.feed_w : null, "#2ed8a3", `${fmtKw(d.feed_w)} Einspeisung um ${d.feed_time}`);
  return `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

// ids: Element-IDs der Card (Verlauf→Tag oder Dashboard); pre: Präfix für clipPath-IDs
const HOUSE_IDS_DAY  = { wrap: "chart-house-day-wrap",  max: "house-day-max",  foot: "house-day-foot",  hourBlock: "house-hour-block",      hour: "chart-house-hour-wrap",      pre: "v" };
const HOUSE_IDS_DASH = { wrap: "chart-house-dash-wrap", max: "house-dash-max", foot: "house-dash-foot", hourBlock: "house-dash-hour-block", hour: "chart-house-dash-hour-wrap", pre: "d" };
function renderHouseDayChart(d, ids = HOUSE_IDS_DAY) {
  const wrap = document.getElementById(ids.wrap);
  const maxEl = document.getElementById(ids.max);
  const foot = document.getElementById(ids.foot);
  const hourBlock = document.getElementById(ids.hourBlock);
  const hourEl = document.getElementById(ids.hour);
  if (!wrap) return;
  const pts = d.points || [];
  if (maxEl) maxEl.textIfChanged = d.peak_w ? `Spitze: ${fmtKw(d.peak_w)}` : "";
  if (pts.length < 2) {
    wrap.htmlIfChanged = '<div class="chart-empty">Noch keine Messwerte (Aufzeichnung läuft seit dem letzten Neustart).</div>';
    if (foot) foot.textIfChanged = "";
    if (hourBlock) hourBlock.classList.add("hidden");
    return;
  }
  const ticks = [0, 3, 6, 9, 12, 15, 18, 21, 24].map(h => ({
    m: h * 60, label: h + "h", anchor: h === 24 ? "end" : h === 0 ? "start" : "middle",
  }));
  wrap.htmlIfChanged = meterChartSvg(pts, 0, 1440, d, { h: 150, ticks, id: ids.pre + "mmd" });

  // Letzte Stunde nur für heute
  const isToday = d.date === new Date().toLocaleDateString("sv-SE");
  if (hourBlock) hourBlock.classList.toggle("hidden", !isToday);
  if (isToday && hourEl) {
    const now = new Date();
    const nm = now.getHours() * 60 + now.getMinutes();
    const lo = Math.max(0, nm - 60);
    const lab = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
    const hd = { ...d, peak_w: null, feed_w: null };   // Spitzen der Stunde: eigener Max-Wert
    const hp = pts.filter(p => p[0] >= lo && p[0] <= nm);
    if (hp.length) {
      const top = hp.reduce((a, b) => (b[2] > a[2] ? b : a));
      if (top[2] > 0) { hd.peak_w = top[2]; hd.peak_time = lab(top[0]); }
    }
    hourEl.htmlIfChanged = meterChartSvg(pts, lo, nm, hd, {
      h: 110, id: ids.pre + "mmh",
      ticks: [{ m: lo, label: lab(lo), anchor: "start" }, { m: (lo + nm) / 2, label: lab(Math.round((lo + nm) / 2)) }, { m: nm, label: "jetzt", anchor: "end" }],
    });
  }
  if (foot) {
    foot.htmlIfChanged = `Ø <b>${fmtKw(d.avg_w)}</b>`
      + (d.peak_w ? ` · Spitze Bezug <b>${fmtKw(d.peak_w)}</b> um ${d.peak_time}` : "")
      + (d.feed_w ? `<br>Max. Einspeisung <b>${fmtKw(d.feed_w)}</b> um ${d.feed_time}` : "");
  }
}

function renderHouseMonthChart(d) {
  const wrap = document.getElementById("chart-house-month-wrap");
  const foot = document.getElementById("house-month-foot");
  if (!wrap) return;
  const days = d.days || [];
  if (!days.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Lastspitzen für diesen Monat.</div>';
    if (foot) foot.textContent = "";
    return;
  }
  const maxLoad = d.max_load_kw ?? 11;
  const [yr, mo] = d.month.split("-").map(Number);
  const nDays = new Date(yr, mo, 0).getDate();
  const VW = 360, VH = 150;
  const pad = { l: 34, r: 8, t: 10, b: 18 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const topW = Math.max(...days.map(x => x.peak_w), maxLoad * 1000);
  const yStep = niceStep(topW / 1000, 4) * 1000;
  const yMax = Math.ceil(topW / yStep) * yStep;
  const sy = w => pad.t + ch - (w / yMax) * ch;
  const slot = cw / nDays, bw = Math.max(2, slot * 0.7);
  let g = "";
  for (let v = 0; v <= yMax + 1; v += yStep) {
    const y = sy(v).toFixed(1);
    g += `<line x1="${pad.l}" y1="${y}" x2="${pad.l + cw}" y2="${y}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${y}" text-anchor="end" dominant-baseline="middle" font-size="8">${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k</text>`;
  }
  for (let day = 1; day <= nDays; day++) {
    if (day !== 1 && day % 5 !== 0) continue;
    g += `<text x="${(pad.l + slot * (day - 0.5)).toFixed(1)}" y="${VH - 4}" text-anchor="middle" font-size="8">${day}</text>`;
  }
  for (const x of days) {
    const day = +x.date.slice(8, 10);
    const color = x.peak_w >= maxLoad * 1000 ? "#ff6b6b" : x.peak_w >= 8000 ? "#e8a435" : "#6a6d75";
    const cx = pad.l + slot * (day - 0.5);
    const y = sy(x.peak_w);
    g += `<rect x="${(cx - bw / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${(pad.t + ch - y).toFixed(1)}" rx="1.5" fill="${color}"><title>${x.date.slice(8, 10)}.${x.date.slice(5, 7)}.: ${fmtKw(x.peak_w)} um ${x.peak_time}</title></rect>`;
  }
  // Warnmarke Hausanschluss
  const ly = sy(maxLoad * 1000).toFixed(1);
  g += `<line x1="${pad.l}" y1="${ly}" x2="${pad.l + cw}" y2="${ly}" stroke="#ff6b6b" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>`;
  g += `<text x="${pad.l + cw}" y="${(+ly - 3).toFixed(1)}" text-anchor="end" font-size="7.5" style="fill:#ff6b6b">${maxLoad} kW</text>`;
  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
  if (foot && d.max) {
    const m = d.max;
    foot.innerHTML = `Höchste Spitze: <b>${fmtKw(m.peak_w)}</b> am ${m.date.slice(8, 10)}.${m.date.slice(5, 7)}. um ${m.peak_time}<br>Ø Tagesspitze: <b>${fmtKw(d.avg_peak_w)}</b>`;
  }
}

// Batterie-Chart (Dashboard + Verlauf → Tag): Füllstand als Fläche (oben, 0–100 %, Mindest-SoC
// gestrichelt), darunter Laden (grün, nach oben) und Entladen (orange, nach unten) als
// 5-Minuten-Balken mit eigener Skala. raw = [[Minute, SoC, Laden W, Entladen W], …]
function zendureChartSvg(raw, opts = {}) {
  const buckets = new Map();
  for (const [m, , c, dis] of raw) {
    const b = Math.floor(m / 5) * 5;
    const e = buckets.get(b) ?? { n: 0, c: 0, d: 0 };
    e.n++; e.c += c || 0; e.d += dis || 0; buckets.set(b, e);
  }
  const bars = [...buckets].map(([m, e]) => ({ m, c: e.c / e.n, d: e.d / e.n }));
  const maxP = Math.max(500, ...bars.map(b => Math.max(b.c, b.d)));
  const VW = 360, VH = opts.height ?? 170;
  const pad = { l: 34, r: 8, t: 8, b: 18 };
  const cw = VW - pad.l - pad.r;
  const socH = Math.round((VH - pad.t - pad.b) * 0.52), gap = 10, pwrTop = pad.t + socH + gap, pwrH = VH - pad.b - pwrTop;
  const sx = m => pad.l + (m / 1440) * cw;
  const sy = v => pad.t + socH - (v / 100) * socH;
  const y0 = pwrTop + pwrH / 2;
  const sp = w => (w / maxP) * (pwrH / 2);
  const hm = m => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const kw = w => w >= 1000 ? (w / 1000).toFixed(2) + " kW" : Math.round(w) + " W";
  let g = "";
  for (const v of [0, 50, 100]) {
    g += `<line x1="${pad.l}" y1="${sy(v)}" x2="${pad.l + cw}" y2="${sy(v)}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${sy(v)}" text-anchor="end" dominant-baseline="middle" font-size="8">${v}%</text>`;
  }
  if (opts.minSoc > 0) {
    g += `<line x1="${pad.l}" y1="${sy(opts.minSoc)}" x2="${pad.l + cw}" y2="${sy(opts.minSoc)}" stroke="rgba(232,164,53,0.55)" stroke-width="0.8" stroke-dasharray="3 3"><title>Mindest-SoC ${opts.minSoc} %</title></line>`;
  }
  for (let h = 0; h <= 24; h += 4) {
    g += `<text x="${sx(h * 60)}" y="${VH - 4}" text-anchor="${h === 24 ? "end" : h === 0 ? "start" : "middle"}" font-size="8">${h}h</text>`;
  }
  const line = raw.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ");
  const last = raw[raw.length - 1];
  g += `<path d="${line} L${sx(last[0]).toFixed(1)},${sy(0)} L${sx(raw[0][0]).toFixed(1)},${sy(0)} Z" fill="rgba(167,139,250,0.15)" stroke="none"/>`;
  g += `<path d="${line}" fill="none" stroke="#a78bfa" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  g += `<circle cx="${sx(last[0]).toFixed(1)}" cy="${sy(last[1]).toFixed(1)}" r="3.5" fill="#a78bfa" stroke="#14161b" stroke-width="1.5"><title>${hm(last[0])}: ${Math.round(last[1])} %</title></circle>`;
  // Leistung: Laden nach oben (grün), Entladen nach unten (orange), eigene Skala ± maxP
  g += `<line x1="${pad.l}" y1="${y0}" x2="${pad.l + cw}" y2="${y0}" stroke="rgba(255,255,255,0.25)" stroke-width="0.8"/>`;
  g += `<text x="${pad.l - 4}" y="${pwrTop + 4}" text-anchor="end" font-size="7" style="fill:#2ed8a3">+${(maxP / 1000).toFixed(1)}k</text>`;
  g += `<text x="${pad.l - 4}" y="${pwrTop + pwrH}" text-anchor="end" font-size="7" style="fill:#e8a435">−${(maxP / 1000).toFixed(1)}k</text>`;
  const bw = Math.max(1, (cw / 288) * 1.1);
  for (const b of bars) {
    const t = `${hm(b.m)}–${hm(b.m + 5)}`;
    if (b.c > 20) g += `<rect x="${(sx(b.m)).toFixed(1)}" y="${(y0 - sp(b.c)).toFixed(1)}" width="${bw.toFixed(1)}" height="${sp(b.c).toFixed(1)}" fill="#2ed8a3" opacity="0.75"><title>${t}: lädt ${kw(b.c)}</title></rect>`;
    if (b.d > 20) g += `<rect x="${(sx(b.m)).toFixed(1)}" y="${y0}" width="${bw.toFixed(1)}" height="${sp(b.d).toFixed(1)}" fill="#e8a435" opacity="0.75"><title>${t}: entlädt ${kw(b.d)}</title></rect>`;
  }
  return `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

// ---------- Batterie: Verlauf Tag + Monat ----------
async function loadZendureDay(dateStr) {
  const card = document.getElementById("zd-day-card");
  if (!card) return;
  const on = lastZendure?.enabled === true;
  card.classList.toggle("hidden", !on);
  if (!on) return;
  const d = await api(`/api/zendure/history?date=${dateStr}`);
  const wrap = document.getElementById("chart-zd-day-wrap"), foot = document.getElementById("zd-day-foot");
  const raw = (d.points || []).filter(p => p[1] != null);
  if (raw.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Messwerte.</div>';
    foot.textContent = "";
    return;
  }
  wrap.innerHTML = zendureChartSvg(raw, { minSoc: lastZendure?.min_soc });
  const last = raw[raw.length - 1];
  const k = v => v == null ? "–" : v.toFixed(1) + " kWh";
  foot.innerHTML = `Geladen <b>${k(d.charged_kwh)}</b> · Entladen <b>${k(d.discharged_kwh)}</b> · SoC jetzt <b>${Math.round(last[1])} %</b>`;
}

async function loadZendureMonth(monthStr) {
  const card = document.getElementById("zd-month-card");
  if (!card) return;
  const on = lastZendure?.enabled === true;
  card.classList.toggle("hidden", !on);
  if (!on) return;
  const d = await api(`/api/history/zendure/month/${monthStr}`);
  const wrap = document.getElementById("chart-zd-month-wrap"), foot = document.getElementById("zd-month-foot");
  if (!d.days.length) {
    wrap.innerHTML = '<div class="chart-empty">Noch keine Daten für diesen Monat.</div>';
    foot.textContent = "";
    return;
  }
  const [yr, mo] = d.month.split("-").map(Number);
  const nDays = new Date(yr, mo, 0).getDate();
  const VW = 360, VH = 140;
  const pad = { l: 34, r: 8, t: 8, b: 18 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const top = Math.max(1, ...d.days.flatMap(x => [x.charged_kwh, x.discharged_kwh]));
  const yStep = niceStep(top, 3), yMax = Math.ceil(top / yStep) * yStep;
  const sy = v => pad.t + ch - (v / yMax) * ch;
  const slot = cw / nDays, bw = Math.max(1.5, slot * 0.38);
  let g = "";
  for (let v = 0; v <= yMax + 1e-9; v += yStep) {
    g += `<line x1="${pad.l}" y1="${sy(v)}" x2="${pad.l + cw}" y2="${sy(v)}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${sy(v)}" text-anchor="end" dominant-baseline="middle" font-size="8">${v}</text>`;
  }
  for (let day = 1; day <= nDays; day++) {
    if (day !== 1 && day % 5 !== 0) continue;
    g += `<text x="${(pad.l + slot * (day - 0.5)).toFixed(1)}" y="${VH - 4}" text-anchor="middle" font-size="8">${day}</text>`;
  }
  for (const x of d.days) {
    const cx = pad.l + slot * (+x.date.slice(8, 10) - 0.5);
    g += `<rect x="${(cx - bw).toFixed(1)}" y="${sy(x.charged_kwh).toFixed(1)}" width="${bw.toFixed(1)}" height="${(pad.t + ch - sy(x.charged_kwh)).toFixed(1)}" fill="#2ed8a3" opacity="0.8"><title>${x.date.slice(8, 10)}.${x.date.slice(5, 7)}.: geladen ${x.charged_kwh.toFixed(1)} kWh</title></rect>`;
    g += `<rect x="${cx.toFixed(1)}" y="${sy(x.discharged_kwh).toFixed(1)}" width="${bw.toFixed(1)}" height="${(pad.t + ch - sy(x.discharged_kwh)).toFixed(1)}" fill="#e8a435" opacity="0.8"><title>${x.date.slice(8, 10)}.${x.date.slice(5, 7)}.: entladen ${x.discharged_kwh.toFixed(1)} kWh</title></rect>`;
  }
  wrap.innerHTML = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
  foot.innerHTML = `Geladen <b>${d.charged_kwh.toFixed(1)} kWh</b> · Entladen <b>${d.discharged_kwh.toFixed(1)} kWh</b>`
    + (d.cycles != null ? `<br>≈ <b>${d.cycles.toFixed(1)}</b> Zyklen (Σ geladen / ${d.capacity_kwh.toFixed(1)} kWh)` : "");
}

// ---------- Batterie (Zendure SolarFlow) ----------
function renderZendureCard(z) {
  const card = document.getElementById("zendure-card");
  if (!card) return;
  const on = z?.enabled === true;
  card.classList.toggle("hidden", !on);
  if (!on) return;
  $("#zd-title").textIfChanged = z.name || "Batterie";
  const dot = $("#zd-dot");
  dot.classIfChanged = "dot " + (z.reachable ? (z.state === "standby" ? "off" : "on") : "off");

  $("#zd-soc").textIfChanged = z.soc != null ? `${Math.round(z.soc)} %` : "–";
  const soc = Math.max(0, Math.min(100, z.soc ?? 0));
  $("#zd-cover").style.width = (100 - soc) + "%";
  $("#zd-minmark").style.left = (z.min_soc ?? 0) + "%";

  const kw = w => w >= 1000 ? (w / 1000).toFixed(2) + " kW" : Math.round(w) + " W";
  const st = $("#zd-status");
  if (!z.reachable) { st.textIfChanged = "nicht erreichbar"; st.classIfChanged = "badge error"; }
  else if (z.state === "charging") { st.textIfChanged = `lädt ${kw(z.charge_w)}`; st.classIfChanged = "badge ok"; }
  else if (z.state === "discharging") { st.textIfChanged = `entlädt ${kw(z.discharge_w)}`; st.classIfChanged = "badge warn zd-orange"; }
  else { st.textIfChanged = "Standby"; st.classIfChanged = "badge"; }

  const k = v => v == null ? "–" : v.toFixed(1) + " kWh";
  $("#zd-charged").textIfChanged = k(z.charged_kwh);
  $("#zd-discharged").textIfChanged = k(z.discharged_kwh);
  $("#zd-temp").textIfChanged = z.temp_c != null ? Math.round(z.temp_c) + " °C" : "–";
  // Nutzbarer Inhalt (über Mindest-SoC) und Reichweite bei der aktuellen Leistung
  const capWh = z.capacity_wh ?? 0;
  const usable = z.soc != null && capWh ? Math.max(0, (z.soc - (z.min_soc ?? 0)) / 100 * capWh) : null;
  $("#zd-usable").textIfChanged = usable != null ? `${(usable / 1000).toFixed(1)} von ${(capWh / 1000).toFixed(1)} kWh` : "–";
  const dur = h => h >= 24 ? "> 24 h" : h >= 1 ? `${Math.floor(h)} h ${String(Math.round((h % 1) * 60)).padStart(2, "0")} min` : `${Math.max(1, Math.round(h * 60))} min`;
  let rng = "–", rngLabel = "Reichweite";
  if (z.reachable && z.state === "discharging" && z.discharge_w > 0 && usable != null) {
    rng = `${dur(usable / z.discharge_w)} bei ${kw(z.discharge_w)}`; rngLabel = "Reicht noch";
  } else if (z.reachable && z.state === "charging" && z.charge_w > 0 && z.soc != null && capWh) {
    const toFull = Math.max(0, ((z.target_soc ?? 100) - z.soc) / 100 * capWh);
    rng = `${dur(toFull / z.charge_w)} bei ${kw(z.charge_w)}`; rngLabel = "Voll in";
  }
  $("#zd-range-label").textIfChanged = rngLabel;
  $("#zd-range").textIfChanged = rng;
  renderZendureForeign(z.foreign);
  const dec = $("#zd-decision");
  dec.textIfChanged = z.control_enabled && z.decision ? z.decision : "";
  dec.classList.toggle("hidden", !(z.control_enabled && z.decision));
}

// Fremdregler (App-Energieplan/HEMS) – roter Hinweis oben im Dashboard und in der Batterie-Card
function renderZendureForeign(f) {
  const banner = $("#zd-foreign-banner");
  const box = $("#zd-foreign");
  const active = f?.active === true;
  banner?.classList.toggle("hidden", !active);
  box?.classList.toggle("hidden", !active);
  if (!active) return;
  const hm = iso => (iso || "").slice(11, 16);
  const since = hm(f.since), last = hm(f.last);
  const when = since && since !== last ? `seit ${since}, zuletzt ${last}` : `um ${last}`;
  const head = "Fremdregler aktiv – HEMS in der Zendure-App prüfen";
  if (banner) banner.textIfChanged = `${head} (${when})`;
  if (box) {
    const ms = f.device_min_soc != null ? ` · Mindest-SoC am Gerät ${f.device_min_soc} %` : "";
    box.htmlIfChanged = `<b>${head}</b><br>${escapeHtml(f.detail || "")}<br>`
      + `<span class="zd-foreign-meta">${when}${ms} · pvcontroller stellt die Batterie bei jedem Eingriff auf Standby</span>`;
  }
}

async function loadZendureChart() {
  const wrap = document.getElementById("zd-chart-wrap");
  const card = document.getElementById("zendure-card");
  if (!wrap || !card || card.classList.contains("hidden")) return;
  const d = await api("/api/zendure/history");
  const pts = (d.points || []).filter(p => p[1] != null);
  if (pts.length < 2) { wrap.htmlIfChanged = '<div class="chart-empty">Noch keine Verlaufsdaten.</div>'; return; }
  wrap.htmlIfChanged = zendureChartSvg(pts, { minSoc: lastZendure?.min_soc });
}

// ---------- Wasserspeicher: Tagesverlauf + letzte Stunde + 7 Tage ----------
let storageRange = "24h";
try { storageRange = localStorage.getItem("storageRange") || "24h"; } catch (e) { /* ohne Storage */ }
if (!["1h", "24h", "7d"].includes(storageRange)) storageRange = "24h";
let storageLast = null;

const ST = { temp: "#a78bfa", heat: "#2ed8a3", sun: "#fbbf24", max: "#f87171", resume: "#fbbf24" };
const fmtHM = ts => new Date(ts * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });

async function loadHouseDash() {
  const day = new Date().toLocaleDateString("sv-SE");
  const d = await api(`/api/history/house/day/${day}`);
  renderHouseDayChart(d, HOUSE_IDS_DASH);
}

async function loadStorage() {
  const d = await api(`/api/speicher/history?range=${storageRange}`);
  storageLast = d;
  renderStorage(d);
}

function setStorageRange(r) {
  storageRange = r;
  try { localStorage.setItem("storageRange", r); } catch (e) { /* ignorieren */ }
  loadStorage().catch(() => {});
}

// Zeitreihen-Chart für Tag (0–24 h) und letzte Stunde; range="7d" -> Tagesband.
// opts: {temps, power, events, resume, max, sun, range, xMin, xMax, now, current,
//        rate, etaTs, zoomFrom, ticks, days}
function renderStorageChart(el, opts) {
  if (!el) return;
  if (opts.range === "7d") { el.htmlIfChanged = storage7dSvg(opts); return; }
  const { temps = [], power = [], events = [], range } = opts;
  const day = range === "day";
  if (temps.length < 2) {
    el.htmlIfChanged = '<div class="chart-empty">Noch keine Verlaufsdaten.</div>';
    return;
  }
  const VW = 360, VH = day ? 190 : 112;
  const pad = { l: 30, r: 8, t: day ? 16 : 8, b: 18 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const bandH = Math.round(ch * (day ? 0.2 : 0.28));
  const tH = ch - bandH - 6;                       // Höhe des Temperaturbereichs
  const { xMin, xMax } = opts;
  const sx = t => pad.l + ((t - xMin) / (xMax - xMin)) * cw;

  // y-Skala Temperatur (Referenzlinien nur im Tageschart einbeziehen)
  const vals = temps.map(p => p[1]);
  if (day) { if (opts.resume != null) vals.push(opts.resume); if (opts.max != null) vals.push(opts.max); }
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (day) {
    const span = Math.max(hi - lo, 3);
    lo -= span * 0.08; hi += span * 0.08;
  } else {
    // Letzte Stunde: ganze Grad ± 2 → mindestens 4 °C Spanne, auch bei konstanter Temperatur
    lo = Math.floor(lo) - 2; hi = Math.ceil(hi) + 2;
  }
  const sy = v => pad.t + tH - ((v - lo) / (hi - lo)) * tH;
  const bandTop = pad.t + tH + 6, bandBot = pad.t + ch;

  let g = "";
  // Sonnenfenster
  if (day && opts.sun) {
    const a = Math.max(xMin, opts.sun.rise), b = Math.min(xMax, opts.sun.set);
    if (b > a) {
      g += `<rect x="${sx(a).toFixed(1)}" y="${pad.t}" width="${(sx(b) - sx(a)).toFixed(1)}" height="${ch}" fill="rgba(251,191,36,0.07)"/>`;
      g += `<text x="${((sx(a) + sx(b)) / 2).toFixed(1)}" y="${pad.t - 4}" text-anchor="middle" font-size="7.5" style="fill:rgba(251,191,36,0.65)">☀ ${opts.sun.rise_label} – ${opts.sun.set_label}</text>`;
    }
  }
  // y-Gitter + Labels
  const yStep = niceStep(hi - lo, 3);
  for (let v = Math.ceil(lo / yStep) * yStep; v <= hi; v += yStep) {
    const y = sy(v).toFixed(1);
    g += `<line x1="${pad.l}" y1="${y}" x2="${pad.l + cw}" y2="${y}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${y}" text-anchor="end" dominant-baseline="middle" font-size="8">${Math.round(v)}°</text>`;
  }
  // x-Ticks
  for (const t of opts.ticks) {
    const x = sx(t.ts).toFixed(1);
    g += `<line x1="${x}" y1="${pad.t}" x2="${x}" y2="${bandBot}" class="grid-line"/>`;
    g += `<text x="${x}" y="${VH - 4}" text-anchor="${t.anchor || "middle"}" font-size="8">${t.label}</text>`;
  }
  // Referenzlinien
  if (day) {
    const ref = (v, color, label, dy) => {
      if (v == null) return "";
      const y = sy(v).toFixed(1);
      return `<line x1="${pad.l}" y1="${y}" x2="${pad.l + cw}" y2="${y}" stroke="${color}" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>`
           + `<text x="${pad.l + 3}" y="${(+y + dy).toFixed(1)}" font-size="7.5" style="fill:${color}" text-anchor="start">${label}</text>`;
    };
    g += ref(opts.max, ST.max, `Max ${Math.round(opts.max)}`, -3);
    g += ref(opts.resume, ST.resume, `Resume ${Math.round(opts.resume)}`, 8);
  }
  // Heizstab-Balken (eigene Skala 0 … max kW)
  const pMax = Math.max(1000, ...power.map(p => p[1]));
  const pTop = Math.ceil(pMax / 500) * 500;
  const bw = Math.max(1, (cw / ((xMax - xMin) / (day ? 300 : 60))) * 0.8);
  g += `<line x1="${pad.l}" y1="${bandBot}" x2="${pad.l + cw}" y2="${bandBot}" class="grid-line"/>`;
  for (const [ts, w] of power) {
    if (w <= 50 || ts < xMin || ts > xMax) continue;
    const h = Math.max(1, (w / pTop) * bandH);
    g += `<rect x="${(sx(ts) - bw / 2).toFixed(1)}" y="${(bandBot - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${ST.heat}" opacity="0.55"/>`;
  }
  g += `<text x="${pad.l + 3}" y="${bandBot - 3}" font-size="7.5" style="fill:rgba(255,255,255,0.45)">Heizstab</text>`;
  g += `<text x="${pad.l + cw}" y="${bandTop + 7}" font-size="7" text-anchor="end" style="fill:rgba(255,255,255,0.35)">${(pTop / 1000).toFixed(1)} kW</text>`;

  // Temperaturkurve (Lücken > 15 min trennen die Linie)
  let line = "", area = "", prev = null, segStart = null;
  const flush = () => {
    if (segStart != null && prev != null) {
      area += ` L${sx(prev[0]).toFixed(1)},${(pad.t + tH).toFixed(1)} L${sx(segStart[0]).toFixed(1)},${(pad.t + tH).toFixed(1)} Z`;
    }
  };
  for (const p of temps) {
    if (p[0] < xMin || p[0] > xMax) continue;
    const gap = prev != null && (p[0] - prev[0]) > 900;
    if (prev == null || gap) { if (gap) flush(); line += `M${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`; area += `M${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`; segStart = p; }
    else { line += ` L${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`; area += ` L${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`; }
    prev = p;
  }
  flush();
  const gid = `stg-${range}`;
  g += `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${ST.temp}" stop-opacity="0.28"/><stop offset="100%" stop-color="${ST.temp}" stop-opacity="0"/></linearGradient></defs>`;
  g += `<path d="${area}" fill="url(#${gid})" stroke="none"/>`;
  g += `<path d="${line}" fill="none" stroke="${ST.temp}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;

  // Zoom-Fenster (letzte Stunde)
  if (day && opts.zoomFrom != null) {
    const x1 = sx(Math.max(xMin, opts.zoomFrom)), x2 = sx(Math.min(xMax, opts.now));
    g += `<rect x="${x1.toFixed(1)}" y="${pad.t}" width="${Math.max(2, x2 - x1).toFixed(1)}" height="${ch}" fill="rgba(167,139,250,0.10)" stroke="${ST.temp}" stroke-width="1" stroke-dasharray="3 2"/>`;
  }
  // Schaltereignisse: nur „an" beschriften
  const yAtTemp = e => sy(e.temp ?? temps[temps.length - 1][1]);
  for (const e of events) {
    if (e.ts < xMin || e.ts > xMax) continue;
    const x = sx(e.ts), y = yAtTemp(e);
    if (e.state === "on") {
      const lx = Math.min(Math.max(x, pad.l + 18), VW - pad.r - 18);
      // obere Hälfte des Temperaturbereichs: Label unter die Kurve, sonst darüber
      const below = y < pad.t + tH / 2;
      g += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${ST.heat}" stroke="#14161b" stroke-width="1"/>`;
      g += `<text x="${lx.toFixed(1)}" y="${(below ? y + 12 : y - 6).toFixed(1)}" text-anchor="middle" font-size="7.5" style="fill:${ST.heat}">${fmtHM(e.ts)} an</text>`;
    } else {
      g += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2" fill="#8a8d95" stroke="#14161b" stroke-width="1"/>`;
    }
  }
  // Hochrechnung bis Resume (nur bei steigender Temperatur) + Live-Punkt
  const cur = opts.current ?? temps[temps.length - 1][1];
  const nx = sx(Math.min(opts.now, xMax)), ny = sy(cur);
  // Ende der Hochrechnung: ETA, spätestens Sonnenuntergang (danach wird nicht mehr geheizt)
  const xe = Math.min(opts.etaTs ?? xMax, opts.sun?.set ?? xMax, xMax);
  if (day && opts.rate > 0 && opts.etaTs && xe > opts.now) {
    const te = cur + opts.rate * (xe - opts.now) / 3600;
    g += `<line x1="${nx.toFixed(1)}" y1="${ny.toFixed(1)}" x2="${sx(xe).toFixed(1)}" y2="${sy(te).toFixed(1)}" stroke="${ST.heat}" stroke-width="1.2" stroke-dasharray="2 2" opacity="0.8"/>`;
  }
  g += `<circle cx="${nx.toFixed(1)}" cy="${ny.toFixed(1)}" r="3.5" fill="${ST.temp}" opacity="0.6"><animate attributeName="r" values="3.5;9;3.5" dur="2s" repeatCount="indefinite"/><animate attributeName="opacity" values="0.6;0;0.6" dur="2s" repeatCount="indefinite"/></circle>`;
  g += `<circle cx="${nx.toFixed(1)}" cy="${ny.toFixed(1)}" r="3.5" fill="${ST.temp}" stroke="#14161b" stroke-width="1.5"/>`;

  el.htmlIfChanged = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

function storage7dSvg(opts) {
  const days = opts.days || [];
  const have = days.filter(d => d.tmin != null);
  if (!have.length) return '<div class="chart-empty">Noch keine Verlaufsdaten.</div>';
  const VW = 360, VH = 170;
  const pad = { l: 30, r: 8, t: 10, b: 18 };
  const cw = VW - pad.l - pad.r, ch = VH - pad.t - pad.b;
  const bandH = Math.round(ch * 0.26), tH = ch - bandH - 8;
  const vals = have.flatMap(d => [d.tmin, d.tmax]);
  if (opts.resume != null) vals.push(opts.resume);
  if (opts.max != null) vals.push(opts.max);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 3); lo -= span * 0.08; hi += span * 0.08;
  const sy = v => pad.t + tH - ((v - lo) / (hi - lo)) * tH;
  const slot = cw / days.length, bw = slot * 0.5;
  const kMax = Math.max(1, ...days.map(d => d.kwh || 0));
  const bandBot = pad.t + ch;
  const WD = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
  let g = "";
  const yStep = niceStep(hi - lo, 3);
  for (let v = Math.ceil(lo / yStep) * yStep; v <= hi; v += yStep) {
    const y = sy(v).toFixed(1);
    g += `<line x1="${pad.l}" y1="${y}" x2="${pad.l + cw}" y2="${y}" class="grid-line"/>`;
    g += `<text x="${pad.l - 4}" y="${y}" text-anchor="end" dominant-baseline="middle" font-size="8">${Math.round(v)}°</text>`;
  }
  const ref = (v, color, label, dy) => v == null ? "" :
    `<line x1="${pad.l}" y1="${sy(v).toFixed(1)}" x2="${pad.l + cw}" y2="${sy(v).toFixed(1)}" stroke="${color}" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>`
    + `<text x="${pad.l + cw - 3}" y="${(sy(v) + dy).toFixed(1)}" text-anchor="end" font-size="7.5" style="fill:${color}">${label}</text>`;
  g += ref(opts.max, ST.max, `Max ${Math.round(opts.max)}`, -3) + ref(opts.resume, ST.resume, `Resume ${Math.round(opts.resume)}`, 8);
  days.forEach((d, i) => {
    const cx = pad.l + slot * (i + 0.5);
    const dt = new Date(d.date + "T12:00:00");
    const isToday = i === days.length - 1;
    g += `<text x="${cx.toFixed(1)}" y="${VH - 4}" text-anchor="middle" font-size="8" style="fill:${isToday ? "#e0e2e6" : "#a0a3ab"}">${isToday ? "Heute" : WD[dt.getDay()]}</text>`;
    if (d.tmin != null) {
      const y1 = sy(d.tmax), y2 = sy(d.tmin);
      g += `<rect x="${(cx - bw / 2).toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(3, y2 - y1).toFixed(1)}" rx="3" fill="rgba(167,139,250,0.35)" stroke="${ST.temp}" stroke-width="1"/>`;
    }
    if (d.kwh == null) {
      g += `<text x="${cx.toFixed(1)}" y="${(bandBot - 3).toFixed(1)}" text-anchor="middle" font-size="8" style="fill:#6a6d75">–</text>`;
    } else {
      const h = Math.max(1, (d.kwh / kMax) * (bandH - 9));
      g += `<rect x="${(cx - bw / 2).toFixed(1)}" y="${(bandBot - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${ST.heat}" opacity="0.55"/>`;
      g += `<text x="${cx.toFixed(1)}" y="${(bandBot - h - 2).toFixed(1)}" text-anchor="middle" font-size="7.5" style="fill:${ST.heat}">${d.kwh.toFixed(1)}</text>`;
    }
  });
  g += `<line x1="${pad.l}" y1="${bandBot}" x2="${pad.l + cw}" y2="${bandBot}" class="grid-line"/>`;
  return `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
}

function renderStorage(d) {
  const title = $("#storage-title");
  if (title) title.textIfChanged = storageRange === "7d" ? "Wasserspeicher · 7 Tage" : storageRange === "1h" ? "Wasserspeicher · letzte Stunde" : "Wasserspeicher · heute";
  // Umschalter
  $$("#storage-range button").forEach(b => b.classList.toggle("on", b.dataset.range === storageRange));

  // Steigung (grün/grau/rot)
  const rEl = $("#m-rate");
  if (rEl) {
    const r = d.rate_c_per_h;
    if (r == null) { rEl.textIfChanged = ""; }
    else {
      const up = r > 0.3, down = r < -0.3;
      rEl.textIfChanged = `${up ? "▲" : down ? "▼" : "▬"} ${r > 0 ? "+" : ""}${r.toFixed(1)} °C/h`;
      rEl.style.color = up ? "var(--ok)" : down ? "var(--error)" : "var(--text-muted)";
    }
  }

  // Infozeile
  const info = $("#storage-info");
  if (info) {
    const mm = (d.min?.v != null)
      ? `Min <b>${d.min.v.toFixed(1)}</b> ${d.min.t}&nbsp;&nbsp;Max <b>${d.maxday.v.toFixed(1)}</b> ${d.maxday.t}` : "";
    let heat = "";
    if (storageRange === "7d") {
      const known = (d.days || []).filter(x => x.kwh != null);
      heat = known.length
        ? `Heizstab 7 T · ${known.reduce((s, x) => s + x.kwh, 0).toFixed(1)} kWh`
        : "Heizstab 7 T · –";
    } else if (d.heating?.kwh != null) {
      const n = d.heating.phases ?? 0;
      heat = `Heizstab ${n} ${n === 1 ? "Phase" : "Phasen"} · ${d.heating.kwh.toFixed(1)} kWh`;
    } else {
      heat = "Heizstab –";
    }
    info.htmlIfChanged = (storageRange === "7d" ? "" : `<div>${mm}</div>`) + `<div class="storage-heat">${heat}</div>`;
  }

  const dayWrap = $("#storage-day"), hourBlock = $("#storage-hour-block"), hourEl = $("#storage-hour");
  const nowD = new Date(d.now * 1000);
  dayWrap.classList.toggle("hidden", storageRange === "1h");
  hourBlock.classList.toggle("hidden", storageRange === "7d");

  if (storageRange === "7d") {
    renderStorageChart(dayWrap, { range: "7d", days: d.days, resume: d.resume, max: d.max });
  } else if (storageRange === "24h") {
    const ticks = [0, 4, 8, 12, 16, 20, 24].map(h => ({
      ts: d.day_start + h * 3600, label: h + "h", anchor: h === 24 ? "end" : h === 0 ? "start" : "middle",
    }));
    renderStorageChart(dayWrap, {
      range: "day", temps: d.temps, power: d.power, events: d.events,
      resume: d.resume, max: d.max, sun: d.sun, xMin: d.day_start, xMax: d.day_end,
      now: d.now, current: d.current, rate: d.rate_c_per_h, etaTs: d.eta_ts, sun: d.sun,
      zoomFrom: d.now - 3600, ticks,
    });
  }

  if (storageRange !== "7d" && d.hour) {
    const h = d.hour;
    const ticks = [
      { ts: h.from, label: fmtHM(h.from), anchor: "start" },
      { ts: (h.from + h.to) / 2, label: fmtHM((h.from + h.to) / 2) },
      { ts: h.to, label: "jetzt", anchor: "end" },
    ];
    renderStorageChart(hourEl, {
      range: "hour", temps: h.temps, power: h.power, events: h.events,
      xMin: h.from, xMax: h.to, now: h.to, current: d.current, ticks,
    });
    const eta = $("#storage-eta");
    if (eta) {
      eta.textIfChanged = d.eta_label === "erreicht" ? "Resume erreicht"
        : d.eta_label === "heute nicht" ? "ETA Resume: heute nicht"
        : d.eta_label ? `ETA Resume ≈ ${d.eta_label}` : "ETA Resume –";
    }
  }

  // Legende
  const lg = $("#storage-legend");
  if (lg) {
    lg.htmlIfChanged = storageRange === "7d"
      ? `<span><i class="lg-line"></i>Min–Max</span><span><i class="lg-box" style="background:${ST.heat};opacity:.55"></i>Heizstab kWh</span>`
      : `<span><i class="lg-line"></i>Temperatur</span><span><i class="lg-box" style="background:${ST.heat};opacity:.55"></i>Heizstab</span>`
        + (storageRange === "24h" ? `<span><i class="lg-box" style="background:rgba(251,191,36,.35)"></i>Sonne</span><span><i class="lg-box lg-dash"></i>Zoom</span>` : "");
  }
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
    wrap.htmlIfChanged = '<div class="chart-empty">Noch keine Ladeverlaufsdaten für heute.</div>';
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

  wrap.htmlIfChanged = `<svg class="temp-chart" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
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
    const off = lastPhasesDisabled[`ph${ph}`] === true;
    rows += `<rect x="${labelW}" y="${y}" width="${trackW}" height="${barH}" rx="2" fill="rgba(255,255,255,${off ? "0.03" : "0.06"})"/>`;
    rows += `<text x="${labelW - 4}" y="${(y + barH / 2).toFixed(1)}" text-anchor="end" dominant-baseline="middle">PH${ph}</text>`;
    if (off) {
      rows += `<text x="${labelW + 6}" y="${(y + barH / 2).toFixed(1)}" dominant-baseline="middle" style="fill:#6a6d75">deaktiviert</text>`;
      return;
    }
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

  wrap.htmlIfChanged = `<svg class="phase-tl-svg" viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="xMidYMid meet">
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
// Jeder Bereich rendert, sobald SEINE Daten da sind – kein Warten auf den
// langsamsten Call. Nur Status + Solax werden gemeinsam gerendert, damit
// Energiefluss, PV&Netz und PV-Erzeugung dieselbe PV-Leistung zeigen.
// Dashboard-Daten braucht nur Dashboard und Prioritäten (Kaskade). Auf anderen Tabs und bei verdeckter
// Seite (Handy gesperrt) pausiert das Polling; beim Zurückkommen sofort ein Update.
const STATUS_TABS = new Set(["dashboard", "prioritaeten"]);
let lastStatusRefresh = 0;
const _activeTab = () => document.querySelector(".tab.active")?.dataset.tab;
const _statusWanted = () => !document.hidden && STATUS_TABS.has(_activeTab());

// „aktualisiert“-Pill in der Kopfzeile: Zeit der Daten, die der aktive Tab zeigt.
// Heizung → letztes Heizungs-Update, Settings → ausgeblendet (Platz bleibt), sonst → Dashboard-Status.
const pillTimes = { status: null, heating: null, shutters: null };
function updatePillTime() {
  const el = $("#pill-time");
  if (!el) return;
  const tab = _activeTab();
  el.style.visibility = tab === "settings" ? "hidden" : "";
  const t = tab === "heizung" ? pillTimes.heating : tab === "rolllaeden" ? pillTimes.shutters : pillTimes.status;
  const val = el.querySelector(".pt-val");
  if (val) val.textIfChanged = t || "…";
}

// Gesamt-PV-Pill in der Kopfzeile (Haupt-WR + Pergola). Dashboard/Prioritäten: aus dem Dashboard-Polling
// (/api/solax); andere Tabs: /api/pv/now (Server-Cache bzw. letzter Schnappschuss, keine Geräteabfrage)
// alle 30 s, pausiert bei verdeckter Seite. Fehlt der Haupt-WR → „☀ –“ gedimmt; Pergola nachts offline
// zählt einfach nicht mit (kein Fehlerzustand).
const PV_PILL_MS = 30000;
function fmtPvShort(w) {
  if (w == null || !isFinite(w)) return "–";
  return Math.round(w) >= 1000 ? (w / 1000).toFixed(1).replace(".", ",") + " kW" : Math.max(0, Math.round(w)) + " W";
}
function setPvPill(w) {
  const el = $("#pill-pv");
  if (!el) return;
  el.textIfChanged = "☀ " + fmtPvShort(w);
  el.classList.toggle("dim", w == null || !isFinite(w) || Math.round(w) <= 0);
}
async function refreshPvPill() {
  if (document.hidden || _statusWanted()) return;
  try { setPvPill((await api("/api/pv/now")).pv_w); } catch { setPvPill(null); }
}

function _statusTick() { if (_statusWanted()) refreshStatus(); }

function _refreshIfStale() {
  if (Date.now() - lastStatusRefresh > 2000) { refreshStatus(); return true; }
  return false;
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (_statusWanted()) _refreshIfStale();
  else refreshPvPill();
  if (_activeTab() === "logs" && $("#log-auto")?.checked) refreshLogs();
});

async function refreshStatus() {
  lastStatusRefresh = Date.now();
  const activeTab = _activeTab();
  const statusP  = api("/api/status");
  const solaxP   = api("/api/solax").catch(() => null);
  const historyP = api("/api/temp-history").catch(() => ({ entries: [] }));
  const cascadeP = api("/api/cascade/status").catch(() => null);
  const hourlyP  = api("/api/history/hourly").catch(() => ({ entries: [] }));

  loadPhaseLogs();
  loadForecast();
  drawHausverbrauchSparkline();
  _checkLiveStringAlert();
  if (activeTab === "prioritaeten") loadCascade();

  cascadeP.then(cascadeStatus => {
    if (!cascadeStatus) return;
    lastCascadeStatus = cascadeStatus;
    renderCascadeMini(cascadeStatus);
    checkCascadeAlerts(cascadeStatus);
  });
  hourlyP.then(hourlyToday => renderStringChart(hourlyToday.entries || [], "pv-strings-chart-wrap", sunWindow(hourlyToday)));
  historyP.then(history => renderWbChart(history.entries));

  try {
    const [data, solax] = await Promise.all([statusP, solaxP]);
    if (solax !== null) {
      lastSolaxData = solax;
    } else {
      console.log("[refreshStatus] /api/solax failed – using cached data:", lastSolaxData ? "available" : "none");
    }
    const effectiveSolax = solax !== null ? solax : lastSolaxData;
    // Single source of truth: pv_total_w aus dem Solax-Call überschreibt pv_power
    // im Status-Result (beide Endpunkte nutzen ohnehin denselben Server-Cache).
    if (effectiveSolax != null && data?.heater != null) {
      data.heater.pv_power = effectiveSolax.pv_total_w;
    }
    renderStatus(data);
    renderSolax(effectiveSolax);
    setPvPill(effectiveSolax ? effectiveSolax.pv_total_w : null);
    const history = await historyP;
    loadStorage().catch(() => {});
    loadHouseDash().catch(() => {});
    loadZendureChart().catch(() => {});
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
    setPvPill(effectiveSolax ? effectiveSolax.pv_total_w : null);
    loadStorage().catch(() => {});
    loadHouseDash().catch(() => {});
    loadZendureChart().catch(() => {});
    api("/api/temp-history").then(h => {
      renderWbChart(h.entries);
    }).catch(() => {});
    api("/api/history/hourly").then(h => {
      renderStringChart(h.entries || [], "pv-strings-chart-wrap", sunWindow(h));
    }).catch(() => {});
    loadPhaseLogs();
    loadPeakTimeChart();
    loadBatteryNights();
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
    setFormValue(f, "solax2.enabled", lastConfig.solax2?.enabled ?? false);
    setFormValue(f, "solax2.url", lastConfig.solax2?.url ?? "");
    setFormValue(f, "solax2.pwd", lastConfig.solax2?.pwd ?? "");
    setFormValue(f, "solax2.name", lastConfig.solax2?.name ?? "Pergola");
    setFormValue(f, "shelly.ph1_url", lastConfig.shelly?.ph1_url ?? "");
    setFormValue(f, "shelly.ph1_type", lastConfig.shelly?.ph1_type ?? "shelly_gen1");
    setFormValue(f, "shelly.ph1_channel", lastConfig.shelly?.ph1_channel ?? 0);
    setFormValue(f, "shelly.ph2_url", lastConfig.shelly?.ph2_url ?? "");
    setFormValue(f, "shelly.ph2_type", lastConfig.shelly?.ph2_type ?? "shelly_gen1");
    setFormValue(f, "shelly.ph2_channel", lastConfig.shelly?.ph2_channel ?? 0);
    setFormValue(f, "shelly.ph3_url", lastConfig.shelly?.ph3_url ?? "");
    setFormValue(f, "shelly.ph3_type", lastConfig.shelly?.ph3_type ?? "shelly_gen1");
    setFormValue(f, "shelly.ph3_channel", lastConfig.shelly?.ph3_channel ?? 0);
    setFormValue(f, "shelly.storage_url", lastConfig.shelly?.storage_url ?? "");
    setFormValue(f, "shelly.main_meter_url", lastConfig.shelly?.main_meter_url ?? "");
    setFormValue(f, "shelly.main_meter_type", lastConfig.shelly?.main_meter_type ?? "shelly_3em_gen1");
    setFormValue(f, "shelly.heater_meter_url", lastConfig.shelly?.heater_meter_url ?? "");
    setFormValue(f, "heater.storage_max_temp", lastConfig.heater?.storage_max_temp);
    setFormValue(f, "heater.storage_temp_hysteresis", lastConfig.heater?.storage_temp_hysteresis);
    setFormValue(f, "heater.comfort_min_temp", lastConfig.heater?.comfort_min_temp ?? "");
    setFormValue(f, "heater.winter_comfort_min_temp", "winter_comfort_min_temp" in (lastConfig.heater || {}) ? (lastConfig.heater.winter_comfort_min_temp ?? "") : 40);
    setFormValue(f, "heater.min_pv_power_ph1", lastConfig.heater?.min_pv_power_ph1);
    setFormValue(f, "heater.min_pv_power_ph2", lastConfig.heater?.min_pv_power_ph2);
    setFormValue(f, "heater.min_pv_power_ph3", lastConfig.heater?.min_pv_power_ph3);
    setFormValue(f, "heater.grid_assist_w", lastConfig.heater?.grid_assist_w ?? 0);
    setFormValue(f, "heater.phase_min_on_s", lastConfig.heater?.phase_min_on_s ?? 420);
    setFormValue(f, "heater.phase_min_off_s", lastConfig.heater?.phase_min_off_s ?? 120);
    setFormValue(f, "heater.phase_down_hyst", lastConfig.heater?.phase_down_hyst ?? 0.15);
    setFormValue(f, "heater.budget_smooth_min", lastConfig.heater?.budget_smooth_min ?? 10);
    setFormValue(f, "heater.budget_smooth_down_min", lastConfig.heater?.budget_smooth_down_min ?? 2);
    for (const n of [1, 2, 3]) setFormValue(f, `heater.phase${n}_disabled`, lastConfig.heater?.[`phase${n}_disabled`] ?? false);
    setFormValue(f, "wallbox.enabled", lastConfig.wallbox?.enabled);
    setFormValue(f, "wallbox.url", lastConfig.wallbox?.url ?? "");
    setFormValue(f, "wallbox.only_control_when_pv_surplus_active", lastConfig.wallbox?.only_control_when_pv_surplus_active);
    setFormValue(f, "wallbox.pause_below_storage_temp", lastConfig.wallbox?.pause_below_storage_temp);
    setFormValue(f, "wallbox.release_above_storage_temp", lastConfig.wallbox?.release_above_storage_temp);
    setFormValue(f, "wallbox.fail_safe", lastConfig.wallbox?.fail_safe ?? "no_change");
    setFormValue(f, "wallbox.eco_phases", lastConfig.wallbox?.eco_phases ?? 1);
    setFormValue(f, "wallbox.eco_amps", lastConfig.wallbox?.eco_amps ?? 16);
    setFormValue(f, "wallbox.basic_phases", lastConfig.wallbox?.basic_phases ?? 2);
    setFormValue(f, "wallbox.basic_amps", lastConfig.wallbox?.basic_amps ?? 10);
    setFormValue(f, "wallbox.auto_reset_on_unplug", lastConfig.wallbox?.auto_reset_on_unplug ?? true);
    setFormValue(f, "auth.enabled", lastConfig.auth_enabled ?? false);
    setFormValue(f, "auth.user", lastConfig.auth_user ?? "admin");
    // Gesetztes Passwort kommt maskiert ("********"); unverändert zurückgeschickt bleibt es erhalten
    setFormValue(f, "auth.password", lastConfig.auth_password ?? "");
    setFormValue(f, "house.max_load_kw", lastConfig.house_max_load_kw ?? 11);
    const zc = lastConfig.zendure ?? {};
    setFormValue(f, "zendure.enabled", zc.enabled ?? false);
    setFormValue(f, "zendure.ip", zc.ip ?? "");
    setFormValue(f, "zendure.sn", zc.sn ?? "");
    setFormValue(f, "zendure.name", zc.name ?? "Batterie");
    setFormValue(f, "zendure.capacity_wh", zc.capacity_wh ?? 2400);
    setFormValue(f, "zendure.max_charge_w", zc.max_charge_w ?? 2400);
    setFormValue(f, "zendure.max_discharge_w", zc.max_discharge_w ?? 2400);
    setFormValue(f, "zendure.min_soc", zc.min_soc ?? 10);
    setFormValue(f, "zendure.target_soc", zc.target_soc ?? 100);
    setFormValue(f, "zendure.control_enabled", zc.control_enabled ?? false);
    setFormValue(f, "location.latitude",  lastConfig.latitude  ?? "");
    setFormValue(f, "location.longitude", lastConfig.longitude ?? "");
    setFormValue(f, "location.name",      lastConfig.location_name ?? "");
    setFormValue(f, "summer_mode.enabled",    lastConfig.summer_mode?.enabled    ?? false);
    setFormValue(f, "summer_mode.min_temp",   lastConfig.summer_mode?.min_temp   ?? 45);
    setFormValue(f, "summer_mode.target_temp",lastConfig.summer_mode?.target_temp ?? 52);
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
    solax2: {
      enabled: boolField("solax2.enabled"),
      url: strField("solax2.url"),
      pwd: strField("solax2.pwd"),
      name: strField("solax2.name"),
    },
    shelly: {
      ph1_url: strField("shelly.ph1_url"),
      ph1_type: f.elements.namedItem("shelly.ph1_type")?.value,
      ph1_channel: numField("shelly.ph1_channel"),
      ph2_url: strField("shelly.ph2_url"),
      ph2_type: f.elements.namedItem("shelly.ph2_type")?.value,
      ph2_channel: numField("shelly.ph2_channel"),
      ph3_url: strField("shelly.ph3_url"),
      ph3_type: f.elements.namedItem("shelly.ph3_type")?.value,
      ph3_channel: numField("shelly.ph3_channel"),
      storage_url: strField("shelly.storage_url"),
      main_meter_url: strField("shelly.main_meter_url"),
      main_meter_type: f.elements.namedItem("shelly.main_meter_type")?.value,
      heater_meter_url: strField("shelly.heater_meter_url"),
    },
    heater: {
      storage_max_temp: numField("heater.storage_max_temp"),
      storage_temp_hysteresis: numField("heater.storage_temp_hysteresis"),
      winter_comfort_min_temp: (() => {                // leer = aus
        const v = f.elements.namedItem("heater.winter_comfort_min_temp")?.value;
        return v === "" || v == null ? null : Number(v);
      })(),
      comfort_min_temp: (() => {                       // leer = Default (Zieltemperatur Sommermodus)
        const v = f.elements.namedItem("heater.comfort_min_temp")?.value;
        return v === "" || v == null ? null : Number(v);
      })(),
      min_pv_power_ph1: numField("heater.min_pv_power_ph1"),
      min_pv_power_ph2: numField("heater.min_pv_power_ph2"),
      min_pv_power_ph3: numField("heater.min_pv_power_ph3"),
      grid_assist_w: numField("heater.grid_assist_w"),
      phase_min_on_s: numField("heater.phase_min_on_s"),
      phase_min_off_s: numField("heater.phase_min_off_s"),
      phase_down_hyst: numField("heater.phase_down_hyst"),
      budget_smooth_min: numField("heater.budget_smooth_min"),
      budget_smooth_down_min: numField("heater.budget_smooth_down_min"),
      phase1_disabled: boolField("heater.phase1_disabled"),
      phase2_disabled: boolField("heater.phase2_disabled"),
      phase3_disabled: boolField("heater.phase3_disabled"),
    },
    wallbox: {
      enabled: boolField("wallbox.enabled"),
      url: strField("wallbox.url"),
      only_control_when_pv_surplus_active: boolField("wallbox.only_control_when_pv_surplus_active"),
      pause_below_storage_temp: numField("wallbox.pause_below_storage_temp"),
      release_above_storage_temp: numField("wallbox.release_above_storage_temp"),
      fail_safe: f.elements.namedItem("wallbox.fail_safe")?.value,
      eco_phases: numField("wallbox.eco_phases"),
      eco_amps: numField("wallbox.eco_amps"),
      basic_phases: numField("wallbox.basic_phases"),
      basic_amps: numField("wallbox.basic_amps"),
      auto_reset_on_unplug: boolField("wallbox.auto_reset_on_unplug"),
    },
    auth: {
      enabled: boolField("auth.enabled"),
      user: strField("auth.user"),
      password: f.elements.namedItem("auth.password")?.value ?? "",
    },
    house: {
      max_load_kw: numField("house.max_load_kw"),
    },
    zendure: {
      enabled: boolField("zendure.enabled"),
      ip: f.elements.namedItem("zendure.ip")?.value?.trim(),
      sn: f.elements.namedItem("zendure.sn")?.value?.trim(),
      name: strField("zendure.name"),
      capacity_wh: numField("zendure.capacity_wh"),
      max_charge_w: numField("zendure.max_charge_w"),
      max_discharge_w: numField("zendure.max_discharge_w"),
      min_soc: numField("zendure.min_soc"),
      target_soc: numField("zendure.target_soc"),
      control_enabled: boolField("zendure.control_enabled"),
    },
    location: {
      latitude:  numField("location.latitude"),
      longitude: numField("location.longitude"),
      name:      strField("location.name"),
    },
    summer_mode: {
      enabled:     boolField("summer_mode.enabled"),
      min_temp:    numField("summer_mode.min_temp"),
      target_temp: numField("summer_mode.target_temp"),
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
    const ms = res.zendure_min_soc;
    if (ms) {
      if (ms.dry_run) msg.textContent += " · Mindest-Ladestand: Dry-Run, nicht ans Gerät geschrieben";
      else if (ms.ok) msg.textContent += ` · Mindest-Ladestand am Gerät bestätigt (${ms.read / 10} %)`;
      else { msg.textContent += ` · Mindest-Ladestand NICHT am Gerät: ${ms.error}`; msg.className = "form-msg error"; }
    }
    await refreshStatus();
  } catch (e) {
    msg.textContent = "Fehler: " + e.message;
    msg.className = "form-msg error";
  }
}

// ---------- logs ----------
// Angezeigte Log-Zeilen; neue Zeilen werden angehängt. Ans Ende gesprungen wird nur, wenn man schon unten war.
let _logShown = null;   // {limit, lines}

function _newLogLines(old, cur) {
  // Position der zuletzt angezeigten Zeile im neuen Ausschnitt suchen (mit bis zu 3 Zeilen Kontext)
  if (!old || !old.length) return null;
  const ctx = Math.min(3, old.length);
  for (let k = cur.length - 1; k >= ctx - 1; k--) {
    let ok = true;
    for (let j = 0; j < ctx; j++) if (cur[k - j] !== old[old.length - 1 - j]) { ok = false; break; }
    if (ok) return cur.slice(k + 1);
  }
  return null;
}

async function refreshLogs() {
  const lines = parseInt($("#log-lines").value, 10) || 200;
  try {
    const res = await api(`/api/logs?lines=${lines}`);
    const out = $("#log-output");
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
    if (res.note && (!res.lines || res.lines.length === 0)) {
      out.textContent = res.note;
      _logShown = null;
      out.scrollTop = out.scrollHeight;
      return;
    }
    const add = _logShown && _logShown.limit === lines ? _newLogLines(_logShown.lines, res.lines) : null;
    const shown = add === null ? res.lines : _logShown.lines.concat(add);
    if (add === null || shown.length > lines * 2) {
      out.textContent = res.lines.join("\n");               // erstes Laden, anderer Ausschnitt oder zu lang
      _logShown = { limit: lines, lines: res.lines.slice() };
      if (add === null || atBottom) out.scrollTop = out.scrollHeight;
      return;
    }
    if (add.length) out.append((out.textContent ? "\n" : "") + add.join("\n"));
    _logShown.lines = shown;
    if (atBottom && add.length) out.scrollTop = out.scrollHeight;
  } catch (e) {
    $("#log-output").textContent = "Konnte Logs nicht laden: " + e.message;
    _logShown = null;
  }
}
function setupLogAuto() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if ($("#log-auto").checked) logTimer = setInterval(() => {
    if (!document.hidden && _activeTab() === "logs") refreshLogs();
  }, 5_000);
}

// ---------- tabs ----------
function activateTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  updatePillTime();
  if (!STATUS_TABS.has(name)) refreshPvPill();
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "verlauf")      loadVerlauf();
  if (name === "settings")     { loadSettings(); loadCascadeSettings(); loadHeatingSettings(); loadShutterSettings(); }
  if (name === "heizung")      heatActivate();
  if (name === "rolllaeden")   shutActivate();
  if (name === "logs")         refreshLogs();
  // Dashboard/Prioritäten: nach Pause sofort aktualisieren (refreshStatus lädt auf Prioritäten die Kaskade mit)
  if (STATUS_TABS.has(name) && !_refreshIfStale() && name === "prioritaeten") loadCascade();
}

// ---------- Wallbox Modus ----------
async function setWallboxMode(mode) {
  const btnBasic = $("#btn-wb-basic");
  const btnEco   = $("#btn-wb-eco");
  [btnBasic, btnEco].forEach(b => { if (b) b.disabled = true; });
  try {
    await api(`/api/wallbox/set-mode?mode=${mode}`, { method: "POST" });
  } catch (e) {
    alert("Fehler beim Umschalten: " + (e.message ?? e));
  } finally {
    [btnBasic, btnEco].forEach(b => { if (b) b.disabled = false; });
  }
}

// Verbindungstest Pergola-Wechselrichter mit den Formularwerten (ungespeichert)
async function testSolax2() {
  const f = $("#settings-form");
  const msg = $("#solax2-test-msg");
  const btn = $("#solax2-test-btn");
  const url = f.elements.namedItem("solax2.url")?.value?.trim();
  const pwd = f.elements.namedItem("solax2.pwd")?.value?.trim();
  if (!url || !pwd) { msg.textContent = "URL und Passwort eintragen."; msg.className = "form-msg error"; return; }
  btn.disabled = true;
  msg.textContent = "teste …"; msg.className = "form-msg";
  try {
    const r = await api("/api/solax2/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, pwd }) });
    msg.textContent = `OK – aktuell ${Math.round(r.power_w)} W, heute ${r.yield_today_kwh.toFixed(1)} kWh`;
    msg.className = "form-msg ok";
  } catch (e) {
    msg.textContent = "Fehler: " + e.message;
    msg.className = "form-msg error";
  } finally {
    btn.disabled = false;
  }
}

// Verbindungstest Hauptzähler mit den Formularwerten (ungespeichert): Gerät, Leistung, Zähler, Typ-Prüfung
async function testMainMeter() {
  const f = $("#settings-form");
  const msg = $("#main-meter-test-msg");
  const btn = $("#main-meter-test-btn");
  const url = f.elements.namedItem("shelly.main_meter_url")?.value?.trim();
  const type = f.elements.namedItem("shelly.main_meter_type")?.value;
  if (!url) { msg.textContent = "URL eintragen."; msg.className = "form-msg error"; return; }
  btn.disabled = true;
  msg.textContent = "teste …"; msg.className = "form-msg";
  try {
    const r = await api("/api/main-meter/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, type }) });
    const w = Math.round(r.power_w);
    const dir = w > 0 ? "Bezug" : w < 0 ? "Einspeisung" : "±0";
    const kwh = r.import_kwh == null ? "Zähler nicht lesbar" : `Zähler Bezug ${r.import_kwh} kWh, Einspeisung ${r.export_kwh} kWh`;
    msg.textContent = `OK – ${r.model} (Gen${r.generation}, MAC ${r.mac}): ${w} W (${dir}); ${kwh}`;
    msg.className = "form-msg ok";
  } catch (e) {
    msg.textContent = "Fehler: " + e.message;
    msg.className = "form-msg error";
  } finally {
    btn.disabled = false;
  }
}

// Verbindungstest Zendure mit den Formularwerten (ungespeichert)
async function testZendure() {
  const f = $("#settings-form");
  const msg = $("#zendure-test-msg");
  const btn = $("#zendure-test-btn");
  const ip = f.elements.namedItem("zendure.ip")?.value?.trim();
  if (!ip) { msg.textContent = "IP eintragen."; msg.className = "form-msg error"; return; }
  btn.disabled = true;
  msg.textContent = "teste …"; msg.className = "form-msg";
  try {
    const r = await api("/api/zendure/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, sn: f.elements.namedItem("zendure.sn")?.value?.trim() ?? "" }),
    });
    const ac = r.state === "charging" ? `lädt ${Math.round(r.charge_w)} W` : r.state === "discharging" ? `entlädt ${Math.round(r.discharge_w)} W` : "Standby";
    msg.textContent = `OK – SoC ${r.soc ?? "?"} %, ${ac}${r.temp_c != null ? ", " + r.temp_c + " °C" : ""} · SN ${r.sn ?? "?"} · ${r.product ?? "?"} v${r.version ?? "?"} · packState=${r.pack_state ?? "?"}`;
    msg.className = "form-msg ok";
  } catch (e) {
    msg.textContent = "Fehler: " + e.message;
    msg.className = "form-msg error";
  } finally {
    btn.disabled = false;
  }
}

// ---------- boot ----------
function boot() {
  $$(".tab").forEach(t => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  initFlowTaps();
  $("#btn-run").addEventListener("click", runOnce);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#solax2-test-btn")?.addEventListener("click", testSolax2);
  $("#main-meter-test-btn")?.addEventListener("click", testMainMeter);
  $("#zendure-test-btn")?.addEventListener("click", testZendure);
  $$("#storage-range button").forEach(b => b.addEventListener("click", () => setStorageRange(b.dataset.range)));
  $("#btn-logs-refresh").addEventListener("click", refreshLogs);
  $("#log-lines").addEventListener("change", refreshLogs);
  $("#log-auto").addEventListener("change", setupLogAuto);

  // Cascade modal
  $("#cascade-edit-form")?.addEventListener("submit", _submitCascadeEdit);
  _bindCascadeProfileTabs();
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
  $$(".verlauf-nav-btn[data-vsec]").forEach(b => b.addEventListener("click", () => showVerlaufSection(b.dataset.vsec)));

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

  // Netz sub-navigation
  $$(".netz-nav-btn").forEach(b => b.addEventListener("click", () => showNetzSection(b.dataset.nsec)));

  // Netz Tag navigation
  document.getElementById("netz-tag-prev")?.addEventListener("click", () => {
    loadVerlaufNetzTag(_dateAddDays(_vNetzTagDate, -1));
  });
  document.getElementById("netz-tag-next")?.addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    if (_vNetzTagDate < today) loadVerlaufNetzTag(_dateAddDays(_vNetzTagDate, 1));
  });

  // Netz Woche navigation
  document.getElementById("netz-woche-prev")?.addEventListener("click", () => {
    loadVerlaufNetzWoche(_dateAddDays(_vNetzWeekEnd, -7));
  });
  document.getElementById("netz-woche-next")?.addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    const next = _dateAddDays(_vNetzWeekEnd, 7);
    if (next <= today) loadVerlaufNetzWoche(next);
    else loadVerlaufNetzWoche(today);
  });

  // Netz Monat navigation
  document.getElementById("netz-monat-prev")?.addEventListener("click", () => {
    loadVerlaufNetzMonat(_monthAdd(_vNetzMonth, -1));
  });
  document.getElementById("netz-monat-next")?.addEventListener("click", () => {
    const thisMonth = new Date().toISOString().slice(0, 7);
    if (_vNetzMonth < thisMonth) loadVerlaufNetzMonat(_monthAdd(_vNetzMonth, 1));
  });

  // Netz Jahr navigation
  document.getElementById("netz-jahr-prev")?.addEventListener("click", () => {
    loadVerlaufNetzJahr(_vNetzYear - 1);
  });
  document.getElementById("netz-jahr-next")?.addEventListener("click", () => {
    if (_vNetzYear < new Date().getFullYear()) loadVerlaufNetzJahr(_vNetzYear + 1);
  });

  refreshStatus();
  refreshTimer = setInterval(_statusTick, REFRESH_MS);
  setInterval(refreshPvPill, PV_PILL_MS);
  $("#pill-pv")?.addEventListener("click", () => { activateTab("dashboard"); window.scrollTo({ top: 0 }); });
  loadPeakTimeChart();
  loadBatteryNights();
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
let cascadeProfileSel  = null;  // angezeigter Tab: "summer" | "winter" (null = aktives Profil)

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
    budget:         "Budget",
    charge:         "lädt",
    discharge:      "entlädt",
    idle:           "Standby",
    hold:           "Pause",
    blocked:        "Entladen gesperrt",
    unreachable:    "nicht erreichbar",
    override:       "Laden erzwungen",
  };
  return map[action] || "–";
}

function _cascadeIcon(type) {
  return { heizstab: "⚡", wallbox: "🚗", zendure: "🔋", shelly_gen1: "🔌", shelly_gen2: "🔌" }[type] || "🔌";
}

// ── load ─────────────────────────────────────────────

async function loadCascade() {
  try {
    const q = cascadeProfileSel ? `?profile=${cascadeProfileSel}` : "";
    const [statusData, devData] = await Promise.all([
      api("/api/cascade/status" + q),
      api("/api/cascade/devices"),
    ]);
    const devMap = Object.fromEntries((devData.devices || []).map(d => [d.id, d]));
    cascadeDevicesData = (statusData.devices || []).map(sd => ({
      ...(devMap[sd.device_id || sd.id] || {}),
      ...sd,
    }));
    cascadeStatusData = statusData;
    if (!cascadeProfileSel) cascadeProfileSel = statusData.profile;
    renderCascadeProfileTabs(statusData);
    renderCascadeTab(cascadeDevicesData, statusData.surplus_watts);
    if (statusData.profile === statusData.active_profile) renderCascadeMini(statusData);
  } catch (err) {
    const el = document.getElementById("cascade-device-list");
    if (el) el.innerHTML = `<div class="card"><p class="note" style="color:var(--error)">Fehler: ${escapeHtml(err.message)}</p></div>`;
  }
}

// ── Profil-Tabs (Sommer / Winter) ────────────────────

function renderCascadeProfileTabs(status) {
  const active = status?.active_profile;
  document.querySelectorAll("#cascade-profile-tabs .cascade-profile-tab").forEach(btn => {
    const p = btn.dataset.profile;
    btn.classList.toggle("selected", p === cascadeProfileSel);
    btn.classList.toggle("is-active-profile", p === active);
    const sel = p === cascadeProfileSel ? "true" : "false";
    if (btn.getAttribute("aria-selected") !== sel) btn.setAttribute("aria-selected", sel);
    btn.textIfChanged = (p === "summer" ? "☀ Sommer" : "❄ Winter") + (p === active ? " · aktiv" : "");
  });
  const hint = document.getElementById("cascade-profile-hint");
  if (hint) {
    const sel = cascadeProfileSel;
    hint.textIfChanged = sel === active
      ? (sel === "summer"
          ? `Sommerprofil ist aktiv (Öl aus): Wasserspeicher bis ${status.comfort_min_temp} °C → Batterie → Wasserspeicher bis ${status.storage_max_temp} °C.`
          : (status.comfort_min_temp != null
              ? `Winterprofil ist aktiv (Öl heizt): Wasserspeicher bis ${status.comfort_min_temp} °C → Batterie → Wasserspeicher bis Max.`
              : `Winterprofil ist aktiv (Öl heizt): Batterie → Wasserspeicher bis Max.`))
      : `Dieses Profil ist gerade nicht aktiv – es gilt, sobald der Sommermodus ${sel === "summer" ? "eingeschaltet" : "ausgeschaltet"} wird.`;
  }
}

function _bindCascadeProfileTabs() {
  document.querySelectorAll("#cascade-profile-tabs .cascade-profile-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      cascadeProfileSel = btn.dataset.profile;
      loadCascade();
    });
  });
}

// ── render tab ───────────────────────────────────────

function renderCascadeTab(devices, surplusW) {
  const listEl = document.getElementById("cascade-device-list");
  if (!listEl) return;

  const assignedW = devices.filter(d => d.is_on && d.enabled).reduce((s, d) => s + d.power_watts, 0);
  const fillPct   = surplusW > 0 ? Math.min(100, assignedW / surplusW * 100) : 0;

  const el = id => document.getElementById(id);
  if (el("cs-surplus"))  el("cs-surplus").textIfChanged  = surplusW != null ? (surplusW / 1000).toFixed(2) + " kW" : "–";
  if (el("cs-assigned")) el("cs-assigned").textIfChanged = (assignedW / 1000).toFixed(2) + " kW";
  if (el("cs-fill"))     el("cs-fill").style.width     = fillPct.toFixed(1) + "%";

  const cardsHtml = devices.map((d, i) => {
    const devId       = d.device_id || d.id;           // Heizstab-Stufen gehören zum Gerät "heizstab"
    const isShelly    = d.type.startsWith("shelly");
    const isProtected = devId === "heizstab" || devId === "wallbox" || devId === "zendure";
    const statusText  = d.skipped
      ? (d.stage === "heizstab_min"
          ? `übersprungen – Speicher ≥ ${cascadeStatusData?.comfort_min_temp ?? ""} °C`
          : "übersprungen – Speicher voll")
      : d.manual_override_action
      ? (d.type === "zendure"
          ? (d.manual_override_action === "on" ? "Laden erzwungen" : "Entladen gesperrt")
          : "Manuell " + (d.manual_override_action === "on" ? "EIN" : "AUS"))
      : (!d.is_on && d.last_reason ? d.last_reason : _cascadeActionText(d.last_action));
    const overrideDot = d.manual_override_action
      ? `<span class="cascade-override-dot" title="Manuelle Übersteuerung">●</span>` : "";

    const warnIcon = isShelly && d.last_status_ok === false
      ? `<span class="cascade-warn-icon" title="Gerät nicht erreichbar">⚠️</span>` : "";

    let pwrText = `${d.power_watts} W`;
    if (d.last_status_power_w != null) {
      pwrText = `${Math.round(d.last_status_power_w)} W <span class="cascade-power-actual">(von ${d.power_watts} W)</span>`;
    }

    return `<div class="cascade-card${d.enabled ? "" : " disabled-device"}${d.skipped ? " skipped-stage" : ""}"
        data-id="${escapeHtml(d.id)}"
        data-device-id="${escapeHtml(devId)}"
        data-is-shelly="${isShelly}"
        data-is-zendure="${d.type === "zendure"}"
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
            data-device-id="${escapeHtml(devId)}" ${d.enabled ? "checked" : ""}>
          <span class="toggle-track"></span>
        </label>
        <button class="cascade-menu-btn" title="Aktionen"
          data-device-id="${escapeHtml(devId)}">⋮</button>
      </div>
    </div>`;
  });

  // Nicht während Drag & Drop oder offenem Aktionsmenü neu aufbauen. Sonst: gleiche Geräte in gleicher
  // Reihenfolge → nur geänderte Cards ersetzen (z. B. Live-Leistung); andernfalls die Liste neu aufbauen.
  if (_cDrag || _activeMenu) return;
  const shown = Array.from(listEl.children);
  const same = listEl._html !== undefined && listEl._cards && listEl._cards.length === cardsHtml.length
    && shown.length === cardsHtml.length && shown.every((c, i) => c === listEl._cardEls[i]);
  if (!same) {
    listEl.innerHTML = cardsHtml.join("");
    listEl._html = listEl.innerHTML;
    listEl._cards = cardsHtml;
    listEl._cardEls = Array.from(listEl.children);
    _attachCardHandlers(listEl);
    return;
  }
  cardsHtml.forEach((h, i) => {
    if (listEl._cards[i] === h) return;
    const t = document.createElement("template");
    t.innerHTML = h;
    const card = t.content.firstElementChild;
    shown[i].replaceWith(card);
    _attachCardHandlers(card);
    listEl._cards[i] = h;
    listEl._cardEls[i] = card;
  });
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
  const id        = card.dataset.deviceId || card.dataset.id;
  const isShelly  = card.dataset.isShelly === "true";
  const isProt    = card.dataset.isProtected === "true";
  const isZendure = card.dataset.isZendure === "true";
  const device    = cascadeDevicesData.find(d => (d.device_id || d.id) === id) || {};
  const enabled   = device.enabled ?? true;

  const menu = document.createElement("div");
  menu.className = "cascade-menu-dropdown";
  menu.innerHTML = `
    <button class="cascade-menu-item" data-action="edit">Bearbeiten</button>
    <button class="cascade-menu-item" data-action="toggle">${enabled ? "Deaktivieren" : "Aktivieren"}</button>
    ${isShelly ? `
    <button class="cascade-menu-item" data-action="on">Manuell einschalten (30 Min)</button>
    <button class="cascade-menu-item" data-action="off">Manuell ausschalten (30 Min)</button>` : ""}
    ${isZendure ? `
    <button class="cascade-menu-item" data-action="on">Laden erzwingen (30 Min)</button>
    <button class="cascade-menu-item" data-action="off">Entladen sperren (30 Min)</button>` : ""}
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
  const cards = Array.from(document.querySelectorAll(`.cascade-card[data-device-id="${id}"]`));
  const card = cards[0];
  invalidateHTML(document.getElementById("cascade-device-list"));   // Schalter von Hand umgestellt
  cards.forEach(c => {
    c.classList.toggle("disabled-device", !enabled);
    const k = c.querySelector(".device-enabled-toggle");
    if (k) k.checked = enabled;
  });
  try {
    await api(`/api/cascade/devices/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    cascadeDevicesData.filter(x => (x.device_id || x.id) === id).forEach(x => { x.enabled = enabled; });
  } catch (err) {
    cards.forEach(c => {
      c.classList.toggle("disabled-device", enabled);
      const k = c.querySelector(".device-enabled-toggle");
      if (k) k.checked = !enabled;
    });
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
  const d = cascadeDevicesData.find(x => (x.device_id || x.id) === id);
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
  invalidateHTML(list);         // Reihenfolge von Hand geändert → nächstes Laden zeichnet neu
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
      body: JSON.stringify({ deviceIds, profile: cascadeProfileSel }),
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
  invalidateHTML(list);
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

  const profEl = document.getElementById("cascade-mini-profile");
  if (profEl) profEl.textIfChanged = status.profile === "summer" ? "· Sommerprofil" : status.profile === "winter" ? "· Winterprofil" : "";

  const surplusEl = document.getElementById("cascade-mini-surplus");
  if (surplusEl) {
    surplusEl.htmlIfChanged = `Überschuss: <b>${surplusW > 0 ? (surplusW / 1000).toFixed(1) + " kW" : "–"}</b>`;
  }

  const devEl = document.getElementById("cascade-mini-devices");
  if (devEl) {
    const barScale = devices.length ? Math.max(...devices.map(d => d.power_watts)) || 1 : 1;
    devEl.htmlIfChanged = devices.map(d => {
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
    footerEl.htmlIfChanged = `
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
  strip.htmlIfChanged = alerts.map(a =>
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
  budget:         { label: "Budget",         cls: "cascade-hist-other" },
  charge:         { label: "lädt",           cls: "cascade-hist-on" },
  discharge:      { label: "entlädt",        cls: "cascade-hist-on" },
  idle:           { label: "Standby",        cls: "cascade-hist-other" },
  hold:           { label: "Pause",          cls: "cascade-hist-other" },
  blocked:        { label: "Entladen gesperrt", cls: "cascade-hist-off" },
  unreachable:    { label: "nicht erreichbar",  cls: "cascade-hist-off" },
  override:       { label: "Laden erzwungen",   cls: "cascade-hist-on" },
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

async function getDeviceLocation(event) {
  const btn = event.target;
  btn.textContent = '📍 Ermittle Standort...';
  btn.disabled = true;

  // Versuch 1: Browser Geolocation (funktioniert nur über HTTPS/localhost)
  if (navigator.geolocation && location.protocol === 'https:') {
    try {
      const pos = await new Promise((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          enableHighAccuracy: true, timeout: 5000
        });
      });
      setLocation(pos.coords.latitude, pos.coords.longitude, btn);
      return;
    } catch(e) {
      // Fallback
    }
  }

  // Versuch 2: IP-Geolocation (funktioniert immer)
  try {
    const response = await fetch('http://ip-api.com/json/?fields=lat,lon,city');
    const data = await response.json();
    if (data.lat && data.lon) {
      setLocation(data.lat, data.lon, btn, data.city);
      return;
    }
  } catch(e) {
    // Nächster Fallback
  }

  // Versuch 3: ipapi.co als zweiter Fallback
  try {
    const response = await fetch('https://ipapi.co/json/');
    const data = await response.json();
    if (data.latitude && data.longitude) {
      setLocation(data.latitude, data.longitude, btn, data.city);
      return;
    }
  } catch(e) {
    // Alle Fallbacks fehlgeschlagen
  }

  btn.textContent = '📍 Standort vom Gerät übernehmen';
  btn.disabled = false;
  alert('Standort konnte nicht ermittelt werden. Bitte manuell eingeben oder Google Maps nutzen.');
}

function setLocation(lat, lon, btn, city) {
  document.querySelector('[name="location.latitude"]').value = lat.toFixed(4);
  document.querySelector('[name="location.longitude"]').value = lon.toFixed(4);

  if (city) {
    const ortField = document.querySelector('[name="location.name"]');
    if (ortField && !ortField.value) {
      ortField.value = city;
    }
  }

  btn.textContent = '📍 Standort übernommen ✓';
  setTimeout(() => {
    btn.textContent = '📍 Standort vom Gerät übernehmen';
    btn.disabled = false;
  }, 2000);
}

let locationSearchTimeout = null;
let locationSearchResults = [];

function searchLocation(query) {
  clearTimeout(locationSearchTimeout);
  const dropdown = document.getElementById('location-suggestions');

  if (query.length < 3) {
    dropdown.style.display = 'none';
    return;
  }

  locationSearchTimeout = setTimeout(async () => {
    try {
      const response = await fetch(
        `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=5&addressdetails=1`,
        { headers: { 'Accept-Language': 'de', 'User-Agent': 'PV-Controller/1.0' } }
      );
      locationSearchResults = await response.json();

      if (locationSearchResults.length === 0) {
        dropdown.style.display = 'none';
        return;
      }

      dropdown.innerHTML = locationSearchResults.map((r, i) => {
        const shortName = r.display_name.split(',').slice(0, 3).join(',');
        return `<div onclick="selectLocation(${i})"
          style="padding:10px 14px;cursor:pointer;font-size:13px;color:#e0e2e6;border-bottom:1px solid #3a3d45"
          onmouseover="this.style.background='#3a3d45'"
          onmouseout="this.style.background='transparent'"
        >${shortName}</div>`;
      }).join('');

      dropdown.style.display = 'block';
    } catch(e) {
      dropdown.style.display = 'none';
    }
  }, 300);
}

function selectLocation(index) {
  const r = locationSearchResults[index];
  const addr = r.address || {};
  const ortName = addr.city || addr.town || addr.village || addr.municipality
                || addr.county || r.display_name.split(',')[0];
  document.querySelector('[name="location.latitude"]').value = parseFloat(r.lat).toFixed(4);
  document.querySelector('[name="location.longitude"]').value = parseFloat(r.lon).toFixed(4);
  document.querySelector('[name="location.name"]').value = ortName;
  document.getElementById('location-suggestions').style.display = 'none';
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('[name="location.name"]') && !e.target.closest('#location-suggestions')) {
    const dropdown = document.getElementById('location-suggestions');
    if (dropdown) dropdown.style.display = 'none';
  }
});
