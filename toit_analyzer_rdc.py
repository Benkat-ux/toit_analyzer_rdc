# -*- coding: utf-8 -*-
"""
Toit Analyzer RDC

Plugin QGIS d'analyse rapide des toitures pour la planification energetique.
"""

import csv
import gzip
import io
import json
import os
import re
import urllib.request

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
        self.dlg.btn_analyser.clicked.connect(self.analyser_zone)
        self.dlg.btn_editer.clicked.connect(self.activer_edition_toits)
        self.dlg.btn_stop_editer.clicked.connect(self.arreter_edition_toits)
        self.dlg.btn_export_gpkg.clicked.connect(self.exporter_geopackage)
        self.dlg.btn_export_csv.clicked.connect(self.exporter_csv)

    def _set_default_values(self):
        self.dlg.input_latitude.setText("-4.325")
        self.dlg.input_longitude.setText("15.322")
        self._set_result("Pret. Saisir une zone ou choisir un point sur la carte.")

    def _set_result(self, text):
        if self.dlg is not None and hasattr(self.dlg, "label_resultat"):
            self.dlg.label_resultat.setPlainText(text)

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
            self.dlg.btn_analyser,
            self.dlg.btn_export_gpkg,
            self.dlg.btn_export_csv,
        ):
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

        for row in rows:
            geom = QgsGeometry.fromPointXY(
                QgsPointXY(row["longitude"], row["latitude"])
            )
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geom)
            feature.setAttributes(
                [
                    row["openb_id"],
                    row["confidence"],
                    row["area_m2_src"],
                    row["plus_code"],
                    "Google Open Buildings",
                ]
            )
            provider.addFeature(feature)

        layer.updateExtents()
        return layer

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

    def _compute_statistics(self, zone_geom, buildings_layer):
        self._remove_layer_by_name(COUNTED_LAYER_NAME)
        geometry_type = QgsWkbTypes.geometryType(buildings_layer.wkbType())
        if geometry_type == QgsWkbTypes.PointGeometry:
            counted_layer = QgsVectorLayer(
                "Point?crs=EPSG:4326", COUNTED_LAYER_NAME, "memory"
            )
        else:
            counted_layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326", COUNTED_LAYER_NAME, "memory"
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
