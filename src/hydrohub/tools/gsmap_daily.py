from __future__ import annotations

import argparse
from datetime import timedelta
import gzip
import os
from pathlib import Path
import tempfile
import time

from hydrohub.tools.common import add_bbox, iso_date, output_path, require, validate_bbox


NX, NY = 3600, 1200
LON0, LAT0, DX, DY = 0.05, 59.95, 0.1, -0.1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Download JAXA GSMaP daily gauge data")
    result.add_argument("--start-date", type=iso_date, required=True)
    result.add_argument("--end-date", type=iso_date, required=True)
    add_bbox(result)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--host", default="ftp.gportal.jaxa.jp")
    result.add_argument("--port", type=int, default=2051)
    result.add_argument("--remote-base", default="/standard/v8/daily_G/00Z-23Z")
    result.add_argument("--max-retries", type=int, default=5)
    result.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def find_daily_file(names: list[str], ymd: str) -> str | None:
    matches = [
        name for name in names
        if ymd in name and "gsmap_gauge" in name.lower() and "daily" in name.lower()
        and "00z-23z" in name.lower() and name.lower().endswith((".dat", ".dat.gz", ".gz"))
    ]
    return sorted(matches, key=lambda name: (not name.lower().endswith(".gz"), name))[0] if matches else None


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    if args.start_date > args.end_date:
        raise ValueError("Start date must not be after end date.")
    username, password = os.getenv("GPORTAL_USER"), os.getenv("GPORTAL_PASSWORD")
    print(f"GSMaP dates={args.start_date}..{args.end_date} BBOX={bbox}")
    print(f"SFTP: {args.host}:{args.port}; output: {args.output_dir}")
    if not username or not password:
        raise RuntimeError("G-Portal credentials are required in GPORTAL_USER and GPORTAL_PASSWORD.")
    if args.dry_run:
        print("Configuration and credentials valid. Dry run complete.")
        return 0

    np = require("numpy")
    xr = require("xarray")
    paramiko = require("paramiko")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    transport = paramiko.Transport((args.host, args.port))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        day = args.start_date
        listed: dict[str, list[str]] = {}
        while day <= args.end_date:
            output = args.output_dir / f"gsmap_daily_{day:%Y%m%d}.nc"
            if args.resume and output.exists() and output.stat().st_size > 10_000:
                print(f"SKIP {output.name}")
                day += timedelta(days=1)
                continue
            remote_dir = f"{args.remote_base}/{day:%Y%m}"
            if remote_dir not in listed:
                listed[remote_dir] = sftp.listdir(remote_dir)
            filename = find_daily_file(listed[remote_dir], day.strftime("%Y%m%d"))
            if not filename:
                raise FileNotFoundError(f"No GSMaP daily file found for {day} in {remote_dir}")
            with tempfile.TemporaryDirectory() as temp_dir:
                local = Path(temp_dir) / filename
                for attempt in range(1, args.max_retries + 1):
                    try:
                        sftp.get(f"{remote_dir}/{filename}", str(local))
                        break
                    except Exception:
                        if attempt == args.max_retries:
                            raise
                        time.sleep(10 * attempt)
                raw = gzip.open(local, "rb").read() if filename.lower().endswith(".gz") else local.read_bytes()
                expected = NX * NY * 4
                if len(raw) != expected:
                    raise ValueError(f"Unexpected GSMaP binary size {len(raw):,}; expected {expected:,}")
                rain = np.frombuffer(raw, dtype="<f4").reshape(NY, NX).astype(np.float32, copy=True)
                rain[(~np.isfinite(rain)) | np.isclose(rain, -999.9, atol=0.01) | (rain < 0)] = np.nan
                lon = LON0 + np.arange(NX) * DX
                lat = LAT0 + np.arange(NY) * DY
                lon_mask = (lon >= args.west) & (lon <= args.east)
                lat_mask = (lat >= args.south) & (lat <= args.north)
                if not lon_mask.any() or not lat_mask.any():
                    raise ValueError(f"BBOX {bbox} does not overlap the GSMaP grid.")
                precip = rain[np.ix_(lat_mask, lon_mask)] * np.float32(24.0)
                dataset = xr.Dataset(
                    {"precip": (("time", "latitude", "longitude"), precip[None, :, :])},
                    coords={"time": [np.datetime64(day)], "latitude": lat[lat_mask], "longitude": lon[lon_mask]},
                    attrs={"source": "JAXA GSMaP Gauge v8 Daily", "source_file": filename},
                )
                dataset["precip"].attrs.update(units="mm", long_name="daily precipitation accumulation")
                dataset.to_netcdf(output, encoding={"precip": {"zlib": True, "complevel": 4, "_FillValue": -9999.0}})
                print(f"OK {day}: {output.name}; finite={float(np.isfinite(precip).mean()):.3f}")
            day += timedelta(days=1)
    finally:
        sftp.close()
        transport.close()
    print("GSMaP download complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
