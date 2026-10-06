import os
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox, QPushButton, QTabWidget  # noqa: E402

from hydrohub.ui.main_window import CreateProjectDialog, MainWindow, PathField  # noqa: E402
from hydrohub.core.project_manager import ProjectManager  # noqa: E402
from hydrohub.core.workflow_manager import WorkflowManager  # noqa: E402
from hydrohub.plugins.registry import ToolDefinition  # noqa: E402
from hydrohub.core.run_registry import RunRegistry  # noqa: E402
from hydrohub.ui.code_editor import CodeEditor  # noqa: E402


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def wait_results(self, window):
        window.project_workspace.setCurrentIndex(5)
        deadline = time.monotonic() + 10
        while window.results_page.output_tab.current_series is None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNotNone(window.results_page.output_tab.current_series)

    def test_main_window_builds_all_core_pages(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)

        self.assertEqual(window.stack.count(), 5)
        self.assertEqual(window.nav_buttons[2].text(), "Automation")
        self.assertEqual(window.nav_buttons[3].text(), "Backup & Restore")
        self.assertEqual(window.nav_buttons[2].iconSize().width(), 28)
        self.assertEqual(window.automation_page.tabs.count(), 4)
        self.assertEqual(window.project_workspace.count(), 7)
        tools_tabs = window.project_workspace.widget(6)
        self.assertEqual([tools_tabs.tabText(i) for i in range(tools_tabs.count())], ["Custom tools", "Library"])
        self.assertNotIn("Setup Guide", [window.project_workspace.tabText(i) for i in range(window.project_workspace.count())])
        self.assertEqual(window.project_workspace.tabText(2), "Data")
        self.assertEqual(window.project_workspace.widget(2).count(), 4)
        model_tabs = window.project_workspace.widget(3)
        simulation_tabs = window.project_workspace.widget(4)
        self.assertEqual(
            [model_tabs.tabText(i) for i in range(model_tabs.count())],
            ["Wflow Catalog", "Wflow Setup", "HydroMT"],
        )
        self.assertEqual(
            [simulation_tabs.tabText(i) for i in range(simulation_tabs.count())],
            ["Forcing", "Run Julia"],
        )
        self.assertFalse(isinstance(window.project_workspace.widget(5), QTabWidget))
        self.assertIs(window.project_workspace.widget(5), window.results_page)
        self.assertEqual(
            [window.results_page.tabs.tabText(i) for i in range(window.results_page.tabs.count())],
            ["Output", "Validation", "SSFI", "Export"],
        )
        self.assertEqual(
            [
                window.results_page.output_tab.inner_tabs.tabText(i)
                for i in range(window.results_page.output_tab.inner_tabs.count())
            ],
            ["Grafik", "Tabel Data Mentah", "Tabel Lengkap (CSV)"],
        )
        self.assertIsInstance(window.catalog_page.preview, CodeEditor)
        self.assertFalse(hasattr(window.catalog_page, "entries"))
        self.assertIsInstance(window.recipe_page.preview, CodeEditor)
        self.assertIn("setup_lulcmaps_from_vector", window.recipe_page.checks)
        config_item = window.recipe_page.checks["setup_config"]
        self.assertGreater(config_item.childCount(), 0)
        self.assertGreater(config_item.child(0).childCount(), 0)
        self.assertTrue(hasattr(window.catalog_page, "driver_options"))
        self.assertTrue(hasattr(window.catalog_page, "extra_metadata"))
        self.assertTrue(window.files_page.editors.tabsClosable())
        self.assertEqual(set(window.tool_pages), {"download", "preparing", "qc"})
        self.assertGreaterEqual(len(window.tools), 5)
        for page in window.tool_pages.values():
            if page.tools:
                self.assertEqual(len(page.parameter_widgets), len(page.tools[0].fields))
            self.assertTrue(
                {
                    "tool", "command", "parameters", "started", "completed",
                    "duration", "summary", "outputs", "diagnostics", "exit_code",
                }.issubset(page.report_fields)
            )
        self.assertIn("Leaflet", window.map_widget.accessibleName())
        self.assertFalse(window.workspace_button.isEnabled())
        self.assertTrue(all(not page.run_button.isEnabled() for page in window.tool_pages.values()))
        self.assertTrue(all(page.execution_splitter.count() == 2 for page in window.tool_pages.values()))
        self.assertTrue(
            all(
                page.execution_splitter.orientation() == Qt.Orientation.Horizontal
                for page in window.tool_pages.values()
            )
        )
        self.assertTrue(all(page.log.minimumHeight() >= 180 for page in window.tool_pages.values()))
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "UI Project")
            window.workflow.save_aoi_bbox(project, 106.0, -8.0, 112.0, -5.0)
            window._set_project(project)
            self.assertTrue(window.workspace_button.isEnabled())
            self.assertEqual(window.stack.currentIndex(), 1)
            self.assertEqual(window.project_workspace.currentIndex(), 0)
            for page in window.tool_pages.values():
                page.refresh_context()
            self.assertTrue(
                all(page.run_button.isEnabled() for page in window.tool_pages.values() if page.tools)
            )
            self.assertFalse(hasattr(window.recipe_page, "profile"))
            self.assertFalse(hasattr(window.catalog_page, "catalog_preset"))
            download_page = window.tool_pages["download"]
            self.assertEqual(
                tuple(download_page.parameter_widgets[name].value() for name in ("west", "south", "east", "north")),
                (106.0, -8.0, 112.0, -5.0),
            )
            updated_aoi = window.workflow.save_aoi_bbox(project, 107.0, -7.0, 113.0, -4.0)
            window._aoi_changed(updated_aoi)
            self.assertEqual(
                tuple(download_page.parameter_widgets[name].value() for name in ("west", "south", "east", "north")),
                (107.0, -7.0, 113.0, -4.0),
            )
            download_page.parameter_widgets["username"].setText("user")
            download_page.parameter_widgets["password"].setText("secret")
            arguments, environment = download_page._execution_config()
            self.assertNotIn("secret", arguments)
            self.assertEqual(environment["GPORTAL_PASSWORD"], "secret")
        window.close()

    def test_run_julia_selects_model_and_reports_progress(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Julia Project")
            model = project.root / "models" / "wflow_test"
            model.mkdir(parents=True)
            (model / "wflow_sbm.toml").write_text(
                '[time]\nstarttime = "2001-01-01T00:00:00"\n'
                'endtime = "2001-01-11T00:00:00"\n'
                "timestepsecs = 86400\n",
                encoding="utf-8",
            )

            window._set_project(project)
            page = window.run_julia_page
            window.project_workspace.widget(4).setCurrentIndex(1)
            window.project_workspace.setCurrentIndex(4)
            page.executable.setText(sys.executable)
            self.assertEqual(page.models.count(), 1)
            self.assertEqual(Path(page.models.currentData()), model.resolve())
            self.assertTrue(page.run_button.isEnabled())
            page.run_id.setText("manual-id")
            page.run_id.setProperty("autoGenerated", False)
            page.refresh_models.click()
            self.assertEqual(page.run_id.text(), "run_20010101_20010111")

            with patch.object(page.process_manager, "run_command") as run_command:
                page._run()
            run_command.assert_called_once()
            self.assertEqual(run_command.call_args.args[2], ["run_wflow.jl"])
            self.assertEqual(
                run_command.call_args.args[3], workspace / "src" / "hydrohub" / "julia"
            )
            environment = run_command.call_args.kwargs["extra_environment"]
            self.assertEqual(environment["HYDROHUB_MODEL_DIR"], str(model.resolve()))
            self.assertTrue(Path(environment["HYDROHUB_CONFIG_PATH"]).is_file())
            self.assertTrue(environment["HYDROHUB_RUN_ID"].startswith("run_20010101_20010111"))
            self.assertIn(environment["HYDROHUB_RUN_ID"], page.terminal.command.text())
            self.assertEqual(len(page._registry.list_runs()), 1)

            page._started_at = time.monotonic() - 10
            page._update_simulation_time("2001-01-06T00:00:00")
            self.assertEqual(page.progress.maximum(), 100)
            self.assertEqual(page.progress.value(), 50)
            self.assertIn("ETA", page.progress_details.text())

            page._update_simulation_time("2001-01-11T00:00:00")
            self.assertEqual(page.progress.value(), 99)
            self.assertEqual(page.status.text(), "Running Simulation")
            page._finished(0, "")
            self.assertEqual(page.progress.value(), 100)
            page.refresh_models.click()
            self.assertEqual(page.run_id.text(), "run_20010101_20010111_v2")

            page.timer.stop()
        window.close()

    def test_navigation_and_analysis_layouts_at_supported_sizes(self) -> None:
        window = MainWindow(Path(__file__).resolve().parents[1])
        try:
            window.show()
            window._navigate(1)
            for width, height in ((1100, 700), (1440, 900)):
                window.resize(width, height)
                for _ in range(2):
                    window._toggle_rail()
                    self.app.processEvents()
                    for button in window.nav_buttons:
                        self.assertEqual(bool(button.text()), window._rail_expanded)
                        self.assertEqual(button.iconSize().width(), 28 if window._rail_expanded else 20)
                        self.assertGreaterEqual(button.width(), 40)
                        self.assertFalse(button.isHidden())
                window.project_workspace.setCurrentIndex(4)
                window.project_workspace.widget(4).setCurrentIndex(1)
                self.app.processEvents()
                page = window.run_julia_page
                self.assertEqual((window.width(), window.height()), (width, height))
                self.assertEqual(page.execution_splitter.count(), 2)
                self.assertGreaterEqual(page.form_scroll.height(), 140)
                self.assertGreaterEqual(page.executable.height(), 28)
                self.assertEqual(page.form_scroll.horizontalScrollBar().maximum(), 0)
                page.form_scroll.ensureWidgetVisible(page.run_button)
                self.app.processEvents()
                self.assertTrue(page.run_button.isVisible())
                window.project_workspace.setCurrentIndex(5)
                for index in range(3):
                    window.results_page.tabs.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertGreaterEqual(window.results_page.tabs.height(), 300)
                    self.assertTrue(all(size > 0 for size in window.results_page.analysis_splitter.sizes()))
                self.assertGreaterEqual(window.results_page.ssfi_tab.table.height(), 140)
                window.results_page.tabs.setCurrentIndex(0)
                window.results_page.output_tab.inner_tabs.setCurrentIndex(1)
                self.app.processEvents()
                self.assertGreaterEqual(window.results_page.output_tab.table.height(), 160)
        finally:
            window.close()

    def test_results_selects_available_model_and_output_without_subtabs(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Results UI Project")
            model = project.root / "models" / "wflow_results"
            run = model / "run_default"
            run.mkdir(parents=True)
            (model / "wflow_sbm.toml").write_text(
                '[time]\nstarttime = "2020-01-01T00:00:00"\n'
                'endtime = "2020-01-03T00:00:00"\n'
                "timestepsecs = 86400\n\n"
                '[output.csv]\npath = "run_default/output.csv"\n',
                encoding="utf-8",
            )
            (run / "output.csv").write_text(
                "time,river_q_7\n2020-01-01,1.0\n2020-01-02,2.0\n",
                encoding="utf-8",
            )

            window._set_project(project)

            self.wait_results(window)
            self.assertEqual(window.results_page.models.count(), 1)
            self.assertEqual(window.results_page.outputs.count(), 1)
            self.assertEqual(window.results_page.variables.currentData(), "river_q_7")
            self.assertLessEqual(window.results_page.outputs.maximumWidth(), 360)
            self.assertNotIn(str(run / "output.csv"), window.results_page.status.text())
            self.assertEqual(window.results_page.tabs.count(), 4)
            window.results_page.output_tab._plot(show=True)
            self.assertIs(window.results_page.tabs.currentWidget(), window.results_page.output_tab)
            self.assertIs(window.results_page.display_stack.currentWidget(), window.results_page.output_tab.plot)
            self.assertEqual(window.results_page.output_tab.inner_tabs.indexOf(window.results_page.output_tab.plot), -1)
            self.assertEqual(len(window.results_page.output_tab.plot.listDataItems()), 1)
            window.results_page._show_map()
            self.assertIs(window.results_page.display_stack.currentWidget(), window.results_page.map)
            self.assertIs(
                window.results_page.output_tab.inner_tabs.currentWidget(),
                window.results_page.output_tab.plot,
            )
            self.assertEqual(window.forcing_page.models.count(), 0)
        window.close()

    def test_ssfi_map_keeps_ssfi_tab_active(self) -> None:
        window = MainWindow(Path(__file__).resolve().parents[1])
        page = window.results_page
        page._show_plot()
        page.tabs.setCurrentWidget(page.ssfi_tab)
        page._show_map()
        self.assertIs(page.tabs.currentWidget(), page.ssfi_tab)
        self.assertIs(page.display_stack.currentWidget(), page.map)
        window.close()

    def test_results_loads_combined_valid_run_chain(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Combined Results Project")
            model = project.root / "models" / "wflow_results"
            model.mkdir(parents=True)
            (model / "wflow_sbm.toml").write_text(
                '[time]\nstarttime = "2001-01-01T00:00:00"\n'
                'endtime = "2001-01-04T00:00:00"\n'
                "timestepsecs = 86400\n",
                encoding="utf-8",
            )
            registry = RunRegistry(project.root, model, project.name)
            for start, end in (
                (datetime(2001, 1, 1), datetime(2001, 1, 2)),
                (datetime(2001, 1, 3), datetime(2001, 1, 4)),
            ):
                plan = registry.plan(start, end, 86400)
                record, _config = registry.prepare_run(plan)
                output_dir = registry.resolve_path(record.output_path)
                pd.DataFrame(
                    {
                        "time": pd.date_range(start, end, freq="D"),
                        "river_q_7": [1.0, 2.0],
                    }
                ).to_csv(output_dir / "output.csv", index=False)
                registry.resolve_path(record.state_output_path).write_bytes(b"state")
                registry.finish_run(record.run_id, True)

            window._set_project(project)

            page = window.results_page
            self.wait_results(window)
            self.assertTrue(page.outputs.itemText(0).startswith("Gabungan registry:"))
            deadline = time.monotonic() + 5
            while page.output_tab.reader is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual(len(page.output_tab.reader.get_timeseries("river_q_7")), 4)
            self.assertTrue(page.export_combined.isEnabled())
        window.close()

    def test_run_wflow_script_only_reads_model_directory_from_environment(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "src" / "hydrohub" / "julia" / "run_wflow.jl"
        ).read_text(encoding="utf-8")
        self.assertIn('ENV, "HYDROHUB_MODEL_DIR"', script)
        self.assertIn('ENV, "HYDROHUB_CONFIG_PATH"', script)
        self.assertIn('Wflow.run(toml_path)', script)
        self.assertNotIn('model_gsm', script)

    def test_create_project_dialog_returns_accepted(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        dialog = CreateProjectDialog(workspace, None)
        dialog.name.setText("Dialog Project")
        dialog.location.setText(str(workspace / "projects"))

        dialog.accept()

        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        dialog.close()

    def test_aoi_dialog_and_overview_propagate_bbox(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        dialog = CreateProjectDialog(workspace, None)
        dialog.name.setText("AOI Project")
        for field, value in zip(dialog.aoi_fields, (106.0, -8.0, 112.0, -5.0)):
            field.setValue(value)
        feature = dialog.aoi_data()
        self.assertEqual(feature["geometry"]["coordinates"][0][0], [106.0, -8.0])
        dialog.close()

        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "AOI UI Project")
            feature = window.workflow.save_aoi_bbox(project, 106.0, -8.0, 112.0, -5.0)
            window._set_project(project)
            with patch.object(window.forcing_page.map, "set_aoi") as forcing_aoi:
                window._aoi_changed(feature)
            forcing_aoi.assert_called_once_with(feature)
            self.assertNotIn("Configuration status", window.overview_page.summary.text())
        window.close()

    def test_aoi_zip_works_in_project_dialog_and_overview(self) -> None:
        import geopandas as gpd
        from shapely.geometry import Polygon

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shape = root / "aoi.shp"
            gpd.GeoDataFrame(
                geometry=[Polygon([(106, -8), (112, -8), (112, -5), (106, -5)])],
                crs="EPSG:4326",
            ).to_file(shape)
            archive_path = root / "aoi.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for file in root.glob("aoi.*"):
                    if file.suffix != ".zip":
                        archive.write(file, file.name)

            dialog = CreateProjectDialog(root, None)
            dialog.name.setText("Imported AOI")
            with patch("hydrohub.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(archive_path), "")) as browse:
                dialog._browse_aoi()
            self.assertIn("*.zip", browse.call_args.args[3])
            self.assertEqual(dialog.aoi_file.text(), str(archive_path))
            self.assertEqual(WorkflowManager.aoi_bounds(dialog.aoi_data()), (106, -8, 112, -5))
            dialog.accept()
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)

            window = MainWindow(root)
            with patch("hydrohub.ui.main_window.CreateProjectDialog", return_value=dialog), \
                    patch.object(dialog, "exec", return_value=dialog.DialogCode.Accepted):
                window._create_project()
            project = window.current_project
            self.assertEqual(window.workflow.load_aoi_bounds(project), (106, -8, 112, -5))
            page = window.overview_page
            with patch("hydrohub.ui.workflow_pages.QFileDialog.getOpenFileName", return_value=(str(archive_path), "")) as browse:
                page._browse_aoi()
            self.assertIn("*.zip", browse.call_args.args[3])
            page._save_aoi()
            self.assertEqual(window.workflow.load_aoi_bounds(project), (106, -8, 112, -5))
            self.assertEqual([float(field.text()) for field in page.aoi_fields], [106, -8, 112, -5])
            window.close()
            dialog.close()

    def test_global_database_is_automatic_and_copies_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            page = window.global_database_page
            self.assertTrue((page.root / "manifest.yml").is_file())
            sample = page.root / "dem" / "sample.tif"
            sample.write_bytes(b"data")
            page._refresh_files()
            matching = [row for row in range(page.files.rowCount()) if page.files.item(row, 0).text() == "sample.tif"]
            self.assertEqual(len(matching), 1)
            page.files.selectRow(matching[0])
            page._copy_selected_path()
            self.assertEqual(QApplication.clipboard().text(), str(sample))
            self.assertFalse(any(button.text() == "Initialize Global Database" for button in page.findChildren(QPushButton)))
            self.assertFalse(any(button.text() == "New project" for button in window.sidebar.findChildren(QPushButton)))
            self.assertTrue(hasattr(window.settings_page, "hydromt_version"))
            self.assertFalse(hasattr(window.settings_page, "python"))
            window.close()

    def test_project_file_explorer_copies_file_and_folder_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            project = ProjectManager().create(Path(temp_dir) / "project", "Files Project")
            folder = project.root / "data" / "sample"
            folder.mkdir()
            file = folder / "input.csv"
            file.write_text("value\n1\n", encoding="utf-8")
            window._set_project(project)
            page = window.files_page

            page._copy_path(file)
            self.assertEqual(QApplication.clipboard().text(), str(file.resolve()))
            page._copy_path(folder)
            self.assertEqual(QApplication.clipboard().text(), str(folder.resolve()))
            window.close()

    def test_dashboard_project_registry_shows_only_project_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            window = MainWindow(workspace)
            project = window.project_manager.create(
                workspace / "projects", "Visible Project"
            )
            window._refresh_project_registry()

            self.assertEqual(window.dashboard_projects.columnCount(), 1)
            self.assertEqual(
                window.dashboard_projects.horizontalHeaderItem(0).text(), "Name"
            )
            self.assertEqual(window.dashboard_projects.rowCount(), 1)
            item = window.dashboard_projects.item(0, 0)
            self.assertEqual(item.text(), "Visible Project")
            self.assertEqual(
                item.data(Qt.ItemDataRole.UserRole), str(project.root)
            )
            self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsEditable)
            window.close()

    def test_wflow_setup_import_yaml_stays_unsaved_until_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            project = window.project_manager.create(Path(temp_dir), "Recipe Import")
            source = project.root / "data" / "raw" / "dem.tif"
            source.write_bytes(b"fixture")
            window.workflow.add_catalog_entry(
                project, "dem_local", "RasterDataset", "rasterio", "data/raw/dem.tif", "4326", "dem",
            )
            recipe = window.workflow.create_wflow_recipe(project)
            original = recipe.read_text(encoding="utf-8")
            imported = project.root / "external.yaml"
            content = "steps:\n  - setup_basemaps:\n      hydrography_fn: dem_local\n"
            imported.write_text(content, encoding="utf-8")
            window._set_project(project)
            page = window.recipe_page
            with patch("hydrohub.ui.workflow_pages.QFileDialog.getOpenFileName", return_value=(str(imported), "")):
                page._import_yaml()
            self.assertEqual(page.preview.toPlainText(), content)
            self.assertTrue(page.preview.document().isModified())
            self.assertEqual(recipe.read_text(encoding="utf-8"), original)
            page._save_yaml()
            self.assertEqual(recipe.read_text(encoding="utf-8"), content)
            self.assertFalse(page.preview.document().isModified())
            imported.write_text("steps: invalid\n", encoding="utf-8")
            with patch("hydrohub.ui.workflow_pages.QFileDialog.getOpenFileName", return_value=(str(imported), "")):
                page._import_yaml()
            with patch("hydrohub.ui.workflow_pages.QMessageBox.warning") as warning:
                page._save_yaml()
            warning.assert_called_once()
            self.assertEqual(recipe.read_text(encoding="utf-8"), content)
            window.close()

    def test_custom_tool_results_stay_with_their_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            first = window.project_manager.create(Path(temp_dir), "First")
            second = window.project_manager.create(Path(temp_dir), "Second")
            script = Path(temp_dir) / "result.py"
            script.write_text("print('project result', flush=True)\n", encoding="utf-8")
            tool = {"id": "result", "name": "Result", "path": script.parent, "entrypoint": script.name}
            window._set_project(first)
            first_session = window.custom_tool_sessions.start(tool, first, Path(sys.executable), [str(script)], {})
            self.assertEqual(window.custom_tools_page.sessions.count(), 1)
            window._set_project(second)
            self.assertEqual(window.custom_tools_page.sessions.count(), 0)
            second_session = window.custom_tool_sessions.start(tool, second, Path(sys.executable), [str(script)], {})
            self.assertEqual(set(window.custom_tools_page.session_widgets), {second_session.id})
            deadline = time.monotonic() + 10
            while (first_session.exit_code is None or second_session.exit_code is None) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertEqual((first_session.exit_code, second_session.exit_code), (0, 0))
            window._set_project(first)
            page = window.custom_tools_page
            self.assertEqual(set(page.session_widgets), {first_session.id})
            self.assertEqual(page.sessions.count(), 1)
            self.assertIn("project result", page.session_widgets[first_session.id][1].output.toPlainText())
            window._set_project(second)
            self.assertEqual(set(window.custom_tools_page.session_widgets), {second_session.id})
            window.close()

    def test_running_project_survives_switch_and_dashboard_status_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            first = window.project_manager.create(Path(temp_dir), "First")
            second = window.project_manager.create(Path(temp_dir), "Second")
            tool = ToolDefinition("test.background", "Background", "model", "test", "Test", ())
            window._set_project(first)
            page = window.tool_pages["download"]
            manager = page.process_manager
            manager.run_command(
                tool, Path(sys.executable), ["-c", "import time; time.sleep(30)"],
                first.root, first.manifests_dir,
            )
            try:
                window._set_project(second)
                self.assertEqual(window.current_project, second)
                self.assertIs(page.project_provider(), first)
                self.assertIs(window.workspace_records[str(first.root)]["tool_pages"]["download"], page)
                names = {
                    window.dashboard_projects.item(row, 0).data(Qt.ItemDataRole.UserRole):
                    window.dashboard_projects.item(row, 0).text()
                    for row in range(window.dashboard_projects.rowCount())
                }
                self.assertIn("Running", names[str(first.root)])
                self.assertNotIn("Running", names[str(second.root)])
                for row in range(window.dashboard_projects.rowCount()):
                    if window.dashboard_projects.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(first.root):
                        window.dashboard_projects.selectRow(row)
                        break
                with patch("hydrohub.ui.main_window.QMessageBox.information") as blocked:
                    window._delete_registered()
                blocked.assert_called_once()
                self.assertTrue(first.root.exists())
                window._set_project(first)
                self.assertIs(window.tool_pages["download"], page)
                self.assertTrue(manager.is_running)
            finally:
                manager.stop()
                deadline = time.monotonic() + 10
                while window.process_slots.active and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertFalse(window.process_slots.active)
                self.app.processEvents()
                names = [window.dashboard_projects.item(row, 0).text() for row in range(window.dashboard_projects.rowCount())]
                self.assertNotIn("Running", " ".join(names))
                window.close()

    def test_process_badges_and_terminals_survive_until_project_is_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            first = window.project_manager.create(Path(temp_dir), "First")
            second = window.project_manager.create(Path(temp_dir), "Second")
            window._set_project(first)
            hydromt = window.hydromt_page
            julia = window.run_julia_page
            hydromt._owns_process = True
            julia._owns_process = True
            julia._started_at = time.monotonic()
            runs = (
                (hydromt, "hydromt.cli", "HydroMT", 0),
                (julia, "wflow.julia", "Julia", 2),
            )
            for page, tool_id, label, code in runs:
                page.process_manager.run_command(
                    ToolDefinition(tool_id, label, "model", "test", "Test", ()),
                    Path(sys.executable),
                    ["-c", f"import sys; print('{label} log', flush=True); sys.exit({code})"],
                    first.root, first.manifests_dir,
                )
            window._set_project(second)
            running = [window.dashboard_projects.item(row, 0).text() for row in range(window.dashboard_projects.rowCount())]
            self.assertTrue(any("HydroMT: Running" in name and "Julia: Running" in name for name in running))
            deadline = time.monotonic() + 10
            while window.process_slots.active and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(window.process_slots.active)
            self.app.processEvents()
            item = next(
                window.dashboard_projects.item(row, 0)
                for row in range(window.dashboard_projects.rowCount())
                if window.dashboard_projects.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(first.root)
            )
            self.assertIn("HydroMT: Selesai", item.text())
            self.assertIn("Julia: Gagal", item.text())
            self.assertIn("HydroMT log", hydromt.terminal.output.toPlainText())
            self.assertIn("Julia log", julia.terminal.output.toPlainText())
            window.dashboard_projects.setCurrentItem(item)
            self.assertIn("Selesai", item.text())
            window.dashboard_projects.itemDoubleClicked.emit(item)
            self.assertEqual(window.current_project, first)
            self.assertIs(window.hydromt_page, hydromt)
            self.assertNotIn(str(first.root), window._process_badges)
            window._set_project(second)
            window._set_project(first)
            self.assertIs(window.hydromt_page, hydromt)
            self.assertIn("HydroMT log", window.hydromt_page.terminal.output.toPlainText())
            self.assertIn("Julia log", window.run_julia_page.terminal.output.toPlainText())
            window._set_project(second)
            item = next(
                window.dashboard_projects.item(row, 0)
                for row in range(window.dashboard_projects.rowCount())
                if window.dashboard_projects.item(row, 0).data(Qt.ItemDataRole.UserRole) == str(first.root)
            )
            window.dashboard_projects.setCurrentItem(item)
            with patch("hydrohub.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._delete_registered()
            self.assertNotIn(str(first.root), window.workspace_records)
            window.close()

    def test_project_workspaces_keep_tool_and_file_state_independent(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ProjectManager()
            first = manager.create(Path(temp_dir), "First UI Project")
            second = manager.create(Path(temp_dir), "Second UI Project")
            window.workflow.create_catalog(first, "first_catalog.yml")
            window.workflow.save_ui_state(first, active_catalog_file="first_catalog.yml")

            window._set_project(first)
            window.project_workspace.setCurrentIndex(3)
            first_workspace = window.project_workspace
            first_tools = window.tool_pages
            first_tools["download"].parameter_widgets["username"].setText("first-user")
            self.assertEqual(window.catalog_page.catalog_files.currentText(), "first_catalog.yml")

            window._set_project(second)
            from PySide6.QtCore import QCoreApplication, QEvent
            from shiboken6 import isValid
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(isValid(first_workspace))
            second_workspace = window.project_workspace
            second_tools = window.tool_pages
            self.assertIsNot(first_workspace, second_workspace)
            self.assertIsNot(first_tools["download"], second_tools["download"])
            self.assertNotEqual(
                second_tools["download"].parameter_widgets["username"].text(),
                "first-user",
            )

            window._set_project(first)
            window.project_workspace.setCurrentIndex(3)
            self.assertEqual(len(window.workspace_records), 1)
            self.assertIsNot(window.project_workspace, first_workspace)
            self.assertIsNot(window.tool_pages["download"], first_tools["download"])
            self.assertNotEqual(
                window.tool_pages["download"].parameter_widgets["username"].text(),
                "first-user",
            )
            self.assertEqual(window.catalog_page.catalog_files.currentText(), "first_catalog.yml")
        window.close()

    def test_catalog_switch_does_not_reload_results_or_forcing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MainWindow(Path(temp_dir))
            project = ProjectManager().create(Path(temp_dir), "Lazy Project")
            window.workflow.create_catalog(project, "second.yml")
            try:
                with patch("hydrohub.ui.result_pages.discover_wflow_models") as discover:
                    window._set_project(project)
                    discover.assert_not_called()
                window.project_workspace.setCurrentIndex(3)
                with patch.object(window.results_page, "refresh_context") as results, \
                     patch.object(window.forcing_page, "refresh_context") as forcing, \
                     patch.object(window.global_database_page, "_refresh_files") as database:
                    window.catalog_page.catalog_files.setCurrentText("second.yml")
                    self.assertIn("second.yml", window.catalog_page.editor_title.text())
                    results.assert_not_called()
                    forcing.assert_not_called()
                    database.assert_not_called()
            finally:
                window.close()

    def test_files_workspace_moves_items_only_by_drag_handler(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Drag Move Project")
            source_dir = project.root / "data" / "source"
            destination = project.root / "data" / "destination"
            source_dir.mkdir()
            destination.mkdir()
            source = source_dir / "forcing.nc"
            source.write_bytes(b"data")

            window._set_project(project)
            page = window.files_page
            self.assertFalse(
                any(
                    button.text() == "Move"
                    for button in page.findChildren(QPushButton)
                )
            )
            self.assertTrue(page.tree.dragEnabled())
            self.assertTrue(page.tree.acceptDrops())
            self.assertFalse(page.model.isReadOnly())
            self.assertEqual(
                page.tree.editTriggers(),
                page.tree.EditTrigger.NoEditTriggers,
            )

            page._move_path(source, destination)

            self.assertFalse(source.exists())
            self.assertTrue((destination / "forcing.nc").exists())
            self.assertIn("Moved", page.output.toPlainText())
        window.close()

    def test_boolean_tool_fields_emit_flags_without_value_arguments(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Boolean Tool Project")
            window._set_project(project)
            tool = next(
                item
                for item in window.tools
                if item.id == "hydrohub.preparing.rebuild_flowdir"
            )
            page = window.tool_pages["preparing"]
            page.selected_tool = tool
            page._build_parameter_form(tool)
            self.assertIn("dem", page.parameter_widgets)
            self.assertIn("old_d8", page.parameter_widgets)
        window.close()

    def test_basin_values_and_database_files_follow_project_catalog(self) -> None:
        workspace = Path(__file__).resolve().parents[1]
        window = MainWindow(workspace)
        with tempfile.TemporaryDirectory() as temp_dir:
            project = ProjectManager().create(Path(temp_dir), "Dynamic Catalog Project")
            source = project.root / "data" / "raw" / "hydrography.tif"
            source.write_bytes(b"source")
            window.workflow.add_catalog_entry(
                project,
                "basin_source",
                "RasterDataset",
                "rasterio",
                "data/raw/hydrography.tif",
                category="hydrography",
                placeholders={
                    "basin": {"solo": [110.0, -7.0]},
                    "subbasin": {
                        "solo_upper": {"value": [110.2, -7.2], "uparea": 250}
                    },
                },
            )
            window.workflow.create_wflow_recipe(project, "wflow.yml")
            database_page = window.project_workspace.widget(2).widget(1)
            database_file = database_page.root / "user_data" / "project-test-dem.tif"
            database_file.write_bytes(b"dem")

            window._set_project(project)
            window.project_workspace.setCurrentIndex(3)
            self.assertEqual(window.catalog_page.catalog_keys.currentText(), "basin_source")
            window.project_workspace.widget(3).setCurrentIndex(1)
            self.assertEqual(window.recipe_page.region_source.currentText(), "basin_source")
            window.recipe_page.region_mode.setCurrentText("basin")
            window.recipe_page.region_input.setCurrentText("Coordinates")
            window.recipe_page.region_lon.setText("110.0")
            window.recipe_page.region_lat.setText("-7.0")
            window.recipe_page._generate()
            recipe = yaml.safe_load(window.recipe_page.preview.toPlainText())
            basemaps = next(
                step["setup_basemaps"]
                for step in recipe["steps"]
                if "setup_basemaps" in step
            )
            self.assertEqual(basemaps["region"]["basin"], [110.0, -7.0])

            window.recipe_page.region_input.setCurrentText("Catalog key")
            window.recipe_page._generate()
            recipe = yaml.safe_load(window.recipe_page.preview.toPlainText())
            basemaps = next(
                step["setup_basemaps"]
                for step in recipe["steps"]
                if "setup_basemaps" in step
            )
            self.assertEqual(basemaps["region"]["basin"], "basin_source")

            window.recipe_page.region_mode.setCurrentText("subbasin")
            window.recipe_page.region_uparea.setText("321")
            self.assertFalse(window.recipe_page.region_form.isRowVisible(window.recipe_page.region_lon))
            self.assertFalse(window.recipe_page.region_form.isRowVisible(window.recipe_page.region_lat))
            self.assertTrue(window.recipe_page.region_form.isRowVisible(window.recipe_page.region_uparea))
            window.recipe_page._generate()
            recipe = yaml.safe_load(window.recipe_page.preview.toPlainText())
            basemaps = next(
                step["setup_basemaps"]
                for step in recipe["steps"]
                if "setup_basemaps" in step
            )
            self.assertEqual(basemaps["region"]["subbasin"], "basin_source")
            self.assertEqual(basemaps["region"]["uparea"], 321.0)

            hydromt = window.hydromt_page
            window.project_workspace.widget(3).setCurrentIndex(2)
            self.assertEqual(hydromt.catalog_file.currentText(), "wflow_catalog.yml")
            self.assertEqual(hydromt.recipe_file.currentText(), "wflow.yml")
            build_args = hydromt.command_arguments()
            self.assertEqual(
                build_args[:3], ["build", "wflow_sbm", hydromt.model_root.text()]
            )
            self.assertIn("-i", build_args)
            self.assertIn("-d", build_args)
            self.assertIn("-v", build_args)
            self.assertTrue(hasattr(hydromt.terminal, "output"))

            source_model = project.root / "models" / "existing"
            source_model.mkdir(parents=True)
            target_model = project.root / "models" / "updated"
            hydromt.mode_combo.setCurrentText("Update")
            hydromt.model_root.setText(str(source_model))
            hydromt.update_target.setText(str(target_model))
            update_args = hydromt.command_arguments()
            self.assertEqual(
                update_args[:3], ["update", "wflow_sbm", str(source_model)]
            )
            self.assertEqual(update_args[3:5], ["-o", str(target_model)])
            self.assertTrue(hydromt.update_target_row.isVisibleTo(hydromt))
            with patch.object(window.hydromt_page.process_manager, "run_command") as run_command:
                hydromt._run_hydromt()
            run_command.assert_called_once()
            runtime_catalog = (
                project.root
                / "workspace"
                / "runtime_catalogs"
                / "wflow_catalog.yml"
            )
            self.assertEqual(
                run_command.call_args.args[2],
                hydromt.command_arguments(runtime_catalog),
            )
            self.assertNotEqual(run_command.call_args.args[2], update_args)
            self.assertIn("hydromt", hydromt.terminal.command.text().lower())
            hydromt._owns_process = False

            database_page._refresh_files()
            names = [database_page.files.item(row, 0).text() for row in range(database_page.files.rowCount())]
            self.assertIn("project-test-dem.tif", names)
            database_file.unlink()
        window.close()


if __name__ == "__main__":
    unittest.main()
