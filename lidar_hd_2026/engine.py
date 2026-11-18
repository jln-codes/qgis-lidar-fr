"""Moteur V0.7 : WMS, cache, reprises et VRT ; sans widgets ni couches vivantes.

Les écritures communes sont protégées par QLockFile et publiées par renommage
atomique depuis un fichier temporaire unique sur le même système de fichiers.
"""
import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from osgeo import gdal, osr
from qgis.core import QgsTask
from qgis.PyQt.QtCore import QLockFile

from . import config

TASKS = set()  # Garder les wrappers Python jusqu'au callback finished().
GLOBAL_VRT_NAME = "mnt_lidar_global.vrt"


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
def tile_lock(path, is_canceled, stale_time_ms=15 * 60 * 1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(path) + ".lock")
    lock.setStaleLockTime(stale_time_ms)
    deadline = time.monotonic() + config.LOCK_TIMEOUT_SECONDS
    while not lock.tryLock(0):
        if is_canceled():
            raise DownloadCancelled()
        if lock.error() != QLockFile.LockFailedError:
            raise OSError(f"Cache ou partage inaccessible, verrou impossible : {path}")
        if time.monotonic() >= deadline:
            raise OSError(f"Fichier utilisé par une autre instance ; réessayez : {path.name}")
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


def _copy_tile_locked(source, dest, xy, is_canceled):
    """Publication atomique ; l'appelant possède déjà le verrou de destination."""
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
        if is_canceled():
            raise DownloadCancelled()
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
        raise ValueError("Choisissez une bibliothèque LiDAR locale.")
    root = Path(directory).expanduser()
    if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK):
        raise ValueError(f"Bibliothèque inaccessible ou non inscriptible : {root}")
    return root


@dataclass(frozen=True)
class Plan:
    blocks: tuple
    cache: Path
    root: Path
    name: str
    mode: str
    cached: int
    available: int
    download_count: int
    estimated_network: int
    estimated_copy: int
    mean_bytes: int

    @property
    def extraction(self):
        return self.root / "extractions" / f"{self.name}.vrt"


def check_extraction_name(root, name):
    validate_name(name)
    if name.casefold() in {"tiles", "extractions", "mnt_lidar_global"}:
        raise ValueError("Nom réservé à l'organisation de la bibliothèque.")
    for path in (root / "extractions" / f"{name}.vrt", root / name, root / "extractions" / name):
        if path.exists() or path.is_symlink():
            raise ValueError(f"Cette extraction existe déjà (fichier ou ancien dossier) : {path}. Choisissez un autre nom.")


def library_status(root):
    """État local du répertoire, sans création ni inventaire complet des tuiles."""
    if not any(root.iterdir()):
        return "Bibliothèque vide : création de tiles/ et extractions/ à la première extraction."
    for name in ("tiles", "extractions"):
        path = root / name
        if path.exists() and not path.is_dir():
            raise ValueError(f"Structure de bibliothèque incorrecte : {path} doit être un dossier.")
    if (root / "tiles").is_dir() and (root / "extractions").is_dir():
        return "Bibliothèque existante : les tuiles sont mutualisées entre extractions."
    return "Dossier existant : nouvelle structure ajoutée, anciens fichiers conservés sans migration."


def diagnose(blocks, cache, root, name, mode="local"):
    """Lecture des seuls blocs demandés ; aucun GetCapabilities ou autre requête."""
    cache, root = Path(cache).absolute(), Path(root).absolute()
    check_extraction_name(root, name)
    library_status(root)
    if root.resolve() == cache.resolve() or cache.resolve() in root.resolve().parents:
        raise ValueError("La bibliothèque doit être distincte du cache technique.")
    blocks = tuple(sorted(set(blocks)))
    cached = available = 0
    sizes = []
    copy_sizes = []
    copy_unknown = 0
    for xy in blocks:
        shared = tile_path(root, xy)
        has_library = check_geotiff(shared, xy)
        available += int(has_library)
        local = tile_path(cache, xy)
        has_cache = not has_library and check_geotiff(local, xy)
        cached += int(has_cache)
        source = shared if has_library else local if has_cache else None
        if source:
            sizes.append(source.stat().st_size)
        if not has_library:
            if has_cache:
                copy_sizes.append(local.stat().st_size)
            else:
                copy_unknown += 1
    mean = max(1, round(sum(sizes) / len(sizes))) if sizes else 1024 ** 2
    missing = len(blocks) - available - cached
    plan = Plan(blocks, cache, root, name, mode, cached, available, missing,
                missing * mean, sum(copy_sizes) + copy_unknown * mean, mean)
    if not blocks:
        raise ValueError("Aucun bloc nécessaire.")
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


@dataclass(frozen=True)
class GlobalVrtResult:
    vrt: Optional[Path]
    changed: bool
    count: int
    ignored: int


def refresh_global_vrt(root, is_canceled=lambda: False, progress=None, force_validation=False):
    """Inventaire réel sous verrou ; aucune couche QGIS, aucune requête IGN.

    La signature reste dans le VRT : pas d'index externe à déplacer avec lui.
    L'inventaire validé et ses métadonnées évitent de relire les pixels des
    sources inchangées. Le contrôle manuel force leur validation complète.
    """
    root = storage_root("local", root, None).absolute()
    dest = root / GLOBAL_VRT_NAME
    with tile_lock(dest, is_canceled, stale_time_ms=0):
        validated = {}
        if not force_validation:
            try:
                previous = ET.parse(dest)
                records = json.loads(previous.findtext(
                    "./Metadata[@domain='lidar_hd_2026']/MDI[@key='validated_tiles']"))
                digest = hashlib.sha256(json.dumps(records).encode("utf-8")).hexdigest()
                if digest == previous.findtext("./Metadata[@domain='lidar_hd_2026']/MDI[@key='sources']"):
                    validated = {r[0]: r for r in records if len(r) == 5
                                 and isinstance(r[0], str)
                                 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in r[1:])}
            except (ET.ParseError, OSError, TypeError, ValueError, OverflowError):
                pass  # Aucun inventaire réutilisable : contrôle complet.
        folder = root / "tiles"
        files = sorted((p for p in folder.iterdir()
                        if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}),
                       key=lambda p: p.name) if folder.exists() else []
        paths, blocks, states = [], [], []
        ignored = 0
        for index, path in enumerate(files):
            if is_canceled():
                raise DownloadCancelled()
            info = path.stat()
            state = [path.name, info.st_size, info.st_mtime_ns]
            previous = validated.get(path.name)
            if previous is not None and previous[:3] == state:
                xy = previous[3:]
            elif geotiff_error(path) is not None:
                ignored += 1
                xy = None
            else:
                ds = gdal.Open(str(path), gdal.GA_ReadOnly)
                if ds is None:
                    raise RuntimeError(f"Tuile devenue illisible pendant l'inventaire : {path}")
                gt = ds.GetGeoTransform()
                ds = None
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise RuntimeError(f"Tuile modifiée pendant l'inventaire : {path}")
                xy = (gt[0], gt[3] - config.BLOCK_M)
            if xy is not None:
                paths.append(path)
                blocks.append(xy)
                states.append(state + list(xy))
            if progress is not None:
                progress(90 * (index + 1) / len(files))
        if is_canceled():
            raise DownloadCancelled()
        if not paths:
            changed = dest.exists()
            # Une bibliothèque devenue vide ne doit pas garder un VRT périmé.
            dest.unlink(missing_ok=True)
            return GlobalVrtResult(None, changed, 0, ignored)
        signature = hashlib.sha256(json.dumps(states).encode("utf-8")).hexdigest()
        try:
            tree = ET.parse(dest)
            old_signature = tree.findtext("./Metadata[@domain='lidar_hd_2026']/MDI[@key='sources']")
            same_inventory = json.loads(tree.findtext(
                "./Metadata[@domain='lidar_hd_2026']/MDI[@key='validated_tiles']")) == states
            sources = tree.findall(".//SourceFilename")
            expected = {f"tiles/{p.name}" for p in paths}
            same_sources = (len(sources) == len(paths)
                            and all(n.get("relativeToVRT") == "1" for n in sources)
                            and {n.text for n in sources} == expected)
            gdal.PushErrorHandler("CPLQuietErrorHandler")
            try:
                ds = gdal.Open(str(dest), gdal.GA_ReadOnly)
                readable = ds is not None
                ds = None
            finally:
                gdal.PopErrorHandler()
            if signature == old_signature and same_inventory and same_sources and readable:
                return GlobalVrtResult(dest, False, len(paths), ignored)
        except (ET.ParseError, OSError, RuntimeError, TypeError, ValueError):
            pass  # Absent ou illisible : reconstruire depuis les seules tuiles.
        with temporary_sibling(dest) as tmp:
            build_vrt(tmp, paths, blocks)
            tree = ET.parse(tmp)
            metadata = ET.SubElement(tree.getroot(), "Metadata", domain="lidar_hd_2026")
            ET.SubElement(metadata, "MDI", key="sources").text = signature
            ET.SubElement(metadata, "MDI", key="validated_tiles").text = json.dumps(states)
            tree.write(tmp, encoding="utf-8", xml_declaration=True)
            if is_canceled():
                raise DownloadCancelled()
            tmp.replace(dest)
        return GlobalVrtResult(dest, True, len(paths), ignored)


class GlobalVrtTask(QgsTask):
    """Régénération manuelle de la couverture, sans extraction ni chargement."""
    def __init__(self, root, callback=None):
        super().__init__("LiDAR HD 2026 — couverture globale", QgsTask.CanCancel)
        self.root = root
        self.callback = callback
        self.result = None
        self.error = None
        TASKS.add(self)

    def run(self):
        try:
            self.result = refresh_global_vrt(self.root, self.isCanceled, self.setProgress, force_validation=True)
            self.setProgress(100)
            return True
        except DownloadCancelled:
            return False
        except Exception as error:
            self.error = str(error)
            return False

    def finished(self, success):
        try:
            if self.callback is not None:
                self.callback(self, success)
        finally:
            self.callback = None
            TASKS.discard(self)


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
        self.global_result = None
        self.global_error = None
        TASKS.add(self)

    def run(self):
        plan = self.plan
        try:
            # Revérifier le partage au début : ne pas le recréer si déconnecté.
            storage_root(plan.mode, plan.root, plan.root if plan.mode == "pool" else None)
            with tile_lock(plan.extraction, self.isCanceled, stale_time_ms=0):
                check_extraction_name(plan.root, plan.name)
                available = []
                for i, xy in enumerate(plan.blocks):
                    if self.isCanceled():
                        raise DownloadCancelled()
                    cached = tile_path(plan.cache, xy)
                    shared = tile_path(plan.root, xy)
                    try:
                        if not check_geotiff(shared, xy):
                            # Bibliothèque puis cache : ordre identique partout.
                            # Deux utilisateurs avec des caches distincts ne
                            # téléchargent pas le même bloc en parallèle.
                            with tile_lock(shared, self.isCanceled):
                                if not check_geotiff(shared, xy):
                                    with tile_lock(cached, self.isCanceled):
                                        if not check_geotiff(cached, xy):
                                            self.transferred += download_block(*xy, cached, self.isCanceled)
                                            interruptible_pause(config.REQUEST_PAUSE_SECONDS, self.isCanceled)
                                    self.copied += _copy_tile_locked(cached, shared, xy, self.isCanceled)
                        available.append((xy, shared))
                    except DownloadCancelled:
                        raise
                    except Exception as error:
                        self.failed.append((xy, str(error)))
                    self.setProgress(90 * (i + 1) / len(plan.blocks))
                if self.isCanceled():
                    raise DownloadCancelled()
                if not available:
                    raise RuntimeError("Aucun GeoTIFF disponible. " + "; ".join(reason for _, reason in self.failed[:3]))
                # Nouvelle vérification après l'attente, sans toucher aux anciens dossiers.
                check_extraction_name(plan.root, plan.name)
                build_vrt(plan.extraction, [p for _, p in available], [xy for xy, _ in available])
                if self.isCanceled():
                    plan.extraction.unlink()  # Seulement le VRT que cette tâche vient de publier.
                    raise DownloadCancelled()
                self.vrt = plan.extraction
            try:
                self.global_result = refresh_global_vrt(plan.root, self.isCanceled)
            except DownloadCancelled:
                self.global_error = "Actualisation du VRT global annulée ; VRT individuel conservé."
                return False
            except Exception as error:
                self.global_error = f"VRT individuel enregistré ; échec de mise à jour du VRT global : {error}"
            self.setProgress(100)
            return True
        except DownloadCancelled:
            return False
        except Exception as error:
            self.error = str(error)
            return False

    def finished(self, success):
        try:
            if self.callback is not None:
                self.callback(self, success)
        finally:
            self.callback = None
            TASKS.discard(self)
