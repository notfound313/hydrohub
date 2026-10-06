import json
import os
import sys
import tempfile
import time
import unittest
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QComboBox
from hydrohub.plugins.custom_tools import (
    CustomToolStore,
    MANIFEST,
    SecretStore,
    export_template,
    validate_manifest,
)
from hydrohub.core.custom_tool_sessions import CustomToolSessionHub
from hydrohub.core.tool_groups import ToolGroups
from hydrohub.core.project_manager import ProjectContext
from hydrohub.plugins.registry import ToolDefinition
from hydrohub.core.process_manager import ProcessManager
from hydrohub.ui.background import BackgroundJob
from hydrohub.ui.custom_tools import CustomToolsPage
from hydrohub.ui.tool_library_page import ToolLibraryPage
from hydrohub.ui.theme import theme_manager


def build_test_wheel(directory: Path) -> Path:
    wheel = directory / "hydrohub_test_dependency-0.0.1-py3-none-any.whl"
    dist_info = "hydrohub_test_dependency-0.0.1.dist-info"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("hydrohub_test_dependency/__init__.py", "VALUE = 'installed'\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: hydrohub-test-dependency\nVersion: 0.0.1\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: HydroHub tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            "hydrohub_test_dependency/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n",
        )
    return wheel


class CustomToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_template_install_and_run_async(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / 'template.zip'; export_template(package)
            store = CustomToolStore(root); store.install(package)
            tools, errors = store.discover()
            self.assertEqual(errors, [])
            self.assertEqual(len(tools), 1)
            self.assertEqual(
                {field["name"] for field in tools[0]["parameters"] if field["name"] in {"west", "south", "east", "north"}},
                {"west", "south", "east", "north"},
            )
            self.assertIn("aoi_path", {field["name"] for field in tools[0]["parameters"]})
            self.assertEqual(CustomToolStore(root / 'other').discover()[0], [])
            with self.assertRaises(FileExistsError): store.install(package)
            source = root / 'input.txt'; source.write_text('test')
            output = root / 'report.json'
            manager = ProcessManager(); done = []; lines = []
            manager.finished.connect(lambda code, path: done.append((code, path)))
            manager.output_received.connect(lines.append)
            tool = ToolDefinition('custom.inspect_data', 'Inspect', 'custom', 'main.py', '', ())
            manager.run_command(tool, Path(sys.executable), ['-u', str(tools[0]['path'] / 'main.py'), '--input', str(source), '--output', str(output), '--west', '106', '--south', '-8', '--east', '112', '--north', '-5'], root, root / 'manifests')
            self.wait_for(lambda: bool(done))
            self.assertEqual(done[0][0], 0)
            self.assertEqual(json.loads(output.read_text())['size_bytes'], 4)
            self.assertIn('[WRITE]', ''.join(lines))

    def test_rejects_zip_path_traversal_before_extracting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); package = root / 'bad.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('tool.json', json.dumps(MANIFEST))
                archive.writestr('../escaped.py', 'pass')
            with self.assertRaises(ValueError): CustomToolStore(root).install(package)
            self.assertFalse((root / 'scripts' / 'escaped.py').exists())

    def test_worker_returns_to_main_thread_for_success_and_failure(self):
        received = []
        job = BackgroundJob(self.app, lambda: QThread.currentThread(), lambda thread: received.append((thread, QThread.currentThread())), self.fail)
        job.start(); self.wait_for(lambda: bool(received))
        self.assertNotEqual(received[0][0], self.app.thread())
        self.assertEqual(received[0][1], self.app.thread())
        failed = []
        def fail(): raise ValueError('worker failure')
        job2 = BackgroundJob(self.app, fail, self.fail, lambda message: failed.append((message, QThread.currentThread())))
        job2.start(); self.wait_for(lambda: bool(failed))
        self.assertEqual(failed[0], ('worker failure', self.app.thread()))

    def test_fluent_themes_keep_terminal_dark(self):
        manager = theme_manager(); original = manager.preference
        try:
            for mode in ('light', 'dark'):
                manager.set_preference(mode)
                self.assertEqual(self.app.property('themeMode'), mode)
                self.assertIn('#111214', self.app.styleSheet())
        finally:
            manager.set_preference(original)

    def test_reusable_definition_has_isolated_project_config_and_secrets(self):
        class FakeKeyring:
            values = {}

            @classmethod
            def get_password(cls, service, account):
                return cls.values.get((service, account))

            @classmethod
            def set_password(cls, service, account, value):
                cls.values[(service, account)] = value

            @classmethod
            def delete_password(cls, service, account):
                cls.values.pop((service, account), None)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "app" / "custom_tools"
            project_a, project_b = root / "a", root / "b"
            manifest = dict(MANIFEST)
            manifest.update(
                id="shared",
                name="Shared",
                parameters=[{"name": "value", "type": "text"}],
                environment=[{"name": "API_TOKEN", "secret": True, "required": True}],
            )
            store_a = CustomToolStore(project_a, library)
            store_a.create(manifest, "print('ok')\n")
            store_b = CustomToolStore(project_b, library)
            self.assertEqual(store_b.discover()[0][0]["id"], "shared")

            store_a.save_config("shared", {"parameters": {"value": "A"}, "secrets": ["API_TOKEN"]})
            store_b.save_config("shared", {"parameters": {"value": "B"}, "secrets": ["API_TOKEN"]})
            self.assertEqual(store_a.load_config("shared")["parameters"]["value"], "A")
            self.assertEqual(store_b.load_config("shared")["parameters"]["value"], "B")

            secrets = SecretStore(FakeKeyring)
            secrets.set(project_a, "shared", "API_TOKEN", "alpha")
            secrets.set(project_b, "shared", "API_TOKEN", "beta")
            self.assertEqual(secrets.get(project_a, "shared", "API_TOKEN"), "alpha")
            self.assertEqual(secrets.get(project_b, "shared", "API_TOKEN"), "beta")
            self.assertNotIn("alpha", (project_a / "workspace" / "custom_tools" / "shared.json").read_text())

    def test_invalid_definition_does_not_replace_valid_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CustomToolStore(root, root / "library")
            manifest = dict(MANIFEST, id="stable", name="Stable")
            tool = store.create(manifest, "print('stable')\n")
            original_manifest = (tool["path"] / "tool.json").read_text()
            original_script = (tool["path"] / "main.py").read_text()
            with self.assertRaises(SyntaxError):
                store.save_definition("stable", json.dumps(manifest), "def broken(:\n")
            self.assertEqual((tool["path"] / "tool.json").read_text(), original_manifest)
            self.assertEqual((tool["path"] / "main.py").read_text(), original_script)
            with self.assertRaises(ValueError):
                validate_manifest({**manifest, "environment": [{"name": "TOKEN", "secret": True, "default": "leak"}]})

    def test_schema_one_manifest_remains_supported(self):
        legacy = {
            "schema_version": 1,
            "id": "legacy",
            "name": "Legacy",
            "scope": "both",
            "entrypoint": "main.py",
            "parameters": [{"name": "input", "type": "file"}],
        }
        validated = validate_manifest(legacy)
        self.assertEqual(validated["schema_version"], 1)
        self.assertEqual(validated["environment"], [])
        self.assertEqual(validated["dependencies"], [])

    def test_manifest_scope_is_optional(self):
        self.assertNotIn("scope", MANIFEST)
        for version in (1, 2):
            manifest = dict(MANIFEST, schema_version=version, parameters=[])
            with self.subTest(version=version):
                validated = validate_manifest(manifest)
                self.assertNotIn("scope", validated)
                self.assertEqual(validated["parameters"], [])
            for scope in ("input", "output", "both", "custom", "", None, [], {}):
                with self.subTest(version=version, scope=scope):
                    self.assertEqual(validate_manifest(dict(manifest, scope=scope))["scope"], scope)

    def test_select_manifest_supports_options_and_choices(self):
        options = [
            {"label": "AOI Bounds", "value": "bounds"},
            {"label": "AOI Geometry", "value": "geometry"},
        ]
        manifest = dict(MANIFEST, id="select_tool", parameters=[
            {"name": "aoi_mode", "label": "AOI mode", "type": "select", "options": options, "default": "bounds"},
        ])
        self.assertEqual(validate_manifest(manifest)["parameters"][0]["options"], options)
        choices = dict(manifest, id="choice_tool", parameters=[
            {"name": "mode", "type": "select", "choices": ["fast", "safe"], "default": "safe"},
        ])
        self.assertEqual(validate_manifest(choices)["parameters"][0]["choices"], ["fast", "safe"])
        with self.assertRaises(ValueError):
            validate_manifest(dict(manifest, id="bad_tool", parameters=[
                {"name": "mode", "type": "select", "options": options, "choices": ["bounds"]},
            ]))

    def test_dependency_manifest_validation(self):
        validated = validate_manifest(
            {**MANIFEST, "dependencies": ["requests>=2.32", "requests>=2.32"]}
        )
        self.assertEqual(validated["dependencies"], ["requests>=2.32"])
        for dependency in ("--index-url=https://example.test", "demo @ https://example.test/demo.whl"):
            with self.subTest(dependency=dependency), self.assertRaises(ValueError):
                validate_manifest({**MANIFEST, "dependencies": [dependency]})

    def test_parallel_sessions_are_isolated_and_create_unique_manifests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ProjectContext("parallel", root)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            script = tool_dir / "main.py"
            script.write_text(
                "import argparse,time\n"
                "p=argparse.ArgumentParser();p.add_argument('--tag');p.add_argument('--delay',type=float);a=p.parse_args()\n"
                "print(a.tag,flush=True);time.sleep(a.delay);print('[OK] '+a.tag,flush=True)\n"
            )
            tool = {"id": "parallel", "name": "Parallel", "path": tool_dir, "entrypoint": "main.py"}
            hub = CustomToolSessionHub()
            first = hub.start(tool, project, Path(sys.executable), ["-u", str(script), "--tag", "first", "--delay", "2"], {})
            second = hub.start(tool, project, Path(sys.executable), ["-u", str(script), "--tag", "second", "--delay", "0.2"], {})
            self.wait_for(lambda: first.manager.is_running and second.manager.is_running)
            hub.stop(first.id)
            self.wait_for(lambda: first.exit_code is not None and second.exit_code is not None)
            self.assertEqual(second.exit_code, 0)
            self.assertIn("second", second.output)
            self.assertNotIn("second", first.output)
            self.assertNotEqual(first.manifest_path, second.manifest_path)
            self.assertTrue(Path(first.manifest_path).is_file())
            self.assertTrue(Path(second.manifest_path).is_file())

    def test_restart_stops_active_session_and_repeats_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ProjectContext("restart", root)
            tool_dir = root / "tool"
            tool_dir.mkdir()
            script = tool_dir / "main.py"
            script.write_text(
                "import argparse,time\n"
                "p=argparse.ArgumentParser();p.add_argument('--tag');a=p.parse_args()\n"
                "print(a.tag,flush=True);time.sleep(0.2);print('[OK] '+a.tag,flush=True)\n"
            )
            tool = {"id": "restart", "name": "Restart", "path": tool_dir, "entrypoint": "main.py"}
            hub = CustomToolSessionHub()
            added = []
            hub.session_added.connect(added.append)
            first = hub.start(tool, project, Path(sys.executable), ["-u", str(script), "--tag", "same"], {})
            self.wait_for(lambda: first.manager.is_running)

            hub.restart(first.id)

            self.wait_for(lambda: len(added) == 2)
            second = hub.sessions[added[1]]
            self.wait_for(lambda: second.exit_code is not None)
            self.assertFalse(first.is_running)
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(second.arguments, first.arguments)
            self.assertIn("[OK] same", second.output)

    def test_custom_tools_page_loads_reusable_tool_and_project_config(self):
        class FakeKeyring:
            @staticmethod
            def get_password(_service, _account):
                return None

            @staticmethod
            def set_password(_service, _account, _value):
                pass

            @staticmethod
            def delete_password(_service, _account):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ProjectContext("ui", root / "project")
            library = root / "library"
            manifest = dict(MANIFEST, id="editor", name="Editor", parameters=[
                {
                    "name": "aoi_mode",
                    "label": "AOI mode",
                    "type": "select",
                    "options": [
                        {"label": "AOI Bounds", "value": "bounds"},
                        {"label": "AOI Geometry", "value": "geometry"},
                    ],
                    "default": "bounds",
                },
            ], environment=[])
            store = CustomToolStore(project.root, library)
            store.create(manifest, "print('editor')\n")
            store.save_config("editor", {"parameters": {"aoi_mode": "geometry"}, "environment": {"MODE": "test"}})
            page = CustomToolsPage(
                lambda: project,
                CustomToolSessionHub(),
                library,
                SecretStore(FakeKeyring),
            )
            self.assertEqual(page.selected_tool["id"], "editor")
            self.assertIsInstance(page.parameter_widgets["aoi_mode"], QComboBox)
            self.assertEqual(page.parameter_widgets["aoi_mode"].currentData(), "geometry")
            self.assertTrue(page._save_config(from_runtime=True))
            arguments, _environment, _interpreter = page._execution()
            self.assertEqual(arguments[-2:], ["--aoi_mode", "geometry"])
            self.assertIn('"MODE": "test"', page.config_editor.toPlainText())
            page.config_editor.setPlainText(
                json.dumps({"parameters": {}, "environment": {"MODE": "edited"}})
            )
            self.assertTrue(page._save_config())
            self.assertEqual(store.load_config("editor")["environment"]["MODE"], "edited")
            self.assertFalse(page.has_unsaved_changes)
            page.deleteLater()

    def test_library_group_filter_preserves_selection_and_unsaved_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ProjectContext("ui", root / "project")
            library = root / ".hydrohub" / "custom_tools"
            store = CustomToolStore(project.root, library)
            long_name = "Long hydrology tool name " * 12
            for tool_id, name in (("alpha", "Alpha"), ("beta", long_name), ("loose", "Loose")):
                store.create(dict(MANIFEST, id=tool_id, name=name, parameters=[], environment=[]),
                             f"print('{tool_id}')\n")
            groups = ToolGroups(library)
            for group in ("First", "Second", "Empty"):
                groups.create(group)
            groups.assign("alpha", "First")
            groups.assign("beta", "Second")
            page = CustomToolsPage(lambda: project, CustomToolSessionHub(), library)
            self.assertTrue(page.select_tool("alpha"))
            page.script_editor.insertPlainText("# unsaved\n")
            edited = page.script_editor.toPlainText()
            page.library_filter.setCurrentIndex(page.library_filter.findData("First"))
            self.assertEqual(page.select.count(), 1)
            self.assertEqual(page.script_editor.toPlainText(), edited)
            self.assertTrue(page.has_unsaved_changes)
            with patch.object(page, "_confirm_discard", return_value=False):
                page.library_filter.setCurrentIndex(page.library_filter.findData("Second"))
            self.assertEqual(page.library_filter.currentData(), "First")
            self.assertEqual(page.selected_tool["id"], "alpha")
            self.assertEqual(page.script_editor.toPlainText(), edited)
            with patch.object(page, "_confirm_discard", return_value=True):
                page.library_filter.setCurrentIndex(page.library_filter.findData("Second"))
            self.assertEqual(page.selected_tool["id"], "beta")
            self.assertEqual(page.script_editor.toPlainText(), "print('beta')\n")
            self.assertFalse(page.has_unsaved_changes)
            page.library_filter.setCurrentIndex(page.library_filter.findData(""))
            self.assertEqual(page.select.count(), 1)
            self.assertEqual(page.selected_tool["id"], "loose")
            page.library_filter.setCurrentIndex(page.library_filter.findData("Empty"))
            self.assertEqual(page.select.count(), 0)
            self.assertIsNone(page.selected_tool)
            self.assertFalse(page.run_button.isEnabled())
            self.assertTrue(page.select_tool("beta"))
            self.assertIsNone(page.library_filter.currentData())
            browser = ToolLibraryPage(lambda: project, root)
            browser.resize(500, 400)
            browser.show()
            self.app.processEvents()
            self.assertGreaterEqual(browser.tree.columnWidth(0), browser.tree.sizeHintForColumn(0) + 24)
            self.assertGreater(browser.tree.horizontalScrollBar().maximum(), 0)
            browser.close()
            browser.deleteLater()
            page.deleteLater()

    def test_custom_tool_bbox_defaults_to_project_aoi(self):
        class FakeKeyring:
            @staticmethod
            def get_password(_service, _account):
                return None

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ProjectContext("bbox", root / "project")
            workspace = project.root / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "aoi.geojson").write_text(json.dumps({
                "type": "Polygon",
                "coordinates": [[[106, -8], [112, -8], [112, -5], [106, -5], [106, -8]]],
            }))
            library = root / "library"
            parameters = [
                {"name": name, "label": name.title(), "type": "number", "default": 0}
                for name in ("west", "south", "east", "north")
            ]
            parameters.append({"name": "aoi_path", "label": "AOI GeoJSON", "type": "file"})
            store = CustomToolStore(project.root, library)
            store.create(
                dict(MANIFEST, id="bbox_tool", name="BBOX Tool", parameters=parameters),
                "print('bbox')\n",
            )
            store.save_config("bbox_tool", {
                "parameters": {name: "0" for name in ("west", "south", "east", "north")},
                "environment": {},
            })

            page = CustomToolsPage(
                lambda: project,
                CustomToolSessionHub(),
                library,
                SecretStore(FakeKeyring),
            )

            self.assertEqual(
                tuple(page.parameter_widgets[name].text() for name in ("west", "south", "east", "north")),
                ("106.0", "-8.0", "112.0", "-5.0"),
            )
            self.assertEqual(page.parameter_widgets["aoi_path"].text(), str(workspace / "aoi.geojson"))
            self.assertTrue(page._save_config(from_runtime=True))
            arguments, _environment, _interpreter = page._execution()
            self.assertIn("--aoi_path", arguments)
            self.assertIn(str(workspace / "aoi.geojson"), arguments)
            page.set_aoi_bounds((107.0, -7.0, 113.0, -4.0))
            self.assertEqual(
                tuple(page.parameter_widgets[name].text() for name in ("west", "south", "east", "north")),
                ("107.0", "-7.0", "113.0", "-4.0"),
            )
            page.deleteLater()

    def test_dependency_install_is_approved_once_and_serialized_per_interpreter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            links = root / "wheels"
            links.mkdir()
            build_test_wheel(links)
            environment = root / "environment"
            venv.EnvBuilder(with_pip=True).create(environment)
            interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            project = ProjectContext("dependencies", root / "project")
            tool_dir = root / "tool"
            tool_dir.mkdir()
            script = tool_dir / "main.py"
            script.write_text(
                "import hydrohub_test_dependency,time\n"
                "print(hydrohub_test_dependency.VALUE,flush=True)\n"
                "time.sleep(0.2)\n"
                "print('[OK] dependency run',flush=True)\n"
            )
            tool = {
                "id": "dependency_test",
                "name": "Dependency test",
                "path": tool_dir,
                "entrypoint": "main.py",
                "dependencies": ["hydrohub-test-dependency==0.0.1"],
            }
            hub = CustomToolSessionHub()
            approvals = []
            hub.dependency_install_required.connect(
                lambda session_id, packages, _interpreter: approvals.append((session_id, packages))
            )
            old_no_index, old_find_links = os.environ.get("PIP_NO_INDEX"), os.environ.get("PIP_FIND_LINKS")
            os.environ["PIP_NO_INDEX"] = "1"
            os.environ["PIP_FIND_LINKS"] = str(links)
            try:
                first = hub.start(tool, project, interpreter, ["-u", str(script)], {})
                second = hub.start(tool, project, interpreter, ["-u", str(script)], {})
                self.wait_for(lambda: first.status == "AWAITING_APPROVAL")
                self.assertEqual(second.status, "WAITING_FOR_ENV")
                plain = hub.start(
                    {**tool, "id": "plain", "name": "Plain", "dependencies": []},
                    project,
                    interpreter,
                    ["-u", str(script)],
                    {},
                )
                self.assertEqual(plain.status, "WAITING_FOR_ENV")
                self.assertEqual(len(approvals), 1)
                self.assertIn("hydrohub-test-dependency==0.0.1", approvals[0][1])
                hub.approve_install(first.id)
                self.wait_for(
                    lambda: first.exit_code is not None
                    and second.exit_code is not None
                    and plain.exit_code is not None
                )
            finally:
                if old_no_index is None:
                    os.environ.pop("PIP_NO_INDEX", None)
                else:
                    os.environ["PIP_NO_INDEX"] = old_no_index
                if old_find_links is None:
                    os.environ.pop("PIP_FIND_LINKS", None)
                else:
                    os.environ["PIP_FIND_LINKS"] = old_find_links
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(plain.exit_code, 0)
            self.assertEqual(len(approvals), 1)
            self.assertIn("installed", first.output)
            self.assertIn("Requirements already satisfied", second.output)

    def test_dependency_install_can_be_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            links = root / "wheels"
            links.mkdir()
            build_test_wheel(links)
            environment = root / "environment"
            venv.EnvBuilder(with_pip=True).create(environment)
            interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            project = ProjectContext("reject", root / "project")
            tool_dir = root / "tool"
            tool_dir.mkdir()
            script = tool_dir / "main.py"
            script.write_text("raise AssertionError('must not run')\n")
            tool = {
                "id": "reject_dependency",
                "name": "Reject dependency",
                "path": tool_dir,
                "entrypoint": "main.py",
                "dependencies": ["hydrohub-test-dependency==0.0.1"],
            }
            hub = CustomToolSessionHub()
            old_no_index, old_find_links = os.environ.get("PIP_NO_INDEX"), os.environ.get("PIP_FIND_LINKS")
            os.environ["PIP_NO_INDEX"] = "1"
            os.environ["PIP_FIND_LINKS"] = str(links)
            try:
                session = hub.start(tool, project, interpreter, ["-u", str(script)], {})
                self.wait_for(lambda: session.status == "AWAITING_APPROVAL")
                hub.reject_install(session.id)
                self.wait_for(lambda: session.exit_code is not None)
            finally:
                if old_no_index is None:
                    os.environ.pop("PIP_NO_INDEX", None)
                else:
                    os.environ["PIP_NO_INDEX"] = old_no_index
                if old_find_links is None:
                    os.environ.pop("PIP_FIND_LINKS", None)
                else:
                    os.environ["PIP_FIND_LINKS"] = old_find_links
            self.assertEqual(session.status, "CANCELLED")
            self.assertNotIn("must not run", session.output)

    def test_dependency_check_failure_does_not_run_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            links = root / "empty"
            links.mkdir()
            project = ProjectContext("failure", root / "project")
            tool_dir = root / "tool"
            tool_dir.mkdir()
            script = tool_dir / "main.py"
            script.write_text("raise AssertionError('must not run')\n")
            tool = {
                "id": "missing_dependency",
                "name": "Missing dependency",
                "path": tool_dir,
                "entrypoint": "main.py",
                "dependencies": ["hydrohub-package-that-does-not-exist==0.0.1"],
            }
            hub = CustomToolSessionHub()
            old_no_index, old_find_links = os.environ.get("PIP_NO_INDEX"), os.environ.get("PIP_FIND_LINKS")
            os.environ["PIP_NO_INDEX"] = "1"
            os.environ["PIP_FIND_LINKS"] = str(links)
            try:
                session = hub.start(tool, project, Path(sys.executable), ["-u", str(script)], {})
                self.wait_for(lambda: session.exit_code is not None)
            finally:
                if old_no_index is None:
                    os.environ.pop("PIP_NO_INDEX", None)
                else:
                    os.environ["PIP_NO_INDEX"] = old_no_index
                if old_find_links is None:
                    os.environ.pop("PIP_FIND_LINKS", None)
                else:
                    os.environ["PIP_FIND_LINKS"] = old_find_links
            self.assertEqual(session.status, "ERROR")
            self.assertNotIn("must not run", session.output)
