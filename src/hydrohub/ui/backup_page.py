"""Workspace-level selective backup and restore."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from hydrohub.core.backup import create_backup, inspect_backup, restore_backup
from hydrohub.core.model_artifacts import discover_wflow_models
from hydrohub.core.project_manager import ProjectManager
from hydrohub.plugins.custom_tools import CustomToolStore
from hydrohub.ui.background import BackgroundJob


class BackupPage(QWidget):
    restored = Signal()

    def __init__(self, workspace: Path, project_provider):
        super().__init__()
        self.workspace = workspace
        self.project_provider = project_provider
        self.manifest = None
        self._busy = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 22)
        title = QLabel("Backup & Restore")
        title.setObjectName("pageTitle")
        outer.addWidget(title)
        description = QLabel("Export selected project files, models, custom tools, and automation to one ZIP archive. OS keyring secrets are not included.")
        description.setObjectName("muted")
        description.setWordWrap(True)
        outer.addWidget(description)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        form = QFormLayout()
        self.project = QComboBox()
        self.project.currentIndexChanged.connect(self._refresh_choices)
        form.addRow("Project", self.project)
        layout.addLayout(form)
        self.include_project = QCheckBox("Project files (excluding models and custom tools)")
        layout.addWidget(self.include_project)
        self.models = self._selection(layout, "Models")
        self.tools = self._selection(layout, "Custom tools")
        self.automations = self._selection(layout, "Automation definitions + schedule settings")
        self.export_button = QPushButton("Create backup ZIP")
        self.export_button.clicked.connect(self._export)
        layout.addWidget(self.export_button)

        line = QLabel("Restore from ZIP")
        line.setObjectName("sectionTitle")
        layout.addWidget(line)
        row = QHBoxLayout()
        self.archive = QLineEdit()
        self.archive.setReadOnly(True)
        self.archive.setAccessibleName("Backup archive")
        browse = QPushButton("Choose ZIP")
        browse.clicked.connect(self._browse)
        row.addWidget(self.archive, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.contents = QLabel("Choose a backup to see its contents.")
        self.contents.setWordWrap(True)
        layout.addWidget(self.contents)
        self.restore_checks = {}
        for category, label in (("project", "Project"), ("models", "Models"),
                                ("tools", "Custom tools"), ("automation", "Automation")):
            checkbox = QCheckBox(label)
            checkbox.setEnabled(False)
            self.restore_checks[category] = checkbox
            layout.addWidget(checkbox)
        self.restore_button = QPushButton("Restore selected")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._restore)
        layout.addWidget(self.restore_button)
        self.status = QLabel("Ready.")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.refresh_context()

    @staticmethod
    def _selection(layout: QVBoxLayout, label: str) -> QListWidget:
        layout.addWidget(QLabel(label))
        widget = QListWidget()
        widget.setAccessibleName(label)
        widget.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        widget.setMinimumHeight(90)
        layout.addWidget(widget)
        return widget

    def refresh_context(self) -> None:
        previous = self.project.currentIndex()
        selected = self.project.currentData()
        active = self.project_provider()
        manager = ProjectManager(self.workspace / ".hydrohub" / "project_registry.json")
        self.project.blockSignals(True)
        self.project.clear()
        self.project.addItem("No project (global tools / automation)", None)
        for root, info in manager.list_registered().items():
            if (Path(root) / "project.yaml").is_file():
                self.project.addItem(info.get("name", Path(root).name), root)
        if active and self.project.findData(str(active.root)) < 0:
            self.project.addItem(active.name, str(active.root))
        index = self.project.findData(selected if previous >= 0 else (str(active.root) if active else None))
        self.project.setCurrentIndex(max(0, index))
        self.project.blockSignals(False)
        self._refresh_choices()

    def _refresh_choices(self, *_args) -> None:
        root = Path(self.project.currentData()) if self.project.currentData() else None
        self.models.clear()
        self.tools.clear()
        self.automations.clear()
        self.include_project.setEnabled(root is not None)
        if root:
            for model in discover_wflow_models(root):
                self.models.addItem(str(model.relative_to(root / "models")))
                self.models.item(self.models.count() - 1).setData(Qt.ItemDataRole.UserRole, str(model))
        store = CustomToolStore(root or self.workspace, self.workspace / ".hydrohub" / "custom_tools")
        tools, errors = store.discover()
        for tool in tools:
            self.tools.addItem(f"{tool['name']} ({tool['source']})")
            self.tools.item(self.tools.count() - 1).setData(Qt.ItemDataRole.UserRole, tool["id"])
        for path in sorted((self.workspace / ".hydrohub" / "automations").glob("*.yaml")):
            self.automations.addItem(path.stem)
        self.status.setText("; ".join(errors) if errors else "Select items to back up.")

    @staticmethod
    def _selected(widget: QListWidget) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) or item.text() for item in widget.selectedItems()]

    def _run_job(self, function, success) -> None:
        self._busy = True
        self.export_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.status.setText("Working…")

        def done(result):
            self._busy = False
            self.export_button.setEnabled(True)
            self.restore_button.setEnabled(self.manifest is not None)
            success(result)

        def failed(message):
            self._busy = False
            self.export_button.setEnabled(True)
            self.restore_button.setEnabled(self.manifest is not None)
            self.status.setText(message)
            QMessageBox.warning(self, "Backup & Restore", message)

        BackgroundJob(self, function, done, failed).start()

    def _export(self) -> None:
        if self._busy:
            return
        root = Path(self.project.currentData()) if self.project.currentData() else None
        models, tools, automations = self._selected(self.models), self._selected(self.tools), self._selected(self.automations)
        if not (self.include_project.isChecked() and root or models or tools or automations):
            QMessageBox.information(self, "Empty selection", "Select at least one category or item.")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save backup", "hydrohub-backup.zip", "ZIP (*.zip)")
        if filename:
            self._run_job(
                lambda: create_backup(self.workspace, Path(filename), project_root=root,
                                      include_project=bool(root and self.include_project.isChecked()),
                                      models=models, tool_ids=tools, automation_ids=automations),
                lambda manifest: self.status.setText(f"Backup saved: {filename} ({len(manifest['files'])} files)"),
            )

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Open backup", str(self.workspace), "ZIP (*.zip)")
        if not filename:
            return
        try:
            manifest = inspect_backup(Path(filename))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Invalid backup", str(exc))
            return
        self.archive.setText(filename)
        self.manifest = manifest
        counts = {key: sum(item["category"] == key for item in manifest["files"]) for key in self.restore_checks}
        self.contents.setText(f"Project: {manifest.get('project_name') or 'None'}  ·  " + "  |  ".join(
            f"{key}: {count} files" for key, count in counts.items() if count
        ))
        for category, checkbox in self.restore_checks.items():
            checkbox.setEnabled(bool(counts[category]))
            checkbox.setChecked(bool(counts[category]))
        self.restore_button.setEnabled(True)

    def _restore(self) -> None:
        if self._busy or not self.manifest:
            return
        categories = {category for category, checkbox in self.restore_checks.items() if checkbox.isChecked()}
        if not categories:
            QMessageBox.information(self, "Empty selection", "Select at least one category to restore.")
            return
        root = Path(self.project.currentData()) if self.project.currentData() else None
        project_files = any(item["category"] in categories and (
            item["category"] in {"project", "models"} or item["name"].startswith(("tools/config/", "tools/legacy/"))
        ) for item in self.manifest["files"])
        if not project_files:
            root = None
        if project_files and root and self.manifest.get("project_name") and root.name != self.manifest["project_name"]:
            QMessageBox.warning(self, "Project mismatch", "Choose the original project or 'No project' to restore by backup name.")
            return
        prompt = QMessageBox(self)
        prompt.setWindowTitle("Restore backup")
        prompt.setText("Restore selected categories?")
        prompt.setInformativeText("Abort if any selected file already exists, or replace existing files.")
        new_only = prompt.addButton("Abort on conflicts", QMessageBox.ButtonRole.AcceptRole)
        replace = prompt.addButton("Replace existing files", QMessageBox.ButtonRole.DestructiveRole)
        prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.exec()
        if prompt.clickedButton() not in (new_only, replace):
            return
        overwrite = prompt.clickedButton() == replace
        archive = Path(self.archive.text())
        self._run_job(
            lambda: restore_backup(self.workspace, archive, categories=categories, project_root=root, overwrite=overwrite),
            self._restore_done,
        )

    def _restore_done(self, files) -> None:
        self.refresh_context()
        self.status.setText(f"Restored {len(files)} files.")
        self.restored.emit()
