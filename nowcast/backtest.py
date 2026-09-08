#!/usr/bin/env python3
"""
Stage 1 back-test — does MRMS see the same rain the gauge does, at this house?

Downloads MRMS PrecipRate for the active-rain hours of last winter's five
biggest storms, extracts the station pixel every 2 minutes, and compares against
the Ambient gauge. Nothing is nowcast until this question is answered.

Frames are decoded in flight and discarded: only the station pixel, basin mean
and box statistics are kept, so the 3.8 GB of GRIB never lands on disk. The two
storms replayed in Stage 2 (Nov 16, Dec 24) also keep their 434x550 crop, which
the motion field needs.

    python nowcast/backtest.py fetch     # populate mrms.sqlite
    python nowcast/backtest.py report    # bias / correlation / timing offset
"""
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

import mrms

HERE = Path(__file__).resolve().parent
HOURS = json.loads((HERE / "backtest_hours.json").read_text())
KEEP_CROPS = {"Nov16", "Dec24"}          # Stage 2 replays these
# ecCodes is NOT thread-safe: decoding 8 frames concurrently in threads
# segfaults with "grib_handle_create: cannot create handle". Separate processes
# each get their own ecCodes state, which is safe and also uses the cores.
WORKERS = 6


def frame_times(storm):
    out = []
    for h in HOURS[storm]:
        base = datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        out += [base + timedelta(minutes=2 * k) for k in range(30)]
    return out


_SESSION = None


def _worker(args):
    """Download + decode one frame. Runs in its own process."""
    t_iso, keep = args
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    t = datetime.strptime(t_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    try:
        raw = mrms.fetch("PrecipRate", t, archive=True, session=_SESSION)
        grid = mrms.decode(raw)
        summ, box = mrms.summarise(grid, _mask())
        return t_iso, summ, (mrms.pack(box) if keep else None), None
    except Exception as e:  # noqa: BLE001
        return t_iso, None, None, f"{type(e).__name__}: {e}"[:80]


_MASK = None


def _mask():
    global _MASK
    if _MASK is None:
        _MASK = mrms.basin_mask()
    return _MASK


def fetch_all(only=None):
    con = mrms.connect()
    have = {r[0] for r in con.execute(
        "SELECT valid_utc FROM frames WHERE product='PrecipRate'")}
    storms = [s for s in HOURS if only is None or s == only]

    for storm in storms:
        keep = storm in KEEP_CROPS
        times = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in frame_times(storm)]
        todo = [(t, keep) for t in times if t not in have]
        if not todo:
            print(f"{storm}: already complete", flush=True)
            continue
        print(f"{storm}: {len(todo)} frames to fetch (crops={keep})", flush=True)
        done = err = 0
        t_start = time.time()
        with ProcessPoolExecutor(WORKERS) as ex:
            batch = []
            for t_iso, summ, blob, e in ex.map(_worker, todo, chunksize=4):
                if e:
                    err += 1
                    if err <= 3:
                        print(f"   miss {t_iso}: {e}", flush=True)
                    continue
                batch.append((t_iso, summ, blob))
                done += 1
                if len(batch) >= 90:
                    _flush(con, batch); batch = []
                if done % 300 == 0:
                    rate = done / max(time.time() - t_start, 1)
                    print(f"   {storm}: {done}/{len(todo)}  {rate:.1f} fps  "
                          f"eta {(len(todo)-done)/max(rate,.01)/60:.0f} min", flush=True)
            _flush(con, batch)
        print(f"{storm}: stored {done}, missing {err} "
              f"({time.time()-t_start:.0f}s)", flush=True)
    con.close()


def _flush(con, rows):
    """Rows arrive with the crop already packed by the worker."""
    for t_iso, summ, blob in rows:
        con.execute(
            "INSERT OR REPLACE INTO frames (product,valid_utc,station,basin_mean,"
            "basin_max,box_max,box_mean,box_wet_frac,rows,cols,j0,i0,crop,fetched_utc)"
            " VALUES ('PrecipRate',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t_iso, summ.get("station"), summ.get("basin_mean"), summ.get("basin_max"),
             summ.get("box_max"), summ.get("box_mean"), summ.get("box_wet_frac"),
             mrms.J1 - mrms.J0, mrms.I1 - mrms.I0, mrms.J0, mrms.I0, blob,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()


# ---------------------------------------------------------------- comparison
def report():
    import pandas as pd
    con = mrms.connect()
    df = pd.read_sql("SELECT valid_utc, station, basin_mean, box_max, box_wet_frac"
                     " FROM frames WHERE product='PrecipRate' ORDER BY valid_utc", con)
    if df.empty:
        print("no frames stored yet"); return
    df["t"] = pd.to_datetime(df.valid_utc, utc=True)
    df["mrms_inhr"] = df.station / mrms.MM_PER_IN
    df["basin_inhr"] = df.basin_mean / mrms.MM_PER_IN
    df = df.set_index("t")

    tl = pd.read_parquet(HERE.parent / "analysis" / "timeline.parquet")
    tl.index = pd.to_datetime(tl.index, utc=True)

    print("=" * 82)
    print("MRMS vs AMBIENT GAUGE AT THE HOUSE")
    print("=" * 82)
    print(f"  frames: {len(df):,}   {df.index.min()} -> {df.index.max()}")
    print(f"  MRMS station pixel centre 37.9750,-122.5650 (~300 m from the house)")

    # --- rate comparison on the gauge's own 5-min clock is unfair to MRMS
    #     (2-min instantaneous vs a 5-min console rate), so compare 10-min totals
    #     as the primary measure and the raw rate as secondary.
    mr10 = (df.mrms_inhr.resample("10min").mean() / 6).rename("mrms_in")   # in per 10 min
    gz = tl["rain_in"].rename("gauge_in")
    j = pd.concat([mr10, gz], axis=1).dropna()
    j = j[(j.gauge_in > 0) | (j.mrms_in > 0)]

    print()
    print("  10-MINUTE TOTALS (inches), slots where either source saw rain")
    _stats(j.gauge_in, j.mrms_in)
    wet = j[j.gauge_in >= 0.01]
    print("\n  ... restricted to slots the GAUGE called wet (>= 0.01 in)")
    _stats(wet.gauge_in, wet.mrms_in)

    print()
    print("  TOTALS OVER THE COMPARED PERIOD")
    print(f"    gauge {j.gauge_in.sum():7.2f} in     MRMS {j.mrms_in.sum():7.2f} in"
          f"     MRMS/gauge = {j.mrms_in.sum()/j.gauge_in.sum():.3f}")

    # --- timing: cross-correlate at 2-min lags
    print()
    print("  TIMING OFFSET  (cross-correlation of 10-min totals)")
    best = None
    for lag in range(-6, 7):                      # +/- 60 min in 10-min steps
        a = j.gauge_in
        b = j.mrms_in.shift(lag)
        m = a.notna() & b.notna()
        if m.sum() < 30:
            continue
        r = np.corrcoef(a[m], b[m])[0, 1]
        star = ""
        if best is None or r > best[1]:
            best, star = (lag, r), ""
        print(f"    MRMS shifted {lag*10:+4d} min   r = {r:.4f}")
    print(f"    -> best at {best[0]*10:+d} min, r = {best[1]:.4f}")
    print("       (positive = MRMS leads the gauge, i.e. radar sees it first)")

    # --- detection
    print()
    print("  DETECTION (10-min slots)")
    g_wet = j.gauge_in >= 0.01
    m_wet = j.mrms_in >= 0.01
    hit = (g_wet & m_wet).sum(); miss = (g_wet & ~m_wet).sum()
    fa = (~g_wet & m_wet).sum(); cn = (~g_wet & ~m_wet).sum()
    print(f"    both wet {hit:5d}   gauge-only {miss:5d}   MRMS-only {fa:5d}   both dry {cn:5d}")
    if hit + miss:
        print(f"    probability of detection {hit/(hit+miss):.3f}")
    if hit + fa:
        print(f"    false alarm ratio        {fa/(hit+fa):.3f}")

    # --- per storm
    print()
    print("  PER STORM")
    print(f"    {'storm':8}{'frames':>8}{'gauge in':>10}{'MRMS in':>9}{'ratio':>7}{'r':>7}")
    for storm, hrs in HOURS.items():
        t0 = pd.Timestamp(hrs[0] + ":00:00Z"); t1 = pd.Timestamp(hrs[-1] + ":59:59Z")
        s = j.loc[t0:t1]
        n = len(df.loc[t0:t1])
        if len(s) < 10:
            print(f"    {storm:8}{n:>8}{'  (insufficient overlap)':>26}"); continue
        r = np.corrcoef(s.gauge_in, s.mrms_in)[0, 1]
        ratio = s.mrms_in.sum() / s.gauge_in.sum() if s.gauge_in.sum() else float("nan")
        print(f"    {storm:8}{n:>8}{s.gauge_in.sum():>10.2f}{s.mrms_in.sum():>9.2f}"
              f"{ratio:>7.2f}{r:>7.3f}")
    con.close()


def _stats(g, m):
    d = m - g
    r = np.corrcoef(g, m)[0, 1] if len(g) > 2 else float("nan")
    print(f"    n={len(g):5d}   bias={d.mean():+.4f} in   MAE={d.abs().mean():.4f}"
          f"   RMSE={np.sqrt((d**2).mean()):.4f}   r={r:.4f}")
    print(f"    gauge mean {g.mean():.4f} in   MRMS mean {m.mean():.4f} in"
          f"   ratio {m.sum()/g.sum():.3f}" if g.sum() else "")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if cmd == "fetch":
        fetch_all(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        report()
