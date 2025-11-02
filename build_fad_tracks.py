#!/usr/bin/env python3
"""
FAD Tracks Builder (annotated)

What this script does (quick overview):
- Reads one or more buoy CSV files (pattern default: buoys*.csv).
- Appends new records to a cumulative archive all_points.csv (dedup by buoy_id+timestamp).
- Optionally flags points that fall inside deployment/operational polygons (from CSVs).
- Hides buoys whose last update is older than --active-days (default 7 days) for map outputs.
- Writes:
  * latest_positions.kml (pins; red if in_area=True, yellow otherwise)
  * tracks.kml (lines per ACTIVE buoy)
  * latest_positions.csv (one row per ACTIVE buoy)
  * all_points.csv (cumulative archive, newest first)
  * inactive_buoys.csv (for visibility)
  * Shapefiles (points/latest/tracks for ACTIVE buoys) if GeoPandas/Fiona are installed.
"""

import argparse
import os
import sys
import glob
import zipfile
import re
import pandas as pd

# ------------------------------------------------------------------------------
# Helpers: XML escaping and DMS parsing
# ------------------------------------------------------------------------------


def kml_escape(text) -> str:
    """
    Escape special XML characters so values are safe inside KML tags.
    """
    return (str(text).replace('&', '&amp;')
                     .replace('<', '&lt;')
                     .replace('>', '&gt;'))


def dms_to_dd(dms: str) -> float:
    """
    Convert DMS strings like: 3°45'33.1"S  or  10°29'37.3"E  to signed decimal degrees.
    - Handles various quote characters (′ ″ ’ ”).
    - Hemisphere letters anywhere in the string (N/S/E/W).
    - If hemisphere not present, the sign of degrees is respected.
    """
    s = dms.strip()
    # Normalize glyphs that sometimes appear in spreadsheets/CSVs
    s = s.replace("’", "'").replace("”", '"').replace("″", '"').replace("′", "'")

    # Find hemisphere (N/S/E/W) if present
    hem_match = re.findall(r'[NnSsEeWw]', s)
    hem = hem_match[-1].upper() if hem_match else None

    # Remove hemisphere from the string so we can parse numbers
    s_numeric = re.sub(r'[NnSsEeWw]', '', s)

    # Try degrees-minutes-seconds first
    m = re.search(r'(-?\d+)\D+(\d+)\D+(\d+(?:\.\d+)?)', s_numeric)
    if m:
        deg = float(m.group(1))
        minutes = float(m.group(2))
        sec = float(m.group(3))
    else:
        # Fallback: degrees-minutes (no seconds)
        m2 = re.search(r'(-?\d+)\D+(\d+(?:\.\d+)?)', s_numeric)
        if not m2:
            raise ValueError(f"Unrecognized DMS format: {dms}")
        deg = float(m2.group(1))
        minutes = float(m2.group(2))
        sec = 0.0

    dd = abs(deg) + minutes / 60.0 + sec / 3600.0

    # Apply sign based on hemisphere or original degree sign
    if hem in ('S', 'W'):
        dd = -dd
    elif hem in ('N', 'E'):
        pass
    else:
        if deg < 0:
            dd = -dd

    return dd

# ------------------------------------------------------------------------------
# I/O helpers for polygons and buoy CSVs
# ------------------------------------------------------------------------------

def load_area_csv(path: str) -> pd.DataFrame:
    """
    Load one polygon CSV (deployment or operational).
    Columns expected (case-insensitive): lat/long or latitude/longitude.
    Values may be DMS; we convert to decimal degrees.
    Returns a DataFrame with columns: lon_dd, lat_dd (in order given).
    """
    df = pd.read_csv(path, encoding='latin-1')
    cols = {c.lower().strip(): c for c in df.columns}
    lat_col = cols.get('lat') or cols.get('latitude') or 'lat'
    lon_col = cols.get('long') or cols.get('lon') or cols.get('longitude') or 'long'
    out = pd.DataFrame({
        'lon_dd': df[lon_col].apply(dms_to_dd),
        'lat_dd': df[lat_col].apply(dms_to_dd),
    })
    return out


def load_buoy_file(path: str) -> pd.DataFrame:
    """
    Load one buoy CSV with header:
      NAME;DATE;LATITUDE;LONGITUDE;SPEED;COURSE;
    The trailing semicolon is tolerated by reading an extra column (dropped).
    Adds a __source_file column for traceability.
    """
    df = pd.read_csv(
        path,
        sep=';',
        engine='python',
        names=['NAME', 'DATE', 'LATITUDE', 'LONGITUDE', 'SPEED', 'COURSE', 'EXTRA'],
        header=0,
    ).drop(columns=['EXTRA'])
    df['__source_file'] = os.path.basename(path)
    return df


# ------------------------------------------------------------------------------
# KML writers
# ------------------------------------------------------------------------------

def write_kml_latest(latest: pd.DataFrame, out_path: str):
    """
    Write a KML with one pin per ACTIVE buoy (latest fix only).
    Red pin if in_area=True, yellow pin otherwise.
    """
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n')
        f.write('<name>Latest FAD Positions</name>\n')

        # Define two simple icon styles
        f.write('  <Style id="redPin"><IconStyle><scale>1.1</scale><Icon>'
                '<href>http://maps.google.com/mapfiles/kml/pushpin/red-pushpin.png</href>'
                '</Icon></IconStyle></Style>\n')
        f.write('  <Style id="defPin"><IconStyle><scale>1.0</scale><Icon>'
                '<href>http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png</href>'
                '</Icon></IconStyle></Style>\n')

        for _, row in latest.iterrows():
            style = "#redPin" if bool(row.get('in_area', False)) else "#defPin"
            f.write('<Placemark>\n')
            f.write(f'  <name>{kml_escape(row["buoy_id"])}</name>\n')
            f.write(f'  <styleUrl>{style}</styleUrl>\n')

            # Compose a small description popup
            desc = f"timestamp: {kml_escape(str(row['timestamp']))}\n"
            if 'speed_kn' in row and pd.notna(row['speed_kn']):
                desc += f"speed_kn: {row['speed_kn']}\n"
            if 'course_deg' in row and pd.notna(row['course_deg']):
                desc += f"course_deg: {row['course_deg']}\n"

            f.write(f'  <description>{kml_escape(desc)}</description>\n')
            f.write(f'  <Point><coordinates>{row["lon"]},{row["lat"]},0</coordinates></Point>\n')
            f.write('</Placemark>\n')

        f.write('</Document>\n</kml>\n')


def write_kml_tracks(df: pd.DataFrame, out_path: str):
    """
    Write a KML with line tracks per ACTIVE buoy (chronological order).
    """
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<kml xmlns="http://www.opengis.net/kml/2.2">\n<Document>\n')
        f.write('<name>FAD Tracks</name>\n')

        for buoy, grp in df.groupby('buoy_id'):
            grp = grp.sort_values('timestamp')  # lines should follow time
            if grp.shape[0] >= 2:
                f.write('<Placemark>\n')
                f.write(f'  <name>{kml_escape(buoy)}</name>\n')
                f.write('  <LineString>\n    <tessellate>1</tessellate>\n    <coordinates>\n')
                for _, r in grp.iterrows():
                    f.write(f'      {r["lon"]},{r["lat"]},0\n')
                f.write('    </coordinates>\n  </LineString>\n')
                f.write('</Placemark>\n')

        f.write('</Document>\n</kml>\n')

# ------------------------------------------------------------------------------
# Shapefile helper (zipper)
# ------------------------------------------------------------------------------

def zip_shapefile(basepath: str, zipname: str, out_dir: str) -> str:
    """
    Create a .zip containing the .shp/.shx/.dbf/.prj/.cpg that form a shapefile.
    Returns the path to the created zip.
    """
    base = os.path.splitext(basepath)[0]
    exts = ['.shp', '.shx', '.dbf', '.prj', '.cpg']
    files = [base + e for e in exts if os.path.exists(base + e)]
    zpath = os.path.join(out_dir, zipname)
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=os.path.basename(p))
    return zpath


# ------------------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------------------

def main():
    # ---- Command-line arguments (tweak defaults here if you like)
    ap = argparse.ArgumentParser(
        description="Build KML/CSV/Shapefiles from buoy fixes; keep cumulative archive."
    )
    ap.add_argument('--in-dir', default='.', help='Folder with FAD CSVs (default: current dir)')
    ap.add_argument('--glob', default='buoys*.csv', help='CSV filename pattern (default: buoys*.csv)')
    ap.add_argument('--out-dir', default='fad_tracks_output', help='Output folder (default: fad_tracks_output)')
    ap.add_argument('--deployment-csv', default='deployment-area.csv', help='Deployment polygon CSV')
    ap.add_argument('--operational-csv', default='operational-area.csv', help='Operational polygon CSV')
    ap.add_argument('--active-days', type=int, default=7,
                    help='Hide buoys whose last update is older than this many days (default: 7)')
    args = ap.parse_args()

    in_dir = args.in_dir
    out_dir = args.out_dir
    pattern = os.path.join(in_dir, args.glob)

    # ---- Find all matching buoy CSV files
    files = sorted(glob.glob(pattern))
    if not files:
        print(f'No FAD CSV files match: {pattern}', file=sys.stderr)
        sys.exit(2)

    # ---- Load deployment/operational polygons (optional)
    deployment_poly = operational_poly = None
    try:
        from shapely.geometry import Polygon
        dep_df = load_area_csv(os.path.join(in_dir, args.deployment_csv))
        op_df = load_area_csv(os.path.join(in_dir, args.operational_csv))

        dep_coords = list(zip(dep_df['lon_dd'], dep_df['lat_dd']))
        if dep_coords[0] != dep_coords[-1]:
            dep_coords.append(dep_coords[0])  # close ring if needed

        op_coords = list(zip(op_df['lon_dd'], op_df['lat_dd']))
        if op_coords[0] != op_coords[-1]:
            op_coords.append(op_coords[0])

        deployment_poly = Polygon(dep_coords)
        operational_poly = Polygon(op_coords)
    except Exception as e:
        # If shapely is missing or CSV headers are wrong, we continue without in-area flags.
        print(f'[info] Could not read/build area polygons: {e}', file=sys.stderr)

    # ---- Load fresh buoy fixes for this run and normalize columns
    dfs = [load_buoy_file(f) for f in files]
    df = pd.concat(dfs, ignore_index=True).rename(columns={
        'NAME': 'buoy_id',
        'DATE': 'timestamp',
        'LATITUDE': 'lat',
        'LONGITUDE': 'lon',
        'SPEED': 'speed_kn',
        'COURSE': 'course_deg',
    })

    # Type conversions: timestamps + numeric values
    df['timestamp'] = pd.to_datetime(df['timestamp'], format='%d/%m/%Y %H:%M:%S', errors='coerce')
    for col in ['lat', 'lon', 'speed_kn', 'course_deg']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Drop rows missing essential fields
    df = df.dropna(subset=['lat', 'lon', 'timestamp', 'buoy_id']).copy()

    # ---- Prepare this run's batch for appending to the cumulative archive
    df_current = df[['buoy_id', 'timestamp', 'lat', 'lon', 'speed_kn', 'course_deg']].copy()

    # Ensure output directory exists
    os.makedirs(out_dir, exist_ok=True)
    all_points_path = os.path.join(out_dir, 'all_points.csv')

    # ---- Append to (or create) the cumulative archive; dedup by (buoy_id, timestamp)
    if os.path.exists(all_points_path):
        try:
            df_existing = pd.read_csv(all_points_path, parse_dates=['timestamp'])
            combined = pd.concat([df_existing, df_current], ignore_index=True)
            combined = combined.drop_duplicates(subset=['buoy_id', 'timestamp'], keep='last')
            df_all = combined
            print(f"Appended to existing all_points.csv (now {len(df_all)} records).")
        except Exception as e:
            print(f"[warning] Could not read existing all_points.csv: {e}")
            df_all = df_current
    else:
        df_all = df_current
        print("Created new all_points.csv")

    # Keep newest first in the archive (handy for quick inspection)
    df_all = df_all.sort_values('timestamp', ascending=False).reset_index(drop=True)

    # ---- Compute in_area flag for the whole archive (if polygons available)
    df_all['in_area'] = False
    if (deployment_poly is not None) and (operational_poly is not None):
        try:
            from shapely.geometry import Point

            def inside_any(lon, lat):
                p = Point(lon, lat)
                return (deployment_poly.contains(p) or deployment_poly.touches(p) or
                        operational_poly.contains(p) or operational_poly.touches(p))

            df_all['in_area'] = df_all.apply(lambda r: inside_any(r['lon'], r['lat']), axis=1)
        except Exception as e:
            print(f"[info] in_area not computed (shapely issue): {e}", file=sys.stderr)

    # ---- Determine "ACTIVE" buoys by last_seen timestamp within N days
    now = pd.Timestamp.now(tz=None)
    cutoff = now - pd.Timedelta(days=args.active_days)
    last_seen = (df_all.groupby('buoy_id', as_index=False)['timestamp']
                 .max()
                 .rename(columns={'timestamp': 'last_seen'}))
    active_ids = set(last_seen[last_seen['last_seen'] >= cutoff]['buoy_id'])
    inactive = last_seen[~last_seen['buoy_id'].isin(active_ids)].sort_values('last_seen')

    # Save inactive list so you can inspect who fell off the grid
    inactive_path = os.path.join(out_dir, 'inactive_buoys.csv')
    inactive.to_csv(inactive_path, index=False)

    # Subset of archive containing only ACTIVE buoy rows
    df_active = df_all[df_all['buoy_id'].isin(active_ids)].copy()

    # ---- Latest row per ACTIVE buoy (used for pins and latest_positions.csv)
    latest = (df_active.sort_values('timestamp', ascending=False)
                        .drop_duplicates(subset=['buoy_id'], keep='first')
                        .sort_values('buoy_id'))

    # ---- Write cumulative archive (always)
    df_all[['buoy_id', 'timestamp', 'lat', 'lon', 'speed_kn', 'course_deg', 'in_area']].to_csv(
        all_points_path, index=False
    )
    print(f"All points CSV written (total {len(df_all)} records, newest first). Active buoys: {len(active_ids)}")

    # ---- Write KMLs and latest CSV from ACTIVE set
    write_kml_latest(latest, os.path.join(out_dir, 'latest_positions.kml'))
    write_kml_tracks(df_active.sort_values(['buoy_id', 'timestamp']), os.path.join(out_dir, 'tracks.kml'))
    latest[['buoy_id', 'timestamp', 'lat', 'lon', 'speed_kn', 'course_deg', 'in_area']].to_csv(
        os.path.join(out_dir, 'latest_positions.csv'), index=False
    )
    print('KMLs + latest CSV (active buoys) written.')

    # ---- Optional shapefiles (ACTIVE set) if GeoPandas/Fiona are available
    try:
        import geopandas as gpd
        from shapely.geometry import Point, LineString

        # IMPORTANT: build geometry from the SAME (unsorted) DataFrame to keep rows aligned
        df_pts = df_active.copy()
        gdf_points = gpd.GeoDataFrame(
            df_pts,
            geometry=[Point(xy) for xy in zip(df_pts['lon'], df_pts['lat'])],
            crs='EPSG:4326'
        )

        latest_gdf = gpd.GeoDataFrame(
            latest,
            geometry=[Point(xy) for xy in zip(latest['lon'], latest['lat'])],
            crs='EPSG:4326'
        )

        # Build per-buoy LineStrings in chronological order
        lines = []
        for buoy, grp in df_active.groupby('buoy_id', sort=True):
            grp = grp.sort_values('timestamp')
            if grp.shape[0] >= 2:
                lines.append({
                    'buoy_id': buoy,
                    'start_time': grp['timestamp'].min(),
                    'end_time': grp['timestamp'].max(),
                    'n_points': grp.shape[0],
                    'geometry': LineString(list(zip(grp['lon'], grp['lat']))),
                })
        gdf_lines = gpd.GeoDataFrame(lines, crs='EPSG:4326')

        def to_shp_df(gdf):
            """
            Prepare a GeoDataFrame for ESRI Shapefile:
            - Cast datetime columns to string.
            - Trim column names to 10 chars (Shapefile limit) except 'geometry'.
            """
            gdf2 = gdf.copy()
            for c in ['timestamp', 'start_time', 'end_time']:
                if c in gdf2.columns:
                    gdf2[c] = gdf2[c].astype(str)
            rename_map = {c: c[:10] for c in gdf2.columns if c != 'geometry' and len(c) > 10}
            if rename_map:
                gdf2 = gdf2.rename(columns=rename_map)
            return gdf2

        # Output filenames
        out_points = os.path.join(out_dir, 'all_points.shp')   # ACTIVE points
        out_latest = os.path.join(out_dir, 'latest_pos.shp')   # ACTIVE latest
        out_lines = os.path.join(out_dir, 'tracks.shp')        # ACTIVE tracks

        # Write shapefiles
        to_shp_df(gdf_points).to_file(out_points, driver='ESRI Shapefile', encoding='UTF-8')
        to_shp_df(latest_gdf).to_file(out_latest, driver='ESRI Shapefile', encoding='UTF-8')
        to_shp_df(gdf_lines).to_file(out_lines, driver='ESRI Shapefile', encoding='UTF-8')

        # Also zip them for convenience
        zip_shapefile(out_points, 'all_points_shapefile.zip', out_dir)
        zip_shapefile(out_latest, 'latest_pos_shapefile.zip', out_dir)
        zip_shapefile(out_lines, 'tracks_shapefile.zip', out_dir)

        print('Shapefiles written (and zipped) for ACTIVE buoys.')
    except Exception as e:
        print(f'[info] Shapefiles skipped (geopandas path failed): {e}', file=sys.stderr)

    print('Done. Outputs in:', out_dir)
    print(f'Inactive buoys list: {inactive_path}')


if __name__ == '__main__':
    main()