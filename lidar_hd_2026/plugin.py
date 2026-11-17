"""Une action ; barre partagée sans dépendance envers SIG-Prestations."""
from pathlib import Path

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QToolBar

TOOLBAR_NAME = "SIGMilieuxToolbar"


class LidarPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.toolbar = None
        self.dialog = None

    def initGui(self):
        if self.action is not None:
            return
        window = self.iface.mainWindow()
        self.toolbar = window.findChild(QToolBar, TOOLBAR_NAME)
        if self.toolbar is None:
            self.toolbar = QToolBar("SIG-Milieux", window)
            self.toolbar.setObjectName(TOOLBAR_NAME)
            window.addToolBar(self.toolbar)
        self.action = QAction(QIcon(str(Path(__file__).parent / "icons/lidar_hd.svg")), "LiDAR HD 2026", window)
        self.action.setObjectName("lidar_hd_2026_action")
        self.action.setToolTip("Extraire un MNT LiDAR HD IGN")
        self.action.triggered.connect(self.show_dialog)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.toolbar.addAction(self.action)
        self.toolbar.show()
        self.iface.addPluginToMenu("SIG-Milieux", self.action)
        metadata = Path(__file__).with_name("metadata.txt").read_text(encoding="utf-8")
        version = next((line.partition("=")[2].strip() for line in metadata.splitlines()
                        if line.startswith("version=")), "inconnue")
        QgsMessageLog.logMessage(f"Plugin chargé : version {version} ; chemin {Path(__file__).resolve()}",
                                 "LiDAR HD 2026", Qgis.Info)

    def show_dialog(self, *_):
        if self.dialog is None:
            from .dialog import LidarDialog
            self.dialog = LidarDialog(self.iface)
        self.dialog.refresh_diagnostic()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog = None
        if self.action is not None:
            self.iface.removePluginMenu("SIG-Milieux", self.action)
            self.toolbar.removeAction(self.action)
            self.action.deleteLater()
            self.action = None
        if self.toolbar is not None:
            if not self.toolbar.actions():
                self.iface.mainWindow().removeToolBar(self.toolbar)
                self.toolbar.setParent(None)
                self.toolbar.deleteLater()
            self.toolbar = None
