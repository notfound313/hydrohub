from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelArtifacts:
    root: Path
    config: Path
    staticmaps: Path | None
    staticgeoms: tuple[Path, ...]
    forcing: Path | None
    outputs: tuple[Path, ...]


def discover_wflow_models(project_root: Path) -> list[Path]:
    models_root = project_root / "models"
    if not models_root.is_dir():
        return []
    return [
        config.parent.resolve()
        for config in sorted(
            models_root.glob("**/wflow_sbm.toml"), key=lambda path: str(path).lower()
        )
    ]


def _configured_path(model: Path, config: dict, dotted: tuple[str, ...]) -> Path | None:
    value = config
    for key in dotted:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return (path if path.is_absolute() else model / path).resolve()


def discover_model_artifacts(project_root: Path, model: Path) -> ModelArtifacts:
    model = model.resolve()
    try:
        model.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError("Model must remain inside the active HydroHub project.") from exc
    config_path = model / "wflow_sbm.toml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Wflow configuration not found: {config_path}")
    with config_path.open("rb") as stream:
        config = tomllib.load(stream)

    staticmaps = model / "staticmaps.nc"
    if not staticmaps.is_file():
        staticmaps = next(iter(sorted(model.glob("**/staticmaps.nc"))), None)

    staticgeoms = []
    for folder_name in ("staticgeoms", "staticgeom"):
        folder = model / folder_name
        if folder.is_dir():
            staticgeoms.extend(
                path
                for suffix in ("*.geojson", "*.gpkg", "*.shp")
                for path in folder.glob(suffix)
            )

    forcing = _configured_path(model, config, ("input", "path_forcing"))
    if forcing is None or not forcing.is_file():
        forcing = model / "forcing.nc"
    if not forcing.is_file():
        forcing = next(iter(sorted(model.glob("**/*forcing*.nc"))), None)

    configured_outputs = [
        _configured_path(model, config, keys)
        for keys in (
            ("output", "netcdf_grid", "path"),
            ("output", "netcdf_scalar", "path"),
            ("output", "csv", "path"),
        )
    ]
    discovered = [
        path
        for pattern in ("**/output*.nc", "**/output*.csv")
        for path in model.glob(pattern)
    ]
    simulation_root = project_root / "outputs" / "simulation"
    if simulation_root.is_dir():
        discovered.extend(
            path
            for pattern in ("**/output*.nc", "**/output*.csv")
            for path in simulation_root.glob(pattern)
        )
    unique_outputs = {
        path.resolve()
        for path in [*configured_outputs, *discovered]
        if path is not None and path.is_file()
    }
    return ModelArtifacts(
        root=model,
        config=config_path,
        staticmaps=staticmaps.resolve() if staticmaps else None,
        staticgeoms=tuple(sorted({path.resolve() for path in staticgeoms})),
        forcing=forcing.resolve() if forcing else None,
        outputs=tuple(sorted(unique_outputs, key=lambda path: str(path).lower())),
    )
