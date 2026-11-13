"""MNT LiDAR HD IGN — chargement sélectif depuis une couche polygonale QGIS.

Exécution : ouvrir le script dans l'éditeur Python intégré de QGIS et cliquer
sur « Exécuter ». Une couche vectorielle nommée « emprise » doit être chargée.

Ce script n'altère pas les données sources. Les GeoTIFF et le VRT sont créés
sur disque et sont réutilisables entre sessions QGIS.
"""

import hashlib
import math
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from osgeo import gdal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
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


def download_block(xmin, ymin, dest):
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
        try:
            with urlopen(request, timeout=90) as response:
                payload = response.read()
            if payload[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                excerpt = payload[:500].decode("utf-8", errors="replace")
                raise RuntimeError(f"Réponse IGN non TIFF : {excerpt}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            try:
                tmp.write_bytes(payload)
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
        time.sleep(delay)


def main():
    layer = find_emprise_layer()
    print(f"Couche : {layer.name()} | SCR : {layer.crs().authid()}")
    polygons = get_polygons_l93(layer)
    blocks = grid_blocks_intersecting(polygons)
    if not blocks:
        raise RuntimeError("Aucun bloc ne croise les polygones.")

    paths = [output_path(x, y) for x, y in blocks]
    missing = [(xy, path) for xy, path in zip(blocks, paths)
               if not check_geotiff(path)]

    print(f"Blocs intersectés : {len(blocks)}")
    print(f"En cache : {len(blocks) - len(missing)} | À télécharger : {len(missing)}")
    print(f"Volume brut théorique si tous téléchargés : "
          f"{len(blocks) * PIXELS * PIXELS * 4 / 1024**2:.1f} Mio")

    if len(missing) > MAX_NEW_BLOCKS:
        raise RuntimeError(
            f"Sécurité : {len(missing)} blocs à télécharger "
            f"(limite MAX_NEW_BLOCKS={MAX_NEW_BLOCKS}). "
            "Réduis l'emprise ou ajuste volontairement cette limite."
        )

    transferred = 0
    failed = []
    for index, ((xmin, ymin), path) in enumerate(missing, start=1):
        try:
            size = download_block(xmin, ymin, path)
            transferred += size
            print(f"  [{index}/{len(missing)}] {xmin},{ymin} — {size / 1024**2:.2f} Mio")
        except RuntimeError as err:
            failed.append((xmin, ymin, str(err)))
            print(f"  [{index}/{len(missing)}] ÉCHEC — {err}")
        time.sleep(REQUEST_PAUSE_SECONDS)

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


main()
