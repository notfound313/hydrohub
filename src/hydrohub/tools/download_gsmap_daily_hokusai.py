from __future__ import annotations

import argparse
from datetime import timedelta
import ftplib
import gzip
import json
import socket
from pathlib import Path
import tempfile
import time

from hydrohub.tools.common import add_bbox, iso_date, output_path, require, validate_bbox


NX, NY = 3600, 1200
LON0, LAT0, DX, DY = 0.05, 59.95, 0.1, -0.1
SOURCE_MISSING_VALUE = -999.9
MIN_SPATIAL_COVERAGE = 0.98
EXTREME_DAILY_REVIEW_MM = 500.0
FTP_TIMEOUT_SECONDS = 120
DEFAULT_HOST = "hokusai.eorc.jaxa.jp"
DEFAULT_PORT = 21


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Download JAXA GSMaP Gauge v8 Daily via the legacy hokusai FTP host",
        epilog="Credentials are read from GSMAP_USER and GSMAP_PASSWORD environment variables.",
    )
    result.add_argument("--start-date", type=iso_date, required=True)
    result.add_argument("--end-date", type=iso_date, required=True)
    add_bbox(result)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--host", default=DEFAULT_HOST)
    result.add_argument("--port", type=int, default=DEFAULT_PORT)
    result.add_argument("--remote-base", default="/standard/v8/daily_G/00Z-23Z")
    result.add_argument("--max-retries", type=int, default=5)
    result.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return result


def _connect(host=DEFAULT_HOST, port=DEFAULT_PORT) -> ftplib.FTP:
    import os

    user = os.getenv("GSMAP_USER")
    password = os.getenv("GSMAP_PASSWORD")
    host = os.getenv("GSMAP_HOST", host)
    port = int(os.getenv("GSMAP_PORT", str(port)))
    if not user or not password:
        raise RuntimeError("GSMaP FTP credentials are required in GSMAP_USER and GSMAP_PASSWORD.")
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=FTP_TIMEOUT_SECONDS)
    ftp.login(user, password)
    ftp.set_pasv(True)
    return ftp


def _list_files(ftp: ftplib.FTP, remote_dir: str) -> list[str]:
    try:
        names = ftp.nlst(remote_dir)
    except ftplib.error_perm as exc:
        if str(exc).startswith("550"):
            return []
        raise
    return sorted({item.rsplit("/", 1)[-1] for item in names})


def _find_daily_file(names: list[str], ymd: str) -> str | None:
    matches = [
        name for name in names
        if ymd in name and "gsmap_gauge" in name.lower() and "daily" in name.lower()
        and "00z-23z" in name.lower()
        and name.lower().endswith((".dat", ".dat.gz", ".gz"))
    ]
    return sorted(matches, key=lambda name: (not name.lower().endswith(".gz"), name))[0] if matches else None


def _download(ftp: ftplib.FTP, remote_path: str, local: Path, retries: int, host=DEFAULT_HOST, port=DEFAULT_PORT) -> None:
    for attempt in range(1, retries + 1):
        try:
            with open(local, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write, blocksize=1024 * 1024)
            if not local.exists() or local.stat().st_size < 1024:
                raise RuntimeError("Downloaded file is missing or too small.")
            return
        except (EOFError, TimeoutError, socket.timeout, ConnectionResetError, BrokenPipeError,
                OSError, ftplib.error_perm) as exc:
            local.unlink(missing_ok=True)
            try:
                ftp.quit()
            except Exception:
                pass
            if attempt == retries:
                raise
            time.sleep(10 * attempt)
            ftp = _connect(host, port)


def _decode_global(path: Path):
    np = require("numpy")
    raw = gzip.open(path, "rb").read() if path.name.lower().endswith(".gz") else path.read_bytes()
    expected = NX * NY * 4
    if len(raw) != expected:
        raise ValueError(f"Unexpected GSMaP binary size {len(raw):,}; expected {expected:,}")
    arr = np.frombuffer(raw, dtype="<f4").reshape(NY, NX).astype(np.float32, copy=True)
    arr[(~np.isfinite(arr)) | np.isclose(arr, SOURCE_MISSING_VALUE, atol=0.01) | (arr < 0)] = np.nan
    return arr


def _crop(arr, west, south, east, north):
    np = require("numpy")
    lon = LON0 + np.arange(NX) * DX
    lat = LAT0 + np.arange(NY) * DY
    lon_mask = (lon >= west) & (lon <= east)
    lat_mask = (lat >= south) & (lat <= north)
    if not lon_mask.any() or not lat_mask.any():
        raise ValueError("BBOX does not overlap GSMaP grid.")
    return arr[np.ix_(lat_mask, lon_mask)], lat[lat_mask], lon[lon_mask]


def _write_daily(d, daily_rate, lat, lon, output, source_file) -> dict:
    np = require("numpy")
    xr = require("xarray")
    precip = daily_rate * np.float32(24.0)
    finite = np.isfinite(precip)
    finite_fraction = float(finite.mean())
    negative_count = int(np.sum(precip < 0))
    extreme_count = int(np.sum(precip > EXTREME_DAILY_REVIEW_MM))
    qc = {
        "finite_fraction": finite_fraction,
        "negative_count": negative_count,
        "min_mm_day": float(np.nanmin(precip)) if finite.any() else None,
        "max_mm_day": float(np.nanmax(precip)) if finite.any() else None,
        "mean_mm_day": float(np.nanmean(precip)) if finite.any() else None,
        "extreme_cells_gt_500mm_day": extreme_count,
        "qc_pass": negative_count == 0 and finite_fraction >= MIN_SPATIAL_COVERAGE,
    }
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), precip[None, :, :])},
        coords={"time": [np.datetime64(d)], "latitude": lat, "longitude": lon},
        attrs={"source": "JAXA GSMaP Gauge v8 Daily 00Z-23Z", "source_units": "mm/hr",
               "conversion": "daily-average rate x 24 h", "source_file": source_file},
    )
    ds["precip"].attrs.update(units="mm", long_name="daily precipitation accumulation",
                              standard_name="precipitation_amount")
    output.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(output, engine="netcdf4",
                 encoding={"precip": {"dtype": "float32", "zlib": True, "complevel": 4, "_FillValue": np.float32(-9999.0)}})
    return qc


def main() -> int:
    args = parser().parse_args()
    bbox = validate_bbox(args)
    if args.start_date > args.end_date:
        raise ValueError("Start date must not be after end date.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "ftp_host": args.host, "ftp_port": args.port, "remote_base": args.remote_base,
        "product": "GSMaP Gauge v8 Daily 00Z-23Z", "source_units": "mm/hr", "output_units": "mm",
        "bbox": list(bbox), "start_date": str(args.start_date), "end_date": str(args.end_date), "days": {},
    }
    print(f"GSMaP Hokusai dates={args.start_date}..{args.end_date} BBOX={bbox}")

    np = require("numpy")
    xr = require("xarray")
    if args.dry_run:
        _connect(args.host, args.port)
        print("Configuration and credentials valid. Dry run complete.")
        return 0

    ftp = _connect(args.host, args.port)
    current_month = None
    month_names: list[str] = []
    total_days = (args.end_date - args.start_date).days + 1
    try:
        with tempfile.TemporaryDirectory(prefix="gsmap_daily_hokusai_") as tmp:
            temp_root = Path(tmp)
            day = args.start_date
            for n in range(1, total_days + 1):
                output = args.output_dir / f"gsmap_daily_{day:%Y%m%d}_jawa.nc"
                if args.resume and output.exists() and output.stat().st_size > 10_000:
                    print(f"[{n}/{total_days}] SKIP {day}")
                    day += timedelta(days=1)
                    continue
                remote_dir = f"{args.remote_base}/{day:%Y%m}"
                if day.strftime("%Y%m") != current_month:
                    current_month = day.strftime("%Y%m")
                    month_names = _list_files(ftp, remote_dir)
                    print(f"[{n}/{total_days}] Listing {remote_dir}: {len(month_names)} files")
                filename = _find_daily_file(month_names, day.strftime("%Y%m%d"))
                if not filename:
                    print(f"[{n}/{total_days}] {day}: NO FILE")
                    manifest["days"][day.isoformat()] = {"status": "not_found", "remote_dir": remote_dir}
                    day += timedelta(days=1)
                    continue
                local_tmp = temp_root / filename
                _download(ftp, f"{remote_dir}/{filename}", local_tmp, args.max_retries, args.host, args.port)
                arr_java, lat, lon = _crop(_decode_global(local_tmp), *bbox)
                qc = _write_daily(day, arr_java, lat, lon, output, filename)
                print(f"[{n}/{total_days}] OK {day}: max={qc['max_mm_day']} mm/day | coverage={qc['finite_fraction']*100:.2f}%")
                manifest["days"][day.isoformat()] = {"status": "written", "source_file": filename,
                                                     "output": str(output), **qc}
                local_tmp.unlink(missing_ok=True)
                day += timedelta(days=1)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    statuses = [x.get("status") for x in manifest["days"].values()]
    manifest["summary"] = {
        "total": len(statuses), "written": statuses.count("written"),
        "skipped_valid": statuses.count("skipped_valid"), "not_found": statuses.count("not_found"),
        "failed": statuses.count("failed"),
    }
    manifest_path = args.output_dir / "gsmap_daily_hokusai_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("CREATED", manifest_path)
    print(json.dumps(manifest["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())