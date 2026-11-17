"""Moteur V0.7 : WMS, cache, reprises et VRT ; sans widgets ni couches vivantes.

Les écritures communes sont protégées par QLockFile et publiées par renommage
atomique depuis un fichier temporaire unique sur le même système de fichiers.
"""
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from osgeo import gdal, osr
from qgis.core import QgsTask
from qgis.PyQt.QtCore import QLockFile

from . import config

TASKS = set()  # Garder les wrappers Python jusqu'au callback finished().


class DownloadCancelled(Exception):
    pass


def interruptible_pause(seconds, is_canceled):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_canceled():
            raise DownloadCancelled()
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def tile_path(root, xy):
    return Path(root) / "tiles" / f"mnt_l93_{xy[0]}_{xy[1]}_{config.BLOCK_M}m_{config.PIXELS}px.tif"


def is_lambert93(srs):
    """SCR officiel, ou WKT IGN incomplet précisément identifié par l'audit.

    L'IGN nomme ce PROJCS EPSG:2154 mais perd son GEOGCS dans le GeoTIFF.
    Ne remplacer le GEOGCS qu'en mémoire, pour cette signature connue, puis
    comparer encore TOUS les paramètres projetés au SCR officiel.
    """
    expected = osr.SpatialReference()
    expected.ImportFromEPSG(2154)
    if srs.IsSame(expected):
        return True
    if not (srs.GetAttrValue("PROJCS") == "EPSG:2154"
            and srs.GetAttrValue("GEOGCS") == "unknown"
            and srs.GetAttrValue("DATUM") == "unnamed"
            and srs.GetAttrValue("SPHEROID") == "unretrievable - using WGS84"
            and math.isclose(srs.GetSemiMajor(), 6378137, rel_tol=0, abs_tol=1e-6)
            and math.isclose(srs.GetInvFlattening(), 298.257223563, rel_tol=0, abs_tol=1e-9)
            and math.isclose(float(srs.GetAttrValue("PRIMEM", 1) or "nan"), 0, rel_tol=0, abs_tol=1e-12)
            and math.isclose(srs.GetAngularUnits(), math.pi / 180, rel_tol=0, abs_tol=1e-15)):
        return False
    comparison = srs.Clone()
    comparison.CopyGeogCSFrom(expected)
    return bool(comparison.IsSame(expected))


def geotiff_error(path, xy=None):
    """None si valide ; sinon le contrôle précis en échec, sans réseau."""
    path = Path(path)
    if not path.is_file():
        return f"GeoTIFF absent : {path.name}"
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    ds = None
    try:
        with path.open("rb") as stream:
            header = stream.read(512)
        if header[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            excerpt = " ".join(header.decode("utf-8", errors="replace").split())[:240]
            return f"Format inattendu (non TIFF), signature {header[:8].hex()} : {excerpt}"
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None:
            return "GeoTIFF illisible par GDAL"
        if ds.GetDriver().ShortName != "GTiff":
            return f"Format inattendu : pilote {ds.GetDriver().ShortName}, attendu GTiff"
        if (ds.RasterXSize, ds.RasterYSize) != (config.PIXELS, config.PIXELS):
            return f"Dimensions incorrectes : {ds.RasterXSize} × {ds.RasterYSize}, attendu {config.PIXELS} × {config.PIXELS}"
        if ds.RasterCount != 1:
            return f"Nombre de bandes incorrect : {ds.RasterCount}, attendu 1"
        band = ds.GetRasterBand(1)
        if band.DataType != gdal.GDT_Float32:
            return f"Type incorrect : {gdal.GetDataTypeName(band.DataType)}, attendu Float32"
        if band.GetNoDataValue() != config.NODATA:
            return f"NoData incorrect : {band.GetNoDataValue()!r}, attendu {config.NODATA}"
        actual = osr.SpatialReference()
        wkt = ds.GetProjection()
        if not wkt:
            return "SCR absent : EPSG:2154 requis"
        try:
            if actual.ImportFromWkt(wkt) != 0 or not is_lambert93(actual):
                return (f"SCR incorrect : nom {actual.GetName() or 'inconnu'}, "
                        f"datum {actual.GetAttrValue('DATUM') or 'absent'} ; "
                        "description non équivalente au Lambert-93 EPSG:2154")
        except (RuntimeError, ValueError):
            return "SCR illisible : EPSG:2154 requis"
        gt = ds.GetGeoTransform()
        if not all(math.isfinite(value) for value in gt):
            return "Grille incorrecte : géotransformation non finie"
        if not all(abs(a - b) < 1e-6 for a, b in zip(gt[1:3] + gt[4:], (0.5, 0, 0, -0.5))):
            return f"Grille incorrecte : résolution 0,5 m sans rotation requise, reçu {gt}"
        if xy is not None and not (abs(gt[0] - xy[0]) < 1e-6 and abs(gt[3] - xy[1] - config.BLOCK_M) < 1e-6):
            return f"Emprise incorrecte : origine {gt[0]},{gt[3]}, bloc attendu {xy[0]},{xy[1]}"
        # Lire tous les pixels détecte aussi les bandes TIFF tronquées.
        gdal.ErrorReset()
        if (len(band.ReadRaster() or b"") != config.PIXELS ** 2 * 4
                or gdal.GetLastErrorType() >= gdal.CE_Failure):
            return "GeoTIFF illisible ou corrompu : lecture complète des pixels impossible"
        return None
    except (RuntimeError, ValueError, OSError) as error:
        return f"GeoTIFF illisible : {error}"
    finally:
        ds = None
        gdal.PopErrorHandler()


def check_geotiff(path, xy=None):
    return geotiff_error(path, xy) is None


@contextmanager
def tile_lock(path, is_canceled):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(path) + ".lock")
    lock.setStaleLockTime(15 * 60 * 1000)
    deadline = time.monotonic() + config.LOCK_TIMEOUT_SECONDS
    while not lock.tryLock(0):
        if is_canceled():
            raise DownloadCancelled()
        if lock.error() != QLockFile.LockFailedError:
            raise OSError(f"Cache ou partage inaccessible, verrou impossible : {path}")
        if time.monotonic() >= deadline:
            raise OSError(f"Bloc utilisé par une autre instance ; réessayez : {path.name}")
        interruptible_pause(0.1, is_canceled)
    try:
        if is_canceled():
            raise DownloadCancelled()
        yield
    finally:
        lock.unlock()


@contextmanager
def temporary_sibling(dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(filename)
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def download_block(xmin, ymin, dest, is_canceled):
    """Même GetMap et politique de reprise que V0.7, flux annulable et temporaire unique."""
    params = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=config.WMS_LAYER,
                  STYLES="normal", CRS="EPSG:2154",
                  BBOX=f"{xmin},{ymin},{xmin + config.BLOCK_M},{ymin + config.BLOCK_M}",
                  WIDTH=config.PIXELS, HEIGHT=config.PIXELS, FORMAT="image/geotiff", EXCEPTIONS="text/xml")
    request = Request(config.WMS_URL + "?" + urlencode(params),
                      headers={"User-Agent": "QGIS-LiDAR-HD-2026/0.1.1"})
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        if is_canceled():
            raise DownloadCancelled()
        try:
            with temporary_sibling(dest) as tmp:
                with urlopen(request, timeout=90) as response, tmp.open("wb") as stream:
                    status = getattr(response, "status", None)
                    content_type = getattr(response, "headers", {}).get("Content-Type", "non indiqué")
                    while True:
                        if is_canceled():
                            raise DownloadCancelled()
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                error = geotiff_error(tmp, (xmin, ymin))
                if error is not None:
                    raise RuntimeError(f"{error} (HTTP {status or 'non indiqué'}, Content-Type {content_type}, {tmp.stat().st_size} octets)")
                if is_canceled():
                    raise DownloadCancelled()
                size = tmp.stat().st_size
                tmp.replace(dest)
                return size
        except HTTPError as error:
            retryable = error.code in {429, 500, 502, 503, 504}
            message = f"HTTP {error.code}: {error.reason}"
        except (URLError, TimeoutError, ConnectionError) as error:
            retryable, message = True, str(error)
        except RuntimeError as error:
            retryable, message = False, str(error)
        if not retryable or attempt == config.MAX_ATTEMPTS:
            raise RuntimeError(f"Bloc {xmin},{ymin} : {message}")
        interruptible_pause(config.RETRY_DELAY_SECONDS * 2 ** (attempt - 1), is_canceled)


def copy_tile(source, dest, xy, is_canceled):
    if source == dest:
        return 0
    with tile_lock(dest, is_canceled):
        if check_geotiff(dest, xy):
            return 0
        with temporary_sibling(dest) as tmp:
            with source.open("rb") as src, tmp.open("wb") as dst:
                while True:
                    if is_canceled():
                        raise DownloadCancelled()
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            error = geotiff_error(tmp, xy)
            if error is not None:
                raise RuntimeError(f"Copie invalide : {source} : {error}")
            size = tmp.stat().st_size
            tmp.replace(dest)
            return size


def validate_name(name):
    # Même politique sur Linux et Windows, y compris noms réservés et points finaux.
    if (not name or name != name.strip() or len(name) > 80
            or name in {".", ".."} or name.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
        raise ValueError("Nom invalide : 1 à 80 caractères, sans séparateur ni caractère réservé Windows.")
    return name


def storage_root(mode, directory, pool_root):
    if mode == "pool":
        if pool_root is None or not str(pool_root).strip():
            raise ValueError("Pool commun désactivé : son emplacement réel n'est pas configuré.")
        root = Path(pool_root).expanduser()
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK):
            raise ValueError(f"Pool commun inaccessible ou non inscriptible : {root}")
        return root
    if mode != "local" or not str(directory).strip():
        raise ValueError("Choisissez un dossier de sortie local.")
    root = Path(directory).expanduser()
    if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK):
        raise ValueError(f"Dossier inaccessible ou non inscriptible : {root}")
    return root


@dataclass(frozen=True)
class Plan:
    blocks: tuple
    cache: Path
    root: Path
    name: str
    mode: str
    cached: int
    pooled: int
    download_count: int
    estimated_network: int
    estimated_copy: int
    mean_bytes: int

    @property
    def extraction(self):
        return self.root / "extractions" / self.name if self.mode == "pool" else self.root / self.name


def diagnose(blocks, cache, root, name, mode="local"):
    """Lecture des seuls blocs demandés ; aucun GetCapabilities ou autre requête."""
    validate_name(name)
    cache, root = Path(cache).absolute(), Path(root).absolute()
    cached = pooled = 0
    sizes = []
    copy_sizes = []
    copy_unknown = 0
    for xy in blocks:
        local = tile_path(cache, xy)
        has_cache = check_geotiff(local, xy)
        cached += int(has_cache)
        shared = tile_path(root, xy)
        has_pool = mode == "pool" and check_geotiff(shared, xy)
        pooled += int(has_pool)
        source = shared if has_pool else local if has_cache else None
        if source:
            sizes.append(source.stat().st_size)
        if not has_pool:
            if has_cache:
                copy_sizes.append(local.stat().st_size)
            else:
                copy_unknown += 1
    mean = max(1, round(sum(sizes) / len(sizes))) if sizes else 1024 ** 2
    # Le cache et le pool peuvent se recouvrir : compter l'union disponible.
    missing = sum(not check_geotiff(tile_path(cache, xy), xy)
                  and not (mode == "pool" and check_geotiff(tile_path(root, xy), xy)) for xy in blocks)
    plan = Plan(tuple(blocks), cache, root, name, mode, cached, pooled, missing,
                missing * mean, sum(copy_sizes) + copy_unknown * mean, mean)
    if not blocks:
        raise ValueError("Aucun bloc nécessaire.")
    if plan.extraction.exists():
        raise ValueError(f"Cette extraction existe déjà : {plan.extraction}. Choisissez un autre nom.")
    # Éviter de confondre le dossier exporté et les blocs techniques/communs.
    if plan.extraction == cache or plan.extraction == cache / "tiles":
        raise ValueError("La destination entre en conflit avec le cache technique.")
    return plan


def build_vrt(dest, paths, blocks):
    if not paths or len(paths) != len(blocks):
        raise RuntimeError("VRT : liste de blocs absente ou incohérente")
    for path, xy in zip(paths, blocks):
        error = geotiff_error(path, xy)
        if error is not None:
            raise RuntimeError(f"VRT : {path.name} : {error}")
    with temporary_sibling(dest) as tmp:
        ds = gdal.BuildVRT(str(tmp), [str(p) for p in paths],
                           options=gdal.BuildVRTOptions(resolution="highest", srcNodata=config.NODATA,
                                                        VRTNodata=config.NODATA,
                                                        outputSRS="EPSG:2154",
                                                        # Chaque entrée a été validée en Lambert-93 ci-dessus.
                                                        # Tolérer uniquement leurs descriptions WKT différentes,
                                                        # notamment le GEOGCS incomplet IGN ; aucune reprojection.
                                                        allowProjectionDifference=True))
        if ds is None:
            raise RuntimeError("Échec de création du VRT.")
        ds.FlushCache()
        ds = None
        tree = ET.parse(tmp)
        # GDAL peut choisir des chemins absolus sur certains volumes Windows.
        for node in tree.findall(".//SourceFilename"):
            original = Path(node.text)
            full = tmp.parent / original if node.get("relativeToVRT") == "1" else original
            node.text = Path(os.path.relpath(full, dest.parent)).as_posix()
            node.set("relativeToVRT", "1")
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        ds = gdal.Open(str(tmp))
        if ds is None:
            raise RuntimeError("VRT illisible.")
        gt = ds.GetGeoTransform()
        actual = (gt[0], gt[3] + ds.RasterYSize * gt[5], gt[0] + ds.RasterXSize * gt[1], gt[3])
        expected = (min(x for x, y in blocks), min(y for x, y in blocks),
                    max(x for x, y in blocks) + config.BLOCK_M, max(y for x, y in blocks) + config.BLOCK_M)
        ds = None
        if any(not math.isclose(a, b, abs_tol=0.01) for a, b in zip(actual, expected)):
            raise RuntimeError(f"Emprise VRT incorrecte : {actual}, attendue : {expected}")
        tmp.replace(dest)


class LidarDownloadTask(QgsTask):
    """run() : fichiers et réseau uniquement ; finished() : rappel sur le thread GUI."""
    def __init__(self, plan, callback=None):
        super().__init__(f"LiDAR HD 2026 — {len(plan.blocks)} blocs", QgsTask.CanCancel)
        self.plan = plan
        self.callback = callback
        self.failed = []
        self.error = None
        self.transferred = 0
        self.copied = 0
        self.vrt = None
        TASKS.add(self)

    def run(self):
        plan = self.plan
        available = []
        created = False
        try:
            # Revérifier le partage au début : ne pas le recréer si déconnecté.
            storage_root(plan.mode, plan.root, plan.root if plan.mode == "pool" else None)
            for i, xy in enumerate(plan.blocks):
                if self.isCanceled():
                    raise DownloadCancelled()
                cached = tile_path(plan.cache, xy)
                pooled = tile_path(plan.root, xy) if plan.mode == "pool" else None
                try:
                    if pooled and check_geotiff(pooled, xy):
                        source = pooled
                    else:
                        with tile_lock(cached, self.isCanceled):
                            if not check_geotiff(cached, xy):
                                self.transferred += download_block(*xy, cached, self.isCanceled)
                                interruptible_pause(config.REQUEST_PAUSE_SECONDS, self.isCanceled)
                        source = cached
                        if pooled:
                            self.copied += copy_tile(cached, pooled, xy, self.isCanceled)
                            source = pooled
                    available.append((xy, source))
                except DownloadCancelled:
                    raise
                except Exception as error:
                    self.failed.append((xy, str(error)))
                self.setProgress(90 * (i + 1) / len(plan.blocks))
            if self.isCanceled():
                raise DownloadCancelled()
            if not available:
                raise RuntimeError("Aucun GeoTIFF disponible. " + "; ".join(reason for _, reason in self.failed[:3]))
            # mkdir exclusif : deux extractions homonymes ne s'écrasent jamais.
            plan.extraction.parent.mkdir(parents=True, exist_ok=True)
            plan.extraction.mkdir()
            created = True
            paths = []
            for xy, source in available:
                if self.isCanceled():
                    raise DownloadCancelled()
                dest = tile_path(plan.extraction, xy) if plan.mode == "local" else source
                self.copied += copy_tile(source, dest, xy, self.isCanceled)
                paths.append(dest)
            dest = plan.extraction / f"{plan.name}.vrt"
            build_vrt(dest, paths, [xy for xy, _ in available])
            if self.isCanceled():
                raise DownloadCancelled()
            self.vrt = dest
            self.setProgress(100)
            return True
        except DownloadCancelled:
            return False
        except Exception as error:
            self.error = str(error)
            return False
        finally:
            # Seulement le dossier exclusif créé par cette tâche. Les blocs
            # du cache et du pool ne sont jamais supprimés.
            if created and self.vrt is None:
                shutil.rmtree(plan.extraction, ignore_errors=True)

    def finished(self, success):
        try:
            if self.callback is not None:
                self.callback(self, success)
        finally:
            self.callback = None
            TASKS.discard(self)
