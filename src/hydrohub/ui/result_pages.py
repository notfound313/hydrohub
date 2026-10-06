from __future__ import annotations

from pathlib import Path
from html import escape
import json
import tomllib
import gc

import numpy as np
import pandas as pd
from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import Qt, QTimer, QRunnable, QThreadPool, QObject, Signal
from PySide6.QtGui import QPainter
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSplitter,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from hydrohub.core.model_artifacts import (
    ModelArtifacts,
    discover_model_artifacts,
    discover_wflow_models,
)
from hydrohub.core.run_registry import RunChain, RunRegistry
from hydrohub.results.analysis import (
    FREQ_LABELS,
    NO_DATA_COLOR,
    SSFI_CLASSES,
    OutputReader,
    build_ssfi_table,
    classify_period,
    extract_station_id,
    rank_output_files,
    validation_metrics,
)
from hydrohub.results.spatial import (
    ForcingReader,
    StaticgeomReader,
    StaticmapsReader,
    geometry_geojson,
)
from hydrohub.results.export import (
    export_basin_ssfi_png,
    export_basin_shapefile,
    export_basin_ssfi_gpkg,
    export_ssfi_excel,
    export_discharge,
)
from hydrohub.ui.map_viewer import IndonesiaMapWidget
from hydrohub.ui.result_widgets import ExportTab, LayerPanel, OutputTab, SSFITab, ValidationTab
from hydrohub.ui.background import BackgroundJob
from hydrohub.ui.layout_helpers import compact_combo
from qfluentwidgets import FluentIcon, PushButton as QPushButton


def _heading(layout: QVBoxLayout, eyebrow: str, title: str, description: str) -> None:
    overline = QLabel(eyebrow)
    overline.setObjectName("eyebrow")
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    copy = QLabel(description)
    copy.setObjectName("muted")
    copy.setWordWrap(True)
    divider = QFrame()
    divider.setFrameShape(QFrame.Shape.HLine)
    divider.setObjectName("divider")
    layout.addWidget(overline)
    layout.addWidget(heading)
    layout.addWidget(copy)
    layout.addWidget(divider)


def _populate_models(combo: QComboBox, project, configured: str = "") -> None:
    current = configured or str(combo.currentData() or "")
    combo.blockSignals(True)
    combo.clear()
    if project:
        for model in discover_wflow_models(project.root):
            combo.addItem(str(model.relative_to(project.root)), str(model))
    if current:
        target = str(Path(current).resolve())
        for index in range(combo.count()):
            if combo.itemData(index) == target:
                combo.setCurrentIndex(index)
                break
    combo.blockSignals(False)


class TimeSeriesChart(QChartView):
    def __init__(self) -> None:
        super().__init__()
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMinimumHeight(280)
        self.show_message("Select an output variable.")

    def show_message(self, message: str) -> None:
        chart = QChart()
        chart.setTitle(message)
        chart.legend().hide()
        self.setChart(chart)

    def plot(self, series: list[tuple[str, pd.Series]], frequency_label: str) -> None:
        chart = QChart()
        chart.setTitle("Simulation time series")
        axis_time = QDateTimeAxis()
        axis_time.setFormat(
            "yyyy" if frequency_label == "Yearly" else "MM-yyyy" if frequency_label == "Monthly" else "dd-MM-yyyy"
        )
        axis_time.setTitleText("Time")
        axis_value = QValueAxis()
        axis_value.setTitleText("Value")
        chart.addAxis(axis_time, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_value, Qt.AlignmentFlag.AlignLeft)
        has_values = False
        for label, values in series:
            line = QLineSeries()
            line.setName(label)
            for timestamp, value in values.dropna().items():
                line.append(pd.Timestamp(timestamp).timestamp() * 1000, float(value))
                has_values = True
            chart.addSeries(line)
            line.attachAxis(axis_time)
            line.attachAxis(axis_value)
        if not has_values:
            self.show_message("No finite values are available for plotting.")
            return
        axis_time.setRange(
            pd.Timestamp(min(values.dropna().index.min() for _label, values in series if not values.dropna().empty)).to_pydatetime(),
            pd.Timestamp(max(values.dropna().index.max() for _label, values in series if not values.dropna().empty)).to_pydatetime(),
        )
        all_values = np.concatenate(
            [values.dropna().to_numpy(dtype=float) for _label, values in series if not values.dropna().empty]
        )
        minimum, maximum = float(np.min(all_values)), float(np.max(all_values))
        padding = max((maximum - minimum) * 0.05, abs(maximum) * 0.01, 1e-9)
        axis_value.setRange(minimum - padding, maximum + padding)
        self.setChart(chart)


class ForcingPage(QWidget):
    def __init__(self, project_provider, workflow) -> None:
        super().__init__()
        self.project_provider = project_provider
        self.workflow = workflow
        self._forcing_path: Path | None = None
        self._reader: ForcingReader | None = None
        self._forcing_times = []
        self._is_csv = False
        self._value_range = None
        self._loading = False
        self._populating_models = False
        self._forcing_request = 0
        self._preview_request = 0
        self._render_request = 0
        self._forcing_job = None
        self._preview_job = None
        self._render_job = None
        self._prefetch_job = None
        self._missing_job = None
        self._frame_cache = {}
        self._shown_variable = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 22)
        layout.setSpacing(12)
        _heading(
            layout,
            "SIMULATION / FORCING",
            "Forcing inspector and timelapse",
            "Inspect model forcing, browse timestep values, and animate spatial input on the map.",
        )

        first = QHBoxLayout()
        self.models = QComboBox()
        self.models.setMinimumWidth(160)
        self.models.currentIndexChanged.connect(self._model_changed)
        self.files = QComboBox()
        self.files.setMinimumWidth(200)
        self.files.currentIndexChanged.connect(self._file_changed)
        self.load_button = QPushButton("Load forcing file")
        self.load_button.clicked.connect(self._browse)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_context)
        first.addWidget(QLabel("Model"))
        first.addWidget(self.models)
        first.addWidget(QLabel("Forcing"))
        first.addWidget(self.files, 1)
        first.addWidget(self.load_button)
        first.addWidget(refresh)
        layout.addLayout(first)

        second = QHBoxLayout()
        self.variables = QComboBox()
        self.variables.currentTextChanged.connect(self._variable_changed)
        self.colormap = QComboBox()
        self.colormap.addItems(("Blues", "viridis", "YlGnBu", "terrain", "coolwarm"))
        self.resolution = QSpinBox()
        self.resolution.setRange(256, 4096)
        self.resolution.setSingleStep(256)
        self.resolution.setValue(1024)
        second.addWidget(QLabel("Variable"))
        second.addWidget(self.variables, 1)
        second.addWidget(QLabel("Color map"))
        second.addWidget(self.colormap)
        second.addWidget(QLabel("Max pixels"))
        second.addWidget(self.resolution)

        timeline = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._time_changed)
        self.time_label = QLabel("No forcing loaded")
        self.time_label.setMinimumWidth(150)
        self.show_button = QPushButton("Show frame")
        self.show_button.clicked.connect(self._render)
        self.play = QPushButton("Play")
        self.play.clicked.connect(self._toggle_playback)
        self.speed = QSpinBox()
        self.speed.setRange(50, 5000)
        self.speed.setValue(300)
        self.speed.valueChanged.connect(lambda value: self.timer.setInterval(value))
        self.step = QSpinBox()
        self.step.setRange(1, 500)
        self.loop = QCheckBox("Loop")
        self.loop.setChecked(True)
        timeline.addWidget(QLabel("Timestep"))
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.time_label)
        timeline.addWidget(self.show_button)
        timeline.addWidget(self.play)
        timeline.addWidget(QLabel("ms"))
        timeline.addWidget(self.speed)
        timeline.addWidget(QLabel("Skip"))
        timeline.addWidget(self.step)
        timeline.addWidget(self.loop)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.forcing_splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        self.map = IndonesiaMapWidget()
        self.map.setMinimumHeight(240)
        forcing_panel = QWidget()
        forcing_panel.setMinimumHeight(140)
        forcing_layout = QVBoxLayout(forcing_panel)
        forcing_layout.setContentsMargins(0, 0, 0, 0)
        forcing_layout.addLayout(second)
        forcing_layout.addLayout(timeline)
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(140)
        forcing_layout.addWidget(self.table)
        splitter.addWidget(self.map)
        splitter.addWidget(forcing_panel)
        splitter.setSizes((520, 320))
        splitter.splitterMoved.connect(lambda *_: self.map.invalidate_size())

        missing_panel = QWidget()
        missing_layout = QVBoxLayout(missing_panel)
        missing_controls = QHBoxLayout()
        self.missing_variable = QComboBox()
        self.expected_interval = QComboBox()
        for label, value in (("Automatic", "auto"), ("Daily", "1D"), ("Hourly", "1h")):
            self.expected_interval.addItem(label, value)
        self.expected_interval.setAccessibleName("Expected timestep interval")
        self.check_missing_button = QPushButton("Check missing values")
        self.check_missing_button.setEnabled(False)
        self.check_missing_button.clicked.connect(self._check_missing_values)
        missing_controls.addWidget(QLabel("Variable"))
        missing_controls.addWidget(self.missing_variable, 1)
        missing_controls.addWidget(QLabel("Interval"))
        missing_controls.addWidget(self.expected_interval)
        missing_controls.addWidget(self.check_missing_button)
        missing_layout.addLayout(missing_controls)
        self.missing_status = QLabel("Load forcing, then check missing timesteps and values across the entire grid.")
        self.missing_status.setWordWrap(True)
        self.missing_status.setObjectName("muted")
        missing_layout.addWidget(self.missing_status)
        self.missing_table = QTableWidget()
        self.missing_table.setAlternatingRowColors(True)
        self.missing_table.setColumnCount(4)
        self.missing_table.setHorizontalHeaderLabels(
            ("Time", "Missing cells", "Total cells", "Coverage (%)")
        )
        self.missing_table.horizontalHeader().setStretchLastSection(True)
        missing_layout.addWidget(self.missing_table, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(splitter, "Inspector")
        self.tabs.addTab(missing_panel, "Missing values")
        info_panel = QWidget()
        info_layout = QVBoxLayout(info_panel)
        self.time_info = QLabel("Load forcing to inspect variables and time.")
        self.time_info.setWordWrap(True)
        info_layout.addWidget(self.time_info)
        self.variable_info = QTableWidget()
        self.variable_info.setColumnCount(6)
        self.variable_info.setHorizontalHeaderLabels(("Variable", "Units", "Dimensions", "Shape", "Type", "Description"))
        self.variable_info.horizontalHeader().setStretchLastSection(True)
        info_layout.addWidget(self.variable_info)
        self.gap_table = QTableWidget()
        self.gap_table.setColumnCount(3)
        self.gap_table.setHorizontalHeaderLabels(("First missing timestep", "Last missing timestep", "Missing count"))
        info_layout.addWidget(self.gap_table)
        self.tabs.addTab(info_panel, "Variables and time")
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("Open a project and select a model.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Forcing data loading progress")
        self.progress.hide()
        layout.addWidget(self.progress)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)

    def project(self):
        return self.project_provider()

    def refresh_context(self) -> None:
        project = self.project()
        self._populating_models = True
        state = self.workflow.load_ui_state(project) if project else {}
        _populate_models(self.models, project, state.get("forcing_model_root", ""))
        self._populating_models = False
        self._model_changed()
        if project:
            aoi = project.root / "workspace" / "aoi.geojson"
            if aoi.exists():
                self.map.set_aoi(json.loads(aoi.read_text(encoding="utf-8")))

    def _selected_model(self) -> Path | None:
        value = self.models.currentData()
        return Path(value) if value else None

    def _model_changed(self, *_args) -> None:
        if self._populating_models:
            return
        project, model = self.project(), self._selected_model()
        self.files.blockSignals(True)
        self.files.clear()
        error = None
        if project and model:
            try:
                artifacts = discover_model_artifacts(project.root, model)
                if artifacts.forcing:
                    self.files.addItem(str(artifacts.forcing.relative_to(project.root)), str(artifacts.forcing))
                self.workflow.save_ui_state(project, forcing_model_root=str(model.resolve()))
            except (OSError, ValueError) as exc:
                error = f"Forcing model could not be loaded: {exc}"
        self.files.blockSignals(False)
        self._file_changed()
        if error:
            self._set_error(error)

    def _browse(self) -> None:
        project = self.project()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Load forcing", str(project.root if project else Path.cwd()), "Forcing (*.nc *.csv)"
        )
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        path = Path(path).resolve()
        self.files.blockSignals(True)
        index = self.files.findData(str(path))
        if index < 0:
            self.files.addItem(str(path), str(path))
            index = self.files.count() - 1
        self.files.setCurrentIndex(index)
        self.files.blockSignals(False)
        self._file_changed()

    def _file_changed(self, *_args) -> None:
        value = self.files.currentData()
        if self._shown_variable:
            self.map.remove_data_layer(f"forcing:{self._shown_variable}")
            self._shown_variable = None
        self.close_resources()
        self.variables.clear()
        self._clear_preview()
        if not value:
            self._set_status("No forcing file was discovered for this model.")
            return
        path = Path(value)
        self._forcing_request += 1
        request = self._forcing_request
        self._set_loading(True, f"Loading forcing file: {path.name}…")

        def load():
            reader = ForcingReader(path)
            try:
                variables = reader.list_variables()
                if not variables:
                    raise ValueError("No spatial or time-series forcing variables found.")
                times = reader.time_values()
                if not len(times):
                    raise ValueError("Forcing file contains no timesteps.")
                reader._loaded_metadata = (variables, reader.is_csv, times)
                reader._diagnostics_metadata = (reader.metadata(), reader.time_diagnostics())
                return reader
            except Exception:
                reader.close()
                raise

        def ready(result):
            if request != self._forcing_request:
                return
            self._forcing_job = None
            self._reader = result
            variables, self._is_csv, self._forcing_times = result._loaded_metadata
            self._forcing_path = path
            self.variables.blockSignals(True)
            self.variables.addItems(variables)
            self.variables.blockSignals(False)
            self.missing_variable.clear()
            metadata, time_info = result._diagnostics_metadata
            self.missing_variable.addItems([
                name for name, info in metadata.items()
                if self._is_csv or result.time_dimension in info["dims"]
            ])
            self.variable_info.setRowCount(len(metadata))
            for row, (name, info) in enumerate(metadata.items()):
                for column, value in enumerate((name, info["units"], ", ".join(info["dims"]),
                                                 str(info["shape"]), info["dtype"], info["description"])):
                    self.variable_info.setItem(row, column, QTableWidgetItem(str(value)))
            self._show_time_diagnostics(time_info)
            self.check_missing_button.setEnabled(True)
            self._set_loading(False)
            self._set_status(f"Forcing loaded: {path}")
            self.variables.setEnabled(True)
            self.variables.setCurrentIndex(0)
            self._variable_changed(self.variables.currentText())

        def failed(message):
            if request == self._forcing_request:
                self._forcing_job = None
                self._set_loading(False)
                self._set_error(f"Forcing could not be loaded: {message}")

        self._forcing_job = BackgroundJob(self, load, ready, failed)
        self._forcing_job.start()

    def _variable_changed(self, variable: str) -> None:
        if not self._forcing_path or not variable:
            return
        self._stop_playback()
        self._frame_cache.clear()
        if self._shown_variable:
            self.map.remove_data_layer(f"forcing:{self._shown_variable}")
            self._shown_variable = None
        self._render_request += 1
        if self._render_job:
            self._render_job.cancel()
            self._render_job = None
        self._preview_request += 1
        request = self._preview_request
        previous = self._preview_job
        if previous:
            previous.cancel()
        reader = self._reader
        path = self._forcing_path
        self._set_loading(True, f"Loading preview: {variable}…")
        self._value_range = None
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        def read():
            return reader.as_table(variable, limit=5000 if reader.is_csv else 24), reader.frame_count()
        def ready(value):
            if request != self._preview_request or not self._forcing_path:
                return
            self._preview_job = None
            frame, frame_count = value
            self._fill_table(frame)
            self.slider.blockSignals(True)
            self.slider.setRange(0, max(0, frame_count - 1))
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self._time_changed(self.slider.value())
            self._set_loading(False)
            self._set_status(f"Forcing loaded: {path.name} · {variable} · preview {len(frame)} of {frame_count} timesteps")
            if not self._is_csv:
                self._render()
        def failed(message):
            if request != self._preview_request:
                return
            self._preview_job = None
            self._set_loading(False)
            self._set_error(f"Forcing preview failed: {message}")
        self._preview_job = BackgroundJob(self, read, ready, failed)
        self._preview_job.start()

    def _fill_table(self, frame: pd.DataFrame) -> None:
        preview = frame.iloc[:5000]
        self.table.setRowCount(len(preview))
        self.table.setColumnCount(len(preview.columns))
        self.table.setHorizontalHeaderLabels([str(column) for column in preview.columns])
        for row in range(len(preview)):
            for column in range(len(preview.columns)):
                self.table.setItem(row, column, QTableWidgetItem(str(preview.iloc[row, column])))

    def _time_changed(self, index: int) -> None:
        if len(self._forcing_times):
            self.time_label.setText(str(pd.Timestamp(self._forcing_times[index])))

    def _check_missing_values(self) -> None:
        variable = self.missing_variable.currentText()
        if not self._reader or not variable:
            self.missing_status.setText("Load forcing before checking missing values.")
            return
        if self._missing_job:
            self._missing_job.cancel()
        reader = self._reader
        interval = self.expected_interval.currentData()
        forcing_request = self._forcing_request
        self.check_missing_button.setEnabled(False)
        self.missing_status.setText(f"Checking {variable} across all timesteps...")

        def ready(value):
            if forcing_request != self._forcing_request:
                return
            self._missing_job = None
            self.check_missing_button.setEnabled(True)
            (frame, cell_count), time_info = value
            self._show_time_diagnostics(time_info)
            self.missing_table.setRowCount(len(frame))
            self.missing_table.setColumnCount(4)
            for row, values in enumerate(frame.itertuples(index=False, name=None)):
                for column, item in enumerate(values):
                    text = f"{item:.3f}" if column == 3 else str(item)
                    self.missing_table.setItem(row, column, QTableWidgetItem(text))
            self.missing_status.setText(
                f"No missing values across {cell_count:,} cells; {time_info['missing']:,} missing timesteps."
                if frame.empty
                else f"{len(frame):,} timestep(s) contain missing {variable} values across the entire grid; "
                     f"{int((frame['coverage_pct'] == 0).sum()):,} entirely empty; "
                     f"{time_info['missing']:,} missing timesteps."
            )

        def failed(message):
            if forcing_request != self._forcing_request:
                return
            self._missing_job = None
            self.check_missing_button.setEnabled(True)
            self.missing_status.setText(f"Missing-value check failed: {message}")

        def check():
            diagnostic_reader = ForcingReader(reader.path)
            try:
                return diagnostic_reader.missing_values(variable), diagnostic_reader.time_diagnostics(interval)
            finally:
                diagnostic_reader.close()

        self._missing_job = BackgroundJob(
            self,
            check,
            ready,
            failed,
        )
        self._missing_job.start()

    def _show_time_diagnostics(self, info: dict) -> None:
        self.time_info.setText(
            f"Time: {info['first']} — {info['last']} · {info['count']:,} timesteps\n"
            f"Calendar: {info['calendar']} · Interval: {info['interval']}\n"
            f"Missing: {info['missing']:,} · Duplicates: {info['duplicates']:,} · "
            f"Invalid: {info['invalid']:,} · Ordered: {info['ordered']}\n"
            "Automatic interval uses the most common spacing; select Daily or Hourly to check expected gaps."
        )
        self.gap_table.setRowCount(len(info["gaps"]))
        for row, values in enumerate(info["gaps"]):
            for column, value in enumerate(values):
                self.gap_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _render(self) -> None:
        variable = self.variables.currentText()
        if not self._forcing_path or self._is_csv or not variable:
            return
        previous = self._render_job
        if previous:
            previous.cancel()
        self._render_request += 1
        request = self._render_request
        forcing_request = self._forcing_request
        reader = self._reader
        dimension = min(self.resolution.value(), max(256, int(self.map.width() * self.map.devicePixelRatioF())))
        args = (variable, self.slider.value(), dimension, self.colormap.currentText(), self._value_range)
        key = args
        def render():
            return reader.render_frame(*args)
        def ready(value):
            if request != self._render_request or forcing_request != self._forcing_request:
                return
            self._frame_cache[key] = value
            if len(self._frame_cache) > 3:
                self._frame_cache.pop(next(iter(self._frame_cache)))
            image, bounds, _range = value
            self.map.set_raster_layer(f"forcing:{variable}", image, bounds)
            if self._shown_variable != variable:
                self.map.fit_bounds(bounds)
            self._shown_variable = variable
            self._render_job = None
            if not self.timer.isActive():
                self._set_loading(False)
                self._set_status(f"Frame shown: {variable} · {self.time_label.text()}")
            else:
                self._prefetch_next(reader, args)
        def failed(message):
            if request != self._render_request or forcing_request != self._forcing_request:
                return
            self._render_job = None
            self._stop_playback()
            self._set_loading(False)
            self._set_error(f"Forcing frame unavailable: {message}")
        if key in self._frame_cache:
            ready(self._frame_cache[key])
            return
        if not self.timer.isActive():
            self._set_loading(True, f"Rendering forcing frame: {variable}…")
        self._render_job = BackgroundJob(self, render, ready, failed)
        self._render_job.start()

    def _prefetch_next(self, reader: ForcingReader, args: tuple) -> None:
        if self._prefetch_job:
            return
        variable, index, dimension, colormap, value_range = args
        next_index = index + self.step.value()
        if next_index > self.slider.maximum():
            if not self.loop.isChecked():
                return
            next_index = 0
        key = (variable, next_index, dimension, colormap, value_range)
        if key in self._frame_cache:
            return
        def ready(value):
            self._prefetch_job = None
            if reader is self._reader and self.timer.isActive():
                self._frame_cache[key] = value
                if len(self._frame_cache) > 3:
                    self._frame_cache.pop(next(iter(self._frame_cache)))
        def failed(_message):
            self._prefetch_job = None
        self._prefetch_job = BackgroundJob(
            self, lambda: reader.render_frame(*key), ready, failed
        )
        self._prefetch_job.start()

    def _toggle_playback(self) -> None:
        if self.timer.isActive():
            self._stop_playback()
        elif self._forcing_path and not self._is_csv and self.slider.maximum() > 0:
            if self._value_range is not None:
                self._start_playback()
                return
            self._set_loading(True, "Preparing fixed color scale for playback…")
            request = self._preview_request
            path, variable, reader = self._forcing_path, self.variables.currentText(), self._reader
            def calculate():
                return reader.value_range(variable)
            def ready(value):
                if request != self._preview_request or path != self._forcing_path:
                    return
                self._preview_job = None
                self._value_range = value
                self._set_loading(False)
                self._start_playback()
            def failed(message):
                if request == self._preview_request:
                    self._preview_job = None
                    self._set_loading(False)
                    self._set_error(f"Forcing color scale unavailable: {message}")
            self._preview_job = BackgroundJob(self, calculate, ready, failed)
            self._preview_job.start()

    def _start_playback(self) -> None:
        self.timer.start(self.speed.value())
        self.play.setText("Pause")
        self.variables.setEnabled(False)
        self._set_status("Playing timelapse…")
        self._render()

    def _advance(self) -> None:
        if self._render_job is not None or self._prefetch_job is not None:
            return
        value = self.slider.value() + self.step.value()
        if value > self.slider.maximum():
            if not self.loop.isChecked():
                self._stop_playback()
                return
            value = 0
        self.slider.setValue(value)
        self._render()

    def _stop_playback(self) -> None:
        if self.timer.isActive():
            self._set_status(f"Frame shown: {self.variables.currentText()} · {self.time_label.text()}")
        self.timer.stop()
        if self._prefetch_job:
            self._prefetch_job.cancel()
            self._prefetch_job = None
        self.play.setText("Play")
        self.variables.setEnabled(not self._loading and self.variables.count() > 0)

    def close_resources(self) -> None:
        self._stop_playback()
        self._forcing_request += 1
        self._preview_request += 1
        self._render_request += 1
        for name in ("_forcing_job", "_preview_job", "_render_job", "_prefetch_job", "_missing_job"):
            job = getattr(self, name, None)
            if job:
                job.cancel()
                setattr(self, name, None)
        if self._reader:
            reader, self._reader = self._reader, None
            BackgroundJob(None, reader.close, lambda _: None, lambda _: None).start()
        self._forcing_path = None
        self._frame_cache.clear()
        self._forcing_times = []
        self.missing_variable.clear()
        self.missing_table.setRowCount(0)
        self.check_missing_button.setEnabled(False)
        self.missing_status.setText("Load forcing, then check missing timesteps and values across the entire grid.")
        self.variable_info.setRowCount(0)
        self.gap_table.setRowCount(0)
        self.time_info.setText("Load forcing to inspect variables and time.")
        self._set_loading(False)

    def _clear_preview(self) -> None:
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.show_button.setEnabled(False)
        self.play.setEnabled(False)
        self.time_label.setText("No forcing loaded")
        self._value_range = None

    def _set_loading(self, loading: bool, message: str = "") -> None:
        self._loading = loading
        self.progress.setVisible(loading)
        self.variables.setEnabled(not loading and not self.timer.isActive() and self.variables.count() > 0)
        spatial = self._forcing_path is not None and not self._is_csv and self.variables.count() > 0
        self.slider.setEnabled(not loading and spatial)
        self.show_button.setEnabled(not loading and spatial)
        self.play.setEnabled(spatial and self.slider.maximum() > 0 and (not loading or self.timer.isActive()))
        self.check_missing_button.setEnabled(not loading and self._reader is not None and self._missing_job is None)
        self.colormap.setEnabled(not loading and self._forcing_path is not None)
        self.resolution.setEnabled(not loading and self._forcing_path is not None)
        if message:
            self.status.setObjectName("statusRunning")
            self.status.setText(message)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)

    def _set_status(self, message: str) -> None:
        self.status.setObjectName("muted")
        self.status.setText(message)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _set_error(self, message: str) -> None:
        self.status.setObjectName("statusError")
        self.status.setText(message)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)


class LegacyResultsPage(QWidget):
    def __init__(self, project_provider, workflow) -> None:
        super().__init__()
        self.project_provider = project_provider
        self.workflow = workflow
        self.artifacts: ModelArtifacts | None = None
        self.output_reader: OutputReader | None = None
        self.staticmaps_reader: StaticmapsReader | None = None
        self.current_series: pd.Series | None = None
        self.observation: pd.Series | None = None
        self.observation_raw: pd.Series | None = None
        self.ssfi_frame = pd.DataFrame()
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 22)
        layout.setSpacing(12)
        _heading(
            layout,
            "RESULTS",
            "Model results dashboard",
            "Choose a Wflow model and inspect spatial layers, output series, validation, and ZIP-compatible SSFI in one workspace.",
        )

        toolbar = QHBoxLayout()
        self.models = QComboBox()
        self.models.setMinimumWidth(220)
        compact_combo(self.models, 280, 20)
        self.models.currentIndexChanged.connect(self._model_changed)
        self.outputs = QComboBox()
        self.outputs.setMinimumWidth(260)
        self.outputs.currentIndexChanged.connect(self._output_changed)
        self.variables = QComboBox()
        self.variables.currentIndexChanged.connect(self._variable_changed)
        self.frequency = QComboBox()
        self.frequency.addItems(FREQ_LABELS)
        self.frequency.currentTextChanged.connect(self._frequency_changed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_context)
        load_output = QPushButton("Load output")
        load_output.clicked.connect(self._browse_output)
        toolbar.addWidget(QLabel("Model"))
        toolbar.addWidget(self.models)
        toolbar.addWidget(QLabel("Output"))
        toolbar.addWidget(self.outputs, 1)
        toolbar.addWidget(load_output)
        toolbar.addWidget(QLabel("Variable"))
        toolbar.addWidget(self.variables)
        toolbar.addWidget(QLabel("Aggregate"))
        toolbar.addWidget(self.frequency)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        layer_panel = QFrame()
        layer_layout = QVBoxLayout(layer_panel)
        layer_layout.setContentsMargins(0, 0, 0, 0)
        layer_layout.addWidget(QLabel("MODEL LAYERS"))
        self.layers = QTreeWidget()
        self.layers.setHeaderLabels(("Layer",))
        self.layers.itemChanged.connect(self._layer_changed)
        layer_layout.addWidget(self.layers, 1)
        controls = QHBoxLayout()
        self.colormap = QComboBox()
        self.colormap.addItems(("viridis", "Blues", "terrain", "coolwarm"))
        self.resolution = QSpinBox()
        self.resolution.setRange(256, 4096)
        self.resolution.setValue(1024)
        controls.addWidget(self.colormap)
        controls.addWidget(self.resolution)
        layer_layout.addLayout(controls)
        clear = QPushButton("Clear map layers")
        clear.clicked.connect(self.map_clear)
        layer_layout.addWidget(clear)
        main_splitter.addWidget(layer_panel)
        self.map = IndonesiaMapWidget()
        main_splitter.addWidget(self.map)
        self.chart = TimeSeriesChart()
        main_splitter.addWidget(self.chart)
        main_splitter.setSizes((220, 620, 500))
        layout.addWidget(main_splitter, 3)

        details = QScrollArea()
        details.setWidgetResizable(True)
        details.setFrameShape(QFrame.Shape.NoFrame)
        details_content = QWidget()
        details_layout = QVBoxLayout(details_content)
        details_layout.setContentsMargins(0, 0, 0, 0)

        self.table_group = self._group("Data preview", True)
        table_controls = QHBoxLayout()
        table_controls.addWidget(QLabel("View"))
        self.table_mode = QComboBox()
        self.table_mode.addItems(("Selected series", "Complete CSV preview"))
        self.table_mode.currentTextChanged.connect(self._refresh_table)
        table_controls.addWidget(self.table_mode)
        table_controls.addStretch()
        self.table_group.layout().addLayout(table_controls)
        self.table = QTableWidget()
        self.table.setMaximumHeight(250)
        self.table_group.layout().addWidget(self.table)
        details_layout.addWidget(self.table_group)

        self.validation_group = self._group("Validation", False)
        validation_controls = QHBoxLayout()
        observation = QPushButton("Load observation CSV")
        observation.clicked.connect(self._load_observation)
        self.validation_status = QLabel("Load observations to compare with the selected simulation series.")
        self.validation_status.setWordWrap(True)
        validation_controls.addWidget(observation)
        validation_controls.addWidget(self.validation_status, 1)
        self.validation_group.layout().addLayout(validation_controls)
        details_layout.addWidget(self.validation_group)

        self.ssfi_group = self._group("SSFI (ZIP-compatible method)", False)
        ssfi_controls = QHBoxLayout()
        compute = QPushButton("Calculate SSFI")
        compute.clicked.connect(self._calculate_ssfi)
        self.ssfi_period = QComboBox()
        self.ssfi_period.currentIndexChanged.connect(self._fill_ssfi_table)
        self.ssfi_geometry = QComboBox()
        show_ssfi = QPushButton("Show on map")
        show_ssfi.clicked.connect(self._show_ssfi)
        ssfi_controls.addWidget(compute)
        ssfi_controls.addWidget(QLabel("Period"))
        ssfi_controls.addWidget(self.ssfi_period)
        ssfi_controls.addWidget(QLabel("Basin layer"))
        ssfi_controls.addWidget(self.ssfi_geometry, 1)
        ssfi_controls.addWidget(show_ssfi)
        self.ssfi_group.layout().addLayout(ssfi_controls)
        legend = QLabel(" | ".join(f"{label}: {color}" for label, color in SSFI_CLASSES) + f" | No Data: {NO_DATA_COLOR}")
        legend.setWordWrap(True)
        legend.setObjectName("muted")
        self.ssfi_group.layout().addWidget(legend)
        self.ssfi_table = QTableWidget()
        self.ssfi_table.setMaximumHeight(220)
        self.ssfi_group.layout().addWidget(self.ssfi_table)
        details_layout.addWidget(self.ssfi_group)
        details_layout.addStretch()
        details.setWidget(details_content)
        layout.addWidget(details, 2)
        self.status = QLabel("Open a project to discover result models.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

    @staticmethod
    def _group(title: str, expanded: bool) -> QGroupBox:
        group = QGroupBox(title)
        group.setCheckable(True)
        group.setChecked(expanded)
        QVBoxLayout(group)
        group.setMaximumHeight(16777215 if expanded else 32)
        group.toggled.connect(
            lambda checked, panel=group: panel.setMaximumHeight(16777215 if checked else 32)
        )
        return group

    def project(self):
        return self.project_provider()

    def refresh_context(self) -> None:
        project = self.project()
        self._loading = True
        state = self.workflow.load_ui_state(project) if project else {}
        _populate_models(self.models, project, state.get("results_model_root", ""))
        self._loading = False
        self._model_changed()

    def _selected_model(self) -> Path | None:
        value = self.models.currentData()
        return Path(value) if value else None

    def _model_changed(self, *_args) -> None:
        if self._loading:
            return
        self.close_resources()
        self.outputs.blockSignals(True)
        self.outputs.clear()
        self.layers.clear()
        self.ssfi_geometry.clear()
        project, model = self.project(), self._selected_model()
        if not project or not model:
            self.outputs.blockSignals(False)
            self.status.setText("No Wflow model containing wflow_sbm.toml was found.")
            return
        try:
            self.artifacts = discover_model_artifacts(project.root, model)
            ranked = rank_output_files(self.artifacts.outputs)
            for path in ranked:
                self.outputs.addItem(str(path.relative_to(project.root)), str(path))
            self._populate_layers()
            self.workflow.save_ui_state(project, results_model_root=str(model.resolve()))
            self.status.setText(
                f"{model.relative_to(project.root)} | {len(ranked)} readable output(s) | "
                f"{len(self.artifacts.staticgeoms)} geometry layer(s)"
            )
        except Exception as exc:
            self.status.setText(f"Model results could not be discovered: {exc}")
        self.outputs.blockSignals(False)
        self._output_changed()

    def _populate_layers(self) -> None:
        if not self.artifacts:
            return
        if self.artifacts.staticmaps:
            try:
                self.staticmaps_reader = StaticmapsReader(self.artifacts.staticmaps)
                root = QTreeWidgetItem(["Staticmaps"])
                self.layers.addTopLevelItem(root)
                for variable in self.staticmaps_reader.list_variables():
                    item = QTreeWidgetItem(root, [variable])
                    item.setData(0, Qt.ItemDataRole.UserRole, ("raster", variable))
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                root.setExpanded(True)
            except Exception as exc:
                self.status.setText(f"Staticmaps unavailable: {exc}")
        if self.artifacts.staticgeoms:
            root = QTreeWidgetItem(["Static geometry"])
            self.layers.addTopLevelItem(root)
            for path in self.artifacts.staticgeoms:
                item = QTreeWidgetItem(root, [path.stem])
                item.setData(0, Qt.ItemDataRole.UserRole, ("geometry", str(path)))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self.ssfi_geometry.addItem(path.stem, str(path))
            root.setExpanded(True)

    def _browse_output(self) -> None:
        project = self.project()
        path, _filter = QFileDialog.getOpenFileName(
            self, "Load model output", str(project.root if project else Path.cwd()), "Output (*.nc *.csv)"
        )
        if path:
            self.outputs.addItem(path, path)
            self.outputs.setCurrentIndex(self.outputs.count() - 1)

    def _output_changed(self, *_args) -> None:
        if self.output_reader:
            self.output_reader.close()
            self.output_reader = None
        self.variables.clear()
        value = self.outputs.currentData()
        if not value:
            self.chart.show_message("No readable output was discovered for this model.")
            return
        try:
            self.output_reader = OutputReader(Path(value))
            for variable in self.output_reader.list_variables():
                station = variable
                self.variables.addItem(station, variable)
        except Exception as exc:
            self.status.setText(f"Output could not be opened: {exc}")

    def _variable_changed(self, *_args) -> None:
        if not self.output_reader or not self.variables.currentData():
            return
        try:
            self.current_series = self.output_reader.get_timeseries(self.variables.currentData())
            self.observation = None
            self.observation_raw = None
            self.validation_status.setText("Load observations to compare with this variable.")
            self._plot_current()
        except Exception as exc:
            self.chart.show_message(str(exc))

    def _aggregated(self) -> pd.Series | None:
        if self.current_series is None:
            return None
        return OutputReader.aggregate(self.current_series, FREQ_LABELS[self.frequency.currentText()])

    def _plot_current(self, *_args) -> None:
        series = self._aggregated()
        if series is None:
            return
        plots = [(str(series.name), series)]
        if self.observation is not None:
            plots.append(("Observation", self.observation))
        self.chart.plot(plots, self.frequency.currentText())
        self._refresh_table()

    def _frequency_changed(self, *_args) -> None:
        if self.observation_raw is not None:
            self._apply_validation()
        else:
            self._plot_current()

    def _refresh_table(self, *_args) -> None:
        series = self._aggregated()
        if series is None:
            return
        if (
            self.table_mode.currentText() == "Complete CSV preview"
            and self.output_reader
            and self.output_reader.is_csv
        ):
            self._fill_dataframe_table(self.output_reader.full_dataframe())
        else:
            self._fill_series_table(series)

    def _fill_dataframe_table(self, frame: pd.DataFrame) -> None:
        self.table.setRowCount(len(frame))
        self.table.setColumnCount(len(frame.columns))
        self.table.setHorizontalHeaderLabels([str(column) for column in frame.columns])
        for row in range(len(frame)):
            for column in range(len(frame.columns)):
                self.table.setItem(row, column, QTableWidgetItem(str(frame.iloc[row, column])))

    def _fill_series_table(self, series: pd.Series) -> None:
        preview = series.iloc[:5000]
        self.table.setRowCount(len(preview))
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(("Time", str(series.name)))
        for row, (timestamp, value) in enumerate(preview.items()):
            self.table.setItem(row, 0, QTableWidgetItem(str(timestamp)))
            self.table.setItem(row, 1, QTableWidgetItem("" if pd.isna(value) else f"{value:.6g}"))

    def _layer_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        kind, value = data
        name = f"{kind}:{item.text(0)}"
        if item.checkState(0) != Qt.CheckState.Checked:
            self.map.remove_data_layer(name)
            return
        try:
            if kind == "raster" and self.staticmaps_reader:
                image, bounds, _range = self.staticmaps_reader.render(
                    value, self.resolution.value(), self.colormap.currentText()
                )
                self.map.set_raster_layer(name, image, bounds)
            elif kind == "geometry":
                self.map.add_geojson(name, geometry_geojson(Path(value)), fit=True)
        except Exception as exc:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            QMessageBox.warning(self, "Layer unavailable", str(exc))

    def map_clear(self) -> None:
        self.map.clear_data_layers()
        self.layers.blockSignals(True)
        iterator = self.layers.findItems("*", Qt.MatchFlag.MatchWildcard | Qt.MatchFlag.MatchRecursive)
        for item in iterator:
            if item.data(0, Qt.ItemDataRole.UserRole):
                item.setCheckState(0, Qt.CheckState.Unchecked)
        self.layers.blockSignals(False)

    def _load_observation(self) -> None:
        if self.current_series is None:
            QMessageBox.information(self, "Select output", "Select a simulation variable first.")
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Load observations", "", "CSV (*.csv)")
        if not path:
            return
        try:
            frame = pd.read_csv(path, sep=None, engine="python")
            time_column = next(
                (column for column in frame.columns if column.lower() in {"time", "date", "datetime", "timestamp"}),
                frame.columns[0],
            )
            value_column = next(
                column
                for column in frame.columns
                if column != time_column
                and pd.to_numeric(frame[column], errors="coerce").notna().any()
            )
            index = pd.to_datetime(frame[time_column], errors="coerce")
            values = pd.to_numeric(frame[value_column], errors="coerce")
            self.observation_raw = pd.Series(
                values.to_numpy(), index=index, name="Observation"
            ).dropna()
            self._apply_validation()
        except Exception as exc:
            QMessageBox.warning(self, "Validation unavailable", str(exc))

    def _apply_validation(self) -> None:
        if self.observation_raw is None or self.current_series is None:
            return
        frequency = FREQ_LABELS[self.frequency.currentText()]
        observation = self.observation_raw.resample(frequency).mean()
        aligned, metrics = validation_metrics(self._aggregated(), observation)
        self.observation = aligned["observation"]
        self.validation_status.setText(
            f"R2={metrics['r2']:.4f} | Bias={metrics['bias']:.4g} | "
            f"PBIAS={metrics['pbias']:.3f}% | Pairs={metrics['count']} | "
            "PBIAS convention: 100 * sum(sim-obs) / sum(obs)"
        )
        self._plot_current()

    def _calculate_ssfi(self) -> None:
        if not self.output_reader:
            return
        try:
            self.ssfi_frame = build_ssfi_table(self.output_reader)
            self.ssfi_period.clear()
            for timestamp in self.ssfi_frame.index:
                label = pd.Timestamp(timestamp).strftime("%Y-%m")
                if self.ssfi_period.findText(label) < 0:
                    self.ssfi_period.addItem(label, (int(timestamp.year), int(timestamp.month)))
            self._fill_ssfi_table()
        except Exception as exc:
            QMessageBox.warning(self, "SSFI unavailable", str(exc))

    def _fill_ssfi_table(self) -> None:
        period = self.ssfi_period.currentData()
        values = classify_period(self.ssfi_frame, *period) if period else {}
        self.ssfi_table.setRowCount(len(values))
        self.ssfi_table.setColumnCount(3)
        self.ssfi_table.setHorizontalHeaderLabels(("Station", "SSFI", "Class"))
        for row, (station, result) in enumerate(values.items()):
            self.ssfi_table.setItem(row, 0, QTableWidgetItem(station))
            self.ssfi_table.setItem(row, 1, QTableWidgetItem("" if result["value"] is None else f"{result['value']:.4f}"))
            self.ssfi_table.setItem(row, 2, QTableWidgetItem(result["label"]))

    def _show_ssfi(self) -> None:
        period, geometry = self.ssfi_period.currentData(), self.ssfi_geometry.currentData()
        if not period or not geometry:
            return
        try:
            values = classify_period(self.ssfi_frame, *period)
            self.map.add_geojson("SSFI", geometry_geojson(Path(geometry), values), choropleth=True, fit=True)
            self._fill_ssfi_table()
        except Exception as exc:
            QMessageBox.warning(self, "SSFI map unavailable", str(exc))

    def close_resources(self) -> None:
        if self.output_reader:
            self.output_reader.close()
            self.output_reader = None
        if self.staticmaps_reader:
            self.staticmaps_reader.close()
            self.staticmaps_reader = None


class ResultsPage(QWidget):
    OUTLET_LAYER = "Outlet / Stasiun"


    def __init__(self, project_provider, workflow) -> None:
        super().__init__()
        self.project_provider = project_provider
        self.workflow = workflow
        self.artifacts: ModelArtifacts | None = None
        self.staticmaps_reader: StaticmapsReader | None = None
        self.staticgeom_reader: StaticgeomReader | None = None
        self._loading = False
        self._outlets_enabled = True
        self._run_registry: RunRegistry | None = None
        self._combined_chains: list[RunChain] = []
        self._active_combined: RunChain | None = None
        self._combined_request = 0
        self._combined_tasks: dict[int, BackgroundJob] = {}
        self._model_request = 0
        self._model_job = None
        self._outlet_points = []
        self._snapshots = {}
        self._snapshot_path = None
        self._layer_jobs = {}
        self._export_job = None
        self._releasing = False
        self._released = False
        self._release_closers = []
        self._release_job = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        self.controls_panel = QWidget()
        controls_layout = QVBoxLayout(self.controls_panel)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        layout.addWidget(self.controls_panel)
        toolbar = QHBoxLayout()
        self.models = QComboBox()
        self.models.setMinimumWidth(220)
        self.models.currentIndexChanged.connect(self._model_changed)
        self.outputs = QComboBox()
        compact_combo(self.outputs, 360, 26)
        self.outputs.currentIndexChanged.connect(self._output_changed)
        load_output = QPushButton("Muat Output Manual")
        self.load_output_button = load_output
        load_output.clicked.connect(self._browse_output)
        self.export_combined = QPushButton("Ekspor Gabungan")
        self.export_combined.setEnabled(False)
        self.export_combined.clicked.connect(self._export_combined)
        compare_models = QPushButton("Bandingkan Model")
        compare_models.clicked.connect(self._compare_model)
        self.combined_progress = QProgressBar()
        self.combined_progress.setRange(0, 0)
        self.combined_progress.setMaximumWidth(130)
        self.combined_progress.setAccessibleName("Combined output loading progress")
        self.combined_progress.hide()
        refresh = QPushButton("Refresh")
        self.refresh_button = refresh
        refresh.clicked.connect(lambda: self.refresh_context(reload_released=True))
        self.release_button = QPushButton("Release model")
        self.release_button.setToolTip("Lepaskan file model dan output sebelum menjalankan HydroMT.")
        self.release_button.clicked.connect(self.release_model)
        toolbar.addWidget(QLabel("Model Wflow:"))
        toolbar.addWidget(self.models)
        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("Output aktif:"))
        toolbar.addWidget(self.outputs, 1)
        toolbar.addStretch(1)
        controls_layout.addLayout(toolbar)
        actions = QHBoxLayout()
        for widget in (load_output, self.export_combined, compare_models, self.combined_progress, refresh, self.release_button):
            actions.addWidget(widget)
        actions.addStretch()
        controls_layout.addLayout(actions)
        self.registry_status = QLabel("Run registry belum dimuat.")
        self.registry_status.setObjectName("muted")
        self.registry_status.setWordWrap(True)
        controls_layout.addWidget(self.registry_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.map_splitter = splitter
        self.layer_panel = LayerPanel()
        self.layer_panel.layer_toggled.connect(self._layer_toggled)
        self.layer_panel.display_resolution_changed.connect(self._resolution_changed)
        self.layer_panel.colormap_changed.connect(self._colormap_changed)
        splitter.addWidget(self.layer_panel)

        right = QSplitter(Qt.Orientation.Vertical)
        self.analysis_splitter = right
        right.setChildrenCollapsible(False)
        display = QWidget()
        display_layout = QGridLayout(display)
        display_layout.setContentsMargins(0, 0, 0, 0)
        self.display_stack = QStackedWidget()
        self.map = IndonesiaMapWidget()
        self.map.setMinimumSize(240, 100)
        self.display_stack.addWidget(self.map)
        display_layout.addWidget(self.display_stack, 0, 0)
        eye_panel = QWidget()
        eye_layout = QHBoxLayout(eye_panel)
        eye_layout.setContentsMargins(8, 8, 8, 8)
        self.map_focus_button = QToolButton()
        self.map_focus_button.setFixedSize(32, 32)
        self.map_focus_button.setIcon(FluentIcon.HIDE.icon())
        self.map_focus_button.setCheckable(True)
        self.map_focus_button.setToolTip("Sembunyikan panel Results — fokus peta")
        self.map_focus_button.setAccessibleName("Sembunyikan panel Results")
        self.map_focus_button.toggled.connect(self._toggle_map_focus)
        eye_layout.addWidget(self.map_focus_button)
        display_layout.addWidget(eye_panel, 0, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        right.addWidget(display)
        self.tabs = QTabWidget()
        self.output_tab = OutputTab()
        self.validation_tab = ValidationTab(self.output_tab)
        project = self.project()
        self.ssfi_tab = SSFITab(self.output_tab, project.root / ".hydrohub" / "ssfi" if project else None)
        self.export_tab = ExportTab()
        self.output_tab.station_selected.connect(self._station_selected)
        self.output_tab.data_loaded.connect(self._refresh_outlets)
        self.output_tab.data_loaded.connect(self._refresh_export_state)
        self.output_tab.plot_requested.connect(self._show_plot)
        self.output_tab.restore_plot_requested.connect(self._restore_plot)
        self.ssfi_tab.show_requested.connect(self._show_ssfi)
        self.ssfi_tab.hide_requested.connect(self._hide_ssfi)
        self.ssfi_tab.state_changed.connect(self._refresh_export_state)
        self.export_tab.gpkg_requested.connect(self._export_ssfi_gpkg)
        self.export_tab.excel_requested.connect(self._export_ssfi_excel)
        self.export_tab.shapefile_requested.connect(self._export_basin_shapefile)
        self.export_tab.png_requested.connect(self._export_ssfi_png)
        self.export_tab.discharge_requested.connect(self._export_discharge)
        self.tabs.addTab(self.output_tab, "Output")
        self.tabs.addTab(self.validation_tab, "Validation")
        self.tabs.addTab(self.ssfi_tab, "SSFI")
        self.tabs.addTab(self.export_tab, "Export")
        right.addWidget(self.tabs)
        self.tabs.setMinimumHeight(300)
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 3)
        right.setSizes((180, 480))
        splitter.addWidget(right)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes((220, 1180))
        layout.addWidget(splitter, 1)
        self.status = QLabel("Buka proyek untuk menemukan model Wflow.")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

        # Compatibility handles used by existing integrations and tests.
        self.variables = self.output_tab.variable_combo

    def project(self):
        return self.project_provider()

    def _toggle_map_focus(self, hidden: bool) -> None:
        panels = (self.controls_panel, self.layer_panel, self.tabs, self.status)
        if hidden:
            self._panel_visibility = [not panel.isHidden() for panel in panels]
            self._splitter_sizes = (self.map_splitter.sizes(), self.analysis_splitter.sizes())
            self._focus_display = self.display_stack.currentWidget()
            self._focus_tab = self.tabs.currentIndex()
            self._show_map()
            for panel in panels:
                panel.hide()
        else:
            for panel, visible in zip(panels, self._panel_visibility):
                panel.setVisible(visible)
            self.map_splitter.setSizes(self._splitter_sizes[0])
            self.analysis_splitter.setSizes(self._splitter_sizes[1])
            if self._focus_display is self.output_tab.plot:
                self._show_plot()
            self.tabs.setCurrentIndex(self._focus_tab)
        label = "Tampilkan panel Results" if hidden else "Sembunyikan panel Results"
        self.map_focus_button.setAccessibleName(label)
        self.map_focus_button.setToolTip(label)
        self.map_focus_button.setIcon((FluentIcon.VIEW if hidden else FluentIcon.HIDE).icon())
        QTimer.singleShot(0, self.map.invalidate_size)

    def release_model(self) -> None:
        if self._releasing:
            return
        self._releasing = self._released = True
        # Transfer readers to serialized cleanup: never close while a worker reads.
        for reader in (self.output_tab.reader, self.staticmaps_reader):
            if reader is not None:
                self._release_closers.append(reader.close)
        if self.staticgeom_reader is not None:
            self._release_closers.append(self.staticgeom_reader.clear)
        self.output_tab.reader = None
        self.staticmaps_reader = self.staticgeom_reader = None
        self.close_resources()
        self.artifacts = self._run_registry = self._active_combined = None
        self._combined_chains = []
        self.layer_panel.clear()
        self.outputs.blockSignals(True)
        self.outputs.clear()
        self.outputs.blockSignals(False)
        self.export_combined.setEnabled(False)
        self.export_tab.set_busy(False)
        self._refresh_export_state()
        self.output_tab.set_empty_state("Melepas model… menunggu pembacaan file selesai.")
        self.controls_panel.setEnabled(False)
        self.tabs.setEnabled(False)
        self._show_map()
        self.combined_progress.show()
        self.registry_status.setText("Melepas model…")
        self.status.setText("Menunggu job baca selesai. Jangan jalankan HydroMT dahulu.")
        closers = list(self._release_closers)

        def close_readers():
            # ponytail: shared single-worker queue; per-page queues if unrelated jobs delay release.
            for close in closers:
                close()

        def finish(message=""):
            if not isValid(self):
                return
            self._release_job = None
            self._releasing = False
            self.controls_panel.setEnabled(True)
            self.tabs.setEnabled(True)
            self.outputs.setEnabled(False)
            self.load_output_button.setEnabled(False)
            self.release_button.setEnabled(bool(message))
            self.combined_progress.hide()
            if message:
                self.registry_status.setText(f"Model gagal dilepas: {message}")
                self.status.setText("Klik Release model untuk mencoba kembali.")
                self.output_tab.set_empty_state(f"Model gagal dilepas: {message}")
            else:
                self._release_closers.clear()
                self.registry_status.setText("Model dilepas. Klik Refresh atau pilih model untuk memuat ulang.")
                self.status.setText("File Results sudah dilepas; HydroMT dapat dijalankan.")
                self.output_tab.set_empty_state("Model dilepas. Muat ulang melalui Refresh atau pilihan model.")

        # Cleanup must run even if this page is destroyed before the queue drains.
        self._release_job = BackgroundJob(None, close_readers, lambda _: finish(), finish)
        self._release_job.start()

    def _show_plot(self) -> None:
        self.tabs.setCurrentWidget(self.output_tab)
        plot = self.output_tab.plot
        tab_index = self.output_tab.inner_tabs.indexOf(plot)
        if tab_index >= 0:
            self._plot_tab_index = tab_index
            self.output_tab.inner_tabs.removeTab(tab_index)
        if self.display_stack.indexOf(plot) < 0:
            self.display_stack.addWidget(plot)
        self.display_stack.setCurrentWidget(plot)
        QTimer.singleShot(0, self.output_tab.plot.autoRange)

    def _show_map(self) -> None:
        self._restore_plot(select_output=False)
        self.display_stack.setCurrentWidget(self.map)
        QTimer.singleShot(0, self.map.invalidate_size)

    def _restore_plot(self, *, select_output: bool = True) -> None:
        plot = self.output_tab.plot
        display_index = self.display_stack.indexOf(plot)
        if display_index >= 0:
            self.display_stack.removeWidget(plot)
        if self.output_tab.inner_tabs.indexOf(plot) < 0:
            index = min(getattr(self, "_plot_tab_index", 0), self.output_tab.inner_tabs.count())
            self.output_tab.inner_tabs.insertTab(index, plot, "Grafik")
        self.output_tab.inner_tabs.setCurrentWidget(plot)
        if select_output:
            self.tabs.setCurrentWidget(self.output_tab)
        self.display_stack.setCurrentWidget(self.map)

    def refresh_context(self, *, reload_released: bool = False) -> None:
        if self._releasing or self._release_closers or (self._released and not reload_released):
            return
        project = self.project()
        state = self.workflow.load_ui_state(project) if project else {}
        self._loading = True
        _populate_models(self.models, project, state.get("results_model_root", ""))
        self._loading = False
        self._model_changed()

    def _selected_model(self) -> Path | None:
        value = self.models.currentData()
        return Path(value) if value else None

    def _model_changed(self, *_args) -> None:
        if self._loading or self._releasing or self._release_closers:
            return
        self._released = False
        self.outputs.setEnabled(True)
        self.load_output_button.setEnabled(True)
        self.release_button.setEnabled(True)
        # reset UI to loading state
        self.close_resources()
        self.layer_panel.clear()
        self.outputs.blockSignals(True)
        self.outputs.clear()
        self._run_registry = None
        self._combined_chains = []
        self._active_combined = None
        self._combined_request += 1
        self.export_combined.setEnabled(False)
        self.combined_progress.hide()
        self.registry_status.setText("Loading…")
        self.map.clear_data_layers()
        self.ssfi_tab.reset()
        self._show_map()
        project, model = self.project(), self._selected_model()
        if not project or not model:
            self.outputs.blockSignals(False)
            self.output_tab.set_empty_state("Tidak ada model Wflow yang tersedia.")
            return
        request = self._model_request
        self.registry_status.setText("Loading model in background...")
        self.combined_progress.show()
        def load():
            artifacts = discover_model_artifacts(project.root, model)
            geometry = StaticgeomReader(artifacts.staticgeoms)
            try:
                polygons = geometry.polygon_layer_names()
                points = geometry.station_points()
                preferred = [point for point in points if any(token in point["layer_name"].lower() for token in ("outlet", "gauge", "station"))]
                points = preferred or points
                if not points and artifacts.staticmaps:
                    staticmaps = StaticmapsReader(artifacts.staticmaps)
                    try:
                        points = staticmaps.outlet_points()
                    finally:
                        staticmaps.close()
            finally:
                geometry.clear()
            return artifacts, RunRegistry(project.root, model, project.name), rank_output_files(artifacts.outputs), points, polygons

        def ready(value):
            if request == self._model_request:
                artifacts, registry, ranked, points, polygons = value
                self._ranked_outputs = ranked
                self._outlet_points = points
                self._polygon_layers = polygons
                try:
                    self._model_loaded(artifacts, registry)
                except Exception as exc:
                    failed(str(exc))

        def failed(message):
            if request == self._model_request:
                self._loading = False
                self.outputs.blockSignals(False)
                self.combined_progress.hide()
                self.registry_status.setText(f"Model could not be loaded: {message}")

        self._model_job = BackgroundJob(self, load, ready, failed)
        self._model_job.start()

    def _model_loaded(self, artifacts: ModelArtifacts, registry: RunRegistry) -> None:
        project = self.project()
        model = self._selected_model()
        if not project or not model:
            self._loading = False
            self.combined_progress.hide()
            return
        self.artifacts = artifacts
        self._run_registry = registry
        self.staticgeom_reader = StaticgeomReader(artifacts.staticgeoms)
        if artifacts.staticmaps:
            self.staticmaps_reader = StaticmapsReader(artifacts.staticmaps)
            self.layer_panel.add_category("Staticmaps")
            for variable in self.staticmaps_reader.list_variables():
                self.layer_panel.add_layer("Staticmaps", variable)
        if artifacts.staticgeoms:
            self.layer_panel.add_category("Staticgeom")
            for name in self.staticgeom_reader.layer_names():
                self.layer_panel.add_layer("Staticgeom", name)
            self.ssfi_tab.set_basin_layers(self._polygon_layers)
        self._refresh_export_state()
        points = self._candidate_outlets()
        if points:
            self.layer_panel.add_category("Outlet")
            self.layer_panel.add_layer("Outlet", self.OUTLET_LAYER, checked=True)
        with artifacts.config.open("rb") as stream:
            timestep = int(tomllib.load(stream).get("time", {}).get("timestepsecs", 86400))
        all_chains = registry.chains(timestep)
        self._combined_chains = [chain for chain in all_chains if len(chain.runs) > 1]
        breaks = [
            f"setelah {chain.runs[-1].run_id}: {chain.break_reason}"
            for chain in all_chains
            if chain.break_reason
        ]
        completed_count = sum(len(chain.runs) for chain in all_chains)
        registry_text = (
            f"Registry: {completed_count} run completed, "
            f"{len(self._combined_chains)} rangkaian dapat digabung."
        )
        if breaks:
            registry_text += " Rangkaian terputus " + "; ".join(breaks) + "."
        self.registry_status.setText(registry_text)
        for index, chain in enumerate(self._combined_chains):
            self.outputs.addItem(
                f"Gabungan registry: {chain.label}",
                {"kind": "combined", "index": index},
            )
            self.outputs.setItemData(self.outputs.count() - 1, chain.label, Qt.ItemDataRole.ToolTipRole)
        for path in self._ranked_outputs:
            relative = path.relative_to(project.root)
            label = f"{relative.parent.name}/{relative.name}" if relative.parent.name else relative.name
            self.outputs.addItem(label, str(path))
            self.outputs.setItemData(self.outputs.count() - 1, str(relative), Qt.ItemDataRole.ToolTipRole)
        self.workflow.save_ui_state(project, results_model_root=str(model.resolve()))
        self.status.setText(
            f"Model: {model.relative_to(project.root)} | "
            f"{self.outputs.count()} output | {len(self._combined_chains)} rangkaian gabungan | "
            f"{len(artifacts.staticgeoms)} staticgeom"
        )
        self._loading = False
        self.combined_progress.hide()
        self.outputs.blockSignals(False)
        self._output_changed()

    def _browse_output(self) -> None:
        project = self.project()
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Pilih output simulasi",
            str(project.root if project else Path.cwd()),
            "Output (*.nc *.csv)",
        )
        if path:
            self.outputs.addItem(Path(path).name, path)
            self.outputs.setItemData(self.outputs.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
            self.outputs.setCurrentIndex(self.outputs.count() - 1)

    def _output_changed(self, *_args) -> None:
        if self._released or self._releasing:
            return
        self.ssfi_tab.reset()
        value = self.outputs.currentData()
        self._combined_request += 1
        request = self._combined_request
        self._active_combined = None
        self.export_combined.setEnabled(False)
        self.combined_progress.hide()
        if not value:
            self.output_tab.set_empty_state("Tidak ditemukan output yang dapat dibaca pada model ini.")
            return
        try:
            if isinstance(value, dict) and value.get("kind") == "combined":
                if not self._run_registry:
                    raise ValueError("Run registry is unavailable.")
                self.status.setText("Menggabungkan output run yang valid...")
                chain = self._combined_chains[int(value["index"])]
                self.combined_progress.show()
                registry = self._run_registry
                task = BackgroundJob(
                    self,
                    lambda: registry.build_combined_output(chain),
                    lambda path: self._combined_ready(request, chain, Path(path)),
                    lambda message: self._combined_failed(request, message),
                )
                self._combined_tasks[request] = task
                task.start()
            else:
                self.output_tab.load_file(Path(value))
                self.status.clear()
        except Exception as exc:
            self.output_tab.set_empty_state(f"Output gagal dimuat: {exc}")

    def _combined_ready(self, request: int, chain: RunChain, path: Path) -> None:
        self._combined_tasks.pop(request, None)
        if request != self._combined_request:
            return
        self.combined_progress.hide()
        try:
            self.output_tab.load_file(path)
        except Exception as exc:
            self._combined_failed(request, str(exc))
            return
        self._active_combined = chain
        self.export_combined.setEnabled(True)
        suffix = f" Batas berikutnya: {chain.break_reason}." if chain.break_reason else ""
        self.status.setText(f"Output gabungan aktif: {chain.label}.{suffix}")

    def _combined_failed(self, request: int, message: str) -> None:
        self._combined_tasks.pop(request, None)
        if request != self._combined_request:
            return
        self.combined_progress.hide()
        self.output_tab.set_empty_state(f"Output gabungan gagal dimuat: {message}")
        self.status.setText(f"Output gabungan gagal dimuat: {message}")

    def _export_combined(self) -> None:
        if not self._run_registry or not self._active_combined:
            return
        project = self.project()
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Ekspor hasil gabungan",
            str((project.root if project else Path.cwd()) / "export_combined.csv"),
            "CSV (*.csv);;Excel (*.xlsx)",
        )
        if not selected:
            return
        registry, chain = self._run_registry, self._active_combined
        destination = Path(selected)
        if destination.suffix.lower() not in {".csv", ".xlsx"}:
            destination = destination.with_suffix(".xlsx" if "xlsx" in _filter else ".csv")
        self._start_export("Mengekspor debit gabungan…", destination,
                           lambda: registry.export_combined(chain, destination))

    def _compare_model(self) -> None:
        project, active_model = self.project(), self._selected_model()
        if not project or not active_model or not self.output_tab.current_variable:
            QMessageBox.information(
                self,
                "Output aktif diperlukan",
                "Muat output dan pilih variabel model aktif sebelum membandingkan model.",
            )
            return
        models = [model for model in discover_wflow_models(project.root) if model != active_model]
        if not models:
            QMessageBox.information(
                self,
                "Model pembanding tidak tersedia",
                "Proyek ini belum memiliki model Wflow lain untuk dibandingkan.",
            )
            return
        labels = [str(model.relative_to(project.root)) for model in models]
        selected, accepted = QInputDialog.getItem(
            self, "Bandingkan model", "Model pembanding", labels, 0, False
        )
        if not accepted:
            return
        model = models[labels.index(selected)]
        try:
            artifacts = discover_model_artifacts(project.root, model)
            outputs = rank_output_files(artifacts.outputs)
            if not outputs:
                raise FileNotFoundError("Model pembanding belum memiliki output.")
            output_labels = [str(path.relative_to(project.root)) for path in outputs]
            output_label, accepted = QInputDialog.getItem(
                self, "Pilih output pembanding", "Output", output_labels, 0, False
            )
            if not accepted:
                return
            path = outputs[output_labels.index(output_label)]
            reader = OutputReader(path)
            try:
                variables = reader.list_variables()
                current = self.output_tab.current_variable
                default = variables.index(current) if current in variables else 0
                variable, accepted = QInputDialog.getItem(
                    self, "Pilih variabel pembanding", "Variabel", variables, default, False
                )
                if not accepted:
                    return
                series = reader.get_timeseries(variable)
            finally:
                reader.close()
            self.output_tab.add_comparison(series, f"{selected}: {variable}")
            self.status.setText(
                f"Membandingkan model aktif dengan {selected}; data tidak digabung ke registry chain."
            )
        except Exception as exc:
            QMessageBox.warning(self, "Perbandingan model gagal", str(exc))

    def _layer_toggled(self, category: str, name: str, checked: bool) -> None:
        key = f"{category}:{name}"
        old = self._layer_jobs.pop(key, None)
        if old:
            old.cancel()
        if not checked:
            self.map.remove_data_layer(key if category != "Outlet" else self.OUTLET_LAYER)
            if category == "Outlet":
                self._outlets_enabled = False
            return
        try:
            if category == "Staticmaps" and self.staticmaps_reader:
                path = self.staticmaps_reader.path
                dimension = self.layer_panel.current_max_display_dim()
                colormap = self.layer_panel.colormap.currentText()
                def render():
                    reader = StaticmapsReader(path)
                    try:
                        return reader.render(name, dimension, colormap)
                    finally:
                        reader.close()
                def display(value):
                    self._layer_jobs.pop(key, None)
                    image, bounds, _range = value
                    self.map.set_raster_layer(key, image, bounds)
                job = BackgroundJob(self, render, display, lambda message: self.status.setText(message))
                self._layer_jobs[key] = job
                job.start()
            elif category == "Staticgeom" and self.staticgeom_reader:
                path = self.staticgeom_reader.path_for_layer(name)
                def render_geometry():
                    reader = StaticgeomReader((path,))
                    try:
                        return reader.geojson(path)
                    finally:
                        reader.clear()
                def display_geometry(value):
                    self._layer_jobs.pop(key, None)
                    self.map.add_geojson(key, value, fit=True)
                job = BackgroundJob(self, render_geometry, display_geometry, lambda message: self.status.setText(message))
                self._layer_jobs[key] = job
                job.start()
            elif category == "Outlet":
                self._outlets_enabled = True
                self._refresh_outlets()
        except Exception as exc:
            QMessageBox.warning(self, "Layer gagal ditampilkan", f"{name}: {exc}")

    def _resolution_changed(self, _value: int) -> None:
        root = self.layer_panel._categories.get("Staticmaps")
        if root:
            for index in range(root.childCount()):
                item = root.child(index)
                if item.checkState(0) == Qt.CheckState.Checked:
                    self._layer_toggled("Staticmaps", item.text(0), True)

    def _colormap_changed(self, _value: str) -> None:
        self._resolution_changed(self.layer_panel.current_max_display_dim())

    def _candidate_outlets(self) -> list[dict]:
        return self._outlet_points

    def _refresh_outlets(self) -> None:
        if not self._outlets_enabled:
            return
        points = self._candidate_outlets()
        if not points:
            return
        reader = self.output_tab.reader
        path = reader.path if reader else None
        if path != self._snapshot_path:
            self._snapshot_path = path
            self._snapshots = {}
            if path:
                request = self._model_request
                def load_snapshots():
                    source = OutputReader(path)
                    try:
                        return source.latest_station_snapshots()
                    finally:
                        source.close()
                def ready(value):
                    if request == self._model_request and self._snapshot_path == path:
                        self._snapshots = value
                        self._refresh_outlets()
                BackgroundJob(self, load_snapshots, ready, lambda message: self.status.setText(message)).start()
        snapshots = self._snapshots
        features = []
        for index, point in enumerate(points, start=1):
            matched = next((station for station in point["station_ids"] if station in snapshots), None)
            station_id = matched or point.get("station_id") or str(index)
            snapshot = snapshots.get(matched) if matched else None
            popup = [f"<b>Outlet / Stasiun {escape(str(station_id))}</b>"]
            popup.append(f"Koordinat: {point['lat']:.5f}, {point['lon']:.5f}")
            if snapshot:
                popup.append(f"Debit terakhir: {snapshot['value']:.4g} {escape(snapshot['unit'])}")
                popup.append(f"Tanggal terakhir: {snapshot['time'].strftime('%Y-%m-%d %H:%M:%S')}")
                popup.append(f"Kolom output: {escape(snapshot['column'])}")
            else:
                popup.append("Debit terakhir: belum tersedia")
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [point["lon"], point["lat"]]},
                    "properties": {
                        "station_id": str(station_id),
                        "__popup_html": "<br>".join(popup),
                    },
                }
            )
        self.map.add_geojson(
            self.OUTLET_LAYER,
            {"type": "FeatureCollection", "features": features},
        )

    def _station_selected(self, column: str, station_id: str, latest: float) -> None:
        if not self.staticgeom_reader:
            return
        point = self.staticgeom_reader.find_station_point(station_id)
        if not point:
            return
        timestamp = self.output_tab.latest_timestamp()
        popup = (
            f"<b>Outlet / Stasiun {escape(station_id)}</b><br>"
            f"Koordinat: {point['lat']:.5f}, {point['lon']:.5f}<br>"
            f"Debit terakhir: {latest:.4g} {escape(self.output_tab.current_unit or 'm3/s')}<br>"
            f"Tanggal terakhir: {timestamp if timestamp is not None else 'tidak tersedia'}<br>"
            f"Kolom output: {escape(column)}"
        )
        self.map.focus_marker(point["lat"], point["lon"], popup, f"Stasiun {station_id}")

    def _show_ssfi(self, layer_name: str, values: dict) -> None:
        if not self.staticgeom_reader:
            return
        scale = getattr(self.ssfi_tab, "scale", None)
        label = f"SSFI-{scale.currentData()}" if scale else "SSFI"
        path = self.staticgeom_reader.path_for_layer(layer_name)
        if path:
            try:
                geojson = self.staticgeom_reader.geojson(path, values)
                features = geojson.get("features", [])
                matched = sum(
                    "__ssfi_value" in feature.get("properties", {}) for feature in features
                )
                self.map.add_geojson("SSFI", geojson, choropleth=True, fit=True)
                self._show_map()
                if matched:
                    self.ssfi_tab.status.setText(
                        f"{label} ditampilkan pada {matched} dari {len(features)} fitur layer '{layer_name}'."
                    )
                else:
                    self.ssfi_tab.status.setText(
                        f"Layer '{layer_name}' ditampilkan, tetapi tidak ada ID basin yang cocok dengan output {label}."
                    )
            except Exception as exc:
                self.ssfi_tab.status.setText(f"Gagal menampilkan {label}: {exc}")

    def _hide_ssfi(self) -> None:
        self.map.remove_data_layer("SSFI")

    def _selected_basin_path(self) -> Path | None:
        if not self.staticgeom_reader:
            return None
        return self.staticgeom_reader.path_for_layer(self.ssfi_tab.layer.currentText())

    def _refresh_export_state(self) -> None:
        frame = self.ssfi_tab.frame
        has_ssfi = frame is not None and not frame.empty and bool(frame.notna().to_numpy().any())
        self.export_tab.set_available(has_ssfi, self._selected_basin_path() is not None,
                                      self.output_tab.reader is not None)
        scale = self.ssfi_tab.scale.currentData()
        self.export_tab.excel.setText(f"Ekspor SSFI-{scale} ke Excel")
        self.export_tab.gpkg.setText(f"Ekspor Basin + SSFI-{scale} ke GeoPackage")
        self.export_tab.png.setText(f"Ekspor Peta SSFI-{scale} ke PNG")

    def _start_export(self, description: str, destination: Path, function) -> None:
        if self._export_job:
            return
        self.export_tab.set_busy(True, description)

        def completed(_value) -> None:
            self._export_job = None
            self.export_tab.set_busy(False)
            self._refresh_export_state()
            self.export_tab.status.setText(f"Export selesai: {destination}")
            if isinstance(_value, int) and _value:
                self.export_tab.status.setText(f"Export selesai: {destination} | {_value} basin tanpa pasangan river; basin_id kosong.")

        def failed(message: str) -> None:
            self._export_job = None
            self.export_tab.set_busy(False)
            self._refresh_export_state()
            self.export_tab.status.setText(f"Export gagal: {message}")

        self._export_job = BackgroundJob(self, function, completed, failed)
        self._export_job.start()

    def _export_ssfi_gpkg(self) -> None:
        basin = self._selected_basin_path()
        period = self.ssfi_tab._selected_period()
        if not basin or not period:
            return
        project = self.project()
        scale = self.ssfi_tab.scale.currentData()
        project_name = project.name if project else "hydrohub"
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Ekspor Basin + SSFI",
            str((project.root if project else Path.cwd()) / f"basin_ssfi_{scale}.gpkg"),
            "GeoPackage (*.gpkg)",
        )
        if not selected:
            return
        destination = Path(selected).with_suffix(".gpkg")
        values = dict(self.ssfi_tab.current_values)
        self._start_export(
            "Mengekspor basin dan SSFI ke GeoPackage…",
            destination,
            lambda: export_basin_ssfi_gpkg(basin, destination, values, period, scale, project_name),
        )

    def _export_ssfi_excel(self) -> None:
        if self.ssfi_tab.frame is None:
            return
        project = self.project()
        scale = self.ssfi_tab.scale.currentData()
        project_name = project.name if project else "hydrohub"
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Ekspor hasil SSFI",
            str((project.root if project else Path.cwd()) / f"ssfi_{scale}.xlsx"),
            "Excel (*.xlsx)",
        )
        if not selected:
            return
        destination = Path(selected).with_suffix(".xlsx")
        frame = self.ssfi_tab.frame.copy()
        self._start_export(
            "Mengekspor seluruh hasil SSFI ke Excel…",
            destination,
            lambda: export_ssfi_excel(frame, destination, scale, project_name),
        )

    def _export_basin_shapefile(self) -> None:
        basin = self._selected_basin_path()
        if not basin:
            return
        project = self.project()
        project_name = project.name if project else "hydrohub"
        reader = self.output_tab.reader
        river_ids = [extract_station_id(name) for name in reader.list_variables()
                     if reader.is_discharge_variable(name) and extract_station_id(name)] if reader else None
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Ekspor layer basin",
            str((project.root if project else Path.cwd()) / "basin.zip"),
            "Shapefile ZIP (*.zip)",
        )
        if not selected:
            return
        destination = Path(selected).with_suffix(".zip")
        self._start_export(
            "Mengekspor basin ke Shapefile ZIP…",
            destination,
            lambda: export_basin_shapefile(basin, destination, project_name, river_ids or None),
        )

    def _export_ssfi_png(self) -> None:
        basin = self._selected_basin_path()
        period = self.ssfi_tab._selected_period()
        if not basin or not period:
            return
        project = self.project()
        project_name = project.name if project else "hydrohub"
        scale = self.ssfi_tab.scale.currentData()
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Ekspor peta SSFI",
            str((project.root if project else Path.cwd()) / f"{project_name}_SSFI-{scale}_{period[1]:02d}_{period[0]:04d}.png"),
            "PNG (*.png)",
        )
        if not selected:
            return
        destination = Path(selected).with_suffix(".png")
        values = dict(self.ssfi_tab.current_values)
        self._start_export(
            "Mengekspor peta SSFI ke PNG…",
            destination,
            lambda: export_basin_ssfi_png(
                basin, destination, values, period, project_name, scale
            ),
        )

    def _export_discharge(self) -> None:
        reader = self.output_tab.reader
        if reader is None:
            return
        project = self.project()
        project_name = project.name if project else "hydrohub"
        selected, file_filter = QFileDialog.getSaveFileName(
            self, "Ekspor seluruh debit", str((project.root if project else Path.cwd()) / "debit.csv"),
            "CSV (*.csv);;Excel (*.xlsx)")
        if not selected:
            return
        destination = Path(selected)
        if destination.suffix.lower() not in {".csv", ".xlsx"}:
            destination = destination.with_suffix(".xlsx" if "xlsx" in file_filter else ".csv")
        source = reader.path
        self._start_export("Mengekspor debit dengan basin_id…", destination,
                           lambda: export_discharge(source, destination, project_name))

    def close_resources(self) -> None:
        self.output_tab.close_reader()
        self.ssfi_tab.reset()
        self._model_request += 1
        self._combined_request += 1
        if self._model_job is not None:
            self._model_job.cancel()
            self._model_job = None
        for job in self._combined_tasks.values():
            job.cancel()
        self._combined_tasks.clear()
        for job in self._layer_jobs.values():
            job.cancel()
        self._layer_jobs.clear()
        if self._export_job:
            self._export_job.cancel()
            self._export_job = None
        self.map.clear_data_layers()
        self._outlet_points = []
        self._snapshots = {}
        self._snapshot_path = None
        if self.staticmaps_reader:
            self.staticmaps_reader.close()
            self.staticmaps_reader = None
        if self.staticgeom_reader:
            self.staticgeom_reader.clear()
            self.staticgeom_reader = None
