import csv
import gzip
import json
import time

from osgeo import ogr

b = json.load(open("data/open_buildings_cache/cod_adm0_boundary.geojson", encoding="utf-8"))
geom = ogr.CreateGeometryFromJson(json.dumps(b["features"][0]["geometry"]))
env = geom.GetEnvelope()
point = ogr.Geometry(ogr.wkbPoint)

start = time.time()
read = 0
kept = 0
with gzip.open("data/open_buildings_cache/tiles/10b_points.csv.gz", "rt", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    for row in reader:
        read += 1
        lon = float(row["longitude"])
        lat = float(row["latitude"])
        if env[0] <= lon <= env[1] and env[2] <= lat <= env[3]:
            point.Empty()
            point.AddPoint(lon, lat)
            if geom.Contains(point):
                kept += 1
        if read >= 200000:
            break

print(read, kept, round(time.time() - start, 2))
