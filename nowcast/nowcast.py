#!/usr/bin/env python3
"""
Stage 2 — pysteps nowcast for the house pixel.

Takes the last 8 MRMS crops, computes a Lucas-Kanade motion field, and runs both
a deterministic extrapolation and a 20-member STEPS ensemble out to +90 min in
5-minute steps. Samples the house pixel for the things that actually drive a
decision: when rain arrives, how hard it gets, and how likely it is to exceed
the rates that matter.

Every nowcast is stored with its issue time so it can be scored an hour later
against what the gauge really did. A nowcast that is never verified is a guess
with extra steps.

    python nowcast/nowcast.py run                    # one nowcast from live data
    python nowcast/nowcast.py replay Nov16 Dec24     # historical replay
    python nowcast/nowcast.py sweep 30               # issue every 30 min over the store
    python nowcast/nowcast.py verify                 # score past nowcasts
    python nowcast/nowcast.py score                  # re-print the scorecard
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import mrms

HERE = Path(__file__).resolve().parent
N_FRAMES = 8                 # history the motion field sees
LEAD_MIN = 90
STEP_MIN = 5
N_ENS = 20
ARRIVAL_INHR = 0.1
EXCEED_INHR = (0.5, 1.0)
MM = mrms.MM_PER_IN

# The ingest crop is 150 miles because that is the context you want on a map.
# The nowcast does not need it: storms here move ~50 km/h, so +90 min reaches
# about 75 km upwind. A +/-125 km sub-box centred on the house covers that with
# margin at a quarter of the cells, which is the difference between a nowcast
# that finishes inside the 4-minute cadence and one that does not.
NOWCAST_HALF_KM = 125


class SkipEnsemble(Exception):
    """Raised to take the no-ensemble path out of make_nowcast."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS nowcasts (
  issued_utc   TEXT NOT NULL,
  kind         TEXT NOT NULL,          -- 'live' | 'replay'
  n_frames     INTEGER,
  motion_u     REAL, motion_v REAL, motion_speed_kmh REAL, motion_dir_deg REAL,
  arrival_min  REAL,                   -- minutes until rate >= 0.1 in/hr
  peak_10min_inhr REAL,
  total_30min_in  REAL,
  total_60min_in  REAL,
  p_exceed_0p5 REAL,
  p_exceed_1p0 REAL,
  curve_json   TEXT,                   -- [{lead, det_inhr, p10, p50, p90}]
  hrrr_json    TEXT,
  PRIMARY KEY (issued_utc, kind)
);
CREATE TABLE IF NOT EXISTS verification (
  issued_utc TEXT, kind TEXT, lead_min INTEGER,
  pred_inhr REAL, obs_gauge_inhr REAL, obs_mrms_inhr REAL,
  PRIMARY KEY (issued_utc, kind, lead_min)
);
"""


def connect():
    con = sqlite3.connect(HERE / "mrms.sqlite")
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- frames
def load_crops(con, upto, n=N_FRAMES):
    """The n most recent stored crops at or before `upto`, oldest first."""
    rows = con.execute(
        "SELECT valid_utc, crop, rows, cols FROM frames"
        " WHERE product='PrecipRate' AND crop IS NOT NULL AND valid_utc<=?"
        " ORDER BY valid_utc DESC LIMIT ?",
        (upto.strftime("%Y-%m-%dT%H:%M:%SZ"), n)).fetchall()
    rows = rows[::-1]
    if len(rows) < n:
        raise ValueError(f"only {len(rows)} crops available before {upto}, need {n}")
    times = [datetime.strptime(r[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             for r in rows]
    # The store holds disjoint periods (storm hours, plus live polls), so the
    # "most recent 8" can silently splice across a gap of months. A motion field
    # computed across that is meaningless, so refuse it.
    gaps = [(times[k + 1] - times[k]).total_seconds() / 60 for k in range(len(times) - 1)]
    if max(gaps) > 3 * min(g for g in gaps if g > 0):
        raise ValueError(
            f"crops before {upto:%Y-%m-%d %H:%M} are not contiguous "
            f"(gaps {[int(g) for g in gaps]} min)")
    stack = np.stack([mrms.unpack(r[1], r[2], r[3]) for r in rows])
    return times, stack


def subbox(stack):
    """Crop the stack to the nowcast domain; returns (stack, house_j, house_i)."""
    h = NOWCAST_HALF_KM                       # 1 px ~ 1 km
    j, i = mrms.STA_RJ, mrms.STA_RI
    j0, j1 = max(0, j - h), min(stack.shape[-2], j + h)
    i0, i1 = max(0, i - h), min(stack.shape[-1], i + h)
    return stack[..., j0:j1, i0:i1], j - j0, i - i0


def to_dbr(rate_mm_hr):
    """pysteps works in dBR. Below the threshold becomes the 'no rain' floor."""
    from pysteps.utils import transformation
    r = np.nan_to_num(rate_mm_hr, nan=0.0)
    r = np.where(r < 0.1, 0.0, r)
    dbr, _ = transformation.dB_transform(r, threshold=0.1, zerovalue=-15.0)
    return dbr


def from_dbr(dbr):
    from pysteps.utils import transformation
    r, _ = transformation.dB_transform(dbr, threshold=-10.0, inverse=True)
    return np.nan_to_num(r, nan=0.0)


# ---------------------------------------------------------------- nowcast
def make_nowcast(stack, times, n_ens=N_ENS, lead=LEAD_MIN, step=STEP_MIN):
    """Returns (deterministic [T,H,W] mm/hr, ensemble [E,T,H,W] or None, motion)."""
    from pysteps import motion, nowcasts

    dt_min = (times[-1] - times[-2]).total_seconds() / 60
    stack, hj, hi = subbox(stack)
    dbr = to_dbr(stack)

    V = motion.get_method("LK")(dbr)

    # The deterministic run is cheap and always available.
    ext = nowcasts.get_method("extrapolation")
    # pysteps steps forward in units of the input interval, so ask for the number
    # of input-interval steps that covers the lead time, then resample to `step`.
    n_in = int(np.ceil(lead / dt_min))
    det_in = ext(dbr[-1], V, n_in)
    det = from_dbr(det_in)

    ens = None
    # The STEPS ensemble is ~130 s per nowcast at 2-minute frames (45 steps x 20
    # members); the deterministic extrapolation is ~2 s. A verification sweep
    # scores `det` only, so it asks for n_ens=0 and skips this entirely.
    try:
        if not n_ens:
            raise SkipEnsemble
        steps = nowcasts.get_method("steps")
        ens_in = steps(dbr[-3:], V, n_in, n_ens_members=n_ens, n_cascade_levels=6,
                       precip_thr=-10.0, kmperpixel=1.0, timestep=dt_min,
                       noise_method="nonparametric", vel_pert_method=None,
                       mask_method="incremental", seed=42)
        ens = from_dbr(ens_in)
    except SkipEnsemble:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"  STEPS ensemble unavailable: {type(e).__name__}: {str(e)[:110]}")

    return det, ens, V, dt_min, (hj, hi)


def sample_house(det, ens, dt_min, house, lead=LEAD_MIN, step=STEP_MIN):
    """Pull the house pixel out of the forecast grids and derive the decision numbers."""
    j, i = house
    n_in = det.shape[0]
    t_in = np.arange(1, n_in + 1) * dt_min                 # minutes ahead
    t_out = np.arange(step, lead + step, step)

    det_series = np.interp(t_out, t_in, det[:, j, i]) / MM  # in/hr
    curve = []
    ens_series = None
    if ens is not None:
        ens_pix = ens[:, :, j, i] / MM                      # [E, T]
        ens_series = np.stack([np.interp(t_out, t_in, m) for m in ens_pix])

    for k, t in enumerate(t_out):
        row = {"lead": int(t), "det_inhr": round(float(det_series[k]), 4)}
        if ens_series is not None:
            col = ens_series[:, k]
            row |= {"p10": round(float(np.percentile(col, 10)), 4),
                    "p50": round(float(np.percentile(col, 50)), 4),
                    "p90": round(float(np.percentile(col, 90)), 4)}
        curve.append(row)

    arrival = next((int(t) for t, v in zip(t_out, det_series) if v >= ARRIVAL_INHR), None)
    # 10-minute peak: the running mean of the 5-min steps, two steps wide
    peak10 = float(np.max(np.convolve(det_series, np.ones(2) / 2, mode="valid"))) \
        if len(det_series) >= 2 else float(det_series.max())
    tot30 = float(det_series[:6].sum() * step / 60)
    tot60 = float(det_series[:12].sum() * step / 60)

    out = {"arrival_min": arrival, "peak_10min_inhr": round(peak10, 4),
           "total_30min_in": round(tot30, 4), "total_60min_in": round(tot60, 4),
           "curve": curve}
    if ens_series is not None:
        for thr in EXCEED_INHR:
            key = f"p_exceed_{str(thr).replace('.', 'p')}"
            out[key] = round(float((ens_series.max(axis=1) >= thr).mean()), 4)
    return out


def motion_summary(V):
    """Domain-median motion, reported as speed and the direction it is heading."""
    u = float(np.nanmedian(V[0])); v = float(np.nanmedian(V[1]))
    # pixels per input step at 1 km, but report km/h using the caller's dt
    return u, v, float(np.hypot(u, v)), float((np.degrees(np.arctan2(u, -v))) % 360)


# ---------------------------------------------------------------- HRRR
def hrrr_house(hours=3):
    """Sub-hourly HRRR precipitation for the house, +1 to +3 h."""
    import requests
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": mrms.LAT, "longitude": mrms.LON, "models": "ncep_hrrr_conus",
        "minutely_15": "precipitation", "precipitation_unit": "inch",
        "timezone": "UTC", "forecast_days": 1}, timeout=45)
    r.raise_for_status()
    m = r.json().get("minutely_15", {})
    t = m.get("time", []); p = m.get("precipitation", [])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = [{"t": tt, "in": pp or 0} for tt, pp in zip(t, p)
           if now <= datetime.fromisoformat(tt) <= now + timedelta(hours=hours)]
    return {"steps": out, "total_in": round(sum(x["in"] for x in out), 3)}


# ---------------------------------------------------------------- drive
def run_one(con, issue_time, kind="live", with_hrrr=True, quiet=False,
            n_ens=N_ENS):
    times, stack = load_crops(con, issue_time)
    det, ens, V, dt_min, house = make_nowcast(stack, times, n_ens=n_ens)
    s = sample_house(det, ens, dt_min, house)
    u, v, speed, direction = motion_summary(V)
    kmh = speed * (60 / dt_min)
    hrrr = None
    if with_hrrr:
        try:
            hrrr = hrrr_house()
        except Exception as e:  # noqa: BLE001
            if not quiet:
                print(f"  HRRR unavailable: {type(e).__name__}")

    con.execute(
        "INSERT OR REPLACE INTO nowcasts (issued_utc,kind,n_frames,motion_u,motion_v,"
        "motion_speed_kmh,motion_dir_deg,arrival_min,peak_10min_inhr,total_30min_in,"
        "total_60min_in,p_exceed_0p5,p_exceed_1p0,curve_json,hrrr_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (times[-1].strftime("%Y-%m-%dT%H:%M:%SZ"), kind, len(times), u, v, kmh,
         direction, s.get("arrival_min"), s["peak_10min_inhr"], s["total_30min_in"],
         s["total_60min_in"], s.get("p_exceed_0p5"), s.get("p_exceed_1p0"),
         json.dumps(s["curve"]), json.dumps(hrrr) if hrrr else None))
    con.commit()
    s |= {"issued": times[-1], "motion_kmh": kmh, "motion_dir": direction,
          "hrrr": hrrr, "ens": ens is not None}
    return s


def fmt(s):
    a = s.get("arrival_min")
    return (f"    motion {s['motion_kmh']:.0f} km/h toward {s['motion_dir']:.0f} deg\n"
            f"    arrival of >=0.1 in/hr : "
            f"{'already raining' if a == 5 else (f'{a} min' if a else 'not within 90 min')}\n"
            f"    peak 10-min rate       : {s['peak_10min_inhr']:.3f} in/hr\n"
            f"    next 30 min total      : {s['total_30min_in']:.3f} in\n"
            f"    next 60 min total      : {s['total_60min_in']:.3f} in\n"
            + (f"    P(rate >= 0.5 in/hr)   : {s.get('p_exceed_0p5', float('nan')):.2f}\n"
               f"    P(rate >= 1.0 in/hr)   : {s.get('p_exceed_1p0', float('nan')):.2f}\n"
               if s.get("ens") else "    (no ensemble)\n"))


# The two long-run episodes Layer 2 found, in UTC. These are what a nowcast
# would have had to warn about.
EPISODES = {
    "Nov16": datetime(2025, 11, 17, 7, 33, tzinfo=timezone.utc),   # 134 s run, stress 0.90
    "Dec24": datetime(2025, 12, 25, 10, 20, tzinfo=timezone.utc),  # 38->100 s escalation
}


def replay(names):
    con = connect()
    import pandas as pd
    tl = pd.read_parquet(HERE.parent / "analysis" / "timeline.parquet")
    tl.index = pd.to_datetime(tl.index, utc=True)
    for name in names:
        run_t = EPISODES[name]
        print("=" * 78)
        print(f"{name}: long pump run at {run_t:%Y-%m-%d %H:%M}Z "
              f"({run_t.astimezone().strftime('%H:%M')} local)")
        print("=" * 78)
        for back in (60, 30):
            issue = run_t - timedelta(minutes=back)
            print(f"\n  --- as it would have read {back} min before, "
                  f"issued {issue:%H:%M}Z ---")
            try:
                s = run_one(con, issue, kind="replay", with_hrrr=False, quiet=True)
            except Exception as e:  # noqa: BLE001
                print(f"    unavailable: {e}")
                continue
            print(f"    (latest frame used: {s['issued']:%H:%M}Z)")
            print(fmt(s), end="")
            # what actually happened at the house over that window
            obs = tl.loc[issue:issue + timedelta(minutes=90), "rain_in"]
            if len(obs):
                print(f"    ACTUAL gauge over the next 90 min: {obs.sum():.3f} in, "
                      f"peak 10-min {obs.max():.3f} in "
                      f"({obs.max()*6:.2f} in/hr)")
    con.close()


# ---------------------------------------------------------------- sweep
def crop_blocks(con, gap_min=4):
    """Contiguous runs of stored crops, as [(t0, t1, n), ...].

    `load_crops` refuses a stack that splices across a gap, so the only issue
    times worth attempting are the ones sitting at least N_FRAMES deep inside
    one of these runs.
    """
    rows = [r[0] for r in con.execute(
        "SELECT valid_utc FROM frames WHERE product='PrecipRate'"
        " AND crop IS NOT NULL ORDER BY valid_utc")]
    if not rows:
        return []
    ts = [datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
          for t in rows]
    blocks, cur = [], [ts[0]]
    for a, b in zip(ts, ts[1:]):
        if (b - a).total_seconds() <= gap_min * 60:
            cur.append(b)
        else:
            blocks.append(cur)
            cur = [b]
    blocks.append(cur)
    return [(b[0], b[-1], len(b)) for b in blocks]


def sweep(cadence_min=30, kind="sweep", n_ens=0):
    """Issue a nowcast every `cadence_min` across every contiguous crop block.

    Two replayed episodes make an anecdote, not a score. Verification needs a
    sample, and the stored crops already cover ~46 hours of the two storms, so
    walk them on a fixed cadence and let `verify` sort out which ones mattered.

    Stored under kind='sweep' and without the ensemble: these exist to be
    scored, not read, and the two `replay` rows stay as they were.
    """
    con = connect()
    blocks = crop_blocks(con)
    lead_in = timedelta(minutes=N_FRAMES * 2)      # crops are 2-minutely
    todo = []
    for t0, t1, _ in blocks:
        t = t0 + lead_in
        while t <= t1:
            todo.append(t)
            t += timedelta(minutes=cadence_min)
    print(f"{len(blocks)} crop blocks -> {len(todo)} issue points "
          f"at {cadence_min}-min cadence", flush=True)
    ok = skipped = failed = 0
    t_start = time.time()
    for k, issue in enumerate(todo, 1):
        try:
            run_one(con, issue, kind=kind, with_hrrr=False, quiet=True,
                    n_ens=n_ens)
            ok += 1
        except ValueError as e:                     # not contiguous / too few
            skipped += 1
            print(f"  [{k}/{len(todo)}] {issue:%m-%d %H:%M}Z skipped: {e}", flush=True)
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  [{k}/{len(todo)}] {issue:%m-%d %H:%M}Z FAILED: "
                  f"{type(e).__name__}: {str(e)[:90]}", flush=True)
        if k % 10 == 0 or k == len(todo):
            el = time.time() - t_start
            print(f"  [{k}/{len(todo)}] {ok} stored  {el/k:.1f}s/nowcast  "
                  f"eta {(len(todo)-k)*el/k/60:.0f} min", flush=True)
    print(f"\nsweep done: {ok} stored, {skipped} skipped, {failed} failed "
          f"({(time.time()-t_start)/60:.1f} min)")
    con.close()


# ---------------------------------------------------------------- verify
VERIFY_LEADS = (10, 20, 30, 40, 50, 60, 70, 80, 90)
WET_INHR = 0.06                  # 0.01 in inside a 10-minute slot


def _gauge_inhr():
    """Ambient gauge as a 10-minute step function in in/hr."""
    import pandas as pd
    tl = pd.read_parquet(HERE.parent / "analysis" / "timeline.parquet")
    tl.index = pd.to_datetime(tl.index, utc=True)
    # build_timeline sums rain into the bin, so the slot labelled T covers
    # [T, T+10) and 6x turns the 10-minute total into a rate.
    return tl["rain_in"] * 6.0


def _radar_inhr(con):
    """MRMS station pixel on the same 10-minute clock, in in/hr."""
    import pandas as pd
    df = pd.read_sql("SELECT valid_utc, station FROM frames"
                     " WHERE product='PrecipRate' AND station IS NOT NULL"
                     " ORDER BY valid_utc", con)
    if df.empty:
        return None
    s = pd.Series(df.station.to_numpy() / MM,
                  index=pd.to_datetime(df.valid_utc, utc=True))
    return s.resample("10min").mean()


def _slot_mean(step, t0, t1):
    """Mean of a 10-minute step function over [t0, t1).

    Issue times sit on the 2-minute radar clock, so the scoring window rarely
    aligns with the gauge's 10-minute bins. Sampling the step function every
    minute weights the straddled bins correctly. The tolerance stops a forward
    fill from carrying a value across one of the store's month-long gaps.
    """
    import numpy as np
    import pandas as pd
    if step is None:
        return float("nan")
    idx = pd.date_range(t0, t1, freq="1min", inclusive="left", tz="UTC")
    v = step.reindex(idx, method="ffill", tolerance=pd.Timedelta("10min"))
    return float(v.mean()) if bool(v.notna().all()) else float("nan")


def verify(kinds=("live", "replay", "sweep")):
    """Score stored nowcasts against the gauge, and against MRMS itself."""
    import numpy as np
    con = connect()
    gauge = _gauge_inhr()
    radar = _radar_inhr(con)
    rows = con.execute(
        "SELECT issued_utc, kind, curve_json FROM nowcasts"
        f" WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY issued_utc",
        tuple(kinds)).fetchall()
    if not rows:
        print("no nowcasts stored")
        return
    out, n_nan = [], 0
    for issued_utc, kind, curve_json in rows:
        issued = datetime.strptime(issued_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        curve = json.loads(curve_json)
        leads = np.array([c["lead"] for c in curve], dtype=float)
        det = np.array([c["det_inhr"] for c in curve], dtype=float)
        for L in VERIFY_LEADS:
            # The prediction is an instantaneous rate every 5 min; average it
            # over the same 10-minute window the observation covers.
            tt = np.arange(L - 9, L + 1, dtype=float)
            pred = float(np.interp(tt, leads, det).mean())
            t1 = issued + timedelta(minutes=L)
            t0 = t1 - timedelta(minutes=10)
            g = _slot_mean(gauge, t0, t1)
            m = _slot_mean(radar, t0, t1)
            if np.isnan(g):
                n_nan += 1
                continue
            out.append((issued_utc, kind, L, pred, g,
                        None if np.isnan(m) else m))
    con.executemany(
        "INSERT OR REPLACE INTO verification (issued_utc,kind,lead_min,"
        "pred_inhr,obs_gauge_inhr,obs_mrms_inhr) VALUES (?,?,?,?,?,?)", out)
    con.commit()
    print(f"verified {len(rows)} nowcasts -> {len(out)} scored pairs"
          + (f" ({n_nan} dropped: no gauge coverage)" if n_nan else ""))
    verify_report(con)
    con.close()


def verify_report(con=None):
    """Skill by lead time, against persistence.

    Persistence -- "it keeps doing what it is doing now" -- is the baseline a
    nowcast has to beat. Bias and RMSE alone can look respectable simply because
    most 10-minute slots are dry.
    """
    import numpy as np
    import pandas as pd
    close = con is None
    con = con or connect()
    df = pd.read_sql("SELECT * FROM verification", con)
    if df.empty:
        print("verification table is empty -- run `verify` first")
        if close:
            con.close()
        return

    # Persistence for an issue time is the observed rate in the 10 minutes
    # ending at issue, which is the last thing the forecaster could have seen.
    gauge = _gauge_inhr()
    base = {u: _slot_mean(gauge,
                          datetime.strptime(u, "%Y-%m-%dT%H:%M:%SZ").replace(
                              tzinfo=timezone.utc) - timedelta(minutes=10),
                          datetime.strptime(u, "%Y-%m-%dT%H:%M:%SZ").replace(
                              tzinfo=timezone.utc))
            for u in df.issued_utc.unique()}
    df["persist_inhr"] = df.issued_utc.map(base)

    print("=" * 82)
    print("NOWCAST VERIFICATION AT THE HOUSE PIXEL")
    print("=" * 82)
    print(f"  {df.issued_utc.nunique()} nowcasts, {len(df)} scored pairs")
    print(f"  {df.issued_utc.min()} -> {df.issued_utc.max()}")
    print("  units are in/hr averaged over the 10 min ending at the lead")

    print()
    print("  BY LEAD TIME  (vs the Ambient gauge)")
    print(f"    {'lead':>5}{'n':>6}{'obs':>8}{'pred':>8}{'bias':>9}"
          f"{'MAE':>8}{'RMSE':>8}{'r':>7}{'persist':>9}{'skill':>8}")
    for L, g in df.groupby("lead_min"):
        d = g.pred_inhr - g.obs_gauge_inhr
        rmse = float(np.sqrt((d ** 2).mean()))
        pv = g.dropna(subset=["persist_inhr"])
        prmse = float(np.sqrt(((pv.persist_inhr - pv.obs_gauge_inhr) ** 2).mean())) \
            if len(pv) else float("nan")
        skill = 1 - rmse / prmse if prmse else float("nan")
        r = float(np.corrcoef(g.pred_inhr, g.obs_gauge_inhr)[0, 1]) \
            if g.obs_gauge_inhr.std() > 0 and g.pred_inhr.std() > 0 else float("nan")
        print(f"    {L:>5}{len(g):>6}{g.obs_gauge_inhr.mean():>8.4f}"
              f"{g.pred_inhr.mean():>8.4f}{d.mean():>+9.4f}{d.abs().mean():>8.4f}"
              f"{rmse:>8.4f}{r:>7.3f}{prmse:>9.4f}{skill:>+8.3f}")

    wet = df[df.obs_gauge_inhr >= WET_INHR]
    if len(wet):
        print()
        print(f"  RESTRICTED TO SLOTS THE GAUGE CALLED WET  (n={len(wet)})")
        print(f"    {'lead':>5}{'n':>6}{'obs':>8}{'pred':>8}{'bias':>9}{'MAE':>8}{'r':>7}")
        for L, g in wet.groupby("lead_min"):
            d = g.pred_inhr - g.obs_gauge_inhr
            r = float(np.corrcoef(g.pred_inhr, g.obs_gauge_inhr)[0, 1]) \
                if len(g) > 2 and g.obs_gauge_inhr.std() > 0 and g.pred_inhr.std() > 0 \
                else float("nan")
            print(f"    {L:>5}{len(g):>6}{g.obs_gauge_inhr.mean():>8.4f}"
                  f"{g.pred_inhr.mean():>8.4f}{d.mean():>+9.4f}"
                  f"{d.abs().mean():>8.4f}{r:>7.3f}")

    print()
    print(f"  DETECTION OF A WET 10-MIN SLOT  (>= 0.01 in, i.e. {WET_INHR} in/hr)")
    print(f"    {'lead':>5}{'hit':>6}{'miss':>6}{'FA':>6}{'POD':>7}{'FAR':>7}{'CSI':>7}")
    for L, g in df.groupby("lead_min"):
        gw = g.obs_gauge_inhr >= WET_INHR
        pw = g.pred_inhr >= WET_INHR
        hit = int((gw & pw).sum())
        miss = int((gw & ~pw).sum())
        fa = int((~gw & pw).sum())
        pod = hit / (hit + miss) if hit + miss else float("nan")
        far = fa / (hit + fa) if hit + fa else float("nan")
        csi = hit / (hit + miss + fa) if hit + miss + fa else float("nan")
        print(f"    {L:>5}{hit:>6}{miss:>6}{fa:>6}{pod:>7.3f}{far:>7.3f}{csi:>7.3f}")

    mr = df.dropna(subset=["obs_mrms_inhr"])
    if len(mr):
        print()
        print("  CEILING CHECK -- the same score against MRMS's own station pixel")
        print("  (how well the nowcast reproduces the radar it was built from,")
        print("   which bounds what it could ever score against the gauge)")
        print(f"    {'lead':>5}{'n':>6}{'bias':>10}{'RMSE':>9}{'r':>7}")
        for L, g in mr.groupby("lead_min"):
            d = g.pred_inhr - g.obs_mrms_inhr
            r = float(np.corrcoef(g.pred_inhr, g.obs_mrms_inhr)[0, 1]) \
                if g.obs_mrms_inhr.std() > 0 and g.pred_inhr.std() > 0 else float("nan")
            print(f"    {L:>5}{len(g):>6}{d.mean():>+10.4f}"
                  f"{float(np.sqrt((d**2).mean())):>9.4f}{r:>7.3f}")
    if close:
        con.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    con = connect()
    if cmd == "run":
        s = run_one(con, datetime.now(timezone.utc))
        print(f"nowcast issued {s['issued']:%Y-%m-%d %H:%M}Z")
        print(fmt(s))
    elif cmd == "replay":
        replay(sys.argv[2:] or list(EPISODES))
    elif cmd == "sweep":
        sweep(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif cmd == "verify":
        verify()
    elif cmd == "score":
        verify_report()
    else:
        print(f"unknown command {cmd!r}; "
              "expected run | replay | sweep | verify | score")
