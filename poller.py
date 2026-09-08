#!/usr/bin/env python3
"""
Floodboard poller for 52 Woodland Ave, San Anselmo.

Runs every 15 minutes (GitHub Actions cron). Each source is isolated:
if one feed fails, the rest still update. Writes:
  docs/data/latest.json   - everything the dashboard needs, one snapshot
  docs/data/history.csv   - one row per run, the calibration dataset
  docs/data/state.json    - last alert tier (so we only push on change)

Secrets (GitHub Actions -> Settings -> Secrets):
  AMBIENT_APP_KEY, AMBIENT_API_KEY   from ambientweather.net -> Account -> API Keys
  SUMP_URL                           (optional) JSON endpoint for sump monitor
  PUSHOVER_TOKEN, PUSHOVER_USER      (optional) phone push alerts
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# Load secrets from .env (local dev). In GitHub Actions these come from the
# environment, so load_dotenv() is a harmless no-op when no file is present.
load_dotenv(ROOT / ".env")

DATA = ROOT / "docs" / "data"
DATA.mkdir(parents=True, exist_ok=True)

CFG = json.loads((ROOT / "config.json").read_text())
LAT, LON = CFG["lat"], CFG["lon"]
TH = CFG["thresholds"]
UA = {"User-Agent": "floodboard-52woodland (personal flood monitor)"}
# Some agencies (NWS) ask for a contact address in the UA; the Bridge Street
# gauge's operators (Marin County / OneRain) likewise appreciate identification.
# Kept out of source (public repo) — set FLOODBOARD_CONTACT (a GitHub secret) to
# your email; falls back to a generic string when unset.
CONTACT = os.environ.get("FLOODBOARD_CONTACT", "personal flood monitor")
BRIDGE_UA = {"User-Agent": f"floodboard-52woodland personal flood monitor ({CONTACT})"}
BRIDGE_THRESHOLDS = {11.3, 13.3, 16.3, 17.8}  # flood-category guide lines OneRain embeds

HISTORY_COLS = [
    "ts_utc", "rain_rate_inhr", "rain_1h_in", "rain_24h_in", "rain_event_in",
    "creek_stage_ft", "creek_flow_cfs", "nwps_stage_ft", "bridge_stage_ft",
    "sump_level", "sump_runtime_min", "sump_cycles", "sump_duty_pct",
    "qpf_24h_in", "qpf_72h_in", "tier",
    # Multi-model QPF logged hourly for a spring skill comparison. Last winter
    # Open-Meteo's blend captured only 75% of observed rain at a day's lead and
    # 50% at three days, so we log the competitors side by side before trusting
    # any of them: see analysis/report.md.
    "qpf_nws_24h_in", "qpf_nws_72h_in",
    "qpf_cnrfc_6h_in", "qpf_cnrfc_24h_in",
    "qpf_ecmwf_24h_in", "qpf_gfs_24h_in", "qpf_hrrr_6h_in",
    "ar_category", "nws_flood_watch",
]


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
                time.sleep(1 + 2 * i)  # 1s, 3s
    raise last


def safe(fn):
    """Run a fetcher; return (result, error) so one failure never kills the run."""
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:200]


# ---------------------------------------------------------------- sources

def fetch_ambient():
    app, key = os.environ.get("AMBIENT_APP_KEY"), os.environ.get("AMBIENT_API_KEY")
    if not (app and key):
        raise RuntimeError("AMBIENT keys not set")
    devices = get("https://rt.ambientweather.net/v1/devices",
                  params={"applicationKey": app, "apiKey": key})
    d = devices[0]["lastData"]
    return {
        "observed_utc": d.get("date"),
        "rain_rate_inhr": d.get("hourlyrainin"),   # Ambient's current rate, in/hr
        "rain_event_in": d.get("eventrainin"),
        # Ambient has no rolling-24h field; dailyrainin is the calendar-day total
        # (resets at local midnight). Closest thing the API actually exposes.
        "rain_24h_in": d.get("dailyrainin"),
        "rain_daily_in": d.get("dailyrainin"),
        "rain_weekly_in": d.get("weeklyrainin"),
        "rain_monthly_in": d.get("monthlyrainin"),
        "baro_inhg": d.get("baromrelin"),
        "temp_f": d.get("tempf"),
        "feels_like_f": d.get("feelsLike"),
        "dew_point_f": d.get("dewPoint"),
        "humidity_pct": d.get("humidity"),
        "wind_mph": d.get("windspeedmph"),
        "wind_gust_mph": d.get("windgustmph"),
        "wind_dir_deg": d.get("winddir"),
        "wind_max_daily_gust_mph": d.get("maxdailygust"),
        "device": devices[0].get("info", {}).get("name"),
    }


def fetch_usgs():
    j = get("https://waterservices.usgs.gov/nwis/iv/",
            params={"format": "json", "sites": CFG["usgs_site"],
                    "parameterCd": "00065,00060", "siteStatus": "all"})
    out = {}
    for ts in j["value"]["timeSeries"]:
        code = ts["variable"]["variableCode"][0]["value"]
        vals = ts["values"][0]["value"]
        if not vals:
            continue
        last = vals[-1]
        key = "stage_ft" if code == "00065" else "flow_cfs"
        out[key] = float(last["value"])
        out["observed"] = last["dateTime"]
    return out


def fetch_nwps():
    lid = CFG["nwps_gauge"]
    base = f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
    meta = get(base)
    sf = get(base + "/stageflow")
    cats = (meta.get("flood") or {}).get("categories") or {}
    obs = (sf.get("observed") or {}).get("data") or []
    fc = (sf.get("forecast") or {}).get("data") or []
    return {
        "name": meta.get("name"),
        "categories": {k: v.get("stage") for k, v in cats.items() if isinstance(v, dict)},
        "observed": [{"t": p["validTime"], "stage": p.get("primary")} for p in obs[-96:]],
        "forecast": [{"t": p["validTime"], "stage": p.get("primary")} for p in fc],
        "latest_stage_ft": obs[-1].get("primary") if obs else None,
        "forecast_peak_ft": max((p.get("primary") or 0) for p in fc) if fc else None,
    }


def fetch_nws():
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
            "office": pt.get("cwa"), "forecast_zone": pt.get("forecastZone")}


def fetch_open_meteo():
    j = get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": LAT, "longitude": LON,
        "hourly": "precipitation,precipitation_probability",
        "precipitation_unit": "inch", "timezone": CFG["timezone"], "forecast_days": 7,
    })
    h = j["hourly"]
    hourly = [{"t": t, "in": p or 0, "prob": pr}
              for t, p, pr in zip(h["time"], h["precipitation"], h["precipitation_probability"])]
    # Forecast starts at the top of the current day; slice from the current hour on.
    now_local = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:00")
    future = [x for x in hourly if x["t"] >= now_local] or hourly
    return {
        "hourly": hourly,
        "qpf_next_6h_in": round(sum(x["in"] for x in future[:6]), 3),
        "qpf_24h_in": round(sum(x["in"] for x in future[:24]), 2),
        "qpf_72h_in": round(sum(x["in"] for x in future[:72]), 2),
        "qpf_7d_in": round(sum(x["in"] for x in future), 2),
        "peak_rate_next_24h": round(max((x["in"] for x in future[:24]), default=0), 2),
    }


def _map_pump_reading(p, j):
    """Map a PumpFuse-style JSON payload to our fields, tolerating key-name variants.
    PumpFuse reports on/off, power (W), run time, cycle count, gallons - no pit level."""
    def pick(*keys):
        for k in keys:
            v = j.get(k) if isinstance(j, dict) else None
            if v is not None:
                return v
        return None
    on = pick("on", "pump_on", "running", "is_on", "state", "relay")
    if isinstance(on, str):
        on = on.strip().lower() in ("on", "true", "running", "1", "yes")
    return {
        "name": p.get("name"), "label": p.get("label", p.get("name")),
        "hp": p.get("hp"), "gpm": p.get("gpm"),
        "on": on,
        "watts": pick("watts", "power", "power_w", "w", "watt"),
        "runtime_min": pick("runtime_min", "run_time_min", "runtime", "run_minutes", "on_minutes"),
        "cycles": pick("cycles", "cycle_count", "starts", "run_count", "count"),
        "gallons": pick("gallons", "gallons_pumped", "gal"),
        "observed": pick("timestamp", "time", "observed", "ts", "last_update", "updated"),
    }


def fetch_sump():
    """
    Dual-pump adapter for the two PumpFuse PF03 units (primary Zoeller 1 HP,
    backup 3/4 HP). Reads each pump from a JSON endpoint set in
    config.json ("sump".pumps[].url) or via SUMP_PRIMARY_URL / SUMP_BACKUP_URL
    (legacy SUMP_URL still works as a single primary endpoint).

    NOTE: a LAN scan (2026-09-02) found both units present as ESP32 devices but
    the PF03 exposes NO local API - no open TCP ports, no mDNS, no Tuya broadcast;
    PumpFuse is cloud-only per the manufacturer. So until a data source exists
    (a future local REST endpoint, or a Home Assistant / MQTT / cloud-export
    bridge) the URLs stay empty and this raises, leaving the board's sump card in
    its "not connected" state. Field names are matched flexibly so any reasonable
    JSON shape will map. Returns per-pump readings plus primary-pump values at the
    top level so the existing duty-cycle / tier / history logic keeps working.
    """
    cfg = CFG.get("sump", {})
    pumps_cfg = cfg.get("pumps")
    if not pumps_cfg:  # legacy single-endpoint fallback
        legacy = os.environ.get("SUMP_URL")
        if not legacy:
            raise RuntimeError("no sump endpoint configured (PumpFuse PF03 has no "
                               "local API; set sump.pumps[].url or SUMP_URL)")
        # GPM comes from the primary_pump spec block, never a literal here:
        # a stale hardcoded 40 silently halves every inflow number downstream.
        pumps_cfg = [{"name": "primary", "label": "Primary", "spec": "primary_pump",
                      "gpm": CFG.get("primary_pump", {}).get("rated_gpm"), "url": legacy}]

    env_url = {"primary": os.environ.get("SUMP_PRIMARY_URL"),
               "backup": os.environ.get("SUMP_BACKUP_URL")}
    readings, ok, errs = [], 0, []
    for p in pumps_cfg:
        url = env_url.get(p.get("name")) or p.get("url")
        if not url:
            readings.append({**_map_pump_reading(p, {}), "note": "no endpoint configured"})
            continue
        try:
            readings.append(_map_pump_reading(p, get_backoff(url, timeout=15).json()))
            ok += 1
        except Exception as e:  # noqa: BLE001
            readings.append({**_map_pump_reading(p, {}),
                             "error": f"{type(e).__name__}: {e}"[:120]})
            errs.append(p.get("name"))

    if ok == 0:
        raise RuntimeError("no sump data (PumpFuse PF03 is cloud-only; no local "
                           "endpoint reachable" + (f"; errors: {', '.join(errs)}" if errs else "") + ")")

    primary = next((r for r in readings if r.get("name") == "primary"), readings[0])
    return {
        "pumps": readings,
        # top-level mirrors the primary pump so existing duty/tier/history code works
        "level": None,  # PumpFuse is energy-based; no pit-level sensor
        "runtime_min": primary.get("runtime_min"),
        "cycles": primary.get("cycles"),
        "pump_on": primary.get("on"),
        "observed": primary.get("observed"),
    }


def fetch_bridge_street():
    """
    San Anselmo Creek at Bridge Street - the downtown gauge that matters most.

    This is the same physical gauge as Marin County OneRain "San Anselmo - FS 19"
    (site 38036, device 5) and NWS/NWPS "SBSC1"; identical datum and flood
    categories (action 11.3, minor 13.3, moderate 16.3, major 17.8 ft; 13 ft is
    the sill of 730 San Anselmo Ave per the Town). Sources, best first:

      1. NWPS SBSC1 JSON  - clean public API, 15-min obs + NWS forecast stage,
                            no auth. This is the recommended source.
      2. OneRain FS 19    - county source; the public /export/file/ CSV is
                            login-only (401 for guests), so we read the same
                            graph page the site itself renders and take the
                            latest embedded point.

    If both fail we raise; the USGS (Ross, 11460000) and NWPS (Ross, CMDC1)
    feeds then act as the downstream fallback. Poll ~5 min; gauge reports ~15 min.
    """
    cfg = CFG.get("bridge_street", {})
    errs = []

    # 1. NWPS SBSC1 - the Bridge Street gauge, served cleanly by the NWS -------
    try:
        lid = cfg.get("nwps_lid", "SBSC1")
        base = f"https://api.water.noaa.gov/nwps/v1/gauges/{lid}"
        meta = get_backoff(base, BRIDGE_UA).json()
        sf = get_backoff(base + "/stageflow", BRIDGE_UA).json()
        cats = {k: v.get("stage") for k, v in
                ((meta.get("flood") or {}).get("categories") or {}).items()
                if isinstance(v, dict)}
        obs = [p for p in ((sf.get("observed") or {}).get("data") or [])
               if (p.get("primary") if p.get("primary") is not None else -999) > -900]
        fc = (sf.get("forecast") or {}).get("data") or []
        if obs:
            latest = obs[-1]
            return {
                "source": "nwps:" + lid,
                "name": meta.get("name"),
                "stage_ft": latest.get("primary"),
                "observed": latest.get("validTime"),
                "categories": cats,
                "forecast_peak_ft": max((p.get("primary") or 0) for p in fc) if fc else None,
                "series": [{"t": p["validTime"], "stage": p.get("primary")} for p in obs[-192:]],
                "forecast": [{"t": p["validTime"], "stage": p.get("primary")} for p in fc[:48]],
                "town_stages": cfg.get("town_stages"),
                "url": CFG.get("bridge_street_gauge_url"),
            }
        errs.append("nwps: no observed data")
    except Exception as e:  # noqa: BLE001
        errs.append(f"nwps: {type(e).__name__}: {e}")

    # 2. OneRain FS 19 graph page - parse the latest embedded flot point -------
    try:
        host = cfg.get("onerain_host", "marin.onerain.com")
        sid = cfg.get("onerain_site_id", 16807)
        did = cfg.get("onerain_device_id", 5)
        duid = cfg.get("onerain_device_uuid", "30b998de-0ca7-49ad-b3fd-426f97ba0b24")
        end = datetime.now(timezone.utc).astimezone()
        start = end - timedelta(days=2)  # small window that always holds the latest point
        fmt = "%Y-%m-%d %H:%M:%S"
        url = (f"https://{host}/graph/?time_zone=US/Pacific&site_id={sid}"
               f"&device_id={did}&device={duid}&bin=0&range=custom"
               f"&data_start={start.strftime(fmt)}&data_end={end.strftime(fmt)}"
               f"&show_raw=true&legend=false&thresholds=false&markers=true"
               f"&devices[]={sid}|{did}")
        html = get_backoff(url, BRIDGE_UA).text
        pairs = []
        for ep, val in re.findall(r"\[(\d{12,}),(-?\d+(?:\.\d+)?)\]", html):
            dec = len(val.split(".")[1]) if "." in val else 0
            if dec <= 2 and float(val) not in BRIDGE_THRESHOLDS:  # skip threshold guide lines
                pairs.append((int(ep), float(val)))
        if pairs:
            ep, v = max(pairs)  # latest by timestamp
            return {
                "source": "onerain:FS19",
                "name": "San Anselmo - FS 19 (38036)",
                "stage_ft": v,
                "observed": datetime.fromtimestamp(ep / 1000, tz=timezone.utc).isoformat(),
                "categories": {"action": 11.3, "minor": 13.3, "moderate": 16.3, "major": 17.8},
                "forecast_peak_ft": None,
                "town_stages": cfg.get("town_stages"),
                "url": CFG.get("bridge_street_gauge_url"),
            }
        errs.append("onerain: no points parsed")
    except Exception as e:  # noqa: BLE001
        errs.append(f"onerain: {type(e).__name__}: {e}")

    raise RuntimeError("bridge street unavailable (" + "; ".join(errs)
                       + "); USGS/NWPS Ross feeds are the downstream fallback")


# ---------------------------------------------------------------- history

def read_history():
    p = DATA / "history.csv"
    if not p.exists():
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def append_history(row):
    p = DATA / "history.csv"
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in HISTORY_COLS})


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def derive_from_history(hist, ambient, sump):
    """Rolling values the feeds don't give directly."""
    now = datetime.now(timezone.utc)
    out = {"rain_1h_in": None, "rain_6h_in": None, "rain_7d_in": None,
           "api": None, "sump_duty_pct": None}

    def rows_since(hours):
        cut = (now - timedelta(hours=hours)).isoformat()
        return [r for r in hist if r["ts_utc"] >= cut]

    # 1-hour rain: difference in Ambient's event/daily counter over the last hour,
    # falling back to rate*time if counters reset.
    if ambient and ambient.get("rain_event_in") is not None:
        last_hr = rows_since(1)
        if last_hr:
            prev = fnum(last_hr[0].get("rain_event_in"))
            cur = ambient["rain_event_in"]
            if prev is not None and cur >= prev:
                out["rain_1h_in"] = round(cur - prev, 3)
    # 7-day antecedent from 24h totals sampled once a day at midnight-ish is fiddly;
    # Ambient's weekly counter is good enough for the antecedent index.
    if ambient:
        out["rain_7d_in"] = ambient.get("rain_weekly_in")

    # 6-hour rain drives the creek predictor (Layer 3A: it carries roughly three
    # times the weight of the 3-hour window). Same counter-difference trick.
    if ambient and ambient.get("rain_event_in") is not None:
        last_6 = rows_since(6)
        if last_6:
            prev = fnum(last_6[0].get("rain_event_in"))
            cur = ambient["rain_event_in"]
            if prev is not None and cur >= prev:
                out["rain_6h_in"] = round(cur - prev, 3)

    # Antecedent precipitation index, 0.9/day decay, rebuilt from history rows.
    # Beats the raw 7-day total: Layer 3A gave it a standardized weight of 0.32.
    if hist:
        acc, prev_t = 0.0, None
        for r in hist[-2016:]:                     # ~3 weeks at 15-min cadence
            try:
                t = datetime.fromisoformat(r["ts_utc"])
            except Exception:  # noqa: BLE001
                continue
            inc = fnum(r.get("rain_1h_in")) or 0.0
            if prev_t is not None:
                days = max((t - prev_t).total_seconds() / 86400, 0)
                acc *= 0.9 ** days
            acc += inc / 4.0 if inc else 0.0       # 15-min row holds a 1-h total
            prev_t = t
        out["api"] = round(acc, 3)

    # Sump duty cycle over the last hour from cumulative runtime.
    if sump and sump.get("runtime_min") is not None:
        last_hr = rows_since(1)
        if last_hr:
            prev = fnum(last_hr[0].get("sump_runtime_min"))
            prev_t = datetime.fromisoformat(last_hr[0]["ts_utc"])
            if prev is not None and sump["runtime_min"] >= prev:
                span_min = max((now - prev_t).total_seconds() / 60, 1)
                out["sump_duty_pct"] = round(100 * (sump["runtime_min"] - prev) / span_min, 1)
    return out


# ---------------------------------------------------------------- rules


# ---------------------------------------------------------------- forecast sources
# Logged hourly so next spring we can rank them. Each is isolated by safe().

def _sum_nws_qpf(qpf_periods, hours):
    """Sum NWS gridded QPF over the next `hours` from now."""
    if not qpf_periods:
        return None
    now = datetime.now(timezone.utc)
    cut = now + timedelta(hours=hours)
    tot = 0.0
    for p in qpf_periods:
        try:
            t = datetime.fromisoformat(p["t"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if now - timedelta(hours=6) <= t <= cut:
            tot += p.get("in") or 0
    return round(tot, 3)


def fetch_cw3e_ar():
    """CW3E atmospheric-river outlook, North Bay / Bay Area landfall category.

    CW3E publishes the AR Scale outlook as a JSON summary alongside its graphics.
    There is no documented stable API, so this is best-effort: it looks for a
    Bay Area / North Bay entry and returns its AR category (1-5). A miss returns
    category None rather than raising, because Watch has two other triggers.
    """
    url = "https://cw3e.ucsd.edu/wp-content/uploads/AR_Scale/ar_scale_summary.json"
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"cw3e {r.status_code}")
    j = r.json()
    best = None
    def walk(o):
        nonlocal best
        if isinstance(o, dict):
            blob = json.dumps(o)[:400].lower()
            if any(k in blob for k in ("north bay", "bay area", "san francisco", "bodega")):
                for key in ("ar_scale", "category", "ar_cat", "scale"):
                    v = o.get(key)
                    if isinstance(v, (int, float)) and 0 <= v <= 5:
                        best = max(best or 0, int(v))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(j)
    return {"ar_category": best, "source": url}


def fetch_cnrfc_qpf():
    """CNRFC 6-hourly QPF for the Ross Valley forecast point.

    The California-Nevada River Forecast Center publishes gridded QPF; the
    per-point CSV is the stable public surface. Returns 6 h and 24 h totals.
    """
    url = ("https://www.cnrfc.noaa.gov/restricted/graphicalRVF_csv.php"
           f"?id={CFG.get('cnrfc_point', 'CMDC1')}")
    r = requests.get(url, headers=UA, timeout=25)
    if r.status_code != 200 or not r.text.strip():
        raise RuntimeError(f"cnrfc {r.status_code}")
    vals = [float(x) for x in re.findall(r"(?<![\d.])(\d+\.\d+)(?![\d.])", r.text)[:8]]
    if not vals:
        raise RuntimeError("cnrfc: no numeric QPF parsed")
    return {"qpf_6h_in": round(vals[0], 3),
            "qpf_24h_in": round(sum(vals[:4]), 3), "source": url}


def fetch_model_qpf():
    """Open-Meteo per-model precipitation: ECMWF, GFS and HRRR side by side."""
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


TIER_ORDER = ["quiet", "watch", "prepare", "act", "emergency"]


def predict_bridge_ft(rain_6h_in, api, qpf_next_6h_in):
    """Layer 3A live predictor: where Bridge Street is heading in ~6 hours.

    Rain already on the ground plus rain still expected, weighted by how wet the
    catchment already is. Fitted on the rising limb only (n=349, R2 0.838,
    residual sd 0.55 ft). Returns (point_estimate, lo, hi) or None.
    """
    cp = CFG.get("creek_predictor")
    if cp is None or rain_6h_in is None or api is None:
        return None
    r6 = rain_6h_in + (qpf_next_6h_in or 0)
    pt = cp["intercept"] + cp["coef_r6h_in"] * r6 + cp["coef_api"] * api
    band = cp["band_95_ft"]
    return round(pt, 2), round(pt - band, 2), round(pt + band, 2)


def evaluate(ambient, usgs, nwps, nws, om, sump, derived, bridge=None,
             ar=None, runs=None):
    """Returns (tier, reasons) on quiet < watch < prepare < act < emergency.

    Rebuilt from the winter 2025-26 replay (analysis/report.md). The old rules
    fired nothing at all on 8 of 15 storms including the largest; these reached
    Act on the same storm 63 hours before its peak. Season-to-date rain was
    dropped - it added nothing in any layer tested.
    """
    T = CFG["tiers"]
    BIAS = CFG.get("qpf_bias", {"lead_24h": 1.0, "lead_72h": 1.0})
    reasons, level = [], 0

    def raise_to(n, why):
        nonlocal level
        level = max(level, n)
        reasons.append(why)

    # Open-Meteo runs dry here; correct before comparing to any threshold.
    q24 = (om or {}).get("qpf_24h_in")
    q72 = (om or {}).get("qpf_72h_in")
    q24c = round(q24 * BIAS["lead_24h"], 2) if q24 is not None else None
    q72c = round(q72 * BIAS["lead_72h"], 2) if q72 is not None else None

    ross = (usgs or {}).get("stage_ft")
    if ross is None:
        ross = (nwps or {}).get("latest_stage_ft")
    bft = (bridge or {}).get("stage_ft")
    alerts = [a.get("event", "").lower() for a in (nws or {}).get("alerts", [])]

    # ---- watch ----------------------------------------------------------
    cat = (ar or {}).get("ar_category")
    if cat is not None and cat >= T["watch"]["cw3e_ar_category_min"]:
        raise_to(1, f"CW3E AR scale {cat} forecast for the North Bay")
    if T["watch"].get("nws_flood_watch") and any("flood watch" in a for a in alerts):
        raise_to(1, "NWS Flood Watch in effect")
    if q72c is not None and q72c >= T["watch"]["qpf_72h_corrected_in"]:
        raise_to(1, f'{q72c}" forecast next 72h (bias-corrected)')

    # ---- prepare --------------------------------------------------------
    P = T["prepare"]
    if q24c is not None and q24c >= P["qpf_24h_corrected_in"]:
        raise_to(2, f'{q24c}" forecast next 24h (bias-corrected)')
    bw = P["bridge_ft_with_rain"]
    if bft is not None and q24c is not None and bft >= bw["bridge_ft"]             and q24c >= bw["qpf_24h_corrected_in"]:
        raise_to(2, f'Bridge St {bft} ft with {q24c}" more forecast')
    if ross is not None and ross >= P["ross_ft"]:
        raise_to(2, f"Ross gauge {ross} ft")
    longest = max((r.get("duration_s") or 0) for r in (runs or [])) if runs else 0
    if longest >= P["sump_run_s"]:
        raise_to(2, f"sump run {longest:.0f} s")

    # ---- act ------------------------------------------------------------
    A = T["act"]
    if ross is not None and ross >= A["ross_ft"]:
        raise_to(3, f"Ross gauge {ross} ft (Town notify line maps to "
                    f"{CFG['ross_to_bridge']['notify_line_ross_ft']} ft)")
    pred = predict_bridge_ft((derived or {}).get("rain_6h_in"),
                             (derived or {}).get("api"),
                             (om or {}).get("qpf_next_6h_in"))
    if pred and pred[0] >= A["predicted_bridge_ft"]:
        raise_to(3, f"Bridge St predicted {pred[0]} ft in ~6 h "
                    f"({pred[1]}-{pred[2]} ft)")
    if longest >= A["sump_run_s"]:
        raise_to(3, f"sump run {longest:.0f} s = "
                    f"{100*(1-14/longest):.0f}% of pump capacity")
    # three consecutive lengthening runs, the third long enough to matter
    if runs and len(runs) >= 3:
        d = [r.get("duration_s") or 0 for r in runs[-3:]]
        if d[0] < d[1] < d[2] and d[2] >= A.get("sump_increasing_min_final_s", 25):
            raise_to(3, f"sump runs lengthening {d[0]:.0f}->{d[1]:.0f}->{d[2]:.0f} s")

    # ---- emergency ------------------------------------------------------
    E = T["emergency"]
    for pump in (sump or {}).get("pumps", []):
        if pump.get("name") == "backup" and pump.get("on"):
            raise_to(4, "BACKUP PUMP RUNNING - primary is being overwhelmed")
    if longest >= E["primary_run_no_stop_s"]:
        raise_to(4, f"primary running {longest:.0f} s without stopping")
    if ross is not None and ross >= E["ross_ft"]:
        raise_to(4, f"Ross gauge {ross} ft - above NWS action stage")

    return TIER_ORDER[level], reasons


def pushover(title, msg, priority=0):
    tok, usr = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not (tok and usr):
        return
    requests.post("https://api.pushover.net/1/messages.json", timeout=15, data={
        "token": tok, "user": usr, "title": title, "message": msg, "priority": priority,
    })


# ---------------------------------------------------------------- main

def main():
    errors = {}
    ambient, errors["ambient"] = safe(fetch_ambient)
    usgs, errors["usgs"] = safe(fetch_usgs)
    nwps, errors["nwps"] = safe(fetch_nwps)
    nws, errors["nws"] = safe(fetch_nws)
    om, errors["open_meteo"] = safe(fetch_open_meteo)
    sump, errors["sump"] = safe(fetch_sump)
    bridge, errors["bridge_street"] = safe(fetch_bridge_street)
    ar, errors["cw3e"] = safe(fetch_cw3e_ar)
    cnrfc, errors["cnrfc"] = safe(fetch_cnrfc_qpf)
    models, errors["model_qpf"] = safe(fetch_model_qpf)
    errors = {k: v for k, v in errors.items() if v}

    hist = read_history()
    derived = derive_from_history(hist, ambient, sump)
    runs = (sump or {}).get("recent_runs") or []
    tier, reasons = evaluate(ambient, usgs, nwps, nws, om, sump, derived, bridge,
                             ar=ar, runs=runs)
    pred = predict_bridge_ft(derived.get("rain_6h_in"), derived.get("api"),
                             (om or {}).get("qpf_next_6h_in"))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    row = {
        "ts_utc": now.isoformat(),
        "rain_rate_inhr": (ambient or {}).get("rain_rate_inhr"),
        "rain_1h_in": derived.get("rain_1h_in"),
        "rain_24h_in": (ambient or {}).get("rain_24h_in"),
        "rain_event_in": (ambient or {}).get("rain_event_in"),
        "creek_stage_ft": (usgs or {}).get("stage_ft"),
        "creek_flow_cfs": (usgs or {}).get("flow_cfs"),
        "nwps_stage_ft": (nwps or {}).get("latest_stage_ft"),
        "bridge_stage_ft": (bridge or {}).get("stage_ft"),
        "sump_level": (sump or {}).get("level"),
        "sump_runtime_min": (sump or {}).get("runtime_min"),
        "sump_cycles": (sump or {}).get("cycles"),
        "sump_duty_pct": derived.get("sump_duty_pct"),
        "qpf_24h_in": (om or {}).get("qpf_24h_in"),
        "qpf_72h_in": (om or {}).get("qpf_72h_in"),
        "tier": tier,
        # Rival forecasts, logged every run so spring can rank them.
        "qpf_nws_24h_in": _sum_nws_qpf((nws or {}).get("qpf_periods"), 24),
        "qpf_nws_72h_in": _sum_nws_qpf((nws or {}).get("qpf_periods"), 72),
        "qpf_cnrfc_6h_in": (cnrfc or {}).get("qpf_6h_in"),
        "qpf_cnrfc_24h_in": (cnrfc or {}).get("qpf_24h_in"),
        "qpf_ecmwf_24h_in": (models or {}).get("qpf_ecmwf_24h_in"),
        "qpf_gfs_24h_in": (models or {}).get("qpf_gfs_24h_in"),
        "qpf_hrrr_6h_in": (models or {}).get("qpf_hrrr_6h_in"),
        "ar_category": (ar or {}).get("ar_category"),
        "nws_flood_watch": int(any("flood watch" in (a.get("event") or "").lower()
                                   for a in (nws or {}).get("alerts", []))),
    }
    append_history(row)

    latest = {
        "generated_utc": now.isoformat(),
        "site": CFG["site_name"],
        "tier": tier, "reasons": reasons,
        "thresholds": TH,
        "tiers": CFG["tiers"],
        "creek_forecast": ({"point_ft": pred[0], "lo_ft": pred[1], "hi_ft": pred[2],
                            "horizon_h": 6, "r2": CFG["creek_predictor"]["r2"]}
                           if pred else None),
        "ross_to_bridge": CFG["ross_to_bridge"],
        "creek_predictor": CFG["creek_predictor"],
        "cw3e": ar, "cnrfc": cnrfc, "model_qpf": models,
        "ambient": ambient, "usgs": usgs, "nwps": nwps, "nws": nws,
        "open_meteo": om, "sump": sump, "bridge_street": bridge, "derived": derived,
        "bridge_street_gauge_url": CFG["bridge_street_gauge_url"],
        "errors": errors,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, indent=1, default=str))

    # Alert only when the tier changes.
    state_p = DATA / "state.json"
    prev = json.loads(state_p.read_text()).get("tier") if state_p.exists() else "quiet"
    order = TIER_ORDER
    if tier not in order:
        tier = "quiet"
    if prev not in order:
        prev = "quiet"
    if tier != prev:
        up = order.index(tier) > order.index(prev)
        pushover(f"Floodboard: {tier.upper()}",
                 ("; ".join(reasons) or "conditions eased"),
                 priority=2 if tier == "emergency" else 1 if tier == "act"
                 else 0 if up else -1)
    state_p.write_text(json.dumps({"tier": tier, "changed_utc": now.isoformat()}))

    print(f"{now.isoformat()} tier={tier} reasons={reasons} errors={errors}")


if __name__ == "__main__":
    sys.exit(main())
