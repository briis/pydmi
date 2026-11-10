#!/usr/bin/env python3
"""SPIKE: is DMI radar worth adding to pydmi / ha-dmi-weather?

Not part of the package. Answers, with real data:
  1. How often new radar files arrive and how late (fullRange and doppler).
  2. How big a file is and how long it takes to download.
  3. What is inside (ODIM HDF5 structure, projection, grid, quantity, scaling).
  4. How long it takes, and how much memory, to turn one into a PNG cropped
     around a location.
  5. Nowcast "sensors" for the location: radar rain rate (mm/h), rain area and
     nearest rain within a radius, and - from the two newest frames - how the
     rain moves and when it would reach the location.

Usage (needs: pip install numpy pillow, plus pyfive and/or h5py; pyproj optional,
only used to cross-check the built-in projection):
    python spike/radar/radar_spike.py --scan doppler           # 2 newest frames
    python spike/radar/radar_spike.py --file a.h5 b.h5         # offline, existing files
    python spike/radar/radar_spike.py --reader pyfive          # pure-Python HDF5 reader

HDF5 readers: h5py (compiled, needs the HDF5 C library) or pyfive (pure Python,
no compiled code - would avoid binary dependencies in Home Assistant).

Writes radar_out/: the downloaded .h5, a cropped PNG and report.json.
Requests: 3 listings + 1 download per --frames (default 2). Be kind to DMI.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import resource
import sys
import time
import urllib.request

BASE = "https://opendataapi.dmi.dk/v1/radardata"
UA = "pydmi-radar-spike (+https://github.com/briis/pydmi)"
OUT = Path("radar_out")


# --- HTTP -------------------------------------------------------------------


def get(url: str, timeout: float = 60) -> tuple[bytes, float]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data, time.perf_counter() - t0


def list_items(scan: str, limit: int) -> list[dict]:
    url = (
        f"{BASE}/collections/composite/items"
        f"?scanType={scan}&sortorder=datetime,DESC&limit={limit}"
    )
    raw, _ = get(url)
    return json.loads(raw)["features"]


def timing(features: list[dict], now: datetime) -> dict:
    times = [datetime.fromisoformat(f["properties"]["datetime"].replace("Z", "+00:00")) for f in features]
    created = [datetime.fromisoformat(f["properties"]["created"].replace("Z", "+00:00")) for f in features]
    gaps = sorted({round((a - b).total_seconds() / 60) for a, b in zip(times, times[1:], strict=False)})
    delays = [round((c - t).total_seconds() / 60, 1) for t, c in zip(times, created, strict=True)]
    return {
        "newest": features[0]["id"] if features else None,
        "newest_age_min": round((now - times[0]).total_seconds() / 60, 1) if times else None,
        "interval_min": gaps,
        "publish_delay_min": {"min": min(delays), "max": max(delays)} if delays else None,
    }


# --- HDF5 / ODIM ---------------------------------------------------------------


def _attrs(obj) -> dict:
    """Attributes as plain Python values. pyfive returns scalars as 1-element
    arrays (UL_lon: [3.0]) - unwrap those."""
    out = {}
    for k, v in obj.attrs.items():
        if hasattr(v, "decode"):
            v = v.decode(errors="replace")
        elif hasattr(v, "tolist"):
            v = v.tolist()
        if isinstance(v, list) and len(v) == 1:
            v = v[0]
        if isinstance(v, bytes):
            v = v.decode(errors="replace")
        out[k] = v
    return out


READER = "auto"


def _open(path: Path):
    """Open with h5py or pyfive (READER = auto/h5py/pyfive)."""
    if READER in ("auto", "h5py"):
        try:
            import h5py

            return h5py.File(path, "r"), "h5py"
        except ImportError:
            if READER == "h5py":
                raise
    import pyfive

    return pyfive.File(str(path)), "pyfive"


def _is_dataset(obj) -> bool:
    return hasattr(obj, "shape") and not hasattr(obj, "keys")


def describe(path: Path) -> dict:
    info: dict = {"groups": {}}
    f, reader = _open(path)
    info["reader"] = reader

    def walk(group, prefix: str) -> None:
        for key in group.keys():
            obj = group[key]
            name = f"{prefix}{key}"
            if _is_dataset(obj):
                info["groups"][name] = {"dataset": True, "shape": list(obj.shape), "dtype": str(obj.dtype)}
            else:
                if obj.attrs:
                    info["groups"][name] = _attrs(obj)
                walk(obj, f"{name}/")

    try:
        info["root"] = _attrs(f)
        walk(f, "")
    finally:
        if hasattr(f, "close"):
            f.close()
    return info


def find_data(path: Path):
    """First dataset*/data*/data array with its ODIM scaling and the root 'where'."""
    import numpy as np

    f, _ = _open(path)
    with _closing(f):
        where = _attrs(f["where"]) if "where" in f.keys() else {}
        # ODIM lets gain/offset/nodata/undetect/quantity live at any level; DMI puts the
        # scaling in the ROOT "what" and "quantity" on the data group itself. Merge all,
        # most specific last.
        root_what = _attrs(f["what"]) if "what" in f.keys() else {}
        for ds in sorted(k for k in f.keys() if k.startswith("dataset")):
            ds_what = _attrs(f[ds]["what"]) if "what" in f[ds].keys() else {}
            for dk in sorted(k for k in f[ds].keys() if k.startswith("data")):
                node = f[ds][dk]
                if "data" not in node.keys():
                    continue
                node_what = _attrs(node["what"]) if "what" in node.keys() else {}
                what = {**root_what, **ds_what, **_attrs(node), **node_what}
                arr = np.asarray(node["data"][()])
                return arr, what, where, f"{ds}/{dk}"
    raise SystemExit("No dataset*/data*/data found - see the structure in report.json")


class _closing:
    def __init__(self, f) -> None:
        self.f = f

    def __enter__(self):
        return self.f

    def __exit__(self, *exc) -> None:
        if hasattr(self.f, "close"):
            self.f.close()


# --- rendering ----------------------------------------------------------------

# Reflectivity (dBZ) colour scale, roughly like common weather radar maps.
DBZ_COLOURS = [
    (5, (160, 230, 255, 140)),
    (15, (60, 170, 255, 170)),
    (20, (30, 110, 230, 190)),
    (25, (40, 200, 70, 200)),
    (30, (20, 150, 40, 210)),
    (35, (250, 230, 40, 220)),
    (40, (250, 160, 20, 230)),
    (45, (240, 70, 20, 235)),
    (50, (200, 0, 30, 240)),
    (55, (220, 0, 220, 245)),
]


def colourise(dbz):
    import numpy as np

    rgba = np.zeros((*dbz.shape, 4), dtype=np.uint8)
    for threshold, colour in DBZ_COLOURS:
        rgba[dbz >= threshold] = colour
    rgba[np.isnan(dbz)] = (0, 0, 0, 0)
    return rgba


def _projdef(projdef: str) -> dict:
    out = {}
    for part in projdef.split():
        if part.startswith("+") and "=" in part:
            k, v = part[1:].split("=", 1)
            out[k] = v
    return out


def stere_xy(projdef: str, lat: float, lon: float) -> tuple[float, float]:
    """Oblique stereographic on the WGS84 ellipsoid (what PROJ's +proj=stere does
    when lat_0 is not a pole; Snyder 1987, eq. 21-27). Plain Python, no pyproj."""
    p = _projdef(projdef)
    if p.get("proj") != "stere" or p.get("ellps", "WGS84") != "WGS84":
        raise ValueError(f"unsupported projection: {projdef}")
    a, f = 6378137.0, 1 / 298.257223563
    e = math.sqrt(f * (2 - f))
    k0 = float(p.get("k", p.get("k_0", 1.0)))
    phi0, lam0 = math.radians(float(p["lat_0"])), math.radians(float(p["lon_0"]))
    phi, lam = math.radians(lat), math.radians(lon)

    def chi(ph: float) -> float:  # conformal latitude
        es = e * math.sin(ph)
        return 2 * math.atan(math.tan(math.pi / 4 + ph / 2) * ((1 - es) / (1 + es)) ** (e / 2)) - math.pi / 2

    def m(ph: float) -> float:
        return math.cos(ph) / math.sqrt(1 - (e * math.sin(ph)) ** 2)

    c0, c = chi(phi0), chi(phi)
    dl = lam - lam0
    big_a = 2 * a * k0 * m(phi0) / (math.cos(c0) * (1 + math.sin(c0) * math.sin(c) + math.cos(c0) * math.cos(c) * math.cos(dl)))
    x = big_a * math.cos(c) * math.sin(dl)
    y = big_a * (math.cos(c0) * math.sin(c) - math.sin(c0) * math.cos(c) * math.cos(dl))
    return x, y


def to_pixels(where: dict, lat: float, lon: float):
    """(col, row, pixel_size_m) of lat/lon in the composite grid, via its projdef."""
    ul_x, ul_y = stere_xy(where["projdef"], where["UL_lat"], where["UL_lon"])
    x, y = stere_xy(where["projdef"], lat, lon)
    return (x - ul_x) / where["xscale"], (ul_y - y) / where["yscale"], where["xscale"]


def projection_check(where: dict, lat: float, lon: float) -> dict:
    """Compare the plain-Python projection with pyproj, if pyproj is installed."""
    try:
        from pyproj import Proj
    except ImportError:
        return {"pyproj": "not installed"}
    proj = Proj(where["projdef"])
    worst = 0.0
    for la, lo in ((lat, lon), (where["UL_lat"], where["UL_lon"]), (where["LR_lat"], where["LR_lon"]), (54.6, 8.0), (57.7, 10.6), (55.1, 14.9)):
        px, py = proj(lo, la)
        ox, oy = stere_xy(where["projdef"], la, lo)
        worst = max(worst, abs(px - ox), abs(py - oy))
    return {"max_difference_m": round(worst, 3)}


def decode(path: Path):
    """dBZ grid (float32). nodata -> NaN (no radar coverage), undetect -> -inf (covered, no echo)."""
    import numpy as np

    raw, what, where, node = find_data(path)
    gain, offset = float(what.get("gain", 1.0)), float(what.get("offset", 0.0))
    dbz = raw.astype(np.float32) * gain + offset
    if "undetect" in what:
        dbz[raw == what["undetect"]] = -np.inf
    if "nodata" in what:
        dbz[raw == what["nodata"]] = np.nan
    return raw, dbz, what, where, node


def scan_time(what: dict) -> datetime | None:
    try:
        return datetime.strptime(f"{what['date']}{str(what['time'])[:4]}", "%Y%m%d%H%M").replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return None


# --- nowcast sensors -----------------------------------------------------------


def rain_rate(dbz, zr_a: float, zr_b: float):
    """mm/h from dBZ with Z = a R^b (DMI's file says a=200, b=1.6 = Marshall-Palmer)."""
    import numpy as np

    with np.errstate(invalid="ignore", over="ignore"):
        rate = (np.power(10.0, dbz / 10.0) / zr_a) ** (1.0 / zr_b)
    return np.where(np.isfinite(dbz), rate, np.where(np.isnan(dbz), np.nan, 0.0)).astype(np.float32)


def _neighbour_count(mask):
    """Number of True cells in each 3x3 neighbourhood (despeckling)."""
    import numpy as np

    p = np.pad(mask.astype(np.uint8), 1)
    h, w = mask.shape
    return sum(p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))


def grid_north_deg(where: dict, lat: float, lon: float) -> float:
    """Angle (deg, clockwise) of true north as seen in the grid at the location."""
    x0, y0 = stere_xy(where["projdef"], lat, lon)
    x1, y1 = stere_xy(where["projdef"], lat + 0.01, lon)
    return math.degrees(math.atan2(x1 - x0, y1 - y0))


def compass(deg: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(deg / 45) % 8]


def sensors(rate, where: dict, lat: float, lon: float, radius_km: float, rain_mmh: float) -> dict:
    """Values a Home Assistant integration could expose, computed for one frame."""
    import numpy as np

    col, row, px = to_pixels(where, lat, lon)
    c, r = round(col), round(row)
    h, w = rate.shape
    if not (0 <= c < w and 0 <= r < h):
        return {"error": "location outside radar grid"}
    rad = int(radius_km * 1000 / px)
    r0, r1, c0, c1 = max(0, r - rad), min(h, r + rad + 1), max(0, c - rad), min(w, c + rad + 1)
    win = rate[r0:r1, c0:c1]
    yy, xx = np.mgrid[r0:r1, c0:c1]
    dist_km = np.hypot(xx - col, yy - row) * px / 1000
    inside = dist_km <= radius_km
    covered = inside & np.isfinite(win)
    rain = np.nan_to_num(win, nan=0.0) >= rain_mmh
    # A rain pixel counts for "nearest rain" only with >= 3 rain pixels around it (drops speckle/clutter).
    solid = rain & (_neighbour_count(rain) >= 3)

    here = rate[r, c]
    near = rate[max(0, r - 2):r + 3, max(0, c - 2):c + 3]  # 2.5 x 2.5 km
    out: dict = {
        "rain_rate_mmh": None if np.isnan(here) else round(float(here), 2),
        "rain_rate_max_1km_mmh": None if np.all(np.isnan(near)) else round(float(np.nanmax(near)), 2),
        "radar_coverage_pct": round(100 * float(covered.sum() / max(1, inside.sum())), 1),
        "rain_area_pct": round(100 * float((rain & covered).sum() / max(1, covered.sum())), 1),
        "rain_max_mmh": round(float(np.nanmax(np.where(covered, win, np.nan))), 2) if covered.any() else None,
        "radius_km": radius_km,
        "rain_threshold_mmh": rain_mmh,
    }
    cand = solid & inside
    if cand.any():
        d = np.where(cand, dist_km, np.inf)
        i = np.unravel_index(int(np.argmin(d)), d.shape)
        dx, dy = (xx[i] - col), -(yy[i] - row)  # grid east, grid north (pixels)
        bearing = (math.degrees(math.atan2(dx, dy)) - grid_north_deg(where, lat, lon)) % 360
        out["nearest_rain_km"] = round(float(d[i]), 1)
        out["nearest_rain_bearing_deg"] = round(bearing)
        out["nearest_rain_direction"] = compass(bearing)
    else:
        out["nearest_rain_km"] = None
    return out


def motion(prev_rate, now_rate, minutes: float, where: dict, lat: float, lon: float,
           rain_mmh: float, window_km: float = 150) -> dict:
    """Movement of the rain field between two frames (FFT phase correlation over a
    window around the location), plus a simple nowcast: follow the field backwards
    from the location to see when rain would arrive."""
    import numpy as np

    col, row, px = to_pixels(where, lat, lon)
    c, r = round(col), round(row)
    half = int(window_km * 1000 / px)
    h, w = now_rate.shape
    r0, r1, c0, c1 = max(0, r - half), min(h, r + half), max(0, c - half), min(w, c + half)

    def field(rate):
        f = np.nan_to_num(rate[r0:r1, c0:c1], nan=0.0)
        return np.log1p(np.where(f >= rain_mmh, f, 0.0))

    a, b = field(now_rate), field(prev_rate)
    if a.max() == 0 or b.max() == 0:
        return {"note": "no rain in the window - no motion to track"}
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2(a * win), np.fft.fft2(b * win)
    cross = fa * np.conj(fb)
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.ifft2(cross).real
    peak = np.unravel_index(int(np.argmax(corr)), corr.shape)
    dy_px, dx_px = (p if p <= s // 2 else p - s for p, s in zip(peak, corr.shape, strict=True))
    # displacement prev -> now in pixels; convert to grid east/north, then to true direction
    east, north = dx_px * px, -dy_px * px  # metres in `minutes`
    speed_kmh = math.hypot(east, north) / 1000 / (minutes / 60)
    rot = grid_north_deg(where, lat, lon)
    towards = (math.degrees(math.atan2(east, north)) - rot) % 360
    out = {
        "shift_px": [int(dx_px), int(dy_px)],
        "speed_kmh": round(speed_kmh, 1),
        "moving_towards_deg": round(towards) if speed_kmh > 0 else None,
        "coming_from": compass((towards + 180) % 360) if speed_kmh > 0 else None,
        "peak_strength": round(float(corr.max()), 3),
    }
    # Nowcast: what is upstream of us now will be here in t minutes.
    vx, vy = dx_px / minutes, dy_px / minutes  # pixels per minute (grid col/row)
    eta = None
    for t in range(0, 121, 5):
        pc, pr = round(col - vx * t), round(row - vy * t)
        if not (2 <= pc < w - 2 and 2 <= pr < h - 2):
            break
        if np.nanmax(np.nan_to_num(now_rate[pr - 2:pr + 3, pc - 2:pc + 3], nan=0.0)) >= rain_mmh:
            eta = t
            break
    out["rain_expected_in_min"] = eta
    out["nowcast_horizon_min"] = 120
    return out


def render(path: Path, lat: float, lon: float, radius_km: float) -> dict:
    import numpy as np
    from PIL import Image, ImageDraw

    t0 = time.perf_counter()
    raw, dbz, what, where, node = decode(path)
    gain, offset = float(what.get("gain", 1.0)), float(what.get("offset", 0.0))
    t_decode = time.perf_counter() - t0

    result = {
        "node": node,
        "quantity": what.get("quantity") or what.get("product"),
        "grid": list(raw.shape),
        "dtype": str(raw.dtype),
        "gain": gain,
        "offset": offset,
        "dbz_max": None if np.all(np.isnan(dbz)) else round(float(np.nanmax(dbz)), 1),
        "pixels_with_echo_pct": round(100 * float(np.mean(dbz >= 5)), 2),
        "decode_s": round(t_decode, 3),
    }

    crop = (0, 0, raw.shape[1], raw.shape[0])
    marker = None
    if "projdef" in where:
        try:
            col, row, px = to_pixels(where, lat, lon)
            r = radius_km * 1000 / px
            crop = (max(0, int(col - r)), max(0, int(row - r)),
                    min(raw.shape[1], int(col + r)), min(raw.shape[0], int(row + r)))
            marker = (col - crop[0], row - crop[1])
            result["pixel_size_m"] = px
            result["location_pixel"] = [round(col), round(row)]
        except Exception as err:  # noqa: BLE001 - spike: report and render the whole grid
            result["projection_error"] = repr(err)

    t1 = time.perf_counter()
    img = Image.fromarray(colourise(dbz[crop[1]:crop[3], crop[0]:crop[2]]), "RGBA")
    bg = Image.new("RGBA", img.size, (24, 28, 36, 255))
    bg.alpha_composite(img)
    if marker:
        d = ImageDraw.Draw(bg)
        mx, my = marker
        d.ellipse((mx - 5, my - 5, mx + 5, my + 5), outline=(255, 255, 255, 255), width=2)
    png = OUT / f"{path.stem}_crop{int(radius_km)}km.png"
    bg.save(png, optimize=True)
    result.update(
        crop_px=[crop[2] - crop[0], crop[3] - crop[1]],
        render_s=round(time.perf_counter() - t1, 3),
        png=str(png),
        png_kb=round(png.stat().st_size / 1024, 1),
    )
    return result


# --- main -----------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=55.7357)
    ap.add_argument("--lon", type=float, default=12.1516)
    ap.add_argument("--radius-km", type=float, default=100)
    ap.add_argument("--scan", choices=["fullRange", "doppler"], default="fullRange")
    ap.add_argument("--frames", type=int, default=2,
                    help="download this many newest files (2 = enough for motion/ETA; 6 = cost of a 1-hour loop)")
    ap.add_argument("--file", type=Path, nargs="+", help="use existing .h5 file(s) instead of downloading")
    ap.add_argument("--sensor-radius-km", type=float, default=50, help="radius for rain area / nearest rain")
    ap.add_argument("--rain-mmh", type=float, default=0.2, help="rain threshold in mm/h")
    ap.add_argument("--reader", choices=["auto", "h5py", "pyfive"], default="auto",
                    help="HDF5 reader (auto = h5py if installed, else pyfive)")
    ap.add_argument("--compare-readers", action="store_true",
                    help="decode the file with both h5py and pyfive and compare time")
    args = ap.parse_args()
    global READER
    READER = args.reader
    OUT.mkdir(exist_ok=True)
    report: dict = {"run_at": datetime.now(UTC).isoformat(), "args": {k: str(v) for k, v in vars(args).items()}}

    files: list[Path] = []
    if args.file:
        files = list(args.file)
    else:
        now = datetime.now(UTC)
        report["timing"] = {}
        for scan in ("fullRange", "doppler"):
            try:
                report["timing"][scan] = timing(list_items(scan, 7), now)
            except Exception as err:  # noqa: BLE001
                report["timing"][scan] = {"error": repr(err)}
        features = list_items(args.scan, args.frames)
        report["downloads"] = []
        for feat in features:
            href = feat["asset"]["data"]["href"]
            data, secs = get(href, timeout=120)
            target = OUT / feat["id"]
            target.write_bytes(data)
            files.append(target)
            report["downloads"].append(
                {"file": feat["id"], "mb": round(len(data) / 1_048_576, 2), "seconds": round(secs, 2)}
            )
            time.sleep(2)  # spacing between downloads

    # newest first, by scan time in the file
    frames = []
    for path in files:
        _, dbz, what, where, _ = decode(path)
        frames.append((scan_time(what) or datetime.min.replace(tzinfo=UTC), path, dbz, what, where))
    frames.sort(key=lambda fr: fr[0], reverse=True)
    files = [fr[1] for fr in frames]
    t_new, _, dbz_new, what_new, where_new = frames[0]
    zr_a, zr_b = 200.0, 1.6
    try:
        how = describe(files[0])["groups"].get("how", {})
        zr_a, zr_b = float(how.get("zr-a", zr_a)), float(how.get("zr-b", zr_b))
    except Exception:  # noqa: BLE001
        pass
    t0 = time.perf_counter()
    rate_new = rain_rate(dbz_new, zr_a, zr_b)
    report["sensors"] = {"scan_time": t_new.isoformat(), "zr": [zr_a, zr_b],
                         **sensors(rate_new, where_new, args.lat, args.lon, args.sensor_radius_km, args.rain_mmh)}
    if len(frames) >= 2:
        t_prev, _, dbz_prev, _, _ = frames[1]
        minutes = (t_new - t_prev).total_seconds() / 60 or 10
        report["motion"] = {"frames": [t_prev.isoformat(), t_new.isoformat()], "minutes": minutes,
                            **motion(rain_rate(dbz_prev, zr_a, zr_b), rate_new, minutes, where_new,
                                     args.lat, args.lon, args.rain_mmh)}
    report["sensors"]["compute_s"] = round(time.perf_counter() - t0, 3)
    report["projection_check"] = projection_check(where_new, args.lat, args.lon)

    report["structure"] = describe(files[0])
    report["render"] = render(files[0], args.lat, args.lon, args.radius_km)
    report["render"]["reader"] = report["structure"]["reader"]
    if args.compare_readers:
        report["reader_compare"] = {}
        for name in ("h5py", "pyfive"):
            READER = name
            try:
                t0 = time.perf_counter()
                find_data(files[0])
                report["reader_compare"][name] = {"read_s": round(time.perf_counter() - t0, 3)}
            except Exception as err:  # noqa: BLE001
                report["reader_compare"][name] = {"error": repr(err)}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["peak_memory_mb"] = round(rss / (1_048_576 if sys.platform == "darwin" else 1024), 1)

    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))

    # short summary
    print("\n=== DMI radar spike ===")
    for scan, t in (report.get("timing") or {}).items():
        print(f"{scan:10} newest {t.get('newest')}  age {t.get('newest_age_min')} min  "
              f"interval {t.get('interval_min')} min  publish delay {t.get('publish_delay_min')}")
    for d in report.get("downloads", []):
        print(f"download   {d['file']}  {d['mb']} MB in {d['seconds']} s")
    r = report["render"]
    print(f"data       {r['node']}  quantity={r['quantity']}  grid={r['grid']} {r['dtype']}  "
          f"pixel={r.get('pixel_size_m')} m  max={r['dbz_max']} dBZ  echo={r['pixels_with_echo_pct']}%")
    for name, res in (report.get("reader_compare") or {}).items():
        print(f"reader     {name}: {res}")
    print(f"render     [{r['reader']}] decode {r['decode_s']} s + png {r['render_s']} s -> {r['png']} "
          f"({r['png_kb']} kB, {r['crop_px'][0]}x{r['crop_px'][1]} px)")
    se = report["sensors"]
    print(f"sensors    @ {se['scan_time']}: rain here {se.get('rain_rate_mmh')} mm/h "
          f"(max within ~1 km {se.get('rain_rate_max_1km_mmh')}), "
          f"rain on {se.get('rain_area_pct')}% within {se.get('radius_km')} km (max {se.get('rain_max_mmh')} mm/h), "
          f"coverage {se.get('radar_coverage_pct')}%")
    if se.get("nearest_rain_km") is not None:
        print(f"           nearest rain {se['nearest_rain_km']} km to the {se['nearest_rain_direction']} "
              f"({se['nearest_rain_bearing_deg']} deg)")
    else:
        print(f"           no rain >= {se.get('rain_threshold_mmh')} mm/h within {se.get('radius_km')} km")
    mo = report.get("motion")
    if mo:
        if "speed_kmh" in mo:
            print(f"motion     {mo['speed_kmh']} km/h from {mo['coming_from']}  shift {mo['shift_px']} px "
                  f"in {mo['minutes']:.0f} min  (peak {mo['peak_strength']})")
            eta = mo["rain_expected_in_min"]
            print(f"nowcast    {'raining now' if eta == 0 else f'rain in ~{eta} min' if eta else 'no rain expected within 2 h'}")
        else:
            print(f"motion     {mo.get('note')}")
    print(f"projection plain Python vs pyproj: {report['projection_check']}  (sensor compute {se['compute_s']} s)")
    print(f"memory     peak {report['peak_memory_mb']} MB")
    print(f"full report: {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
