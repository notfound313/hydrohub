import json
import os
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
import pandas as pd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTime, Qt, QPointF
from PySide6.QtTest import QTest

from hydrohub.core.automation import AutomationStore, graph_order, next_trigger, pipeline_nodes, validate_pipeline
from hydrohub.core.automation_runner import AutomationRunner
from hydrohub.core.automation_scheduler import AutomationScheduler
from hydrohub.core.project_manager import ProjectManager
from hydrohub.plugins.custom_tools import CustomToolStore, MANIFEST
from hydrohub.ui.automation_page import AutomationPage
from hydrohub.ui.automation_canvas import WorkflowCanvas


class AutomationWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_graph_migration_validation_and_branch_execution(self):
        legacy = {"steps": [{"id": "first", "tool": "test", "params": {"keep": 4}},
                            {"id": "last", "tool": "test"}]}
        migrated = pipeline_nodes(legacy)
        self.assertEqual(migrated[0]["next"], ["last"])
        self.assertEqual(migrated[0]["params"], {"keep": 4})
        self.assertEqual(migrated[0]["config_mode"], "global")
        nodes = [
            {"id": "end", "tool": "test", "next": []},
            {"id": "ok", "tool": "test", "next": ["end"]},
            {"id": "warn", "tool": "test", "next": ["end"]},
            {"id": "start", "tool": "test", "next": [], "next_on_status": {"ok": ["ok"], "warning": ["warn"]}},
        ]
        self.assertEqual(graph_order(nodes)[0], "start")
        invalid = deepcopy(nodes)
        invalid[0]["next"] = ["start"]
        with self.assertRaises(ValueError):
            graph_order(invalid)
        invalid[0]["next"] = ["missing"]
        with self.assertRaises(ValueError):
            graph_order(invalid)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = AutomationRunner(root)
            calls = []
            def step(job, node, *_args):
                calls.append(node["id"])
                return {"status": "warning" if node["id"] == "start" else "ok"}
            runner._step = step
            jobs = runner.start({"id": "branch", "nodes": nodes, "targets": [{"project_id": "p", "model_id": "m"}]},
                                {"p": {"root": root, "models": {"m": root}}})
            deadline = time.monotonic() + 5
            while "branch" in runner.active and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(calls, ["start", "warn", "end"])
            self.assertEqual(jobs[0]["status"], "SUCCESS")
            self.assertEqual(jobs[0]["node_status"]["ok"], "Skipped")

    def test_local_custom_parameters_and_registered_python_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            root = workspace / "project"
            model = root / "model"
            model.mkdir(parents=True)
            (model / "wflow_sbm.toml").write_text("[time]\nstarttime='2026-01-01'\nendtime='2026-01-02'\n")
            store = CustomToolStore(root, workspace / ".hydrohub" / "custom_tools")
            store.create({**MANIFEST, "id": "sample", "parameters": [{"name": "value", "type": "integer", "required": True}]},
                         "import json,sys\np=json.load(sys.stdin)\nprint('QC diagnostic', file=sys.stderr)\nprint(json.dumps({'status':'ok','value':p['params']['value']}))\n")
            store.save_config("sample", json.dumps({"schema_version": 1, "tool_id": "sample", "parameters": {"value": 9}}))
            runner = AutomationRunner(workspace)
            project = {"root": root, "name": "Project"}
            for mode, expected in (("local", 9), ("global", 4)):
                result = runner._step({"id": mode}, {"tool": "custom.sample", "config_mode": mode, "params": {"value": 4}}, project, model, {})
                self.assertEqual(result["value"], expected)
            script = root / "registered.py"
            script.write_text("def main(request):\n    return {'status': 'ok', 'value': request['params']['value']}\n")
            tools = workspace / ".hydrohub" / "automations" / "tools"
            tools.mkdir(parents=True)
            (tools / "registered.json").write_text(json.dumps({"tool_id": "custom.registered", "type": "custom",
                "executable": {"kind": "python_script", "path": "registered.py", "entry": "main"},
                "input_schema": {"value": "integer"}, "output_schema": {"value": "integer", "status": "string"}}))
            result = runner._step({"id": "registered"}, {"tool": "custom.registered", "params": {"value": 6}}, project, model, {})
            self.assertEqual(result["value"], 6)
            (tools / "cli.json").write_text(json.dumps({"tool_id": "custom.cli", "input_schema": {"value": "integer"}, "executable": {
                "kind": "cli_binary", "path": sys.executable,
                "arguments": ["-c", "import json,sys; p=json.load(sys.stdin); print(json.dumps({'status':'ok','value':p['params']['value']}))"]}}))
            result = runner._step({"id": "cli"}, {"tool": "custom.cli", "params": {"value": 8}}, project, model, {})
            self.assertEqual(result["value"], 8)
            (tools / "http.json").write_text(json.dumps({"tool_id": "custom.http", "executable": {
                "kind": "http_endpoint", "path": "https://example.test/qc"}}))
            with patch("urllib.request.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = b'{"status":"warning","notes":"gap"}'
                result = runner._step({"id": "http"}, {"tool": "custom.http", "params": {}}, project, model, {})
                self.assertEqual(result["status"], "warning")
                self.assertEqual(request.call_args.args[0].get_method(), "POST")

    def test_error_branch_timeout_and_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = AutomationRunner(root)
            nodes = [{"id": "first", "tool": "test", "next": [], "next_on_status": {"error": ["recover"]}},
                     {"id": "recover", "tool": "test", "next": []}]
            def step(job, node, *_args):
                if node["id"] == "first":
                    raise ValueError("forcing unavailable")
                return {"status": "ok"}
            runner._step = step
            spec = {"id": "recovery", "nodes": nodes, "targets": [{"project_id": "p", "model_id": "m"}]}
            jobs = runner.start(spec, {"p": {"models": {"m": root}}})
            deadline = time.monotonic() + 5
            while "recovery" in runner.active and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(jobs[0]["status"], "SUCCESS")
            self.assertEqual(jobs[0]["node_status"]["first"], "Error")
            self.assertEqual(jobs[0]["node_status"]["recover"], "Success")
            with self.assertRaises(TimeoutError):
                runner._command({"id": "timeout"}, [sys.executable, "-c", "import time; time.sleep(5)"], root, timeout=1)
            def cancel(job, *_args):
                runner.cancel(job["id"])
                return {"status": "ok"}
            runner._step = cancel
            jobs = runner.start({**spec, "id": "cancelled"}, {"p": {"models": {"m": root}}})
            deadline = time.monotonic() + 5
            while "cancelled" in runner.active and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(jobs[0]["status"], "CANCELLED")

    def test_builtin_index_uses_shared_ssfi_calculation_in_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model"
            model.mkdir()
            output = root / "output.csv"
            pd.DataFrame({"time": pd.date_range("2000-01-01", periods=800),
                          "river_q_1": [1 + i % 31 for i in range(800)]}).to_csv(output, index=False)
            runner = AutomationRunner(root)
            with patch("hydrohub.core.automation_runner.RunRegistry") as registry:
                registry.return_value.list_runs.return_value = [SimpleNamespace(run_id="run")]
                registry.return_value.output_files.return_value = [output]
                result = runner._step({"id": "index"}, {"tool": "builtin.compute_index", "params": {"timescale": 3}},
                                      {"root": root, "name": "Sample"}, model, {"run_id": "run"})
            frame = pd.read_csv(result["index_path"])
            self.assertIn("1", frame.columns)
            self.assertGreater(len(frame), 20)

    def test_schema_checks_global_values_but_allows_runtime_local_config(self):
        catalog = {"custom.qc": {"type": "custom", "parameters": [], "input_schema": {"path": "string"}}}
        spec = {"id": "qc", "name": "QC", "trigger": {"type": "manual"},
                "targets": [{"project_id": "p", "model_id": "m"}],
                "nodes": [{"id": "run", "tool": "custom.qc", "config_mode": "global", "params": {"path": 3}, "next": []}]}
        projects = {"p": {"models": {"m": Path(".")}}}
        with self.assertRaisesRegex(ValueError, "run input.path must be string"):
            validate_pipeline(yaml.safe_dump({"automation": spec}), catalog, projects)
        spec["nodes"][0]["config_mode"] = "local"
        self.assertEqual(validate_pipeline(yaml.safe_dump({"automation": spec}), catalog, projects)["nodes"][0]["config_mode"], "local")

    def test_scheduler_coalesces_missed_triggers_and_delete_preserves_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scheduler = AutomationScheduler(root)
            scheduler.timer.stop()
            now = datetime(2026, 2, 4, 15, tzinfo=timezone.utc)
            spec = {"id": "daily", "name": "Daily", "trigger": {"type": "schedule", "mode": "interval", "days": 1, "time": "02:00"},
                    "targets": [{"project_id": "p", "model_id": "m"}], "nodes": [{"id": "check", "tool": "builtin.check_last_run_date", "next": []}]}
            scheduler.store.save(yaml.safe_dump({"automation": spec}), {"builtin.check_last_run_date": {"type": "builtin"}},
                                 {"p": {"models": {"m": root}}})
            scheduler.store.update_state("daily", enabled=True, next_due="2026-01-01T02:00:00+00:00")
            with patch("hydrohub.core.automation_scheduler.datetime") as clock, patch.object(scheduler, "start", return_value=[{}]) as start:
                clock.now.return_value = now
                clock.fromisoformat.side_effect = datetime.fromisoformat
                scheduler.tick()
                scheduler.tick()
                self.assertEqual(start.call_count, 1)
            self.assertEqual(scheduler.store.state("daily")["next_due"], "2026-02-05T02:00:00+00:00")
            history = scheduler.store.root / "jobs" / "saved" / "run.log"
            history.parent.mkdir(parents=True)
            history.write_text("keep history")
            with self.assertRaises(ValueError):
                scheduler.store.delete("daily", {"daily"})
            scheduler.store.delete("daily")
            self.assertEqual(history.read_text(), "keep history")
            self.assertEqual(scheduler.store.list(), [])
            scheduler.shutdown()

    def test_trigger_fields_and_yaml_round_trip_preserve_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            page = AutomationPage(Path(folder))
            self.assertTrue(page.day.isHidden())
            self.assertTrue(page.clock.isHidden())
            self.assertEqual(page._trigger_spec(), {"type": "manual"})
            page.trigger.setCurrentText("interval")
            page.day.setValue(90)
            page.clock.setTime(QTime(5, 45))
            self.assertEqual(page._trigger_spec()["days"], 90)
            self.assertEqual(page._trigger_spec()["time"], "05:45")
            page.trigger.setCurrentText("cron")
            page.cron.setText("15 3 * * 1")
            page.clock.setTime(QTime(6, 30))
            self.assertEqual(page.cron.text(), "30 6 * * 1")
            page.cron.setText("0 2,14 * * *")
            self.assertFalse(page.clock.isEnabled())
            page.trigger.setCurrentText("manual")
            self.assertTrue(page.cron.isHidden())
            page.canvas.set_nodes([{"id": "first", "tool": "builtin.compute_index", "config_mode": "global", "params": {"timescale": 3}, "next": []}])
            page._select_node("first")
            page._apply_node()
            page.identifier.setText("sample")
            page.name.setText("Sample")
            page._generate()
            spec = yaml.safe_load(page.editor.toPlainText())["automation"]
            self.assertEqual(spec["nodes"][0]["params"], {"timescale": 3})
            self.assertEqual(spec["trigger"], {"type": "manual"})
            self.assertFalse(page._canvas_dirty)
            page.trigger.setCurrentText("monthly_on_date")
            self.assertTrue(page._canvas_dirty)
            page.close()
            self.assertTrue(page.scheduler.timer.isActive())
            page.scheduler.shutdown()

    def test_native_canvas_drag_connection(self):
        canvas = WorkflowCanvas()
        canvas.resize(900, 300)
        canvas.set_nodes([{"id": "first", "tool": "test", "next": []}, {"id": "last", "tool": "test", "next": []}])
        canvas.show()
        self.app.processEvents()
        start = canvas.mapFromScene(canvas.nodes["first"].pos() + QPointF(230, 41))
        end = canvas.mapFromScene(canvas.nodes["last"].pos() + QPointF(0, 41))
        QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(canvas.nodes["first"].definition["next"], ["last"])
        canvas.close()

    def test_save_uses_changed_schedule_and_preserves_node_parameters(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(root / ".hydrohub" / "project_registry.json")
            project = manager.create(root, "Sample")
            model = project.root / "model"
            model.mkdir()
            (model / "wflow_sbm.toml").write_text("[time]\nstarttime='2026-01-01'\nendtime='2026-01-02'\n")
            page = AutomationPage(root)
            page.name.setText("Sample automation")
            page.identifier.setText("sample")
            page._add_target()
            page.tool.setCurrentText("builtin.compute_index")
            page._add_step()
            page.node_params.setPlainText("timescale: 3")
            page._apply_node()
            page.save()
            self.assertEqual(page.store.list()[0]["trigger"], {"type": "manual"})
            page.trigger.setCurrentText("monthly_on_date")
            page.day.setValue(12)
            page.clock.setTime(QTime(6, 30))
            page.save()
            spec = page.store.list()[0]
            self.assertEqual(spec["trigger"], {"type": "schedule", "mode": "monthly_on_date", "day_of_month": 12, "time": "06:30"})
            self.assertEqual(spec["nodes"][0]["params"], {"timescale": 3})
            page.scheduler.shutdown()
            page.close()


if __name__ == "__main__":
    unittest.main()
