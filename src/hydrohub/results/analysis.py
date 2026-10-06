from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd


FREQ_LABELS = {"Harian": "D", "Bulanan": "MS", "Tahunan": "YS"}
SSFI_SCALES = (1, 3, 9, 12)


def extract_station_id(column: str) -> str | None:
    matches = re.findall(r"(\d+)", str(column))
    return str(int(matches[-1])) if matches else None


class OutputReader:
    TIME_NAMES = ("time", "datetime", "date", "timestamp")

    def __init__(self, path: Path):
        import xarray as xr

        self.path = path.resolve()
        self.is_csv = self.path.suffix.lower() == ".csv"
        self._ds = None
        self._df = None
        self._time_dim = None
        if self.is_csv:
            import csv
            with self.path.open(encoding="utf-8-sig") as stream:
                sample = "".join(line for _, line in zip(range(20), stream) if not line.startswith("#"))
            try:
                self._separator = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                self._separator = ","
            first_row = next(csv.reader(sample.splitlines(), delimiter=self._separator), [])
            self._csv_header = 1 if first_row and first_row[0] == "basin_id" else 0
            frame = pd.read_csv(self.path, sep=self._separator, comment="#", header=self._csv_header, nrows=5000)
            if not len(frame.columns):
                raise ValueError("Output CSV has no columns.")
            lower = {str(column).strip().lower(): column for column in frame.columns}
            time_column = next(
                (lower[name] for name in self.TIME_NAMES if name in lower), frame.columns[0]
            )
            self._time_column = time_column
            frame[time_column] = pd.to_datetime(frame[time_column], errors="coerce")
            frame = frame.loc[frame[time_column].notna()].set_index(time_column)
            if frame.empty:
                raise ValueError("Output CSV has no valid timestamps.")
            self._df = frame.sort_index(kind="stable")
        else:
            self._ds = xr.open_dataset(self.path, decode_times=True, cache=False)
            self._time_dim = self._detect_time_dimension()

    def _detect_time_dimension(self) -> str | None:
        for name in self.TIME_NAMES:
            if name in self._ds.dims:
                return name
        for coordinate in self._ds.coords.values():
            try:
                if coordinate.ndim == 1 and np.issubdtype(coordinate.dtype, np.datetime64):
                    return coordinate.dims[0]
            except TypeError:
                continue
        return None

    @staticmethod
    def is_discharge_variable(variable: str) -> bool:
        name = str(variable).lower()
        return any(token in name for token in ("river_q", "discharge", "streamflow")) or name in {"q"}

    def list_variables(self) -> list[str]:
        if self.is_csv:
            # Do not drop stations whose first preview rows are all missing.
            variables = [str(column) for column in self._df.columns
                         if self._df[column].isna().all()
                         or pd.to_numeric(self._df[column], errors="coerce").notna().any()]
        else:
            variables = []
            for name, data in self._ds.data_vars.items():
                try:
                    numeric = np.issubdtype(data.dtype, np.number)
                except TypeError:
                    numeric = False
                if numeric and data.size and self._time_dim in data.dims:
                    variables.append(str(name))
        return sorted(
            variables, key=lambda name: (not self.is_discharge_variable(name), name.lower())
        )

    def get_timeseries(self, variable: str) -> pd.Series:
        if self.is_csv:
            frame = pd.read_csv(self.path, sep=self._separator, comment="#", header=self._csv_header, usecols=[self._time_column, variable])
            index = pd.to_datetime(frame[self._time_column], errors="coerce")
            series = pd.Series(
                pd.to_numeric(frame[variable], errors="coerce").to_numpy(dtype=float),
                index=index,
                name=variable,
            )
            series = series.loc[~series.index.isna()]
        else:
            data = self._ds[variable]
            if self._time_dim not in data.dims:
                raise ValueError(f"{variable} has no recognized time dimension.")
            selectors = {dim: 0 for dim in data.dims if dim != self._time_dim}
            if selectors:
                data = data.isel(selectors)
            index = pd.to_datetime(data[self._time_dim].values, errors="coerce")
            values = pd.to_numeric(
                pd.Series(np.asarray(data.values).reshape(-1)), errors="coerce"
            ).to_numpy()
            series = pd.Series(values, index=index, name=variable)
            series = series.loc[~series.index.isna()]
        series = series.sort_index(kind="stable")
        if series.index.has_duplicates:
            series = series.groupby(level=0).mean()
            series.name = variable
        return series

    def get_unit(self, variable: str) -> str:
        if not self.is_csv:
            unit = self._ds[variable].attrs.get("units") or self._ds[variable].attrs.get("unit")
            if unit:
                return str(unit)
        return "m3/s" if self.is_discharge_variable(variable) else ""

    def station_label(self, variable: str) -> str:
        station_id = extract_station_id(variable)
        return f"Stasiun {station_id}" if station_id and self.is_discharge_variable(variable) else str(variable)

    @staticmethod
    def aggregate(series: pd.Series, frequency: str) -> pd.Series:
        return series.resample(frequency).mean()

    def full_dataframe(self, limit: int = 5000) -> pd.DataFrame:
        if not self.is_csv:
            raise ValueError("Complete table is only available for CSV output.")
        return self._df.iloc[:limit].reset_index()

    def discharge_dataframe(self) -> pd.DataFrame:
        """Read all station discharge rows, never the limited UI preview."""
        # ponytail: full station matrix in RAM; chunk aggregation/exports if matrices outgrow memory.
        variables = [name for name in self.list_variables() if self.is_discharge_variable(name)]
        if self.is_csv:
            frame = pd.read_csv(self.path, sep=self._separator, comment="#", header=self._csv_header,
                                usecols=[self._time_column, *variables])
            frame[self._time_column] = pd.to_datetime(frame[self._time_column], errors="coerce")
            frame = frame.loc[frame[self._time_column].notna()].set_index(self._time_column)
            frame = frame.apply(pd.to_numeric, errors="coerce")
        else:
            columns = {}
            for name in variables:
                data = self._ds[name]
                extra = [dim for dim in data.dims if dim != self._time_dim]
                if len(extra) > 1 or any(dim.lower() in {"lat", "latitude", "lon", "longitude", "x", "y"} for dim in extra):
                    raise ValueError("Gunakan output CSV stasiun atau NetCDF scalar; grid debit tidak memiliki ID river per kolom.")
                if extra:
                    from hydrohub.results.spatial import _normalize_feature_id
                    dim = extra[0]
                    if dim not in data.coords:
                        raise ValueError(f"{name}: koordinat ID stasiun tidak tersedia.")
                    for index, station in enumerate(data[dim].values):
                        label = _normalize_feature_id(station)
                        if label is None or not label.isdigit():
                            raise ValueError(f"{name}: ID river tidak valid: {station!r}")
                        columns[f"{name}_{label}"] = pd.Series(data.isel({dim: index}).values,
                            index=pd.to_datetime(data[self._time_dim].values))
                else:
                    columns[name] = self.get_timeseries(name)
            frame = pd.DataFrame(columns)
        frame = frame.sort_index(kind="stable")
        return frame.groupby(level=0).mean() if frame.index.has_duplicates else frame

    def latest_station_snapshots(self) -> dict[str, dict]:
        if self.is_csv:
            return self._csv_station_snapshots()
        snapshots = {}
        for variable in self.list_variables():
            station_id = extract_station_id(variable)
            if not station_id or not self.is_discharge_variable(variable):
                continue
            series = self.get_timeseries(variable).dropna()
            if series.empty:
                continue
            snapshots[station_id] = {
                "column": variable,
                "value": float(series.iloc[-1]),
                "time": pd.Timestamp(series.index[-1]),
                "unit": self.get_unit(variable) or "m3/s",
            }
        return snapshots

    def _csv_station_snapshots(self) -> dict[str, dict]:
        variables = [name for name in self.list_variables()
                     if self.is_discharge_variable(name) and extract_station_id(name)]
        latest = {}
        if not variables:
            return {}
        # One bounded pass for all stations, rather than rereading the entire
        # CSV for every outlet. Duplicate latest timestamps retain mean semantics.
        with pd.read_csv(self.path, sep=self._separator, comment="#", header=self._csv_header,
                         usecols=[self._time_column, *variables], chunksize=4096) as chunks:
            for frame in chunks:
                times = pd.to_datetime(frame[self._time_column], errors="coerce")
                for variable in variables:
                    values = pd.to_numeric(frame[variable], errors="coerce")
                    valid = times.notna() & values.notna()
                    if not valid.any():
                        continue
                    timestamp = times[valid].max()
                    selected = values[valid & times.eq(timestamp)]
                    total, count = float(selected.sum()), int(selected.count())
                    previous = latest.get(variable)
                    if previous and previous[0] > timestamp:
                        continue
                    if previous and previous[0] == timestamp:
                        total += previous[1]
                        count += previous[2]
                    latest[variable] = timestamp, total, count
        return {extract_station_id(variable): {
            "column": variable, "value": total / count, "time": timestamp,
            "unit": self.get_unit(variable) or "m3/s",
        } for variable, (timestamp, total, count) in latest.items()}

    def score(self) -> int:
        variables = self.list_variables()
        if not variables:
            return -1
        score = 1000 if any(self.is_discharge_variable(name) for name in variables) else 0
        score += 100 if self.is_csv else 0
        score += 30 if "scalar" in self.path.name.lower() else 0
        score += 20 if self.path.name.lower() == "output.csv" else 0
        return score

    def close(self) -> None:
        if self._ds is not None:
            self._ds.close()
        self._df = None


def rank_output_files(paths: tuple[Path, ...]) -> list[Path]:
    ranked = []
    for path in paths:
        reader = None
        try:
            reader = OutputReader(path)
            score = reader.score()
            if score >= 0:
                ranked.append((score, path))
        except Exception:
            continue
        finally:
            if reader is not None:
                reader.close()
    return [path for _score, path in sorted(ranked, key=lambda item: (-item[0], str(item[1]).lower()))]


def validation_metrics(simulation: pd.Series, observation: pd.Series) -> tuple[pd.DataFrame, dict]:
    aligned = pd.concat(
        [simulation.rename("simulation"), observation.rename("observation")], axis=1, join="inner"
    ).dropna()
    if aligned.empty:
        raise ValueError("Simulation and observation have no overlapping valid timestamps.")
    simulated = aligned["simulation"].to_numpy(dtype=float)
    observed = aligned["observation"].to_numpy(dtype=float)
    residual = observed - simulated
    denominator = float(np.sum((observed - observed.mean()) ** 2))
    r2 = float("nan") if denominator == 0 else 1.0 - float(np.sum(residual**2)) / denominator
    bias = float(np.mean(simulated - observed))
    observed_sum = float(np.sum(observed))
    pbias = float("nan") if observed_sum == 0 else 100.0 * float(np.sum(simulated - observed)) / observed_sum
    return aligned, {"r2": r2, "bias": bias, "pbias": pbias, "count": len(aligned)}


MONTH_NAMES_ID = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
    5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
    9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}

SSFI_CLASSES = [
    ("Sangat Kering", "#8B0000"),
    ("Kering Berat", "#E31A1C"),
    ("Kering Sedang", "#FD8D3C"),
    ("Normal", "#EFF171"),
    ("Basah Sedang", "#89F0A3"),
    ("Basah Berat", "#1FB444"),
    ("Sangat Basah", "#086B1D"),
]
NO_DATA_LABEL = "Tidak Ada Data"
NO_DATA_COLOR = "#BDBDBD"


def calculate_ssi(series: pd.Series) -> pd.Series:
    from scipy.stats import gamma, norm

    series = pd.to_numeric(series, errors="coerce").where(lambda values: np.isfinite(values) & (values >= 0))
    valid = series.dropna()
    if len(valid) < 30:
        return pd.Series(np.nan, index=series.index)
    positive = valid[valid > 0]
    if len(positive) < 2:
        return pd.Series(np.nan, index=series.index)
    try:
        shape, location, scale = gamma.fit(positive, floc=0)
        zero_probability = float((valid == 0).sum()) / len(valid)
        cumulative = gamma.cdf(series, shape, location, scale)
        adjusted = np.clip(
            zero_probability + (1 - zero_probability) * cumulative, 0.000001, 0.999999
        )
        values = norm.ppf(adjusted)
    except Exception:
        values = np.full(series.shape, np.nan)
    return pd.Series(values, index=series.index)


def _ssfi_label(value) -> str:
    if pd.isna(value):
        return NO_DATA_LABEL
    if value <= -2:
        return SSFI_CLASSES[0][0]
    if value <= -1.5:
        return SSFI_CLASSES[1][0]
    if value <= -1:
        return SSFI_CLASSES[2][0]
    if value < 1:
        return SSFI_CLASSES[3][0]
    if value < 1.5:
        return SSFI_CLASSES[4][0]
    if value < 2:
        return SSFI_CLASSES[5][0]
    return SSFI_CLASSES[6][0]


def calculate_multiscalar_ssi_all(df: pd.DataFrame, scales=SSFI_SCALES) -> pd.DataFrame:
    """Aggregate runoff monthly, then fit SSI to each basin's rolling means."""
    scales = tuple(scales)
    if not scales or len(set(scales)) != len(scales) or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in scales):
        raise ValueError("SSI scales must be distinct positive integers.")
    data = df[["nama_das", "bulan_tahun", "runoff"]].copy()
    data["bulan_tahun"] = pd.to_datetime(data["bulan_tahun"], errors="raise")
    data["runoff"] = pd.to_numeric(data["runoff"], errors="raise")
    if data[["nama_das", "bulan_tahun"]].isna().any().any():
        raise ValueError("Basin IDs and dates must not be missing.")
    data = data.sort_values(["nama_das", "bulan_tahun"])
    groups = data.groupby("nama_das", sort=False)
    total = groups.ngroups
    print(f"Memulai perhitungan multiskalar untuk {total} DAS...")
    results = []
    for i, (name, basin) in enumerate(groups, 1):
        monthly = basin.set_index("bulan_tahun")["runoff"].resample("MS").mean()
        monthly = monthly.where(np.isfinite(monthly) & (monthly >= 0))
        result = pd.DataFrame({"nama_das": name, "bulan_tahun": monthly.index})
        for n in scales:
            rolled = monthly.rolling(window=n, min_periods=n).mean()
            result[f"SSI-{n}"] = calculate_ssi(rolled).to_numpy()
        results.append(result)
        if i % 500 == 0 or i == total:
            print(f"Progres: {i}/{total} DAS selesai.")
    return pd.concat(results, ignore_index=True) if results else pd.DataFrame(
        columns=["nama_das", "bulan_tahun", *[f"SSI-{n}" for n in scales]])


def build_ssfi_table(reader: OutputReader, scales=(1,)) -> pd.DataFrame:
    discharge = reader.discharge_dataframe()
    if discharge.empty:
        return pd.DataFrame()
    monthly = discharge.resample("MS").mean()
    ids = [extract_station_id(name) or name for name in monthly.columns]
    if len(set(ids)) != len(ids):
        raise ValueError("Multiple discharge columns have the same basin ID.")
    monthly.columns = ids
    long = monthly.rename_axis(index="bulan_tahun", columns="nama_das").reset_index().melt(
        id_vars="bulan_tahun", var_name="nama_das", value_name="runoff")
    result = calculate_multiscalar_ssi_all(long, scales)
    tables = {str(n): result.pivot(index="bulan_tahun", columns="nama_das", values=f"SSI-{n}").sort_index() for n in scales}
    frame = next(iter(tables.values())) if len(tables) == 1 else pd.concat(tables, axis=1)
    frame.index.name = None
    frame.columns.names = [None] * frame.columns.nlevels
    return frame


def available_periods(frame: pd.DataFrame) -> list[tuple[int, int]]:
    if frame is None or frame.empty:
        return []
    return sorted({(int(timestamp.year), int(timestamp.month)) for timestamp in frame.index})


def classify_period(frame: pd.DataFrame, year: int, month: int) -> dict[str, dict]:
    color_map = dict(SSFI_CLASSES)
    color_map[NO_DATA_LABEL] = NO_DATA_COLOR
    matches = [timestamp for timestamp in frame.index if timestamp.year == year and timestamp.month == month]
    if not matches:
        return {}
    result = {}
    for station, value in frame.loc[matches[0]].items():
        label = _ssfi_label(value)
        result[str(station)] = {
            "value": float(value) if pd.notna(value) else None,
            "label": label,
            "color": color_map[label],
        }
    return result
