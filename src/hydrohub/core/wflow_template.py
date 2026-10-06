from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


MASTER_TEMPLATE_NAME = "setup_wflow_master_hydromt.yml"

PRESET_STEPS = {
    "Minimal": (
        "setup_config",
        "setup_basemaps",
        "setup_rivers",
        "setup_outlets",
    ),
    "Standard": (
        "setup_config",
        "setup_basemaps",
        "setup_rivers",
        "setup_river_roughness",
        "setup_lulcmaps",
        "setup_laimaps",
        "setup_soilmaps",
        "setup_constant_pars",
        "setup_outlets",
    ),
    "Full": (),
    "Custom": (),
}

PLACEHOLDER_DEFAULTS = {
    "PLACEHOLDER_HYDROGRAPHY": "merit_jawa",
    "PLACEHOLDER_RIVER_GEOMETRY": "river_geom_jawa_qbf",
    "PLACEHOLDER_LULC": "esa_worldcover_jawa",
    "PLACEHOLDER_LULC_MAPPING": "esa_worldcover_mapping_default",
    "PLACEHOLDER_LAI": "modis_lai_jawa",
    "PLACEHOLDER_SOIL": "soilgrids_2020",
    "PLACEHOLDER_PRECIP": "gpm_imerg_jawa",
    "PLACEHOLDER_PET": "era5land_pet_jawa",
}

PLACEHOLDER_LABELS = {
    "PLACEHOLDER_HYDROGRAPHY": "Hydrography",
    "PLACEHOLDER_RIVER_GEOMETRY": "River geometry",
    "PLACEHOLDER_LULC": "Land use / land cover",
    "PLACEHOLDER_LULC_MAPPING": "LULC mapping",
    "PLACEHOLDER_LAI": "Leaf area index",
    "PLACEHOLDER_SOIL": "Soil",
    "PLACEHOLDER_PRECIP": "Precipitation",
    "PLACEHOLDER_PET": "Potential evapotranspiration",
}

PLACEHOLDER_CATEGORIES = {
    "PLACEHOLDER_HYDROGRAPHY": {"dem", "hydrography"},
    "PLACEHOLDER_RIVER_GEOMETRY": {"hydrography", "river_geometry"},
    "PLACEHOLDER_LULC": {"landuse", "landcover"},
    "PLACEHOLDER_LULC_MAPPING": {"landuse", "landcover", "reference"},
    "PLACEHOLDER_LAI": {"landuse", "landcover", "vegetation"},
    "PLACEHOLDER_SOIL": {"soil"},
    "PLACEHOLDER_PRECIP": {"forcing", "meteo", "rainfall"},
    "PLACEHOLDER_PET": {"forcing", "meteo", "pet"},
}

SETUP_GROUPS = {
    "Configuration": ("setup_config",),
    "Domain": ("setup_basemaps",),
    "Rivers and floodplains": (
        "setup_rivers",
        "setup_river_roughness",
        "setup_floodplains",
        "setup_riverwidth",
    ),
    "Water bodies": (
        "setup_reservoirs_simple_control",
        "setup_reservoirs_no_control",
        "setup_glaciers",
    ),
    "Land cover and vegetation": (
        "setup_lulcmaps",
        "setup_lulcmaps_from_vector",
        "setup_lulcmaps_with_paddy",
        "setup_laimaps",
        "setup_laimaps_from_lulc_mapping",
        "setup_agroforestry",
    ),
    "Soil and root zone": (
        "setup_soilmaps",
        "setup_ksathorfrac",
        "setup_ksatver_vegetation",
        "setup_rootzoneclim",
        "setup_constant_pars",
    ),
    "Water demand": (
        "setup_allocation_areas",
        "setup_allocation_surfacewaterfrac",
        "setup_domestic_demand",
        "setup_domestic_demand_from_population",
        "setup_other_demand",
        "setup_irrigation",
        "setup_irrigation_from_vector",
    ),
    "Forcing": (
        "setup_precip_forcing",
        "setup_precip_from_point_timeseries",
        "setup_pet_forcing",
        "setup_temp_pet_forcing",
    ),
    "Outputs": (
        "setup_outlets",
        "setup_gauges",
        "setup_areamap",
        "setup_config_output_timeseries",
    ),
    "Advanced": (
        "setup_cold_states",
        "setup_grid_from_raster",
        "setup_grid_from_geodataset",
        "setup_1dmodel_connection",
    ),
}

EXCLUSIVE_SETUP_METHODS = (
    frozenset(
        {
            "setup_lulcmaps",
            "setup_lulcmaps_from_vector",
            "setup_lulcmaps_with_paddy",
        }
    ),
    frozenset({"setup_laimaps", "setup_laimaps_from_lulc_mapping"}),
    frozenset({"setup_precip_forcing", "setup_precip_from_point_timeseries"}),
    frozenset({"setup_pet_forcing", "setup_temp_pet_forcing"}),
)


@dataclass(frozen=True)
class SetupParameter:
    name: str
    value: Any
    required: bool
    selected: bool


@dataclass(frozen=True)
class SetupMethod:
    name: str
    group: str
    parameters: tuple[SetupParameter, ...]
    selected: bool


def master_template_path() -> Path:
    path = Path(__file__).resolve().parents[3] / MASTER_TEMPLATE_NAME
    if not path.is_file():
        raise FileNotFoundError(f"Wflow master template not found: {path}")
    return path


def load_master_template(path: Path | None = None) -> dict:
    source = path or master_template_path()
    template = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(template.get("steps"), list):
        raise ValueError("Wflow master template must contain a steps list.")
    return template


def active_step_names(template: dict) -> list[str]:
    return [next(iter(entry)) for entry in template["steps"]]


def discover_setup_methods(template: dict) -> list[SetupMethod]:
    """Return setup methods supported by the installed HydroMT-Wflow version."""
    try:
        from hydromt_wflow import WflowSbmModel
    except ImportError:
        available = set(active_step_names(template))
        model_class = None
    else:
        available = {
            name
            for name in dir(WflowSbmModel)
            if name.startswith("setup_") and callable(getattr(WflowSbmModel, name))
        }
        model_class = WflowSbmModel

    active_configs = {
        name: copy.deepcopy(config)
        for entry in template["steps"]
        for name, config in entry.items()
    }
    configured_order = [name for names in SETUP_GROUPS.values() for name in names]
    ordered_names = [name for name in configured_order if name in available]
    ordered_names.extend(sorted(available.difference(ordered_names)))
    group_by_method = {
        name: group for group, names in SETUP_GROUPS.items() for name in names
    }

    methods = []
    for name in ordered_names:
        configured = active_configs.get(name, {})
        parameters = []
        if model_class is None:
            signature_parameters = []
        else:
            signature_parameters = inspect.signature(getattr(model_class, name)).parameters.values()
        accepted_names = {
            parameter.name
            for parameter in signature_parameters
            if parameter.kind
            not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        }
        accepts_keywords = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature_parameters
        )
        for parameter in signature_parameters:
            if parameter.name == "self" or parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                continue
            required = parameter.default is inspect.Parameter.empty
            explicit = isinstance(configured, dict) and parameter.name in configured
            value = (
                copy.deepcopy(configured[parameter.name])
                if explicit
                else None
                if required
                else copy.deepcopy(parameter.default)
            )
            parameters.append(
                SetupParameter(parameter.name, value, required, required or explicit)
            )
        if (model_class is None or accepts_keywords) and isinstance(configured, dict):
            parameters.extend(
                SetupParameter(key, copy.deepcopy(value), True, True)
                for key, value in configured.items()
                if key not in accepted_names
            )
        methods.append(
            SetupMethod(
                name=name,
                group=group_by_method.get(name, "Other installed methods"),
                parameters=tuple(parameters),
                selected=name in active_configs,
            )
        )
    return methods


def preset_steps(profile: str, template: dict) -> list[str]:
    active = active_step_names(template)
    if profile == "Full":
        return active
    configured = PRESET_STEPS.get(profile, ())
    return [name for name in active if name in configured]


def catalog_keys_by_category(catalog: dict, categories: set[str]) -> list[str]:
    preferred = []
    remaining = []
    for key, entry in catalog.items():
        if key == "meta" or not isinstance(entry, dict):
            continue
        category = str((entry.get("metadata") or {}).get("category", "")).lower()
        (preferred if category in categories else remaining).append(key)
    return sorted(preferred) + sorted(remaining)


def _replace_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def _step(recipe: dict, name: str) -> dict:
    for entry in recipe["steps"]:
        if name in entry:
            return entry[name]
    raise KeyError(name)


def _remove_unsupported_parameters(recipe: dict) -> list[str]:
    try:
        from hydromt_wflow import WflowSbmModel
    except ImportError:
        return []

    removed = []
    for entry in recipe["steps"]:
        name, parameters = next(iter(entry.items()))
        method = getattr(WflowSbmModel, name, None)
        if method is None or not isinstance(parameters, dict):
            continue
        signature = inspect.signature(method)
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            continue
        for key in list(parameters):
            if key not in signature.parameters:
                parameters.pop(key)
                removed.append(f"{name}.{key}")
    return removed


def build_recipe(
    template: dict,
    selected_steps: list[str],
    replacements: dict[str, str],
    *,
    starttime: str,
    endtime: str,
    timestep_seconds: int,
    region_mode: str,
    bbox: tuple[float, float, float, float],
    outlet: tuple[float, float],
    uparea: float,
    region_value=None,
    step_configs: dict[str, dict] | None = None,
) -> tuple[dict, list[str]]:
    available = (
        {method.name for method in discover_setup_methods(template)}
        if step_configs is not None
        else set(active_step_names(template))
    )
    unknown = sorted(set(selected_steps).difference(available))
    if unknown:
        raise ValueError("Unknown or unavailable setup steps: " + ", ".join(unknown))
    recipe = copy.deepcopy(template)
    if step_configs is None:
        recipe["steps"] = [
            entry for entry in recipe["steps"] if next(iter(entry)) in selected_steps
        ]
    else:
        recipe["steps"] = [
            {name: copy.deepcopy(step_configs.get(name, {}))} for name in selected_steps
        ]
    recipe = _replace_placeholders(recipe, replacements)

    names = active_step_names(recipe)
    if "setup_config" in names:
        setup_config = _step(recipe, "setup_config")
        config = setup_config.setdefault("data", {})
        config["time.starttime"] = starttime
        config["time.endtime"] = endtime
        config["time.timestepsecs"] = timestep_seconds
        config["model.snow__flag"] = False
        config["model.snow_gravitational_transport__flag"] = False
    if "setup_basemaps" in names:
        basemaps = _step(recipe, "setup_basemaps")
        if region_mode == "bbox":
            west, south, east, north = bbox
            if west >= east or south >= north:
                raise ValueError("AOI bounds are invalid.")
            basemaps["region"] = {"bbox": [west, south, east, north]}
        else:
            if isinstance(region_value, dict) and "value" in region_value:
                region = {region_mode: region_value["value"]}
                region.update(
                    {key: value for key, value in region_value.items() if key != "value"}
                )
            else:
                region = {
                    region_mode: region_value
                    if region_value is not None
                    else [outlet[0], outlet[1]]
                }
                if region_value is None and region_mode == "subbasin":
                    region["uparea"] = uparea
            basemaps["region"] = region

    unresolved = []
    for line in yaml.safe_dump(recipe, sort_keys=False).splitlines():
        if "PLACEHOLDER_" in line:
            unresolved.append(line.strip())
    if unresolved:
        raise ValueError("Unresolved Wflow template placeholders: " + ", ".join(unresolved))
    return recipe, _remove_unsupported_parameters(recipe)
