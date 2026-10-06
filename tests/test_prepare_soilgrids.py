import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from hydrohub.tools.prepare_soilgrids import DEPTHS, SOIL_PROPERTY_MAP, main


class PrepareSoilGridsTests(unittest.TestCase):
    def test_rejects_conflicting_input_crs(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, prepared = Path(tmp) / "raw", Path(tmp) / "prepared"
            raw.mkdir()
            with rasterio.open(
                raw / "bdod_0-5cm_mean.tif", "w", driver="GTiff", width=16,
                height=16, count=1, dtype="int16", crs="EPSG:4326",
                transform=from_origin(12600000, 1000, 250, 250),
            ) as dst:
                dst.write(np.ones((16, 16), dtype="int16"), 1)
            with patch.object(sys, "argv", ["prepare_soilgrids", "--input-dir", str(raw), "--output-dir", str(prepared)]):
                with self.assertRaisesRegex(ValueError, "Unexpected SoilGrids CRS"):
                    main()

    def test_missing_input_crs_produces_clippable_homolosine_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, prepared = Path(tmp) / "raw", Path(tmp) / "prepared"
            raw.mkdir()
            profile = {
                "driver": "GTiff", "height": 16, "width": 16, "count": 1,
                "dtype": "int16", "transform": from_origin(12600000, 1000, 250, 250),
            }
            for name in SOIL_PROPERTY_MAP:
                for depth in DEPTHS:
                    with rasterio.open(raw / f"{name}_{depth}_mean.tif", "w", **profile) as dst:
                        dst.write(np.full((16, 16), 100, dtype="int16"), 1)

            with patch.object(sys, "argv", ["prepare_soilgrids", "--input-dir", str(raw), "--output-dir", str(prepared)]):
                self.assertEqual(main(), 0)

            outputs = list(prepared.glob("*.tif"))
            self.assertEqual(len(outputs), 37)
            for path in outputs:
                with rasterio.open(path) as src:
                    self.assertEqual(src.crs, rasterio.crs.CRS.from_string("ESRI:54052"))
                    west, south, east, north = transform_bounds(
                        "EPSG:4326", src.crs, 108.6846, -4.7049, 118.9884, 4.4088
                    )
                    self.assertLess(src.bounds.left, east)
                    self.assertGreater(src.bounds.right, west)
                    self.assertLess(src.bounds.bottom, north)
                    self.assertGreater(src.bounds.top, south)


if __name__ == "__main__":
    unittest.main()
