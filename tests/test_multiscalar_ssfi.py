import csv
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import xarray as xr
from scipy.stats import gamma, norm

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from hydrohub.results.analysis import OutputReader, calculate_ssi, calculate_multiscalar_ssi_all
from hydrohub.core.run_registry import RunRegistry
from hydrohub.results.export import export_discharge, export_basin_shapefile, export_basin_ssfi_gpkg
from hydrohub.ui.result_pages import ResultsPage


class MultiscalarSSFITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_missing_values_do_not_change_zero_probability_or_fit_sample_size(self):
        valid = pd.Series([0.0] * 6 + list(np.linspace(1, 36, 36)))
        series = pd.concat([pd.Series([np.nan] * 11), valid], ignore_index=True)
        shape, location, scale = gamma.fit(valid[valid > 0], floc=0)
        expected = norm.ppf(np.clip(6 / 42 + (1 - 6 / 42) * gamma.cdf(valid, shape, location, scale),
                                    0.000001, 0.999999))
        actual = calculate_ssi(series)
        self.assertTrue(actual.iloc[:11].isna().all())
        np.testing.assert_allclose(actual.iloc[11:], expected)
        self.assertTrue(calculate_ssi(pd.Series([np.nan] * 12 + list(range(1, 30)))).isna().all())

    def test_daily_discharge_is_monthly_before_rolling_and_missing_month_breaks_window(self):
        dates = pd.date_range("2000-01-01", "2005-12-31", freq="D")
        data = pd.DataFrame({"nama_das": "12", "bulan_tahun": dates,
                             "runoff": 2 + np.arange(len(dates)) % 37})
        other = data.assign(nama_das="7", runoff=lambda frame: frame.runoff * 2 + 10)
        data = pd.concat([data, other], ignore_index=True)
        data = data.loc[data.bulan_tahun.dt.to_period("M") != pd.Period("2002-04")]
        result = calculate_multiscalar_ssi_all(data.sample(frac=1, random_state=4))
        for station in ("12", "7"):
            original = data.loc[data.nama_das == station].set_index("bulan_tahun").runoff.resample("MS").mean()
            actual = result.loc[result.nama_das == station].set_index("bulan_tahun")
            self.assertEqual(len(actual), 72)
            for n in (1, 3, 9, 12):
                expected = calculate_ssi(original.rolling(n, min_periods=n).mean())
                np.testing.assert_allclose(actual[f"SSI-{n}"], expected, equal_nan=True)
                self.assertTrue(actual[f"SSI-{n}"].iloc[:n - 1].isna().all())
                self.assertTrue(actual[f"SSI-{n}"].loc["2002-04":pd.Timestamp("2002-04-01") + pd.DateOffset(months=n - 1)].isna().all())

    def test_full_discharge_export_headers_and_basin_ids_match_shapefile(self):
        import geopandas as gpd
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "output.csv"
            dates = pd.date_range("2000-01-01", periods=6001, freq="D")
            pd.DataFrame({"time": dates, "river_q_12": np.arange(6001),
                          "river_q_7": np.arange(6001) + 10}).to_csv(source, index=False)
            original = source.read_bytes()
            destination = root / "debit.csv"
            export_discharge(source, destination, "Papua")
            with destination.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0], ["basin_id", "Papua_12", "Papua_7"])
            self.assertEqual(rows[1], ["time", "river_q_12", "river_q_7"])
            self.assertEqual(len(rows), 6003)
            self.assertEqual(float(rows[-1][1]), 6000)
            self.assertEqual(source.read_bytes(), original)
            registry = SimpleNamespace(project_name="Papua", build_combined_output=lambda _: source)
            combined = RunRegistry.export_combined(registry, None, root / "combined.csv")
            self.assertEqual(combined.read_bytes(), destination.read_bytes())
            restored = OutputReader(destination)
            try:
                self.assertEqual(len(restored.get_timeseries("river_q_12")), 6001)
                self.assertEqual(restored.latest_station_snapshots()["12"]["value"], 6000)
            finally:
                restored.close()
            excel = root / "debit.xlsx"
            export_discharge(source, excel, "Papua")
            spreadsheet = pd.read_excel(excel, sheet_name="Debit", header=None)
            self.assertEqual(spreadsheet.iloc[0].tolist(), rows[0])
            self.assertEqual(spreadsheet.iloc[1].tolist(), rows[1])
            self.assertEqual(len(spreadsheet), 6003)
            with self.assertRaises(ValueError):
                export_discharge(source, source, "Papua")

            basin = root / "basins.geojson"
            basin.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"value": value}, "geometry": {
                    "type": "Polygon", "coordinates": [[[106, -7], [107, -7], [107, -6], [106, -7]]]}}
                for value in (7.0, 12.0, 999.0)]}), encoding="utf-8")
            shape = root / "basins.zip"
            self.assertEqual(export_basin_shapefile(basin, shape, "Papua", [12, 7]), 1)
            with zipfile.ZipFile(shape) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "basins.shp", "basins.shx", "basins.dbf", "basins.prj", "basins.cpg"})
                self.assertIsNone(archive.testzip())
            exported = gpd.read_file(shape)
            self.assertEqual(exported.basin_id.iloc[:2].tolist(), ["Papua_7", "Papua_12"])
            self.assertTrue(pd.isna(exported.basin_id.iloc[2]))
            gpkg = root / "ssfi9.gpkg"
            export_basin_ssfi_gpkg(basin, gpkg, {"12": {"value": -1.5}}, (2005, 12), 9, "Papua")
            exported = gpd.read_file(gpkg)
            self.assertEqual(exported.ssfi_scale.tolist(), [9, 9, 9])
            self.assertEqual(exported.basin_id.iloc[1], "Papua_12")
            self.assertEqual(exported.ssfi_period.iloc[1], "2005-12")

    def test_scalar_netcdf_uses_station_coordinates_and_grid_is_not_mislabeled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scalar.nc"
            xr.Dataset({"river_q": (("time", "station"), [[1.0, 2.0], [3.0, 4.0]])},
                       coords={"time": pd.date_range("2000-01-01", periods=2),
                               "station": ["12.0", "7"]}).to_netcdf(source)
            destination = root / "scalar.csv"
            export_discharge(source, destination, "Papua")
            with destination.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0], ["basin_id", "Papua_12", "Papua_7"])
            self.assertEqual(rows[1], ["time", "river_q_12", "river_q_7"])
            self.assertEqual([float(value) for value in rows[-1][1:]], [3.0, 4.0])
            grid = root / "grid.nc"
            xr.Dataset({"river_q": (("time", "lat", "lon"), np.ones((2, 2, 2)))},
                       coords={"time": pd.date_range("2000-01-01", periods=2),
                               "lat": [-7, -6], "lon": [106, 107]}).to_netcdf(grid)
            with self.assertRaisesRegex(ValueError, "grid debit"):
                export_discharge(grid, root / "wrong.csv", "Papua")
            self.assertFalse((root / "wrong.csv").exists())

    def test_shapefile_zip_matches_alternative_ids_and_preserves_existing_export_on_failure(self):
        import geopandas as gpd
        from shapely.geometry import Polygon

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            basin = root / "basins.gpkg"
            polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])
            gpd.GeoDataFrame({"value": [999, 999], "station_id": [12, 999],
                              "basin_id": [None, "Papua_7"]},
                             geometry=[polygon, polygon], crs="EPSG:4326").to_file(basin)
            destination = root / "basins.zip"
            self.assertEqual(export_basin_shapefile(basin, destination, "Papua", [12, 7]), 0)
            self.assertEqual(gpd.read_file(destination).basin_id.tolist(), ["Papua_12", "Papua_7"])
            self.assertFalse((root / "basins.shp").exists())
            original = destination.read_bytes()
            with self.assertRaises(ValueError):
                export_basin_shapefile(basin, destination, "Papua", [100])
            self.assertEqual(destination.read_bytes(), original)
            with patch.object(gpd.GeoDataFrame, "to_file", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    export_basin_shapefile(basin, destination, "Papua", [12, 7])
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(set(root.iterdir()), {basin, destination})

    def test_dropdown_updates_map_exports_and_reuses_all_scale_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "output.csv"
            pd.DataFrame({"time": pd.date_range("2000-01-01", periods=72, freq="MS"),
                          "river_q_12": 2 + np.arange(72) % 37}).to_csv(source, index=False)
            page = ResultsPage(lambda: SimpleNamespace(name="Papua", root=root), None)
            try:
                page.output_tab.load_file(source)
                self.wait_for(lambda: page.output_tab.current_series is not None)
                page.ssfi_tab._compute()
                self.wait_for(lambda: page.ssfi_tab._job is None)
                self.assertEqual(set(page.ssfi_tab.frames), {1, 3, 9, 12})
                page.ssfi_tab.set_basin_layers(["basins"])
                emitted = []
                page.ssfi_tab.show_requested.connect(lambda layer, values: emitted.append(values))
                with patch.object(page, "_show_ssfi"):
                    page.ssfi_tab.show.setChecked(True)
                    period = page.ssfi_tab._selected_period()
                    page.ssfi_tab.scale.setCurrentIndex(page.ssfi_tab.scale.findData(9))
                self.assertEqual(page.ssfi_tab._selected_period(), period)
                self.assertEqual(page.ssfi_tab.current_values, emitted[-1])
                self.assertEqual(page.ssfi_tab.frame.iloc[-1, 0], page.ssfi_tab.frames[9].iloc[-1, 0])
                self.assertIn("SSFI-9", page.export_tab.excel.text())
                expected = page.ssfi_tab.frame.copy()
                hidden = []
                page.ssfi_tab.hide_requested.connect(lambda: hidden.append(True))
                with patch("hydrohub.ui.result_widgets.build_ssfi_table", side_effect=AssertionError("cache missed")):
                    page.ssfi_tab._compute()
                    self.wait_for(lambda: page.ssfi_tab._job is None)
                self.assertEqual(hidden, [True])
                pd.testing.assert_frame_equal(page.ssfi_tab.frame, expected, check_freq=False)
                self.assertIn("dimuat dari cache", page.ssfi_tab.status.text())
                destination = root / "ssfi.xlsx"
                with patch("hydrohub.ui.result_pages.QFileDialog.getSaveFileName", return_value=(str(destination), "")):
                    page._export_ssfi_excel()
                self.wait_for(lambda: page._export_job is None)
                exported = pd.read_excel(destination, sheet_name="SSFI-9")
                self.assertEqual(exported.columns.tolist(), ["period", "Papua_12"])
                np.testing.assert_allclose(exported.Papua_12, expected["12"], equal_nan=True)
                self.assertTrue(page.export_tab.discharge.isEnabled())
            finally:
                page.close_resources()
                page.close()
                page.deleteLater()


if __name__ == "__main__":
    unittest.main()
