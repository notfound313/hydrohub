from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="QC PET source and HydroMT output")
    result.add_argument("--input-file", type=output_path, required=True)
    result.add_argument("--report-file", type=output_path, required=True)
    result.add_argument("--min-mean-coverage", type=float, default=0.98)
    result.add_argument("--min-step-coverage", type=float, default=0.90)
    return result


def _check(path):
    import numpy as np
    import pandas as pd
    import xarray as xr

    ds = xr.open_dataset(path, chunks={"time": 168})
    pet = ds["pet"]
    t = pd.DatetimeIndex(pd.to_datetime(pet.time.values))
    dh = np.diff(t.asi8) / 3.6e12 if len(t) > 1 else np.array([])
    sp = [d for d in pet.dims if d != "time"]
    cov = np.isfinite(pet).mean(dim=sp)
    return {
        "count": len(t),
        "start": str(t[0]),
        "end": str(t[-1]),
        "duplicates": int(t.duplicated().sum()),
        "monotonic": bool(t.is_monotonic_increasing),
        "median_timestep_hours": float(np.median(dh)) if len(dh) else None,
        "irregular_intervals": int(np.sum(np.abs(dh - np.median(dh)) > 1e-6)) if len(dh) else None,
        "min_mm": float(pet.min(skipna=True).compute()),
        "max_mm": float(pet.max(skipna=True).compute()),
        "mean_mm": float(pet.mean(skipna=True).compute()),
        "negative_count": int((pet < 0).sum().compute()),
        "coverage_mean": float(cov.mean().compute()),
        "coverage_min_timestep": float(cov.min().compute()),
    }


def main() -> int:
    args = parser().parse_args()
    if not args.input_file.exists():
        raise FileNotFoundError(args.input_file)

    result = {"errors": [], "warnings": [], "source": _check(args.input_file)}
    s = result["source"]

    if s["duplicates"] > 0:
        result["errors"].append("Duplicate timestamps")
    if not s["monotonic"]:
        result["errors"].append("Time not monotonic")
    if s["irregular_intervals"] not in (0, None):
        result["errors"].append("Irregular/missing timesteps")
    if s["negative_count"] > 0:
        result["errors"].append("Negative PET exists")
    if s["coverage_mean"] < args.min_mean_coverage:
        result["errors"].append(f"Mean coverage <{args.min_mean_coverage*100:.0f}%")
    if s["coverage_min_timestep"] < args.min_step_coverage:
        result["errors"].append(f"One timestep coverage <{args.min_step_coverage*100:.0f}%")

    dt = s["median_timestep_hours"]
    limit = 2.0 if dt and dt <= 1.5 else 15.0 if dt and dt >= 20 else 2.0 * (dt or 1)
    if s["max_mm"] > limit:
        result["warnings"].append(f"PET max {s['max_mm']:.3f} mm exceeds review threshold {limit:.3f}")

    result["pass"] = len(result["errors"]) == 0
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print("QC PASS =", result["pass"])
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
