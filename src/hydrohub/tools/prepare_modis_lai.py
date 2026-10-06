from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Convert 12-band MODIS LAI TIFF to monthly NetCDF")
    result.add_argument("--input-file", type=output_path, required=True)
    result.add_argument("--output-file", type=output_path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    pandas = require("pandas")
    xr = require("xarray")
    rasterio = require("rasterio")

    with rasterio.open(args.input_file) as src:
        if src.count != 12:
            raise ValueError(f"Expected 12 bands, found {src.count}")
        arr = src.read().astype("float32")
        transform = src.transform
        crs = src.crs
        nodata = src.nodata
        height, width = src.height, src.width

    bad = ~np.isfinite(arr)
    if nodata is not None:
        bad |= arr == nodata
    bad |= arr < 0
    arr = np.where(bad, np.nan, arr)

    x = transform.c + (np.arange(width) + 0.5) * transform.a
    y = transform.f + (np.arange(height) + 0.5) * transform.e
    time = pandas.date_range("2001-01-01", periods=12, freq="MS")

    da = xr.DataArray(arr, dims=("time", "y", "x"), coords={"time": time, "y": y, "x": x}, name="LAI",
                      attrs={"units": "-", "long_name": "Monthly LAI climatology"})
    ds = da.to_dataset()
    ds["spatial_ref"] = xr.DataArray(0)
    ds["spatial_ref"].attrs["crs_wkt"] = crs.to_wkt()
    ds["LAI"].attrs["grid_mapping"] = "spatial_ref"

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(args.output_file, encoding={"LAI": {"dtype": "float32", "zlib": True, "complevel": 4, "_FillValue": -9999.0}})

    coverage = float(np.isfinite(arr).mean() * 100)
    report = {
        "months": int(arr.shape[0]),
        "coverage_percent": coverage,
        "min": float(np.nanmin(arr)),
        "max": float(np.nanmax(arr)),
        "mean": float(np.nanmean(arr)),
        "output": str(args.output_file),
    }
    print(f"CREATED {args.output_file}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
