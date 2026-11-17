"""Rendu natif repris de lidar_ign.py V0.7 ; appelé sur le thread principal."""
from qgis.core import (Qgis, QgsColorRampShader, QgsRasterMinMaxOrigin,
                       QgsRasterShader, QgsSingleBandPseudoColorRenderer, QgsStyle)


def create_altitude_renderer(raster):
    """Rendu natif Turbo, reclassé par QGIS sur chaque nouvelle vue."""
    ramp = QgsStyle.defaultStyle().colorRamp("Turbo")
    if ramp is None:
        raise RuntimeError("Palette Turbo introuvable dans le style par défaut QGIS.")

    # Bornes provisoires uniquement : aucune lecture des pixels ici. Au premier
    # rendu, UpdatedCanvas calcule les min/max estimés de la vue et reclasse la
    # rampe. Ne pas appeler bandStatistics(1) : sampleSize=0 lirait tout le VRT.
    # Énumérations déplacées en 3.38 : les anciens noms restent nécessaires
    # sous QGIS 3.34 (Prizren). QGIS 3.40/3.44 utilisent les noms Qgis.
    if (hasattr(Qgis, "ShaderInterpolationMethod")
            and hasattr(Qgis, "ShaderClassificationMethod")):
        interpolation = Qgis.ShaderInterpolationMethod.Linear
        classification = Qgis.ShaderClassificationMethod.Continuous
    else:
        interpolation = QgsColorRampShader.Interpolated
        classification = QgsColorRampShader.Continuous

    color_shader = QgsColorRampShader(
        0.0, 1.0, ramp,
        interpolation,
        classification,
    )
    color_shader.classifyColorRamp()
    shader = QgsRasterShader(0.0, 1.0)
    shader.setRasterShaderFunction(color_shader)
    renderer = QgsSingleBandPseudoColorRenderer(raster.dataProvider(), 1, shader)
    renderer.setClassificationMin(0.0)
    renderer.setClassificationMax(1.0)

    origin = QgsRasterMinMaxOrigin()
    # QGIS 3.42 moved these enums from QgsRasterMinMaxOrigin to Qgis.
    # Maintenir les mêmes réglages natifs de QGIS 3.34 à QGIS 3.44.
    if hasattr(Qgis, "RasterRangeLimit"):
        origin.setLimits(Qgis.RasterRangeLimit.MinimumMaximum)
        origin.setExtent(Qgis.RasterRangeExtent.UpdatedCanvas)
        origin.setStatAccuracy(Qgis.RasterRangeAccuracy.Estimated)
    else:
        origin.setLimits(QgsRasterMinMaxOrigin.MinMax)
        origin.setExtent(QgsRasterMinMaxOrigin.UpdatedCanvas)
        origin.setStatAccuracy(QgsRasterMinMaxOrigin.Estimated)
    renderer.setMinMaxOrigin(origin)
    return renderer
