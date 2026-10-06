import unittest

from hydrohub.plugins.registry import ToolRegistry
from hydrohub.tools.soilgrids import SOILGRIDS_CRS_URI, _request_params, parser as soilgrids_parser


class ToolRegistryTests(unittest.TestCase):
    def test_registers_hydrohub_owned_categories(self) -> None:
        tools = ToolRegistry().discover()
        self.assertEqual({tool.category for tool in tools}, {"download", "preparing", "qc"})
        self.assertEqual(len(tools), 25)
        self.assertTrue(all(tool.id.startswith("hydrohub.") for tool in tools))
        self.assertTrue(all(tool.module.startswith("hydrohub.tools.") for tool in tools))

    def test_all_tools_have_domain_and_fields(self) -> None:
        tools = ToolRegistry().discover()
        for tool in tools:
            self.assertGreater(len(tool.fields), 0, f"{tool.id} has no fields")
            self.assertTrue(tool.domain, f"{tool.id} has no domain")

    def test_downloaders_expose_configurable_spatial_output_and_credentials(self) -> None:
        downloaders = ToolRegistry().by_category("download")
        self.assertGreaterEqual(len(downloaders), 5)
        for tool in downloaders:
            fields = {field.name: field for field in tool.fields}
            if tool.id.endswith("gportal_probe"):
                self.assertTrue({"report_file", "host", "port", "username", "password"}.issubset(fields))
                self.assertEqual(fields["password"].environment_name, "GPORTAL_PASSWORD")
                self.assertTrue(fields["password"].secret)
                continue
            self.assertTrue({"west", "south", "east", "north", "output_dir"}.issubset(fields))
        gsmap = next(tool for tool in downloaders if tool.id.endswith("gsmap_daily"))
        fields = {field.name: field for field in gsmap.fields}
        self.assertEqual(fields["password"].environment_name, "GPORTAL_PASSWORD")
        self.assertTrue(fields["password"].secret)
        self.assertIn("start_date", fields)

    def test_each_domain_has_dedicated_tools(self) -> None:
        tools = ToolRegistry().discover()
        domains = {tool.domain for tool in tools}
        self.assertIn("Rainfall", domains)
        self.assertIn("PET", domains)
        self.assertIn("Soil", domains)
        self.assertIn("River", domains)
        self.assertGreaterEqual(len([t for t in tools if t.domain == "Rainfall"]), 4)
        self.assertGreaterEqual(len([t for t in tools if t.domain == "PET"]), 2)
        self.assertGreaterEqual(len([t for t in tools if t.domain == "Soil"]), 4)
        self.assertGreaterEqual(len([t for t in tools if t.domain == "River"]), 5)

    def test_preparing_tools_match_sample_directory(self) -> None:
        ids = {tool.id for tool in ToolRegistry().by_category("preparing")}
        self.assertNotIn("hydrohub.preparing.merit_hydro_preprocessing", ids)
        self.assertNotIn("hydrohub.preparing.river_geom_qbankfull", ids)
        self.assertNotIn("hydrohub.preparing.gqbf_to_gpkg", ids)
        self.assertNotIn("hydrohub.preparing.extract_river_geom", ids)
        self.assertNotIn("hydrohub.preparing.fix_soilgrids_crs", ids)
        self.assertNotIn("hydrohub.preparing.inspect_forcing", ids)
        self.assertNotIn("hydrohub.preparing.export_hydro_rivers_lin", ids)
        self.assertNotIn("hydrohub.preparing.river_geom", ids)

    def test_optional_file_fields_are_not_required_by_the_ui(self) -> None:
        fields = {
            (tool.id, field.name): field
            for tool in ToolRegistry().discover()
            for field in tool.fields
        }
        for key in (
            ("hydrohub.preparing.rebuild_flowdir", "old_d8"),
            ("hydrohub.qc.runtime_soil", "forcing"),
            ("hydrohub.qc.gpm", "hydromt_forcing"),
        ):
            self.assertFalse(fields[key].required, key)

    def test_soilgrids_uses_native_wcs_contract(self) -> None:
        args = soilgrids_parser().parse_args(
            ["--west", "105", "--south", "-8.9", "--east", "114.7", "--north", "-5.8", "--output-dir", "raw"]
        )
        self.assertEqual(args.properties, "bdod,clay,silt,sand,soc,phh2o")
        params = _request_params("soc_0-5cm_mean", (1.0, 2.0, 3.0, 4.0))
        self.assertIn(("VERSION", "2.0.1"), params)
        self.assertIn(("COVERAGEID", "soc_0-5cm_mean"), params)
        self.assertIn(("SUBSETTINGCRS", SOILGRIDS_CRS_URI), params)
        self.assertEqual([value for key, value in params if key == "SUBSET"], ["X(1.000,3.000)", "Y(2.000,4.000)"])


if __name__ == "__main__":
    unittest.main()
