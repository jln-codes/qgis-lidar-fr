"""Tests hors réseau avec PyQGIS réel et GeoTIFF synthétiques (aucune donnée métier).

QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s LiDAR/tests -v
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
from zipfile import ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from osgeo import gdal, osr
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsVectorLayer, QgsSingleBandPseudoColorRenderer,
                       QgsMapSettings, QgsMapRendererSequentialJob)
from qgis.gui import QgsMapCanvas, QgsMapToolPan
from qgis.PyQt.QtCore import QEventLoop, QPoint, QSize, QSettings, QThread, QTimer, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "LiDAR"), str(ROOT / "plugin")]
from lidar_hd_2026 import classFactory
from lidar_hd_2026 import config, engine, rendering
from lidar_hd_2026.dialog import LidarDialog
from lidar_hd_2026.selection import PolygonSelectionTool
from lidar_hd_2026.spatial import snapshot, grid_blocks_intersecting

APP = QgsApplication.instance() or QgsApplication([], True)
APP.initQgis()
PROJECT = QgsProject.instance()
X, Y = 640000, 6860032


def box(x=X, y=Y, size=20):
    return QgsGeometry.fromRect(QgsRectangle(x + 1, y + 1, x + size, y + size))


def layer(geometries=None, multi=False, crs="EPSG:2154", name="polygones"):
    result = QgsVectorLayer(f"{'MultiPolygon' if multi else 'Polygon'}?crs={crs}", name, "memory")
    features = []
    for geometry in geometries if geometries is not None else [box()]:
        feature = QgsFeature()
        geometry = QgsGeometry(geometry)
        if multi:
            geometry.convertToMultiType()
        feature.setGeometry(geometry)
        features.append(feature)
    assert result.dataProvider().addFeatures(features)[0]
    result.updateExtents()
    PROJECT.addMapLayer(result)
    return result


def fake_tiff(path, xy=(X, Y), value=42, dtype=gdal.GDT_Float32, nodata=-9999):
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 512, 512, 1, dtype)
    ds.SetGeoTransform((xy[0], 0.5, 0, xy[1] + 256, 0, -0.5))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2154)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.Fill(value)
    ds = None
    return path


class Interface:
    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2154"))
        self.pan = QgsMapToolPan(self.canvas)
        self.canvas.setMapTool(self.pan)
        self.messages = []
        self.active = None
        self.menus = []

    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def messageBar(self): return self
    def pushMessage(self, *args, **kwargs): self.messages.append((args, kwargs))
    def addPluginToMenu(self, title, action): self.menus.append((title, action))
    def removePluginMenu(self, title, action): self.menus.remove((title, action))
    def setActiveLayer(self, raster): self.active = raster
    def zoomToActiveLayer(self):
        self.canvas.setLayers([self.active])
        self.canvas.setExtent(self.active.extent())

    def close(self):
        self.canvas.setLayers([])
        self.canvas.close()
        self.window.close()
        self.canvas.deleteLater()
        self.window.deleteLater()


class PluginTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "audits").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="lidar-test-", dir=ROOT / "audits")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.cache = self.work / "cache"
        self.dest = self.work / "sortie"
        self.dest.mkdir()
        PROJECT.clear()
        self.addCleanup(PROJECT.clear)
        self.addCleanup(lambda: APP.processEvents())
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(self.work / "settings"))
        for target, value in (("CACHE_DIR", self.cache), ("POOL_ROOT", None),
                              ("REQUEST_PAUSE_SECONDS", 0), ("RETRY_DELAY_SECONDS", 0)):
            p = patch.object(config, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ("lidar_hd_2026.engine.urlopen", "socket.socket.connect"):
            p = patch(target, side_effect=AssertionError("Réseau interdit dans ce test"))
            p.start()
            self.addCleanup(p.stop)

    def plan(self, blocks=((X, Y),), name="test", mode="local", root=None):
        return engine.diagnose(blocks, self.cache, root or self.dest, name, mode)

    def task(self, plan=None, callback=None):
        task = engine.LidarDownloadTask(plan or self.plan(), callback)
        self.addCleanup(lambda: engine.TASKS.discard(task))
        return task

    def iface(self):
        iface = Interface()
        self.addCleanup(iface.close)
        return iface

    def dialog(self):
        dialog = LidarDialog(self.iface())
        self.addCleanup(dialog.shutdown)
        dialog.directory.setText(str(self.dest))
        return dialog

    def test_polygon_multipart_features_and_deduplication(self):
        a = box()
        b = box(X + 512, Y)
        multi = QgsGeometry.collectGeometry([a, b])
        source = layer([multi, multi], multi=True)
        originals = [f.geometry().asWkb() for f in source.getFeatures()]
        result = snapshot([(source, None)], 0)
        self.assertEqual((result.features, result.parts), (2, 4))
        self.assertEqual(result.blocks, ((X, Y), (X + 512, Y)))
        self.assertAlmostEqual(result.area_m2, 2 * 19 ** 2)
        self.assertEqual(originals, [f.geometry().asWkb() for f in source.getFeatures()])

    def test_buffer_0_25_100(self):
        source = layer()
        areas = []
        for buffer in (0, 25, 100):
            result = snapshot([(source, None)], buffer)
            areas.append(result.area_m2)
            self.assertEqual(len(result.blocks), 1 if buffer == 0 else 4)
        self.assertLess(areas[0], areas[1])
        self.assertLess(areas[1], areas[2])
        with self.assertRaises(ValueError): snapshot([(source, None)], 101)

    def test_buffer_after_transformation_and_invalid_geometry(self):
        geom = box()
        geom.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:2154"),
                                              QgsCoordinateReferenceSystem("EPSG:4326"), PROJECT))
        source = layer([geom], crs="EPSG:4326")
        self.assertAlmostEqual(snapshot([(source, None)], 0).area_m2, 361, places=1)
        self.assertGreater(snapshot([(source, None)], 25).area_m2, 3500)
        invalid = layer([QgsGeometry.fromWkt("POLYGON((0 0,1 1,0 1,1 0,0 0))")])
        with self.assertRaisesRegex(ValueError, "invalide"): snapshot([(invalid, None)], 0)

    def test_exact_intersection_avoids_bounding_box_and_hole(self):
        outer = f"{X} {Y},{X+768} {Y},{X+768} {Y+768},{X} {Y+768},{X} {Y}"
        hole = f"{X+250} {Y+250},{X+518} {Y+250},{X+518} {Y+518},{X+250} {Y+518},{X+250} {Y+250}"
        geom = QgsGeometry.fromWkt(f"POLYGON(({outer}),({hole}))")
        blocks = grid_blocks_intersecting([geom, geom])
        self.assertEqual(len(blocks), 8)
        self.assertNotIn((X+256, Y+256), blocks)

    def test_cache_integral_partial_and_corrupt(self):
        blocks = ((X, Y), (X + 256, Y))
        empty = self.plan(blocks)
        self.assertEqual((empty.cached, empty.download_count, empty.estimated_network), (0, 2, 2*1024**2))
        first = fake_tiff(engine.tile_path(self.cache, blocks[0]))
        partial = self.plan(blocks)
        self.assertEqual((partial.cached, partial.download_count), (1, 1))
        self.assertEqual(partial.mean_bytes, first.stat().st_size)
        fake_tiff(engine.tile_path(self.cache, blocks[1]), blocks[1])
        complete = self.plan(blocks)
        self.assertEqual((complete.cached, complete.download_count, complete.estimated_network), (2, 0, 0))
        self.assertGreater(complete.estimated_copy, 0)
        first.write_bytes(b"TIFF tronque")
        self.assertEqual(self.plan(blocks).download_count, 1)

    def test_validation_float32_nodata_grid_and_crs(self):
        path = self.work / "tile.tif"
        fake_tiff(path)
        self.assertTrue(engine.check_geotiff(path, (X, Y)))
        self.assertFalse(engine.check_geotiff(path, (X+256, Y)))
        fake_tiff(path, dtype=gdal.GDT_Int16)
        self.assertFalse(engine.check_geotiff(path))
        fake_tiff(path, nodata=0)
        self.assertFalse(engine.check_geotiff(path))
        fake_tiff(path)
        ds = gdal.Open(str(path), gdal.GA_Update)
        srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt()); ds = None
        self.assertFalse(engine.check_geotiff(path))

    def test_names_and_conflicts_no_overwrite(self):
        for name in ("", "../test", "a/b", "a\\b", "test.", "test ", "CON", "com1.txt", "x:y"):
            with self.subTest(name=name), self.assertRaises(ValueError): self.plan(name=name)
        self.assertEqual(self.plan(name="Relief été 2026").name, "Relief été 2026")
        (self.dest / "test").mkdir()
        with self.assertRaisesRegex(ValueError, "existe déjà"): self.plan()

    def test_vrt_disjoint_relative_moved_and_project_reopen(self):
        blocks = ((X, Y), (X + 768, Y + 512))
        for xy in blocks: fake_tiff(engine.tile_path(self.cache, xy), xy)
        task = self.task(self.plan(blocks, name="relief été"))
        self.assertTrue(task.run(), task.error)
        root = ET.parse(task.vrt)
        filenames = root.findall(".//SourceFilename")
        self.assertEqual(len(filenames), 2)
        self.assertTrue(all(n.get("relativeToVRT") == "1" and n.text.startswith("tiles/") for n in filenames))
        ds = gdal.Open(str(task.vrt))
        self.assertEqual((ds.RasterXSize, ds.RasterYSize), (2048, 1536))
        self.assertEqual(ds.GetRasterBand(1).ReadAsArray(600, 600, 1, 1)[0,0], -9999)
        ds = None
        raster = QgsRasterLayer(str(task.vrt), "relief", "gdal")
        raster.setRenderer(rendering.create_altitude_renderer(raster))
        PROJECT.addMapLayer(raster)
        PROJECT.setFilePathStorage(Qgis.FilePathType.Relative)
        self.assertTrue(PROJECT.write(str(task.vrt.parent / "projet.qgz")), PROJECT.error())
        PROJECT.clear(); raster = None
        moved = self.work / "déplacement"
        shutil.move(task.vrt.parent, moved)
        shutil.rmtree(self.cache)
        self.assertTrue(PROJECT.read(str(moved / "projet.qgz")))
        restored = next(iter(PROJECT.mapLayers().values()))
        self.assertTrue(restored.isValid())
        self.assertIsInstance(restored.renderer(), QgsSingleBandPseudoColorRenderer)

    def test_cancel_and_resume_keeps_valid_cache(self):
        blocks = ((X, Y), (X + 256, Y))
        task = self.task(self.plan(blocks))
        def first_download(x, y, dest, canceled):
            fake_tiff(dest, (x, y)); task.cancel(); return dest.stat().st_size
        with patch.object(engine, "download_block", side_effect=first_download):
            self.assertFalse(task.run())
        self.assertTrue(engine.check_geotiff(engine.tile_path(self.cache, blocks[0])))
        self.assertFalse(task.plan.extraction.exists())
        resumed = self.task(self.plan(blocks))
        self.assertEqual(resumed.plan.download_count, 1)
        def download(x, y, dest, canceled):
            fake_tiff(dest, (x,y)); return dest.stat().st_size
        with patch.object(engine, "download_block", side_effect=download) as mock:
            self.assertTrue(resumed.run(), resumed.error)
        self.assertEqual(mock.call_count, 1)
        self.assertFalse(list(self.cache.rglob("*.part")))

    def test_partial_download_and_empty_failure(self):
        blocks = ((X, Y), (X+256, Y))
        fake_tiff(engine.tile_path(self.cache, blocks[0]))
        task = self.task(self.plan(blocks))
        with patch.object(engine, "download_block", side_effect=RuntimeError("HTTP 503")):
            self.assertTrue(task.run(), task.error)
        self.assertEqual(len(task.failed), 1)
        self.assertTrue(task.vrt.is_file())
        empty = self.task(self.plan(((X+512, Y),), name="vide"))
        with patch.object(engine, "download_block", side_effect=RuntimeError("HTTP 503")):
            self.assertFalse(empty.run())
        self.assertIn("Aucun GeoTIFF", empty.error)

    def test_retry_temporary_http_and_atomic_download(self):
        response = fake_tiff(self.work / "response.tif").read_bytes()
        dest = engine.tile_path(self.cache, (X, Y))
        with patch.object(engine, "urlopen", side_effect=[HTTPError("IGN",503,"temporaire",None,None), io.BytesIO(response)]) as mock:
            count = engine.download_block(X, Y, dest, lambda: False)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(count, len(response))
        self.assertTrue(engine.check_geotiff(dest, (X,Y)))
        self.assertFalse(list(dest.parent.glob("*.part")))

    def test_non_retryable_http_invalid_response_and_cancel_cleanup(self):
        dest = engine.tile_path(self.cache, (X,Y))
        with patch.object(engine,"urlopen",side_effect=HTTPError("IGN",404,"absent",None,None)) as mock:
            with self.assertRaises(RuntimeError): engine.download_block(X,Y,dest,lambda:False)
            self.assertEqual(mock.call_count, 1)
        with patch.object(engine,"urlopen",return_value=io.BytesIO(b"<ServiceException/>")):
            with self.assertRaisesRegex(RuntimeError,"non GeoTIFF"): engine.download_block(X,Y,dest,lambda:False)
        with self.assertRaises(engine.DownloadCancelled): engine.download_block(X,Y,dest,lambda:True)
        self.assertFalse(dest.exists())
        self.assertFalse(list(dest.parent.glob("*.part")))

    def test_pool_unconfigured_inaccessible_and_reused(self):
        with self.assertRaisesRegex(ValueError,"pas configuré"): engine.storage_root("pool","",None)
        with self.assertRaisesRegex(ValueError,"inaccessible"): engine.storage_root("pool","",self.work/"absent")
        pool = self.work / "pool"; pool.mkdir()
        fake_tiff(engine.tile_path(pool,(X,Y)))
        plan = self.plan(mode="pool",root=pool)
        self.assertEqual((plan.cached,plan.pooled,plan.download_count,plan.estimated_copy),(0,1,0,0))
        task = self.task(plan)
        self.assertTrue(task.run(),task.error)
        self.assertEqual(task.transferred,0)
        shutil.rmtree(task.vrt.parent)
        self.assertTrue(engine.check_geotiff(engine.tile_path(pool,(X,Y))))

    def test_pool_copies_cache_and_detects_disconnection(self):
        pool = self.work / "pool"; pool.mkdir()
        fake_tiff(engine.tile_path(self.cache,(X,Y)))
        plan = self.plan(mode="pool",root=pool)
        self.assertEqual((plan.download_count,plan.pooled),(0,0))
        task = self.task(plan)
        self.assertTrue(task.run(),task.error)
        self.assertGreater(task.copied,0)
        filename=ET.parse(task.vrt).find(".//SourceFilename")
        self.assertEqual(filename.get("relativeToVRT"),"1")
        other=self.task(self.plan(name="autre",mode="pool",root=pool))
        shutil.rmtree(pool)
        self.assertFalse(other.run())
        self.assertIn("inaccessible",other.error)
        self.assertFalse(pool.exists())

    def test_two_instances_same_cache_no_duplicate_download(self):
        first=self.task(self.plan(name="premier"))
        second=self.task(self.plan(name="second"))
        calls=[]
        def download(x,y,dest,canceled):
            calls.append((x,y))
            fake_tiff(dest,(x,y))
            return dest.stat().st_size
        with patch.object(engine,"download_block",side_effect=download), ThreadPoolExecutor(2) as executor:
            results=list(executor.map(lambda task: task.run(),(first,second)))
        self.assertEqual(results,[True,True],(first.error,second.error))
        self.assertEqual(len(calls),1)
        self.assertFalse(list(self.cache.rglob("*.lock")))
        self.assertFalse(list(self.cache.rglob("*.part")))

    def test_two_processes_same_cache(self):
        import subprocess
        reference=fake_tiff(self.work/"reference.tif")
        code='''
import shutil,sys,time
from pathlib import Path
from qgis.PyQt.QtCore import QCoreApplication
from lidar_hd_2026 import config,engine
app=QCoreApplication([])
root,reference,name=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
config.REQUEST_PAUSE_SECONDS=0
plan=engine.diagnose(((640000,6860032),),root/'cache',root/'sortie',name)
(root/('ready-'+name)).touch()
deadline=time.monotonic()+15
while len(list(root.glob('ready-*')))<2:
 if time.monotonic()>deadline: raise RuntimeError('Autre instance absente')
 time.sleep(.01)
def download(x,y,dest,canceled):
 with (root/'downloads').open('ab') as log: log.write(b'IGN simule\\n')
 time.sleep(.2)
 shutil.copyfile(reference,dest)
 return dest.stat().st_size
engine.download_block=download
task=engine.LidarDownloadTask(plan)
success=task.run()
assert success,task.error
task.finished(success)
'''
        env={**os.environ,"PYTHONPATH":str(ROOT/"LiDAR"),"QT_QPA_PLATFORM":"offscreen"}
        processes=[subprocess.Popen([sys.executable,"-c",code,str(self.work),str(reference),name],
                                    cwd=self.work,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                   for name in ("processus1","processus2")]
        try:
            for process in processes:
                _,error=process.communicate(timeout=40)
                self.assertEqual(process.returncode,0,error.decode(errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.communicate()
        self.assertEqual((self.work/"downloads").read_bytes().splitlines(),[b"IGN simule"])
        self.assertTrue(engine.check_geotiff(engine.tile_path(self.cache,(X,Y))))

    def test_extraction_reservation_race_does_not_overwrite(self):
        fake_tiff(engine.tile_path(self.cache,(X,Y)))
        plan=self.plan()
        first=self.task(plan); second=self.task(plan)
        self.assertTrue(first.run(),first.error)
        before=first.vrt.read_bytes()
        self.assertFalse(second.run())
        self.assertEqual(first.vrt.read_bytes(),before)

    def test_selection_overlaps_multiple_layers_toggle_right_escape(self):
        a=layer(name="A"); b=layer(name="B")
        iface=self.iface(); iface.canvas.setLayers([a,b])
        dialog=LidarDialog(iface); self.addCleanup(dialog.shutdown)
        dialog.mode.setCurrentIndex(1); dialog.directory.setText(str(self.dest))
        dialog.show(); dialog.start_capture()
        tool=dialog.capture
        point=QgsPointXY(X+5,Y+5)
        matches=tool.candidates(point)
        self.assertEqual({l.id() for l,fid in matches},{a.id(),b.id()})
        self.assertFalse(dialog.isVisible())
        event=SimpleNamespace(button=lambda:Qt.LeftButton,mapPoint=lambda:point,pos=lambda:QPoint(0,0))
        with patch.object(tool,"choose",return_value=matches[0]) as chooser:
            tool.canvasReleaseEvent(event); self.assertEqual(len(tool.selected),1)
            chooser.assert_called_once()
        tool.toggle(*matches[1]); self.assertEqual(len(tool.selected),2)
        tool.toggle(*matches[0]); self.assertEqual(len(tool.selected),1)
        tool.canvasReleaseEvent(SimpleNamespace(button=lambda:Qt.RightButton))
        self.assertIsNone(dialog.capture)
        self.assertTrue(dialog.isVisible())
        self.assertIs(iface.canvas.mapTool(),iface.pan)
        self.assertEqual(a.selectedFeatureCount()+b.selectedFeatureCount(),0)
        before=set(dialog.selected)
        dialog.start_capture(); dialog.capture.toggle(*matches[0])
        dialog.capture.keyPressEvent(SimpleNamespace(key=lambda:Qt.Key_Escape))
        self.assertEqual(dialog.selected,before)
        self.assertIs(iface.canvas.mapTool(),iface.pan)

    def test_selection_external_tool_change_restores_dialog(self):
        source=layer(); iface=self.iface(); iface.canvas.setLayers([source])
        dialog=LidarDialog(iface); self.addCleanup(dialog.shutdown)
        dialog.start_capture(); iface.canvas.setMapTool(iface.pan)
        self.assertIsNone(dialog.capture)
        self.assertTrue(dialog.isVisible())
        self.assertIs(iface.canvas.mapTool(),iface.pan)

    def test_diagnostic_updates_parameters_selection_and_geometry(self):
        source=layer(); dialog=self.dialog()
        dialog.layer.setLayer(source); dialog.buffer.setValue(0); dialog.refresh_diagnostic()
        self.assertTrue(dialog.extract.isEnabled())
        self.assertEqual(len(dialog.plan.blocks),1)
        self.assertIn("Prédiagnostic sans réseau",dialog.diagnostic.text())
        self.assertIn("1.00 Mio",dialog.diagnostic.text())
        dialog.buffer.setValue(25)
        self.assertFalse(dialog.extract.isEnabled())
        dialog.refresh_diagnostic(); self.assertEqual(len(dialog.plan.blocks),4)
        dialog.mode.setCurrentIndex(1); dialog.refresh_diagnostic()
        self.assertFalse(dialog.extract.isEnabled())
        dialog.selected={(source.id(),next(source.getFeatures()).id())}; dialog.refresh_diagnostic()
        self.assertTrue(dialog.extract.isEnabled())
        dialog.name.setText("CON"); dialog.refresh_diagnostic()
        self.assertFalse(dialog.extract.isEnabled())
        self.assertFalse(dialog.storage.model().item(1).isEnabled())
        dialog.name.setText("autre"); dialog.refresh_diagnostic()
        source.startEditing(); fid=next(source.getFeatures()).id()
        source.changeGeometry(fid,box(X+1024,Y))
        self.assertFalse(dialog.extract.isEnabled())
        source.rollBack()

    def test_all_features_ignores_existing_qgis_selection(self):
        source=layer([box(),box(X+512,Y)])
        source.selectByIds([next(source.getFeatures()).id()])
        dialog=self.dialog(); dialog.layer.setLayer(source); dialog.buffer.setValue(0)
        dialog.refresh_diagnostic(); self.assertEqual(len(dialog.plan.blocks),2)

    def test_source_layer_removed_with_open_dialog(self):
        source=layer(); dialog=self.dialog()
        dialog.layer.setLayer(source); dialog.refresh_diagnostic()
        self.assertTrue(dialog.extract.isEnabled())
        PROJECT.removeMapLayer(source.id())
        APP.processEvents()
        dialog.refresh_diagnostic()
        self.assertFalse(dialog.extract.isEnabled())

    def test_task_manager_real_worker_gui_callback_and_turbo_fallback(self):
        source=layer(); fake_tiff(engine.tile_path(self.cache,(X,Y)))
        dialog=self.dialog(); dialog.layer.setLayer(source); dialog.buffer.setValue(0)
        callback_threads=[]
        original=dialog.task_finished
        loop=QEventLoop()
        def finished(task,success):
            callback_threads.append(QThread.currentThread() is APP.thread())
            original(task,success); loop.quit()
        dialog.task_finished=finished
        with patch("lidar_hd_2026.dialog.create_altitude_renderer",side_effect=RuntimeError("Turbo indisponible")):
            dialog.start_extraction()
            QTimer.singleShot(10000,loop.quit)
            loop.exec_()
        self.assertEqual(callback_threads,[True])
        self.assertIsNone(dialog.task)
        self.assertTrue(dialog.iface.active.isValid())
        self.assertTrue(any("Échec Turbo" in str(message) for message in dialog.iface.messages))

    def test_toolbar_both_plugins_order_unload_reload(self):
        from sig_prestations import classFactory as sigFactory
        for order in ((classFactory,sigFactory),(sigFactory,classFactory)):
            for unload_first in (0,1):
                with self.subTest(order=order,unload_first=unload_first):
                    iface=self.iface()
                    plugins=[factory(iface) for factory in order]
                    for plugin in plugins:
                        plugin.initGui(); plugin.initGui()
                    bars=iface.window.findChildren(QToolBar,"SIGMilieuxToolbar")
                    self.assertEqual(len(bars),1)
                    self.assertEqual(len(bars[0].actions()),2)
                    plugins[unload_first].unload()
                    self.assertEqual(len(bars[0].actions()),1)
                    self.assertIs(plugins[1-unload_first].toolbar,bars[0])
                    plugins[1-unload_first].unload()
                    self.assertEqual(iface.window.findChildren(QToolBar,"SIGMilieuxToolbar"),[])
                    reloaded=classFactory(iface); reloaded.initGui()
                    self.assertEqual(len(reloaded.toolbar.actions()),1)
                    reloaded.unload(); self.assertEqual(iface.menus,[])

    def test_unload_during_capture_and_task(self):
        plugin=classFactory(self.iface()); plugin.initGui(); plugin.show_dialog()
        dialog=plugin.dialog
        dialog.start_capture(); plugin.unload()
        self.assertIsNone(dialog.capture)
        self.assertIs(plugin.iface.canvas.mapTool(),plugin.iface.pan)
        dialog=LidarDialog(plugin.iface)
        task=self.task(); dialog.task=task; task.callback=dialog.task_finished
        dialog.shutdown()
        self.assertTrue(task.isCanceled()); self.assertIsNone(task.callback)

    def test_renderer_compatibility_334_340_344_simulated_and_native(self):
        path=fake_tiff(self.work/"raster.tif")
        raster=QgsRasterLayer(str(path),"test","gdal")
        self.assertIsInstance(rendering.create_altitude_renderer(raster),QgsSingleBandPseudoColorRenderer)
        if not hasattr(Qgis,"RasterRangeLimit"):
            # Sur 3.34/3.40, le rendu ci-dessus vérifie leurs véritables API.
            # Les façades des trois familles sont simulées sur le runtime 3.44.
            return
        native_shader=rendering.QgsColorRampShader
        native_origin=rendering.QgsRasterMinMaxOrigin
        # Façades des anciens noms, sur le runtime 3.44 : simulation explicite.
        class OldShader:
            Interpolated=Qgis.ShaderInterpolationMethod.Linear
            Continuous=Qgis.ShaderClassificationMethod.Continuous
            def __new__(cls,*args): return native_shader(*args)
        class OldOrigin:
            MinMax=Qgis.RasterRangeLimit.MinimumMaximum
            UpdatedCanvas=Qgis.RasterRangeExtent.UpdatedCanvas
            Estimated=Qgis.RasterRangeAccuracy.Estimated
            def __new__(cls,*args): return native_origin(*args)
        for version in (334,340,344):
            qgis=SimpleNamespace()
            if version>=340:
                qgis.ShaderInterpolationMethod=Qgis.ShaderInterpolationMethod
                qgis.ShaderClassificationMethod=Qgis.ShaderClassificationMethod
            if version>=344:
                qgis.RasterRangeLimit=Qgis.RasterRangeLimit
                qgis.RasterRangeExtent=Qgis.RasterRangeExtent
                qgis.RasterRangeAccuracy=Qgis.RasterRangeAccuracy
            with self.subTest(simulated=version), patch.object(rendering,"Qgis",qgis), \
                 patch.object(rendering,"QgsColorRampShader",OldShader), \
                 patch.object(rendering,"QgsRasterMinMaxOrigin",OldOrigin):
                renderer=rendering.create_altitude_renderer(raster)
                self.assertEqual(renderer.minMaxOrigin().extent(),Qgis.RasterRangeExtent.UpdatedCanvas)
                self.assertEqual(renderer.minMaxOrigin().limits(),Qgis.RasterRangeLimit.MinimumMaximum)
                self.assertEqual(renderer.minMaxOrigin().statAccuracy(),Qgis.RasterRangeAccuracy.Estimated)
                self.assertEqual(renderer.shader().rasterShaderFunction().colorRampType(),Qgis.ShaderInterpolationMethod.Linear)
        renderer=rendering.create_altitude_renderer(raster)
        self.assertIsInstance(renderer,QgsSingleBandPseudoColorRenderer)

    def test_native_turbo_render_actual_pixels_updated_extent(self):
        from array import array
        path=fake_tiff(self.work/"gradient.tif")
        ds=gdal.Open(str(path),gdal.GA_Update)
        values=array("f",(float(x+20) for x in range(512)))*512
        ds.GetRasterBand(1).WriteRaster(0,0,512,512,values.tobytes())
        ds=None
        raster=QgsRasterLayer(str(path),"gradient","gdal")
        raster.setRenderer(rendering.create_altitude_renderer(raster))
        settings=QgsMapSettings()
        settings.setDestinationCrs(raster.crs())
        settings.setLayers([raster]); settings.setExtent(raster.extent())
        settings.setOutputSize(QSize(256,256))
        job=QgsMapRendererSequentialJob(settings)
        job.start(); job.waitForFinished()
        image=job.renderedImage()
        colors={image.pixelColor(x,128).name() for x in range(5,250)}
        self.assertGreater(len(colors),100,"Le rendu doit utiliser les altitudes de la vue et une rampe continue")
        self.assertTrue(any(image.pixelColor(x,128).red()!=image.pixelColor(x,128).blue()
                            for x in range(5,250)))

    def test_icon_svg_png_sizes_transparency_color_order(self):
        svg=ROOT/"LiDAR/lidar_hd_2026/icons/lidar_hd.svg"
        xml=ET.parse(svg)
        self.assertEqual(xml.getroot().get("viewBox"),"0 0 64 64")
        renderer=QSvgRenderer(str(svg)); self.assertTrue(renderer.isValid())
        self.assertFalse(xml.findall(".//{http://www.w3.org/2000/svg}image"), "Le SVG doit être vectoriel natif")
        for size in (16,24,32,128):
            image=QImage(size,size,QImage.Format_ARGB32); image.fill(Qt.transparent)
            painter=QPainter(image); renderer.render(painter); painter.end()
            self.assertEqual(image.pixelColor(0,0).alpha(),0)
            scanner = image.pixelColor(size//2,round(11*size/64))
            self.assertLess(scanner.red(),80)
            self.assertGreater(scanner.blue(),scanner.red())
            self.assertEqual(image.pixelColor(round(15*size/64),round(20*size/64)).name(),"#ffffff")
            # Le scanner et les trois vagues restent distincts à 24 pixels.
            if size >= 24:
                pixels=[image.pixelColor(x,y) for x in range(size//4,3*size//4)
                        for y in range(3*size//5,7*size//8)]
                self.assertTrue(any(c.blue()>c.red()+50 and c.blue()>c.green()+20 for c in pixels))
                self.assertTrue(any(c.red()>120 and c.green()>70 and c.blue()<60 for c in pixels))
                self.assertTrue(any(c.red()>c.green()+60 and c.red()>c.blue()+60 for c in pixels))
            if size==128:
                self.assertEqual(image,QImage(str(svg.with_suffix(".png"))))

    def test_package_reproducible_matches_sources_and_installed_import(self):
        spec=importlib.util.spec_from_file_location("lidar_builder",ROOT/"LiDAR/build_plugin.py")
        builder=importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        self.assertEqual(builder.DIST,ROOT/"plugin/dist")
        archive=builder.build(self.work)
        content=archive.read_bytes(); self.assertEqual(content,builder.build(self.work).read_bytes())
        with ZipFile(archive) as z:
            self.assertIn("lidar_hd_2026/icons/lidar_hd.svg",z.namelist())
            self.assertIn("lidar_hd_2026/icons/lidar_hd.png",z.namelist())
            self.assertEqual(set(z.namelist()),{"lidar_hd_2026/"+p for p in builder.FILES})
            for name in builder.FILES:
                self.assertEqual(z.read("lidar_hd_2026/"+name),(builder.PACKAGE/name).read_bytes())
            installed=self.work/"installed"; z.extractall(installed)
        # Vérification dans un processus neuf, uniquement depuis le ZIP extrait.
        import subprocess
        code = '''
import sys
from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QMainWindow
from qgis.utils import findPlugins
app=QgsApplication([], True); app.initQgis()
sys.path.insert(0,sys.argv[1])
assert list(findPlugins(sys.argv[1]))[0][0]=='lidar_hd_2026'
from lidar_hd_2026 import classFactory
class Iface:
 def __init__(self): self.w=QMainWindow()
 def mainWindow(self): return self.w
 def addPluginToMenu(self,*args): pass
 def removePluginMenu(self,*args): pass
p=classFactory(Iface()); p.initGui(); assert not p.action.icon().isNull(); p.unload()
'''
        subprocess.run([sys.executable,"-c",code,str(installed)],cwd=self.work,check=True,
                       env={**os.environ,"QT_QPA_PLATFORM":"offscreen"},capture_output=True,timeout=30)


if __name__=="__main__":
    unittest.main(verbosity=2)
