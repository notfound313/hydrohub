from __future__ import annotations

import argparse
import glob
import json

from hydrohub.tools.common import add_bbox, iso_date, output_path, require, validate_bbox

PRECIP_CANDIDATES = ("precipitation", "precipitationCal", "precip", "precipitation_rate", "rain")
LAT_CANDIDATES = ("lat", "latitude", "Latitude", "LAT")
LON_CANDIDATES = ("lon", "longitude", "Longitude", "LON")
TIME_CANDIDATES = ("time", "Time", "TIME")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Prepare GPM IMERG precipitation NetCDF for HydroMT-Wflow")
    result.add_argument("--input-glob", required=True)
    result.add_argument("--output-file", type=output_path, required=True)
    add_bbox(result)
    result.add_argument("--source-variable", default="auto", help="IMERG variable name; 'auto' detects.")
    result.add_argument("--source-units", default=None, help="Override units: mm/hr, mm/day or mm.")
    result.add_argument("--mask-negative", action=argparse.BooleanOptionalAction, default=True)
    return result


def _find_name(candidates, available):
    low = {str(k).lower(): k for k in available}
    for name in candidates:
        if name in available:
            return name
        if name.lower() in low:
            return low[name.lower()]
    return None


def _preprocess(ds):
    rename = {}
    lat = _find_name(LAT_CANDIDATES, list(ds.coords) + list(ds.dims))
    lon = _find_name(LON_CANDIDATES, list(ds.coords) + list(ds.dims))
    tim = _find_name(TIME_CANDIDATES, list(ds.coords) + list(ds.dims))
    if lat and lat != "latitude":
        rename[lat] = "latitude"
    if lon and lon != "longitude":
        rename[lon] = "longitude"
    if tim and tim != "time":
        rename[tim] = "time"
    return ds.rename(rename) if rename else ds


def _source_units(units, dt_h):
    u = (units or "").lower().replace(" ", "")
    if u in {"mm", "millimeter", "millimeters"}:
        return 1.0, "already_accumulated_mm"
    if any(v in u for v in ("mm/hr", "mm/h", "mmhr-1", "mmh-1", "mmhour-1", "mm/hour")):
        return dt_h, f"rate_mm_per_hour_x_{dt_h:g}_hours"
    if any(v in u for v in ("mm/day", "mm/d", "mmday-1", "mmd-1")):
        return dt_h / 24.0, f"rate_mm_per_day_x_{dt_h / 24.0:g}_days"
    raise ValueError(
        f"Unknown IMERG units {units!r}. Use --source-units mm/hr, mm/day or mm."
    )


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    np = require("numpy")
    pandas = require("pandas")
    xr = require("xarray")

    files = sorted(glob.glob(args.input_glob))
    if not files:
        raise FileNotFoundError(args.input_glob)

    try:
        ds = xr.open_mfdataset(files, combine="by_coords", preprocess=_preprocess, decode_times=True, mask_and_scale=True, chunks={"time": 336}, parallel=False)
    except Exception:
        ds = xr.open_mfdataset(files, combine="nested", concat_dim="time", preprocess=_preprocess, decode_times=True, mask_and_scale=True, chunks={"time": 336}, parallel=False)

    if not {"time", "latitude", "longitude"}.issubset(set(ds.coords) | set(ds.dims)):
        raise KeyError(f"Standard time/latitude/longitude not found. Coords={list(ds.coords)}")

    if args.source_variable != "auto":
        if args.source_variable not in ds.data_vars:
            raise KeyError(f"{args.source_variable!r} not found. Available={list(ds.data_vars)}")
        varname = args.source_variable
    else:
        varname = _find_name(PRECIP_CANDIDATES, list(ds.data_vars))
        if varname is None:
            raise KeyError(f"Cannot detect IMERG precipitation variable. Available={list(ds.data_vars)}")

    raw = ds[varname]
    for dim in [d for d in raw.dims if d not in {"time", "latitude", "longitude"}]:
        if raw.sizes[dim] == 1:
            raw = raw.isel({dim: 0}, drop=True)
        else:
            raise ValueError(f"Unsupported extra dimension {dim!r}, size={raw.sizes[dim]}")

    raw = raw.transpose("time", "latitude", "longitude").sortby("time")

    idx = pandas.DatetimeIndex(pandas.to_datetime(raw.time.values))
    dup = int(idx.duplicated().sum())
    if dup:
        _, unique_idx = np.unique(idx.values, return_index=True)
        raw = raw.isel(time=np.sort(unique_idx))
        idx = pandas.DatetimeIndex(pandas.to_datetime(raw.time.values))

    if len(idx) < 2:
        raise ValueError("Need at least two timestamps.")
    dh = np.diff(idx.asi8) / 3.6e12
    dh = dh[np.isfinite(dh) & (dh > 0)]
    if dh.size == 0:
        raise ValueError("Cannot infer positive timestep.")
    dt_h = float(np.median(dh))

    units = args.source_units or raw.attrs.get("units", "")
    factor, conversion = _source_units(units, dt_h)

    west, south, east, north = bbox
    raw = raw.sel(
        longitude=raw.longitude.where((raw.longitude >= west) & (raw.longitude <= east), drop=True),
        latitude=raw.latitude.where((raw.latitude >= south) & (raw.latitude <= north), drop=True),
    )
    if raw.sizes["latitude"] == 0 or raw.sizes["longitude"] == 0:
        raise ValueError("BBOX does not overlap IMERG coordinates.")

    neg = int((raw < 0).sum().compute())
    if args.mask_negative:
        raw = raw.where(raw >= 0)

    precip = (raw * factor).astype("float32").rename("precip")
    precip.attrs = {
        "long_name": "GPM IMERG precipitation accumulation per source timestep",
        "units": "mm",
        "source_variable": varname,
        "source_units": str(units),
        "source_timestep_hours": dt_h,
        "conversion": conversion,
        "source": "NASA GPM IMERG",
    }

    out = precip.to_dataset()
    out["latitude"].attrs.update({"standard_name": "latitude", "units": "degrees_north", "axis": "Y"})
    out["longitude"].attrs.update({"standard_name": "longitude", "units": "degrees_east", "axis": "X"})
    out["time"].attrs.update({"standard_name": "time", "axis": "T"})

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    out.to_netcdf(args.output_file, engine="netcdf4", encoding={"precip": {"dtype": "float32", "zlib": True, "complevel": 4, "_FillValue": np.float32(-9999.0)}})

    check = xr.open_dataset(args.output_file)
    p = check["precip"]
    report = {
        "input_file_count": len(files),
        "selected_variable": varname,
        "source_units": str(units),
        "source_timestep_hours": dt_h,
        "conversion": conversion,
        "duplicate_timestamps_removed": dup,
        "negative_source_values_masked": neg,
        "bbox": bbox,
        "output": str(args.output_file),
        "time_start": str(pandas.to_datetime(check.time.values[0])),
        "time_end": str(pandas.to_datetime(check.time.values[-1])),
        "time_count": int(check.sizes["time"]),
        "precip_min_mm_per_source_step": float(p.min(skipna=True)),
        "precip_max_mm_per_source_step": float(p.max(skipna=True)),
        "precip_mean_mm_per_source_step": float(p.mean(skipna=True)),
    }
    print(f"CREATED {args.output_file}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())