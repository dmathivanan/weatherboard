/* 52 Woodland flood board — shared helpers, nav, data loading, rollups */
const TZ = "America/Los_Angeles";
const SITE = { lat: 37.9745, lon: -122.5625, zip: "94960", name: "San Anselmo" };

const PAGES = [
  { id: "dashboard", href: "index.html",   label: "Dashboard" },
  { id: "rainfall",  href: "rainfall.html", label: "Rainfall" },
  { id: "creek",     href: "creek.html",    label: "Creek" },
  { id: "storm",     href: "storm.html",    label: "Storm" },
];
const TIER_LABEL = { quiet: "Quiet", watch: "Watch", prepare: "Prepare", act: "Act now" };

const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => (v == null || v === "" || isNaN(v)) ? "–" : Number(v).toFixed(d);
const num = v => { const n = parseFloat(v); return isNaN(n) ? null : n; };
const clockOpts = { timeZone: TZ, weekday: "short", hour: "numeric", minute: "2-digit" };
const timeOpts = { timeZone: TZ, hour: "numeric", minute: "2-digit" };
const laDate = ms => new Date(ms).toLocaleDateString("en-CA", { timeZone: TZ }); // YYYY-MM-DD
const compass = deg => ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"][Math.round(((deg||0)%360)/22.5)%16];

/* ---- header / nav ---- */
function renderHeader(active) {
  const el = document.querySelector("header.top");
  if (!el) return;
  el.innerHTML =
    `<div class="top-in">
       <span class="brand">52 Weatherboard</span>
       <nav class="tabs">${PAGES.map(p =>
         `<a href="${p.href}"${p.id === active ? ' class="active"' : ''}>${p.label}</a>`).join("")}</nav>
       <span class="tierchip quiet" id="tierchip">–</span>
     </div>`;
}
function setTierChip(tier) {
  const c = $("tierchip"); if (!c) return;
  c.className = "tierchip " + tier;
  c.textContent = TIER_LABEL[tier] || tier;
}

/* ---- data loading ---- */
async function loadLatest() {
  return fetch("data/latest.json?" + Date.now()).then(r => r.json());
}
async function loadHistory() {
  const txt = await fetch("data/history.csv?" + Date.now()).then(r => r.ok ? r.text() : "").catch(() => "");
  if (!txt) return [];
  const [head, ...lines] = txt.trim().split("\n");
  const cols = head.split(",");
  return lines.map(l => {
    const o = Object.fromEntries(l.split(",").map((v, i) => [cols[i], v]));
    o.ts = Date.parse(o.ts_utc);
    return o;
  }).filter(o => !isNaN(o.ts));
}

/* boot: render header, load data, run page callback, stamp time, handle errors.
   Auto-refreshes the data every 5 min so the board stays live without a manual
   reload; the poller itself commits fresh data every ~15 min. */
const REFRESH_MS = 5 * 60 * 1000;
async function boot(pageId, cb) {
  renderHeader(pageId);
  async function refresh() {
    try {
      const latest = await loadLatest();
      const history = await loadHistory().catch(() => []);
      setTierChip(latest.tier);
      if ($("updated")) {
        const t = new Date(latest.generated_utc);
        $("updated").textContent = "Updated " + t.toLocaleString("en-US", clockOpts) +
          " · auto-refreshes every 5 min" +
          (latest.errors && Object.keys(latest.errors).length
            ? " · feeds down: " + Object.keys(latest.errors).join(", ") : "");
      }
      await cb(latest, history);
    } catch (e) {
      if ($("updated")) $("updated").textContent = "Could not load data: " + e;
      console.error(e);
    }
  }
  await refresh();
  setInterval(refresh, REFRESH_MS);
}

/* ---- generic time-series rollups ----
   points: array of {t, v} where t is ms-epoch and v is a number (or null) */
function toPoints(arr, tKey, vKey) {
  return (arr || []).map(p => ({ t: typeof p[tKey] === "number" ? p[tKey] : Date.parse(p[tKey]), v: num(p[vKey]) }))
    .filter(p => !isNaN(p.t)).sort((a, b) => a.t - b.t);
}
// value "as of" N hours ago = last reading at or before the target time.
// (Gauges here report on change, so an exact-time match rarely exists.)
function valueAgo(points, hours) {
  if (!points.length) return null;
  const target = Date.now() - hours * 3600e3;
  let best = null;
  for (const p of points) { if (p.v == null) continue; if (p.t <= target) best = p; else break; }
  return best ? best.v : null;
}
function maxToday(points) {
  const today = laDate(Date.now());
  const vals = points.filter(p => p.v != null && laDate(p.t) === today).map(p => p.v);
  return vals.length ? Math.max(...vals) : null;
}
// {t,v} of today's max (for "peak X ft at HH:MM"), or null
function maxTodayInfo(points) {
  const today = laDate(Date.now());
  let best = null;
  for (const p of points) { if (p.v == null || laDate(p.t) !== today) continue; if (!best || p.v > best.v) best = p; }
  return best;
}
// {t,v} of the highest point in a series (e.g. an NWS forecast), or null
function peakInfo(points) {
  let best = null;
  for (const p of points) { if (p.v == null) continue; if (!best || p.v > best.v) best = p; }
  return best;
}
const hhmm = ms => new Date(ms).toLocaleString("en-US", timeOpts);   // "6:56 AM"
function latestVal(points) {
  for (let i = points.length - 1; i >= 0; i--) if (points[i].v != null) return points[i].v;
  return null;
}
/* trailing rain total (inches) by integrating rate (in/hr) over the window */
function integrateInches(points, hours) {
  const since = Date.now() - hours * 3600e3;
  const p = points.filter(x => x.t >= since && x.v != null);
  if (p.length < 2) return null;
  let total = 0;
  for (let i = 1; i < p.length; i++) {
    const dt = Math.min((p[i].t - p[i - 1].t) / 3600e3, 0.5); // hours, cap gaps at 30 min
    total += p[i - 1].v * dt;
  }
  return total;
}
/* sump runs: increase in a cumulative counter over the window */
function counterDelta(points, hours) {
  const since = Date.now() - hours * 3600e3;
  const p = points.filter(x => x.t >= since && x.v != null);
  if (p.length < 2) return null;
  const d = p[p.length - 1].v - p[0].v;
  return d >= 0 ? d : null; // negative => counter reset, unknown
}

/* SVG line chart fallback (used if Chart.js absent). series: [{label,color,points:[{t,v}]}] */
function windowPoints(arr, tKey, vKey, hours) {
  const since = Date.now() - hours * 3600e3;
  return toPoints(arr, tKey, vKey).filter(p => p.t >= since);
}
