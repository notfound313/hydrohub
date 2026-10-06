from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Rebuild D8 flow direction directly from DEM via pyflwdir fill_depressions")
    result.add_argument("--dem", type=output_path, required=True)
    result.add_argument("--uparea", type=output_path, required=True, help="MERIT upstream area (km2) for cross-check")
    result.add_argument("--old-d8", type=output_path, default=None, help="Optional old repaired D8 for hole comparison")
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--outlets", default="edge", choices=("edge", "min"))
    result.add_argument("--max-depth", type=float, default=-1.0)
    result.add_argument("--river-threshold-km2", type=float, default=50.0)
    return result


def _read_raster(path, band=1):
    rasterio = require("rasterio")
    with rasterio.open(path) as src:
        return src.read(band), src.profile.copy(), src.transform, src.crs, src.nodata, (src.height, src.width)


def _write_raster(path, arr, base_profile, dtype, nodata, rasterio):
    profile = base_profile.copy()
    profile.update(driver="GTiff", count=1, dtype=dtype, nodata=nodata, compress="LZW", tiled=True, BIGTIFF="IF_SAFER")
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(__import__("numpy").asarray(arr).astype(dtype), 1)
    print(f"[WRITE] {path}")


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    rasterio = require("rasterio")
    pyflwdir = require("pyflwdir", "pyflwdir")

    dem_raw, dem_profile, dem_transform, dem_crs, dem_nodata, dem_shape = _read_raster(args.dem)
    upa_merit, _, _, _, _, upa_shape = _read_raster(args.uparea)

    if dem_shape != upa_shape:
        raise ValueError(f"Raster shapes differ: DEM={dem_shape}, UPA={upa_shape}")

    latlon = bool(dem_crs.is_geographic)
    dem_nd = -9999.0
    dem = np.asarray(dem_raw, dtype=np.float32)
    if dem_nodata is not None and dem_nodata != dem_nd:
        dem = np.where(dem_raw == dem_nodata, dem_nd, dem)
    valid_mask = dem != dem_nd

    elevtn_filled, d8_new = pyflwdir.dem.fill_depressions(
        elevtn=dem, outlets=args.outlets, nodata=dem_nd, max_depth=args.max_depth, connectivity=8,
    )

    flw = pyflwdir.from_array(
        d8_new.astype(np.uint8), ftype="d8", check_ftype=True, mask=valid_mask, transform=dem_transform,
        latlon=latlon, cache=True,
    )

    is_valid = bool(flw.isvalid)
    n_pits = len(flw.idxs_pit)
    if not is_valid:
        raise RuntimeError("DEM-derived flow network is still invalid.")

    elevtn = flw.dem_adjust(elevtn=np.asarray(elevtn_filled, dtype=np.float32)).astype(np.float32)
    elevtn[~valid_mask] = dem_nd

    uparea = flw.upstream_area(unit="km2").astype(np.float64)
    uparea[~valid_mask] = -9999.0
    flowacc = flw.upstream_area(unit="cell").astype(np.float64)
    flowacc[~valid_mask] = -9999.0
    strord = flw.stream_order().astype(np.uint8)
    strord[~valid_mask] = 255
    basins = flw.basins(idxs=flw.idxs_pit).astype(np.int32)
    basins[~valid_mask] = 0
    ldd = flw.to_array(ftype="ldd").astype(np.uint8)
    ldd[~valid_mask] = 255
    slope = pyflwdir.dem.slope(elevtn=elevtn, nodata=dem_nd, latlon=latlon, transform=dem_transform).astype(np.float32)
    slope[~valid_mask] = -9999.0
    river = ((uparea >= args.river_threshold_km2) & valid_mask).astype(np.uint8)
    d8_out = flw.to_array(ftype="d8").astype(np.uint8)
    d8_out[~valid_mask] = 247

    hydro = args.output_dir / "hydrography_rebuilt"
    d8_profile = dem_profile.copy()
    _write_raster(hydro / "flwdir.tif", d8_out, d8_profile, "uint8", 247, rasterio)
    _write_raster(hydro / "elevtn.tif", elevtn, dem_profile, "float32", dem_nd, rasterio)
    _write_raster(hydro / "uparea.tif", uparea, dem_profile, "float64", -9999.0, rasterio)
    _write_raster(hydro / "strord.tif", strord, d8_profile, "uint8", 255, rasterio)
    _write_raster(hydro / "slope.tif", slope, dem_profile, "float32", -9999.0, rasterio)
    _write_raster(hydro / "basins.tif", basins, d8_profile, "int32", 0, rasterio)
    _write_raster(hydro / "ldd.tif", ldd, d8_profile, "uint8", 255, rasterio)
    _write_raster(hydro / "flowacc_cells.tif", flowacc, dem_profile, "float64", -9999.0, rasterio)
    _write_raster(hydro / "river.tif", river, d8_profile, "uint8", 0, rasterio)

    holes_before = holes_after = holes_closed = None
    if args.old_d8 is not None and args.old_d8.exists():
        with rasterio.open(args.old_d8) as src:
            d8_old_after = src.read(1)
        old_pit = valid_mask & (d8_old_after == 0)
        new_pit = valid_mask & (d8_out == 0)
        holes_before = int(old_pit.sum())
        holes_after = int(new_pit.sum())
        holes_closed = int((old_pit & ~new_pit).sum())

    upa_merit_float = np.asarray(upa_merit, dtype=float)
    compare = valid_mask & np.isfinite(upa_merit_float) & (upa_merit_float >= 0) & np.isfinite(uparea) & (uparea >= 0)
    if np.any(compare):
        diff = uparea[compare] - upa_merit_float[compare]
        mae, rmse, max_abs = float(np.mean(np.abs(diff))), float(np.sqrt(np.mean(diff**2))), float(np.max(np.abs(diff)))
    else:
        mae = rmse = max_abs = None

    report = {
        "output_dir": str(args.output_dir),
        "crs": str(dem_crs),
        "shape": list(dem_shape),
        "method": "pyflwdir.dem.fill_depressions() direct D8 derivation (no repair_loops)",
        "outlets_mode": args.outlets,
        "max_depth": args.max_depth,
        "valid_cells": int(flw.nnodes),
        "flow_valid": is_valid,
        "pits": int(n_pits),
        "river_threshold_km2": args.river_threshold_km2,
        "upa_comparison": {"mae_km2": mae, "rmse_km2": rmse, "max_abs_difference_km2": max_abs},
        "hole_comparison_vs_old_repair_loops_output": {"old_pit_cells": holes_before, "new_pit_cells": holes_after, "old_pit_cells_resolved": holes_closed},
        "hydromt_ready": is_valid,
    }
    (args.output_dir / "flow_network_rebuild_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"CREATED {hydro}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())