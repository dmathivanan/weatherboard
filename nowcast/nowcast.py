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
    python nowcast/nowcast.py verify                 # score past nowcasts
"""
import json
import sqlite3
import sys
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
    try:
        steps = nowcasts.get_method("steps")
        ens_in = steps(dbr[-3:], V, n_in, n_ens_members=n_ens, n_cascade_levels=6,
                       precip_thr=-10.0, kmperpixel=1.0, timestep=dt_min,
                       noise_method="nonparametric", vel_pert_method=None,
                       mask_method="incremental", seed=42)
        ens = from_dbr(ens_in)
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
def run_one(con, issue_time, kind="live", with_hrrr=True, quiet=False):
    times, stack = load_crops(con, issue_time)
    det, ens, V, dt_min, house = make_nowcast(stack, times)
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    con = connect()
    if cmd == "run":
        s = run_one(con, datetime.now(timezone.utc))
        print(f"nowcast issued {s['issued']:%Y-%m-%d %H:%M}Z")
        print(fmt(s))
    elif cmd == "replay":
        replay(sys.argv[2:] or list(EPISODES))
