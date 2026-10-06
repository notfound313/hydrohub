"""Global Automation workspace: pipeline editor, catalog and live jobs."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

import yaml
from PySide6.QtCore import Qt, QTimer, Signal, QTime
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout,
    QFileDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QTimeEdit, QSplitter, QApplication, QScrollArea, QGridLayout, QToolButton, QMenu)

from hydrohub.core.automation import (discover_projects, next_trigger,
                                       tool_catalog, validate_pipeline, pipeline_nodes, validate_tool_registration)
from hydrohub.core.automation_scheduler import AutomationScheduler
from hydrohub.plugins.custom_tools import CustomToolStore
from hydrohub.ui.code_editor import CodeEditor
from hydrohub.ui.automation_canvas import WorkflowCanvas


class AutomationPage(QWidget):
    job_changed = Signal()

    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace
        self.scheduler = AutomationScheduler(workspace, QApplication.instance())
        QApplication.instance().aboutToQuit.connect(self.scheduler.shutdown)
        self.store = self.scheduler.store
        self.runner = self.scheduler.runner
        self._builder_spec = {}
        self._selected_node = None
        self._canvas_dirty = False
        self._populating = False
        self.projects = {}
        self.catalog = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 22)
        label = QLabel("AUTOMATION")
        label.setObjectName("eyebrow")
        layout.addWidget(label)
        title = QLabel("Automation workspace")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_list()
        self._build_editor()
        self._build_monitor()
        self._build_catalog()
        self.refresh_context()
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(self.refresh_jobs)
        timer.start()
        self._timer = timer
        self.scheduler.failed.connect(lambda message: self.editor_status.setText(f"Schedule stopped: {message}"))

    def _build_list(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        buttons = QHBoxLayout()
        for text, callback in (("New", self.new), ("Edit", self.edit), ("Run Now", self.run_now),
                               ("Enable / Disable", self.toggle), ("Delete", self.delete_automation), ("Refresh", self.refresh_context)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.automations = QTableWidget(0, 4)
        self.automations.setHorizontalHeaderLabels(("Name", "ID", "Status", "Next trigger (local)"))
        self.automations.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.automations.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.automations.horizontalHeader().setStretchLastSection(True)
        self.automations.doubleClicked.connect(self.edit)
        layout.addWidget(self.automations)
        self.tabs.addTab(panel, "Automations")

    def _build_editor(self):
        panel = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.trigger_form = form
        self.name = QLineEdit()
        self.identifier = QLineEdit()
        self.trigger = QComboBox()
        self.trigger.addItems(("manual", "monthly_on_date", "interval", "cron"))
        self.day = QSpinBox()
        self.day.setRange(1, 31)
        self.day.setValue(4)
        self.clock = QTimeEdit(QTime(2, 0))
        self.clock.setDisplayFormat("HH:mm")
        self.cron = QLineEdit("0 2 * * *")
        self.workers = QSpinBox()
        self.workers.setRange(1, 32)
        self.workers.setValue(2)
        for label, widget in (("Name", self.name), ("ID", self.identifier), ("Trigger", self.trigger),
                              ("Day of month / interval days", self.day), ("Time HH:MM", self.clock),
                              ("Cron (minute hour day month weekday)", self.cron),
                              ("Parallel models", self.workers)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.schedule_preview = QLabel()
        self.schedule_preview.setWordWrap(True)
        layout.addWidget(self.schedule_preview)
        self.trigger.currentTextChanged.connect(self._trigger_changed)
        self.clock.timeChanged.connect(self._clock_changed)
        self.cron.textChanged.connect(self._cron_changed)
        self.day.valueChanged.connect(self._update_schedule_preview)
        self._trigger_changed()
        controls = QGridLayout()
        self.target = QComboBox()
        add_target = QPushButton("Add target")
        add_target.clicked.connect(self._add_target)
        self.tool = QComboBox()
        add_step = QPushButton("Add step")
        add_step.clicked.connect(self._add_step)
        for row, (label, combo, button) in enumerate((("Target", self.target, add_target), ("Tool", self.tool, add_step))):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(20)
            controls.addWidget(QLabel(label), row, 0)
            controls.addWidget(combo, row, 1)
            controls.addWidget(button, row, 2)
        controls.setColumnStretch(1, 1)
        layout.addLayout(controls)
        self.target_list = QListWidget()
        self.target_list.setMaximumHeight(85)
        lists = QHBoxLayout()
        lists.addWidget(self.target_list)
        layout.addLayout(lists)
        actions = QHBoxLayout()
        for text, callback in (("Generate YAML", self._generate), ("Load YAML into canvas", self._load_yaml), ("Save automation", self.save)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        more = QToolButton()
        more.setText("More actions")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        for text, callback in (("Remove target", lambda: self.target_list.takeItem(self.target_list.currentRow())),
                               ("Remove node", self._remove_node), ("Auto layout", self._move_step),
                               ("Import YAML", self._import_yaml), ("Export YAML", self._export_yaml)):
            menu.addAction(text, callback)
        more.setMenu(menu)
        actions.addWidget(more)
        layout.addLayout(actions)
        self.canvas = WorkflowCanvas()
        self.canvas.selected.connect(self._select_node)
        self.canvas.changed.connect(self._canvas_changed)
        properties = QWidget()
        properties_layout = QVBoxLayout(properties)
        node_form = QFormLayout()
        self.node_id = QLineEdit()
        self.node_tool = QComboBox()
        self.config_mode = QComboBox()
        self.config_mode.addItems(("global", "local"))
        self.node_timeout = QSpinBox()
        self.node_timeout.setRange(0, 2147483647)
        self.node_timeout.setSpecialValueText("Tool default")
        self.node_policy = QComboBox()
        self.node_policy.addItems(("inherit", "stop_model", "skip_step", "retry"))
        self.node_retries = QSpinBox()
        self.node_retries.setRange(0, 10)
        self.node_next = QLineEdit()
        self.node_next.setPlaceholderText("node_a, node_b")
        self.connection_status = QComboBox()
        self.connection_status.addItems(("always", "ok", "warning", "error"))
        self.connection_status.currentTextChanged.connect(lambda value: setattr(self.canvas, "connection_status", value))
        for label, widget in (("Node ID", self.node_id), ("Tool", self.node_tool),
                              ("Config mode", self.config_mode), ("Timeout (seconds)", self.node_timeout),
                              ("On failure", self.node_policy), ("Retries", self.node_retries),
                              ("Next node IDs", self.node_next), ("Drag connection condition", self.connection_status)):
            widget.setAccessibleName(label)
            node_form.addRow(label, widget)
        properties_layout.addLayout(node_form)
        params_label = QLabel("Parameters (YAML mapping; Local reads project/model settings)")
        params_label.setWordWrap(True)
        properties_layout.addWidget(params_label)
        self.node_params = QPlainTextEdit("{}")
        self.node_params.setMaximumHeight(100)
        self.node_params.setAccessibleName("Node parameters")
        self.config_mode.currentTextChanged.connect(lambda mode: self.node_params.setEnabled(mode == "global"))
        properties_layout.addWidget(self.node_params)
        properties_layout.addWidget(QLabel("Status branches (e.g. warning: [qc_node])"))
        self.node_branches = QPlainTextEdit("{}")
        self.node_branches.setMaximumHeight(80)
        self.node_branches.setAccessibleName("Node status branches")
        properties_layout.addWidget(self.node_branches)
        apply_node = QPushButton("Apply node properties")
        apply_node.clicked.connect(self._apply_node)
        properties_layout.addWidget(apply_node)
        split = QSplitter()
        split.addWidget(self.canvas)
        properties_scroll = QScrollArea()
        properties_scroll.setWidgetResizable(True)
        properties_scroll.setWidget(properties)
        split.addWidget(properties_scroll)
        split.setSizes([700, 350])
        layout.addWidget(split, 2)
        self.editor = CodeEditor()
        self.editor.setMinimumHeight(120)
        self.editor.setMaximumHeight(220)
        self.editor.set_language("yaml")
        layout.addWidget(self.editor, 1)
        self.editor_status = QLabel("Add nodes, drag output ports to input ports, and apply node properties. Generate YAML to save.")
        self.editor_status.setWordWrap(True)
        layout.addWidget(self.editor_status)
        self.tabs.addTab(scroll, "Builder / YAML")
        for signal in (self.name.textChanged, self.identifier.textChanged, self.trigger.currentTextChanged,
                       self.day.valueChanged, self.clock.timeChanged, self.cron.textChanged, self.workers.valueChanged,
                       self.target_list.model().rowsInserted, self.target_list.model().rowsRemoved):
            signal.connect(self._form_changed)

    def _build_monitor(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        buttons = QHBoxLayout()
        for text, callback in (("Cancel", self.cancel), ("Retry failed", self.retry), ("Refresh", self.refresh_jobs)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.jobs = QTableWidget(0, 5)
        self.jobs.setHorizontalHeaderLabels(("Job", "Automation", "Model", "Status", "Step"))
        self.jobs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.horizontalHeader().setStretchLastSection(True)
        self.jobs.itemSelectionChanged.connect(self._show_job)
        layout.addWidget(self.jobs, 2)
        self.monitor_canvas = WorkflowCanvas(editable=False)
        layout.addWidget(self.monitor_canvas, 2)
        self.job_outcome = QLabel("Select a model job to inspect its nodes and log.")
        self.job_outcome.setWordWrap(True)
        layout.addWidget(self.job_outcome)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        # ponytail: show the latest 10,000 log lines; complete history stays in run.log.
        self.log.setMaximumBlockCount(10_000)
        layout.addWidget(self.log, 1)
        self.tabs.addTab(panel, "Run monitor")

    def _build_catalog(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        note = QLabel("Custom tools: register scripts in Project Workspace → Tools → Custom tools. "
                      "Automation calls them with JSON stdin; stdout must be JSON with status ok, warning or error.")
        note.setWordWrap(True)
        layout.addWidget(note)
        install = QPushButton("Install custom tool package (.zip)")
        install.clicked.connect(self.install_tool)
        layout.addWidget(install)
        registry_controls = QHBoxLayout()
        register = QPushButton("Register / update tool JSON")
        register.clicked.connect(self._register_tool)
        remove = QPushButton("Remove tool registration")
        remove.clicked.connect(self._remove_tool_registration)
        registry_controls.addWidget(register)
        registry_controls.addWidget(remove)
        layout.addLayout(registry_controls)
        self.tools = QTableWidget(0, 3)
        self.tools.setHorizontalHeaderLabels(("Tool ID", "Type", "Description"))
        self.tools.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.tools)
        self.tabs.addTab(panel, "Tool registry")

    def install_tool(self):
        if not self.projects:
            QMessageBox.warning(self, "Project required", "Register a project before installing tools.")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Install tool", str(self.workspace), "Tool packages (*.zip)")
        if not filename:
            return
        project = next(iter(self.projects.values()))
        try:
            CustomToolStore(project["root"], self.workspace / ".hydrohub" / "custom_tools").install(Path(filename))
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Tool install failed", str(exc))
            return
        self.refresh_context()

    def refresh_context(self):
        self.projects = discover_projects(self.workspace)
        self.catalog = tool_catalog(self.workspace, self.projects)
        self.target.clear()
        for project_id, project in self.projects.items():
            for model_id, model in project["models"].items():
                self.target.addItem(f"{project['name']} / {model.name}", {"project_id": project_id, "model_id": model_id})
        self.tool.clear()
        self.tool.addItems(sorted(self.catalog))
        selected_tool = self.node_tool.currentText()
        self.node_tool.clear()
        self.node_tool.addItems(sorted(self.catalog))
        if selected_tool in self.catalog:
            self.node_tool.setCurrentText(selected_tool)
        self.tools.setRowCount(0)
        for tool_id, info in sorted(self.catalog.items()):
            row = self.tools.rowCount()
            self.tools.insertRow(row)
            for column, value in enumerate((tool_id, info["type"], info.get("description", ""))):
                self.tools.setItem(row, column, QTableWidgetItem(value))
        self.automations.setRowCount(0)
        for spec in self.store.list():
            state = self.store.state(spec["id"])
            last = datetime.fromisoformat(state["last_trigger"]) if state.get("last_trigger") else None
            due = datetime.fromisoformat(state["next_due"]) if state.get("next_due") else next_trigger(spec["trigger"], last, datetime.now().astimezone())
            row = self.automations.rowCount()
            self.automations.insertRow(row)
            for column, value in enumerate((spec["name"], spec["id"], self._automation_status(spec["id"], state),
                                            due.astimezone().strftime("%Y-%m-%d %H:%M") if due else "Manual")):
                self.automations.setItem(row, column, QTableWidgetItem(value))
        self.refresh_jobs()

    def _selected_spec(self):
        row = self.automations.currentRow()
        if row < 0:
            return None
        selected = self.automations.item(row, 1).text()
        return next((spec for spec in self.store.list() if spec["id"] == selected), None)

    def new(self):
        self._populating = True
        self._builder_spec = {}
        self._selected_node = None
        self.canvas.set_nodes([])
        self._canvas_dirty = False
        self.trigger.setCurrentText("manual")
        self.name.clear()
        self.identifier.clear()
        self.target_list.clear()
        self.editor.clear()
        self._populating = False
        self._canvas_dirty = False
        self.tabs.setCurrentIndex(1)

    def edit(self, *_args):
        spec = self._selected_spec()
        if spec:
            self._populate_builder(spec)
            self.tabs.setCurrentIndex(1)

    def _populate_builder(self, spec):
        self._populating = True
        self._selected_node = None
        self._builder_spec = spec
        self.editor.setPlainText(yaml.safe_dump({"automation": spec}, sort_keys=False))
        self.name.setText(spec["name"])
        self.identifier.setText(spec["id"])
        trigger = spec["trigger"]
        self.trigger.setCurrentText(trigger.get("mode", "manual") if trigger["type"] == "schedule" else "manual")
        self.day.setValue(trigger.get("days", trigger.get("day_of_month", 4)))
        self.clock.setTime(QTime.fromString(trigger.get("time", "02:00"), "HH:mm"))
        self.cron.setText(trigger.get("expression", "0 2 * * *"))
        self.workers.setValue(spec.get("concurrency", {}).get("max_parallel_models", 1))
        self.target_list.clear()
        for target in spec["targets"]:
            self.target_list.addItem(f"{target['project_id']} / {target['model_id']}")
        self.canvas.set_nodes(pipeline_nodes(spec))
        self.tabs.setCurrentIndex(1)
        self._populating = False
        self._canvas_dirty = False

    def _add_target(self):
        target = self.target.currentData()
        if target:
            value = f"{target['project_id']} / {target['model_id']}"
            if not self.target_list.findItems(value, Qt.MatchFlag.MatchExactly):
                self.target_list.addItem(value)

    def _add_step(self):
        tool = self.tool.currentText()
        if tool:
            nodes = self.canvas.definitions()
            index = len(nodes) + 1
            while f"step_{index}" in self.canvas.nodes:
                index += 1
            node = {"id": f"step_{index}", "tool": tool, "params": {}, "next": [],
                    "config_mode": "local" if tool in {"builtin.hydromt_update", "builtin.wflow_run"} else "global"}
            nodes.append(node)
            self.canvas.set_nodes(nodes)
            self._canvas_dirty = True
            self._select_node(node["id"])

    def _move_step(self):
        for index, node in enumerate(self.canvas.nodes.values()):
            node.setPos(index * 320, 0)

    def _generate(self):
        trigger = self._trigger_spec()
        spec = {**self._builder_spec, "id": self.identifier.text().strip(), "name": self.name.text().strip(),
                "trigger": trigger, "concurrency": {"max_parallel_models": self.workers.value()},
                "targets": [dict(zip(("project_id", "model_id"), item.text().split(" / ", 1)))
                            for item in (self.target_list.item(i) for i in range(self.target_list.count()))],
                "nodes": self.canvas.definitions(),
                "on_failure": self._builder_spec.get("on_failure", {"strategy": "stop_model", "retry_count": 0})}
        if self.editor.document().isModified() and self.editor.toPlainText().strip():
            answer = QMessageBox.question(self, "Replace edited YAML?", "Replace edited YAML with canvas values? Load YAML into the canvas first to retain those edits.")
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.editor.setPlainText(yaml.safe_dump({"automation": spec}, sort_keys=False))
        self._builder_spec = spec
        self._canvas_dirty = False
        return True

    def save(self):
        if self._canvas_dirty and not self._generate():
            return
        try:
            spec = self.store.save(self.editor.toPlainText(), self.catalog, self.projects)
            self.store.update_state(spec["id"], next_due=None)
        except (OSError, ValueError) as exc:
            self.editor_status.setText(f"Cannot save: {exc}")
            return
        self._populate_builder(spec)
        self.editor.document().setModified(False)
        self.editor_status.setText(f"Saved {spec['id']}.")
        self.refresh_context()

    def _start(self, spec, targets=None, show_monitor=True):
        try:
            jobs = self.scheduler.start(spec, targets)
        except (RuntimeError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Automation unavailable", str(exc))
            return False
        if show_monitor:
            self.tabs.setCurrentIndex(2)
        self.refresh_jobs()
        return bool(jobs)

    def run_now(self):
        spec = self._selected_spec()
        if spec:
            self._start(spec)

    def toggle(self):
        spec = self._selected_spec()
        if spec:
            state = self.store.state(spec["id"])
            try:
                enabled = not state.get("enabled", False)
                if enabled:
                    validate_pipeline(yaml.safe_dump({"automation": spec}), self.catalog, self.projects)
                self.scheduler.set_enabled(spec, enabled)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "Cannot enable automation", str(exc))
            self.refresh_context()

    def _schedule(self):
        self.scheduler.tick()
        self.refresh_context()

    def refresh_jobs(self):
        selected = self._selected_job()
        self.jobs.setRowCount(0)
        for job in sorted(self.runner.jobs.values(), key=lambda item: item["events"][0]["at"], reverse=True):
            row = self.jobs.rowCount()
            self.jobs.insertRow(row)
            visible_status = "Idle" if job["status"] == "QUEUED" else "Running" if job["status"] in {"RUNNING", "STEP_FAILED", "RETRYING"} else "Finish"
            for column, value in enumerate((job["id"], job["automation_id"], job["target"]["model_id"], visible_status, job["step"])):
                self.jobs.setItem(row, column, QTableWidgetItem(str(value)))
            if job["id"] == selected:
                self.jobs.selectRow(row)
        if selected is None and self.jobs.rowCount():
            self.jobs.selectRow(0)
        self._show_job()
        for row in range(self.automations.rowCount()):
            identifier = self.automations.item(row, 1).text()
            state = self.store.state(identifier)
            self.automations.item(row, 2).setText(self._automation_status(identifier, state))
            if state.get("next_due"):
                due = datetime.fromisoformat(state["next_due"])
                self.automations.item(row, 3).setText(due.astimezone().strftime("%Y-%m-%d %H:%M"))

    def _selected_job(self):
        row = self.jobs.currentRow()
        return self.jobs.item(row, 0).text() if row >= 0 and self.jobs.item(row, 0) else None

    def _show_job(self):
        job_id = self._selected_job()
        if job_id:
            path = self.runner.root / job_id / "run.log"
            job = self.runner.jobs[job_id]
            if getattr(self, "_monitor_job", None) != job_id:
                self.monitor_canvas.set_nodes(job.get("nodes", []))
                self._monitor_job = job_id
                self._log_offset = 0
                self.log.clear()
            self.monitor_canvas.show_status(job.get("node_status", {}))
            self.job_outcome.setText(f"Outcome: {job['status']} · {job.get('error', '')}\nLog: {path}")
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    stream.seek(self._log_offset)
                    text = stream.read(64 * 1024)
                    self._log_offset = stream.tell()
                if text:
                    self.log.appendPlainText(text.rstrip("\n"))

    def cancel(self):
        job_id = self._selected_job()
        if job_id:
            self.runner.cancel(job_id)

    def retry(self):
        job_id = self._selected_job()
        if job_id:
            job = self.runner.jobs[job_id]
            spec = next((spec for spec in self.store.list() if spec["id"] == job["automation_id"]), None)
            if spec and job["status"] == "FAILED":
                self._start(spec, [job["target"]])

    def _trigger_spec(self):
        mode = self.trigger.currentText()
        if mode == "manual":
            return {"type": "manual"}
        if mode == "cron":
            return {"type": "schedule", "mode": "cron", "expression": self.cron.text().strip()}
        return {"type": "schedule", "mode": mode, "time": self.clock.time().toString("HH:mm"),
                "days" if mode == "interval" else "day_of_month": self.day.value()}

    def _trigger_changed(self, *_args):
        mode = self.trigger.currentText()
        self.trigger_form.setRowVisible(self.day, mode in {"monthly_on_date", "interval"})
        self.trigger_form.setRowVisible(self.clock, mode != "manual")
        self.trigger_form.setRowVisible(self.cron, mode == "cron")
        self.trigger_form.labelForField(self.day).setText("Interval days" if mode == "interval" else "Day of month")
        self.day.setMaximum(2147483647 if mode == "interval" else 31)
        self.clock.setEnabled(True)
        self.clock.setDisplayFormat("HH:mm")
        self._cron_changed()

    def _clock_changed(self, *_args):
        if self.trigger.currentText() == "cron":
            parts = self.cron.text().split()
            if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
                parts[:2] = [str(self.clock.time().minute()), str(self.clock.time().hour())]
                self.cron.setText(" ".join(parts))
        self._update_schedule_preview()

    def _cron_changed(self, *_args):
        if self.trigger.currentText() == "cron":
            parts = self.cron.text().split()
            single = len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit()
            self.clock.setEnabled(single)
            self.clock.setDisplayFormat("HH:mm" if single else "'Cron times'")
            if single:
                time = QTime(int(parts[1]), int(parts[0]))
                if time.isValid():
                    self.clock.blockSignals(True)
                    self.clock.setTime(time)
                    self.clock.blockSignals(False)
        self._update_schedule_preview()

    def _update_schedule_preview(self, *_args):
        try:
            now = datetime.now().astimezone()
            due = next_trigger(self._trigger_spec(), None, now)
            self.schedule_preview.setText(f"Next trigger: {due.strftime('%Y-%m-%d %H:%M %Z')} (local time)" if due else "Manual: use Run Now; no schedule.")
        except (ValueError, KeyError, OverflowError) as exc:
            self.schedule_preview.setText(f"Invalid schedule: {exc}")

    def _select_node(self, identifier):
        item = self.canvas.nodes.get(identifier)
        if not item:
            return
        self._selected_node = identifier
        node = item.definition
        self.node_id.setText(identifier)
        self.node_tool.setCurrentText(node["tool"])
        self.config_mode.setCurrentText(node.get("config_mode", "global"))
        self.node_timeout.setValue(node.get("timeout_seconds", 0))
        self.node_policy.setCurrentText(node.get("on_failure", {}).get("strategy", "inherit"))
        self.node_retries.setValue(node.get("on_failure", {}).get("retry_count", 0))
        self.node_next.setText(", ".join(node.get("next", [])))
        self.node_params.setPlainText(yaml.safe_dump(node.get("params", {}), sort_keys=False))
        self.node_branches.setPlainText(yaml.safe_dump(node.get("next_on_status", {}), sort_keys=False))

    def _canvas_changed(self):
        self._canvas_dirty = True
        if self._selected_node in self.canvas.nodes:
            node = self.canvas.nodes[self._selected_node].definition
            self.node_next.setText(", ".join(node.get("next", [])))
            self.node_branches.setPlainText(yaml.safe_dump(node.get("next_on_status", {}), sort_keys=False))

    def _form_changed(self, *_args):
        if not self._populating:
            self._canvas_dirty = True

    def _apply_node(self):
        from hydrohub.core.automation import IDENTIFIER
        if self._selected_node not in self.canvas.nodes:
            return
        try:
            identifier = self.node_id.text().strip()
            if not IDENTIFIER.fullmatch(identifier) or (identifier != self._selected_node and identifier in self.canvas.nodes):
                raise ValueError("Node ID must be a unique lowercase identifier.")
            params = yaml.safe_load(self.node_params.toPlainText()) or {}
            branches = yaml.safe_load(self.node_branches.toPlainText()) or {}
            if not isinstance(params, dict) or not isinstance(branches, dict):
                raise ValueError("Parameters and branches must be mappings.")
            if set(branches) - {"ok", "warning", "error"} or any(
                    not isinstance(links, list) or any(not isinstance(target, str) for target in links)
                    for links in branches.values()):
                raise ValueError("Branches must map ok/warning/error to lists of node IDs.")
            nodes = self.canvas.definitions()
            for node in nodes:
                if node["id"] == self._selected_node:
                    node.update(id=identifier, tool=self.node_tool.currentText(), config_mode=self.config_mode.currentText(),
                                params=params, next=[value.strip() for value in self.node_next.text().split(",") if value.strip()],
                                next_on_status=branches)
                    if self.node_timeout.value():
                        node["timeout_seconds"] = self.node_timeout.value()
                    else:
                        node.pop("timeout_seconds", None)
                    if self.node_policy.currentText() != "inherit":
                        node["on_failure"] = {"strategy": self.node_policy.currentText(), "retry_count": self.node_retries.value()}
                    else:
                        node.pop("on_failure", None)
                if identifier != self._selected_node:
                    for links in [node.get("next", []), *node.get("next_on_status", {}).values()]:
                        links[:] = [identifier if target == self._selected_node else target for target in links]
            self.canvas.set_nodes(nodes)
            self._canvas_dirty = True
            self._select_node(identifier)
            self.editor_status.setText(f"Applied {identifier}. Generate YAML to save canvas changes.")
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            self.editor_status.setText(f"Cannot apply node: {exc}")

    def _remove_node(self):
        if self._selected_node:
            self.canvas.remove_node(self._selected_node)
            self._selected_node = None

    def _register_tool(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Register automation tool", str(self.workspace), "Tool registration (*.json)")
        if not filename:
            return
        try:
            definition = validate_tool_registration(json.loads(Path(filename).read_text(encoding="utf-8")))
            existing = self.catalog.get(definition["tool_id"], {})
            if existing and "path" in existing:
                raise ValueError("This ID belongs to an installed tool package; choose a different tool_id.")
            folder = self.store.root / "tools"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (definition["tool_id"][7:] + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(definition, indent=2), encoding="utf-8")
            temporary.replace(path)
            self.refresh_context()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot register tool", str(exc))

    def _remove_tool_registration(self):
        row = self.tools.currentRow()
        if row < 0:
            return
        identifier = self.tools.item(row, 0).text()
        if not identifier.startswith("custom.") or not (self.store.root / "tools" / (identifier[7:] + ".json")).is_file():
            QMessageBox.warning(self, "Tool registration", "Built-in tools and installed packages cannot be removed here.")
            return
        if self.runner.active:
            QMessageBox.warning(self, "Automation running", "Wait until automation jobs finish before removing a tool registration.")
            return
        users = [spec["name"] for spec in self.store.list() if any(node["tool"] == identifier for node in spec["nodes"])]
        if users:
            QMessageBox.warning(self, "Tool in use", "Remove this tool from these automations first: " + ", ".join(users))
            return
        if QMessageBox.question(self, "Remove tool registration?", f"Remove {identifier}? Script files will be kept.") == QMessageBox.StandardButton.Yes:
            (self.store.root / "tools" / (identifier[7:] + ".json")).unlink(missing_ok=True)
            self.refresh_context()

    def _load_yaml(self):
        try:
            spec = validate_pipeline(self.editor.toPlainText(), self.catalog, self.projects)
            self._populate_builder(spec)
            self.editor_status.setText("YAML loaded into canvas; parameters and connections preserved.")
        except (ValueError, yaml.YAMLError) as exc:
            self.editor_status.setText(f"Cannot load YAML: {exc}")

    def _import_yaml(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Import automation", str(self.workspace), "YAML (*.yaml *.yml)")
        if filename:
            try:
                spec = validate_pipeline(Path(filename).read_text(encoding="utf-8"), self.catalog, self.projects)
                self._populate_builder(spec)
            except (OSError, ValueError) as exc:
                self.editor_status.setText(f"Cannot import: {exc}")

    def _export_yaml(self):
        try:
            spec = validate_pipeline(self.editor.toPlainText(), self.catalog, self.projects)
            filename, _ = QFileDialog.getSaveFileName(self, "Export automation", f"{spec['id']}.yaml", "YAML (*.yaml)")
            if filename:
                destination = Path(filename).with_suffix(".yaml")
                temporary = destination.with_suffix(".yaml.tmp")
                temporary.write_text(yaml.safe_dump({"automation": spec}, sort_keys=False), encoding="utf-8")
                temporary.replace(destination)
        except (OSError, ValueError) as exc:
            self.editor_status.setText(f"Cannot export: {exc}")

    def _automation_status(self, identifier, state):
        if identifier in self.runner.active:
            return "Running"
        if not state.get("enabled"):
            return "OFF"
        jobs = [job for job in self.runner.jobs.values() if job["automation_id"] == identifier]
        if jobs:
            latest = max(jobs, key=lambda job: job["events"][0]["at"])
            finished = datetime.fromisoformat(latest["events"][-1]["at"])
            if (datetime.now().astimezone() - finished).total_seconds() < 5:
                return "Finish"
        return "Idle"

    def delete_automation(self):
        spec = self._selected_spec()
        if not spec:
            return
        if spec["id"] in self.runner.active:
            QMessageBox.warning(self, "Automation running", "Stop all jobs or wait until they finish before deleting this automation.")
            return
        answer = QMessageBox.question(self, "Delete automation?",
                                      f"Delete {spec['name']} and its schedule? Run history and simulation results will be kept.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.store.delete(spec["id"], self.runner.active)
            self.refresh_context()
