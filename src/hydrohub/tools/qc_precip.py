from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Quality-control daily precipitation")
    result.add_argument("--input-file", type=output_path, required=True)
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--variable", default="precip")
    result.add_argument("--min-mean-coverage", type=float, default=0.98)
    result.add_argument("--min-step-coverage", type=float, default=0.9)
    result.add_argument("--extreme-threshold", type=float, default=500.0)
    return result


def main() -> int:
    args = parser().parse_args()
    if not args.input_file.exists():
        raise FileNotFoundError(args.input_file)
    np = require("numpy")
    xr = require("xarray")
    with xr.open_dataset(args.input_file) as dataset:
        if args.variable not in dataset:
            raise ValueError(f"Variable '{args.variable}' not found in {args.input_file}")
        data = dataset[args.variable]
        if "time" not in data.dims:
            raise ValueError(f"Variable '{args.variable}' has no time dimension.")
        values = data.values
        coverage = np.isfinite(values).reshape(data.sizes["time"], -1).mean(axis=1)
        times = dataset.time.values.astype("datetime64[D]")
        duplicate_times = int(len(times) - len(np.unique(times)))
        negative_count = int(np.sum(values < 0))
        finite = values[np.isfinite(values)]
        maximum = float(finite.max()) if finite.size else None
        checks = {
            "unique_timestamps": duplicate_times == 0,
            "no_negative_values": negative_count == 0,
            "mean_coverage": float(coverage.mean()) >= args.min_mean_coverage,
            "minimum_step_coverage": float(coverage.min()) >= args.min_step_coverage,
            "recognized_units": str(data.attrs.get("units", "")).lower() in {"mm", "mm/day", "mm d-1"},
        }
        report = {
            "file": str(args.input_file),
            "variable": args.variable,
            "time_steps": data.sizes["time"],
            "duplicate_timestamps": duplicate_times,
            "negative_count": negative_count,
            "mean_spatial_coverage": float(coverage.mean()),
            "minimum_spatial_coverage": float(coverage.min()),
            "maximum": maximum,
            "extreme_value_review": maximum is not None and maximum > args.extreme_threshold,
            "checks": checks,
            "passed": all(checks.values()),
        }
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
