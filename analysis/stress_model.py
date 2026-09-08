#!/usr/bin/env python3
"""
Layer 2 — sump stress model.

stress = 1 - 14/duration_s, with 14 s the shortest run ever recorded.

That definition is not arbitrary. For a pit of fixed drawdown volume V emptied
by a pump of capacity Qp against an inflow Qin:

    duration = V / (Qp - Qin)

The shortest possible run is the dry-pit case Qin = 0, so dur_min = V/Qp = 14 s,
hence V = 14*Qp. Substituting:

    14/duration = (Qp - Qin)/Qp = 1 - Qin/Qp
    stress      = 1 - 14/duration = Qin/Qp

So stress IS fractional inflow against pump capacity. stress 0.6 = 50 GPM of the
84 GPM rating; stress 1.0 is not a number the pump can reach - it is the
asymptote where inflow equals capacity and the run never ends. That is why the
'100% stress' row below is reported as a capacity crossing rather than a
duration, and why a linear model in rain is the physically right shape: stress is
a flow ratio, not a bounded score that needs squashing.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TZ = "America/Los_Angeles"
CFG = json.loads((ROOT / "config.json").read_text())
QP = CFG["primary_pump"]["rated_gpm"]          # 84
DUR_MIN = 14.0

# --------------------------------------------------------------- runs
pf = pd.read_csv(ROOT / "imports" / "PumpFuse.csv", sep="\t")
pf["ts"] = (pd.to_datetime(pf["Time"])
            .dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
            .dt.tz_convert("UTC"))
pf = pf.sort_values("ts").reset_index(drop=True)
pf["dur"] = pf["Duration"].astype(float)
pf["stress"] = 1.0 - DUR_MIN / pf["dur"]
pf["inflow_gpm"] = pf["stress"] * QP

# per-cycle duty: run length over the start-to-start interval
nxt = pf["ts"].shift(-1)
pf["cycle_s"] = (nxt - pf["ts"]).dt.total_seconds()
pf["gap_s"] = pf["cycle_s"] - pf["dur"]
pf["cycle_duty"] = pf["dur"] / pf["cycle_s"]

# --------------------------------------------------------------- rain at 5 min
tl = pd.read_parquet(HERE / "timeline.parquet")
tl.index = pd.to_datetime(tl.index, utc=True)

awn = pd.read_csv(ROOT / "imports" / "AWN.csv", encoding="utf-8-sig", low_memory=False)
at = pd.to_datetime(awn["Date"], utc=True, format="ISO8601")
awn = awn.assign(ts=at).sort_values("ts").drop_duplicates("ts").set_index("ts")
daily = pd.to_numeric(awn["Daily Rain (in)"], errors="coerce").ffill()
d = daily.diff()
d = d.where(~(d < 0), daily)
inc = d.fillna(0).clip(lower=0)
inc = inc.where(inc <= 0.5, 0.0)
rain5 = inc.resample("5min").sum()
cum = rain5.cumsum()

STEP = 5


def rain_window(times, win_min, lag_min):
    """Rain (in) falling in [t-lag-win, t-lag], from the 5-min cumulative curve."""
    hi = times - pd.Timedelta(minutes=lag_min)
    lo = hi - pd.Timedelta(minutes=win_min)
    c_hi = cum.reindex(cum.index.union(hi)).ffill().reindex(hi).to_numpy()
    c_lo = cum.reindex(cum.index.union(lo)).ffill().reindex(lo).to_numpy()
    return np.maximum(c_hi - c_lo, 0.0)


def at_time(col, times):
    s = tl[col]
    return s.reindex(s.index.union(times)).ffill().reindex(times).to_numpy()


t = pd.DatetimeIndex(pf["ts"])
for w in (10, 30, 60):
    pf[f"rain_{w}m"] = rain_window(t, w, 0)
pf["api"] = at_time("api_0p9", t)
pf["season"] = at_time("season_rain_in", t)
pf["bridge"] = at_time("bridge_stage_ft", t)
pf["bridge_gap"] = at_time("bridge_gap_min", t)

# analysis set: the winter window the other layers cover
W = pf[(pf.ts >= "2025-11-01") & (pf.ts < "2026-05-01")].copy()


# --------------------------------------------------------------- helpers
def wls(X, y, w):
    """Weighted least squares -> (beta, weighted R^2)."""
    sw = np.sqrt(w)
    Xw, yw = X * sw[:, None], y * sw
    beta, *_ = np.linalg.lstsq(Xw, yw, rcond=None)
    pred = X @ beta
    ybar = np.average(y, weights=w)
    ss_res = np.sum(w * (y - pred) ** 2)
    ss_tot = np.sum(w * (y - ybar) ** 2)
    return beta, 1 - ss_res / ss_tot


def design(df, cols):
    return np.column_stack([np.ones(len(df))] + [df[c].to_numpy() for c in cols])


# --------------------------------------------------------------- analysis
if __name__ == "__main__":
    tt = pd.DatetimeIndex(W.ts)
    for w_ in (10, 30, 60):
        W[f"r{w_}"] = rain_window(tt, w_, 0)
    wt = 0.1 + W.stress.to_numpy()
    y = W.stress.to_numpy()

    hi = W[W.stress >= 0.6].copy()
    hi["local"] = hi.ts.dt.tz_convert(TZ).dt.strftime("%Y-%m-%d %H:%M")
    hi[["local", "dur", "stress", "inflow_gpm", "rain_10m", "rain_30m", "rain_60m",
        "api", "season", "bridge", "cycle_duty"]].to_csv(HERE / "high_stress_runs.csv", index=False)

    # lag search
    best = {}
    for win in (10, 30, 60):
        cand = []
        for lag in range(0, 65, 5):
            x = rain_window(tt, win, lag)
            _, r2 = wls(np.column_stack([np.ones(len(x)), x]), y, wt)
            cand.append((lag, r2))
        best[win] = max(cand, key=lambda c: c[1])

    beta, r2 = wls(design(W, ["r10", "api", "season"]), y, wt)
    beta_b, r2_b = wls(design(W, ["r10", "api", "season", "bridge"]), y, wt)

    # tail-only: does rain explain the runs that matter?
    tail = {}
    for thr in (0.0, 0.2, 0.3, 0.4):
        s = W[W.stress >= thr]
        if len(s) >= 20:
            _, rt = wls(design(s, ["r10", "api"]), s.stress.to_numpy(), np.ones(len(s)))
            tail[thr] = (len(s), rt)

    print(f"runs {len(W)} | stress>=0.6: {len(hi)} | max stress {W.stress.max():.3f} "
          f"= {W.stress.max()*QP:.1f} GPM")
    print(f"stress vs cycle_duty  r={W.stress.corr(W.cycle_duty):.4f}")
    print(f"best lags: {[(k, v[0]) for k, v in best.items()]}  wR2 {[round(v[1],4) for v in best.values()]}")
    print(f"model  stress = {beta[0]:.4f} + {beta[1]:.4f}*r10 + {beta[2]:.5f}*API "
          f"+ {beta[3]:.5f}*season   wR2={r2:.4f}")
    print(f"  + bridge term: wR2={r2_b:.4f}  (delta {r2_b-r2:+.4f}, beta {beta_b[4]:+.4f})")
    print("tail-only R2 (rain+API):", {k: round(v[1], 3) for k, v in tail.items()})
    print(f"wrote {HERE/'high_stress_runs.csv'}")
