from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from hydrohub.core.automation import AutomationStore, check_schema, discover_projects, next_trigger, validate_pipeline
from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.run_registry import RunRegistry


class AutomationTests(unittest.TestCase):
    def test_pipeline_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp_path = Path(folder)
            model = tmp_path / "model"
            model.mkdir()
            projects = {"p": {"root": tmp_path, "models": {"m": model}}}
            catalog = {"builtin.check_last_run_date": {"type": "builtin"}}
            text = """automation:
  id: daily_run
  name: Daily run
  trigger: {type: schedule, mode: interval, days: 1, time: '02:00'}
  concurrency: {max_parallel_models: 2}
  targets: [{project_id: p, model_id: m}]
  steps: [{id: check, tool: builtin.check_last_run_date}]
"""
            store = AutomationStore(tmp_path)
            self.assertEqual(store.save(text, catalog, projects)["id"], "daily_run")
            self.assertEqual(store.list()[0]["name"], "Daily run")
            store.update_state("daily_run", enabled=True)
            self.assertTrue(store.state("daily_run")["enabled"])
            with self.assertRaisesRegex(ValueError, "Unknown tool"):
                validate_pipeline(text.replace("builtin.check_last_run_date", "builtin.unknown"), catalog, projects)
            with self.assertRaisesRegex(ValueError, "Unknown model_id"):
                validate_pipeline(text.replace("model_id: m", "model_id: missing"), catalog, projects)


    def test_schedule_month_end_interval_and_cron(self):
        now = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
        monthly = {"type": "schedule", "mode": "monthly_on_date", "day_of_month": 31, "time": "02:00"}
        self.assertEqual(next_trigger(monthly, now, now), datetime(2026, 2, 28, 2, tzinfo=timezone.utc))
        interval = {"type": "schedule", "mode": "interval", "days": 2, "time": "02:00"}
        self.assertEqual(next_trigger(interval, now, now), now.replace(day=2, month=2, hour=2))
        cron = {"type": "schedule", "mode": "cron", "expression": "15 3 * * 1"}
        self.assertEqual(next_trigger(cron, now, now), datetime(2026, 2, 2, 3, 15, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "Invalid cron field"):
            next_trigger({**cron, "expression": "61 3 * * 1"}, now, now)

    def test_custom_schema(self):
        check_schema({"period": {"start": "date"}, "count": "integer"},
                     {"period": {"start": "2026-01-01"}, "count": 3}, "input")
        with self.assertRaisesRegex(ValueError, "input.count"):
            check_schema({"count": "integer"}, {"count": True}, "input")

    def test_target_ids_stay_stable_after_registry_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            manager = ProjectManager(workspace / ".hydrohub" / "project_registry.json")
            project = manager.create(workspace, "Project")
            model = project.root / "model"
            model.mkdir()
            (model / "wflow_sbm.toml").write_text("[time]\nstarttime = '2026-01-01'\nendtime = '2026-01-02'\n")
            first = discover_projects(workspace)
            registry = RunRegistry(project.root, model, project.name)
            self.assertEqual(list(first), [registry.project_id])
            self.assertEqual(list(first[registry.project_id]["models"]), [registry.model_id])
            self.assertEqual(discover_projects(workspace), first)


if __name__ == "__main__":
    unittest.main()
