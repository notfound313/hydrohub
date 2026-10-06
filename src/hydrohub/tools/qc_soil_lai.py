from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require

REQUIRED_SOIL_FILES = (
    [f"{prop}_sl{i}.tif" for prop in ("bd", "clyppt", "sltppt", "sndppt", "oc", "ph") for i in range(1, 7)]
    + ["soilthickness.tif"]
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="QC soil properties, LAI, and staticmaps consistency")
    result.add_argument("--soil-dir", type=output_path, default=None)
    result.add_argument("--lai-file", type=output_path, default=None)
    result.add_argument("--staticmaps", type=output_path, default=None)
    result.add_argument("--report-file", type=output_path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    xr = require("xarray")
    rasterio = require("rasterio")

    result = {"pass": True, "warnings": [], "errors": [], "lai": {}, "soil": {}, "staticmaps": {}}

    # --- LAI ---
    if args.lai_file is not None and args.lai_file.exists():
        ds = xr.open_dataset(args.lai_file)
        a = np.asarray(ds["LAI"].values, float)
        v = np.isfinite(a)
        vals = a[v]
        result["lai"] = {
            "months": int(a.shape[0]),
            "coverage_percent": float(v.mean() * 100),
            "min": float(vals.min()),
            "max": float(vals.max()),
            "mean": float(vals.mean()),
        }
        if a.shape[0] != 12:
            result["pass"] = False
            result["errors"].append("LAI must have 12 months")
        if np.any(vals < 0):
            result["pass"] = False
            result["errors"].append("negative LAI")
        if np.any(vals > 10):
            result["warnings"].append("LAI >10: check scaling/QC")
        if v.mean() < 0.90:
            result["warnings"].append("LAI coverage <90%")
    elif args.lai_file is not None:
        result["pass"] = False
        result["errors"].append(f"LAI file not found: {args.lai_file}")

    # --- SOIL ---
    if args.soil_dir is not None and args.soil_dir.exists():
        missing = [f for f in REQUIRED_SOIL_FILES if not (args.soil_dir / f).exists()]
        if missing:
            result["pass"] = False
            result["errors"].append(f"missing soil files: {missing}")
        else:
            arrays = {}
            ref = None
            for f in REQUIRED_SOIL_FILES:
                with rasterio.open(args.soil_dir / f) as s:
                    a = s.read(1).astype(float)
                    valid = np.isfinite(a)
                    if s.nodata is not None:
                        valid &= a != s.nodata
                    sig = (s.shape, s.crs, s.transform)
                    if ref is None:
                        ref = sig
                    elif s.shape != ref[0] or s.crs != ref[1] or not s.transform.almost_equals(ref[2]):
                        result["pass"] = False
                        result["errors"].append(f"grid mismatch {f}")
                    arrays[f] = np.where(valid, a, np.nan)

            for i in range(1, 7):
                clay = arrays[f"clyppt_sl{i}.tif"]
                silt = arrays[f"sltppt_sl{i}.tif"]
                sand = arrays[f"sndppt_sl{i}.tif"]
                sm = clay + silt + sand
                ok = np.isfinite(sm)
                if ok.any():
                    p5, p95 = np.nanpercentile(sm[ok], [5, 95])
                    result["soil"][f"texture_sum_sl{i}_p05_p95"] = [float(p5), float(p95)]
                    if p5 < 90 or p95 > 110:
                        result["warnings"].append(f"texture sum layer {i} not near 100%")
                for b in ("clyppt", "sltppt", "sndppt"):
                    x = arrays[f"{b}_sl{i}.tif"]
                    if np.nanmin(x) < 0 or np.nanmax(x) > 100:
                        result["pass"] = False
                        result["errors"].append(f"{b}_sl{i} outside 0-100%")
                ph = arrays[f"ph_sl{i}.tif"]
                if np.nanmin(ph) < 2 or np.nanmax(ph) > 12:
                    result["warnings"].append(f"ph_sl{i} broad-range warning")
    elif args.soil_dir is not None:
        result["pass"] = False
        result["errors"].append(f"soil directory not found: {args.soil_dir}")

    # --- STATICMAPS ---
    if args.staticmaps is not None and args.staticmaps.exists():
        ds = xr.open_dataset(args.staticmaps, mask_and_scale=False)
        for k in ("soil_theta_s", "soil_theta_r", "soil_ksat_vertical", "soil_thickness",
                   "soil_f", "soil_brooks_corey_c", "vegetation_leaf_area_index"):
            if k in ds:
                result["staticmaps"][k] = "found"
        if "soil_theta_s" in ds and "soil_theta_r" in ds:
            ts = np.asarray(ds["soil_theta_s"].values, float)
            tr = np.asarray(ds["soil_theta_r"].values, float)
            ok = np.isfinite(ts) & np.isfinite(tr) & (ts > -900) & (tr > -900)
            bad = ok & ((ts <= tr) | (ts > 1) | (tr < 0))
            result["staticmaps"]["invalid_theta_cells"] = int(bad.sum())
            if bad.any():
                result["pass"] = False
                result["errors"].append("invalid theta_s/theta_r")
    else:
        result["staticmaps"]["note"] = "optional staticmaps not found"

    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("QC PASS =", result["pass"])
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
