from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


CANDIDATES = {
    "ldd": ("local_drain_direction", "wflow_ldd"),
    "subcatchment": ("subcatchment", "wflow_subcatch"),
    "uparea": ("meta_upstream_area", "wflow_uparea"),
    "streamorder": ("meta_streamorder", "wflow_streamorder"),
    "river": ("river_mask", "wflow_river"),
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build Java river geometry with MERIT width and QBankfull depth"
    )
    result.add_argument("--staticmaps", type=output_path, required=True)
    result.add_argument("--merit-width", type=output_path, required=True)
    result.add_argument("--qbankfull-gpkg", type=output_path, required=True)
    result.add_argument("--output-gpkg", type=output_path, required=True)
    result.add_argument("--river-upa-threshold-km2", type=float, default=50.0)
    result.add_argument("--max-fill-cells", type=int, default=3)
    result.add_argument("--min-rivwth", type=float, default=1.0)
    result.add_argument("--min-rivdph", type=float, default=1.0)
    return result


def _find_var(dataset, names: tuple[str, ...]) -> str | None:
    lower_names = {name.lower(): name for name in dataset.data_vars}
    for name in names:
        if name in dataset.data_vars:
            return name
        if name.lower() in lower_names:
            return lower_names[name.lower()]
    return None


def _qbankfull_column(columns) -> str:
    names = {str(column).lower(): str(column) for column in columns}
    for candidate in ("qbankfull", "qbf"):
        if candidate in names:
            return names[candidate]
    raise KeyError("QBankfull input must contain a 'qbankfull' or 'qbf' column.")


def _grid_info(dataset, data_array, np, rasterio):
    from affine import Affine

    y_dimension, x_dimension = data_array.dims[-2:]
    x = np.asarray(dataset[x_dimension].values, dtype=float)
    y = np.asarray(dataset[y_dimension].values, dtype=float)
    if x.size < 2 or y.size < 2:
        raise ValueError("Staticmaps coordinates must contain at least two cells per axis.")
    dx = float(np.median(np.diff(x)))
    dy = float(np.median(np.diff(y)))
    transform = Affine.translation(x[0] - dx / 2, y[0] - dy / 2) * Affine.scale(
        dx, dy
    )
    crs = None
    if "spatial_ref" in dataset.variables:
        attributes = dataset["spatial_ref"].attrs
        for key in ("crs_wkt", "spatial_ref"):
            if attributes.get(key):
                try:
                    crs = rasterio.crs.CRS.from_wkt(attributes[key])
                    break
                except Exception:
                    pass
    return transform, crs or rasterio.crs.CRS.from_epsg(4326)


def main() -> int:
    args = parser().parse_args()
    for path in (args.staticmaps, args.merit_width, args.qbankfull_gpkg):
        if not path.exists():
            raise FileNotFoundError(path)
    if args.max_fill_cells < 0:
        raise ValueError("Maximum fill cells cannot be negative.")
    if args.min_rivwth <= 0 or args.min_rivdph <= 0:
        raise ValueError("Minimum river width and depth must be positive.")

    np = require("numpy")
    xr = require("xarray")
    rasterio = require("rasterio")
    geopandas = require("geopandas")
    pyflwdir = require("pyflwdir")
    from pyproj import CRS
    from rasterio.warp import Resampling, reproject
    from scipy.ndimage import distance_transform_edt

    with xr.open_dataset(args.staticmaps, mask_and_scale=False) as dataset:
        names = {
            key: _find_var(dataset, candidates)
            for key, candidates in CANDIDATES.items()
        }
        if not names["ldd"]:
            raise KeyError("LDD variable was not found in staticmaps.")

        ldd_data = dataset[names["ldd"]]
        raw_ldd = np.asarray(ldd_data.values, dtype=float)
        ldd = np.where(np.isfinite(raw_ldd), np.rint(raw_ldd), 255).astype(
            np.int16
        )
        transform, crs = _grid_info(dataset, ldd_data, np, rasterio)
        active = np.ones(ldd.shape, dtype=bool)
        if names["subcatchment"]:
            active = np.asarray(dataset[names["subcatchment"]].values) > 0
        valid_ldd = active & np.isin(ldd, np.arange(1, 10))

        ldd_pyflwdir = np.full(ldd.shape, 255, dtype=np.uint8)
        ldd_pyflwdir[valid_ldd] = ldd[valid_ldd]
        flow = pyflwdir.from_array(
            ldd_pyflwdir,
            ftype="ldd",
            mask=valid_ldd,
            transform=transform,
            latlon=crs.is_geographic,
        )
        if not flow.isvalid:
            raise RuntimeError("Staticmaps LDD is invalid.")

        uparea = None
        if names["uparea"]:
            uparea = np.asarray(dataset[names["uparea"]].values, dtype=float)
            uparea[~np.isfinite(uparea) | (uparea <= 0)] = np.nan

        if names["river"]:
            river = np.asarray(dataset[names["river"]].values) > 0
        elif uparea is not None:
            river = uparea >= args.river_upa_threshold_km2
        else:
            raise KeyError("Staticmaps has no river mask or upstream area variable.")
        river &= valid_ldd

        streamorder = None
        if names["streamorder"]:
            streamorder = np.asarray(dataset[names["streamorder"]].values)

    with rasterio.open(args.merit_width) as source:
        raw_width = source.read(1).astype(np.float32)
        width_valid = np.isfinite(raw_width) & (raw_width > 0)
        if source.nodata is not None:
            width_valid &= raw_width != source.nodata
        source_width = np.full(raw_width.shape, -9999.0, dtype=np.float32)
        source_width[width_valid] = raw_width[width_valid]
        width = np.full(ldd.shape, -9999.0, dtype=np.float32)
        reproject(
            source=source_width,
            destination=width,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=-9999.0,
            dst_transform=transform,
            dst_crs=crs,
            dst_nodata=-9999.0,
            resampling=Resampling.average,
        )

    width = width.astype(float)
    width[width <= 0] = np.nan
    width[~river] = np.nan
    valid_width = np.isfinite(width)
    filled_cells = 0
    if valid_width.any() and args.max_fill_cells:
        distance, indexes = distance_transform_edt(
            ~valid_width, return_indices=True
        )
        fill_mask = river & ~valid_width & (distance <= args.max_fill_cells)
        width[fill_mask] = width[indexes[0][fill_mask], indexes[1][fill_mask]]
        filled_cells = int(fill_mask.sum())
    width[np.isfinite(width) & (width < args.min_rivwth)] = args.min_rivwth

    stream_options = {"rivwth": width}
    if uparea is not None:
        stream_options["uparea"] = uparea
    if streamorder is not None:
        stream_options["strord"] = streamorder
    features = flow.streams(mask=river & np.isfinite(width), **stream_options)
    river_geometry = geopandas.GeoDataFrame.from_features(features, crs=crs)
    if river_geometry.empty or "rivwth" not in river_geometry:
        raise RuntimeError("No river segments with valid MERIT width were created.")
    river_geometry = river_geometry[
        np.isfinite(river_geometry["rivwth"])
    ].copy().reset_index(drop=True)

    qbankfull = geopandas.read_file(args.qbankfull_gpkg)
    if qbankfull.empty:
        raise ValueError("QBankfull GeoPackage contains no features.")
    if qbankfull.crs is None:
        raise ValueError("QBankfull GeoPackage has no CRS.")
    qbankfull_column = _qbankfull_column(qbankfull.columns)
    qbankfull = qbankfull[[qbankfull_column, "geometry"]].rename(
        columns={qbankfull_column: "qbankfull"}
    )
    qbankfull["qbankfull"] = np.asarray(qbankfull["qbankfull"], dtype=float)
    qbankfull = qbankfull[
        np.isfinite(qbankfull["qbankfull"]) & (qbankfull["qbankfull"] > 0)
    ].copy()
    if qbankfull.empty:
        raise ValueError("QBankfull GeoPackage has no positive discharge values.")

    java_metric = CRS.from_proj4(
        "+proj=aeqd +lat_0=-7.5 +lon_0=110 +datum=WGS84 +units=m"
    )
    river_metric = river_geometry.to_crs(java_metric).reset_index(names="river_index")
    qbankfull_metric = qbankfull.to_crs(java_metric)
    nearest = geopandas.sjoin_nearest(
        river_metric,
        qbankfull_metric,
        how="left",
        distance_col="qbf_distance",
    )
    nearest = (
        nearest.sort_values(["river_index", "qbf_distance"], na_position="last")
        .drop_duplicates("river_index")
        .set_index("river_index")
    )
    river_geometry["qbankfull"] = nearest["qbankfull"].reindex(
        river_geometry.index
    )
    river_geometry["qbf_distance"] = nearest["qbf_distance"].reindex(
        river_geometry.index
    )

    valid_qbankfull = np.isfinite(river_geometry["qbankfull"]) & (
        river_geometry["qbankfull"] > 0
    )
    river_geometry["rivdph"] = np.nan
    river_geometry.loc[valid_qbankfull, "rivdph"] = np.maximum(
        args.min_rivdph,
        0.27 * river_geometry.loc[valid_qbankfull, "qbankfull"] ** 0.30,
    )
    river_geometry["depth_source"] = np.where(
        valid_qbankfull, "QBANKFULL_POWERLAW", "MISSING"
    )
    river_geometry["width_source"] = "MERIT"
    river_geometry = river_geometry.to_crs(4326)

    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if args.output_gpkg.exists():
        args.output_gpkg.unlink()
    river_geometry.to_file(args.output_gpkg, layer="river_geom", driver="GPKG")

    report = {
        "segments": int(len(river_geometry)),
        "qbankfull_valid": int(valid_qbankfull.sum()),
        "qbankfull_missing": int((~valid_qbankfull).sum()),
        "filled_width_cells": filled_cells,
        "output": str(args.output_gpkg),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
