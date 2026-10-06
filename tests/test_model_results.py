import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import xarray as xr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hydrohub.core.model_artifacts import (
    discover_model_artifacts,
    discover_wflow_models,
)
from hydrohub.core.project_manager import ProjectManager
from hydrohub.results.analysis import (
    NO_DATA_LABEL,
    OutputReader,
    build_ssfi_table,
    classify_period,
    rank_output_files,
    validation_metrics,
)
from hydrohub.results.spatial import ForcingReader, StaticgeomReader
from hydrohub.results.export import (
    export_basin_shapefile,
    export_basin_ssfi_gpkg,
    export_basin_ssfi_png,
    export_ssfi_excel,
    export_validation_excel,
    export_validation_template,
)
from hydrohub.ui.background import BackgroundJob
from hydrohub.ui.result_pages import ForcingPage, ResultsPage
from hydrohub.ui.result_widgets import OutputTab, SSFITab, ValidationTab
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


class ModelResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = ProjectManager().create(Path(self.temp.name), "Results Project")
        self.model = self.project.root / "models" / "wflow" / "model_a"
        self.model.mkdir(parents=True)
        (self.model / "wflow_sbm.toml").write_text(
            '[input]\npath_forcing = "forcing.nc"\n\n'
            '[output.csv]\npath = "run_default/output.csv"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wait_output(self, output):
        deadline = time.monotonic() + 10
        while output.current_series is None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNotNone(output.current_series, output.plot.plotItem.titleLabel.text)

    def wait_ssfi(self, ssfi):
        deadline = time.monotonic() + 10
        while ssfi._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(ssfi._job, ssfi.status.text())

    def test_discovers_model_forcing_geometry_and_outputs(self) -> None:
        forcing = self.model / "forcing.nc"
        forcing.write_bytes(b"fixture")
        geometry = self.model / "staticgeoms"
        geometry.mkdir()
        (geometry / "outlets.geojson").write_text("{}", encoding="utf-8")
        run = self.model / "run_default"
        run.mkdir()
        (run / "output.csv").write_text("time,river_q_1\n2020-01-01,2\n", encoding="utf-8")

        self.assertEqual(discover_wflow_models(self.project.root), [self.model.resolve()])
        artifacts = discover_model_artifacts(self.project.root, self.model)

        self.assertEqual(artifacts.forcing, forcing.resolve())
        self.assertEqual(artifacts.staticgeoms, ((geometry / "outlets.geojson").resolve(),))
        self.assertIn((run / "output.csv").resolve(), artifacts.outputs)

    def test_output_ranking_timeseries_validation_and_ssfi(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        dates = pd.date_range("2000-01-01", periods=40, freq="MS")
        csv = run / "output.csv"
        pd.DataFrame(
            {"time": dates, "river_q_12": np.linspace(1, 40, 40), "temperature": 20}
        ).to_csv(csv, index=False)
        empty = run / "output.nc"
        xr.Dataset().to_netcdf(empty)

        self.assertEqual(rank_output_files((empty, csv))[0], csv)
        reader = OutputReader(csv)
        try:
            series = reader.get_timeseries("river_q_12")
            self.assertEqual(len(series), 40)
            aligned, metrics = validation_metrics(series, series)
            self.assertEqual(len(aligned), 40)
            self.assertAlmostEqual(metrics["r2"], 1.0)
            self.assertAlmostEqual(metrics["bias"], 0.0)
            ssfi = build_ssfi_table(reader)
            self.assertEqual(list(ssfi.columns), ["12"])
            self.assertEqual(len(ssfi), 40)
        finally:
            reader.close()

    def test_forcing_reader_renders_spatial_frame(self) -> None:
        forcing = self.model / "forcing.nc"
        dataset = xr.Dataset(
            {
                "precip": (
                    ("time", "latitude", "longitude"),
                    np.arange(24, dtype=float).reshape(2, 3, 4),
                )
            },
            coords={
                "time": pd.date_range("2020-01-01", periods=2),
                "latitude": [-7.0, -6.5, -6.0],
                "longitude": [106.0, 106.5, 107.0, 107.5],
            },
        )
        dataset.to_netcdf(forcing)

        reader = ForcingReader(forcing)
        try:
            self.assertEqual(reader.list_variables(), ["precip"])
            self.assertEqual(reader.frame_count(), 2)
            image, bounds, value_range = reader.render_frame(
                "precip", 0, 512, "Blues", reader.value_range("precip")
            )
            self.assertTrue(image.startswith("data:image/png;base64,"))
            self.assertEqual(len(bounds), 2)
            self.assertEqual(value_range, (0.0, 23.0))
        finally:
            reader.close()

    def test_forcing_reader_chunked_preview_and_global_range(self) -> None:
        forcing = self.model / "forcing_chunks.nc"
        data = np.arange(36, dtype=float).reshape(9, 2, 2)
        data[0] = np.nan
        xr.Dataset(
            {"precip": (("time", "latitude", "longitude"), data)},
            coords={"time": pd.date_range("2020-01-01", periods=9),
                    "latitude": [-7.0, -6.0], "longitude": [106.0, 107.0]},
        ).to_netcdf(forcing)
        reader = ForcingReader(forcing)
        try:
            preview = reader.as_table("precip", limit=7)
            self.assertEqual(len(preview), 7)
            self.assertTrue(np.isnan(preview["precip"].iloc[0]))
            self.assertEqual(preview["precip"].iloc[5], 21.5)
            self.assertEqual(reader.value_range("precip"), (4.0, 35.0))
        finally:
            reader.close()

    def test_forcing_reader_reports_missing_values_on_active_cells(self) -> None:
        forcing = self.model / "forcing_missing.nc"
        staticmaps = self.model / "staticmaps.nc"
        data = np.ones((3, 2, 2))
        data[1, 0, 0] = np.nan
        data[2, 1, 1] = np.nan
        coords = {"time": pd.date_range("2010-12-30", periods=3), "y": [1.0, 0.0], "x": [0.0, 1.0]}
        xr.Dataset({"pet": (("time", "y", "x"), data)}, coords=coords).to_netcdf(forcing)
        xr.Dataset(
            {"wflow_subcatch": (("y", "x"), [[1, 1], [1, 0]])},
            coords={"y": [1.0, 0.0], "x": [0.0, 1.0]},
        ).to_netcdf(staticmaps)

        reader = ForcingReader(forcing)
        try:
            missing, active_count = reader.missing_active_cells("pet", staticmaps)
            self.assertEqual(active_count, 3)
            self.assertEqual(list(missing["time"]), [pd.Timestamp("2010-12-31")])
            self.assertEqual(list(missing["missing_active_cells"]), [1])
            self.assertAlmostEqual(missing["coverage_pct"].iloc[0], 200 / 3)
        finally:
            reader.close()

    def test_forcing_page_load_shows_progress_success_and_error(self) -> None:
        forcing = self.model / "forcing_page.nc"
        xr.Dataset(
            {"precip": (("time", "latitude", "longitude"), np.ones((30, 2, 2)))},
            coords={
                "time": pd.date_range("2020-01-01", periods=30),
                "latitude": [-7.0, -6.0],
                "longitude": [106.0, 107.0],
            },
        ).to_netcdf(forcing)
        page = ForcingPage(lambda: None, None)
        self.assertEqual([page.tabs.tabText(index) for index in range(page.tabs.count())], ["Inspector", "Missing values", "Variables and time"])
        self.assertEqual(page.forcing_splitter.orientation(), Qt.Orientation.Vertical)
        self.assertFalse(page.forcing_splitter.childrenCollapsible())
        self.assertEqual(page.map.minimumHeight(), 240)
        with patch.object(page.map, "fit_bounds") as fit_bounds:
            page.load_file(forcing)
            self.assertFalse(page.progress.isHidden())
            deadline = time.monotonic() + 10
            while (page._loading or page._render_job) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            fit_bounds.assert_called_once()
        self.assertTrue(page.progress.isHidden())
        self.assertEqual(page.variables.currentText(), "precip")
        self.assertEqual(page.table.rowCount(), 24)
        self.assertEqual(page.slider.maximum(), 29)
        self.assertEqual(page._shown_variable, "precip")
        self.assertEqual(page.variable_info.rowCount(), 1)
        self.assertIn("30 timesteps", page.time_info.text())
        page.check_missing_button.click()
        deadline = time.monotonic() + 10
        while page._missing_job and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(page._missing_job)
        self.assertIn("No missing values across 4 cells", page.missing_status.text())
        first_reader = page._reader
        self.assertIsNone(page._value_range)
        self.assertTrue(page.show_button.isEnabled())
        self.assertTrue(page.play.isEnabled())
        self.assertIn("Frame shown:", page.status.text())
        page.show_button.click()
        deadline = time.monotonic() + 10
        while page._render_job and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(page._render_job)
        self.assertIs(page._reader, first_reader)
        self.assertIn("Frame shown:", page.status.text())

        with patch.object(ForcingReader, "as_table", side_effect=ValueError("invalid values")):
            page._variable_changed("precip")
            deadline = time.monotonic() + 10
            while not page.progress.isHidden() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertEqual(page.status.objectName(), "statusError")
        self.assertIn("Forcing preview failed:", page.status.text())

        page._variable_changed("precip")
        deadline = time.monotonic() + 10
        while (page._loading or page._render_job) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        with patch.object(ForcingReader, "render_frame", side_effect=ValueError("render failed")):
            page.slider.setValue(1)
            page._render()
            deadline = time.monotonic() + 10
            while not page.progress.isHidden() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertEqual(page.status.objectName(), "statusError")
        self.assertIn("Forcing frame unavailable:", page.status.text())

        with patch.object(ForcingReader, "value_range", side_effect=ValueError("no finite values")):
            self.assertTrue(page.play.isEnabled())
            page.play.click()
            deadline = time.monotonic() + 10
            while not page.progress.isHidden() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIn("Forcing color scale unavailable:", page.status.text())

        invalid = self.model / "invalid_forcing.nc"
        invalid.write_text("not a NetCDF file", encoding="utf-8")
        with patch.object(first_reader, "close", wraps=first_reader.close) as closed:
            page.load_file(invalid)
            deadline = time.monotonic() + 10
            while not page.progress.isHidden() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertTrue(BackgroundJob._pool.waitForDone(10000))
            closed.assert_called_once()
        self.assertTrue(page.progress.isHidden())
        self.assertEqual(page.status.objectName(), "statusError")
        self.assertIn("Forcing could not be loaded:", page.status.text())
        self.assertIsNone(page._forcing_path)
        self.assertEqual(page.variables.count(), 0)
        page.load_file(forcing)
        deadline = time.monotonic() + 10
        while (page._loading or page._render_job) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(page._forcing_path, forcing.resolve())
        page.load_file(forcing)  # Selecting same file reloads it.
        self.assertFalse(page.progress.isHidden())
        deadline = time.monotonic() + 10
        while (page._loading or page._render_job) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(page.table.rowCount(), 24)
        page.speed.setValue(5000)
        page._toggle_playback()
        deadline = time.monotonic() + 10
        while page._value_range is None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(page._value_range, (1.0, 2.0))
        self.assertTrue(page.timer.isActive())
        self.assertTrue(page.slider.isEnabled())
        self.assertTrue(page.progress.isHidden())
        deadline = time.monotonic() + 10
        while (page._render_job or page._prefetch_job) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        next_frame = ("precip", 1, min(page.resolution.value(), max(256, int(page.map.width() * page.map.devicePixelRatioF()))),
                      page.colormap.currentText(), page._value_range)
        self.assertIn(next_frame, page._frame_cache)
        page._advance()
        self.assertEqual(page.slider.value(), 1)
        self.assertIsNone(page._render_job)
        page._stop_playback()
        while page._render_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        page.close_resources()
        self.assertTrue(BackgroundJob._pool.waitForDone(10000))
        page.close()

    def test_forcing_csv_loads_table_without_spatial_frame(self) -> None:
        path = self.model / "forcing.csv"
        pd.DataFrame({"time": ["2020-01-01"], "precip": [12.0]}).to_csv(path, index=False)
        page = ForcingPage(lambda: None, None)
        page.load_file(path)
        deadline = time.monotonic() + 10
        while page._loading and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(page.table.rowCount(), 1)
        self.assertFalse(page.show_button.isEnabled())
        self.assertFalse(page.play.isEnabled())
        self.assertIsNone(page._shown_variable)
        page.resize(1100, 700)
        page.show()
        self.app.processEvents()
        page.forcing_splitter.moveSplitter(260, 1)
        self.app.processEvents()
        self.assertGreaterEqual(page.forcing_splitter.sizes()[0], 240)
        self.assertGreaterEqual(page.forcing_splitter.sizes()[1], 140)
        page.close_resources()
        self.assertTrue(BackgroundJob._pool.waitForDone(10000))
        page.close()

    def test_ssfi_active_layer_updates_when_month_changes(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        pd.DataFrame(
            {
                "time": pd.date_range("2000-01-01", periods=40, freq="MS"),
                "river_q_12": np.linspace(1, 40, 40),
            }
        ).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)
        ssfi = SSFITab(output)
        ssfi.set_basin_layers(["basins"])
        emitted = []
        ssfi.show_requested.connect(lambda layer, values: emitted.append((layer, values)))

        ssfi._compute()
        self.wait_ssfi(ssfi)
        ssfi.show.setChecked(True)
        initial_count = len(emitted)
        ssfi.month.setCurrentIndex(max(0, ssfi.month.currentIndex() - 1))

        self.assertGreater(initial_count, 0)
        self.assertGreater(len(emitted), initial_count)
        self.assertEqual(emitted[-1][0], "basins")
        self.assertIn("12", emitted[-1][1])
        output.close_reader()

    def test_ssfi_reference_threshold_labels(self) -> None:
        frame = pd.DataFrame(
            {
                "extreme_dry": [-2.0],
                "severe_dry": [-1.5],
                "moderate_dry": [-1.0],
                "normal": [0.0],
                "moderate_wet": [1.0],
                "severe_wet": [1.5],
                "extreme_wet": [2.0],
                "missing": [np.nan],
            },
            index=pd.to_datetime(["2020-01-01"]),
        )

        values = classify_period(frame, 2020, 1)

        self.assertEqual(values["extreme_dry"]["label"], "Sangat Kering")
        self.assertEqual(values["severe_dry"]["label"], "Kering Berat")
        self.assertEqual(values["moderate_wet"]["label"], "Basah Sedang")
        self.assertEqual(values["extreme_wet"]["label"], "Sangat Basah")
        self.assertEqual(values["missing"]["label"], NO_DATA_LABEL)

    def test_output_model_comparison_overlays_without_concatenating(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        dates = pd.date_range("2000-01-01", periods=4, freq="D")
        pd.DataFrame({"time": dates, "river_q_1": [1, 2, 3, 4]}).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)

        output.add_comparison(pd.Series([4, 3, 2, 1], index=dates), "Model B")

        self.assertEqual(len(output.plot.listDataItems()), 2)
        self.assertEqual(len(output.reader.get_timeseries("river_q_1")), 4)
        output.close_reader()

    def test_output_range_modes_clip_plot_and_comparison(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        dates = pd.date_range("2000-01-01", periods=400, freq="D")
        pd.DataFrame({"time": dates, "river_q_1": np.sin(np.arange(400))}).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)

        self.assertEqual(
            [output.range_combo.itemText(i) for i in range(output.range_combo.count())],
            ["Semua", "Kustom"],
        )
        self.assertEqual(output.range_combo.currentText(), "Semua")
        self.assertIsNone(output._window_limits)
        self.assertTrue(output.date_start.isHidden())
        self.assertTrue(output.date_end.isHidden())

        output.range_combo.setCurrentText("Kustom")
        self.assertFalse(output.date_start.isHidden())
        self.assertFalse(output.date_end.isHidden())
        output.date_start.setDate(output.date_start.maximumDate().addDays(-30))
        output.date_end.setDate(output.date_end.maximumDate())
        self.assertIsNotNone(output._window_limits)
        start, limit = output._window_limits
        end = pd.Timestamp(dates[-1])
        self.assertEqual(start, end - pd.Timedelta(days=30))
        self.assertEqual(limit, end + pd.Timedelta(days=1))
        xmin, xmax = output.plot.getViewBox().viewRange()[0]
        self.assertAlmostEqual(xmax, limit.timestamp(), delta=86_400)
        self.assertLessEqual(xmin, xmax)

        output.add_comparison(pd.Series(np.ones(400), index=dates), "Model B")
        self.assertEqual(len(output.plot.listDataItems()), 2)
        xmin_c, xmax_c = output.plot.getViewBox().viewRange()[0]
        self.assertEqual(xmin_c, xmin)
        self.assertEqual(xmax_c, xmax)

        output.range_combo.setCurrentText("Semua")
        self.assertIsNone(output._window_limits)
        self.assertTrue(output.date_start.isHidden())
        self.assertTrue(output.date_end.isHidden())
        curve = output.plot.listDataItems()[0]
        self.assertEqual(len(curve.xData), 400)
        xmin_all, xmax_all = output.plot.getViewBox().viewRange()[0]
        self.assertAlmostEqual(xmin_all, pd.Timestamp(dates[0]).timestamp(), delta=86_400)
        self.assertAlmostEqual(xmax_all, end.timestamp(), delta=86_400)
        output.close_reader()

    def test_output_manual_dates_set_kustom_window(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        dates = pd.date_range("2020-01-01", periods=90, freq="D")
        pd.DataFrame({"time": dates, "river_q_1": list(range(90))}).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)

        output.date_start.setDate(output.date_start.minimumDate())
        output.date_end.setDate(output.date_end.maximumDate())
        output._on_date_changed()
        self.assertEqual(output.range_combo.currentText(), "Kustom")
        self.assertIsNotNone(output._window_limits)
        xmin, xmax = output.plot.getViewBox().viewRange()[0]
        self.assertAlmostEqual(xmin, pd.Timestamp(dates[0]).timestamp(), delta=86_400)
        self.assertAlmostEqual(xmax, pd.Timestamp(dates[-1]).timestamp(), delta=2 * 86_400)
        output.close_reader()

    def test_output_and_validation_graphs_keep_visible_curves(self) -> None:
        csv = self.model / "output.csv"
        dates = pd.date_range("2020-01-01", periods=4, freq="D")
        pd.DataFrame({"time": dates, "river_q_1": [1, 2, 3, 4]}).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)

        self.assertIs(output.inner_tabs.currentWidget(), output.plot)
        self.assertEqual(len(output.plot.listDataItems()), 1)
        validation = ValidationTab(output)
        validation.observation = pd.Series([1, 2, 3, 4], index=dates)
        validation._compare()
        self.assertEqual(len(validation.plot.listDataItems()), 2)
        self.assertEqual(validation.status.text(), "Komparasi berhasil dihitung.")
        output.close_reader()

    def test_validation_monthly_alignment_and_excel_template_export(self) -> None:
        dates = pd.date_range("2020-01-01", periods=31, freq="D")
        csv = self.model / "output.csv"
        pd.DataFrame({
            "time": dates,
            "river_q_1": np.arange(31, dtype=float),
            "river_q_2": np.arange(31, dtype=float) * 2,
        }).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)
        validation = ValidationTab(output)
        self.assertEqual(validation.range_combo.currentText(), "Semua")
        self.assertEqual(validation.stations, ["river_q_1", "river_q_2"])
        validation.observation = pd.Series(np.arange(31, dtype=float), index=dates)
        validation.frequency.setCurrentText("Bulanan")
        validation._compare()
        self.assertEqual(validation.metrics["count"], 1)
        self.assertEqual(validation.aligned.index[0], pd.Timestamp("2020-01-01"))

        template = self.model / "template.xlsx"
        result = self.model / "validation.xlsx"
        export_validation_template(pd.DatetimeIndex(dates[:1]), validation.stations, template)
        export_validation_excel(validation.aligned, validation.metrics, result, "Bulanan")
        template_frame = pd.read_excel(template, sheet_name="Observasi")
        self.assertEqual(list(template_frame.columns), ["tanggal", "river_q_1", "river_q_2"])
        self.assertEqual(pd.to_datetime(template_frame["tanggal"]).iloc[0], pd.Timestamp("2020-01-01"))
        self.assertEqual(list(pd.read_excel(result, sheet_name="Validation").columns), [
            "tanggal", "simulation", "observation", "residual"
        ])
        self.assertEqual(pd.read_excel(result, sheet_name="Metrics").loc[0, "frekuensi"], "Bulanan")
        upload = self.model / "observasi.xlsx"
        pd.DataFrame({"tanggal": dates, "observasi": np.arange(31, dtype=float)}).to_excel(
            upload, sheet_name="Observasi", index=False
        )
        with patch("hydrohub.ui.result_widgets.QFileDialog.getOpenFileName", return_value=(str(upload), "")):
            validation._upload()
        self.assertEqual(len(validation.observation), 31)
        validation.frequency.setCurrentText("Harian")
        validation.range_combo.setCurrentText("Kustom")
        validation.date_start.setDate(validation.date_start.date().addDays(1))
        validation.date_end.setDate(validation.date_start.date().addDays(2))
        self.assertEqual(len(validation._clip_range(validation._selected_simulation())), 3)
        validation.station.setCurrentText("river_q_2")
        deadline = time.monotonic() + 5
        while output.current_variable != "river_q_2" and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(output.current_variable, "river_q_2")
        output.close_reader()

    def test_ssfi_geojson_matches_integer_id_stored_as_float(self) -> None:
        geometry = self.model / "staticgeoms"
        geometry.mkdir()
        basin_path = geometry / "basins.geojson"
        basin_path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"value": 12.0},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[106, -7], [107, -7], [107, -6], [106, -7]]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {"value": 13.0},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[107, -7], [108, -7], [108, -6], [107, -7]]],
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        reader = StaticgeomReader((basin_path,))
        values = {"12": {"value": -1.75, "label": "Kering Berat", "color": "#E31A1C"}}

        geojson = reader.geojson(basin_path, values)

        matched, unmatched = [feature["properties"] for feature in geojson["features"]]
        self.assertEqual(matched["__fill_color"], "#E31A1C")
        self.assertEqual(matched["__ssfi_value"], -1.75)
        self.assertIn("Basin/Stasiun 12", matched["__popup_html"])
        self.assertEqual(unmatched["__fill_color"], "#BDBDBD")
        self.assertEqual(unmatched["__ssfi_label"], NO_DATA_LABEL)

    def test_results_ssfi_layer_fits_map_and_reports_matches(self) -> None:
        class FakeReader:
            @staticmethod
            def path_for_layer(_name):
                return Path("basins.geojson")

            @staticmethod
            def geojson(_path, _values):
                return {
                    "type": "FeatureCollection",
                    "features": [{"properties": {"__ssfi_value": 0.25}}],
                }

        class FakeMap:
            def __init__(self):
                self.call = None

            def add_geojson(self, *args, **kwargs):
                self.call = (args, kwargs)

        messages = []
        target = SimpleNamespace(
            staticgeom_reader=FakeReader(),
            map=FakeMap(),
            ssfi_tab=SimpleNamespace(status=SimpleNamespace(setText=messages.append)),
            _show_map=lambda: None,
        )

        ResultsPage._show_ssfi(target, "basins", {"12": {}})

        self.assertEqual(target.map.call[0][0], "SSFI")
        self.assertEqual(target.map.call[1], {"choropleth": True, "fit": True})
        self.assertIn("1 dari 1", messages[-1])

    def test_ssfi_rejects_output_shorter_than_thirty_months(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        pd.DataFrame(
            {
                "time": pd.date_range("2000-01-01", periods=10, freq="MS"),
                "river_q_12": np.linspace(1, 10, 10),
            }
        ).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)
        ssfi = SSFITab(output)

        ssfi._compute()
        self.wait_ssfi(ssfi)

        self.assertFalse(ssfi.show.isEnabled())
        self.assertIn("minimal 30 periode bulanan", ssfi.status.text())
        output.close_reader()

    def test_ssfi_disk_cache_reuses_results_and_recovers_when_output_changes(self) -> None:
        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        dates = pd.date_range("2000-01-01", periods=40, freq="MS")
        pd.DataFrame({"time": dates, "river_q_12": np.linspace(1, 40, 40)}).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)
        cache_dir = self.project.root / ".hydrohub" / "ssfi"
        ssfi = SSFITab(output, cache_dir)
        try:
            ssfi._compute()
            self.wait_ssfi(ssfi)
            first = ssfi.frame.copy()
            cache_files = list(cache_dir.glob("*.csv"))
            self.assertEqual(len(cache_files), 1)

            restored = SSFITab(output, cache_dir)
            with patch("hydrohub.ui.result_widgets.build_ssfi_table", side_effect=AssertionError("calculated again")):
                restored._compute()
                self.wait_ssfi(restored)
            pd.testing.assert_frame_equal(restored.frame, first, check_freq=False)
            self.assertIn("dimuat dari cache", restored.status.text())

            pd.DataFrame({"time": dates, "river_q_12": np.linspace(40, 1, 40)}).to_csv(csv, index=False)
            with patch("hydrohub.ui.result_widgets.build_ssfi_table", wraps=build_ssfi_table) as calculate:
                restored._compute()
                self.wait_ssfi(restored)
                calculate.assert_called_once()
            self.assertFalse(restored.frame.equals(first))
            self.assertEqual(len(list(cache_dir.glob("*.csv"))), 1)

            cache_files[0].write_text("broken cache", encoding="utf-8")
            with patch("hydrohub.ui.result_widgets.build_ssfi_table", wraps=build_ssfi_table) as calculate:
                restored._compute()
                self.wait_ssfi(restored)
                calculate.assert_called_once()
            self.assertIsNotNone(restored.frame)
            self.assertNotIn("dimuat dari cache", restored.status.text())
        finally:
            output.close_reader()

    def test_ssfi_calculation_does_not_block_the_ui_thread(self) -> None:
        from threading import Event
        from unittest.mock import patch

        run = self.model / "run_default"
        run.mkdir()
        csv = run / "output.csv"
        pd.DataFrame(
            {
                "time": pd.date_range("2000-01-01", periods=40, freq="MS"),
                "river_q_12": np.linspace(1, 40, 40),
            }
        ).to_csv(csv, index=False)
        output = OutputTab()
        output.load_file(csv)
        self.wait_output(output)
        ssfi = SSFITab(output)
        entered, release = Event(), Event()

        def delayed_build(reader, **kwargs):
            entered.set()
            release.wait(5)
            return build_ssfi_table(reader, **kwargs)

        try:
            with patch("hydrohub.ui.result_widgets.build_ssfi_table", side_effect=delayed_build):
                started = time.monotonic()
                ssfi._compute()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(2))
                self.assertFalse(ssfi.compute.isEnabled())
                release.set()
                self.wait_ssfi(ssfi)
            self.assertTrue(ssfi.compute.isEnabled())
            self.assertIsNotNone(ssfi.frame)
        finally:
            release.set()
            ssfi.reset()
            output.close_reader()

    def test_result_exports_write_expected_files_and_ssfi_attributes(self) -> None:
        import geopandas as gpd

        basin = self.model / "basins.geojson"
        basin.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"value": 12},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[106, -7], [107, -7], [107, -6], [106, -7]]],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        values = {"12": {"value": -1.25, "label": "Kering", "color": "#ff0000"}}
        gpkg = self.model / "basin_ssfi.gpkg"
        export_basin_ssfi_gpkg(basin, gpkg, values, (2020, 1))
        exported = gpd.read_file(gpkg, layer="basin_ssfi")
        self.assertAlmostEqual(float(exported.loc[0, "ssfi"]), -1.25)
        self.assertEqual(exported.loc[0, "ssfi_class"], "Kering")
        self.assertEqual(exported.loc[0, "ssfi_color"], "#ff0000")
        self.assertEqual(exported.loc[0, "ssfi_period"], "2020-01")

        png = self.model / "Results Project_01_2020.png"
        export_basin_ssfi_png(basin, png, values, (2020, 1), self.project.name)
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

        excel = self.model / "ssfi.xlsx"
        frame = pd.DataFrame({"12": [-1.25]}, index=pd.to_datetime(["2020-01-01"]))
        export_ssfi_excel(frame, excel)
        self.assertEqual(pd.read_excel(excel, sheet_name="SSFI").loc[0, "period"], "2020-01")

        shapefile = self.model / "basin.zip"
        export_basin_shapefile(basin, shapefile)
        self.assertTrue(shapefile.exists())
        self.assertEqual(gpd.read_file(shapefile).basin_id.tolist(), ["hydrohub_12"])

    def test_large_csv_retains_preview_but_reads_complete_selected_series(self):
        csv = self.model / "large.csv"
        rows = 12000
        pd.DataFrame({"time": pd.date_range("2000-01-01", periods=rows, freq="h"),
                      "river_q_1": np.arange(rows),
                      "river_q_2": np.arange(rows) * 2}).to_csv(csv, index=False)
        reader = OutputReader(csv)
        try:
            self.assertLessEqual(len(reader._df), 5000)
            self.assertEqual(len(reader.get_timeseries("river_q_1")), rows)
            self.assertEqual(reader.latest_station_snapshots()["2"]["value"], (rows - 1) * 2)
        finally:
            reader.close()

    def test_latest_snapshot_preserves_duplicate_timestamp_mean_across_chunks(self):
        csv = self.model / "duplicates.csv"
        pd.DataFrame({"time": ["2020-01-01"] * 5000,
                      "river_q_1": np.arange(5000)}).to_csv(csv, index=False)
        reader = OutputReader(csv)
        try:
            self.assertEqual(reader.latest_station_snapshots()["1"]["value"], 2499.5)
        finally:
            reader.close()

    def test_slow_output_does_not_block_ui_or_replace_newer_selection(self):
        from threading import Event
        from unittest.mock import patch
        import time
        from hydrohub.ui.background import BackgroundJob

        first = self.model / "slow.csv"
        second = self.model / "latest.csv"
        first.write_text("time,river_q_1\n2020-01-01,1\n", encoding="utf-8")
        second.write_text("time,river_q_2\n2020-01-01,2\n", encoding="utf-8")
        entered, release = Event(), Event()
        def delayed_reader(path):
            if path == first:
                entered.set()
                release.wait(5)
            return OutputReader(path)
        output = OutputTab()
        try:
            with patch("hydrohub.ui.result_widgets.OutputReader", side_effect=delayed_reader):
                started = time.monotonic()
                output.load_file(first)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.wait(2))
                output.load_file(second)
                self.app.processEvents()
                self.assertIsNone(output.reader)
                release.set()
                # Restore the class before plotting (aggregate is a static method).
            self.wait_output(output)
            self.assertEqual(output.current_variable, "river_q_2")
            self.assertEqual(float(output.current_series.iloc[0]), 2)
        finally:
            release.set()
            output.close_reader()
            BackgroundJob._pool.waitForDone(5000)
            self.app.processEvents()

    def test_result_table_virtualizes_large_series(self):
        from hydrohub.ui.result_widgets import DataFrameModel
        model = DataFrameModel()
        frame = pd.DataFrame({"value": np.arange(100000)})
        model.set_frame(frame)
        self.assertIs(model.frame, frame)
        self.assertEqual(model.rowCount(), 100000)
        self.assertEqual(model.data(model.index(99999, 0)), "99999")


if __name__ == "__main__":
    unittest.main()
