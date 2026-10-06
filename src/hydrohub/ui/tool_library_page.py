"""Browse custom tools by global group without moving tool definitions."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHeaderView, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from hydrohub.core.tool_groups import ToolGroups
from hydrohub.plugins.custom_tools import CustomToolStore


class ToolLibraryPage(QWidget):
    open_tool = Signal(str)

    def __init__(self, provider, workspace: Path):
        super().__init__()
        self.provider = provider
        self.workspace = workspace
        self.groups = ToolGroups(workspace / ".hydrohub" / "custom_tools")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Tool library")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel("Find custom tools and organize them into shared groups. Tool files stay in place.")
        description.setObjectName("muted")
        layout.addWidget(description)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tools")
        self.search.setAccessibleName("Search custom tools")
        self.search.textChanged.connect(self.refresh_context)
        self.filter = QComboBox()
        self.filter.setAccessibleName("Filter tool group")
        self.filter.currentIndexChanged.connect(self.refresh_context)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.filter)
        layout.addLayout(controls)

        groups_row = QHBoxLayout()
        for text, action in (("New group", self._create_group), ("Rename group", self._rename_group), ("Delete group", self._delete_group)):
            button = QPushButton(text)
            button.clicked.connect(action)
            groups_row.addWidget(button)
        groups_row.addStretch()
        layout.addLayout(groups_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Group / tool", "Source"))
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setMinimumSectionSize(120)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.setAccessibleName("Custom tool groups")
        self.tree.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.destination = QComboBox()
        self.destination.setAccessibleName("Move selected tool to group")
        move = QPushButton("Assign group")
        move.clicked.connect(self._assign)
        open_button = QPushButton("Open in Custom tools")
        open_button.clicked.connect(self._open)
        actions.addWidget(self.destination)
        actions.addWidget(move)
        actions.addStretch()
        actions.addWidget(open_button)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        self.refresh_context()

    def refresh_context(self, *_args) -> None:
        try:
            data = self.groups.load()
            project = self.provider()
            store = CustomToolStore(project.root if project else self.workspace, self.groups.path.parent)
            tools, errors = store.discover()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        selected_filter = self.filter.currentData()
        selected_destination = self.destination.currentData()
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.addItem("All groups", None)
        self.filter.addItem("Ungrouped", "")
        self.destination.clear()
        self.destination.addItem("Ungrouped", "")
        for group in data["groups"]:
            self.filter.addItem(group, group)
            self.destination.addItem(group, group)
        self.filter.setCurrentIndex(max(0, self.filter.findData(selected_filter)))
        self.destination.setCurrentIndex(max(0, self.destination.findData(selected_destination)))
        self.filter.blockSignals(False)

        self.tree.clear()
        needle = self.search.text().casefold().strip()
        active = self.filter.currentData()
        for group in [*data["groups"], ""]:
            if active is not None and group != active:
                continue
            matches = [tool for tool in tools if data["assignments"].get(tool["id"], "") == group
                       and (needle in tool["name"].casefold() or needle in tool["id"].casefold())]
            if not matches and (needle or group == ""):
                continue
            parent = QTreeWidgetItem(self.tree, [group or "Ungrouped", ""])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for tool in matches:
                item = QTreeWidgetItem(parent, [tool["name"], "Project" if tool["source"] == "legacy" else "Library"])
                item.setData(0, Qt.ItemDataRole.UserRole, tool["id"])
            parent.setExpanded(True)
        for column in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(column)
            self.tree.setColumnWidth(column, self.tree.columnWidth(column) + 24)
        self.status.setText("; ".join(errors) if errors else f"{len(tools)} tools available.")

    def _selected_id(self) -> str:
        item = self.tree.currentItem()
        return str(item.data(0, Qt.ItemDataRole.UserRole) or "") if item else ""

    def _create_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Name")
        if ok:
            self._change(lambda: self.groups.create(name))

    def _rename_group(self) -> None:
        old = self.filter.currentData()
        if not old:
            QMessageBox.information(self, "Select group", "Choose a group in the filter first.")
            return
        name, ok = QInputDialog.getText(self, "Rename group", "Name", text=old)
        if ok:
            self._change(lambda: self.groups.rename(old, name))

    def _delete_group(self) -> None:
        name = self.filter.currentData()
        if not name:
            return
        if QMessageBox.question(self, "Delete group", f"Remove group '{name}'? Its tools become ungrouped.") == QMessageBox.StandardButton.Yes:
            self._change(lambda: self.groups.delete(name))

    def _assign(self) -> None:
        tool_id = self._selected_id()
        if tool_id:
            self._change(lambda: self.groups.assign(tool_id, self.destination.currentData()))

    def _open(self, *_args) -> None:
        tool_id = self._selected_id()
        if tool_id:
            self.open_tool.emit(tool_id)

    def _change(self, action) -> None:
        try:
            action()
            self.refresh_context()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Tool library", str(exc))
