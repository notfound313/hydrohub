import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from hydrohub.core.backup import create_backup, inspect_backup, restore_backup
from hydrohub.core.tool_groups import ToolGroups
from hydrohub.plugins.custom_tools import MANIFEST


class BackupLibraryTests(unittest.TestCase):
    def test_groups_survive_rename_and_removal_without_moving_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ToolGroups(Path(temp) / "custom_tools")
            store.create("hydropy")
            store.assign("river", "hydropy")
            store.rename("hydropy", "Hydrology")
            self.assertEqual(ToolGroups(store.path.parent).load()["assignments"], {"river": "Hydrology"})
            store.delete("Hydrology")
            self.assertEqual(store.load(), {"groups": [], "assignments": {}})

    def test_selected_backup_restores_all_categories_and_preserves_unselected_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            project = source / "projects" / "Basin"
            model = project / "models" / "wflow"
            model.mkdir(parents=True)
            (project / "project.yaml").write_text("project:\n  name: Basin\n", encoding="utf-8")
            (project / "notes.txt").write_text("important", encoding="utf-8")
            (model / "wflow_sbm.toml").write_text("[model]\n", encoding="utf-8")
            definition = source / ".hydrohub" / "custom_tools" / "river"
            definition.mkdir(parents=True)
            (definition / "tool.json").write_text(json.dumps({**MANIFEST, "id": "river"}), encoding="utf-8")
            (definition / "main.py").write_text("print('ok')", encoding="utf-8")
            ToolGroups(definition.parent).create("hydropy")
            ToolGroups(definition.parent).assign("river", "hydropy")
            config = project / "workspace" / "custom_tools"
            config.mkdir(parents=True)
            (config / "river.json").write_text('{"parameter_values":{}}', encoding="utf-8")
            automation = source / ".hydrohub" / "automations"
            automation.mkdir(parents=True)
            (automation / "run.yaml").write_text("automation:\n  id: run\n", encoding="utf-8")
            (automation / "run.json").write_text('{"enabled":true}', encoding="utf-8")
            archive = root / "backup.zip"
            create_backup(source, archive, project_root=project, include_project=True,
                          models=[model], tool_ids=["river"], automation_ids=["run"])
            manifest = inspect_backup(archive)
            self.assertEqual({item["category"] for item in manifest["files"]}, {"project", "models", "tools", "automation"})

            destination = root / "restored"
            destination.mkdir()
            other = destination / "untouched.txt"
            other.write_text("leave me", encoding="utf-8")
            restore_backup(destination, archive, categories={"automation"})
            self.assertFalse((destination / "projects" / "Basin" / "project.yaml").exists())
            self.assertEqual((destination / ".hydrohub" / "automations" / "run.json").read_text(), '{"enabled":true}')
            restore_backup(destination, archive, categories={"project"})
            self.assertFalse((destination / "projects" / "Basin" / "models" / "wflow" / "wflow_sbm.toml").exists())
            restore_backup(destination, archive, categories={"tools", "models"})
            self.assertEqual(ToolGroups(destination / ".hydrohub" / "custom_tools").load()["assignments"], {"river": "hydropy"})
            self.assertEqual((destination / "projects" / "Basin" / "notes.txt").read_text(), "important")
            self.assertTrue((destination / "projects" / "Basin" / "models" / "wflow" / "wflow_sbm.toml").exists())
            self.assertEqual(other.read_text(), "leave me")
            with self.assertRaises(FileExistsError):
                restore_backup(destination, archive, categories={"project"})

    def test_rejects_archive_path_traversal_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            entry = "project/../outside.txt"
            manifest = {"format": "hydrohub-backup", "version": 1, "project_name": "Basin",
                        "files": [{"name": entry, "category": "project"}]}
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("manifest.json", json.dumps(manifest))
                handle.writestr(entry, "bad")
            with self.assertRaises(ValueError):
                restore_backup(root, archive, categories={"project"})
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
