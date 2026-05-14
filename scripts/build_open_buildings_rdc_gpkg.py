#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Build a local GeoPackage of Google Open Buildings points for DR Congo.

The script uses only Python's standard library so it can run on machines where
GDAL/GeoPandas are not available. It writes a valid point GeoPackage with an
RTree spatial index that QGIS can read directly.
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sqlite3
import struct
import sys
import time
import urllib.request
import urllib.error


GB_API_URL = "https://www.geoboundaries.org/api/current/gbOpen/COD/ADM0/"
OPEN_BUILDINGS_TILES_URL = (
    "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
)


def log(message):
    print(time.strftime("[%H:%M:%S]"), message, flush=True)


def download_json(url, path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    log("Downloading {}".format(url))
    request = urllib.request.Request(url, headers={"User-Agent": "ToitAnalyzerRDC/0.2"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return data


def remote_size(url):
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "ToitAnalyzerRDC/0.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            value = response.headers.get("Content-Length")
            return int(value) if value else None
    except Exception:
        return None


def download_tile(url, path, retries=4):
    expected_size = remote_size(url)
    if os.path.exists(path):
        local_size = os.path.getsize(path)
        if expected_size is None or local_size == expected_size:
            return path
        log(
            "  cached tile has wrong size: {} bytes, expected {}; redownloading".format(
                local_size, expected_size
            )
        )
        os.remove(path)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    part_path = path + ".part"

    for attempt in range(1, retries + 1):
        if os.path.exists(part_path):
            os.remove(part_path)
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "ToitAnalyzerRDC/0.2"},
            )
            log("  downloading tile attempt {}/{}".format(attempt, retries))
            downloaded = 0
            next_report = 50 * 1024 * 1024
            with urllib.request.urlopen(request, timeout=300) as response:
                with open(part_path, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_report:
                            if expected_size:
                                pct = downloaded * 100.0 / expected_size
                                log("    downloaded {:.1f}%".format(pct))
                            else:
                                log("    downloaded {:.1f} MB".format(downloaded / 1048576.0))
                            next_report += 50 * 1024 * 1024

            final_size = os.path.getsize(part_path)
            if expected_size is not None and final_size != expected_size:
                raise IOError(
                    "incomplete tile: {} bytes, expected {}".format(
                        final_size, expected_size
                    )
                )
            os.replace(part_path, path)
            return path
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            log("  download failed: {}".format(exc))
            if attempt == retries:
                raise
            time.sleep(5 * attempt)

    return path


def boundary_geojson_url(metadata):
    for key in ("gjDownloadURL", "simplifiedGeometryGeoJSON", "downloadURL"):
        value = metadata.get(key)
        if value and value.lower().endswith((".geojson", ".json")):
            return value
    if metadata.get("gjDownloadURL"):
        return metadata["gjDownloadURL"]
    raise RuntimeError("Could not find a GeoJSON boundary URL in geoBoundaries metadata.")


def iter_positions(coords):
    if (
        isinstance(coords, list)
        and len(coords) >= 2
        and isinstance(coords[0], (int, float))
        and isinstance(coords[1], (int, float))
    ):
        yield float(coords[0]), float(coords[1])
        return
    if isinstance(coords, list):
        for item in coords:
            yield from iter_positions(item)


def bbox_from_geojson_geometry(geometry):
    positions = list(iter_positions(geometry.get("coordinates", [])))
    xs = [item[0] for item in positions]
    ys = [item[1] for item in positions]
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_intersect(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def normalize_polygons(geometry):
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if geom_type == "Polygon":
        return [coords]
    if geom_type == "MultiPolygon":
        return coords
    raise RuntimeError("Unsupported boundary geometry type: {}".format(geom_type))


def point_in_ring(x, y, ring):
    inside = False
    if len(ring) < 4:
        return False

    x1, y1 = ring[0]
    for i in range(1, len(ring)):
        x2, y2 = ring[i]
        crosses = (y1 > y) != (y2 > y)
        if crosses:
            x_intersect = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-20) + x1
            if x < x_intersect:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_boundary(x, y, polygons):
    for polygon in polygons:
        if not polygon:
            continue
        outer = polygon[0]
        if not point_in_ring(x, y, outer):
            continue
        in_hole = any(point_in_ring(x, y, hole) for hole in polygon[1:])
        if not in_hole:
            return True
    return False


def polygon_bboxes(polygons):
    indexed = []
    for polygon in polygons:
        if not polygon:
            continue
        outer = polygon[0]
        xs = [point[0] for point in outer]
        ys = [point[1] for point in outer]
        indexed.append((min(xs), min(ys), max(xs), max(ys), polygon))
    return indexed


def build_ring_index(ring, bin_size):
    if len(ring) < 4:
        return {}

    bins = {}
    for idx in range(1, len(ring)):
        x1, y1 = ring[idx - 1]
        x2, y2 = ring[idx]
        if y1 == y2:
            continue
        min_bin = math.floor(min(y1, y2) / bin_size)
        max_bin = math.floor(max(y1, y2) / bin_size)
        segment = (x1, y1, x2, y2)
        for bin_id in range(min_bin, max_bin + 1):
            bins.setdefault(bin_id, []).append(segment)
    return bins


def build_exact_boundary_index(polygons, bin_size=0.02):
    indexed = []
    for polygon in polygons:
        if not polygon:
            continue
        outer = polygon[0]
        xs = [point[0] for point in outer]
        ys = [point[1] for point in outer]
        holes = polygon[1:]
        indexed.append(
            {
                "bbox": (min(xs), min(ys), max(xs), max(ys)),
                "outer": build_ring_index(outer, bin_size),
                "holes": [build_ring_index(hole, bin_size) for hole in holes],
            }
        )
    return indexed, bin_size


def point_in_indexed_ring(x, y, ring_index, bin_size):
    inside = False
    for x1, y1, x2, y2 in ring_index.get(math.floor(y / bin_size), []):
        if (y1 > y) == (y2 > y):
            continue
        x_intersect = (x2 - x1) * (y - y1) / (y2 - y1) + x1
        if x < x_intersect:
            inside = not inside
    return inside


def point_in_exact_boundary(x, y, indexed_boundary, bin_size):
    for item in indexed_boundary:
        xmin, ymin, xmax, ymax = item["bbox"]
        if x < xmin or x > xmax or y < ymin or y > ymax:
            continue
        if not point_in_indexed_ring(x, y, item["outer"], bin_size):
            continue
        in_hole = any(
            point_in_indexed_ring(x, y, hole_index, bin_size)
            for hole_index in item["holes"]
        )
        if not in_hole:
            return True
    return False


def gpkg_point_blob(x, y):
    gpkg_header = struct.pack("<2sBBi", b"GP", 0, 1, 4326)
    wkb = struct.pack("<BIdd", 1, 1, x, y)
    return gpkg_header + wkb


def create_geopackage(path):
    if os.path.exists(path):
        os.remove(path)

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("PRAGMA application_id = 1196437808")
    cur.execute("PRAGMA user_version = 10300")
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")

    cur.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (
          srs_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL PRIMARY KEY,
          organization TEXT NOT NULL,
          organization_coordsys_id INTEGER NOT NULL,
          definition TEXT NOT NULL,
          description TEXT
        );
        INSERT INTO gpkg_spatial_ref_sys VALUES
          ('Undefined Cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined'),
          ('Undefined Geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined'),
          ('WGS 84 geodetic', 4326, 'EPSG', 4326,
           'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
           'longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid');

        CREATE TABLE gpkg_contents (
          table_name TEXT NOT NULL PRIMARY KEY,
          data_type TEXT NOT NULL,
          identifier TEXT UNIQUE,
          description TEXT DEFAULT '',
          last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          min_x DOUBLE,
          min_y DOUBLE,
          max_x DOUBLE,
          max_y DOUBLE,
          srs_id INTEGER,
          CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
            REFERENCES gpkg_spatial_ref_sys(srs_id)
        );

        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT NOT NULL,
          column_name TEXT NOT NULL,
          geometry_type_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL,
          z TINYINT NOT NULL,
          m TINYINT NOT NULL,
          PRIMARY KEY (table_name, column_name)
        );

        CREATE TABLE gpkg_extensions (
          table_name TEXT,
          column_name TEXT,
          extension_name TEXT NOT NULL,
          definition TEXT NOT NULL,
          scope TEXT NOT NULL,
          CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name)
        );

        CREATE TABLE open_buildings_rdc (
          fid INTEGER PRIMARY KEY AUTOINCREMENT,
          openb_id TEXT,
          confidence REAL,
          area_m2 REAL,
          plus_code TEXT,
          tile_id TEXT,
          geom BLOB NOT NULL
        );

        CREATE TABLE processed_tiles (
          tile_id TEXT PRIMARY KEY,
          row_count INTEGER NOT NULL,
          processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );

        CREATE VIRTUAL TABLE rtree_open_buildings_rdc_geom
          USING rtree(id, minx, maxx, miny, maxy);

        INSERT INTO gpkg_contents
          (table_name, data_type, identifier, description, srs_id)
        VALUES
          ('open_buildings_rdc', 'features', 'open_buildings_rdc',
           'Google Open Buildings V3 points filtered to DR Congo', 4326);

        INSERT INTO gpkg_geometry_columns
          (table_name, column_name, geometry_type_name, srs_id, z, m)
        VALUES ('open_buildings_rdc', 'geom', 'POINT', 4326, 0, 0);

        INSERT INTO gpkg_extensions
          (table_name, column_name, extension_name, definition, scope)
        VALUES
          ('open_buildings_rdc', 'geom', 'gpkg_rtree_index',
           'http://www.geopackage.org/spec/#extension_rtree', 'write-only');
        """
    )
    conn.commit()
    return conn


def open_or_create_geopackage(path, resume):
    if resume and os.path.exists(path):
        conn = sqlite3.connect(path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "open_buildings_rdc" in tables and "processed_tiles" not in tables:
            conn.execute(
                """
                CREATE TABLE processed_tiles (
                  tile_id TEXT PRIMARY KEY,
                  row_count INTEGER NOT NULL,
                  processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
                """
            )
            conn.commit()
            tables.add("processed_tiles")
        if "open_buildings_rdc" in tables and "processed_tiles" in tables:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn
        conn.close()
    return create_geopackage(path)


def processed_tile_ids(conn):
    try:
        return {
            row[0]
            for row in conn.execute("SELECT tile_id FROM processed_tiles").fetchall()
        }
    except sqlite3.Error:
        return set()


def mark_tile_processed(conn, tile_id, row_count):
    conn.execute(
        "INSERT OR REPLACE INTO processed_tiles (tile_id, row_count) VALUES (?, ?)",
        (tile_id, row_count),
    )
    conn.commit()


def delete_tile_rows(conn, tile_id):
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM rtree_open_buildings_rdc_geom
        WHERE id IN (SELECT fid FROM open_buildings_rdc WHERE tile_id=?)
        """,
        (tile_id,),
    )
    cur.execute("DELETE FROM open_buildings_rdc WHERE tile_id=?", (tile_id,))
    cur.execute("DELETE FROM processed_tiles WHERE tile_id=?", (tile_id,))
    conn.commit()


def deduplicate_by_openb_id(conn):
    log("Checking duplicate building ids")
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TEMP TABLE duplicate_fids AS
        SELECT fid
        FROM open_buildings_rdc
        WHERE fid NOT IN (
          SELECT min(fid)
          FROM open_buildings_rdc
          GROUP BY openb_id
        )
        """
    )
    count = cur.execute("SELECT count(*) FROM duplicate_fids").fetchone()[0]
    if count:
        log("Removing {:,} duplicate rows".format(count))
        cur.execute(
            "DELETE FROM rtree_open_buildings_rdc_geom "
            "WHERE id IN (SELECT fid FROM duplicate_fids)"
        )
        cur.execute(
            "DELETE FROM open_buildings_rdc "
            "WHERE fid IN (SELECT fid FROM duplicate_fids)"
        )
        conn.commit()
    cur.execute("DROP TABLE duplicate_fids")
    return count


def update_extent(conn, extent):
    if extent is None:
        return
    conn.execute(
        """
        UPDATE gpkg_contents
        SET min_x=?, min_y=?, max_x=?, max_y=?,
            last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE table_name='open_buildings_rdc'
        """,
        (extent[0], extent[1], extent[2], extent[3]),
    )
    conn.commit()


def insert_batch(conn, rows):
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO open_buildings_rdc
          (openb_id, confidence, area_m2, plus_code, tile_id, geom)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    last_id = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
    first_id = last_id - len(rows) + 1
    rtree_rows = []
    for index, row in enumerate(rows):
        geom = row[5]
        x, y = struct.unpack("<dd", geom[-16:])
        fid = first_id + index
        rtree_rows.append((fid, x, x, y, y))
    cur.executemany(
        "INSERT INTO rtree_open_buildings_rdc_geom VALUES (?, ?, ?, ?, ?)",
        rtree_rows,
    )
    conn.commit()


def build(args):
    os.makedirs(args.cache_dir, exist_ok=True)
    metadata = download_json(GB_API_URL, os.path.join(args.cache_dir, "cod_adm0_meta.json"))
    boundary = download_json(
        boundary_geojson_url(metadata),
        os.path.join(args.cache_dir, "cod_adm0_boundary.geojson"),
    )
    tiles_geojson = download_json(
        OPEN_BUILDINGS_TILES_URL,
        os.path.join(args.cache_dir, "open_buildings_tiles.geojson"),
    )

    boundary_geometry = boundary["features"][0]["geometry"]
    boundary_bbox = bbox_from_geojson_geometry(boundary_geometry)
    boundary_polygons = normalize_polygons(boundary_geometry)
    indexed_boundary, boundary_bin_size = build_exact_boundary_index(
        boundary_polygons, args.boundary_bin_size
    )
    log(
        "Using exact RDC boundary: {} polygon parts, bin size {} deg".format(
            len(indexed_boundary), boundary_bin_size
        )
    )

    tiles = []
    for feature in tiles_geojson.get("features", []):
        tile_bbox = bbox_from_geojson_geometry(feature["geometry"])
        if not bboxes_intersect(boundary_bbox, tile_bbox):
            continue
        props = feature.get("properties", {})
        polygon_url = props.get("tile_url")
        if not polygon_url:
            continue
        tiles.append(
            {
                "tile_id": props.get("tile_id") or os.path.basename(polygon_url),
                "tile_url": polygon_url.replace(
                    "/polygons_s2_level_4_gzip/",
                    "/points_s2_level_4_gzip/",
                ),
                "size_mb": float(props.get("size_mb") or 0.0) / 3.5,
            }
        )

    log("Tiles intersecting RDC bbox: {}".format(len(tiles)))
    log("Approx compressed points size: {:.1f} MB".format(sum(t["size_mb"] for t in tiles)))
    if args.estimate_only:
        return

    conn = open_or_create_geopackage(args.output, args.resume)
    done_tiles = processed_tile_ids(conn)
    total_rows = conn.execute("SELECT count(*) FROM open_buildings_rdc").fetchone()[0]
    extent = None
    if total_rows:
        row = conn.execute(
            "SELECT min(minx), min(miny), max(maxx), max(maxy) "
            "FROM rtree_open_buildings_rdc_geom"
        ).fetchone()
        if row and row[0] is not None:
            extent = [row[0], row[1], row[2], row[3]]
    if done_tiles:
        log("Resuming; completed tiles: {}".format(", ".join(sorted(done_tiles))))
        log("Existing rows: {:,}".format(total_rows))
        deduplicate_by_openb_id(conn)
        total_rows = conn.execute("SELECT count(*) FROM open_buildings_rdc").fetchone()[0]

    for tile_number, tile in enumerate(tiles, start=1):
        if tile["tile_id"] in done_tiles:
            log(
                "Tile {}/{} {} already processed; skipping".format(
                    tile_number, len(tiles), tile["tile_id"]
                )
            )
            continue

        delete_tile_rows(conn, tile["tile_id"])
        log(
            "Tile {}/{} {} (~{:.1f} MB points)".format(
                tile_number, len(tiles), tile["tile_id"], tile["size_mb"]
            )
        )
        tile_path = os.path.join(
            args.cache_dir,
            "tiles",
            "{}_points.csv.gz".format(tile["tile_id"]),
        )
        if os.path.exists(tile_path):
            log("  using cached tile {}".format(os.path.basename(tile_path)))
        else:
            download_tile(tile["tile_url"], tile_path, retries=args.retries)
        batch = []
        seen_in_tile = 0
        kept_in_tile = 0

        try:
            source = open(tile_path, "rb")
            gzip_file = gzip.GzipFile(fileobj=source)
            text_file = io.TextIOWrapper(gzip_file, encoding="utf-8")
            reader = csv.DictReader(text_file)
            for row_index, row in enumerate(reader, start=1):
                seen_in_tile += 1
                try:
                    lat = float(row["latitude"])
                    lon = float(row["longitude"])
                    confidence = float(row["confidence"])
                    area_m2 = float(row["area_in_meters"])
                except Exception:
                    continue

                if confidence < args.min_confidence:
                    continue
                if not (
                    boundary_bbox[0] <= lon <= boundary_bbox[2]
                    and boundary_bbox[1] <= lat <= boundary_bbox[3]
                ):
                    continue
                if not point_in_exact_boundary(
                    lon, lat, indexed_boundary, boundary_bin_size
                ):
                    continue

                openb_id = "{}_{}".format(tile["tile_id"], row_index)
                batch.append(
                    (
                        openb_id,
                        confidence,
                        area_m2,
                        row.get("full_plus_code", ""),
                        tile["tile_id"],
                        gpkg_point_blob(lon, lat),
                    )
                )
                kept_in_tile += 1
                total_rows += 1

                if extent is None:
                    extent = [lon, lat, lon, lat]
                else:
                    extent[0] = min(extent[0], lon)
                    extent[1] = min(extent[1], lat)
                    extent[2] = max(extent[2], lon)
                    extent[3] = max(extent[3], lat)

                if len(batch) >= args.batch_size:
                    insert_batch(conn, batch)
                    batch = []

                if row_index % 500000 == 0:
                    log("  read {:,} rows, kept {:,}".format(row_index, kept_in_tile))
        except EOFError:
            source.close()
            os.remove(tile_path)
            raise RuntimeError(
                "Cached tile {} is incomplete. It was deleted; rerun the script.".format(
                    tile["tile_id"]
                )
            )
        finally:
            try:
                source.close()
            except Exception:
                pass

        if batch:
            insert_batch(conn, batch)

        mark_tile_processed(conn, tile["tile_id"], kept_in_tile)
        log(
            "  finished tile: read {:,}, kept {:,}, total {:,}".format(
                seen_in_tile, kept_in_tile, total_rows
            )
        )

    update_extent(conn, extent)
    conn.execute("VACUUM")
    conn.close()
    log("Done: {} ({:,} buildings)".format(args.output, total_rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data", "open_buildings_rdc_points.gpkg")
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data", "open_buildings_cache")
        ),
    )
    parser.add_argument("--min-confidence", type=float, default=0.65)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--boundary-bin-size", type=float, default=0.02)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.add_argument("--estimate-only", action="store_true")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    build(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(130)
