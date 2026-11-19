"""Tests QGIS 3.44 hors réseau, sur le cache existant (aucun téléchargement).

Exécuter avec le Python de QGIS, depuis la racine du dépôt :
  QT_QPA_PLATFORM=offscreen /usr/bin/python3 test_lidar_ign.py
LIDAR_IGN_CACHE permet de choisir un autre cache local. Sorties temporaires
sous audits/, supprimées à la fin. Les mesures sont imprimées en JSON.
"""
import ast
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['GDAL_PAM_ENABLED'] = 'NO'  # Pas de fichiers statistiques .aux.xml.
from osgeo import gdal
from qgis.core import (
    Qgis, QgsApplication, QgsColorRampShader, QgsDataProvider, QgsGeometry,
    QgsMapRendererSequentialJob, QgsMapSettings, QgsProject, QgsProviderRegistry,
    QgsRasterLayer, QgsRasterShader, QgsRectangle,
    QgsRenderedLayerStatistics, QgsSingleBandPseudoColorRenderer, QgsStyle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEventLoop, QTimer

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'lidar_ign.py'
CACHE = Path(os.environ.get('LIDAR_IGN_CACHE', Path.home() / 'qgis_lidar_ign_cache'))
EMPRISE = ROOT / 'emprise.gpkg'
APP = QgsApplication.instance() or QgsApplication([], True)
APP.initQgis()
gdal.UseExceptions()
gdal.SetConfigOption('GDAL_PAM_ENABLED', 'NO')


def load_script():
    # Le script reste directement exécutable dans l'éditeur QGIS. Seul son
    # appel final est écarté ici, pour pouvoir tester les fonctions hors ligne.
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'), str(SCRIPT))
    assert ast.unparse(tree.body[-1]) == 'main()'
    tree.body.pop()
    namespace = {'__file__': str(SCRIPT)}
    exec(compile(tree, str(SCRIPT), 'exec'), namespace)
    return namespace


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    return {str(p): digest(p) for p in [EMPRISE, EMPRISE.with_name('emprise_brute.gpkg'),
                                       *sorted(CACHE.rglob('*'))] if p.is_file()}


def timed(callback):
    start = time.perf_counter()
    result = callback()
    return result, 1000 * (time.perf_counter() - start)


def bounds(renderer):
    return renderer.classificationMin(), renderer.classificationMax()


def refresh(canvas, extent):
    """Rendu réel et surveillance de la boucle Qt, sans waitForFinished bloquant."""
    loop = QEventLoop()
    completed = []
    ticks = [time.perf_counter()]
    heartbeat = QTimer()
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: ticks.append(time.perf_counter()))
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def done():
        completed.append(True)
        loop.quit()

    canvas.mapCanvasRefreshed.connect(done)
    start = time.perf_counter()
    canvas.setExtent(extent)
    heartbeat.start()
    deadline.start(30000)
    canvas.refresh()
    loop.exec()
    elapsed = 1000 * (time.perf_counter() - start)
    ticks.append(time.perf_counter())
    heartbeat.stop()
    deadline.stop()
    canvas.mapCanvasRefreshed.disconnect(done)
    if not completed:
        canvas.stopRendering()
        raise AssertionError('Rendu non terminé en 30 s')
    # QgsMapCanvas seul ne synchronise pas la légende : QgisApp le fait dans
    # renderComplete. Lire les statistiques effectivement utilisées par le
    # rendu, pas les bornes du renderer original (copié par le pipeline).
    statistics = []
    for item in canvas.renderedItemResults().renderedItems():
        stat = sip.cast(item, QgsRenderedLayerStatistics)
        statistics.append((stat.minimum(), stat.maximum()))
    return {
        'elapsed_ms': elapsed,
        'qt_max_gap_ms': 1000 * max(b - a for a, b in zip(ticks, ticks[1:])),
        'qt_ticks': len(ticks) - 2,
        'statistics': statistics,
    }


class LidarSymbology(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = load_script()
        cls.before = snapshot()
        cls.temp = tempfile.TemporaryDirectory(prefix='lidar-qgis-01-', dir=ROOT / 'audits')
        cls.work = Path(cls.temp.name)
        cls.emprise = QgsVectorLayer(str(EMPRISE) + '|layername=emprise', 'emprise', 'ogr')
        assert cls.emprise.isValid()
        # Read tile inputs only; VRTs and diagnostic logs belong to the temporary test folder.
        original_path = cls.api['output_path']
        cls.api['CACHE_DIR'] = cls.work
        cls.api['output_path'] = lambda x, y: CACHE / 'tiles' / original_path(x, y).name
        expected = cls.api['grid_blocks_intersecting'](cls.api['get_polygons_l93'](cls.emprise))
        available = [xy for xy in expected if cls.api['check_geotiff'](cls.api['output_path'](*xy))]
        cls.tiles = sorted((CACHE / 'tiles').glob('mnt_l93_*_256m_512px.tif'))
        assert cls.tiles, 'Aucune tuile locale ; aucun téléchargement autorisé'
        assert all(cls.api['check_geotiff'](p) for p in cls.tiles)
        cls.blocks = [(int(p.stem.split('_')[2]), int(p.stem.split('_')[3])) for p in cls.tiles]
        cls.coverage = {'expected': len(expected), 'available': len(available)}
        cls.addClassCleanup(cls.temp.cleanup)
        if available:
            cls.footprint_vrt = cls.work / 'emprise_disponible.vrt'
            ds = gdal.BuildVRT(str(cls.footprint_vrt), [str(cls.api['output_path'](*xy)) for xy in available],
                               options=gdal.BuildVRTOptions(resolution='highest', srcNodata=-9999, VRTNodata=-9999))
            assert ds is not None
            ds = None
        cls.vrt = cls.work / 'emprise.vrt'
        ds = gdal.BuildVRT(str(cls.vrt), [str(p) for p in cls.tiles],
                           options=gdal.BuildVRTOptions(resolution='highest', srcNodata=-9999, VRTNodata=-9999))
        assert ds is not None
        ds = None
        cls.metrics = {'qgis': Qgis.QGIS_VERSION, 'tiles': len(cls.tiles), 'emprise_coverage': cls.coverage}

    @classmethod
    def tearDownClass(cls):
        QgsProject.instance().clear()
        cls.emprise = None
        cls.temp.cleanup()
        assert snapshot() == cls.before, 'Le cache ou emprise.gpkg a changé'
        print('\nMESURES ' + json.dumps(cls.metrics, ensure_ascii=False, indent=2), flush=True)

    def raster(self):
        raster = QgsRasterLayer(str(self.vrt), 'MNT test', 'gdal')
        self.assertTrue(raster.isValid())
        return raster

    def assert_renderer(self, renderer):
        self.assertIsInstance(renderer, QgsSingleBandPseudoColorRenderer)
        self.assertEqual(renderer.inputBand(), 1)
        self.assertIsInstance(renderer.shader(), QgsRasterShader)
        shader = renderer.shader().rasterShaderFunction()
        self.assertIsInstance(shader, QgsColorRampShader)
        self.assertEqual(shader.colorRampType(), Qgis.ShaderInterpolationMethod.Linear)
        self.assertEqual(shader.classificationMode(), Qgis.ShaderClassificationMethod.Continuous)
        self.assertGreater(len(shader.colorRampItemList()), 2)
        turbo = QgsStyle.defaultStyle().colorRamp('Turbo')
        self.assertEqual(shader.sourceColorRamp().properties(), turbo.properties())
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            self.assertEqual(shader.sourceColorRamp().color(fraction), turbo.color(fraction))
        origin = renderer.minMaxOrigin()
        self.assertEqual(origin.limits(), Qgis.RasterRangeLimit.MinimumMaximum)
        self.assertEqual(origin.extent(), Qgis.RasterRangeExtent.UpdatedCanvas)
        self.assertEqual(origin.statAccuracy(), Qgis.RasterRangeAccuracy.Estimated)

    def test_01_native_renderer_and_style_roundtrip(self):
        raster = self.raster()
        # Interdire toute statistique lors de la création du renderer.
        with patch.object(type(raster.dataProvider()), 'bandStatistics', side_effect=AssertionError('Statistique explicite')):
            renderer = self.api['create_altitude_renderer'](raster)
        raster.setRenderer(renderer)
        self.assert_renderer(renderer)
        qml = self.work / 'turbo.qml'
        _, ok = raster.saveNamedStyle(str(qml))
        self.assertTrue(ok)
        restored = self.raster()
        _, ok = restored.loadNamedStyle(str(qml))
        self.assertTrue(ok)
        self.assert_renderer(restored.renderer())
        if self.coverage['available']:
            footprint = QgsRasterLayer(str(self.footprint_vrt), 'Emprise disponible', 'gdal')
            self.assertTrue(footprint.isValid())
            footprint.setRenderer(self.api['create_altitude_renderer'](footprint))
            self.assert_renderer(footprint.renderer())

    def test_02_float32_nodata_and_altitudes(self):
        ds = gdal.Open(str(self.vrt))
        band = ds.GetRasterBand(1)
        self.assertEqual(band.DataType, gdal.GDT_Float32)
        self.assertEqual(band.GetNoDataValue(), -9999)
        self.assertEqual(ds.GetGeoTransform()[1::4], (0.5, -0.5))
        raster = self.raster()
        raster.setRenderer(self.api['create_altitude_renderer'](raster))
        for tile in self.tiles:
            src = gdal.Open(str(tile))
            self.assertEqual(src.GetRasterBand(1).GetNoDataValue(), -9999)
            gt, st = ds.GetGeoTransform(), src.GetGeoTransform()
            x, y = round((st[0] - gt[0]) / gt[1]), round((st[3] - gt[3]) / gt[5])
            # Comparaison binaire de TOUS les pixels de chacune des tuiles.
            self.assertEqual(band.ReadRaster(x, y, src.RasterXSize, src.RasterYSize), src.GetRasterBand(1).ReadRaster())
        self.assertEqual(raster.dataProvider().dataType(1), Qgis.DataType.Float32)
        self.assertEqual(raster.dataProvider().sourceNoDataValue(1), -9999)
        self.metrics['vrt_pixels'] = ds.RasterXSize * ds.RasterYSize

    def test_03_canvas_adapts_and_performance(self):
        measurements = {}
        # Représenter l'ancien script par son rendu gris par défaut, sans
        # conserver de copie historique ni lancer de statistiques exhaustives.
        for mode in ('before_gray', 'after_turbo'):
            raster, opening = timed(self.raster)
            create = apply = 0
            if mode == 'after_turbo':
                renderer, create = timed(lambda: self.api['create_altitude_renderer'](raster))
                _, apply = timed(lambda: raster.setRenderer(renderer))
            canvas = QgsMapCanvas()
            canvas.resize(800, 600)
            canvas.setDestinationCrs(raster.crs())
            canvas.setLayers([raster])
            canvas.setCachingEnabled(False)
            canvas.setPreviewJobsEnabled(False)
            try:
                full = refresh(canvas, raster.extent())
                first = gdal.Open(str(self.tiles[0])).GetGeoTransform()
                last = gdal.Open(str(self.tiles[-1])).GetGeoTransform()
                views = [
                    QgsRectangle(first[0], first[3] - 256, first[0] + 256, first[3]),
                    QgsRectangle(last[0], last[3] - 256, last[0] + 256, last[3]),
                ]
                zoom = refresh(canvas, views[0])
                pan = refresh(canvas, views[1])
                if mode == 'after_turbo':
                    self.assert_renderer(raster.renderer())
                    for result in (full, zoom, pan):
                        self.assertEqual(len(result['statistics']), 1)
                        minimum, maximum = result['statistics'][0]
                        self.assertEqual(len(minimum), 1)
                        self.assertTrue(math.isfinite(minimum[0]))
                        self.assertGreater(maximum[0], minimum[0])
                    self.assertNotEqual(zoom['statistics'], pan['statistics'])
                    # Vérifier que les statistiques utilisées sont celles de
                    # la VUE, avec le même échantillonnage natif QGIS.
                    stats = raster.dataProvider().bandStatistics(
                        1, Qgis.RasterBandStatistics(Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max),
                        canvas.extent(), int(QgsRasterLayer.SAMPLE_SIZE))
                    self.assertEqual(pan['statistics'][0], ([stats.minimumValue], [stats.maximumValue]))
                    # Le rendu adaptatif doit être identique à la même rampe
                    # native classée avec les min/max mesurés de cette vue.
                    # Cela vérifie les couleurs produites, pas seulement les
                    # métadonnées statistiques rapportées par QGIS.
                    settings = QgsMapSettings(canvas.mapSettings())
                    adaptive_job = QgsMapRendererSequentialJob(settings)
                    adaptive_job.start()
                    adaptive_job.waitForFinished()
                    reference = raster.clone()
                    ref_renderer = reference.renderer()
                    ref_renderer.setClassificationMin(stats.minimumValue)
                    ref_renderer.setClassificationMax(stats.maximumValue)
                    ref_renderer.shader().rasterShaderFunction().classifyColorRamp()
                    origin = ref_renderer.minMaxOrigin()
                    origin.setExtent(Qgis.RasterRangeExtent.FixedCanvas)
                    ref_renderer.setMinMaxOrigin(origin)
                    settings.setLayers([reference])
                    fixed_job = QgsMapRendererSequentialJob(settings)
                    fixed_job.start()
                    fixed_job.waitForFinished()
                    self.assertEqual(adaptive_job.renderedImage(), fixed_job.renderedImage())
                measurements[mode] = {'open_ms': opening, 'create_ms': create, 'apply_ms': apply,
                                      'full': full, 'zoom': zoom, 'pan': pan}
            finally:
                canvas.setLayers([])
                canvas.close()
                sip.delete(canvas)
        self.metrics['comparison'] = measurements
        # Mesure diagnostique séparée, bornée comme le calcul initial natif.
        # Fournisseur neuf sans le renderer gris : le constructeur d'une
        # QgsRasterLayer peut déjà avoir mis ses statistiques en mémoire.
        diagnostic = self.work / 'stats.vrt'
        ds = gdal.BuildVRT(str(diagnostic), [str(p) for p in self.tiles],
                           options=gdal.BuildVRTOptions(srcNodata=-9999, VRTNodata=-9999))
        ds = None
        provider = QgsProviderRegistry.instance().createProvider(
            'gdal', str(diagnostic), QgsDataProvider.ProviderOptions())
        stats, elapsed = timed(lambda: provider.bandStatistics(
            1, Qgis.RasterBandStatistics(Qgis.RasterBandStatistic.Min | Qgis.RasterBandStatistic.Max),
            provider.extent(), int(QgsRasterLayer.SAMPLE_SIZE)))
        self.metrics['estimated_minmax_ms'] = elapsed
        self.metrics['estimated_sample_size'] = int(QgsRasterLayer.SAMPLE_SIZE)
        self.metrics['explicit_initial_statistics_in_renderer'] = 0

    def test_04_main_cache_selection_and_changed_footprint(self):
        project = QgsProject.instance()
        project.clear()
        # Contrôle de l'emprise réelle avec garde stricte avant tout appel réseau.
        project.addMapLayer(self.emprise)
        if self.coverage['available'] < self.coverage['expected']:
            with patch.dict(self.api, MAX_NEW_BLOCKS=0):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, 'Sécurité'):
                        self.api['main']()
            project.removeMapLayer(self.emprise.id())
        # Deux objets MultiPolygon construits en mémoire à partir de blocs déjà
        # présents : tester main() et la sélection sans changer le GeoPackage.
        from qgis.core import QgsFeature
        layer = QgsVectorLayer('MultiPolygon?crs=EPSG:2154', 'emprise', 'memory')
        for x, y in (self.blocks[0], self.blocks[-1]):
            feature = QgsFeature()
            geom = QgsGeometry.fromRect(QgsRectangle(x + 1, y + 1, x + 255, y + 255))
            geom.convertToMultiType()
            feature.setGeometry(geom)
            layer.dataProvider().addFeatures([feature])
        # Emprise et sources ne sont jamais écrites : seuls les VRT sont créés
        # dans audits/. Le chemin des tuiles continue à viser le cache réel.
        cache_output_path = self.api['output_path']
        previous_cache = self.api['CACHE_DIR']
        canvas = QgsMapCanvas()
        active = []

        class Iface:
            def setActiveLayer(self, raster):
                active[:] = [raster]
            def zoomToActiveLayer(self):
                canvas.setDestinationCrs(active[0].crs())
                canvas.setLayers(active)
                canvas.setExtent(active[0].extent())
            def mapCanvas(self):
                return canvas
            def messageBar(self):
                return type("Messages", (), {"pushMessage": lambda *args, **kwargs: None})()

        self.api.update(CACHE_DIR=self.work, iface=Iface(), output_path=lambda x, y: CACHE / 'tiles' / cache_output_path(x, y).name)
        def complete_main():
            # main() schedules QgsTask; only its GUI callback adds and styles the raster.
            self.api['main']()
            pending = list(self.api['_LIDAR_IGN_TASK_REFS'])
            deadline = time.monotonic() + 90
            while self.api['_LIDAR_IGN_TASK_REFS'] and time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.01)
            self.assertFalse(self.api['_LIDAR_IGN_TASK_REFS'], 'Tâche LiDAR non terminée')
            self.assertTrue(pending, 'Aucune tâche créée')
            for task in pending:
                self.assertFalse(task.error, task.error)
                self.assertFalse(task.isCanceled())
            APP.processEvents()

        try:
            with patch.dict(self.api, download_block=unittest.mock.Mock(side_effect=AssertionError('Téléchargement interdit'))):
                with contextlib.redirect_stdout(io.StringIO()):
                    if self.coverage['available'] == self.coverage['expected']:
                        complete_main()
                        self.assert_renderer(active[0].renderer())
                        real_vrt = gdal.Open(active[0].source())
                        self.assertEqual(len(real_vrt.GetFileList()) - 1, self.coverage['expected'])
                        real_vrt = None
                        project.removeMapLayer(self.emprise.id())
                    project.addMapLayer(layer)
                    complete_main()
                    first_source = active[0].source()
                    self.assert_renderer(active[0].renderer())
                    complete_main()
                    self.assertEqual(active[0].source(), first_source)
                    # Une sélection spatiale différente doit changer le VRT.
                    layer.selectByIds([next(layer.getFeatures()).id()])
                    complete_main()
                    self.assertNotEqual(active[0].source(), first_source)
                self.api['download_block'].assert_not_called()
            # Vérifier aussi Polygon (les deux géométries sont acceptées).
            project.removeMapLayer(layer.id())
            polygon = QgsVectorLayer('Polygon?crs=EPSG:2154', 'emprise', 'memory')
            feature = QgsFeature()
            x, y = self.blocks[-1]
            feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(x + 1, y + 1, x + 255, y + 255)))
            polygon.dataProvider().addFeatures([feature])
            project.addMapLayer(polygon)
            with patch.dict(self.api, download_block=unittest.mock.Mock(side_effect=AssertionError('Téléchargement interdit'))):
                with contextlib.redirect_stdout(io.StringIO()):
                    complete_main()
                self.api['download_block'].assert_not_called()
            outputs = [l for l in project.mapLayers().values() if isinstance(l, QgsRasterLayer)]
            self.assertEqual(len(outputs), 1)
            self.assert_renderer(outputs[0].renderer())
            self.assertTrue(Path(first_source).is_file())
        finally:
            canvas.setLayers([])
            canvas.close()
            sip.delete(canvas)
            project.clear()
            self.api['CACHE_DIR'] = previous_cache
            self.api['output_path'] = cache_output_path


if __name__ == '__main__':
    # Gardes réseau Python, y compris en cas de cache incomplet. Ne jamais
    # charger LiDAR.qgz : il contient aussi un fond OSM distant.
    (ROOT / 'audits').mkdir(exist_ok=True)
    with patch('socket.socket.connect', side_effect=AssertionError('Réseau interdit')), \
         patch('urllib.request.urlopen', side_effect=AssertionError('Réseau interdit')):
        unittest.main(verbosity=2)
