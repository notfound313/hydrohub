from __future__ import annotations

from pathlib import Path
import csv
import tempfile
import zipfile

import pandas as pd

from hydrohub.results.analysis import (
    MONTH_NAMES_ID,
    NO_DATA_COLOR,
    NO_DATA_LABEL,
    SSFI_CLASSES,
    OutputReader,
    extract_station_id,
)
from hydrohub.results.spatial import _candidate_feature_ids, _normalize_feature_id


def make_basin_id(project_name: str, river_id) -> str:
    station = _normalize_feature_id(river_id)
    if not project_name.strip() or station is None or not station.isdigit():
        raise ValueError(f"Nama proyek atau ID river tidak valid: {project_name!r}, {river_id!r}")
    return f"{project_name.strip()}_{int(station)}"


def _basin_ids(frame, project_name: str, river_ids=None) -> list:
    known = {str(int(value)) for value in river_ids} if river_ids is not None else None
    ids = []
    for row in frame.drop(columns=frame.geometry.name).to_dict("records"):
        properties = {key: value for key, value in row.items() if any(
            token in key.lower() for token in ("station", "gauge", "outlet", "id", "fid", "index", "value"))}
        candidates = []
        existing = row.get("basin_id")
        prefix = project_name.strip() + "_"
        if isinstance(existing, str) and existing.startswith(prefix):
            candidates.append(_normalize_feature_id(existing[len(prefix):]))
        if "value" in properties:
            candidates.append(_normalize_feature_id(properties["value"]))
        candidates.extend(_candidate_feature_ids(properties))
        station = next((value for value in candidates if value is not None and value.isdigit()
                        and (known is None or str(int(value)) in known)), None)
        ids.append(make_basin_id(project_name, station) if station is not None else None)
    return ids


def _basin_ssfi_frame(basin_path: Path, values: dict[str, dict], period: tuple[int, int],
                      scale: int = 1, project_name: str = "hydrohub"):
    import geopandas as gpd

    frame = gpd.read_file(basin_path)
    matched_values = []
    matched_classes = []
    matched_colors = []
    for row in frame.drop(columns=frame.geometry.name).to_dict("records"):
        matched = next(
            (candidate for candidate in _candidate_feature_ids(row) if candidate in values),
            None,
        )
        info = values.get(matched, {}) if matched is not None else {}
        matched_values.append(info.get("value"))
        matched_classes.append(info.get("label") or NO_DATA_LABEL)
        matched_colors.append(info.get("color") or NO_DATA_COLOR)
    frame["ssfi"] = matched_values
    frame["ssfi_class"] = matched_classes
    frame["ssfi_color"] = matched_colors
    frame["ssfi_period"] = f"{period[0]:04d}-{period[1]:02d}"
    frame["ssfi_scale"] = scale
    frame["basin_id"] = _basin_ids(frame, project_name)
    return frame


def export_basin_ssfi_gpkg(
    basin_path: Path,
    destination: Path,
    values: dict[str, dict],
    period: tuple[int, int],
    scale: int = 1,
    project_name: str = "hydrohub",
) -> None:
    frame = _basin_ssfi_frame(basin_path, values, period, scale, project_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    frame.to_file(destination, layer="basin_ssfi", driver="GPKG")


def export_basin_ssfi_png(
    basin_path: Path,
    destination: Path,
    values: dict[str, dict],
    period: tuple[int, int],
    project_name: str,
    scale: int = 1,
) -> None:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import matplotlib.patches as mpatches
    from matplotlib.patches import Patch

    frame = _basin_ssfi_frame(basin_path, values, period, scale, project_name)
    figure = Figure(figsize=(10, 8), layout="constrained")
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    frame.plot(ax=axis, color=frame["ssfi_color"], edgecolor="#333333", linewidth=0.6)
    axis.set_title(f"{project_name} - SSFI-{scale} {MONTH_NAMES_ID[period[1]]} {period[0]}")
    axis.set_axis_off()
    axis.set_aspect("equal")
    entries = [*SSFI_CLASSES, (NO_DATA_LABEL, NO_DATA_COLOR)]
    axis.legend(
        handles=[Patch(facecolor=color, edgecolor="#333333", label=label) for label, color in entries],
        loc="lower left",
        frameon=True,
        prop={'size': 6},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, dpi=200, format="png", facecolor="white")


def export_ssfi_excel(frame: pd.DataFrame, destination: Path, scale: int | None = None,
                      project_name: str = "") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = frame.copy()
    output.index = pd.to_datetime(output.index).strftime("%Y-%m")
    output.index.name = "period"
    if project_name:
        output.columns = [make_basin_id(project_name, station) for station in output.columns]
    output.to_excel(destination, sheet_name=f"SSFI-{scale}" if scale is not None else "SSFI")


def export_validation_template(
    dates: pd.DatetimeIndex, stations: list[str], destination: Path
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(destination) as writer:
        frame = pd.DataFrame({"tanggal": pd.to_datetime(dates)})
        for station in stations:
            frame[station] = None
        frame.to_excel(writer, sheet_name="Observasi", index=False)
        pd.DataFrame({"Petunjuk": ["Isi tanggal dan nilai observasi, lalu simpan sebagai .xlsx."]}).to_excel(
            writer, sheet_name="Petunjuk", index=False
        )


def export_validation_excel(
    aligned: pd.DataFrame, metrics: dict, destination: Path, frequency: str,
    station: str = "",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = aligned.copy()
    output.insert(0, "tanggal", pd.to_datetime(output.index))
    output["residual"] = output["simulation"] - output["observation"]
    with pd.ExcelWriter(destination) as writer:
        output.to_excel(writer, sheet_name="Validation", index=False)
        pd.DataFrame([{"stasiun": station, "frekuensi": frequency, **metrics}]).to_excel(
            writer, sheet_name="Metrics", index=False
        )


def export_basin_shapefile(basin_path: Path, destination: Path, project_name: str = "hydrohub",
                          river_ids=None) -> int:
    import geopandas as gpd

    if destination.suffix.lower() != ".zip":
        raise ValueError("Ekspor Shapefile memerlukan format ZIP (.zip).")
    if basin_path.resolve() == destination.resolve():
        raise ValueError("Ekspor tidak boleh menimpa layer sumber.")
    frame = gpd.read_file(basin_path)
    frame["basin_id"] = _basin_ids(frame, project_name, river_ids)
    if not frame.empty and frame["basin_id"].isna().all():
        raise ValueError("Layer tidak memiliki ID basin yang cocok dengan river output.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        root = Path(temporary)
        shapefile = root / (destination.stem + ".shp")
        frame.to_file(shapefile, driver="ESRI Shapefile", encoding="UTF-8")
        archive_path = root / "package.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                component = shapefile.with_suffix(suffix)
                if component.exists():
                    archive.write(component, arcname=component.name)
        archive_path.replace(destination)
    return int(frame["basin_id"].isna().sum())


def export_discharge(source: Path, destination: Path, project_name: str) -> None:
    """Export full discharge with basin IDs above the original river headers."""
    if source.resolve() == destination.resolve():
        raise ValueError("Ekspor tidak boleh menimpa file output sumber.")
    if destination.suffix.lower() not in {".csv", ".xlsx"}:
        raise ValueError("Ekspor debit memerlukan format CSV atau Excel (.xlsx).")
    reader = OutputReader(source)
    try:
        frame = reader.discharge_dataframe()
    finally:
        reader.close()
    if frame.empty:
        raise ValueError("Output tidak memiliki data debit untuk diekspor.")
    ids = [make_basin_id(project_name, extract_station_id(name)) for name in frame.columns]
    if len(set(ids)) != len(ids):
        raise ValueError("Beberapa kolom river memiliki basin_id yang sama.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".csv":
        with destination.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(["basin_id", *ids])
            frame.to_csv(stream, index_label="time")
    else:
        from openpyxl import Workbook
        if frame.shape[1] > 16383 or len(frame) > 1048574:
            raise ValueError("Data melebihi batas baris/kolom Excel; gunakan CSV.")
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Debit")
        sheet.append(["basin_id", *ids])
        sheet.append(["time", *map(str, frame.columns)])
        for row in frame.itertuples(index=True, name=None):
            sheet.append([row[0].to_pydatetime(), *[None if pd.isna(value) else value for value in row[1:]]])
        workbook.save(destination)
