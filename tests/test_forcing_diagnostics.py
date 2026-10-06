import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from hydrohub.results.spatial import ForcingReader


class ForcingDiagnosticsTests(unittest.TestCase):
    def test_gaps_metadata_and_missing_grid_without_staticmaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "forcing.nc"
            xr.Dataset(
                {"rain": (("time", "y", "x"), [[[0., 1.]], [[np.nan, np.inf]], [[2., np.nan]]],
                          {"units": "mm/day", "long_name": "Rainfall"}),
                 "static": (("y", "x"), [[1., 2.]])},
                coords={"time": pd.to_datetime(["2020-01-04", "2020-01-01", "2020-01-01"]),
                        "y": [0.], "x": [0., 1.]},
            ).to_netcdf(path)
            reader = ForcingReader(path)
            try:
                info = reader.time_diagnostics("1D")
                self.assertEqual(info["missing"], 2)
                self.assertEqual(info["duplicates"], 1)
                self.assertFalse(info["ordered"])
                self.assertEqual(info["gaps"][0][0], "2020-01-02 00:00:00")
                self.assertEqual(reader.metadata()["rain"]["units"], "mm/day")
                self.assertIn("static", reader.metadata())
                frame, cells = reader.missing_values("rain")
                self.assertEqual(cells, 2)
                self.assertEqual(frame["missing_cells"].tolist(), [2, 1])
                self.assertEqual(frame["coverage_pct"].tolist(), [0., 50.])
                with self.assertRaises(ValueError):
                    reader.missing_values("static")
            finally:
                reader.close()

    def test_automatic_daily_interval_and_csv_invalid_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "forcing.csv"
            path.write_text("time,rain\n2020-01-01,0\n2020-01-02,1\n2020-01-04,\nbad,3\n2020-01-05,2\n")
            reader = ForcingReader(path)
            info = reader.time_diagnostics()
            self.assertEqual(info["invalid"], 1)
            self.assertEqual(info["count"], 5)
            self.assertEqual(info["missing"], 1)
            frame, cells = reader.missing_values("rain")
            self.assertEqual(cells, 1)
            self.assertEqual(len(frame), 1)


if __name__ == "__main__":
    unittest.main()
