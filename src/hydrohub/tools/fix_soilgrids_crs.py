from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import add_bbox, output_path, require, validate_bbox


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Assign the SoilGrids Homolosine CRS (ESRI:54052) to GeoTIFFs that lack it, in place")
    result.add_argument("--input-dir", type=output_path, required=True)
    add_bbox(result)
    result.add_argument("--fix-in-place", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--stop-on-different-crs", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--report-file", type=output_path, default=None)
    return result


def _bbox_intersects(a, b):
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    rasterio = require("rasterio")
    CRS = require("pyproj.crs").CRS
    Transformer = require("pyproj").Transformer

    soilgrids_proj4 = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs"
    try:
        soilgrids_crs = CRS.from_string("ESRI:54052")
    except Exception:
        soilgrids_crs = CRS.from_string(soilgrids_proj4)

    if not args.input_dir.exists():
        raise FileNotFoundError(args.input_dir)

    files = sorted(args.input_dir.glob("*.tif"))
    if not files:
        raise RuntimeError(f"No .tif files found in {args.input_dir}")

    transformer = Transformer.from_crs("EPSG:4326", soilgrids_crs, always_xy=True)
    bbox_sg = transformer.transform_bounds(bbox[0], bbox[1], bbox[2], bbox[3], densify_pts=41)

    report = {
        "input_dir": str(args.input_dir),
        "soilgrids_crs": str(soilgrids_crs),
        "soilgrids_proj4": soilgrids_proj4,
        "bbox_wgs84": bbox,
        "bbox_soilgrids": list(bbox_sg),
        "files": {},
        "all_overlap_java_bbox": True,
        "all_have_crs_after_fix": True,
    }

    for path in files:
        with rasterio.open(path) as src:
            old_crs = src.crs
            bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
            width, height, res, nodata = src.width, src.height, src.res, src.nodata
            transform_before = tuple(src.transform)

        if old_crs is None:
            if not args.fix_in_place:
                status = "missing_crs_not_fixed"
            else:
                with rasterio.open(path, "r+") as dst:
                    dst.crs = soilgrids_crs
                status = "crs_assigned"
        else:
            same = False
            try:
                same = CRS.from_user_input(old_crs) == soilgrids_crs
            except Exception:
                pass
            if same:
                status = "already_correct"
            else:
                if args.stop_on_different_crs:
                    raise RuntimeError(f"{path.name} already has a different CRS: {old_crs}. Refusing to overwrite.")
                with rasterio.open(path, "r+") as dst:
                    dst.crs = soilgrids_crs
                status = "different_crs_overwritten"

        with rasterio.open(path) as src:
            new_crs = src.crs
            if tuple(src.transform) != transform_before:
                raise RuntimeError(f"Unexpected transform change in {path.name}")
            new_bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)

        overlap = _bbox_intersects(bbox_sg, new_bounds)
        has_crs = new_crs is not None
        report["all_overlap_java_bbox"] &= overlap
        report["all_have_crs_after_fix"] &= has_crs
        report["files"][path.name] = {
            "status": status,
            "old_crs": str(old_crs),
            "new_crs": str(new_crs),
            "width": width,
            "height": height,
            "resolution": list(res),
            "nodata": nodata,
            "bounds": list(new_bounds),
            "overlaps_java_bbox": bool(overlap),
        }

    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not (report["all_have_crs_after_fix"] and report["all_overlap_java_bbox"]):
        raise RuntimeError("CRS fix completed but QC did not pass.")
    print(f"FIXED {len(files)} file(s) in {args.input_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())