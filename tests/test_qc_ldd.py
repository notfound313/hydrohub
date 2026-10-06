import argparse
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from hydrohub.tools import qc_ldd


class QcLddTests(unittest.TestCase):
    def test_grid_validation_uses_uparea_transform_not_profile_attribute(self) -> None:
        args = argparse.Namespace(
            dem="dem.tif",
            flwdir="d8.tif",
            uparea="upa.tif",
            output_dir="output",
            auto_repair=True,
            river_threshold_km2=50.0,
            subcatchment_min_area_km2=250.0,
        )
        profile = {"driver": "GTiff"}
        array = np.zeros((2, 2), dtype=np.float32)
        crs = MagicMock()
        rasters = [
            (array, profile, (1, 0, 0, 0, -1, 0), crs, None, (2, 2)),
            (array, profile, (1, 0, 0, 0, -1, 0), crs, None, (2, 2)),
            (array, profile, (2, 0, 0, 0, -2, 0), crs, None, (2, 2)),
        ]
        parsed = MagicMock()
        parsed.parse_args.return_value = args

        with (
            patch.object(qc_ldd, "parser", return_value=parsed),
            patch.object(qc_ldd, "_read_raster", side_effect=rasters),
            patch.object(
                qc_ldd,
                "require",
                side_effect=lambda name, *_args: np if name == "numpy" else MagicMock(),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Raster transforms differ"):
                qc_ldd.main()


if __name__ == "__main__":
    unittest.main()
