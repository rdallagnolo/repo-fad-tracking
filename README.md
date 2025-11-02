# FAD Tracks Builder

Utilities to turn hourly GPS fixes from FAD (Fish Aggregating Device) buoys into
Google Earth KMLs, CSVs, and (optionally) ESRI Shapefiles. The script keeps a
cumulative archive of all fixes and highlights buoys operating inside defined
areas.

## Features

- Appends new `buoys*.csv` files to a cumulative archive (`all_points.csv`)
- Deduplicates on (`buoy_id`, `timestamp`)
- Flags positions inside “deployment” and “operational” polygons
- Exports:
  - `latest_positions.kml` (pins; red if inside area, yellow otherwise)
  - `tracks.kml` (lines per active buoy)
  - `latest_positions.csv` (one row per active buoy)
  - `all_points.csv` (cumulative archive, newest first)
  - Shapefiles (points, latest, lines) if GeoPandas/Fiona are installed
- Hides buoys whose last update is older than N days (default 7)
- Writes a list of currently inactive buoys (`inactive_buoys.csv`)

## Requirements

- Python 3.9+ (tested with Miniforge/Conda base on WSL Ubuntu)
- Required Python packages:
  - `pandas`
- Optional (for shapefiles and polygon tests):
  - `shapely` (for point-in-polygon checks)
  - `geopandas` + `fiona` + `pyproj` (for writing shapefiles)

> If you only need KML and CSV outputs, you can skip GeoPandas/Fiona.

## Input Data

- Buoy fixes files: `buoys*.csv` with header:

- `DATE` format: `%d/%m/%Y %H:%M:%S`
- `LATITUDE`,`LONGITUDE` are decimal degrees
- Area polygons (CSV): `deployment-area.csv` and `operational-area.csv`
- Columns: `lat`, `long` (or `latitude`, `longitude`)
- Values may be DMS (e.g., `3°45'33.1"S`, `10°29'37.3"E`). The script converts to decimal degrees.
- All polygon vertices must be in order; the script will auto-close the ring.

## Typical Folder Layout
```
repo-fad/
├─ build_fad_tracks.py
├─ deployment-area.csv
├─ operational-area.csv
├─ fads/
│ ├─ buoys-2025-11-01.csv
│ ├─ buoys-2025-11-02.csv
│ └─ ...
└─ fad_tracks_output/
  ├─ latest_positions.kml
  ├─ tracks.kml
  ├─ latest_positions.csv
  ├─ all_points.csv
  ├─ inactive_buoys.csv
└─ (optional shapefiles and zipped sets)
```