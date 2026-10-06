"""Workspace-wide automation definitions and scheduling."""
from __future__ import annotations

import json
import re
import math
from copy import deepcopy
from calendar import monthrange
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from hydrohub.plugins.custom_tools import CustomToolStore


BUILTINS = {
    "builtin.check_last_run_date": "Find latest completed model run",
    "builtin.download_forcing": "Run configured forcing download tool",
    "builtin.hydromt_update": "Update Wflow model through HydroMT",
    "builtin.wflow_run": "Run Wflow.jl with Run Registry chaining",
    "builtin.compute_index": "Compute SSFI from run output",
    "builtin.save_to_registry": "Verify and register completed run",
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _cron_values(field: str, minimum: int, maximum: int) -> set[int]:
    values = set()
    for part in field.split(","):
        base, separator, step_text = part.partition("/")
        try:
            step = int(step_text) if separator else 1
            if base == "*":
                first, last = minimum, maximum
            elif "-" in base:
                first, last = map(int, base.split("-", 1))
            else:
                first = int(base)
                last = maximum if separator else first
            if step < 1 or first < minimum or last > maximum or first > last:
                raise ValueError()
            values.update(range(first, last + 1, step))
        except ValueError as exc:
            raise ValueError(f"Invalid cron field: {field}") from exc
    return values


def _cron_fields(expression: str):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron needs five fields: minute hour day month weekday.")
    return [_cron_values(value, lower, upper) for value, lower, upper in
            zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))]


def check_schema(schema: dict, values: dict, label: str, *, required: bool = False):
    if not isinstance(schema, dict):
        raise ValueError(f"{label} schema must be a mapping.")
    types = {"string": str, "date": str, "integer": int, "number": (int, float), "boolean": bool}
    for key, expected in schema.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} schema fields must be strings.")
        if required and key not in values:
            raise ValueError(f"Missing {label}.{key}.")
        if isinstance(expected, dict):
            check_schema(expected, {}, f"{label}.{key}")
            if key in values:
                if not isinstance(values[key], dict):
                    raise ValueError(f"{label}.{key} must be a mapping.")
                check_schema(expected, values[key], f"{label}.{key}", required=required)
        elif not isinstance(expected, str) or expected not in types:
            raise ValueError(f"{label}.{key} uses unsupported type {expected}.")
        elif key in values and (not isinstance(values[key], types[expected]) or
                                expected in {"integer", "number"} and isinstance(values[key], bool)):
            raise ValueError(f"{label}.{key} must be {expected}.")


def tool_catalog(workspace: Path, projects: dict[str, dict]) -> dict[str, dict]:
    catalog = {name: {"name": name, "description": description, "type": "builtin"}
               for name, description in BUILTINS.items()}
    for info in projects.values():
        store = CustomToolStore(Path(info["root"]), workspace / ".hydrohub" / "custom_tools")
        for tool in store.discover()[0]:
            catalog[f"custom.{tool['id']}"] = {**tool, "type": "custom"}
    for path in (workspace / ".hydrohub" / "automations" / "tools").glob("*.json"):
        try:
            definition = validate_tool_registration(json.loads(path.read_text(encoding="utf-8")))
            catalog[definition["tool_id"]] = definition
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return catalog


def validate_tool_registration(definition: dict) -> dict:
    if not isinstance(definition, dict):
        raise ValueError("Tool registration must be a mapping.")
    identifier = definition.get("tool_id", "")
    if not isinstance(identifier, str) or not identifier.startswith("custom.") or not IDENTIFIER.fullmatch(identifier[7:]):
        raise ValueError("Custom tool_id must be custom.<lowercase_identifier>.")
    executable = definition.get("executable", {})
    if not isinstance(executable, dict) or executable.get("kind") not in {"python_script", "cli_binary", "http_endpoint"}:
        raise ValueError("Executable kind must be python_script, cli_binary or http_endpoint.")
    if not isinstance(executable.get("path"), str) or not executable["path"].strip():
        raise ValueError("Executable path or URL is required.")
    if executable["kind"] == "http_endpoint" and not executable["path"].startswith(("http://", "https://")):
        raise ValueError("HTTP endpoint requires an http:// or https:// URL.")
    arguments = executable.get("arguments", [])
    if not isinstance(arguments, list) or any(not isinstance(value, str) for value in arguments):
        raise ValueError("Executable arguments must be a list of strings.")
    for name in ("input_schema", "output_schema"):
        check_schema(definition.get(name, {}), {}, name)
    timeout = definition.get("timeout_seconds", 1800)
    if type(timeout) is not int or timeout < 1:
        raise ValueError("Tool timeout_seconds must be a positive integer.")
    return {**definition, "type": "custom"}


def pipeline_nodes(spec: dict) -> list[dict]:
    """Migrate legacy ordered steps without changing their parameter semantics."""
    legacy = "nodes" not in spec
    nodes = deepcopy(spec.get("steps" if legacy else "nodes", []))
    if not isinstance(nodes, list):
        raise ValueError("Nodes must be a list.")
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError("Each node must be a mapping.")
        node.setdefault("config_mode", "global" if legacy else
                        "local" if node.get("tool") in {"builtin.hydromt_update", "builtin.wflow_run"} else "global")
        node.setdefault("next", [nodes[index + 1].get("id")] if legacy and index + 1 < len(nodes) else [])
    return nodes


def graph_order(nodes: list[dict]) -> list[str]:
    """Validate one connected acyclic workflow and return its execution order."""
    by_id = {}
    for node in nodes:
        identifier = node.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in by_id:
            raise ValueError("Node IDs must be unique lowercase identifiers.")
        by_id[identifier] = node
    if not by_id:
        raise ValueError("At least one node required.")
    incoming = dict.fromkeys(by_id, 0)
    edges = {}
    for identifier, node in by_id.items():
        branches = node.get("next_on_status", {})
        if not isinstance(branches, dict) or set(branches) - {"ok", "warning", "error"}:
            raise ValueError("next_on_status must map ok/warning/error to node lists.")
        targets = []
        for links in [node.get("next", []), *branches.values()]:
            if not isinstance(links, list) or any(not isinstance(link, str) or link not in by_id for link in links):
                raise ValueError(f"Invalid connection from {identifier}.")
            targets.extend(links)
        edges[identifier] = list(dict.fromkeys(targets))
        for target in edges[identifier]:
            incoming[target] += 1
    ready = [identifier for identifier, count in incoming.items() if count == 0]
    if len(ready) != 1:
        raise ValueError("Workflow needs one entry node and no cycles or disconnected nodes.")
    order = []
    while ready:
        identifier = ready.pop(0)
        order.append(identifier)
        for target in edges[identifier]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if len(order) != len(nodes):
        raise ValueError("Workflow contains a cycle or disconnected nodes.")
    return order


def validate_pipeline(text: str, catalog: dict[str, dict], projects: dict[str, dict]) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("automation"), dict):
        raise ValueError("Expected automation mapping.")
    spec = doc["automation"]
    if not IDENTIFIER.fullmatch(str(spec.get("id", ""))):
        raise ValueError("Automation ID must be a lowercase identifier.")
    if not isinstance(spec.get("name"), str) or not spec["name"].strip():
        raise ValueError("Automation name required.")
    trigger = spec.get("trigger")
    if not isinstance(trigger, dict) or trigger.get("type") not in {"manual", "schedule"}:
        raise ValueError("Trigger type must be manual or schedule.")
    if trigger["type"] == "schedule":
        mode = trigger.get("mode")
        if mode == "interval":
            if type(trigger.get("days")) is not int or trigger["days"] < 1:
                raise ValueError("Interval requires positive integer days.")
        elif mode == "monthly_on_date":
            day = trigger.get("day_of_month")
            if type(day) is not int or not 1 <= day <= 31:
                raise ValueError("day_of_month must be 1..31.")
        elif mode == "cron":
            _cron_fields(str(trigger.get("expression", "")))
        else:
            raise ValueError("Schedule mode must be interval, monthly_on_date or cron.")
        if mode != "cron":
            try:
                datetime.strptime(trigger.get("time", ""), "%H:%M")
            except (TypeError, ValueError) as exc:
                raise ValueError("Schedule time must be HH:MM.") from exc
        try:
            next_trigger(trigger, None, datetime.now().astimezone())
        except OverflowError as exc:
            raise ValueError("Schedule exceeds the supported datetime range.") from exc
    concurrency = spec.get("concurrency", {})
    if not isinstance(concurrency, dict) or type(concurrency.get("max_parallel_models", 1)) is not int or not 1 <= concurrency.get("max_parallel_models", 1) <= 32:
        raise ValueError("max_parallel_models must be 1..32.")
    targets = spec.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("At least one target required.")
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("project_id"), str) or target["project_id"] not in projects:
            raise ValueError(f"Unknown project_id: {target!r}")
        project = projects[target["project_id"]]
        model = project["models"].get(target.get("model_id")) if isinstance(target.get("model_id"), str) else None
        if not model:
            raise ValueError(f"Unknown model_id: {target!r}")
        key = (target["project_id"], target["model_id"])
        if key in seen:
            raise ValueError(f"Duplicate target: {key}")
        seen.add(key)
    steps = pipeline_nodes(spec)
    graph_order(steps)
    spec["nodes"] = steps
    spec.pop("steps", None)
    for step in steps:
        tool = step.get("tool")
        if not isinstance(tool, str) or tool not in catalog:
            raise ValueError(f"Unknown tool: {tool}")
        params = step.get("params", {})
        if step.get("config_mode") not in {"global", "local"}:
            raise ValueError("config_mode must be global or local.")
        position = step.get("position", [0, 0])
        if not isinstance(position, list) or len(position) != 2 or any(type(value) not in {int, float} or not math.isfinite(value) for value in position):
            raise ValueError("Node position must contain two numbers.")
        if not isinstance(params, dict):
            raise ValueError(f"Step {step['id']} params must be a mapping.")
        definition = catalog[tool]
        if definition["type"] == "custom":
            check_schema(definition.get("input_schema", {}), {}, f"{step['id']} input")
            allowed = {field["name"] for field in definition.get("parameters", [])} | set(definition.get("input_schema", {}))
            if set(params) - allowed:
                raise ValueError(f"Unknown parameter in {step['id']}: {set(params) - allowed}")
            for field in definition.get("parameters", []):
                if step["config_mode"] == "global" and field.get("required") and field["name"] not in params and "default" not in field:
                    raise ValueError(f"Missing parameter {field['name']} in {step['id']}.")
            if "input_schema" in definition:
                effective = {**{field["name"]: field["default"] for field in definition.get("parameters", []) if "default" in field}, **params}
                check_schema(definition["input_schema"], effective if step["config_mode"] == "global" else {}, f"{step['id']} input")
            if "output_schema" in definition:
                check_schema(definition["output_schema"], {}, f"{step['id']} output")
        elif tool == "builtin.compute_index":
            if params.get("index", "SSFI") != "SSFI":
                raise ValueError("Only SSFI index supported.")
            if type(params.get("timescale", 1)) is not int or params.get("timescale", 1) < 1:
                raise ValueError("SSFI timescale must be positive integer months.")
        elif tool == "builtin.download_forcing" and step["config_mode"] == "global":
            from hydrohub.plugins.registry import ToolRegistry
            known = {item.id for item in ToolRegistry().by_category("download")}
            if params.get("tool_id") not in known or not isinstance(params.get("arguments", {}), dict):
                raise ValueError("download_forcing needs registered download tool_id and arguments mapping.")
    policy = spec.get("on_failure", {})
    if not isinstance(policy, dict) or policy.get("strategy", "stop_model") not in {"stop_model", "skip_step", "retry"}:
        raise ValueError("Invalid failure strategy.")
    if type(policy.get("retry_count", 0)) is not int or not 0 <= policy.get("retry_count", 0) <= 10:
        raise ValueError("retry_count must be 0..10.")
    for node in steps:
        local_policy = node.get("on_failure", policy)
        if not isinstance(local_policy, dict) or local_policy.get("strategy", "stop_model") not in {"stop_model", "skip_step", "retry"}:
            raise ValueError("Invalid node failure strategy.")
        if type(local_policy.get("retry_count", 0)) is not int or not 0 <= local_policy.get("retry_count", 0) <= 10:
            raise ValueError("Node retry_count must be 0..10.")
        if "timeout_seconds" in node and (type(node["timeout_seconds"]) is not int or node["timeout_seconds"] < 1):
            raise ValueError("Node timeout_seconds must be a positive integer.")
    return spec


def discover_projects(workspace: Path) -> dict[str, dict]:
    from hydrohub.core.run_registry import RunRegistry

    registry = workspace / ".hydrohub" / "project_registry.json"
    entries = json.loads(registry.read_text(encoding="utf-8")).get("projects", {}) if registry.exists() else {}
    projects = {}
    for root_name, info in entries.items():
        root = Path(root_name).resolve()
        meta = root / "project_meta.json"
        if not (root / "project.yaml").is_file():
            continue
        models = {}
        for config in root.rglob("wflow_sbm.toml"):
            if "runs" in config.relative_to(root).parts:
                continue
            model_root = config.parent
            model_meta = model_root / "model_meta.json"
            if not meta.exists() or not model_meta.exists():
                RunRegistry(root, model_root, info["name"])
            model_id = json.loads(model_meta.read_text(encoding="utf-8"))["model_id"]
            models[model_id] = model_root
        project_id = json.loads(meta.read_text(encoding="utf-8"))["project_id"] if meta.exists() else root.name
        projects[project_id] = {"root": root, "name": info["name"], "models": models}
    return projects


def next_trigger(trigger: dict, last: datetime | None, now: datetime) -> datetime | None:
    if trigger["type"] == "manual":
        return None
    if trigger["mode"] == "cron":
        minutes, hours, days, months, weekdays = _cron_fields(trigger["expression"])
        cursor = (last or now).replace(second=0, microsecond=0)
        if last or cursor <= now:
            cursor += timedelta(minutes=1)
        dom_any = trigger["expression"].split()[2] == "*"
        dow_any = trigger["expression"].split()[4] == "*"
        for offset in range(366 * 5):
            day = cursor.date() + timedelta(days=offset)
            weekday = (day.weekday() + 1) % 7
            if 7 in weekdays:
                weekdays.add(0)
            if day.month not in months or not ((day.day in days or dom_any) if dow_any else
                                                  (weekday in weekdays or dow_any) if dom_any else
                                                  (day.day in days or weekday in weekdays)):
                continue
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
                    if candidate >= cursor:
                        return candidate
        raise ValueError("No cron occurrence found in five years.")
    hour, minute = map(int, trigger["time"].split(":"))
    if trigger["mode"] == "interval":
        base = (last or now).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if last:
            return base + timedelta(days=trigger["days"])
        return base if base > now else base + timedelta(days=trigger["days"])
    year, month = now.year, now.month
    while True:
        day = min(trigger["day_of_month"], monthrange(year, month)[1])
        candidate = now.replace(year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > (last or now):
            return candidate
        month += 1
        if month == 13:
            year, month = year + 1, 1


class AutomationStore:
    def __init__(self, workspace: Path):
        self.root = workspace / ".hydrohub" / "automations"

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("*.yaml")):
            try:
                spec = yaml.safe_load(path.read_text(encoding="utf-8"))["automation"]
                if isinstance(spec, dict) and IDENTIFIER.fullmatch(str(spec.get("id", ""))) and path.stem == spec["id"]:
                    spec["nodes"] = pipeline_nodes(spec)
                    spec.pop("steps", None)
                    result.append(spec)
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                continue
        return result

    def save(self, text: str, catalog: dict, projects: dict) -> dict:
        spec = validate_pipeline(text, catalog, projects)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{spec['id']}.yaml"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(yaml.safe_dump({"automation": spec}, sort_keys=False), encoding="utf-8")
        temporary.replace(path)
        return spec

    def state(self, automation_id: str) -> dict:
        if not IDENTIFIER.fullmatch(automation_id):
            raise ValueError("Invalid automation ID")
        path = self.root / f"{automation_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"enabled": False}

    def update_state(self, automation_id: str, **changes) -> None:
        if not IDENTIFIER.fullmatch(automation_id):
            raise ValueError("Invalid automation ID")
        data = {**self.state(automation_id), **changes}
        path = self.root / f"{automation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(path)

    def delete(self, automation_id: str, active=()) -> None:
        if not IDENTIFIER.fullmatch(automation_id):
            raise ValueError("Invalid automation ID")
        if automation_id in active:
            raise ValueError("Stop or finish the running automation before deleting it.")
        for suffix in (".yaml", ".json"):
            (self.root / (automation_id + suffix)).unlink(missing_ok=True)
