#!/usr/bin/env python3
"""
Feed fetchers for the 52 Woodland storm planner.

Lifted from the retired poller.py so the six-hour forecast job and any offline
analysis read the same sources the same way. Every function returns a plain
dict and raises on failure; callers wrap them in safe() so one dead feed never
takes the others down.

Sources
  Ambient Weather      station rain (needs AMBIENT_APP_KEY / AMBIENT_API_KEY)
  USGS 11460000        Corte Madera Creek at Ross, stage + flow, last 7 days
  NWPS SBSC1 / CMDC1   Bridge Street and Ross gauges with NWS flood categories
  NWS api.weather.gov  gridded QPF + active alerts for the house
  Open-Meteo           hourly rain, past days + 7-day forecast, per-model too
  CNRFC                6-hourly point QPF, days 1-6 (nearest points, see config)
  CW3E                 maximum forecast AR Scale at the nearest coastal points
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
LAT, LON = CFG["lat"], CFG["lon"]
TZ = ZoneInfo(CFG["timezone"])
CONTACT = os.environ.get("FLOODBOARD_CONTACT", "personal flood monitor")
UA = {"User-Agent": f"floodboard-52woodland storm planner ({CONTACT})"}


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r.json()


def get_backoff(url, headers=UA, attempts=3, timeout=25, **kw):
    """GET with a short exponential backoff; returns the Response or raises the last error."""
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(1 + 2 * i)
    raise last


def safe(fn, *a, **kw):
    """Run a fetcher; return (result, error) so one failure never kills the run."""
    try:
        return fn(*a, **kw), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:200]


# ---------------------------------------------------------------- Ambient

def _ambient_keys():
    app, key = os.environ.get("AMBIENT_APP_KEY"), os.environ.get("AMBIENT_API_KEY")
    if not (app and key):
        raise RuntimeError("AMBIENT keys not set")
    return {"applicationKey": app, "apiKey": key}


def fetch_ambient():
    """Current station snapshot: rain counters, temperature, wind, pressure."""
    devices = get("https://rt.ambientweather.net/v1/devices", params=_ambient_keys())
    d = devices[0]["lastData"]
    return {
        "observed_utc": d.get("date"),
        "rain_rate_inhr": d.get("hourlyrainin"),
        "rain_event_in": d.get("eventrainin"),
        "rain_daily_in": d.get("dailyrainin"),
        "rain_weekly_in": d.get("weeklyrainin"),
        "rain_monthly_in": d.get("monthlyrainin"),
        "rain_yearly_in": d.get("yearlyrainin"),
        "baro_inhg": d.get("baromrelin"),
        "temp_f": d.get("tempf"),
        "humidity_pct": d.get("humidity"),
        "wind_mph": d.get("windspeedmph"),
        "wind_gust_mph": d.get("windgustmph"),
        "device": devices[0].get("info", {}).get("name"),
        "mac": devices[0].get("macAddress"),
    }


def fetch_ambient_daily(days, mac=None):
    """Station rain per LOCAL calendar day for the last `days` days, ending today.

    Ambient's dailyrainin counter resets at local midnight, so the day's total is
    the maximum of the counter inside that local day. One 288-record page (a
    full day at 5 min) is pulled per day, newest first, at the 1 request/s the
    API allows. Returns {"YYYY-MM-DD": inches} plus the mac used.
    """
    keys = _ambient_keys()
    if mac is None:
        mac = get("https://rt.ambientweather.net/v1/devices", params=keys)[0]["macAddress"]
        time.sleep(1.1)
    today = datetime.now(TZ).date()
    out = {}
    for i in range(days):
        day = today - timedelta(days=i)
        end_local = datetime.combine(day, datetime.max.time()).replace(tzinfo=TZ)
        if i == 0:
            end_local = datetime.now(TZ)
        end_ms = int(end_local.timestamp() * 1000)
        recs = get(f"https://rt.ambientweather.net/v1/devices/{mac}",
                   params={**keys, "limit": 288, "endDate": end_ms})
        vals = []
        for r in recs:
            try:
                t = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).astimezone(TZ)
            except Exception:  # noqa: BLE001
                continue
            if t.date() == day and r.get("dailyrainin") is not None:
                vals.append(float(r["dailyrainin"]))
        if vals:
            out[day.isoformat()] = round(max(vals), 3)
        time.sleep(1.1)
    return {"days": out, "mac": mac}


# ---------------------------------------------------------------- creek gauges

def fetch_usgs(period="P7D"):
    """Ross: stage and flow series over `period`, plus the latest reading."""
    j = get("https://waterservices.usgs.gov/nwis/iv/",
            params={"format": "json", "sites": CFG["usgs_site"],
                    "parameterCd": "00065,00060", "siteStatus": "all", "period": period})
    out = {"series": {}}
    for ts in j["value"]["timeSeries"]:
        code = ts["variable"]["variableCode"][0]["value"]
        vals = [v for v in ts["values"][0]["value"] if float(v["value"]) > -900]
        if not vals:
            continue
        key = "stage_ft" if code == "00065" else "flow_cfs"
        out[key] = float(vals[-1]["value"])
        out["observed"] = vals[-1]["dateTime"]
        out["series"][key] = [{"t": v["dateTime"], "v": float(v["value"])} for v in vals]
    return out


def _gauge(lid, headers=UA):
    base = f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
    meta = get_backoff(base, headers).json()
    sf = get_backoff(base + "/stageflow", headers).json()
    cats = {k: v.get("stage") for k, v in ((meta.get("flood") or {}).get("categories") or {}).items()
            if isinstance(v, dict)}
    obs = [p for p in ((sf.get("observed") or {}).get("data") or [])
           if (p.get("primary") if p.get("primary") is not None else -999) > -900]
    fc = (sf.get("forecast") or {}).get("data") or []
    return meta, cats, obs, fc


def fetch_nwps(lid=None):
    """An NWPS gauge (default: Ross CMDC1): latest stage, categories, NWS forecast."""
    lid = lid or CFG["nwps_gauge"]
    meta, cats, obs, fc = _gauge(lid)
    return {
        "lid": lid, "name": meta.get("name"), "categories": cats,
        "latest_stage_ft": obs[-1].get("primary") if obs else None,
        "observed": obs[-1].get("validTime") if obs else None,
        "forecast_peak_ft": max((p.get("primary") or 0) for p in fc) if fc else None,
        "forecast": [{"t": p["validTime"], "stage": p.get("primary")} for p in fc[:48]],
    }


def fetch_bridge_street():
    """San Anselmo Creek at Bridge Street (NWPS SBSC1), the gauge that floods first."""
    cfg = CFG.get("bridge_street", {})
    out = fetch_nwps(cfg.get("nwps_lid", "SBSC1"))
    out["stage_ft"] = out.pop("latest_stage_ft")
    out["town_stages"] = cfg.get("town_stages")
    out["url"] = CFG.get("bridge_street_gauge_url")
    return out


# ---------------------------------------------------------------- NWS

def fetch_nws():
    """Gridded QPF periods (inches, with ISO durations) and active alerts for the house."""
    pt = get(f"https://api.weather.gov/points/{LAT},{LON}")["properties"]
    grid = get(pt["forecastGridData"])["properties"]
    qpf = []
    for v in grid.get("quantitativePrecipitation", {}).get("values", []):
        start, dur = v["validTime"].split("/")
        qpf.append({"t": start, "dur": dur, "in": round((v["value"] or 0) / 25.4, 3)})
    alerts = get("https://api.weather.gov/alerts/active", params={"point": f"{LAT},{LON}"})
    al = [{
        "event": a["properties"].get("event"),
        "severity": a["properties"].get("severity"),
        "headline": a["properties"].get("headline"),
        "ends": a["properties"].get("ends") or a["properties"].get("expires"),
    } for a in alerts.get("features", [])]
    return {"qpf_periods": qpf, "alerts": al,
            "office": pt.get("cwa"), "forecast_zone": pt.get("forecastZone"),
            "updated": grid.get("updateTime")}


# ---------------------------------------------------------------- Open-Meteo

def fetch_open_meteo(past_days=0, forecast_days=7):
    """Hourly rain (inches, local time) from `past_days` ago through `forecast_days` ahead."""
    j = get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": LAT, "longitude": LON,
        "hourly": "precipitation,precipitation_probability",
        "precipitation_unit": "inch", "timezone": CFG["timezone"],
        "past_days": past_days, "forecast_days": forecast_days,
    })
    h = j["hourly"]
    return {"hourly": [{"t": t, "in": p or 0, "prob": pr}
                       for t, p, pr in zip(h["time"], h["precipitation"],
                                           h["precipitation_probability"])],
            "generated": j.get("generationtime_ms")}


def fetch_model_qpf():
    """Open-Meteo per-model precipitation: ECMWF, GFS and HRRR side by side (24 h / 6 h)."""
    out = {}
    for key, model, hours in (("ecmwf", "ecmwf_ifs025", 24),
                              ("gfs", "gfs_seamless", 24),
                              ("hrrr", "ncep_hrrr_conus", 6)):
        try:
            j = get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": LAT, "longitude": LON, "models": model,
                "hourly": "precipitation", "precipitation_unit": "inch",
                "timezone": "UTC", "forecast_days": 2})
            pr = [x or 0 for x in j["hourly"]["precipitation"][:hours]]
            out[f"qpf_{key}_{hours}h_in"] = round(sum(pr), 3)
        except Exception as e:  # noqa: BLE001
            out[f"qpf_{key}_{hours}h_in"] = None
            out.setdefault("errors", {})[key] = f"{type(e).__name__}: {e}"[:120]
    return out


# ---------------------------------------------------------------- CNRFC

_BR = re.compile(r"\.BR RSA (\d{8}) Z /DC\d+")
_DH = re.compile(r"/DH(\d{2})/")


def _parse_cnrfc_product(text):
    """Parse an HD8RSA / HD4RSA point-QPF product into {point: [(end_utc, inches), ...]}.

    The product is SHEF .B format: values are stamped at the END of each 6-hour
    period, the first ending at DH (UTC hour) on the .BR date. Only the first
    occurrence of each point is read; the products repeat the layout further
    down for freezing levels.
    """
    m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
    body = m.group(1) if m else text
    br = _BR.search(body)
    dh = _DH.search(body)
    if not (br and dh):
        raise RuntimeError("cnrfc: header not found")
    t0 = datetime.strptime(br.group(1) + dh.group(1), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    wanted = set(CFG.get("cnrfc", {}).get("points", []))
    out = {}
    for line in body.splitlines():
        mm = re.match(r"^([A-Z0-9]{3,5})\s+((?:\s*-?\d+\.\d+/){11}\s*-?\d+\.\d+)\s*:", line)
        if not mm or mm.group(1) not in wanted or mm.group(1) in out:
            continue
        vals = [float(x) for x in mm.group(2).replace(" ", "").split("/")]
        out[mm.group(1)] = [((t0 + timedelta(hours=6 * i)).isoformat(), v) for i, v in enumerate(vals)]
    if not out:
        raise RuntimeError("cnrfc: no configured points found")
    return out, t0


def fetch_cnrfc_qpf():
    """CNRFC 6-hourly point QPF for days 1-6 at the configured nearest points."""
    cfg = CFG.get("cnrfc", {})
    prods = cfg.get("products", {})
    d13, issued = _parse_cnrfc_product(get_backoff(prods["days_1_3"]).text)
    periods = {k: list(v) for k, v in d13.items()}
    err = None
    try:
        d46, _ = _parse_cnrfc_product(get_backoff(prods["days_4_6"]).text)
        for k, v in d46.items():
            periods.setdefault(k, []).extend(v)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:120]
    out = {"issued_utc": issued.isoformat(), "points": {}, "source": prods}
    if err:
        out["days_4_6_error"] = err
    for pt, vals in periods.items():
        def tot(n, vals=vals):
            return round(sum(v for _, v in vals[:n]), 2)
        out["points"][pt] = {
            "periods": [{"end_utc": t, "in": v} for t, v in vals],
            "qpf_24h_in": tot(4), "qpf_48h_in": tot(8), "qpf_72h_in": tot(12),
            "qpf_6d_in": tot(24),
            "peak_6h_in": max((v for _, v in vals), default=0),
        }
    return out


# ---------------------------------------------------------------- CW3E

def fetch_cw3e_ar():
    """Maximum forecast AR Scale (0-5) at the nearest coastal points, per model/calc key."""
    cfg = CFG.get("cw3e", {})
    r = get_backoff(cfg["url"])
    j = r.json()
    pts = [tuple(p) for p in cfg.get("points", [])]
    out = {"source": cfg["url"], "file_modified": r.headers.get("last-modified"),
           "points": pts, "by_key": {}}
    best = None
    for key in cfg.get("keys", []):
        cats = [int(c) for la, lo, c in j.get(key, []) if (la, lo) in pts]
        if cats:
            out["by_key"][key] = max(cats)
            best = max(best or 0, max(cats))
    if best is None:
        raise RuntimeError("cw3e: configured points not present in file")
    out["ar_category"] = best
    return out
