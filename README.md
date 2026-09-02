# 52 Woodland flood board

A cloud-only flood monitor: GitHub Actions polls every 15 minutes, commits the
data to this repo, and GitHub Pages serves the dashboard. No hardware at home.

## Dashboard pages

`docs/` is a small multi-page site (shared `assets/app.css` + `assets/app.js`),
all reading the same `data/latest.json` and `data/history.csv`:

- **index.html** — Dashboard: current weather, rainfall rollups, sump activity,
  creek summary.
- **rainfall.html** — rain rate history + Open-Meteo/NWS forecast.
- **creek.html** — San Anselmo (SBSC1) vs Ross (CMDC1) stage over the past 12 h,
  with the Town's flood-stage lines.
- **storm.html** — RainViewer animated radar (past + 2-h nowcast) centered on
  94960, NWS alerts, QPF outlook.

External libraries are loaded from CDNs (allowed on GitHub Pages): Chart.js
(charts), Leaflet + RainViewer (radar), Google Fonts. Client-side rollups
(6 h / max-today / N-hours-ago / sump run-rates) are computed in `app.js` from
`history.csv`; the poller only needs to keep appending rows.

## Feeds

| Source | What it gives | Access |
|---|---|---|
| Ambient Weather | rain rate (in/hr), event / 24h / weekly totals | API keys from ambientweather.net |
| USGS 11460000 | Corte Madera Creek at Ross, stage + flow | public JSON |
| NWS NWPS gauge CMDC1 | same gauge with flood categories and NWS forecast stage | public JSON |
| NWS api.weather.gov | gridded QPF + active alerts for the house | public JSON |
| Open-Meteo | hourly rain forecast, 7 days, used for the 24h/72h totals | public JSON |
| Sump monitor | pit level, runtime, cycles | placeholder adapter (see below) |
| Bridge Street gauge | San Anselmo Creek downtown, the gauge that floods first | NWPS `SBSC1` JSON (primary) + Marin OneRain FS 19 graph (fallback) |

## Setup (about 20 minutes)

1. Create a GitHub repository (private is fine) and upload these files, keeping
   the folder structure (`.github/workflows/poll.yml`, `docs/index.html`, ...).
2. Ambient Weather: sign in at ambientweather.net, Account -> API Keys, create
   one Application Key and one API Key.
3. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:
   `AMBIENT_APP_KEY`, `AMBIENT_API_KEY`. Optional: `PUSHOVER_TOKEN`,
   `PUSHOVER_USER` (pushover.net, ~$5 one-time, gives phone push alerts).
4. Repo -> Settings -> Pages -> Source: "Deploy from a branch", branch `main`,
   folder `/docs`. Your dashboard URL appears there.
5. Repo -> Actions -> "poll" -> Run workflow. Check the run log; the last line
   prints the tier and any feed errors. After that it runs on its own.
6. Open `config.json` and adjust `lat`/`lon` if needed. Thresholds are starter
   values; recalibrate after each storm (below).

Notes: GitHub's cron is best-effort and often runs 5–15 minutes late under
load. GitHub disables scheduled workflows on repos with no activity for 60
days; the bot's own commits count, so this only matters if polling stops.

## v2 design: local server, two cadences (build this first)

Rain rates here change in minutes, so `poller.py` should be refactored from a
one-shot cron script into a long-running process with two loops:

- **Fast loop, every 60–120 s:** Ambient Weather (or, better, the station's
  "custom server" upload pushed to a local HTTP listener every 16–60 s) and
  both PumpFuse units (primary 1 HP Zoeller, backup 3/4 HP). Discover the
  PumpFuse local endpoint on the LAN first; fall back to its app export.
- **Slow loop:** USGS + NWPS every 15 min, NWS alerts every 5 min, Open-Meteo
  and NWS grid forecasts every 60 min.
- **Storage:** SQLite (`floodboard.db`), one table per source, plus a
  `windows` table with rolling 10/30/60-min rain totals and 15/60-min sump
  duty for each pump, recomputed on every fast tick.
- **Rules:** evaluate tiers on the rolling windows, never on a single reading.
  Backup pump activation is an Act-tier signal on its own.
- **Dashboard:** serve `docs/` locally (any static server); it reads a small
  `latest.json` the process rewrites each tick, plus a 48-h window endpoint.
- **Deployment:** develop on the laptop; run the same process on a Raspberry
  Pi (on the Tesla-backed circuit) for winter. GitHub Actions is not suitable
  at this cadence.

## Calibrating from last winter

Import the Ambient and PumpFuse exports (`./imports/`) into the same SQLite
schema, resample to 10-min windows, then fit: primary duty cycle (and inflow =
duty × pump GPM) against rain rate with a lag search (0–90 min) and a 7-day
antecedent term. Do the same for creek stage at Ross (USGS historical) against
6-h and 24-h rain totals. Write the resulting thresholds into `config.json`.

`./imports/bridge_street.csv` holds last winter's downtown gauge (Nov 2025–Apr
2026, San Anselmo Creek at Bridge Street, `datetime_utc,stage_ft`), pulled by
`imports/download_bridge_street.py`. It is event-based (sub-15-min during
storms, sparse when flat); the peak was 7.38 ft on 2026-01-06, well below the
13.3-ft minor-flood stage. Note it only covers the Nov–Apr window, while the
Ambient export runs through the following summer, so join on the overlap.

## Sump adapter

`fetch_sump()` in `poller.py` is a placeholder that reads a JSON endpoint
(`SUMP_URL` secret). Replace it with the real API once the monitor brand is
known. Duty cycle is computed from cumulative runtime between polls, so any
device that exposes runtime or an on/off history will work.

## Calibrating the model

`docs/data/history.csv` is the dataset. After each storm, pull the rows for
that event and record, in one spreadsheet line per storm:

- peak 15-minute rain rate, 1-hour and 24-hour totals
- 7-day antecedent rain before the storm started
- peak sump duty cycle (and whether the pit ever came close to overflowing)
- peak creek stage at Ross, and the Bridge Street reading if you noted it
- how much of the 24h/72h forecast total actually fell

Two or three storms are enough to set `thresholds` in `config.json` from your
own house rather than guesses.

## Tiers

- **Quiet** – nothing in observations or the 3-day forecast.
- **Watch** – 3-day forecast total over the watch threshold, or NWS forecasting
  the Ross gauge to reach action stage.
- **Prepare** – NWS Flood Watch, 24-hour forecast over threshold, sump duty
  above the prepare level, or Ross gauge above action stage.
- **Act** – NWS Flood Warning, observed rain rate or 1-hour total over the act
  thresholds, sump duty above the act level, or Ross gauge above minor flood.

The phone gets one push when the tier changes, high priority for Act.
