from __future__ import annotations

import argparse
import glob
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Prepare direct GSMaP hourly rainfall into a daily HydroMT forcing file"
    )
    result.add_argument("--input-glob", required=True)
    result.add_argument("--output-file", type=output_path, required=True)
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--variable", default="precip")
    result.add_argument("--min-coverage", type=float, default=0.9)
    result.add_argument("--min-daily-coverage", type=float, default=0.90)
    result.add_argument("--fail-missing-days", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--extreme-review-mm", type=float, default=500.0)
    result.add_argument("--time-chunk", type=int, default=365)
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    pd = require("pandas")
    xr = require("xarray")

    files = sorted(glob.glob(args.input_glob))
    print(f"Matched {len(files)} files from {args.input_glob}")
    if not files:
        raise FileNotFoundError(f"No input files match: {args.input_glob}")
    if not 0 <= args.min_coverage <= 1 or not 0 <= args.min_daily_coverage <= 1:
        raise ValueError("Coverage thresholds must be between 0 and 1.")
    if args.dry_run:
        print("Configuration and input files valid. Dry run complete.")
        return 0

    ds = xr.open_mfdataset(
        files,
        combine="by_coords",
        data_vars="minimal",
        coords="minimal",
        compat="override",
        parallel=False,
        chunks={"time": args.time_chunk},
    )
    try:
        if args.variable not in ds:
            raise ValueError(f"Variable '{args.variable}' missing in input files.")
        p = ds[args.variable].transpose("time", "latitude", "longitude").sortby("time")

        time_raw = pd.to_datetime(p.time.values)
        step_hours = float(np.median(np.diff(time_raw.asi8) / 3.6e12)) if len(time_raw) >= 2 else 24.0
        if step_hours < 24.0:
            print(f"Detected {step_hours:g}-hourly timestep; aggregating to daily totals.")
            p = p.resample(time="D").sum(skipna=True)
        else:
            print(f"Detected {step_hours:g}-hourly timestep; already daily.")

        idx = pd.DatetimeIndex(pd.to_datetime(p.time.values)).normalize()
        duplicates = int(idx.duplicated().sum())
        if duplicates:
            _, keep = np.unique(idx.values, return_index=True)
            p = p.isel(time=np.sort(keep))
            idx = pd.DatetimeIndex(pd.to_datetime(p.time.values)).normalize()
        if len(idx) == 0:
            raise RuntimeError("No dates available in the input files.")

        expected = pd.date_range(idx.min(), idx.max(), freq="D")
        missing_dates = expected.difference(idx)
        if len(idx) >= 2:
            irregular = int(np.sum(np.abs(np.diff(idx.asi8) / 3.6e12 - 24.0) > 1e-6))
        else:
            irregular = 0

        negative_count = int((p < 0).sum().compute())
        coverage_by_day = np.isfinite(p).mean(dim=["latitude", "longitude"])
        mean_coverage = float(coverage_by_day.mean().compute())
        min_daily_coverage = float(coverage_by_day.min().compute())
        pmin = float(p.min(skipna=True).compute())
        pmax = float(p.max(skipna=True).compute())
        pmean = float(p.mean(skipna=True).compute())
        extreme_count = int((p > args.extreme_review_mm).sum().compute())
        domain_mean_total = float(
            p.mean(dim=["latitude", "longitude"], skipna=True).sum(skipna=True).compute()
        )

        errors = []
        warnings = []
        if duplicates > 0:
            warnings.append(f"{duplicates} duplicate dates were removed.")
        if irregular > 0:
            errors.append(f"irregular daily intervals: {irregular}")
        if len(missing_dates) > 0:
            msg = f"missing dates: {len(missing_dates)}"
            if args.fail_missing_days:
                errors.append(msg)
            else:
                warnings.append(msg)
        if negative_count > 0:
            errors.append(f"negative precipitation: {negative_count}")
        if mean_coverage < args.min_coverage:
            errors.append(f"mean coverage={mean_coverage:.4f}")
        if min_daily_coverage < args.min_daily_coverage:
            errors.append(f"minimum daily coverage={min_daily_coverage:.4f}")
        if extreme_count > 0:
            warnings.append(f"{extreme_count} cells > {args.extreme_review_mm:g} mm/day; review only.")

        report = {
            "input_file_count": len(files),
            "source_timestep_hours": step_hours,
            "time_start": str(idx.min()),
            "time_end": str(idx.max()),
            "time_count": int(len(idx)),
            "duplicates_removed": duplicates,
            "irregular_intervals": irregular,
            "missing_dates_count": int(len(missing_dates)),
            "missing_dates": [str(x.date()) for x in missing_dates],
            "precip_min_mm_day": pmin,
            "precip_max_mm_day": pmax,
            "precip_mean_mm_day": pmean,
            "negative_count": negative_count,
            "mean_spatial_coverage": mean_coverage,
            "minimum_daily_spatial_coverage": min_daily_coverage,
            "extreme_cells_gt_500mm_day": extreme_count,
            "domain_mean_total_mm": domain_mean_total,
            "errors": errors,
            "warnings": warnings,
            "qc_pass": len(errors) == 0,
        }
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if errors:
            raise RuntimeError("GSMaP direct source QC failed. HydroMT forcing was not written.")

        p = p.astype("float32")
        p.name = args.variable
        p.attrs.update(
            {"long_name": "Daily precipitation accumulation",
             "standard_name": "precipitation_amount",
             "units": "mm",
             "source": "JAXA GSMaP Gauge v8 Hourly",
             "temporal_resolution": "daily"}
        )
        out = p.to_dataset()
        out["latitude"].attrs.update({"standard_name": "latitude", "units": "degrees_north", "axis": "Y"})
        out["longitude"].attrs.update({"standard_name": "longitude", "units": "degrees_east", "axis": "X"})
        out["time"].attrs.update({"standard_name": "time", "axis": "T"})
        wkt = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
               'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]')
        out["spatial_ref"] = xr.DataArray(np.int32(0))
        out["spatial_ref"].attrs["spatial_ref"] = wkt
        out["spatial_ref"].attrs["crs_wkt"] = wkt
        out[args.variable].attrs["grid_mapping"] = "spatial_ref"

        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        out.to_netcdf(
            args.output_file,
            engine="netcdf4",
            encoding={
                args.variable: {
                    "dtype": "float32",
                    "zlib": True,
                    "complevel": 4,
                    "_FillValue": np.float32(-9999.0),
                    "chunksizes": (
                        min(args.time_chunk, int(out.sizes["time"])),
                        min(64, int(out.sizes["latitude"])),
                        min(128, int(out.sizes["longitude"])),
                    ),
                }
            },
        )
        print(f"WROTE {args.output_file}")
        print(f"REPORT {args.report_file}")
        print(json.dumps(report, indent=2))
    finally:
        ds.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())