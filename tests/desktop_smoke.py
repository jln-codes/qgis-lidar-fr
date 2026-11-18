"""Recette automatisée dans QGIS Desktop réel, via --code (voir LiDAR/README.md).

Ce fichier est chargé par QgisApp, pas par unittest. Aucun appel IGN autorisé.
Sources installées depuis les ZIP ; clics Qt réels ; sorties dans audits/.
"""
import json
import configparser
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from osgeo import gdal, osr
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsFeature,
                       QgsGeometry, QgsProject, QgsRasterLayer, QgsRectangle,
                       QgsSingleBandPseudoColorRenderer, QgsVectorLayer)
from qgis.PyQt.QtCore import QPoint, QTimer, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QToolBar
from qgis.utils import iface

ROOT = Path(os.environ["LIDAR_PROJECT_ROOT"])
OUTPUT = Path(os.environ.get("LIDAR_SMOKE_OUTPUT", str(ROOT / "audits/lidar-desktop")))
OUTPUT.mkdir(parents=True, exist_ok=True)
WORK = tempfile.TemporaryDirectory(prefix="run-", dir=OUTPUT)
WORK_PATH = Path(WORK.name)
RESULT = {"qgis": Qgis.QGIS_VERSION, "runtime": "QGIS Desktop / QgisApp / " + os.environ.get("QT_QPA_PLATFORM", "native"),
          "network": "interdit dans le moteur", "ok": False}
PLUGINS = []
X, Y = 640000, 6860032


def record(phase):
    RESULT["phase"] = phase
    (OUTPUT / "result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")


def stop(error=None):
    if error:
        RESULT["error"] = error
    (OUTPUT / "result.json").write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
    for plugin in reversed(PLUGINS):
        plugin.unload()
    PLUGINS.clear()
    QgsProject.instance().clear()
    QgsProject.instance().setDirty(False)
    WORK.cleanup()
    QgsApplication.exit(1 if error else 0)


def guarded(callback):
    def run():
        try:
            callback()
        except Exception:
            stop(traceback.format_exc())
    return run


def create_inputs():
    from lidar_hd_2026 import config, engine
    config.CACHE_DIR = WORK_PATH / "cache"
    config.POOL_ROOT = None
    config.REQUEST_PAUSE_SECONDS = 0
    def no_network(*args, **kwargs):
        raise AssertionError("Réseau IGN interdit")
    engine.urlopen = no_network
    layers = []
    for index, x in enumerate((X, X + 512)):
        layer = QgsVectorLayer("Polygon?crs=EPSG:2154", f"Emprise {index + 1}", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(x + 10, Y + 10, x + 220, Y + 220)))
        layer.dataProvider().addFeatures([feature]); layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        layers.append(layer)
        path = engine.tile_path(config.CACHE_DIR, (x, Y)); path.parent.mkdir(parents=True, exist_ok=True)
        ds = gdal.GetDriverByName("GTiff").Create(str(path), 512, 512, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((x, .5, 0, Y + 256, 0, -.5))
        srs = osr.SpatialReference(); srs.ImportFromEPSG(2154); ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(-9999)
        # Secteurs d'altitudes distinctes, sans lecture d'un cache réel.
        ds.GetRasterBand(1).Fill(20 + index * 50); ds = None
    return layers


def assert_no_lidar_menu():
    assert not any(a.text().replace("&", "") == "SIG-Milieux"
                   for a in iface.pluginMenu().actions()), "Sous-menu Extensions résiduel"


def start():
    record("installation ZIP")
    installed = WORK_PATH / "plugins"; installed.mkdir()
    archives = []
    for name, folder in (("lidar_hd_2026", "LiDAR"), ("sig_prestations", "plugin")):
        metadata = configparser.ConfigParser(interpolation=None)
        metadata.read(ROOT / folder / name / "metadata.txt", encoding="utf-8")
        default=ROOT / f"plugin/dist/{name}-{metadata['general']['version']}.zip"
        override="LIDAR_SMOKE_SIG_ZIP" if name=="sig_prestations" else "LIDAR_SMOKE_LIDAR_ZIP"
        archives.append(Path(os.environ.get(override,str(default))))
    for archive in archives:
        with ZipFile(archive) as z:
            z.extractall(installed)
    sys.path.insert(0, str(installed))
    from lidar_hd_2026 import classFactory as lidar_factory
    from sig_prestations import classFactory as sig_factory
    import lidar_hd_2026, sig_prestations
    RESULT["installed_packages"] = {}
    for name, package in (("lidar_hd_2026",lidar_hd_2026),("sig_prestations",sig_prestations)):
        metadata=configparser.ConfigParser(interpolation=None)
        metadata.read(Path(package.__file__).with_name("metadata.txt"),encoding="utf-8")
        RESULT["installed_packages"][name]={"version":metadata["general"]["version"],"path":package.__file__}
    lidar = lidar_factory(iface); sig = sig_factory(iface)
    PLUGINS.extend([sig, lidar])
    sig.initGui(); lidar.initGui(); lidar.initGui()
    assert_no_lidar_menu()
    toolbar = iface.mainWindow().findChild(QToolBar, "SIGMilieuxToolbar")
    iface.mainWindow().resize(1280, 900)
    QTest.qWait(100)
    RESULT["toolbar_geometry"] = [toolbar.width(),toolbar.height()]
    RESULT["toolbar_buttons"] = [{"visible":toolbar.widgetForAction(a).isVisible(),
                                 "style":int(toolbar.widgetForAction(a).toolButtonStyle()),
                                 "text":a.text()} for a in toolbar.actions()]
    record("vérification de la barre")
    assert len(toolbar.actions()) == 2
    assert toolbar.isVisible()
    assert toolbar.toolButtonStyle() == Qt.ToolButtonIconOnly
    assert all(toolbar.widgetForAction(a).isVisible() and
               toolbar.widgetForAction(a).toolButtonStyle()==Qt.ToolButtonIconOnly for a in toolbar.actions())
    toolbar.hide(); toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    sig.unload(); assert len(toolbar.actions()) == 1
    PLUGINS.remove(sig)
    sig = sig_factory(iface); sig.initGui(); PLUGINS.insert(0, sig)
    assert len(toolbar.actions()) == 2
    assert toolbar.isVisible() and toolbar.toolButtonStyle()==Qt.ToolButtonIconOnly
    RESULT["toolbar_load_unload"] = True
    lidar.unload(); PLUGINS.remove(lidar)
    assert_no_lidar_menu()
    lidar=lidar_factory(iface); lidar.initGui(); PLUGINS.append(lidar)
    assert_no_lidar_menu()
    assert len(toolbar.actions()) == 2
    RESULT["extensions_menu_absent_after_reload"] = True
    record("barre commune validée")
    layers = create_inputs()
    iface.mainWindow().resize(1280, 900)
    toolbar.grab().save(str(OUTPUT/"toolbar.png"))
    canvas = iface.mapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
    canvas.setLayers(layers)
    canvas.setExtent(QgsRectangle(X - 60, Y - 60, X + 790, Y + 330))
    iface.actionPan().trigger()
    canvas.refresh()
    lidar.action.trigger()
    dialog = lidar.dialog
    dialog.buffer.setValue(0)
    library=WORK_PATH/"LiDAR HD IGN 2026";library.mkdir()
    dialog.directory.setText(str(library))
    dialog.name.setText("desktop_relief")
    dialog.mode.setCurrentIndex(1)
    dialog.refresh_diagnostic()
    def select():
        dialog.select_button.click()
        assert not dialog.isVisible()
        for x in (X + 70, X + 582):
            point = canvas.getCoordinateTransform().transform(x, Y + 70)
            QTest.mouseClick(canvas.viewport(), Qt.LeftButton, pos=QPoint(round(point.x()), round(point.y())))
        QTest.mouseClick(canvas.viewport(), Qt.RightButton, pos=QPoint(20, 20))
        assert dialog.isVisible() and len(dialog.selected) == 2
        assert dialog.plan is not None and dialog.plan.download_count == 0
        assert dialog.plan.cached == 2 and dialog.extract.isEnabled()
        RESULT["map_click_selection"] = True
        record("sélection cartographique validée")
        QTest.qWait(300)
        assert dialog.diagnostic.height() >= dialog.diagnostic.fontMetrics().height()*8
        dialog.grab().save(str(OUTPUT / "prediagnostic.png"))
        dialog.extract.click()
        QTimer.singleShot(100, guarded(lambda: wait_for_task(lidar, layers)))
    QTimer.singleShot(1500, guarded(select))


def wait_for_task(plugin, sources):
    # Le chargement du raster peut traiter des événements Qt avant le retour
    # du callback ; laisser aussi s'exécuter la fermeture différée.
    if plugin.dialog.task is not None or plugin.dialog.isVisible():
        QTimer.singleShot(100, guarded(lambda: wait_for_task(plugin, sources)))
        return
    raster = iface.activeLayer()
    assert isinstance(raster, QgsRasterLayer) and raster.isValid(), plugin.dialog.diagnostic.text()
    assert isinstance(raster.renderer(), QgsSingleBandPseudoColorRenderer)
    assert raster.renderer().minMaxOrigin().extent() == Qgis.RasterRangeExtent.UpdatedCanvas
    path = Path(raster.source())
    assert all(n.get("relativeToVRT") == "1" for n in ET.parse(path).findall(".//SourceFilename"))
    assert path.parent.name == "extractions"
    assert all(n.text.startswith("../tiles/") for n in ET.parse(path).findall(".//SourceFilename"))
    global_vrt=path.parent.parent/"mnt_lidar_global.vrt"
    assert global_vrt.is_file()
    assert all(n.get("relativeToVRT")=="1" and n.text.startswith("tiles/")
               for n in ET.parse(global_vrt).findall(".//SourceFilename"))
    assert len(ET.parse(global_vrt).findall(".//SourceFilename"))==2
    assert not any(isinstance(l,QgsRasterLayer) and Path(l.source())==global_vrt
                   for l in QgsProject.instance().mapLayers().values())
    RESULT["library_global_not_loaded"] = True
    RESULT["extract_task_turbo"] = True
    record("extraction et Turbo validés")
    assert not plugin.dialog.isVisible(), plugin.dialog.diagnostic.text()
    RESULT["automatic_close_cached_extraction"] = True
    plugin.action.trigger()
    assert plugin.dialog.isVisible() and plugin.dialog.progress.value() == 0
    assert_no_lidar_menu()
    RESULT["reopen_zero_progress"] = True
    plugin.dialog.close()
    # Le canevas a été configuré explicitement au démarrage de ce test :
    # isoler maintenant le raster pour contrôler son véritable rendu.
    iface.mapCanvas().setLayers([raster])
    iface.mapCanvas().refreshAllLayers()
    def reopen():
        iface.mainWindow().grab().save(str(OUTPUT / "raster.png"))
        project = QgsProject.instance()
        for source in sources:
            project.removeMapLayer(source.id())
        record("sources retirées")
        project.setFilePathStorage(Qgis.FilePathType.Relative)
        project_file = path.parent / "projet.qgz"
        record("sauvegarde du projet")
        assert project.write(str(project_file)), project.error()
        record("projet sauvegardé")
        iface.mapCanvas().stopRendering()
        iface.mapCanvas().setLayers([])
        project.clear()
        record("projet vidé")
        shutil.rmtree(WORK_PATH / "cache")
        assert project.read(str(project_file)), project.error()
        record("projet rouvert")
        assert all(layer.isValid() for layer in project.mapLayers().values())
        RESULT["project_reopen_without_cache"] = True
        RESULT["ok"] = True
        stop()
    QTimer.singleShot(1200, guarded(reopen))


record("recette chargée")
QTimer.singleShot(45000, lambda: stop("Délai de recette dépassé"))
QTimer.singleShot(2000, guarded(start))
