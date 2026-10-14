#!/usr/bin/env python3
"""
Export the fitted models to docs/data/models.json so planner.html can run them
client-side, and the storm catalog to docs/data/storms_2025_26.json.

Everything the planner computes (Bridge Street peak and band, Ross equivalent,
sump stress, tier, checklist) comes from this file plus config.json. Re-run after
any refit; the page reads the file on load.

Fits reproduced here (all from events.csv, storms with >= 0.10 in and a Bridge
Street peak, n = 24, winter 2025-26):

  event_peak        peak_ft  = b0 + b1*total_rain_in + b2*api_at_start      (report sec. 4, R2 0.82)
  r6h_from_storm    peak_6h  = c0 + c1*total_rain_in + c2*peak_1h_in         (feeds the Layer 3A cross-check)
  r10_from_r1h      peak_10m = k * peak_1h_in                               (through origin)
  sump_inflow       peak_gpm = d0 + d1*peak_10min_in + d2*api_at_start       (15-min peak inflow)
  baseflow_to_api   api      = a + b*ln(ross_flow_cfs) on dry 10-min bins    (timeline.parquet)

The Ross->Bridge line and the Layer 3A rising-limb model are copied from
config.json, where they were written by creek_model.py.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ROOT / "docs" / "data"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = "America/Los_Angeles"

ev = pd.read_csv(HERE / "events.csv")
E = ev[(ev.total_rain_in >= 0.10) & ev.peak_bridge_stage_ft.notna()].copy()

# t for a 95% two-sided band, by residual degrees of freedom (no scipy needed)
T95 = {18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069}


def ols(df, cols, ykey, origin=False):
    X = np.column_stack([df[c].to_numpy(dtype=float) for c in cols])
    if not origin:
        X = np.column_stack([np.ones(len(df)), X])
    y = df[ykey].to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ beta
    res = y - pred
    k = X.shape[1]
    dof = len(y) - k
    sd = float(np.sqrt(np.sum(res ** 2) / dof))
    r2 = float(1 - np.sum(res ** 2) / np.sum((y - y.mean()) ** 2))
    t = T95.get(dof, 2.07)
    return {"beta": [round(float(b), 4) for b in beta], "cols": cols, "r2": round(r2, 4),
            "residual_sd": round(sd, 4), "n": int(len(y)), "dof": int(dof),
            "band_95": round(t * sd * math.sqrt(1 + 1 / len(y)), 3)}


def main():
    m = {}
    m["event_peak"] = ols(E, ["total_rain_in", "api_at_start"], "peak_bridge_stage_ft")
    m["event_peak"]["_note"] = ("Bridge Street crest from the storm total and the wetness index at onset. "
                                "Fitted on a winter whose highest crest was 7.38 ft; above that it is extrapolation.")
    m["r6h_from_storm"] = ols(E, ["total_rain_in", "peak_rain_1h_in"], "peak_rain_6h_in")
    m["r10_from_r1h"] = ols(E, ["peak_rain_1h_in"], "peak_rain_10min_in", origin=True)
    m["sump_inflow"] = ols(E, ["peak_rain_10min_in", "api_at_start"], "peak_inflow_gpm")
    m["sump_inflow"]["_note"] = ("Peak 15-minute inflow (GPM) from the heaviest 10 minutes of rain and wetness. "
                                 "Last winter's max was 42.65 GPM; the run-level model under-reads the worst "
                                 "single runs by about 2x (report sec. 3), so the planner shows a 2x tail band.")

    # Run-level stress model (stress_model.py), for the per-run story.
    try:
        b = np.load(HERE / "beta_r10_api_season.npy")
        m["sump_stress_run"] = {"beta": [round(float(x), 6) for x in b],
                                "cols": ["rain_10min_in", "api", "season_rain_in"],
                                "_note": "stress = 1 - 14/run_s = Qin/Qp, per run (stress_model.py, wR2 ~0.3)"}
    except Exception:  # noqa: BLE001
        pass

    # Baseflow -> wetness cross-check, from the 10-min timeline on dry bins.
    try:
        tl = pd.read_parquet(HERE / "timeline.parquet")
        dry = tl[(tl.rain_24h_in <= 0.0) & (tl.ross_flow_cfs > 0) & tl.api_0p9.notna()].copy()
        dry["ln_flow"] = np.log(dry.ross_flow_cfs)
        fit = ols(dry, ["ln_flow"], "api_0p9")
        m["baseflow_to_api"] = {"intercept": fit["beta"][0], "coef_ln_flow": fit["beta"][1],
                                "r2": fit["r2"], "residual_sd": fit["residual_sd"], "n": fit["n"],
                                "_note": "API implied by Ross baseflow on rain-free bins; a cross-check and "
                                         "the fallback when the station history is unavailable."}
    except Exception as e:  # noqa: BLE001
        m["baseflow_to_api_error"] = str(e)[:120]

    big = E[E.total_rain_in >= 0.5]
    lag = float(big.lag_rain_to_bridge_h.clip(lower=0).median()) if len(big) else 1.5
    m["timing"] = {"lag_peak_rain_to_crest_h": round(lag, 2),
                   "lag_rain_to_inflow_h": round(float(big.lag_rain_to_inflow_h.median()), 2),
                   "_note": "median across storms >= 0.5 in, heaviest hour of rain to Bridge Street crest / peak inflow"}

    m["pump"] = {"rated_gpm": CFG["primary_pump"]["rated_gpm"],
                 "min_run_s": 14.0,
                 "drawdown_gal": round(14.0 * CFG["primary_pump"]["rated_gpm"] / 60.0, 2),
                 "backup_gpm": CFG["backup_pump"]["rated_gpm"],
                 "_note": "run_s = V/(Qp-Qin); V = 14 s x Qp. Float travel is assumed, not measured (README)."}
    m["observed_maxima"] = {
        "bridge_peak_ft": float(E.peak_bridge_stage_ft.max()), "ross_peak_ft": float(E.peak_ross_stage_ft.max()),
        "total_rain_in": float(E.total_rain_in.max()), "peak_1h_in": float(E.peak_rain_1h_in.max()),
        "api_at_start": float(E.api_at_start.max()), "inflow_gpm_15min": float(E.peak_inflow_gpm.max()),
        "_note": "largest values the models have seen; beyond them the planner flags extrapolation",
    }
    for k in ("ross_to_bridge", "creek_predictor", "tiers", "qpf_bias", "wetness", "planner"):
        m[k] = CFG[k]
    m["town_stages"] = CFG["bridge_street"]["town_stages"]["lines"]
    m["bridge_flood_categories"] = {"action": 11.3, "minor": 13.3, "moderate": 16.3, "major": 17.8}
    m["generated"] = pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    m["_source"] = "analysis/export_models.py"
    (DOCS / "models.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    print("models.json:", {k: v.get("r2") for k, v in m.items() if isinstance(v, dict) and "r2" in v})

    # ---- storm catalog, last winter --------------------------------------
    fc = None
    fcp = HERE / "cache" / "fcst_daily.csv"
    if fcp.exists():
        fc = pd.read_csv(fcp, parse_dates=["time"]).set_index("time")
    b = m["event_peak"]["beta"]
    rows = []
    for _, e in E.sort_values("start").iterrows():
        s = pd.to_datetime(e.start, utc=True).tz_convert(TZ)
        en = pd.to_datetime(e.end, utc=True).tz_convert(TZ)
        f1 = f3 = None
        if fc is not None:
            seg = fc.loc[s.normalize().tz_localize(None):en.normalize().tz_localize(None)]
            if len(seg):
                f1 = round(float(seg.precipitation_previous_day1.sum()), 2)
                f3 = round(float(seg.precipitation_previous_day3.sum()), 2)
        pred = b[0] + b[1] * e.total_rain_in + b[2] * e.api_at_start
        rows.append({
            "id": f"2025-26-{int(e.event):02d}", "season": "2025-26",
            "start_local": s.strftime("%Y-%m-%dT%H:%M"), "end_local": en.strftime("%Y-%m-%dT%H:%M"),
            "duration_h": e.duration_h,
            "forecast": {"day_ahead_total_in": f1, "three_day_total_in": f3,
                         "_note": "Open-Meteo previous-run archive, summed over the storm's local days"},
            "predicted": {"bridge_peak_ft": round(float(pred), 2), "kind": "hindcast",
                          "_note": "event_peak model applied to the ACTUAL total and API"},
            "actual": {"total_rain_in": e.total_rain_in, "peak_1h_in": e.peak_rain_1h_in,
                       "peak_10min_in": e.peak_rain_10min_in, "peak_30min_in": e.peak_rain_30min_in,
                       "peak_6h_in": e.peak_rain_6h_in, "api_at_start": e.api_at_start,
                       "bridge_peak_ft": e.peak_bridge_stage_ft, "ross_peak_ft": e.peak_ross_stage_ft,
                       "ross_peak_cfs": e.peak_ross_flow_cfs,
                       "sump_peak_inflow_gpm": e.peak_inflow_gpm, "sump_runs": int(e.pump_runs),
                       "sump_gallons": e.total_gallons},
            "notes": "",
        })
    (DOCS / "storms_2025_26.json").write_text(json.dumps({"storms": rows}, indent=1), encoding="utf-8")
    print(f"storms_2025_26.json: {len(rows)} storms")


if __name__ == "__main__":
    main()
