# Toit Analyzer RDC

Toit Analyzer RDC is a QGIS plugin for roof counting and rural electrification planning in the Democratic Republic of the Congo.

## Features

- Draw or select an analysis zone in QGIS.
- Automatically detect an inhabited-zone polygon around a center coordinate.
- Import multiple named zones from Excel or CSV and run automatic analysis in batch.
- Load administrative boundary GeoJSON files and count roofs per polygon.
- Estimate electricity demand from counted households using residential consumption profiles and a community/productive demand margin.
- Load Google Open Buildings roof points for the selected zone.
- Use a local national Open Buildings GeoPackage when available.
- Count buildings inside the zone and estimate households and population.
- Manually correct roof points and recalculate statistics.
- Export analysis layers to GeoPackage and statistics to CSV.

The manual drawing workflow remains available. Automatic detection creates an editable polygon proposal from nearby building density, so users can adjust the boundary before counting.

Batch files should include columns equivalent to `nom_zone`, `latitude`, and `longitude`. An optional `province` column can be included and will be carried into demand exports. The batch workflow uses the local RDC Open Buildings GeoPackage so large lists of points can be processed without repeatedly downloading online tiles.

Administrative boundary analysis works with polygon GeoJSON files such as COD admin1, admin2, or admin3 boundaries. It uses the local RDC Open Buildings GeoPackage and creates a result layer with roof counts, density, household estimates, and population estimates for each polygon.

Demand estimation can use admin, batch, or single-zone results. Users define connection rate, residential consumption profile shares, kWh/day per profile, community/productive margin, load factors, growth, and projection horizon. The plugin reports daily energy, annual energy, and peak demand indicators.

## Open Buildings RDC data

The full RDC Open Buildings GeoPackage is several GB and is not included in the QGIS plugin package. This keeps the public plugin zip compatible with the QGIS plugin repository size limit.

Maintainers can configure an authorized direct download link in `toit_analyzer_rdc.py`:

```python
OPEN_BUILDINGS_DOWNLOAD_URL = "https://..."
```

or a Google Drive file id:

```python
OPEN_BUILDINGS_DRIVE_FILE_ID = "..."
```

The link must allow direct download of the `.gpkg` file. If the local file is missing, the plugin asks the user before downloading it to the QGIS profile data folder. If no authorized download link is configured, the plugin falls back to Google Open Buildings online tiles for the selected zone.

## License

GPL-2.0-or-later.
