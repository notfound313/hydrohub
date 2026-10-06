import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pandas as pd
import xarray as xr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.workflow_manager import WorkflowManager
from hydrohub.results.analysis import OutputReader
from hydrohub.ui.result_pages import ResultsPage


class ResultsControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = ProjectManager().create(Path(self.temp.name), "Results controls")
        self.model = self.project.root / "models" / "wflow"
        self.model.mkdir(exist_ok=True)
        (self.model / "wflow_sbm.toml").write_text(
            '[time]\ntimestepsecs = 86400\n\n'
            '[output.netcdf_scalar]\npath = "output.nc"\n', encoding="utf-8",
        )
        self.output = self.model / "output.nc"
        self.staticmaps = self.model / "staticmaps.nc"
        self.output_data = xr.Dataset(
            {"river_q": ("time", [1.0, 2.0])},
            coords={"time": pd.date_range("2020-01-01", periods=2)},
        )
        self.static_data = xr.Dataset(
            {"elevation": (("y", "x"), [[1.0, 2.0], [3.0, 4.0]])},
            coords={"x": [106.0, 107.0], "y": [-6.0, -7.0]},
        )
        self.output_data.to_netcdf(self.output)
        self.static_data.to_netcdf(self.staticmaps)
        self.page = ResultsPage(lambda: self.project, WorkflowManager())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate(), self.page.status.text())

    def tearDown(self):
        if not self.page._released:
            self.page.release_model()
        self.wait_for(lambda: not self.page._releasing)
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_release_unlocks_output_and_staticmaps_and_refresh_reloads(self):
        self.page.refresh_context()
        self.wait_for(lambda: self.page.output_tab.current_series is not None)
        self.assertIsNotNone(self.page.staticmaps_reader)
        self.page.release_button.click()
        self.wait_for(lambda: not self.page._releasing)
        self.assertIsNone(self.page.output_tab.reader)
        self.assertIsNone(self.page.staticmaps_reader)
        self.assertIsNone(self.page.staticgeom_reader)
        self.assertIsNone(self.page.artifacts)
        self.assertEqual(self.page.outputs.count(), 0)
        self.assertFalse(self.page.outputs.isEnabled())
        self.assertIn("Model dilepas", self.page.registry_status.text())
        # NetCDF overwrite fails on Windows if any result reader still owns the file.
        self.output_data.to_netcdf(self.output, mode="w")
        self.static_data.to_netcdf(self.staticmaps, mode="w")
        self.page.refresh_context()
        self.assertIsNone(self.page.output_tab.reader)
        self.assertTrue(self.page._released)
        self.page.refresh_button.click()
        self.wait_for(lambda: self.page.output_tab.current_series is not None)
        self.assertFalse(self.page._released)
        self.assertTrue(self.page.outputs.isEnabled())

    def test_release_waits_for_late_reader_without_reopening_results(self):
        entered, resume = Event(), Event()
        late_readers = []

        def slow_reader(path):
            reader = OutputReader(path)
            late_readers.append(reader)
            entered.set()
            resume.wait(10)
            return reader

        try:
            with patch("hydrohub.ui.result_widgets.OutputReader", side_effect=slow_reader):
                self.page.output_tab.load_file(self.output)
                self.wait_for(entered.is_set)
                self.page.release_button.click()
                self.app.processEvents()
                self.assertTrue(self.page._releasing)
                self.assertFalse(self.page.controls_panel.isEnabled())
                request = self.page._model_request
                self.page.refresh_context()
                self.assertEqual(self.page._model_request, request)
                self.assertNotIn("Model dilepas", self.page.registry_status.text())
                resume.set()
                self.wait_for(lambda: not self.page._releasing)
            self.assertIsNone(self.page.output_tab.reader)
            self.assertIsNone(self.page.output_tab.current_series)
            self.assertIsNone(late_readers[0]._ds._close)
            self.output_data.to_netcdf(self.output, mode="w")
        finally:
            resume.set()

    def test_eye_toggle_restores_panels_splitters_and_active_tab(self):
        self.page.resize(1200, 900)
        self.page.show()
        self.app.processEvents()
        self.page._show_plot()
        self.page.tabs.setCurrentWidget(self.page.ssfi_tab)
        sizes = (self.page.map_splitter.sizes(), self.page.analysis_splitter.sizes())
        reader = object()
        # Toggle must not touch resource ownership.
        self.page.output_tab.reader = reader
        try:
            for _ in range(2):
                with patch.object(self.page.map, "invalidate_size") as invalidate:
                    self.page.map_focus_button.click()
                    self.app.processEvents()
                    self.assertTrue(self.page.tabs.isHidden())
                    self.assertTrue(self.page.layer_panel.isHidden())
                    self.assertTrue(self.page.controls_panel.isHidden())
                    self.assertTrue(self.page.status.isHidden())
                    self.assertTrue(self.page.map_focus_button.isVisible())
                    self.assertIs(self.page.display_stack.currentWidget(), self.page.map)
                    self.assertIs(self.page.output_tab.reader, reader)
                    invalidate.assert_called()
                    self.page.map_focus_button.click()
                    self.app.processEvents()
                self.assertFalse(self.page.tabs.isHidden())
                self.assertFalse(self.page.layer_panel.isHidden())
                self.assertFalse(self.page.controls_panel.isHidden())
                self.assertFalse(self.page.status.isHidden())
                self.assertIs(self.page.tabs.currentWidget(), self.page.ssfi_tab)
                self.assertIs(self.page.display_stack.currentWidget(), self.page.output_tab.plot)
                self.assertEqual(self.page.map_splitter.sizes(), sizes[0])
                self.assertEqual(self.page.analysis_splitter.sizes(), sizes[1])
        finally:
            self.page.output_tab.reader = None


if __name__ == "__main__":
    unittest.main()
