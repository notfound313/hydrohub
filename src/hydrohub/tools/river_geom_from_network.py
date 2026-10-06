from __future__ import annotations

import argparse
import json
import math

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Sample MERIT river width along an existing river network GPKG and build HydroMT river_geom")
    result.add_argument("--river-gpkg", type=output_path, required=True)
    result.add_argument("--merit-width", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--sample-step-pixels", type=float, default=0.5)
    result.add_argument("--max-search-radius-pixels", type=float, default=2.5)
    result.add_argument("--width-statistic", default="median", choices=("median", "mean", "max", "p75"))
    result.add_argument("--min-rivwth-m", type=float, default=1.0)
    result.add_argument("--min-valid-sample-fraction", type=float, default=0.10)
    result.add_argument("--create-test-geom", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--test-rivdph-m", type=float, default=1.0)
    return result


def _densify_geometry(geom, step, LineString, MultiLineString, np):
    if geom is None or geom.is_empty or geom.length == 0:
        return []
    lines = geom.geoms if isinstance(geom, MultiLineString) else [geom]
    coords = []
    for line in lines:
        if not hasattr(line, "length") or line.is_empty:
            continue
        n = max(2, int(math.ceil(line.length / step)) + 1)
        distances = np.linspace(0.0, line.length, n)
        coords.extend((line.interpolate(float(d)).x, line.interpolate(float(d)).y) for d in distances)
    return coords


def _stat(values, method, np):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if values.size == 0:
        return np.nan
    if method == "mean":
        return float(np.mean(values))
    if method == "max":
        return float(np.max(values))
    if method == "p75":
        return float(np.percentile(values, 75))
    return float(np.median(values))


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    gpd = require("geopandas", "geopandas")
    rasterio = require("rasterio")
    distance_transform_edt = require("scipy.ndimage").distance_transform_edt
    LineString, MultiLineString = require("shapely.geometry").LineString, require("shapely.geometry").MultiLineString

    for p in (args.river_gpkg, args.merit_width):
        if not p.exists():
            raise FileNotFoundError(p)

    rivers = gpd.read_file(args.river_gpkg)
    if rivers.empty:
        raise RuntimeError("River GPKG is empty.")
    if rivers.crs is None:
        raise ValueError("River GPKG has no CRS.")

    with rasterio.open(args.merit_width) as src:
        raw = np.asarray(src.read(1), dtype=np.float32)
        raster_crs = src.crs
        transform = src.transform
        raster_nodata = src.nodata
        resx, resy = src.res
    if raster_crs is None:
        raise ValueError("MERIT width GeoTIFF has no CRS.")

    valid = np.isfinite(raw) & (raw > 0)
    if raster_nodata is not None:
        valid &= raw != raster_nodata
    width_raw = np.full(raw.shape, np.nan, dtype=np.float32)
    width_raw[valid] = raw[valid]
    if not np.any(valid):
        raise RuntimeError("No positive MERIT wth pixels found.")

    nearest_dist, nearest_idx = distance_transform_edt(~valid, return_distances=True, return_indices=True)
    nearest_width = width_raw[nearest_idx[0], nearest_idx[1]]

    rivers_sample = rivers.to_crs(raster_crs)
    pixel_size = float((abs(resx) + abs(resy)) / 2.0)
    sample_step = max(pixel_size * args.sample_step_pixels, pixel_size * 0.10)

    nrow, ncol = raw.shape
    inv = ~transform
    rivwth, coverage, source = [], [], []
    for geom in rivers_sample.geometry:
        coords = _densify_geometry(geom, sample_step, LineString, MultiLineString, np)
        dirs, nears = [], []
        for x, y in coords:
            colf, rowf = inv * (x, y)
            r, c = int(math.floor(rowf)), int(math.floor(colf))
            if not (0 <= r < nrow and 0 <= c < ncol):
                continue
            w = raw[r, c]
            if np.isfinite(w) and w > 0:
                dirs.append(float(w))
                nears.append(float(w))
            elif nearest_dist[r, c] <= args.max_search_radius_pixels and np.isfinite(nearest_width[r, c]) and nearest_width[r, c] > 0:
                nears.append(float(nearest_width[r, c]))
        n_tot = len(coords)
        frac = (len(nears) / n_tot) if n_tot > 0 else 0.0
        coverage.append(frac)
        if frac >= args.min_valid_sample_fraction:
            w = _stat(nears, args.width_statistic, np)
            rivwth.append(max(float(w), args.min_rivwth_m) if np.isfinite(w) else np.nan)
            source.append("MERIT_DIRECT" if len(dirs) == len(nears) else "MERIT_NEAREST")
        else:
            rivwth.append(np.nan)
            source.append("MISSING")

    rivers["rivwth"] = np.asarray(rivwth, dtype=float)
    rivers["wth_src"] = source
    rivers["wth_cov"] = np.asarray(coverage, dtype=float)

    valid_seg = np.isfinite(rivers["rivwth"]) & (rivers["rivwth"] > 0)
    missing_seg = ~valid_seg
    if valid_seg.any() and missing_seg.any():
        rproj = rivers.to_crs(3857)
        good = rproj.loc[valid_seg, ["rivwth", "geometry"]].copy()
        bad = rproj.loc[missing_seg, ["geometry"]].copy()
        joined = gpd.sjoin_nearest(bad, good, how="left", distance_col="fill_dist_m")
        joined = joined[~joined.index.duplicated(keep="first")]
        for idx, row in joined.iterrows():
            if np.isfinite(row.get("rivwth", np.nan)):
                rivers.loc[idx, "rivwth"] = max(float(row["rivwth"]), args.min_rivwth_m)
                rivers.loc[idx, "wth_src"] = "SEGMENT_NEAREST"

    valid_final = np.isfinite(rivers["rivwth"]) & (rivers["rivwth"] > 0)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    width_file = args.output_dir / "river_geom_jawa_width_only.gpkg"
    if width_file.exists():
        width_file.unlink()
    rivers.to_file(width_file, layer="river_geom", driver="GPKG")

    test_file = None
    if args.create_test_geom:
        test = rivers.copy()
        test["rivdph"] = float(args.test_rivdph_m)
        test["depth_src"] = "TEST_CONSTANT_ONLY"
        test_file = args.output_dir / "river_geom_jawa_TEST.gpkg"
        if test_file.exists():
            test_file.unlink()
        test.to_file(test_file, layer="river_geom", driver="GPKG")

    report = {
        "river_source": str(args.river_gpkg),
        "width_source": str(args.merit_width),
        "segments_total": int(len(rivers)),
        "segments_with_width_percent": float(100.0 * valid_final.sum() / len(rivers)),
        "segments_merit_direct": int((rivers["wth_src"] == "MERIT_DIRECT").sum()),
        "segments_merit_nearest_pixel": int((rivers["wth_src"] == "MERIT_NEAREST").sum()),
        "segments_nearest_segment_fill": int((rivers["wth_src"] == "SEGMENT_NEAREST").sum()),
        "segments_missing_width": int((~valid_final).sum()),
        "median_rivwth_m": float(np.nanmedian(rivers["rivwth"].values)),
        "width_only_file": str(width_file),
        "test_file": str(test_file) if test_file else None,
    }
    print(f"CREATED {width_file}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())