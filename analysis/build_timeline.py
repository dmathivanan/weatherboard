#!/usr/bin/env python3
"""
Layer 0 — one time-aligned dataset for Nov 2025 through Apr 2026.

Sources and their native resolution:
  Ambient (imports/AWN.csv)          5 min, console export
  PumpFuse (imports/PumpFuse.csv)    event rows, one per pump run, 1 s stamps
  Bridge Street (imports/*.csv)      event-based: ~15 min in storms, 12 h when flat
  USGS 11460000                      15 min, instantaneous-values API

Grid: 10 minutes, UTC. Ambient (5 min) and PumpFuse (events) genuinely resolve
that. USGS at 15 min and Bridge Street when flat do not, so their columns carry
an explicit *_native_res_min and *_gap_min so nothing downstream mistakes an
interpolated point for an observation. build_report() prints the honest per-
source picture.

Warm-up: everything is processed from 2025-10-01 (water-year start) so the 72 h
rollups, the antecedent index and season-to-date are already correct on Nov 1.
Rows are trimmed to the Nov-Apr window only at the very end.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
IMPORTS = ROOT / "imports"
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)

TZ = "America/Los_Angeles"
GRID_MIN = 10
WARMUP_START = pd.Timestamp("2025-10-01", tz="UTC")      # water year, for antecedent terms
WINDOW_START = pd.Timestamp("2025-11-01", tz="UTC")
WINDOW_END = pd.Timestamp("2026-05-01", tz="UTC")        # exclusive

CFG = json.loads((ROOT / "config.json").read_text())
PUMP_GPM = CFG["primary_pump"]["rated_gpm"]              # 84
USGS_SITE = CFG["usgs_site"]                             # 11460000

# A 5-minute Ambient tick above this is a counter glitch, not weather: the
# station's own record peak rate is 1.56 in/hr = 0.13 in per 5 min.
MAX_SANE_5MIN_IN = 0.5


# --------------------------------------------------------------- helpers
def counter_increments(s: pd.Series, max_step=None):
    """Per-step increase of a cumulative counter that periodically resets to 0.

    Ambient's Daily Rain resets at local midnight and Event Rain resets after a
    dry spell, so a negative diff means 'reset, then accumulated to the current
    value' — the increment is the current value, not the (negative) diff.
    """
    d = s.diff()
    reset = d < 0
    d = d.where(~reset, s)
    d.iloc[0] = 0.0
    d = d.fillna(0.0).clip(lower=0.0)
    n_susp = 0
    if max_step is not None:
        susp = d > max_step
        n_susp = int(susp.sum())
        d = d.where(~susp, 0.0)
    return d, int(reset.sum()), n_susp


def gaps_over(idx: pd.DatetimeIndex, hours=1.0):
    """Gaps longer than `hours` in an observation index -> list of (start, end, hours)."""
    if len(idx) < 2:
        return []
    s = pd.Series(idx).sort_values().reset_index(drop=True)
    d = s.diff().dt.total_seconds() / 3600.0
    out = []
    for i in np.where(d > hours)[0]:
        out.append((s[i - 1], s[i], float(d[i])))
    return out


def native_res_min(idx: pd.DatetimeIndex):
    """Median spacing in minutes, the honest 'what does this source resolve'."""
    if len(idx) < 3:
        return np.nan
    d = pd.Series(idx).sort_values().diff().dt.total_seconds() / 60.0
    return float(d.median())


# --------------------------------------------------------------- loaders
def load_ambient():
    df = pd.read_csv(IMPORTS / "AWN.csv", encoding="utf-8-sig", low_memory=False)
    # 'Date' carries an explicit UTC offset; 'Simple Date' is naive local.
    t = pd.to_datetime(df["Date"], utc=True, format="ISO8601")
    df = df.assign(ts=t).sort_values("ts").drop_duplicates("ts").set_index("ts")
    df = df[df.index >= WARMUP_START]

    out = pd.DataFrame(index=df.index)
    out["rain_rate_inhr"] = pd.to_numeric(df["Rain Rate (in/hr)"], errors="coerce")

    # Primary: difference Daily Rain, which resets at local midnight.
    daily = pd.to_numeric(df["Daily Rain (in)"], errors="coerce").ffill()
    inc_daily, n_reset_daily, n_susp = counter_increments(daily, MAX_SANE_5MIN_IN)

    # Cross-check: Yearly Rain resets only on Jan 1, so it is an independent
    # witness that spans every midnight in the window.
    yearly = pd.to_numeric(df["Yearly Rain (in)"], errors="coerce").ffill()
    inc_year, n_reset_year, _ = counter_increments(yearly, MAX_SANE_5MIN_IN)

    event = pd.to_numeric(df["Event Rain (in)"], errors="coerce").ffill()
    inc_event, n_reset_event, _ = counter_increments(event, MAX_SANE_5MIN_IN)

    out["rain_in"] = inc_daily
    out["rain_in_yearly_check"] = inc_year
    out["rain_in_event_check"] = inc_event

    meta = dict(
        rows=len(df), res_min=native_res_min(df.index),
        daily_resets=n_reset_daily, yearly_resets=n_reset_year,
        event_resets=n_reset_event, suspect_steps=n_susp,
        total_daily=float(inc_daily.sum()), total_yearly=float(inc_year.sum()),
        total_event=float(inc_event.sum()),
        gaps=gaps_over(df.index),
    )
    return out, meta


def load_pumpfuse():
    df = pd.read_csv(IMPORTS / "PumpFuse.csv", sep="\t")
    naive = pd.to_datetime(df["Time"])
    # Device stamps wall-clock local time with no offset. DST end (2 Nov 2025)
    # repeats 01:00-02:00; take the first (PDT) pass and shift any nonexistent
    # spring-forward stamp rather than dropping runs.
    ts = naive.dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")
    df = df.assign(ts=ts).sort_values("ts").set_index("ts")
    df = df[df.index >= WARMUP_START]
    df["Duration"] = pd.to_numeric(df["Duration"], errors="coerce")
    meta = dict(runs=len(df), res_min=native_res_min(df.index),
                gaps=gaps_over(df.index), total_runtime_s=float(df["Duration"].sum()))
    return df, meta


def load_bridge():
    df = pd.read_csv(IMPORTS / "bridge_street.csv")
    ts = pd.to_datetime(df["datetime_utc"], utc=True, format="ISO8601")
    s = (df.assign(ts=ts).sort_values("ts").drop_duplicates("ts")
           .set_index("ts")["stage_ft"].astype(float))
    meta = dict(rows=len(s), res_min=native_res_min(s.index), gaps=gaps_over(s.index))
    return s, meta


def load_usgs():
    """Instantaneous values, monthly chunks, cached to analysis/cache/."""
    frames = []
    months = pd.date_range("2025-10-01", "2026-04-01", freq="MS")
    for m in months:
        end = (m + pd.offsets.MonthEnd(1)).strftime("%Y-%m-%d")
        f = CACHE / f"usgs_{m:%Y%m}.json"
        if not f.exists():
            r = requests.get(
                "https://waterservices.usgs.gov/nwis/iv/",
                params={"format": "json", "sites": USGS_SITE,
                        "parameterCd": "00065,00060",
                        "startDT": m.strftime("%Y-%m-%d"), "endDT": end},
                timeout=120, allow_redirects=True)
            r.raise_for_status()
            f.write_text(r.text)
            print(f"  fetched USGS {m:%Y-%m} ({len(r.content)} bytes)", file=sys.stderr)
        j = json.loads(f.read_text())
        for ts_block in j["value"]["timeSeries"]:
            code = ts_block["variable"]["variableCode"][0]["value"]
            col = {"00065": "ross_stage_ft", "00060": "ross_flow_cfs"}.get(code)
            if not col:
                continue
            vals = ts_block["values"][0]["value"]
            if not vals:
                continue
            d = pd.DataFrame(vals)
            t = pd.to_datetime(d["dateTime"], utc=True, format="ISO8601")
            v = pd.to_numeric(d["value"], errors="coerce")
            v = v.where(v > -900)                      # USGS no-data sentinel
            frames.append(pd.Series(v.values, index=t, name=col))

    out = {}
    for name in ("ross_stage_ft", "ross_flow_cfs"):
        parts = [f for f in frames if f.name == name]
        if parts:
            s = pd.concat(parts).sort_index()
            out[name] = s[~s.index.duplicated()]
    df = pd.DataFrame(out).sort_index()
    df = df[df.index >= WARMUP_START]
    meta = dict(rows=len(df), res_min=native_res_min(df.index),
                gaps=gaps_over(df.dropna(how="all").index))
    return df, meta


# --------------------------------------------------------------- build
def build():
    print("loading sources...", file=sys.stderr)
    amb, m_amb = load_ambient()
    pf, m_pf = load_pumpfuse()
    br, m_br = load_bridge()
    usgs, m_usgs = load_usgs()

    grid = pd.date_range(WARMUP_START, WINDOW_END, freq=f"{GRID_MIN}min",
                         tz="UTC", inclusive="left")
    df = pd.DataFrame(index=grid)
    df.index.name = "ts_utc"

    # --- Ambient: rain is a flux, so sum into the bin; rate is a state, so mean.
    r = amb.resample(f"{GRID_MIN}min")
    df["rain_in"] = r["rain_in"].sum().reindex(grid).fillna(0.0)
    df["rain_in_yearly_check"] = r["rain_in_yearly_check"].sum().reindex(grid).fillna(0.0)
    df["rain_rate_inhr"] = r["rain_rate_inhr"].mean().reindex(grid)
    df["rain_obs_n"] = r["rain_in"].count().reindex(grid).fillna(0).astype(int)

    # --- PumpFuse: runs are ~16 s, so bin by start time; straddling is immaterial.
    pr = pf.resample(f"{GRID_MIN}min")
    df["sump_runtime_s"] = pr["Duration"].sum().reindex(grid).fillna(0.0)
    df["sump_runs"] = pr["Duration"].count().reindex(grid).fillna(0).astype(int)
    df["sump_watts_mean"] = pr["Power(w)"].mean().reindex(grid)
    df["sump_gal"] = df["sump_runtime_s"] / 60.0 * PUMP_GPM
    df["sump_inflow_gpm"] = df["sump_gal"] / GRID_MIN          # avg over the bin
    df["sump_duty_pct"] = df["sump_runtime_s"] / (GRID_MIN * 60.0) * 100.0

    # --- Bridge Street: interpolation for continuity, per-bin max for true peaks.
    df["bridge_stage_ft"], df["bridge_stage_obs_max"] = _on_grid(br, grid)
    df["bridge_gap_min"] = _distance_to_obs(grid, br.index)

    # --- USGS: 15 min native, so most 10-min bins hold no reading of their own.
    for col in ("ross_stage_ft", "ross_flow_cfs"):
        if col in usgs:
            df[col], df[col + "_obs_max"] = _on_grid(usgs[col], grid)
    df["ross_gap_min"] = _distance_to_obs(grid, usgs.dropna(how="all").index)

    # --- derived rain terms
    per_hour = 60 // GRID_MIN
    windows = {"10min": 1, "30min": 3, "1h": per_hour, "3h": 3 * per_hour,
               "6h": 6 * per_hour, "12h": 12 * per_hour, "24h": 24 * per_hour,
               "72h": 72 * per_hour}
    for name, n in windows.items():
        df[f"rain_{name}_in"] = df["rain_in"].rolling(n, min_periods=1).sum()

    # Antecedent precipitation index, 0.9 per day, applied per 10-min step.
    decay = 0.9 ** (GRID_MIN / 1440.0)
    api = np.empty(len(df))
    acc = 0.0
    rain = df["rain_in"].to_numpy()
    for i in range(len(df)):
        acc = acc * decay + rain[i]
        api[i] = acc
    df["api_0p9"] = api

    # Season-to-date on the water year (Oct 1). The warm-up start is that date,
    # so a plain cumulative sum is already season-to-date.
    df["season_rain_in"] = df["rain_in"].cumsum()

    # Dry hours since the last bin with measurable rain.
    wet = df["rain_in"] > 0
    idx = np.arange(len(df))
    last_wet = pd.Series(np.where(wet, idx, np.nan), index=df.index).ffill()
    df["dry_hours"] = (idx - last_wet.to_numpy()) * GRID_MIN / 60.0

    # local time is what 'midnight' and 'storm on a Tuesday' actually mean here
    df["ts_local"] = df.index.tz_convert(TZ)

    full = df
    win = df.loc[(df.index >= WINDOW_START) & (df.index < WINDOW_END)].copy()
    return win, full, dict(ambient=m_amb, pumpfuse=m_pf, bridge=m_br, usgs=m_usgs)


def _distance_to_obs(grid, obs_idx):
    """Minutes from each grid point to the nearest real observation.

    Done with get_indexer(method='nearest') and a real Timedelta rather than
    integer views: pandas 3 stores datetimes as microseconds, so an assumed
    nanosecond divisor silently reports distances 1000x too small.
    """
    if len(obs_idx) == 0:
        return pd.Series(np.nan, index=grid)
    o = pd.DatetimeIndex(obs_idx).sort_values().unique()
    pos = o.get_indexer(grid, method="nearest")
    d = (grid - o[pos]).to_series(index=grid).abs().dt.total_seconds() / 60.0
    return d


def _on_grid(s: pd.Series, grid):
    """Put an irregular series on the grid without losing peaks.

    Returns (interpolated, per_bin_max). Interpolation is evaluated at the grid
    points from the ORIGINAL timestamps — binning first and averaging would blur
    a storm crest, which is exactly the number this whole analysis is about.
    """
    s = s.dropna().sort_index()
    s = s[~s.index.duplicated()]
    if s.empty:
        return pd.Series(np.nan, index=grid), pd.Series(np.nan, index=grid)
    union = s.index.union(grid)
    interp = s.reindex(union).interpolate(method="time", limit_direction="both").reindex(grid)
    obs_max = s.resample(f"{GRID_MIN}min").max().reindex(grid)
    return interp, obs_max


# --------------------------------------------------------------- report
def build_report(win, meta):
    L = []
    A = L.append
    A("=" * 78)
    A("LAYER 0 — TIMELINE BUILD REPORT")
    A("=" * 78)
    A(f"window        : {win.index.min()}  ->  {win.index.max()}")
    A(f"grid          : {GRID_MIN} min UTC, {len(win):,} rows "
      f"({(win.index.max()-win.index.min()).days} days)")
    A(f"inflow basis  : runtime x {PUMP_GPM} GPM (config primary_pump.rated_gpm)")
    A("")
    A("-" * 78)
    A("RESOLUTION PER SOURCE")
    A("-" * 78)
    A(f"{'source':<16}{'native':>10}  {'rows/runs':>10}  how it lands on the 10-min grid")
    rows = [
        ("Ambient", f"{meta['ambient']['res_min']:.0f} min", meta["ambient"]["rows"],
         "5 min -> 2 obs per bin. Genuine 10-min resolution."),
        ("PumpFuse", "event", meta["pumpfuse"]["runs"],
         "one row per run, 1 s stamps -> exact per-bin totals. Genuine."),
        ("USGS 11460000", f"{meta['usgs']['res_min']:.0f} min", meta["usgs"]["rows"],
         "15 min -> most bins hold no reading; interpolated between."),
        ("Bridge Street", "event", meta["bridge"]["rows"],
         "~4-15 min in storms, 720 min when flat. Effectively 30 min+ off-storm."),
    ]
    for name, res, n, note in rows:
        A(f"{name:<16}{res:>10}  {n:>10,}  {note}")
    A("")
    A("  PumpFuse and Bridge Street are event sources: 'native' is not a sampling")
    A("  rate. For PumpFuse a gap means the pump did not run. For Bridge Street the")
    A("  gauge reports on change, so it self-densifies exactly when it matters.")
    A("")
    A("  Effective resolution is per source AND per period, not global:")
    real = win["ross_stage_ft_obs_max"].notna().mean() * 100
    A(f"    USGS         : {real:.0f}% of bins hold a real reading (15-min native on a 10-min grid)")
    b_real = win["bridge_stage_obs_max"].notna().mean() * 100
    A(f"    Bridge Street: {b_real:.1f}% of bins hold a real reading overall")
    A(f"                   median distance to a real reading: "
      f"{win['bridge_gap_min'].median():.0f} min  "
      f"(p90 {win['bridge_gap_min'].quantile(.9):.0f} min)")
    wet = win["rain_24h_in"] > 0.25
    A(f"                   during wet periods (24-h rain > 0.25 in, "
      f"{wet.mean()*100:.0f}% of window):")
    A(f"                     {(win.loc[wet,'bridge_stage_obs_max'].notna().mean()*100):.1f}% of bins real, "
      f"median distance {win.loc[wet,'bridge_gap_min'].median():.0f} min")
    A(f"                   when dry: median distance "
      f"{win.loc[~wet,'bridge_gap_min'].median():.0f} min")
    A("")
    A("-" * 78)
    A("RAINFALL RECONSTRUCTION (Ambient cumulative counters)")
    A("-" * 78)
    m = meta["ambient"]
    A(f"  Daily Rain  resets handled : {m['daily_resets']:>5}  (local midnight)")
    A(f"  Event Rain  resets handled : {m['event_resets']:>5}  (dry-spell reset)")
    A(f"  Yearly Rain resets handled : {m['yearly_resets']:>5}  (Jan 1)")
    A(f"  suspect steps zeroed       : {m['suspect_steps']:>5}  (> {MAX_SANE_5MIN_IN} in per 5 min)")
    A("")
    A(f"  season total from Daily  counter : {m['total_daily']:8.2f} in   <- primary")
    A(f"  season total from Yearly counter : {m['total_yearly']:8.2f} in   <- independent check")
    A(f"  season total from Event  counter : {m['total_event']:8.2f} in")
    d = abs(m["total_daily"] - m["total_yearly"])
    pct = 100 * d / m["total_yearly"] if m["total_yearly"] else float("nan")
    A(f"  Daily vs Yearly disagreement     : {d:8.2f} in  ({pct:.2f}%)")
    A(f"  window rain (Nov-Apr)            : {win['rain_in'].sum():8.2f} in")
    A("")
    A("-" * 78)
    A("GAPS LONGER THAN 1 HOUR (in the source's own observations)")
    A("-" * 78)
    for label, key in (("Ambient", "ambient"), ("PumpFuse", "pumpfuse"),
                       ("Bridge Street", "bridge"), ("USGS", "usgs")):
        g = [x for x in meta[key]["gaps"] if x[0] >= WINDOW_START and x[0] < WINDOW_END]
        if key == "pumpfuse":
            A(f"  {label:<14} {len(g):>4} gaps  (a gap here means 'pump did not run', not missing data)")
            continue
        A(f"  {label:<14} {len(g):>4} gaps > 1 h")
        for s, e, h in sorted(g, key=lambda x: -x[2])[:6]:
            A(f"                    {s:%Y-%m-%d %H:%M} -> {e:%Y-%m-%d %H:%M}  ({h:6.1f} h)")
        if len(g) > 6:
            A(f"                    ... and {len(g)-6} shorter ones")
    A("")
    A("-" * 78)
    A("COVERAGE / SANITY")
    A("-" * 78)
    A(f"  bins with any Ambient obs   : {(win['rain_obs_n']>0).mean()*100:5.1f}%")
    A(f"  bins with a pump run        : {(win['sump_runs']>0).mean()*100:5.1f}%   "
      f"({int(win['sump_runs'].sum()):,} runs, {win['sump_gal'].sum():,.0f} gal)")
    A(f"  peak 10-min rain            : {win['rain_in'].max():5.2f} in")
    A(f"  peak 1-h rain               : {win['rain_1h_in'].max():5.2f} in")
    A(f"  peak 24-h rain              : {win['rain_24h_in'].max():5.2f} in")
    bpk = win['bridge_stage_obs_max']
    A(f"  peak Bridge Street stage    : {bpk.max():5.2f} ft "
      f"on {bpk.idxmax():%Y-%m-%d %H:%M} UTC  (observed, not interpolated)")
    A(f"  peak Ross stage / flow      : {win['ross_stage_ft_obs_max'].max():5.2f} ft / "
      f"{win['ross_flow_cfs_obs_max'].max():,.0f} cfs")
    A(f"  peak sump inflow (10-min)   : {win['sump_inflow_gpm'].max():5.2f} GPM avg-over-bin")
    A(f"  max API / season total      : {win['api_0p9'].max():5.2f} / "
      f"{win['season_rain_in'].max():5.2f} in")
    return "\n".join(L)


if __name__ == "__main__":
    win, full, meta = build()
    out = HERE / "timeline.parquet"
    win.drop(columns=["ts_local"]).to_parquet(out, engine="pyarrow", compression="snappy")
    rep = build_report(win, meta)
    (HERE / "layer0_report.txt").write_text(rep, encoding="utf-8")
    print(rep)
    print(f"\nwrote {out}  ({out.stat().st_size/1e6:.2f} MB, {len(win):,} rows x {win.shape[1]-1} cols)")
