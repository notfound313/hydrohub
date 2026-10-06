from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
from hydrohub.ui.layout_helpers import scroll_panel
from hydrohub.ui.layout_helpers import compact_combo
from hydrohub.ui.background import BackgroundJob
from PySide6.QtCore import QDate, Qt, Signal, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QSlider,
    QTableWidget,
    QTableView,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hydrohub.results.analysis import (
    FREQ_LABELS,
    MONTH_NAMES_ID,
    NO_DATA_COLOR,
    NO_DATA_LABEL,
    SSFI_CLASSES,
    SSFI_SCALES,
    OutputReader,
    available_periods,
    build_ssfi_table,
    classify_period,
    extract_station_id,
    validation_metrics,
)
from hydrohub.results.export import export_validation_excel, export_validation_template


class FreqAwareDateAxisItem(pg.DateAxisItem):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._frequency = "Harian"
        self._dense = False

    def set_frequency(self, frequency: str) -> None:
        self._frequency = frequency
        self.picture = None
        self.update()

    def set_density(self, dense: bool) -> None:
        self._dense = dense
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):
        result = []
        for value in values:
            try:
                date = datetime.fromtimestamp(value)
                if self._frequency == "Harian":
                    result.append(f"{date.day}-{date.month}-{date.year}")
                elif self._frequency == "Bulanan":
                    result.append(f"{date.month}-{date.year}")
                else:
                    result.append(str(date.year))
            except (ValueError, OSError, OverflowError):
                result.append("")
        return result


class LayerPanel(QWidget):
    layer_toggled = Signal(str, str, bool)
    display_resolution_changed = Signal(int)
    colormap_changed = Signal(str)
    RESOLUTIONS = (256, 512, 1024, 2048, 4096)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Layer Proyek",))
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree, 1)
        settings = QGroupBox("Pengaturan Tampilan")
        form = QFormLayout(settings)
        self.resolution = QSlider(Qt.Orientation.Horizontal)
        self.resolution.setRange(0, len(self.RESOLUTIONS) - 1)
        self.resolution.setValue(2)
        self.resolution.valueChanged.connect(self._resolution_changed)
        self.resolution_label = QLabel("1024 px (default)")
        form.addRow(QLabel("Resolusi tampilan maks.:"))
        form.addRow(self.resolution)
        form.addRow("", self.resolution_label)
        note = QLabel("Nilai lebih kecil = render lebih cepat, tampilan lebih kasar.")
        note.setWordWrap(True)
        form.addRow(note)
        self.colormap = QComboBox()
        self.colormap.addItems(("viridis", "terrain", "Blues", "YlOrBr", "coolwarm"))
        self.colormap.currentTextChanged.connect(self.colormap_changed)
        form.addRow(QLabel("Skema warna:"))
        form.addRow(self.colormap)
        layout.addWidget(settings)
        self._categories: dict[str, QTreeWidgetItem] = {}

    def current_max_display_dim(self) -> int:
        return self.RESOLUTIONS[self.resolution.value()]

    def clear(self) -> None:
        self.tree.clear()
        self._categories.clear()

    def add_category(self, category: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem((category,))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        item.setExpanded(True)
        self.tree.addTopLevelItem(item)
        self._categories[category] = item
        return item

    def add_layer(self, category: str, name: str, checked: bool = False) -> None:
        parent = self._categories.get(category) or self.add_category(category)
        item = QTreeWidgetItem(parent, (name,))
        item.setData(0, Qt.ItemDataRole.UserRole, category)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        category = item.data(0, Qt.ItemDataRole.UserRole)
        if category is not None:
            self.layer_toggled.emit(category, item.text(0), item.checkState(0) == Qt.CheckState.Checked)

    def _resolution_changed(self, index: int) -> None:
        value = self.RESOLUTIONS[index]
        self.resolution_label.setText(f"{value} px")
        self.display_resolution_changed.emit(value)


class DataFrameModel(QAbstractTableModel):
    """Expose values on demand instead of allocating a Qt object for every cell."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame = pd.DataFrame()

    def set_frame(self, frame):
        self.beginResetModel()
        self.frame = frame
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.frame)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.frame.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            value = self.frame.iat[index.row(), index.column()]
            return "" if pd.isna(value) else str(value)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return str(self.frame.columns[section]) if orientation == Qt.Orientation.Horizontal else str(section + 1)


class OutputTab(QWidget):
    station_selected = Signal(str, str, float)
    data_loaded = Signal()
    plot_requested = Signal()
    restore_plot_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.reader: OutputReader | None = None
        self.current_series: pd.Series | None = None
        self.current_variable: str | None = None
        self.current_unit = ""
        self._request = 0
        self._jobs = set()
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        load = QPushButton("Muat output.nc / .csv")
        load.clicked.connect(self._browse)
        self.variable_label = QLabel("Variabel:")
        self.variable_combo = QComboBox()
        compact_combo(self.variable_combo, 300, 22)
        self.variable_combo.currentIndexChanged.connect(self._variable_changed)
        self.frequency = QComboBox()
        compact_combo(self.frequency, 150, 12)
        self.frequency.addItems(FREQ_LABELS)
        self.frequency.currentTextChanged.connect(self._frequency_changed)
        plot = QPushButton("Plot")
        plot.clicked.connect(lambda: self._plot(show=True))
        restore_plot = QPushButton("Kembalikan Grafik")
        restore_plot.clicked.connect(self.restore_plot_requested.emit)
        for widget in (load, plot, restore_plot):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.range_combo = QComboBox()
        self.range_combo.setFixedWidth(120)
        self._populate_range_combo("Harian")
        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setFixedWidth(115)
        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setFixedWidth(115)
        self.date_start_label = QLabel("Dari")
        self.date_end_label = QLabel("Sampai")

        selectors = QHBoxLayout()
        selectors.setContentsMargins(0, 0, 0, 0)
        selectors.setSpacing(6)
        for widget in (
            self.variable_label,
            self.variable_combo,
            QLabel("Agregasi:"),
            self.frequency,
            QLabel("Rentang:"),
            self.range_combo,
            self.date_start_label,
            self.date_start,
            self.date_end_label,
            self.date_end,
        ):
            selectors.addWidget(widget)
        selectors.addStretch(1)
        layout.addLayout(selectors)

        self._window_limits: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self.range_combo.setCurrentText("Semua")
        for widget in (
            self.date_start_label,
            self.date_start,
            self.date_end_label,
            self.date_end,
        ):
            widget.hide()

        self.range_combo.currentTextChanged.connect(self._on_range_combo_changed)
        self.date_end.dateChanged.connect(self._on_date_changed)
        self.date_start.dateChanged.connect(self._on_date_changed)

        self.inner_tabs = QTabWidget()
        self.date_axis = FreqAwareDateAxisItem(orientation="bottom")
        self.plot = pg.PlotWidget(title="Deret Waktu", axisItems={"bottom": self.date_axis})
        self.plot.addLegend()
        self.plot.setLabel("bottom", "Waktu")
        self.plot.showGrid(x=True, y=True)
        self.plot.getPlotItem().getViewBox().setMouseEnabled(x=True, y=False)
        self.inner_tabs.addTab(self.plot, "Grafik")
        self.table = QTableView()
        self.table_model = DataFrameModel(self.table)
        self.table.setModel(self.table_model)
        self.inner_tabs.addTab(self.table, "Tabel Data Mentah")
        self.full_table = QTableView()
        self.full_table_model = DataFrameModel(self.full_table)
        self.full_table.setModel(self.full_table_model)
        self.inner_tabs.addTab(self.full_table, "Tabel Lengkap (CSV)")
        layout.addWidget(self.inner_tabs, 1)
        for table in (self.table, self.full_table):
            table.setMinimumHeight(160)
            table.setAlternatingRowColors(True)
            table.horizontalHeader().setDefaultSectionSize(150)
            table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Pilih file output", "", "Output simulasi (*.nc *.csv)")
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        self.close_reader()
        request = self._request
        self.variable_combo.clear()
        self.plot.setTitle("Memuat output…")
        self._start_job(lambda: OutputReader(path), lambda reader: self._file_ready(request, reader), request)

    def _start_job(self, function, completed, request):
        def done(value):
            self._jobs.discard(job)
            completed(value)

        def failed(message):
            self._jobs.discard(job)
            if request == self._request:
                self.plot.setTitle(f"Gagal memuat output: {message}")

        job = BackgroundJob(self, function, done, failed)
        self._jobs.add(job)
        job.start()

    def _file_ready(self, request, reader):
        if request != self._request:
            reader.close()
            return
        self.reader = reader
        self.current_series = None
        self.variable_combo.blockSignals(True)
        self.variable_combo.clear()
        variables = self.reader.list_variables()
        if self.reader.is_csv:
            self.variable_label.setText("Stasiun (kolom CSV):")
            for variable in variables:
                self.variable_combo.addItem(self.reader.station_label(variable), variable)
                self.variable_combo.setItemData(self.variable_combo.count() - 1, str(variable), Qt.ItemDataRole.ToolTipRole)
        else:
            self.variable_label.setText("Variabel:")
            for variable in variables:
                self.variable_combo.addItem(variable, variable)
                self.variable_combo.setItemData(self.variable_combo.count() - 1, str(variable), Qt.ItemDataRole.ToolTipRole)
        self.variable_combo.blockSignals(False)
        if not variables:
            self.set_empty_state(f"'{reader.path.name}' tidak memiliki variabel data.")
            self.data_loaded.emit()
            return
        self.variable_combo.setCurrentIndex(0)
        self._variable_changed(0)
        if self.reader.is_csv:
            self._fill_full_table()

    def set_empty_state(self, message: str) -> None:
        self.close_reader()
        self.variable_combo.clear()
        self.plot.clear()
        self.plot.setTitle(message)
        self.table_model.set_frame(pd.DataFrame())
        self.full_table_model.set_frame(pd.DataFrame())

    def _variable_changed(self, index: int) -> None:
        if index < 0 or not self.reader:
            return
        variable = self.variable_combo.itemData(index) or self.variable_combo.itemText(index)
        self._request += 1
        request = self._request
        for job in self._jobs:
            job.cancel()
        self._jobs.clear()
        path = self.reader.path
        self.current_series = None
        self.plot.clear()
        self.plot.setTitle("Memuat deret waktu…")

        def read_series():
            reader = OutputReader(path)
            try:
                return reader.get_timeseries(str(variable))
            finally:
                reader.close()

        self._start_job(read_series, lambda series: self._series_ready(request, variable, series), request)

    def _series_ready(self, request, variable, series):
        if request != self._request or self.reader is None:
            return
        self.current_series = series
        self.current_variable = str(variable)
        self.current_unit = self.reader.get_unit(str(variable))
        self._populate_range_combo(self.frequency.currentText())
        self._apply_range_bounds()
        self._sync_range_ui(self.range_combo.currentText())
        if self.reader.is_csv:
            station_id = extract_station_id(str(variable))
            if station_id:
                valid = self.current_series.dropna()
                latest = float(valid.iloc[-1]) if not valid.empty else float("nan")
                self.station_selected.emit(str(variable), station_id, latest)
        self.data_loaded.emit()

    def _frequency_changed(self, _value: str) -> None:
        self._populate_range_combo(self.frequency.currentText())
        self._sync_range_ui(self.range_combo.currentText())
        self._fill_table()

    def current_aggregated(self) -> pd.Series | None:
        if self.current_series is None or not self.reader:
            return None
        return self.reader.aggregate(self.current_series, FREQ_LABELS[self.frequency.currentText()])

    def _plot(self, *, show: bool = False) -> None:
        series = self.current_aggregated()
        self.plot.clear()
        if series is None or series.dropna().empty:
            self.plot.setTitle("Tidak ada data yang dapat diplot.")
            return
        if self._window_limits is not None:
            series = self._clip_to_window(series)
            if series is None or series.dropna().empty:
                self.plot.setTitle("Tidak ada data pada rentang terpilih.")
                return
        frequency = self.frequency.currentText()
        self.date_axis.set_frequency(frequency)
        valid_series = series.dropna()
        if not valid_series.empty:
            span = pd.Timestamp(valid_series.index.max()) - pd.Timestamp(valid_series.index.min())
            self.date_axis.set_density(frequency == "Harian" and span <= timedelta(days=93))
        x = np.asarray([pd.Timestamp(value).timestamp() for value in series.index], dtype=float)
        y = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(x) & np.isfinite(y)
        if not finite.any():
            self.plot.setTitle("Tidak ada data finite yang dapat diplot.")
            return
        curve = self.plot.plot(x[finite], y[finite], pen=pg.mkPen("#22d3ee", width=2), connect="finite", name="Model aktif")
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        self.plot.setTitle(f"Variabel: {series.name} ({frequency})")
        self.plot.setLabel("left", str(series.name), units=self.current_unit or None)
        if self.inner_tabs.indexOf(self.plot) >= 0:
            self.inner_tabs.setCurrentWidget(self.plot)
        self.plot.autoRange()
        if self._window_limits is not None:
            start, end = self._window_limits
            self.plot.setXRange(
                pd.Timestamp(start).timestamp(),
                pd.Timestamp(end).timestamp(),
                padding=0,
            )
        elif len(valid_series) > 1:
            self.plot.setXRange(
                pd.Timestamp(valid_series.index.min()).timestamp(),
                pd.Timestamp(valid_series.index.max()).timestamp(),
                padding=0,
            )
        if show:
            self.plot_requested.emit()

    def add_comparison(self, series: pd.Series, label: str) -> None:
        self._plot()
        aggregated = OutputReader.aggregate(
            series, FREQ_LABELS[self.frequency.currentText()]
        )
        if self._window_limits is not None:
            aggregated = self._clip_to_window(aggregated)
        if aggregated.dropna().empty:
            raise ValueError("Output pembanding tidak memiliki data pada rentang terpilih.")
        x = np.asarray(
            [pd.Timestamp(value).timestamp() for value in aggregated.index], dtype=float
        )
        y = pd.to_numeric(aggregated, errors="coerce").to_numpy(dtype=float)
        self.plot.plot(
            x,
            y,
            pen=pg.mkPen("#f59e0b", width=2),
            connect="finite",
            name=label,
        )
        self.plot.setTitle(f"Perbandingan model ({self.frequency.currentText()})")
        if self.inner_tabs.indexOf(self.plot) >= 0:
            self.inner_tabs.setCurrentWidget(self.plot)
        self.plot_requested.emit()

    def _fill_table(self) -> None:
        series = self.current_aggregated()
        if series is None:
            return
        self.table_model.set_frame(series.rename(str(series.name)).rename_axis("Waktu").reset_index())

    def _fill_full_table(self) -> None:
        frame = self.reader.full_dataframe(5000)
        self.full_table_model.set_frame(frame)

    def _format_date(self, timestamp) -> str:
        value = pd.Timestamp(timestamp)
        if self.frequency.currentText() == "Tahunan":
            return value.strftime("%Y")
        if self.frequency.currentText() == "Bulanan":
            return value.strftime("%Y-%m")
        return value.strftime("%Y-%m-%d %H:%M") if value.hour or value.minute else value.strftime("%Y-%m-%d")

    def latest_timestamp(self):
        valid = self.current_series.dropna() if self.current_series is not None else pd.Series(dtype=float)
        return pd.Timestamp(valid.index[-1]) if not valid.empty else None

    def _populate_range_combo(self, _frequency: str) -> None:
        if not hasattr(self, "range_combo"):
            return
        current = self.range_combo.currentText()
        self.range_combo.blockSignals(True)
        self.range_combo.clear()
        self.range_combo.addItems(("Semua", "Kustom"))
        self.range_combo.setCurrentText(current if current in {"Semua", "Kustom"} else "Semua")
        self.range_combo.blockSignals(False)

    def _data_bounds(self) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        valid = self.current_series.dropna() if self.current_series is not None else pd.Series(dtype=float)
        if valid.empty:
            return None
        return pd.Timestamp(valid.index.min()), pd.Timestamp(valid.index.max())

    def _sync_range_ui(self, text: str) -> None:
        custom = text == "Kustom"
        for widget in (
            self.date_start_label,
            self.date_start,
            self.date_end_label,
            self.date_end,
        ):
            widget.setVisible(custom)
        self.date_start.setEnabled(custom)
        self.date_end.setEnabled(custom)
        if custom:
            self._set_from_dates()
            return
        self._window_limits = None
        self._plot()
        self._fill_table()

    def _set_from_dates(self) -> None:
        start = pd.Timestamp(self.date_start.date().toPython())
        end = pd.Timestamp(self.date_end.date().toPython()) + pd.Timedelta(days=1)
        self._window_limits = (start, end)
        self._plot()
        self._fill_table()

    def _on_range_combo_changed(self, text: str) -> None:
        self._sync_range_ui(text)

    def _on_date_changed(self, *_args) -> None:
        if self.range_combo.currentText() != "Kustom":
            self.range_combo.setCurrentText("Kustom")
            return
        self._set_from_dates()

    def _apply_range_bounds(self) -> None:
        bounds = self._data_bounds()
        if bounds is None:
            return
        start, end = bounds
        qmin = QDate(start.year, start.month, start.day)
        qmax = QDate(end.year, end.month, end.day)
        self.date_start.blockSignals(True)
        self.date_end.blockSignals(True)
        self.date_start.setDateRange(qmin, qmax)
        self.date_end.setDateRange(qmin, qmax)
        self.date_start.setDate(qmin)
        self.date_end.setDate(qmax)
        self.date_start.blockSignals(False)
        self.date_end.blockSignals(False)

    def _clip_to_window(self, series: pd.Series) -> pd.Series:
        window = self._window_limits
        if window is None:
            return series
        start, end = window
        return series.loc[start:end]

    def close_reader(self) -> None:
        self._request += 1
        for job in self._jobs:
            job.cancel()
        self._jobs.clear()
        if self.reader:
            self.reader.close()
            self.reader = None
        self.current_series = None
        self.current_variable = None
        self._window_limits = None
        self._populate_range_combo(self.frequency.currentText())
        self._sync_range_ui("Semua")
        self.plot.clear()
        self.table_model.set_frame(pd.DataFrame())
        self.full_table_model.set_frame(pd.DataFrame())


class ValidationTab(QWidget):
    def __init__(self, output_tab: OutputTab) -> None:
        super().__init__()
        self.output_tab = output_tab
        self.observation: pd.Series | None = None
        self.observation_frame: pd.DataFrame | None = None
        self.aligned: pd.DataFrame | None = None
        self.metrics: dict | None = None
        self.stations: list[str] = []
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        upload = QPushButton("Unggah Observasi (CSV/Excel)")
        upload.clicked.connect(self._upload)
        template = QPushButton("Download Template Excel")
        template.clicked.connect(self._download_template)
        self.frequency = QComboBox()
        self.frequency.addItems(("Harian", "Bulanan"))
        self.frequency.currentTextChanged.connect(self._compare_if_ready)
        self.station = QComboBox()
        self.station.currentIndexChanged.connect(self._station_changed)
        self.output_tab.data_loaded.connect(self._output_data_loaded)
        range_row = QHBoxLayout()
        self.range_combo = QComboBox()
        self.range_combo.addItems(("Semua", "Kustom"))
        self.range_combo.currentTextChanged.connect(self._range_changed)
        self.date_start_label = QLabel("Dari")
        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_end_label = QLabel("Sampai")
        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_start.dateChanged.connect(self._compare_if_ready)
        self.date_end.dateChanged.connect(self._compare_if_ready)
        for widget in (self.date_start_label, self.date_start, self.date_end_label, self.date_end):
            widget.hide()
        compare = QPushButton("Bandingkan dengan Simulasi Aktif")
        compare.clicked.connect(self._compare)
        self.export_button = QPushButton("Download Hasil Excel")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        toolbar.addWidget(template)
        toolbar.addWidget(upload)
        toolbar.addWidget(QLabel("Periode:"))
        toolbar.addWidget(self.frequency)
        toolbar.addWidget(QLabel("Stasiun:"))
        toolbar.addWidget(self.station)
        toolbar.addWidget(compare)
        toolbar.addWidget(self.export_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        range_row.addWidget(QLabel("Rentang:"))
        range_row.addWidget(self.range_combo)
        for widget in (self.date_start_label, self.date_start, self.date_end_label, self.date_end):
            range_row.addWidget(widget)
        range_row.addStretch()
        layout.addLayout(range_row)
        self.status = QLabel("Belum ada data observasi diunggah.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.date_axis = FreqAwareDateAxisItem(orientation="bottom")
        self.plot = pg.PlotWidget(title="Komparasi Simulasi vs Observasi", axisItems={"bottom": self.date_axis})
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True)
        layout.addWidget(self.plot, 1)
        metrics = QGridLayout()
        self.r2 = QLabel("R2: -")
        self.bias = QLabel("Bias: -")
        self.pbias = QLabel("PBIAS: -")
        self.count = QLabel("Jumlah pasangan data: -")
        metrics.addWidget(self.r2, 0, 0)
        metrics.addWidget(self.bias, 0, 1)
        metrics.addWidget(self.pbias, 1, 0)
        metrics.addWidget(self.count, 1, 1)
        layout.addLayout(metrics)
        self._output_data_loaded()

    def _upload(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Pilih file observasi", "", "Data (*.csv *.xlsx);;CSV (*.csv);;Excel (*.xlsx)"
        )
        if not path:
            return
        try:
            frame = pd.read_excel(path, sheet_name="Observasi") if Path(path).suffix.lower() == ".xlsx" else pd.read_csv(path)
            columns = {str(column).strip().lower(): column for column in frame.columns}
            time_column = next((columns[name] for name in ("time", "date", "datetime", "tanggal") if name in columns), frame.columns[0])
            station = self.station.currentText()
            value_column = next((column for column in frame.columns if str(column).strip().lower() == station.strip().lower()), None)
            value_column = value_column or next((columns[name] for name in ("observation", "observasi", "value", "nilai") if name in columns), next(column for column in frame.columns if column != time_column))
            self.observation = pd.Series(
                pd.to_numeric(frame[value_column], errors="coerce").to_numpy(),
                index=pd.to_datetime(frame[time_column], errors="coerce"),
                name="observasi",
            ).dropna()
            self.observation_frame = frame
            self.aligned = None
            self.metrics = None
            self.export_button.setEnabled(False)
            self.status.setText(f"Observasi dimuat: {len(self.observation)} baris dari {Path(path).name}")
            self._compare_if_ready()
        except Exception as exc:
            self.status.setText(f"Gagal memuat observasi: {exc}")

    def _download_template(self) -> None:
        if self.output_tab.current_series is None:
            self.status.setText("Muat output simulasi terlebih dahulu untuk membuat template bertanggal.")
            return
        series = self._selected_simulation()
        if series is None:
            self.status.setText("Tunggu data stasiun aktif selesai dimuat.")
            return
        dates = self._clip_range(series).dropna().index
        if len(dates) == 0:
            self.status.setText("Tidak ada tanggal output pada rentang terpilih.")
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self, "Simpan template observasi", "template_observasi.xlsx", "Excel (*.xlsx)"
        )
        if selected:
            export_validation_template(
                pd.DatetimeIndex(dates), self.stations or [self.station.currentText()],
                Path(selected).with_suffix(".xlsx"),
            )

    def _output_data_loaded(self) -> None:
        reader = self.output_tab.reader
        if reader is None:
            return
        variables = reader.list_variables()
        discharge = [name for name in variables if reader.is_discharge_variable(name)]
        self.stations = discharge or variables
        current = self.output_tab.current_variable
        self.station.blockSignals(True)
        self.station.clear()
        self.station.addItems(self.stations)
        if current in self.stations:
            self.station.setCurrentText(current)
        self.station.blockSignals(False)
        series = self._selected_simulation()
        if series is not None and not series.empty:
            first, last = pd.Timestamp(series.index.min()), pd.Timestamp(series.index.max())
            self.date_start.setDateRange(QDate(first.year, first.month, first.day), QDate(last.year, last.month, last.day))
            self.date_end.setDateRange(QDate(first.year, first.month, first.day), QDate(last.year, last.month, last.day))
            if self.range_combo.currentText() == "Semua":
                self.date_start.setDate(QDate(first.year, first.month, first.day))
                self.date_end.setDate(QDate(last.year, last.month, last.day))
        self._compare_if_ready()

    def _station_changed(self, _index: int) -> None:
        variable = self.station.currentText()
        index = self.output_tab.variable_combo.findData(variable)
        if index >= 0 and index != self.output_tab.variable_combo.currentIndex():
            self.output_tab.variable_combo.setCurrentIndex(index)
        else:
            self._compare_if_ready()

    def _range_changed(self, mode: str) -> None:
        custom = mode == "Kustom"
        for widget in (self.date_start_label, self.date_start, self.date_end_label, self.date_end):
            widget.setVisible(custom)
        self._compare_if_ready()

    def _selected_simulation(self) -> pd.Series | None:
        if self.output_tab.current_series is None:
            return None
        return OutputReader.aggregate(
            self.output_tab.current_series, FREQ_LABELS[self.frequency.currentText()]
        )

    def _clip_range(self, series: pd.Series) -> pd.Series:
        if self.range_combo.currentText() != "Kustom":
            return series
        start = pd.Timestamp(self.date_start.date().toPython())
        end = pd.Timestamp(self.date_end.date().toPython()) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        index = pd.DatetimeIndex(series.index)
        if self.frequency.currentText() == "Bulanan":
            periods = index.to_period("M")
            return series.loc[(periods.end_time >= start) & (periods.start_time <= end)]
        return series.loc[(index >= start) & (index <= end)]

    def _compare_if_ready(self, *_args) -> None:
        if self.observation is not None:
            self._compare()

    def _compare(self) -> None:
        self.aligned = None
        self.metrics = None
        self.export_button.setEnabled(False)
        frequency = FREQ_LABELS[self.frequency.currentText()]
        if self.output_tab.current_variable != self.station.currentText():
            self.status.setText("Tunggu data stasiun aktif selesai dimuat.")
            return
        simulation = self._selected_simulation()
        if simulation is None or self.observation is None:
            self.status.setText("Muat simulasi dan observasi terlebih dahulu.")
            return
        try:
            source = self.observation
            if self.observation_frame is not None:
                station_column = next((column for column in self.observation_frame.columns if str(column).strip().lower() == self.station.currentText().strip().lower()), None)
                if station_column is not None:
                    time_column = next((column for column in self.observation_frame.columns if str(column).strip().lower() in ("time", "date", "datetime", "tanggal")), self.observation_frame.columns[0])
                    source = pd.Series(pd.to_numeric(self.observation_frame[station_column], errors="coerce").to_numpy(), index=pd.to_datetime(self.observation_frame[time_column], errors="coerce")).dropna()
            observation = OutputReader.aggregate(source, frequency)
            simulation = self._clip_range(simulation)
            observation = self._clip_range(observation)
            aligned, metrics = validation_metrics(simulation, observation)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.aligned, self.metrics = aligned, metrics
        self.date_axis.set_frequency(self.frequency.currentText())
        self.plot.clear()
        x = [pd.Timestamp(value).timestamp() for value in aligned.index]
        self.plot.plot(x, aligned["simulation"].to_list(), pen=pg.mkPen("#22d3ee", width=2), name="Simulasi")
        self.plot.plot(x, aligned["observation"].to_list(), pen=pg.mkPen("#f59e0b", width=2), name="Observasi")
        self.plot.autoRange()
        self.r2.setText(f"R2: {metrics['r2']:.3f}")
        self.bias.setText(f"Bias: {metrics['bias']:.3f}")
        self.pbias.setText(f"PBIAS: {metrics['pbias']:.2f}%")
        self.count.setText(f"Jumlah pasangan data: {metrics['count']}")
        self.export_button.setEnabled(True)
        self.status.setText("Komparasi berhasil dihitung.")

    def _export(self) -> None:
        if self.aligned is None or self.metrics is None:
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self, "Simpan hasil validasi", "hasil_validasi.xlsx", "Excel (*.xlsx)"
        )
        if selected:
            export_validation_excel(
                self.aligned, self.metrics, Path(selected).with_suffix(".xlsx"),
                self.frequency.currentText(), self.station.currentText(),
            )


def _cached_ssfi(path: Path, cache_dir: Path) -> tuple[pd.DataFrame, bool]:
    path = path.resolve()
    stat = path.stat()
    signature = {"version": 2, "scales": list(SSFI_SCALES), "aggregation": "monthly_mean",
                 "source": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    cache = cache_dir / f"{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]}.csv"
    try:
        with cache.open(encoding="utf-8") as stream:
            metadata = json.loads(stream.readline())
            if isinstance(metadata, dict) and all(metadata.get(key) == value for key, value in signature.items()):
                frame = pd.read_csv(stream, header=[0, 1], index_col=0, parse_dates=True)
                if (list(frame.shape) == metadata.get("shape") and
                    isinstance(frame.index, pd.DatetimeIndex) and
                    set(frame.columns.get_level_values(0)) == {str(n) for n in SSFI_SCALES} and
                    all(pd.api.types.is_numeric_dtype(dtype) for dtype in frame.dtypes)):
                    return frame, True
    except (OSError, ValueError, TypeError):
        pass

    reader = OutputReader(path)
    try:
        frame = build_ssfi_table(reader, scales=SSFI_SCALES)
    finally:
        reader.close()
    try:
        current = path.stat()
    except OSError:
        return frame, False
    if not frame.empty and (current.st_size, current.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns):
        temporary = None
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_dir, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps({**signature, "shape": list(frame.shape)}) + "\n")
                frame.to_csv(stream)
            temporary.replace(cache)
        except OSError:
            pass  # Computation still succeeds when cache storage is unavailable.
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return frame, False


class SSFITab(QWidget):
    show_requested = Signal(str, dict)
    hide_requested = Signal()
    state_changed = Signal()

    def __init__(self, output_tab: OutputTab, cache_dir: Path | None = None) -> None:
        super().__init__()
        self.output_tab = output_tab
        self.cache_dir = cache_dir
        self.frame: pd.DataFrame | None = None
        self.frames = {}
        self._cached = False
        self.current_values = {}
        self._request = 0
        self._job = None
        outer = QVBoxLayout(self)
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.compute = QPushButton("Hitung SSFI dari Output")
        self.compute.clicked.connect(self._compute)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(120)
        self.progress.hide()
        self.layer = QComboBox()
        compact_combo(self.layer, 260, 18)
        self.layer.currentTextChanged.connect(self._selection_changed)
        self.month = QComboBox()
        compact_combo(self.month, 125, 10)
        self.month.currentIndexChanged.connect(self._selection_changed)
        self.year = QComboBox()
        compact_combo(self.year, 100, 6)
        self.year.currentIndexChanged.connect(self._selection_changed)
        self.show = QPushButton("Tampilkan ke Peta")
        self.show.setCheckable(True)
        self.show.setEnabled(False)
        self.show.toggled.connect(self._show_toggled)
        for widget in (self.compute, self.show, self.progress):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        selectors = QHBoxLayout()
        selectors.setContentsMargins(0, 0, 0, 0)
        selectors.setSpacing(6)
        self.scale = QComboBox()
        self.scale.setAccessibleName("Jenis SSFI")
        for n in SSFI_SCALES:
            self.scale.addItem(f"SSFI-{n}", n)
        self.scale.currentIndexChanged.connect(self._scale_changed)
        for widget in (QLabel("Jenis SSFI:"), self.scale, QLabel("Layer Basin:"), self.layer, QLabel("Bulan:"), self.month, QLabel("Tahun:"), self.year):
            selectors.addWidget(widget)
        selectors.addStretch(1)
        layout.addLayout(selectors)
        self.status = QLabel("Muat output debit di tab Output, lalu klik 'Hitung SSFI dari Output'.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        legend = QGroupBox("Legenda Klasifikasi SSFI")
        legend_layout = QGridLayout(legend)
        entries = [*SSFI_CLASSES, (NO_DATA_LABEL, NO_DATA_COLOR)]
        for row, (label, color) in enumerate(entries):
            swatch = QLabel()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(f"background-color:{color}; border:1px solid #555;")
            legend_layout.addWidget(swatch, row // 3, (row % 3) * 2)
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            legend_layout.addWidget(label_widget, row // 3, (row % 3) * 2 + 1)
        layout.addWidget(legend)
        controls_scroll = scroll_panel(controls)
        controls_scroll.setMinimumHeight(100)
        controls_scroll.setMaximumHeight(230)
        outer.addWidget(controls_scroll)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(("ID Stasiun/Basin", "Nilai SSFI", "Klasifikasi"))
        self.table.setMinimumHeight(140)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.table, 1)

    def set_basin_layers(self, names: list[str]) -> None:
        current = self.layer.currentText()
        self.layer.blockSignals(True)
        self.layer.clear()
        self.layer.addItems(names)
        for index, name in enumerate(names):
            self.layer.setItemData(index, name, Qt.ItemDataRole.ToolTipRole)
        self.layer.blockSignals(False)
        preferred = current if current in names else next((name for name in names if "basin" in name.lower()), "")
        self.layer.setCurrentText(preferred)
        self.state_changed.emit()

    def reset(self) -> None:
        self._request += 1
        if self._job:
            self._job.cancel()
            self._job = None
        self.compute.setEnabled(True)
        self.progress.hide()
        self.show.setChecked(False)
        self.show.setEnabled(False)
        self.frame = None
        self.frames = {}
        self.current_values = {}
        self.month.clear()
        self.year.clear()
        self.table.setRowCount(0)
        self.state_changed.emit()

    def _compute(self) -> None:
        if not self.output_tab.reader:
            self.status.setText("Muat output debit terlebih dahulu di tab Output.")
            return
        self.reset()
        self._request += 1
        request = self._request
        path = self.output_tab.reader.path
        cache_dir = self.cache_dir or path.parent / ".hydrohub" / "ssfi"
        if self._job:
            self._job.cancel()
        self.compute.setEnabled(False)
        self.show.setEnabled(False)
        self.progress.show()
        self.status.setText("Mengubah debit menjadi rata-rata bulanan; menghitung SSFI-1/3/9/12 di background…")

        def calculate():
            return _cached_ssfi(path, cache_dir)

        def completed(result):
            if request != self._request:
                return
            self._job = None
            self.compute.setEnabled(True)
            self.progress.hide()
            self._computed(*result)

        def failed(message):
            if request != self._request:
                return
            self._job = None
            self.compute.setEnabled(True)
            self.progress.hide()
            self.status.setText(f"Gagal menghitung SSFI: {message}")
            self.state_changed.emit()

        self._job = BackgroundJob(self, calculate, completed, failed)
        self._job.start()

    def _computed(self, frame: pd.DataFrame, cached: bool = False) -> None:
        self._cached = cached
        if isinstance(frame.columns, pd.MultiIndex):
            self.frames = {int(n): frame.xs(n, axis=1, level=0) for n in frame.columns.get_level_values(0).unique()}
            frame = self.frames[self.scale.currentData()]
        elif not self.frames:
            self.frames = {self.scale.currentData(): frame}
        self.frame = frame
        self.current_values = {}
        self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(("ID Stasiun/Basin", f"Nilai SSFI-{self.scale.currentData()}", "Klasifikasi"))
        periods = available_periods(self.frame)
        if not periods:
            self.show.setChecked(False)
            self.show.setEnabled(False)
            self.month.clear()
            self.year.clear()
            self.status.setText("Data debit tidak cukup panjang untuk menghitung SSFI (minimal 30 bulan).")
            self.state_changed.emit()
            return
        if not self.frame.notna().to_numpy().any():
            self.show.setChecked(False)
            self.show.setEnabled(False)
            self.status.setText(
                "Semua nilai SSFI kosong. Output harus memiliki minimal 30 periode bulanan "
                f"valid setelah rolling {self.scale.currentData()} bulan dan sedikitnya dua nilai debit positif."
            )
            self.state_changed.emit()
            return
        self.year.blockSignals(True)
        self.month.blockSignals(True)
        self.year.clear()
        self.year.addItems([str(value) for value in sorted({year for year, _month in periods})])
        self.month.clear()
        for month in range(1, 13):
            self.month.addItem(MONTH_NAMES_ID[month], month)
        last_year, last_month = periods[-1]
        self.year.setCurrentText(str(last_year))
        self.month.setCurrentIndex(last_month - 1)
        self.year.blockSignals(False)
        self.month.blockSignals(False)
        self.show.setEnabled(True)
        self.status.setText(
            f"SSFI-{self.scale.currentData()} {'dimuat dari cache' if cached else 'dihitung'} untuk {self.frame.shape[1]} stasiun, {len(periods)} periode bulanan "
            f"({periods[0][1]:02d}/{periods[0][0]}-{periods[-1][1]:02d}/{periods[-1][0]})."
        )
        self._update_table()
        self.state_changed.emit()

    def _scale_changed(self, *_args) -> None:
        if not self.frames:
            return
        period = self._selected_period()
        self._computed(self.frames.get(self.scale.currentData(), pd.DataFrame()), self._cached)
        if period and self.year.findText(str(period[0])) >= 0:
            self.year.setCurrentText(str(period[0]))
            self.month.setCurrentIndex(self.month.findData(period[1]))
        self._selection_changed()

    def _selected_period(self):
        if self.frame is None or not self.year.count() or not self.month.count():
            return None
        return int(self.year.currentText()), int(self.month.currentData())

    def _selection_changed(self, *_args) -> None:
        if self.frame is None:
            return
        self._update_table()
        if self.show.isChecked():
            self._emit_show()
        self.state_changed.emit()

    def _update_table(self) -> None:
        period = self._selected_period()
        self.current_values = classify_period(self.frame, *period) if period else {}
        rows = sorted(self.current_values.items(), key=lambda item: (0, int(item[0])) if item[0].isdigit() else (1, item[0]))
        self.table.setRowCount(len(rows))
        for row, (station, info) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(station))
            self.table.setItem(row, 1, QTableWidgetItem("-" if info["value"] is None else f"{info['value']:.3f}"))
            classification = QTableWidgetItem(info["label"])
            classification.setBackground(QColor(info["color"]))
            self.table.setItem(row, 2, classification)

    def _show_toggled(self, checked: bool) -> None:
        if checked:
            if not self.current_values or not self.layer.currentText():
                self.show.blockSignals(True)
                self.show.setChecked(False)
                self.show.blockSignals(False)
                return
            self._emit_show()
        else:
            self.hide_requested.emit()

    def _emit_show(self) -> None:
        period = self._selected_period()
        if not period or not self.layer.currentText():
            return
        self.status.setText(f"Menampilkan SSFI-{self.scale.currentData()} {MONTH_NAMES_ID[period[1]]} {period[0]} pada layer '{self.layer.currentText()}' di peta.")
        self.show_requested.emit(self.layer.currentText(), dict(self.current_values))


class ExportTab(QWidget):
    gpkg_requested = Signal()
    excel_requested = Signal()
    shapefile_requested = Signal()
    png_requested = Signal()
    discharge_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._has_ssfi = False
        self._has_basin = False
        self._has_output = False
        layout = QVBoxLayout(self)
        description = QLabel(
            "Ekspor basin, debit lengkap, dan skala SSFI terpilih ke GIS, spreadsheet, atau gambar peta."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        form = QFormLayout()
        self.gpkg = QPushButton("Ekspor Basin + SSFI ke GeoPackage")
        self.excel = QPushButton("Ekspor Seluruh SSFI ke Excel")
        self.shapefile = QPushButton("Ekspor Basin ke Shapefile ZIP")
        self.png = QPushButton("Ekspor Peta SSFI ke PNG")
        self.discharge = QPushButton("Ekspor Debit ke CSV / Excel")
        self.gpkg.clicked.connect(self.gpkg_requested.emit)
        self.excel.clicked.connect(self.excel_requested.emit)
        self.shapefile.clicked.connect(self.shapefile_requested.emit)
        self.png.clicked.connect(self.png_requested.emit)
        self.discharge.clicked.connect(self.discharge_requested.emit)
        form.addRow("GeoPackage", self.gpkg)
        form.addRow("Excel", self.excel)
        form.addRow("Shapefile", self.shapefile)
        form.addRow("Gambar peta", self.png)
        form.addRow("Debit", self.discharge)
        layout.addLayout(form)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("Hitung SSFI dan pilih layer basin untuk mengaktifkan opsi export.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.set_available(False, False)

    def set_available(self, has_ssfi: bool, has_basin: bool, has_output: bool = False) -> None:
        self._has_ssfi = has_ssfi
        self._has_basin = has_basin
        self._has_output = has_output
        self.gpkg.setEnabled(not self._busy and has_ssfi and has_basin)
        self.excel.setEnabled(not self._busy and has_ssfi)
        self.shapefile.setEnabled(not self._busy and has_basin)
        self.png.setEnabled(not self._busy and has_ssfi and has_basin)
        self.discharge.setEnabled(not self._busy and has_output)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        self.set_available(self._has_ssfi, self._has_basin, self._has_output)
        if message:
            self.status.setText(message)
