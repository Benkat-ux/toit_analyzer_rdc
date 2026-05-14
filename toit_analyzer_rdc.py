# -*- coding: utf-8 -*-
"""
Toit Analyzer RDC

Plugin QGIS d'analyse rapide des toitures pour la planification energetique.
"""

import csv
import gzip
import io
import json
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape

from qgis.PyQt.QtCore import QCoreApplication, QSettings, QTranslator, QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsApplication,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapToolEmitPoint

from .resources import *
from .toit_analyzer_rdc_dialog import ToitAnalyzerRDCDialog


ZONE_LAYER_NAME = "TA_RDC_zone_analyse"
POINT_LAYER_NAME = "TA_RDC_point_centre"
BUILDINGS_LAYER_NAME = "TA_RDC_toits"
COUNTED_LAYER_NAME = "TA_RDC_toits_comptes"
BATCH_POINTS_LAYER_NAME = "TA_RDC_points_batch"
BATCH_ZONES_LAYER_NAME = "TA_RDC_zones_batch"
BATCH_BUILDINGS_LAYER_NAME = "TA_RDC_toits_batch"
ADMIN_BOUNDARIES_LAYER_NAME = "TA_RDC_limites_admin"
ADMIN_RESULTS_LAYER_NAME = "TA_RDC_admin_resultats"
ADMIN_BUILDINGS_LAYER_NAME = "TA_RDC_admin_toits"
OPEN_BUILDINGS_TILES_URL = (
    "https://openbuildings-public-dot-gweb-research.uw.r.appspot.com/public/tiles.geojson"
)
OPEN_BUILDINGS_DRIVE_FILE_ID = ""
OPEN_BUILDINGS_DOWNLOAD_URL = ""
BUNDLED_OPEN_BUILDINGS_GPKG = os.path.join(
    os.path.dirname(__file__), "data", "open_buildings_rdc_points_exact.gpkg"
)
USER_DATA_DIR = os.path.join(
    QgsApplication.qgisSettingsDirPath(), "toit_analyzer_rdc"
)
USER_OPEN_BUILDINGS_GPKG = os.path.join(
    USER_DATA_DIR, "open_buildings_rdc_points_exact.gpkg"
)
LOCAL_OPEN_BUILDINGS_LAYER = "open_buildings_rdc"


class ToitAnalyzerRDC:
    """Implementation du plugin QGIS."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        locale = QSettings().value("locale/userLocale", "fr")[0:2]
        locale_path = os.path.join(
            self.plugin_dir, "i18n", "ToitAnalyzerRDC_{}.qm".format(locale)
        )

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr("&Toit Analyzer RDC")
        self.first_start = True
        self.dlg = None
        self.zone_layer = None
        self.buildings_layer = None
        self.counted_layer = None
        self.last_stats = {}
        self.point_tool = None
        self.open_buildings_task = None
        self.open_buildings_download_task = None
        self.pending_open_buildings_zone_wkt = None
        self.pending_auto_zone_params = None
        self.batch_rows = []
        self.batch_results = []
        self.batch_points_layer = None
        self.batch_zones_layer = None
        self.batch_buildings_layer = None
        self.admin_layer = None
        self.admin_results_layer = None
        self.admin_buildings_layer = None
        self.admin_results = []
        self.auto_count_after_load = False

    def tr(self, message):
        return QCoreApplication.translate("ToitAnalyzerRDC", message)

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        action = QAction(QIcon(icon_path), text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = ":/plugins/toit_analyzer_rdc/icon.png"
        self.add_action(
            icon_path,
            text=self.tr("Toit Analyzer RDC"),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    def run(self):
        if self.first_start:
            self.first_start = False
            self.dlg = ToitAnalyzerRDCDialog()
            self.dlg.setModal(False)
            self._connect_dialog()
            self._set_default_values()

        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def _connect_dialog(self):
        self.dlg.btn_pointer_coordonnees.clicked.connect(self.pointer_coordonnees)
        self.dlg.btn_pick_point.clicked.connect(self.activer_selection_point)
        self.dlg.btn_creer_zone.clicked.connect(self.creer_zone)
        if hasattr(self.dlg, "btn_detecter_zone"):
            self.dlg.btn_detecter_zone.clicked.connect(self.detecter_zone_autour_point)
        self.dlg.btn_analyser.clicked.connect(self.analyser_zone)
        self.dlg.btn_editer.clicked.connect(self.activer_edition_toits)
        self.dlg.btn_stop_editer.clicked.connect(self.arreter_edition_toits)
        self.dlg.btn_export_gpkg.clicked.connect(self.exporter_geopackage)
        self.dlg.btn_export_csv.clicked.connect(self.exporter_csv)
        if hasattr(self.dlg, "btn_import_batch"):
            self.dlg.btn_import_batch.clicked.connect(self.importer_batch)
        if hasattr(self.dlg, "btn_lancer_batch"):
            self.dlg.btn_lancer_batch.clicked.connect(self.lancer_batch)
        if hasattr(self.dlg, "btn_export_batch_csv"):
            self.dlg.btn_export_batch_csv.clicked.connect(self.exporter_batch_csv)
        if hasattr(self.dlg, "btn_charger_admin"):
            self.dlg.btn_charger_admin.clicked.connect(self.charger_limites_admin)
        if hasattr(self.dlg, "btn_analyser_admin_selection"):
            self.dlg.btn_analyser_admin_selection.clicked.connect(
                self.analyser_admin_selection
            )
        if hasattr(self.dlg, "btn_analyser_admin_tous"):
            self.dlg.btn_analyser_admin_tous.clicked.connect(self.analyser_admin_tous)
        if hasattr(self.dlg, "btn_export_admin_xlsx"):
            self.dlg.btn_export_admin_xlsx.clicked.connect(self.exporter_admin_xlsx)

    def _set_default_values(self):
        self.dlg.input_latitude.setText("-4.325")
        self.dlg.input_longitude.setText("15.322")
        self._set_result("Pret. Saisir une zone ou choisir un point sur la carte.")

    def _set_result(self, text):
        if self.dlg is not None and hasattr(self.dlg, "label_resultat"):
            self.dlg.label_resultat.setPlainText(text)

    def _set_batch_result(self, text):
        if self.dlg is not None and hasattr(self.dlg, "label_batch_resultat"):
            self.dlg.label_batch_resultat.setPlainText(text)

    def _set_admin_result(self, text):
        if self.dlg is not None and hasattr(self.dlg, "label_admin_resultat"):
            self.dlg.label_admin_resultat.setPlainText(text)

    def _set_progress(self, value, maximum=100):
        if self.dlg is None or not hasattr(self.dlg, "progress_chargement"):
            return
        self.dlg.progress_chargement.setMaximum(maximum)
        self.dlg.progress_chargement.setValue(value)
        QCoreApplication.processEvents()

    def _set_buttons_enabled(self, enabled):
        for button in (
            self.dlg.btn_pick_point,
            self.dlg.btn_pointer_coordonnees,
            self.dlg.btn_creer_zone,
            getattr(self.dlg, "btn_detecter_zone", None),
            self.dlg.btn_analyser,
            self.dlg.btn_export_gpkg,
            self.dlg.btn_export_csv,
            getattr(self.dlg, "btn_import_batch", None),
            getattr(self.dlg, "btn_lancer_batch", None),
            getattr(self.dlg, "btn_export_batch_csv", None),
            getattr(self.dlg, "btn_charger_admin", None),
            getattr(self.dlg, "btn_analyser_admin_selection", None),
            getattr(self.dlg, "btn_analyser_admin_tous", None),
            getattr(self.dlg, "btn_export_admin_xlsx", None),
        ):
            if button is not None:
                button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Donnees et geometries
    # ------------------------------------------------------------------

    def _get_inputs(self):
        try:
            lat = self._parse_coordinate(
                self.dlg.input_latitude.text(), "latitude"
            )
            lon = self._parse_coordinate(
                self.dlg.input_longitude.text(), "longitude"
            )
        except Exception:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Parametres invalides",
                "Veuillez saisir une latitude et une longitude valides.",
            )
            return None

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Coordonnees invalides",
                "Latitude entre -90 et 90, longitude entre -180 et 180.",
            )
            return None

        return lat, lon

    def _parse_coordinate(self, value, coord_type):
        text = value.strip().upper().replace(",", ".")
        if not text:
            raise ValueError("coordonnee vide")

        hemisphere_sign = 1
        hemisphere_matches = re.findall(r"[NSEOW]", text)
        if hemisphere_matches:
            hemisphere = hemisphere_matches[-1]
            if hemisphere in ("S", "O", "W"):
                hemisphere_sign = -1
            if coord_type == "latitude" and hemisphere not in ("N", "S"):
                raise ValueError("hemisphere latitude invalide")
            if coord_type == "longitude" and hemisphere not in ("E", "O", "W"):
                raise ValueError("hemisphere longitude invalide")

        normalized = (
            text.replace("DEG", " ")
            .replace("D", " ")
            .replace("°", " ")
            .replace("'", " ")
            .replace("’", " ")
            .replace('"', " ")
            .replace("MIN", " ")
            .replace("SEC", " ")
        )
        normalized = re.sub(r"[NSEOW]", " ", normalized)
        parts = [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", normalized)]
        if not parts:
            raise ValueError("coordonnee invalide")

        if len(parts) == 1:
            coordinate = parts[0]
            if hemisphere_matches:
                coordinate = abs(coordinate) * hemisphere_sign
            return coordinate

        degrees = parts[0]
        minutes = parts[1]
        seconds = parts[2] if len(parts) >= 3 else 0.0
        if minutes >= 60 or seconds >= 60:
            raise ValueError("minutes ou secondes invalides")

        sign = -1 if degrees < 0 else 1
        if hemisphere_matches:
            sign = hemisphere_sign

        return sign * (abs(degrees) + minutes / 60.0 + seconds / 3600.0)

    def _distance_area(self):
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext(),
        )
        distance_area.setEllipsoid("WGS84")
        return distance_area

    def _metric_crs_for_point(self, lat, lon):
        crs = QgsCoordinateReferenceSystem()
        proj = (
            "+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 "
            "+datum=WGS84 +units=m +no_defs"
        ).format(lat=lat, lon=lon)
        if hasattr(crs, "createFromProj") and crs.createFromProj(proj):
            return crs
        if hasattr(crs, "createFromProj4") and crs.createFromProj4(proj):
            return crs
        return QgsCoordinateReferenceSystem("EPSG:3857")

    def _remove_layer_by_name(self, layer_name):
        project = QgsProject.instance()
        for layer in project.mapLayersByName(layer_name):
            project.removeMapLayer(layer.id())

    def _first_layer_by_name(self, layer_name):
        layers = QgsProject.instance().mapLayersByName(layer_name)
        return layers[0] if layers else None

    def _current_zone_layer(self):
        try:
            valid = self.zone_layer is not None and self.zone_layer.isValid()
        except RuntimeError:
            valid = False
        if not valid:
            self.zone_layer = self._first_layer_by_name(ZONE_LAYER_NAME)
        return self.zone_layer

    def _current_buildings_layer(self):
        try:
            valid = self.buildings_layer is not None and self.buildings_layer.isValid()
        except RuntimeError:
            valid = False
        if not valid:
            self.buildings_layer = self._first_layer_by_name(BUILDINGS_LAYER_NAME)
        return self.buildings_layer

    def _zone_geometry(self):
        layer = self._current_zone_layer()
        if layer is None:
            return None
        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom and not geom.isEmpty():
                return geom
        return None

    def _style_zone(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(255, 221, 87, 55))
        symbol.symbolLayer(0).setStrokeColor(QColor(230, 120, 0))
        symbol.symbolLayer(0).setStrokeWidth(0.8)
        layer.triggerRepaint()

    def _style_point(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(230, 120, 0))
        if hasattr(symbol, "setSize"):
            symbol.setSize(4.5)
        layer.triggerRepaint()

    def _style_buildings(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(40, 120, 210, 115))
        if hasattr(symbol, "setSize"):
            symbol.setSize(2.2)
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeColor"):
            symbol.symbolLayer(0).setStrokeColor(QColor(30, 75, 140))
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeWidth"):
            symbol.symbolLayer(0).setStrokeWidth(0.25)
        layer.triggerRepaint()

    def _style_counted(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(30, 170, 90, 150))
        symbol.symbolLayer(0).setStrokeColor(QColor(0, 105, 50))
        symbol.symbolLayer(0).setStrokeWidth(0.35)
        layer.triggerRepaint()

    def _style_admin_result(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(255, 221, 87, 0))
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeColor"):
            symbol.symbolLayer(0).setStrokeColor(QColor(255, 140, 0))
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeWidth"):
            symbol.symbolLayer(0).setStrokeWidth(0.9)
        layer.triggerRepaint()

    def _style_admin_buildings(self, layer):
        symbol = layer.renderer().symbol()
        symbol.setColor(QColor(30, 170, 90, 170))
        if hasattr(symbol, "setSize"):
            symbol.setSize(1.6)
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeColor"):
            symbol.symbolLayer(0).setStrokeColor(QColor(0, 105, 50))
        if symbol.symbolLayerCount() > 0 and hasattr(symbol.symbolLayer(0), "setStrokeWidth"):
            symbol.symbolLayer(0).setStrokeWidth(0.15)
        layer.triggerRepaint()

    def _zoom_to_layer(self, layer):
        if layer is None or layer.extent().isEmpty():
            return
        canvas = self.iface.mapCanvas()
        extent = QgsRectangle(layer.extent())
        extent.scale(1.25)
        canvas.setExtent(extent)
        canvas.refresh()

    def _center_map_on_wgs84(self, lat, lon):
        canvas = self.iface.mapCanvas()
        destination_crs = canvas.mapSettings().destinationCrs()
        point = QgsPointXY(lon, lat)
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if destination_crs != wgs84:
            transform = QgsCoordinateTransform(
                wgs84, destination_crs, QgsProject.instance().transformContext()
            )
            point = transform.transform(point)
        canvas.setCenter(point)
        if hasattr(canvas, "zoomScale"):
            canvas.zoomScale(10000.0)
        canvas.refresh()

    # ------------------------------------------------------------------
    # Zone d'analyse
    # ------------------------------------------------------------------

    def activer_selection_point(self):
        canvas = self.iface.mapCanvas()
        self.point_tool = QgsMapToolEmitPoint(canvas)
        self.point_tool.canvasClicked.connect(self._point_selectionne)
        canvas.setMapTool(self.point_tool)
        self._set_result("Cliquer sur la carte pour definir le centre de la zone.")

    def pointer_coordonnees(self):
        values = self._get_inputs()
        if values is None:
            return

        lat, lon = values
        self._remove_layer_by_name(POINT_LAYER_NAME)

        layer = QgsVectorLayer("Point?crs=EPSG:4326", POINT_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("latitude", QVariant.Double),
                QgsField("longitude", QVariant.Double),
            ]
        )
        layer.updateFields()

        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        feature.setAttributes([lat, lon])
        provider.addFeature(feature)
        layer.updateExtents()

        QgsProject.instance().addMapLayer(layer)
        self._style_point(layer)
        self._center_map_on_wgs84(lat, lon)
        self.iface.setActiveLayer(layer)
        self._set_result(
            "Carte centree sur les coordonnees ({:.6f}, {:.6f}).".format(lat, lon)
        )

    def _point_selectionne(self, point, button):
        del button
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if canvas_crs != wgs84:
            transform = QgsCoordinateTransform(
                canvas_crs, wgs84, QgsProject.instance().transformContext()
            )
            point = transform.transform(QgsPointXY(point))

        self.dlg.input_latitude.setText("{:.8f}".format(point.y()))
        self.dlg.input_longitude.setText("{:.8f}".format(point.x()))
        self._set_result("Point selectionne. Cliquer sur 'Dessiner la zone d'analyse'.")

    def creer_zone(self):
        layer = self._create_zone_layer()
        QgsProject.instance().addMapLayer(layer)
        self.zone_layer = layer
        self._style_zone(layer)
        self.iface.setActiveLayer(layer)
        layer.startEditing()
        self.iface.actionAddFeature().trigger()
        self._set_result(
            "Dessin manuel active. Tracez le polygone de la zone sur la carte, "
            "clic droit pour terminer, puis lancez le comptage ou sauvegardez les edits."
        )

    def _create_zone_layer(self):
        self._remove_layer_by_name(ZONE_LAYER_NAME)
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", ZONE_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("nom", QVariant.String),
                QgsField("latitude", QVariant.Double),
                QgsField("longitude", QVariant.Double),
                QgsField("surface_ha", QVariant.Double),
                QgsField("source", QVariant.String),
            ]
        )
        layer.updateFields()
        return layer

    def detecter_zone_autour_point(self):
        values = self._get_inputs()
        if values is None:
            return

        lat, lon = values
        search_radius = float(self.dlg.input_rayon_detection.value())
        connect_distance = float(self.dlg.input_distance_detection.value())
        margin = float(self.dlg.input_marge_detection.value())
        min_confidence = 0.65
        if hasattr(self.dlg, "input_confidence"):
            min_confidence = float(self.dlg.input_confidence.value())

        search_geom = self._circle_geometry_wgs84(lat, lon, search_radius)
        local_gpkg = self._local_open_buildings_gpkg()
        self._set_buttons_enabled(False)
        self._set_progress(0, 100)
        self._set_result("Detection automatique de la zone habitee...")
        QCoreApplication.processEvents()

        if os.path.exists(local_gpkg):
            try:
                rows = self._open_buildings_rows_from_local(
                    search_geom, min_confidence
                )
                self._finish_auto_zone_detection(
                    rows, lat, lon, search_radius, connect_distance, margin
                )
            except Exception as exc:
                self._set_buttons_enabled(True)
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Detection impossible",
                    "Impossible de detecter la zone automatiquement.\n\nDetail: {}".format(
                        exc
                    ),
                )
            return

        try:
            tiles = self._open_buildings_tiles_for_zone(search_geom)
            if not tiles:
                raise RuntimeError("Aucune tuile Open Buildings ne couvre ce point.")
            bbox = search_geom.boundingBox()
            self.pending_auto_zone_params = (
                lat,
                lon,
                search_radius,
                connect_distance,
                margin,
            )
            self.open_buildings_task = QgsTask.fromFunction(
                "Detection zone habitee",
                self._open_buildings_task,
                tiles=tiles,
                zone_wkt=search_geom.asWkt(),
                zone_bbox=(
                    bbox.xMinimum(),
                    bbox.yMinimum(),
                    bbox.xMaximum(),
                    bbox.yMaximum(),
                ),
                min_confidence=min_confidence,
                on_finished=self._auto_zone_detection_finished,
            )
            self.open_buildings_task.progressChanged.connect(
                lambda progress: self._set_progress(int(progress), 100)
            )
            QgsApplication.taskManager().addTask(self.open_buildings_task)
        except Exception as exc:
            self._set_buttons_enabled(True)
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Detection impossible",
                "Impossible de charger les batiments autour du point.\n\nDetail: {}".format(
                    exc
                ),
            )

    def _circle_geometry_wgs84(self, lat, lon, radius_m):
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric = self._metric_crs_for_point(lat, lon)
        to_metric = QgsCoordinateTransform(
            wgs84, metric, QgsProject.instance().transformContext()
        )
        to_wgs84 = QgsCoordinateTransform(
            metric, wgs84, QgsProject.instance().transformContext()
        )
        center_metric = to_metric.transform(QgsPointXY(lon, lat))
        geom = QgsGeometry.fromPointXY(center_metric).buffer(radius_m, 48)
        geom.transform(to_wgs84)
        return geom

    def _open_buildings_rows_from_local(self, zone_geom, min_confidence):
        local_gpkg = self._local_open_buildings_gpkg()
        uri = "{}|layername={}".format(local_gpkg, LOCAL_OPEN_BUILDINGS_LAYER)
        source = QgsVectorLayer(uri, "Open Buildings RDC", "ogr")
        if not source.isValid():
            raise RuntimeError("Base locale invalide: {}".format(local_gpkg))

        rows = []
        request = QgsFeatureRequest().setFilterRect(zone_geom.boundingBox())
        for feature in source.getFeatures(request):
            geom = feature.geometry()
            if geom is None or geom.isEmpty() or not zone_geom.contains(geom):
                continue

            names = feature.fields().names()

            def attr(name, default=""):
                if name not in names:
                    return default
                value = feature[name]
                return default if value is None else value

            try:
                confidence = float(attr("confidence", 0))
            except Exception:
                confidence = 0.0
            if confidence < min_confidence:
                continue

            try:
                point = geom.asPoint()
            except Exception:
                continue

            try:
                area_src = float(attr("area_m2", attr("area_m2_src", 0)))
            except Exception:
                area_src = 0.0

            rows.append(
                {
                    "openb_id": str(attr("openb_id", feature.id())),
                    "confidence": confidence,
                    "area_m2_src": area_src,
                    "plus_code": str(attr("plus_code", "")),
                    "latitude": point.y(),
                    "longitude": point.x(),
                }
            )
        return rows

    def _create_open_buildings_memory_layer(
        self, rows, layer_name=BUILDINGS_LAYER_NAME, remove_existing=True
    ):
        if remove_existing:
            self._remove_layer_by_name(layer_name)
        layer = QgsVectorLayer("Point?crs=EPSG:4326", layer_name, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("openb_id", QVariant.String),
                QgsField("confidence", QVariant.Double),
                QgsField("area_m2_src", QVariant.Double),
                QgsField("plus_code", QVariant.String),
                QgsField("source", QVariant.String),
                QgsField("zone_nom", QVariant.String),
            ]
        )
        layer.updateFields()

        for row in rows:
            geom = QgsGeometry.fromPointXY(
                QgsPointXY(float(row["longitude"]), float(row["latitude"]))
            )
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geom)
            feature.setAttributes(
                [
                    row.get("openb_id", ""),
                    float(row.get("confidence", 0)),
                    float(row.get("area_m2_src", 0)),
                    row.get("plus_code", ""),
                    row.get("source", "Google Open Buildings"),
                    row.get("zone_nom", ""),
                ]
            )
            provider.addFeature(feature)

        layer.updateExtents()
        return layer

    def _auto_zone_detection_finished(self, exception, result=None):
        self._set_buttons_enabled(True)
        if exception is not None:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Detection impossible",
                "Impossible de detecter la zone automatiquement.\n\nDetail: {}".format(
                    exception
                ),
            )
            self._set_result("Detection automatique interrompue.")
            return

        if result is None:
            self._set_result("Detection automatique interrompue.")
            return

        params = self.pending_auto_zone_params
        self.pending_auto_zone_params = None
        if params is None:
            self._set_result("Parametres de detection introuvables.")
            return

        self._finish_auto_zone_detection(result, *params)

    def _finish_auto_zone_detection(
        self, rows, lat, lon, search_radius, connect_distance, margin
    ):
        try:
            cluster_rows, zone_geom = self._auto_zone_from_buildings(
                rows, lat, lon, connect_distance, margin
            )
        except Exception as exc:
            self._set_buttons_enabled(True)
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Zone non detectee",
                "La zone automatique n'a pas pu etre construite.\n\nDetail: {}".format(
                    exc
                ),
            )
            self._set_result("Aucune zone habitee detectee autour du point.")
            return

        layer = self._create_zone_layer()
        surface_ha = self._distance_area().measureArea(zone_geom) / 10000.0
        feature = QgsFeature(layer.fields())
        feature.setGeometry(zone_geom)
        feature.setAttributes(
            [
                "Zone detectee",
                lat,
                lon,
                surface_ha,
                "detection_auto_open_buildings",
            ]
        )
        layer.dataProvider().addFeature(feature)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.zone_layer = layer
        self._style_zone(layer)

        buildings_layer = self._create_open_buildings_memory_layer(cluster_rows)
        QgsProject.instance().addMapLayer(buildings_layer)
        self.buildings_layer = buildings_layer
        self._style_buildings(buildings_layer)
        self._zoom_to_layer(layer)
        self.iface.setActiveLayer(layer)
        layer.startEditing()
        self._set_buttons_enabled(True)
        self._set_progress(100, 100)
        self._set_result(
            "Zone habitee detectee automatiquement.\n"
            "Rayon recherche: {:,.0f} m\n"
            "Distance entre toits: {:,.0f} m\n"
            "Batiments du groupe: {:,}\n"
            "Surface zone: {:.2f} ha\n\n"
            "Le polygone reste modifiable manuellement avant le comptage.".format(
                search_radius, connect_distance, len(cluster_rows), surface_ha
            )
        )

    def _auto_zone_from_buildings(self, rows, lat, lon, connect_distance, margin):
        if not rows:
            raise RuntimeError("Aucun batiment trouve dans le rayon de recherche.")

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric = self._metric_crs_for_point(lat, lon)
        to_metric = QgsCoordinateTransform(
            wgs84, metric, QgsProject.instance().transformContext()
        )
        to_wgs84 = QgsCoordinateTransform(
            metric, wgs84, QgsProject.instance().transformContext()
        )
        center = to_metric.transform(QgsPointXY(lon, lat))
        points = []
        for index, row in enumerate(rows):
            try:
                point = to_metric.transform(
                    QgsPointXY(float(row["longitude"]), float(row["latitude"]))
                )
            except Exception:
                continue
            dx = point.x() - center.x()
            dy = point.y() - center.y()
            points.append(
                {
                    "index": index,
                    "row": row,
                    "point": point,
                    "distance2_center": dx * dx + dy * dy,
                }
            )

        if not points:
            raise RuntimeError("Aucun batiment exploitable autour du point.")

        seed = min(points, key=lambda item: item["distance2_center"])
        cluster_indices = self._connected_building_indices(
            points, seed["index"], connect_distance
        )
        cluster_points = [
            item["point"] for item in points if item["index"] in cluster_indices
        ]
        cluster_rows = [
            item["row"] for item in points if item["index"] in cluster_indices
        ]
        if len(cluster_points) < 3:
            raise RuntimeError(
                "Moins de 3 batiments connectes au point central avec ces parametres."
            )

        zone_metric = self._zone_geometry_from_metric_points(cluster_points, margin)
        zone_wgs84 = QgsGeometry(zone_metric)
        zone_wgs84.transform(to_wgs84)
        return cluster_rows, zone_wgs84

    def _connected_building_indices(self, points, seed_index, connect_distance):
        cell_size = max(connect_distance, 1.0)
        max_distance2 = connect_distance * connect_distance
        grid = {}
        by_index = {}
        for item in points:
            point = item["point"]
            cell = (
                int(math.floor(point.x() / cell_size)),
                int(math.floor(point.y() / cell_size)),
            )
            item["cell"] = cell
            by_index[item["index"]] = item
            grid.setdefault(cell, []).append(item)

        visited = set()
        queue = [seed_index]
        while queue:
            current_index = queue.pop(0)
            if current_index in visited:
                continue
            visited.add(current_index)
            current = by_index[current_index]
            cx, cy = current["cell"]
            current_point = current["point"]
            for nx in range(cx - 1, cx + 2):
                for ny in range(cy - 1, cy + 2):
                    for neighbor in grid.get((nx, ny), []):
                        neighbor_index = neighbor["index"]
                        if neighbor_index in visited:
                            continue
                        neighbor_point = neighbor["point"]
                        dx = neighbor_point.x() - current_point.x()
                        dy = neighbor_point.y() - current_point.y()
                        if dx * dx + dy * dy <= max_distance2:
                            queue.append(neighbor_index)
        return visited

    def _zone_geometry_from_metric_points(self, points, margin):
        margin = max(float(margin), 1.0)
        if len(points) > 5000:
            geom = QgsGeometry.fromMultiPointXY(points).convexHull().buffer(margin, 12)
            if geom is None or geom.isEmpty():
                raise RuntimeError("Enveloppe automatique vide.")
            return geom

        buffers = [QgsGeometry.fromPointXY(point).buffer(margin, 8) for point in points]
        geom = QgsGeometry.unaryUnion(buffers)
        if geom is None or geom.isEmpty():
            raise RuntimeError("Enveloppe automatique vide.")
        if geom.isMultipart():
            geom = QgsGeometry.fromMultiPointXY(points).convexHull().buffer(margin, 12)
        return geom

    # ------------------------------------------------------------------
    # Chargement Open Buildings
    # ------------------------------------------------------------------

    def analyser_zone(self):
        self.auto_count_after_load = True
        self.charger_toits_open_buildings()

    def _local_open_buildings_gpkg(self):
        if os.path.exists(USER_OPEN_BUILDINGS_GPKG):
            return USER_OPEN_BUILDINGS_GPKG
        if os.path.exists(BUNDLED_OPEN_BUILDINGS_GPKG):
            return BUNDLED_OPEN_BUILDINGS_GPKG
        return USER_OPEN_BUILDINGS_GPKG

    def _open_buildings_download_url(self):
        if OPEN_BUILDINGS_DOWNLOAD_URL:
            return OPEN_BUILDINGS_DOWNLOAD_URL
        if OPEN_BUILDINGS_DRIVE_FILE_ID:
            return (
                "https://drive.google.com/uc?export=download&id={}".format(
                    OPEN_BUILDINGS_DRIVE_FILE_ID
                )
            )
        return ""

    def charger_toits_open_buildings(self):
        zone_geom = self._zone_geometry()
        if zone_geom is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Zone manquante",
                "Creez ou dessinez d'abord une zone d'analyse.",
            )
            return

        local_gpkg = self._local_open_buildings_gpkg()
        if os.path.exists(local_gpkg):
            self.charger_toits_open_buildings_local(zone_geom)
            return

        download_url = self._open_buildings_download_url()
        if download_url:
            answer = QMessageBox.question(
                self.iface.mainWindow(),
                "Base Open Buildings RDC",
                "La base locale Open Buildings RDC n'est pas encore installee.\n\n"
                "Voulez-vous la telecharger maintenant depuis la source autorisee ?\n"
                "Sinon, le plugin utilisera le chargement en ligne par tuiles.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self._telecharger_base_open_buildings(zone_geom, download_url)
                return

        min_confidence = 0.65
        if hasattr(self.dlg, "input_confidence"):
            min_confidence = float(self.dlg.input_confidence.value())

        self._set_buttons_enabled(False)
        self._set_result(
            "Recherche des tuiles Google Open Buildings qui croisent la zone..."
        )
        QCoreApplication.processEvents()

        try:
            tiles = self._open_buildings_tiles_for_zone(zone_geom)
            if not tiles:
                raise RuntimeError(
                    "Aucune tuile Open Buildings ne couvre cette zone."
                )

            self._set_progress(0, 100)
            bbox = zone_geom.boundingBox()
            self.open_buildings_task = QgsTask.fromFunction(
                "Chargement Google Open Buildings",
                self._open_buildings_task,
                tiles=tiles,
                zone_wkt=zone_geom.asWkt(),
                zone_bbox=(
                    bbox.xMinimum(),
                    bbox.yMinimum(),
                    bbox.xMaximum(),
                    bbox.yMaximum(),
                ),
                min_confidence=min_confidence,
                on_finished=self._open_buildings_finished,
            )
            self.open_buildings_task.progressChanged.connect(
                lambda progress: self._set_progress(int(progress), 100)
            )
            QgsApplication.taskManager().addTask(self.open_buildings_task)
            return
        except Exception as exc:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Erreur Open Buildings",
                "Impossible de charger Google Open Buildings.\n\nDetail: {}".format(
                    exc
                ),
            )
            self._set_result("Erreur pendant le chargement Open Buildings.")
            self._set_buttons_enabled(True)
            return

    def _telecharger_base_open_buildings(self, zone_geom, download_url):
        self.pending_open_buildings_zone_wkt = zone_geom.asWkt()
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        self._set_buttons_enabled(False)
        self._set_progress(0, 100)
        self._set_result("Telechargement de la base Open Buildings RDC...")
        self.open_buildings_download_task = QgsTask.fromFunction(
            "Telechargement Open Buildings RDC",
            self._download_open_buildings_task,
            download_url=download_url,
            target_path=USER_OPEN_BUILDINGS_GPKG,
            on_finished=self._open_buildings_download_finished,
        )
        self.open_buildings_download_task.progressChanged.connect(
            lambda progress: self._set_progress(int(progress), 100)
        )
        QgsApplication.taskManager().addTask(self.open_buildings_download_task)

    def _download_open_buildings_task(self, task, download_url, target_path):
        temp_path = target_path + ".download"
        request = urllib.request.Request(
            download_url,
            headers={"User-Agent": "QGIS ToitAnalyzerRDC/0.3"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(temp_path, "wb") as handle:
                while True:
                    if task.isCanceled():
                        return None
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        task.setProgress(min(99, downloaded * 100.0 / total))

        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            with open(temp_path, "rb") as handle:
                signature = handle.read(16)
            if signature != b"SQLite format 3\x00":
                raise RuntimeError(
                    "Le fichier telecharge n'est pas un GeoPackage valide. "
                    "Le lien doit pointer directement vers le fichier .gpkg."
                )
            os.replace(temp_path, target_path)
        task.setProgress(100)
        return target_path

    def _open_buildings_download_finished(self, exception, result=None):
        self._set_buttons_enabled(True)
        if exception is not None:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Erreur de telechargement",
                "Impossible de telecharger la base Open Buildings RDC.\n\n"
                "Verifiez que le lien Drive autorise le telechargement direct.\n\n"
                "Detail: {}".format(exception),
            )
            self._set_result("Telechargement Open Buildings RDC impossible.")
            return

        if result is None or not os.path.exists(result):
            self._set_result("Telechargement Open Buildings RDC interrompu.")
            return

        zone_geom = QgsGeometry.fromWkt(self.pending_open_buildings_zone_wkt)
        self.pending_open_buildings_zone_wkt = None
        self.charger_toits_open_buildings_local(zone_geom)

    def charger_toits_open_buildings_local(self, zone_geom):
        min_confidence = 0.65
        if hasattr(self.dlg, "input_confidence"):
            min_confidence = float(self.dlg.input_confidence.value())

        self._set_buttons_enabled(False)
        self._set_progress(0, 100)
        self._set_result("Lecture de la base locale Open Buildings RDC...")
        QCoreApplication.processEvents()

        local_gpkg = self._local_open_buildings_gpkg()
        uri = "{}|layername={}".format(
            local_gpkg, LOCAL_OPEN_BUILDINGS_LAYER
        )
        source = QgsVectorLayer(uri, "Open Buildings RDC", "ogr")
        if not source.isValid():
            self._set_buttons_enabled(True)
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Base locale invalide",
                "Impossible de lire la base locale Open Buildings:\n{}".format(
                    local_gpkg
                ),
            )
            return

        self._remove_layer_by_name(BUILDINGS_LAYER_NAME)
        layer = QgsVectorLayer("Point?crs=EPSG:4326", BUILDINGS_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("openb_id", QVariant.String),
                QgsField("confidence", QVariant.Double),
                QgsField("area_m2_src", QVariant.Double),
                QgsField("plus_code", QVariant.String),
                QgsField("source", QVariant.String),
            ]
        )
        layer.updateFields()

        request = QgsFeatureRequest().setFilterRect(zone_geom.boundingBox())
        total_candidates = 0
        kept = 0
        batch = []
        for feature in source.getFeatures(request):
            total_candidates += 1
            if total_candidates % 5000 == 0:
                self._set_progress(min(95, int(total_candidates / 5000)), 100)
                self._set_result(
                    "Lecture locale Open Buildings...\n"
                    "Candidats bbox: {:,}\n"
                    "Batiments dans la zone: {:,}".format(total_candidates, kept)
                )

            try:
                confidence = float(feature["confidence"])
            except Exception:
                confidence = 0.0
            if confidence < min_confidence:
                continue

            geom = feature.geometry()
            if geom is None or geom.isEmpty() or not zone_geom.contains(geom):
                continue

            output = QgsFeature(layer.fields())
            output.setGeometry(geom)
            output.setAttributes(
                [
                    str(feature["openb_id"]),
                    confidence,
                    float(feature["area_m2"]),
                    str(feature["plus_code"]),
                    "Google Open Buildings local RDC",
                ]
            )
            batch.append(output)
            kept += 1
            if len(batch) >= 5000:
                provider.addFeatures(batch)
                batch = []

        if batch:
            provider.addFeatures(batch)

        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.buildings_layer = layer
        self._style_buildings(layer)
        self.iface.setActiveLayer(layer)
        self.iface.mapCanvas().refresh()
        self._set_buttons_enabled(True)
        self._set_progress(100, 100)
        self._set_result(
            "Base locale Open Buildings RDC utilisee.\n"
            "Candidats bbox: {:,}\n"
            "Batiments dans la zone: {:,}".format(total_candidates, kept)
        )
        if self.auto_count_after_load:
            self.auto_count_after_load = False
            self.compter_toits()

    def _open_buildings_finished(self, exception, result=None):
        self._set_buttons_enabled(True)
        if exception is not None:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Erreur Open Buildings",
                "Impossible de charger Google Open Buildings.\n\nDetail: {}".format(
                    exception
                ),
            )
            self._set_result("Erreur pendant le chargement Open Buildings.")
            return

        if result is None:
            self._set_result("Chargement Open Buildings interrompu.")
            return

        layer = self._create_open_buildings_layer(result)
        QgsProject.instance().addMapLayer(layer)
        self.buildings_layer = layer
        self._style_buildings(layer)
        self.iface.setActiveLayer(layer)
        self.iface.mapCanvas().refresh()
        self._set_progress(100, 100)
        self._set_result(
            "Open Buildings charges: {} batiments ponctuels. "
            "Vous pouvez maintenant compter.".format(
                layer.featureCount()
            )
        )
        if self.auto_count_after_load:
            self.auto_count_after_load = False
            self.compter_toits()

    def _open_buildings_tiles_for_zone(self, zone_geom):
        with urllib.request.urlopen(OPEN_BUILDINGS_TILES_URL, timeout=60) as response:
            tiles_geojson = json.loads(response.read().decode("utf-8"))

        zone_bbox = zone_geom.boundingBox()
        tiles = []
        for feature in tiles_geojson.get("features", []):
            geometry = feature.get("geometry")
            tile_bbox = self._bbox_from_geojson_geometry(geometry)
            if tile_bbox is None or not tile_bbox.intersects(zone_bbox):
                continue

            properties = feature.get("properties", {})
            tile_url = properties.get("tile_url") or properties.get("url")
            if not tile_url:
                continue

            tiles.append(
                {
                    "tile_id": properties.get("tile_id")
                    or properties.get("s2_token")
                    or os.path.basename(tile_url),
                    "tile_url": tile_url,
                    "size_mb": properties.get("size_mb", 0),
                }
            )

        return tiles

    def _open_buildings_task(self, task, tiles, zone_wkt, zone_bbox, min_confidence):
        zone_geom = QgsGeometry.fromWkt(zone_wkt)
        xmin, ymin, xmax, ymax = zone_bbox
        rows = []

        for tile_index, tile in enumerate(tiles, start=1):
            if task.isCanceled():
                return None

            tile_start = ((tile_index - 1) / max(len(tiles), 1)) * 100.0
            tile_span = 100.0 / max(len(tiles), 1)
            task.setProgress(tile_start)

            point_url = tile["tile_url"].replace(
                "/polygons_s2_level_4_gzip/",
                "/points_s2_level_4_gzip/",
            )
            request = urllib.request.Request(
                point_url,
                headers={"User-Agent": "QGIS ToitAnalyzerRDC/0.2"},
            )

            with urllib.request.urlopen(request, timeout=240) as response:
                gzip_file = gzip.GzipFile(fileobj=response)
                text_file = io.TextIOWrapper(gzip_file, encoding="utf-8")
                reader = csv.DictReader(text_file)

                for row_index, row in enumerate(reader, start=1):
                    if task.isCanceled():
                        return None

                    if row_index % 5000 == 0:
                        # Progression indicative: les CSV n'exposent pas le nombre
                        # total de lignes avant lecture complete.
                        within_tile = min(0.95, row_index / 250000.0)
                        task.setProgress(tile_start + tile_span * within_tile)

                    try:
                        lat = float(row.get("latitude", "nan"))
                        lon = float(row.get("longitude", "nan"))
                        confidence = float(row.get("confidence", 0))
                    except Exception:
                        continue

                    if confidence < min_confidence:
                        continue
                    if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
                        continue

                    point_geom = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
                    if not zone_geom.contains(point_geom):
                        continue

                    try:
                        area_src = float(row.get("area_in_meters", 0))
                    except Exception:
                        area_src = 0.0

                    rows.append(
                        {
                            "openb_id": "{}_{}".format(tile["tile_id"], row_index),
                            "confidence": confidence,
                            "area_m2_src": area_src,
                            "plus_code": row.get("full_plus_code", ""),
                            "latitude": lat,
                            "longitude": lon,
                        }
                    )

            task.setProgress(tile_start + tile_span)

        task.setProgress(100)
        return rows

    def _create_open_buildings_layer(self, rows):
        return self._create_open_buildings_memory_layer(rows)

    def _bbox_from_geojson_geometry(self, geometry):
        if not geometry:
            return None

        coords = []

        def collect(value):
            if (
                isinstance(value, list)
                and len(value) >= 2
                and isinstance(value[0], (int, float))
                and isinstance(value[1], (int, float))
            ):
                coords.append((float(value[0]), float(value[1])))
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)

        collect(geometry.get("coordinates", []))
        if not coords:
            return None

        xs = [item[0] for item in coords]
        ys = [item[1] for item in coords]
        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))

    # ------------------------------------------------------------------
    # Comptage et statistiques
    # ------------------------------------------------------------------

    def compter_toits(self):
        zone_layer = self._current_zone_layer()
        buildings_layer = self._current_buildings_layer()

        if zone_layer is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Zone manquante",
                "Veuillez dessiner une zone d'analyse avant de lancer l'analyse.",
            )
            return

        if buildings_layer is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toits manquants",
                "Veuillez charger Open Buildings avant de lancer le comptage.",
            )
            return

        zone_geom = self._zone_geometry()
        if zone_geom is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Zone vide",
                "La couche de zone ne contient aucune geometrie exploitable.",
            )
            return

        stats, counted_layer = self._compute_statistics(zone_geom, buildings_layer)
        QgsProject.instance().addMapLayer(counted_layer)
        self.counted_layer = counted_layer
        self._style_counted(counted_layer)
        self.iface.mapCanvas().refresh()
        self.last_stats = stats
        self._set_result(self._format_stats(stats))

        QMessageBox.information(
            self.iface.mainWindow(),
            "Resultat du comptage",
            self._format_stats(stats),
        )

    def _compute_statistics(
        self,
        zone_geom,
        buildings_layer,
        layer_name=COUNTED_LAYER_NAME,
        remove_existing=True,
    ):
        if remove_existing:
            self._remove_layer_by_name(layer_name)
        geometry_type = QgsWkbTypes.geometryType(buildings_layer.wkbType())
        if geometry_type == QgsWkbTypes.PointGeometry:
            counted_layer = QgsVectorLayer(
                "Point?crs=EPSG:4326", layer_name, "memory"
            )
        else:
            counted_layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326", layer_name, "memory"
            )
        provider = counted_layer.dataProvider()
        provider.addAttributes([field for field in buildings_layer.fields()])
        provider.addAttributes(
            [
                QgsField("surface_m2", QVariant.Double),
                QgsField("part_zone", QVariant.Double),
            ]
        )
        counted_layer.updateFields()

        index = QgsSpatialIndex(buildings_layer.getFeatures())
        candidate_ids = index.intersects(zone_geom.boundingBox())
        distance_area = self._distance_area()
        seen_ids = set()
        nb_toits = 0
        surface_batie_m2 = 0.0
        invalides = 0
        doublons = 0

        for fid in candidate_ids:
            feature = buildings_layer.getFeature(fid)
            geom = feature.geometry()
            if geom is None or geom.isEmpty() or not geom.isGeosValid():
                invalides += 1
                continue

            if "openb_id" in feature.fields().names():
                building_id = str(feature["openb_id"])
            else:
                building_id = ""
            geom_key = geom.asWkt(2)
            dedupe_key = building_id or geom_key
            if dedupe_key in seen_ids:
                doublons += 1
                continue
            seen_ids.add(dedupe_key)

            if geometry_type == QgsWkbTypes.PointGeometry:
                if not zone_geom.contains(geom):
                    continue
                try:
                    surface_intersection = float(feature["area_m2_src"])
                except Exception:
                    surface_intersection = 0.0
                part_zone = 1.0
            else:
                if not geom.intersects(zone_geom):
                    continue

                intersection = geom.intersection(zone_geom)
                if intersection is None or intersection.isEmpty():
                    continue

                try:
                    surface_intersection = distance_area.measureArea(intersection)
                    surface_totale = distance_area.measureArea(geom)
                except Exception:
                    invalides += 1
                    continue

                part_zone = (
                    surface_intersection / surface_totale if surface_totale > 0 else 1.0
                )

            nb_toits += 1
            surface_batie_m2 += surface_intersection

            output = QgsFeature(counted_layer.fields())
            output.setGeometry(geom)
            output.setAttributes(feature.attributes() + [surface_intersection, part_zone])
            provider.addFeature(output)

        counted_layer.updateExtents()
        surface_zone_m2 = distance_area.measureArea(zone_geom)
        surface_zone_ha = surface_zone_m2 / 10000.0 if surface_zone_m2 else 0.0
        centroid = zone_geom.centroid().asPoint()
        densite = nb_toits / surface_zone_ha if surface_zone_ha else 0.0

        menage_par_toit = float(self.dlg.input_menage_par_toit.value())
        pers_par_menage = float(self.dlg.input_personnes_menage.value())
        menages = nb_toits * menage_par_toit
        population = menages * pers_par_menage

        stats = {
            "nb_toits": nb_toits,
            "surface_zone_m2": surface_zone_m2,
            "surface_zone_ha": surface_zone_ha,
            "surface_zone_km2": surface_zone_m2 / 1000000.0 if surface_zone_m2 else 0.0,
            "densite_toits_ha": densite,
            "surface_batie_m2": surface_batie_m2,
            "latitude_centre": centroid.y(),
            "longitude_centre": centroid.x(),
            "menages_estimes": menages,
            "population_estimee": population,
            "invalides_ignores": invalides,
            "doublons_ignores": doublons,
            "source_batiments": buildings_layer.name(),
        }
        return stats, counted_layer

    def _format_stats(self, stats):
        return (
            "Nombre de toits: {nb_toits}\n"
            "Surface zone: {surface_zone_ha:.2f} ha ({surface_zone_km2:.4f} km2)\n"
            "Densite: {densite_toits_ha:.2f} toits/ha\n"
            "Surface batie dans la zone: {surface_batie_m2:.0f} m2\n"
            "Centre: {latitude_centre:.6f}, {longitude_centre:.6f}\n"
            "Menages estimes: {menages_estimes:.0f}\n"
            "Population estimee: {population_estimee:.0f}\n"
            "Invalides ignores: {invalides_ignores} | Doublons ignores: {doublons_ignores}"
        ).format(**stats)

    # ------------------------------------------------------------------
    # Corrections manuelles
    # ------------------------------------------------------------------

    def activer_edition_toits(self):
        layer = self._current_buildings_layer()
        if layer is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Couche manquante",
                "Chargez d'abord les toits avant de les corriger.",
            )
            return

        self.iface.setActiveLayer(layer)
        if not layer.isEditable():
            layer.startEditing()
        self._set_result(
            "Edition active sur TA_RDC_toits. Utilisez les outils QGIS pour ajouter, "
            "supprimer ou corriger des batiments, puis cliquez sur 'Sauver et recalculer'."
        )

    def arreter_edition_toits(self):
        layer = self._current_buildings_layer()
        if layer is None:
            return
        if layer.isEditable():
            if not layer.commitChanges():
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Edition",
                    "Les changements n'ont pas pu etre sauvegardes.",
                )
                return
        self.compter_toits()

    # ------------------------------------------------------------------
    # Analyse par lots
    # ------------------------------------------------------------------

    def importer_batch(self):
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Importer des zones a analyser",
            os.path.expanduser("~"),
            "Tableur ou CSV (*.xlsx *.csv);;Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return

        try:
            rows = self._read_batch_file(path)
            batch_rows = self._normalize_batch_rows(rows)
        except Exception as exc:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Import impossible",
                "Impossible de lire le fichier.\n\nDetail: {}".format(exc),
            )
            return

        if not batch_rows:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Fichier vide",
                "Aucune ligne exploitable n'a ete trouvee.",
            )
            return

        self.batch_rows = batch_rows
        self.batch_results = []
        self._create_batch_points_layer(batch_rows)
        if hasattr(self.dlg, "label_batch_file"):
            self.dlg.label_batch_file.setText(os.path.basename(path))
        if hasattr(self.dlg, "label_batch_count"):
            self.dlg.label_batch_count.setText(
                "{} zones importees".format(len(batch_rows))
            )
        self._set_batch_result(
            "{} zones importees.\n"
            "Verifiez les points sur la carte, puis lancez l'analyse par lots.".format(
                len(batch_rows)
            )
        )

    def _read_batch_file(self, path):
        if path.lower().endswith(".csv"):
            return self._read_csv_table(path)
        if path.lower().endswith(".xlsx"):
            return self._read_xlsx_table(path)
        raise RuntimeError("Format non supporte. Utilisez .xlsx ou .csv.")

    def _read_csv_table(self, path):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except Exception:
                dialect = csv.excel
            reader = csv.reader(handle, dialect)
            return [row for row in reader if any(cell.strip() for cell in row)]

    def _read_xlsx_table(self, path):
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(path) as archive:
            shared_strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall("m:si", ns):
                    texts = [node.text or "" for node in item.findall(".//m:t", ns)]
                    shared_strings.append("".join(texts))

            sheet_path = "xl/worksheets/sheet1.xml"
            if sheet_path not in archive.namelist():
                sheet_names = [
                    name
                    for name in archive.namelist()
                    if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                ]
                if not sheet_names:
                    raise RuntimeError("Aucune feuille Excel lisible.")
                sheet_path = sorted(sheet_names)[0]

            root = ET.fromstring(archive.read(sheet_path))
            table = []
            for row in root.findall(".//m:sheetData/m:row", ns):
                values = {}
                max_col = 0
                for cell in row.findall("m:c", ns):
                    ref = cell.get("r", "")
                    col_index = self._xlsx_col_index(ref)
                    max_col = max(max_col, col_index)
                    value_node = cell.find("m:v", ns)
                    inline_node = cell.find("m:is/m:t", ns)
                    if inline_node is not None:
                        value = inline_node.text or ""
                    elif value_node is None:
                        value = ""
                    elif cell.get("t") == "s":
                        idx = int(value_node.text)
                        value = shared_strings[idx] if idx < len(shared_strings) else ""
                    else:
                        value = value_node.text or ""
                    values[col_index] = value
                if values:
                    table.append([values.get(index, "") for index in range(1, max_col + 1)])
            return table

    def _xlsx_col_index(self, cell_ref):
        letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
        index = 0
        for char in letters:
            index = index * 26 + ord(char) - ord("A") + 1
        return max(index, 1)

    def _normalize_batch_rows(self, table):
        if not table:
            return []
        headers = [self._normalize_header(value) for value in table[0]]
        name_idx = self._find_column(headers, ("nom_zone", "nom", "zone", "village", "localite", "name"))
        lat_idx = self._find_column(headers, ("latitude", "lat", "y"))
        lon_idx = self._find_column(headers, ("longitude", "lon", "long", "lng", "x"))

        data_rows = table[1:]
        first_data_line = 2
        if lat_idx is None or lon_idx is None:
            if len(table[0]) >= 3:
                name_idx, lat_idx, lon_idx = 0, 1, 2
                data_rows = table
                first_data_line = 1
            else:
                raise RuntimeError(
                    "Colonnes attendues: nom_zone, latitude, longitude."
                )

        batch_rows = []
        for row_index, row in enumerate(data_rows, start=first_data_line):
            if max(lat_idx, lon_idx) >= len(row):
                continue
            name = ""
            if name_idx is not None and name_idx < len(row):
                name = str(row[name_idx]).strip()
            if not name:
                name = "Zone {}".format(len(batch_rows) + 1)
            try:
                lat = self._parse_coordinate(str(row[lat_idx]), "latitude")
                lon = self._parse_coordinate(str(row[lon_idx]), "longitude")
            except Exception:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            batch_rows.append(
                {
                    "nom_zone": name,
                    "latitude": lat,
                    "longitude": lon,
                    "ligne": row_index,
                }
            )
        return batch_rows

    def _normalize_header(self, value):
        text = str(value).strip().lower()
        text = text.replace(" ", "_").replace("-", "_")
        text = text.replace("é", "e").replace("è", "e").replace("ê", "e")
        text = text.replace("à", "a").replace("ç", "c")
        return text

    def _find_column(self, headers, names):
        for name in names:
            if name in headers:
                return headers.index(name)
        return None

    def _create_batch_points_layer(self, rows):
        self._remove_layer_by_name(BATCH_POINTS_LAYER_NAME)
        layer = QgsVectorLayer("Point?crs=EPSG:4326", BATCH_POINTS_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("nom_zone", QVariant.String),
                QgsField("latitude", QVariant.Double),
                QgsField("longitude", QVariant.Double),
                QgsField("ligne", QVariant.Int),
            ]
        )
        layer.updateFields()

        features = []
        for row in rows:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPointXY(
                    QgsPointXY(row["longitude"], row["latitude"])
                )
            )
            feature.setAttributes(
                [row["nom_zone"], row["latitude"], row["longitude"], row["ligne"]]
            )
            features.append(feature)

        provider.addFeatures(features)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.batch_points_layer = layer
        self._style_point(layer)
        self._zoom_to_layer(layer)

    def lancer_batch(self):
        if not self.batch_rows:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Aucun fichier batch",
                "Importez d'abord un fichier Excel ou CSV.",
            )
            return

        local_gpkg = self._local_open_buildings_gpkg()
        if not os.path.exists(local_gpkg):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Base locale manquante",
                "L'analyse par lots necessite la base locale Open Buildings RDC.\n"
                "Telechargez ou placez d'abord le fichier GeoPackage local.",
            )
            return

        self.batch_results = []
        self._remove_layer_by_name(BATCH_ZONES_LAYER_NAME)
        self._remove_layer_by_name(BATCH_BUILDINGS_LAYER_NAME)
        self._set_buttons_enabled(False)
        self._set_progress(0, max(len(self.batch_rows), 1))
        self._set_batch_result("Analyse par lots en cours...")
        QCoreApplication.processEvents()

        zones_layer = self._create_batch_zones_layer()
        buildings_layer = None
        search_radius = float(self.dlg.input_rayon_detection.value())
        connect_distance = float(self.dlg.input_distance_detection.value())
        margin = float(self.dlg.input_marge_detection.value())
        min_confidence = float(self.dlg.input_confidence.value())

        for index, row in enumerate(self.batch_rows, start=1):
            status_line = "{} / {} - {}".format(
                index, len(self.batch_rows), row["nom_zone"]
            )
            self._set_batch_result(status_line)
            QCoreApplication.processEvents()
            try:
                result, counted_layer = self._run_batch_row(
                    row, search_radius, connect_distance, margin, min_confidence
                )
                self.batch_results.append(result)
                self._add_batch_zone_feature(zones_layer, result)
                if counted_layer is not None:
                    buildings_layer = self._append_counted_to_batch_layer(
                        buildings_layer, counted_layer, row["nom_zone"]
                    )
            except Exception as exc:
                result = self._batch_error_result(row, str(exc))
                self.batch_results.append(result)
                self._add_batch_zone_feature(zones_layer, result)

            self._set_progress(index, len(self.batch_rows))

        QgsProject.instance().addMapLayer(zones_layer)
        self.batch_zones_layer = zones_layer
        self._style_zone(zones_layer)
        if buildings_layer is not None:
            QgsProject.instance().addMapLayer(buildings_layer)
            self.batch_buildings_layer = buildings_layer
            self._style_counted(buildings_layer)
        self._set_buttons_enabled(True)
        self._set_progress(100, 100)
        self._set_batch_result(self._format_batch_summary())
        self._set_result(self._format_batch_summary())
        self._zoom_to_layer(zones_layer)

    def _run_batch_row(
        self, row, search_radius, connect_distance, margin, min_confidence
    ):
        search_geom = self._circle_geometry_wgs84(
            row["latitude"], row["longitude"], search_radius
        )
        building_rows = self._open_buildings_rows_from_local(
            search_geom, min_confidence
        )
        for building_row in building_rows:
            building_row["zone_nom"] = row["nom_zone"]
        cluster_rows, zone_geom = self._auto_zone_from_buildings(
            building_rows,
            row["latitude"],
            row["longitude"],
            connect_distance,
            margin,
        )
        for building_row in cluster_rows:
            building_row["zone_nom"] = row["nom_zone"]
        buildings_layer = self._create_open_buildings_memory_layer(
            cluster_rows, "{}_tmp".format(BUILDINGS_LAYER_NAME), remove_existing=False
        )
        stats, counted_layer = self._compute_statistics(
            zone_geom,
            buildings_layer,
            layer_name="{}_tmp".format(COUNTED_LAYER_NAME),
            remove_existing=False,
        )
        result = {
            "nom_zone": row["nom_zone"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "ligne": row["ligne"],
            "statut": "OK",
            "message": "Zone detectee",
            "geometry": zone_geom,
        }
        result.update(stats)
        return result, counted_layer

    def _create_batch_zones_layer(self):
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", BATCH_ZONES_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("nom_zone", QVariant.String),
                QgsField("latitude", QVariant.Double),
                QgsField("longitude", QVariant.Double),
                QgsField("statut", QVariant.String),
                QgsField("message", QVariant.String),
                QgsField("nb_toits", QVariant.Int),
                QgsField("surface_ha", QVariant.Double),
                QgsField("densite", QVariant.Double),
                QgsField("menages", QVariant.Double),
                QgsField("population", QVariant.Double),
            ]
        )
        layer.updateFields()
        return layer

    def _add_batch_zone_feature(self, layer, result):
        feature = QgsFeature(layer.fields())
        geom = result.get("geometry")
        if geom is not None:
            feature.setGeometry(geom)
        feature.setAttributes(
            [
                result.get("nom_zone", ""),
                result.get("latitude", 0.0),
                result.get("longitude", 0.0),
                result.get("statut", ""),
                result.get("message", ""),
                int(result.get("nb_toits", 0) or 0),
                float(result.get("surface_zone_ha", 0.0) or 0.0),
                float(result.get("densite_toits_ha", 0.0) or 0.0),
                float(result.get("menages_estimes", 0.0) or 0.0),
                float(result.get("population_estimee", 0.0) or 0.0),
            ]
        )
        layer.dataProvider().addFeature(feature)
        layer.updateExtents()

    def _append_counted_to_batch_layer(self, batch_layer, counted_layer, zone_name):
        if batch_layer is None:
            batch_layer = QgsVectorLayer(
                "Point?crs=EPSG:4326", BATCH_BUILDINGS_LAYER_NAME, "memory"
            )
            batch_layer.dataProvider().addAttributes(
                [field for field in counted_layer.fields()]
            )
            batch_layer.dataProvider().addAttributes(
                [QgsField("zone_batch", QVariant.String)]
            )
            batch_layer.updateFields()

        provider = batch_layer.dataProvider()
        features = []
        for source_feature in counted_layer.getFeatures():
            feature = QgsFeature(batch_layer.fields())
            feature.setGeometry(source_feature.geometry())
            feature.setAttributes(source_feature.attributes() + [zone_name])
            features.append(feature)
        provider.addFeatures(features)
        batch_layer.updateExtents()
        return batch_layer

    def _batch_error_result(self, row, message):
        return {
            "nom_zone": row["nom_zone"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "ligne": row["ligne"],
            "statut": "ERREUR",
            "message": message[:180],
            "geometry": None,
            "nb_toits": 0,
            "surface_zone_ha": 0.0,
            "densite_toits_ha": 0.0,
            "menages_estimes": 0.0,
            "population_estimee": 0.0,
        }

    def _format_batch_summary(self):
        total = len(self.batch_results)
        ok = sum(1 for result in self.batch_results if result.get("statut") == "OK")
        errors = total - ok
        lines = [
            "Analyse par lots terminee.",
            "Zones traitees: {}".format(total),
            "OK: {} | Erreurs: {}".format(ok, errors),
            "",
            "nom_zone;statut;nb_toits;menages;population;message",
        ]
        for result in self.batch_results[:30]:
            lines.append(
                "{};{};{};{:.0f};{:.0f};{}".format(
                    result.get("nom_zone", ""),
                    result.get("statut", ""),
                    int(result.get("nb_toits", 0) or 0),
                    float(result.get("menages_estimes", 0.0) or 0.0),
                    float(result.get("population_estimee", 0.0) or 0.0),
                    result.get("message", ""),
                )
            )
        if total > 30:
            lines.append("... {} lignes supplementaires".format(total - 30))
        return "\n".join(lines)

    def exporter_batch_csv(self):
        if not self.batch_results:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Resultats absents",
                "Lancez d'abord une analyse par lots.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporter les resultats batch en CSV",
            os.path.expanduser("~/toit_analyzer_rdc_batch.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        columns = [
            "nom_zone",
            "latitude",
            "longitude",
            "ligne",
            "statut",
            "message",
            "nb_toits",
            "surface_zone_ha",
            "surface_zone_km2",
            "densite_toits_ha",
            "surface_batie_m2",
            "menages_estimes",
            "population_estimee",
            "invalides_ignores",
            "doublons_ignores",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for result in self.batch_results:
                writer.writerow([result.get(column, "") for column in columns])

        self._set_batch_result("Export batch termine:\n{}".format(path))

    # ------------------------------------------------------------------
    # Analyse par limites administratives
    # ------------------------------------------------------------------

    def charger_limites_admin(self):
        default_dir = os.path.join(self.plugin_dir, "cod_admin_boundaries.geojson (1)")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")

        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Charger des limites administratives",
            default_dir,
            "GeoJSON (*.geojson *.json);;Tous les fichiers (*.*)",
        )
        if not path:
            return

        layer = QgsVectorLayer(path, ADMIN_BOUNDARIES_LAYER_NAME, "ogr")
        if not layer.isValid():
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Limites invalides",
                "Impossible de lire le GeoJSON administratif:\n{}".format(path),
            )
            return

        geometry_type = QgsWkbTypes.geometryType(layer.wkbType())
        if geometry_type != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Geometrie non supportee",
                "Le fichier doit contenir des polygones administratifs.",
            )
            return

        self._remove_layer_by_name(ADMIN_BOUNDARIES_LAYER_NAME)
        QgsProject.instance().addMapLayer(layer)
        self.admin_layer = layer
        self._style_zone(layer)
        self._zoom_to_layer(layer)
        if hasattr(self.dlg, "label_admin_file"):
            self.dlg.label_admin_file.setText(os.path.basename(path))
        if hasattr(self.dlg, "label_admin_count"):
            self.dlg.label_admin_count.setText(
                "{} polygones charges".format(layer.featureCount())
            )
        self._set_admin_result(
            "{} polygones administratifs charges.\n"
            "Vous pouvez selectionner quelques polygones sur la carte ou analyser tout le fichier.".format(
                layer.featureCount()
            )
        )

    def analyser_admin_selection(self):
        layer = self._current_admin_layer()
        if layer is None:
            return
        selected = list(layer.selectedFeatures())
        if not selected:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Aucune selection",
                "Selectionnez un ou plusieurs polygones admin, ou utilisez l'analyse de tous les polygones.",
            )
            return
        self._analyser_admin_features(selected, "selection")

    def analyser_admin_tous(self):
        layer = self._current_admin_layer()
        if layer is None:
            return
        self._analyser_admin_features(list(layer.getFeatures()), "tous")

    def _current_admin_layer(self):
        try:
            valid = self.admin_layer is not None and self.admin_layer.isValid()
        except RuntimeError:
            valid = False
        if not valid:
            self.admin_layer = self._first_layer_by_name(ADMIN_BOUNDARIES_LAYER_NAME)
        if self.admin_layer is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Limites manquantes",
                "Chargez d'abord un fichier GeoJSON de limites administratives.",
            )
        return self.admin_layer

    def _analyser_admin_features(self, features, mode):
        if not features:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Aucun polygone",
                "Aucun polygone administratif a analyser.",
            )
            return

        local_gpkg = self._local_open_buildings_gpkg()
        if not os.path.exists(local_gpkg):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Base locale manquante",
                "L'analyse administrative necessite la base locale Open Buildings RDC.",
            )
            return

        self._remove_layer_by_name(ADMIN_RESULTS_LAYER_NAME)
        self._remove_layer_by_name(ADMIN_BUILDINGS_LAYER_NAME)
        self.admin_results = []
        results_layer = self._create_admin_results_layer()
        admin_buildings_layer = None
        min_confidence = float(self.dlg.input_confidence.value())
        menage_par_toit = float(self.dlg.input_menage_par_toit.value())
        pers_par_menage = float(self.dlg.input_personnes_menage.value())
        distance_area = self._distance_area()

        self._set_buttons_enabled(False)
        self._set_progress(0, max(len(features), 1))
        self._set_admin_result("Analyse administrative en cours ({})...".format(mode))
        QCoreApplication.processEvents()

        for index, feature in enumerate(features, start=1):
            admin_info = self._admin_feature_info(feature)
            label = admin_info["name"] or "Polygone {}".format(index)
            self._set_admin_result(
                "{} / {} - {}".format(index, len(features), label)
            )
            QCoreApplication.processEvents()
            try:
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    raise RuntimeError("Geometrie vide.")
                building_rows = self._open_buildings_rows_from_local(
                    geom, min_confidence
                )
                for building_row in building_rows:
                    building_row["zone_nom"] = label
                nb_toits = len(building_rows)
                surface_batie_m2 = sum(
                    float(row.get("area_m2_src", 0.0) or 0.0)
                    for row in building_rows
                )
                surface_zone_m2 = distance_area.measureArea(geom)
                surface_zone_ha = surface_zone_m2 / 10000.0 if surface_zone_m2 else 0.0
                densite = nb_toits / surface_zone_ha if surface_zone_ha else 0.0
                menages = nb_toits * menage_par_toit
                population = menages * pers_par_menage
                result = {
                    "statut": "OK",
                    "message": "Analyse terminee",
                    "nb_toits": nb_toits,
                    "surface_zone_m2": surface_zone_m2,
                    "surface_zone_ha": surface_zone_ha,
                    "surface_zone_km2": surface_zone_m2 / 1000000.0 if surface_zone_m2 else 0.0,
                    "densite_toits_ha": densite,
                    "surface_batie_m2": surface_batie_m2,
                    "menages_estimes": menages,
                    "population_estimee": population,
                    "geometry": geom,
                }
                result.update(admin_info)
                if building_rows:
                    admin_buildings_layer = self._append_admin_buildings_layer(
                        admin_buildings_layer, building_rows, admin_info
                    )
            except Exception as exc:
                result = {
                    "statut": "ERREUR",
                    "message": str(exc)[:180],
                    "nb_toits": 0,
                    "surface_zone_m2": 0.0,
                    "surface_zone_ha": 0.0,
                    "surface_zone_km2": 0.0,
                    "densite_toits_ha": 0.0,
                    "surface_batie_m2": 0.0,
                    "menages_estimes": 0.0,
                    "population_estimee": 0.0,
                    "geometry": feature.geometry(),
                }
                result.update(admin_info)

            self.admin_results.append(result)
            self._add_admin_result_feature(results_layer, result)
            self._set_progress(index, len(features))

        QgsProject.instance().addMapLayer(results_layer)
        self.admin_results_layer = results_layer
        self._style_admin_result(results_layer)
        if admin_buildings_layer is not None:
            QgsProject.instance().addMapLayer(admin_buildings_layer)
            self.admin_buildings_layer = admin_buildings_layer
            self._style_admin_buildings(admin_buildings_layer)
        self._zoom_to_layer(results_layer)
        self._set_buttons_enabled(True)
        self._set_progress(100, 100)
        summary = self._format_admin_summary()
        self._set_admin_result(summary)
        self._set_result(summary)

    def _admin_feature_info(self, feature):
        names = feature.fields().names()

        def attr(name, default=""):
            if name not in names:
                return default
            value = feature[name]
            return default if value is None else str(value)

        level = ""
        name = ""
        pcode = ""
        for candidate in ("adm3", "adm2", "adm1", "adm0"):
            candidate_name = attr("{}_name".format(candidate))
            candidate_pcode = attr("{}_pcode".format(candidate))
            if candidate_name or candidate_pcode:
                level = candidate
                name = candidate_name
                pcode = candidate_pcode
                break

        return {
            "level": level,
            "name": name,
            "pcode": pcode,
            "adm0_name": attr("adm0_name"),
            "adm0_pcode": attr("adm0_pcode"),
            "adm1_name": attr("adm1_name"),
            "adm1_pcode": attr("adm1_pcode"),
            "adm2_name": attr("adm2_name"),
            "adm2_pcode": attr("adm2_pcode"),
            "adm3_name": attr("adm3_name"),
            "adm3_pcode": attr("adm3_pcode"),
        }

    def _create_admin_results_layer(self):
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", ADMIN_RESULTS_LAYER_NAME, "memory")
        provider = layer.dataProvider()
        provider.addAttributes(
            [
                QgsField("niveau", QVariant.String),
                QgsField("nom", QVariant.String),
                QgsField("pcode", QVariant.String),
                QgsField("adm1_name", QVariant.String),
                QgsField("adm2_name", QVariant.String),
                QgsField("adm3_name", QVariant.String),
                QgsField("statut", QVariant.String),
                QgsField("message", QVariant.String),
                QgsField("nb_toits", QVariant.Int),
                QgsField("surface_ha", QVariant.Double),
                QgsField("surface_km2", QVariant.Double),
                QgsField("densite_ha", QVariant.Double),
                QgsField("surface_batie", QVariant.Double),
                QgsField("menages", QVariant.Double),
                QgsField("population", QVariant.Double),
            ]
        )
        layer.updateFields()
        return layer

    def _add_admin_result_feature(self, layer, result):
        feature = QgsFeature(layer.fields())
        geom = result.get("geometry")
        if geom is not None:
            feature.setGeometry(geom)
        feature.setAttributes(
            [
                result.get("level", ""),
                result.get("name", ""),
                result.get("pcode", ""),
                result.get("adm1_name", ""),
                result.get("adm2_name", ""),
                result.get("adm3_name", ""),
                result.get("statut", ""),
                result.get("message", ""),
                int(result.get("nb_toits", 0) or 0),
                float(result.get("surface_zone_ha", 0.0) or 0.0),
                float(result.get("surface_zone_km2", 0.0) or 0.0),
                float(result.get("densite_toits_ha", 0.0) or 0.0),
                float(result.get("surface_batie_m2", 0.0) or 0.0),
                float(result.get("menages_estimes", 0.0) or 0.0),
                float(result.get("population_estimee", 0.0) or 0.0),
            ]
        )
        layer.dataProvider().addFeature(feature)
        layer.updateExtents()

    def _append_admin_buildings_layer(self, layer, rows, admin_info):
        if layer is None:
            layer = QgsVectorLayer(
                "Point?crs=EPSG:4326", ADMIN_BUILDINGS_LAYER_NAME, "memory"
            )
            provider = layer.dataProvider()
            provider.addAttributes(
                [
                    QgsField("openb_id", QVariant.String),
                    QgsField("confidence", QVariant.Double),
                    QgsField("area_m2_src", QVariant.Double),
                    QgsField("plus_code", QVariant.String),
                    QgsField("admin_nom", QVariant.String),
                    QgsField("admin_pcode", QVariant.String),
                    QgsField("admin_niv", QVariant.String),
                ]
            )
            layer.updateFields()

        features = []
        for row in rows:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(
                QgsGeometry.fromPointXY(
                    QgsPointXY(float(row["longitude"]), float(row["latitude"]))
                )
            )
            feature.setAttributes(
                [
                    row.get("openb_id", ""),
                    float(row.get("confidence", 0.0) or 0.0),
                    float(row.get("area_m2_src", 0.0) or 0.0),
                    row.get("plus_code", ""),
                    admin_info.get("name", ""),
                    admin_info.get("pcode", ""),
                    admin_info.get("level", ""),
                ]
            )
            features.append(feature)

        layer.dataProvider().addFeatures(features)
        layer.updateExtents()
        return layer

    def _format_admin_summary(self):
        total = len(self.admin_results)
        ok = sum(1 for result in self.admin_results if result.get("statut") == "OK")
        errors = total - ok
        lines = [
            "Analyse administrative terminee.",
            "Polygones traites: {}".format(total),
            "OK: {} | Erreurs: {}".format(ok, errors),
            "",
            "{:<28} {:>12} {:>12} {:>14}".format(
                "Entite", "Toits", "Menages", "Population"
            ),
            "{:<28} {:>12} {:>12} {:>14}".format(
                "-" * 28, "-" * 12, "-" * 12, "-" * 14
            ),
        ]
        for result in self.admin_results[:30]:
            entity_name = result.get("name", "") or result.get("pcode", "") or "Sans nom"
            lines.append(
                "{:<28} {:>12,} {:>12,.0f} {:>14,.0f}".format(
                    entity_name[:28],
                    int(result.get("nb_toits", 0) or 0),
                    float(result.get("menages_estimes", 0.0) or 0.0),
                    float(result.get("population_estimee", 0.0) or 0.0),
                )
            )
        if total > 30:
            lines.append("... {} lignes supplementaires".format(total - 30))
        return "\n".join(lines)

    def exporter_admin_csv(self):
        if not self.admin_results:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Resultats absents",
                "Lancez d'abord une analyse administrative.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporter les resultats admin en CSV",
            os.path.expanduser("~/toit_analyzer_rdc_admin.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        columns = [
            "level",
            "name",
            "pcode",
            "adm0_name",
            "adm0_pcode",
            "adm1_name",
            "adm1_pcode",
            "adm2_name",
            "adm2_pcode",
            "adm3_name",
            "adm3_pcode",
            "statut",
            "message",
            "nb_toits",
            "surface_zone_ha",
            "surface_zone_km2",
            "densite_toits_ha",
            "surface_batie_m2",
            "menages_estimes",
            "population_estimee",
        ]
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for result in self.admin_results:
                writer.writerow([result.get(column, "") for column in columns])

        self._set_admin_result("Export admin termine:\n{}".format(path))

    def exporter_admin_xlsx(self):
        if not self.admin_results:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Resultats absents",
                "Lancez d'abord une analyse administrative.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporter le tableau admin en Excel",
            os.path.expanduser("~/toit_analyzer_rdc_admin.xlsx"),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        rows = [["Entite", "Toits", "Menages", "Population"]]
        for result in self.admin_results:
            rows.append(
                [
                    result.get("name", "") or result.get("pcode", "") or "Sans nom",
                    int(result.get("nb_toits", 0) or 0),
                    int(round(float(result.get("menages_estimes", 0.0) or 0.0))),
                    int(round(float(result.get("population_estimee", 0.0) or 0.0))),
                ]
            )

        self._write_simple_xlsx(path, "Resultats admin", rows)
        self._set_admin_result("Export Excel admin termine:\n{}".format(path))

    def _write_simple_xlsx(self, path, sheet_name, rows):
        sheet_name = sheet_name[:31] or "Feuille1"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._xlsx_content_types())
            archive.writestr("_rels/.rels", self._xlsx_root_rels())
            archive.writestr("docProps/core.xml", self._xlsx_core_props())
            archive.writestr("docProps/app.xml", self._xlsx_app_props())
            archive.writestr("xl/workbook.xml", self._xlsx_workbook(sheet_name))
            archive.writestr("xl/_rels/workbook.xml.rels", self._xlsx_workbook_rels())
            archive.writestr("xl/styles.xml", self._xlsx_styles())
            archive.writestr("xl/worksheets/sheet1.xml", self._xlsx_sheet(rows))

    def _xlsx_content_types(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        )

    def _xlsx_root_rels(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>"
        )

    def _xlsx_core_props(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:creator>Toit Analyzer RDC</dc:creator>"
            "<cp:lastModifiedBy>Toit Analyzer RDC</cp:lastModifiedBy>"
            "</cp:coreProperties>"
        )

    def _xlsx_app_props(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>Toit Analyzer RDC</Application>"
            "</Properties>"
        )

    def _xlsx_workbook(self, sheet_name):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<workbookPr/>"
            "<bookViews><workbookView activeTab=\"0\"/></bookViews>"
            "<sheets><sheet name=\"{}\" sheetId=\"1\" r:id=\"rId1\"/></sheets>"
            "</workbook>"
        ).format(escape(sheet_name))

    def _xlsx_workbook_rels(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        )

    def _xlsx_styles(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2">'
            '<font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
            '</fonts>'
            '<fills count="3">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF2F6F4E"/><bgColor indexed="64"/></patternFill></fill>'
            '</fills>'
            '<borders count="2">'
            '<border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border>'
            '</borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
            '<xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
            '</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        )

    def _xlsx_sheet(self, rows):
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            for col_index, value in enumerate(row, start=1):
                cell_ref = "{}{}".format(self._xlsx_column_name(col_index), row_index)
                style_id = 1 if row_index == 1 else (2 if isinstance(value, (int, float)) else 0)
                if isinstance(value, (int, float)):
                    cells.append(
                        '<c r="{ref}" s="{style}"><v>{value}</v></c>'.format(
                            ref=cell_ref, style=style_id, value=value
                        )
                    )
                else:
                    cells.append(
                        '<c r="{ref}" s="{style}" t="inlineStr"><is><t>{value}</t></is></c>'.format(
                            ref=cell_ref, style=style_id, value=escape(str(value))
                        )
                    )
            row_xml.append('<row r="{}">{}</row>'.format(row_index, "".join(cells)))

        dimension_ref = "A1:D{}".format(max(len(rows), 1))
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<dimension ref="{}"/>'
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
            '</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            '<cols><col min="1" max="1" width="32" customWidth="1"/>'
            '<col min="2" max="4" width="16" customWidth="1"/></cols>'
            '<sheetData>{}</sheetData>'
            '<autoFilter ref="A1:D{}"/>'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
            "</worksheet>"
        ).format(dimension_ref, "".join(row_xml), max(len(rows), 1))

    def _xlsx_column_name(self, index):
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def exporter_geopackage(self):
        if self._current_zone_layer() is None and self._current_buildings_layer() is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Rien a exporter",
                "Creez une analyse avant d'exporter.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporter l'analyse en GeoPackage",
            os.path.expanduser("~/toit_analyzer_rdc.gpkg"),
            "GeoPackage (*.gpkg)",
        )
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"

        layers = [
            (self._current_zone_layer(), "zone"),
            (self._current_buildings_layer(), "toits"),
            (self.counted_layer or self._first_layer_by_name(COUNTED_LAYER_NAME), "toits_comptes"),
        ]
        for layer, name in layers:
            if layer is not None:
                self._write_layer(layer, path, name)

        self._set_result("Export GeoPackage termine:\n{}".format(path))

    def _write_layer(self, layer, path, layer_name):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
            QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                path,
                QgsProject.instance().transformContext(),
                options,
            )
        else:
            QgsVectorFileWriter.writeAsVectorFormat(
                layer,
                path,
                "utf-8",
                layer.crs(),
                "GPKG",
                layerOptions=["OVERWRITE=YES", "GEOMETRY_NAME=geom"],
            )

    def exporter_csv(self):
        if not self.last_stats:
            zone_geom = self._zone_geometry()
            buildings_layer = self._current_buildings_layer()
            if zone_geom is None or buildings_layer is None:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Statistiques manquantes",
                    "Lancez d'abord le comptage.",
                )
                return
            self.last_stats, self.counted_layer = self._compute_statistics(
                zone_geom, buildings_layer
            )

        path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Exporter les statistiques en CSV",
            os.path.expanduser("~/toit_analyzer_rdc_stats.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["indicateur", "valeur"])
            for key, value in self.last_stats.items():
                writer.writerow([key, value])

        self._set_result("Export CSV termine:\n{}".format(path))
