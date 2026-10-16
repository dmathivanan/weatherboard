#!/usr/bin/env python3
"""
Six-hourly forecast job for the 52 Woodland storm planner.

Runs from GitHub Actions (every 6 h) or by hand. Pulls every forecast feed,
bias-corrects Open-Meteo per config, rebuilds the ground wetness index from the
station's daily rain, and writes:

  docs/data/forecast.json    everything planner.html / wetness.html pre-fill from
  docs/data/rain_daily.json  the running ledger of station rain per local day

The job never decides anything. Tiers, creek peaks, sump stress and the
checklist are computed in the browser from docs/data/models.json so the user can
move the sliders and see the plan change. This file only supplies the inputs.

Secrets (GitHub Actions -> Settings -> Secrets):
  AMBIENT_APP_KEY, AMBIENT_API_KEY   station history for the wetness index
  FLOODBOARD_CONTACT                 (optional) contact for the User-Agent
"""

import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetchers as F
from fetchers import CFG, TZ, safe

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
DATA.mkdir(parents=True, exist_ok=True)
LEDGER = DATA / "rain_daily.json"
MODELS = DATA / "models.json"

W = CFG.get("wetness", {})
P = CFG.get("planner", {})
BIAS = CFG.get("qpf_bias", {"lead_24h": 1.0, "lead_72h": 1.0})
HORIZON_H = 168


# ---------------------------------------------------------------- helpers

PREFILL_H = 72   # the bias factors were measured at 1- and 3-day leads; beyond that is outlook


def lead_factor(lead_h):
    """Open-Meteo bias factor by lead: flat to 24 h, linear to 72 h. Beyond 72 h
    the correction is unmeasured, so the factor is 1 and the hours are outlook."""
    a, b = BIAS["lead_24h"], BIAS["lead_72h"]
    if lead_h <= 24:
        return a
    if lead_h <= 72:
        return a + (b - a) * (lead_h - 24) / 48.0
    return 1.0


def local_hour_str(dt):
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:00")


def parse_local(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)


def iso_dur_hours(s):
    """PT6H / PT1H / P1DT6H -> hours."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", s or "")
    if not m:
        return 6.0
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60.0


def r2(x):
    return None if x is None else round(x, 2)


def r3(x):
    return None if x is None else round(x, 3)


# ---------------------------------------------------------------- rain ledger

def load_ledger():
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"days": {}, "source": {}}


def water_year_start(today):
    m = W.get("water_year_start_month", 10)
    y = today.year if today.month >= m else today.year - 1
    return today.replace(year=y, month=m, day=1)


def update_ledger(ledger, errors):
    """Refresh the per-day station rain. Bootstraps `ledger_days` on first run,
    then re-reads the last three days each run so partial days get completed.
    Days the station cannot supply are filled from Open-Meteo's analysis and
    marked, so the wetness page can say which days are model, not gauge."""
    today = datetime.now(TZ).date()
    days = ledger.setdefault("days", {})
    src = ledger.setdefault("source", {})
    want = W.get("ledger_days", 35)
    have = [d for d in days if src.get(d) == "ambient"]
    need = want if not have else 3
    # Cover the water year for the season total when the ledger is young.
    wys = water_year_start(today)
    need = max(need, min((today - wys).days + 1, 120)) if not have else need

    amb, err = safe(F.fetch_ambient_daily, need)
    if err:
        errors["ambient_daily"] = err
    else:
        for d, v in amb["days"].items():
            days[d] = v
            src[d] = "ambient"
        ledger["mac"] = amb.get("mac")

    # Fill gaps (or everything, if the station is down) from Open-Meteo past days.
    missing = [(today - timedelta(days=i)).isoformat() for i in range(want)]
    missing = [d for d in missing if d not in days]
    if missing:
        om, err = safe(F.fetch_open_meteo, past_days=want, forecast_days=1)
        if err:
            errors["open_meteo_past"] = err
        else:
            by_day = {}
            for h in om["hourly"]:
                by_day[h["t"][:10]] = by_day.get(h["t"][:10], 0.0) + (h["in"] or 0)
            for d in missing:
                if d in by_day:
                    days[d] = round(by_day[d], 3)
                    src[d] = "open-meteo"
    # Trim to a season's worth so the file stays small.
    keep = (wys - timedelta(days=45)).isoformat()
    for d in list(days):
        if d < keep:
            days.pop(d, None)
            src.pop(d, None)
    ledger["updated_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return ledger


def wetness_from_ledger(ledger, usgs, models):
    today = datetime.now(TZ).date()
    days, src = ledger.get("days", {}), ledger.get("source", {})
    decay = W.get("decay_per_day", 0.9)
    api = 0.0
    for d, v in days.items():
        age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        if 0 <= age <= 60:
            api += (v or 0) * decay ** age
    rain_7d = sum((days.get((today - timedelta(days=i)).isoformat()) or 0) for i in range(7))
    rain_today = days.get(today.isoformat()) or 0
    wys = water_year_start(today)
    season = sum(v for d, v in days.items() if d >= wys.isoformat())
    season_complete = wys.isoformat() in days or all(
        (wys + timedelta(days=i)).isoformat() in days for i in range(3))
    sat = W.get("saturated_api_in", 5.2)
    pct = max(0.0, min(100.0, 100.0 * api / sat))
    cat = "dry"
    for c in W.get("categories", []):
        if api <= c["max_api_in"]:
            cat = c["label"]
            break

    # Baseflow cross-check from Ross: 10th percentile of the last week's flow.
    base_cfs = base_ft = implied = None
    flows = [p["v"] for p in ((usgs or {}).get("series") or {}).get("flow_cfs", [])]
    if flows:
        s = sorted(flows)
        base_cfs = s[max(0, int(0.1 * len(s)) - 1)]
        stages = [p["v"] for p in usgs["series"].get("stage_ft", [])]
        if stages:
            base_ft = sorted(stages)[max(0, int(0.1 * len(stages)) - 1)]
        fit = (models or {}).get("baseflow_to_api")
        if fit and base_cfs and base_cfs > 0:
            implied = max(0.0, fit["intercept"] + fit["coef_ln_flow"] * math.log(base_cfs))

    gauge_days = sum(1 for d in days if src.get(d) == "ambient"
                     and (today - datetime.strptime(d, "%Y-%m-%d").date()).days < 35)
    source = "ambient" if gauge_days >= 20 else ("open-meteo" if days else "none")
    if source == "none" and implied is not None:
        api, source = implied, "baseflow"
        pct = max(0.0, min(100.0, 100.0 * api / sat))
    return {
        "api_in": r3(api), "wetness_pct": round(pct), "category": cat,
        "saturated_api_in": sat, "decay_per_day": decay,
        "rain_today_in": r3(rain_today), "rain_7d_in": r3(rain_7d),
        "season_rain_in": r2(season), "season_start": wys.isoformat(),
        "season_complete": bool(season_complete),
        "baseflow_cfs": base_cfs, "baseflow_stage_ft": base_ft,
        "baseflow_implied_api_in": r3(implied),
        "source": source, "gauge_days_in_window": gauge_days,
    }


# ---------------------------------------------------------------- forecast assembly

def build_hourly(om, nws):
    """One row per local hour from now through HORIZON_H: Open-Meteo raw and
    bias-corrected, NWS grid QPF spread evenly over its periods, and PoP."""
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    rows = {}
    for i in range(HORIZON_H):
        t = now + timedelta(hours=i)
        rows[local_hour_str(t)] = {"t": local_hour_str(t), "lead_h": i,
                                   "om_in": None, "omc_in": None, "nws_in": None, "prob": None}
    for h in (om or {}).get("hourly", []):
        if h["t"] in rows:
            r = rows[h["t"]]
            r["om_in"] = r3(h["in"] or 0)
            r["omc_in"] = r3((h["in"] or 0) * lead_factor(r["lead_h"]))
            r["prob"] = h.get("prob")
    for p in (nws or {}).get("qpf_periods", []):
        try:
            start = datetime.fromisoformat(p["t"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        hrs = max(iso_dur_hours(p["dur"]), 1.0)
        per = (p["in"] or 0) / hrs
        for k in range(int(round(hrs))):
            key = local_hour_str(start + timedelta(hours=k))
            if key in rows:
                rows[key]["nws_in"] = r3((rows[key]["nws_in"] or 0) + per)
    return list(rows.values())


def totals(hourly, key, hours):
    vals = [h[key] for h in hourly[:hours] if h[key] is not None]
    return r2(sum(vals)) if vals else None


def peak(hourly, key, hours):
    vals = [h[key] for h in hourly[:hours] if h[key] is not None]
    return r3(max(vals)) if vals else None


def storm_window(hourly, key="omc_in", lo_h=0, hi_h=PREFILL_H):
    """The wettest 48 h between lo_h and hi_h (bias-corrected Open-Meteo by
    default), and the onset / heaviest-hour / end inside it. Everything is
    editable on the page; this just gives the sliders a starting point."""
    win = P.get("storm_window_h", 48)
    thr = P.get("rain_threshold_in", 0.02)
    hourly = [h for h in hourly if lo_h <= h["lead_h"] < hi_h]
    vals = [h[key] or 0 for h in hourly]
    if len(vals) < win:
        return None
    best_i, best_s = 0, -1
    run = sum(vals[:win])
    for i in range(0, len(vals) - win + 1):
        if i:
            run += vals[i + win - 1] - vals[i - 1]
        if run > best_s + 1e-9:
            best_s, best_i = run, i
    seg = vals[best_i:best_i + win]
    wet = [i for i, v in enumerate(seg) if v >= thr]
    if not wet:
        return {"total_48h_in": r2(best_s), "peak_1h_inhr": r3(max(seg) if seg else 0),
                "onset": None, "peak_at": None, "end": None, "dry": True}
    onset = best_i + wet[0]
    pk = best_i + max(range(len(seg)), key=lambda i: seg[i])
    end = best_i + wet[-1]
    # extend the end through any rain continuing past the window
    j = best_i + win
    while j < len(vals) and vals[j] >= thr:
        end = j
        j += 1
    tot_from_onset = sum(vals[onset:onset + win])
    seg = hourly[onset:onset + win]
    probs = [h["prob"] for h in seg if h["prob"] is not None and (h[key] or 0) >= thr]
    return {
        "total_48h_in": r2(tot_from_onset), "peak_1h_inhr": r3(vals[pk]),
        "raw_total_48h_in": r2(sum((h["om_in"] or 0) for h in seg)),
        "onset": hourly[onset]["t"], "peak_at": hourly[pk]["t"], "end": hourly[end]["t"],
        "nws_total_in_window": r2(sum((h["nws_in"] or 0) for h in seg)),
        "pop_mean_pct": round(sum(probs) / len(probs)) if probs else None,
        "window_start_lead_h": hourly[onset]["lead_h"], "dry": False,
    }


def main():
    errors = {}
    models = json.loads(MODELS.read_text(encoding="utf-8")) if MODELS.exists() else None

    nws, errors["nws"] = safe(F.fetch_nws)
    om, errors["open_meteo"] = safe(F.fetch_open_meteo, past_days=1, forecast_days=7)
    cnrfc, errors["cnrfc"] = safe(F.fetch_cnrfc_qpf)
    cw3e, errors["cw3e"] = safe(F.fetch_cw3e_ar)
    usgs, errors["usgs"] = safe(F.fetch_usgs, "P7D")
    ross, errors["nwps"] = safe(F.fetch_nwps)
    bridge, errors["bridge_street"] = safe(F.fetch_bridge_street)
    ambient, errors["ambient"] = safe(F.fetch_ambient)
    model_qpf, errors["model_qpf"] = safe(F.fetch_model_qpf)

    ledger = update_ledger(load_ledger(), errors)
    LEDGER.write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    wet = wetness_from_ledger(ledger, usgs, models)

    hourly = build_hourly(om, nws)
    window = storm_window(hourly)                                   # next 72 h, bias-corrected: the prefill
    outlook = storm_window(hourly, key="om_in", lo_h=PREFILL_H, hi_h=HORIZON_H)   # days 4-7, raw: shown, not prefilled
    if outlook and not outlook.get("dry"):
        cn_pts = (cnrfc or {}).get("points") or {}
        outlook["cnrfc_6d_in"] = {k: v.get("qpf_6d_in") for k, v in cn_pts.items()}
        outlook["_note"] = ("Beyond 72 h. Open-Meteo raw, no bias correction (unmeasured at this lead), "
                            "with the mean chance of rain over the wet hours. Not used to pre-fill the planner.")
    alerts = (nws or {}).get("alerts", [])
    events = [(a.get("event") or "").lower() for a in alerts]
    flood_watch = any("flood watch" in e for e in events)
    flood_warning = any("flood warning" in e or "flash flood" in e for e in events)

    cn = {k: {kk: vv for kk, vv in v.items() if kk != "periods"}
          for k, v in ((cnrfc or {}).get("points") or {}).items()}
    errors = {k: v for k, v in errors.items() if v}
    now = datetime.now(timezone.utc).replace(microsecond=0)

    out = {
        "generated_utc": now.isoformat(),
        "generated_local": now.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z"),
        "site": CFG["site_name"],
        "qpf": {
            "om_24h_in": totals(hourly, "om_in", 24), "om_48h_in": totals(hourly, "om_in", 48),
            "om_72h_in": totals(hourly, "om_in", 72), "om_7d_in": totals(hourly, "om_in", HORIZON_H),
            "omc_24h_in": totals(hourly, "omc_in", 24), "omc_48h_in": totals(hourly, "omc_in", 48),
            "omc_72h_in": totals(hourly, "omc_in", 72), "omc_7d_in": totals(hourly, "omc_in", HORIZON_H),
            "nws_24h_in": totals(hourly, "nws_in", 24), "nws_48h_in": totals(hourly, "nws_in", 48),
            "nws_72h_in": totals(hourly, "nws_in", 72),
            "peak_1h_om_inhr": peak(hourly, "om_in", 72), "peak_1h_omc_inhr": peak(hourly, "omc_in", 72),
            "peak_1h_nws_inhr": peak(hourly, "nws_in", 72),
            "bias": BIAS, "cnrfc_points": cn, "models_24h": model_qpf,
            "nws_grid_updated": (nws or {}).get("updated"),
            "cnrfc_issued_utc": (cnrfc or {}).get("issued_utc"),
        },
        "hourly": hourly,
        "storm": window,
        "outlook": outlook,
        "prefill_horizon_h": PREFILL_H,
        "ar": cw3e,
        "alerts": alerts, "flood_watch": flood_watch, "flood_warning": flood_warning,
        "wetness": wet,
        "creek_now": {
            "ross_stage_ft": (usgs or {}).get("stage_ft") or (ross or {}).get("latest_stage_ft"),
            "ross_flow_cfs": (usgs or {}).get("flow_cfs"),
            "ross_observed": (usgs or {}).get("observed") or (ross or {}).get("observed"),
            "ross_nws_forecast_peak_ft": (ross or {}).get("forecast_peak_ft"),
            "bridge_stage_ft": (bridge or {}).get("stage_ft"),
            "bridge_observed": (bridge or {}).get("observed"),
            "bridge_nws_forecast_peak_ft": (bridge or {}).get("forecast_peak_ft"),
            "bridge_categories": (bridge or {}).get("categories"),
        },
        "station": ambient,
        "prefill": {
            "total_48h_in": (window or {}).get("total_48h_in"),
            "peak_1h_inhr": (window or {}).get("peak_1h_inhr"),
            "onset": (window or {}).get("onset"), "peak_at": (window or {}).get("peak_at"),
            "end": (window or {}).get("end"),
            "api_in": wet.get("api_in"), "season_rain_in": wet.get("season_rain_in"),
            "ar_category": (cw3e or {}).get("ar_category"),
            "flood_watch": flood_watch,
            "window_start_lead_h": (window or {}).get("window_start_lead_h"),
            "pop_mean_pct": (window or {}).get("pop_mean_pct"),
            "nws_total_in_window": (window or {}).get("nws_total_in_window"),
            "alternatives": {
                "open_meteo_raw_48h": totals(hourly, "om_in", 48),
                "open_meteo_corrected_48h": totals(hourly, "omc_in", 48),
                "nws_grid_48h": totals(hourly, "nws_in", 48),
                "cnrfc_apc_48h": (cn.get("APC") or {}).get("qpf_48h_in"),
                "cnrfc_venc1_48h": (cn.get("VENC1") or {}).get("qpf_48h_in"),
            },
        },
        "errors": errors,
    }
    (DATA / "forecast.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"{now.isoformat()} 48h={out['prefill']['total_48h_in']} in "
          f"peak={out['prefill']['peak_1h_inhr']} in/hr api={wet['api_in']} ({wet['category']}, "
          f"{wet['source']}) ar={out['prefill']['ar_category']} errors={errors}")


if __name__ == "__main__":
    sys.exit(main())
