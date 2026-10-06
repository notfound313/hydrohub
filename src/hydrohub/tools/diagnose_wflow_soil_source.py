from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Diagnose which soilgrids_2020 source a Wflow model actually resolves (verifies its data catalog)"
    )
    result.add_argument("--model-root", type=output_path, required=True)
    result.add_argument("--data-libs", default="")
    result.add_argument("--report-file", type=output_path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    from hydromt_wflow import WflowSbmModel

    libs = [lib.strip() for lib in args.data_libs.split(",") if lib.strip()]
    print("MODEL_ROOT:", args.model_root)
    print("DATA_LIBS:", libs or "(model own catalog only)")

    model = WflowSbmModel(root=args.model_root, mode="r+", data_libs=libs or None)
    src = model.data_catalog.get_source("soilgrids_2020")
    report = {
        "model_root": str(args.model_root),
        "data_libs": libs,
        "source": str(src),
        "uri": str(src.uri),
        "region": str(model.region),
    }
    ds = model.data_catalog.get_rasterdataset("soilgrids_2020")
    report["variables"] = list(ds.data_vars)
    report["crs"] = str(ds.raster.crs)
    report["bounds"] = list(map(float, ds.raster.bounds))

    ds_clip = model.data_catalog.get_rasterdataset("soilgrids_2020", geom=model.region, buffer=2)
    report["clip_variables"] = list(ds_clip.data_vars)
    report["clip_crs"] = str(ds_clip.raster.crs)
    report["clip_bounds"] = list(map(float, ds_clip.raster.bounds))
    report["pass"] = bool(ds_clip.data_vars)

    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"CREATED {args.report_file}")
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())