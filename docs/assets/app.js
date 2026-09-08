/* 52 Woodland flood board — shared helpers, nav, data loading, rollups */
const TZ = "America/Los_Angeles";
const SITE = { lat: 37.9745, lon: -122.5625, zip: "94960", name: "San Anselmo" };

/* Optional proxy for the one feed that cannot be read from the browser: Ambient
   needs an account key, and a public page cannot hold one. Leave it empty and
   the weather card falls back to the committed file (as stale as the last poll);
   point it at a proxy returning poller.py's ambient shape and it goes live too.
   Every other feed is fetched directly by live.js and needs nothing here. */
const AMBIENT_PROXY_URL = "";

const PAGES = [
  { id: "dashboard", href: "index.html",   label: "Dashboard" },
  { id: "rainfall",  href: "rainfall.html", label: "Rainfall" },
  { id: "creek",     href: "creek.html",    label: "Creek" },
  { id: "storm",     href: "storm.html",    label: "Storm" },
  { id: "report",    href: "report.html",   label: "Report" },
];
const TIER_LABEL = { quiet: "Quiet", watch: "Watch", prepare: "Prepare", act: "Act now",
                     emergency: "EMERGENCY" };

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
       <button class="refresh" id="refreshbtn"
          title="Re-read the live gauges and forecast">Refresh</button>
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

/* boot / refresh.

   Two layers, deliberately:
     - the committed latest.json is the baseline. It carries what only the
       archive can produce (history rollups, sump counters) and acts as the
       fallback for anything the browser cannot reach.
     - live.js fetches the gauges, alerts and forecast straight from the source
       on every load and every Refresh, then the tier is recomputed from those
       live values so the banner can never lag the numbers under it.

   The poller keeps running on its own erratic schedule; nothing on screen waits
   for it any more. */
const REFRESH_MS = 5 * 60 * 1000;
const STALE_MIN = 30;                 // committed-only data older than this is flagged
let _latest = null, _history = [], _render = null, _liveAt = null, _liveErrs = {};

/* Overlay live feeds on the committed baseline and re-derive the tier. Feeds
   that failed simply keep their committed values, so a dead upstream degrades
   one card instead of the page. */
function merge(base, live) {
  const out = Object.assign({}, base, live.data);
  out.derived = base.derived;                 // history-only, never live

  /* Creek forecast: rain already on the ground comes from the poller's history
     (the browser cannot difference the station's counters), but the rain still
     to come is refetched live, so the number moves between polls. */
  const cp = base.creek_predictor;
  if (cp && out.derived && out.derived.rain_6h_in != null && out.derived.api != null) {
    const hourly = (out.open_meteo || {}).hourly || [];
    const now = Date.now();
    const next6 = hourly
      .filter(x => { const t = Date.parse(x.t); return t >= now && t < now + 6 * 3600e3; })
      .reduce((a, x) => a + (x.in || 0), 0);
    const r6 = out.derived.rain_6h_in + next6;
    const pt = cp.intercept + cp.coef_r6h_in * r6 + cp.coef_api * out.derived.api;
    out.creek_forecast = { point_ft: Math.round(pt * 100) / 100,
                           lo_ft: Math.round((pt - cp.band_95_ft) * 100) / 100,
                           hi_ft: Math.round((pt + cp.band_95_ft) * 100) / 100,
                           horizon_h: 6, r2: cp.r2 };
  }

  const t = evaluateTier(out);
  out.tier = t.tier;
  out.reasons = t.reasons;
  return out;
}

function stamp(note) {
  const el = $("updated"); if (!el || !_latest) return;
  const bits = [];
  const liveKeys = ["usgs", "nwps", "bridge_street", "nws", "open_meteo"]
    .filter(k => !_liveErrs[k]).length;

  if (_liveAt && liveKeys) {
    bits.push("Live " + new Date(_liveAt).toLocaleString("en-US", clockOpts));
    // The creek gauges report on change, so name the observation age rather than
    // implying the fetch time is the reading time.
    const obs = (_latest.bridge_street || {}).observed;
    if (obs) {
      const m = Math.round((Date.now() - Date.parse(obs)) / 60000);
      bits.push("gauge reading " + (m < 60 ? m + " min" : (m / 60).toFixed(1) + " h") + " old");
    }
  }

  // The weather card only lags when there is no Ambient proxy configured.
  if (!AMBIENT_PROXY_URL || _liveErrs.ambient) {
    const t = new Date(_latest.generated_utc);
    const ageMin = Math.round((Date.now() - t.getTime()) / 60000);
    const stale = ageMin >= STALE_MIN;
    el.classList.toggle("stale", stale);
    bits.push("station from last poll " +
      (ageMin < 60 ? ageMin + " min" : (ageMin / 60).toFixed(1) + " h") + " ago");
  } else {
    el.classList.remove("stale");
  }

  const failed = Object.keys(_liveErrs);
  if (failed.length) bits.push("feeds down: " + failed.join(", "));
  if (note) bits.push(note);
  el.textContent = bits.join(" · ");
}

async function paint() {
  setTierChip(_latest.tier);
  await _render(_latest, _history);
  stamp();
}

async function boot(pageId, cb) {
  renderHeader(pageId);
  _render = cb;
  const btn = $("refreshbtn");
  if (btn) btn.addEventListener("click", () => pull(true));

  async function pull(manual) {
    if (btn) { btn.disabled = true; btn.textContent = manual ? "Refreshing…" : "Refresh"; }
    try {
      const base = await loadLatest();
      _history = await loadHistory().catch(() => []);
      const live = await fetchLiveFeeds(base);
      _liveErrs = live.errors;
      _liveAt = Object.keys(live.data).length ? live.at : null;
      _latest = merge(base, live);
      await paint();
    } catch (e) {
      if ($("updated")) $("updated").textContent = "Could not load data: " + e;
      console.error(e);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Refresh"; }
    }
  }

  await pull(false);
  setInterval(() => pull(false), REFRESH_MS);
  setInterval(() => stamp(), 60000);   // keep the stated ages honest between pulls
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
