from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Delineate subbasins and outlets from a D8 flow network, then build basin index envelopes")
    result.add_argument("--flwdir", type=output_path, required=True, help="D8 flow direction GeoTIFF")
    result.add_argument("--output-gpkg", type=output_path, required=True)
    result.add_argument("--area-min-km2", type=float, default=250.0)
    result.add_argument("--layer-basins", default="basins")
    result.add_argument("--layer-outlets", default="outlets")
    result.add_argument("--layer-index", default="basin_index")
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    rasterio = require("rasterio")
    pyflwdir = require("pyflwdir", "pyflwdir")
    gpd = require("geopandas", "geopandas")
    shapes = require("rasterio.features").shapes
    shape_fn = require("shapely.geometry").shape

    if not args.flwdir.exists():
        raise FileNotFoundError(args.flwdir)

    with rasterio.open(args.flwdir) as src:
        d8 = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    if crs is None:
        raise ValueError("Flow direction raster has no CRS.")

    valid = np.isfinite(d8)
    if nodata is not None:
        valid &= d8 != nodata
    else:
        valid &= d8 != 247
    if not np.any(valid):
        raise ValueError("No valid D8 cells.")

    d8_clean = np.where(valid, d8, 247).astype(np.uint8)
    flw = pyflwdir.from_array(d8_clean, ftype="d8", check_ftype=True, mask=valid, transform=transform, latlon=bool(crs.is_geographic), cache=True)
    subbas, idxs_out = flw.subbasins_area(area_min=args.area_min_km2)

    mask = subbas != 0
    feats = [{"geometry": shape_fn(geom), "basid": int(val)} for geom, val in shapes(subbas.astype(np.int32), mask=mask, transform=transform)]
    if not feats:
        raise ValueError("No subbasins met the configured minimum area.")
    gdf_bas = gpd.GeoDataFrame(feats, crs=crs)
    gdf_bas["Luas_km2"] = gdf_bas.geometry.to_crs(3857).area / 1e6
    gdf_bas = gdf_bas.sort_values("basid")

    x, y = flw.xy(idxs_out)
    gdf_out = gpd.GeoDataFrame({"basid": subbas.flat[idxs_out]}, geometry=gpd.points_from_xy(x, y), crs=crs)

    gdf_index = gpd.GeoDataFrame(gdf_bas[["basid"]].copy(), geometry=gdf_bas.geometry.envelope, crs=crs)

    if args.output_gpkg.exists():
        args.output_gpkg.unlink()
    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gdf_bas.to_file(args.output_gpkg, layer=args.layer_basins, driver="GPKG")
    gdf_out.to_file(args.output_gpkg, layer=args.layer_outlets, driver="GPKG")
    gdf_index.to_file(args.output_gpkg, layer=args.layer_index, driver="GPKG")

    report = {
        "flwdir": str(args.flwdir),
        "area_min_km2": args.area_min_km2,
        "n_basins": int(len(gdf_bas)),
        "n_outlets": int(len(gdf_out)),
        "output_gpkg": str(args.output_gpkg),
        "crs": str(crs),
    }
    print(f"CREATED {args.output_gpkg}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
