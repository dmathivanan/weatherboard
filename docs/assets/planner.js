/* 52 Woodland storm planner — shared client-side code.

   Every number on planner.html / wetness.html / storms.html is computed here
   from two committed files:
     data/models.json    coefficients exported from analysis/ (export_models.py)
     data/forecast.json  inputs written every 6 h by forecast.py
   Nothing is fetched live; the sliders are the live part. */

const TZ = "America/Los_Angeles";
const PAGES = [
  { id: "planner", href: "planner.html", label: "Planner" },
  { id: "wetness", href: "wetness.html", label: "Wetness" },
  { id: "storms",  href: "storms.html",  label: "Storms" },
  { id: "creek",   href: "creek.html",   label: "Creek (live)" },
  { id: "storm",   href: "storm.html",   label: "Radar" },
  { id: "report",  href: "report.html",  label: "Report" },
];
const TIER_ORDER = ["quiet", "watch", "prepare", "act", "emergency"];
const TIER_LABEL = { quiet: "Quiet", watch: "Watch", prepare: "Prepare", act: "Act",
                     emergency: "EMERGENCY" };

const $ = id => document.getElementById(id);
const fmt = (v, d = 2) => (v == null || v === "" || isNaN(v)) ? "–" : Number(v).toFixed(d);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const clockOpts = { timeZone: TZ, weekday: "short", hour: "numeric", minute: "2-digit" };
const dateOpts = { timeZone: TZ, month: "short", day: "numeric" };
const whenStr = ms => new Date(ms).toLocaleString("en-US", clockOpts);
const dayStr = ms => new Date(ms).toLocaleString("en-US", dateOpts);

/* ---- nav ---- */
function renderNav(active) {
  const el = document.querySelector("header.top");
  if (!el) return;
  el.innerHTML =
    `<div class="top-in">
       <a class="brand" href="index.html">52 Storm Planner</a>
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

/* ---- data ---- */
async function loadJSON(path) {
  const r = await fetch(path + "?" + Date.now());
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}
async function loadModelsAndForecast() {
  const [models, forecast] = await Promise.all([
    loadJSON("data/models.json"),
    loadJSON("data/forecast.json").catch(e => ({ _missing: String(e) })),
  ]);
  return { models, forecast };
}
/* "YYYY-MM-DDTHH:MM" written in LA local time -> ms epoch. datetime-local
   inputs produce the same string, so one parser serves both. */
function parseLocal(s) {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s);
  if (!m) return null;
  const guess = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]);
  // offset of LA at that instant (handles DST), two passes for the edge hour
  const off = t => {
    const p = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour12: false, year: "numeric",
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      .formatToParts(new Date(t)).reduce((a, x) => (a[x.type] = x.value, a), {});
    const h = p.hour === "24" ? 0 : +p.hour;
    return Date.UTC(+p.year, p.month - 1, +p.day, h, +p.minute) - t;
  };
  let t = guess - off(guess);
  t = guess - off(t);
  return t;
}
function toLocalInput(ms) {
  if (ms == null) return "";
  const p = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour12: false, year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    .formatToParts(new Date(ms)).reduce((a, x) => (a[x.type] = x.value, a), {});
  const h = p.hour === "24" ? "00" : p.hour;
  return `${p.year}-${p.month}-${p.day}T${h}:${p.minute}`;
}

/* ---- models ----------------------------------------------------------- */
function lin(fit, ...x) {
  const b = fit.beta;
  const k = b.length - x.length;          // 1 with intercept, 0 through origin
  let y = k ? b[0] : 0;
  x.forEach((v, i) => { y += b[k + i] * v; });
  return y;
}

/* Bridge Street crest from the storm total and the wetness index.
   Returns point, 95% band, and the rain still needed to reach each town line. */
function predictBridge(M, total, api) {
  const f = M.event_peak;
  const pt = lin(f, total, api);
  const needed = line => (line - f.beta[0] - f.beta[2] * api) / f.beta[1];
  return {
    pt, lo: Math.max(0, pt - f.band_95), hi: pt + f.band_95,
    rain_to_notify: needed(6.5), rain_to_flood: needed(13.0),
    extrapolated: pt > M.observed_maxima.bridge_peak_ft || total > M.observed_maxima.total_rain_in,
  };
}
/* Layer 3A cross-check: rising-limb stage from the heaviest 6 h, which is
   itself estimated from the total and the peak hourly rate. */
function crossCheck(M, total, peak1h, api) {
  const r6 = Math.max(0, Math.min(total, lin(M.r6h_from_storm, total, peak1h)));
  const cp = M.creek_predictor;
  return { r6h: r6, pt: cp.intercept + cp.coef_r6h_in * r6 + cp.coef_api * api, band: cp.band_95_ft };
}
function rossEquivalent(M, bridge) {
  const r = M.ross_to_bridge;
  const ross = (bridge - r.intercept) / r.slope;
  return { ross, inRange: ross <= r.fitted_ross_range_ft[1] };
}
/* Sump at the peak rate.
   Event level: peak 15-min inflow from the heaviest 10 minutes and wetness, then
   the pit identity run_s = V/(Qp-Qin), cycle = run + refill (V/Qin).
   Run level: the typical single run at that rate (sump_stress_run, 10 min of
   rain ending 5 min after the run starts) and a worst-run estimate at the 95 %
   edge of the residual spread on stressed runs. */
function sumpAt(M, peak1h, api, season) {
  const r10 = lin(M.r10_from_r1h, peak1h);
  const Qp = M.pump.rated_gpm, V = M.pump.drawdown_gal, tmin = M.pump.min_run_s;
  const fromStress = st => {
    const stress = clamp(st, 0, 0.98);
    const run = tmin / (1 - stress);
    const q = stress * Qp;
    const refill = q > 0.5 ? V / q * 60 : Infinity;
    return { gpm: q, stress, run_s: run, runs_per_hr: isFinite(refill) ? 3600 / (run + refill) : 0, capacity: stress >= 0.98 };
  };
  const inflow = Math.max(0, lin(M.sump_inflow, r10, api));
  const out = { r10, mean: fromStress(inflow / Qp), extrapolated: peak1h > M.observed_maxima.peak_1h_in };
  const R = M.sump_stress_run;
  if (R && R.beta) {
    const typ = R.beta[0] + R.beta[1] * r10 + R.beta[2] * api + (R.beta[3] || 0) * (season || 0);
    out.typical = fromStress(typ);
    // worst single run: the typical run plus the 95 % residual spread seen on
    // stressed runs. Last winter's 134 s outlier sat beyond even this.
    const band = 1.96 * (R.residual_sd_stressed || 0.08);
    out.worst = fromStress(typ + band);
  }
  return out;
}

/* Tier for a planned storm, on the config ladder. The ladder was written for
   live readings; here each rule is fed the planner's equivalent. */
function planTier(M, inp, out) {
  const T = M.tiers, P = M.planner;
  const reasons = []; let level = 0;
  const up = (n, why) => { level = Math.max(level, n); reasons.push(why); };
  if (inp.ar_category != null && inp.ar_category >= T.watch.cw3e_ar_category_min)
    up(1, `CW3E AR scale ${inp.ar_category} forecast for the coast`);
  if (inp.flood_watch) up(1, "NWS Flood Watch in effect");
  if (inp.total >= T.watch.qpf_72h_corrected_in) up(1, `${fmt(inp.total)}" storm total`);
  const peak24 = inp.total * (P.peak_24h_fraction_of_48h || 0.8);
  if (peak24 >= T.prepare.qpf_24h_corrected_in)
    up(2, `~${fmt(peak24, 1)}" in the heaviest 24 h`);
  if (out.bridge.pt >= T.prepare.bridge_ft_with_rain.bridge_ft && inp.total >= T.prepare.bridge_ft_with_rain.qpf_24h_corrected_in)
    up(2, `Bridge St expected ${fmt(out.bridge.pt, 1)} ft`);
  const runS = out.sump.typical ? out.sump.typical.run_s : out.sump.mean.run_s;   // single-run rules want the run-level model
  const rate10 = out.sump.r10 * 6;   // the storm's heaviest 10 min, in/hr: the observed thresholds
  if (T.emergency.sump_rate_10min_inhr && rate10 >= T.emergency.sump_rate_10min_inhr)
    up(4, `${fmt(rate10, 1)} in/hr over 10 min: pump expected to run without stopping`);
  else if (T.act.sump_rate_10min_inhr && rate10 >= T.act.sump_rate_10min_inhr)
    up(3, `${fmt(rate10, 1)} in/hr over 10 min: long sump runs start`);
  else if (T.prepare.sump_rate_10min_inhr && rate10 >= T.prepare.sump_rate_10min_inhr)
    up(2, `${fmt(rate10, 1)} in/hr over 10 min: pump runs lengthen`);
  if (runS >= T.emergency.primary_run_no_stop_s || (out.sump.typical && out.sump.typical.capacity)) up(4, "primary pump at capacity");
  else if (runS >= T.act.sump_run_s) up(3, `sump runs ~${fmt(runS, 0)} s`);
  else if (runS >= T.prepare.sump_run_s) up(2, `sump runs ~${fmt(runS, 0)} s`);
  if (out.bridge.pt >= T.act.predicted_bridge_ft) up(3, `Bridge St expected ${fmt(out.bridge.pt, 1)} ft, past the 6.5 ft notify line`);
  if (out.ross.ross >= T.act.ross_ft) up(3, `Ross equivalent ${fmt(out.ross.ross, 1)} ft`);
  if (out.bridge.pt >= 13) up(4, "Bridge St expected at the 13 ft flood-horn line");
  if (out.ross.ross >= T.emergency.ross_ft) up(4, `Ross equivalent ${fmt(out.ross.ross, 1)} ft, above NWS action stage`);
  return { tier: TIER_ORDER[level], level, reasons };
}

/* Checklist with clock-time deadlines from the storm timing. */
function checklist(M, tierLevel, onsetMs, crestMs) {
  return (M.planner.checklist || []).map(c => {
    const base = c.when === "crest" ? crestMs : onsetMs;
    const due = base != null ? base + c.offset_h * 3600e3 : null;
    const need = TIER_ORDER.indexOf(c.min_tier);
    return { ...c, due, active: tierLevel >= need, needLabel: TIER_LABEL[c.min_tier] };
  }).sort((a, b) => (a.due ?? Infinity) - (b.due ?? Infinity));
}

/* One call that does everything the planner needs for a set of inputs. */
function evaluatePlan(M, inp) {
  const bridge = predictBridge(M, inp.total, inp.api);
  const cross = crossCheck(M, inp.total, inp.peak1h, inp.api);
  const ross = rossEquivalent(M, bridge.pt);
  const sump = sumpAt(M, inp.peak1h, inp.api, inp.season);
  const out = { bridge, cross, ross, sump };
  out.tier = planTier(M, inp, out);
  const lag = (M.timing && M.timing.lag_peak_rain_to_crest_h) || M.planner.lag_peak_rain_to_crest_h || 1;
  out.lagH = Math.max(1, Math.round(lag * 2) / 2);   // never less than an hour, half-hour steps
  out.crestMs = inp.peakAt != null ? inp.peakAt + out.lagH * 3600e3 : null;
  out.checklist = checklist(M, out.tier.level, inp.onset, out.crestMs);
  return out;
}

/* Tier the forecast itself implies (for the header chip on pages without sliders). */
function tierFromForecast(M, F) {
  const p = (F && F.prefill) || {};
  if (!M || !F || F._missing) return "quiet";
  return evaluatePlan(M, { total: p.total_48h_in || 0, peak1h: p.peak_1h_inhr || 0, api: p.api_in || 0,
    onset: null, peakAt: null, end: null, ar_category: p.ar_category, flood_watch: p.flood_watch }).tier.tier;
}

/* ---- saved plans (this browser only) ---- */
const PLANS_KEY = "planner.plans";
function loadPlans() { try { return JSON.parse(localStorage.getItem(PLANS_KEY) || "[]"); } catch { return []; } }
function savePlans(p) { try { localStorage.setItem(PLANS_KEY, JSON.stringify(p)); } catch {} }

/* ---- small SVG helpers for the wetness page ---- */
function svgEl(tag, attrs, text) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}
