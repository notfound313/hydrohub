from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path


def add_bbox(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--west", type=float, required=True)
    parser.add_argument("--south", type=float, required=True)
    parser.add_argument("--east", type=float, required=True)
    parser.add_argument("--north", type=float, required=True)


def validate_bbox(args: argparse.Namespace) -> tuple[float, float, float, float]:
    bbox = (args.west, args.south, args.east, args.north)
    if not (-180 <= args.west < args.east <= 180 and -90 <= args.south < args.north <= 90):
        raise ValueError(f"Invalid BBOX (west, south, east, north): {bbox}")
    return bbox


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def output_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def require(module: str, package: str | None = None):
    try:
        return __import__(module)
    except ImportError as exc:
        install = package or module
        raise RuntimeError(
            f"Missing dependency '{module}'. Install it in the selected interpreter with: "
            f"python -m pip install {install}"
        ) from exc
