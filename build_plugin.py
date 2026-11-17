#!/usr/bin/env python3
"""ZIP reproductible, manifeste fermé et contrôle binaire des sources.

python3 LiDAR/build_plugin.py
QT_QPA_PLATFORM=offscreen /usr/bin/python3 LiDAR/build_plugin.py --icons
"""
import argparse
import ast
import configparser
import importlib.util
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "LiDAR/lidar_hd_2026"
DIST = ROOT / "dist"
FILES = {"__init__.py", "metadata.txt", "plugin.py", "config.py", "dialog.py",
         "engine.py", "spatial.py", "selection.py", "rendering.py",
         "icons/lidar_hd.svg", "icons/lidar_hd.png"}


def export_icons():
    """Qt SVG natif QGIS, sans bibliothèque graphique additionnelle."""
    from qgis.PyQt.QtCore import Qt, QRectF
    from qgis.PyQt.QtGui import QColor, QGuiApplication, QImage, QPainter
    from qgis.PyQt.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(PACKAGE / "icons/lidar_hd.svg"))
    assert renderer.isValid()
    output = ROOT / "audits/lidar-icons"
    output.mkdir(parents=True, exist_ok=True)
    sheet = QImage(680, 334, QImage.Format_ARGB32)
    sheet.fill(QColor("#808080"))
    painter = QPainter(sheet)
    for col, size in enumerate((16, 24, 32, 128)):
        transparent = QImage(size, size, QImage.Format_ARGB32)
        transparent.fill(Qt.transparent)
        p = QPainter(transparent)
        renderer.render(p)
        p.end()
        assert transparent.save(str(output / f"lidar_hd-{size}.png"))
        if size == 128:
            assert transparent.save(str(PACKAGE / "icons/lidar_hd.png"))
        painter.setPen(Qt.white)
        painter.drawText(col * 170 + 12, 20, f"{size} × {size} px")
        for row, (name, color) in enumerate((("light", "#f4f4f4"), ("dark", "#202124"))):
            image = QImage(size, size, QImage.Format_ARGB32)
            image.fill(QColor(color))
            p = QPainter(image)
            p.drawImage(0, 0, transparent)
            p.end()
            assert image.save(str(output / f"lidar_hd-{size}-{name}.png"))
            painter.fillRect(QRectF(col * 170, 30 + row * 152, 170, 152), QColor(color))
            painter.drawImage(col * 170 + (170 - size) // 2, 30 + row * 152 + (152 - size) // 2, image)
    painter.end()
    assert sheet.save(str(output / "previews.png"))
    return app


def build(dist=None):
    metadata = configparser.ConfigParser(interpolation=None)
    metadata.read(PACKAGE / "metadata.txt", encoding="utf-8")
    assert metadata["general"]["name"] == "LiDAR HD 2026"
    assert metadata["general"]["icon"] == "icons/lidar_hd.svg"
    assert metadata["general"]["qgisMinimumVersion"] == "3.34"
    svg = ET.parse(PACKAGE / "icons/lidar_hd.svg").getroot()
    assert svg.get("viewBox") == "0 0 64 64"
    for relative in FILES:
        path = PACKAGE / relative
        assert path.is_file(), path
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), str(path))
    output = Path(dist or DIST) / f"lidar_hd_2026-{metadata['general']['version']}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(FILES):
            info = ZipInfo("lidar_hd_2026/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (PACKAGE / relative).read_bytes())
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"lidar_hd_2026/" + name for name in FILES}
        for relative in FILES:
            assert archive.read("lidar_hd_2026/" + relative) == (PACKAGE / relative).read_bytes()
    return output


def build_sig():
    """Réutiliser le constructeur existant ; seuls les ZIP vont dans dist/."""
    spec = importlib.util.spec_from_file_location("sig_builder", ROOT / "plugin/tools/build_plugin.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.DIST_DIR = DIST
    output = builder.write_archive(builder.validate_plugin())
    builder.validate_archive(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icons", action="store_true", help="Régénérer PNG et aperçus avec Qt SVG")
    parser.add_argument("--with-sig", action="store_true", help="Construire aussi le ZIP SIG-Prestations dans dist/")
    args = parser.parse_args()
    if args.icons:
        export_icons()
    print(build())
    if args.with_sig:
        print(build_sig())
