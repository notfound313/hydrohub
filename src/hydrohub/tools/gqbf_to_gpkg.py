from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import add_bbox, output_path, require, validate_bbox


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Convert GQBF Southsphere raster ZIP to a Java-only point GeoPackage")
    result.add_argument("--input-zip", type=output_path, required=True)
    result.add_argument("--output-gpkg", type=output_path, required=True)
    add_bbox(result)
    result.add_argument("--min-qbf", type=float, default=0.0)
    result.add_argument("--min-drainage-area", type=float, default=0.0)
    result.add_argument("--keep-every-n", type=int, default=1)
    return result


def _intersects(bounds, bbox):
    return not (bounds.right < bbox[0] or bounds.left > bbox[2] or bounds.top < bbox[1] or bounds.bottom > bbox[3])


def _clipped_window(src, bbox, Window, from_bounds):
    left = max(bbox[0], src.bounds.left)
    bottom = max(bbox[1], src.bounds.bottom)
    right = min(bbox[2], src.bounds.right)
    top = min(bbox[3], src.bounds.top)
    if left >= right or bottom >= top:
        return None
    win = from_bounds(left, bottom, right, top, src.transform).round_offsets().round_lengths()
    col0 = max(0, int(win.col_off))
    row0 = max(0, int(win.row_off))
    col1 = min(src.width, int(win.col_off + win.width))
    row1 = min(src.height, int(win.row_off + win.height))
    width = col1 - col0
    height = row1 - row0
    if width <= 0 or height <= 0:
        return None
    return Window(col0, row0, width, height)


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    np = require("numpy")
    pandas = require("pandas")
    gpd = require("geopandas", "geopandas")
    rasterio = require("rasterio")
    zipfile = __import__("zipfile")

    if not args.input_zip.exists():
        raise FileNotFoundError(args.input_zip)

    with zipfile.ZipFile(args.input_zip, "r") as zf:
        tif_members = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff")) and not n.endswith("/")]
    if not tif_members:
        raise RuntimeError("No TIFF files found inside ZIP.")

    zp = args.input_zip.resolve().as_posix()
    frames = []
    selected = []
    failed = []
    for member in tif_members:
        vsi = f"/vsizip/{zp}/{member}"
        try:
            with rasterio.open(vsi) as src:
                if src.count < 5:
                    failed.append({"member": member, "reason": f"{src.count} bands, expected 5"})
                    continue
                if not _intersects(src.bounds, bbox):
                    continue
                win = _clipped_window(src, bbox, rasterio.windows.Window, rasterio.windows.from_bounds)
                if win is None:
                    continue
                arr = src.read([1, 2, 3, 4, 5], window=win)
                win_transform = src.window_transform(win)
                qbf = np.asarray(arr[0], dtype=np.float64)
                drainage_area = np.asarray(arr[1], dtype=np.float64)
                river_width = np.asarray(arr[2], dtype=np.float64)
                grwl_value = np.asarray(arr[3], dtype=np.float64)
                is_mainstem = np.asarray(arr[4], dtype=np.float64)
                valid = np.isfinite(qbf) & np.isfinite(drainage_area) & (qbf > args.min_qbf) & (drainage_area > args.min_drainage_area) & (qbf != -1) & (drainage_area != -1)
                rows, cols = np.where(valid)
                n_raw = len(rows)
                if n_raw == 0:
                    selected.append({"member": member, "valid_pixels": 0, "kept_points": 0, "bounds": list(src.bounds)})
                    continue
                if args.keep_every_n > 1:
                    take = np.arange(0, n_raw, args.keep_every_n)
                    rows, cols = rows[take], cols[take]
                n_keep = len(rows)
                xs, ys = rasterio.transform.xy(win_transform, rows, cols, offset="center")
                df = pandas.DataFrame({
                    "qbf": qbf[rows, cols],
                    "drainage_area": drainage_area[rows, cols],
                    "grwl_width_median": river_width[rows, cols],
                    "grwl_value": grwl_value[rows, cols].astype(np.int32),
                    "is_mainstem": is_mainstem[rows, cols].astype(np.int16),
                    "tile": member,
                })
                frames.append(gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(xs, ys), crs=src.crs))
                selected.append({"member": member, "valid_pixels": int(n_raw), "kept_points": int(n_keep), "bounds": list(src.bounds)})
                print(f"[SELECTED] {member} | valid={n_raw:,} | kept={n_keep:,}")
        except Exception as exc:
            failed.append({"member": member, "reason": repr(exc)})
            print(f"[FAILED] {member}: {exc}")

    if not frames:
        raise RuntimeError("No valid GQBF pixels found for the selected bbox.")

    gqbf = gpd.GeoDataFrame(pandas.concat(frames, ignore_index=True), geometry="geometry", crs=frames[0].crs)
    if gqbf.crs is None:
        raise ValueError("GQBF raster CRS could not be determined.")
    gqbf = gqbf.to_crs(4326)
    gqbf = gqbf[np.isfinite(gqbf["qbf"]) & (gqbf["qbf"] > args.min_qbf) & np.isfinite(gqbf["drainage_area"]) & (gqbf["drainage_area"] > args.min_drainage_area)].copy()

    if args.output_gpkg.exists():
        args.output_gpkg.unlink()
    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gqbf.to_file(args.output_gpkg, layer="gqbf_jawa", driver="GPKG")

    report = {
        "source_zip": str(args.input_zip),
        "tif_members_in_zip": len(tif_members),
        "selected_tile_count": len(selected),
        "failed_tile_count": len(failed),
        "output_points": int(len(gqbf)),
        "keep_every_n": args.keep_every_n,
        "output_crs": str(gqbf.crs),
        "bbox": bbox,
        "output_gpkg": str(args.output_gpkg),
    }
    print(f"CREATED {args.output_gpkg}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())