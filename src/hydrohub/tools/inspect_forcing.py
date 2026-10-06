from __future__ import annotations

import argparse
import glob
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Inspect the structure, variables, coordinates and units of a NetCDF forcing/inspection file")
    result.add_argument("--input-glob", required=True, help="Glob matching NetCDF file(s); first file is inspected")
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--print-variable-values", action="store_true", help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    pandas = require("pandas")
    xr = require("xarray")

    files = sorted(glob.glob(args.input_glob))
    if not files:
        raise FileNotFoundError(args.input_glob)

    ds = xr.open_dataset(files[0], decode_times=True, mask_and_scale=True)

    variables = {}
    for name, da in ds.data_vars.items():
        entry = {
            "dims": list(da.dims),
            "shape": list(da.shape),
            "dtype": str(da.dtype),
            "units": da.attrs.get("units"),
            "long_name": da.attrs.get("long_name"),
        }
        if np.issubdtype(da.dtype, np.number) and da.size:
            try:
                entry["min"] = float(da.min().item())
                entry["max"] = float(da.max().item())
            except Exception:
                pass
        variables[name] = entry

    coordinates = {}
    for name, c in ds.coords.items():
        entry = {"dims": list(c.dims), "shape": list(c.shape), "dtype": str(c.dtype)}
        if np.issubdtype(c.dtype, np.number) and c.size:
            try:
                entry["min"] = float(c.min().item())
                entry["max"] = float(c.max().item())
            except Exception:
                pass
        if name.lower() == "time" and c.size:
            entry["first"] = str(pandas.to_datetime(c.values.flat[0]))
            entry["last"] = str(pandas.to_datetime(c.values.flat[-1]))
        coordinates[name] = entry

    report = {
        "inspect_file": files[0],
        "input_file_count": len(files),
        "dims": dict(ds.sizes),
        "variables": variables,
        "coordinates": coordinates,
    }

    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"CREATED {args.report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())