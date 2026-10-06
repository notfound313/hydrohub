import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.run_registry import RunRegistry


class RunRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = ProjectManager().create(Path(self.temp.name), "Registry Project")
        self.model = self.project.root / "models" / "wflow" / "model_a"
        self.model.mkdir(parents=True)
        (self.model / "forcing.nc").write_bytes(b"forcing-v1")
        (self.model / "wflow_sbm.toml").write_text(
            'dir_output = "run_default"\n\n'
            '[time]\nstarttime = "2001-01-01T00:00:00"\n'
            'endtime = "2001-12-31T00:00:00"\ntimestepsecs = 86400\n\n'
            '[model]\ncold_start__flag = true\nsoil_parameter = 3\n\n'
            '[state]\npath_input = "instate.nc"\npath_output = "outstate.nc"\n\n'
            '[input]\npath_forcing = "forcing.nc"\npath_static = "staticmaps.nc"\n\n'
            '[output.csv]\npath = "output.csv"\n',
            encoding="utf-8",
        )
        self.registry = RunRegistry(self.project.root, self.model, self.project.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepares_isolated_config_and_chains_completed_state(self) -> None:
        first = self.registry.plan(
            datetime(2001, 1, 1), datetime(2001, 12, 31), 86400
        )
        self.assertIsNone(first.predecessor)
        first_record, first_config = self.registry.prepare_run(first)
        with first_config.open("rb") as stream:
            config = tomllib.load(stream)
        self.assertEqual(Path(config["dir_input"]), self.model)
        self.assertEqual(Path(config["dir_output"]), self.registry.resolve_path(first_record.output_path))
        self.assertEqual(config["output"]["csv"]["path"], "output.csv")
        self.assertNotIn("path_input", config["state"])
        self.assertTrue(config["model"]["cold_start__flag"])
        state = self.registry.resolve_path(first_record.state_output_path)
        state.write_bytes(b"state")
        self.registry.finish_run(first_record.run_id, True)

        second = self.registry.plan(
            datetime(2002, 1, 1), datetime(2002, 12, 31), 86400
        )
        self.assertEqual(second.predecessor.run_id, first_record.run_id)
        second_record, second_config = self.registry.prepare_run(second)
        with second_config.open("rb") as stream:
            config = tomllib.load(stream)
        self.assertEqual(Path(config["state"]["path_input"]), state)
        self.assertFalse(config["model"]["cold_start__flag"])
        self.assertEqual(second_record.state_input_path, first_record.state_output_path)

    def test_registry_is_model_scoped_and_written_atomically_with_backup(self) -> None:
        plan = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 2, 1), 86400)
        record, _config = self.registry.prepare_run(plan)
        self.registry.finish_run(record.run_id, False)

        self.assertTrue((self.model / "registry.json").is_file())
        self.assertTrue((self.model / "registry.json.bak").is_file())
        self.assertTrue((self.model / "model_meta.json").is_file())
        self.assertTrue((self.project.root / "project_meta.json").is_file())
        self.assertEqual(self.registry.list_runs()[0].model_id, self.registry.model_id)

        other_model = self.project.root / "models" / "wflow" / "model_b"
        other_model.mkdir(parents=True)
        (other_model / "wflow_sbm.toml").write_text(
            (self.model / "wflow_sbm.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        other_registry = RunRegistry(self.project.root, other_model, self.project.name)
        self.assertNotEqual(other_registry.model_id, self.registry.model_id)
        self.assertEqual(other_registry.project_id, self.registry.project_id)
        self.assertEqual(other_registry.list_runs(), [])

    def test_overlap_requires_explicit_overwrite_and_can_backup(self) -> None:
        plan = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 2, 1), 86400)
        record, _config = self.registry.prepare_run(plan)
        overlap = self.registry.plan(
            datetime(2001, 1, 15), datetime(2001, 2, 15), 86400, record.run_id
        )
        self.assertEqual([run.run_id for run in overlap.overlaps], [record.run_id])
        with self.assertRaises(FileExistsError):
            self.registry.prepare_run(overlap, overwrite_run_id=None)

        replacement, _config = self.registry.prepare_run(
            overlap, overwrite_run_id=record.run_id, backup_output=True
        )
        self.assertEqual(replacement.run_id, record.run_id)
        self.assertTrue(any(self.model.glob(f"runs/{record.run_id}.backup_*")))

    def test_valid_chain_breaks_when_model_parameters_change(self) -> None:
        first = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 1, 31), 86400)
        first_record, _config = self.registry.prepare_run(first)
        self.registry.resolve_path(first_record.state_output_path).write_bytes(b"state")
        self.registry.finish_run(first_record.run_id, True)
        config_path = self.model / "wflow_sbm.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("soil_parameter = 3", "soil_parameter = 4"),
            encoding="utf-8",
        )
        second = self.registry.plan(datetime(2001, 2, 1), datetime(2001, 2, 28), 86400)
        self.assertTrue(any("different model config" in error for error in second.errors))
        with self.assertRaisesRegex(ValueError, "Warm start blocked"):
            self.registry.prepare_run(second)
        second = self.registry.plan(
            datetime(2001, 2, 1),
            datetime(2001, 2, 28),
            86400,
            start_mode="cold",
        )
        second_record, _config = self.registry.prepare_run(second)
        self.registry.finish_run(second_record.run_id, True)

        chains = self.registry.chains(86400)
        self.assertEqual(len(chains), 2)
        self.assertEqual(chains[0].break_reason, "config hash changed")

    def test_rejects_incomplete_netcdf_state_before_creating_run_folder(self) -> None:
        config_path = self.model / "wflow_sbm.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[state.variables]\nsoil_water_saturated_zone__depth = "soil_saturated_depth"\n',
            encoding="utf-8",
        )
        first = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 1, 31), 86400)
        first_record, _config = self.registry.prepare_run(first)
        xr.Dataset({"other": (("y", "x"), [[1.0]])}).to_netcdf(
            self.registry.resolve_path(first_record.state_output_path)
        )
        self.registry.finish_run(first_record.run_id, True)

        second = self.registry.plan(datetime(2001, 2, 1), datetime(2001, 2, 28), 86400)
        self.assertTrue(any("soil_saturated_depth" in error for error in second.errors))
        run_dir = self.model / "runs" / second.run_id
        with self.assertRaisesRegex(ValueError, "missing required variable"):
            self.registry.prepare_run(second)
        self.assertFalse(run_dir.exists())

    def test_state_validation_ignores_time_and_matches_spatial_aliases(self) -> None:
        config_path = self.model / "wflow_sbm.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[state.variables]\nvegetation_canopy_water__depth = "vegetation_water_depth"\n',
            encoding="utf-8",
        )
        xr.Dataset(
            {"vegetation_leaf_area_index": (("time", "latitude", "longitude"), np.zeros((12, 2, 3)))}
        ).to_netcdf(self.model / "staticmaps.nc")
        first = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 1, 31), 86400)
        first_record, _config = self.registry.prepare_run(first)
        state_path = self.registry.resolve_path(first_record.state_output_path)
        xr.Dataset(
            {"vegetation_water_depth": (("time", "lat", "lon"), np.zeros((1, 2, 3)))}
        ).to_netcdf(state_path)
        self.registry.finish_run(first_record.run_id, True)

        second = self.registry.plan(datetime(2001, 2, 1), datetime(2001, 2, 28), 86400)
        self.assertFalse(second.errors)

        xr.Dataset(
            {"vegetation_water_depth": (("time", "lat", "lon"), np.zeros((1, 2, 4)))}
        ).to_netcdf(state_path)
        second = self.registry.plan(datetime(2001, 2, 1), datetime(2001, 2, 28), 86400)
        self.assertTrue(any("lon=4" in error for error in second.errors))

    def test_combines_contiguous_csv_outputs_without_changing_sources(self) -> None:
        records = []
        for start, end, dates in (
            (datetime(2001, 1, 1), datetime(2001, 1, 2), ["2001-01-01", "2001-01-02"]),
            (datetime(2001, 1, 3), datetime(2001, 1, 4), ["2001-01-03", "2001-01-04"]),
        ):
            plan = self.registry.plan(start, end, 86400)
            record, _config = self.registry.prepare_run(plan)
            folder = self.registry.resolve_path(record.output_path)
            pd.DataFrame({"time": dates, "river_q_1": [1.0, 2.0]}).to_csv(
                folder / "output.csv", index=False
            )
            self.registry.resolve_path(record.state_output_path).write_bytes(b"state")
            self.registry.finish_run(record.run_id, True)
            records.append(record)

        chain = self.registry.chains(86400)[0]
        combined = self.registry.build_combined_output(chain)

        self.assertEqual(len(pd.read_csv(combined)), 4)
        self.assertEqual(len(pd.read_csv(self.registry.resolve_path(records[0].output_path) / "output.csv")), 2)
        self.assertTrue(str(combined).startswith(str(self.model / ".hydrohub" / "combined")))

    def test_detects_revised_forcing_but_accepts_a_larger_extension(self) -> None:
        first = self.registry.plan(datetime(2001, 1, 1), datetime(2001, 1, 31), 86400)
        first_record, _config = self.registry.prepare_run(first)
        self.registry.resolve_path(first_record.state_output_path).write_bytes(b"state")
        self.registry.finish_run(first_record.run_id, True)
        forcing = self.model / "forcing.nc"
        forcing.write_bytes(b"forcing-v2")
        second = self.registry.plan(datetime(2001, 2, 1), datetime(2001, 2, 28), 86400)
        second_record, _config = self.registry.prepare_run(second)
        self.registry.finish_run(second_record.run_id, True)
        self.assertEqual(self.registry.chains(86400)[0].break_reason, "forcing source was revised")

        forcing.write_bytes(b"forcing-v2-with-extension")
        third = self.registry.plan(datetime(2001, 3, 1), datetime(2001, 3, 31), 86400)
        third_record, _config = self.registry.prepare_run(third)
        self.registry.finish_run(third_record.run_id, True)
        chains = self.registry.chains(86400)
        self.assertEqual([run.run_id for run in chains[-1].runs], [second_record.run_id, third_record.run_id])

    def test_combines_contiguous_netcdf_outputs(self) -> None:
        for start, end in (
            (datetime(2001, 1, 1), datetime(2001, 1, 2)),
            (datetime(2001, 1, 3), datetime(2001, 1, 4)),
        ):
            plan = self.registry.plan(start, end, 86400)
            record, _config = self.registry.prepare_run(plan)
            folder = self.registry.resolve_path(record.output_path)
            xr.Dataset(
                {"river_q": ("time", [1.0, 2.0])},
                coords={"time": pd.date_range(start, end, freq="D")},
            ).to_netcdf(folder / "output.nc")
            self.registry.resolve_path(record.state_output_path).write_bytes(b"state")
            self.registry.finish_run(record.run_id, True)

        combined = self.registry.build_combined_output(self.registry.chains(86400)[0])
        with xr.open_dataset(combined) as dataset:
            self.assertEqual(dataset.sizes["time"], 4)


if __name__ == "__main__":
    unittest.main()
