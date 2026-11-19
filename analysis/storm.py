#!/usr/bin/env python3
"""
What happened in a storm: rain, creek and sump, straight from the raw inputs.

    python analysis/storm.py last              # the most recent storm
    python analysis/storm.py 2026-11-13        # the storm on or nearest that local date
    python analysis/storm.py list              # every storm on record
    python analysis/storm.py list --since 2026-10-01
    python analysis/storm.py last --json       # same facts, machine-readable

Options: --min IN   smallest storm total to count (default 0.25 in)
         --json     JSON instead of text

A storm is defined exactly as in build_events.py, so these numbers agree with
analysis/events.csv for last winter: contiguous rain on the 10-minute grid,
split by a 6-hour dry gap. The creek and the pump are read over the response
window, from the first rain to 12 h after the last (or the next storm's start).

This reads the tracked files only and never touches the network. Run
`python analysis/update_data.py` first when the data is stale; the header of
every answer says how fresh each source is.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import build_timeline as bt   # noqa: E402

TZ = "America/Los_Angeles"
GRID_MIN = bt.GRID_MIN
DRY_GAP_H = 6                 # as build_events.py
TRAIL_H = 12
SINCE = pd.Timestamp("2025-10-01", tz="UTC")     # start of the record (water year 2026)
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GPM = CFG["primary_pump"]["rated_gpm"]
MODELS = ROOT / "docs" / "data" / "models.json"


def local(t, fmt="%a %b %d %H:%M"):
    return None if t is None or pd.isna(t) else pd.Timestamp(t).tz_convert(TZ).strftime(fmt)


def r(x, n=2):
    return None if x is None or pd.isna(x) else round(float(x), n)


# -------------------------------------------------------------------- data
def load():
    amb, _ = bt.load_ambient(since=SINCE)
    pf, _ = bt.load_pumpfuse(since=SINCE)
    br, _ = bt.load_bridge()
    months = sorted(pd.Timestamp(f.stem.split("_")[1] + "01") for f in bt.CACHE.glob("usgs_*.json"))
    usgs, _ = bt.load_usgs(since=SINCE, months=months)

    end = amb.index.max().ceil(f"{GRID_MIN}min")
    grid = pd.date_range(SINCE, end, freq=f"{GRID_MIN}min", tz="UTC", inclusive="left")
    rain10 = amb["rain_in"].resample(f"{GRID_MIN}min").sum().reindex(grid).fillna(0.0)
    rain5 = amb["rain_in"].resample("5min").sum()

    # antecedent precipitation index, as build_timeline: 0.9/day, per 10-min step
    decay = 0.9 ** (GRID_MIN / 1440.0)
    api, acc = np.empty(len(rain10)), 0.0
    for i, v in enumerate(rain10.to_numpy()):
        acc = acc * decay + v
        api[i] = acc

    return dict(amb=amb, pf=pf, bridge=br, ross=usgs, rain10=rain10, rain5=rain5,
                api=pd.Series(api, index=grid))


def segment(rain10):
    wet = rain10.index[rain10 > 0]
    ev = []
    if len(wet):
        start = prev = wet[0]
        for t in wet[1:]:
            if (t - prev) >= pd.Timedelta(hours=DRY_GAP_H):
                ev.append((start, prev))
                start = t
            prev = t
        ev.append((start, prev))
    return ev


# ------------------------------------------------------------------ one storm
def creek(series, s, resp_end, pk1h_t, pk10_t):
    """Stage (or flow) before the rain, the crest, the rise and the lag.

    `before` is the last reading at or before the first rain. Bridge Street
    reports only on change (12 h apart when flat), so that reading can be hours
    old and still be the true level, and the crest is the highest reading it
    sent, not a continuous trace. Two lags: minutes from the start of the
    heaviest 10 minutes, and hours from the wettest hour as events.csv defines
    it (lag_rain_to_bridge_h), so last winter's lags stay comparable.
    """
    series = series.dropna()
    w = series.loc[s:resp_end]
    prior = series.loc[:s]
    if w.empty:
        return None
    before = float(prior.iloc[-1]) if len(prior) else None
    t_pk = w.idxmax()
    out = dict(before=r(before), before_at=local(prior.index[-1]) if len(prior) else None,
               crest=r(w.max()), crest_at=local(t_pk),
               rise=r(w.max() - before) if before is not None else None,
               min_after_heaviest_10min=r((t_pk - pk10_t).total_seconds() / 60, 0),
               lag_h_from_wettest_hour=r((t_pk - pk1h_t).total_seconds() / 3600, 2),
               readings=int(len(w)), last_reading=local(series.index[-1]))
    # A crest on the final reading of a window that is still open is not a crest yet.
    out["still_rising"] = bool(t_pk == series.index[-1] and series.index[-1] < resp_end)
    return out


def describe(d, i, events, models):
    s, e = events[i]
    step = pd.Timedelta(minutes=GRID_MIN)
    rain_end = e + step                                    # e labels the START of the last wet bin
    data_end = d["rain10"].index[-1] + step
    nxt = events[i + 1][0] if i + 1 < len(events) else data_end + pd.Timedelta(hours=1)
    resp_end = min(e + pd.Timedelta(hours=TRAIL_H), nxt)

    r10 = d["rain10"]
    roll = {k: r10.rolling(n, min_periods=1).sum() for k, n in
            (("10min", 1), ("30min", 3), ("1h", 6), ("3h", 18), ("6h", 36), ("24h", 144))}
    w = slice(s, e)
    total = float(r10.loc[w].sum())
    pk1h_t = roll["1h"].loc[w].idxmax()                    # start of the bin that closes the wettest hour
    pk1h_end = pk1h_t + step
    pk10_t = roll["10min"].loc[w].idxmax()                 # start of the heaviest 10 minutes
    five = d["rain5"].loc[s:rain_end]
    rate = d["amb"]["rain_rate_inhr"].loc[s:rain_end].dropna()

    out = dict(
        start=local(s, "%Y-%m-%d %H:%M"), start_day=local(s, "%a %b %d %Y"),
        end=local(rain_end, "%Y-%m-%d %H:%M"),
        duration_h=r((rain_end - s).total_seconds() / 3600, 1),
        in_progress=bool((data_end - rain_end) < pd.Timedelta(hours=DRY_GAP_H)),
        rain=dict(
            total_in=r(total),
            peak_10min_in=r(roll["10min"].loc[w].max()), peak_30min_in=r(roll["30min"].loc[w].max()),
            peak_1h_in=r(roll["1h"].loc[w].max()), wettest_hour_ended=local(pk1h_end),
            peak_3h_in=r(roll["3h"].loc[w].max()), peak_6h_in=r(roll["6h"].loc[w].max()),
            peak_24h_in=r(roll["24h"].loc[w].max()),
            peak_10min_rate_inhr=r(roll["10min"].loc[w].max() * 6), heaviest_10min_began=local(pk10_t),
            peak_5min_rate_inhr=r(five.max() * 12) if len(five) else None,
            peak_5min_at=local(five.idxmax()) if len(five) and five.max() > 0 else None,
            peak_station_rate_inhr=r(rate.max()) if len(rate) else None,
            peak_station_rate_at=local(rate.idxmax()) if len(rate) and rate.max() > 0 else None,
            wetness_index_at_start_in=r(d["api"].loc[s]),
        ),
        response_window_end=local(resp_end, "%Y-%m-%d %H:%M"),
    )

    out["bridge_street_ft"] = creek(d["bridge"], s, resp_end, pk1h_t, pk10_t)
    out["ross_stage_ft"] = creek(d["ross"]["ross_stage_ft"], s, resp_end, pk1h_t, pk10_t)
    rf = creek(d["ross"]["ross_flow_cfs"], s, resp_end, pk1h_t, pk10_t)
    out["ross_flow_cfs"] = None if rf is None else dict(before=r(rf["before"], 0), peak=r(rf["crest"], 0),
                                                         peak_at=rf["crest_at"])

    # sump: the pump log is a manual export, so say when it stops short of the storm
    pf = d["pf"]
    if len(pf) == 0 or pf.index.max() < s:
        out["sump"] = dict(covered=False, log_ends=local(pf.index.max()) if len(pf) else None)
    else:
        runs = pf.loc[s:resp_end]
        lead = pf.loc[s - pd.Timedelta(minutes=15):resp_end]["Duration"]
        minute = pd.date_range(s - pd.Timedelta(minutes=15), resp_end, freq="1min", tz="UTC")
        in_win = (minute >= s) & (minute <= resp_end)
        run_s = lead.resample("1min").sum().reindex(minute).fillna(0.0)
        run_n = lead.resample("1min").count().reindex(minute).fillna(0)
        duty15 = (run_s.rolling(15, min_periods=1).sum() / (15 * 60))[in_win]
        runs15 = run_n.rolling(15, min_periods=1).sum()[in_win]
        gaps = pd.Series(runs.index).diff().dt.total_seconds().dropna()
        out["sump"] = dict(
            covered=True, complete=bool(pf.index.max() >= min(resp_end, data_end) - pd.Timedelta(hours=1)),
            log_ends=local(pf.index.max()),
            runs=int(len(runs)), gallons=r(runs["Duration"].sum() / 60 * GPM, 0),
            longest_run_s=r(runs["Duration"].max(), 0) if len(runs) else None,
            longest_run_at=local(runs["Duration"].idxmax()) if len(runs) else None,
            median_run_s=r(runs["Duration"].median(), 0) if len(runs) else None,
            shortest_interval_min=r(gaps.min() / 60, 1) if len(gaps) else None,
            peak_runs_per_15min=int(runs15.max()) if len(runs) else 0,
            peak_inflow_gpm_15min=r(duty15.max() * GPM, 1) if len(runs) else 0.0,
            peak_inflow_at=local(duty15.idxmax()) if len(runs) else None,
        )

    # what the planner's crest model would have said, given the rain that fell
    ep = (models or {}).get("event_peak")
    b = out["bridge_street_ft"]
    if ep and b:
        pred = ep["beta"][0] + ep["beta"][1] * total + ep["beta"][2] * float(d["api"].loc[s])
        out["planner_crest_model"] = dict(
            predicted_ft=r(pred), band_95_ft=r(ep["band_95"]), observed_ft=b["crest"],
            error_ft=r(b["crest"] - pred), inside_band=bool(abs(b["crest"] - pred) <= ep["band_95"]),
            note="hindcast from the observed storm total and wetness index, not a forecast")
    return out


# --------------------------------------------------------------------- output
def text(o):
    L = []
    A = L.append
    rn = o["rain"]
    A(f"STORM  {o['start_day']}   rain {o['start'][11:]} -> {o['end']}  ({o['duration_h']} h)"
      + ("   ** STILL IN PROGRESS **" if o["in_progress"] else ""))
    A(f"  rain total          {rn['total_in']} in   (wetness index at start {rn['wetness_index_at_start_in']} in)")
    A(f"  heaviest 5 min      {rn['peak_5min_rate_inhr']} in/hr at {rn['peak_5min_at']}")
    A(f"  heaviest 10 min     {rn['peak_10min_rate_inhr']} in/hr  ({rn['peak_10min_in']} in), beginning {rn['heaviest_10min_began']}")
    A(f"  heaviest hour       {rn['peak_1h_in']} in, ending {rn['wettest_hour_ended']}")
    A(f"  heaviest 3/6/24 h   {rn['peak_3h_in']} / {rn['peak_6h_in']} / {rn['peak_24h_in']} in")
    A(f"  station peak rate   {rn['peak_station_rate_inhr']} in/hr at {rn['peak_station_rate_at']}"
      "   (the console's own hourly-rate reading)")
    for key, name in (("bridge_street_ft", "Bridge Street"), ("ross_stage_ft", "Ross")):
        c = o[key]
        if not c:
            A(f"  {name:<19} no readings in the window")
            continue
        A(f"  {name:<19} {c['before']} -> {c['crest']} ft, a rise of {c['rise']} ft; crest {c['crest_at']}, "
          f"{c['min_after_heaviest_10min']:.0f} min after the heaviest 10 minutes began"
          + ("   ** still rising at the last reading **" if c["still_rising"] else ""))
    f = o["ross_flow_cfs"]
    if f:
        A(f"  Ross flow           {f['before']} -> {f['peak']} cfs at {f['peak_at']}")
    s = o["sump"]
    if not s["covered"]:
        A(f"  sump                no pump data: the log ends {s['log_ends']} (needs a new PumpFuse export)")
    else:
        A(f"  sump                {s['runs']} runs, {s['gallons']:,.0f} gal; longest run {s['longest_run_s']} s at "
          f"{s['longest_run_at']}; runs as close as {s['shortest_interval_min']} min apart")
        A(f"  sump peak inflow    {s['peak_inflow_gpm_15min']} GPM over 15 min at {s['peak_inflow_at']} "
          f"(pump moves {GPM} GPM)" + ("" if s["complete"] else f"   ** pump log ends {s['log_ends']}, mid-storm **"))
    m = o.get("planner_crest_model")
    if m:
        A(f"  planner crest model {m['predicted_ft']} ft predicted vs {m['observed_ft']} observed "
          f"(error {m['error_ft']:+.2f} ft, band +/-{m['band_95_ft']}; "
          f"{'inside' if m['inside_band'] else 'OUTSIDE'} the band)")
    return "\n".join(L)


def freshness(d):
    now = pd.Timestamp.now(tz="UTC")

    def age(t):
        h = (now - t).total_seconds() / 3600
        return f"{h:.0f} h ago" if h < 72 else f"{h / 24:.0f} days ago"
    src = (("station rain", d["amb"].index.max()), ("Bridge Street", d["bridge"].index.max()),
           ("Ross", d["ross"].index.max()), ("pump log", d["pf"].index.max()))
    return {n: dict(last=local(t, "%Y-%m-%d %H:%M"), age=age(t)) for n, t in src}


def main(argv):
    opts = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in opts
    min_in, since = 0.25, None
    for flag in ("--min", "--since"):
        if flag in argv:
            val = argv[argv.index(flag) + 1]
            args.remove(val)
            if flag == "--min":
                min_in = float(val)
            else:
                since = pd.Timestamp(val, tz=TZ).tz_convert("UTC")
    cmd = args[0] if args else "last"

    d = load()
    events = segment(d["rain10"])
    models = json.loads(MODELS.read_text(encoding="utf-8")) if MODELS.exists() else None
    big = [i for i, (s, e) in enumerate(events) if d["rain10"].loc[s:e].sum() >= min_in
           and (since is None or s >= since)]
    fresh = freshness(d)

    if cmd == "list":
        rows = [describe(d, i, events, models) for i in big]
        if as_json:
            print(json.dumps({"data": fresh, "storms": rows}, indent=1))
            return
        print(f"{len(rows)} storms of {min_in} in or more"
              + (f" since {local(since, '%Y-%m-%d')}" if since is not None else "") + " (local time)")
        print(f"  {'start':<17}{'h':>5}{'rain':>6}{'pk 1h':>7}{'pk5 in/hr':>10}{'wet':>6}"
              f"{'Bridge':>8}{'rise':>6}{'Ross':>7}{'runs':>6}{'gpm15':>7}{'model':>7}{'err':>7}")
        for o in rows:
            b, ro, s, m = o["bridge_street_ft"] or {}, o["ross_stage_ft"] or {}, o["sump"], o.get("planner_crest_model") or {}

            def f(v, w, p=2):
                return f"{'-':>{w}}" if v is None else f"{v:>{w}.{p}f}"
            rn = o["rain"]
            print(f"  {o['start']:<17}{o['duration_h']:>5.1f}{f(rn['total_in'], 6)}{f(rn['peak_1h_in'], 7)}"
                  f"{f(rn['peak_5min_rate_inhr'], 10)}{f(rn['wetness_index_at_start_in'], 6)}"
                  f"{f(b.get('crest'), 8)}{f(b.get('rise'), 6)}{f(ro.get('crest'), 7)}"
                  f"{(str(s['runs']) if s['covered'] else '-'):>6}"
                  f"{f(s.get('peak_inflow_gpm_15min'), 7, 1)}{f(m.get('predicted_ft'), 7)}{f(m.get('error_ft'), 7)}"
                  + ("  *in progress" if o["in_progress"] else ""))
        print("  rain in inches; wet = wetness index at the start; Bridge/Ross = crest ft; "
              "model/err = planner crest hindcast and observed minus predicted")
    else:
        if not big:
            raise SystemExit(f"no storm of {min_in} in or more on record")
        if cmd == "last":
            i = big[-1]
        else:
            day = pd.Timestamp(cmd, tz=TZ)
            lo, hi = day.tz_convert("UTC"), (day + pd.Timedelta(days=1)).tz_convert("UTC")
            inside = [k for k in big if events[k][0] < hi and events[k][1] + pd.Timedelta(minutes=GRID_MIN) > lo]
            # raining that day -> the biggest such storm; otherwise the nearest start
            i = (max(inside, key=lambda k: d["rain10"].loc[events[k][0]:events[k][1]].sum()) if inside
                 else min(big, key=lambda k: abs((events[k][0] - lo).total_seconds())))
        o = describe(d, i, events, models)
        if as_json:
            print(json.dumps({"data": fresh, "storm": o}, indent=1))
            return
        print(text(o))
    print("\ndata through: " + "; ".join(f"{n} {v['last']} ({v['age']})" for n, v in fresh.items()))


if __name__ == "__main__":
    main(sys.argv[1:])
