#!/usr/bin/env python3
"""
Stage 1 live ingest — poll MRMS and keep the crop, station pixel and basin mean.

Cadence follows the weather, not the clock: 4 minutes while there is any rain in
the 150-mile box, 20 minutes when the box is dry. MRMS PrecipRate updates every
2 minutes, so a 4-minute poll sees every other frame during rain, which is what
the nowcast motion field needs, and a dry poll costs one file per 20 minutes.

    python nowcast/ingest.py once     # single cycle, for cron
    python nowcast/ingest.py loop     # run continuously

Real-time frames come from NCEP (about 48 h of retention); anything older falls
back to the IEM archive automatically.
"""
import sys
import time
from datetime import datetime, timedelta, timezone

import mrms

WET_INTERVAL_MIN = 4
DRY_INTERVAL_MIN = 20
WET_THRESHOLD_MM_HR = 0.1        # anything at or above this counts as "rain in the box"
QPE_EVERY_MIN = 20               # RadarOnly_QPE_01H changes slowly; no need for 4 min


def latest_slot(lag_min=4):
    """Most recent 2-minute slot likely to be published (MRMS lags a few minutes)."""
    return mrms.slot(datetime.now(timezone.utc) - timedelta(minutes=lag_min))


def pull(con, mask, product, when):
    """Fetch one frame, preferring real-time NCEP and falling back to the archive."""
    for archive in (False, True):
        try:
            raw = mrms.fetch(product, when, archive=archive)
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise last
    grid = mrms.decode(raw)
    summ, box = mrms.summarise(grid, mask)
    mrms.store(con, product, when, summ, box)
    return summ


def cycle(con, mask, last_qpe=None):
    """One poll. Returns (summary, next_interval_minutes)."""
    when = latest_slot()
    iso = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = con.execute("SELECT station FROM frames WHERE product='PrecipRate'"
                      " AND valid_utc=?", (iso,)).fetchone()
    if row:
        summ = {"station": row[0], "box_max": None}
        note = "already stored"
    else:
        summ = pull(con, mask, "PrecipRate", when)
        note = "fetched"

    wet = (summ.get("box_max") or 0) >= WET_THRESHOLD_MM_HR
    interval = WET_INTERVAL_MIN if wet else DRY_INTERVAL_MIN

    # The hourly QPE is a slow field; pull it on its own cadence.
    if last_qpe is None or (datetime.now(timezone.utc) - last_qpe
                            >= timedelta(minutes=QPE_EVERY_MIN)):
        try:
            pull(con, mask, "RadarOnly_QPE_01H", when)
            last_qpe = datetime.now(timezone.utc)
        except Exception as e:  # noqa: BLE001
            print(f"  QPE_01H unavailable: {type(e).__name__}", flush=True)

    con.execute("INSERT OR REPLACE INTO poll_log (ts_utc,wet,interval_min,note)"
                " VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 int(wet), interval, note))
    con.commit()
    return summ, interval, wet, last_qpe, when


def main(mode="once"):
    con = mrms.connect()
    mask = mrms.basin_mask()
    last_qpe = None
    while True:
        t0 = time.time()
        try:
            summ, interval, wet, last_qpe, when = cycle(con, mask, last_qpe)
            sta = summ.get("station")
            print(f"{when:%Y-%m-%d %H:%M}Z  station "
                  f"{'--' if sta is None else f'{sta/mrms.MM_PER_IN:.3f} in/hr'}"
                  f"  box_max {summ.get('box_max') or 0:.1f} mm/hr"
                  f"  basin {(summ.get('basin_mean') or 0)/mrms.MM_PER_IN:.3f} in/hr"
                  f"  -> {'WET' if wet else 'dry'}, next in {interval} min", flush=True)
        except Exception as e:  # noqa: BLE001
            interval = DRY_INTERVAL_MIN
            print(f"cycle failed: {type(e).__name__}: {e}", flush=True)
        if mode != "loop":
            break
        time.sleep(max(interval * 60 - (time.time() - t0), 30))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "once")
