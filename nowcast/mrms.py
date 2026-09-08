#!/usr/bin/env python3
"""
MRMS ingest for 52 Woodland — grid maths, decode, crop, and the SQLite store.

MRMS national grid (verified against a real frame, not assumed):
    7000 x 3500 at 0.01 deg, first grid point 54.995 N / -129.995 E,
    latitude decreasing, longitude increasing, PNG-packed, -3 = no coverage.

The house lands on pixel (j=1702, i=743) whose centre is 37.9750 / -122.5650,
about 300 m from the actual address — well inside one 1 km cell.

ecCodes note: the eccodes wheel for Python 3.14 does not exist, so the library
comes from ecmwflibs. findlibs looks for "libeccodes.dll" while ecmwflibs ships
"eccodes.dll", so lib/libeccodes.dll is a copy under that name and ECCODES_HOME
points here. bootstrap() wires that up before eccodes is imported.
"""
import gzip
import io
import json
import math
import os
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = json.loads((ROOT / "config.json").read_text())
LAT, LON = CFG["lat"], CFG["lon"]

DB = HERE / "mrms.sqlite"
BASIN_JSON = HERE / "basin.json"

# --- MRMS grid ---------------------------------------------------------------
NI, NJ = 7000, 3500
LAT0, LON0, DX = 54.995, -129.995, 0.01
BOX_MILES = 150

IEM = "https://mtarchive.geol.iastate.edu/{y:04d}/{m:02d}/{d:02d}/mrms/ncep/{prod}/"
NCEP = "https://mrms.ncep.noaa.gov/data/2D/{prod}/"
PRODUCTS = {"PrecipRate": "MRMS_PrecipRate_00.00_{stamp}.grib2.gz",
            "RadarOnly_QPE_01H": "MRMS_RadarOnly_QPE_01H_00.00_{stamp}.grib2.gz"}
UA = {"User-Agent": "floodboard-52woodland nowcast (personal flood monitor)"}

_BOOTSTRAPPED = False


def bootstrap():
    """Make eccodes importable. Safe to call repeatedly."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    os.environ.setdefault("ECCODES_HOME", str(HERE))
    for d in (r"C:\Python314\Lib\site-packages\ecmwflibs",):
        if os.path.isdir(d):
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    _BOOTSTRAPPED = True


def rowcol(lat, lon):
    return int(round((LAT0 - lat) / DX)), int(round((lon - LON0) / DX))


def crop_window(miles=BOX_MILES):
    """(j0, j1, i0, i1) for a box `miles` around the house."""
    r = miles * 1.609344
    dlat = r / 111.32
    dlon = r / (111.32 * math.cos(math.radians(LAT)))
    j0, _ = rowcol(LAT + dlat, LON)
    j1, _ = rowcol(LAT - dlat, LON)
    _, i0 = rowcol(LAT, LON - dlon)
    _, i1 = rowcol(LAT, LON + dlon)
    return j0, j1, i0, i1


J0, J1, I0, I1 = crop_window()
STA_J, STA_I = rowcol(LAT, LON)
STA_RJ, STA_RI = STA_J - J0, STA_I - I0          # station within the crop
MM_PER_IN = 25.4


# --- basin -------------------------------------------------------------------
def basin_polygon():
    """Corte Madera Creek at Ross (USGS 11460000) drainage, from USGS NLDI."""
    if not BASIN_JSON.exists():
        r = requests.get(
            "https://api.water.usgs.gov/nldi/linked-data/nwissite/"
            f"USGS-{CFG['usgs_site']}/basin", headers=UA, timeout=60)
        r.raise_for_status()
        BASIN_JSON.write_text(r.text)
    g = json.loads(BASIN_JSON.read_text())["features"][0]["geometry"]
    if g["type"] == "Polygon":
        return g["coordinates"][0]
    return max(g["coordinates"], key=lambda p: len(p[0]))[0]


def basin_mask():
    """Boolean mask over the crop marking cells inside the Ross basin.

    Ray casting rather than a GIS dependency: the polygon is 66 points and the
    crop is 434x550, so this costs nothing and avoids pulling in shapely.
    """
    ring = np.asarray(basin_polygon(), dtype=float)
    lats = LAT0 - (np.arange(J0, J1) * DX)
    lons = LON0 + (np.arange(I0, I1) * DX)
    LO, LA = np.meshgrid(lons, lats)
    inside = np.zeros(LA.shape, dtype=bool)
    x, y = ring[:, 0], ring[:, 1]
    for k in range(len(ring) - 1):
        x1, y1, x2, y2 = x[k], y[k], x[k + 1], y[k + 1]
        cond = ((y1 > LA) != (y2 > LA))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (LA - y1) / (y2 - y1) + x1
        inside ^= cond & (LO < xint)
    return inside


# --- decode ------------------------------------------------------------------
def decode(raw_gz):
    """gzipped GRIB2 bytes -> full (3500, 7000) float32 grid, -3 preserved."""
    bootstrap()
    import eccodes as ec
    data = gzip.decompress(raw_gz)
    gid = ec.codes_new_from_message(data)
    try:
        return ec.codes_get_values(gid).reshape(NJ, NI).astype(np.float32)
    finally:
        ec.codes_release(gid)


def fetch(product, when, archive=True, session=None):
    """Download one frame. `when` is UTC, rounded to the product's 2-min slot."""
    stamp = when.strftime("%Y%m%d-%H%M%S")
    s = session or requests
    if archive:
        url = (IEM.format(y=when.year, m=when.month, d=when.day, prod=product)
               + f"{product}_00.00_{stamp}.grib2.gz")
    else:
        url = NCEP.format(prod=product) + PRODUCTS[product].format(stamp=stamp)
    r = s.get(url, headers=UA, timeout=90)
    if r.status_code != 200:
        raise FileNotFoundError(f"{r.status_code} {url}")
    return r.content


def summarise(grid, mask=None):
    """Station pixel, basin mean and box stats, all in mm/hr (or mm for QPE)."""
    box = grid[J0:J1, I0:I1]
    valid = box >= 0
    sta = float(box[STA_RJ, STA_RI])
    out = {
        "station": None if sta < 0 else sta,
        "box_max": float(box[valid].max()) if valid.any() else None,
        "box_mean": float(box[valid].mean()) if valid.any() else None,
        "box_wet_frac": float((box[valid] > 0.1).mean()) if valid.any() else None,
    }
    if mask is not None:
        m = mask & valid
        out["basin_mean"] = float(box[m].mean()) if m.any() else None
        out["basin_max"] = float(box[m].max()) if m.any() else None
    return out, box


def pack(box):
    """Crop -> compressed blob. int16 tenths of mm/hr; -1 marks no coverage."""
    q = np.where(box < 0, -1, np.round(box * 10)).astype(np.int16)
    return zlib.compress(q.tobytes(), 6)


def unpack(blob, rows=None, cols=None):
    rows = rows or (J1 - J0)
    cols = cols or (I1 - I0)
    q = np.frombuffer(zlib.decompress(blob), dtype=np.int16).reshape(rows, cols)
    return np.where(q < 0, np.nan, q / 10.0).astype(np.float32)


# --- store -------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
  product      TEXT NOT NULL,
  valid_utc    TEXT NOT NULL,
  station      REAL,          -- mm/hr (PrecipRate) or mm (QPE_01H)
  basin_mean   REAL,
  basin_max    REAL,
  box_max      REAL,
  box_mean     REAL,
  box_wet_frac REAL,
  rows         INTEGER,
  cols         INTEGER,
  j0           INTEGER,
  i0           INTEGER,
  crop         BLOB,
  fetched_utc  TEXT,
  PRIMARY KEY (product, valid_utc)
);
CREATE INDEX IF NOT EXISTS ix_frames_time ON frames(valid_utc);

CREATE TABLE IF NOT EXISTS poll_log (
  ts_utc   TEXT PRIMARY KEY,
  wet      INTEGER,
  interval_min INTEGER,
  note     TEXT
);
"""


def connect(path=DB):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def store(con, product, when, summary, box, keep_crop=True):
    con.execute(
        "INSERT OR REPLACE INTO frames (product,valid_utc,station,basin_mean,"
        "basin_max,box_max,box_mean,box_wet_frac,rows,cols,j0,i0,crop,fetched_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (product, when.strftime("%Y-%m-%dT%H:%M:%SZ"), summary.get("station"),
         summary.get("basin_mean"), summary.get("basin_max"), summary.get("box_max"),
         summary.get("box_mean"), summary.get("box_wet_frac"),
         J1 - J0, I1 - I0, J0, I0,
         pack(box) if keep_crop else None,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()


def slot(when, minutes=2):
    """Round down to the product's cadence."""
    return when.replace(second=0, microsecond=0,
                        minute=(when.minute // minutes) * minutes)


if __name__ == "__main__":
    bootstrap()
    m = basin_mask()
    print(f"crop      rows {J0}:{J1} cols {I0}:{I1}  = {J1-J0} x {I1-I0} px "
          f"({BOX_MILES} mi box)")
    print(f"station   grid ({STA_J},{STA_I}) -> crop ({STA_RJ},{STA_RI}) "
          f"centre {LAT0-STA_J*DX:.4f},{LON0+STA_I*DX:.4f}")
    print(f"basin     {m.sum()} cells in crop = {m.sum():.0f} km2 "
          f"({m.sum()/2.59:.1f} sq mi)")
    print(f"db        {DB}")
