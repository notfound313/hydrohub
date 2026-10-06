from __future__ import annotations

import argparse
import calendar
import time

from hydrohub.tools.common import add_bbox, iso_date, output_path, require, validate_bbox


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Download monthly ERA5-Land PET from CDS")
    result.add_argument("--start-date", type=iso_date, required=True)
    result.add_argument("--end-date", type=iso_date, required=True)
    add_bbox(result)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--dataset", default="reanalysis-era5-land")
    result.add_argument("--variable", default="potential_evaporation")
    result.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def month_range(start, end):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def main() -> int:
    args = parser().parse_args()
    north_west_south_east = [args.north, args.west, args.south, args.east]
    validate_bbox(args)
    if args.start_date > args.end_date:
        raise ValueError("Start date must not be after end date.")
    print(f"ERA5-Land dataset={args.dataset} variable={args.variable}")
    print(f"Dates: {args.start_date} through {args.end_date}")
    print(f"CDS area [N,W,S,E]: {north_west_south_east}")
    print(f"Output: {args.output_dir}")
    if args.dry_run:
        print("Configuration valid. Dry run complete.")
        return 0

    cdsapi = require("cdsapi")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = cdsapi.Client()
    for year, month in month_range(args.start_date, args.end_date):
        target = args.output_dir / f"era5land_pev_{year}{month:02d}.nc"
        if args.resume and target.exists() and target.stat().st_size > 10_000:
            print(f"SKIP {target.name}")
            continue
        first_day = 1 if (year, month) != (args.start_date.year, args.start_date.month) else args.start_date.day
        month_last = calendar.monthrange(year, month)[1]
        last_day = month_last if (year, month) != (args.end_date.year, args.end_date.month) else args.end_date.day
        request = {
            "variable": [args.variable],
            "year": str(year),
            "month": f"{month:02d}",
            "day": [f"{day:02d}" for day in range(first_day, last_day + 1)],
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "area": north_west_south_east,
            "data_format": "netcdf",
            "download_format": "unarchived",
        }
        print(f"DOWNLOAD {year}-{month:02d} -> {target.name}")
        client.retrieve(args.dataset, request).download(str(target))
        time.sleep(1)
    print("ERA5-Land download complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
