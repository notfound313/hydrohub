from __future__ import annotations

import argparse

from hydrohub.tools.common import add_bbox, output_path, require, validate_bbox


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Export HydroRIVERS_LIN into a river_geom GeoPackage via the HydroMT Deltares data catalog"
    )
    add_bbox(result)
    result.add_argument("--output-gpkg", type=output_path, required=True)
    result.add_argument("--data-libs", default="deltares_data")
    result.add_argument("--variables", default="rivwth,qbankfull")
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    print(f"Exporting hydro_rivers_lin BBOX={bbox} variables={args.variables} from {args.data_libs}")

    hydromt = require("hydromt")
    if args.dry_run:
        print("Configuration valid. Dry run complete.")
        return 0

    dc = hydromt.DataCatalog(data_libs=[args.data_libs])
    gdf = dc.get_geodataframe(
        "hydro_rivers_lin",
        bbox=bbox,
        variables=[v.strip() for v in args.variables.split(",") if v.strip()],
    )
    if gdf is None or gdf.empty:
        raise RuntimeError("No hydro_rivers_lin data returned.")
    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if args.output_gpkg.exists():
        args.output_gpkg.unlink()
    gdf.to_file(args.output_gpkg, layer="river_geom", driver="GPKG")
    print(f"CREATED {args.output_gpkg}")
    print(f"Features: {len(gdf)}")
    print(f"Columns: {list(gdf.columns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())