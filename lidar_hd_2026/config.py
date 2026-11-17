"""Configuration commune ; aucun accès réseau à l'import."""
from pathlib import Path

WMS_URL = "https://data.geopf.fr/wms-r/wms"
WMS_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
BLOCK_M = 256
PIXELS = 512
NODATA = -9999
CACHE_DIR = Path.home() / "qgis_lidar_ign_cache"
# Renseigner le chemin réel (UNC sous Windows si nécessaire), puis recharger
# le plugin. Ne pas substituer une adresse réseau fictive.
POOL_ROOT = None
MAX_ATTEMPTS = 4
RETRY_DELAY_SECONDS = 2
REQUEST_PAUSE_SECONDS = 0.3
LOCK_TIMEOUT_SECONDS = 600
