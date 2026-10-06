from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from importlib.metadata import metadata

from PySide6.QtCore import QDate, Qt, QSize, QProcess, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hydrohub.core.process_manager import ProcessManager, ProcessSlots
from hydrohub.core.custom_tool_sessions import CustomToolSessionHub
from hydrohub.core.project_manager import ProjectContext, ProjectManager
from hydrohub.core.workflow_manager import WorkflowManager
from hydrohub.plugins.registry import ToolDefinition, ToolField, ToolRegistry
from hydrohub.ui.map_viewer import IndonesiaMapWidget
from hydrohub.ui.result_pages import ForcingPage, ResultsPage
from hydrohub.ui.custom_tools import CustomToolsPage
from hydrohub.ui.tool_library_page import ToolLibraryPage
from hydrohub.ui.automation_page import AutomationPage
from hydrohub.ui.backup_page import BackupPage
from hydrohub.ui.settings_page import SettingsPage
from hydrohub.ui.theme import theme_manager
from hydrohub.ui.layout_helpers import configure_forms, scroll_panel
from qfluentwidgets import PushButton as QPushButton, FluentIcon
from hydrohub.ui.workflow_pages import (
    CatalogBuilderPage,
    EnvironmentPage,
    FileWorkspacePage,
    GatedWorkspacePage,
    GlobalDatabasePage,
    HistoryPage,
    HydroMTWorkspace,
    JuliaRunPage,
    ProjectOverviewPage,
    RecipeBuilderPage,
)


class MainWindow(QMainWindow):
    NAV_ICONS = (FluentIcon.HOME, FluentIcon.FOLDER, FluentIcon.SYNC, FluentIcon.SAVE, FluentIcon.SETTING)

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        from PySide6.QtGui import QPixmapCache
        QPixmapCache.setCacheLimit(50 * 1024)  # 50 MB
        import gc
        gc.set_threshold(700, 10, 10)
        self.theme = theme_manager()
        self.workspace = workspace
        self.project_manager = ProjectManager(workspace / ".hydrohub" / "project_registry.json")
        self.workflow = WorkflowManager()
        self.current_project: ProjectContext | None = None
        self.registry = ToolRegistry()
        self.tools = self.registry.discover()
        self.process_slots = ProcessSlots(parent=self)
        self.custom_tool_sessions = CustomToolSessionHub(self)
        self.custom_tool_sessions.dependency_install_required.connect(
            self._confirm_custom_tool_install
        )
        self.tool_pages: dict[str, ToolWorkspace] = {}
        self.context_pages: list[QWidget] = []
        self.workspace_records: dict[str, dict] = {}
        self._process_badges: dict[str, dict[ProcessManager, tuple[str, str]]] = {}
        self.system_context_pages: list[QWidget] = []
        self._version_processes: list[QProcess] = []
        self._version_generation = 0
        self._runtime_versions = {"HydroMT": "Checking...", "Julia": "Checking..."}

        self.setWindowTitle("HydroHub")
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)
        self._build_ui()
        self.theme.changed.connect(self._on_theme_changed)
        self.process_slots.changed.connect(self._refresh_project_registry)
        self._build_actions()
        self._build_menu()
        bar = self.addToolBar('Appearance and navigation')
        bar.setMovable(False)
        collapse = QAction('☰', self); collapse.setToolTip('Collapse / expand navigation')
        collapse.triggered.connect(self._toggle_rail); bar.addAction(collapse)
        back = QAction('← Dashboard', self); back.triggered.connect(lambda: self._navigate(0)); bar.addAction(back)
        self.breadcrumb = QLabel('HydroHub'); bar.addWidget(self.breadcrumb)
        toggle = QAction('Dark / light', self)
        toggle.triggered.connect(lambda: self.theme.set_preference('light' if self.theme.mode == 'dark' else 'dark'))
        bar.addAction(toggle)
        self._refresh_runtime_versions()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QFrame()
        self.sidebar = sidebar
        self._rail_expanded = True
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 12, 12, 12)
        side_layout.setSpacing(8)
        brand = QLabel("HydroHub")
        brand.setObjectName("brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        side_layout.addWidget(brand)
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_container = QWidget()
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)

        self.stack = QStackedWidget()
        self.nav_buttons: list[QToolButton] = []
        initial_workspace = self._build_project_workspace(None)
        self.workspace_records[""] = initial_workspace
        self._activate_project_workspace(initial_workspace)
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(self.project_workspace)

        provider = lambda: self.current_project
        self.settings_page = SettingsPage()
        self.automation_page = AutomationPage(self.workspace)
        self.backup_page = BackupPage(self.workspace, lambda: self.current_project)
        self.backup_page.restored.connect(self._refresh_after_restore)
        configure_forms(self.settings_page)
        self.system_context_pages.append(self.settings_page)
        pages = [
            (None, "Dashboard", self._dashboard_page()),
            ("PROJECT", "Project Workspace", self.workspace_stack),
            ("AUTOMATION", "Automation", self.automation_page),
            (None, "Backup & Restore", self.backup_page),
            (None, "Settings", self.settings_page),
        ]
        for group, label, page in pages:
            if group:
                group_label = QLabel(group)
                group_label.setObjectName("eyebrow")
                nav_layout.addSpacing(12)
                nav_layout.addWidget(group_label)
            index = self.stack.count()
            button = QToolButton()
            button.setText(label)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setProperty('expandedText', label)
            button.setToolTip(label)
            self._set_nav_icon(button, index, spaced=True)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.setFixedHeight(36)
            button.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            button.clicked.connect(lambda _checked=False, i=index: self._navigate(i))
            self.nav_buttons.append(button)
            if label == "Project Workspace":
                self.workspace_button = button
                button.setEnabled(False)
            nav_layout.addWidget(button)
            self.stack.addWidget(page)

        nav_layout.addStretch()
        nav_scroll.setWidget(nav_container)
        side_layout.addWidget(nav_scroll, 1)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        side_layout.addWidget(divider)
        project_label = QLabel("CURRENT PROJECT")
        project_label.setObjectName("eyebrow")
        side_layout.addWidget(project_label)
        self.project_name = QLabel("No project selected")
        self.project_name.setWordWrap(True)
        self.project_name.setObjectName("sectionTitle")
        side_layout.addWidget(self.project_name)
        self.project_chip_button = QPushButton('Buka workspace →')
        self.project_chip_button.clicked.connect(lambda: self._navigate(1))
        self.project_chip_button.setMinimumHeight(36)
        side_layout.addWidget(self.project_chip_button)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.stack, 1)
        self._navigate(0)

        status = QStatusBar()
        status.setObjectName("statusBar")
        status.showMessage(f"Ready  |  {len(self.tools)} tools")
        self.version_status = QLabel("HydroMT: Checking...  |  Julia: Checking...")
        status.addPermanentWidget(self.version_status)
        self.setStatusBar(status)
        self._refresh_project_registry()

    def _build_project_workspace(self, context: ProjectContext | None) -> dict:
        provider = lambda: context
        workspace = QTabWidget()
        managers = []

        def manager() -> ProcessManager:
            process = ProcessManager(
                workspace, slots=self.process_slots,
                project_root=context.root if context else None,
            )
            process.state_changed.connect(
                lambda state: self.statusBar().showMessage(f"Process: {state}")
            )
            process.state_changed.connect(
                lambda state, owner=process: self._process_state_changed(owner, state)
            )
            process.finished.connect(
                lambda exit_code, manifest, owner=process: self._process_finished(owner, exit_code, manifest)
            )
            managers.append(process)
            return process

        overview = ProjectOverviewPage(provider, self.workflow)
        overview.aoi_changed.connect(self._aoi_changed)
        files = FileWorkspacePage(provider)
        history = HistoryPage(provider)
        global_db = GlobalDatabasePage(provider, self.workflow, self.workspace)
        catalog = CatalogBuilderPage(provider, self.workflow, self._refresh_model_config)
        recipe = RecipeBuilderPage(provider, self.workflow, self._refresh_model_config)
        results = ResultsPage(provider, self.workflow)
        forcing = ForcingPage(provider, self.workflow)
        download = self._tool_page("download", provider, manager())
        preparing = self._tool_page("preparing", provider, manager())
        qc = self._tool_page("qc", provider, manager())
        tool_pages = {"download": download, "preparing": preparing, "qc": qc}
        hydromt_executable = Path(
            shutil.which("hydromt")
            or Path(sys.executable).with_name(
                "hydromt.exe" if sys.platform == "win32" else "hydromt"
            )
        )
        hydromt = HydroMTWorkspace(
            provider,
            self.workflow,
            manager(),
            catalog,
            recipe,
            hydromt_executable,
        )
        run_julia = JuliaRunPage(
            provider,
            self.workflow,
            manager(),
            Path(__file__).parents[1] / "julia" / "run_wflow.jl",
            Path(shutil.which("julia") or "julia"),
        )
        hydromt.executable.editingFinished.connect(self._refresh_runtime_versions)
        run_julia.executable.editingFinished.connect(self._refresh_runtime_versions)
        built_in = CustomToolsPage(
            provider,
            self.custom_tool_sessions,
            self.workspace / ".hydrohub" / "custom_tools",
        )
        library = ToolLibraryPage(provider, self.workspace)

        data_tabs = self._group_tabs((("Download", download), ("Global Database", global_db), ("Preparing", preparing), ("Quality Control", qc)))
        model_tabs = self._group_tabs((("Wflow Catalog", catalog), ("Wflow Setup", recipe), ("HydroMT", hydromt)))
        simulation_tabs = self._group_tabs((("Forcing", forcing), ("Run Julia", run_julia)))
        tools_tabs = self._group_tabs((("Custom tools", built_in), ("Library", library)))
        def open_library_tool(tool_id: str) -> None:
            if not provider():
                QMessageBox.information(self, "Project required", "Open a project to use a custom tool.")
                return
            if built_in.select_tool(tool_id):
                workspace.setCurrentWidget(tools_tabs)
                tools_tabs.setCurrentWidget(built_in)
        library.open_tool.connect(open_library_tool)
        tools_tabs.currentChanged.connect(lambda index: library.refresh_context() if index == 1 else None)

        workspace.setDocumentMode(True)
        for label, page in (
            ("Overview", overview),
            ("Files", files),
            ("Data", data_tabs),
            ("Model", model_tabs),
            ("Simulation", simulation_tabs),
            ("Results", results),
            ("Tools", tools_tabs),
        ):
            workspace.addTab(page, label)
        context_pages = [
            overview, files, global_db, catalog, recipe, results,
            download, preparing, qc, hydromt, forcing, run_julia,
            built_in, library,
        ]
        configure_forms(workspace)
        for tabs in (workspace, data_tabs, model_tabs, simulation_tabs, tools_tabs):
            tabs.currentChanged.connect(lambda *_args, owner=workspace: self._refresh_visible_pages() if owner is self.project_workspace else None)
        return {
            "workspace": workspace,
            "managers": managers,
            "context_pages": context_pages,
            "overview_page": overview,
            "files_page": files,
            "global_database_page": global_db,
            "catalog_page": catalog,
            "recipe_page": recipe,
            "hydromt_page": hydromt,
            "run_julia_page": run_julia,
            "results_page": results,
            "forcing_page": forcing,
            "tool_pages": tool_pages,
            "custom_tools_page": built_in,
        }

    def _activate_project_workspace(self, record: dict) -> None:
        self.project_workspace = record["workspace"]
        self.context_pages = record["context_pages"]
        self.overview_page = record["overview_page"]
        self.files_page = record["files_page"]
        self.global_database_page = record["global_database_page"]
        self.catalog_page = record["catalog_page"]
        self.recipe_page = record["recipe_page"]
        self.hydromt_page = record["hydromt_page"]
        self.run_julia_page = record["run_julia_page"]
        self.results_page = record["results_page"]
        self.results_map = self.results_page
        self.forcing_page = record["forcing_page"]
        self.tool_pages = record["tool_pages"]
        self.custom_tools_page = record['custom_tools_page']

    def _toggle_rail(self):
        self._rail_expanded = not self._rail_expanded
        self.sidebar.setFixedWidth(224 if self._rail_expanded else 68)
        self.sidebar.layout().setContentsMargins(12 if self._rail_expanded else 4, 12, 12 if self._rail_expanded else 4, 12)
        for button in self.nav_buttons:
            button.setText(button.property('expandedText') if self._rail_expanded else '')
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if self._rail_expanded else Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setAccessibleName(button.property('expandedText'))
            self._set_nav_icon(button, self.nav_buttons.index(button), spaced=self._rail_expanded)
        for label in self.sidebar.findChildren(QLabel):
            label.setVisible(self._rail_expanded)
        for button in self.sidebar.findChildren(QPushButton):
            if button not in self.nav_buttons and button is not self.project_chip_button:
                button.setVisible(self._rail_expanded)
        for frame in self.sidebar.findChildren(QFrame):
            if frame.frameShape() == QFrame.Shape.HLine:
                frame.setVisible(self._rail_expanded)
        self.project_name.setVisible(self._rail_expanded)
        self.project_chip_button.setVisible(self._rail_expanded)

    def _set_nav_icon(self, button: QToolButton, index: int, *, spaced: bool) -> None:
        icon = self.NAV_ICONS[index].icon()
        if spaced:
            glyph = icon.pixmap(QSize(20, 20))
            canvas = QPixmap(28, 20)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.drawPixmap(0, 0, glyph)
            painter.end()
            icon = QIcon(canvas)
            button.setIconSize(QSize(28, 20))
        else:
            button.setIconSize(QSize(20, 20))
        button.setIcon(icon)

    @staticmethod
    def _group_tabs(pages: tuple[tuple[str, QWidget], ...]) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        for label, page in pages:
            tabs.addTab(page, label)
        return tabs

    def _build_menu(self) -> None:
        self.menuBar().setVisible(False)

    def _dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        eyebrow = QLabel("PROJECT OVERVIEW")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Indonesia Hydrology Workspace")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Inspect spatial context, prepare datasets, and run reproducible tools.")
        subtitle.setObjectName("muted")
        titles.addWidget(eyebrow)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        open_button = QPushButton("Open project")
        open_button.clicked.connect(self._open_project)
        create_button = QPushButton("Create project")
        create_button.setObjectName("primary")
        create_button.clicked.connect(self._create_project)
        header.addWidget(open_button)
        header.addWidget(create_button)
        layout.addLayout(header)

        map_panel = QFrame()
        map_panel.setObjectName("panel")
        map_layout = QVBoxLayout(map_panel)
        map_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 10, 12, 4)
        toolbar.setSpacing(8)
        self.map_widget = IndonesiaMapWidget()
        for label, callback in (
            ("Zoom In", self.map_widget.zoom_in),
            ("Zoom Out", self.map_widget.zoom_out),
            ("Reset View", self.map_widget.reset_indonesia),
        ):
            button = QPushButton(label)
            button.setFixedHeight(32)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        layer = QLabel("Layers: Basin · River · Rainfall")
        layer.setObjectName("muted")
        toolbar.addWidget(layer)
        map_layout.addLayout(toolbar)
        map_layout.addWidget(self.map_widget, 1)

        registry_panel = QFrame()
        registry_panel.setObjectName("panel")
        registry_layout = QVBoxLayout(registry_panel)
        registry_layout.setContentsMargins(12, 12, 12, 12)
        registry_layout.setSpacing(8)
        registry_title = QLabel("Project Registry")
        registry_title.setObjectName("sectionTitle")
        self.project_filter = QLineEdit()
        self.project_filter.setPlaceholderText("Filter projects...")
        self.project_filter.setClearButtonEnabled(True)
        self.project_filter.textChanged.connect(self._filter_projects)
        delete_btn = QPushButton("Delete Project")
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self._delete_registered)
        registry_layout.addWidget(registry_title)
        registry_layout.addWidget(self.project_filter)
        registry_layout.addWidget(delete_btn)

        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        self.dashboard_projects = QTableWidget()
        self.dashboard_projects.setColumnCount(1)
        self.dashboard_projects.setHorizontalHeaderLabels(["Name"])
        self.dashboard_projects.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dashboard_projects.verticalHeader().setVisible(False)
        self.dashboard_projects.setShowGrid(False)
        self.dashboard_projects.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dashboard_projects.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.dashboard_projects.setAlternatingRowColors(True)
        self.dashboard_projects.itemDoubleClicked.connect(self._open_registered)

        registry_layout.addWidget(self.dashboard_projects, 1)

        workspace_split = QSplitter(Qt.Orientation.Horizontal)
        workspace_split.addWidget(registry_panel)
        workspace_split.addWidget(map_panel)
        workspace_split.setSizes([360, 900])
        layout.addWidget(workspace_split, 1)

        summary = QHBoxLayout()
        summary.setSpacing(12)
        for value, label in (
            (str(len(self.registry.by_category("download"))), "Download tools"),
            (str(len(self.registry.by_category("preparing"))), "Preparing tools"),
            (str(len(self.registry.by_category("qc"))), "QC tools"),
        ):
            panel = QFrame()
            panel.setObjectName("heroCard")
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(16, 12, 16, 12)
            panel_layout.setSpacing(4)
            number = QLabel(value)
            number.setObjectName("heroNumber")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption = QLabel(label)
            caption.setObjectName("heroLabel")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            panel_layout.addWidget(number)
            panel_layout.addWidget(caption)
            summary.addWidget(panel)
        layout.addLayout(summary)
        return page

    def _tool_page(self, category: str, provider, process_manager: ProcessManager) -> QWidget:
        page = ToolWorkspace(
            category=category,
            tools=self.registry.by_category(category),
            process_manager=process_manager,
            default_interpreter=Path(sys.executable),
            workspace=self.workspace,
            project_provider=provider,
        )
        return page

    def _navigate(self, index: int) -> None:
        if index == 2 and hasattr(self, "automation_page"):
            self.automation_page.refresh_context()
        if index == 3 and hasattr(self, "backup_page"):
            self.backup_page.refresh_context()
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)

    def _refresh_after_restore(self) -> None:
        self._refresh_project_registry()
        self.automation_page.refresh_context()
        self.custom_tools_page.refresh_context()
        for record in self.workspace_records.values():
            for page in record["context_pages"]:
                if isinstance(page, ToolLibraryPage):
                    page.refresh_context()

    def _build_actions(self) -> None:
        new_action = QAction("New Project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._create_project)
        self.addAction(new_action)
        open_action = QAction("Open Project", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_project)
        self.addAction(open_action)

    def _refresh_runtime_versions(self) -> None:
        self._version_generation += 1
        generation = self._version_generation
        for process in self._version_processes:
            try:
                process.finished.disconnect()
                process.errorOccurred.disconnect()
                if process.state() != QProcess.ProcessState.NotRunning:
                    process.kill()
                process.deleteLater()
            except RuntimeError:
                pass
        self._version_processes.clear()
        self._runtime_versions = {"HydroMT": "Checking...", "Julia": "Checking..."}
        self._update_runtime_version_labels()
        executables = {
            "HydroMT": self.hydromt_page.executable.text().strip(),
            "Julia": self.run_julia_page.executable.text().strip(),
        }
        for component, executable in executables.items():
            if not executable:
                self._runtime_versions[component] = "Not found"
                continue
            process = QProcess(self)
            process.setProgram(executable)
            process.setArguments(["--version"])
            process.setProperty("handled", False)
            process.setProperty("component", component)
            process.setProperty("generation", generation)
            process.finished.connect(self._runtime_version_finished)
            process.errorOccurred.connect(self._runtime_version_failed)
            self._version_processes.append(process)
            process.start()
            timeout = QTimer(process)
            timeout.setSingleShot(True)
            timeout.timeout.connect(process.kill)
            timeout.start(8000)
        self._update_runtime_version_labels()

    def _runtime_version_finished(self, _code, _status) -> None:
        process = self.sender()
        if not isinstance(process, QProcess):
            return
        component = process.property("component")
        generation = process.property("generation")
        if generation != self._version_generation or process.property("handled"):
            return
        process.setProperty("handled", True)
        output = bytes(process.readAllStandardOutput()).decode(errors="replace")
        output += "\n" + bytes(process.readAllStandardError()).decode(errors="replace")
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        version_line = next(
            (line for line in lines if "version" in line.lower() or component.lower() in line.lower()),
            lines[0] if lines else "Unavailable",
        )
        self._runtime_versions[component] = version_line
        if process in self._version_processes:
            self._version_processes.remove(process)
        process.deleteLater()
        self._update_runtime_version_labels()

    def _runtime_version_failed(self, _error) -> None:
        process = self.sender()
        if not isinstance(process, QProcess):
            return
        component = process.property("component")
        generation = process.property("generation")
        if generation != self._version_generation or process.property("handled"):
            return
        process.setProperty("handled", True)
        self._runtime_versions[component] = "Not found"
        if process in self._version_processes:
            self._version_processes.remove(process)
        process.deleteLater()
        self._update_runtime_version_labels()

    def _update_runtime_version_labels(self) -> None:
        hydromt = self._runtime_versions["HydroMT"]
        julia = self._runtime_versions["Julia"]
        if hasattr(self, "version_status"):
            self.version_status.setText(f"HydroMT: {hydromt}  |  Julia: {julia}")
        if hasattr(self, "settings_page"):
            self.settings_page.set_versions(hydromt, julia)

    def _create_project(self) -> None:
        dialog = CreateProjectDialog(self.workspace, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            context = self.project_manager.create(
                Path(dialog.location.text()), dialog.name.text(), dialog.description.text()
            )
            self.workflow.save_aoi_geojson(context, dialog.aoi_feature)
        except (ValueError, FileExistsError, OSError) as exc:
            QMessageBox.critical(self, "Project could not be created", str(exc))
            return
        self._set_project(context)

    def _open_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open HydroHub project", str(self.workspace))
        if not path:
            return
        try:
            self._set_project(self.project_manager.open(Path(path)))
        except (ValueError, KeyError, OSError) as exc:
            QMessageBox.critical(self, "Project could not be opened", str(exc))

    def _refresh_project_registry(self) -> None:
        self.dashboard_projects.setRowCount(0)
        projects = self.project_manager.list_registered()
        if not projects:
            self.dashboard_projects.setRowCount(1)
            empty_item = QTableWidgetItem("No registered projects — create a project to open its workspace.")
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.dashboard_projects.setItem(0, 0, empty_item)
            return
        ordered = sorted(projects.items(), key=lambda item: item[1].get("last_opened", ""), reverse=True)
        self.dashboard_projects.setRowCount(len(ordered))
        for row, (path, metadata) in enumerate(ordered):
            name_item = QTableWidgetItem(metadata.get("name", Path(path).name))
            name_item.setData(Qt.ItemDataRole.UserRole, path)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.dashboard_projects.setItem(row, 0, name_item)
            badges = self._process_badges.get(path, {}).values()
            if badges:
                name_item.setText(f"{name_item.text()}  •  {'; '.join(f'{tool}: {status}' for tool, status in badges)}")
        self._filter_projects(self.project_filter.text())

    def _filter_projects(self, query: str) -> None:
        query = query.strip().lower()
        for row in range(self.dashboard_projects.rowCount()):
            name_item = self.dashboard_projects.item(row, 0)
            if name_item:
                match = not query or query in name_item.text().lower()
                self.dashboard_projects.setRowHidden(row, not match)

    def _delete_registered(self) -> None:
        # get selected project row
        selected_items = self.dashboard_projects.selectedItems()
        if not selected_items:
            return
        row = selected_items[0].row()
        name_item = self.dashboard_projects.item(row, 0)
        if not name_item:
            return
        path = name_item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if any(str(manager.project_root) == path for manager in self.process_slots.active):
            QMessageBox.information(self, "Process running", "Stop the project's active processes before deleting it.")
            return
        # confirm
        reply = QMessageBox.question(
            self,
            "Delete Project",
            f"Delete project '{name_item.text()}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            # close if open
            if self.current_project and self.current_project.root == Path(path):
                self._set_project(None)
                if self.current_project is not None:
                    return
            self.project_manager.delete(ProjectContext(name_item.text(), Path(path)))
            self._process_badges.pop(path, None)
            record = self.workspace_records.pop(path, None)
            if record:
                record["results_page"].close_resources()
                record["forcing_page"].close_resources()
                self.workspace_stack.removeWidget(record["workspace"])
                record["workspace"].deleteLater()
            # remove row
            self.dashboard_projects.removeRow(row)
            self._refresh_project_registry()
        except Exception as exc:
            QMessageBox.critical(self, "Delete failed", str(exc))

    def _open_registered(self, item: QTableWidgetItem) -> None:
        # open project on double click
        name_item = self.dashboard_projects.item(item.row(), 0)
        if not name_item:
            return
        path = name_item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        try:
            self._set_project(self.project_manager.open(Path(path)))
        except (ValueError, KeyError, OSError) as exc:
            QMessageBox.critical(self, "Project could not be opened", str(exc))


    def _set_project(self, context: ProjectContext) -> None:
        if self.current_project != context:
            editors = list(self.files_page.open_files.values())
            editors += [self.catalog_page.preview, self.recipe_page.preview]
            if any(editor.document().isModified() for editor in editors) or self.custom_tools_page.has_unsaved_changes:
                answer = QMessageBox.question(self, "Unsaved changes", "Switch project and discard unsaved changes?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._prune_inactive_workspaces(str(context.root) if context else "")
            self._dirty_pages = set()
        if context is None:
            self.current_project = None
            if "" not in self.workspace_records:
                record = self._build_project_workspace(None)
                self.workspace_records[""] = record
                self.workspace_stack.addWidget(record["workspace"])
            record = self.workspace_records[""]
            self._activate_project_workspace(record)
            self.workspace_button.setEnabled(False)
            self._navigate(0)
            return
        self.current_project = context
        key = str(context.root)
        if key not in self.workspace_records:
            record = self._build_project_workspace(context)
            self.workspace_records[key] = record
            self.workspace_stack.addWidget(record["workspace"])
        record = self.workspace_records[key]
        self._activate_project_workspace(record)
        badges = self._process_badges.get(key)
        if badges:
            for manager in list(badges):
                if not manager.is_running:
                    del badges[manager]
            if not badges:
                del self._process_badges[key]
        self.workspace_stack.setCurrentWidget(self.project_workspace)
        self.workspace_button.setEnabled(True)
        state = self.workflow.load_setup(context)
        valid = sum(status == "VALID" for status in state["steps"].values())
        self.project_name.setText(context.name)
        if hasattr(self, 'breadcrumb'):
            self.breadcrumb.setText(f'Project Workspace / {context.name}')
        self.statusBar().showMessage(f"Project opened: {context.root}")
        self._dirty_pages = set(self.context_pages)
        self.project_workspace.setCurrentIndex(0)
        self._refresh_visible_pages()
        self._refresh_project_registry()
        self._refresh_runtime_versions()
        aoi_path = context.root / "workspace" / "aoi.geojson"
        if aoi_path.exists():
            import json
            self._aoi_changed(json.loads(aoi_path.read_text(encoding="utf-8")))
        self._navigate(1)

    def _prune_inactive_workspaces(self, keep: str) -> None:
        for key, record in list(self.workspace_records.items()):
            # ponytail: Keep process workspaces for session terminal logs; reload logs from disk if memory becomes costly.
            if key == keep or any(manager._tool is not None for manager in record["managers"]):
                continue
            record["results_page"].close_resources()
            record["forcing_page"].close_resources()
            self.workspace_stack.removeWidget(record["workspace"])
            record["workspace"].deleteLater()
            del self.workspace_records[key]

    def _refresh_all(self) -> None:
        if self.current_project:
            self.project_name.setText(self.current_project.name)
        self._dirty_pages = set(self.context_pages)
        self._refresh_visible_pages()
        for page in self.system_context_pages:
            page.refresh_context()

    def _refresh_visible_pages(self, *_args) -> None:
        if not self.current_project:
            return
        page = self.project_workspace.currentWidget()
        while isinstance(page, QTabWidget):
            page = page.currentWidget()
        dirty = getattr(self, "_dirty_pages", set())
        if page in dirty:
            dirty.discard(page)
            page.refresh_context()

    def _refresh_model_config(self) -> None:
        if not hasattr(self, "_dirty_pages"):
            self._dirty_pages = set()
        self._dirty_pages.update((self.catalog_page, self.recipe_page, self.hydromt_page, self.overview_page))
        self._refresh_visible_pages()

    def _on_theme_changed(self, _mode: str) -> None:
        # FluentIcon renders its glyph color for the active theme; re-apply so
        # icons stay visible after switching between dark and light mode.
        for index, button in enumerate(self.nav_buttons):
            self._set_nav_icon(button, index, spaced=self._rail_expanded)

    def _aoi_changed(self, feature: dict) -> None:
        # Dashboard stays on the Indonesia overview; project pages focus the AOI.
        self.map_widget.set_aoi(feature, fit=False)
        self.overview_page.map.set_aoi(feature)
        self.results_page.map.set_aoi(feature)
        self.forcing_page.map.set_aoi(feature)
        bounds = self.workflow.aoi_bounds(feature)
        for page in self.tool_pages.values():
            page.set_aoi_bounds(bounds)
        self.custom_tools_page.set_aoi_bounds(bounds)
        self._dirty_pages.add(self.recipe_page)

    def closeEvent(self, event) -> None:
        if self.process_slots.active:
            reply = QMessageBox.question(
                self, "Processes still running",
                "Stop running tool, HydroMT, and Julia processes and exit HydroHub?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            event.ignore()
            self.process_slots.changed.connect(self._close_when_idle, Qt.ConnectionType.SingleShotConnection)
            for manager in list(self.process_slots.active):
                manager.stop()
            return
        if self.custom_tool_sessions.has_running:
            confirmation = QMessageBox(self)
            confirmation.setIcon(QMessageBox.Icon.Warning)
            confirmation.setWindowTitle("Custom tools still running")
            confirmation.setText("Stop all custom tool sessions and exit HydroHub?")
            confirmation.setInformativeText("Running processes will receive a stop request and be killed after the timeout.")
            stop_button = confirmation.addButton("Stop tools and exit", QMessageBox.ButtonRole.DestructiveRole)
            confirmation.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            confirmation.exec()
            if confirmation.clickedButton() is not stop_button:
                event.ignore()
                return
            event.ignore()
            self.custom_tool_sessions.idle.connect(self.close, Qt.ConnectionType.SingleShotConnection)
            self.custom_tool_sessions.stop_all()
            return
        for job in list(self.automation_page.runner.jobs.values()):
            if job["status"] not in {"SUCCESS", "FAILED", "CANCELLED"}:
                self.automation_page.runner.cancel(job["id"])
        for record in self.workspace_records.values():
            record["results_page"].close_resources()
            record["forcing_page"].close_resources()
        super().closeEvent(event)

    def _close_when_idle(self) -> None:
        if self.process_slots.active:
            self.process_slots.changed.connect(self._close_when_idle, Qt.ConnectionType.SingleShotConnection)
        else:
            QTimer.singleShot(0, self.close)

    def _confirm_custom_tool_install(
        self, session_id: str, packages: list[str], interpreter: str
    ) -> None:
        session = self.custom_tool_sessions.sessions.get(session_id)
        if not session or session.status != "AWAITING_APPROVAL":
            return
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Question)
        confirmation.setWindowTitle("Install custom tool dependencies?")
        confirmation.setText(
            f"Install required packages for '{session.tool_name}' and then run it?"
        )
        confirmation.setInformativeText(
            f"Project: {session.project_name}\n"
            f"Interpreter: {interpreter}\n\n"
            + "\n".join(packages)
        )
        install_button = confirmation.addButton(
            "Install packages and run", QMessageBox.ButtonRole.AcceptRole
        )
        confirmation.addButton("Cancel run", QMessageBox.ButtonRole.RejectRole)
        confirmation.exec()
        if confirmation.clickedButton() is install_button:
            self.custom_tool_sessions.approve_install(session_id)
        else:
            self.custom_tool_sessions.reject_install(session_id)

    def _process_state_changed(self, manager: ProcessManager, state: str) -> None:
        if state != "STARTING" or manager.project_root is None or manager._tool is None:
            return
        tool = manager._tool
        name = {"hydromt.cli": "HydroMT", "wflow.julia": "Julia"}.get(tool.id, tool.name)
        self._process_badges.setdefault(str(manager.project_root), {})[manager] = (name, "Running")
        self._refresh_project_registry()

    def _process_finished(self, manager: ProcessManager, exit_code: int, manifest: str) -> None:
        badges = self._process_badges.get(str(manager.project_root))
        if badges and manager in badges:
            name, _status = badges[manager]
            badges[manager] = (name, "Selesai" if exit_code == 0 else "Gagal")
            self._refresh_project_registry()
        message = "Tool completed." if exit_code == 0 else f"Tool failed with exit code {exit_code}."
        self.statusBar().showMessage(f"{message} Manifest: {manifest}")


class PathField(QWidget):
    def __init__(self, value: str = "") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.edit = QLineEdit(value)
        self.browse = QPushButton("Browse")
        self.browse.setAccessibleName("Browse path")
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, value: str) -> None:
        self.edit.setText(value)


class ToolWorkspace(QWidget):
    TITLES = {
        "download": ("DATA ACQUISITION", "Download center"),
        "preparing": ("DATA PIPELINE", "Preparing tools"),
        "qc": ("VALIDATION", "Quality control"),
    }

    def __init__(
        self,
        category: str,
        tools: list[ToolDefinition],
        process_manager: ProcessManager,
        default_interpreter: Path,
        workspace: Path,
        project_provider,
    ) -> None:
        super().__init__()
        self.category = category
        self.tools = tools
        self.process_manager = process_manager
        self.workspace = workspace
        self.project_provider = project_provider
        self.selected_tool: ToolDefinition | None = None
        self.parameter_widgets: dict[str, QWidget] = {}
        self._owns_process = False
        self._started_at: datetime | None = None
        self._build(default_interpreter)
        process_manager.output_received.connect(self._append_output)
        process_manager.state_changed.connect(self._state_changed)
        process_manager.finished.connect(self._finished)

    def _group_domains(self, tools: list[ToolDefinition]) -> None:
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self._tree_item_tool: dict[str, ToolDefinition] = {}
        groups: dict[str, list[ToolDefinition]] = {}
        for tool in tools:
            groups.setdefault(tool.domain, []).append(tool)
        for domain, domain_tools in groups.items():
            group_item = QTreeWidgetItem([domain])
            group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = group_item.font(0)
            font.setBold(True)
            group_item.setFont(0, font)
            self.tool_tree.addTopLevelItem(group_item)
            for tool in domain_tools:
                item = QTreeWidgetItem([tool.name])
                item.setData(0, Qt.ItemDataRole.UserRole, tool.id)
                item.setToolTip(0, tool.description)
                item.setText(1, f"v{tool.version}")
                group_item.addChild(item)
                self._tree_item_tool[tool.id] = tool
            self._tree_items[domain] = group_item
            group_item.setExpanded(True)

    def _build(self, interpreter: Path) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 20)
        layout.setSpacing(12)
        eyebrow_text, title_text = self.TITLES[self.category]
        eyebrow = QLabel(eyebrow_text)
        eyebrow.setObjectName("eyebrow")
        title = QLabel(title_text)
        title.setObjectName("pageTitle")
        note = QLabel("Configure and run versioned HydroHub tools. Runs and logs are tracked in the active project.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(note)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        layout.addWidget(divider)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tool_tree = QTreeWidget()
        self.tool_tree.setMinimumWidth(160)
        self.tool_tree.setHeaderHidden(True)
        self.tool_tree.setIndentation(16)
        self.tool_tree.setAccessibleName(f"Available {self.category} tools")
        self._group_domains(self.tools)
        if not self.tools:
            empty = QTreeWidgetItem(["No tools registered for this category."])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tool_tree.addTopLevelItem(empty)
        self.tool_tree.itemSelectionChanged.connect(self._select_tool)
        splitter.addWidget(self.tool_tree)

        details = QFrame()
        details.setObjectName("panel")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(16, 16, 16, 16)
        details_layout.setSpacing(12)
        self.tool_title = QLabel("Select a tool")
        self.tool_title.setObjectName("sectionTitle")
        self.tool_description = QLabel("Choose a built-in tool to configure and run.")
        self.tool_description.setObjectName("muted")
        self.tool_description.setWordWrap(True)
        self.tool_io = QLabel("Input and output fields appear after selecting a tool.")
        self.tool_io.setObjectName("muted")
        self.tool_io.setWordWrap(True)
        details_layout.addWidget(self.tool_title)
        details_layout.addWidget(self.tool_description)
        details_layout.addWidget(self.tool_io)
        details_layout.addSpacing(8)

        runtime_form = QFormLayout()
        runtime_form.setSpacing(12)
        runtime_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.module_path = QLineEdit()
        self.module_path.setReadOnly(True)
        self.interpreter = PathField(str(interpreter))
        self.interpreter.browse.clicked.connect(self._browse_interpreter)
        self.working_directory = PathField(str(self.workspace))
        self.working_directory.browse.clicked.connect(self._browse_workdir)
        runtime_form.addRow("Tool module", self.module_path)
        runtime_form.addRow("Python interpreter", self.interpreter)
        runtime_form.addRow("Working directory", self.working_directory)
        details_layout.addLayout(runtime_form)

        parameter_title = QLabel("Parameters")
        parameter_title.setObjectName("sectionTitle")
        details_layout.addWidget(parameter_title)
        self.parameter_container = QWidget()
        self.parameter_form = QFormLayout(self.parameter_container)
        self.parameter_form.setContentsMargins(0, 0, 8, 0)
        self.parameter_form.setSpacing(12)
        self.parameter_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        details_layout.addWidget(self.parameter_container)

        context_row = QHBoxLayout()
        self.context_status = QLabel("Project required before this tool can run")
        self.context_status.setObjectName("muted")
        self.context_status.setWordWrap(True)
        context_row.addWidget(self.context_status, 1)
        details_layout.addLayout(context_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.run_button = QPushButton("Review and run")
        self.run_button.setObjectName("primary")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.process_manager.stop)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        details_layout.addLayout(buttons)
        splitter.addWidget(scroll_panel(details))
        splitter.setSizes([360, 800])

        terminal = QFrame()
        terminal.setObjectName("terminalPanel")
        terminal_layout = QVBoxLayout(terminal)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.setSpacing(0)
        terminal_header = QFrame()
        terminal_header.setObjectName("terminalHeader")
        log_header = QHBoxLayout()
        log_header.setContentsMargins(12, 8, 12, 8)
        log_title = QLabel("EXECUTION TERMINAL")
        log_title.setObjectName("terminalTitle")
        self.process_state = QLabel("READY")
        self.process_state.setObjectName("statusReady")
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(self.process_state)
        terminal_header.setLayout(log_header)
        terminal_layout.addWidget(terminal_header)
        self.log = QPlainTextEdit()
        self.log.setObjectName("executionTerminal")
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setMinimumHeight(180)
        self.log.setAccessibleName("Tool execution log")
        self.log.setPlaceholderText("$ stdout and stderr will appear here...")
        terminal_layout.addWidget(self.log, 1)

        left_workspace = QSplitter(Qt.Orientation.Vertical)
        left_workspace.addWidget(splitter)
        left_workspace.addWidget(terminal)
        left_workspace.setStretchFactor(0, 3)
        left_workspace.setStretchFactor(1, 2)
        left_workspace.setSizes([430, 280])

        report_panel = QFrame()
        report_panel.setObjectName("reportPanel")
        report_layout = QVBoxLayout(report_panel)
        report_layout.setContentsMargins(16, 16, 16, 16)
        report_layout.setSpacing(12)
        report_header = QHBoxLayout()
        report_header.setSpacing(12)
        report_title = QLabel("EXECUTION REPORT")
        report_title.setObjectName("terminalTitle")
        self.report_status = QLabel("READY")
        self.report_status.setObjectName("statusReady")
        report_header.addWidget(report_title)
        report_header.addStretch()
        report_header.addWidget(self.report_status)
        report_layout.addLayout(report_header)
        self.activity = QProgressBar()
        self.activity.setTextVisible(False)
        self.activity.setFixedHeight(2)
        self.activity.hide()
        report_layout.addWidget(self.activity)
        report_scroll = QScrollArea()
        report_scroll.setWidgetResizable(True)
        report_scroll.setFrameShape(QFrame.Shape.NoFrame)
        report_content = QWidget()
        report_form = QFormLayout(report_content)
        report_form.setContentsMargins(0, 8, 4, 0)
        report_form.setVerticalSpacing(12)
        report_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.report_fields: dict[str, QLabel] = {}
        for key, label in (
            ("tool", "Tool"),
            ("command", "Command"),
            ("parameters", "Parameters"),
            ("started", "Start time"),
            ("completed", "End time"),
            ("duration", "Duration"),
            ("summary", "Summary"),
            ("outputs", "Output"),
            ("diagnostics", "Warning / Error"),
            ("exit_code", "Exit code"),
        ):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setObjectName("reportValue")
            self.report_fields[key] = value
            report_form.addRow(label, value)
        report_scroll.setWidget(report_content)
        report_layout.addWidget(report_scroll, 1)

        self.execution_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.execution_splitter.addWidget(left_workspace)
        self.execution_splitter.addWidget(report_panel)
        self.execution_splitter.setStretchFactor(0, 2)
        self.execution_splitter.setStretchFactor(1, 1)
        self.execution_splitter.setSizes([820, 420])
        layout.addWidget(self.execution_splitter, 1)
        if self.tools:
            first_group = self.tool_tree.topLevelItem(0)
            if first_group and first_group.childCount():
                first_group.child(0).setSelected(True)

    def refresh_context(self) -> None:
        project = self.project_provider()
        if project:
            self.context_status.setText(f"Project: {project.name} | manifest tracked")
            self.working_directory.setText(str(project.root))
            self.run_button.setEnabled(self.selected_tool is not None and not self.process_manager.is_running)
        else:
            self.context_status.setText("Project required before this tool can run")
            self.run_button.setEnabled(False)

    def _select_tool(self) -> None:
        items = self.tool_tree.selectedItems()
        if not items:
            self.selected_tool = None
            self.run_button.setEnabled(False)
            return
        tool_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        self.selected_tool = self._tree_item_tool.get(tool_id) if tool_id else None
        if not self.selected_tool:
            self.run_button.setEnabled(False)
            return
        self.tool_title.setText(self.selected_tool.name)
        self.tool_description.setText(self.selected_tool.description)
        output_names = {
            field.label
            for field in self.selected_tool.fields
            if any(token in field.name for token in ("output", "report", "destination"))
        }
        input_names = [
            field.label for field in self.selected_tool.fields if field.label not in output_names
        ]
        self.tool_io.setText(
            f"Input: {', '.join(input_names) or 'None'}\n"
            f"Output: {', '.join(sorted(output_names)) or 'Console/report only'}"
        )
        self.module_path.setText(self.selected_tool.module)
        self._build_parameter_form(self.selected_tool)
        self.run_button.setEnabled(
            not self.process_manager.is_running and self.project_provider() is not None
        )

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select working directory", self.working_directory.text())
        if path:
            self.working_directory.setText(path)

    def _browse_interpreter(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self, "Select Python interpreter", self.interpreter.text(), "Executable (*.exe);;All files (*)"
        )
        if selected:
            self.interpreter.setText(selected)

    def _build_parameter_form(self, tool: ToolDefinition) -> None:
        while self.parameter_form.rowCount():
            self.parameter_form.removeRow(0)
        self.parameter_widgets.clear()
        for field in tool.fields:
            widget = self._parameter_widget(field)
            self.parameter_widgets[field.name] = widget
            self.parameter_form.addRow(field.label, widget)
        self.set_aoi_bounds()

    def set_aoi_bounds(
        self, bounds: tuple[float, float, float, float] | None = None
    ) -> None:
        project = self.project_provider()
        bounds = bounds or (WorkflowManager.load_aoi_bounds(project) if project else None)
        if bounds is None:
            return
        for name, value in zip(("west", "south", "east", "north"), bounds):
            widget = self.parameter_widgets.get(name)
            if isinstance(widget, QDoubleSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))

    def _parameter_widget(self, field: ToolField) -> QWidget:
        if field.kind in {"bool", "boolean"}:
            widget = QCheckBox()
            widget.setChecked(bool(field.default))
            return widget
        if field.kind == "date":
            widget = QDateEdit()
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            parsed = QDate.fromString(str(field.default), "yyyy-MM-dd")
            widget.setDate(parsed if parsed.isValid() else QDate.currentDate())
            return widget
        if field.kind == "integer":
            widget = QSpinBox()
            widget.setRange(1, 1_000_000)
            widget.setValue(int(field.default))
            return widget
        if field.kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(-180.0, 1_000_000.0)
            widget.setDecimals(6)
            widget.setValue(float(field.default))
            return widget
        if field.choices:
            widget = QComboBox()
            widget.addItems(field.choices)
            widget.setCurrentText(str(field.default))
            return widget
        if field.kind in {"directory", "file"}:
            widget = PathField(str(field.default))
            widget.browse.setAccessibleName(f"Browse {field.label}")
            widget.browse.clicked.connect(
                lambda _checked=False, f=field, w=widget.edit: self._browse_parameter_path(f, w)
            )
            return widget
        widget = QLineEdit(str(field.default))
        if field.kind == "password":
            widget.setEchoMode(QLineEdit.EchoMode.Password)
        if field.environment_name:
            widget.setPlaceholderText(f"Passed as {field.environment_name}")
        return widget

    def _browse_parameter_path(self, field: ToolField, widget: QLineEdit) -> None:
        project = self.project_provider()
        start = str(project.root if project else self.workspace)
        current = Path(widget.text()).expanduser()
        if current.is_absolute():
            start = str(current if current.is_dir() else current.parent)
        if field.kind == "directory":
            selected = QFileDialog.getExistingDirectory(self, field.label, start)
        elif any(token in field.name for token in ("output", "report", "destination")):
            selected, _ = QFileDialog.getSaveFileName(self, field.label, start)
        else:
            selected, _ = QFileDialog.getOpenFileName(self, field.label, start)
        if selected:
            widget.setText(selected)

    @staticmethod
    def _widget_value(widget: QWidget):
        if isinstance(widget, PathField):
            return widget.text().strip()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QDateEdit):
            return widget.date().toString("yyyy-MM-dd")
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        if isinstance(widget, QComboBox):
            return widget.currentText()
        return widget.text().strip()

    def _execution_config(self) -> tuple[list[str], dict[str, str]]:
        if not self.selected_tool:
            return [], {}
        arguments: list[str] = []
        environment: dict[str, str] = {}
        project = self.project_provider()
        for field in self.selected_tool.fields:
            value = self._widget_value(self.parameter_widgets[field.name])
            if field.required and (value == "" or value is None):
                raise ValueError(f"{field.label} is required.")
            if field.environment_name:
                if value != "":
                    environment[field.environment_name] = str(value)
                continue
            option = f"--{field.name.replace('_', '-')}"
            if field.kind in {"bool", "boolean"}:
                arguments.append(option if value else f"--no-{field.name.replace('_', '-')}")
                continue
            if field.kind in {"directory", "file"} and project and value:
                path = Path(str(value)).expanduser()
                value = str(path if path.is_absolute() else project.root / path)
            arguments.extend([option, str(value)])
        return arguments, environment

    def _run(self) -> None:
        if not self.selected_tool:
            return
        if not self.project_provider():
            QMessageBox.information(self, "Project required", "Create or open a project before running this tool.")
            return
        try:
            arguments, environment = self._execution_config()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid parameters", str(exc))
            return
        command = " ".join(
            [f'"{self.interpreter.text()}"', "-u", "-m", self.selected_tool.module, *arguments]
        )
        answer = QMessageBox.question(
            self,
            "Run HydroHub tool",
            "Review the generated command. Credential values are passed securely through environment variables.\n\n"
            f"Command:\n{command}\n\nWorking directory:\n{self.working_directory.text()}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        project = self.project_provider()
        manifests = project.manifests_dir if project else self.workspace / ".hydrohub" / "runs"
        self.log.clear()
        self.log.appendPlainText(f"$ {command}\n")
        self._owns_process = True
        self._started_at = datetime.now(timezone.utc)
        self.report_fields["tool"].setText(
            f"{self.selected_tool.name}\n{self.selected_tool.id}"
        )
        self.report_fields["command"].setText(command)
        self.report_fields["parameters"].setText("\n".join(arguments) or "None")
        self.report_fields["started"].setText(self._started_at.isoformat())
        for key in ("completed", "duration", "summary", "outputs", "diagnostics", "exit_code"):
            self.report_fields[key].setText("—")
        try:
            self.process_manager.run_tool(
                self.selected_tool,
                Path(self.interpreter.text()),
                Path(self.working_directory.text()),
                manifests,
                extra_environment=environment,
                arguments=arguments,
            )
        except (RuntimeError, FileNotFoundError, OSError) as exc:
            self._owns_process = False
            self.activity.hide()
            self._set_report_status("ERROR")
            self.report_fields["diagnostics"].setText(str(exc))
            QMessageBox.critical(self, "Tool could not start", str(exc))

    def _append_output(self, text: str) -> None:
        if self._owns_process:
            self.log.moveCursor(QTextCursor.MoveOperation.End)
            self.log.insertPlainText(text)
            self.log.ensureCursorVisible()

    def _state_changed(self, state: str) -> None:
        self.process_state.setText(state)
        status_name = {
            "ERROR": "statusError",
            "WARNING": "statusWarning",
            "SUCCESS": "statusSuccess",
            "STARTING": "statusRunning",
            "RUNNING": "statusRunning",
            "STOPPING": "statusWarning",
        }.get(state, "statusReady")
        self.process_state.setObjectName(status_name)
        self.process_state.style().unpolish(self.process_state)
        self.process_state.style().polish(self.process_state)
        running = state in {"STARTING", "RUNNING", "STOPPING"}
        if self._owns_process:
            self._set_report_status(state)
            if running:
                self.activity.setRange(0, 0)
                self.activity.show()
            else:
                self.activity.hide()
        self.run_button.setEnabled(
            not running and self.selected_tool is not None and self.project_provider() is not None
        )
        self.stop_button.setEnabled(running)

    def _set_report_status(self, status: str) -> None:
        self.report_status.setText(status)
        object_name = {
            "ERROR": "statusError",
            "WARNING": "statusWarning",
            "SUCCESS": "statusSuccess",
            "RUNNING": "statusRunning",
            "STARTING": "statusRunning",
            "STOPPING": "statusWarning",
        }.get(status, "statusReady")
        self.report_status.setObjectName(object_name)
        self.report_status.style().unpolish(self.report_status)
        self.report_status.style().polish(self.report_status)

    def _finished(self, _exit_code: int, manifest_path: str) -> None:
        if not self._owns_process:
            return
        self._owns_process = False
        self.activity.hide()
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._set_report_status("ERROR")
            self.report_fields["diagnostics"].setText(
                "Execution finished, but its manifest could not be loaded."
            )
            return
        status = str(manifest.get("status", "ERROR"))
        self._set_report_status(status)
        self.report_fields["tool"].setText(
            f"{manifest.get('tool_name', self.selected_tool.name if self.selected_tool else '')}\n"
            f"{manifest.get('tool_id', '')}"
        )
        self.report_fields["command"].setText(" ".join(manifest.get("command", [])))
        self.report_fields["parameters"].setText(
            "\n".join(map(str, manifest.get("parameters", []))) or "None"
        )
        self.report_fields["started"].setText(str(manifest.get("started_at", "—")))
        self.report_fields["completed"].setText(str(manifest.get("completed_at", "—")))
        self.report_fields["duration"].setText(
            f"{manifest.get('duration_seconds', 0):.3f} seconds"
        )
        self.report_fields["summary"].setText(
            "\n".join(manifest.get("summary", [])) or "No summary was emitted."
        )
        self.report_fields["outputs"].setText(
            "\n".join(manifest.get("outputs", [])) or "No output path was detected."
        )
        diagnostics = [*manifest.get("warnings", []), *manifest.get("errors", [])]
        self.report_fields["diagnostics"].setText(
            "\n".join(diagnostics) or "No warnings or errors."
        )
        self.report_fields["exit_code"].setText(str(manifest.get("exit_code", "—")))


class CreateProjectDialog(QDialog):
    def __init__(self, workspace: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create new Wflow project")
        self.setModal(True)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        title = QLabel("Create a project workspace")
        title.setObjectName("pageTitle")
        note = QLabel("HydroHub creates the project structure, then opens its workspace.")
        note.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addSpacing(12)

        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Bengawan Solo")
        self.location = QLineEdit(str(workspace / "projects"))
        location_row = QHBoxLayout()
        location_row.addWidget(self.location, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_location)
        location_row.addWidget(browse)
        self.model_type = QComboBox()
        self.model_type.addItem("Wflow")
        self.description = QLineEdit()
        self.description.setPlaceholderText("Optional project description")
        form.addRow("Project name", self.name)
        form.addRow("Parent location", location_row)
        form.addRow("Model type", self.model_type)
        form.addRow("Description", self.description)
        bounds_row = QHBoxLayout()
        self.aoi_fields = []
        for placeholder, minimum, maximum, value in (
            ("West", -180.0, 180.0, 95.0),
            ("South", -90.0, 90.0, -11.0),
            ("East", -180.0, 180.0, 141.0),
            ("North", -90.0, 90.0, 6.0),
        ):
            field = QDoubleSpinBox()
            field.setRange(minimum, maximum)
            field.setDecimals(6)
            field.setValue(value)
            field.setToolTip(placeholder)
            self.aoi_fields.append(field)
            bounds_row.addWidget(field)
        form.addRow("AOI bounds", bounds_row)
        self.aoi_file = QLineEdit()
        self.aoi_file.setPlaceholderText("Optional GeoJSON or Shapefile ZIP (Polygon/MultiPolygon)")
        self.aoi_feature: dict | None = None
        aoi_row = QHBoxLayout()
        aoi_row.addWidget(self.aoi_file, 1)
        browse_aoi = QPushButton("Browse")
        browse_aoi.clicked.connect(self._browse_aoi)
        aoi_row.addWidget(browse_aoi)
        form.addRow("AOI file", aoi_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create project")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_location(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select project parent location", self.location.text()
        )
        if path:
            self.location.setText(path)

    def _browse_aoi(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select AOI file", self.location.text(), "AOI files (*.geojson *.json *.zip)"
        )
        if path:
            self.aoi_file.setText(path)

    def aoi_data(self) -> dict:
        if self.aoi_file.text().strip():
            return WorkflowManager.load_aoi_file(Path(self.aoi_file.text().strip()), f"{self.name.text().strip()} AOI")
        west, south, east, north = (field.value() for field in self.aoi_fields)
        if west >= east or south >= north:
            raise ValueError("AOI bounds are invalid.")
        return {
            "type": "Feature",
            "properties": {"name": f"{self.name.text().strip()} AOI"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
            },
        }

    def accept(self) -> None:
        if not self.name.text().strip() or not self.location.text().strip():
            QMessageBox.warning(self, "Required fields", "Project name and location are required.")
            return
        try:
            self.aoi_feature = self.aoi_data()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid AOI", str(exc))
            return
        super().accept()
    QDoubleSpinBox,
