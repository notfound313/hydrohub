import json
import tempfile
import unittest
from pathlib import Path

from hydrohub.core.project_manager import PROJECT_DIRS, ProjectManager


class ProjectManagerTests(unittest.TestCase):
    def test_create_and_open_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ProjectManager()
            created = manager.create(Path(temp_dir), "Solo Basin", "Test project")

            self.assertEqual(created.name, "Solo Basin")
            self.assertTrue((created.root / "project.yaml").exists())
            for relative in PROJECT_DIRS:
                self.assertTrue((created.root / relative).is_dir(), relative)

            setup = json.loads(
                (created.root / "workspace" / "setup_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(setup["current_step"], 1)
            self.assertEqual(manager.open(created.root), created)

    def test_rejects_reserved_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                ProjectManager().create(Path(temp_dir), "Solo/Basin")

    def test_persists_project_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "app" / "project_registry.json"
            manager = ProjectManager(registry)
            project = manager.create(root / "projects", "Citarum")

            entries = manager.list_registered()
            self.assertIn(str(project.root), entries)
            self.assertEqual(entries[str(project.root)]["name"], "Citarum")


if __name__ == "__main__":
    unittest.main()
