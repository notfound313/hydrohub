from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require

D8_CODES = (0, 1, 2, 4, 8, 16, 32, 64, 128, 247)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="QC + optionally repair a D8 flow network and rebuild the HydroMT hydrography mapstack")
    result.add_argument("--dem", type=output_path, required=True)
    result.add_argument("--flwdir", type=output_path, required=True)
    result.add_argument("--uparea", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--auto-repair", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--river-threshold-km2", type=float, default=50.0)
    result.add_argument("--subcatchment-min-area-km2", type=float, default=250.0)
    return result


def _same_transform(a, b, np, tol=1e-12):
    return np.allclose(np.asarray(tuple(a), float), np.asarray(tuple(b), float), rtol=0, atol=tol)


def _read_raster(path, band=1):
    rasterio = require("rasterio")
    with rasterio.open(path) as src:
        return src.read(band), src.profile.copy(), src.transform, src.crs, src.nodata, (src.height, src.width)


def _write_raster(path, arr, base_profile, dtype, nodata, rasterio, np):
    profile = base_profile.copy()
    profile.update(driver="GTiff", count=1, dtype=dtype, nodata=nodata, compress="LZW", tiled=True, BIGTIFF="IF_SAFER")
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.asarray(arr).astype(dtype), 1)
    print(f"[WRITE] {path}")


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    rasterio = require("rasterio")
    pyflwdir = require("pyflwdir", "pyflwdir")

    dem, dem_profile, dem_transform, dem_crs, dem_nodata, dem_shape = _read_raster(args.dem)
    d8_raw, d8_profile, d8_transform, d8_crs, d8_nodata, d8_shape = _read_raster(args.flwdir)
    upa_merit, upa_profile, upa_transform, upa_crs, _, upa_shape = _read_raster(
        args.uparea
    )

    if not (dem_shape == d8_shape == upa_shape):
        raise ValueError(f"Raster shapes differ: DEM={dem_shape}, D8={d8_shape}, UPA={upa_shape}")
    if not (dem_crs == d8_crs == upa_crs):
        raise ValueError(f"CRS differ: DEM={dem_crs}, D8={d8_crs}, UPA={upa_crs}")
    if not (
        _same_transform(dem_transform, d8_transform, np)
        and _same_transform(dem_transform, upa_transform, np)
    ):
        raise ValueError("Raster transforms differ.")
    if dem_crs is None:
        raise ValueError("Input raster has no CRS.")

    d8 = np.asarray(d8_raw).astype(np.uint16)
    if d8_nodata is not None:
        d8[d8_raw == d8_nodata] = 247
    unique = np.unique(d8)
    bad_codes = unique[~np.isin(unique, D8_CODES)]
    if bad_codes.size > 0:
        raise ValueError("Invalid D8 codes found: " + ", ".join(str(int(v)) for v in bad_codes[:30]))
    valid_mask = d8 != 247
    if not np.any(valid_mask):
        raise ValueError("No valid D8 cells.")

    latlon = bool(dem_crs.is_geographic)
    flw = pyflwdir.from_array(d8.astype(np.uint8), ftype="d8", check_ftype=True, mask=valid_mask, transform=dem_transform, latlon=latlon, cache=True)

    d8_before = flw.to_array(ftype="d8").copy()
    valid_before = bool(flw.isvalid)
    pits_before = len(flw.idxs_pit)

    repair_performed = False
    if not valid_before:
        if not args.auto_repair:
            raise RuntimeError("AUTO_REPAIR=False. Inspect the D8 first or enable AUTO_REPAIR.")
        flw.repair_loops()
        repair_performed = True

    d8_after = flw.to_array(ftype="d8").copy()
    valid_after = bool(flw.isvalid)
    pits_after = len(flw.idxs_pit)
    changed_mask = valid_mask & (d8_before != d8_after)
    n_changed = int(changed_mask.sum())
    if not valid_after:
        raise RuntimeError("Flow network remains invalid after repair.")

    _write_raster(args.output_dir / "d8_repair_mask.tif", changed_mask.astype(np.uint8), d8_profile, "uint8", 0, rasterio, np)
    _write_raster(args.output_dir / "d8_before_qc.tif", d8_before, d8_profile, "uint8", 247, rasterio, np)
    _write_raster(args.output_dir / "d8_after_qc.tif", d8_after, d8_profile, "uint8", 247, rasterio, np)

    pandas = None
    gpd = None
    try:
        import pandas as _pd

        pandas = _pd
    except Exception:
        pass
    try:
        import geopandas as _gpd

        gpd = _gpd
    except Exception:
        pass

    if n_changed > 0:
        rows, cols = np.where(changed_mask)
        xs, ys = rasterio.transform.xy(dem_transform, rows, cols, offset="center")
        if pandas is not None:
            pandas.DataFrame({
                "row": rows, "col": cols, "x": xs, "y": ys,
                "d8_old": d8_before[rows, cols].astype(int), "d8_new": d8_after[rows, cols].astype(int),
                "elev_m": dem[rows, cols], "upa_merit_km2": upa_merit[rows, cols],
            }).to_csv(args.output_dir / "d8_repair_cells.csv", index=False)
        if gpd is not None:
            gdf = gpd.GeoDataFrame(
                {"row": rows, "col": cols, "d8_old": d8_before[rows, cols].astype(int), "d8_new": d8_after[rows, cols].astype(int), "elev_m": dem[rows, cols], "upa_merit": upa_merit[rows, cols]},
                geometry=gpd.points_from_xy(xs, ys), crs=dem_crs,
            )
            gpkg = args.output_dir / "d8_repair_cells.gpkg"
            if gpkg.exists():
                gpkg.unlink()
            gdf.to_file(gpkg, layer="repair_cells", driver="GPKG")

    dem_float = np.asarray(dem, dtype=np.float32)
    if dem_nodata is not None:
        dem_float = np.where(dem == dem_nodata, np.nan, dem_float)
    dem_work = np.where(np.isfinite(dem_float), dem_float, -9999.0).astype(np.float32)
    elevtn = flw.dem_adjust(elevtn=dem_work).astype(np.float32)
    elevtn[~valid_mask] = -9999.0

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
    slope = pyflwdir.dem.slope(elevtn=elevtn, nodata=-9999.0, latlon=latlon, transform=dem_transform).astype(np.float32)
    slope[~valid_mask] = -9999.0
    river = ((uparea >= args.river_threshold_km2) & valid_mask).astype(np.uint8)
    subcatch, idxs_out = flw.subbasins_area(area_min=args.subcatchment_min_area_km2, uparea=np.where(valid_mask, uparea, 0.0))
    subcatch = subcatch.astype(np.int32)
    subcatch[~valid_mask] = 0

    hydro = args.output_dir / "hydrography_checked"
    _write_raster(hydro / "flwdir.tif", d8_after, d8_profile, "uint8", 247, rasterio, np)
    _write_raster(hydro / "elevtn.tif", elevtn, dem_profile, "float32", -9999.0, rasterio, np)
    _write_raster(hydro / "uparea.tif", uparea, upa_profile, "float64", -9999.0, rasterio, np)
    _write_raster(hydro / "strord.tif", strord, d8_profile, "uint8", 255, rasterio, np)
    _write_raster(hydro / "slope.tif", slope, dem_profile, "float32", -9999.0, rasterio, np)
    _write_raster(hydro / "basins.tif", basins, d8_profile, "int32", 0, rasterio, np)
    _write_raster(hydro / "ldd.tif", ldd, d8_profile, "uint8", 255, rasterio, np)
    _write_raster(hydro / "flowacc_cells.tif", flowacc, upa_profile, "float64", -9999.0, rasterio, np)
    _write_raster(hydro / "river.tif", river, d8_profile, "uint8", 0, rasterio, np)
    _write_raster(hydro / "subcatchment_qc.tif", subcatch, d8_profile, "int32", 0, rasterio, np)

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
        "valid_cells": int(flw.nnodes),
        "flow_valid_before": valid_before,
        "flow_valid_after": valid_after,
        "repair_performed": repair_performed,
        "d8_cells_changed": n_changed,
        "pits_before": int(pits_before),
        "pits_after": int(pits_after),
        "river_threshold_km2": args.river_threshold_km2,
        "subcatchment_min_area_km2": args.subcatchment_min_area_km2,
        "n_subcatchments": int(np.max(subcatch)),
        "upa_comparison": {"mae_km2": mae, "rmse_km2": rmse, "max_abs_difference_km2": max_abs},
        "hydromt_ready": bool(valid_after),
    }
    (args.output_dir / "flow_network_qc_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"CREATED {hydro}")
    print(json.dumps(report, indent=2))
    return 0 if report["hydromt_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
