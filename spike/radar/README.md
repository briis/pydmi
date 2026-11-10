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

## Run it

```bash
pip install numpy pillow pyproj pyfive        # pure-Python HDF5 reader
pip install h5py                              # optional: compiled reader, to compare
python spike/radar/radar_spike.py --lat 55.7357 --lon 12.1516           # newest file
python spike/radar/radar_spike.py --reader pyfive --compare-readers   # both readers, if h5py is installed
python spike/radar/radar_spike.py --frames 6                            # cost of a 1-hour loop
python spike/radar/radar_spike.py --file radar_out/<file>.h5 --radius-km 50   # offline re-render
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
