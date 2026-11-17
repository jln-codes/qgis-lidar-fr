"""Point d'entrée du gestionnaire d'extensions QGIS."""


def classFactory(iface):
    from .plugin import LidarPlugin
    return LidarPlugin(iface)
