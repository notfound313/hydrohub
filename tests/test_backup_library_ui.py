import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.tool_groups import ToolGroups
from hydrohub.plugins.custom_tools import CustomToolStore, MANIFEST
from hydrohub.ui.main_window import MainWindow


class BackupLibraryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_library_group_assignment_opens_existing_custom_tool_and_nav_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            project = ProjectManager().create(workspace / "projects", "Basin")
            library_root = workspace / ".hydrohub" / "custom_tools"
            store = CustomToolStore(project.root, library_root)
            store.create({**MANIFEST, "id": "river", "name": "River tool"})
            window = MainWindow(workspace)
            try:
                window._set_project(project)
                tabs = window.project_workspace.widget(6)
                library = tabs.widget(1)
                groups = ToolGroups(library_root)
                groups.create("hydropy")
                library.refresh_context()
                library.tree.setCurrentItem(library.tree.topLevelItem(1).child(0))
                library.destination.setCurrentIndex(library.destination.findData("hydropy"))
                library._assign()
                self.assertEqual(groups.load()["assignments"], {"river": "hydropy"})
                library.refresh_context()
                library.filter.setCurrentIndex(library.filter.findData("hydropy"))
                self.assertEqual(library.tree.topLevelItemCount(), 1)
                library.search.setText("unrelated")
                self.assertEqual(library.tree.topLevelItemCount(), 0)
                library.search.setText("river")
                self.assertEqual(library.tree.topLevelItemCount(), 1)
                library.tree.setCurrentItem(library.tree.topLevelItem(0).child(0))
                library._open()
                self.assertEqual(tabs.currentIndex(), 0)
                self.assertEqual(window.custom_tools_page.selected_tool["id"], "river")
                window._navigate(3)
                self.assertIs(window.stack.currentWidget(), window.backup_page)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
