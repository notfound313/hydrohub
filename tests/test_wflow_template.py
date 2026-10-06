import unittest

from hydrohub.core.wflow_template import (
    PLACEHOLDER_DEFAULTS,
    active_step_names,
    build_recipe,
    discover_setup_methods,
    load_master_template,
    preset_steps,
)


class WflowTemplateTests(unittest.TestCase):
    def test_setup_discovery_uses_installed_methods_and_master_defaults(self) -> None:
        methods = {
            method.name: method for method in discover_setup_methods(load_master_template())
        }

        self.assertIn("setup_lulcmaps_from_vector", methods)
        self.assertNotIn("setup_subbasins", methods)
        self.assertTrue(methods["setup_precip_forcing"].selected)
        precip = {
            parameter.name: parameter
            for parameter in methods["setup_precip_forcing"].parameters
        }
        self.assertEqual(precip["chunksize"].value, 336)
        self.assertTrue(precip["chunksize"].selected)
        rivers = {
            parameter.name for parameter in methods["setup_rivers"].parameters
        }
        self.assertNotIn("river_depth_kwargs", rivers)
        constants = methods["setup_constant_pars"].parameters
        self.assertGreater(len(constants), 5)

    def test_profiles_follow_active_master_step_order(self) -> None:
        template = load_master_template()
        active = active_step_names(template)

        self.assertEqual(preset_steps("Full", template), active)
        self.assertEqual(
            preset_steps("Minimal", template),
            ["setup_config", "setup_basemaps", "setup_rivers", "setup_outlets"],
        )
        self.assertNotIn("setup_precip_forcing", preset_steps("Standard", template))

    def test_recipe_resolves_sources_region_and_installed_version(self) -> None:
        template = load_master_template()
        recipe, removed = build_recipe(
            template,
            preset_steps("Full", template),
            PLACEHOLDER_DEFAULTS,
            starttime="2020-01-01T00:00:00",
            endtime="2020-12-31T00:00:00",
            timestep_seconds=86400,
            region_mode="bbox",
            bbox=(105.0, -9.0, 115.0, -5.0),
            outlet=(110.0, -7.0),
            uparea=50.0,
        )

        serialized = str(recipe)
        self.assertNotIn("PLACEHOLDER_", serialized)
        basemaps = next(
            entry["setup_basemaps"]
            for entry in recipe["steps"]
            if "setup_basemaps" in entry
        )
        rivers = next(
            entry["setup_rivers"] for entry in recipe["steps"] if "setup_rivers" in entry
        )
        self.assertEqual(basemaps["region"]["bbox"], [105.0, -9.0, 115.0, -5.0])
        self.assertEqual(basemaps["hydrography_fn"], "merit_jawa")
        setup_config = next(
            entry["setup_config"]
            for entry in recipe["steps"]
            if "setup_config" in entry
        )
        self.assertFalse(setup_config["data"]["model.snow__flag"])
        self.assertFalse(
            setup_config["data"]["model.snow_gravitational_transport__flag"]
        )
        self.assertNotIn("river_depth_kwargs", rivers)
        self.assertIn("setup_rivers.river_depth_kwargs", removed)

    def test_recipe_uses_dynamic_subbasin_value_from_catalog(self) -> None:
        template = load_master_template()
        recipe, _removed = build_recipe(
            template,
            ["setup_basemaps"],
            PLACEHOLDER_DEFAULTS,
            starttime="2020-01-01T00:00:00",
            endtime="2020-12-31T00:00:00",
            timestep_seconds=86400,
            region_mode="subbasin",
            bbox=(105.0, -9.0, 115.0, -5.0),
            outlet=(0.0, 0.0),
            uparea=0.0,
            region_value={"value": [110.2, -7.2], "uparea": 250},
        )
        basemaps = recipe["steps"][0]["setup_basemaps"]

        self.assertEqual(basemaps["region"]["subbasin"], [110.2, -7.2])
        self.assertEqual(basemaps["region"]["uparea"], 250)

    def test_recipe_accepts_available_alternative_method_configuration(self) -> None:
        template = load_master_template()
        config = {
            "setup_precip_from_point_timeseries": {
                "precip_fn": "station_precip",
                "interp_type": "nearest",
            }
        }

        recipe, removed = build_recipe(
            template,
            ["setup_precip_from_point_timeseries"],
            PLACEHOLDER_DEFAULTS,
            starttime="2020-01-01T00:00:00",
            endtime="2020-12-31T00:00:00",
            timestep_seconds=86400,
            region_mode="bbox",
            bbox=(105.0, -9.0, 115.0, -5.0),
            outlet=(110.0, -7.0),
            uparea=50.0,
            step_configs=config,
        )

        self.assertEqual(
            recipe["steps"], [{"setup_precip_from_point_timeseries": config["setup_precip_from_point_timeseries"]}]
        )
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
