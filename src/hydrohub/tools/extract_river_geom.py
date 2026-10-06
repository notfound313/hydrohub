from __future__ import annotations

import argparse
import json
import warnings

from hydrohub.tools.common import output_path, require

D8_POW2 = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}
D8_SEQ = {1: (0, 1), 2: (1, 1), 3: (1, 0), 4: (1, -1), 5: (0, -1), 6: (-1, -1), 7: (-1, 0), 8: (-1, 1)}
SNAP_TOLERANCE_DEG = 100 / 111000


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Extract connected river network from MERIT D8/uparea/elevation rasters to GeoPackage")
    result.add_argument("--dir-tif", type=output_path, required=True)
    result.add_argument("--uparea-tif", type=output_path, required=True)
    result.add_argument("--elevtn-tif", type=output_path, required=True)
    result.add_argument("--strord-tif", type=output_path, default=None)
    result.add_argument("--rivwth-tif", type=output_path, default=None)
    result.add_argument("--output-gpkg", type=output_path, required=True)
    result.add_argument("--output-layer", default="rivers")
    result.add_argument("--min-uparea-km2", type=float, default=10.0)
    result.add_argument("--min-strahler", type=int, default=3)
    result.add_argument("--use-strahler", action=argparse.BooleanOptionalAction, default=False)
    result.add_argument("--min-segment-cells", type=int, default=5)
    return result


def _detect_d8(values, nodata):
    np = require("numpy")
    valid = values[values != nodata] if nodata is not None else values[values > 0]
    unique = set(np.unique(valid))
    pow2 = {1, 2, 4, 8, 16, 32, 64, 128}
    seq = {1, 2, 3, 4, 5, 6, 7, 8}
    if unique.issubset(pow2):
        return "pow2", D8_POW2
    if unique.issubset(seq):
        return "seq", D8_SEQ
    return "seq" if len(unique & seq) > len(unique & pow2) else "pow2", (D8_SEQ if len(unique & seq) > len(unique & pow2) else D8_POW2)


def _load_flat(path, dtype, nodata_to, rioxarray, np):
    da = rioxarray.open_rasterio(path).squeeze()
    arr = da.values.flatten().copy()
    nodata = da.rio.nodata
    if nodata is not None:
        arr[arr == nodata] = nodata_to
    arr[np.isnan(arr)] = nodata_to
    return arr.astype(dtype), da


def main() -> int:
    args = parser().parse_args()
    warnings.filterwarnings("ignore")
    np = require("numpy")
    gpd = require("geopandas", "geopandas")
    rioxarray = require("rioxarray", "rioxarray")
    LineString = require("shapely.geometry").LineString

    for key in ("dir_tif", "uparea_tif", "elevtn_tif"):
        if not getattr(args, key).exists():
            raise FileNotFoundError(getattr(args, key))

    flw_flat, dir_da = _load_flat(args.dir_tif, np.int32, -1, rioxarray, np)
    nodata_meta = dir_da.rio.nodata
    enc, offsets = _detect_d8(flw_flat[flw_flat >= 0], nodata_meta)
    valid_d8 = set(offsets.keys())
    flw_flat[(flw_flat < 0) | ~np.isin(flw_flat, list(valid_d8))] = -1

    uparea, _ = _load_flat(args.uparea_tif, np.float64, 0.0, rioxarray, np)
    uparea[uparea < 0] = 0
    if np.sum(uparea > 0) == 0:
        uparea, _ = _load_flat(args.uparea_tif, np.float64, 0.0, rioxarray, np)
        uparea[uparea < 0] = 0

    elevtn, _ = _load_flat(args.elevtn_tif, np.float32, -9999.0, rioxarray, np)
    elevtn[elevtn < -9000] = -9999.0

    strord = None
    if args.strord_tif is not None and args.strord_tif.exists():
        strord, _ = _load_flat(args.strord_tif, np.float32, 0.0, rioxarray, np)

    rivwth = None
    if args.rivwth_tif is not None and args.rivwth_tif.exists():
        rivwth, _ = _load_flat(args.rivwth_tif, np.float32, 0.0, rioxarray, np)

    nrows, ncols = dir_da.shape
    transform = dir_da.rio.transform()
    crs = str(dir_da.rio.crs)

    use_strahler = args.use_strahler
    if not use_strahler and np.sum(uparea > 0) == 0:
        use_strahler = True

    if use_strahler:
        if strord is None:
            raise RuntimeError("Stream order not available and uparea empty.")
        mask = strord >= args.min_strahler
    else:
        mask = uparea >= args.min_uparea_km2 * 1e6

    river_idxs = np.where(mask)[0]

    def downstream(i):
        d8 = flw_flat[i]
        if d8 not in valid_d8:
            return -1
        dr, dc = offsets[d8]
        r, c = i // ncols + dr, i % ncols + dc
        return r * ncols + c if 0 <= r < nrows and 0 <= c < ncols else -1

    def upstream_of(idx):
        r, c = idx // ncols, idx % ncols
        out = []
        for d8, (dr, dc) in offsets.items():
            ur, uc = r - dr, c - dc
            if 0 <= ur < nrows and 0 <= uc < ncols:
                us = ur * ncols + uc
                if flw_flat[us] == d8:
                    out.append(us)
        return out

    headwaters = [i for i in river_idxs if not any(mask[u] for u in upstream_of(i) if u >= 0)]

    segments = []
    used = set()
    for hw in headwaters:
        if hw in used:
            continue
        path = [hw]
        cur = hw
        seen = {hw}
        while True:
            ds = downstream(cur)
            if ds < 0 or not mask[ds] or ds in seen:
                break
            seen.add(ds)
            path.append(ds)
            cur = ds
        new = set(path) - used
        if len(new) < args.min_segment_cells:
            continue
        segments.append({"indices": path})
        used.update(path)

    records = []
    for i, seg in enumerate(segments):
        idxs = seg["indices"]
        coords = [(transform[2] + (j % ncols + 0.5) * transform[0], transform[5] + (j // ncols + 0.5) * transform[4]) for j in idxs]
        line = LineString(coords)
        length_km = sum(((coords[k][0] - coords[k - 1][0]) ** 2 + (coords[k][1] - coords[k - 1][1]) ** 2) ** 0.5 for k in range(1, len(coords))) * 111
        ua = uparea[idxs]
        el = elevtn[idxs]
        v_ua = ua[ua > 0]
        v_el = el[el > -9000]
        rec = {
            "geometry": line,
            "seg_id": i,
            "n_cells": len(idxs),
            "length_km": round(length_km, 2),
            "uparea_max_km2": round(float(np.max(v_ua)) / 1e6, 1) if len(v_ua) else -9999,
            "elev_start_m": round(float(el[0]), 1),
            "elev_end_m": round(float(el[-1]), 1),
            "slope_mkm": round(float(el[0] - el[-1]) / max(length_km, 0.01), 2),
        }
        if rivwth is not None:
            wv = rivwth[idxs]
            v_wv = wv[wv > 0]
            rec["rivwth_mean_m"] = round(float(np.mean(v_wv)), 1) if len(v_wv) else -9999
        if strord is not None:
            rec["strord"] = int(strord[idxs].max())
        records.append(rec)

    if not records:
        raise ValueError("No river segments met the configured thresholds.")
    gdf = gpd.GeoDataFrame(records, crs=crs)

    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if args.output_gpkg.exists():
        args.output_gpkg.unlink()
    gdf.to_file(args.output_gpkg, layer=args.output_layer, driver="GPKG")
    geojson = args.output_gpkg.with_suffix(".geojson")
    gdf.to_file(geojson, driver="GeoJSON")

    report = {
        "d8_encoding": enc,
        "river_pixels": int(mask.sum()),
        "segments": int(len(gdf)),
        "total_length_km": float(gdf["length_km"].sum()),
        "output_gpkg": str(args.output_gpkg),
        "output_geojson": str(geojson),
    }
    print(f"CREATED {args.output_gpkg}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
