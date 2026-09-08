#!/usr/bin/env python3
"""
Layer 1 — storm catalog.

Events are contiguous rain separated by a 6-hour dry gap. Two windows per event:

  rain window     [start, end]                     where the rain actually fell
  response window [start, min(end+12h, next start)] where the creek and pump answer

The creek crests after the rain stops, so measuring peak stage only inside the
rain window would systematically clip it. The trailing 12 h is capped at the next
event so two storms never claim the same peak.

Sump rolling windows are built from the raw PumpFuse rows on a 1-minute grid,
not from the 10-minute timeline: 15 minutes is not a multiple of 10, and a
'peak duty over 15 min' computed on 10-min bins would be a different quantity.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TZ = "America/Los_Angeles"
DRY_GAP_H = 6
TRAIL_H = 12

CFG = json.loads((ROOT / "config.json").read_text())
GPM = CFG["primary_pump"]["rated_gpm"]

df = pd.read_parquet(HERE / "timeline.parquet")
df.index = pd.to_datetime(df.index, utc=True)

# ---------------------------------------------------------------- sump at 1 min
pf = pd.read_csv(ROOT / "imports" / "PumpFuse.csv", sep="\t")
pf["ts"] = (pd.to_datetime(pf["Time"])
            .dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
            .dt.tz_convert("UTC"))
pf = pf.sort_values("ts").set_index("ts")
minute = pd.date_range(df.index.min(), df.index.max() + pd.Timedelta(minutes=9),
                       freq="1min", tz="UTC")
run_s = pf["Duration"].resample("1min").sum().reindex(minute).fillna(0.0)
run_n = pf["Duration"].resample("1min").count().reindex(minute).fillna(0).astype(int)

# duty over a window = seconds running / seconds in window
duty15 = run_s.rolling(15, min_periods=1).sum() / (15 * 60) * 100
duty60 = run_s.rolling(60, min_periods=1).sum() / (60 * 60) * 100
runs15 = run_n.rolling(15, min_periods=1).sum()
inflow15 = duty15 / 100 * GPM
inflow60 = duty60 / 100 * GPM

# ---------------------------------------------------------------- segmentation
wet = df.index[df["rain_in"] > 0]
events = []
if len(wet):
    start = prev = wet[0]
    for t in wet[1:]:
        if (t - prev) >= pd.Timedelta(hours=DRY_GAP_H):
            events.append((start, prev))
            start = t
        prev = t
    events.append((start, prev))

rows = []
for i, (s, e) in enumerate(events):
    nxt = events[i + 1][0] if i + 1 < len(events) else df.index[-1] + pd.Timedelta(hours=1)
    resp_end = min(e + pd.Timedelta(hours=TRAIL_H), nxt)
    rain_w = df.loc[s:e]
    resp = df.loc[s:resp_end]
    m_resp = (minute >= s) & (minute <= resp_end)

    peak_1h_t = rain_w["rain_1h_in"].idxmax()

    # sump, from the 1-min series
    if m_resp.any():
        i15, i60 = inflow15[m_resp], inflow60[m_resp]
        pk_inflow = float(i15.max()); pk_inflow_t = i15.idxmax()
        pk_inflow60 = float(i60.max())
        pk_duty15 = float(duty15[m_resp].max()); pk_duty60 = float(duty60[m_resp].max())
        pk_runs15 = int(runs15[m_resp].max())
        tot_s = float(run_s[m_resp].sum()); n_runs = int(run_n[m_resp].sum())
    else:
        pk_inflow = pk_inflow60 = pk_duty15 = pk_duty60 = 0.0
        pk_runs15 = n_runs = 0; tot_s = 0.0; pk_inflow_t = pd.NaT
    gal = tot_s / 60 * GPM

    # creek: observed readings only, never the interpolation
    b = resp["bridge_stage_obs_max"].dropna()
    r_st = resp["ross_stage_ft_obs_max"].dropna()
    r_fl = resp["ross_flow_cfs_obs_max"].dropna()

    def lag(t_to):
        if pd.isna(t_to) or pd.isna(peak_1h_t):
            return np.nan
        return (t_to - peak_1h_t).total_seconds() / 3600.0

    rows.append(dict(
        event=i + 1,
        start=s, end=e,
        start_local=s.tz_convert(TZ).strftime("%Y-%m-%d %H:%M"),
        duration_h=round((e - s).total_seconds() / 3600, 2),
        total_rain_in=round(float(rain_w["rain_in"].sum()), 3),
        peak_rain_10min_in=round(float(rain_w["rain_10min_in"].max()), 3),
        peak_rain_30min_in=round(float(rain_w["rain_30min_in"].max()), 3),
        peak_rain_1h_in=round(float(rain_w["rain_1h_in"].max()), 3),
        peak_rain_3h_in=round(float(rain_w["rain_3h_in"].max()), 3),
        peak_rain_6h_in=round(float(rain_w["rain_6h_in"].max()), 3),
        peak_rain_12h_in=round(float(rain_w["rain_12h_in"].max()), 3),
        peak_rain_24h_in=round(float(rain_w["rain_24h_in"].max()), 3),
        api_at_start=round(float(df.loc[s, "api_0p9"]), 3),
        season_rain_at_start_in=round(float(df.loc[s, "season_rain_in"]), 2),
        peak_duty_15min_pct=round(pk_duty15, 2),
        peak_duty_60min_pct=round(pk_duty60, 2),
        peak_runs_per_15min=pk_runs15,
        pump_runs=n_runs,
        total_gallons=round(gal, 1),
        gal_per_cycle=round(gal / n_runs, 2) if n_runs else np.nan,
        peak_inflow_gpm=round(pk_inflow, 2),
        peak_inflow_gpm_60min=round(pk_inflow60, 2),
        headroom_gpm=round(GPM - pk_inflow, 2),
        peak_bridge_stage_ft=round(float(b.max()), 2) if len(b) else np.nan,
        bridge_obs_in_window=len(b),
        peak_ross_stage_ft=round(float(r_st.max()), 2) if len(r_st) else np.nan,
        peak_ross_flow_cfs=round(float(r_fl.max()), 1) if len(r_fl) else np.nan,
        lag_rain_to_inflow_h=round(lag(pk_inflow_t), 2) if n_runs else np.nan,
        lag_rain_to_bridge_h=round(lag(b.idxmax()), 2) if len(b) else np.nan,
    ))

ev = pd.DataFrame(rows)
ev.to_csv(HERE / "events.csv", index=False)
print(f"wrote {HERE/'events.csv'}: {len(ev)} events")
