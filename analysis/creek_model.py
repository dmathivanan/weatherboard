#!/usr/bin/env python3
"""
Layer 3 — creek model for San Anselmo Creek at Bridge Street.

Three models, deliberately in increasing order of usefulness:
  A  stage from rolling rain + wetness, rising limb only (the causal story)
  B  event peak from event total  (the simple story, from events.csv)
  C  stage from Ross stage        (the operational story - Ross is real-time)

Then the earliest-warning question: does the first 3 hours of rise rate predict
where the event ends up?

Rising limb only, per the brief: a falling gauge is draining, and including
recession would fit the wrong process.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = json.loads((ROOT / "config.json").read_text())
TOWN = {s["stage"]: s for s in CFG["bridge_street"]["town_stages"]["lines"]}
NOTIFY_FT, FLOOD_FT = 6.5, 13.0            # town stage 2, and water at 730 SA Ave

tl = pd.read_parquet(HERE / "timeline.parquet")
tl.index = pd.to_datetime(tl.index, utc=True)
ev = pd.read_csv(HERE / "events.csv")


def wls(X, y, w=None):
    w = np.ones(len(y)) if w is None else w
    sw = np.sqrt(w)
    beta, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
    pred = X @ beta
    ybar = np.average(y, weights=w)
    r2 = 1 - np.sum(w * (y - pred) ** 2) / np.sum(w * (y - ybar) ** 2)
    return beta, r2, pred


def design(df, cols):
    return np.column_stack([np.ones(len(df))] + [df[c].to_numpy() for c in cols])


# ---------------------------------------------------------------- A: stage model
def rolling_rain_lagged(win_h, lag_h):
    """Rain total over win_h ending lag_h before each grid point."""
    n = int(win_h * 6)
    s = tl["rain_in"].rolling(n, min_periods=1).sum()
    return s.shift(int(lag_h * 6))


def model_a():
    obs = tl["bridge_stage_obs_max"].dropna()
    # rising limb: stage strictly above the previous observation
    rising = obs.diff() > 0
    idx = obs.index[rising]
    print("=" * 78)
    print("A. BRIDGE STREET STAGE vs ROLLING RAIN  (observed readings, rising limb only)")
    print("=" * 78)
    print(f"  observed readings in window : {len(obs)}")
    print(f"  on a rising limb            : {len(idx)}  ({100*len(idx)/len(obs):.0f}%)")

    y = obs.loc[idx].to_numpy()
    best = {}
    print(f"\n  {'window':>8}  {'best lag':>9}  {'R2':>7}     lag profile (R2 by lag h)")
    for win in (3, 6, 12, 24, 48):
        prof = []
        for lag in (0, 1, 2, 3, 4, 6, 8, 12):
            x = rolling_rain_lagged(win, lag).reindex(idx).to_numpy()
            ok = ~np.isnan(x)
            if ok.sum() < 20:
                continue
            _, r2, _ = wls(np.column_stack([np.ones(ok.sum()), x[ok]]), y[ok])
            prof.append((lag, r2))
        lag, r2 = max(prof, key=lambda p: p[1])
        best[win] = (lag, r2)
        pr = " ".join(f"{l}h:{r:.2f}" for l, r in prof)
        print(f"  {win:>6}h  {lag:>8}h  {r2:>7.4f}     {pr}")

    # combined model at each window's best lag
    feat = pd.DataFrame(index=idx)
    for win in (3, 6, 12, 24, 48):
        feat[f"r{win}h"] = rolling_rain_lagged(win, best[win][0]).reindex(idx)
    feat["api"] = tl["api_0p9"].reindex(idx)
    feat["season"] = tl["season_rain_in"].reindex(idx)
    feat["y"] = y
    feat = feat.dropna()

    print(f"\n  combined models (n={len(feat)}):")
    for cols in (["r24h"], ["r24h", "r48h"], ["r6h", "r24h"], ["r6h", "r24h", "r48h"],
                 ["r3h", "r6h", "r12h", "r24h", "r48h"],
                 ["r6h", "r24h", "r48h", "api"],
                 ["r6h", "r24h", "r48h", "api", "season"]):
        X = design(feat, cols)
        beta, r2, _ = wls(X, feat["y"].to_numpy())
        sd = np.array([feat[c].std() for c in cols])
        std_b = beta[1:] * sd / feat["y"].std()
        terms = "  ".join(f"{c}:{b:+.3f}({sb:+.2f})" for c, b, sb in zip(cols, beta[1:], std_b))
        print(f"    R2={r2:.4f}  {terms}")
    print("    (coefficient shown as beta(standardized))")
    return feat


# ---------------------------------------------------------------- B: event peaks
def model_b():
    print()
    print("=" * 78)
    print("B. EVENT PEAK STAGE vs EVENT TOTALS  (from events.csv)")
    print("=" * 78)
    e = ev.dropna(subset=["peak_bridge_stage_ft"]).copy()
    e = e[e.total_rain_in >= 0.10]
    y = e["peak_bridge_stage_ft"].to_numpy()
    for cols in (["total_rain_in"], ["peak_rain_24h_in"], ["total_rain_in", "api_at_start"],
                 ["total_rain_in", "peak_rain_6h_in", "api_at_start"],
                 ["total_rain_in", "peak_rain_24h_in", "api_at_start", "season_rain_at_start_in"]):
        X = design(e, cols)
        beta, r2, _ = wls(X, y)
        sd = np.array([e[c].std() for c in cols])
        std_b = beta[1:] * sd / y.std()
        terms = "  ".join(f"{c}:{b:+.3f}({sb:+.2f})" for c, b, sb in zip(cols, beta[1:], std_b))
        print(f"    n={len(e)}  R2={r2:.4f}  {terms}")
    X = design(e, ["total_rain_in", "api_at_start"])
    beta, r2, pred = wls(X, y)
    print(f"\n  chosen: peak_ft = {beta[0]:.3f} + {beta[1]:.3f}*total_rain + {beta[2]:.3f}*API"
          f"   R2={r2:.4f}  residual sd={np.std(y-pred):.3f} ft")
    return e, beta, r2, np.std(y - pred)


# ---------------------------------------------------------------- C: Ross -> Bridge
def model_c():
    print()
    print("=" * 78)
    print("C. ROSS -> BRIDGE STREET, WITH CONFIDENCE BANDS")
    print("=" * 78)
    e = ev.dropna(subset=["peak_bridge_stage_ft", "peak_ross_stage_ft"])
    e = e[e.total_rain_in >= 0.10]
    x, y = e["peak_ross_stage_ft"].to_numpy(), e["peak_bridge_stage_ft"].to_numpy()
    n = len(x)
    X = np.column_stack([np.ones(n), x])
    beta, r2, pred = wls(X, y)
    resid = y - pred
    dof = n - 2
    s2 = np.sum(resid ** 2) / dof
    xbar, sxx = x.mean(), np.sum((x - x.mean()) ** 2)
    # t for 95% at dof; table value, no scipy
    tcrit = {20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060}.get(dof, 2.07)
    print(f"  bridge = {beta[1]:.4f}*ross {beta[0]:+.4f}   n={n}  R2={r2:.4f}  "
          f"resid sd={np.sqrt(s2):.4f} ft")
    print(f"  observed Ross range: {x.min():.2f} - {x.max():.2f} ft   "
          f"Bridge range: {y.min():.2f} - {y.max():.2f} ft")

    def invert(target_bridge):
        ross = (target_bridge - beta[0]) / beta[1]
        # prediction interval on bridge at that ross, mapped back to a ross range
        se_pred = np.sqrt(s2 * (1 + 1 / n + (ross - xbar) ** 2 / sxx))
        lo = (target_bridge - beta[0] - tcrit * se_pred) / beta[1]
        hi = (target_bridge - beta[0] + tcrit * se_pred) / beta[1]
        return ross, lo, hi, se_pred

    print(f"\n  {'town line':<28}{'Ross ft':>9}{'95% pred band':>22}{'extrapolation':>16}")
    for label, tgt in (("stage 2 - notify public", NOTIFY_FT),
                       ("stage 5 - flood horn", FLOOD_FT),
                       ("NWS minor flood (13.3)", 13.3)):
        ross, lo, hi, se = invert(tgt)
        beyond = ross / x.max()
        note = "within data" if ross <= x.max() else f"{beyond:.2f}x past max"
        print(f"  {label:<28}{ross:>9.2f}{f'{lo:.2f} - {hi:.2f}':>22}{note:>16}")
    print(f"\n  Bridge peak actually observed: {y.max():.2f} ft at Ross {x[np.argmax(y)]:.2f} ft")
    return beta, s2, x, y


# ---------------------------------------------------------------- D: early warning
def model_d():
    print()
    print("=" * 78)
    print("D. DOES THE FIRST 3 HOURS OF RISE PREDICT THE PEAK?")
    print("=" * 78)
    obs = tl["bridge_stage_obs_max"].dropna()
    rows = []
    for _, r in ev.iterrows():
        if pd.isna(r.peak_bridge_stage_ft) or r.total_rain_in < 0.10:
            continue
        s = pd.Timestamp(r.start, tz="UTC") if not isinstance(r.start, pd.Timestamp) else r.start
        s = pd.to_datetime(r.start, utc=True)
        w = obs.loc[s:s + pd.Timedelta(hours=3)]
        if len(w) < 2:
            continue
        dt = (w.index[-1] - w.index[0]).total_seconds() / 3600
        if dt < 0.5:
            continue
        rise = (w.iloc[-1] - w.iloc[0]) / dt
        base = w.iloc[0]
        rows.append(dict(event=int(r.event), start_local=r.start_local, n_obs=len(w),
                         base_ft=round(base, 2), rise_ft_h=round(rise, 3),
                         peak_ft=r.peak_bridge_stage_ft,
                         rise_to_peak_ft=round(r.peak_bridge_stage_ft - base, 2),
                         total_rain_in=r.total_rain_in))
    d = pd.DataFrame(rows)
    print(f"  events with >=2 observations in the first 3 h: {len(d)}")
    for cols in (["rise_ft_h"], ["rise_ft_h", "base_ft"], ["rise_ft_h", "base_ft", "total_rain_in"]):
        X = design(d, cols)
        beta, r2, _ = wls(X, d["peak_ft"].to_numpy())
        terms = "  ".join(f"{c}:{b:+.3f}" for c, b in zip(cols, beta[1:]))
        print(f"    peak_ft ~ {str(cols):<44} R2={r2:.4f}   {terms}")
    print(f"\n  corr(rise_ft_h, peak_ft)      = {d.rise_ft_h.corr(d.peak_ft):.4f}")
    print(f"  corr(base_ft,  peak_ft)       = {d.base_ft.corr(d.peak_ft):.4f}")
    print(f"  corr(rise, peak MINUS base)   = {d.rise_ft_h.corr(d.rise_to_peak_ft):.4f}")
    d = d.sort_values("peak_ft", ascending=False)
    print()
    print(d.to_string(index=False))
    d.to_csv(HERE / "early_warning.csv", index=False)
    return d


if __name__ == "__main__":
    model_a()
    model_b()
    model_c()
    model_d()
