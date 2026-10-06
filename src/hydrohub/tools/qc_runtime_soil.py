from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require

SOIL_CANDIDATES = {
    "theta_s": ("soil_theta_s", "thetaS", "thetas"),
    "theta_r": ("soil_theta_r", "thetaR", "thetar"),
    "ksat_vertical": ("soil_ksat_vertical", "KsatVer", "ksat_vertical"),
    "soil_f": ("soil_f", "f"),
    "brooks_corey_c": ("soil_brooks_corey_c", "c"),
    "soil_thickness": ("soil_thickness", "SoilThickness"),
    "ksat_vertical_factor": ("soil_ksat_vertical_factor", "KsatVerFrac", "kvfrac"),
}
FORCING_CANDIDATES = {
    "precip": ("precip", "atmosphere_water__precipitation_volume_flux", "P"),
    "pet": ("pet", "land_surface_water__potential_evaporation_volume_flux", "PET"),
    "temp": ("temp", "atmosphere_air__temperature", "TEMP"),
}
FILL_VALUES = (-9999.0, -999.0, 1.0e20, 9.96921e36)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Check soil/LAI/forcing staticmaps values for values that trigger Wflow runtime Int64(NaN)")
    result.add_argument("--staticmaps", type=output_path, required=True)
    result.add_argument("--forcing", type=output_path, default=None, help="Optional forcing NetCDF; static soil QC only if omitted")
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--forcing-time-chunk", type=int, default=100)
    return result


def _first_existing(ds, candidates):
    low = {k.lower(): k for k in ds.data_vars}
    for c in candidates:
        if c in ds.data_vars:
            return c
        if c.lower() in low:
            return low[c.lower()]
    return None


def _as_float(da, np):
    a = np.asarray(da.values, dtype=float)
    bad = ~np.isfinite(a)
    for v in FILL_VALUES:
        bad |= np.isclose(a, v, rtol=0, atol=1e-4)
    for source in (da.attrs, da.encoding):
        for key in ("_FillValue", "missing_value"):
            fv = source.get(key, None)
            if fv is not None:
                try:
                    bad |= np.isclose(a, float(fv), rtol=0, atol=1e-4)
                except Exception:
                    pass
    return a, bad


def _invalid_count_chunked(da, time_chunk, np, extra_negative=False):
    time_dim = da.dims[0]
    ntime = da.sizes[time_dim]
    fill_values = list(FILL_VALUES)
    for source in (da.attrs, da.encoding):
        for key in ("_FillValue", "missing_value"):
            fv = source.get(key, None)
            if fv is not None:
                try:
                    fill_values.append(float(fv))
                except Exception:
                    pass
    nbad = 0
    for start in range(0, ntime, time_chunk):
        chunk = da.isel({time_dim: slice(start, min(start + time_chunk, ntime))}).values
        chunk = np.asarray(chunk, dtype=np.float32)
        bad = ~np.isfinite(chunk)
        for v in fill_values:
            bad |= np.isclose(chunk, v, rtol=0, atol=1e-4)
        if extra_negative:
            bad |= chunk < 0
        nbad += int(bad.sum())
        del chunk, bad
    return nbad


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    xr = require("xarray")
    rasterio = require("rasterio")
    Affine = require("affine", "affine")

    if not args.staticmaps.exists():
        raise FileNotFoundError(args.staticmaps)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ds = xr.open_dataset(args.staticmaps, mask_and_scale=False)
    sub_name = _first_existing(ds, ("subcatchment", "wflow_subcatch"))
    if sub_name is None:
        raise KeyError("Could not find subcatchment / wflow_subcatch")

    sub, sub_bad = _as_float(ds[sub_name], np)
    active = (~sub_bad) & (sub > 0)
    while active.ndim > 2:
        active = active[0]
    spatial_shape = active.shape

    ydim, xdim = ds[sub_name].dims[-2], ds[sub_name].dims[-1]
    x = np.asarray(ds[xdim].values, float)
    y = np.asarray(ds[ydim].values, float)
    dx = float(np.median(np.diff(x)))
    dy = float(np.median(np.diff(y)))
    transform = Affine.translation(x[0] - dx / 2, y[0] - dy / 2) * Affine.scale(dx, dy)
    crs = "EPSG:4326"
    if "spatial_ref" in ds.variables:
        wkt = ds["spatial_ref"].attrs.get("crs_wkt") or ds["spatial_ref"].attrs.get("spatial_ref")
        if wkt:
            try:
                crs = rasterio.crs.CRS.from_wkt(wkt)
            except Exception:
                pass

    def write_mask(path, mask):
        with rasterio.open(path, "w", driver="GTiff", height=mask.shape[0], width=mask.shape[1], count=1, dtype="uint8", crs=crs, transform=transform, nodata=0, compress="LZW") as dst:
            dst.write(mask.astype(np.uint8), 1)
        print(f"[WRITE] {path}")

    report = {
        "staticmaps": str(args.staticmaps),
        "active_cells": int(active.sum()),
        "variables_found": {},
        "soil": {},
        "forcing": {},
        "errors": [],
        "warnings": [],
        "likely_runtime_nan_cells": 0,
        "pass": True,
    }

    invalid_union = np.zeros(spatial_shape, dtype=bool)
    arrays = {}
    for key, candidates in SOIL_CANDIDATES.items():
        name = _first_existing(ds, candidates)
        report["variables_found"][key] = name
        if name is None:
            if key != "ksat_vertical_factor":
                report["errors"].append(f"Required soil variable not found: {key}")
            continue
        da = ds[name]
        a, explicit_bad = _as_float(da, np)
        arrays[key] = a
        invalid = explicit_bad.copy()
        if key == "theta_s":
            invalid |= (a <= 0) | (a > 1)
        elif key == "theta_r":
            invalid |= (a < 0) | (a >= 1)
        elif key == "ksat_vertical":
            invalid |= a <= 0
        elif key == "soil_f":
            invalid |= a < 0
        elif key == "brooks_corey_c":
            invalid |= a <= 3
        elif key == "soil_thickness":
            invalid |= a <= 0
        elif key == "ksat_vertical_factor":
            invalid |= a <= 0

        inv = np.asarray(invalid, bool)
        while inv.ndim > 2:
            inv = np.any(inv, axis=0)
        if inv.shape != spatial_shape:
            raise ValueError(f"Cannot reduce mask to spatial shape {spatial_shape}; got {inv.shape}")
        invalid_active = inv & active
        invalid_union |= invalid_active

        finite_vals = a[np.isfinite(a)]
        report["soil"][key] = {
            "netcdf_variable": name,
            "dims": list(da.dims),
            "shape": list(da.shape),
            "invalid_active_cells": int(invalid_active.sum()),
        }
        if finite_vals.size:
            report["soil"][key]["min"] = float(np.nanmin(finite_vals))
            report["soil"][key]["max"] = float(np.nanmax(finite_vals))
        if invalid_active.any():
            report["errors"].append(f"{key} has {int(invalid_active.sum())} invalid active model cells")
            write_mask(args.output_dir / f"invalid_{key}.tif", invalid_active)

    if "theta_s" in arrays and "theta_r" in arrays:
        ts, tr = arrays["theta_s"], arrays["theta_r"]
        bad = (~np.isfinite(ts)) | (~np.isfinite(tr)) | (ts <= tr)
        while bad.ndim > 2:
            bad = np.any(bad, axis=0)
        bad2d = bad & active
        invalid_union |= bad2d
        report["soil"]["theta_s_le_theta_r_active_cells"] = int(bad2d.sum())
        if bad2d.any():
            report["errors"].append(f"theta_s <= theta_r in {int(bad2d.sum())} active cells")
            write_mask(args.output_dir / "invalid_theta_relationship.tif", bad2d)

    if args.forcing and args.forcing.exists():
        fds = xr.open_dataset(args.forcing, mask_and_scale=False)
        for key, candidates in FORCING_CANDIDATES.items():
            name = _first_existing(fds, candidates)
            if name is None:
                report["forcing"][key] = {"found": False}
                continue
            da = fds[name]
            nbad = _invalid_count_chunked(da, args.forcing_time_chunk, np, extra_negative=key in ("precip", "pet"))
            report["forcing"][key] = {"found": True, "netcdf_variable": name, "dims": list(da.dims), "shape": list(da.shape), "invalid_values_total": nbad}
            if nbad:
                report["errors"].append(f"Forcing {key} contains {nbad} NaN/fill/invalid values")
        fds.close()
    else:
        report["forcing"]["note"] = "Forcing file not found; static soil QC only."

    report["likely_runtime_nan_cells"] = int(invalid_union.sum())
    write_mask(args.output_dir / "invalid_soil_union.tif", invalid_union)
    if report["errors"]:
        report["pass"] = False

    report_path = args.output_dir / "wflow_runtime_nan_qc.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("QC PASS =", report["pass"])
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())