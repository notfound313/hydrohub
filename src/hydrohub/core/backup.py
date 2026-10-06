"""Selective workspace backup with validated, staged ZIP restore."""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from hydrohub.core.project_manager import ProjectManager
from hydrohub.core.tool_groups import ToolGroups
from hydrohub.plugins.custom_tools import CustomToolStore


CATEGORIES = {"project", "models", "tools", "automation"}


def _files(root: Path):
    if root.is_symlink():
        raise ValueError(f"Cannot back up symbolic link: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Cannot back up symbolic link: {path}")
        if path.is_file():
            yield path


def create_backup(
    workspace: Path, destination: Path, *, project_root: Path | None = None,
    include_project: bool = False, models=(), tool_ids=(), automation_ids=(),
) -> dict:
    workspace, destination = Path(workspace).resolve(), Path(destination).resolve()
    project_root = Path(project_root).resolve() if project_root else None
    if (include_project or models) and (not project_root or not (project_root / "project.yaml").is_file()):
        raise ValueError("Choose an existing project for project and model backup")
    if project_root and destination.is_relative_to(project_root):
        raise ValueError("Save backup outside the project being backed up")
    sources: dict[str, tuple[Path | bytes, str]] = {}

    def add(name: str, source: Path | bytes, category: str) -> None:
        if name in sources:
            raise ValueError(f"Duplicate backup entry: {name}")
        sources[name] = source, category

    if include_project:
        for path in _files(project_root):
            relative = path.relative_to(project_root)
            if relative.parts[0] == "models" or relative.parts[:2] in (("workspace", "custom_tools"), ("scripts", "custom_tools")):
                continue
            add(f"project/{relative.as_posix()}", path, "project")

    for model in models:
        model = Path(model).resolve()
        if not model.is_dir() or not (model / "wflow_sbm.toml").is_file() or not model.is_relative_to(project_root / "models"):
            raise ValueError(f"Invalid model directory: {model}")
        for path in _files(model):
            add(f"models/{path.relative_to(project_root / 'models').as_posix()}", path, "models")

    library = workspace / ".hydrohub" / "custom_tools"
    groups = ToolGroups(library).load()
    available = {tool["id"]: tool for tool in CustomToolStore(project_root or workspace, library).discover()[0]}
    for tool_id in dict.fromkeys(tool_ids):
        if not isinstance(tool_id, str) or not tool_id or "/" in tool_id or "\\" in tool_id or ":" in tool_id or tool_id in {".", ".."}:
            raise ValueError("Invalid tool ID")
        if tool_id not in available:
            raise ValueError(f"Tool not found: {tool_id}")
        definition = available[tool_id]["path"]
        kind = available[tool_id]["source"]
        for path in _files(definition):
            add(f"tools/{kind}/{tool_id}/{path.relative_to(definition).as_posix()}", path, "tools")
        if project_root:
            config = project_root / "workspace" / "custom_tools" / f"{tool_id}.json"
            if config.is_file():
                add(f"tools/config/{config.name}", config, "tools")
    if tool_ids:
        selected = set(tool_ids)
        assignments = {
            tool: group for tool, group in groups["assignments"].items() if tool in selected
        }
        selected_groups = {"groups": [group for group in groups["groups"] if group in assignments.values()],
                           "assignments": assignments}
        add("tools/groups.json", json.dumps(selected_groups).encode("utf-8"), "tools")

    automations = workspace / ".hydrohub" / "automations"
    for automation_id in dict.fromkeys(automation_ids):
        if not isinstance(automation_id, str) or not automation_id or "/" in automation_id or "\\" in automation_id or ":" in automation_id or automation_id in {".", ".."}:
            raise ValueError("Invalid automation ID")
        definition = automations / f"{automation_id}.yaml"
        if not definition.is_file():
            raise ValueError(f"Automation not found: {automation_id}")
        add(f"automation/{definition.name}", definition, "automation")
        state = definition.with_suffix(".json")
        if state.is_file():
            add(f"automation/{state.name}", state, "automation")

    if not sources:
        raise ValueError("Select at least one item to back up")
    manifest = {"format": "hydrohub-backup", "version": 1,
                "project_name": project_root.name if project_root else None,
                "files": [{"name": name, "category": category} for name, (_, category) in sources.items()]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".zip", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, (source, _) in sources.items():
                if isinstance(source, bytes):
                    archive.writestr(name, source)
                else:
                    archive.write(source, name)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return manifest


def inspect_backup(archive_path: Path) -> dict:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
                raise ValueError("Backup manifest is too large")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "hydrohub-backup" or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
                raise ValueError("Unsupported backup format")
            if any(not isinstance(entry, dict) for entry in manifest["files"]):
                raise ValueError("Invalid backup manifest entries")
            names = [entry.get("name") for entry in manifest["files"]]
            if any(not isinstance(name, str) for name in names):
                raise ValueError("Invalid backup file name")
            if len(names) != len(set(names)) or set(archive.namelist()) != set(names) | {"manifest.json"} or len(archive.namelist()) != len(names) + 1:
                raise ValueError("Backup manifest does not match archive contents")
            for entry in manifest["files"]:
                name, category = entry["name"], entry["category"]
                path = PurePosixPath(name)
                if (category not in CATEGORIES or not isinstance(name, str) or path.is_absolute()
                    or "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in path.parts)
                    or path.as_posix() != name or len(path.parts) < 2 or path.parts[0] != category
                    or (archive.getinfo(name).external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError(f"Invalid backup entry: {name}")
                if category == "project" and (path.parts[1] == "models" or path.parts[1:3] in (("workspace", "custom_tools"), ("scripts", "custom_tools"))):
                    raise ValueError(f"Invalid project backup entry: {name}")
                if category == "automation" and (len(path.parts) != 2 or path.suffix not in {".yaml", ".json"}):
                    raise ValueError(f"Invalid automation backup entry: {name}")
                if category == "tools" and (path.parts[1] not in {"library", "legacy", "config", "groups.json"}
                                            or path.parts[1] in {"library", "legacy"} and len(path.parts) < 4
                                            or path.parts[1] == "config" and (len(path.parts) != 3 or path.suffix != ".json")
                                            or path.parts[1] == "groups.json" and len(path.parts) != 2):
                    raise ValueError(f"Invalid tool backup entry: {name}")
            return manifest
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid backup archive: {exc}") from exc


def restore_backup(workspace: Path, archive_path: Path, *, categories: set[str],
                   project_root: Path | None = None, overwrite: bool = False) -> list[Path]:
    workspace = Path(workspace).resolve()
    manifest = inspect_backup(archive_path)
    if not categories or not categories.issubset(CATEGORIES):
        raise ValueError("Choose valid categories to restore")
    entries = [entry for entry in manifest["files"] if entry["category"] in categories]
    if not entries:
        raise ValueError("Selected categories contain no files")
    project_name = manifest.get("project_name")
    if not project_root and project_name:
        if not isinstance(project_name, str) or project_name in {".", ".."} or any(char in project_name for char in '/\\:<>|?*'):
            raise ValueError("Invalid project name in backup")
        project_root = workspace / "projects" / project_name
    project_root = Path(project_root).resolve() if project_root else None
    if any(entry["category"] in {"project", "models"} or entry["name"].startswith(("tools/config/", "tools/legacy/")) for entry in entries) and not project_root:
        raise ValueError("Select a project to restore project files or tool configurations")
    if (project_root and "project" not in categories and not (project_root / "project.yaml").is_file()
        and any(entry["category"] == "models" or entry["name"].startswith(("tools/config/", "tools/legacy/")) for entry in entries)):
        raise ValueError("Restore project files first or choose an existing project")
    if project_root and "project" in categories and project_root.exists() and (project_root / "project.yaml").is_file() and project_root.name != project_name:
        raise ValueError("Project name differs from backup project")

    targets = {}
    for entry in entries:
        name = entry["name"]
        category, relative = name.split("/", 1)
        if name == "tools/groups.json":
            continue
        if category == "project":
            target = project_root / relative
        elif category == "models":
            target = project_root / "models" / relative
        elif category == "automation":
            target = workspace / ".hydrohub" / "automations" / relative
        elif relative.startswith("library/"):
            target = workspace / ".hydrohub" / "custom_tools" / relative.removeprefix("library/")
        elif relative.startswith("legacy/"):
            target = project_root / "scripts" / "custom_tools" / relative.removeprefix("legacy/")
        else:
            target = project_root / "workspace" / "custom_tools" / relative.removeprefix("config/")
        if target in targets.values() or target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise ValueError(f"Unsafe restore target: {target}")
        if target.exists() and not overwrite:
            raise FileExistsError(f"Restore target exists: {target}")
        targets[name] = target

    with tempfile.TemporaryDirectory(dir=workspace) as staging:
        stage = Path(staging)
        with zipfile.ZipFile(archive_path) as archive:
            for name in targets:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
            if "tools/groups.json" in {entry["name"] for entry in entries}:
                restored_groups = json.loads(archive.read("tools/groups.json"))
                if (not isinstance(restored_groups, dict) or not isinstance(restored_groups.get("groups"), list)
                    or not isinstance(restored_groups.get("assignments"), dict)
                    or any(not isinstance(group, str) or not group.strip() for group in restored_groups["groups"])
                    or any(not isinstance(tool, str) or group not in restored_groups["groups"]
                           for tool, group in restored_groups["assignments"].items())):
                    raise ValueError("Invalid tool groups")
            else:
                restored_groups = None
        for name, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage / name).replace(target)
    if restored_groups is not None:
        groups = ToolGroups(workspace / ".hydrohub" / "custom_tools")
        existing = groups.load()
        names = {group.casefold(): group for group in existing["groups"]}
        for group in restored_groups["groups"]:
            if group.casefold() not in names:
                existing["groups"].append(group)
                names[group.casefold()] = group
        existing["assignments"].update({tool: names[group.casefold()]
                                        for tool, group in restored_groups["assignments"].items()})
        groups.save(existing)
    if "project" in categories and (project_root / "project.yaml").exists():
        ProjectManager(workspace / ".hydrohub" / "project_registry.json").open(project_root)
    return list(targets.values())
