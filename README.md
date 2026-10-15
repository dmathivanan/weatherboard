# 52 Woodland storm planner

A planner for the next storm at 52 Woodland Ave, San Anselmo. Served by GitHub
Pages from `docs/`. No hardware at home, no live loop.

The product changed on 2026-10-14. It used to be a live monitor polled every 15
minutes (which GitHub's cron turned into every ~3 hours). It is now a **planner**:
a job runs every six hours and writes the forecast inputs, and the pages turn
those inputs into a Bridge Street crest, a sump load, a tier and a checklist with
clock-time deadlines, all computed in the browser so the sliders are live.

## How it fits together

```
config.json            thresholds, pump specs, town stage lines, bias factors, checklist
analysis/              last winter's data and the fitted models (see analysis/report.md)
  export_models.py     -> docs/data/models.json         coefficients the pages run
                       -> docs/data/storms_2025_26.json the calibration catalog
fetchers.py            one function per feed; raises on failure, isolated by safe()
forecast.py            the 6-hour job
                       -> docs/data/forecast.json       forecast inputs + wetness + prefill
                       -> docs/data/rain_daily.json     station rain per local day (ledger)
docs/
  index.html           landing page
  planner.html         the planner
  wetness.html         the ground wetness index
  storms.html          the storm catalog (forecast vs predicted vs actual)
  data/storms.json     hand-maintained catalog for the current season
  assets/planner.js    the models, tier ladder and checklist, client-side
  creek.html, storm.html   legacy live pages (browser-fetched gauges, alerts, radar)
  report.html          the winter 2025-26 analysis write-up
.github/workflows/forecast.yml   runs forecast.py every 6 h and commits docs/data/
```

### The six-hour job (`forecast.py`)

Pulls, each isolated so one dead feed never blanks the file:

| Source | What | Notes |
|---|---|---|
| NWS grid | QPF periods + active alerts for the house | `api.weather.gov` |
| Open-Meteo | hourly rain, 7 days, plus ECMWF / GFS / HRRR 24 h | **bias-corrected per `qpf_bias`**: ×1.35 for the first 24 h, sliding to ×2.0 at 72 h |
| CNRFC | 6-hourly point QPF, days 1–6 | no Marin point exists; Napa (`APC`) and Venado (`VENC1`) bracket the house |
| CW3E | maximum forecast AR Scale at the coast off Marin | the JSON behind the AR Scale plume map, points 38.0N/123.0W and 37.5N/122.5W |
| USGS 11460000 | Ross stage and flow, last 7 days | baseflow = 10th percentile of the week |
| Ambient | station rain per local day | needs `AMBIENT_APP_KEY` / `AMBIENT_API_KEY` |

Then it computes the **ground wetness index**: the antecedent precipitation index
(API, 0.9/day decay) that both creek models were fitted on, rebuilt from the
station's daily totals. `rain_daily.json` is a ledger that grows through the
season; the job re-reads the last three days each run and fills any gap from
Open-Meteo's analysis, marking those days as model. Ross baseflow gives a loose
cross-check (`baseflow_to_api`, R² 0.50) and is the fallback when the station is
unreachable. `saturated_api_in` (5.2 in, the index when the January 2026 storm
began) sets 100 % on the wetness bar.

Finally it picks the wettest 48 h in the horizon and writes `prefill`: the
48-hour total, peak hourly rate, onset / heaviest hour / end, the index, the AR
category and whether a Flood Watch is up. The planner starts from those and the
chips under each slider offer the other sources' numbers.

### The planner (`planner.html`, `assets/planner.js`)

All from `models.json`:

- **Bridge Street crest** `peak_ft = 1.19 + 0.93 × total_in + 0.33 × API`
  (R² 0.82, n = 24, 95 % band ±1.5 ft). Fitted on a winter whose highest crest
  was 7.38 ft; above that the page says so.
- **Rate-based cross-check**: the Layer 3A rising-limb model fed an estimated
  heaviest-6-hour rain (`r6h_from_storm`, R² 0.83).
- **Ross equivalent** from `bridge = 0.767 × ross − 1.96` (R² 0.997), with the
  fitted range flagged.
- **Distance to 6.5 and 13 ft**, and the rain still needed to reach each.
- **Sump at the peak rate**: peak 15-minute inflow from the heaviest 10 minutes
  (`0.31 × peak_1h`) and the index (R² 0.89), then the pit identity
  `run_s = 14 / (1 − Qin/Qp)` and runs per hour from run + refill. The
  run-level model (10 min of rain ending 5 min after the run starts, R² 0.72)
  gives the typical run, and its stressed-run variant the worst single run
  with a band (report §3).
- **Tier** on the `config.json` ladder, each rule fed the planner's equivalent
  (the 48-h total × 0.8 stands in for the heaviest 24 h).
- **Checklist** from `config.json → planner.checklist`: each item is anchored to
  rain onset or the creek crest (heaviest hour + the median 0.8 h lag) with an
  offset in hours and a minimum tier.

**Save this plan** stores the inputs and outputs in the browser; `storms.html`
shows saved plans with a JSON row to paste into `data/storms.json`. After the
storm, fill the `actual` block there from the station, NWPS `SBSC1`, USGS and
the PumpFuse app. The PumpFuse live feed stays out on purpose; the app's own
alerts cover the pump.

## Setup

1. Repo → Settings → Secrets → Actions: `AMBIENT_APP_KEY`, `AMBIENT_API_KEY`,
   optionally `FLOODBOARD_CONTACT` (an email for the User-Agent).
2. Repo → Settings → Pages → Deploy from branch `main`, folder `/docs`.
3. Actions → **forecast** → Run workflow once; the log's last line prints the
   48-h total, peak rate, index and any feed errors. After that it runs on its
   own every six hours (late, usually; GitHub's cron is best-effort).
4. Locally: `pip install requests python-dotenv`, put the Ambient keys in `.env`,
   `python forecast.py`, then serve `docs/` with any static server.

Re-run `python analysis/export_models.py` after any refit (it needs pandas and
pyarrow and reads `analysis/timeline.parquet` + `events.csv`).

## What was retired

- `poller.py` and the 15-minute `poll.yml`. `docs/data/latest.json` and
  `history.csv` stay as the last snapshot (the legacy live pages overlay fresh
  gauge reads on them) but no longer grow.
- The sump adapter (`fetch_sump`): the PumpFuse PF03 has no local API, and the
  planner does not need one.
- `rainfall.html`, which only read the archive.
- The dead CNRFC (`restricted/graphicalRVF_csv.php`, 401) and CW3E
  (`ar_scale_summary.json`, 404) endpoints, replaced as in the table above.
- Pushover tier-change pushes, which lived in the poller.

## Pump capacity is a derived number, not a measured one

The 84 GPM used throughout is the **nameplate** figure for the Zoeller N140.
Geometry (a 36-inch pit, 4.41 gal per inch, an *assumed* 4.5-inch float travel)
gives 85.0 GPM, and the 14-second shortest run gives the same drawdown volume,
so the three agree. But the float travel is assumed: at 4.0 in delivered drops
to 75.5 GPM and at 3.0 in to 56.7 GPM, and every inflow and stress figure
scales with it. Measuring the actual float travel is the single highest-value
calibration left.

## December 2005

Water entered this basement from the north side in the 2005 flood, before the
sump existed. The defense against creek flooding on that side is physical
barriers, not the pump; the checklist's Act-tier items are about the barriers.
See `analysis/report.md` §7 for what is and is not known about that event.

## Tiers

Quiet < Watch < Prepare < Act < Emergency; the highest satisfied rule wins
(`config.json → tiers`, rebuilt from the winter 2025-26 replay, report §6).
