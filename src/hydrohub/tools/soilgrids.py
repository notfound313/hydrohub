from __future__ import annotations

import argparse
import json
import math
import time

from hydrohub.tools.common import add_bbox, output_path, require, validate_bbox


PROPERTY_ALIASES = {
    "bd": "bdod",
    "clyppt": "clay",
    "oc": "soc",
    "ph": "phh2o",
    "sltppt": "silt",
    "sndppt": "sand",
}
PROPERTIES = ("bdod", "clay", "silt", "sand", "soc", "phh2o")
DEPTHS = ("0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm")
SOILGRIDS_PROJ4 = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs +towgs84=0,0,0"
SOILGRIDS_CRS_URI = "http://www.opengis.net/def/crs/EPSG/0/152160"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Download raw SoilGrids 2.0 layers on the native 250 m Homolosine grid")
    add_bbox(result)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--properties", default=",".join(PROPERTIES))
    result.add_argument("--bbox-buffer-deg", type=float, default=0.05)
    result.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def _native_bbox(bbox, buffer_deg, CRS, Transformer):
    west, south, east, north = bbox
    buffered = (west - buffer_deg, south - buffer_deg, east + buffer_deg, north + buffer_deg)
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_proj4(SOILGRIDS_PROJ4), always_xy=True)
    xmin, ymin, xmax, ymax = transformer.transform_bounds(*buffered, densify_pts=41)
    return tuple(value * 250.0 for value in (
        math.floor(xmin / 250.0),
        math.floor(ymin / 250.0),
        math.ceil(xmax / 250.0),
        math.ceil(ymax / 250.0),
    ))


def _request_params(coverage: str, bbox_native) -> list[tuple[str, str]]:
    xmin, ymin, xmax, ymax = bbox_native
    return [
        ("SERVICE", "WCS"),
        ("VERSION", "2.0.1"),
        ("REQUEST", "GetCoverage"),
        ("COVERAGEID", coverage),
        ("FORMAT", "GEOTIFF_INT16"),
        ("SUBSET", f"X({xmin:.3f},{xmax:.3f})"),
        ("SUBSET", f"Y({ymin:.3f},{ymax:.3f})"),
        ("SUBSETTINGCRS", SOILGRIDS_CRS_URI),
        ("OUTPUTCRS", SOILGRIDS_CRS_URI),
    ]


def _valid_tif(path, rasterio) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return False
        with rasterio.open(path) as src:
            return src.count == 1 and src.width > 0 and src.height > 0
    except Exception:
        return False


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    selected = [PROPERTY_ALIASES.get(value.strip(), value.strip()) for value in args.properties.split(",") if value.strip()]
    if not selected:
        raise ValueError("At least one SoilGrids property is required.")
    unknown = sorted(set(selected) - set(PROPERTIES))
    if unknown:
        raise ValueError(f"Unknown SoilGrids properties: {', '.join(unknown)}")
    if args.bbox_buffer_deg < 0:
        raise ValueError("BBOX buffer must not be negative.")

    pyproj = require("pyproj")
    bbox_native = _native_bbox(bbox, args.bbox_buffer_deg, pyproj.CRS, pyproj.Transformer)
    print(f"SoilGrids properties={selected} depths={len(DEPTHS)} BBOX={bbox}")
    print(f"Homolosine 250 m BBOX={bbox_native}; output: {args.output_dir}")
    if args.dry_run:
        print("Configuration valid. Dry run complete.")
        return 0

    requests = require("requests")
    rasterio = require("rasterio")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bbox_wgs84_original": list(bbox),
        "bbox_soilgrids_native": list(bbox_native),
        "local_soilgrids_proj4": SOILGRIDS_PROJ4,
        "wcs_pseudo_epsg": 152160,
        "properties": selected,
        "depths": list(DEPTHS),
        "files": {},
    }
    expected = []
    for prop in selected:
        url = f"https://maps.isric.org/mapserv?map=/map/{prop}.map"
        for depth in DEPTHS:
            coverage = f"{prop}_{depth}_mean"
            output = args.output_dir / f"{coverage}.tif"
            expected.append(output)
            if args.resume and _valid_tif(output, rasterio):
                print(f"SKIP VALID {output.name}")
                manifest["files"][output.name] = "skipped_existing_valid"
                continue
            part = output.with_suffix(".tif.part")
            part.unlink(missing_ok=True)
            last_error = None
            for attempt in range(1, 5):
                try:
                    response = requests.get(url, params=_request_params(coverage, bbox_native), timeout=(30, 600))
                    response.raise_for_status()
                    part.write_bytes(response.content)
                    if not _valid_tif(part, rasterio):
                        raise RuntimeError("WCS response is not a valid single-band GeoTIFF")
                    part.replace(output)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    part.unlink(missing_ok=True)
                    if attempt < 4:
                        time.sleep(min(60, 5 * (2 ** (attempt - 1))))
            if last_error is not None:
                raise RuntimeError(f"Failed download {coverage}: {last_error}") from last_error
            manifest["files"][output.name] = "downloaded"
            print(f"OK {coverage} -> {output.name}")
            time.sleep(1.5)

    signatures = []
    for path in expected:
        with rasterio.open(path) as src:
            signatures.append((src.width, src.height, str(src.crs), tuple(src.transform)))
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise RuntimeError("Downloaded SoilGrids layers do not share one grid.")
    manifest["reference_grid"] = {
        "width": signatures[0][0],
        "height": signatures[0][1],
        "crs": signatures[0][2],
        "transform": list(signatures[0][3]),
    }
    (args.output_dir / "soilgrids_download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"SoilGrids download complete: {len(expected)} validated layers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
