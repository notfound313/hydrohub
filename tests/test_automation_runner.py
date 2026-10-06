import tempfile
import time
import unittest
from pathlib import Path

from hydrohub.core.automation_runner import AutomationRunner
from hydrohub.plugins.custom_tools import CustomToolStore, MANIFEST


class RunnerTests(unittest.TestCase):
    def test_parallel_jobs_retry_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = AutomationRunner(Path(directory))
            calls = {}

            def step(job, definition, project, model, context):
                calls[job["id"]] = calls.get(job["id"], 0) + 1
                if calls[job["id"]] == 1:
                    raise ValueError("try again")
                return {"completed": True}

            runner._step = step
            spec = {"id": "sample", "targets": [{"project_id": "p", "model_id": "one"},
                                                  {"project_id": "p", "model_id": "two"}],
                    "steps": [{"id": "run", "tool": "test"}],
                    "concurrency": {"max_parallel_models": 2},
                    "on_failure": {"strategy": "retry", "retry_count": 1}}
            projects = {"p": {"root": Path(directory), "models": {"one": Path(directory), "two": Path(directory)}}}
            jobs = runner.start(spec, projects)
            deadline = time.monotonic() + 5
            while any(job["status"] not in {"SUCCESS", "FAILED"} for job in jobs) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([job["status"] for job in jobs], ["SUCCESS", "SUCCESS"])
            for job in jobs:
                self.assertIn("RETRYING", [event["status"] for event in job["events"]])
                self.assertIn("SUCCESS run", (runner.root / job["id"] / "run.log").read_text())
            self.assertEqual(len(AutomationRunner(Path(directory)).jobs), 2)

    def test_same_model_jobs_never_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = AutomationRunner(Path(directory))
            active = [0, 0]

            def step(*_args):
                active[0] += 1
                active[1] = max(active)
                time.sleep(0.05)
                active[0] -= 1
                return {}

            runner._step = step
            projects = {"p": {"root": Path(directory), "models": {"m": Path(directory)}}}
            spec = {"targets": [{"project_id": "p", "model_id": "m"}],
                    "steps": [{"id": "step", "tool": "test"}]}
            jobs = []
            for automation_id in ("first", "second"):
                jobs.extend(runner.start({**spec, "id": automation_id}, projects))
            deadline = time.monotonic() + 5
            while any(job["status"] != "SUCCESS" for job in jobs) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([job["status"] for job in jobs], ["SUCCESS", "SUCCESS"])
            self.assertEqual(active[1], 1)

    def test_custom_step_json_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            project = workspace / "project"
            model = project / "model"
            model.mkdir(parents=True)
            (model / "wflow_sbm.toml").write_text("[time]\nstarttime = '2026-01-01'\nendtime = '2026-01-02'\n")
            store = CustomToolStore(project, workspace / ".hydrohub" / "custom_tools")
            manifest = {**MANIFEST, "id": "sample", "parameters": [{"name": "value", "type": "integer", "required": True}],
                        "input_schema": {"value": "integer"}, "output_schema": {"status": "string", "value": "integer"}}
            store.create(manifest, "import json, sys\ndata = json.load(sys.stdin)\nprint(json.dumps({'status': 'ok', 'value': data['params']['value']}))\n")
            runner = AutomationRunner(workspace)
            job = {"id": "samplejob", "target": {"project_id": "p", "model_id": "m"}}
            output = runner._step(job, {"tool": "custom.sample", "params": {"value": 4}},
                                  {"root": project, "name": "Project"}, model, {})
            self.assertEqual(output, {"status": "ok", "value": 4})
            self.assertIn("status", (runner.root / "samplejob" / "run.log").read_text())


if __name__ == "__main__":
    unittest.main()
