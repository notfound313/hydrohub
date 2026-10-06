import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from hydrohub.ui.automation_page import AutomationPage


class AutomationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_workspace_builder_and_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            page = AutomationPage(Path(folder))
            self.assertEqual([page.tabs.tabText(i) for i in range(page.tabs.count())],
                             ["Automations", "Builder / YAML", "Run monitor", "Tool registry"])
            page.name.setText("Sample")
            page.identifier.setText("sample")
            page._generate()
            self.assertIn("id: sample", page.editor.toPlainText())
            self.assertEqual(page.jobs.rowCount(), 0)
            page.close()


if __name__ == "__main__":
    unittest.main()
