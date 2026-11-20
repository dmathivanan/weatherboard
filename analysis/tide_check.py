#!/usr/bin/env python3
"""
Does the Bay tide move the creek? (It does not, at Ross or Bridge Street.)

    python analysis/tide_check.py            # run the three checks on the cached tide record
    python analysis/tide_check.py --fetch    # refresh the tide record from NOAA first

The tide is San Francisco (NOAA 9414290), hourly, feet above MLLW: observed
where NOAA has verified it, predicted for the most recent weeks. It is cached
in analysis/cache/tide_sf_hourly.csv. The creek records are the tracked files
storm.py reads.

  A. Dry weather: does Ross stage follow the tide at all?
  B. The highest tides on record: what Ross did then.
  C. Storm crests: does the tide at the crest explain the crest model's error,
     or make Ross read high for its Bridge Street crest (backwater)?

Result, Oct 2025 - Nov 2026: A is 0.004 in of Ross per ft of tide; in B Ross sat
flat through the Oct 2026 king tides; C shows no backwater at Ross and only a
borderline, fragile correlation with the model error. So the tide is shown on
the planner for the bayside, and kept out of the crest model and the tiers.

Host needed for --fetch: api.tidesandcurrents.noaa.gov
"""
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fetchers as F          # noqa: E402
import storm                  # noqa: E402

CACHE = HERE / "cache" / "tide_sf_hourly.csv"
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
TZ = storm.TZ


def fetch():
    """Hourly observed and predicted water level since the start of the creek record.
    NOAA serves at most a year of hourly data per request."""
    cfg = F.CFG["tides"]
    start, end = storm.SINCE.tz_convert(None).normalize(), pd.Timestamp.now("UTC").tz_convert(None).normalize()
    cols = {}
    for name, extra, key in (("observed_ft", {"product": "hourly_height"}, "data"),
                             ("predicted_ft", {"product": "predictions", "interval": "h"}, "predictions")):
        rows, a = {}, start
        while a <= end:
            b = min(a + pd.Timedelta(days=360), end)
            r = requests.get(API, headers=F.UA, timeout=90, params={
                **extra, "station": cfg["station"], "datum": cfg.get("datum", "MLLW"), "time_zone": "gmt",
                "units": "english", "format": "json", "application": "floodboard",
                "begin_date": a.strftime("%Y%m%d"), "end_date": b.strftime("%Y%m%d")})
            r.raise_for_status()
            for x in r.json().get(key, []):     # the newest weeks have predictions but no verified heights yet
                try:
                    rows[x["t"]] = float(x["v"])
                except (KeyError, TypeError, ValueError):
                    pass
            a = b + pd.Timedelta(days=1)
        cols[name] = pd.Series(rows)
    df = pd.DataFrame(cols).sort_index().round(3)
    df.index.name = "time_utc"
    df.to_csv(CACHE)
    print(f"tide record: {len(df)} hours, {df.index[0]} -> {df.index[-1]} UTC -> {CACHE.name}")


def main(argv):
    warnings.filterwarnings("ignore")
    if "--fetch" in argv:
        fetch()
    if not CACHE.exists():
        raise SystemExit("no tide record cached: run with --fetch")
    td = pd.read_csv(CACHE, index_col=0)
    td.index = pd.to_datetime(td.index, utc=True)
    obs, pred = td["observed_ft"].dropna(), td["predicted_ft"].dropna()
    tide = obs.combine_first(pred)
    surge = (obs - pred).dropna()
    print(f"tide: observed through {obs.index.max():%Y-%m-%d}, predicted through {pred.index.max():%Y-%m-%d}; "
          f"highest observed {obs.max():.2f} ft on {storm.local(obs.idxmax(), '%a %b %d %Y %H:%M')}; "
          f"largest surge above prediction {surge.max():.2f} ft on {storm.local(surge.idxmax(), '%a %b %d %Y')}")

    d = storm.load()
    ross_raw = d["ross"]["ross_stage_ft"].dropna()
    ross = ross_raw.resample("15min").mean().interpolate(limit=4)
    t15 = tide.resample("15min").interpolate("time").reindex(ross.index)
    rain72 = d["rain10"].rolling(432, min_periods=1).sum().reindex(ross.index, method="ffill")
    dry = (rain72 < 0.01) & t15.notna() & ross.notna()

    print(f"\nA. Dry weather (no rain in 72 h): {int(dry.sum() / 96)} days of 15-minute Ross readings")
    det = ross[dry] - ross[dry].rolling(192, center=True, min_periods=48).mean()    # take out the slow recession
    for lag in (0, 1, 2, 3):
        tl = t15.shift(lag * 4)[dry]
        m = det.notna() & tl.notna()
        print(f"   tide {lag} h earlier: correlation {np.corrcoef(det[m], tl[m])[0, 1]:+.3f}, "
              f"Ross moves {np.polyfit(tl[m], det[m], 1)[0] * 12:+.3f} in per ft of tide")

    print("\nB. The highest tides on record, and Ross within 6 h of each")
    seen = set()
    for t in obs.sort_values(ascending=False).index:
        day = t.tz_convert(TZ).date()
        if day in seen:
            continue
        seen.add(day)
        w = ross[t - pd.Timedelta("6h"):t + pd.Timedelta("6h")]
        rain = d["rain10"][t - pd.Timedelta("24h"):t + pd.Timedelta("3h")].sum()
        print(f"   {storm.local(t, '%a %b %d %Y %H:%M')}  tide {obs[t]:.2f} ft   rain in 24 h {rain:.2f} in   "
              f"Ross {w.min():.2f} to {w.max():.2f} ft")
        if len(seen) >= 8:
            break

    print("\nC. Storm crests (0.25 in or more)")
    models = json.loads(storm.MODELS.read_text(encoding="utf-8"))
    ep, rb = models["event_peak"], models["ross_to_bridge"]
    tide5 = tide.resample("5min").interpolate("time")
    events, br, rows = storm.segment(d["rain10"]), d["bridge"].dropna(), []
    for i, (s, e) in enumerate(events):
        total = float(d["rain10"].loc[s:e].sum())
        nxt = events[i + 1][0] if i + 1 < len(events) else e + pd.Timedelta(hours=13)
        end = min(e + pd.Timedelta(hours=storm.TRAIL_H), nxt)
        wr, wb = ross_raw[s:end], br[s:end]
        if total < 0.25 or not len(wr) or not len(wb) or s < tide.index.min():
            continue
        tr = wr.idxmax()
        pred_ft = ep["beta"][0] + ep["beta"][1] * total + ep["beta"][2] * float(d["api"].loc[s])
        rows.append(dict(storm=storm.local(s, "%Y-%m-%d"), rain_in=total, bridge_ft=wb.max(), ross_ft=wr.max(),
                         model_err_ft=wb.max() - pred_ft,
                         ross_high_ft=wr.max() - (wb.max() - rb["intercept"]) / rb["slope"],
                         tide_at_crest_ft=float(tide5.asof(tr))))
    df = pd.DataFrame(rows)
    print(df.round(2).to_string(index=False))
    for col, what in (("model_err_ft", "crest-model error at Bridge Street (observed - predicted)"),
                      ("ross_high_ft", "Ross reading high for its Bridge Street crest (backwater)")):
        r = np.corrcoef(df.tide_at_crest_ft, df[col])[0, 1]
        rng = np.random.default_rng(1)
        p = np.mean([abs(np.corrcoef(rng.permutation(df.tide_at_crest_ft), df[col])[0, 1]) >= abs(r) for _ in range(5000)])
        print(f"   tide at the crest vs {what}: r = {r:+.2f} (p ~ {p:.2f}, n = {len(df)}), "
              f"{np.polyfit(df.tide_at_crest_ft, df[col], 1)[0]:+.2f} ft per ft of tide")


if __name__ == "__main__":
    main(sys.argv[1:])
