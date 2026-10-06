import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.workflow_manager import (
    WorkflowManager,
    dump_catalog_yaml,
    dump_recipe_yaml,
)


class WorkflowManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = ProjectManager().create(Path(self.temp.name), "Workflow Test")
        self.workflow = WorkflowManager()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_aoi_is_persisted_as_geojson(self) -> None:
        feature = self.workflow.save_aoi_bbox(self.project, 104.5, -9.5, 115.3, -5.0)

        self.assertEqual(feature["geometry"]["type"], "Polygon")
        self.assertEqual(
            self.workflow.load_aoi_bounds(self.project),
            (104.5, -9.5, 115.3, -5.0),
        )
        state = self.workflow.load_setup(self.project)
        self.assertEqual(state["steps"]["aoi"], "VALID")

    def test_polygon_feature_collection_can_be_saved_as_aoi(self) -> None:
        collection = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[106.0, -8.0], [112.0, -8.0], [112.0, -5.0], [106.0, -5.0], [106.0, -8.0]]],
                },
            }],
        }
        saved = self.workflow.save_aoi_geojson(self.project, collection)
        self.assertEqual(self.workflow.aoi_bounds(saved), (106.0, -8.0, 112.0, -5.0))
        with self.assertRaises(ValueError):
            self.workflow.save_aoi_geojson(self.project, {"type": "Point", "coordinates": [1, 2]})

    def test_shapefile_zip_import_reprojects_and_validates_contents(self) -> None:
        import geopandas as gpd
        from shapely.geometry import Point, Polygon

        source = Path(self.temp.name) / "shape"
        source.mkdir()
        shapefile = source / "basin.shp"
        polygon = Polygon([(106, -8), (112, -8), (112, -5), (106, -5)])
        gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326").to_crs("EPSG:3857").to_file(shapefile)
        archive_path = Path(self.temp.name) / "basin.zip"

        def archive(files):
            with zipfile.ZipFile(archive_path, "w") as zipped:
                for file in files:
                    zipped.write(file, f"nested/{file.name}")

        files = list(source.iterdir())
        archive(files)
        feature = self.workflow.load_aoi_file(archive_path)
        saved = self.workflow.save_aoi_geojson(self.project, feature)
        for actual, expected in zip(self.workflow.aoi_bounds(saved), (106, -8, 112, -5)):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertEqual(self.workflow.load_setup(self.project)["steps"]["aoi"], "VALID")

        archive(file for file in files if file.suffix != ".shx")
        with self.assertRaisesRegex(ValueError, "needs matching"):
            self.workflow.load_aoi_file(archive_path)
        with zipfile.ZipFile(archive_path, "w") as zipped:
            for file in files:
                zipped.write(file, file.name)
            zipped.write(shapefile, "other.shp")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.workflow.load_aoi_file(archive_path)

        point = source / "point.shp"
        gpd.GeoDataFrame(geometry=[Point(106, -8)], crs="EPSG:4326").to_file(point)
        archive(file for file in source.iterdir() if file.stem == "point")
        with self.assertRaisesRegex(ValueError, "Polygon or MultiPolygon"):
            self.workflow.load_aoi_file(archive_path)
        archive_path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "ZIP is invalid"):
            self.workflow.load_aoi_file(archive_path)

    def test_catalog_first_gate_then_recipe_and_runtime(self) -> None:
        self.workflow.create_catalog(self.project)
        valid, issues = self.workflow.validate_catalog(self.project)
        self.assertFalse(valid)
        self.assertIn("Add at least one dataset entry.", issues)
        with self.assertRaises(ValueError):
            self.workflow.save_wflow_recipe(self.project, ["setup_config"], "")

        data = self.project.root / "data" / "raw" / "dem.tif"
        data.write_bytes(b"test fixture")
        catalog_path = self.workflow.add_catalog_entry(
            self.project,
            "dem_local",
            "RasterDataset",
            "rasterio",
            "data/raw/dem.tif",
            "4326",
            "dem",
        )
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(catalog["dem_local"]["driver"]["name"], "rasterio")
        self.assertTrue(self.workflow.validate_catalog(self.project)[0])

        catalog["meta"]["name"] = "advanced_editor_catalog"
        self.workflow.save_catalog_yaml(
            self.project, yaml.safe_dump(catalog, sort_keys=False)
        )
        self.assertIn("advanced_editor_catalog", catalog_path.read_text(encoding="utf-8"))

        recipe = self.workflow.save_wflow_recipe(
            self.project, ["setup_config", "setup_basemaps"], "dem_local"
        )
        self.assertTrue(recipe.exists())
        recipe_content = recipe.read_text(encoding="utf-8")
        self.workflow.save_wflow_recipe_yaml(self.project, recipe_content)
        with self.assertRaises(ValueError):
            self.workflow.save_wflow_recipe_yaml(
                self.project,
                "steps:\n  - setup_basemaps:\n      hydrography_fn: unknown_key\n",
            )
        with self.assertRaises(ValueError):
            self.workflow.save_runtime(
                self.project,
                "2020-01-01T00:00:00",
                "2020-12-31T00:00:00",
                86400,
                "output.nc",
                "output.csv",
            )
        (self.project.root / "models" / "wflow" / "model.toml").write_text(
            "[model]\n", encoding="utf-8"
        )
        runtime = self.workflow.save_runtime(
            self.project,
            "2020-01-01T00:00:00",
            "2020-12-31T00:00:00",
            86400,
            "output.nc",
            "output.csv",
        )
        self.assertIn("timestepsecs = 86400", runtime.read_text(encoding="utf-8"))

    def test_invalid_local_uri_keeps_catalog_in_warning(self) -> None:
        self.workflow.add_catalog_entry(
            self.project,
            "missing",
            "Dataset",
            "netcdf",
            "data/raw/missing.nc",
        )

        valid, issues = self.workflow.validate_catalog(self.project)
        self.assertFalse(valid)
        self.assertTrue(any("does not resolve" in issue for issue in issues))

    def test_catalog_entry_preserves_driver_options_and_metadata(self) -> None:
        source = self.project.root / "data" / "forcing.nc"
        source.write_bytes(b"forcing")

        path = self.workflow.add_catalog_entry(
            self.project,
            "daily_forcing",
            "RasterDataset",
            "raster_xarray",
            "forcing.nc",
            "4326",
            "meteo",
            driver_options={"chunks": {"time": 336}, "decode_times": True},
            metadata={
                "notes": "Daily forcing",
                "source_url": "https://example.test/forcing",
                "source_license": "CC-BY-4.0",
            },
        )

        entry = yaml.safe_load(path.read_text(encoding="utf-8"))["daily_forcing"]
        self.assertEqual(entry["driver"]["options"]["chunks"], {"time": 336})
        self.assertIs(entry["driver"]["options"]["decode_times"], True)
        self.assertEqual(entry["metadata"]["crs"], "4326")
        self.assertEqual(entry["metadata"]["category"], "meteo")
        self.assertEqual(entry["metadata"]["source_license"], "CC-BY-4.0")

    def test_multiple_catalogs_and_recipes_are_independent(self) -> None:
        first_data = self.project.root / "data" / "raw" / "first.tif"
        second_data = self.project.root / "data" / "raw" / "second.tif"
        first_data.write_bytes(b"first")
        second_data.write_bytes(b"second")

        first_catalog = self.workflow.add_catalog_entry(
            self.project,
            "first_dem",
            "RasterDataset",
            "rasterio",
            "data/raw/first.tif",
            category="dem",
            filename="first_catalog.yml",
        )
        second_catalog = self.workflow.add_catalog_entry(
            self.project,
            "second_dem",
            "RasterDataset",
            "rasterio",
            "data/raw/second.tif",
            category="dem",
            filename="second_catalog.yml",
        )

        self.assertNotEqual(first_catalog, second_catalog)
        self.assertEqual(
            self.workflow.list_catalogs(self.project),
            ["first_catalog.yml", "second_catalog.yml"],
        )
        self.assertTrue(self.workflow.validate_catalog(self.project, "first_catalog.yml")[0])
        self.assertTrue(self.workflow.validate_catalog(self.project, "second_catalog.yml")[0])
        self.assertNotIn("second_dem", first_catalog.read_text(encoding="utf-8"))

        first_recipe = self.workflow.save_wflow_recipe(
            self.project,
            ["setup_config", "setup_basemaps"],
            "first_dem",
            "first_catalog.yml",
            "first_wflow.yml",
        )
        second_recipe = self.workflow.save_wflow_recipe(
            self.project,
            ["setup_config", "setup_basemaps"],
            "second_dem",
            "second_catalog.yml",
            "second_wflow.yml",
        )
        self.assertIn("first_dem", first_recipe.read_text(encoding="utf-8"))
        self.assertIn("second_dem", second_recipe.read_text(encoding="utf-8"))
        self.assertEqual(
            self.workflow.list_wflow_recipes(self.project),
            ["first_wflow.yml", "second_wflow.yml"],
        )

        state = self.workflow.save_ui_state(
            self.project,
            active_catalog_file="second_catalog.yml",
            active_wflow_file="second_wflow.yml",
        )
        self.assertEqual(state, self.workflow.load_ui_state(self.project))

    def test_generated_yaml_has_space_between_entries(self) -> None:
        catalog_text = dump_catalog_yaml(
            {
                "meta": {"version": "1"},
                "first": {"data_type": "Dataset"},
                "second": {"data_type": "Dataset"},
            }
        )
        recipe_text = dump_recipe_yaml(
            {"steps": [{"setup_config": {}}, {"setup_basemaps": {}}]}
        )

        self.assertIn("version: '1'\n\nfirst:", catalog_text)
        self.assertIn("setup_config: {}\n\n- setup_basemaps:", recipe_text)

    def test_catalog_uri_resolves_dynamic_placeholders_and_wildcards(self) -> None:
        source_dir = self.project.root / "data" / "dynamic"
        source_dir.mkdir(parents=True)
        (source_dir / "flwdir_main.tif").write_bytes(b"flow")
        (source_dir / "elevtn_main.tif").write_bytes(b"elevation")
        path = self.workflow.add_catalog_entry(
            self.project,
            "dynamic_hydrography",
            "RasterDataset",
            "rasterio",
            "data/dynamic/{variabel}*tif",
            category="hydrography",
            placeholders={
                "variabel": ["flwdir", "elevtn"],
                "basin": {"solo": [110.0, -7.0]},
                "subbasin": {
                    "solo_upper": {"value": [110.2, -7.2], "uparea": 250}
                },
            },
        )
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
        resolution = self.workflow.resolve_catalog_uri(
            self.project, catalog, "dynamic_hydrography"
        )

        self.assertTrue(resolution.valid, resolution.issues)
        self.assertEqual(resolution.placeholders["variabel"], ["flwdir", "elevtn"])
        self.assertEqual(len(resolution.patterns), 2)
        self.assertEqual(len(resolution.matches), 2)
        self.assertEqual(
            self.workflow.catalog_values(catalog, "dynamic_hydrography", "basin"),
            [("solo", [110.0, -7.0])],
        )

        runtime_path = self.workflow.prepare_hydromt_catalog(
            self.project, path.name
        )
        runtime_catalog = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
        runtime_entry = runtime_catalog["dynamic_hydrography"]
        self.assertIn("{variable}", runtime_entry["uri"])
        self.assertNotIn("{variabel}", runtime_entry["uri"])
        self.assertEqual(
            Path(runtime_entry["uri"]).parent,
            source_dir.resolve(),
        )
        self.assertNotIn("variabel", runtime_entry.get("placeholders", {}))
        self.assertIn("{variabel}", path.read_text(encoding="utf-8"))

    def test_catalog_relative_uri_uses_project_data_as_root(self) -> None:
        source = self.project.root / "data" / "raw" / "data_root.tif"
        source.write_bytes(b"data")
        path = self.workflow.add_catalog_entry(
            self.project,
            "data_root_source",
            "RasterDataset",
            "rasterio",
            "raw/data_root.tif",
        )
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))

        resolution = self.workflow.resolve_catalog_uri(
            self.project, catalog, "data_root_source"
        )
        self.assertTrue(resolution.valid, resolution.issues)
        self.assertEqual(resolution.matches, [source])

    def test_catalog_uri_rejects_unknown_placeholder_and_supports_delete(self) -> None:
        path = self.workflow.add_catalog_entry(
            self.project,
            "unknown_source",
            "RasterDataset",
            "rasterio",
            "data/raw/{unknown}*.tif",
        )
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))
        resolution = self.workflow.resolve_catalog_uri(
            self.project, catalog, "unknown_source"
        )

        self.assertFalse(resolution.valid)
        self.assertTrue(any("not recognized" in issue for issue in resolution.issues))
        self.workflow.delete_catalog_entry(self.project, "unknown_source")
        updated = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn("unknown_source", updated)

    def test_catalog_uri_discovers_available_placeholder_values_from_files(self) -> None:
        source_dir = self.project.root / "data" / "daily"
        source_dir.mkdir(parents=True)
        (source_dir / "rain_20260101_part.tif").write_bytes(b"rain")
        path = self.workflow.add_catalog_entry(
            self.project,
            "daily_rain",
            "RasterDataset",
            "rasterio",
            "data/daily/rain_{date}*tif",
        )
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))

        resolution = self.workflow.resolve_catalog_uri(self.project, catalog, "daily_rain")
        self.assertTrue(resolution.valid, resolution.issues)
        self.assertTrue(resolution.placeholders["date"])
        self.assertEqual(len(resolution.matches), 1)

    def test_catalog_uri_resolves_global_database_configuration(self) -> None:
        global_root = self.project.root.parent / "global-db"
        (global_root / "dem").mkdir(parents=True)
        (global_root / "dem" / "solo.tif").write_bytes(b"dem")
        self.workflow.save_ui_state(
            self.project, global_database_root=str(global_root)
        )
        path = self.workflow.add_catalog_entry(
            self.project,
            "global_dem",
            "RasterDataset",
            "rasterio",
            "${GLOBAL_DB}/dem/{basin}*.tif",
            placeholders={"basin": {"solo": [110.0, -7.0]}},
        )
        catalog = yaml.safe_load(path.read_text(encoding="utf-8"))

        resolution = self.workflow.resolve_catalog_uri(self.project, catalog, "global_dem")
        self.assertTrue(resolution.valid, resolution.issues)
        self.assertEqual(Path(resolution.patterns[0]), global_root / "dem" / "solo*.tif")
        self.assertEqual(len(resolution.matches), 1)


if __name__ == "__main__":
    unittest.main()
