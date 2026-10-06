from __future__ import annotations

import base64
from html import escape
import io
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from hydrohub.results.analysis import NO_DATA_COLOR, NO_DATA_LABEL


def _normalize_feature_id(value) -> str | None:
    if value is None or isinstance(value, (bool, np.bool_)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)):
            return None
        return str(int(value)) if float(value).is_integer() else str(value).strip()
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        if math.isfinite(numeric) and numeric.is_integer():
            return str(int(numeric))
    except ValueError:
        pass
    return text


def _candidate_feature_ids(values, geometry_name: str = "geometry") -> list[str]:
    preferred_tokens = ("station", "gauge", "outlet", "id", "fid", "index", "value")
    preferred = []
    fallback = []
    for key, value in values.items():
        if key == geometry_name:
            continue
        normalized = _normalize_feature_id(value)
        if normalized is None:
            continue
        target = preferred if any(token in str(key).lower() for token in preferred_tokens) else fallback
        target.append(normalized)
    return list(dict.fromkeys(preferred + fallback))


def _apply_ssfi_values(frame, geojson: dict, values: dict[str, dict]) -> dict:
    rows = frame.drop(columns=frame.geometry.name).to_dict("records")
    for feature, row in zip(geojson.get("features", []), rows):
        properties = feature.setdefault("properties", {})
        matched_id = next((candidate for candidate in _candidate_feature_ids(row) if candidate in values), None)
        info = values.get(matched_id) if matched_id is not None else None
        if info is None:
            properties["__fill_color"] = NO_DATA_COLOR
            properties["__ssfi_label"] = NO_DATA_LABEL
            continue
        value = info.get("value")
        label = info.get("label") or NO_DATA_LABEL
        properties["__fill_color"] = info.get("color") or NO_DATA_COLOR
        properties["__ssfi_value"] = value
        properties["__ssfi_label"] = label
        value_text = f"{value:.2f}" if isinstance(value, (int, float)) else "tidak tersedia"
        properties["__popup_html"] = (
            f"<b>Basin/Stasiun {escape(matched_id)}</b><br>"
            f"Nilai SSFI: {value_text}<br>Klasifikasi: {escape(label)}"
        )
    return geojson


def _spatial_dimensions(dimensions) -> tuple[str, str]:
    names = list(dimensions)
    pairs = (("y", "x"), ("lat", "lon"), ("latitude", "longitude"), ("north", "east"))
    lower = {name.lower(): name for name in names}
    for y_name, x_name in pairs:
        if y_name in lower and x_name in lower:
            return lower[y_name], lower[x_name]
    raise ValueError(f"No supported spatial dimensions found in {names}.")


def _crs(dataset):
    try:
        value = dataset.rio.crs
        if value is not None:
            return value
    except Exception:
        pass
    try:
        from pyproj import CRS

        for data in dataset.data_vars.values():
            mapping = data.attrs.get("grid_mapping")
            if mapping and mapping in dataset.variables:
                attributes = dataset[mapping].attrs
                value = attributes.get("crs_wkt") or attributes.get("spatial_ref")
                if value:
                    return CRS.from_wkt(value)
    except Exception:
        pass
    return None


def _edge_bounds(values) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    if not len(values):
        raise ValueError("Spatial coordinate is empty.")
    if len(values) == 1:
        return float(values[0] - 0.5), float(values[0] + 0.5)
    step_start = (values[1] - values[0]) / 2
    step_end = (values[-1] - values[-2]) / 2
    return tuple(sorted((float(values[0] - step_start), float(values[-1] + step_end))))


def _render_dataarray(
    data,
    dataset,
    max_dimension: int,
    colormap: str,
    value_range=None,
    *,
    categorical: bool = False,
):
    from PIL import Image
    from matplotlib import colormaps
    from matplotlib.colors import Normalize
    from rasterio.warp import transform_bounds

    y_dim, x_dim = _spatial_dimensions(data.dims)
    selectors = {dim: 0 for dim in data.dims if dim not in {y_dim, x_dim}}
    if selectors:
        data = data.isel(selectors)
    height, width = data.sizes[y_dim], data.sizes[x_dim]
    west, east = _edge_bounds(data[x_dim].values)
    south, north = _edge_bounds(data[y_dim].values)
    factor = max(1, int(np.ceil(max(height, width) / max_dimension)))
    if factor > 1:
        # Preview sampling must happen before materializing the array. Eager
        # coarsen().mean() can allocate the entire native grid just to display it.
        data = data.isel({y_dim: slice(None, None, factor), x_dim: slice(None, None, factor)})
    array = np.asarray(data.values, dtype=np.float32)
    x = np.asarray(data[x_dim].values, dtype=float)
    y = np.asarray(data[y_dim].values, dtype=float)
    if x[0] > x[-1]:
        array = np.fliplr(array)
        x = x[::-1]
    if y[0] < y[-1]:
        array = np.flipud(array)
        y = y[::-1]
    valid = np.isfinite(array)
    if not valid.any():
        raise ValueError("Raster contains no finite values.")
    if value_range is None:
        minimum, maximum = float(np.nanmin(array)), float(np.nanmax(array))
    else:
        minimum, maximum = map(float, value_range)
    if minimum == maximum:
        maximum = minimum + 1.0
    rgba = colormaps.get_cmap(colormap)(Normalize(minimum, maximum, clip=True)(array), bytes=True)
    rgba[~valid, 3] = 0
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
    image_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    source_crs = _crs(dataset)
    if source_crs is not None and str(source_crs).upper() not in {"EPSG:4326", "WGS84"}:
        west, south, east, north = transform_bounds(
            source_crs, "EPSG:4326", west, south, east, north
        )
    return image_url, [[south, west], [north, east]], (minimum, maximum)


class ForcingReader:
    def __init__(self, path: Path):
        import xarray as xr
        import rioxarray  # noqa: F401

        self.path = path.resolve()
        self.is_csv = self.path.suffix.lower() == ".csv"
        self._ds = None
        self._df = None
        self.time_dimension = None
        if self.is_csv:
            frame = pd.read_csv(self.path)
            if not len(frame.columns):
                raise ValueError("Forcing CSV has no columns.")
            time_column = next(
                (column for column in frame.columns if column.lower() in {"time", "date", "datetime"}),
                frame.columns[0],
            )
            frame[time_column] = pd.to_datetime(frame[time_column], errors="coerce")
            self._invalid_csv_times = int(frame[time_column].isna().sum())
            self._df = frame.loc[frame[time_column].notna()].set_index(time_column)
        else:
            self._ds = xr.open_dataset(self.path, decode_times=True, cache=False)
            self.time_dimension = next(
                (name for name in ("time", "datetime", "date") if name in self._ds.dims), None
            )
            if self.time_dimension is None:
                raise ValueError("Forcing dataset has no recognized time dimension.")

    def list_variables(self) -> list[str]:
        if self.is_csv:
            return [str(column) for column in self._df.columns]
        result = []
        for name, data in self._ds.data_vars.items():
            if self.time_dimension not in data.dims:
                continue
            try:
                _spatial_dimensions(data.dims)
            except ValueError:
                continue
            result.append(str(name))
        return result

    def as_table(self, variable: str, limit: int = 5000) -> pd.DataFrame:
        if self.is_csv:
            return self._df[[variable]].reset_index().iloc[:limit]
        data = self._ds[variable]
        spatial = [dim for dim in data.dims if dim != self.time_dimension]
        # Bound working memory to one timestep, even without a Dask backend.
        count = min(limit, self.frame_count())
        values = []
        for start in range(0, count, 4):
            block = data.isel({self.time_dimension: slice(start, min(start + 4, count))})
            values.extend(np.asarray(block.mean(dim=spatial, skipna=True).values).tolist())
        series = pd.Series(values, index=pd.to_datetime(data[self.time_dimension].values[:count]))
        series.index.name = self.time_dimension
        return series.rename(variable).to_frame().reset_index().iloc[:limit]

    def frame_count(self) -> int:
        return len(self._df) if self.is_csv else int(self._ds.sizes[self.time_dimension])

    def time_value(self, index: int):
        return self._df.index[index] if self.is_csv else self._ds[self.time_dimension].values[index]

    def time_values(self):
        return self._df.index.to_numpy(copy=True) if self.is_csv else self._ds[self.time_dimension].values.copy()

    def metadata(self) -> dict:
        if self.is_csv:
            return {name: {"dtype": str(self._df[name].dtype), "dims": ["time"],
                           "shape": [len(self._df)], "units": "", "description": ""}
                    for name in self._df.columns}
        return {name: {"dtype": str(data.dtype), "dims": list(data.dims),
                       "shape": list(data.shape), "units": data.attrs.get("units", ""),
                       "description": data.attrs.get("long_name", data.attrs.get("standard_name", ""))}
                for name, data in self._ds.data_vars.items()}

    def time_diagnostics(self, frequency: str = "auto") -> dict:
        raw = self.time_values()
        calendar = "standard" if self.is_csv else self._ds[self.time_dimension].encoding.get("calendar", "standard")
        if len(raw) and hasattr(raw[0], "calendar"):
            times = self._ds.indexes[self.time_dimension]
            calendar = raw[0].calendar
            invalid = 0
        else:
            times = pd.DatetimeIndex(pd.to_datetime(raw, errors="coerce"))
            invalid = int(times.isna().sum()) + getattr(self, "_invalid_csv_times", 0)
            times = times.dropna()
        ordered = times.unique().sort_values()
        deltas = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
        interval = None
        if frequency != "auto":
            interval = pd.Timedelta(frequency).to_pytimedelta()
        elif deltas:
            interval = pd.Series(deltas).mode().iloc[0]
        missing, gaps = 0, []
        if interval is not None:
            for left, right in zip(ordered[:-1], ordered[1:]):
                count = max(0, int((right - left) // interval) - 1)
                if count:
                    missing += count
                    gaps.append((str(left + interval), str(left + count * interval), count))
        return {"count": len(raw) + getattr(self, "_invalid_csv_times", 0), "first": str(ordered[0]) if len(ordered) else "—",
                "last": str(ordered[-1]) if len(ordered) else "—", "calendar": calendar,
                "interval": str(interval) if interval is not None else "unknown",
                "invalid": invalid, "duplicates": len(times) - len(ordered),
                "ordered": bool(times.is_monotonic_increasing), "missing": missing, "gaps": gaps}

    def missing_values(self, variable: str) -> tuple[pd.DataFrame, int]:
        data = self._df[variable] if self.is_csv else self._ds[variable]
        if not self.is_csv and self.time_dimension not in data.dims:
            raise ValueError("Variable has no time dimension.")
        times = self.time_values()
        cells = 1 if self.is_csv else int(data.size // max(1, len(times)))
        rows = []
        for index, time in enumerate(times):
            values = np.asarray([data.iloc[index]] if self.is_csv else data.isel({self.time_dimension: index}).values)
            missing = pd.isna(values)
            if np.issubdtype(values.dtype, np.number):
                missing = ~np.isfinite(values)
            count = int(missing.sum())
            if count:
                rows.append((str(time), count, cells, 100.0 * (cells - count) / max(1, cells)))
        return pd.DataFrame(rows, columns=("time", "missing_cells", "total_cells", "coverage_pct")), cells

    def value_range(self, variable: str) -> tuple[float, float]:
        if self.is_csv:
            data = self._df[variable]
            minimum, maximum = float(data.min(skipna=True)), float(data.max(skipna=True))
        else:
            minimum, maximum = float("inf"), float("-inf")
            data = self._ds[variable]
            for start in range(0, self.frame_count(), 4):
                values = np.asarray(data.isel({self.time_dimension: slice(start, start + 4)}).values)
                if np.isfinite(values).any():
                    minimum = min(minimum, float(np.nanmin(values)))
                    maximum = max(maximum, float(np.nanmax(values)))
            if not np.isfinite(minimum):
                raise ValueError("Forcing variable contains no finite values.")
        return (minimum, maximum if maximum != minimum else minimum + 1.0)

    def missing_active_cells(self, variable: str, staticmaps_path: Path) -> tuple[pd.DataFrame, int]:
        if self.is_csv:
            raise ValueError("CSV forcing has no spatial grid.")

        import xarray as xr

        data = self._ds[variable]
        y_dim, x_dim = _spatial_dimensions(data.dims)
        selectors = {
            dim: 0
            for dim in data.dims
            if dim not in {self.time_dimension, y_dim, x_dim}
        }
        if selectors:
            data = data.isel(selectors)
        data = data.transpose(self.time_dimension, y_dim, x_dim)

        with xr.open_dataset(staticmaps_path, decode_times=False) as staticmaps:
            names = {name.lower(): name for name in staticmaps.data_vars}
            mask_name = next(
                (names[name] for name in ("subcatchment", "wflow_subcatch") if name in names),
                None,
            )
            ldd_mask = False
            if mask_name is None:
                mask_name = next(
                    (names[name] for name in ("local_drain_direction", "wflow_ldd") if name in names),
                    None,
                )
                ldd_mask = True
            if mask_name is None:
                raise ValueError("Staticmaps has no subcatchment or local drain direction mask.")
            mask_data = staticmaps[mask_name]
            mask_y, mask_x = _spatial_dimensions(mask_data.dims)
            mask_selectors = {dim: 0 for dim in mask_data.dims if dim not in {mask_y, mask_x}}
            if mask_selectors:
                mask_data = mask_data.isel(mask_selectors)
            mask_values = np.asarray(mask_data.transpose(mask_y, mask_x).values)
            active = (
                np.isin(mask_values, np.arange(1, 10))
                if ldd_mask
                else np.isfinite(mask_values) & (mask_values > 0)
            )

            if active.shape != (data.sizes[y_dim], data.sizes[x_dim]):
                raise ValueError(
                    f"Forcing grid {data.sizes[y_dim]}x{data.sizes[x_dim]} does not match "
                    f"staticmaps grid {active.shape[0]}x{active.shape[1]}."
                )
            for forcing_dim, static_dim, axis in ((y_dim, mask_y, 0), (x_dim, mask_x, 1)):
                forcing_coords = np.asarray(data[forcing_dim].values)
                static_coords = np.asarray(mask_data[static_dim].values)
                if np.allclose(forcing_coords, static_coords, equal_nan=True):
                    continue
                if np.allclose(forcing_coords, static_coords[::-1], equal_nan=True):
                    active = np.flip(active, axis=axis)
                    continue
                raise ValueError(f"Forcing and staticmaps {forcing_dim} coordinates do not match.")

        active_count = int(active.sum())
        if not active_count:
            raise ValueError("Staticmaps contains no active model cells.")
        times = pd.to_datetime(data[self.time_dimension].values)
        rows = []
        for start in range(0, len(times), 32):
            values = np.asarray(data.isel({self.time_dimension: slice(start, start + 32)}).values)
            missing = ((~np.isfinite(values)) & active).sum(axis=(1, 2))
            rows.extend(
                (times[start + offset], int(count), active_count, 100.0 * (active_count - count) / active_count)
                for offset, count in enumerate(missing)
                if count
            )
        return pd.DataFrame(
            rows,
            columns=("time", "missing_active_cells", "active_cells", "coverage_pct"),
        ), active_count

    def render_frame(self, variable: str, index: int, max_dimension: int, colormap: str, value_range=None):
        if self.is_csv:
            raise ValueError("CSV forcing has no spatial grid.")
        data = self._ds[variable].isel({self.time_dimension: index})
        return _render_dataarray(data, self._ds, max_dimension, colormap, value_range)

    def close(self) -> None:
        if self._ds is not None:
            self._ds.close()


class StaticmapsReader:
    def __init__(self, path: Path):
        import xarray as xr
        import rioxarray  # noqa: F401

        self.path = path.resolve()
        self._ds = xr.open_dataset(self.path, decode_times=False, cache=False)

    def list_variables(self) -> list[str]:
        result = []
        for name, data in self._ds.data_vars.items():
            try:
                _spatial_dimensions(data.dims)
            except ValueError:
                continue
            result.append(str(name))
        return result

    def render(self, variable: str, max_dimension: int, colormap: str):
        categorical = any(
            token in variable.lower()
            for token in (
                "subcatch",
                "subbasin",
                "landuse",
                "outlet",
                "gauge",
                "streamorder",
                "mask",
                "local_drain_direction",
            )
        )
        return _render_dataarray(
            self._ds[variable],
            self._ds,
            max_dimension,
            colormap,
            categorical=categorical,
        )

    def close(self) -> None:
        self._ds.close()

    def outlet_points(self) -> list[dict]:
        candidates = [
            name
            for name in self.list_variables()
            if any(token in name.lower() for token in ("outlet", "gauge", "station"))
        ]
        for name in candidates:
            data = self._ds[name]
            try:
                y_dim, x_dim = _spatial_dimensions(data.dims)
            except ValueError:
                continue
            selectors = {dim: 0 for dim in data.dims if dim not in {y_dim, x_dim}}
            if selectors:
                data = data.isel(selectors)
            values = np.asarray(data.values)
            points = []
            for station_id in np.unique(values[np.isfinite(values)]):
                if float(station_id) <= 0:
                    continue
                rows, columns = np.where(values == station_id)
                if not len(rows):
                    continue
                points.append(
                    {
                        "station_id": str(int(station_id)),
                        "station_ids": [str(int(station_id))],
                        "lat": float(np.mean(data[y_dim].values[rows])),
                        "lon": float(np.mean(data[x_dim].values[columns])),
                        "properties": {"source": name},
                        "layer_name": name,
                    }
                )
            if points:
                return points
        return []


class StaticgeomReader:
    def __init__(self, paths: tuple[Path, ...]):
        self.paths = paths
        self._cache = {}

    def path_for_layer(self, name: str) -> Path | None:
        return next((path for path in self.paths if path.stem == name), None)

    def layer_names(self) -> list[str]:
        return [path.stem for path in self.paths]

    def _frame(self, path: Path):
        import geopandas as gpd

        if path not in self._cache:
            frame = gpd.read_file(path)
            if frame.crs is not None and str(frame.crs).upper() != "EPSG:4326":
                frame = frame.to_crs(4326)
            self._cache[path] = frame
        return self._cache[path]

    def polygon_layer_names(self) -> list[str]:
        names = []
        for path in self.paths:
            try:
                frame = self._frame(path)
            except Exception:
                continue
            if not frame.empty and frame.geometry.geom_type.str.contains("Polygon").any():
                names.append(path.stem)
        return names

    def geojson(self, path: Path, values: dict[str, dict] | None = None) -> dict:
        frame = self._frame(path)
        geojson = json.loads(frame.to_json(drop_id=True))
        if not values:
            return geojson
        return _apply_ssfi_values(frame, geojson, values)

    def station_points(self) -> list[dict]:
        points = []
        for path in self.paths:
            try:
                frame = self._frame(path)
            except Exception:
                continue
            if frame.empty:
                continue
            for _, row in frame.loc[frame.geometry.geom_type.isin(("Point", "MultiPoint"))].iterrows():
                geometry = row.geometry
                if geometry.geom_type == "MultiPoint":
                    geometry = next(iter(geometry.geoms), None)
                if geometry is None:
                    continue
                properties = {}
                for key, value in row.items():
                    if key == frame.geometry.name:
                        continue
                    try:
                        valid = bool(pd.notna(value))
                    except (TypeError, ValueError):
                        valid = True
                    if valid:
                        properties[key] = value
                station_ids = _candidate_feature_ids(properties)
                points.append(
                    {
                        "station_id": station_ids[0] if station_ids else str(len(points) + 1),
                        "station_ids": station_ids,
                        "lat": float(geometry.y),
                        "lon": float(geometry.x),
                        "properties": properties,
                        "layer_name": path.stem,
                    }
                )
        return points

    def find_station_point(self, station_id: str) -> dict | None:
        target = _normalize_feature_id(station_id)
        if target is None:
            return None
        return next(
            (point for point in self.station_points() if target in point["station_ids"]),
            None,
        )

    def clear(self) -> None:
        self._cache.clear()


def geometry_geojson(path: Path, ssfi_values: dict[str, dict] | None = None) -> dict:
    import geopandas as gpd

    frame = gpd.read_file(path)
    if frame.crs is not None and str(frame.crs).upper() != "EPSG:4326":
        frame = frame.to_crs(4326)
    geojson = json.loads(frame.to_json(drop_id=True))
    if not ssfi_values:
        return geojson
    return _apply_ssfi_values(frame, geojson, ssfi_values)
