import tempfile
import unittest
from pathlib import Path

from hydrohub.core.file_manager import ProjectFileManager


class ProjectFileManagerTests(unittest.TestCase):
    def test_create_upload_move_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            data = root / "data"
            data.mkdir()
            manager = ProjectFileManager(root)

            folder = manager.create_folder(data, "raw")
            created = manager.create_file(folder, "notes.txt")
            source = Path(temp_dir) / "forcing.nc"
            source.write_text("data", encoding="utf-8")
            uploaded = manager.upload([source], folder)[0]
            moved = manager.move(uploaded, data)
            manager.delete(folder)

            self.assertTrue(created.parent == folder)
            self.assertEqual(moved, data / "forcing.nc")
            self.assertTrue(moved.exists())
            self.assertFalse(folder.exists())

    def test_blocks_paths_outside_project_and_project_root_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            manager = ProjectFileManager(root)
            with self.assertRaises(ValueError):
                manager.create_file(root, "../outside.txt")
            with self.assertRaises(ValueError):
                manager.delete(root)

    def test_upload_collision_does_not_partially_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "project"
            destination = root / "data"
            destination.mkdir(parents=True)
            first_source = base / "one" / "first.txt"
            second_source = base / "two" / "second.txt"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_text("first", encoding="utf-8")
            second_source.write_text("second", encoding="utf-8")
            (destination / "second.txt").write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                ProjectFileManager(root).upload([first_source, second_source], destination)
            self.assertFalse((destination / "first.txt").exists())


if __name__ == "__main__":
    unittest.main()
