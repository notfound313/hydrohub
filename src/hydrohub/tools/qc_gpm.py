from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="QC GPM IMERG source and optional HydroMT forcing for duplicate/irregular time, negatives and coverage")
    result.add_argument("--input-file", type=output_path, required=True, help="Prepared IMERG precip NetCDF")
    result.add_argument("--hydromt-forcing", type=output_path, default=None, help="Optional HydroMT forcing NetCDF for ratio check")
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--min-mean-spatial-coverage", type=float, default=0.98)
    result.add_argument("--min-timestep-spatial-coverage", type=float, default=0.90)
    return result


def _get_precip(ds):
    for n in ("precip", "atmosphere_water__precipitation_volume_flux", "precipitation"):
        if n in ds:
            return ds[n]
    raise KeyError(f"No precipitation variable. Variables={list(ds.data_vars)}")


def _time_qc(values, pandas, np):
    idx = pandas.DatetimeIndex(pandas.to_datetime(values))
    result = {"count": len(idx), "start": str(idx[0]) if len(idx) else None, "end": str(idx[-1]) if len(idx) else None,
              "duplicates": int(idx.duplicated().sum()), "monotonic": bool(idx.is_monotonic_increasing)}
    if len(idx) >= 2:
        dh = np.diff(idx.asi8) / 3.6e12
        med = float(np.median(dh))
        result["median_timestep_hours"] = med
        result["min_timestep_hours"] = float(np.min(dh))
        result["max_timestep_hours"] = float(np.max(dh))
        result["irregular_intervals"] = int(np.sum(np.abs(dh - med) > max(1e-6, abs(med) * 1e-6)))
    else:
        result["median_timestep_hours"] = None
        result["irregular_intervals"] = None
    return result


def _precip_stats(da, np):
    spatial_dims = [d for d in da.dims if d != "time"]
    finite = np.isfinite(da)
    if spatial_dims:
        coverage_ts = finite.mean(dim=spatial_dims)
        domain_max = da.max(dim=spatial_dims, skipna=True)
    else:
        coverage_ts = finite.astype(float)
        domain_max = da
    return {
        "min": float(da.min(skipna=True).compute()),
        "max": float(da.max(skipna=True).compute()),
        "mean": float(da.mean(skipna=True).compute()),
        "negative_count": int((da < 0).sum().compute()),
        "nan_count": int(da.isnull().sum().compute()),
        "spatial_coverage_mean": float(coverage_ts.mean().compute()),
        "spatial_coverage_min_timestep": float(coverage_ts.min().compute()),
        "fully_dry_domain_timesteps": int((domain_max.fillna(0) == 0).sum().compute()),
        "domain_mean_total_mm": float(da.mean(dim=spatial_dims, skipna=True).sum(skipna=True).compute()),
    }


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    pandas = require("pandas")
    xr = require("xarray")

    if not args.input_file.exists():
        raise FileNotFoundError(args.input_file)

    result = {"source": {}, "hydromt_output": {}, "errors": [], "warnings": [], "pass": True}

    ds = xr.open_dataset(args.input_file, chunks={"time": 336})
    p = _get_precip(ds)
    tq = _time_qc(p.time.values, pandas, np)
    ps = _precip_stats(p, np)
    result["source"] = {"time": tq, "precip": ps, "units": p.attrs.get("units")}

    if tq["duplicates"] > 0:
        result["errors"].append("Duplicate IMERG timestamps.")
    if not tq["monotonic"]:
        result["errors"].append("IMERG time axis is not monotonic.")
    if tq["irregular_intervals"] not in (0, None):
        result["errors"].append("IMERG contains irregular/missing timesteps.")
    if ps["negative_count"] > 0:
        result["errors"].append("Negative precipitation remains.")
    if ps["spatial_coverage_mean"] < args.min_mean_spatial_coverage:
        result["errors"].append(f"Mean spatial coverage {ps['spatial_coverage_mean']:.3f} is too low.")
    if ps["spatial_coverage_min_timestep"] < args.min_timestep_spatial_coverage:
        result["errors"].append(f"Minimum timestep spatial coverage {ps['spatial_coverage_min_timestep']:.3f} is too low.")

    dt = tq["median_timestep_hours"]
    if dt is not None:
        if dt <= 0.75:
            limit = 100.0
        elif dt <= 1.5:
            limit = 150.0
        elif dt >= 20:
            limit = 500.0
        else:
            limit = 150.0 * dt
        n_extreme = int((p > limit).sum().compute())
        result["source"]["extreme_review_threshold_mm"] = limit
        result["source"]["extreme_cells_above_threshold"] = n_extreme
        if n_extreme > 0:
            result["warnings"].append(f"{n_extreme} values exceed review threshold {limit:g} mm/source-step.")
    if ps["fully_dry_domain_timesteps"] > 0:
        result["warnings"].append(f"{ps['fully_dry_domain_timesteps']} timesteps are dry over the full selected domain.")

    if args.hydromt_forcing is not None and args.hydromt_forcing.exists():
        hds = xr.open_dataset(args.hydromt_forcing, chunks={"time": 168})
        hp = _get_precip(hds)
        ht = _time_qc(hp.time.values, pandas, np)
        hs = _precip_stats(hp, np)
        result["hydromt_output"] = {"time": ht, "precip": hs, "units": hp.attrs.get("units")}
        if ht["duplicates"] > 0 or not ht["monotonic"]:
            result["errors"].append("HydroMT GPM forcing time axis is invalid.")
        if ht["irregular_intervals"] not in (0, None):
            result["errors"].append("HydroMT GPM forcing has irregular timesteps.")
        if hs["negative_count"] > 0:
            result["errors"].append("HydroMT GPM forcing contains negative precipitation.")
        src_total, out_total = ps["domain_mean_total_mm"], hs["domain_mean_total_mm"]
        if src_total > 0:
            ratio = out_total / src_total
            result["hydromt_output"]["source_output_total_ratio"] = ratio
            if ratio < 0.90 or ratio > 1.10:
                result["warnings"].append(f"HydroMT/source domain-mean total ratio={ratio:.3f}; review temporal aggregation.")
    else:
        result["hydromt_output"]["note"] = "HydroMT forcing file not found; source-only QC performed."

    if result["errors"]:
        result["pass"] = False

    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("QC PASS =", result["pass"])
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())