# weatherboard — the 52 Woodland storm planner

A static site in `docs/` (GitHub Pages) plus a six-hourly Python job that writes its
forecast inputs. The local folder is called `floodboard`; the GitHub repo is `weatherboard`.
`README.md` explains the product and the models; read it before changing behaviour.

Live site: https://dmathivanan.github.io/weatherboard/ (Pages deploys `main` → `/docs`, so
**a push to `main` is a deploy**; it goes live a minute or two later).

## Every session
1. `git pull --rebase origin main` first. A GitHub Actions bot (`forecast.yml`) commits
   `docs/data/forecast.json` and `docs/data/rain_daily.json` every six hours, so the clone
   is usually behind.
2. Dependencies come from the cloud setup script (see Cloud session notes). If you install
   in a session, use `python -m pip install ...`, never bare `pip`.
3. Preview with `python -m http.server 8765 --directory docs`. The pages fetch
   `docs/data/*.json` with relative paths, so they need a server, not `file://`.
4. Before the session ends: commit, `git pull --rebase origin main`, `git push origin main`.
   Work that is not pushed is lost with the sandbox.

## Answering questions about a storm
Questions like "what was the peak rainfall rate in the last storm", "how much did the creek
rise", "how hard did the pump work" are answered from the raw data, not from memory:

1. `python analysis/update_data.py` — brings station rain, Bridge Street and Ross up to now,
   rebuilds the storms page's catalog (below), and prints how fresh each source is.
2. `python analysis/storm.py last` (or a date, `2026-11-13`, or `list --since 2026-10-01`;
   add `--json` for every field). One storm gives the rain total and peak 5-min / 10-min /
   1-h rates with times, the Bridge Street and Ross stage before, crest, rise and timing,
   the sump runs, longest run and peak inflow, and what the planner's crest model would
   have predicted for that rain.
3. Answer from that output and say how fresh the data is. Things to carry into the answer:
   - A storm is rain split by a 6-hour dry gap (same rule as `events.csv`). One flagged
     `STILL IN PROGRESS` rained within the last 6 h, so its totals can still grow.
   - Bridge Street reports only when the level changes: the crest is the highest reading
     it sent. Ross (USGS) is a continuous 15-minute record.
   - The pump log is a manual export. If `storm.py` says the log ends before or during
     the storm, say so and ask for a new export rather than reporting zero runs.
   - All times are Pacific. A storm's start is its first trace of rain, which can be hours
     before the real rain: never put a peak rate next to the start time. Give every peak
     its own time and its averaging window ("0.20 in in the 5 minutes ending 9:45 PM, a
     rate of 2.4 in/hr for those 5 minutes; 1.5 in/hr over the heaviest 10").
   - "heaviest 5 min" is a single 5-minute reading of the rain counter. "station peak
     rate" is the console's own number, which is a 10-minute rate and so reads lower.
4. If `update_data.py` changed files, commit them (`imports/`, `analysis/cache/`,
   `docs/data/storms.json`) and push, so the next session starts current. `imports/` and
   `analysis/cache/` do not change the live site; `storms.json` is the storms page.

For anything `storm.py` does not print, read the same files with pandas (below).

## The data (all tracked, all in this clone)
| File | What | Grows by |
|---|---|---|
| `imports/station.csv` | 5-min station rain since Sept 2025: `Date` (UTC) and the Rain Rate / Event / Daily / Yearly counters | `update_data.py station` (Ambient API, needs the keys) |
| `imports/PumpFuse.csv` | one row per primary-pump run: local time, watts, seconds | `update_data.py pump FILE` |
| `imports/bridge_street.csv` | San Anselmo Creek at Bridge Street, stage ft (UTC) | `update_data.py bridge` (marin.onerain.com) |
| `analysis/cache/usgs_YYYYMM.json` | Ross stage and flow, 15 min, one file per month | `update_data.py usgs` |
| `analysis/cache/fcst_daily.csv`, `openmeteo_prevruns.json` | last winter's archived forecasts, for the forecast-skill work | fixed |
| `analysis/cache/fcst_daily_2026_27.csv` | this season's archived Open-Meteo forecasts per day (one file per season) | `update_data.py forecast` |

**The storms page** (`docs/storms.html`) shows this season in the same table as last winter.
Its rows are `docs/data/storms.json`, written by `storm.py` (`build_catalog`) at the end of
every `update_data.py` run and by `python analysis/storm.py catalog`: one row per storm of
0.10 in or more this water year. Do not edit it by hand; a new storm appears the next time
the data is refreshed and pushed. Its forecast columns stay blank until `update_data.py
forecast` has read Open-Meteo's archive for those days.

**New pump runs** arrive only when Deepak exports them from the PumpFuse app. Save what he
sends (an attached file, or rows pasted into the chat) as a file and run
`python analysis/update_data.py pump <file>`. It matches rows by shape, so pasted text
with spaces instead of tabs is fine, and re-merging an old export adds nothing.

The repo is public. The raw Ambient console export (`imports/AWN.csv`, laptop only) is
kept out because it carries indoor readings; do not add indoor or device-identifying data.

## Refitting the models
The fitted models were built on winter 2025-26 (Nov–Apr) and that window is still
hard-coded: `build_timeline.py` (`WINDOW_START`/`WINDOW_END`, the USGS months),
`stress_model.py` (`W`), and the catalog name `storms_2025_26.json`. The pipeline, in order:

    python analysis/build_timeline.py    # -> timeline.parquet, layer0_report.txt
    python analysis/build_events.py      # -> events.csv
    python analysis/creek_model.py       # crest and rising-limb fits (prints)
    python analysis/stress_model.py      # -> stress_run_fit.json, runs_points.json, ...
    python analysis/export_models.py     # -> docs/data/models.json  (what the planner runs)

Run as-is it reproduces the committed files, except the sump run-length table
(`stress_run_fit.json → rate_bins`), which covers the whole pump record and so picks up
every newly merged run. That refresh is safe to commit.

Anything that changes a fitted coefficient is a change to the live planner. Before pushing
one: show the old and new coefficients, and the `storm.py list` hindcast errors for this
season's storms under each, and get a yes. `storm.py`'s `planner_crest_model` line is the
running out-of-sample check: this season's storms were not in the fit.

## What to leave alone
- `docs/data/forecast.json` and `docs/data/rain_daily.json` belong to the bot. Do not
  hand-edit or commit them from a session; if `python forecast.py` was run for a test,
  `git checkout -- docs/data/forecast.json docs/data/rain_daily.json` before committing.
  A conflict in either is resolved by taking the remote version.
- `docs/data/latest.json` and `history.csv` are the frozen last snapshot of the retired poller.
- Do not bring back live polling or a sump feed (README, "What was retired").

## Where things are
- `docs/*.html`, `docs/assets/planner.js` — the pages; all models run client-side.
- `config.json` — thresholds, tier ladder, checklist, bias factors.
- `docs/data/storms.json` — this season's storm catalog, generated (see "The storms page").
- `docs/data/models.json`, `storms_2025_26.json` — written by `analysis/export_models.py`.
- `forecast.py`, `fetchers.py` — the six-hour job; each feed is isolated by `safe()`.
- `analysis/` — the data tools above, last winter's fits and `report.md`.
- `nowcast/` — MRMS radar + pysteps.

## Secrets
`AMBIENT_APP_KEY`, `AMBIENT_API_KEY`, optional `FLOODBOARD_CONTACT` (see `.env.template`).
`forecast.py` and `update_data.py station` need them; everything else, including
`storm.py`, does not. The keys travel in the query string, so never print a raw
`requests` error: pass it through `fetchers.err_text`. Never commit `.env`.

## Cloud session notes
- Setup script (it runs before the repo is cloned, so packages are named explicitly):
  `pip install requests python-dotenv pandas pyarrow numpy markdown`
- Hosts the environment must allow: api.weather.gov, api.open-meteo.com, www.cnrfc.noaa.gov,
  cw3e.ucsd.edu, waterservices.usgs.gov, api.water.noaa.gov, rt.ambientweather.net
  (`forecast.py`), marin.onerain.com (`update_data.py bridge`),
  previous-runs-api.open-meteo.com (`update_data.py forecast`) and dmathivanan.github.io
  (to check the live site after a push). A blocked host shows up as one failed source; the
  others still update.
- Open-Meteo limits requests per address per day, and cloud sessions share addresses, so
  `update_data.py forecast` can fail with "request limit used up". It keeps the last file;
  run it again in a later session.
- Ambient allows one request per second per key. `update_data.py` paces itself and retries
  a 429; a full backfill of a month is about 30 requests.
- **Not in the clone:** `nowcast/mrms.sqlite` (~130 MB, over GitHub's file limit). The
  nowcast needs its own install (`nowcast/README.md`) and a store rebuilt with
  `python nowcast/backtest.py fetch`; `nowcast.py sweep` / `verify` score against it.
