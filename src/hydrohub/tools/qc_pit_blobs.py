from __future__ import annotations

import argparse
import csv
import json

from hydrohub.tools.common import output_path, require


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Diagnose new-pit blobs created by repair_loops(), with blob CSV/TIF and optional PNG panels")
    result.add_argument("--d8-before", type=output_path, required=True, help="D8 before repair_loops()")
    result.add_argument("--d8-after", type=output_path, required=True, help="D8 after repair_loops()")
    result.add_argument("--dem", type=output_path, required=True)
    result.add_argument("--output-dir", type=output_path, required=True)
    result.add_argument("--n-zoom", type=int, default=5)
    result.add_argument("--create-plots", action=argparse.BooleanOptionalAction, default=True)
    return result


def _read_raster(path, band=1):
    rasterio = require("rasterio")
    with rasterio.open(path) as src:
        return src.read(band), src.profile.copy(), src.transform, src.crs, src.nodata


def _write_raster(path, arr, base_profile, dtype, nodata, rasterio, np):
    profile = base_profile.copy()
    profile.update(driver="GTiff", count=1, dtype=dtype, nodata=nodata, compress="LZW", tiled=True, BIGTIFF="IF_SAFER")
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.asarray(arr).astype(dtype), 1)
    print(f"[WRITE] {path}")


def main() -> int:
    args = parser().parse_args()
    np = require("numpy")
    rasterio = require("rasterio")
    ndimage = require("scipy.ndimage", "scipy")
    pyflwdir = require("pyflwdir", "pyflwdir")

    d8_before, d8_profile, _, d8_crs, d8_nodata = _read_raster(args.d8_before)
    d8_after, _, _, _, _ = _read_raster(args.d8_after)
    dem, _, dem_transform, dem_crs, dem_nodata = _read_raster(args.dem)

    if not (d8_before.shape == d8_after.shape == dem.shape):
        raise ValueError("Input rasters do not share the same shape/grid.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    valid_mask = d8_before != (d8_nodata if d8_nodata is not None else 247)
    latlon = bool(dem_crs.is_geographic)

    dem_f = np.asarray(dem, dtype=np.float32)
    if dem_nodata is not None:
        dem_f = np.where(dem == dem_nodata, np.nan, dem_f)
    dem_fill = np.where(np.isfinite(dem_f), dem_f, -9999.0).astype(np.float32)
    slope = pyflwdir.dem.slope(elevtn=dem_fill, nodata=-9999.0, latlon=latlon, transform=dem_transform).astype(np.float32)
    slope_deg = np.degrees(np.arctan(slope))

    new_pits = valid_mask & (d8_after == 0) & (d8_before != 0)
    n_new = int(new_pits.sum())
    n_valid = int(valid_mask.sum())

    lbl, n_blobs = ndimage.label(new_pits, structure=np.ones((3, 3)))
    blob_ids = np.arange(1, n_blobs + 1)
    sizes = ndimage.sum(new_pits, lbl, blob_ids).astype(int)
    objs = ndimage.find_objects(lbl)

    rows = []
    for i, sl in enumerate(objs):
        bid = i + 1
        if sl is None:
            continue
        local = lbl[sl] == bid
        elev = dem_f[sl][local]
        slp = slope_deg[sl][local]
        ev, sv = elev[np.isfinite(elev)], slp[np.isfinite(slp)]
        rows.append({
            "blob_id": bid,
            "size_cells": int(sizes[i]),
            "row_min": sl[0].start,
            "row_max": sl[0].stop - 1,
            "col_min": sl[1].start,
            "col_max": sl[1].stop - 1,
            "mean_elev_m": float(np.mean(ev)) if ev.size else "",
            "std_elev_m": float(np.std(ev)) if ev.size else "",
            "mean_slope_deg": float(np.mean(sv)) if sv.size else "",
            "frac_flat_under_0.5deg": float(np.mean(sv < 0.5)) if sv.size else 0.0,
        })
    rows.sort(key=lambda r: r["size_cells"], reverse=True)

    csv_path = args.output_dir / "new_pit_blobs.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["blob_id", "size_cells"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"[WRITE] {csv_path}")

    _write_raster(args.output_dir / "new_pit_blobs.tif", np.where(new_pits, lbl, 0).astype(np.int32), d8_profile, "int32", 0, rasterio, np)

    row_counts = new_pits.sum(axis=1)
    col_counts = new_pits.sum(axis=0)

    def flag_seams(counts, label, min_count=200, factor=8):
        med = np.median(counts[counts > 0]) if np.any(counts > 0) else 0
        thresh = max(min_count, med * factor)
        idx = np.where(counts > thresh)[0]
        return label, idx.tolist(), [int(counts[i]) for i in idx]

    row_seam, row_idx, row_c = flag_seams(row_counts, "row")
    col_seam, col_idx, col_c = flag_seams(col_counts, "column")

    if args.create_plots:
        try:
            plt = require("matplotlib.pyplot", "matplotlib")
            plt.ioff()
            st = 4
            dem_show = dem_f[::st, ::st]
            pits_show = new_pits[::st, ::st]
            fig, ax = plt.subplots(figsize=(16, 6))
            im = ax.imshow(dem_show, cmap="terrain", vmin=0, vmax=np.nanpercentile(dem_show, 98))
            overlay = np.ma.masked_where(~pits_show, pits_show.astype(np.uint8))
            ax.imshow(overlay, cmap="autumn_r", vmin=0, vmax=1, alpha=0.9)
            ax.set_title(f"New pit cells created by repair_loops() ({n_new:,} cells / {n_blobs} blobs)")
            plt.colorbar(im, ax=ax, label="elevation (m)", fraction=0.02, pad=0.01)
            fig.tight_layout()
            ov_path = args.output_dir / "new_pit_overview.png"
            fig.savefig(ov_path, dpi=150)
            plt.close(fig)
            print(f"[WRITE] {ov_path}")

            for rank, r in enumerate(rows[: args.n_zoom], start=1):
                r0 = max(r["row_min"] - 40, 0)
                r1 = min(r["row_max"] + 40, dem.shape[0] - 1)
                c0 = max(r["col_min"] - 40, 0)
                c1 = min(r["col_max"] + 40, dem.shape[1] - 1)
                sl = (slice(r0, r1 + 1), slice(c0, c1 + 1))
                fig, axes = plt.subplots(1, 3, figsize=(16, 5))
                axes[0].imshow(np.where(np.isfinite(dem_f[sl]), dem_f[sl], np.nan), cmap="terrain")
                axes[0].set_title("Elevation")
                axes[1].imshow(slope_deg[sl], cmap="viridis", vmax=np.nanpercentile(slope_deg[sl], 95))
                axes[1].set_title("Slope (deg)")
                axes[2].imshow(np.where(valid_mask[sl], d8_before[sl], np.nan), cmap="tab10")
                hole_ov = np.ma.masked_where(~new_pits[sl], new_pits[sl].astype(np.uint8))
                axes[2].imshow(hole_ov, cmap="autumn_r", vmin=0, vmax=1, alpha=0.9)
                axes[2].set_title("D8 before (colors) + new pits (red)")
                fig.suptitle(f"Blob rank {rank} | size={r['size_cells']:,} cells | mean elev={r['mean_elev_m']} m | flat frac={r['frac_flat_under_0.5deg']:.2f}")
                fig.tight_layout()
                zoom_path = args.output_dir / f"blob_rank{rank:02d}_zoom.png"
                fig.savefig(zoom_path, dpi=150)
                plt.close(fig)
                print(f"[WRITE] {zoom_path}")
        except Exception as exc:
            print(f"[WARN] Plotting skipped: {exc}")

    report = {
        "output_dir": str(args.output_dir),
        "crs": str(d8_crs),
        "valid_cells": n_valid,
        "new_pit_cells": n_new,
        "new_pit_percent_of_domain": float(100.0 * n_new / n_valid) if n_valid else 0.0,
        "new_pit_blobs": n_blobs,
        "largest_blob_cells": rows[0]["size_cells"] if rows else 0,
        "row_seams": row_idx,
        "column_seams": col_idx,
        "blob_csv": str(csv_path),
    }
    (args.output_dir / "new_pit_diagnostics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"CREATED {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())