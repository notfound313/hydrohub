from __future__ import annotations

import copy
import json
import glob
import itertools
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from hydrohub.core.project_manager import ProjectContext


SETUP_STEPS = (
    "project",
    "aoi",
    "environment",
    "global_data",
    "wflow_catalog",
    "data_entries",
    "wflow_yml",
    "validation",
    "ready",
)


EXTERNAL_CATALOG_KEYS = {"esa_worldcover_mapping_default"}
URI_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")
CONFIG_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class CatalogUriResolution:
    patterns: list[str]
    placeholders: dict[str, list[str]]
    matches: list[Path]
    issues: list[str]

    @property
    def valid(self) -> bool:
        return not self.issues


def dump_catalog_yaml(catalog: dict) -> str:
    blocks = [
        yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=False).rstrip()
        for key, value in catalog.items()
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def dump_recipe_yaml(recipe: dict) -> str:
    lines = yaml.safe_dump(recipe, sort_keys=False, allow_unicode=False).splitlines()
    spaced: list[str] = []
    has_step = False
    for line in lines:
        if line.startswith("- "):
            if has_step and spaced and spaced[-1] != "":
                spaced.append("")
            has_step = True
        spaced.append(line)
    return "\n".join(spaced) + ("\n" if spaced else "")


class WorkflowManager:
    def load_setup(self, project: ProjectContext) -> dict:
        path = project.root / "workspace" / "setup_state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def update_step(self, project: ProjectContext, step: str, status: str) -> dict:
        if step not in SETUP_STEPS:
            raise ValueError(f"Unknown setup step: {step}")
        if status not in {"NOT_STARTED", "IN_PROGRESS", "VALID", "WARNING", "BLOCKED"}:
            raise ValueError(f"Unknown setup status: {status}")
        state = self.load_setup(project)
        state["steps"][step] = status
        state["current_step"] = SETUP_STEPS.index(step) + 1
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_json(project.root / "workspace" / "setup_state.json", state)
        return state

    def load_ui_state(self, project: ProjectContext) -> dict:
        path = project.root / "workspace" / "ui_state.json"
        if not path.exists():
            return {}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def save_ui_state(self, project: ProjectContext, **changes: str) -> dict:
        state = self.load_ui_state(project)
        state.update(changes)
        self._atomic_json(project.root / "workspace" / "ui_state.json", state)
        return state

    @staticmethod
    def _yaml_filename(filename: str, default: str) -> str:
        name = filename.strip() or default
        if Path(name).name != name or Path(name).suffix.lower() not in {".yml", ".yaml"}:
            raise ValueError("File name must be a .yml or .yaml name without folders.")
        return name

    def catalog_path(self, project: ProjectContext, filename: str = "wflow_catalog.yml") -> Path:
        return project.root / "data" / "catalog" / self._yaml_filename(filename, "wflow_catalog.yml")

    def prepare_hydromt_catalog(
        self, project: ProjectContext, filename: str = "wflow_catalog.yml"
    ) -> Path:
        source = self.catalog_path(project, filename)
        catalog = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(catalog, dict):
            raise ValueError("Catalog root must be a YAML mapping.")

        runtime_catalog = dict(catalog)
        meta = dict(runtime_catalog.get("meta") or {})
        meta.pop("root", None)
        meta.pop("roots", None)
        runtime_catalog["meta"] = meta

        for key, value in catalog.items():
            if key == "meta" or not isinstance(value, dict):
                continue
            entry = dict(value)
            uri, issues = self._replace_configuration_variables(
                project, catalog, str(entry.get("uri", "")).strip()
            )
            if issues:
                raise ValueError("; ".join(issues))
            uri = uri.replace("{variabel}", "{variable}")
            parsed = urlparse(uri)
            if parsed.scheme not in {"http", "https", "s3", "gs"} and uri:
                uri = self._catalog_data_path(project, uri).resolve().as_posix()
            entry["uri"] = uri
            placeholders = entry.get("placeholders")
            if isinstance(placeholders, dict) and "variabel" in placeholders:
                entry["placeholders"] = {
                    name: options
                    for name, options in placeholders.items()
                    if name != "variabel"
                }
                if not entry["placeholders"]:
                    entry.pop("placeholders")
            runtime_catalog[key] = entry

        target = project.root / "workspace" / "runtime_catalogs" / source.name
        self._atomic_text(target, dump_catalog_yaml(runtime_catalog))
        return target

    def recipe_path(self, project: ProjectContext, filename: str = "wflow.yml") -> Path:
        return project.root / "configs" / "hydromt" / self._yaml_filename(filename, "wflow.yml")

    def list_catalogs(self, project: ProjectContext) -> list[str]:
        root = project.root / "data" / "catalog"
        root.mkdir(parents=True, exist_ok=True)
        return sorted(
            (path.name for path in root.iterdir() if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}),
            key=lambda name: (name != "wflow_catalog.yml", name.lower()),
        )

    def list_wflow_recipes(self, project: ProjectContext) -> list[str]:
        root = project.root / "configs" / "hydromt"
        root.mkdir(parents=True, exist_ok=True)
        return sorted(
            (path.name for path in root.iterdir() if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}),
            key=lambda name: (name != "wflow.yml", name.lower()),
        )

    def save_aoi_bbox(self, project: ProjectContext, west: float, south: float, east: float, north: float) -> dict:
        if west >= east or south >= north:
            raise ValueError("AOI bounds are invalid.")
        geometry = {
            "type": "Feature",
            "properties": {"name": f"{project.name} AOI"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
            },
        }
        path = project.root / "workspace" / "aoi.geojson"
        self._atomic_json(path, geometry)
        self.update_step(project, "aoi", "VALID")
        return geometry

    @staticmethod
    def normalize_aoi_geojson(data: dict, name: str = "AOI") -> dict:
        if not isinstance(data, dict):
            raise ValueError("AOI GeoJSON must be an object.")
        kind = data.get("type")
        if kind in {"Polygon", "MultiPolygon"}:
            feature = {"type": "Feature", "properties": {"name": name}, "geometry": data}
        elif kind == "Feature":
            feature = data
        elif kind == "FeatureCollection":
            features = data.get("features")
            if not isinstance(features, list) or not features:
                raise ValueError("AOI FeatureCollection is empty.")
            feature = data
        else:
            raise ValueError("AOI GeoJSON must contain Polygon or MultiPolygon geometry.")
        geometries = (
            [item.get("geometry", {}) for item in feature.get("features", [])]
            if feature.get("type") == "FeatureCollection"
            else [feature.get("geometry", {})]
        )
        if not geometries or any(geometry.get("type") not in {"Polygon", "MultiPolygon"} for geometry in geometries):
            raise ValueError("Every AOI feature must use Polygon or MultiPolygon geometry.")
        WorkflowManager.aoi_bounds(feature)
        return feature

    @staticmethod
    def load_aoi_file(path: Path, name: str = "AOI") -> dict:
        if path.suffix.lower() in {".json", ".geojson"}:
            return WorkflowManager.normalize_aoi_geojson(
                json.loads(path.read_text(encoding="utf-8")), name
            )
        if path.suffix.lower() != ".zip":
            raise ValueError("AOI file must be GeoJSON or a Shapefile ZIP.")
        try:
            import geopandas as gpd
        except ImportError as exc:
            raise ValueError("GeoPandas is required to import a Shapefile ZIP.") from exc
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                shapefiles = [member for member in names if member.lower().endswith(".shp")]
                if len(shapefiles) != 1:
                    raise ValueError("AOI ZIP must contain exactly one Shapefile (.shp).")
                stem = shapefiles[0][:-4].lower()
                parts = {member[-4:].lower(): member for member in names if member[:-4].lower() == stem}
                if not {".shp", ".shx", ".dbf", ".prj"} <= parts.keys():
                    raise ValueError("AOI ZIP needs matching .shp, .shx, .dbf, and .prj files.")
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / f"{Path(shapefiles[0]).stem}.shp"
                    for suffix, member in parts.items():
                        if suffix not in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
                            continue
                        with archive.open(member) as input_file, source.with_suffix(suffix).open("wb") as output_file:
                            shutil.copyfileobj(input_file, output_file)
                    frame = gpd.read_file(source)
        except zipfile.BadZipFile as exc:
            raise ValueError("AOI ZIP is invalid.") from exc
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"AOI Shapefile could not be read: {exc}") from exc
        if (frame.empty or frame.geometry.isna().any() or frame.geometry.is_empty.any()
                or not frame.geom_type.isin(["Polygon", "MultiPolygon"]).all()):
            raise ValueError("AOI Shapefile must contain Polygon or MultiPolygon geometry.")
        if frame.crs is None:
            raise ValueError("AOI Shapefile has no coordinate reference system (CRS).")
        try:
            data = json.loads(frame.to_crs("EPSG:4326").to_json())
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"AOI Shapefile could not be converted to EPSG:4326: {exc}") from exc
        return WorkflowManager.normalize_aoi_geojson(data, name)

    @staticmethod
    def aoi_bounds(data: dict) -> tuple[float, float, float, float]:
        coordinates: list[tuple[float, float]] = []

        def collect(value) -> None:
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
                    coordinates.append((float(value[0]), float(value[1])))
                else:
                    for item in value:
                        collect(item)

        if data.get("type") == "FeatureCollection":
            for feature in data.get("features", []):
                collect(feature.get("geometry", {}).get("coordinates", []))
        else:
            geometry = data.get("geometry", data)
            collect(geometry.get("coordinates", []))
        if not coordinates:
            raise ValueError("AOI GeoJSON has no valid coordinates.")
        xs, ys = zip(*coordinates)
        west, south, east, north = min(xs), min(ys), max(xs), max(ys)
        if west >= east or south >= north:
            raise ValueError("AOI bounds are invalid.")
        return west, south, east, north

    @staticmethod
    def load_aoi_bounds(project: ProjectContext) -> tuple[float, float, float, float] | None:
        path = project.root / "workspace" / "aoi.geojson"
        if not path.is_file():
            return None
        try:
            return WorkflowManager.aoi_bounds(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save_aoi_geojson(self, project: ProjectContext, data: dict) -> dict:
        feature = self.normalize_aoi_geojson(data, f"{project.name} AOI")
        self._atomic_json(project.root / "workspace" / "aoi.geojson", feature)
        self.update_step(project, "aoi", "VALID")
        return feature

    def create_catalog(self, project: ProjectContext, filename: str = "wflow_catalog.yml") -> Path:
        path = self.catalog_path(project, filename)
        if not path.exists():
            catalog = {"meta": {"version": "v1.0.0", "name": path.stem}}
            self._atomic_text(path, dump_catalog_yaml(catalog))
        self.update_step(project, "wflow_catalog", "IN_PROGRESS")
        return path

    def create_wflow_recipe(self, project: ProjectContext, filename: str = "wflow.yml") -> Path:
        path = self.recipe_path(project, filename)
        if not path.exists():
            self._atomic_text(path, dump_recipe_yaml({"steps": []}))
        return path

    def add_catalog_entry(
        self,
        project: ProjectContext,
        key: str,
        data_type: str,
        driver: str,
        uri: str,
        crs: str = "",
        category: str = "",
        filename: str = "wflow_catalog.yml",
        placeholders: dict | None = None,
        *,
        driver_options: dict | None = None,
        metadata: dict | None = None,
    ) -> Path:
        if not key.strip() or not driver.strip() or not uri.strip():
            raise ValueError("Catalog key, driver, and URI are required.")
        path = self.create_catalog(project, filename)
        catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        driver_config = {"name": driver}
        if driver_options:
            driver_config["options"] = copy.deepcopy(driver_options)
        entry = {"data_type": data_type, "uri": uri, "driver": driver_config}
        entry_metadata = copy.deepcopy(metadata or {})
        entry_metadata.update(
            {key: value for key, value in {"crs": crs, "category": category}.items() if value}
        )
        if entry_metadata:
            entry["metadata"] = entry_metadata
        if placeholders:
            entry["placeholders"] = placeholders
        catalog[key.strip()] = entry
        self._atomic_text(path, dump_catalog_yaml(catalog))
        valid, _issues = self.validate_catalog(project, filename)
        self.update_step(project, "data_entries", "VALID" if valid else "WARNING")
        return path

    def delete_catalog_entry(
        self, project: ProjectContext, key: str, filename: str = "wflow_catalog.yml"
    ) -> Path:
        if not key or key == "meta":
            raise ValueError("Select a catalog key to delete.")
        path = self.catalog_path(project, filename)
        catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if key not in catalog:
            raise ValueError(f"Catalog key does not exist: {key}")
        del catalog[key]
        self._atomic_text(path, dump_catalog_yaml(catalog))
        valid, _issues = self.validate_catalog(project, filename)
        self.update_step(project, "data_entries", "VALID" if valid else "WARNING")
        return path

    def save_catalog_yaml(
        self, project: ProjectContext, content: str, filename: str = "wflow_catalog.yml"
    ) -> Path:
        try:
            catalog = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"Catalog YAML is invalid: {exc}") from exc
        if not isinstance(catalog, dict):
            raise ValueError("Catalog root must be a YAML mapping.")
        path = self.catalog_path(project, filename)
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        self._atomic_text(path, content.rstrip() + "\n")
        valid, issues = self.validate_catalog(project, filename)
        if not valid:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic_text(path, previous)
                self.validate_catalog(project, filename)
            raise ValueError("Catalog was not saved: " + "; ".join(issues))
        self.update_step(project, "data_entries", "VALID")
        return path

    def validate_catalog(
        self, project: ProjectContext, filename: str = "wflow_catalog.yml"
    ) -> tuple[bool, list[str]]:
        path = self.catalog_path(project, filename)
        issues: list[str] = []
        if not path.exists():
            return False, ["Wflow catalog has not been created."]
        try:
            catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            return False, [f"Catalog YAML is invalid: {exc}"]
        entries = {key: value for key, value in catalog.items() if key != "meta"}
        if not entries:
            issues.append("Add at least one dataset entry.")
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                issues.append(f"Entry '{key}' must be a mapping.")
                continue
            for required in ("data_type", "uri", "driver"):
                if not entry.get(required):
                    issues.append(f"Entry '{key}' is missing {required}.")
            uri = str(entry.get("uri", ""))
            if uri:
                resolution = self.resolve_catalog_uri(project, catalog, key)
                issues.extend(f"Entry '{key}': {issue}" for issue in resolution.issues)
        valid = not issues
        self.update_step(project, "wflow_catalog", "VALID" if valid else "WARNING")
        return valid, issues

    def save_wflow_recipe(
        self,
        project: ProjectContext,
        steps: list[str],
        source_key: str,
        catalog_file: str = "wflow_catalog.yml",
        recipe_file: str = "wflow.yml",
    ) -> Path:
        catalog_valid, issues = self.validate_catalog(project, catalog_file)
        if not catalog_valid:
            raise ValueError("Catalog must be valid before wflow.yml is created: " + "; ".join(issues))
        recipe_steps = []
        for step in steps:
            config = {}
            if step == "setup_basemaps" and source_key:
                config["hydrography_fn"] = source_key
            recipe_steps.append({step: config})
        path = self.recipe_path(project, recipe_file)
        self._atomic_text(path, dump_recipe_yaml({"steps": recipe_steps}))
        self.update_step(project, "wflow_yml", "VALID")
        return path

    def save_wflow_recipe_yaml(
        self,
        project: ProjectContext,
        content: str,
        catalog_file: str = "wflow_catalog.yml",
        recipe_file: str = "wflow.yml",
    ) -> Path:
        catalog_valid, issues = self.validate_catalog(project, catalog_file)
        if not catalog_valid:
            raise ValueError("Catalog must be valid before wflow.yml is saved: " + "; ".join(issues))
        try:
            recipe = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"wflow.yml is invalid: {exc}") from exc
        if not isinstance(recipe, dict) or not isinstance(recipe.get("steps"), list):
            raise ValueError("wflow.yml must contain a 'steps' list.")
        catalog_path = self.catalog_path(project, catalog_file)
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        known_keys = set(catalog) - {"meta"}
        unknown = []
        for item in recipe["steps"]:
            if not isinstance(item, dict):
                raise ValueError("Every recipe step must be a YAML mapping.")
            for config in item.values():
                if not isinstance(config, dict):
                    continue
                for name, value in config.items():
                    if (
                        name.endswith("_fn")
                        and value
                        and isinstance(value, str)
                        and value not in known_keys
                        and value not in EXTERNAL_CATALOG_KEYS
                    ):
                        unknown.append(value)
                    if isinstance(value, str) and value.startswith("PLACEHOLDER_"):
                        unknown.append(value)
        if unknown:
            raise ValueError("Recipe references unknown catalog keys: " + ", ".join(sorted(set(unknown))))
        path = self.recipe_path(project, recipe_file)
        self._atomic_text(path, content.rstrip() + "\n")
        self.update_step(project, "wflow_yml", "VALID")
        return path

    def save_runtime(
        self,
        project: ProjectContext,
        start: str,
        end: str,
        timestep_seconds: int,
        netcdf_name: str,
        csv_name: str,
    ) -> Path:
        active_recipe = self.load_ui_state(project).get("active_wflow_file", "wflow.yml")
        if not self.recipe_path(project, active_recipe).exists():
            raise ValueError("Create wflow.yml before the runtime configuration.")
        model_root = project.root / "models" / "wflow"
        if not any(path.is_file() for path in model_root.rglob("*")):
            raise ValueError("Build and validate the HydroMT-Wflow model before creating runtime configuration.")
        content = (
            f'starttime = "{start}"\nendtime = "{end}"\n\n'
            f'[time]\ntimestepsecs = {timestep_seconds}\n\n'
            f'[output]\nnetcdf = "{netcdf_name}"\ncsv = "{csv_name}"\n'
        )
        path = project.root / "configs" / "wflow" / "wflow_sbm.toml"
        self._atomic_text(path, content)
        return path

    @staticmethod
    def _value_pairs(value) -> list[tuple[str, object]]:
        if isinstance(value, dict):
            return [(str(key), item) for key, item in value.items()]
        if isinstance(value, (list, tuple, set)):
            pairs: list[tuple[str, object]] = []
            for item in value:
                if isinstance(item, dict):
                    pairs.extend((str(key), nested) for key, nested in item.items())
                else:
                    pairs.append((str(item), item))
            return pairs
        if value is None or value == "":
            return []
        return [(str(value), value)]

    @classmethod
    def catalog_values(cls, catalog: dict, entry_key: str, name: str) -> list[tuple[str, object]]:
        entry = catalog.get(entry_key, {})
        if not isinstance(entry, dict):
            return []
        metadata = entry.get("metadata") or {}
        meta = catalog.get("meta") or {}
        aliases = {name, f"{name}s"}
        if name in {"variable", "variabel"}:
            aliases.update({"variable", "variables", "variabel"})
        sources = [
            meta.get("variables"),
            meta.get("placeholders"),
            entry.get("variables"),
            entry.get("placeholders"),
            metadata.get("variables") if isinstance(metadata, dict) else None,
            metadata.get("placeholders") if isinstance(metadata, dict) else None,
        ]
        for source in reversed(sources):
            if not isinstance(source, dict):
                continue
            for alias in aliases:
                if alias in source:
                    return cls._value_pairs(source[alias])
        return []

    @staticmethod
    def _replace_configuration_variables(
        project: ProjectContext, catalog: dict, uri: str
    ) -> tuple[str, list[str]]:
        meta = catalog.get("meta") or {}
        configured = meta.get("configuration") if isinstance(meta, dict) else {}
        configured = configured if isinstance(configured, dict) else {}
        ui_state_path = project.root / "workspace" / "ui_state.json"
        try:
            ui_state = json.loads(ui_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ui_state = {}
        values = {
            str(key): str(value)
            for key, value in configured.items()
            if value is not None and value != ""
        }
        if ui_state.get("global_database_root"):
            values.setdefault("GLOBAL_DB", str(ui_state["global_database_root"]))
        issues: list[str] = []

        def replace(match: re.Match) -> str:
            name = match.group(1)
            value = values.get(name) or os.environ.get(name)
            if value:
                return value
            issues.append(f"Configuration variable '${{{name}}}' has no value.")
            return match.group(0)

        return CONFIG_VARIABLE_RE.sub(replace, uri), issues

    @staticmethod
    def _catalog_data_path(project: ProjectContext, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        if path.parts and path.parts[0].lower() == "data":
            return project.root / path
        return project.root / "data" / path

    @staticmethod
    def _glob_path(project: ProjectContext, pattern: str) -> tuple[str, list[Path]]:
        absolute_pattern = str(WorkflowManager._catalog_data_path(project, pattern))
        matches = [Path(item) for item in glob.glob(absolute_pattern, recursive=True)]
        return absolute_pattern, [item for item in matches if item.is_file()]

    @staticmethod
    def _discovered_value(uri: str, placeholder: str, matched_path: Path, project: ProjectContext) -> str:
        candidate = str(matched_path)
        source = str(WorkflowManager._catalog_data_path(project, uri))
        token = "{" + placeholder + "}"
        before, _separator, after = source.partition(token)
        suffix = after.lstrip("*")
        value = candidate[len(before):] if candidate.startswith(before) else matched_path.stem
        if suffix and value.endswith(suffix):
            value = value[: -len(suffix)]
        return value.strip("._-") or matched_path.stem

    def resolve_catalog_uri(
        self, project: ProjectContext, catalog: dict, entry_key: str
    ) -> CatalogUriResolution:
        entry = catalog.get(entry_key, {})
        if not isinstance(entry, dict):
            return CatalogUriResolution([], {}, [], ["Catalog entry must be a mapping."])
        raw_uri = str(entry.get("uri", "")).strip()
        if not raw_uri:
            return CatalogUriResolution([], {}, [], ["URI has no value."])
        uri, issues = self._replace_configuration_variables(project, catalog, raw_uri)
        names = list(dict.fromkeys(URI_PLACEHOLDER_RE.findall(uri)))
        values: dict[str, list[str]] = {}

        discovery_pattern = URI_PLACEHOLDER_RE.sub("*", uri)
        parsed = urlparse(uri)
        discovered_files: list[Path] = []
        if parsed.scheme not in {"http", "https", "s3", "gs"} and "${" not in discovery_pattern:
            _absolute, discovered_files = self._glob_path(project, discovery_pattern)
        for name in names:
            configured = [
                label if isinstance(value, (dict, list, tuple, set)) else str(value)
                for label, value in self.catalog_values(catalog, entry_key, name)
            ]
            if configured:
                values[name] = list(dict.fromkeys(configured))
                continue
            discovered = [
                self._discovered_value(uri, name, path, project) for path in discovered_files
            ]
            discovered = list(dict.fromkeys(value for value in discovered if value))
            if discovered:
                values[name] = discovered
            else:
                issues.append(f"Placeholder '{{{name}}}' is not recognized or has no value.")

        patterns: list[str] = []
        if names and all(values.get(name) for name in names):
            for combination in itertools.product(*(values[name] for name in names)):
                pattern = uri
                for name, value in zip(names, combination):
                    pattern = pattern.replace("{" + name + "}", value)
                patterns.append(pattern)
        elif not names:
            patterns.append(uri)

        matches: list[Path] = []
        if parsed.scheme in {"http", "https", "s3", "gs"}:
            return CatalogUriResolution(patterns, values, matches, issues)
        for pattern in patterns:
            if "${" in pattern:
                continue
            _absolute, found = self._glob_path(project, pattern)
            matches.extend(found)
            if not found:
                issues.append(f"URI does not resolve; pattern matched no files: {pattern}")
        matches = list(dict.fromkeys(matches))
        return CatalogUriResolution(patterns, values, matches, issues)

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        WorkflowManager._atomic_text(path, json.dumps(value, indent=2))

    @staticmethod
    def _atomic_yaml(path: Path, value: dict) -> None:
        WorkflowManager._atomic_text(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=False))

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
