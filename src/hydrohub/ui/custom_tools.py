from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import PrimaryPushButton, PushButton

from hydrohub.core.custom_tool_sessions import CustomToolSessionHub
from hydrohub.core.tool_groups import ToolGroups
from hydrohub.core.workflow_manager import WorkflowManager
from hydrohub.plugins.custom_tools import (
    MANIFEST,
    SCRIPT,
    CustomToolStore,
    SecretStore,
    export_template,
    select_options,
)
from hydrohub.ui.code_editor import CodeEditor
from hydrohub.ui.workflow_pages import ProcessTerminal


class CustomToolsPage(QWidget):
    def __init__(
        self,
        provider,
        session_hub: CustomToolSessionHub,
        library_root: Path,
        secret_store: SecretStore | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.hub = session_hub
        self.library_root = Path(library_root)
        self.groups = ToolGroups(self.library_root)
        self.group_data = {"groups": [], "assignments": {}}
        self._active_group = None
        self.secrets = secret_store or SecretStore()
        self.tools: list[dict] = []
        self.selected_tool: dict | None = None
        self.parameter_widgets: dict[str, QWidget] = {}
        self.environment_widgets: dict[str, QLineEdit] = {}
        self.session_widgets: dict[
            str, tuple[QWidget, ProcessTerminal, QLabel, PushButton, PushButton]
        ] = {}
        self._loading = False
        self._build_ui()
        self.hub.session_added.connect(self._add_session)
        self.hub.session_changed.connect(self._update_session)
        self.hub.session_output.connect(self._session_output)
        for session_id in self.hub.sessions:
            self._add_session(session_id)
        self.refresh_context()

    @property
    def has_unsaved_changes(self) -> bool:
        return any(
            editor.document().isModified()
            for editor in (self.manifest_editor, self.script_editor, self.config_editor)
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Custom tools")
        title.setObjectName("pageTitle")
        description = QLabel(
            "Edit reusable Python tools and keep parameters, environment, and secrets isolated per project."
        )
        description.setWordWrap(True)
        description.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(description)

        actions = QHBoxLayout()
        self.new_button = PushButton("New tool")
        self.new_button.clicked.connect(self._new_tool)
        self.template_button = PushButton("Download template")
        self.template_button.clicked.connect(self._template)
        self.upload_button = PushButton("Import ZIP")
        self.upload_button.clicked.connect(self._upload)
        self.select = QComboBox()
        self.select.setAccessibleName("Custom tool")
        self.select.currentIndexChanged.connect(self._select)
        self.library_filter = QComboBox()
        self.library_filter.setAccessibleName("Filter tool library group")
        self.library_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.library_filter.currentIndexChanged.connect(self._filter_tools)
        for widget in (
            self.new_button,
            self.template_button,
            self.upload_button,
            self.select,
            self.library_filter,
        ):
            actions.addWidget(widget)
        actions.addStretch()
        layout.addLayout(actions)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.editors = QTabWidget()
        self.script_editor = CodeEditor()
        self.script_editor.set_language("python")
        self.manifest_editor = CodeEditor()
        self.manifest_editor.set_language("json")
        self.config_editor = CodeEditor()
        self.config_editor.set_language("json")
        self.editors.addTab(self.script_editor, "main.py")
        self.editors.addTab(self.manifest_editor, "tool.json")
        self.editors.addTab(self.config_editor, "Project config")
        editor_layout.addWidget(self.editors)
        editor_actions = QHBoxLayout()
        self.save_definition_button = PrimaryPushButton("Save definition")
        self.save_definition_button.clicked.connect(self._save_definition)
        self.save_config_button = PushButton("Save project config")
        self.save_config_button.clicked.connect(self._save_config)
        self.delete_button = PushButton("Delete from library")
        self.delete_button.clicked.connect(self._delete_tool)
        editor_actions.addWidget(self.save_definition_button)
        editor_actions.addWidget(self.save_config_button)
        editor_actions.addWidget(self.delete_button)
        editor_actions.addStretch()
        editor_layout.addLayout(editor_actions)

        runtime_panel = QWidget()
        runtime_layout = QVBoxLayout(runtime_panel)
        runtime_layout.setContentsMargins(0, 0, 0, 0)
        runtime_title = QLabel("Project runtime")
        runtime_title.setObjectName("sectionTitle")
        runtime_layout.addWidget(runtime_title)
        self.runtime_scroll = QScrollArea()
        self.runtime_scroll.setWidgetResizable(True)
        self.form_widget = QWidget()
        self.form = QFormLayout(self.form_widget)
        self.runtime_scroll.setWidget(self.form_widget)
        runtime_layout.addWidget(self.runtime_scroll, 1)
        self.secret_button = PushButton("Set secrets")
        self.secret_button.clicked.connect(self._set_secrets)
        self.run_button = PrimaryPushButton("Run in new session")
        self.run_button.clicked.connect(self._run)
        runtime_buttons = QHBoxLayout()
        runtime_buttons.addWidget(self.secret_button)
        runtime_buttons.addWidget(self.run_button)
        runtime_layout.addLayout(runtime_buttons)

        upper = QSplitter(Qt.Orientation.Horizontal)
        upper.addWidget(editor_panel)
        upper.addWidget(runtime_panel)
        upper.setSizes([760, 360])
        upper.setChildrenCollapsible(False)

        self.sessions = QTabWidget()
        self.sessions.setTabsClosable(True)
        self.sessions.setDocumentMode(True)
        self.sessions.tabCloseRequested.connect(self._close_session)
        self.sessions.setAccessibleName("Custom tool execution sessions")
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(upper)
        splitter.addWidget(self.sessions)
        splitter.setSizes([520, 260])
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)
        self.status = QLabel("Open a project to configure custom tools.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

    def _store(self) -> CustomToolStore | None:
        project = self.provider()
        return CustomToolStore(project.root, self.library_root) if project else None

    def refresh_context(self) -> None:
        if self.has_unsaved_changes:
            return
        store = self._store()
        try:
            self.group_data = self.groups.load()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self._loading = True
        self.library_filter.clear()
        self.library_filter.addItem("All groups", None)
        self.library_filter.addItem("Ungrouped", "")
        for group in self.group_data["groups"]:
            self.library_filter.addItem(group, group)
        self.library_filter.setCurrentIndex(max(0, self.library_filter.findData(self._active_group)))
        self.tools = []
        errors = []
        if store:
            self.tools, errors = store.discover()
        self._loading = False
        self._filter_tools()
        if errors:
            self.status.setText("; ".join(errors))

    def _filter_tools(self, *_args) -> None:
        if self._loading:
            return
        active = self.library_filter.currentData()
        current_id = self.selected_tool["id"] if self.selected_tool else None
        tools = [tool for tool in self.tools if active is None
                 or self.group_data["assignments"].get(tool["id"], "") == active]
        keep_current = any(tool["id"] == current_id for tool in tools)
        if not keep_current and not self._confirm_discard():
            self.library_filter.blockSignals(True)
            self.library_filter.setCurrentIndex(max(0, self.library_filter.findData(self._active_group)))
            self.library_filter.blockSignals(False)
            return
        if not keep_current:
            for editor in (self.script_editor, self.manifest_editor, self.config_editor):
                editor.document().setModified(False)
        self._active_group = active
        self._loading = True
        self.select.clear()
        for tool in tools:
            source = "project" if tool["source"] == "legacy" else "library"
            self.select.addItem(f"{tool['name']} ({source})", tool["id"])
        self.select.setCurrentIndex(max(0, self.select.findData(current_id)) if tools else -1)
        self._loading = False
        self.status.setText(f"{len(tools)} of {len(self.tools)} tools available.")
        if keep_current and self.has_unsaved_changes:
            return
        self._select()

    def select_tool(self, tool_id: str) -> bool:
        self.refresh_context()
        if self.select.findData(tool_id) < 0 and any(tool["id"] == tool_id for tool in self.tools):
            self.library_filter.setCurrentIndex(0)
        index = self.select.findData(tool_id)
        if index < 0:
            return False
        self.select.setCurrentIndex(index)
        return bool(self.selected_tool and self.selected_tool["id"] == tool_id)

    def _confirm_discard(self) -> bool:
        if not self.has_unsaved_changes:
            return True
        answer = QMessageBox.question(
            self,
            "Discard custom tool changes?",
            "Unsaved definition or project configuration changes will be lost.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _select(self, *_args) -> None:
        if self._loading:
            return
        tool_id = self.select.currentData()
        candidate = next((tool for tool in self.tools if tool["id"] == tool_id), None)
        if self.selected_tool and candidate is not self.selected_tool and not self._confirm_discard():
            old = self.select.findData(self.selected_tool["id"])
            self.select.blockSignals(True)
            self.select.setCurrentIndex(old)
            self.select.blockSignals(False)
            return
        self.selected_tool = candidate
        self._loading = True
        if not candidate:
            for editor in (self.script_editor, self.manifest_editor, self.config_editor):
                editor.clear()
            self._build_runtime_form({}, {})
        else:
            store = self._store()
            config = store.load_config(candidate["id"]) if store else {}
            self.script_editor.setPlainText((candidate["path"] / "main.py").read_text(encoding="utf-8"))
            manifest = {key: value for key, value in candidate.items() if key not in {"path", "source"}}
            self.manifest_editor.setPlainText(json.dumps(manifest, indent=2) + "\n")
            self.config_editor.setPlainText(json.dumps(config, indent=2) + "\n")
            self._build_runtime_form(candidate, config)
        for editor in (self.script_editor, self.manifest_editor, self.config_editor):
            editor.document().setModified(False)
        self._loading = False
        enabled = candidate is not None
        self.save_definition_button.setEnabled(enabled)
        self.save_config_button.setEnabled(enabled)
        self.run_button.setEnabled(enabled and self.provider() is not None)
        self.secret_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled and candidate.get("source") == "library" if candidate else False)

    def _build_runtime_form(self, tool: dict, config: dict) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.parameter_widgets = {}
        self.environment_widgets = {}
        configured_parameters = config.get("parameters", {})
        configured_environment = config.get("environment", {})
        for field in tool.get("parameters", []):
            kind = field["type"]
            value = configured_parameters.get(field["name"], field.get("default", False if kind == "boolean" else ""))
            if kind == "boolean":
                widget = QCheckBox()
                widget.setChecked(bool(value))
            elif kind == "select":
                widget = QComboBox()
                options = select_options(field)
                for option in options:
                    widget.addItem(option["label"], option["value"])
                selected = str(value or field.get("default", options[0]["value"]))
                index = widget.findData(selected)
                widget.setCurrentIndex(index if index >= 0 else 0)
            else:
                widget = QLineEdit(str(value))
            label = field.get("label", field["name"]) + (" *" if field.get("required") else "")
            widget.setAccessibleName(label)
            self.parameter_widgets[field["name"]] = widget
            self.form.addRow(label, widget)
        for field in tool.get("environment", []):
            name = field["name"]
            label = field.get("label", name) + (" *" if field.get("required") else "")
            if field.get("secret"):
                stored = bool(self.provider() and self.secrets.get(self.provider().root, tool["id"], name))
                status = QLabel("Stored in OS keyring" if stored else "Not set")
                status.setAccessibleName(label)
                self.form.addRow(label, status)
                continue
            widget = QLineEdit(str(configured_environment.get(name, field.get("default", ""))))
            widget.setAccessibleName(label)
            self.environment_widgets[name] = widget
            self.form.addRow(label, widget)
        self.set_aoi_bounds()

    def set_aoi_bounds(
        self, bounds: tuple[float, float, float, float] | None = None
    ) -> None:
        project = self.provider()
        self.set_aoi_path()
        bounds = bounds or (WorkflowManager.load_aoi_bounds(project) if project else None)
        if bounds is None:
            return
        for name, value in zip(("west", "south", "east", "north"), bounds):
            widget = self.parameter_widgets.get(name)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))

    def set_aoi_path(self) -> None:
        project = self.provider()
        if not project:
            return
        path = project.root / "workspace" / "aoi.geojson"
        widget = self.parameter_widgets.get("aoi_path")
        if path.is_file() and isinstance(widget, QLineEdit):
            widget.setText(str(path))

    def _definition_store(self) -> CustomToolStore:
        project = self.provider()
        if not project or not self.selected_tool:
            raise ValueError("Open a project and select a tool first.")
        if self.selected_tool["source"] == "legacy":
            return CustomToolStore(project.root)
        return CustomToolStore(project.root, self.library_root)

    def _save_definition(self) -> None:
        if not self.selected_tool:
            return
        try:
            self._definition_store().save_definition(
                self.selected_tool["id"],
                self.manifest_editor.toPlainText(),
                self.script_editor.toPlainText(),
            )
        except (OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
            self.status.setText(f"Definition not saved: {exc}")
            self.editors.setCurrentWidget(
                self.script_editor if isinstance(exc, SyntaxError) else self.manifest_editor
            )
            return
        self.script_editor.document().setModified(False)
        self.manifest_editor.document().setModified(False)
        self.status.setText("Definition saved.")
        self.refresh_context()

    def _runtime_config(self) -> dict:
        if not self.selected_tool:
            return {}
        current = json.loads(self.config_editor.toPlainText() or "{}")
        parameters = dict(current.get("parameters", {}))
        environment = dict(current.get("environment", {}))
        for field in self.selected_tool.get("parameters", []):
            widget = self.parameter_widgets[field["name"]]
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
            else:
                value = widget.text()
            parameters[field["name"]] = value
        for name, widget in self.environment_widgets.items():
            environment[name] = widget.text()
        secret_names = [
            field["name"] for field in self.selected_tool.get("environment", []) if field.get("secret")
        ]
        secret_names.extend(current.get("secrets", []))
        return {
            **current,
            "schema_version": 1,
            "tool_id": self.selected_tool["id"],
            "parameters": parameters,
            "environment": environment,
            "secrets": list(dict.fromkeys(secret_names)),
        }

    def _save_config(self, from_runtime: bool = False) -> bool:
        store = self._store()
        if not store or not self.selected_tool:
            return False
        try:
            previous = store.load_config(self.selected_tool["id"])
            source = self._runtime_config() if from_runtime else self.config_editor.toPlainText()
            data = store.save_config(self.selected_tool["id"], source)
            for name in set(previous.get("secrets", [])) - set(data.get("secrets", [])):
                self.secrets.set(self.provider().root, self.selected_tool["id"], name, "")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.status.setText(f"Project configuration not saved: {exc}")
            self.editors.setCurrentWidget(self.config_editor)
            return False
        self.config_editor.setPlainText(json.dumps(data, indent=2) + "\n")
        self.config_editor.document().setModified(False)
        self._build_runtime_form(self.selected_tool, data)
        self.status.setText("Project configuration saved.")
        return True

    def _new_tool(self) -> None:
        store = self._store()
        if not store or not self._confirm_discard():
            return
        tool_id, accepted = QInputDialog.getText(self, "New custom tool", "Tool ID")
        if not accepted or not tool_id.strip():
            return
        name, accepted = QInputDialog.getText(self, "New custom tool", "Display name", text=tool_id.strip())
        if not accepted or not name.strip():
            return
        manifest = deepcopy(MANIFEST)
        manifest.update(id=tool_id.strip(), name=name.strip(), parameters=[], environment=[])
        try:
            store.create(manifest, SCRIPT)
        except (OSError, ValueError, FileExistsError) as exc:
            QMessageBox.warning(self, "Tool not created", str(exc))
            return
        self.selected_tool = None
        self.select_tool(tool_id.strip())

    def _delete_tool(self) -> None:
        if not self.selected_tool or self.selected_tool.get("source") != "library":
            return
        tool_id = self.selected_tool["id"]
        answer = QMessageBox.question(
            self,
            "Delete reusable tool?",
            f"Delete '{self.selected_tool['name']}' from the reusable library? Project configurations remain.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store().delete(tool_id)
        self.selected_tool = None
        self.refresh_context()

    def _template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save template", "hydrohub-tool-template.zip", "ZIP (*.zip)"
        )
        if path:
            export_template(Path(path))

    def _upload(self) -> None:
        store = self._store()
        project = self.provider()
        if not store or not project:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import reusable tool", str(project.root), "ZIP (*.zip)"
        )
        if not path:
            return
        try:
            store.install(Path(path))
        except (OSError, ValueError, FileExistsError) as exc:
            QMessageBox.warning(self, "Tool not imported", str(exc))
            return
        self.selected_tool = None
        self.refresh_context()

    def _set_secrets(self) -> None:
        project = self.provider()
        if not project or not self.selected_tool:
            return
        try:
            config = json.loads(self.config_editor.toPlainText() or "{}")
        except json.JSONDecodeError as exc:
            self.status.setText(f"Fix project configuration JSON first: {exc}")
            return
        names = list(config.get("secrets", []))
        names.extend(
            field["name"]
            for field in self.selected_tool.get("environment", [])
            if field.get("secret")
        )
        names = list(dict.fromkeys(names))
        if not names:
            QMessageBox.information(
                self,
                "No secret variables",
                "Add a secret environment declaration to tool.json or a name to project config secrets.",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Set project secrets")
        layout = QVBoxLayout(dialog)
        note = QLabel("Blank values keep existing secrets. Select Clear to delete a stored value.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        fields: dict[str, tuple[QLineEdit, QCheckBox]] = {}
        for name in names:
            row = QHBoxLayout()
            editor = QLineEdit()
            editor.setEchoMode(QLineEdit.EchoMode.Password)
            editor.setAccessibleName(name)
            editor.setPlaceholderText("Stored" if self.secrets.get(project.root, self.selected_tool["id"], name) else "Not set")
            clear = QCheckBox("Clear")
            row.addWidget(editor)
            row.addWidget(clear)
            form.addRow(name, row)
            fields[name] = (editor, clear)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            for name, (editor, clear) in fields.items():
                if clear.isChecked() or editor.text():
                    self.secrets.set(project.root, self.selected_tool["id"], name, "" if clear.isChecked() else editor.text())
        except RuntimeError as exc:
            QMessageBox.warning(self, "Secrets not saved", str(exc))
            return
        self.status.setText("Secrets updated in OS keyring.")
        self._build_runtime_form(self.selected_tool, config)

    def _execution(self) -> tuple[list[str], dict[str, str], Path]:
        if not self.selected_tool:
            raise ValueError("Select a tool first.")
        project = self.provider()
        config = self._store().load_config(self.selected_tool["id"])
        arguments = ["-u", str(self.selected_tool["path"] / self.selected_tool.get("entrypoint", "main.py"))]
        values = config.get("parameters", {})
        for field in self.selected_tool.get("parameters", []):
            value = values.get(field["name"], field.get("default", ""))
            if field.get("required") and (value == "" or value is None):
                raise ValueError(f"{field.get('label', field['name'])} is required.")
            if field["type"] == "boolean":
                if value:
                    arguments.append("--" + field["name"])
                continue
            if value == "" or value is None:
                continue
            if field["type"] == "integer":
                int(value)
            elif field["type"] == "number":
                float(value)
            elif field["type"] in {"file", "directory", "output"}:
                path = Path(str(value)).expanduser()
                path = path if path.is_absolute() else project.root / path
                if field["type"] == "file" and not path.is_file():
                    raise ValueError(f"File not found: {path}")
                if field["type"] == "directory" and not path.is_dir():
                    raise ValueError(f"Directory not found: {path}")
                value = str(path)
            arguments.extend(["--" + field["name"], str(value)])
        environment = dict(config.get("environment", {}))
        declarations = {field["name"]: field for field in self.selected_tool.get("environment", [])}
        for name in config.get("secrets", []):
            value = self.secrets.get(project.root, self.selected_tool["id"], name)
            if value:
                environment[name] = value
        for name, field in declarations.items():
            value = environment.get(name, field.get("default", ""))
            if field.get("required") and not value:
                raise ValueError(f"{field.get('label', name)} is required.")
            if value:
                environment[name] = str(value)
        return arguments, environment, Path(config.get("interpreter", sys.executable))

    def _run(self) -> None:
        project = self.provider()
        if not project or not self.selected_tool or not self._save_config(from_runtime=True):
            return
        try:
            arguments, environment, interpreter = self._execution()
            session = self.hub.start(
                dict(self.selected_tool), project, interpreter, arguments, environment
            )
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "Tool could not start", str(exc))
            return
        self.sessions.setCurrentWidget(self.session_widgets[session.id][0])
        self.status.setText(f"Started {session.tool_name} for {session.project_name}.")

    @Slot(str)
    def _add_session(self, session_id: str) -> None:
        if session_id in self.session_widgets or session_id not in self.hub.sessions:
            return
        session = self.hub.sessions[session_id]
        project = self.provider()
        if not project or session.project_root != project.root:
            return
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        status = QLabel()
        stop = PushButton("Stop session")
        stop.clicked.connect(lambda _=False, sid=session_id: self.hub.stop(sid))
        restart = PushButton("Restart")
        restart.clicked.connect(lambda _=False, sid=session_id: self.hub.restart(sid))
        header.addWidget(status)
        header.addStretch()
        header.addWidget(stop)
        header.addWidget(restart)
        terminal = ProcessTerminal()
        terminal.set_command(session.command)
        if session.output:
            terminal.append_output(session.output)
        layout.addLayout(header)
        layout.addWidget(terminal, 1)
        self.session_widgets[session_id] = (page, terminal, status, stop, restart)
        self.sessions.addTab(page, session.tool_name)
        self._update_session(session_id)

    @Slot(str)
    def _update_session(self, session_id: str) -> None:
        if session_id not in self.hub.sessions:
            widgets = self.session_widgets.pop(session_id, None)
            if widgets:
                self.sessions.removeTab(self.sessions.indexOf(widgets[0]))
            return
        self._add_session(session_id)
        if session_id not in self.session_widgets:
            return
        session = self.hub.sessions[session_id]
        page, _terminal, status, stop, restart = self.session_widgets[session_id]
        status.setText(f"{session.project_name} | {session.status}")
        stop.setEnabled(session.is_running)
        restart.setEnabled(session.status != "STOPPING")
        index = self.sessions.indexOf(page)
        if index >= 0:
            self.sessions.setTabText(index, f"{session.tool_name} [{session.status}]")

    @Slot(str, str)
    def _session_output(self, session_id: str, text: str) -> None:
        widgets = self.session_widgets.get(session_id)
        if widgets:
            widgets[1].append_output(text)

    def _close_session(self, index: int) -> None:
        page = self.sessions.widget(index)
        session_id = next(
            (sid for sid, widgets in self.session_widgets.items() if widgets[0] is page), ""
        )
        session = self.hub.sessions.get(session_id)
        if not session:
            return
        if session.is_running:
            QMessageBox.information(self, "Session still running", "Stop this session before closing its tab.")
            return
        self.hub.close(session_id)
