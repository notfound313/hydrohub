import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from hydrohub.core.process_manager import ProcessManager, ProcessSlots  # noqa: E402
from hydrohub.plugins.registry import ToolDefinition  # noqa: E402


class ProcessManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_runs_tool_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool = ToolDefinition(
                "hydrohub.test", "Test", "download", "hydrohub.tools.era5_land_pet", "Test", ()
            )
            manager = ProcessManager()
            loop = QEventLoop()
            result = {}

            def completed(exit_code: int, manifest: str) -> None:
                result.update(exit_code=exit_code, manifest=manifest)
                loop.quit()

            manager.finished.connect(completed)
            manager.run_tool(
                tool,
                Path(os.sys.executable),
                root,
                root / "manifests",
                extra_environment={"HYDROHUB_TEST_SECRET": "must-not-be-recorded"},
                arguments=[
                    "--start-date", "2026-01-01", "--end-date", "2026-01-01",
                    "--west", "104.5", "--south", "-9.5", "--east", "115.3", "--north", "-5",
                    "--output-dir", str(root / "output"), "--dry-run",
                ],
            )
            QTimer.singleShot(10_000, loop.quit)
            loop.exec()

            self.assertEqual(result.get("exit_code"), 0)
            manifest_path = Path(result["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tool_id"], "hydrohub.test")
            self.assertEqual(manifest["status"], "SUCCESS")
            self.assertEqual(manifest["module"], "hydrohub.tools.era5_land_pet")
            self.assertEqual(manifest["tool_name"], "Test")
            self.assertGreaterEqual(manifest["duration_seconds"], 0)
            self.assertEqual(manifest["exit_code"], 0)
            self.assertIn("summary", manifest)
            self.assertIn("outputs", manifest)
            self.assertIn("warnings", manifest)
            self.assertIn("errors", manifest)
            self.assertNotIn("must-not-be-recorded", manifest_path.read_text(encoding="utf-8"))
            self.assertIn("Dry run complete", Path(manifest["log_file"]).read_text())

    def test_runs_direct_executable_and_preserves_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool = ToolDefinition(
                "hydromt.test", "HydroMT Test", "model", "hydromt", "Test", ()
            )
            manager = ProcessManager()
            loop = QEventLoop()
            result = {}

            def completed(exit_code: int, manifest: str) -> None:
                result.update(exit_code=exit_code, manifest=manifest)
                loop.quit()

            arguments = ["-c", "print('direct command complete')"]
            manager.finished.connect(completed)
            manager.run_command(
                tool,
                Path(os.sys.executable),
                arguments,
                root,
                root / "manifests",
            )
            QTimer.singleShot(10_000, loop.quit)
            loop.exec()

            self.assertEqual(result.get("exit_code"), 0)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["parameters"], arguments)
            self.assertEqual(manifest["command"][1:], arguments)
            self.assertIn("direct command complete", Path(manifest["log_file"]).read_text())

    def test_three_processes_share_slots_and_release_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            slots = ProcessSlots()
            tool = ToolDefinition("test.parallel", "Parallel", "model", "test", "Test", ())
            managers = [ProcessManager(slots=slots, project_root=root / str(i)) for i in range(4)]
            for index, manager in enumerate(managers[:3]):
                manager.run_command(
                    tool, Path(os.sys.executable), ["-c", "import time; time.sleep(30)"],
                    root / str(index), root / str(index) / "manifests",
                )
            self.assertEqual(len(slots.active), 3)
            self.assertTrue(all(manager.is_running for manager in managers[:3]))
            with self.assertRaisesRegex(RuntimeError, "Maximum 3 processes"):
                managers[3].run_command(
                    tool, Path(os.sys.executable), ["-c", "print('fourth')"],
                    root / "3", root / "3" / "manifests",
                )
            self.assertFalse(managers[3].is_running)
            for manager in managers[:3]:
                manager.stop()
            deadline = time.monotonic() + 10
            while slots.active and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(slots.active)
            managers[3].run_command(
                tool, Path(os.sys.executable), ["-c", "print('fourth')"],
                root / "3", root / "3" / "manifests",
            )
            deadline = time.monotonic() + 10
            while slots.active and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.assertFalse(slots.active)


if __name__ == "__main__":
    unittest.main()
