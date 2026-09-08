#!/usr/bin/env python3
"""
Layer 4 — forecast skill, and a replay of the config.json tier rules.

Forecast source is Open-Meteo's previous-runs archive: precipitation_previous_dayN
at date X is what the model run from X-N days predicted for X. So a 24 h
day-ahead forecast for day D is previous_day1 at D, and the 72 h forecast issued
at D-1 is previous_day1[D] + previous_day2[D+1] + previous_day3[D+2] - all three
drawn from the same D-1 run.

Truth is the STATION, not the model's own analysis: daily totals come from the
Ambient counter reconstruction in timeline.parquet, on local days.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TZ = "America/Los_Angeles"
CFG = json.loads((ROOT / "config.json").read_text())
TH = CFG["thresholds"]

tl = pd.read_parquet(HERE / "timeline.parquet")
tl.index = pd.to_datetime(tl.index, utc=True)
ev = pd.read_csv(HERE / "events.csv")
fc = pd.read_csv(HERE / "cache" / "fcst_daily.csv", parse_dates=["time"]).set_index("time")

# station truth, local days
obs_daily = (tl["rain_in"].tz_convert(TZ).resample("D").sum()
             .rename("obs_in"))
obs_daily.index = obs_daily.index.tz_localize(None)

d = fc.join(obs_daily, how="inner")
d["f24_lead1"] = d["precipitation_previous_day1"]
d["f_lead3"] = d["precipitation_previous_day3"]
# 72 h total issued the day before, from that single run
d["obs72"] = d["obs_in"].rolling(3).sum().shift(-2)
d["f72_lead1"] = (d["precipitation_previous_day1"]
                  + d["precipitation_previous_day2"].shift(-1)
                  + d["precipitation_previous_day3"].shift(-2))
win = d.loc["2025-11-01":"2026-04-30"]


def skill(o, f, label):
    m = o.notna() & f.notna()
    o, f = o[m], f[m]
    bias = (f - o).mean()
    pct = 100 * f.sum() / o.sum() if o.sum() else np.nan
    rmse = np.sqrt(((f - o) ** 2).mean())
    mae = (f - o).abs().mean()
    r = np.corrcoef(o, f)[0, 1] if len(o) > 2 else np.nan
    print(f"  {label:<34} n={len(o):3}  bias={bias:+.3f} in/day  "
          f"total={pct:5.1f}% of obs  MAE={mae:.3f}  RMSE={rmse:.3f}  r={r:.3f}")
    return dict(bias=bias, pct=pct, rmse=rmse, mae=mae, r=r)


def main():
    print("=" * 84)
    print("A. DAILY FORECAST SKILL vs STATION  (Nov 2025 - Apr 2026)")
    print("=" * 84)
    skill(win["obs_in"], win["f24_lead1"], "24 h total, 1-day lead, all days")
    skill(win["obs_in"], win["f_lead3"], "24 h total, 3-day lead, all days")
    wet = win[win["obs_in"] >= 0.10]
    skill(wet["obs_in"], wet["f24_lead1"], "24 h, 1-day lead, wet days only")
    skill(wet["obs_in"], wet["f_lead3"], "24 h, 3-day lead, wet days only")
    print()
    skill(win["obs72"], win["f72_lead1"], "72 h total, issued 1 day ahead")
    w72 = win[win["obs72"] >= 0.25]
    skill(w72["obs72"], w72["f72_lead1"], "72 h, issued 1 day ahead, wet only")

    print()
    print("  wettest 10 station days, forecast vs observed:")
    top = win.nlargest(10, "obs_in")[["obs_in", "f24_lead1", "f_lead3"]]
    print(f"    {'date':<12}{'obs_in':>8}{'f 1-day':>9}{'ratio':>8}{'f 3-day':>9}{'ratio':>8}")
    for t, r in top.iterrows():
        print(f"    {t:%Y-%m-%d}{r.obs_in:>8.2f}{r.f24_lead1:>9.2f}"
              f"{r.f24_lead1/r.obs_in:>8.2f}{r.f_lead3:>9.2f}{r.f_lead3/r.obs_in:>8.2f}")

    print()
    print("=" * 84)
    print("B. THE FIVE WETTEST EVENTS")
    print("=" * 84)
    top5 = ev.nlargest(5, "total_rain_in")
    print(f"  {'#':>3} {'start (local)':<17}{'obs_in':>8}{'f24 lead1':>11}{'f lead3':>9}"
          f"{'ratio1':>8}{'ratio3':>8}{'peak BSt':>10}")
    rows = []
    for _, e in top5.iterrows():
        s = pd.to_datetime(e.start, utc=True).tz_convert(TZ).normalize().tz_localize(None)
        en = pd.to_datetime(e.end, utc=True).tz_convert(TZ).normalize().tz_localize(None)
        seg = d.loc[s:en]
        o, f1, f3 = seg.obs_in.sum(), seg.f24_lead1.sum(), seg.f_lead3.sum()
        rows.append((o, f1, f3))
        print(f"  {int(e.event):>3} {e.start_local:<17}{o:>8.2f}{f1:>11.2f}{f3:>9.2f}"
              f"{f1/o:>8.2f}{f3/o:>8.2f}{e.peak_bridge_stage_ft:>10.2f}")
    O = sum(r[0] for r in rows); F1 = sum(r[1] for r in rows); F3 = sum(r[2] for r in rows)
    print(f"  {'':>3} {'TOTAL':<17}{O:>8.2f}{F1:>11.2f}{F3:>9.2f}{F1/O:>8.2f}{F3/O:>8.2f}")
    print(f"\n  the five wettest events were under-forecast by "
          f"{100*(1-F1/O):.0f}% at 1-day lead and {100*(1-F3/O):.0f}% at 3-day lead")

    print()
    print("=" * 84)
    print("C. TIER REPLAY through config.json thresholds")
    print("=" * 84)
    print(f"  act_rain_rate_inhr {TH['act_rain_rate_inhr']}  act_rain_1h_in {TH['act_rain_1h_in']}  "
          f"prepare_qpf_24h_in {TH['prepare_qpf_24h_in']}  watch_qpf_72h_in {TH['watch_qpf_72h_in']}")
    print(f"  sump_prepare_duty_pct {TH['sump_prepare_duty_pct']}  sump_act_duty_pct {TH['sump_act_duty_pct']}")

    # forecast columns broadcast onto the 10-min grid by local day
    g = tl.copy()
    g["day"] = g.index.tz_convert(TZ).normalize().tz_localize(None)
    q24 = d["f24_lead1"].reindex(g["day"]).to_numpy()
    q72 = d["f72_lead1"].reindex(g["day"]).to_numpy()
    g["qpf24"], g["qpf72"] = q24, q72

    # Exact 15-min duty from the raw runs, then mapped onto the 10-min grid.
    # A 2-bin (20 min) proxy dilutes the peak and would wrongly report that the
    # 50% prepare threshold was never crossed.
    pf = pd.read_csv(ROOT / "imports" / "PumpFuse.csv", sep="	")
    pf["ts"] = (pd.to_datetime(pf["Time"])
                .dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
                .dt.tz_convert("UTC"))
    pf = pf.sort_values("ts").set_index("ts")
    minute = pd.date_range(g.index.min(), g.index.max() + pd.Timedelta(minutes=9),
                           freq="1min", tz="UTC")
    rs = pf["Duration"].resample("1min").sum().reindex(minute).fillna(0.0)
    duty15 = rs.rolling(15, min_periods=1).sum() / (15 * 60) * 100
    g["duty"] = duty15.resample("10min").max().reindex(g.index)
    print(f"\n  exact peak 15-min sump duty in window: {g['duty'].max():.1f}%  "
          f"(prepare threshold {TH['sump_prepare_duty_pct']}, act {TH['sump_act_duty_pct']})")

    fired = {}
    def firing(mask, name, level):
        idx = g.index[mask.fillna(False)]
        fired[name] = (level, idx)

    firing(g["rain_rate_inhr"] >= TH["act_rain_rate_inhr"], "act:rain_rate", 3)
    firing(g["rain_1h_in"] >= TH["act_rain_1h_in"], "act:rain_1h", 3)
    firing(g["duty"] >= TH["sump_act_duty_pct"], "act:sump_duty", 3)
    firing(g["duty"] >= TH["sump_prepare_duty_pct"], "prepare:sump_duty", 2)
    firing(g["qpf24"] >= TH["prepare_qpf_24h_in"], "prepare:qpf24", 2)
    firing(g["qpf72"] >= TH["watch_qpf_72h_in"], "watch:qpf72", 1)

    print(f"\n  {'rule':<22}{'tier':>7}{'bins fired':>12}{'distinct days':>15}")
    for k, (lvl, idx) in fired.items():
        days = len(pd.Series(idx).dt.tz_convert(TZ).dt.normalize().unique()) if len(idx) else 0
        print(f"  {k:<22}{['quiet','watch','prepare','act'][lvl]:>7}{len(idx):>12}{days:>15}")

    print()
    print("  per storm: earliest tier trigger vs time of peak Bridge Street stage")
    print(f"  {'#':>3} {'start (local)':<17}{'rain':>6}{'peak':>6}  "
          f"{'tier fired':<20}{'lead h':>8}  {'rule':<20}")
    obs = tl["bridge_stage_obs_max"].dropna()
    big = ev[ev.total_rain_in >= 0.5].sort_values("peak_bridge_stage_ft", ascending=False)
    for _, e in big.iterrows():
        s = pd.to_datetime(e.start, utc=True)
        end = pd.to_datetime(e.end, utc=True) + pd.Timedelta(hours=12)
        seg = obs.loc[s:end]
        if seg.empty:
            continue
        tpeak = seg.idxmax()
        best = None
        for k, (lvl, idx) in fired.items():
            sel = idx[(idx >= s - pd.Timedelta(hours=24)) & (idx <= tpeak)]
            if len(sel):
                lead = (tpeak - sel[0]).total_seconds() / 3600
                cand = (lvl, lead, k, sel[0])
                if best is None or (cand[0] > best[0]) or (cand[0] == best[0] and cand[1] > best[1]):
                    best = cand
        if best:
            print(f"  {int(e.event):>3} {e.start_local:<17}{e.total_rain_in:>6.2f}"
                  f"{e.peak_bridge_stage_ft:>6.2f}  "
                  f"{['quiet','watch','prepare','act'][best[0]]:<20}{best[1]:>8.1f}  {best[2]:<20}")
        else:
            print(f"  {int(e.event):>3} {e.start_local:<17}{e.total_rain_in:>6.2f}"
                  f"{e.peak_bridge_stage_ft:>6.2f}  {'NOTHING FIRED':<20}{'-':>8}  {'-':<20}")


if __name__ == "__main__":
    main()
