/* 52 Weatherboard — client-side live feeds.

   Everything here is fetched by the browser, on load and on Refresh, so these
   values never wait on the poller. That matters because GitHub's scheduled runs
   are best-effort: the every-15-minutes cron in poll.yml lands closer to every
   ~3 hours in practice, which is fine for an archive and useless for a board
   you check during a storm.

   Five of the six upstream feeds allow cross-origin reads and need no key, so
   they are called directly. The two that are not here:
     - Ambient (DMATStation) needs an account key, which cannot ship in a public
       page; it stays on the committed file unless AMBIENT_PROXY_URL is set.
     - OneRain blocks cross-origin reads. It was only fallback #2 behind NWPS
       for the Bridge Street gauge, so losing it in the browser costs nothing.

   Freshness here is capped by the sources, not by us: the creek gauges report on
   change, so in dry weather the newest NWPS reading can be a couple of hours old.
   That is the gauge's cadence and no amount of polling improves it.
*/

const LIVE = {
  lat: 37.9745, lon: -122.5625, tz: "America/Los_Angeles",
  usgsSite: "11460000",      // Ross Creek — downstream reference
  rossLid: "CMDC1",
  bridgeLid: "SBSC1",        // San Anselmo at Bridge St — the one that floods first
  timeoutMs: 12000,
};

/* Mirrors config.json "thresholds". Kept in step with poller.py by hand; both
   sides are small and change rarely. */
const TH = {
  watch_qpf_72h_in: 3.0, prepare_qpf_24h_in: 2.0,
  act_rain_rate_inhr: 0.75, act_rain_1h_in: 0.6,
  sump_prepare_duty_pct: 50, sump_act_duty_pct: 80,
};

async function jget(url) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), LIVE.timeoutMs);
  try {
    const r = await fetch(url, { signal: ctl.signal, headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(String(r.status));
    return await r.json();
  } finally { clearTimeout(timer); }
}

/* ---- individual feeds (shapes match poller.py so the pages need no changes) ---- */

async function liveUsgs() {
  const j = await jget("https://waterservices.usgs.gov/nwis/iv/?format=json"
    + "&sites=" + LIVE.usgsSite + "&parameterCd=00065,00060&siteStatus=all");
  const out = {};
  for (const ts of j.value.timeSeries) {
    const code = ts.variable.variableCode[0].value;
    const vals = ts.values[0].value;
    if (!vals || !vals.length) continue;
    const last = vals[vals.length - 1];
    out[code === "00065" ? "stage_ft" : "flow_cfs"] = parseFloat(last.value);
    out.observed = last.dateTime;
  }
  return out;
}

// -999 is the NWPS no-data sentinel; letting it through would read as a dry creek.
const realPoints = data => (data || []).filter(p => (p.primary == null ? -999 : p.primary) > -900);
const catsOf = meta => Object.fromEntries(
  Object.entries(((meta.flood || {}).categories) || {})
    .filter(([, v]) => v && typeof v === "object").map(([k, v]) => [k, v.stage]));
const peakOf = fc => fc.length ? Math.max(...fc.map(p => p.primary || 0)) : null;

async function gauge(lid) {
  const base = "https://api.water.noaa.gov/nwps/v1/gauges/" + lid;
  const [meta, sf] = await Promise.all([jget(base), jget(base + "/stageflow")]);
  return {
    meta,
    obs: realPoints((sf.observed || {}).data),
    fc: ((sf.forecast || {}).data) || [],
    categories: catsOf(meta),
  };
}

async function liveNwps() {
  const { meta, obs, fc, categories } = await gauge(LIVE.rossLid);
  return {
    name: meta.name, categories,
    observed: obs.slice(-96).map(p => ({ t: p.validTime, stage: p.primary })),
    forecast: fc.map(p => ({ t: p.validTime, stage: p.primary })),
    latest_stage_ft: obs.length ? obs[obs.length - 1].primary : null,
    forecast_peak_ft: peakOf(fc),
  };
}

async function liveBridge(townStages, gaugeUrl) {
  const { meta, obs, fc, categories } = await gauge(LIVE.bridgeLid);
  if (!obs.length) throw new Error("nwps: no observed data");
  const latest = obs[obs.length - 1];
  return {
    source: "nwps:" + LIVE.bridgeLid, name: meta.name,
    stage_ft: latest.primary, observed: latest.validTime, categories,
    forecast_peak_ft: peakOf(fc),
    series: obs.slice(-192).map(p => ({ t: p.validTime, stage: p.primary })),
    forecast: fc.slice(0, 48).map(p => ({ t: p.validTime, stage: p.primary })),
    town_stages: townStages, url: gaugeUrl,
  };
}

async function liveNws() {
  const pt = (await jget("https://api.weather.gov/points/" + LIVE.lat + "," + LIVE.lon)).properties;
  const [grid, alerts] = await Promise.all([
    jget(pt.forecastGridData).then(g => g.properties).catch(() => ({})),
    jget("https://api.weather.gov/alerts/active?point=" + LIVE.lat + "," + LIVE.lon),
  ]);
  const qpf = ((grid.quantitativePrecipitation || {}).values || []).map(v => {
    const parts = v.validTime.split("/");
    return { t: parts[0], dur: parts[1], in: Math.round(((v.value || 0) / 25.4) * 1000) / 1000 };
  });
  return {
    qpf_periods: qpf,
    alerts: (alerts.features || []).map(a => ({
      event: a.properties.event, severity: a.properties.severity,
      headline: a.properties.headline, ends: a.properties.ends || a.properties.expires,
    })),
    office: pt.cwa, forecast_zone: pt.forecastZone,
  };
}

const r2 = n => Math.round(n * 100) / 100;

async function liveOpenMeteo() {
  const j = await jget("https://api.open-meteo.com/v1/forecast"
    + "?latitude=" + LIVE.lat + "&longitude=" + LIVE.lon
    + "&hourly=precipitation,precipitation_probability&precipitation_unit=inch"
    + "&timezone=" + encodeURIComponent(LIVE.tz) + "&forecast_days=7");
  const h = j.hourly;
  const hourly = h.time.map((t, i) => ({
    t, in: h.precipitation[i] || 0, prob: h.precipitation_probability[i],
  }));
  // Open-Meteo starts at midnight local; slice from the current hour forward.
  const p = new Intl.DateTimeFormat("en-CA", {
    timeZone: LIVE.tz, year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", hour12: false,
  }).formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
  const nowLocal = p.year + "-" + p.month + "-" + p.day + "T"
    + (p.hour === "24" ? "00" : p.hour) + ":00";
  const future = hourly.filter(x => x.t >= nowLocal);
  const f = future.length ? future : hourly;
  const sum = n => r2(f.slice(0, n).reduce((a, x) => a + x.in, 0));
  return {
    hourly,
    qpf_24h_in: sum(24), qpf_72h_in: sum(72), qpf_7d_in: sum(f.length),
    peak_rate_next_24h: r2(Math.max(0, ...f.slice(0, 24).map(x => x.in))),
  };
}

/* ---- tier ----
   Port of poller.py evaluate(). Without this the page would show live numbers
   under a tier banner computed hours ago — the exact failure this board exists
   to avoid. rain_1h_in and sump duty still come from the committed history,
   since both are counter deltas that need the archive. */
function evaluateTier(d) {
  const reasons = [];
  let level = 0;
  const raise = (n, why) => { level = Math.max(level, n); reasons.push(why); };

  const alerts = ((d.nws || {}).alerts || []).map(a => (a.event || "").toLowerCase());
  if (alerts.some(a => a.includes("flood warning") || a.includes("flash flood")))
    raise(3, "NWS flood warning in effect");
  else if (alerts.some(a => a.includes("flood watch")))
    raise(2, "NWS flood watch in effect");

  const om = d.open_meteo;
  if (om) {
    if (om.qpf_24h_in >= TH.prepare_qpf_24h_in) raise(2, om.qpf_24h_in + '" forecast next 24h');
    else if (om.qpf_72h_in >= TH.watch_qpf_72h_in) raise(1, om.qpf_72h_in + '" forecast next 72h');
  }

  const derived = d.derived || {};
  if (d.ambient) {
    const rr = d.ambient.rain_rate_inhr || 0;
    if (rr >= TH.act_rain_rate_inhr) raise(3, "rain rate " + rr + '"/hr');
    const r1 = derived.rain_1h_in;
    if (r1 != null && r1 >= TH.act_rain_1h_in) raise(3, r1 + '" in the last hour');
  }

  const duty = derived.sump_duty_pct;
  if (duty != null) {
    if (duty >= TH.sump_act_duty_pct) raise(3, "sump running " + duty + "% of the hour");
    else if (duty >= TH.sump_prepare_duty_pct) raise(2, "sump running " + duty + "% of the hour");
  }
  for (const pump of ((d.sump || {}).pumps || []))
    if (pump.name === "backup" && pump.on) raise(3, "backup pump running (primary being overwhelmed)");

  const stageCheck = (g, label) => {
    if (!g) return;
    const st = g.stage_ft != null ? g.stage_ft : g.latest_stage_ft;
    if (st == null) return;
    const c = g.categories || {};
    if (c.minor && st >= c.minor) raise(3, label + " " + st + " ft, above minor flood stage");
    else if (c.action && st >= c.action) raise(2, label + " " + st + " ft, above action stage");
    const pk = g.forecast_peak_ft;
    if (pk && c.action && pk >= c.action) raise(Math.max(level, 1), "NWS forecasts " + label + " to " + pk + " ft");
  };
  stageCheck(d.nwps, "Ross gauge");
  stageCheck(d.bridge_street, "Bridge Street gauge");

  return { tier: ["quiet", "watch", "prepare", "act"][level], reasons };
}

/* ---- entry point ----
   One failed feed must not blank the board, so each is settled independently and
   whatever is missing falls back to the committed file. */
async function fetchLiveFeeds(base) {
  const b = base || {};
  const jobs = {
    usgs: liveUsgs(),
    nwps: liveNwps(),
    bridge_street: liveBridge((b.bridge_street || {}).town_stages, b.bridge_street_gauge_url),
    nws: liveNws(),
    open_meteo: liveOpenMeteo(),
  };
  if (typeof AMBIENT_PROXY_URL === "string" && AMBIENT_PROXY_URL)
    jobs.ambient = jget(AMBIENT_PROXY_URL);

  const keys = Object.keys(jobs);
  const settled = await Promise.allSettled(keys.map(k => jobs[k]));
  const data = {}, errors = {};
  settled.forEach((r, i) => {
    if (r.status === "fulfilled") data[keys[i]] = r.value;
    else errors[keys[i]] = String((r.reason && r.reason.message) || r.reason);
  });
  return { data, errors, at: Date.now() };
}
