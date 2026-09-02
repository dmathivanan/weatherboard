#!/usr/bin/env python3
"""
One-time download of last winter's San Anselmo Creek at Bridge Street stage
(Marin County OneRain gauge "San Anselmo - FS 19", site 38036 / device 5).

The public OneRain graph embeds the full event-based series in the page HTML as
flot [epoch_ms, value] pairs. A plain GET (no XHR header) returns all of it;
the authenticated /export/file/ CSV endpoint is 401 for guests, so we parse the
graph page instead. Timestamps are true UTC (verified against NWPS SBSC1).

Writes imports/bridge_street.csv  ->  datetime_utc,stage_ft
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "bridge_street.csv"

import os
UA = {"User-Agent": "floodboard-52woodland personal flood monitor "
                    f"({os.environ.get('FLOODBOARD_CONTACT', 'personal flood monitor')})"}
SITE_ID, DEVICE_ID = 16807, 5
DEVICE_UUID = "30b998de-0ca7-49ad-b3fd-426f97ba0b24"
THRESHOLDS = {11.3, 13.3, 16.3, 17.8}  # flood-category lines the graph also embeds

WIN_START = "2025-11-01 00:00:00"
WIN_END = "2026-04-30 23:59:00"


def graph_url(start, end):
    return (
        "https://marin.onerain.com/graph/"
        f"?time_zone=US/Pacific&site_id={SITE_ID}&device_id={DEVICE_ID}"
        f"&device={DEVICE_UUID}&bin=0&range=custom"
        f"&data_start={start}&data_end={end}"
        "&show_raw=true&legend=false&thresholds=false&markers=true"
        f"&devices[]={SITE_ID}|{DEVICE_ID}"
    )


def fetch_pairs(start, end):
    r = requests.get(graph_url(start, end), headers=UA, timeout=60)
    r.raise_for_status()
    html = r.text
    # capture value as a string so we can tell threshold lines (many decimals,
    # e.g. 11.30000000) from real readings (<=2 decimals).
    pairs = []
    for ep, val in re.findall(r"\[(\d{12,}),(-?\d+(?:\.\d+)?)\]", html):
        f = float(val)
        decimals = len(val.split(".")[1]) if "." in val else 0
        if decimals > 2 or f in THRESHOLDS:
            continue  # threshold guide-line, not an observation
        pairs.append((int(ep), f))
    return html, pairs


def main():
    html, pairs = fetch_pairs(WIN_START, WIN_END)
    # dedupe by timestamp, sort chronologically
    seen = {}
    for ep, v in pairs:
        seen[ep] = v
    rows = sorted(seen.items())
    if not rows:
        print(f"NO DATA parsed (html {len(html)} bytes). Aborting.", file=sys.stderr)
        return 1

    with OUT.open("w", newline="") as f:
        f.write("datetime_utc,stage_ft\n")
        for ep, v in rows:
            dt = datetime.fromtimestamp(ep / 1000, tz=timezone.utc)
            f.write(f"{dt.strftime('%Y-%m-%dT%H:%M:%SZ')},{v}\n")

    vals = [v for _, v in rows]
    peak_ep, peak_v = max(rows, key=lambda kv: kv[1])
    peak_dt = datetime.fromtimestamp(peak_ep / 1000, tz=timezone.utc)
    print(f"wrote {OUT} : {len(rows)} rows")
    print(f"  range   {datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc):%Y-%m-%d} "
          f"-> {datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc):%Y-%m-%d}")
    print(f"  stage   min {min(vals)}  max {max(vals)} ft (peak {peak_dt:%Y-%m-%d %H:%MZ})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
