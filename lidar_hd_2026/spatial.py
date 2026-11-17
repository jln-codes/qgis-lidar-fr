"""Instantanés spatiaux et grille V0.7, exclusivement sur le thread principal."""
import math
from dataclasses import dataclass

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle,
                       QgsWkbTypes)

from .config import BLOCK_M


@dataclass(frozen=True)
class Footprint:
    blocks: tuple
    features: int
    parts: int
    area_m2: float


def is_polygon(layer):
    return (layer is not None and layer.isValid() and hasattr(layer, "getFeatures")
            and QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PolygonGeometry)


def grid_blocks_intersecting(geometries):
    blocks = set()
    for geom in geometries:
        bounds = geom.boundingBox()
        for ix in range(math.floor(bounds.xMinimum() / BLOCK_M), math.ceil(bounds.xMaximum() / BLOCK_M)):
            for iy in range(math.floor(bounds.yMinimum() / BLOCK_M), math.ceil(bounds.yMaximum() / BLOCK_M)):
                xmin, ymin = ix * BLOCK_M, iy * BLOCK_M
                block = QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmin + BLOCK_M, ymin + BLOCK_M))
                if geom.intersects(block):
                    blocks.add((xmin, ymin))
    return tuple(sorted(blocks))


def snapshot(selections, buffer_m):
    """(couche, IDs ou None=tout) ; aucune couche ne quitte ce thread."""
    if not 0 <= buffer_m <= 100:
        raise ValueError("Le tampon doit être compris entre 0 et 100 m.")
    project = QgsProject.instance()
    geometries, count, parts = [], 0, 0
    for layer, ids in selections:
        if not is_polygon(layer) or not layer.crs().isValid():
            raise ValueError("Choisissez une couche polygonale avec un SCR valide.")
        transform = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:2154"), project)
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes([])
        if ids is not None:
            request.setFilterFids(list(ids))
        for feature in layer.getFeatures(request):
            geom = QgsGeometry(feature.geometry())
            if geom.isEmpty() or not geom.isGeosValid():
                raise ValueError(f"{layer.name()} / entité {feature.id()} : géométrie vide ou invalide.")
            parts += len(geom.asGeometryCollection()) if geom.isMultipart() else 1
            if geom.transform(transform) != 0:
                raise ValueError(f"Transformation EPSG:2154 impossible : entité {feature.id()}.")
            if buffer_m:
                geom = geom.buffer(buffer_m, 8)
            if geom.isEmpty() or not geom.isGeosValid():
                raise ValueError("Tampon vide ou invalide.")
            geometries.append(geom)
            count += 1
    if not geometries:
        raise ValueError("Aucun polygone retenu. Choisissez une couche ou sélectionnez sur la carte.")
    # L'union évite de compter deux fois la surface des polygones superposés.
    union = QgsGeometry.unaryUnion(geometries)
    if union.isEmpty():
        raise ValueError("Impossible de calculer l'emprise cumulée.")
    return Footprint(grid_blocks_intersecting(geometries), count, parts, union.area())
