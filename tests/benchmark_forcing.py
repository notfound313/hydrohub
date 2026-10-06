"""Manual forcing benchmark: python -m tests.benchmark_forcing."""
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from hydrohub.results.spatial import ForcingReader


def main():
    with tempfile.TemporaryDirectory() as directory:
        for count in (30, 300, 1000):
            path = Path(directory) / f"forcing_{count}.nc"
            xr.Dataset(
                {"precip": (("time", "latitude", "longitude"),
                            np.arange(count * 64 * 64, dtype=np.float32).reshape(count, 64, 64))},
                coords={"time": pd.date_range("2020-01-01", periods=count, freq="h"),
                        "latitude": np.linspace(-7, -6, 64),
                        "longitude": np.linspace(106, 107, 64)},
            ).to_netcdf(path)
            start = time.perf_counter()
            reader = ForcingReader(path)
            try:
                reader.list_variables(), reader.time_values()
                loaded = time.perf_counter()
                reader.as_table("precip", limit=24)
                preview = time.perf_counter()
                value_range = reader.value_range("precip")
                scaled = time.perf_counter()
                reader.render_frame("precip", 0, 512, "Blues", value_range)
                warm = time.perf_counter()
                for index in range(1, 6):
                    reader.render_frame("precip", index, 512, "Blues", value_range)
                rendered = time.perf_counter()
            finally:
                reader.close()
            print(f"{count:4} steps | load {loaded-start:.3f}s | preview {preview-loaded:.3f}s "
                  f"| scale {scaled-preview:.3f}s | warm frame {(rendered-warm)/5:.3f}s")


if __name__ == "__main__":
    main()
