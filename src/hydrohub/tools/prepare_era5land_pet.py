from __future__ import annotations

import argparse
import glob
import json

from hydrohub.tools.common import add_bbox, iso_date, output_path, require, validate_bbox


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Prepare ERA5-Land PET daily for HydroMT-Wflow")
    result.add_argument("--input-glob", required=True)
    result.add_argument("--output-file", type=output_path, required=True)
    result.add_argument("--start-date", type=iso_date, required=True)
    result.add_argument("--end-date", type=iso_date, required=True)
    add_bbox(result)
    result.add_argument("--variable", default="potential_evaporation")
    result.add_argument("--fail-missing-days", action=argparse.BooleanOptionalAction, default=True)
    return result


def _rename_standard(ds):
    rename = {}
    if "valid_time" in ds.coords:
        rename["valid_time"] = "time"
    if "lat" in ds.coords:
        rename["lat"] = "latitude"
    if "lon" in ds.coords:
        rename["lon"] = "longitude"
    return ds.rename(rename) if rename else ds


def _crop_bbox(ds, bbox):
    west, south, east, north = bbox
    ds = ds.sel(
        longitude=ds.longitude.where((ds.longitude >= west) & (ds.longitude <= east), drop=True),
        latitude=ds.latitude.where((ds.latitude >= south) & (ds.latitude <= north), drop=True),
    )
    return ds


def _prep_daily(ds, bbox, variable):
    import numpy as np
    import pandas as pd

    ds = _rename_standard(ds)
    if "time" not in ds.coords:
        raise ValueError("time/valid_time coordinate not found.")

    candidates = [variable, "pev", "potential_evaporation"]
    var = next((v for v in candidates if v in ds.data_vars), None)
    if var is None:
        raise KeyError(f"No PET variable found. Available: {list(ds.data_vars)}")

    ds = ds[[var]]
    ds = _crop_bbox(ds, bbox)

    ti = pd.DatetimeIndex(pd.to_datetime(ds.time.values))
    if len(ti) < 1:
        raise ValueError("File has no timesteps.")

    if len(ti) > 1:
        dh = np.diff(ti.asi8) / 3.6e12
        median_h = float(np.median(dh))
    else:
        median_h = float("nan")

    if np.isfinite(median_h) and 0.5 <= median_h <= 1.5:
        keep = np.where(ti.hour == 0)[0]
        if len(keep) == 0:
            raise ValueError("No 00:00 UTC timestep found.")
        ds = ds.isel(time=keep)
        shifted = pd.DatetimeIndex(pd.to_datetime(ds.time.values)) - pd.Timedelta(days=1)
        ds = ds.assign_coords(time=shifted)
    elif np.isfinite(median_h) and 23.0 <= median_h <= 25.0:
        pass
    elif len(ti) > 1:
        raise ValueError(f"Unrecognized timestep interval: median {median_h:.3f} hours.")

    return ds


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    if args.start_date > args.end_date:
        raise ValueError("Start date must not be after end date.")

    np = require("numpy")
    pandas = require("pandas")
    xr = require("xarray")

    files = sorted(glob.glob(args.input_glob))
    if not files:
        raise FileNotFoundError(f"No files match: {args.input_glob}")

    print(f"Source files: {len(files)}")
    ds = xr.open_mfdataset(
        files,
        combine="by_coords",
        preprocess=lambda ds: _prep_daily(ds, bbox, args.variable),
        decode_times=True,
        mask_and_scale=True,
        chunks={"time": 31},
        parallel=False,
    )

    candidates = ["pev", "potential_evaporation"]
    raw = next((ds[v] for v in candidates if v in ds.data_vars), None)
    if raw is None:
        raise KeyError("PET variable not found after merge.")

    for d in [d for d in raw.dims if d not in {"time", "latitude", "longitude"}]:
        if raw.sizes[d] == 1:
            raw = raw.isel({d: 0}, drop=True)
        elif d.lower() in {"expver", "number"}:
            raw = raw.max(dim=d, skipna=True)
        else:
            raise ValueError(f"Unsupported dimension {d}: {raw.sizes[d]}")

    raw = raw.transpose("time", "latitude", "longitude").sortby("time")

    idx = pd.DatetimeIndex(pd.to_datetime(raw.time.values))
    if idx.duplicated().any():
        _, ii = np.unique(idx.values, return_index=True)
        raw = raw.isel(time=np.sort(ii))

    start_iso = args.start_date.isoformat()
    end_iso = args.end_date.isoformat()
    raw = raw.sel(time=slice(start_iso, end_iso))

    pet = (-raw * 1000.0).astype("float32")
    pet = pet.where(pet >= 0.0, 0.0).rename("pet")
    pet.attrs = {
        "units": "mm",
        "long_name": "ERA5-Land daily potential evaporation",
        "source": "Copernicus ERA5-Land",
        "temporal_resolution": "daily",
    }

    ti = pd.DatetimeIndex(pd.to_datetime(pet.time.values)).normalize()
    expected = pd.date_range(start_iso, end_iso, freq="D")
    missing = expected.difference(ti)
    if missing and args.fail_missing_days:
        raise RuntimeError(f"Forcing incomplete: {len(missing)} days missing.")

    if len(ti) > 1:
        dh = np.diff(ti.asi8) / 3.6e12
        if not np.allclose(dh, 24.0):
            raise ValueError("Output does not have consistent 24-hour intervals.")

    out = pet.to_dataset()
    out["latitude"].attrs.update({"standard_name": "latitude", "units": "degrees_north", "axis": "Y"})
    out["longitude"].attrs.update({"standard_name": "longitude", "units": "degrees_east", "axis": "X"})
    out["time"].attrs.update({"standard_name": "time", "axis": "T"})

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    lat_n = out.sizes["latitude"]
    lon_n = out.sizes["longitude"]
    out.to_netcdf(
        args.output_file,
        encoding={"pet": {"dtype": "float32", "zlib": True, "complevel": 4, "_FillValue": np.float32(-9999.0), "chunksizes": (min(31, out.sizes["time"]), lat_n, lon_n)}},
    )

    report = {
        "files": len(files),
        "output": str(args.output_file),
        "time_start": str(ti[0]) if len(ti) else None,
        "time_end": str(ti[-1]) if len(ti) else None,
        "time_count": len(ti),
        "expected_daily_count": len(expected),
        "missing_days": len(missing),
        "pet_min_mm_day": float(pet.min(skipna=True)),
        "pet_max_mm_day": float(pet.max(skipna=True)) if len(pet) else None,
        "pet_mean_mm_day": float(pet.mean(skipna=True)),
    }
    print(f"WROTE {args.output_file}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
