"""Configuration commune ; aucun accès réseau à l'import."""
import os
from pathlib import Path

WMS_URL = "https://data.geopf.fr/wms-r/wms"
WMS_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"

BLOCK_M = 256
PIXELS = 512
NODATA = -9999

CACHE_DIR = Path.home() / "qgis_lidar_ign_cache"

# Pool commun LiDAR HD 2026
POOL_ROOT = (
    Path(
        r"J:\1-milieux-aquatiques-et-risques"
        r"\0. QGIS\Catalogues des couches\LiDAR HD IGN 2026"
    )
    if os.name == "nt"
    else None
)

MAX_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 2
REQUEST_PAUSE_SECONDS = 0.3
LOCK_TIMEOUT_SECONDS = 600
