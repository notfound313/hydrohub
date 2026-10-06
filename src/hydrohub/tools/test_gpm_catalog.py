from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Smoke-test a HydroMT GPM data catalog (gpm_imerg_jawa)")
    result.add_argument("--catalog", type=output_path, required=True)
    result.add_argument("--source-name", default="gpm_imerg_jawa")
    result.add_argument("--variables", default="precip")
    result.add_argument("--report-file", type=output_path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    hydromt = require("hydromt")
    if not args.catalog.exists():
        raise FileNotFoundError(f"Catalog not found: {args.catalog}")

    dc = hydromt.DataCatalog(data_libs=[str(args.catalog)])
    da = dc.get_rasterdataset(
        args.source_name,
        variables=[v.strip() for v in args.variables.split(",") if v.strip()],
    )
    report = {
        "catalog": str(args.catalog),
        "source": args.source_name,
        "crs": str(da.raster.crs),
        "bounds": list(map(float, da.raster.bounds)),
        "resolution": list(map(float, da.raster.res)),
        "time_start": str(da.time.values[0]) if "time" in da.coords else None,
        "time_end": str(da.time.values[-1]) if "time" in da.coords else None,
        "units": da.attrs.get("units"),
        "min": float(da.min(skipna=True).compute()),
        "max": float(da.max(skipna=True).compute()),
        "nan_fraction": float(da.isnull().mean().compute()),
    }
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"CREATED {args.report_file}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())