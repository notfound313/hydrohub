"""Native Qt workflow canvas shared by the builder and run monitor."""
from copy import deepcopy
import math

from PySide6.QtCore import Qt, Signal, QPointF
from PySide6.QtGui import QColor, QPen, QBrush, QPainterPath
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsItem


class WorkflowNode(QGraphicsRectItem):
    def __init__(self, definition, canvas, index):
        super().__init__(0, 0, 230, 82)
        self.definition = definition
        self.canvas = canvas
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsFocusable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges |
                      (QGraphicsItem.GraphicsItemFlag.ItemIsMovable if canvas.editable else QGraphicsItem.GraphicsItemFlag(0)))
        self.setBrush(QBrush(canvas.palette().base().color()))
        self.setPen(QPen(canvas.palette().mid().color(), 2))
        self.setPos(*definition.get("position", [index * 320, 0]))
        self.setToolTip(f"{definition['id']}\n{definition['tool']}" +
                        ("\nDrag output port to another node's input port." if canvas.editable else ""))
        self.status = "Pending"

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        painter.setPen(self.canvas.palette().text().color())
        painter.drawText(12, 22, self.definition["id"])
        painter.drawText(12, 44, self.definition["tool"].split(".")[-1][:30])
        painter.drawText(12, 67, f"{self.definition.get('config_mode', 'global').title()} · {self.status}")
        painter.setBrush(self.canvas.palette().highlight())
        painter.drawEllipse(QPointF(0, 41), 6, 6)
        painter.drawEllipse(QPointF(230, 41), 6, 6)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.scene():
            self.definition["position"] = [self.pos().x(), self.pos().y()]
            self.canvas.update_links()
        return super().itemChange(change, value)

    def focusInEvent(self, event):
        self.setSelected(True)
        super().focusInEvent(event)


class WorkflowCanvas(QGraphicsView):
    selected = Signal(str)
    changed = Signal()

    def __init__(self, parent=None, editable=True):
        super().__init__(parent)
        self.editable = editable
        self.nodes = {}
        self.links = []
        self.connect_from = None
        self.connection_status = "always"
        self.setScene(QGraphicsScene(self))
        self.scene().selectionChanged.connect(self._selected)
        self.setMinimumHeight(200)
        self.setAccessibleName("Automation workflow canvas")
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def _selected(self):
        node = next((item for item in self.scene().selectedItems() if isinstance(item, WorkflowNode)), None)
        if node:
            self.selected.emit(node.definition["id"])

    def set_nodes(self, nodes):
        self.scene().blockSignals(True)
        self.scene().clear()
        self.links = []
        self.nodes = {}
        for index, definition in enumerate(deepcopy(nodes)):
            item = WorkflowNode(definition, self, index)
            self.nodes[definition["id"]] = item
            self.scene().addItem(item)
        self.scene().blockSignals(False)
        self.update_links()

    def definitions(self):
        return [deepcopy(item.definition) for item in self.nodes.values()]

    def update_links(self):
        for item in self.links:
            self.scene().removeItem(item)
        self.links = []
        for source in self.nodes.values():
            connections = [(target, "always") for target in source.definition.get("next", [])]
            for status, targets in source.definition.get("next_on_status", {}).items():
                connections.extend((target, status) for target in targets)
            for target_id, status in connections:
                target = self.nodes.get(target_id)
                if not target:
                    continue
                start, end = source.pos() + QPointF(230, 41), target.pos() + QPointF(0, 41)
                path = QPainterPath(start)
                path.cubicTo(start + QPointF(70, 0), end - QPointF(70, 0), end)
                line = self.scene().addPath(path, QPen(self.palette().highlight().color(), 2))
                line.setZValue(-1)
                arrow = QPainterPath(end)
                arrow.lineTo(end + QPointF(-10, -5))
                arrow.moveTo(end)
                arrow.lineTo(end + QPointF(-10, 5))
                tip = self.scene().addPath(arrow, QPen(self.palette().highlight().color(), 2))
                label = self.scene().addText(status)
                label.setDefaultTextColor(self.palette().text().color())
                label.setPos(path.pointAtPercent(0.5) - QPointF(label.boundingRect().width() / 2, 24))
                self.links.extend((line, tip, label))
        self.scene().setSceneRect(self.scene().itemsBoundingRect().adjusted(-70, -70, 70, 70))

    def connect_nodes(self, source_id, target_id, status="always"):
        if source_id == target_id or source_id not in self.nodes or target_id not in self.nodes:
            return
        node = self.nodes[source_id].definition
        links = node.setdefault("next", []) if status == "always" else node.setdefault("next_on_status", {}).setdefault(status, [])
        if target_id not in links:
            links.append(target_id)
            self.update_links()
            self.changed.emit()

    def mousePressEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if self.editable and event.button() == Qt.MouseButton.LeftButton:
            for identifier, node in self.nodes.items():
                if math.hypot(*(point - (node.pos() + QPointF(230, 41))).toTuple()) < 12:
                    self.connect_from = identifier
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.connect_from:
            point = self.mapToScene(event.position().toPoint())
            for identifier, node in self.nodes.items():
                if math.hypot(*(point - (node.pos() + QPointF(0, 41))).toTuple()) < 18:
                    self.connect_nodes(self.connect_from, identifier, self.connection_status)
                    break
            self.connect_from = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if self.editable:
            self.changed.emit()

    def remove_node(self, identifier):
        definitions = self.definitions()
        for node in definitions:
            node["next"] = [target for target in node.get("next", []) if target != identifier]
            for targets in node.get("next_on_status", {}).values():
                if identifier in targets:
                    targets.remove(identifier)
        self.set_nodes([node for node in definitions if node["id"] != identifier])
        self.changed.emit()

    def show_status(self, statuses):
        colors = {"Running": "#2563eb", "Success": "#15803d", "Warning": "#b45309", "Error": "#b91c1c", "Failed": "#b91c1c"}
        for identifier, node in self.nodes.items():
            node.status = statuses.get(identifier, "Pending")
            node.setPen(QPen(QColor(colors[node.status]) if node.status in colors else self.palette().mid().color(), 3))
            node.update()
