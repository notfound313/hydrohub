from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolField:
    name: str
    label: str
    kind: str = "text"
    default: str | int | float | bool = ""
    required: bool = True
    choices: tuple[str, ...] = ()
    secret: bool = False
    environment_name: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    name: str
    category: str
    module: str
    description: str
    fields: tuple[ToolField, ...]
    domain: str = "General"
    version: str = "1.0.0"
    runtime: str = "python"


BBOX_FIELDS = (
    ToolField("west", "BBOX west", "float", 104.5),
    ToolField("south", "BBOX south", "float", -9.5),
    ToolField("east", "BBOX east", "float", 115.3),
    ToolField("north", "BBOX north", "float", -5.0),
)


BUILTIN_TOOLS = (
    # --- DOWNLOADS ---
    ToolDefinition(
        "hydrohub.download.gsmap_daily",
        "GSMaP Daily Gauge v8",
        "download",
        "hydrohub.tools.gsmap_daily",
        "Download daily JAXA GSMaP, crop to the project BBOX, convert mm/hr to mm/day, and write daily NetCDF files.",
        (
            ToolField("start_date", "Start date", "date", "2026-01-01"),
            ToolField("end_date", "End date", "date", "2026-01-31"),
            *BBOX_FIELDS,
            ToolField("output_dir", "Output directory", "directory", "data/raw/gsmap_daily"),
            ToolField("host", "SFTP host", default="ftp.gportal.jaxa.jp"),
            ToolField("port", "SFTP port", "integer", 2051),
            ToolField("username", "G-Portal username", "text", required=True, environment_name="GPORTAL_USER"),
            ToolField("password", "G-Portal password", "password", required=True, secret=True, environment_name="GPORTAL_PASSWORD"),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.download.era5_land_pet",
        "ERA5-Land raw PET",
        "download",
        "hydrohub.tools.era5_land_pet",
        "Download monthly ERA5-Land potential evaporation NetCDF files through the CDS API.",
        (
            ToolField("start_date", "Start date", "date", "2026-01-01"),
            ToolField("end_date", "End date", "date", "2026-12-31"),
            *BBOX_FIELDS,
            ToolField("output_dir", "Output directory", "directory", "data/raw/era5land_pet"),
        ),
        domain="PET",
    ),
    ToolDefinition(
        "hydrohub.download.soilgrids",
        "SoilGrids 2.0 WCS",
        "download",
        "hydrohub.tools.soilgrids",
        "Download six SoilGrids depth layers (250m) through ISRIC WCS using canonical Homolosine grid logic.",
        (
            *BBOX_FIELDS,
            ToolField("output_dir", "Output directory", "directory", "data/raw/soilgrids_2020"),
            ToolField("properties", "Properties", default="bdod,clay,silt,sand,soc,phh2o"),
            ToolField("bbox_buffer_deg", "BBOX buffer (degrees)", "float", 0.05),
        ),
        domain="Soil",
    ),
    ToolDefinition(
        "hydrohub.download.gportal_probe",
        "Probe G-Portal SFTP",
        "download",
        "hydrohub.tools.probe_gportal",
        "List directories on the JAXA G-Portal SFTP account to confirm the GSMaP daily path is exposed.",
        (
            ToolField("report_file", "Report JSON", "file", "outputs/reports/gportal_probe.json"),
            ToolField("host", "SFTP host", default="ftp.gportal.jaxa.jp"),
            ToolField("port", "SFTP port", "integer", 2051),
            ToolField("username", "G-Portal username", "text", required=True, environment_name="GPORTAL_USER"),
            ToolField("password", "G-Portal password", "password", required=True, secret=True, environment_name="GPORTAL_PASSWORD"),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.download.gsmap_daily_hokusai",
        "GSMaP Daily via FTP (Hokusai)",
        "download",
        "hydrohub.tools.download_gsmap_daily_hokusai",
        "Download JAXA GSMaP Gauge v8 Daily through the legacy hokusai FTP host, convert mm/hr to mm/day, and write daily NetCDF files.",
        (
            ToolField("start_date", "Start date", "date", "2026-01-01"),
            ToolField("end_date", "End date", "date", "2026-01-31"),
            *BBOX_FIELDS,
            ToolField("output_dir", "Output directory", "directory", "data/raw/gsmap_daily_hokusai"),
            ToolField("host", "FTP host", default="hokusai.eorc.jaxa.jp"),
            ToolField("port", "FTP port", "integer", 21),
            ToolField("username", "GSMaP FTP username", "text", required=True, environment_name="GSMAP_USER"),
            ToolField("password", "GSMaP FTP password", "password", required=True, secret=True, environment_name="GSMAP_PASSWORD"),
        ),
        domain="Rainfall",
    ),

    # --- PREPARING ---
    ToolDefinition(
        "hydrohub.preparing.gsmap_precip",
        "Prepare GSMaP Hourly Forcing",
        "preparing",
        "hydrohub.tools.prepare_gsmap_daily",
        "Combine direct GSMaP HOURLY rainfall files, aggregate to daily totals, and run coverage checks for HydroMT.",
        (
            ToolField("input_glob", "Input file glob", default="data/raw/gsmap_daily/*.nc"),
            ToolField("output_file", "Output NetCDF", "file", "data/preparing_data/precip_daily_hydromt.nc"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/prepare_precip.json"),
            ToolField("variable", "Variable", default="precip"),
            ToolField("min_coverage", "Minimum mean coverage", "float", 0.9),
            ToolField("min_daily_coverage", "Minimum daily coverage", "float", 0.90),
            ToolField("fail_missing_days", "Fail on missing days", "bool", True),
            ToolField("extreme_review_mm", "Extreme review threshold (mm)", "float", 500.0),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.preparing.gsmap_precip_hokusai",
        "Prepare GSMaP Daily Forcing (Hokusai)",
        "preparing",
        "hydrohub.tools.prepare_gsmap_daily_hokusai",
        "Combine hokusai GSMaP DAILY rainfall files (already daily, no aggregation) and run coverage checks for HydroMT.",
        (
            ToolField("input_glob", "Input file glob", default="data/raw/gsmap_daily_hokusai/*.nc"),
            ToolField("output_file", "Output NetCDF", "file", "data/preparing_data/precip_daily_hydromt_hokusai.nc"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/prepare_precip_hokusai.json"),
            ToolField("variable", "Variable", default="precip"),
            ToolField("min_coverage", "Minimum mean coverage", "float", 0.98),
            ToolField("min_daily_coverage", "Minimum daily coverage", "float", 0.90),
            ToolField("fail_missing_days", "Fail on missing days", "bool", True),
            ToolField("extreme_review_mm", "Extreme review threshold (mm)", "float", 500.0),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.preparing.era5land_pet",
        "Prepare ERA5-Land PET",
        "preparing",
        "hydrohub.tools.prepare_era5land_pet",
        "Convert ERA5-Land PET to Wflow positive mm/day, handle hourly accumulation shift, and clip to BBOX.",
        (
            ToolField("input_glob", "Input file glob", default="data/raw/era5land_pet/*.nc"),
            ToolField("output_file", "Output NetCDF", "file", "data/preparing_data/pet_daily_hydromt.nc"),
            ToolField("start_date", "Start date", "date", "2026-01-01"),
            ToolField("end_date", "End date", "date", "2026-12-31"),
            *BBOX_FIELDS,
        ),
        domain="PET",
    ),
    ToolDefinition(
        "hydrohub.preparing.soilgrids",
        "Prepare SoilGrids (Wflow)",
        "preparing",
        "hydrohub.tools.prepare_soilgrids",
        "Convert raw SoilGrids TIFFs to HydroMT-Wflow units (scaling factors) and create soilthickness.tif.",
        (
            ToolField("input_dir", "Raw TIFF directory", "directory", "data/raw/soilgrids_2020"),
            ToolField("output_dir", "Output directory", "directory", "data/preparing_data/soil"),
        ),
        domain="Soil",
    ),
    ToolDefinition(
        "hydrohub.preparing.modis_lai",
        "Prepare MODIS LAI",
        "preparing",
        "hydrohub.tools.prepare_modis_lai",
        "Convert 12-band MODIS LAI climatology GeoTIFF to monthly NetCDF for HydroMT.",
        (
            ToolField("input_file", "MODIS LAI TIFF", "file", "data/raw/vegetation/modis_lai.tif"),
            ToolField("output_file", "Output NetCDF", "file", "data/preparing_data/lai.nc"),
        ),
        domain="Vegetation",
    ),
    ToolDefinition(
        "hydrohub.preparing.gpm_imerg",
        "Prepare GPM IMERG",
        "preparing",
        "hydrohub.tools.prepare_gpm_imerg",
        "Combine GPM IMERG NetCDF files, detect units/hourly step, convert to mm per source step, and crop to BBOX.",
        (
            ToolField("input_glob", "Input file glob", default="data/raw/gpm_imerg/*.nc"),
            ToolField("output_file", "Output NetCDF", "file", "data/preparing_data/gpm_imerg_precip_jawa.nc"),
            *BBOX_FIELDS,
            ToolField("source_variable", "Source variable", default="auto"),
            ToolField("source_units", "Source units override", default=""),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.preparing.rebuild_flowdir",
        "Rebuild Flow Direction",
        "preparing",
        "hydrohub.tools.rebuild_flowdir",
        "Rebuild the HydroMT hydrography mapstack directly from the DEM using pyflwdir fill_depressions (no repair_loops).",
        (
            ToolField("dem", "DEM TIFF", "file", "data/raw/hydrography/dem_jawa.tif"),
            ToolField("uparea", "MERIT uparea TIFF", "file", "data/raw/hydrography/uparea_jawa_merit_km2.tif"),
            ToolField("old_d8", "Old D8 TIFF (optional)", "file", "", required=False),
            ToolField("output_dir", "Output directory", "directory", "data/preparing_data/hydrography_rebuilt"),
            ToolField("outlets", "Outlets mode", "choice", "edge", choices=("edge", "min")),
            ToolField("max_depth", "Max pour-point depth (m)", "float", -1.0),
            ToolField("river_threshold_km2", "River threshold (km2)", "float", 50.0),
        ),
        domain="River",
    ),
    ToolDefinition(
        "hydrohub.preparing.basin_delineate",
        "Delineate Subbasins",
        "preparing",
        "hydrohub.tools.basin_delineate",
        "Delineate subbasins and outlets from a D8 network and write basins/outlets/index layers to GeoPackage.",
        (
            ToolField("flwdir", "D8 TIFF", "file", "data/preparing_data/hydrography/flwdir.tif"),
            ToolField("output_gpkg", "Output GeoPackage", "file", "data/preparing_data/basins/min250km2_basins.gpkg"),
            ToolField("area_min_km2", "Minimum basin area (km2)", "float", 250.0),
        ),
        domain="River",
    ),
    ToolDefinition(
        "hydrohub.preparing.river_geom_from_network",
        "River Width from Network",
        "preparing",
        "hydrohub.tools.river_geom_from_network",
        "Sample MERIT river width along an existing river network GPKG and build HydroMT river_geom (width-only plus constant-depth TEST file).",
        (
            ToolField("river_gpkg", "River network GPKG", "file", "data/preparing_data/hydrography/river_network_jawa.gpkg"),
            ToolField("merit_width", "MERIT Width TIFF", "file", "data/raw/hydrography/MERIT_RiverWidth_Jawa.tif"),
            ToolField("output_dir", "Output directory", "directory", "data/preparing_data/hydrography"),
            ToolField("sample_step_pixels", "Sample step (pixels)", "float", 0.5),
            ToolField("max_search_radius_pixels", "Nearest search radius", "float", 2.5),
            ToolField("width_statistic", "Width statistic", "choice", "median", choices=("median", "mean", "max", "p75")),
            ToolField("create_test_geom", "Create TEST geometry", "bool", True),
            ToolField("test_rivdph_m", "TEST depth (m)", "float", 1.0),
        ),
        domain="River",
    ),
    # --- QC ---
    ToolDefinition(
        "hydrohub.qc.rainfall",
        "Rainfall QC",
        "qc",
        "hydrohub.tools.qc_precip",
        "Check rainfall timestamps, negatives, and spatial coverage for source and HydroMT output.",
        (
            ToolField("input_file", "Precipitation NC", "file", "data/preparing_data/precip_daily_hydromt.nc"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/qc_precip.json"),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.qc.pet",
        "PET QC",
        "qc",
        "hydrohub.tools.qc_pet",
        "Check PET timestamps, values, and consistency between source and HydroMT output.",
        (
            ToolField("input_file", "PET NC", "file", "data/preparing_data/pet_daily_hydromt.nc"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/qc_pet.json"),
        ),
        domain="PET",
    ),
    ToolDefinition(
        "hydrohub.qc.soil",
        "SoilGrids QC",
        "qc",
        "hydrohub.tools.qc_soil_lai",
        "Check SoilGrids grid consistency, texture sums, and value ranges across layers.",
        (
            ToolField("soil_dir", "Soil directory", "directory", "data/preparing_data/soil"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/qc_soil.json"),
        ),
        domain="Soil",
    ),
    ToolDefinition(
        "hydrohub.qc.lai",
        "MODIS LAI QC",
        "qc",
        "hydrohub.tools.qc_soil_lai",
        "Check MODIS LAI monthly coverage, ranges, and Staticmaps LAI presence.",
        (
            ToolField("lai_file", "LAI NC", "file", "data/preparing_data/lai.nc"),
            ToolField("staticmaps", "Staticmaps NC", "file", "data/staticmaps/staticmaps.nc"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/qc_lai.json"),
        ),
        domain="Vegetation",
    ),
    ToolDefinition(
        "hydrohub.qc.ldd",
        "Flow Network QC",
        "qc",
        "hydrohub.tools.qc_ldd",
        "Check D8 codes and validity, optionally repair loops, and rebuild the HydroMT hydrography mapstack.",
        (
            ToolField("dem", "DEM TIFF", "file", "data/raw/hydrography/dem_jawa.tif"),
            ToolField("flwdir", "D8 TIFF", "file", "data/raw/hydrography/d8_jawa.tif"),
            ToolField("uparea", "MERIT uparea TIFF", "file", "data/raw/hydrography/uparea_jawa_merit_km2.tif"),
            ToolField("output_dir", "Output directory", "directory", "data/preparing_data/hydrography_qc"),
            ToolField("auto_repair", "Auto repair loops", "bool", True),
            ToolField("river_threshold_km2", "River threshold (km2)", "float", 50.0),
            ToolField("subcatchment_min_area_km2", "Subcatchment min area (km2)", "float", 250.0),
        ),
        domain="River",
    ),
    ToolDefinition(
        "hydrohub.qc.staticmaps",
        "Staticmaps LDD QC",
        "qc",
        "hydrohub.tools.qc_staticmaps",
        "Detect LDD cycles, invalid codes and out-of-bounds flow targets inside a HydroMT staticmaps NetCDF.",
        (
            ToolField("staticmaps", "Staticmaps NC", "file", "data/staticmaps/staticmaps.nc"),
            ToolField("output_dir", "Output directory", "directory", "outputs/qc/staticmaps"),
        ),
        domain="River",
    ),
    ToolDefinition(
        "hydrohub.qc.runtime_soil",
        "Runtime Soil QC",
        "qc",
        "hydrohub.tools.qc_runtime_soil",
        "Check soil, LAI and forcing staticmaps for values that can trigger Wflow runtime Int64(NaN) errors.",
        (
            ToolField("staticmaps", "Staticmaps NC", "file", "data/staticmaps/staticmaps.nc"),
            ToolField("forcing", "Forcing NC (optional)", "file", "", required=False),
            ToolField("output_dir", "Output directory", "directory", "outputs/qc/runtime_nan"),
            ToolField("forcing_time_chunk", "Forcing time chunk", "integer", 100),
        ),
        domain="Soil",
    ),
    ToolDefinition(
        "hydrohub.qc.gpm",
        "GPM IMERG QC",
        "qc",
        "hydrohub.tools.qc_gpm",
        "Check GPM IMERG timestamps, negatives, spatial coverage, and the source/HydroMT total ratio.",
        (
            ToolField("input_file", "Precipitation NC", "file", "data/preparing_data/gpm_imerg_precip_jawa.nc"),
            ToolField("hydromt_forcing", "HydroMT forcing NC (optional)", "file", "", required=False),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/qc_gpm.json"),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.qc.pit_blobs",
        "Diagnose Pit Blobs",
        "qc",
        "hydrohub.tools.qc_pit_blobs",
        "Diagnose new-pit (holey) cells introduced by repair_loops, with blob CSV/TIF and optional PNG panels.",
        (
            ToolField("d8_before", "D8 before repair TIFF", "file", "data/raw/hydrography/d8_jawa.tif"),
            ToolField("d8_after", "D8 after QC TIFF", "file", "data/preparing_data/hydrography_qc/d8_after_qc.tif"),
            ToolField("dem", "DEM TIFF", "file", "data/raw/hydrography/dem_jawa.tif"),
            ToolField("output_dir", "Output directory", "directory", "outputs/qc/pit_blobs"),
            ToolField("create_plots", "Create PNG panels", "bool", True),
        ),
        domain="River",
    ),
    ToolDefinition(
        "hydrohub.qc.test_gpm_catalog",
        "Test GPM Catalog",
        "qc",
        "hydrohub.tools.test_gpm_catalog",
        "Smoke-test a HydroMT GPM data catalog: resolve gpm_imerg_jawa, report CRS/bounds/time/min/max/NaN fraction.",
        (
            ToolField("catalog", "Data catalog YML", "file", "data_catalog_gpm.yml"),
            ToolField("source_name", "Source name", default="gpm_imerg_jawa"),
            ToolField("variables", "Variables", default="precip"),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/test_gpm_catalog.json"),
        ),
        domain="Rainfall",
    ),
    ToolDefinition(
        "hydrohub.qc.diagnose_wflow_soil",
        "Diagnose Wflow Soil Source",
        "qc",
        "hydrohub.tools.diagnose_wflow_soil_source",
        "Diagnose which soilgrids_2020 source a Wflow model actually resolves and verify clip reads.",
        (
            ToolField("model_root", "Wflow model root", "directory", "model_jawa"),
            ToolField("data_libs", "Data catalogs (comma separated)", default=""),
            ToolField("report_file", "Report JSON", "file", "outputs/reports/diagnose_wflow_soil.json"),
        ),
        domain="Soil",
    ),
)


class ToolRegistry:
    """Registry for HydroHub-owned tools. The sample directory is not executed."""

    def discover(self) -> list[ToolDefinition]:
        return list(BUILTIN_TOOLS)

    def by_category(self, category: str) -> list[ToolDefinition]:
        return [tool for tool in BUILTIN_TOOLS if tool.category == category]
