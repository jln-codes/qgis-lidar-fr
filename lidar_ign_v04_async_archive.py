"""MNT LiDAR HD IGN — chargement sélectif depuis une couche polygonale QGIS.

Exécution : ouvrir le script dans l'éditeur Python intégré de QGIS et cliquer
sur « Exécuter ». Une couche vectorielle nommée « emprise » doit être chargée.

Ce script n'altère pas les données sources. Les GeoTIFF et le VRT sont créés
sur disque et sont réutilisables entre sessions QGIS.
"""

import hashlib
import traceback
import math
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsTask,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsRectangle,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsWkbTypes,
)

# ------------------------ PARAMÈTRES MODIFIABLES ------------------------
EMPRISE_LAYER_NAME = "emprise"      # Nom de la couche dans le panneau QGIS
USE_SELECTION_IF_PRESENT = True     # Si sélection : seules les entités sélectionnées
BUFFER_M = 0                        # Tampon autour des polygones, en mètres
BLOCK_M = 256                       # Alignement identique à digues-app
PIXELS = 512                        # Résolution = 256 / 512 = 0,5 m/pixel
MAX_NEW_BLOCKS = 200                # Sécurité : limite de nouveaux téléchargements
MAX_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 2
REQUEST_PAUSE_SECONDS = 0.3
CACHE_DIR = Path.home() / "qgis_lidar_ign_cache"
WMS_URL = "https://data.geopf.fr/wms-r/wms"
WMS_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
OUTPUT_LAYER_NAME = "MNT LiDAR HD IGN — emprise (VRT)"
VRT_PREFIX = "mnt_lidar_ign_emprise_v03_"
TASK_DESCRIPTION = "IGN LiDAR HD — téléchargement des blocs"
# -----------------------------------------------------------------------


def find_emprise_layer():
    candidates = [
        lyr for lyr in QgsProject.instance().mapLayersByName(EMPRISE_LAYER_NAME)
        if lyr.isValid() and hasattr(lyr, "getFeatures")
    ]
    if not candidates:
        raise RuntimeError(
            "Couche 'emprise' introuvable dans QGIS. "
            "Charge emprise.gpkg et vérifie le nom affiché de sa couche."
        )
    return candidates[0]


def get_polygons_l93(layer):
    if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
        raise RuntimeError("La couche 'emprise' doit être Polygon ou MultiPolygon.")
    if not layer.crs().isValid():
        raise RuntimeError("Le SCR de la couche 'emprise' est invalide.")

    target_crs = QgsCoordinateReferenceSystem("EPSG:2154")
    transformer = QgsCoordinateTransform(layer.crs(), target_crs, QgsProject.instance())
    selected = USE_SELECTION_IF_PRESENT and layer.selectedFeatureCount() > 0
    iterator = layer.getSelectedFeatures() if selected else layer.getFeatures()

    geometries = []
    for feature in iterator:
        geom = QgsGeometry(feature.geometry())
        if geom.isNull() or geom.isEmpty():
            continue
        if not geom.isGeosValid():
            print(f"ATTENTION : géométrie non valide pour l'entité {feature.id()} — ignorée")
            continue
        geom.transform(transformer)
        if BUFFER_M:
            geom = geom.buffer(BUFFER_M, 8)
        if not geom.isEmpty():
            geometries.append(geom)

    if not geometries:
        raise RuntimeError("Aucun polygone valide dans la couche ou la sélection.")
    print(f"Géométries utilisées : {len(geometries)} "
          f"({'sélection' if selected else 'toutes les entités'})")
    return geometries


def grid_blocks_intersecting(geometries):
    blocks = set()
    for geom in geometries:
        bounds = geom.boundingBox()
        ix_min = math.floor(bounds.xMinimum() / BLOCK_M)
        ix_max = math.ceil(bounds.xMaximum() / BLOCK_M)
        iy_min = math.floor(bounds.yMinimum() / BLOCK_M)
        iy_max = math.ceil(bounds.yMaximum() / BLOCK_M)
        for ix in range(ix_min, ix_max):
            for iy in range(iy_min, iy_max):
                xmin, ymin = ix * BLOCK_M, iy * BLOCK_M
                block = QgsGeometry.fromRect(
                    QgsRectangle(xmin, ymin, xmin + BLOCK_M, ymin + BLOCK_M)
                )
                if geom.intersects(block):
                    blocks.add((xmin, ymin))
    return sorted(blocks)


def output_path(xmin, ymin):
    return CACHE_DIR / "tiles" / f"mnt_l93_{xmin}_{ymin}_{BLOCK_M}m_{PIXELS}px.tif"


def check_geotiff(path):
    if not path.is_file():
        return False
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        return False
    ok = (ds.RasterXSize == PIXELS and ds.RasterYSize == PIXELS
          and ds.RasterCount == 1
          and ds.GetRasterBand(1).DataType == gdal.GDT_Float32)
    ds = None
    return ok


class DownloadCancelled(Exception):
    """Annulation volontaire, distincte d'une erreur du serveur."""


def interruptible_pause(seconds, is_canceled):
    """Attend sans bloquer l'interface et permet d'annuler les délais."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_canceled():
            raise DownloadCancelled()
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def download_block(xmin, ymin, dest, is_canceled):
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.3.0",
        "REQUEST": "GetMap",
        "LAYERS": WMS_LAYER,
        "STYLES": "normal",
        "CRS": "EPSG:2154",
        "BBOX": f"{xmin},{ymin},{xmin + BLOCK_M},{ymin + BLOCK_M}",
        "WIDTH": PIXELS,
        "HEIGHT": PIXELS,
        "FORMAT": "image/geotiff",
        "EXCEPTIONS": "text/xml",
    }
    url = WMS_URL + "?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "QGIS-LiDAR-IGN-prototype/0.1"})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if is_canceled():
            raise DownloadCancelled()
        try:
            with urlopen(request, timeout=90) as response:
                payload = response.read()
            if payload[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                excerpt = payload[:500].decode("utf-8", errors="replace")
                raise RuntimeError(f"Réponse IGN non TIFF : {excerpt}")
            if is_canceled():
                raise DownloadCancelled()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            try:
                tmp.write_bytes(payload)
                if is_canceled():
                    raise DownloadCancelled()
                if not check_geotiff(tmp):
                    raise RuntimeError(f"GeoTIFF invalide ou non Float32 : {xmin},{ymin}")
                tmp.replace(dest)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return len(payload)
        except HTTPError as err:
            retryable = err.code in {429, 500, 502, 503, 504}
            message = f"HTTP {err.code}: {err.reason}"
        except (URLError, TimeoutError, ConnectionError) as err:
            retryable = True
            message = str(err)
        except RuntimeError as err:
            retryable = False
            message = str(err)
        if not retryable or attempt == MAX_ATTEMPTS:
            raise RuntimeError(f"Bloc {xmin},{ymin} : {message}")
        delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
        print(f"    Échec tentative {attempt}/{MAX_ATTEMPTS} ({message}) ; "
              f"nouvel essai dans {delay:.0f} s")
        interruptible_pause(delay, is_canceled)


def create_altitude_renderer(raster):
    """Rendu natif Turbo, reclassé par QGIS sur chaque nouvelle vue."""
    ramp = QgsStyle.defaultStyle().colorRamp("Turbo")
    if ramp is None:
        raise RuntimeError("Palette Turbo introuvable dans le style par défaut QGIS.")

    # Bornes provisoires uniquement : aucune lecture des pixels ici. Au premier
    # rendu, UpdatedCanvas calcule les min/max estimés de la vue et reclasse la
    # rampe. Ne pas appeler bandStatistics(1) : sampleSize=0 lirait tout le VRT.
    color_shader = QgsColorRampShader(
        0.0, 1.0, ramp,
        Qgis.ShaderInterpolationMethod.Linear,
        Qgis.ShaderClassificationMethod.Continuous,
    )
    color_shader.classifyColorRamp()
    shader = QgsRasterShader(0.0, 1.0)
    shader.setRasterShaderFunction(color_shader)
    renderer = QgsSingleBandPseudoColorRenderer(raster.dataProvider(), 1, shader)
    renderer.setClassificationMin(0.0)
    renderer.setClassificationMax(1.0)

    origin = QgsRasterMinMaxOrigin()
    origin.setLimits(Qgis.RasterRangeLimit.MinimumMaximum)
    origin.setExtent(Qgis.RasterRangeExtent.UpdatedCanvas)
    origin.setStatAccuracy(Qgis.RasterRangeAccuracy.Estimated)
    renderer.setMinMaxOrigin(origin)
    return renderer


def build_and_show_vrt(blocks, transferred, failed, elapsed_download):
    """Appel uniquement depuis le thread principal QGIS, après la tâche réseau."""
    started = time.monotonic()
    paths = [output_path(x, y) for x, y in blocks]
    available_paths = [path for path in paths if check_geotiff(path)]
    if not available_paths:
        raise RuntimeError("Aucun GeoTIFF valide à assembler.")
    if len(available_paths) != len(paths):
        print(f"ATTENTION : VRT incomplet ({len(available_paths)}/{len(paths)} blocs).")
        print("Relancer le script pour récupérer les blocs manquants.")
    for x, y, reason in failed:
        print(f"  Manquant : {x},{y} — {reason}")

    # Le VRT est propre à la liste actuelle des blocs. Son nom change lorsque
    # les polygones (et donc les blocs) changent : QGIS/GDAL ne peut pas
    # conserver en mémoire l'emprise d'un VRT précédemment chargé sous le
    # même chemin. Ne jamais inclure toutes les tuiles du cache dans ce VRT.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    signature = "\n".join([
        WMS_URL, WMS_LAYER, str(BLOCK_M), str(PIXELS),
        *[f"{x},{y}" for (x, y), path in zip(blocks, paths)
          if path in available_paths],
    ])
    fingerprint = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    vrt_path = CACHE_DIR / f"{VRT_PREFIX}{fingerprint}.vrt"

    # Pour une liste déjà connue, conserver le VRT existant validé. Les
    # nouveaux blocs aboutissent à un nouveau nom, sans écraser l'ancien.
    if not vrt_path.exists():
        ds = gdal.BuildVRT(
            str(vrt_path),
            [str(path) for path in available_paths],
            options=gdal.BuildVRTOptions(
                resolution="highest", srcNodata=-9999, VRTNodata=-9999
            ),
        )
        if ds is None:
            raise RuntimeError("Échec de la création du VRT.")
        ds.FlushCache()
        ds = None

    # Contrôler que le VRT couvre bien TOUS les blocs attendus.
    vrt_ds = gdal.Open(str(vrt_path), gdal.GA_ReadOnly)
    if vrt_ds is None:
        raise RuntimeError(f"VRT illisible : {vrt_path}")
    gt = vrt_ds.GetGeoTransform()
    actual = (
        gt[0], gt[3] + vrt_ds.RasterYSize * gt[5],
        gt[0] + vrt_ds.RasterXSize * gt[1], gt[3],
    )
    vrt_ds = None
    available_blocks = [xy for xy, path in zip(blocks, paths)
                        if path in available_paths]
    expected = (
        min(x for x, y in available_blocks),
        min(y for x, y in available_blocks),
        max(x for x, y in available_blocks) + BLOCK_M,
        max(y for x, y in available_blocks) + BLOCK_M,
    )
    if any(abs(a - b) > 0.01 for a, b in zip(actual, expected)):
        raise RuntimeError(
            f"Emprise VRT incorrecte : {actual}, attendue : {expected}. "
            "Aucune nouvelle couche ajoutée."
        )

    project = QgsProject.instance()
    # Nettoyer les anciennes couches du prototype, SANS supprimer les GeoTIFF
    # ni les fichiers VRT : un projet QGIS sauvegardé pourrait y faire référence.
    for old in list(project.mapLayers().values()):
        if (isinstance(old, QgsRasterLayer)
                and old.providerType() == "gdal"
                and old.name() == OUTPUT_LAYER_NAME
                and Path(old.source()).suffix.lower() == ".vrt"
                and Path(old.source()).parent == CACHE_DIR):
            project.removeMapLayer(old.id())

    raster = QgsRasterLayer(str(vrt_path), OUTPUT_LAYER_NAME, "gdal")
    if not raster.isValid():
        raise RuntimeError("VRT créé, mais impossible de l'ajouter à QGIS.")
    raster.setRenderer(create_altitude_renderer(raster))
    project.addMapLayer(raster)
    iface.setActiveLayer(raster)
    iface.zoomToActiveLayer()
    iface.mapCanvas().refreshAllLayers()

    print(f"Emprise VRT (EPSG:2154) : {expected}")
    print("\nCouche VRT ajoutée à QGIS." + (" (INCOMPLÈTE)" if len(available_paths) < len(paths) else " (COMPLÈTE)"))
    print(f"GeoTIFF en cache : {CACHE_DIR / 'tiles'}")
    print(f"VRT : {vrt_path}")
    print(f"Transfert réalisé : {transferred / 1024**2:.2f} Mio")
    print(f"Blocs disponibles : {len(available_paths)}/{len(paths)}")
    print("Note : les blocs sont rectangulaires ; leurs pixels hors polygone "
          "ne sont pas masqués dans le VRT.")
    print(f"Temps tâche téléchargement : {elapsed_download:.2f} s")
    print(f"Temps assemblage et affichage : {time.monotonic() - started:.2f} s")


class LidarDownloadTask(QgsTask):
    """Opérations réseau et fichiers seulement ; ne touche jamais au projet QGIS."""

    def __init__(self, blocks, missing, cached):
        super().__init__(
            f"{TASK_DESCRIPTION} ({cached} en cache, {len(missing)} à télécharger)",
            QgsTask.CanCancel,
        )
        # Instantané sans QgsGeometry, QgsMapLayer, QgsProject ou widgets.
        self.blocks = list(blocks)
        self.missing = list(missing)
        self.cached = cached
        self.transferred = 0
        self.failed = []
        self.error = None
        self.elapsed_download = 0.0

    def run(self):
        started = time.monotonic()
        try:
            total = len(self.blocks)
            processed = self.cached
            self.setProgress(100.0 * processed / total)
            for index, ((xmin, ymin), path) in enumerate(self.missing, start=1):
                if self.isCanceled():
                    return False
                try:
                    size = download_block(xmin, ymin, path, self.isCanceled)
                    self.transferred += size
                except DownloadCancelled:
                    return False
                except Exception as err:
                    # Conserver le comportement V0.3 : rapporter les échecs
                    # et afficher un VRT éventuellement incomplet à la fin.
                    self.failed.append((xmin, ymin, str(err)))
                processed += 1
                self.setProgress(100.0 * processed / total)
                if index < len(self.missing):
                    interruptible_pause(REQUEST_PAUSE_SECONDS, self.isCanceled)
            return not self.isCanceled()
        except DownloadCancelled:
            return False
        except Exception:
            self.error = traceback.format_exc()
            return False
        finally:
            self.elapsed_download = time.monotonic() - started

    def finished(self, success):
        """QgsTaskManager appelle cette méthode sur le thread principal."""
        try:
            if self.isCanceled():
                print("LiDAR IGN : téléchargement annulé. Tuiles déjà validées conservées.")
                iface.messageBar().pushMessage(
                    "LiDAR IGN", "Annulé ; les tuiles reçues restent en cache.",
                    level=Qgis.Warning, duration=6,
                )
                return
            if not success:
                print("LiDAR IGN : erreur de tâche :", self.error)
                iface.messageBar().pushMessage(
                    "LiDAR IGN", "Erreur dans la tâche ; consulter la console Python.",
                    level=Qgis.Critical, duration=10,
                )
                return
            build_and_show_vrt(
                self.blocks, self.transferred, self.failed, self.elapsed_download
            )
            count = len(self.failed)
            iface.messageBar().pushMessage(
                "LiDAR IGN",
                (f"VRT affiché : {len(self.blocks) - count}/{len(self.blocks)} "
                 f"blocs, {self.transferred / 1024**2:.1f} Mio transférés"),
                level=Qgis.Warning if count else Qgis.Success,
                duration=10,
            )
        except Exception:
            print("LiDAR IGN : échec d'assemblage/affichage :", traceback.format_exc())
            iface.messageBar().pushMessage(
                "LiDAR IGN", "Erreur VRT/rendu ; consulter la console Python.",
                level=Qgis.Critical, duration=10,
            )


def main():
    # Évite deux téléchargements simultanés de la même tuile et du même .part.
    if any(task.description().startswith(TASK_DESCRIPTION)
           for task in QgsApplication.taskManager().activeTasks()):
        print("LiDAR IGN : une tâche est déjà active. Attendre ou l'annuler.")
        return

    layer = find_emprise_layer()
    print(f"Couche : {layer.name()} | SCR : {layer.crs().authid()}")
    polygons = get_polygons_l93(layer)
    blocks = grid_blocks_intersecting(polygons)
    if not blocks:
        raise RuntimeError("Aucun bloc ne croise les polygones.")
    paths = [output_path(x, y) for x, y in blocks]
    missing = [(xy, path) for xy, path in zip(blocks, paths)
               if not check_geotiff(path)]
    cached = len(blocks) - len(missing)

    print(f"Blocs intersectés : {len(blocks)}")
    print(f"En cache : {cached} | À télécharger : {len(missing)}")
    print(f"Volume brut théorique si tous téléchargés : "
          f"{len(blocks) * PIXELS * PIXELS * 4 / 1024**2:.1f} Mio")
    if len(missing) > MAX_NEW_BLOCKS:
        raise RuntimeError(
            f"Sécurité : {len(missing)} blocs à télécharger "
            f"(limite MAX_NEW_BLOCKS={MAX_NEW_BLOCKS}). "
            "Réduis l'emprise ou ajuste volontairement cette limite."
        )

    task = LidarDownloadTask(blocks, missing, cached)
    QgsApplication.taskManager().addTask(task)
    print("Tâche démarrée dans le gestionnaire QGIS :", task.description())
    print("La progression et le bouton Annuler sont accessibles dans les tâches QGIS.")
    print("Le VRT ne sera ajouté qu'une fois la tâche terminée.")


main()
