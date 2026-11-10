#!/usr/bin/env python3
"""SPIKE: is DMI radar worth adding to pydmi / ha-dmi-weather?

Not part of the package. Answers, with real data:
  1. How often new radar files arrive and how late (fullRange and doppler).
  2. How big a file is and how long it takes to download.
  3. What is inside (ODIM HDF5 structure, projection, grid, quantity, scaling).
  4. How long it takes, and how much memory, to turn one into a PNG cropped
     around a location.

Usage (needs: pip install h5py numpy pillow pyproj):
    python spike/radar/radar_spike.py --lat 55.7357 --lon 12.1516
    python spike/radar/radar_spike.py --file some.h5           # offline, existing file

Writes radar_out/: the downloaded .h5, a cropped PNG and report.json.
Requests: 2 listings + 1 download (+1 per --frames). Be kind to DMI.
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
    out = {}
    for k, v in obj.attrs.items():
        if hasattr(v, "decode"):
            v = v.decode(errors="replace")
        elif hasattr(v, "tolist"):
            v = v.tolist()
        out[k] = v
    return out


def describe(path: Path) -> dict:
    import h5py

    info: dict = {"groups": {}}
    with h5py.File(path, "r") as f:

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                info["groups"][name] = {"dataset": True, "shape": list(obj.shape), "dtype": str(obj.dtype)}
            elif obj.attrs:
                info["groups"][name] = _attrs(obj)

        info["root"] = _attrs(f)
        f.visititems(visit)
    return info


def find_data(path: Path):
    """First dataset*/data*/data array with its ODIM scaling and the root 'where'."""
    import h5py
    import numpy as np

    with h5py.File(path, "r") as f:
        where = _attrs(f["where"]) if "where" in f else {}
        for ds in sorted(k for k in f if k.startswith("dataset")):
            for dk in sorted(k for k in f[ds] if k.startswith("data")):
                node = f[ds][dk]
                if "data" not in node:
                    continue
                what = {**(_attrs(f[ds]["what"]) if "what" in f[ds] else {}),
                        **(_attrs(node["what"]) if "what" in node else {})}
                arr = np.asarray(node["data"][()])
                return arr, what, where, f"{ds}/{dk}"
    raise SystemExit("No dataset*/data*/data found - see the structure in report.json")


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


def to_pixels(where: dict, lat: float, lon: float):
    """(col, row, pixel_size_m) of lat/lon in the composite grid, via its projdef."""
    from pyproj import Proj

    proj = Proj(where["projdef"])
    ul_x, ul_y = proj(where["UL_lon"], where["UL_lat"])
    x, y = proj(lon, lat)
    return (x - ul_x) / where["xscale"], (ul_y - y) / where["yscale"], where["xscale"]


def render(path: Path, lat: float, lon: float, radius_km: float) -> dict:
    import numpy as np
    from PIL import Image, ImageDraw

    t0 = time.perf_counter()
    raw, what, where, node = find_data(path)
    gain, offset = float(what.get("gain", 1.0)), float(what.get("offset", 0.0))
    dbz = raw.astype(np.float32) * gain + offset
    for key in ("nodata", "undetect"):
        if key in what:
            dbz[raw == what[key]] = np.nan
    t_decode = time.perf_counter() - t0

    result = {
        "node": node,
        "quantity": what.get("quantity"),
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
    ap.add_argument("--frames", type=int, default=1, help="download this many newest files (animation cost)")
    ap.add_argument("--file", type=Path, help="use an existing .h5 instead of downloading")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    report: dict = {"run_at": datetime.now(UTC).isoformat(), "args": {k: str(v) for k, v in vars(args).items()}}

    files: list[Path] = []
    if args.file:
        files = [args.file]
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

    report["structure"] = describe(files[0])
    report["render"] = render(files[0], args.lat, args.lon, args.radius_km)
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
    print(f"render     decode {r['decode_s']} s + png {r['render_s']} s -> {r['png']} "
          f"({r['png_kb']} kB, {r['crop_px'][0]}x{r['crop_px'][1]} px)")
    print(f"memory     peak {report['peak_memory_mb']} MB")
    print(f"full report: {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
