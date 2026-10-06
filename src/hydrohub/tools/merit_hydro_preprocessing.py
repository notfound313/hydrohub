from __future__ import annotations

import argparse
import json
import re
import warnings
from pathlib import Path

from hydrohub.tools.common import output_path, require

# PyFlwDir's Numba-backed DEM operations currently require affine 2.x.
# Project metadata pins affine<3.0 so fresh data-tools installs stay compatible.


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Convert a four-band MERIT Hydro GeoTIFF into HydroMT-ready hydrology rasters and vectors"
    )
    result.add_argument("--input-file", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--output-prefix", default="jawa")
    result.add_argument("--river-threshold-km2", type=float, default=50.0)
    result.add_argument("--subcatchment-min-area-km2", type=float, default=250.0)
    result.add_argument(
        "--export-river-vector",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return result


def _validated_prefix(value: str) -> str:
    prefix = value.strip().lower()
    if not prefix or not re.fullmatch(r"[a-z0-9_-]+", prefix):
        raise ValueError("Output prefix may only contain letters, numbers, underscores, and hyphens.")
    return prefix


def _write_raster(path: Path, array, base_profile: dict, dtype: str, nodata, rasterio, np, mask=None) -> Path:
    profile = base_profile.copy()
    profile.update(
        driver="GTiff",
        count=1,
        dtype=dtype,
        nodata=nodata,
        compress="LZW",
        tiled=True,
        BIGTIFF="IF_SAFER",
    )
    output = np.asarray(array).copy()
    if mask is not None:
        output[~mask] = nodata
    with rasterio.open(path, "w", **profile) as destination:
        destination.write(output.astype(dtype), 1)
    print(f"[WRITE] {path}")
    return path


def run(args: argparse.Namespace) -> dict:
    if not args.input_file.exists():
        raise FileNotFoundError(f"MERIT Hydro input does not exist: {args.input_file}")

    prefix = _validated_prefix(args.output_prefix)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    np = require("numpy")
    rasterio = require("rasterio")
    gpd = require("geopandas")
    pyflwdir = require("pyflwdir", "pyflwdir")

    with rasterio.open(args.input_file) as source:
        if source.count < 4:
            raise ValueError(
                f"MERIT GeoTIFF has {source.count} bands; expected elv, dir, upa, and upg as bands 1-4."
            )
        dem = source.read(1).astype(np.float32)
        merit_dir_raw = source.read(2)
        merit_upa = source.read(3).astype(np.float32)
        profile = source.profile.copy()
        transform = source.transform
        crs = source.crs
        direction_mask = source.read_masks(2) > 0
        direction_nodata = source.nodatavals[1]

    if crs is None:
        raise ValueError("MERIT Hydro raster has no CRS.")

    direction_float = merit_dir_raw.astype(np.float64)
    finite = np.isfinite(direction_float)
    rounded_direction = np.zeros(direction_float.shape, dtype=np.int16)
    rounded_direction[finite] = np.rint(direction_float[finite]).astype(np.int16)
    valid_codes = np.array([-1, 0, 1, 2, 4, 8, 16, 32, 64, 128], dtype=np.int16)
    valid_mask = finite & direction_mask & np.isin(rounded_direction, valid_codes)
    if direction_nodata is not None and np.isfinite(direction_nodata):
        valid_mask &= direction_float != direction_nodata
    if not np.any(valid_mask):
        raise ValueError("No valid MERIT D8 cells were found; check the band order and source export.")

    d8 = np.full(rounded_direction.shape, 247, dtype=np.uint8)
    d8[valid_mask] = np.where(
        rounded_direction[valid_mask] == -1,
        0,
        rounded_direction[valid_mask],
    ).astype(np.uint8)
    flow = pyflwdir.from_array(
        d8,
        ftype="d8",
        mask=valid_mask,
        transform=transform,
        latlon=bool(crs.is_geographic),
        cache=True,
    )

    outputs: list[Path] = []

    def raster(stem: str, array, dtype: str, nodata, mask=None) -> Path:
        path = args.output_dir / f"{stem}.tif"
        outputs.append(_write_raster(path, array, profile, dtype, nodata, rasterio, np, mask))
        return path

    dem_valid = valid_mask & np.isfinite(dem)
    raster(f"dem_{prefix}", dem, "float32", -9999.0, dem_valid)
    raster(f"d8_{prefix}", d8, "uint8", 247)

    ldd = flow.to_array(ftype="ldd").astype(np.uint8)
    raster(f"ldd_{prefix}", ldd, "uint8", 255, valid_mask)

    flow_accumulation = flow.upstream_area(unit="cell").astype(np.float32)
    raster(f"flowacc_{prefix}_cells", flow_accumulation, "float32", -9999.0, valid_mask)

    uparea_valid = valid_mask & np.isfinite(merit_upa) & (merit_upa >= 0)
    uparea_work = np.where(uparea_valid, merit_upa, 0.0).astype(np.float32)
    raster(f"uparea_{prefix}_merit_km2", merit_upa, "float32", -9999.0, uparea_valid)

    calculated_uparea = flow.upstream_area(unit="km2").astype(np.float32)
    raster(
        f"uparea_{prefix}_pyflwdir_km2",
        calculated_uparea,
        "float32",
        -9999.0,
        valid_mask,
    )

    river_mask = valid_mask & uparea_valid & (merit_upa >= args.river_threshold_km2)
    raster(f"river_{prefix}", river_mask.astype(np.uint8), "uint8", 0)

    stream_order = flow.stream_order(type="strahler", mask=river_mask).astype(np.uint8)
    stream_order[~river_mask] = 0
    raster(f"streamorder_{prefix}", stream_order, "uint8", 0)

    basins = flow.basins().astype(np.int32)
    basins[~valid_mask] = 0
    raster(f"basin_{prefix}", basins, "int32", 0)

    subcatchments, outlet_indexes = flow.subbasins_area(
        area_min=args.subcatchment_min_area_km2,
        uparea=uparea_work,
    )
    subcatchments = subcatchments.astype(np.int32)
    subcatchments[~valid_mask] = 0
    raster(f"subcatchment_{prefix}", subcatchments, "int32", 0)

    if len(outlet_indexes) > 0:
        xs, ys = flow.xy(outlet_indexes)
        outlet_path = args.output_dir / f"subcatchment_outlets_{prefix}.gpkg"
        outlet_path.unlink(missing_ok=True)
        outlets = gpd.GeoDataFrame(
            {"subbas_id": subcatchments.ravel()[outlet_indexes].astype(np.int32)},
            geometry=gpd.points_from_xy(xs, ys),
            crs=crs,
        )
        outlets.to_file(outlet_path, layer="subcatchment_outlets", driver="GPKG")
        outputs.append(outlet_path)
        print(f"[WRITE] {outlet_path}")

    river_segments = 0
    if args.export_river_vector:
        features = flow.streams(mask=river_mask, strord=stream_order, uparea=uparea_work)
        if len(features) > 0:
            rivers = gpd.GeoDataFrame.from_features(features, crs=crs)
            river_path = args.output_dir / f"river_network_{prefix}.gpkg"
            river_path.unlink(missing_ok=True)
            rivers.to_file(river_path, layer="river_network", driver="GPKG")
            outputs.append(river_path)
            river_segments = len(rivers)
            print(f"[WRITE] {river_path}")
        else:
            warnings.warn("No river vector was generated; try lowering the river threshold.")

    codes, counts = np.unique(d8[valid_mask], return_counts=True)
    report = {
        "input_file": str(args.input_file),
        "output_dir": str(args.output_dir),
        "output_prefix": prefix,
        "crs": str(crs),
        "shape": list(dem.shape),
        "valid_cells": int(flow.nnodes),
        "pits_or_outlets": int(len(flow.idxs_pit)),
        "d8_codes": dict(zip(map(str, codes.tolist()), map(int, counts.tolist()))),
        "river_threshold_km2": args.river_threshold_km2,
        "subcatchment_min_area_km2": args.subcatchment_min_area_km2,
        "river_cells": int(river_mask.sum()),
        "river_segments": river_segments,
        "outputs": [str(path) for path in outputs],
    }
    report_path = args.output_dir / "merit_hydro_preprocessing_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[WRITE] {report_path}")
    print(json.dumps(report, indent=2))
    return report


def main() -> int:
    run(parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
