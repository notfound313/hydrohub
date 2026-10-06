from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


# Mapping: source WCS property name -> (Wflow base name, unit multiplier)
SOIL_PROPERTY_MAP = {
    "bdod": ("bd", 0.01),
    "clay": ("clyppt", 0.1),
    "silt": ("sltppt", 0.1),
    "sand": ("sndppt", 0.1),
    "soc": ("oc", 0.01),
    "phh2o": ("ph", 0.1),
}
DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Convert raw SoilGrids TIFFs to HydroMT-Wflow units")
    result.add_argument("--input-dir", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    rasterio = require("rasterio")
    soil_crs = rasterio.crs.CRS.from_string("ESRI:54052")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    ref_signature = None
    ref_profile = None

    for wcs_name, (base, factor) in SOIL_PROPERTY_MAP.items():
        for i, depth in enumerate(DEPTHS, 1):
            src_path = args.input_dir / f"{wcs_name}_{depth}_mean.tif"
            if not src_path.exists():
                raise FileNotFoundError(f"Missing source TIFF: {src_path}")

            with rasterio.open(src_path) as src:
                if src.crs is not None and src.crs != soil_crs:
                    raise ValueError(f"Unexpected SoilGrids CRS in {src_path}: {src.crs}")
                arr = src.read(1).astype("float32")
                valid = np.isfinite(arr)
                if src.nodata is not None:
                    valid &= arr != src.nodata
                signature = (src.width, src.height, src.crs, src.transform)
                if ref_signature is None:
                    ref_signature = signature
                    ref_profile = src.profile.copy()
                    ref_profile["crs"] = soil_crs
                elif signature[:3] != ref_signature[:3] or not src.transform.almost_equals(ref_signature[3]):
                    raise ValueError(f"Grid mismatch: {src_path}")

            out_arr = np.full(arr.shape, -9999.0, dtype="float32")
            out_arr[valid] = arr[valid] * factor
            profile = ref_profile.copy()
            profile.update(dtype="float32", count=1, nodata=-9999.0, compress="LZW", tiled=True)
            with rasterio.open(args.output_dir / f"{base}_sl{i}.tif", "w", **profile) as dst:
                dst.write(out_arr, 1)
            print(f"OK {base}_sl{i}")

    # Create synthetic soilthickness
    thickness = np.full(arr.shape, -9999.0, dtype="float32")
    # Use bd_sl1 valid mask as reference for soil presence
    bd_path = args.input_dir / "bdod_0-5cm_mean.tif"
    with rasterio.open(bd_path) as src:
        bd = src.read(1).astype("float32")
        bd_valid = np.isfinite(bd)
        if src.nodata is not None:
            bd_valid &= bd != src.nodata
    thickness[bd_valid] = 200.0
    profile = ref_profile.copy()
    profile.update(dtype="float32", count=1, nodata=-9999.0, compress="LZW", tiled=True)
    with rasterio.open(args.output_dir / "soilthickness.tif", "w", **profile) as dst:
        dst.write(thickness, 1)
    print(f"OK soilthickness")

    report = {
        "properties": list(SOIL_PROPERTY_MAP.keys()),
        "depth_layers": len(DEPTHS),
        "total_files": len(SOIL_PROPERTY_MAP) * len(DEPTHS) + 1,
        "output": str(args.output_dir),
    }
    print(f"PREPARED {args.output_dir}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
