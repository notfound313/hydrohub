from __future__ import annotations

import argparse
import json

from hydrohub.tools.common import output_path, require

LDD_NAMES = ("local_drain_direction", "wflow_ldd")
SUB_NAMES = ("subcatchment", "wflow_subcatch")
OFF = {1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, -1), 5: (0, 0), 6: (0, 1), 7: (-1, -1), 8: (-1, 0), 9: (-1, 1)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Detect LDD cycles and invalid flow targets inside a HydroMT staticmaps NetCDF")
    result.add_argument("--staticmaps", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    return result


def _findvar(ds, names):
    low = {k.lower(): k for k in ds.data_vars}
    for n in names:
        if n in ds.data_vars:
            return n
        if n.lower() in low:
            return low[n.lower()]
    return None


def _gridinfo(ds, da, Affine, rasterio, np):
    ydim, xdim = da.dims[-2], da.dims[-1]
    x = np.asarray(ds[xdim].values, float)
    y = np.asarray(ds[ydim].values, float)
    dx = float(np.median(np.diff(x)))
    dy = float(np.median(np.diff(y)))
    tr = Affine.translation(x[0] - dx / 2, y[0] - dy / 2) * Affine.scale(dx, dy)
    crs = None
    if "spatial_ref" in ds.variables:
        a = ds["spatial_ref"].attrs
        for k in ("crs_wkt", "spatial_ref"):
            if a.get(k):
                try:
                    crs = rasterio.crs.CRS.from_wkt(a[k])
                    break
                except Exception:
                    pass
    if crs is None:
        crs = rasterio.crs.CRS.from_epsg(4326)
    return tr, crs, x, y


def main() -> int:
    args = parser().parse_args()
    from collections import deque

    np = require("numpy")
    xr = require("xarray")
    rasterio = require("rasterio")
    Affine = require("affine", "affine")
    gpd = require("geopandas", "geopandas")
    pyflwdir = require("pyflwdir", "pyflwdir")

    if not args.staticmaps.exists():
        raise FileNotFoundError(args.staticmaps)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ds = xr.open_dataset(args.staticmaps, mask_and_scale=False)
    ldd_name = _findvar(ds, LDD_NAMES)
    if not ldd_name:
        raise KeyError(f"LDD not found. Available: {list(ds.data_vars)}")
    da = ds[ldd_name]
    ldd = np.rint(np.asarray(da.values)).astype(np.int16)
    tr, crs, x, y = _gridinfo(ds, da, Affine, rasterio, np)

    sub_name = _findvar(ds, SUB_NAMES)
    active = (np.asarray(ds[sub_name].values) > 0) if sub_name else np.isin(ldd, np.arange(1, 10))
    invalid = active & ~np.isin(ldd, np.arange(1, 10))
    graph = active & np.isin(ldd, np.arange(1, 10))

    nr, nc = ldd.shape
    dsidx = np.full(nr * nc, -1, np.int64)
    bad = np.zeros((nr, nc), bool)
    rr, cc = np.where(graph)
    for r, c in zip(rr, cc):
        code = int(ldd[r, c])
        if code == 5:
            continue
        dr, dc = OFF[code]
        r2, c2 = r + dr, c + dc
        if r2 < 0 or r2 >= nr or c2 < 0 or c2 >= nc or not graph[r2, c2]:
            bad[r, c] = True
        else:
            dsidx[r * nc + c] = r2 * nc + c2

    flat = graph.ravel()
    nodes = np.flatnonzero(flat)
    indeg = np.zeros(dsidx.size, np.int32)
    src = nodes[dsidx[nodes] >= 0]
    np.add.at(indeg, dsidx[src], 1)
    q = deque(int(i) for i in nodes if indeg[i] == 0)
    removed = np.zeros(dsidx.size, bool)
    while q:
        i = q.popleft()
        if removed[i]:
            continue
        removed[i] = True
        j = dsidx[i]
        if j >= 0:
            indeg[j] -= 1
            if indeg[j] == 0:
                q.append(int(j))
    cycflat = flat & (~removed) & (dsidx >= 0)

    labels = np.zeros(dsidx.size, np.int32)
    seen = np.zeros(dsidx.size, bool)
    cid = 0
    for s in np.flatnonzero(cycflat):
        if seen[s]:
            continue
        path = []
        pos = {}
        cur = int(s)
        while cycflat[cur] and not seen[cur]:
            if cur in pos:
                cid += 1
                for i in path[pos[cur]:]:
                    labels[i] = cid
                break
            pos[cur] = len(path)
            path.append(cur)
            cur = int(dsidx[cur])
        for i in path:
            seen[i] = True
    cycid = labels.reshape(ldd.shape)
    ncyc = int(cid)
    cycmask = cycflat.reshape(ldd.shape)

    def write_tif(path, a, dtype, nodata=0):
        with rasterio.open(path, "w", driver="GTiff", height=a.shape[0], width=a.shape[1], count=1, dtype=dtype, crs=crs, transform=tr, nodata=nodata, compress="LZW", tiled=True, BIGTIFF="IF_SAFER") as dst:
            dst.write(a.astype(dtype), 1)

    write_tif(args.output_dir / "invalid_ldd_code.tif", invalid, "uint8")
    write_tif(args.output_dir / "bad_target.tif", bad, "uint8")
    write_tif(args.output_dir / "cycle_mask.tif", cycmask, "uint8")
    write_tif(args.output_dir / "cycle_id.tif", cycid, "int32")

    if cycmask.any():
        r, c = np.where(cycmask)
        g = gpd.GeoDataFrame({"cycle_id": cycid[r, c], "ldd": ldd[r, c]}, geometry=gpd.points_from_xy(x[c], y[r]), crs=crs)
        g.to_file(args.output_dir / "cycle_cells.gpkg", layer="cycle_cells", driver="GPKG")
    if bad.any():
        r, c = np.where(bad)
        g = gpd.GeoDataFrame({"ldd": ldd[r, c]}, geometry=gpd.points_from_xy(x[c], y[r]), crs=crs)
        g.to_file(args.output_dir / "bad_target_cells.gpkg", layer="bad_targets", driver="GPKG")

    ldd_pf = np.full(ldd.shape, 255, np.uint8)
    ldd_pf[graph] = ldd[graph].astype(np.uint8)
    pf_valid = False
    pf_error = None
    try:
        flw = pyflwdir.from_array(ldd_pf, ftype="ldd", check_ftype=True, mask=graph, transform=tr, latlon=crs.is_geographic, cache=True)
        pf_valid = bool(flw.isvalid)
    except Exception as exc:
        pf_error = repr(exc)

    report = {
        "staticmaps": str(args.staticmaps),
        "ldd_variable": ldd_name,
        "subcatch_variable": sub_name,
        "active_cells": int(active.sum()),
        "invalid_ldd_code_cells": int(invalid.sum()),
        "bad_target_cells": int(bad.sum()),
        "number_of_cycles": ncyc,
        "cycle_cells": int(cycmask.sum()),
        "pyflwdir_isvalid": pf_valid,
        "pyflwdir_error": pf_error,
        "sound_ldd": bool(invalid.sum() == 0 and bad.sum() == 0 and cycmask.sum() == 0 and pf_valid),
    }
    (args.output_dir / "staticmaps_ldd_qc_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print("QC PASS =", report["sound_ldd"])
    return 0 if report["sound_ldd"] else 2


if __name__ == "__main__":
    raise SystemExit(main())