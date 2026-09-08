#!/usr/bin/env python3
"""
Layer 5 (2) — replay winter 2025-26 through the rebuilt config.json tiers.

Reports, per storm, the highest tier reached and the lead time from each tier's
first trigger to the observed Bridge Street peak. Also counts false alarms:
tier firings on days that never produced a meaningful creek response.

Two rules cannot be replayed and are reported as such rather than silently
skipped: CW3E AR category and NWS Flood Watch have no archive here, and the
backup pump was never instrumented (Layer 2: all 3,674 exported runs sit in a
single power mode matching the primary).
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TZ = "America/Los_Angeles"
CFG = json.loads((ROOT / "config.json").read_text())
T = CFG["tiers"]
BIAS = CFG["qpf_bias"]
CP = CFG["creek_predictor"]

TIERS = ["quiet", "watch", "prepare", "act", "emergency"]

tl = pd.read_parquet(HERE / "timeline.parquet")
tl.index = pd.to_datetime(tl.index, utc=True)
ev = pd.read_csv(HERE / "events.csv")
fc = pd.read_csv(HERE / "cache" / "fcst_daily.csv", parse_dates=["time"]).set_index("time")

g = tl.copy()
g["day"] = g.index.tz_convert(TZ).normalize().tz_localize(None)

# bias-corrected forecast, broadcast by local day
f24 = fc["precipitation_previous_day1"]
f72 = (fc["precipitation_previous_day1"]
       + fc["precipitation_previous_day2"].shift(-1)
       + fc["precipitation_previous_day3"].shift(-2))
g["qpf24"] = f24.reindex(g["day"]).to_numpy() * BIAS["lead_24h"]
g["qpf72"] = f72.reindex(g["day"]).to_numpy() * BIAS["lead_72h"]
# next-6h QPF: the day's forecast spread evenly, bias-corrected
g["qpf6"] = g["qpf24"] / 4.0

# Layer 3A live predictor: rain already fallen + rain still expected
g["r6h_eff"] = g["rain_6h_in"] + g["qpf6"]
g["pred_bridge"] = (CP["intercept"] + CP["coef_r6h_in"] * g["r6h_eff"]
                    + CP["coef_api"] * g["api_0p9"])

# --- pump runs at 1-minute resolution
pf = pd.read_csv(ROOT / "imports" / "PumpFuse.csv", sep="\t")
pf["ts"] = (pd.to_datetime(pf["Time"])
            .dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
            .dt.tz_convert("UTC"))
pf = pf.sort_values("ts").reset_index(drop=True)
pf["dur"] = pf["Duration"].astype(float)
pf = pf[(pf.ts >= g.index.min()) & (pf.ts <= g.index.max())].reset_index(drop=True)
# three consecutive runs of increasing duration
inc = (pf["dur"].diff() > 0) & (pf["dur"].diff().shift(1) > 0)
# The bare rule fires on trivial 16->17->18 s ripples; require the third run to
# be long enough to mean something (Layer 2: median run is 16 s).
pf["esc"] = inc & (pf["dur"] >= T["act"]["sump_increasing_min_final_s"])

def run_flag(mask, label):
    """Map a per-run boolean onto the 10-min grid."""
    s = pd.Series(False, index=g.index)
    for t in pf.loc[mask, "ts"]:
        b = g.index[g.index.get_indexer([t], method="ffill")]
        if len(b):
            s.loc[b[0]] = True
    return s

rules = {}
rules["prepare:sump_run>=35s"] = (2, run_flag(pf.dur >= T["prepare"]["sump_run_s"], ""))
rules["act:sump_run>=93s"] = (3, run_flag(pf.dur >= T["act"]["sump_run_s"], ""))
rules["act:3 rising runs >=25s"] = (3, run_flag(pf.esc, ""))
rules["emergency:run>=180s"] = (4, run_flag(pf.dur >= T["emergency"]["primary_run_no_stop_s"], ""))

rules["watch:qpf72"] = (1, g["qpf72"] >= T["watch"]["qpf_72h_corrected_in"])
rules["prepare:qpf24"] = (2, g["qpf24"] >= T["prepare"]["qpf_24h_corrected_in"])
rules["prepare:bridge>=3 + rain"] = (2, (g["bridge_stage_ft"] >= T["prepare"]["bridge_ft_with_rain"]["bridge_ft"])
                                     & (g["qpf24"] >= T["prepare"]["bridge_ft_with_rain"]["qpf_24h_corrected_in"]))
rules["prepare:ross>=9"] = (2, g["ross_stage_ft"] >= T["prepare"]["ross_ft"])
rules["act:ross>=11"] = (3, g["ross_stage_ft"] >= T["act"]["ross_ft"])
rules["act:pred_bridge>=6.5"] = (3, g["pred_bridge"] >= T["act"]["predicted_bridge_ft"])
rules["emergency:ross>=15"] = (4, g["ross_stage_ft"] >= T["emergency"]["ross_ft"])

UNREPLAYABLE = ["watch:CW3E AR category (no archive)",
                "watch:NWS Flood Watch (no archive)",
                "emergency:backup pump start (never instrumented)"]


def main():
    print("=" * 96)
    print("REPLAY OF THE REBUILT TIERS  —  winter 2025-26")
    print("=" * 96)
    print(f"  QPF bias correction applied: 24h x{BIAS['lead_24h']}, 72h x{BIAS['lead_72h']}")
    print(f"  creek predictor: {CP['intercept']} + {CP['coef_r6h_in']}*r6h_eff + {CP['coef_api']}*API "
          f"(+/-{CP['band_95_ft']} ft 95%)")
    print(f"\n  {'rule':<30}{'tier':>11}{'bins':>8}{'days':>7}")
    for k, (lvl, m) in rules.items():
        m = m.fillna(False)
        days = len(pd.Series(g.index[m]).dt.tz_convert(TZ).dt.normalize().unique()) if m.any() else 0
        print(f"  {k:<30}{TIERS[lvl]:>11}{int(m.sum()):>8}{days:>7}")
    for u in UNREPLAYABLE:
        print(f"  {u:<30}{'-':>11}{'n/a':>8}{'n/a':>7}")

    obs = tl["bridge_stage_obs_max"].dropna()
    big = ev[ev.total_rain_in >= 0.5].sort_values("peak_bridge_stage_ft", ascending=False)

    print()
    print("  PER STORM — highest tier reached, and lead time to the Bridge Street peak")
    print(f"  {'#':>3} {'start (local)':<17}{'rain':>6}{'peak':>6}  {'highest':>10}"
          f"{'lead h':>8}  {'first trigger':<30}{'watch':>7}{'prep':>7}{'act':>7}")
    out = []
    for _, e in big.iterrows():
        s = pd.to_datetime(e.start, utc=True)
        end = pd.to_datetime(e.end, utc=True) + pd.Timedelta(hours=12)
        seg = obs.loc[s:end]
        if seg.empty:
            continue
        tpeak = seg.idxmax()
        lo = s - pd.Timedelta(hours=24)
        per_tier = {}
        best = None
        for k, (lvl, m) in rules.items():
            m = m.fillna(False)
            idx = g.index[m]
            sel = idx[(idx >= lo) & (idx <= tpeak)]
            if len(sel):
                lead = (tpeak - sel[0]).total_seconds() / 3600
                per_tier.setdefault(lvl, []).append(lead)
                if best is None or lvl > best[0] or (lvl == best[0] and lead > best[1]):
                    best = (lvl, lead, k)
        w = max(per_tier.get(1, []), default=np.nan)
        p = max(per_tier.get(2, []), default=np.nan)
        a = max(per_tier.get(3, []), default=np.nan)
        if best:
            print(f"  {int(e.event):>3} {e.start_local:<17}{e.total_rain_in:>6.2f}"
                  f"{e.peak_bridge_stage_ft:>6.2f}  {TIERS[best[0]]:>10}{best[1]:>8.1f}  {best[2]:<30}"
                  f"{w:>7.1f}{p:>7.1f}{a:>7.1f}".replace("nan", "  -"))
        else:
            print(f"  {int(e.event):>3} {e.start_local:<17}{e.total_rain_in:>6.2f}"
                  f"{e.peak_bridge_stage_ft:>6.2f}  {'NOTHING':>10}{'-':>8}  {'-':<30}")
        out.append(dict(event=int(e.event), start=e.start_local, rain=e.total_rain_in,
                        peak_ft=e.peak_bridge_stage_ft,
                        highest=TIERS[best[0]] if best else "none",
                        lead_h=round(best[1], 1) if best else None,
                        watch_lead_h=None if np.isnan(w) else round(w, 1),
                        prepare_lead_h=None if np.isnan(p) else round(p, 1),
                        act_lead_h=None if np.isnan(a) else round(a, 1)))
    df = pd.DataFrame(out)
    df.to_csv(HERE / "replay_new_rules.csv", index=False)

    print()
    print("  coverage: %d of %d storms reached at least Prepare, %d reached Act"
          % ((df.highest.isin(["prepare", "act", "emergency"])).sum(), len(df),
             (df.highest.isin(["act", "emergency"])).sum()))
    print("  storms with no tier at all: %d" % (df.highest == "none").sum())

    # false alarms: tier days with no creek response
    print()
    print("  FALSE ALARMS  (tier fired on a day whose max observed Bridge St stayed low)")
    daily_peak = obs.tz_convert(TZ).resample("D").max()
    for lvl, name in ((2, "prepare"), (3, "act")):
        idx = pd.DatetimeIndex([])
        for k, (l, m) in rules.items():
            if l == lvl:
                idx = idx.union(g.index[m.fillna(False)])
        if len(idx) == 0:
            print(f"    {name:<9} never fired"); continue
        days = pd.DatetimeIndex(idx).tz_convert(TZ).normalize().unique()
        hits, quiet_days = 0, []
        for d in days:
            pk = daily_peak.get(d, np.nan)
            if pd.isna(pk):
                continue
            thresh = 3.0 if lvl == 2 else 4.5
            if pk >= thresh:
                hits += 1
            else:
                quiet_days.append((d.date(), round(float(pk), 2)))
        tot = hits + len(quiet_days)
        print(f"    {name:<9} fired on {tot:3d} days | {hits:3d} had Bridge St >= "
              f"{3.0 if lvl==2 else 4.5} ft | {len(quiet_days):3d} did not "
              f"({100*len(quiet_days)/tot if tot else 0:.0f}% false)")
        if quiet_days:
            print("               quiet days:", ", ".join(f"{d} ({p} ft)" for d, p in quiet_days[:8]))


if __name__ == "__main__":
    main()
