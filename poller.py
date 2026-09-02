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
        pumps_cfg = [{"name": "primary", "label": "Primary", "hp": 1.0, "gpm": 40, "url": legacy}]

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
    out = {"rain_1h_in": None, "rain_7d_in": None, "sump_duty_pct": None}

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

def evaluate(ambient, usgs, nwps, nws, om, sump, derived, bridge=None):
    """Returns (tier, reasons). Tiers: quiet < watch < prepare < act."""
    reasons, level = [], 0

    def raise_to(n, why):
        nonlocal level
        level = max(level, n)
        reasons.append(why)

    alerts = [a["event"].lower() for a in (nws or {}).get("alerts", []) if a.get("event")]
    if any("flood warning" in a or "flash flood" in a for a in alerts):
        raise_to(3, "NWS flood warning in effect")
    elif any("flood watch" in a for a in alerts):
        raise_to(2, "NWS flood watch in effect")

    if om:
        if om["qpf_24h_in"] >= TH["prepare_qpf_24h_in"]:
            raise_to(2, f'{om["qpf_24h_in"]}" forecast next 24h')
        elif om["qpf_72h_in"] >= TH["watch_qpf_72h_in"]:
            raise_to(1, f'{om["qpf_72h_in"]}" forecast next 72h')

    if ambient:
        rr = ambient.get("rain_rate_inhr") or 0
        if rr >= TH["act_rain_rate_inhr"]:
            raise_to(3, f'rain rate {rr}"/hr')
        r1 = derived.get("rain_1h_in")
        if r1 is not None and r1 >= TH["act_rain_1h_in"]:
            raise_to(3, f'{r1}" in the last hour')

    duty = derived.get("sump_duty_pct")
    if duty is not None:
        if duty >= TH["sump_act_duty_pct"]:
            raise_to(3, f"sump running {duty}% of the hour")
        elif duty >= TH["sump_prepare_duty_pct"]:
            raise_to(2, f"sump running {duty}% of the hour")

    # Backup pump running at all is an Act-tier signal on its own (v2 design):
    # the primary only fails over to the 3/4 HP backup when it can't keep up.
    for pump in (sump or {}).get("pumps", []):
        if pump.get("name") == "backup" and pump.get("on"):
            raise_to(3, "backup pump running (primary being overwhelmed)")

    if nwps and nwps.get("latest_stage_ft") is not None:
        st, cats = nwps["latest_stage_ft"], nwps.get("categories", {})
        if cats.get("minor") and st >= cats["minor"]:
            raise_to(3, f"Ross gauge {st} ft, above minor flood stage")
        elif cats.get("action") and st >= cats["action"]:
            raise_to(2, f"Ross gauge {st} ft, above action stage")
        pk = nwps.get("forecast_peak_ft")
        if pk and cats.get("action") and pk >= cats["action"]:
            raise_to(max(level, 1), f"NWS forecasts Ross gauge to {pk} ft")

    # Bridge Street (downtown San Anselmo) - the gauge that floods first.
    if bridge and bridge.get("stage_ft") is not None:
        st, cats = bridge["stage_ft"], bridge.get("categories", {})
        if cats.get("minor") and st >= cats["minor"]:
            raise_to(3, f"Bridge Street gauge {st} ft, above minor flood stage")
        elif cats.get("action") and st >= cats["action"]:
            raise_to(2, f"Bridge Street gauge {st} ft, above action stage")
        pk = bridge.get("forecast_peak_ft")
        if pk and cats.get("action") and pk >= cats["action"]:
            raise_to(max(level, 1), f"NWS forecasts Bridge Street to {pk} ft")

    return ["quiet", "watch", "prepare", "act"][level], reasons


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
    errors = {k: v for k, v in errors.items() if v}

    hist = read_history()
    derived = derive_from_history(hist, ambient, sump)
    tier, reasons = evaluate(ambient, usgs, nwps, nws, om, sump, derived, bridge)

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
    }
    append_history(row)

    latest = {
        "generated_utc": now.isoformat(),
        "site": CFG["site_name"],
        "tier": tier, "reasons": reasons,
        "thresholds": TH,
        "ambient": ambient, "usgs": usgs, "nwps": nwps, "nws": nws,
        "open_meteo": om, "sump": sump, "bridge_street": bridge, "derived": derived,
        "bridge_street_gauge_url": CFG["bridge_street_gauge_url"],
        "errors": errors,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, indent=1, default=str))

    # Alert only when the tier changes.
    state_p = DATA / "state.json"
    prev = json.loads(state_p.read_text()).get("tier") if state_p.exists() else "quiet"
    order = ["quiet", "watch", "prepare", "act"]
    if tier != prev:
        up = order.index(tier) > order.index(prev)
        pushover(f"Floodboard: {tier.upper()}",
                 ("; ".join(reasons) or "conditions eased"),
                 priority=1 if tier == "act" else 0 if up else -1)
    state_p.write_text(json.dumps({"tier": tier, "changed_utc": now.isoformat()}))

    print(f"{now.isoformat()} tier={tier} reasons={reasons} errors={errors}")


if __name__ == "__main__":
    sys.exit(main())
