"""Sélection cartographique sans modifier les géométries ni la sélection source."""
from qgis.core import (QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsRectangle, QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMenu

from .spatial import is_polygon


class PolygonSelectionTool(QgsMapTool):
    def __init__(self, canvas, selected, done, changed):
        super().__init__(canvas)
        self.selected = set(selected)  # couples (ID de couche, ID d'entité)
        self.done = done
        self.changed = changed
        self.bands = []
        self.setCursor(Qt.CrossCursor)
        self.redraw()

    def candidates(self, point):
        matches = []
        for layer in self.canvas().layers():
            if not is_polygon(layer):
                continue
            transform = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(),
                                               layer.crs(), QgsProject.instance())
            local = transform.transform(point)
            geom_point = QgsGeometry.fromPointXY(local)
            rect = QgsRectangle(local.x(), local.y(), local.x(), local.y())
            rect.grow(1e-8)
            request = QgsFeatureRequest().setFilterRect(rect)
            request.setSubsetOfAttributes([])
            for feature in layer.getFeatures(request):
                if feature.geometry().intersects(geom_point):
                    matches.append((layer, feature.id()))
        return matches

    def choose(self, matches, pos):
        if len(matches) == 1:
            return matches[0]
        menu = QMenu(self.canvas())
        menu.addSection("Polygones superposés : choisir l'entité à basculer")
        actions = {}
        for layer, fid in matches:
            action = menu.addAction(f"{layer.name()} — entité {fid} [{layer.id()[-8:]}]")
            action.setCheckable(True)
            action.setChecked((layer.id(), fid) in self.selected)
            actions[action] = (layer, fid)
        chosen = menu.exec_(self.canvas().mapToGlobal(pos))
        result = actions.get(chosen)
        menu.deleteLater()
        return result

    def toggle(self, layer, fid):
        key = (layer.id(), fid)
        if key in self.selected:
            self.selected.remove(key)
        else:
            self.selected.add(key)
        self.redraw()
        self.changed(set(self.selected))

    def redraw(self):
        self.clear_bands()
        for layer_id, fid in self.selected:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is None:
                continue
            feature = layer.getFeature(fid)
            if not feature.isValid():
                continue
            band = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
            band.setColor(QColor(0, 180, 220, 180))
            band.setFillColor(QColor(0, 180, 220, 45))
            band.setWidth(2)
            band.setToGeometry(feature.geometry(), layer)
            self.bands.append(band)

    def clear_bands(self):
        for band in self.bands:
            self.canvas().scene().removeItem(band)
        self.bands.clear()

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.done(True)
        elif event.button() == Qt.LeftButton:
            try:
                matches = self.candidates(event.mapPoint())
                chosen = self.choose(matches, event.pos()) if matches else None
                if chosen:
                    self.toggle(*chosen)
            except Exception as error:
                self.messageEmitted.emit(str(error))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.done(False)

    def deactivate(self):
        super().deactivate()
        # Un changement d'outil externe annule aussi la capture.
        if self.done is not None:
            self.done(False)
