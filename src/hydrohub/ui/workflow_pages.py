from __future__ import annotations

import json
import re
import shutil
import time
import tomllib
from datetime import datetime
from pathlib import Path

import yaml
from PySide6.QtCore import QDateTime, QDir, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFontDatabase, QKeySequence, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QAbstractItemView,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from hydrohub.core.environment_manager import EnvironmentManager
from hydrohub.core.file_manager import ProjectFileManager
from hydrohub.core.project_manager import ProjectContext
from hydrohub.core.model_artifacts import discover_wflow_models
from hydrohub.core.run_registry import RunRecord, RunRegistry
from hydrohub.core.wflow_template import (
    EXCLUSIVE_SETUP_METHODS,
    PLACEHOLDER_CATEGORIES,
    PLACEHOLDER_DEFAULTS,
    build_recipe,
    catalog_keys_by_category,
    discover_setup_methods,
    load_master_template,
)
from hydrohub.core.workflow_manager import WorkflowManager, dump_recipe_yaml
from hydrohub.plugins.registry import ToolDefinition
from hydrohub.ui.code_editor import CodeEditor
from hydrohub.ui.map_viewer import IndonesiaMapWidget
from hydrohub.ui.background import BackgroundJob
from hydrohub.ui.layout_helpers import scroll_panel
from qfluentwidgets import PushButton as QPushButton


class ProjectPage(QWidget):
    def __init__(self, eyebrow: str, title: str, description: str, project_provider) -> None:
        super().__init__()
        self.project_provider = project_provider
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 18, 24, 22)
        self.layout.setSpacing(12)
        overline = QLabel(eyebrow)
        overline.setObjectName("eyebrow")
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        copy = QLabel(description)
        copy.setObjectName("muted")
        copy.setWordWrap(True)
        self.layout.addWidget(overline)
        self.layout.addWidget(heading)
        self.layout.addWidget(copy)
        self.layout.addSpacing(4)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        self.layout.addWidget(divider)

    def project(self) -> ProjectContext | None:
        return self.project_provider()

    def require_project(self) -> ProjectContext | None:
        project = self.project()
        if not project:
            QMessageBox.information(
                self,
                "Project required",
                "Create or open a HydroHub project before using this workspace.",
            )
        return project

    def refresh_context(self) -> None:
        pass


class ProjectOverviewPage(ProjectPage):
    aoi_changed = Signal(dict)

    def __init__(self, project_provider, workflow: WorkflowManager) -> None:
        super().__init__("PROJECT", "Project overview", "State is derived from persisted project artifacts and validation results.", project_provider)
        self.workflow = workflow
        self.map = IndonesiaMapWidget()
        self.summary = QLabel("No active project")
        self.summary.setObjectName("sectionTitle")
        self.summary.setWordWrap(True)
        self.map.setMinimumHeight(300)
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)
        panel_layout.addWidget(self.summary)
        aoi_form = QFormLayout()
        bounds = QHBoxLayout()
        self.aoi_fields = []
        for placeholder, value in (("West", 95.0), ("South", -11.0), ("East", 141.0), ("North", 6.0)):
            field = QLineEdit(str(value))
            field.setPlaceholderText(placeholder)
            self.aoi_fields.append(field)
            bounds.addWidget(field)
        aoi_form.addRow("AOI bounds", bounds)
        import_row = QHBoxLayout()
        self.aoi_file = QLineEdit()
        self.aoi_file.setPlaceholderText("Optional GeoJSON or Shapefile ZIP (Polygon/MultiPolygon)")
        import_row.addWidget(self.aoi_file, 1)
        browse_aoi = QPushButton("Browse")
        browse_aoi.clicked.connect(self._browse_aoi)
        import_row.addWidget(browse_aoi)
        aoi_form.addRow("Import AOI", import_row)
        save_aoi = QPushButton("Set AOI")
        save_aoi.setObjectName("primary")
        save_aoi.clicked.connect(self._save_aoi)
        aoi_form.addRow("", save_aoi)
        panel_layout.addLayout(aoi_form)
        panel_layout.addWidget(self.map)
        self.layout.addWidget(panel)
        self.layout.addStretch()

    def refresh_context(self) -> None:
        project = self.project()
        if not project:
            self.summary.setText("No active project. Create or open a project from the Dashboard.")
            return
        ui_state = self.workflow.load_ui_state(project)
        catalog_file = ui_state.get("active_catalog_file", "wflow_catalog.yml")
        recipe_file = ui_state.get("active_wflow_file", "wflow.yml")
        catalog = self.workflow.catalog_path(project, catalog_file)
        recipe = self.workflow.recipe_path(project, recipe_file)
        runtime = project.root / "configs" / "wflow" / "wflow_sbm.toml"
        self.summary.setText(
            f"{project.name}\n{project.root}\n\n"
            f"Catalog ({catalog_file}): {'Created' if catalog.exists() else 'Not created'}\n"
            f"HydroMT recipe ({recipe_file}): {'Created' if recipe.exists() else 'Blocked by catalog'}\n"
            f"Runtime config: {'Created' if runtime.exists() else 'Not created'}"
        )
        aoi_path = project.root / "workspace" / "aoi.geojson"
        if aoi_path.exists():
            feature = json.loads(aoi_path.read_text(encoding="utf-8"))
            self.map.set_aoi(feature)
            for field, value in zip(self.aoi_fields, self.workflow.aoi_bounds(feature)):
                field.setText(str(value))

    def _browse_aoi(self) -> None:
        project = self.project()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select AOI file", str(project.root if project else Path.cwd()), "AOI files (*.geojson *.json *.zip)"
        )
        if path:
            self.aoi_file.setText(path)

    def _save_aoi(self) -> None:
        project = self.require_project()
        if not project:
            return
        try:
            if self.aoi_file.text().strip():
                data = self.workflow.load_aoi_file(Path(self.aoi_file.text().strip()), f"{project.name} AOI")
                feature = self.workflow.save_aoi_geojson(project, data)
            else:
                feature = self.workflow.save_aoi_bbox(project, *(float(field.text()) for field in self.aoi_fields))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "AOI could not be saved", str(exc))
            return
        self.map.set_aoi(feature)
        self.aoi_changed.emit(feature)
        self.refresh_context()


class FileFilterProxy(QSortFilterProxyModel):
    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        if super().filterAcceptsRow(source_row, source_parent):
            return True
        index = self.sourceModel().index(source_row, 0, source_parent)
        for row in range(self.sourceModel().rowCount(index)):
            if self.filterAcceptsRow(row, index):
                return True
        return False


class ProjectTreeView(QTreeView):
    moveRequested = Signal(object, object)

    def dropEvent(self, event) -> None:  # noqa: N802
        source = self.currentIndex()
        if not source.isValid():
            event.ignore()
            return
        self.moveRequested.emit(source, self.indexAt(event.position().toPoint()))
        event.acceptProposedAction()


class FileWorkspacePage(QWidget):
    TEXT_EXTENSIONS = {".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".py", ".jl", ".xml", ".md"}

    def __init__(self, project_provider) -> None:
        super().__init__()
        self.project_provider = project_provider
        self.open_files: dict[Path, CodeEditor] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        command_bar = QFrame()
        command_bar.setObjectName("editorBar")
        command_layout = QHBoxLayout(command_bar)
        command_layout.setContentsMargins(12, 8, 12, 8)
        command_layout.setSpacing(12)
        self.workspace_path = QLabel("WORKSPACE / No project")
        self.workspace_path.setObjectName("eyebrow")
        command_layout.addWidget(self.workspace_path, 1)
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        save_all = QPushButton("Save all")
        save_all.clicked.connect(self._save_all)
        command_layout.addWidget(save)
        command_layout.addWidget(save_all)
        layout.addWidget(command_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        explorer = QFrame()
        explorer.setObjectName("editorSidebar")
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(8, 8, 8, 8)
        explorer_layout.setSpacing(8)
        explorer_header = QHBoxLayout()
        explorer_header.setSpacing(8)
        title = QLabel("PROJECT FILES")
        title.setObjectName("eyebrow")
        new_file = QPushButton("+ File")
        new_file.clicked.connect(self._new_file)
        new_folder = QPushButton("+ Folder")
        new_folder.clicked.connect(self._new_folder)
        explorer_header.addWidget(title, 1)
        explorer_header.addWidget(new_file)
        explorer_header.addWidget(new_folder)
        explorer_layout.addLayout(explorer_header)
        file_actions = QHBoxLayout()
        file_actions.setSpacing(8)
        upload = QPushButton("Upload")
        upload.clicked.connect(self._upload_files)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete_selected)
        file_actions.addWidget(upload)
        file_actions.addWidget(delete)
        explorer_layout.addLayout(file_actions)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter workspace files...")
        self.filter.setClearButtonEnabled(True)
        explorer_layout.addWidget(self.filter)
        self.model = QFileSystemModel(self)
        self.model.setReadOnly(False)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.proxy = FileFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.tree = ProjectTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(16)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAccessibleName("Project files")
        self.tree.setAccessibleDescription(
            "Drag a file or folder onto another folder to move it."
        )
        self.tree.setToolTip("Drag a file or folder onto another folder to move it.")
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_file_context_menu)
        for column in range(1, 4):
            self.tree.hideColumn(column)
        self.tree.doubleClicked.connect(self._open_index)
        self.tree.moveRequested.connect(self._move_dropped)
        explorer_layout.addWidget(self.tree, 1)
        splitter.addWidget(explorer)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.breadcrumb = QLabel("Select a file from Project Files")
        self.breadcrumb.setObjectName("editorBreadcrumb")
        self.breadcrumb.setContentsMargins(12, 6, 12, 6)
        right_layout.addWidget(self.breadcrumb)
        self.editors = QTabWidget()
        self.editors.setTabsClosable(True)
        self.editors.setMovable(True)
        self.editors.setDocumentMode(True)
        self.editors.tabCloseRequested.connect(self._close_tab)
        self.editors.currentChanged.connect(self._tab_changed)
        right_layout.addWidget(self.editors, 4)
        self.bottom_panel = QTabWidget()
        self.bottom_panel.setMaximumHeight(180)
        self.problems = QPlainTextEdit()
        self.problems.setReadOnly(True)
        self.problems.setPlaceholderText("No problems detected.")
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Editor operations appear here.")
        self.bottom_panel.addTab(self.problems, "Problems")
        self.bottom_panel.addTab(self.output, "Output")
        right_layout.addWidget(self.bottom_panel, 1)
        splitter.addWidget(right)
        splitter.setSizes([300, 900])
        layout.addWidget(splitter, 1)

        self.editor_status = QLabel("No file | UTF-8 | Spaces: 4")
        self.editor_status.setObjectName("editorStatus")
        self.editor_status.setContentsMargins(10, 4, 10, 4)
        layout.addWidget(self.editor_status)
        save_action = QAction(self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save)
        self.addAction(save_action)

    def project(self) -> ProjectContext | None:
        return self.project_provider()

    def refresh_context(self) -> None:
        project = self.project()
        if not project:
            return
        root_index = self.model.setRootPath(str(project.root))
        self.tree.setRootIndex(self.proxy.mapFromSource(root_index))
        self.workspace_path.setText(f"WORKSPACE / {project.name} / {project.root}")

    def _show_file_context_menu(self, position) -> None:
        index = self.tree.indexAt(position)
        if not index.isValid():
            return
        self.tree.setCurrentIndex(index)
        source_index = self.proxy.mapToSource(index)
        path = Path(self.model.filePath(source_index)).resolve()
        if not path.exists():
            return
        menu = QMenu(self.tree)
        copy_path = menu.addAction("Copy path")
        if menu.exec(self.tree.viewport().mapToGlobal(position)) is copy_path:
            self._copy_path(path)

    def _copy_path(self, path: Path) -> None:
        path = path.resolve()
        QApplication.clipboard().setText(str(path))
        self.editor_status.setText(f"Copied path: {path}")

    def _open_index(self, index) -> None:
        source_index = self.proxy.mapToSource(index)
        path = Path(self.model.filePath(source_index))
        if not path.is_file():
            return
        if path in self.open_files:
            self.editors.setCurrentWidget(self.open_files[path])
            return
        editor = CodeEditor()
        editor.setProperty("filePath", str(path))
        if path.suffix.lower() not in self.TEXT_EXTENSIONS:
            editor.setPlainText("Binary/geospatial file. Open it with Map, Table, or Dataset Inspector.")
            editor.setReadOnly(True)
        else:
            editor.setPlainText(path.read_text(encoding="utf-8", errors="replace"))
            editor.document().setModified(False)
            editor.set_language(self._language(path))
            editor.modificationChanged.connect(lambda modified, e=editor: self._mark_modified(e, modified))
        self.open_files[path] = editor
        index = self.editors.addTab(editor, path.name)
        self.editors.setCurrentIndex(index)
        self._validate_editor(editor)

    def _save(self) -> None:
        editor = self.editors.currentWidget()
        if not isinstance(editor, CodeEditor):
            return
        self._save_editor(editor)

    def _save_all(self) -> None:
        for editor in self.open_files.values():
            if editor.document().isModified():
                self._save_editor(editor)

    def _save_editor(self, editor: CodeEditor) -> None:
        project = self.project()
        path_value = editor.property("filePath")
        if not project or not path_value or editor.isReadOnly():
            return
        path = Path(path_value)
        try:
            path.resolve().relative_to(project.root.resolve())
        except ValueError:
            QMessageBox.critical(self, "Save blocked", "The selected file is outside the active project.")
            return
        path.write_text(editor.toPlainText(), encoding="utf-8")
        editor.document().setModified(False)
        self.output.appendPlainText(f"Saved {path}")
        self._validate_editor(editor)

    def _close_tab(self, index: int) -> None:
        editor = self.editors.widget(index)
        if isinstance(editor, CodeEditor) and editor.document().isModified():
            answer = QMessageBox.question(self, "Unsaved changes", "Close this file without saving changes?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        if isinstance(editor, CodeEditor):
            path = Path(editor.property("filePath"))
            self.open_files.pop(path, None)
        self.editors.removeTab(index)
        editor.deleteLater()

    def _tab_changed(self, index: int) -> None:
        editor = self.editors.widget(index)
        if not isinstance(editor, CodeEditor):
            self.breadcrumb.setText("Select a file from Project Files")
            return
        path = Path(editor.property("filePath"))
        project = self.project()
        relative = path.relative_to(project.root) if project else path
        self.breadcrumb.setText("  >  ".join(relative.parts))
        cursor = editor.textCursor()
        self.editor_status.setText(
            f"{self._language(path).upper()} | Ln {cursor.blockNumber() + 1}, Col {cursor.columnNumber() + 1} | UTF-8 | Spaces: 4"
        )

    def _mark_modified(self, editor: CodeEditor, modified: bool) -> None:
        index = self.editors.indexOf(editor)
        if index >= 0:
            path = Path(editor.property("filePath"))
            self.editors.setTabText(index, path.name + (" *" if modified else ""))

    def _validate_editor(self, editor: CodeEditor) -> None:
        path = Path(editor.property("filePath"))
        try:
            if path.suffix.lower() in {".yaml", ".yml"}:
                yaml.safe_load(editor.toPlainText())
            elif path.suffix.lower() == ".json":
                json.loads(editor.toPlainText())
            self.problems.setPlainText("No syntax problems detected.")
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            self.problems.setPlainText(str(exc))
            self.bottom_panel.setCurrentWidget(self.problems)

    def _new_file(self) -> None:
        project = self.project()
        if not project:
            return
        manager = ProjectFileManager(project.root)
        directory = manager.directory_for(self._selected_path())
        name, accepted = QInputDialog.getText(self, "New file", f"Name in {directory.relative_to(project.root) or '.'}")
        if not accepted or not name.strip():
            return
        try:
            path = manager.create_file(directory, name)
            self.output.appendPlainText(f"Created {path}")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "File not created", str(exc))

    def _new_folder(self) -> None:
        project = self.project()
        if not project:
            return
        manager = ProjectFileManager(project.root)
        directory = manager.directory_for(self._selected_path())
        name, accepted = QInputDialog.getText(self, "New folder", f"Name in {directory.relative_to(project.root) or '.'}")
        if not accepted or not name.strip():
            return
        try:
            path = manager.create_folder(directory, name)
            self.output.appendPlainText(f"Created {path}")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Folder not created", str(exc))

    def _selected_path(self) -> Path | None:
        index = self.tree.currentIndex()
        if not index.isValid():
            return None
        return Path(self.model.filePath(self.proxy.mapToSource(index)))

    def _upload_files(self) -> None:
        project = self.project()
        if not project:
            return
        manager = ProjectFileManager(project.root)
        directory = manager.directory_for(self._selected_path())
        selected, _ = QFileDialog.getOpenFileNames(self, "Upload files to project", str(project.root))
        if not selected:
            return
        try:
            copied = manager.upload([Path(path) for path in selected], directory)
            self.output.appendPlainText(f"Uploaded {len(copied)} file(s) to {directory}")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Upload failed", str(exc))

    def _move_dropped(self, source_index, destination_index) -> None:
        project = self.project()
        if not project:
            return
        source = Path(self.model.filePath(self.proxy.mapToSource(source_index)))
        destination = project.root
        if destination_index.isValid():
            destination = Path(
                self.model.filePath(self.proxy.mapToSource(destination_index))
            )
        if not destination.is_dir():
            QMessageBox.warning(
                self, "Move failed", "Drop the item onto a project folder."
            )
            return
        self._move_path(source, destination)

    def _move_path(self, source: Path, destination: Path) -> None:
        project = self.project()
        if not project:
            return
        if source.parent.resolve() == destination.resolve():
            return
        if self._has_modified_paths(source):
            QMessageBox.warning(self, "Move blocked", "Save or close modified files before moving this item.")
            return
        manager = ProjectFileManager(project.root)
        try:
            target = manager.move(source, destination)
            self._close_paths_under(source)
            self.output.appendPlainText(f"Moved {source} to {target}")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Move failed", str(exc))

    def _delete_selected(self) -> None:
        project = self.project()
        selected = self._selected_path()
        if not project or selected is None:
            QMessageBox.information(self, "Select an item", "Select a file or folder to delete.")
            return
        try:
            relative = ProjectFileManager(project.root).checked(selected, allow_root=False).relative_to(project.root.resolve())
        except ValueError as exc:
            QMessageBox.warning(self, "Delete blocked", str(exc))
            return
        if self._has_modified_paths(selected):
            QMessageBox.warning(self, "Delete blocked", "Save or close modified files before deleting this item.")
            return
        answer = QMessageBox.warning(
            self,
            "Delete project item",
            f"Permanently delete '{relative}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ProjectFileManager(project.root).delete(selected)
            self._close_paths_under(selected)
            self.output.appendPlainText(f"Deleted {selected}")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))

    def _close_paths_under(self, path: Path) -> None:
        base = path.resolve()
        for open_path, editor in list(self.open_files.items()):
            try:
                open_path.resolve().relative_to(base)
            except ValueError:
                continue
            index = self.editors.indexOf(editor)
            if index >= 0:
                self.editors.removeTab(index)
            self.open_files.pop(open_path, None)
            editor.deleteLater()

    def _has_modified_paths(self, path: Path) -> bool:
        base = path.resolve()
        for open_path, editor in self.open_files.items():
            try:
                open_path.resolve().relative_to(base)
            except ValueError:
                continue
            if editor.document().isModified():
                return True
        return False

    @staticmethod
    def _language(path: Path) -> str:
        return {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml", ".py": "python", ".jl": "julia"}.get(path.suffix.lower(), "text")


class CatalogBuilderPage(ProjectPage):
    def __init__(self, project_provider, workflow: WorkflowManager, changed) -> None:
        super().__init__("MODEL / DATA CATALOG", "Wflow catalog workbench", "Manage catalog entries in form view and edit the generated YAML directly.", project_provider)
        self.workflow = workflow
        self.changed = changed
        file_actions = QHBoxLayout()
        file_actions.setSpacing(12)
        self.catalog_files = QComboBox()
        self.catalog_files.setMinimumWidth(220)
        self.catalog_files.currentTextChanged.connect(self._catalog_changed)
        create = QPushButton("New catalog file")
        create.clicked.connect(self._create)
        file_actions.addWidget(QLabel("Active catalog"))
        file_actions.addWidget(self.catalog_files)
        file_actions.addWidget(create)
        file_actions.addStretch()
        self.layout.addLayout(file_actions)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        import_button = QPushButton("Import YAML")
        import_button.clicked.connect(self._import_yaml)
        validate = QPushButton("Validate catalog")
        validate.clicked.connect(self._validate)
        actions.addWidget(import_button)
        actions.addWidget(validate)
        actions.addStretch()
        self.layout.addLayout(actions)

        workbench = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        list_title = QLabel("CATALOG SOURCE")
        list_title.setObjectName("eyebrow")
        left_layout.addWidget(list_title)
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.catalog_keys = QComboBox()
        self.catalog_keys.setMinimumWidth(220)
        self.catalog_keys.currentTextChanged.connect(self._select_entry)
        delete_key = QPushButton("Delete key")
        delete_key.clicked.connect(self._delete_key)
        source_row.addWidget(self.catalog_keys, 1)
        source_row.addWidget(delete_key)
        left_layout.addLayout(source_row)
        self.source_state = QLabel("Select a Catalog source to inspect its URI.")
        self.source_state.setObjectName("muted")
        self.source_state.setWordWrap(True)
        left_layout.addWidget(self.source_state)

        panel = QFrame()
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.data_type = QComboBox()
        self.data_type.addItems(["RasterDataset", "GeoDataFrame", "DataFrame", "Dataset"])
        self.key = QLineEdit()
        self.key.setPlaceholderText("e.g. merit_dem")
        self.driver = QComboBox()
        self.driver.setEditable(True)
        self.driver.addItems(["rasterio", "raster_xarray", "pyogrio", "netcdf", "pandas"])
        self.uri = QLineEdit()
        self.uri.setPlaceholderText("e.g. data/hydro/{variable}*.tif or ${GLOBAL_DB}/dem/{basin}.tif")
        uri_row = QHBoxLayout()
        uri_row.setSpacing(8)
        uri_row.addWidget(self.uri, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_uri)
        uri_row.addWidget(browse)
        self.crs = QLineEdit("4326/ESRI:54052")
        self.category = QComboBox()
        self.category.addItems(
            [
                "dem",
                "hydrography",
                "river_geometry",
                "forcing",
                "meteo",
                "rainfall",
                "soil",
                "landuse",
                "landcover",
                "vegetation",
                "pet",
                "reference",
            ]
        )
        for label, widget in (("Template / type", self.data_type), ("Catalog key", self.key), ("Driver", self.driver)):
            form.addRow(label, widget)
        form.addRow("URI", uri_row)
        self.placeholders = QPlainTextEdit()
        self.placeholders.setMaximumHeight(92)
        self.placeholders.setPlaceholderText(
            "YAML mapping, e.g.\nvariable: [flwdir, elevtn]\nbasin: {solo: [110.0, -7.0]}"
        )
        form.addRow("URI variables", self.placeholders)
        self.driver_options = QPlainTextEdit()
        self.driver_options.setMaximumHeight(108)
        self.driver_options.setPlaceholderText(
            "YAML mapping, e.g.\nchunks: {x: 2000, y: 2000}\ndecode_times: true"
        )
        form.addRow("Driver options", self.driver_options)
        self.uri_resolution = QLabel("Available variables appear after selecting a source.")
        self.uri_resolution.setObjectName("muted")
        self.uri_resolution.setWordWrap(True)
        form.addRow("Resolved URI", self.uri_resolution)
        form.addRow("CRS", self.crs)
        form.addRow("Category", self.category)
        self.extra_metadata = QPlainTextEdit()
        self.extra_metadata.setMaximumHeight(108)
        self.extra_metadata.setPlaceholderText(
            "YAML mapping, e.g.\nnotes: Local dataset\nsource_license: CC-BY-4.0"
        )
        form.addRow("Additional metadata", self.extra_metadata)
        add = QPushButton("Add catalog entry")
        add.setObjectName("primary")
        add.clicked.connect(self._add)
        form.addRow("", add)
        left_layout.addWidget(panel)
        workbench.addWidget(scroll_panel(left))

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_header = QHBoxLayout()
        self.editor_title = QLabel("No catalog selected")
        self.editor_title.setObjectName("sectionTitle")
        self.validation = QLabel("Catalog has not been validated.")
        self.validation.setObjectName("muted")
        self.validation.setWordWrap(True)
        save_yaml = QPushButton("Save YAML")
        save_yaml.setObjectName("primary")
        save_yaml.clicked.connect(self._save_yaml)
        editor_header.addWidget(self.editor_title)
        editor_header.addStretch()
        editor_header.addWidget(save_yaml)
        self.preview = CodeEditor()
        self.preview.set_language("yaml")
        self.preview.setPlaceholderText("Create or import a catalog to begin editing YAML.")
        editor_layout.addLayout(editor_header)
        editor_layout.addWidget(self.preview, 1)
        editor_layout.addWidget(self.validation)
        workbench.addWidget(editor_panel)
        workbench.setSizes([560, 620])
        self.layout.addWidget(workbench, 1)

    def refresh_context(self) -> None:
        project = self.project()
        if not project:
            self.catalog_files.clear()
            self.preview.clear()
            self.catalog_keys.clear()
            self.editor_title.setText("No catalog selected")
            return
        files = self.workflow.list_catalogs(project)
        state = self.workflow.load_ui_state(project)
        current = self.catalog_files.currentText()
        selected = state.get("active_catalog_file", "wflow_catalog.yml")
        if selected not in files:
            selected = current if current in files else (files[0] if files else "")
        self.catalog_files.blockSignals(True)
        self.catalog_files.clear()
        self.catalog_files.addItems(files)
        self.catalog_files.setCurrentText(selected)
        self.catalog_files.blockSignals(False)
        if selected and state.get("active_catalog_file") != selected:
            self.workflow.save_ui_state(project, active_catalog_file=selected)
        self.editor_title.setText(
            f"data / catalog / {selected}" if selected else "No catalog selected"
        )
        path = self.workflow.catalog_path(project, selected) if selected else None
        self.preview.setPlainText(path.read_text(encoding="utf-8") if path and path.exists() else "")
        self.preview.document().setModified(False)
        current_key = self.catalog_keys.currentText()
        keys: list[str] = []
        if path and path.exists():
            try:
                catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                catalog = {}
            keys = [
                key for key, entry in catalog.items() if key != "meta" and isinstance(entry, dict)
            ]
        self.catalog_keys.blockSignals(True)
        self.catalog_keys.clear()
        self.catalog_keys.addItems(keys)
        self.catalog_keys.setCurrentText(current_key if current_key in keys else (keys[0] if keys else ""))
        self.catalog_keys.blockSignals(False)
        self._select_entry()

    def _create(self) -> None:
        project = self.require_project()
        if not project:
            return
        filename, accepted = QInputDialog.getText(
            self, "New catalog file", "File name", text="wflow_catalog.yml"
        )
        if not accepted or not filename.strip():
            return
        filename = filename.strip()
        if not Path(filename).suffix:
            filename += ".yml"
        try:
            path = self.workflow.catalog_path(project, filename)
            if path.exists():
                raise ValueError(f"Catalog file already exists: {filename}")
            self.workflow.create_catalog(project, filename)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Catalog was not created", str(exc))
            return
        self.workflow.save_ui_state(project, active_catalog_file=filename)
        self.changed()

    def _catalog_changed(self, filename: str) -> None:
        project = self.project()
        if not project or not filename:
            return
        self.workflow.save_ui_state(project, active_catalog_file=filename)
        self.changed()

    def _add(self) -> None:
        project = self.require_project()
        if not project:
            return
        try:
            placeholders = yaml.safe_load(self.placeholders.toPlainText()) or {}
            if not isinstance(placeholders, dict):
                raise ValueError("URI variables must be a YAML mapping.")
            driver_options = yaml.safe_load(self.driver_options.toPlainText()) or {}
            if not isinstance(driver_options, dict):
                raise ValueError("Driver options must be a YAML mapping.")
            metadata = yaml.safe_load(self.extra_metadata.toPlainText()) or {}
            if not isinstance(metadata, dict):
                raise ValueError("Additional metadata must be a YAML mapping.")
            self.workflow.add_catalog_entry(
                project,
                self.key.text(),
                self.data_type.currentText(),
                self.driver.currentText(),
                self.uri.text(),
                self.crs.text(),
                self.category.currentText(),
                self.catalog_files.currentText(),
                placeholders,
                driver_options=driver_options,
                metadata=metadata,
            )
        except (ValueError, OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "Catalog entry was not added", str(exc))
            return
        self.refresh_context()
        self._validate()
        self.changed()

    def _save_yaml(self) -> None:
        project = self.require_project()
        if not project:
            return
        try:
            self.workflow.save_catalog_yaml(
                project, self.preview.toPlainText(), self.catalog_files.currentText()
            )
        except ValueError as exc:
            self.validation.setText(str(exc))
            return
        self.preview.document().setModified(False)
        self.validation.setText("READY: catalog YAML saved and validated.")
        self.refresh_context()
        self.changed()

    def _import_yaml(self) -> None:
        project = self.require_project()
        if not project:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Import catalog YAML", str(project.root), "YAML (*.yml *.yaml)")
        if path:
            self.preview.setPlainText(Path(path).read_text(encoding="utf-8"))
            self.preview.document().setModified(True)

    def _browse_uri(self) -> None:
        project = self.require_project()
        if not project:
            return
        data_root = project.root / "data"
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select catalog source", str(data_root), "All files (*)"
        )
        if path:
            selected = Path(path)
            try:
                self.uri.setText(selected.resolve().relative_to(data_root.resolve()).as_posix())
            except ValueError:
                self.uri.setText(str(selected))

    def _select_entry(self, _key: str = "") -> None:
        project = self.project()
        if not project:
            return
        key = self.catalog_keys.currentText()
        filename = self.catalog_files.currentText()
        if not filename or not key:
            self.source_state.setObjectName("muted")
            self.source_state.setText("No Catalog sources are available in this file.")
            self.uri_resolution.setText(
                "Add a source or edit the Catalog YAML to configure URI variables."
            )
            return
        path = self.workflow.catalog_path(project, filename)
        catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = catalog.get(key, {})
        self.key.setText(key)
        self.data_type.setCurrentText(str(entry.get("data_type", "Dataset")))
        driver = entry.get("driver", {})
        self.driver.setCurrentText(driver.get("name", "") if isinstance(driver, dict) else str(driver))
        options = driver.get("options", {}) if isinstance(driver, dict) else {}
        self.driver_options.setPlainText(
            yaml.safe_dump(options, sort_keys=False).strip() if options else ""
        )
        self.uri.setText(str(entry.get("uri", "")))
        metadata = entry.get("metadata", {}) or {}
        self.crs.setText(str(metadata.get("crs", "")))
        self.category.setCurrentText(str(metadata.get("category", "reference")))
        extra_metadata = {
            name: value
            for name, value in metadata.items()
            if name not in {"crs", "category"}
        }
        self.extra_metadata.setPlainText(
            yaml.safe_dump(extra_metadata, sort_keys=False).strip()
            if extra_metadata
            else ""
        )
        configured = (
            entry.get("placeholders")
            or entry.get("variables")
            or metadata.get("placeholders")
            or metadata.get("variables")
            or {}
        )
        self.placeholders.setPlainText(
            yaml.safe_dump(configured, sort_keys=False).strip() if configured else ""
        )
        resolution = self.workflow.resolve_catalog_uri(project, catalog, key)
        variables = ", ".join(
            f"{{{name}}} = {', '.join(values)}"
            for name, values in resolution.placeholders.items()
        )
        patterns = "\n".join(resolution.patterns) or "No resolved pattern"
        if resolution.valid:
            self.source_state.setText(
                f"READY · {len(resolution.matches)} matching file(s)"
            )
            self.source_state.setObjectName("statusReady")
            self.uri_resolution.setText(
                f"{variables or 'No dynamic placeholders'}\n{patterns}"
            )
        else:
            self.source_state.setText("WARNING · " + " ".join(resolution.issues))
            self.source_state.setObjectName("statusWarning")
            self.uri_resolution.setText(
                f"{variables or 'No placeholder values available'}\n{patterns}"
            )
        for label in (self.source_state, self.uri_resolution):
            label.style().unpolish(label)
            label.style().polish(label)

    def _delete_key(self) -> None:
        project = self.require_project()
        key = self.catalog_keys.currentText()
        filename = self.catalog_files.currentText()
        if not project or not key or not filename:
            return
        answer = QMessageBox.question(
            self,
            "Delete Catalog key",
            f"Delete '{key}' from {filename}? This cannot be undone.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.workflow.delete_catalog_entry(project, key, filename)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "Catalog key was not deleted", str(exc))
            return
        self.refresh_context()
        self.changed()

    def _validate(self) -> None:
        project = self.require_project()
        if not project:
            return
        filename = self.catalog_files.currentText()
        if not filename:
            self.validation.setText("Create or select a catalog file first.")
            return
        valid, issues = self.workflow.validate_catalog(project, filename)
        self.validation.setText("READY: catalog is valid." if valid else "WARNING:\n" + "\n".join(issues))
        self.changed()


class RecipeBuilderPage(ProjectPage):
    def __init__(self, project_provider, workflow: WorkflowManager, changed) -> None:
        super().__init__(
            "MODEL / HYDROMT RECIPE",
            "Wflow Setup Builder",
            "Configure and generate a HydroMT Wflow recipe, then customize the YAML before saving.",
            project_provider,
        )
        self.workflow = workflow
        self.changed = changed
        self.master_template = load_master_template()
        files_row = QHBoxLayout()
        files_row.setSpacing(12)
        self.recipe_files = QComboBox()
        self.recipe_files.setMinimumWidth(220)
        self.recipe_files.currentTextChanged.connect(self._recipe_changed)
        new_recipe = QPushButton("New wflow file")
        new_recipe.clicked.connect(self._create_recipe)
        files_row.addWidget(QLabel("Active wflow file"))
        files_row.addWidget(self.recipe_files)
        files_row.addWidget(new_recipe)
        files_row.addStretch()
        self.layout.addLayout(files_row)
        workbench = QSplitter(Qt.Orientation.Horizontal)
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(16)
        self.status = QLabel("Catalog validation required")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        panel_layout.addWidget(self.status)

        step_label = QLabel("SETUP METHODS AND PARAMETERS")
        step_label.setObjectName("eyebrow")
        panel_layout.addWidget(step_label)
        self.setup_tree = QTreeWidget()
        self.setup_tree.setHeaderLabels(("Setup / parameter", "Value"))
        self.setup_tree.setAlternatingRowColors(True)
        self.setup_tree.setMinimumHeight(360)
        self.setup_tree.setColumnWidth(0, 285)
        self.checks: dict[str, QTreeWidgetItem] = {}
        self.parameter_sources: dict[tuple[str, tuple[str, ...]], QComboBox] = {}
        self._building_setup_tree = True
        self._populate_setup_tree()
        self._building_setup_tree = False
        self.setup_tree.itemChanged.connect(self._setup_item_changed)
        panel_layout.addWidget(self.setup_tree)

        time_form = QFormLayout()
        time_form.setSpacing(12)
        time_form.setVerticalSpacing(16)
        time_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.starttime = QLineEdit("2001-01-01T00:00:00")
        self.endtime = QLineEdit("2001-12-31T00:00:00")
        self.timestep = QSpinBox()
        self.timestep.setRange(60, 86400 * 31)
        self.timestep.setValue(86400)
        time_form.addRow("Start time", self.starttime)
        time_form.addRow("End time", self.endtime)
        time_form.addRow("Timestep seconds", self.timestep)
        panel_layout.addLayout(time_form)

        region_form = QFormLayout()
        self.region_form = region_form
        region_form.setSpacing(12)
        region_form.setVerticalSpacing(16)
        region_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.region_mode = QComboBox()
        self.region_mode.addItems(("bbox", "basin", "subbasin"))
        self.region_input = QComboBox()
        self.region_input.addItems(("Coordinates", "Catalog key"))
        self.west = QLineEdit("105.0")
        self.south = QLineEdit("-9.0")
        self.east = QLineEdit("115.0")
        self.north = QLineEdit("-5.0")
        self.region_lon = QLineEdit("110.0")
        self.region_lat = QLineEdit("-7.0")
        self.region_uparea = QLineEdit("0.0")
        self.region_source = QComboBox()
        self.region_source.currentTextChanged.connect(self._region_source_changed)
        self.region_state = QLabel("Choose coordinates or a Catalog key for the region.")
        self.region_state.setObjectName("muted")
        self.region_state.setWordWrap(True)
        region_form.addRow("Region mode", self.region_mode)
        region_form.addRow("Region input", self.region_input)
        region_form.addRow("West", self.west)
        region_form.addRow("South", self.south)
        region_form.addRow("East", self.east)
        region_form.addRow("North", self.north)
        region_form.addRow("Longitude", self.region_lon)
        region_form.addRow("Latitude", self.region_lat)
        region_form.addRow("Minimum upstream area", self.region_uparea)
        region_form.addRow("Catalog source", self.region_source)
        region_form.addRow("Availability", self.region_state)
        self.region_mode.currentTextChanged.connect(self._region_mode_changed)
        self.region_input.currentTextChanged.connect(self._region_mode_changed)
        panel_layout.addLayout(region_form)

        generate = QPushButton("Generate YAML")
        generate.setObjectName("primary")
        generate.clicked.connect(self._generate)
        panel_layout.addWidget(generate)
        panel_layout.addStretch()
        controls = QScrollArea()
        controls.setWidgetResizable(True)
        controls.setFrameShape(QFrame.Shape.NoFrame)
        controls.setWidget(panel)
        workbench.addWidget(controls)
        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_header = QHBoxLayout()
        editor_header.setSpacing(12)
        self.editor_title = QLabel("No wflow file selected")
        self.editor_title.setObjectName("sectionTitle")
        import_button = QPushButton("Import YAML")
        import_button.clicked.connect(self._import_yaml)
        save = QPushButton("Save wflow.yml")
        save.setObjectName("primary")
        save.clicked.connect(self._save_yaml)
        editor_header.addWidget(self.editor_title)
        editor_header.addStretch()
        editor_header.addWidget(import_button)
        editor_header.addWidget(save)
        self.preview = CodeEditor()
        self.preview.set_language("yaml")
        editor_layout.addLayout(editor_header)
        editor_layout.addWidget(self.preview, 1)
        workbench.addWidget(editor_panel)
        workbench.setSizes([430, 820])
        self.layout.addWidget(workbench, 1)
        self._region_mode_changed("bbox")

    @staticmethod
    def _yaml_value_text(value) -> str:
        text = yaml.safe_dump(value, default_flow_style=True, sort_keys=False).strip()
        return text.removesuffix("\n...").removesuffix("...").rstrip()

    def _populate_setup_tree(self) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        for method in discover_setup_methods(self.master_template):
            group_item = groups.get(method.group)
            if group_item is None:
                group_item = QTreeWidgetItem([method.group, ""])
                group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                group_item.setExpanded(True)
                groups[method.group] = group_item
                self.setup_tree.addTopLevelItem(group_item)
            method_item = QTreeWidgetItem(group_item, [method.name, ""])
            method_item.setData(0, Qt.ItemDataRole.UserRole, ("method", method.name))
            method_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            method_item.setCheckState(
                0, Qt.CheckState.Checked if method.selected else Qt.CheckState.Unchecked
            )
            self.checks[method.name] = method_item
            for parameter in method.parameters:
                self._add_parameter_item(
                    method_item,
                    method.name,
                    (parameter.name,),
                    parameter.value,
                    parameter.selected,
                    parameter.required,
                )
            method_item.setExpanded(method.selected)

    def _add_parameter_item(
        self,
        parent: QTreeWidgetItem,
        method: str,
        path: tuple[str, ...],
        value,
        selected: bool,
        required: bool = False,
    ) -> None:
        label = path[-1] + (" *" if required else "")
        item = QTreeWidgetItem(parent, [label, ""])
        item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            ("parameter", method, path, required),
        )
        flags = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setFlags(flags)
        item.setCheckState(
            0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
        )
        if isinstance(value, dict):
            for name, child_value in value.items():
                self._add_parameter_item(
                    item,
                    method,
                    path + (str(name),),
                    child_value,
                    selected,
                )
            item.setExpanded(selected)
            return
        if path[-1].endswith("_fn"):
            source = QComboBox()
            source.setMinimumWidth(170)
            source.setProperty("catalog_default", "" if value is None else str(value))
            source.setProperty("required", required)
            self.parameter_sources[(method, path)] = source
            self.setup_tree.setItemWidget(item, 1, source)
            return
        item.setText(1, self._yaml_value_text(value))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)

    def _setup_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._building_setup_tree or column != 0:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "method" or item.checkState(0) != Qt.CheckState.Checked:
            return
        method = data[1]
        exclusive = next(
            (methods for methods in EXCLUSIVE_SETUP_METHODS if method in methods), None
        )
        if not exclusive:
            return
        self._building_setup_tree = True
        for other in exclusive - {method}:
            if other in self.checks:
                self.checks[other].setCheckState(0, Qt.CheckState.Unchecked)
        self._building_setup_tree = False

    def refresh_context(self) -> None:
        project = self.project()
        if not project:
            self.status.setText("Create or open a project first.")
            self.recipe_files.clear()
            self.preview.clear()
            self.editor_title.setText("No wflow file selected")
            return
        state = self.workflow.load_ui_state(project)
        catalogs = self.workflow.list_catalogs(project)
        catalog_file = state.get("active_catalog_file", "wflow_catalog.yml")
        if catalog_file not in catalogs:
            catalog_file = catalogs[0] if catalogs else ""
        recipes = self.workflow.list_wflow_recipes(project)
        current_recipe = self.recipe_files.currentText()
        recipe_file = state.get("active_wflow_file", "wflow.yml")
        if recipe_file not in recipes:
            recipe_file = current_recipe if current_recipe in recipes else (recipes[0] if recipes else "")
        self.recipe_files.blockSignals(True)
        self.recipe_files.clear()
        self.recipe_files.addItems(recipes)
        self.recipe_files.setCurrentText(recipe_file)
        self.recipe_files.blockSignals(False)
        if recipe_file and state.get("active_wflow_file") != recipe_file:
            self.workflow.save_ui_state(project, active_wflow_file=recipe_file)
        self.editor_title.setText(
            f"configs / hydromt / {recipe_file}" if recipe_file else "No wflow file selected"
        )
        valid, issues = (
            self.workflow.validate_catalog(project, catalog_file)
            if catalog_file
            else (False, ["Wflow catalog has not been created."])
        )
        self.status.setText("Catalog READY" if valid else "BLOCKED: " + "; ".join(issues))
        catalog_path = self.workflow.catalog_path(project, catalog_file) if catalog_file else None
        catalog = {}
        if catalog_path and catalog_path.exists():
            catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        current_region_source = self.region_source.currentText()
        region_sources = [
            key for key, entry in catalog.items() if key != "meta" and isinstance(entry, dict)
        ]
        self.region_source.blockSignals(True)
        self.region_source.clear()
        self.region_source.addItems(region_sources)
        self.region_source.setCurrentText(
            current_region_source
            if current_region_source in region_sources
            else (region_sources[0] if region_sources else "")
        )
        self.region_source.blockSignals(False)
        self._region_source_changed()
        self._refresh_setup_sources(catalog)
        self._load_aoi(project)
        recipe = self.workflow.recipe_path(project, recipe_file) if recipe_file else None
        self.preview.setPlainText(recipe.read_text(encoding="utf-8") if recipe and recipe.exists() else "")
        self.preview.document().setModified(False)

    @staticmethod
    def _parameter_categories(path: tuple[str, ...], default: str) -> set[str]:
        if default in PLACEHOLDER_CATEGORIES:
            return PLACEHOLDER_CATEGORIES[default]
        name = path[-1]
        if "hydrography" in name:
            return {"dem", "hydrography"}
        if "river_geom" in name:
            return {"hydrography", "river_geometry"}
        if "lulc" in name or "paddy" in name:
            return {"landuse", "landcover", "reference"}
        if "lai" in name:
            return {"landuse", "landcover", "vegetation"}
        if "soil" in name or "ksat" in name:
            return {"soil"}
        if "precip" in name:
            return {"forcing", "meteo", "rainfall"}
        if "pet" in name or "temp" in name:
            return {"forcing", "meteo", "pet"}
        return set()

    def _refresh_setup_sources(self, catalog: dict) -> None:
        for (_method, path), source in self.parameter_sources.items():
            current = source.currentText()
            default = str(source.property("catalog_default") or "")
            categories = self._parameter_categories(path, default)
            options = catalog_keys_by_category(catalog, categories)
            expected = PLACEHOLDER_DEFAULTS.get(default, default)
            is_placeholder = default in PLACEHOLDER_DEFAULTS
            if expected and not is_placeholder and expected not in options:
                options.append(expected)
            source.blockSignals(True)
            source.clear()
            source.setEditable(True)
            if not bool(source.property("required")):
                source.addItem("")
            source.addItems(options)
            if current in options:
                desired = current
            elif expected in options:
                desired = expected
            elif is_placeholder:
                desired = options[0] if options else ""
            else:
                desired = expected
            source.setCurrentText(desired)
            source.blockSignals(False)

    def _create_recipe(self) -> None:
        project = self.require_project()
        if not project:
            return
        filename, accepted = QInputDialog.getText(
            self, "New wflow file", "File name", text="wflow.yml"
        )
        if not accepted or not filename.strip():
            return
        filename = filename.strip()
        if not Path(filename).suffix:
            filename += ".yml"
        try:
            path = self.workflow.recipe_path(project, filename)
            if path.exists():
                raise ValueError(f"Wflow file already exists: {filename}")
            self.workflow.create_wflow_recipe(project, filename)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "wflow file was not created", str(exc))
            return
        self.workflow.save_ui_state(project, active_wflow_file=filename)
        self.changed()

    def _recipe_changed(self, filename: str) -> None:
        project = self.project()
        if not project or not filename:
            return
        self.workflow.save_ui_state(project, active_wflow_file=filename)
        self.changed()

    def _import_yaml(self) -> None:
        project = self.require_project()
        if not project:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Import wflow YAML", str(project.root), "YAML (*.yml *.yaml)")
        if path:
            try:
                self.preview.setPlainText(Path(path).read_text(encoding="utf-8"))
            except OSError as exc:
                QMessageBox.warning(self, "wflow YAML could not be imported", str(exc))
                return
            self.preview.document().setModified(True)

    def _active_catalog(self) -> dict:
        project = self.project()
        if not project:
            return {}
        state = self.workflow.load_ui_state(project)
        files = self.workflow.list_catalogs(project)
        filename = state.get("active_catalog_file", "wflow_catalog.yml")
        if filename not in files:
            filename = files[0] if files else ""
        if not filename:
            return {}
        path = self.workflow.catalog_path(project, filename)
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _region_source_changed(self, _source: str = "") -> None:
        project = self.project()
        source = self.region_source.currentText()
        catalog = self._active_catalog()
        if not project or not source or source not in catalog:
            self.region_state.setText("No Catalog key is available for the region.")
            self._region_mode_changed(self.region_mode.currentText())
            return
        self.region_state.setText(f"Catalog key '{source}' will define the region.")
        self._region_mode_changed(self.region_mode.currentText())

    def _region_mode_changed(self, mode: str = "") -> None:
        mode = self.region_mode.currentText()
        is_bbox = mode == "bbox"
        use_catalog = not is_bbox and self.region_input.currentText() == "Catalog key"
        self.region_input.setEnabled(not is_bbox)
        for widget in (self.west, self.south, self.east, self.north):
            widget.setEnabled(is_bbox)
            self.region_form.setRowVisible(widget, is_bbox)
        for widget in (self.region_lon, self.region_lat):
            widget.setEnabled(not is_bbox and not use_catalog)
            self.region_form.setRowVisible(widget, not is_bbox and not use_catalog)
        self.region_form.setRowVisible(self.region_input, not is_bbox)
        self.region_uparea.setEnabled(mode == "subbasin")
        self.region_form.setRowVisible(self.region_uparea, mode == "subbasin")
        self.region_source.setEnabled(use_catalog)
        self.region_form.setRowVisible(self.region_source, use_catalog)

    def _load_aoi(self, project: ProjectContext) -> None:
        path = project.root / "workspace" / "aoi.geojson"
        if not path.exists():
            return
        try:
            feature = json.loads(path.read_text(encoding="utf-8"))
            west, south, east, north = self.workflow.aoi_bounds(feature)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self.west.setText(str(west))
        self.south.setText(str(south))
        self.east.setText(str(east))
        self.north.setText(str(north))

    def _generate(self) -> None:
        selected = [
            name
            for name, item in self.checks.items()
            if item.checkState(0) == Qt.CheckState.Checked
        ]
        if not selected:
            QMessageBox.warning(self, "No setup steps", "Select at least one setup step.")
            return
        mode = self.region_mode.currentText()
        region_value = None
        if mode != "bbox":
            if self.region_input.currentText() == "Catalog key":
                region_value = self.region_source.currentText()
                if not region_value:
                    QMessageBox.warning(
                        self, "Catalog key unavailable", "Select a Catalog key for the region."
                    )
                    return
                if mode == "subbasin":
                    region_value = {
                        "value": region_value,
                        "uparea": float(self.region_uparea.text()),
                    }
            else:
                coordinates = [float(self.region_lon.text()), float(self.region_lat.text())]
                region_value = coordinates
                if mode == "subbasin":
                    region_value = {
                        "value": coordinates,
                        "uparea": float(self.region_uparea.text()),
                    }
        try:
            step_configs = self._selected_step_configs(selected)
            recipe, removed = build_recipe(
                self.master_template,
                selected,
                PLACEHOLDER_DEFAULTS,
                starttime=self.starttime.text().strip(),
                endtime=self.endtime.text().strip(),
                timestep_seconds=self.timestep.value(),
                region_mode=mode,
                bbox=(
                    float(self.west.text()),
                    float(self.south.text()),
                    float(self.east.text()),
                    float(self.north.text()),
                ),
                outlet=(0.0, 0.0),
                uparea=0.0,
                region_value=region_value,
                step_configs=step_configs,
            )
        except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "wflow.yml was not generated", str(exc))
            return
        self.preview.setPlainText(dump_recipe_yaml(recipe))
        self.preview.document().setModified(True)
        message = f"Generated {len(selected)} setup step(s)."
        if removed:
            message += " Removed unsupported parameters: " + ", ".join(removed)
        self.status.setText(message)

    def _selected_step_configs(self, selected: list[str]) -> dict[str, dict]:
        return {
            method: self._selected_parameters(self.checks[method], method)
            for method in selected
        }

    def _selected_parameters(
        self, parent: QTreeWidgetItem, method: str
    ) -> dict:
        values = {}
        for index in range(parent.childCount()):
            item = parent.child(index)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if not data or data[0] != "parameter":
                continue
            _kind, _method, path, required = data
            if item.checkState(0) != Qt.CheckState.Checked:
                if required:
                    raise ValueError(f"{method}.{'.'.join(path)} is required.")
                continue
            values[path[-1]] = self._parameter_item_value(item, method, path, required)
        return values

    def _parameter_item_value(
        self,
        item: QTreeWidgetItem,
        method: str,
        path: tuple[str, ...],
        required: bool,
    ):
        if item.childCount():
            return self._selected_parameters(item, method)
        source = self.parameter_sources.get((method, path))
        if source is not None:
            value = source.currentText().strip()
            if required and not value:
                raise ValueError(f"{method}.{'.'.join(path)} requires a Catalog source.")
            return value or None
        text = item.text(1).strip()
        value = yaml.safe_load(text) if text else None
        if required and value is None:
            raise ValueError(f"{method}.{'.'.join(path)} requires a value.")
        return value

    def _save_yaml(self) -> None:
        project = self.require_project()
        if not project:
            return
        state = self.workflow.load_ui_state(project)
        catalogs = self.workflow.list_catalogs(project)
        catalog_file = state.get("active_catalog_file", "wflow_catalog.yml")
        if catalog_file not in catalogs:
            catalog_file = catalogs[0] if catalogs else ""
        recipe_file = self.recipe_files.currentText() or "wflow.yml"
        try:
            path = self.workflow.save_wflow_recipe_yaml(
                project,
                self.preview.toPlainText(),
                catalog_file,
                recipe_file,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "wflow.yml is blocked", str(exc))
            return
        self.preview.setPlainText(path.read_text(encoding="utf-8"))
        self.preview.document().setModified(False)
        self.changed()


class ProcessTerminal(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("terminalPane")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.command = QLabel("HydroMT terminal")
        self.command.setObjectName("terminalCommand")
        self.command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.output = QTextBrowser()
        self.output.setObjectName("terminalOutput")
        self.output.setLineWrapMode(QTextBrowser.LineWrapMode.NoWrap)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        header = QHBoxLayout()
        header.addWidget(self.command, 1)
        clear = QPushButton('Clear'); clear.clicked.connect(self.clear)
        header.addWidget(clear)
        layout.addLayout(header)
        layout.addWidget(self.output, 1)

    def clear(self) -> None:
        self.output.clear()

    def set_command(self, command: str) -> None:
        self.command.setText(f"> {command}")

    def append_output(self, text: str) -> None:
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for line in text.splitlines(keepends=True):
            fmt = QTextCharFormat()
            lower = line.lower()
            color = '#f0736b' if 'error' in lower or 'traceback' in lower else '#f0b155' if 'warning' in lower else '#34c7c2' if 'info' in lower else '#f2f2f2'
            fmt.setForeground(QColor(color))
            cursor.insertText(line, fmt)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()


class HydroMTWorkspace(ProjectPage):
    def __init__(
        self,
        project_provider,
        workflow: WorkflowManager,
        process_manager,
        catalog_page: CatalogBuilderPage,
        recipe_page: RecipeBuilderPage,
        default_executable: Path,
    ) -> None:
        super().__init__(
            "MODEL",
            "HydroMT build / update",
            "Select project inputs, preview the command, and run HydroMT with live terminal output.",
            project_provider,
        )
        self.workflow = workflow
        self.process_manager = process_manager
        self.catalog_page = catalog_page
        self.recipe_page = recipe_page
        self._owns_process = False
        self._loading = False

        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        form = QFormLayout()
        form.setSpacing(12)
        form.setVerticalSpacing(16)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Build", "Update"])
        self.executable = QLineEdit(str(default_executable))
        self.catalog_file = QComboBox()
        self.recipe_file = QComboBox()
        self.model_root = QLineEdit()
        self.model_root_label = QLabel("Model output directory")
        self.update_target = QLineEdit()
        self.update_target_row = QWidget()
        target_layout = QHBoxLayout(self.update_target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.addWidget(self.update_target, 1)
        target_browse = QPushButton("Browse")
        target_browse.clicked.connect(
            lambda: self._browse_directory(self.update_target, "Select updated model target")
        )
        target_layout.addWidget(target_browse)
        self.force_overwrite = QCheckBox("Overwrite existing model files (--fo)")
        self.verbose = QCheckBox("Verbose output (-v)")
        self.verbose.setChecked(True)

        executable_row = self._path_row(
            self.executable, "Select HydroMT executable", executable=True
        )
        model_row = self._path_row(self.model_root, "Select model directory")
        form.addRow("Mode", self.mode_combo)
        form.addRow("HydroMT executable", executable_row)
        form.addRow("Data catalog", self.catalog_file)
        form.addRow("WFlow recipe", self.recipe_file)
        form.addRow(self.model_root_label, model_row)
        form.addRow("Updated model target", self.update_target_row)
        form.addRow("Options", self.force_overwrite)
        form.addRow("", self.verbose)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.run_button = QPushButton("Run HydroMT")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self._run_hydromt)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.process_manager.stop)
        actions.addWidget(self.run_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.status = QLabel("Open a project and select HydroMT inputs.")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.terminal = ProcessTerminal()
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.execution_splitter = splitter
        self.form_scroll = scroll_panel(panel)
        splitter.addWidget(self.form_scroll)
        splitter.addWidget(self.terminal)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 240])
        self.layout.addWidget(splitter, 1)

        self.process_manager.output_received.connect(self._append_output)
        self.process_manager.state_changed.connect(self._state_changed)
        self.process_manager.finished.connect(self._finished)
        self.catalog_page.catalog_files.currentTextChanged.connect(self.refresh_context)
        self.recipe_page.recipe_files.currentTextChanged.connect(self.refresh_context)
        for widget in (self.mode_combo, self.catalog_file, self.recipe_file):
            widget.currentTextChanged.connect(self._configuration_changed)
        for widget in (self.executable, self.model_root, self.update_target):
            widget.textChanged.connect(self._configuration_changed)
        self.force_overwrite.toggled.connect(self._configuration_changed)
        self.verbose.toggled.connect(self._configuration_changed)
        self._mode_changed()
        self._update_ready()

    def _path_row(self, field: QLineEdit, title: str, executable: bool = False) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        browse = QPushButton("Browse")
        if executable:
            browse.clicked.connect(lambda: self._browse_executable(field, title))
        else:
            browse.clicked.connect(lambda: self._browse_directory(field, title))
        layout.addWidget(browse)
        return row

    def _browse_executable(self, field: QLineEdit, title: str) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self, title, field.text(), "Executable (*.exe);;All files (*)"
        )
        if selected:
            field.setText(selected)

    def _browse_directory(self, field: QLineEdit, title: str) -> None:
        start = field.text() or str(self.project().root if self.project() else Path.home())
        selected = QFileDialog.getExistingDirectory(self, title, start)
        if selected:
            field.setText(selected)

    def _mode_changed(self) -> None:
        is_update = self.mode_combo.currentText() == "Update"
        self.model_root_label.setText(
            "Existing model directory" if is_update else "Model output directory"
        )
        self.update_target_row.setVisible(is_update)

    def _configuration_changed(self, *_args) -> None:
        self._mode_changed()
        self._save_state()
        self._update_ready()

    def _save_state(self) -> None:
        if self._loading or not self.project():
            return
        self.workflow.save_ui_state(
            self.project(),
            hydromt_mode=self.mode_combo.currentText(),
            hydromt_executable=self.executable.text().strip(),
            hydromt_catalog_file=self.catalog_file.currentText(),
            hydromt_recipe_file=self.recipe_file.currentText(),
            hydromt_model_root=self.model_root.text().strip(),
            hydromt_update_target=self.update_target.text().strip(),
            hydromt_force_overwrite=self.force_overwrite.isChecked(),
            hydromt_verbose=self.verbose.isChecked(),
        )

    def _missing_requirements(self) -> list[str]:
        project = self.project()
        if not project:
            return ["open a project"]
        missing = []
        executable = Path(self.executable.text().strip())
        if not executable.is_file():
            missing.append("select hydromt executable")
        catalog = self.catalog_file.currentText()
        if not catalog:
            missing.append("select data catalog")
        elif not self.workflow.validate_catalog(project, catalog)[0]:
            missing.append("fix catalog validation")
        recipe = self.recipe_file.currentText()
        if not recipe or not self.workflow.recipe_path(project, recipe).is_file():
            missing.append("select WFlow recipe")
        if not self.model_root.text().strip():
            missing.append("select model directory")
        if self.mode_combo.currentText() == "Update":
            if not Path(self.model_root.text().strip()).is_dir():
                missing.append("select existing model directory")
            if not self.update_target.text().strip():
                missing.append("select updated model target")
        return missing

    def _update_ready(self) -> None:
        missing = self._missing_requirements()
        self.run_button.setEnabled(not missing and not self.process_manager.is_running)
        if not self._owns_process:
            self.status.setText(
                "READY" if not missing else "Required: " + ", ".join(missing) + "."
            )

    def command_arguments(self, catalog_path: Path | None = None) -> list[str]:
        project = self.project()
        if not project:
            return []
        mode = self.mode_combo.currentText().lower()
        arguments = [mode, "wflow_sbm", self.model_root.text().strip()]
        if mode == "update":
            arguments.extend(["-o", self.update_target.text().strip()])
        arguments.extend(
            [
                "-i",
                str(self.workflow.recipe_path(project, self.recipe_file.currentText())),
                "-d",
                str(
                    catalog_path
                    or self.workflow.catalog_path(
                        project, self.catalog_file.currentText()
                    )
                ),
            ]
        )
        if self.force_overwrite.isChecked():
            arguments.append("--fo")
        if self.verbose.isChecked():
            arguments.append("-v")
        return arguments

    def _run_hydromt(self) -> None:
        project = self.require_project()
        if not project:
            return
        if self.process_manager.slots and len(self.process_manager.slots.active) >= self.process_manager.slots.limit:
            QMessageBox.warning(self, "Process limit reached", "Maximum 3 processes are already running.")
            return
        missing = self._missing_requirements()
        if missing:
            QMessageBox.warning(self, "HydroMT is not ready", "\n".join(missing))
            return
        try:
            runtime_catalog = self.workflow.prepare_hydromt_catalog(
                project, self.catalog_file.currentText()
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "HydroMT catalog is not ready", str(exc))
            return
        arguments = self.command_arguments(runtime_catalog)
        executable = Path(self.executable.text().strip())
        command = " ".join(
            [f'"{executable}"', *[f'"{value}"' if " " in value else value for value in arguments]]
        )
        self.terminal.clear()
        self.terminal.set_command(command)
        tool = ToolDefinition(
            "hydromt.cli",
            f"HydroMT {self.mode_combo.currentText()}",
            "model",
            "hydromt",
            "Build or update a Wflow model with HydroMT.",
            (),
            domain="Wflow",
            version="1",
            runtime="executable",
        )
        self._owns_process = True
        try:
            self.process_manager.run_command(
                tool,
                executable,
                arguments,
                project.root,
                project.manifests_dir,
            )
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            self._owns_process = False
            self.status.setText(f"ERROR: {exc}")
            self._update_ready()
            QMessageBox.warning(self, "HydroMT could not start", str(exc))

    def _append_output(self, text: str) -> None:
        if self._owns_process:
            self.terminal.append_output(text)

    def _state_changed(self, status: str) -> None:
        if self._owns_process:
            self.status.setText(status)
            self.run_button.setEnabled(False)
            self.stop_button.setEnabled(status in {"STARTING", "RUNNING", "STOPPING"})

    def _finished(self, exit_code: int, _manifest_path: str) -> None:
        if not self._owns_process:
            return
        self._owns_process = False
        self.stop_button.setEnabled(False)
        self.status.setText("SUCCESS" if exit_code == 0 else f"ERROR (exit {exit_code})")
        self._update_ready()

    def refresh_context(self) -> None:
        project = self.project()
        self._loading = True
        self.catalog_file.clear()
        self.recipe_file.clear()
        if not project:
            self._loading = False
            self._update_ready()
            return
        state = self.workflow.load_ui_state(project)
        catalogs = self.workflow.list_catalogs(project)
        recipes = self.workflow.list_wflow_recipes(project)
        self.catalog_file.addItems(catalogs)
        self.recipe_file.addItems(recipes)
        selected_catalog = state.get(
            "hydromt_catalog_file", self.catalog_page.catalog_files.currentText()
        )
        selected_recipe = state.get(
            "hydromt_recipe_file", self.recipe_page.recipe_files.currentText()
        )
        if selected_catalog in catalogs:
            self.catalog_file.setCurrentText(selected_catalog)
        if selected_recipe in recipes:
            self.recipe_file.setCurrentText(selected_recipe)
        default_model = project.root / "models" / "wflow"
        default_target = project.root / "models" / "wflow_updated"
        self.mode_combo.setCurrentText(state.get("hydromt_mode", "Build"))
        self.executable.setText(state.get("hydromt_executable", self.executable.text()))
        self.model_root.setText(state.get("hydromt_model_root", str(default_model)))
        self.update_target.setText(
            state.get("hydromt_update_target", str(default_target))
        )
        self.force_overwrite.setChecked(bool(state.get("hydromt_force_overwrite", False)))
        self.verbose.setChecked(bool(state.get("hydromt_verbose", True)))
        self._loading = False
        self._mode_changed()
        self._update_ready()



class JuliaRunPage(ProjectPage):
    TIMESTAMP_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\b")

    def __init__(
        self,
        project_provider,
        workflow: WorkflowManager,
        process_manager,
        script_path: Path,
        default_executable: Path,
    ) -> None:
        super().__init__(
            "SIMULATION",
            "Run Julia",
            "Select a Wflow model, run julia/run_wflow.jl, and monitor progress in one place.",
            project_provider,
        )
        self.workflow = workflow
        self.process_manager = process_manager
        self.script_path = script_path.resolve()
        self._owns_process = False
        self._loading = False
        self._started_at = 0.0
        self._output_buffer = ""
        self._simulation_start: datetime | None = None
        self._simulation_end: datetime | None = None
        self._progress_fraction: float | None = None
        self._previous_duration: float | None = None
        self._registry: RunRegistry | None = None
        self._active_run: RunRecord | None = None

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(16)
        form = QFormLayout()
        form.setSpacing(12)
        form.setVerticalSpacing(16)
        self.executable = QLineEdit(str(default_executable))
        executable_layout = QHBoxLayout()
        executable_layout.setContentsMargins(0, 0, 0, 0)
        executable_layout.addWidget(self.executable, 1)
        self.browse_executable = QPushButton("Browse")
        self.browse_executable.clicked.connect(self._browse_executable)
        executable_layout.addWidget(self.browse_executable)
        executable_row = QWidget()
        executable_row.setLayout(executable_layout)

        self.models = QComboBox()
        self.models.setMinimumWidth(320)
        model_layout = QHBoxLayout()
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.addWidget(self.models, 1)
        self.refresh_models = QPushButton("Refresh")
        self.refresh_models.clicked.connect(self.refresh_context)
        model_layout.addWidget(self.refresh_models)
        model_row = QWidget()
        model_row.setLayout(model_layout)
        form.addRow("Julia executable", executable_row)
        form.addRow("Wflow model", model_row)
        self.start_time = QDateTimeEdit()
        self.start_time.setCalendarPopup(True)
        self.start_time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.end_time = QDateTimeEdit()
        self.end_time.setCalendarPopup(True)
        self.end_time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.run_id = QLineEdit()
        self.run_id.setPlaceholderText("Generated from the selected period")
        self.run_mode = QComboBox()
        self.run_mode.addItem("Create a new run", "new")
        self.run_mode.addItem("Overwrite selected run", "overwrite")
        self.start_mode = QComboBox()
        self.start_mode.addItem("Automatic warm start", "auto")
        self.start_mode.addItem("Force cold start", "cold")
        self.backup_output = QCheckBox("Back up the existing run folder before overwrite")
        self.backup_output.setChecked(True)
        form.addRow("Simulation starts", self.start_time)
        form.addRow("Simulation ends", self.end_time)
        form.addRow("Run ID", self.run_id)
        form.addRow("Initial state", self.start_mode)
        form.addRow("Save behavior", self.run_mode)
        form.addRow("Overwrite safety", self.backup_output)
        panel_layout.addLayout(form)

        self.model_summary = QLabel("Open a project to discover Wflow models.")
        self.model_summary.setObjectName("muted")
        self.model_summary.setWordWrap(True)
        panel_layout.addWidget(self.model_summary)
        self.chain_summary = QLabel("Select a model and period to inspect state chaining.")
        self.chain_summary.setObjectName("muted")
        self.chain_summary.setWordWrap(True)
        panel_layout.addWidget(self.chain_summary)
        actions = QHBoxLayout()
        self.run_button = QPushButton("Run Julia")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self._run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.process_manager.stop)
        actions.addWidget(self.run_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        panel_layout.addLayout(actions)
        self.registry_table = QTableWidget(0, 6)
        self.registry_table.setHorizontalHeaderLabels(
            ("Run ID", "Period", "Status", "State input", "Config", "Created")
        )
        self.registry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.registry_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.registry_table.setMinimumHeight(150)
        self.registry_table.horizontalHeader().setStretchLastSection(True)
        self.registry_table.itemSelectionChanged.connect(self._registry_selection_changed)
        panel_layout.addWidget(self.registry_table)
        progress_panel = QFrame()
        progress_panel.setObjectName("panel")
        progress_layout = QVBoxLayout(progress_panel)
        progress_layout.setContentsMargins(16, 14, 16, 14)
        self.status = QLabel("Select a model to run.")
        self.status.setObjectName("sectionTitle")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName("Wflow simulation progress")
        self.progress_details = QLabel("Elapsed 00:00:00 | ETA unavailable")
        self.progress_details.setObjectName("muted")
        self.progress_details.setWordWrap(True)
        progress_layout.addWidget(self.status)
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.progress_details)
        panel_layout.addWidget(progress_panel)

        self.terminal = ProcessTerminal()
        self.terminal.command.setText("> julia run_wflow.jl")
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.execution_splitter = splitter
        self.form_scroll = scroll_panel(panel)
        splitter.addWidget(self.form_scroll)
        splitter.addWidget(self.terminal)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 240])
        self.layout.addWidget(splitter, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

        self.models.currentIndexChanged.connect(self._model_changed)
        self.executable.textChanged.connect(self._configuration_changed)
        self.run_id.textEdited.connect(lambda: self.run_id.setProperty("autoGenerated", False))
        self.start_time.dateTimeChanged.connect(self._period_changed)
        self.end_time.dateTimeChanged.connect(self._period_changed)
        self.run_mode.currentIndexChanged.connect(self._run_mode_changed)
        self.start_mode.currentIndexChanged.connect(self._period_changed)
        self.process_manager.output_received.connect(self._append_output)
        self.process_manager.state_changed.connect(self._state_changed)
        self.process_manager.finished.connect(self._finished)
        self._run_mode_changed()
        self._update_ready()

    def _browse_executable(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select Julia executable",
            self.executable.text(),
            "Executable (*.exe);;All files (*)",
        )
        if selected:
            self.executable.setText(selected)

    def _selected_model(self) -> Path | None:
        value = self.models.currentData()
        return Path(value) if value else None

    @staticmethod
    def _configuration(model: Path) -> tuple[datetime, datetime, int]:
        with (model / "wflow_sbm.toml").open("rb") as stream:
            config = tomllib.load(stream)
        time_config = config.get("time", {})
        start = datetime.fromisoformat(
            str(time_config["starttime"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            str(time_config["endtime"]).replace("Z", "+00:00")
        )
        timestep = int(time_config["timestepsecs"])
        if end <= start or timestep <= 0:
            raise ValueError("Wflow time range or timestep is invalid.")
        return start, end, timestep

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return "unavailable"
        total = max(0, int(round(seconds)))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _load_model_summary(self) -> None:
        model = self._selected_model()
        self._simulation_start = None
        self._simulation_end = None
        self._previous_duration = None
        if model is None:
            self._registry = None
            self._refresh_registry_table()
            self.model_summary.setText("No model containing wflow_sbm.toml was found.")
            self._update_ready()
            return
        try:
            start, end, timestep = self._configuration(model)
        except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            self._registry = None
            self._refresh_registry_table()
            self.model_summary.setText(f"Invalid model configuration: {exc}")
            self._update_ready()
            return
        self._simulation_start = start
        self._simulation_end = end
        self.start_time.blockSignals(True)
        self.end_time.blockSignals(True)
        self.start_time.setDateTime(QDateTime(start))
        self.end_time.setDateTime(QDateTime(end))
        self.start_time.blockSignals(False)
        self.end_time.blockSignals(False)
        steps = int((end - start).total_seconds() / timestep)
        project = self.project()
        if project:
            state = self.workflow.load_ui_state(project)
            history = state.get("julia_run_history", {})
            if isinstance(history, dict):
                value = history.get(str(model.resolve()))
                if isinstance(value, (int, float)) and value > 0:
                    self._previous_duration = float(value)
        previous = (
            f" | Previous run {self._format_duration(self._previous_duration)}"
            if self._previous_duration
            else ""
        )
        self.model_summary.setText(
            f"{model}\n{start.isoformat()} to {end.isoformat()} | "
            f"{steps:,} timestep(s){previous}"
        )
        project = self.project()
        try:
            self._registry = RunRegistry(project.root, model, project.name) if project else None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._registry = None
            self.chain_summary.setText(f"Run registry cannot be loaded: {exc}")
        self._refresh_registry_table()
        self._period_changed()
        self._update_ready()

    def _selected_period(self) -> tuple[datetime, datetime]:
        return self.start_time.dateTime().toPython(), self.end_time.dateTime().toPython()

    def _period_changed(self, *_args) -> None:
        if self._loading or not self._registry:
            return
        try:
            start, end = self._selected_period()
            _base_start, _base_end, timestep = self._configuration(self._selected_model())
            run_id = "" if self.run_id.property("autoGenerated") else self.run_id.text()
            plan = self._registry.plan(
                start, end, timestep, run_id, self.start_mode.currentData()
            )
            if not self.run_id.text().strip() or self.run_id.property("autoGenerated"):
                self.run_id.blockSignals(True)
                self.run_id.setText(plan.run_id)
                self.run_id.setProperty("autoGenerated", True)
                self.run_id.blockSignals(False)
            predecessor = (
                f"Warm start from {plan.predecessor.run_id}."
                if plan.predecessor
                else "Cold start: no contiguous completed state is available."
            )
            overlap = (
                " Overlaps: " + ", ".join(run.run_id for run in plan.overlaps) + "."
                if plan.overlaps
                else ""
            )
            warning = " " + " ".join(plan.warnings) if plan.warnings else ""
            error = " BLOCKED: " + " ".join(plan.errors) if plan.errors else ""
            self.chain_summary.setText(predecessor + overlap + warning + error)
        except (OSError, TypeError, ValueError) as exc:
            self.chain_summary.setText(f"Run period is invalid: {exc}")
        self._update_ready()

    def _run_mode_changed(self, *_args) -> None:
        overwrite = self.run_mode.currentData() == "overwrite"
        self.backup_output.setEnabled(overwrite)
        if overwrite:
            self._registry_selection_changed()
        else:
            self.run_id.setProperty("autoGenerated", True)
            self._period_changed()

    def _registry_selection_changed(self) -> None:
        if self.run_mode.currentData() != "overwrite":
            return
        row = self.registry_table.currentRow()
        if row < 0:
            return
        item = self.registry_table.item(row, 0)
        if item:
            self.run_id.setText(item.text())
            self.run_id.setProperty("autoGenerated", False)

    def _refresh_registry_table(self) -> None:
        runs = self._registry.list_runs() if self._registry else []
        self.registry_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            values = (
                run.run_id,
                f"{run.start.date()} - {run.end.date()}",
                run.status,
                run.state_input_path or "Cold start",
                run.config_hash[:10],
                run.created_at[:19],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.registry_table.setItem(row, column, item)

    def _configuration_changed(self, *_args) -> None:
        if not self._loading and self.project():
            self.workflow.save_ui_state(
                self.project(), julia_executable=self.executable.text().strip()
            )
        self._update_ready()

    def _model_changed(self, *_args) -> None:
        model = self._selected_model()
        if not self._loading and self.project() and model:
            self.workflow.save_ui_state(
                self.project(), julia_model_root=str(model.resolve())
            )
        self._load_model_summary()

    def _missing_requirements(self) -> list[str]:
        missing = []
        if not self.project():
            missing.append("open a project")
        executable = Path(self.executable.text().strip())
        if not executable.is_file():
            missing.append("select Julia executable")
        if not self.script_path.is_file():
            missing.append("julia/run_wflow.jl is missing")
        model = self._selected_model()
        if model is None or not (model / "wflow_sbm.toml").is_file():
            missing.append("select a Wflow model")
        if self._simulation_start is None or self._simulation_end is None:
            missing.append("fix wflow_sbm.toml time configuration")
        if self._registry is None:
            missing.append("fix the model run registry")
        try:
            start, end = self._selected_period()
            if end <= start:
                missing.append("select a valid simulation period")
            elif self._registry and model:
                _base_start, _base_end, timestep = self._configuration(model)
                plan = self._registry.plan(
                    start, end, timestep, self.run_id.text(), self.start_mode.currentData()
                )
                missing.extend(plan.errors)
        except (TypeError, ValueError):
            missing.append("select a valid simulation period")
        if not self.run_id.text().strip():
            missing.append("enter a run ID")
        return missing

    def _update_ready(self) -> None:
        missing = self._missing_requirements()
        self.run_button.setEnabled(not missing and not self.process_manager.is_running)
        if not self._owns_process:
            self.status.setText(
                "READY" if not missing else "Required: " + ", ".join(missing) + "."
            )

    def _run(self) -> None:
        project = self.require_project()
        if not project:
            return
        if self.process_manager.slots and len(self.process_manager.slots.active) >= self.process_manager.slots.limit:
            QMessageBox.warning(self, "Process limit reached", "Maximum 3 processes are already running.")
            return
        missing = self._missing_requirements()
        if missing:
            QMessageBox.warning(self, "Julia run is not ready", "\n".join(missing))
            return
        model = self._selected_model()
        if model is None or self._registry is None:
            return
        start, end = self._selected_period()
        _base_start, _base_end, timestep = self._configuration(model)
        try:
            plan = self._registry.plan(
                start, end, timestep, self.run_id.text(), self.start_mode.currentData()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Run configuration is invalid", str(exc))
            return
        overwrite_id = None
        if self.run_mode.currentData() == "overwrite":
            selected = next(
                (run for run in self._registry.list_runs() if run.run_id == self.run_id.text().strip()),
                None,
            )
            if selected is None:
                QMessageBox.warning(self, "Select a run to overwrite", "Select an existing registry row before choosing overwrite.")
                return
            confirmation = QMessageBox(self)
            confirmation.setIcon(QMessageBox.Icon.Warning)
            confirmation.setWindowTitle("Overwrite existing run")
            confirmation.setText(f"Overwrite '{selected.run_id}' and replace its registry entry?")
            confirmation.setInformativeText("The existing run folder will be backed up first." if self.backup_output.isChecked() else "The existing run folder will be permanently deleted.")
            overwrite_button = confirmation.addButton("Overwrite run", QMessageBox.ButtonRole.DestructiveRole)
            confirmation.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            confirmation.setDefaultButton(confirmation.buttons()[-1])
            confirmation.exec()
            if confirmation.clickedButton() is not overwrite_button:
                return
            overwrite_id = selected.run_id
        try:
            record, run_config = self._registry.prepare_run(
                plan,
                overwrite_run_id=overwrite_id,
                backup_output=self.backup_output.isChecked(),
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Run could not be prepared", str(exc))
            return
        self._active_run = record
        self._simulation_start = start
        self._simulation_end = end
        self._owns_process = True
        self._started_at = time.monotonic()
        self._output_buffer = ""
        self._progress_fraction = None
        self.terminal.clear()
        self.terminal.set_command(f"julia run_wflow.jl  # {record.run_id}")
        self.progress.setRange(0, 0)
        self.progress_details.setText("Elapsed 00:00:00 | Estimating completion...")
        self.status.setText("STARTING")
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.models.setEnabled(False)
        self.executable.setEnabled(False)
        for widget in (self.start_time, self.end_time, self.run_id, self.start_mode, self.run_mode, self.backup_output):
            widget.setEnabled(False)
        self.browse_executable.setEnabled(False)
        self.refresh_models.setEnabled(False)
        self.timer.start()
        tool = ToolDefinition(
            "wflow.julia",
            "Run Wflow with Julia",
            "simulation",
            "Wflow",
            "Run the selected Wflow model through julia/run_wflow.jl.",
            (),
            domain="Wflow",
            version="1",
            runtime="executable",
        )
        try:
            self.process_manager.run_command(
                tool,
                Path(self.executable.text().strip()),
                [self.script_path.name],
                self.script_path.parent,
                project.manifests_dir,
                extra_environment={
                    "HYDROHUB_MODEL_DIR": str(model.resolve()),
                    "HYDROHUB_CONFIG_PATH": str(run_config.resolve()),
                    "HYDROHUB_RUN_ID": record.run_id,
                },
            )
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            self._registry.finish_run(record.run_id, False)
            self._active_run = None
            self._owns_process = False
            self.timer.stop()
            self.stop_button.setEnabled(False)
            self.status.setText(f"ERROR: {exc}")
            self.models.setEnabled(True)
            self.executable.setEnabled(True)
            for widget in (self.start_time, self.end_time, self.run_id, self.start_mode, self.run_mode):
                widget.setEnabled(True)
            self.backup_output.setEnabled(self.run_mode.currentData() == "overwrite")
            self.browse_executable.setEnabled(True)
            self.refresh_models.setEnabled(True)
            self.run_button.setEnabled(not self._missing_requirements())
            QMessageBox.warning(self, "Julia could not start", str(exc))

    def _append_output(self, text: str) -> None:
        if not self._owns_process:
            return
        self.terminal.append_output(text)
        self._output_buffer += text
        lines = self._output_buffer.splitlines(keepends=True)
        self._output_buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._output_buffer = lines.pop()
        for line in lines:
            for match in self.TIMESTAMP_RE.finditer(line):
                self._update_simulation_time(match.group(1))

    def _update_simulation_time(self, value: str) -> None:
        if self._simulation_start is None or self._simulation_end is None:
            return
        try:
            current = datetime.fromisoformat(value)
        except ValueError:
            return
        if self._simulation_start.tzinfo and current.tzinfo is None:
            current = current.replace(tzinfo=self._simulation_start.tzinfo)
        total = (self._simulation_end - self._simulation_start).total_seconds()
        completed = (current - self._simulation_start).total_seconds()
        if total > 0 and completed >= 0:
            self._progress_fraction = min(1.0, completed / total)
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, round(self._progress_fraction * 100)))
            if self._progress_fraction >= 1.0:
                self.status.setText("Running Simulation")
            self._tick()

    def _tick(self) -> None:
        if not self._started_at:
            return
        elapsed = max(0.0, time.monotonic() - self._started_at)
        label = "Estimating completion..."
        if self._progress_fraction and self._progress_fraction > 0:
            if self._progress_fraction >= 1.0:
                label = "Running Simulation"
            else:
                estimated_total = elapsed / self._progress_fraction
                label = f"ETA {self._format_duration(estimated_total - elapsed)}"
        elif self._previous_duration:
            remaining = max(0.0, self._previous_duration - elapsed)
            estimated_fraction = min(0.99, elapsed / self._previous_duration)
            label = f"Estimated from previous run: {self._format_duration(remaining)}"
        self.progress_details.setText(
            f"Elapsed {self._format_duration(elapsed)} | {label}"
        )

    def _state_changed(self, status: str) -> None:
        if not self._owns_process:
            running = status in {"STARTING", "RUNNING", "STOPPING"}
            self.run_button.setEnabled(not running and not self._missing_requirements())
            return
        self.status.setText(status)
        running = status in {"STARTING", "RUNNING", "STOPPING"}
        self.run_button.setEnabled(False if running else not self._missing_requirements())
        self.stop_button.setEnabled(running)

    def _finished(self, exit_code: int, _manifest_path: str) -> None:
        if not self._owns_process:
            return
        elapsed = max(0.0, time.monotonic() - self._started_at)
        self._owns_process = False
        self.timer.stop()
        self.stop_button.setEnabled(False)
        self.models.setEnabled(True)
        self.executable.setEnabled(True)
        for widget in (self.start_time, self.end_time, self.run_id, self.start_mode, self.run_mode):
            widget.setEnabled(True)
        self.backup_output.setEnabled(self.run_mode.currentData() == "overwrite")
        self.browse_executable.setEnabled(True)
        self.refresh_models.setEnabled(True)
        if exit_code == 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.status.setText("SUCCESS")
            model = self._selected_model()
            project = self.project()
            if model and project:
                state = self.workflow.load_ui_state(project)
                history = state.get("julia_run_history", {})
                history = dict(history) if isinstance(history, dict) else {}
                history[str(model.resolve())] = round(elapsed, 3)
                self.workflow.save_ui_state(project, julia_run_history=history)
            self.progress_details.setText(
                f"Completed in {self._format_duration(elapsed)} | ETA 00:00:00"
            )
        else:
            self.status.setText(f"ERROR (exit {exit_code})")
            self.progress_details.setText(
                f"Stopped after {self._format_duration(elapsed)} | No completion estimate"
            )
        if self._registry and self._active_run:
            self._registry.finish_run(self._active_run.run_id, exit_code == 0)
        self._active_run = None
        self._refresh_registry_table()
        self.run_button.setEnabled(not self._missing_requirements())

    def refresh_context(self) -> None:
        project = self.project()
        selected = self.models.currentData()
        if self.run_mode.currentData() == "new":
            self.run_id.setProperty("autoGenerated", True)
        self._loading = True
        self.models.clear()
        if not project:
            self._loading = False
            self._load_model_summary()
            return
        state = self.workflow.load_ui_state(project)
        configured = state.get("julia_model_root") or selected
        for model in discover_wflow_models(project.root):
            self.models.addItem(str(model.relative_to(project.root)), str(model))
        if configured:
            configured = str(Path(configured).resolve())
            for index in range(self.models.count()):
                if self.models.itemData(index) == configured:
                    self.models.setCurrentIndex(index)
                    break
        default_julia = str(shutil.which("julia") or self.executable.text())
        self.executable.setText(state.get("julia_executable", default_julia))
        self._loading = False
        self._load_model_summary()


class RuntimeBuilderPage(ProjectPage):
    def __init__(self, project_provider, workflow: WorkflowManager, changed) -> None:
        super().__init__("MODEL / WFLOW.JL", "Runtime and output configuration", "Configure runtime period and explicit NetCDF/CSV outputs. Saving never starts a simulation.", project_provider)
        self.workflow = workflow
        self.changed = changed
        panel = QFrame()
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        self.start = QLineEdit("2020-01-01T00:00:00")
        self.end = QLineEdit("2020-12-31T00:00:00")
        self.timestep = QSpinBox()
        self.timestep.setRange(60, 86400)
        self.timestep.setValue(86400)
        self.netcdf = QLineEdit("output.nc")
        self.csv = QLineEdit("output.csv")
        for label, widget in (("Start", self.start), ("End", self.end), ("Timestep seconds", self.timestep), ("Spatial output", self.netcdf), ("Time-series output", self.csv)):
            form.addRow(label, widget)
        save = QPushButton("Save runtime TOML")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        form.addRow("", save)
        self.layout.addWidget(panel)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.layout.addWidget(self.preview, 1)

    def refresh_context(self) -> None:
        project = self.project()
        if project:
            path = project.root / "configs" / "wflow" / "wflow_sbm.toml"
            self.preview.setPlainText(path.read_text(encoding="utf-8") if path.exists() else "")

    def _save(self) -> None:
        project = self.require_project()
        if not project:
            return
        try:
            path = self.workflow.save_runtime(project, self.start.text(), self.end.text(), self.timestep.value(), self.netcdf.text(), self.csv.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Runtime configuration is blocked", str(exc))
            return
        self.preview.setPlainText(path.read_text(encoding="utf-8"))
        self.changed()


class EnvironmentPage(ProjectPage):
    def __init__(self, project_provider, workflow: WorkflowManager) -> None:
        super().__init__("SYSTEM", "Environment diagnostics", "Checks executable availability and versions without running modeling workflows.", project_provider)
        self.workflow = workflow
        self.manager = EnvironmentManager()
        run = QPushButton("Run diagnostics")
        self.diagnose_button = run
        run.setObjectName("primary")
        run.clicked.connect(self._diagnose)
        self.layout.addWidget(run, alignment=Qt.AlignmentFlag.AlignLeft)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Component", "Status", "Version", "Executable"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.layout.addWidget(self.table, 1)
        self._diagnose()

    def _diagnose(self) -> None:
        if not self.diagnose_button.isEnabled():
            return
        self.diagnose_button.setEnabled(False)
        self.diagnose_button.setText("Checking environment…")
        self._diagnostic_project = self.project()
        self._diagnostic_job = BackgroundJob(self, self.manager.diagnose, self._diagnosed, self._diagnostic_failed)
        self._diagnostic_job.start()

    def _diagnostic_failed(self, message) -> None:
        self.diagnose_button.setEnabled(True)
        self.diagnose_button.setText("Run diagnostics")
        self.diagnose_button.setToolTip(message)

    def _diagnosed(self, checks) -> None:
        self.diagnose_button.setEnabled(True)
        self.diagnose_button.setText("Run diagnostics")
        self.table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            for column, value in enumerate((check.component, check.status, check.version, check.executable)):
                self.table.setItem(row, column, QTableWidgetItem(value))
        project = self._diagnostic_project
        if project:
            python_ready = any(check.component == "Python" and check.status == "READY" for check in checks)
            self.workflow.update_step(project, "environment", "VALID" if python_ready else "WARNING")


class GlobalDatabasePage(ProjectPage):
    CATEGORIES = ("qbankfull", "rainfall", "soil", "dem", "landcover", "pet", "hydrography", "river_geometry", "gauges", "climate", "reference", "user_data", "cache", "metadata", "catalog")

    def __init__(self, project_provider, workflow: WorkflowManager, workspace: Path) -> None:
        super().__init__("DATA", "Global Database", "Create or inspect the reusable application-level data repository. Dataset registration remains explicit.", project_provider)
        self.workflow = workflow
        self.root = (workspace / "HydroHub_Global_Database").resolve()
        actions = QHBoxLayout()
        refresh = QPushButton("Refresh files")
        refresh.clicked.connect(self._refresh_files)
        copy_path = QPushButton("Copy selected path")
        copy_path.clicked.connect(self._copy_selected_path)
        actions.addWidget(refresh)
        actions.addWidget(copy_path)
        actions.addStretch()
        self.layout.addLayout(actions)
        self.status = QLabel("Not initialized")
        self.status.setObjectName("muted")
        self.layout.addWidget(self.status)
        self.files = QTableWidget(0, 5)
        self.files.setHorizontalHeaderLabels(
            ["Name", "Type", "Size", "Modified", "Path"]
        )
        self.files.horizontalHeader().setStretchLastSection(True)
        self.files.setAlternatingRowColors(True)
        self.files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.files.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.files.itemDoubleClicked.connect(lambda _item: self._copy_selected_path())
        self.layout.addWidget(self.files, 1)
        # Create the small directory manifest now, but defer recursive listing.
        self._initialize()

    def refresh_context(self) -> None:
        project = self.project()
        if project and self.root.is_dir():
            self.workflow.update_step(project, "global_data", "VALID")
        self._refresh_files()

    def _copy_selected_path(self) -> None:
        row = self.files.currentRow()
        item = self.files.item(row, 4) if row >= 0 else None
        if item is None:
            self.status.setText("Select a database file before copying its path.")
            return
        path = item.data(Qt.ItemDataRole.UserRole) or item.toolTip()
        QApplication.clipboard().setText(str(path))
        self.status.setText(f"Copied: {path}")

    @staticmethod
    def _file_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    def _refresh_files(self) -> None:
        root = self.root
        self.files.setRowCount(0)
        if not root.is_dir():
            self.status.setText("Database location does not exist. Browse or initialize it first.")
            return
        paths = sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: str(path).lower())
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            row = self.files.rowCount()
            self.files.insertRow(row)
            values = (
                path.name,
                path.suffix.lower() or "file",
                self._file_size(stat.st_size),
                datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                str(path.relative_to(root)),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(str(path))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.files.setItem(row, column, item)
        self.status.setText(f"READY · {len(paths)} file(s) · {root}")

    def _initialize(self) -> None:
        root = self.root
        for category in self.CATEGORIES:
            (root / category).mkdir(parents=True, exist_ok=True)
        manifest = root / "manifest.yml"
        if not manifest.exists():
            manifest.write_text("version: 1\ndatasets: {}\n", encoding="utf-8")
        self.status.setText(f"READY: {root}")


class HistoryPage(ProjectPage):
    def __init__(self, project_provider) -> None:
        super().__init__("PROJECT", "Execution history", "Review process manifests generated for the active project.", project_provider)
        self.list = QListWidget()
        self.layout.addWidget(self.list, 1)

    def refresh_context(self) -> None:
        self.list.clear()
        project = self.project()
        if not project:
            self.list.addItem("Open a project to view execution history.")
            return
        manifests = sorted(project.manifests_dir.glob("*.json"), reverse=True)
        if not manifests:
            self.list.addItem("No executions yet. Run a project tool to create a manifest.")
        for path in manifests:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.list.addItem(f"{data.get('status', 'UNKNOWN')}  {data.get('tool_id', path.stem)}\n{data.get('completed_at', '')}")
            except (OSError, json.JSONDecodeError):
                self.list.addItem(f"INVALID MANIFEST  {path.name}")


class ResultsMapPage(ProjectPage):
    def __init__(self, project_provider) -> None:
        super().__init__("RESULTS", "Spatial map", "Load project GeoJSON output as an interactive Leaflet layer.", project_provider)
        controls = QHBoxLayout()
        load = QPushButton("Add GeoJSON layer")
        load.clicked.connect(self._load_geojson)
        clear = QPushButton("Clear data layers")
        clear.clicked.connect(lambda: self.map.clear_data_layers())
        controls.addWidget(load)
        controls.addWidget(clear)
        controls.addStretch()
        self.layout.addLayout(controls)
        self.map = IndonesiaMapWidget()
        self.layout.addWidget(self.map, 1)

    def refresh_context(self) -> None:
        project = self.project()
        if not project:
            return
        aoi = project.root / "workspace" / "aoi.geojson"
        if aoi.exists():
            self.map.set_aoi(json.loads(aoi.read_text(encoding="utf-8")))

    def _load_geojson(self) -> None:
        project = self.require_project()
        if not project:
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Add GeoJSON", str(project.root), "GeoJSON (*.geojson *.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.map.add_geojson(Path(path).stem, data)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Layer could not be loaded", str(exc))


class GatedWorkspacePage(ProjectPage):
    def __init__(self, eyebrow: str, title: str, description: str, requirement: str, project_provider) -> None:
        super().__init__(eyebrow, title, description, project_provider)
        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        heading = QLabel("Workflow gate")
        heading.setObjectName("sectionTitle")
        note = QLabel(requirement)
        note.setWordWrap(True)
        note.setObjectName("muted")
        panel_layout.addWidget(heading)
        panel_layout.addWidget(note)
        self.layout.addWidget(panel)
        self.layout.addStretch()
