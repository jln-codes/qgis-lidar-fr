"""Petit dialogue natif QGIS ; diagnostic local avant tout téléchargement."""
from pathlib import Path

from qgis.core import (Qgis, QgsApplication, QgsMapLayerProxyModel, QgsProject,
                       QgsRasterLayer)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QSettings, QTimer, Qt
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                                QFormLayout, QFrame, QGridLayout, QGroupBox,
                                QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import config
from .engine import LidarDownloadTask, diagnose, storage_root
from .rendering import create_altitude_renderer
from .selection import PolygonSelectionTool
from .spatial import is_polygon, snapshot


class LidarDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("LiDAR HD 2026")
        self.setWindowModality(Qt.NonModal)
        self.selected = set()
        self.capture = None
        self.previous_tool = None
        self.before_capture = set()
        self.task = None
        self.plan = None
        self._closed = False
        self._signals = []
        self._layer_signals = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        self.parameters = QWidget(self)
        grid = QGridLayout(self.parameters)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        footprint_box = QGroupBox("Emprise", self.parameters)
        output_box = QGroupBox("Fichiers", self.parameters)
        grid.addWidget(footprint_box, 0, 0)
        grid.addWidget(output_box, 0, 1)
        footprint_form = QFormLayout(footprint_box)
        output_form = QFormLayout(output_box)
        for form in (footprint_form, output_form):
            form.setContentsMargins(12, 18, 12, 14)
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(12)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.layer = QgsMapLayerComboBox(self)
        self.layer.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.layer.setShowCrs(True)
        self.layer.setToolTip("Couche Polygon ou MultiPolygon du projet, avec un SCR valide.")
        footprint_form.addRow("Couche", self.layer)
        self.mode = QComboBox(self)
        self.mode.addItems(["Toutes les entités", "Sur la carte"])
        self.mode.setToolTip("Toutes les entités de la couche, indépendamment de la sélection QGIS, "
                             "ou choix de polygones dans plusieurs couches visibles.")
        footprint_form.addRow("Entités", self.mode)
        self.select_button = QPushButton("Choisir les polygones…", self)
        self.select_button.setToolTip("Clic gauche : ajouter ou retirer un polygone. Clic droit : terminer. "
                                      "Échap : annuler. Les géométries sources restent intactes.")
        footprint_form.addRow("", self.select_button)
        self.selection_label = QLabel("", self)
        self.selection_label.setWordWrap(True)
        footprint_form.addRow("", self.selection_label)
        self.buffer = QDoubleSpinBox(self)
        self.buffer.setRange(0, 100)
        self.buffer.setDecimals(1)
        self.buffer.setValue(25)
        self.buffer.setSuffix(" m")
        self.buffer.setToolTip("Augmente l'emprise extraite de 0 à 100 m (25 m par défaut), "
                               "après transformation des polygones en EPSG:2154.")
        footprint_form.addRow("Tampon", self.buffer)
        self.name = QLineEdit("mnt_lidar_hd", self)
        self.name.setToolTip("Nom du dossier et du VRT. Une extraction existante ne sera jamais écrasée.")
        output_form.addRow("Nom", self.name)
        self.storage = QComboBox(self)
        self.storage.addItem("Disque local", "local")
        self.storage.addItem("Pool commun" if config.POOL_ROOT is not None else "Pool non configuré", "pool")
        self.storage.setToolTip("Disque local recommandé : dossier autonome avec VRT et GeoTIFF. "
                               + ("Pool désactivé : emplacement partagé non configuré."
                                  if config.POOL_ROOT is None else f"Pool partagé : {config.POOL_ROOT}"))
        if config.POOL_ROOT is None:
            self.storage.model().item(1).setEnabled(False)
        output_form.addRow("Stockage", self.storage)
        row = QWidget(self)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.directory = QLineEdit(QSettings().value("lidar_hd_2026/directory", "", type=str), self)
        self.directory.setToolTip("Dossier existant où sera créé le sous-dossier portant le nom de l'extraction.")
        self.browse = QPushButton("Parcourir…", self)
        row_layout.addWidget(self.directory)
        row_layout.addWidget(self.browse)
        output_form.addRow("Dossier", row)
        content_layout.addWidget(self.parameters)
        diagnostic_box = QGroupBox("Prédiagnostic", content)
        diagnostic_layout = QVBoxLayout(diagnostic_box)
        diagnostic_layout.setContentsMargins(12, 18, 12, 14)
        self.diagnostic = QLabel(self)
        self.diagnostic.setTextFormat(Qt.PlainText)
        self.diagnostic.setWordWrap(True)
        self.diagnostic.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.diagnostic.setToolTip("Estimation sans requête IGN, actualisée selon l'emprise et le tampon. "
                                   "Les blocs sont rectangulaires ; les pixels hors polygones ne sont pas masqués.")
        diagnostic_layout.addWidget(self.diagnostic)
        content_layout.addWidget(diagnostic_box)
        content_layout.addStretch()
        self.body_scroll = QScrollArea(self)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body_scroll.setWidget(content)
        layout.addWidget(self.body_scroll, 1)
        self.progress = QProgressBar(self)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.extract = QPushButton("Extraire le MNT", self)
        self.cancel = QPushButton("Annuler la tâche", self)
        self.cancel.setEnabled(False)
        close = QPushButton("Fermer", self)
        for button in (self.extract, self.cancel, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh_diagnostic)
        for signal in (self.layer.layerChanged, self.mode.currentIndexChanged,
                       self.buffer.valueChanged, self.name.textChanged,
                       self.storage.currentIndexChanged, self.directory.textChanged):
            signal.connect(self.schedule_diagnostic)
        self.select_button.clicked.connect(self.start_capture)
        self.browse.clicked.connect(self.choose_directory)
        self.extract.clicked.connect(self.start_extraction)
        self.cancel.clicked.connect(self.cancel_task)
        close.clicked.connect(self.close)
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            signal.connect(self.watch_layers)
            self._signals.append((signal, self.watch_layers))
        self.watch_layers()
        self.refresh_diagnostic()
        available = self.screen().availableGeometry()
        self.setMinimumWidth(min(740, max(320, available.width() - 40)))
        self.resize(min(820, available.width() - 40), min(640, available.height() - 48))

    def watch_layers(self, *_):
        for layer, signal, callback in self._layer_signals:
            try:
                if not sip.isdeleted(layer):
                    signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        self._layer_signals.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if is_polygon(layer):
                for signal in (layer.geometryChanged, layer.featureAdded, layer.featureDeleted,
                               layer.dataChanged, layer.crsChanged):
                    signal.connect(self.schedule_diagnostic)
                    self._layer_signals.append((layer, signal, self.schedule_diagnostic))
        self.selected = {(lid, fid) for lid, fid in self.selected
                         if QgsProject.instance().mapLayer(lid) is not None}
        self.schedule_diagnostic()

    def schedule_diagnostic(self, *_):
        self.plan = None
        self.extract.setEnabled(False)
        if not self._closed:
            self.timer.start()

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Dossier des extractions LiDAR", self.directory.text())
        if path:
            self.directory.setText(path)

    def selected_sources(self):
        if self.mode.currentIndex() == 0:
            return [(self.layer.currentLayer(), None)]
        project = QgsProject.instance()
        ids = sorted({lid for lid, fid in self.selected})
        return [(project.mapLayer(lid), {fid for key, fid in self.selected if key == lid}) for lid in ids]

    def refresh_diagnostic(self):
        self.timer.stop()
        self.plan = None
        self.extract.setEnabled(False)
        cartographic = self.mode.currentIndex() == 1
        self.layer.setEnabled(not cartographic)
        self.select_button.setEnabled(cartographic and self.task is None)
        self.selection_label.setText(
            f"{len(self.selected)} entité(s) · {len({lid for lid, _ in self.selected})} couche(s)")
        self.selection_label.setVisible(cartographic)
        self.directory.setEnabled(self.storage.currentData() == "local")
        self.browse.setEnabled(self.storage.currentData() == "local")
        if self.task is not None:
            return
        try:
            footprint = snapshot(self.selected_sources(), self.buffer.value())
            mode = self.storage.currentData()
            root = storage_root(mode, self.directory.text(), config.POOL_ROOT)
            plan = diagnose(footprint.blocks, config.CACHE_DIR, root, self.name.text(), mode)
            self.diagnostic.setText(
                f"Prédiagnostic sans réseau\n"
                f"Entités : {footprint.features} ; parties polygonales : {footprint.parts}\n"
                f"Surface après tampon (approx.) : {footprint.area_m2 / 10000:.2f} ha\n"
                f"Blocs nécessaires : {len(plan.blocks)} ; valides en cache : {plan.cached}"
                + (f" ; disponibles dans le pool : {plan.pooled}" if mode == "pool" else "")
                + f"\nBlocs à télécharger : {plan.download_count}\n"
                f"Transfert IGN estimé : {plan.estimated_network / 1024**2:.2f} Mio\n"
                f"Copie vers {'le pool' if mode == 'pool' else 'le dossier local'} : "
                f"{plan.estimated_copy / 1024**2:.2f} Mio (approx.)\n"
                f"Estimation par bloc : {plan.mean_bytes / 1024**2:.2f} Mio\n"
                f"Destination : {plan.extraction / (plan.name + '.vrt')}")
            self.plan = plan
            self.extract.setEnabled(True)
        except Exception as error:
            self.diagnostic.setText(str(error))

    def start_capture(self):
        if self.capture is not None or self.task is not None:
            return
        self.before_capture = set(self.selected)
        canvas = self.iface.mapCanvas()
        self.previous_tool = canvas.mapTool()
        self.capture = PolygonSelectionTool(canvas, self.selected, self.finish_capture, self.capture_changed)
        self.capture.messageEmitted.connect(self.capture_message)
        self.hide()
        canvas.setMapTool(self.capture)
        canvas.setFocus()
        self.iface.messageBar().pushMessage("LiDAR HD 2026", "Clic gauche : basculer un polygone ; "
                                           "clic droit : terminer ; Échap : annuler.", duration=8)

    def capture_message(self, message, *_):
        self.iface.messageBar().pushMessage("LiDAR HD 2026", message, level=Qgis.Warning, duration=10)

    def capture_changed(self, selected):
        self.selected = selected
        self.schedule_diagnostic()

    def finish_capture(self, accept, restore=True):
        tool = self.capture
        if tool is None:
            return
        self.capture = None
        tool.done = None  # Ne pas réentrer via deactivate().
        if not accept:
            self.selected = set(self.before_capture)
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is tool:
            canvas.unsetMapTool(tool)
            if self.previous_tool is not None and not sip.isdeleted(self.previous_tool):
                canvas.setMapTool(self.previous_tool)
        tool.clear_bands()
        tool.deleteLater()
        self.previous_tool = None
        if restore and not self._closed:
            self.refresh_diagnostic()
            self.show()
            self.raise_()

    def start_extraction(self):
        # Revérifier tous les paramètres et les conflits juste avant la tâche.
        self.refresh_diagnostic()
        if self.plan is None or self.task is not None:
            return
        QSettings().setValue("lidar_hd_2026/directory", self.directory.text())
        self.task = LidarDownloadTask(self.plan, self.task_finished)
        self.task.progressChanged.connect(self.task_progress)
        self.parameters.setEnabled(False)
        self.extract.setEnabled(False)
        self.cancel.setEnabled(True)
        self.progress.setValue(0)
        QgsApplication.taskManager().addTask(self.task)

    def task_progress(self, value):
        self.progress.setValue(round(value))

    def cancel_task(self):
        if self.task is not None:
            self.task.cancel()
            self.cancel.setEnabled(False)
            self.diagnostic.setText("Annulation demandée ; les blocs validés restent en cache pour la reprise.")

    def task_finished(self, task, success):
        self.task = None
        self.parameters.setEnabled(True)
        self.cancel.setEnabled(False)
        self.refresh_diagnostic()
        try:
            if task.isCanceled():
                self.capture_message("Extraction annulée ; relancez pour reprendre depuis le cache.")
                return
            if not success or task.vrt is None:
                raise RuntimeError(task.error or "La tâche a échoué.")
            raster = QgsRasterLayer(str(task.vrt), task.plan.name, "gdal")
            if not raster.isValid():
                raise RuntimeError(f"VRT créé mais illisible par QGIS : {task.vrt}")
            QgsProject.instance().addMapLayer(raster)
            raster.setCustomProperty("lidar_hd_2026/missing_blocks", len(task.failed))
            try:
                raster.setRenderer(create_altitude_renderer(raster))
            except Exception as error:
                self.capture_message(f"Raster conservé avec rendu par défaut. Échec Turbo : {error}")
            raster.triggerRepaint()
            self.iface.setActiveLayer(raster)
            self.iface.zoomToActiveLayer()
            self.iface.mapCanvas().refreshAllLayers()
            message = (f"{len(task.plan.blocks) - len(task.failed)}/{len(task.plan.blocks)} blocs ; "
                       f"IGN : {task.transferred / 1024**2:.2f} Mio ; "
                       f"copie : {task.copied / 1024**2:.2f} Mio.\n{task.vrt}")
            if task.failed:
                message += "\nMNT INCOMPLET. Relancez avec un autre nom pour récupérer les blocs manquants.\n"
                message += "\n".join(f"{xy} : {reason}" for xy, reason in task.failed[:5])
            self.diagnostic.setText(message)
            self.iface.messageBar().pushMessage("LiDAR HD 2026", message,
                                               level=Qgis.Warning if task.failed else Qgis.Success, duration=15)
        except Exception as error:
            self.diagnostic.setText(str(error))
            self.iface.messageBar().pushMessage("LiDAR HD 2026", str(error), level=Qgis.Critical, duration=15)

    def closeEvent(self, event):
        # Fermer le panneau masque l'interface ; la tâche reste suivie par QGIS.
        self.finish_capture(False, restore=False)
        super().closeEvent(event)

    def shutdown(self):
        self._closed = True
        self.timer.stop()
        self.finish_capture(False, restore=False)
        if self.task is not None:
            self.task.callback = None
            self.task.cancel()
            try:
                self.task.progressChanged.disconnect(self.task_progress)
            except (TypeError, RuntimeError):
                pass
            self.task = None
        for layer, signal, callback in self._layer_signals:
            try:
                if not sip.isdeleted(layer):
                    signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        for signal, callback in self._signals:
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        self._signals.clear()
        self._layer_signals.clear()
        self.close()
        self.deleteLater()
