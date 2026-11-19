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
- `docs/data/storms.json` — hand-maintained storm catalog for the current season.
- `docs/data/models.json`, `storms_2025_26.json` — written by `analysis/export_models.py`.
- `forecast.py`, `fetchers.py` — the six-hour job; each feed is isolated by `safe()`.
- `analysis/` — last winter's fits and `report.md`; `nowcast/` — MRMS radar + pysteps.

## Secrets
`AMBIENT_APP_KEY`, `AMBIENT_API_KEY`, optional `FLOODBOARD_CONTACT` (see `.env.template`).
Only `forecast.py` needs them, and it degrades without them: the station rain falls back
to Open-Meteo. Editing the pages needs none. Never commit `.env`; this repo is public.

## Cloud session notes
- Setup script (it runs before the repo is cloned, so packages are named explicitly):
  `pip install requests python-dotenv pandas pyarrow numpy markdown`
- `forecast.py` calls api.weather.gov, Open-Meteo, CNRFC, CW3E, USGS and Ambient. None is
  on the default network allowlist, so it only runs if the environment allows those hosts.
- **Not in the clone** (gitignored, on the laptop only): `imports/*.csv` (raw Ambient,
  PumpFuse and Bridge Street exports), `analysis/cache/`, `nowcast/mrms.sqlite` (~130 MB).
  So in a cloud session:
  - `analysis/export_models.py` runs (it reads the committed `timeline.parquet` and
    `events.csv`), but without `cache/fcst_daily.csv` it writes `storms_2025_26.json` with
    the forecast columns empty. Do not commit that file from a cloud session.
  - `build_timeline.py`, `build_events.py`, `stress_model.py`, `forecast_skill.py` and
    `replay_new_rules.py` do not: they read the raw imports. Refits are laptop work.
  - `nowcast/` needs its own install (`nowcast/README.md`; on Linux the stock `eccodes`
    wheel works) and an MRMS store rebuilt with `python nowcast/backtest.py fetch`.
    `nowcast.py sweep` / `verify` score against that store.
