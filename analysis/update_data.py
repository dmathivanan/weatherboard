#!/usr/bin/env python3
"""
Keep the raw inputs current.

Everything the analysis reads is a tracked file, so a cloud session sees the
same data as the laptop. This script extends each one from its source:

  imports/station.csv          5-min station rain            Ambient API      station
  imports/bridge_street.csv    creek stage at Bridge Street  Marin OneRain    bridge
  analysis/cache/usgs_*.json   Ross stage and flow by month  USGS             usgs
  imports/PumpFuse.csv         one row per pump run          app export only  pump FILE

    python analysis/update_data.py                 # station + bridge + usgs, then status
    python analysis/update_data.py station bridge  # just those
    python analysis/update_data.py pump imports/pump_runs_2026-11-19.csv
    python analysis/update_data.py status

Each source is appended after its own last reading, so re-running is cheap and
never rewrites history. The PumpFuse has no API (config.json, sump._note): its
runs arrive only as a CSV exported from the app, merged here by timestamp.

Hosts needed: rt.ambientweather.net, marin.onerain.com, waterservices.usgs.gov.
"""
import importlib.util
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fetchers as F          # noqa: E402  (keys, User-Agent, err_text)
import build_timeline as bt   # noqa: E402  (USGS month cache)

IMPORTS = ROOT / "imports"
STATION = IMPORTS / "station.csv"
BRIDGE = IMPORTS / "bridge_street.csv"
PUMP = IMPORTS / "PumpFuse.csv"
TZ = "America/Los_Angeles"
AMBIENT = "https://rt.ambientweather.net/v1/devices"
STATION_COLS = ["Date", "Rain Rate (in/hr)", "Event Rain (in)", "Daily Rain (in)", "Yearly Rain (in)"]
PUMP_COLS = ["Time", "Power(w)", "Duration", "Min(w)", "Max(w)", "Std.Dev"]
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _ambient(url, params, tries=5):
    """One Ambient call. The API allows 1 request/second per key and answers a
    faster one with 429, so wait and retry. Errors go through err_text because
    requests puts the keys (they travel in the query string) in its messages."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=F.UA, timeout=30)
            if r.status_code == 429 and i < tries - 1:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == tries - 1:
                raise RuntimeError(F.err_text(e)) from None
            time.sleep(2 + 2 * i)


# ------------------------------------------------------------------ station
def update_station():
    cur = pd.read_csv(STATION, low_memory=False)
    last = pd.to_datetime(cur["Date"], utc=True, format="ISO8601").max()
    keys = F._ambient_keys()
    mac = _ambient(AMBIENT, keys)[0]["macAddress"]

    rows, end, pages = {}, datetime.now(timezone.utc), 0
    while end > last:
        time.sleep(1.1)
        recs = _ambient(f"{AMBIENT}/{mac}", {**keys, "limit": 288,
                                             "endDate": int(end.timestamp() * 1000)})
        pages += 1
        if not recs:
            break
        for r in recs:
            t = datetime.fromtimestamp(r["dateutc"] / 1000, tz=timezone.utc)
            if t > last:
                rows[t] = (r.get("hourlyrainin"), r.get("eventrainin"),
                           r.get("dailyrainin"), r.get("yearlyrainin"))
        oldest = datetime.fromtimestamp(min(r["dateutc"] for r in recs) / 1000, tz=timezone.utc)
        if oldest >= end:                      # no progress: stop rather than loop
            break
        end = oldest
        if pages % 20 == 0:
            print(f"  station: back to {end:%Y-%m-%d} ({len(rows)} new rows)", file=sys.stderr)

    if rows:
        new = pd.DataFrame([(t.strftime(ISO), *v) for t, v in sorted(rows.items())],
                           columns=STATION_COLS)
        pd.concat([cur[STATION_COLS], new]).to_csv(STATION, index=False, lineterminator="\n")
    print(f"station: +{len(rows)} readings ({pages} requests)")


# ------------------------------------------------------------------- bridge
def _bridge_downloader():
    spec = importlib.util.spec_from_file_location("download_bridge_street",
                                                  IMPORTS / "download_bridge_street.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def update_bridge():
    cur = pd.read_csv(BRIDGE)
    last = pd.to_datetime(cur["datetime_utc"], utc=True, format="ISO8601").max()
    # OneRain takes local wall-clock strings; overlap a day and filter on the epoch.
    fmt = "%Y-%m-%d %H:%M:%S"
    start = (last - pd.Timedelta(days=1)).tz_convert(TZ).strftime(fmt)
    end = (pd.Timestamp.now(tz=TZ) + pd.Timedelta(days=1)).strftime(fmt)
    _, pairs = _bridge_downloader().fetch_pairs(start, end)
    new = {ep: v for ep, v in pairs if ep / 1000 > last.timestamp()}
    if new:
        add = pd.DataFrame(
            [(datetime.fromtimestamp(ep / 1000, tz=timezone.utc).strftime(ISO), v)
             for ep, v in sorted(new.items())], columns=["datetime_utc", "stage_ft"])
        pd.concat([cur, add]).to_csv(BRIDGE, index=False, lineterminator="\n")
    print(f"bridge:  +{len(new)} readings")


# --------------------------------------------------------------------- usgs
def water_year_start(now=None):
    now = now or pd.Timestamp.now(tz="UTC")
    return pd.Timestamp(year=now.year if now.month >= 10 else now.year - 1, month=10, day=1, tz="UTC")


def update_usgs():
    """Month files for the current water year. Finished months are fetched
    once; the month in progress is re-fetched every time."""
    now = pd.Timestamp.now(tz="UTC")
    months = pd.date_range(water_year_start(now).tz_localize(None), now.tz_localize(None), freq="MS")
    before = {f.name for f in bt.CACHE.glob("usgs_*.json")}
    df, _ = bt.load_usgs(since=water_year_start(now), months=months, refresh_current=True)
    after = {f.name for f in bt.CACHE.glob("usgs_*.json")}
    print(f"usgs:    {len(after - before)} new month file(s), current month refreshed; "
          f"{len(df)} readings this water year")


# --------------------------------------------------------------------- pump
_PUMP_ROW = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[\s,;]+"
                       r"(-?[\d.]+)[\s,;]+(-?[\d.]+)[\s,;]+(-?[\d.]+)[\s,;]+(-?[\d.]+)[\s,;]+(-?[\d.]+)\s*$")


def read_pump_export(path):
    """Rows of a PumpFuse export. The app writes tab-separated text, but an
    export pasted through a chat arrives with the tabs turned into spaces, so
    match each row by shape instead of trusting the separator."""
    rows, skipped = [], 0
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = _PUMP_ROW.match(line)
        if m:
            rows.append((m.group(1).replace("T", " "),) + m.groups()[1:])
        elif line.strip() and not line.lstrip().lower().startswith("time"):
            skipped += 1
    if not rows:
        raise SystemExit(f"{path}: no pump runs found. Expected rows like "
                         "'2026-11-19 09:34:34  1410  15  1  1476  248.63' (" + ", ".join(PUMP_COLS) + ")")
    if skipped:
        print(f"pump:    ignored {skipped} line(s) that are not run rows", file=sys.stderr)
    return pd.DataFrame(rows, columns=PUMP_COLS)


def merge_pump(path):
    new = read_pump_export(path)
    TAB, NL = chr(9), chr(10)
    cur = pd.read_csv(PUMP, sep=TAB, dtype=str)
    out = pd.concat([cur, new]).drop_duplicates("Time")
    out = (out.assign(_t=pd.to_datetime(out["Time"]))
              .sort_values("_t", ascending=False).drop(columns="_t"))
    out.to_csv(PUMP, sep=TAB, index=False, lineterminator=NL)
    t = pd.to_datetime(new["Time"])
    print(f"pump:    +{len(out) - len(cur)} new runs from {Path(path).name} "
          f"({t.min():%Y-%m-%d} -> {t.max():%Y-%m-%d}); {len(out)} runs in the log")


# ------------------------------------------------------------------- status
def status():
    now = pd.Timestamp.now(tz="UTC")

    def line(name, first, last, n, note=""):
        age_h = (now - last).total_seconds() / 3600
        age = f"{age_h:.0f} h ago" if age_h < 72 else f"{age_h / 24:.0f} days ago"
        print(f"  {name:<14}{n:>8,} rows   {first.tz_convert(TZ):%Y-%m-%d} -> "
              f"{last.tz_convert(TZ):%Y-%m-%d %H:%M}  ({age}){note}")

    print("data on disk (local time):")
    s = pd.to_datetime(pd.read_csv(STATION, usecols=["Date"])["Date"], utc=True, format="ISO8601")
    line("station rain", s.min(), s.max(), len(s))
    b = pd.to_datetime(pd.read_csv(BRIDGE)["datetime_utc"], utc=True, format="ISO8601")
    line("Bridge Street", b.min(), b.max(), len(b), "  event-based: 12 h apart when flat")
    months = sorted(pd.Timestamp(f.stem.split("_")[1] + "01") for f in bt.CACHE.glob("usgs_*.json"))
    u, _ = bt.load_usgs(since=pd.Timestamp("2000-01-01", tz="UTC"), months=months)
    line("Ross (USGS)", u.index.min(), u.index.max(), len(u))
    p = pd.to_datetime(pd.read_csv(PUMP, sep="\t")["Time"]).dt.tz_localize(
        TZ, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")
    line("pump runs", p.min(), p.max(), len(p), "  manual export: ask for a new one when stale")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["pump"]:
        if len(args) != 2:
            raise SystemExit("usage: update_data.py pump <export.csv>")
        merge_pump(args[1])
    elif args[:1] != ["status"]:
        steps = {"station": update_station, "bridge": update_bridge, "usgs": update_usgs}
        failed = []
        for name in (args or list(steps)):
            if name not in steps:
                raise SystemExit(f"unknown source {name!r}; expected {' | '.join(steps)} | pump FILE | status")
            try:
                steps[name]()
            except Exception as e:  # noqa: BLE001  one dead source must not stop the others
                failed.append(name)
                print(f"{name}: FAILED  {F.err_text(e)}")
        if failed:
            print(f"\nnot updated: {', '.join(failed)}")
    status()
