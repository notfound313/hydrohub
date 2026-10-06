from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


PROJECT_DIRS = (
    "data/raw",
    "data/preparing_data",
    "data/catalog/catalog_assets",
    "data/wflow",
    "models/wflow",
    "configs/hydromt",
    "configs/wflow",
    "configs/julia",
    "outputs/simulation",
    "outputs/maps",
    "outputs/graphs",
    "outputs/reports",
    "logs",
    "manifests",   
    "workspace",
)


@dataclass(frozen=True)
class ProjectContext:
    name: str
    root: Path

    @property
    def manifests_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"


class ProjectManager:
    def __init__(self, registry_path: Path | None = None) -> None:
        self.registry_path = registry_path

    def register(self, context: ProjectContext) -> None:
        if self.registry_path is None:
            return
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry = self.list_registered()
        now = datetime.now(timezone.utc).isoformat()
        registry[str(context.root)] = {"name": context.name, "last_opened": now}
        self._atomic_json(self.registry_path, {"projects": registry})

    def create(self, parent: Path, name: str, description: str = "") -> ProjectContext:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name is required.")
        if re.search(r'[<>:"/\\|?*]', clean_name):
            raise ValueError("Project name contains reserved characters.")
        root = parent.expanduser().resolve() / clean_name
        if root.exists():
            raise FileExistsError(f"Project already exists: {root}")
        root.mkdir(parents=True)
        for relative in PROJECT_DIRS:
            (root / relative).mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "project": {
                "name": clean_name,
                "description": description.strip(),
                "model_type": "Wflow",
                "state": "NEW",
                "created_at": created_at,
            }
        }
        (root / "project.yaml").write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")
        setup_state = {
            "current_step": 1,
            "steps": {
                "project": "VALID",
                "aoi": "NOT_STARTED",
                "environment": "NOT_STARTED",
                "global_data": "NOT_STARTED",
                "wflow_catalog": "BLOCKED",
                "data_entries": "BLOCKED",
                "wflow_yml": "BLOCKED",
                "validation": "BLOCKED",
                "ready": "BLOCKED",
            },
            "total_steps": 9,
            "updated_at": created_at,
        }
        (root / "workspace" / "setup_state.json").write_text(json.dumps(setup_state, indent=2), encoding="utf-8")
        (root / "README.md").write_text(f"# {clean_name}\n\n{description.strip()}\n", encoding="utf-8")
        (root / "workspace" / "ui_state.json").write_text("{}\n", encoding="utf-8")
        (root / "workspace" / "validation_state.json").write_text(json.dumps({"catalog": "NOT_VALIDATED", "wflow_yml": "BLOCKED", "runtime": "BLOCKED"}, indent=2), encoding="utf-8")
        context = ProjectContext(clean_name, root)
        self.register(context)
        return context

    def open(self, root: Path) -> ProjectContext:
        root = root.expanduser().resolve()
        metadata_path = root / "project.yaml"
        if not metadata_path.exists():
            raise ValueError(f"Not a HydroHub project: {root}")
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        context = ProjectContext(metadata["project"]["name"], root)
        self.register(context)
        return context

    def delete(self, context: ProjectContext) -> None:
        # Ensure the project is not currently open
        if hasattr(self, "_open_projects") and context.root in getattr(self, "_open_projects", set()):
            raise RuntimeError("Cannot delete an open project.")
        # Remove project directory
        if context.root.is_dir():
            import shutil
            shutil.rmtree(context.root)
        # Remove from registry
        if self.registry_path:
            registry = self.list_registered()
            registry.pop(str(context.root), None)
            self._atomic_json(self.registry_path, {"projects": registry})

    def list_registered(self) -> dict[str, dict]:
        if self.registry_path is None or not self.registry_path.exists():
            return {}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data.get("projects", {})
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(path)
