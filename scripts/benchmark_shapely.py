import csv
import gzip
import json
import time

import numpy as np
from shapely.geometry import shape
from shapely import points, contains

b = json.load(open("data/open_buildings_cache/cod_adm0_boundary.geojson", encoding="utf-8"))
geom = shape(b["features"][0]["geometry"])
minx, miny, maxx, maxy = geom.bounds

lons = []
lats = []
start = time.time()
with gzip.open("data/open_buildings_cache/tiles/10b_points.csv.gz", "rt", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    for idx, row in enumerate(reader, start=1):
        lon = float(row["longitude"])
        lat = float(row["latitude"])
        if minx <= lon <= maxx and miny <= lat <= maxy:
            lons.append(lon)
            lats.append(lat)
        if idx >= 200000:
            break
pts = points(np.array(lons), np.array(lats))
mask = contains(geom, pts)
print(len(lons), int(mask.sum()), round(time.time() - start, 2))
