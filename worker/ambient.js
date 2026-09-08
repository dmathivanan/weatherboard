/* 52 Weatherboard — Ambient proxy.

   The only feed the dashboard cannot read for itself. Ambient needs an account
   key and the board is a public page, so the key lives here instead. Everything
   else (USGS, NWPS, NWS, Open-Meteo) the browser fetches directly — see
   docs/assets/live.js.

   GET / -> poller.py fetch_ambient() shape, so docs/assets/app.js can drop the
   result straight into latest.ambient with no special-casing.

   Deploy:
     npx wrangler secret put AMBIENT_APP_KEY
     npx wrangler secret put AMBIENT_API_KEY
     npx wrangler deploy
   Then set AMBIENT_PROXY_URL in docs/assets/app.js to the deployed URL.

   No GitHub token, no write access anywhere: the worst this can do is read the
   weather at the house, which the dashboard publishes anyway.
*/

const ALLOWED_ORIGINS = [
  "https://dmathivanan.github.io",
  "http://localhost:8000",
];
const CACHE_S = 20;   // the station reports about once a minute

// Same field names as poller.py fetch_ambient().
function normalize(device) {
  const d = device.lastData || {};
  return {
    observed_utc: d.date,
    rain_rate_inhr: d.hourlyrainin,
    rain_event_in: d.eventrainin,
    rain_24h_in: d.dailyrainin,
    rain_daily_in: d.dailyrainin,
    rain_weekly_in: d.weeklyrainin,
    rain_monthly_in: d.monthlyrainin,
    baro_inhg: d.baromrelin,
    temp_f: d.tempf,
    feels_like_f: d.feelsLike,
    dew_point_f: d.dewPoint,
    humidity_pct: d.humidity,
    wind_mph: d.windspeedmph,
    wind_gust_mph: d.windgustmph,
    wind_dir_deg: d.winddir,
    wind_max_daily_gust_mph: d.maxdailygust,
    device: device.info && device.info.name,
  };
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const headers = {
      "Access-Control-Allow-Origin": ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0],
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Vary": "Origin",
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    };
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
    if (origin && !ALLOWED_ORIGINS.includes(origin))
      return new Response(JSON.stringify({ error: "forbidden origin" }), { status: 403, headers });

    // Short shared cache so a burst of refreshes cannot outpace Ambient's own
    // rate limit (roughly one request per second per key).
    const key = new Request("https://weatherboard.invalid/ambient");
    const hit = await caches.default.match(key);
    if (hit) return new Response(await hit.text(), { headers });

    try {
      const r = await fetch("https://rt.ambientweather.net/v1/devices"
        + "?applicationKey=" + encodeURIComponent(env.AMBIENT_APP_KEY)
        + "&apiKey=" + encodeURIComponent(env.AMBIENT_API_KEY),
        { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error("ambient " + r.status);
      const devices = await r.json();
      if (!Array.isArray(devices) || !devices.length) throw new Error("ambient returned no devices");

      const body = JSON.stringify(normalize(devices[0]));
      ctx.waitUntil(caches.default.put(key, new Response(body, {
        headers: { "Content-Type": "application/json", "Cache-Control": "max-age=" + CACHE_S },
      })));
      return new Response(body, { headers });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e.message || e) }), { status: 502, headers });
    }
  },
};
