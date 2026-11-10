# Spike: DMI radar

**Not part of the package.** This folder lives on the `spike/radar` branch only, to find out
whether radar is worth adding to pydmi and ha-dmi-weather before touching the real code.

## What DMI offers (from the API, 10 Nov 2026)

- `https://opendataapi.dmi.dk/v1/radardata/collections/composite/items`, scan types `fullRange`
  (240 km per radar) and `doppler` (120 km).
- Composite of all DMI radars, file name `dk.com.<YYYYMMDDhhmm>.500_max.h5`, covering about
  4.4–20.7°E, 52.3–59.8°N.
- New file every 10 minutes (`fullRange`), published about 12 minutes after its timestamp.
- HDF5 only (no images). File size not documented.

## Findings (first live run, 10 Nov 2026)

| | fullRange | doppler |
|---|---|---|
| Interval | 10 min (:00, :10 …) | 10 min, offset 5 min (:05, :15 …) |
| Published after | ~12 min | ~7 min |

- One file is **0.13 MB** (downloaded in 0.19 s): 1984 × 1728 points at 500 m, `uint8`, stereographic
  projection (`+proj=stere +lat_0=56 +lon_0=10.5666 +lat_ts=56`), ODIM_H5 V2_0, quantity `DBZH`.
- DMI puts `gain` (0.5), `offset` (-32), `nodata` (255) and `undetect` (0) in the **root** `what`, and
  `quantity` directly on `dataset1/data1`. Corners in `where` (`UL` = 3.0°E, 60.0°N).
- pyfive returns scalar attributes as 1-element arrays; the script unwraps them.
- pyfive (pure Python) reads the real file fine.

### Second run (doppler)

- `doppler` is published after ~7 min vs ~12 min for `fullRange`: about 5 minutes fresher.
- The 100 km crop around Gundsømagle is fully covered (no range edge), same file size (0.12 MB)
  and speed. Doppler is the better default; `fullRange` only matters near the edge of coverage.

## Nowcast sensors (option B)

Computed for the location from the newest frame (and the frame before it):

| Value | How |
|---|---|
| `rain_rate_mmh` | dBZ → Z = 10^(dBZ/10) → R = (Z/a)^(1/b), with a=200, b=1.6 from the file's `how` |
| `rain_rate_max_1km_mmh` | max over 2.5 × 2.5 km around the location |
| `rain_area_pct`, `rain_max_mmh` | share / max of rain ≥ threshold (default 0.2 mm/h) within the radius (default 50 km) |
| `nearest_rain_km` + direction | nearest rain pixel with ≥ 3 rain neighbours (drops speckle), bearing corrected to true north |
| `radar_coverage_pct` | share of the radius that has radar data (nodata = no coverage) |
| motion `speed_kmh`, `coming_from` | FFT phase correlation of the two newest frames in a 300 × 300 km window |
| `rain_expected_in_min` | follow the motion back from the location, 5-min steps, up to 2 h (0 = raining now) |

- The stereographic projection is done in plain Python (Snyder's oblique ellipsoidal formulas);
  identical to pyproj on DMI's grid, so **pyproj is not needed**.
- Checked on synthetic data: a blob 30 km west moving east at 60 km/h gives 60 km/h from W and
  rain in ~25 min; a blob passing by gives no ETA.
- Limits: constant-velocity extrapolation (no growth/decay); showers developing in place are
  not predicted. Static clutter would show as zero motion.

## Watch mode: compare with a rain gauge

```bash
caffeinate -i python spike/radar/radar_spike.py --watch 24 --since-midnight
```

- Backfills every doppler frame since local midnight (about 6 per hour, 2 s apart), then logs each
  new frame for 24 h to `radar_out/watch.csv` (also printed). `caffeinate -i` keeps the Mac awake.
- Columns: scan time (UTC/local), publish delay, rain rate at the location and max within ~1 km,
  `precip` (`none` / `drizzle` ≥ 0.1 / `rain` ≥ 0.2 mm/h), **`radar_total_today_mm`** (rate × 10 min,
  summed, reset at local midnight - compare with the station's daily rain), rain area / max / nearest
  rain within 50 km, motion speed / direction / correlation peak, rain ETA, coverage.
- DMI load: 1 listing + 1 download (0.12 MB) per 10 minutes. Downloaded files are not kept.
- First observation (10 Nov, 06:45): radar 0.11 mm/h within 1 km while the station reported
  near-invisible drizzle. Radar sees drizzle only weakly (small drops; beam 1–2 km up).

### First night vs the rain gauge (10 Nov, 00:00–07:05 local)

- **Timing matches the gauge to within one frame**: every 0.2 mm tip (02:09, 03:26, 04:06–05:08 UTC)
  falls in a radar rain period; the heaviest radar rain (1.54 mm/h at 04:55) matches the fastest tips
  (0.6 mm in 22 min). No tips after 05:08, radar only drizzle.
- **Amount too low**: radar 0.94 mm vs gauge 1.6 mm (about -40 %). The single pixel missed the first
  0.2 mm entirely (0.0 at the pixel, 0.12–0.17 within 1 km). Typical for drizzle / light rain with
  Marshall-Palmer and a beam 1–2 km up. → now uses the mean over 3×3 pixels (1.5 km); pixel kept
  as a separate column to compare. A correction factor may be needed (decide after more days).
- **Motion was broken**: 30 of 42 frames gave 0 km/h, peak 0.03–0.07. Phase correlation weighs
  every speck equally and locked onto echoes that never move. Reproduced with synthetic data
  (fixed echoes + moving rain: always 0 km/h). → replaced by plain cross-correlation (big rain
  areas dominate, quality 0..1) plus a **fixed-echo mask** learned in watch mode (pixels with echo
  in ≥ 80 % of frames over ~6 h, after 3 h of history). Synthetic test: correct motion at 2–47 %
  rain area; stationary rain stays at 0.
- Fixes: "rain now", `precip` and ETA 0 use the same 1.5 km mean; nearest rain = 0 km (no direction)
  while it rains at the location; `rain_max` only from despeckled rain (one 236 mm/h false echo seen).

## Run it

```bash
pip install numpy pillow pyfive               # pure-Python HDF5 reader
pip install h5py pyproj                       # optional: compiled reader / projection cross-check
python spike/radar/radar_spike.py --scan doppler                        # 2 newest frames + nowcast
python spike/radar/radar_spike.py --reader pyfive --compare-readers   # both readers, if h5py is installed
python spike/radar/radar_spike.py --frames 6                            # cost of a 1-hour loop
python spike/radar/radar_spike.py --file radar_out/<new>.h5 radar_out/<old>.h5   # offline, with motion
```

Output in `radar_out/`: the `.h5` file, a PNG cropped around the location (with a marker) and
`report.json` (timing, file size, download time, HDF5 structure, decode/render time, peak memory).

## Questions to answer

1. File size and download time: is one file every 10 minutes acceptable for DMI and for users?
2. Delay: is a picture that is 12–20 minutes old useful?
3. Can it run on a Home Assistant Yellow / Raspberry Pi (time, memory)? Does the pure-Python reader
   `pyfive` suffice, so we avoid `h5py`'s compiled HDF5 dependency (no wheel for Python 3.15 on macOS
   yet; Home Assistant OS uses Alpine/musl)? Is `pyproj` needed, or can the projection be done in plain
   Python?
4. Does it look useful compared with dmi.dk's own radar?
