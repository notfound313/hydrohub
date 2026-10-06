from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build MERIT river geometry (width) GeoPackage")
    result.add_argument("--staticmaps", type=output_path, required=True)
    result.add_argument("--merit-width", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--upstream-threshold-km2", type=float, default=50.0)
    result.add_argument("--min-rivwth", type=float, default=1.0)
    result.add_argument("--max-nearest-fill", type=float, default=3.0)
    return result


CANDIDATES = {
    "ldd": ["local_drain_direction", "wflow_ldd"],
    "sub": ["subcatchment", "wflow_subcatch"],
    "river": ["river_mask", "wflow_river"],
    "upa": ["meta_upstream_area", "wflow_uparea"],
    "strord": ["meta_streamorder", "wflow_streamorder"],
}


def _find_var(ds, names):
    low = {k.lower(): k for k in ds.data_vars}
    for n in names:
        if n in ds.data_vars:
            return n
        if n.lower() in low:
            return low[n.lower()]
    return None


def _grid_info(ds, da):
    from affine import Affine
    import numpy as np
    import rasterio

    yd, xd = da.dims[-2:]
    x = np.asarray(ds[xd].values, float)
    y = np.asarray(ds[yd].values, float)
    dx = float(np.median(np.diff(x)))
    dy = float(np.median(np.diff(y)))
    tr = Affine.translation(x[0] - dx / 2, y[0] - dy / 2) * Affine.scale(dx, dy)
    crs = None
    if "spatial_ref" in ds.variables:
        a = ds["spatial_ref"].attrs
        for k in ("crs_wkt", "spatial_ref"):
            if a.get(k):
                try:
                    crs = rasterio.crs.CRS.from_wkt(a[k])
                    break
                except Exception:
                    pass
    return tr, crs or rasterio.crs.CRS.from_epsg(4326)


def _write_tif(path, arr, tr, crs, dtype, nodata):
    import rasterio
    with rasterio.open(path, "w", driver="GTiff",
                       height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype=dtype, crs=crs, transform=tr, nodata=nodata,
                       compress="LZW", tiled=True, BIGTIFF="IF_SAFER") as dst:
        dst.write(arr.astype(dtype), 1)


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    xr = require("xarray")
    rasterio = require("rasterio")
    from rasterio.warp import reproject, Resampling

    if not args.staticmaps.exists():
        raise FileNotFoundError(args.staticmaps)
    if not args.merit_width.exists():
        raise FileNotFoundError(args.merit_width)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ds = xr.open_dataset(args.staticmaps, mask_and_scale=False)
    names = {k: _find_var(ds, v) for k, v in CANDIDATES.items()}
    if not names["ldd"]:
        raise KeyError("LDD not found in staticmaps.")

    da = ds[names["ldd"]]
    ldd = np.rint(np.asarray(da.values)).astype(np.int16)
    tr, crs = _grid_info(ds, da)

    active = (np.asarray(ds[names["sub"]].values) > 0) if names["sub"] else np.isin(ldd, np.arange(1, 10))
    valid = active & np.isin(ldd, np.arange(1, 10))

    ldd_pf = np.full(ldd.shape, 255, np.uint8)
    ldd_pf[valid] = ldd[valid].astype(np.uint8)

    pyflwdir = require("pyflwdir")
    flw = pyflwdir.from_array(ldd_pf, ftype="ldd", check_ftype=True,
                              mask=valid, transform=tr, latlon=crs.is_geographic, cache=True)
    if not flw.isvalid:
        raise RuntimeError("LDD is invalid.")

    upa = None
    if names["upa"]:
        raw_upa = np.asarray(ds[names["upa"]].values, float)
        raw_upa[np.isclose(raw_upa, (-9999, -999, 255))] = np.nan
        upa = raw_upa

    if names["river"]:
        river = np.asarray(ds[names["river"]].values) > 0
    elif upa is not None:
        river = upa >= args.upstream_threshold_km2
    else:
        raise KeyError("No river mask and no upstream area.")
    river &= valid

    with rasterio.open(args.merit_width) as src:
        raw = src.read(1).astype(np.float32)
        src_valid = np.isfinite(raw) & (raw > 0)
        if src.nodata is not None:
            src_valid &= raw != src.nodata
        srcdata = np.full(raw.shape, -9999.0, np.float32)
        srcdata[src_valid] = raw[src_valid]
        width = np.full(ldd.shape, -9999.0, np.float32)
        reproject(
            source=srcdata, destination=width,
            src_transform=src.transform, src_crs=src.crs, src_nodata=-9999.0,
            dst_transform=tr, dst_crs=crs, dst_nodata=-9999.0,
            resampling=Resampling.average,
        )

    width[width <= 0] = np.nan
    width[~river] = np.nan

    from scipy.ndimage import distance_transform_edt
    valid_w = np.isfinite(width)
    fillmask = np.zeros(width.shape, bool)
    if valid_w.any() and args.max_nearest_fill > 0:
        dist, idx = distance_transform_edt(~valid_w, return_indices=True)
        fillmask = river & (~valid_w) & (dist <= args.max_nearest_fill)
        rr = idx[0][fillmask]
        cc = idx[1][fillmask]
        width[fillmask] = width[rr, cc]

    width[river & np.isfinite(width) & (width < args.min_rivwth)] = args.min_rivwth

    coverage_before = float(100.0 * (river & np.isfinite(width)).sum() / max(1, river.sum()))
    _write_tif(args.output_dir / "merit_rivwth_on_model_grid.tif",
               np.where(np.isfinite(width), width, -9999.0), tr, crs, "float32", -9999.0)
    _write_tif(args.output_dir / "merit_rivwth_fill_mask.tif",
               fillmask.astype(np.uint8), tr, crs, "uint8", 0)

    kwargs = {"rivwth": width}
    if upa is not None:
        kwargs["uparea"] = upa
    if names["strord"]:
        kwargs["strord"] = np.asarray(ds[names["strord"]].values)

    geopandas = require("geopandas")
    features = flw.streams(mask=river & np.isfinite(width), **kwargs)
    gdf = geopandas.GeoDataFrame.from_features(features, crs=crs)
    gdf = gdf[np.isfinite(gdf["rivwth"]) & (gdf["rivwth"] > 0)].copy()
    gdf["wth_source"] = "MERIT_Hydro_GEE"

    gpkg = args.output_dir / "river_geom_merit_width.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    gdf.to_file(gpkg, layer="river_geom", driver="GPKG")

    report = {
        "river_cells": int(river.sum()),
        "filled_cells": int(fillmask.sum()),
        "segments": int(len(gdf)),
        "output": str(gpkg),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
