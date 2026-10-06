"""Global labels for custom tools; definitions stay in their original locations."""

import json
from pathlib import Path


class ToolGroups:
    def __init__(self, library_root: Path):
        self.path = library_root / "groups.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"groups": [], "assignments": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("groups"), list) or not isinstance(data.get("assignments"), dict):
            raise ValueError("Invalid tool groups file")
        if (any(not isinstance(name, str) or not name.strip() for name in data["groups"])
            or len({name.casefold() for name in data["groups"]}) != len(data["groups"])
            or any(not isinstance(tool, str) or group not in data["groups"] for tool, group in data["assignments"].items())):
            raise ValueError("Invalid tool group names or assignments")
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def create(self, name: str) -> None:
        name = name.strip()
        data = self.load()
        if not name or any(group.casefold() == name.casefold() for group in data["groups"]):
            raise ValueError("Group name is empty or already exists")
        data["groups"].append(name)
        self.save(data)

    def rename(self, old: str, new: str) -> None:
        new = new.strip()
        data = self.load()
        if old not in data["groups"] or not new or any(group.casefold() == new.casefold() for group in data["groups"] if group != old):
            raise ValueError("Group name is empty, missing, or already exists")
        data["groups"][data["groups"].index(old)] = new
        data["assignments"] = {tool: new if group == old else group for tool, group in data["assignments"].items()}
        self.save(data)

    def delete(self, name: str) -> None:
        data = self.load()
        if name not in data["groups"]:
            raise ValueError("Group not found")
        data["groups"].remove(name)
        data["assignments"] = {tool: group for tool, group in data["assignments"].items() if group != name}
        self.save(data)

    def assign(self, tool_id: str, group: str) -> None:
        data = self.load()
        if group and group not in data["groups"]:
            raise ValueError("Group not found")
        if group:
            data["assignments"][tool_id] = group
        else:
            data["assignments"].pop(tool_id, None)
        self.save(data)
