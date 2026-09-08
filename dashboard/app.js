const WINDOWS = [["1m",60],["5m",300],["15m",900],["1h",3600],["all",null]];
let meta = [], metaByKey = {}, metaVersion = -1;
let series = {};              // key -> [[ts, value], ...] (history + live, detail mode)
let latest = {};             // key -> latest value
let stat = {};               // key -> {min, max, ts} running, since page load
let winSec = 300, runId = null, liveRun = null, dirty = false, hoverTs = null, lastTs = 0;
let MODE = localStorage.getItem("f10mode") || "drive";

const el = id => document.getElementById(id);
const fmt = (v, d) => v === undefined || v === null ? "--" :
  (typeof v === "number" ? v.toFixed(d) : String(v));
function clockLabel(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
}
function ageLabel(ts, now) {
  if (!ts) return "--";
  const s = Math.max(0, now - ts);
  return s < 2 ? "live" : s < 90 ? Math.round(s) + "s" : Math.round(s/60) + "m";
}

/* which channels lead the Drive view, best-effort by key (present ones win) */
const DRIVE_PRIMARY = ["rpm","boost","speed"];
const DRIVE_SECONDARY = [
  "gear","n47d_gbx_oil_temp","n47d_turbine_speed",
  "n47d_rail_act","rail","n47d_boost_act","map","load","throttle","pedal","n47d_pedal",
  "coolant","n47d_coolant","oil","n47d_oil_temp","n47d_engine_temp",
  "n47d_maf_per_cyl","maf","n47d_charge_air_temp","iat","voltage",
  "n47d_dpf_dp","n47d_exh_temp_pre_dpf","n47d_soot_meas","n47d_ambient_press"
];
function present(keys) {
  const seen = new Set(); const out = [];
  for (const k of keys) if (metaByKey[k] && !seen.has(k)) { seen.add(k); out.push(k); }
  return out;
}

/* ---------------------------------------------------------- gauges */
function polar(cx, cy, r, deg) { const a=(deg-90)*Math.PI/180; return [cx+r*Math.cos(a), cy+r*Math.sin(a)]; }
function arcPath(cx, cy, r, a0, a1) {
  const [x0,y0]=polar(cx,cy,r,a0),[x1,y1]=polar(cx,cy,r,a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${(a1-a0)>180?1:0} 1 ${x1} ${y1}`;
}
const A0=215, A1=505;
function gaugeSVG(frac, ticks, color, r, sw) {
  frac = Math.max(0, Math.min(1, frac));
  const cx=100, cy=98;
  let s = `<path d="${arcPath(cx,cy,r,A0,A1)}" stroke="var(--line)" stroke-width="${sw}" fill="none" stroke-linecap="round"/>`;
  if (frac > 0.001)
    s += `<path d="${arcPath(cx,cy,r,A0,A0+(A1-A0)*frac)}" stroke="${color}" stroke-width="${sw}" fill="none" stroke-linecap="round"/>`;
  for (const t of ticks) {
    const a=A0+(A1-A0)*t.f;
    const [x0,y0]=polar(cx,cy,r-12,a),[x1,y1]=polar(cx,cy,r-18,a);
    s += `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="#3c485c" stroke-width="2"/>`;
    const [lx,ly]=polar(cx,cy,r-30,a);
    s += `<text x="${lx}" y="${ly}" fill="var(--muted)" font-size="9.5" text-anchor="middle" dominant-baseline="middle">${t.t}</text>`;
  }
  return s;
}
function drawGauge(svg, frac, ticks, color) { svg.innerHTML = gaugeSVG(frac, ticks, color, 74, 11); }
const T = (lo, hi, n) => Array.from({length:n+1}, (_,i) => ({f:i/n, t:Math.round(lo+(hi-lo)*i/n)}));

/* alarms (shared) */
function statusOf(key, v) {
  if (v === undefined || v === null) return null;
  if (key==="coolant"||key==="n47d_coolant") return v>115?"bad":v>108?"warn":null;
  if (key==="oil"||key==="n47d_oil_temp"||key==="n47d_engine_temp") return v>125?"bad":v>115?"warn":null;
  if (key==="voltage") return v<11.8?"bad":v<12.2?"warn":null;
  if (key==="cattemp") return v>700?"warn":null;
  return null;
}

/* ---------------------------------------------------------- mode 1: drive */
/* which channels fill the secondary tile row, best available first */
const DRIVE_TILES = ["boost","n47d_boost_act","n47d_rail_act","coolant","n47d_coolant",
  "oil","n47d_oil_temp","n47d_gbx_oil_temp","n47d_turbine_speed","load","n47d_maf_per_cyl",
  "maf","n47d_dpf_dp","voltage","distance"];
const RPM_MAX = 5200, RPM_REDLINE = 4600, REV_LEDS = 22;

function buildDrive() {
  // rev-bar LEDs
  const rb = el("revbar"); rb.innerHTML = "";
  for (let i=0;i<REV_LEDS;i++){ const d=document.createElement("div"); d.className="led"; rb.appendChild(d); }
  // secondary tiles from whatever channels exist
  const tiles = present(DRIVE_TILES).slice(0, 6);
  const tr = el("tilerow"); tr.innerHTML = "";
  for (const k of tiles) {
    const m = metaByKey[k];
    const d = document.createElement("div");
    d.className = "mtile"; d.id = "tile-"+k;
    d.innerHTML = `<div class="tname">${m.label}</div>` +
      `<div><span class="tval" id="tv-${k}">--</span><span class="tunit">${m.unit}</span></div>`;
    tr.appendChild(d);
  }
}

function heroGauge(svgId, valId, key, unit, digits, gmax, color) {
  const m = metaByKey[key];
  const v = latest[key];
  const svg = el(svgId); if (!svg) return;
  const hi = gmax || (m ? m.hi : 100);
  const frac = (v||0) / Math.max(hi, 1e-6);
  svg.innerHTML = gaugeSVG(frac, T(0, hi, 5), color, 82, 14);
  el(valId).textContent = (v===undefined||v===null) ? "--" : (unit==="k" ? (v/1000).toFixed(1) : Math.round(v));
}

function renderDrive() {
  // rev bar: green -> the last few red (shift light)
  const rpm = latest.rpm || 0;
  const lit = Math.round(REV_LEDS * Math.min(1, rpm / RPM_MAX));
  const leds = el("revbar").children;
  for (let i=0;i<leds.length;i++){
    const on = i < lit;
    const red = i >= REV_LEDS * (RPM_REDLINE/RPM_MAX);
    const mid = i >= REV_LEDS * 0.6;
    leds[i].style.background = !on ? "#171d28"
      : red ? "var(--m-red)" : mid ? "var(--warn)" : "var(--m-lblue)";
    leds[i].style.boxShadow = on ? "0 0 6px "+(red?"var(--m-red)":mid?"var(--warn)":"var(--m-lblue)") : "none";
  }
  // hero gauges: rpm (x1000) + speed
  heroGauge("bg-rpm","hv-rpm","rpm","k",1, RPM_MAX,
            rpm>RPM_REDLINE ? "var(--m-red)" : "var(--m-lblue)");
  el("hv-rpm").textContent = rpm ? (rpm/1000).toFixed(1) : "--";
  heroGauge("bg-speed","hv-speed","speed","",0, 250, "var(--accent)");
  // big gear
  const g = latest.gear;
  const gn = el("gearnum");
  gn.textContent = (g===undefined||g===null) ? "N" : String(Math.round(g));
  gn.classList.toggle("rev", rpm>RPM_REDLINE);
  // tiles
  for (const k of present(DRIVE_TILES)) {
    const tv = el("tv-"+k); if (!tv) continue;
    tv.textContent = fmt(latest[k], metaByKey[k].digits);
    const st = statusOf(k, latest[k]);
    const tile = el("tile-"+k);
    if (tile) tile.className = "mtile" + (st ? " "+st : "");
  }
}
function sparkline(cvId, key) {
  const cv = el(cvId); if (!cv) return;
  const data = (series[key] || []).slice(-120);
  const dpr = window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  if (cv.width !== Math.round(w*dpr)) { cv.width=w*dpr; cv.height=h*dpr; }
  const x = cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0); x.clearRect(0,0,w,h);
  if (data.length < 2) return;
  let lo=Infinity, hi=-Infinity;
  for (const p of data) { lo=Math.min(lo,p[1]); hi=Math.max(hi,p[1]); }
  if (hi-lo<1e-9) { hi+=.5; lo-=.5; }
  const px = i => (i/(data.length-1))*w, py = v => h-2-(h-4)*(v-lo)/(hi-lo);
  x.beginPath(); data.forEach((p,i)=> i?x.lineTo(px(i),py(p[1])):x.moveTo(px(i),py(p[1])));
  x.strokeStyle="var(--series)"; x.lineWidth=1.5; x.lineJoin="round"; x.stroke();
}

/* ---------------------------------------------------------- mode 2: detail panels */
function buildPanels() {
  const box = el("panels"); box.innerHTML = "";
  if (!meta.length) { box.innerHTML='<div class="empty">waiting for channel list...</div>'; return; }
  for (const m of meta) {
    const d = document.createElement("div"); d.className="panel";
    d.innerHTML =
      `<div class="phead"><span class="pname">${m.label}</span>` +
      `<span id="badge-${m.key}"></span>` +
      `<span class="pval" id="pv-${m.key}">--</span><span class="punit">${m.unit}</span></div>` +
      `<canvas id="pc-${m.key}"></canvas>` +
      `<div class="prange"><span id="plo-${m.key}"></span><span id="phi-${m.key}"></span></div>`;
    box.appendChild(d);
    const cv = d.querySelector("canvas");
    cv.addEventListener("mousemove", e => {
      const r = cv.getBoundingClientRect(); const [t0,t1]=windowBounds();
      hoverTs = t0 + (t1-t0)*((e.clientX-r.left)/r.width); dirty=true;
    });
    cv.addEventListener("mouseleave", () => { hoverTs=null; dirty=true; });
  }
}
function windowBounds() {
  let t1=0, t0=Infinity;
  for (const k in series) { const s=series[k]; if (!s.length) continue;
    t1=Math.max(t1,s[s.length-1][0]); t0=Math.min(t0,s[0][0]); }
  if (!isFinite(t0)||!t1) return [0,0];
  if (winSec) t0=Math.max(t0, t1-winSec);
  return [t0,t1];
}
function drawPanel(m, t0, t1) {
  const cv = el("pc-"+m.key); if (!cv) return;
  const data = (series[m.key]||[]).filter(p => p[0]>=t0 && p[0]<=t1);
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=cv.clientHeight;
  if (cv.width!==Math.round(w*dpr)) { cv.width=w*dpr; cv.height=h*dpr; }
  const x=cv.getContext("2d"); x.setTransform(dpr,0,0,dpr,0,0); x.clearRect(0,0,w,h);
  if (data.length < 2) { el("plo-"+m.key).textContent=""; el("phi-"+m.key).textContent="no data in window"; return; }
  let lo=Infinity, hi=-Infinity;
  for (const p of data) { lo=Math.min(lo,p[1]); hi=Math.max(hi,p[1]); }
  if (hi-lo<1e-9) { hi+=.5; lo-=.5; }
  const pad=(hi-lo)*.12; lo-=pad; hi+=pad;
  x.strokeStyle="var(--grid)"; x.lineWidth=1;
  for (let i=0;i<=2;i++){ const y=3+(h-8)*i/2; x.beginPath(); x.moveTo(0,y); x.lineTo(w,y); x.stroke(); }
  const px=t=>((t-t0)/Math.max(t1-t0,1e-6))*w, py=v=>3+(h-8)*(1-(v-lo)/(hi-lo));
  x.beginPath(); data.forEach((p,i)=> i?x.lineTo(px(p[0]),py(p[1])):x.moveTo(px(p[0]),py(p[1])));
  x.strokeStyle="var(--series)"; x.lineWidth=2; x.lineJoin="round"; x.stroke();
  let shown = data[data.length-1][1];
  if (hoverTs!==null && hoverTs>=t0 && hoverTs<=t1) {
    let best=null, bd=Infinity;
    for (const p of data){ const d=Math.abs(p[0]-hoverTs); if (d<bd){bd=d;best=p;} }
    if (best) { shown=best[1]; const hx=px(best[0]);
      x.strokeStyle="#4a5670"; x.lineWidth=1; x.beginPath(); x.moveTo(hx,0); x.lineTo(hx,h); x.stroke();
      x.beginPath(); x.arc(hx,py(best[1]),3,0,Math.PI*2); x.fillStyle="var(--series)"; x.fill();
      x.strokeStyle="var(--surface)"; x.lineWidth=2; x.stroke(); }
  }
  el("pv-"+m.key).textContent = fmt(shown, m.digits);
  el("plo-"+m.key).textContent = fmt(lo+pad, m.digits);
  el("phi-"+m.key).textContent = fmt(hi-pad, m.digits);
  const st = statusOf(m.key, shown), b = el("badge-"+m.key);
  b.className = st ? "badge "+st : ""; b.textContent = st==="bad"?"critical":st==="warn"?"high":"";
}
function drawDetail() {
  const [t0,t1]=windowBounds();
  for (const m of meta) drawPanel(m, t0, t1);
  el("ax0").textContent = clockLabel(t0);
  el("ax1").textContent = clockLabel(t1) + (hoverTs?"   (hover: "+clockLabel(hoverTs)+")":"");
  const secs = Math.round(t1-t0);
  el("span").textContent = t1 ? `- ${secs}s window, ${meta.length} channels` : "";
}

/* ---------------------------------------------------------- mode 3: table */
function renderTable() {
  const now = latest.__ts || 0;
  const tb = el("tbody");
  const rows = meta.map(m => {
    const v = latest[m.key], s = stat[m.key] || {};
    const age = ageLabel(s.ts, now), stale = s.ts && (now - s.ts) > 5;
    const prox = m.key.startsWith("n47d_");
    return `<tr class="${stale?'stale':''}">` +
      `<td>${m.label}</td>` +
      `<td class="dim">${m.key}${prox?' <span class="src">DDE</span>':''}</td>` +
      `<td class="num">${fmt(v, m.digits)}</td>` +
      `<td class="dim">${m.unit}</td>` +
      `<td class="num dim">${s.min!==undefined?fmt(s.min,m.digits):'--'}</td>` +
      `<td class="num dim">${s.max!==undefined?fmt(s.max,m.digits):'--'}</td>` +
      `<td class="num dim">${age}</td></tr>`;
  });
  tb.innerHTML = rows.join("") || '<tr><td colspan="7" class="dim">waiting for data...</td></tr>';
}

/* ---------------------------------------------------------- head + gauges (detail) */
function renderHead(s) {
  el("status").textContent = s.status || "-";
  el("dot").className = "dot " + (s.connected ? "on" : "off");
  el("vin").textContent = s.vin || "-";
  el("ecu").textContent = s.ecu || "-";
  el("hz").textContent = (s.hz||0).toFixed(1);
  el("lat").textContent = (s.latency_ms||0).toFixed(0);
  el("rows").textContent = (s.rows||0).toLocaleString();
  const v = s.values || {};
  drawGauge(el("g-rpm"), (v.rpm||0)/5000, T(0,5,5), v.rpm>4400?"var(--bad)":"var(--series)");
  el("v-rpm").textContent = v.rpm===undefined?"--":Math.round(v.rpm);
  drawGauge(el("g-boost"), ((v.boost||0)+0.2)/2.4,
    [{f:0,t:"-.2"},{f:.29,t:".5"},{f:.5,t:"1"},{f:.71,t:"1.5"},{f:1,t:"2.2"}],
    v.boost>1.9?"var(--warn)":"var(--good)");
  el("v-boost").textContent = v.boost===undefined?"--":v.boost.toFixed(2);
  drawGauge(el("g-speed"), (v.speed||0)/250, T(0,250,5), "var(--accent)");
  el("v-speed").textContent = v.speed===undefined?"--":Math.round(v.speed);
}

/* ---------------------------------------------------------- data plumbing */
async function loadMeta() {
  const j = await (await fetch(API+"/api/meta")).json();
  meta = j.meta; metaVersion = j.meta_version;
  metaByKey = {}; for (const m of meta) metaByKey[m.key] = m;
  buildDrive(); buildPanels();
}
async function loadRuns() {
  const runs = await (await fetch(API+"/api/runs")).json();
  if (runs.error) return;
  liveRun = runs.length ? runs[0].id : null;
  const sel = el("run"); sel.innerHTML = "";
  for (const r of runs) {
    const o = document.createElement("option"); o.value = r.id;
    const when = new Date(r.started*1000).toLocaleString();
    o.textContent = `#${r.id}  ${when}  ${r.samples.toLocaleString()} pts` + (r.ended?"":"  (live)");
    sel.appendChild(o);
  }
  if (runId === null) runId = liveRun;
  sel.value = runId;
}
async function loadHistory() {
  el("hint").textContent = "loading history...";
  const q = new URLSearchParams({points: 900});
  if (runId!==null) q.set("run", runId);
  if (winSec) q.set("seconds", winSec);
  const j = await (await fetch(API+"/api/history?"+q)).json();
  series = j.series || {};
  for (const m of meta) if (!series[m.key]) series[m.key] = [];
  el("hint").textContent = runId===liveRun ? "live run - appending in real time"
                                           : "historical run - live updates paused";
  dirty = true;
}
function appendLive(ts, values) {
  latest = Object.assign({}, values); latest.__ts = ts;
  for (const k in values) {
    const v = values[k];
    if (typeof v !== "number") continue;
    const s = stat[k] || (stat[k] = {min:v, max:v, ts:ts});
    s.min = Math.min(s.min, v); s.max = Math.max(s.max, v); s.ts = ts;
  }
  if (runId === liveRun) {
    for (const k in values) (series[k] = series[k] || []).push([ts, values[k]]);
    const cutoff = ts - (winSec ? winSec*1.2 : 7200);
    for (const k in series) { const s=series[k]; let i=0; while(i<s.length && s[i][0]<cutoff) i++; if (i) s.splice(0,i); }
  }
  dirty = true;
}

/* ---------------------------------------------------------- mode switching + render loop */
function setMode(mode) {
  MODE = mode; localStorage.setItem("f10mode", mode);
  for (const b of el("modeswitch").children) b.className = b.dataset.mode===mode ? "on" : "";
  el("drive").classList.toggle("hidden", mode!=="drive");
  el("detail").classList.toggle("hidden", mode!=="detail");
  el("table").classList.toggle("hidden", mode!=="table");
  el("histctl").classList.toggle("hidden", mode!=="detail");
  dirty = true;
}
for (const b of el("modeswitch").children) b.onclick = () => setMode(b.dataset.mode);

const seg = el("win");
WINDOWS.forEach(([label, secs]) => {
  const b = document.createElement("button"); b.className="ctl"; b.textContent=label;
  if (secs===winSec) b.classList.add("on");
  b.onclick = () => { winSec=secs; [...seg.children].forEach(c=>c.className="ctl"); b.classList.add("on"); loadHistory(); };
  seg.appendChild(b);
});
el("run").onchange = e => { runId = parseInt(e.target.value,10); loadHistory(); };
el("reload").onclick = () => { loadRuns(); loadHistory(); };

/* ---------------------------------------------------------- sync agent */
/* The sync agent (infra/sync/agent.py) runs on this machine and exposes
   a CORS-enabled control endpoint. The dashboard polls it so sync can be
   watched and paused during a drive. If the agent is not running the
   chip just shows "off". This talks to a separate process; live.py's
   recording path is untouched. */
/* Same-origin: live.py proxies the agent's status at /api/sync, so this
   works both on the Pi and through the server's reverse proxy. Pause and
   resume still go straight to the agent, which only works when the page is
   opened on the Pi itself - deliberately, so a public vhost cannot pause
   syncing. */
/* Served under /s/ for a share link, at / for the owner. Every same-origin
   call is built off API so one page serves both. */
const API = window.__F10_API__ || "";
const SHARED = !!window.__F10_SHARE__;
const SYNC_BASE = `http://${location.hostname || "localhost"}:8091`;
let syncEnabled = null;
async function pollSync() {
  try {
    const s = await (await fetch(API+"/api/sync", {cache: "no-store"})).json();
    syncEnabled = (s.state === "unreachable") ? null : s.enabled;
    el("syncdot").className = "dot " + (s.enabled && s.state !== "offline" ? "on" : "off");
    el("syncstate").textContent = s.state || "-";
    let pend = 0;
    for (const k in (s.databases || {})) pend += (s.databases[k].pending || 0);
    el("syncpend").textContent = pend > 0 ? `  ${pend.toLocaleString()} pending` : "";
    el("syncchip").title = (s.last_error ? "error: " + s.last_error + " — " : "") +
      "click to " + (s.enabled ? "pause" : "resume") + " sync";
  } catch (e) {
    el("syncdot").className = "dot off";
    el("syncstate").textContent = "off";
    el("syncpend").textContent = "";
    syncEnabled = null;
  }
}
el("syncchip").onclick = async () => {
  if (syncEnabled === null) return;                 // agent not reachable
  try {
    await fetch(SYNC_BASE + (syncEnabled ? "/sync/pause" : "/sync/resume"),
                {method: "POST"});
    pollSync();
  } catch (e) {}
};
setInterval(pollSync, 3000);
pollSync();

/* ------------------------------------------------------- drive mode */
/* How hard to poll the car. The picker POSTs a request; the poll loop
   applies it between cycles, so the chip shows the REQUESTED mode in
   amber until a snapshot comes back confirming it took effect. Without
   that the control would look instant and lie whenever the link is down. */
let modeWanted = null;
async function loadModes() {
  try {
    const m = await (await fetch(API+"/api/modes", {cache: "no-store"})).json();
    const sel = el("drivemode");
    sel.innerHTML = "";
    for (const mode of m.modes || []) {
      const o = document.createElement("option");
      o.value = mode.name;
      o.textContent = mode.name;
      o.title = mode.description || "";
      sel.appendChild(o);
    }
    sel.value = m.current;
    el("drivemodechip").title =
      (m.modes || []).map(x => `${x.name} — ${x.description}`).join("\n");
  } catch (e) {}
}
el("drivemode").onchange = async ev => {
  const want = ev.target.value;
  modeWanted = want;
  el("drivemodechip").classList.add("pending");
  try {
    const r = await fetch(API+"/api/mode", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode: want}),
    });
    if (!r.ok) throw new Error(await r.text());
  } catch (e) {
    /* Put the picker back where it was: nothing changed on the car. */
    modeWanted = null;
    el("drivemodechip").classList.remove("pending");
    loadModes();
  }
};
function renderMode(s) {
  if (SHARED || !s.mode) return;
  const sel = el("drivemode");
  if (modeWanted !== null && s.mode === modeWanted) modeWanted = null;
  if (modeWanted === null) {
    el("drivemodechip").classList.remove("pending");
    if (document.activeElement !== sel) sel.value = s.mode;
  }
  const asleep = s.duty === "asleep";
  el("drivemodechip").classList.toggle("asleep", asleep);
  el("driveduty").textContent = asleep ? " · asleep" : "";
}
if (!SHARED) loadModes();

const es = new EventSource(API+"/api/stream");
es.onmessage = async e => {
  const s = JSON.parse(e.data);
  if (s.meta_version !== metaVersion) { await loadMeta(); if (MODE==="detail") await loadHistory(); }
  renderHead(s);
  renderMode(s);
  if (s.connected && s.ts !== lastTs) { lastTs = s.ts; appendLive(s.ts, s.values||{}); }
};
es.onerror = () => { el("dot").className="dot off"; el("status").textContent="server unreachable"; };

function render() {
  if (!dirty) return; dirty = false;
  if (MODE === "drive") renderDrive();
  else if (MODE === "detail") drawDetail();
  else renderTable();
}
function tick() { render(); requestAnimationFrame(tick); }
requestAnimationFrame(tick);
window.addEventListener("resize", () => { dirty = true; });

/* ---------------------------------------------------------------- sharing */
const shareSheet = el("sharesheet");
function fmtLeft(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
  return h ? `${h}h ${m}m` : (m ? `${m}m` : `${sec}s`);
}
async function loadShares() {
  const box = el("sharelist");
  let j;
  try { j = await (await fetch(API+"/api/share", {cache:"no-store"})).json(); }
  catch (e) { box.innerHTML = '<div class="sharerow dim">could not reach the server</div>'; return; }
  const links = j.links || [];
  if (!links.length) { box.innerHTML = '<div class="sharerow dim">no active links</div>'; return; }
  const now = Date.now()/1000;
  box.innerHTML = links.map(l =>
    `<div class="sharerow"><code>${l.url}</code>` +
    `<span class="dim">${fmtLeft(l.expires-now)} left &middot; ${l.hits} hit${l.hits===1?"":"s"}</span>` +
    `<button data-revoke="${l.token}">Revoke</button></div>`).join("");
  box.querySelectorAll("[data-revoke]").forEach(b => {
    b.onclick = async () => {
      await fetch(API+"/api/share/revoke", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({token: b.getAttribute("data-revoke")})});
      loadShares();
    };
  });
}
if (!SHARED) {
  el("sharechip").onclick = () => { shareSheet.classList.add("open"); loadShares(); };
  el("shareclose").onclick = () => shareSheet.classList.remove("open");
  shareSheet.onclick = e => { if (e.target === shareSheet) shareSheet.classList.remove("open"); };
  el("sharemint").onclick = async () => {
    const ttl = parseInt(el("sharettl").value, 10);
    const r = await fetch(API+"/api/share", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({ttl})});
    const j = await r.json();
    if (j.url) { el("shareurl").value = j.url; el("sharenew").style.display = "flex"; }
    loadShares();
  };
  el("sharecopy").onclick = async () => {
    const input = el("shareurl");
    input.select();
    try { await navigator.clipboard.writeText(input.value); }
    catch (e) { document.execCommand("copy"); }
    el("sharecopy").textContent = "Copied";
    setTimeout(() => { el("sharecopy").textContent = "Copy"; }, 1500);
  };
  el("sharerevokeall").onclick = async () => {
    await fetch(API+"/api/share/revoke", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({all:true})});
    el("sharenew").style.display = "none";
    loadShares();
  };
}

/* A share viewer gets the live views only: the owner-only chips go away,
   and Detail is removed because its history endpoints are not served
   under the share prefix. */
if (SHARED) {
  el("sharechip").style.display = "none";
  el("syncchip").style.display = "none";
  //: A share viewer must not be able to change how the car is polled.
  //: The POST is refused server-side for the /s/ surface regardless;
  //: this keeps the control from appearing at all.
  el("drivemodechip").style.display = "none";
  el("sharedbadge").style.display = "";
  const vinChip = el("vin").closest(".chip"); if (vinChip) vinChip.style.display = "none";
  const detail = document.querySelector('[data-mode="detail"]');
  if (detail) detail.style.display = "none";
  if (MODE === "detail") MODE = "drive";
}

setMode(MODE);
(async () => {
  await loadMeta();
  if (!SHARED) { await loadRuns(); await loadHistory(); }
})();
