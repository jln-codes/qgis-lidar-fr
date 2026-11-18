"""Mesure hors réseau : 100 tuiles synthétiques, extractions limitées à deux.

QT_QPA_PLATFORM=offscreen /usr/bin/python3 LiDAR/tests/benchmark_library.py
Le répertoire de résultats facultatif est le premier argument ; par défaut audits/.
Aucune donnée utilisateur n'est lue ; les GeoTIFF temporaires sont nettoyés.
"""
import json,sys,tempfile,time,statistics
from pathlib import Path
root=Path(__file__).resolve().parents[2];sys.path[:0]=[str(root/'LiDAR/tests'),str(root/'LiDAR')]
from test_plugin import APP,fake_tiff,X,Y
from lidar_hd_2026 import engine,rendering
from qgis.core import Qgis,QgsRasterLayer,QgsMapSettings,QgsMapRendererSequentialJob
from qgis.PyQt.QtCore import QSize
from unittest.mock import patch
output=Path(sys.argv[1]).absolute() if len(sys.argv)>1 else root/'audits/lidar-library-benchmark'
output.mkdir(parents=True,exist_ok=True)
results={'qgis':Qgis.QGIS_VERSION,'tiles':100,'selected':2,'runs':3,'network':'interdit','units':'seconds'}
with tempfile.TemporaryDirectory(prefix='benchmark-',dir=output) as tmp,patch.object(engine,'urlopen',side_effect=AssertionError('Réseau interdit')):
 work=Path(tmp);library=work/'LiDAR HD IGN 2026';library.mkdir();cache=work/'cache'
 blocks=[(X+256*(i%10),Y+256*(i//10)) for i in range(100)]
 for i,xy in enumerate(blocks):fake_tiff(engine.tile_path(library,xy),xy,value=20+i)
 t=time.perf_counter();global_result=engine.refresh_global_vrt(library);results['global_first']=time.perf_counter()-t
 rows=[]
 def render(path):
  t=time.perf_counter();layer=QgsRasterLayer(str(path),'test','gdal');assert layer.isValid()
  layer.setRenderer(rendering.create_altitude_renderer(layer))
  loaded=time.perf_counter()-t
  settings=QgsMapSettings();settings.setDestinationCrs(layer.crs());settings.setLayers([layer]);settings.setExtent(layer.extent());settings.setOutputSize(QSize(512,512))
  t=time.perf_counter();job=QgsMapRendererSequentialJob(settings);job.start();job.waitForFinished();elapsed=time.perf_counter()-t
  assert not job.renderedImage().isNull()
  return {'open_and_turbo':loaded,'render':elapsed}
 for i in range(3):
  t=time.perf_counter();plan=engine.diagnose(blocks[:2],cache,library,f'site{i}');diagnose=time.perf_counter()-t
  t=time.perf_counter();task=engine.LidarDownloadTask(plan);ok=task.run();elapsed=time.perf_counter()-t;assert ok,task.error
  assert task.copied==task.transferred==0 and not task.global_result.changed
  engine.TASKS.discard(task)
  row={'diagnostic':diagnose,'extraction_including_global_check':elapsed,'bytes_copied':task.copied,'individual_vrt_bytes':task.vrt.stat().st_size}
  row['restricted']=render(task.vrt);row['global_control']=render(global_result.vrt);rows.append(row)
 results['rows']=rows
 results['global_vrt_bytes']=global_result.vrt.stat().st_size
 results['library_geotiffs']=len(list(library.rglob('*.tif')))
 results['medians']={'diagnostic':statistics.median(r['diagnostic'] for r in rows),'extraction_including_global_check':statistics.median(r['extraction_including_global_check'] for r in rows),'restricted_open_and_turbo':statistics.median(r['restricted']['open_and_turbo'] for r in rows),'restricted_render':statistics.median(r['restricted']['render'] for r in rows),'global_render':statistics.median(r['global_control']['render'] for r in rows)}
(output/'performance.json').write_text(json.dumps(results,indent=2));print(json.dumps(results['medians']))
