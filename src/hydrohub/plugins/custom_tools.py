"""Reusable custom-tool definitions with project-local configuration."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement


MANIFEST = {
    "schema_version": 2,
    "id": "inspect_data",
    "name": "Inspect input / output",
    "description": "Inspect a HydroMT input or simulation output file.",
    "entrypoint": "main.py",
    "version": "1.0.0",
    "parameters": [
        {"name": "input", "label": "Input file", "type": "file", "required": True},
        {"name": "output", "label": "Report file", "type": "output", "required": True},
        {"name": "west", "label": "AOI west", "type": "number"},
        {"name": "south", "label": "AOI south", "type": "number"},
        {"name": "east", "label": "AOI east", "type": "number"},
        {"name": "north", "label": "AOI north", "type": "number"},
        {"name": "aoi_path", "label": "AOI GeoJSON", "type": "file"},
    ],
    "environment": [],
    "dependencies": [],
}
SCRIPT = '''import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--west", type=float)
parser.add_argument("--south", type=float)
parser.add_argument("--east", type=float)
parser.add_argument("--north", type=float)
parser.add_argument("--aoi_path")
args = parser.parse_args()
source, target = Path(args.input), Path(args.output)
if source.resolve() == target.resolve():
    parser.error("Report must differ from the input file")
print("INFO Inspecting " + str(source), flush=True)
report = {"name": source.name, "size_bytes": source.stat().st_size}
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x", encoding="utf-8") as stream:
    json.dump(report, stream, indent=2)
print("[WRITE] " + str(target), flush=True)
'''

PARAMETER_TYPES = {"text", "file", "output", "directory", "integer", "number", "boolean", "select"}
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def select_options(field: dict) -> list[dict[str, str]]:
    options = field.get("options")
    choices = field.get("choices")
    if options is not None and choices is not None:
        raise ValueError(f"Select parameter {field.get('name', '')} cannot define both options and choices.")
    values = options if options is not None else choices
    if not isinstance(values, list) or not values:
        raise ValueError(f"Select parameter {field.get('name', '')} requires options or choices.")
    normalized = []
    seen = set()
    for option in values:
        if isinstance(option, str):
            label = value = option
        elif isinstance(option, dict) and isinstance(option.get("label"), str) and isinstance(option.get("value"), str):
            label, value = option["label"], option["value"]
        else:
            raise ValueError(f"Select parameter {field.get('name', '')} options must use label/value strings.")
        if not label or not value or value in seen:
            raise ValueError(f"Select parameter {field.get('name', '')} options must be unique and non-empty.")
        seen.add(value)
        normalized.append({"label": label, "value": value})
    default = field.get("default", normalized[0]["value"])
    if default not in seen:
        raise ValueError(f"Select parameter {field.get('name', '')} default must match an option value.")
    return normalized


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") not in {1, 2}:
        raise ValueError("Unsupported template version; use schema version 1 or 2.")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(data.get("id", ""))):
        raise ValueError("Tool ID must use lowercase letters, digits and underscores.")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("Tool name is required.")
    if data.get("entrypoint") != "main.py":
        raise ValueError("Entrypoint must be main.py.")

    result = dict(data)
    parameters = result.get("parameters", [])
    environment = result.get("environment", []) if result.get("schema_version") == 2 else []
    dependencies = result.get("dependencies", []) if result.get("schema_version") == 2 else []
    if not isinstance(parameters, list) or not isinstance(environment, list) or not isinstance(dependencies, list):
        raise ValueError("Parameters, environment, and dependencies must be arrays.")
    names: set[str] = set()
    for field in parameters:
        if not isinstance(field, dict):
            raise ValueError("Each parameter must be an object.")
        name = str(field.get("name", ""))
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in names:
            raise ValueError("Parameter names must be unique lowercase identifiers.")
        names.add(name)
        if field.get("type") not in PARAMETER_TYPES:
            raise ValueError(f"Unsupported parameter type: {name}")
        if field.get("type") == "select":
            select_options(field)
    env_names: set[str] = set()
    for field in environment:
        if not isinstance(field, dict):
            raise ValueError("Each environment variable must be an object.")
        name = str(field.get("name", ""))
        if not ENVIRONMENT_NAME.fullmatch(name) or name in env_names:
            raise ValueError("Environment names must be unique shell identifiers.")
        env_names.add(name)
        if "default" in field and field.get("secret"):
            raise ValueError(f"Secret environment variable {name} cannot have a default value.")
    normalized_dependencies: list[str] = []
    for value in dependencies:
        if not isinstance(value, str):
            raise ValueError("Dependencies must be PEP 508 requirement strings.")
        try:
            requirement = Requirement(value)
        except InvalidRequirement as exc:
            raise ValueError(f"Invalid dependency: {value}") from exc
        if requirement.url:
            raise ValueError(f"Dependency URLs are not allowed: {value}")
        normalized_dependencies.append(str(requirement))
    result["parameters"] = parameters
    result["environment"] = environment
    result["dependencies"] = list(dict.fromkeys(normalized_dependencies))
    return result


def validate_config(data: dict, tool_id: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Project configuration must be a JSON object.")
    if data.get("schema_version", 1) != 1:
        raise ValueError("Unsupported project configuration version.")
    if data.get("tool_id", tool_id) != tool_id:
        raise ValueError("Project configuration tool_id does not match selected tool.")
    parameters = data.get("parameters", {})
    environment = data.get("environment", {})
    secrets = data.get("secrets", [])
    if not isinstance(parameters, dict) or not isinstance(environment, dict) or not isinstance(secrets, list):
        raise ValueError("Parameters/environment must be objects and secrets must be an array.")
    if any(not ENVIRONMENT_NAME.fullmatch(str(name)) for name in environment):
        raise ValueError("Environment names must be shell identifiers.")
    if any(not ENVIRONMENT_NAME.fullmatch(str(name)) for name in secrets):
        raise ValueError("Secret names must be shell identifiers.")
    if set(environment) & set(secrets):
        raise ValueError("Secret values cannot also appear in plaintext environment.")
    return {
        "schema_version": 1,
        "tool_id": tool_id,
        "parameters": parameters,
        "environment": {str(key): str(value) for key, value in environment.items()},
        "secrets": list(dict.fromkeys(map(str, secrets))),
        **({"interpreter": str(data["interpreter"])} if data.get("interpreter") else {}),
    }


def export_template(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tool.json", json.dumps(MANIFEST, indent=2))
        archive.writestr("main.py", SCRIPT)
        archive.writestr(
            "README.md",
            "# HydroHub custom tool\n\nEdit tool.json and main.py. "
            "Parameters become argparse flags; environment declarations are configured per project.\n\n"
            "## Supported parameter types\n\n"
            "| Type | Behavior |\n| --- | --- |\n"
            "| `text` | Text value. |\n"
            "| `file` | Input file path; the file must exist. |\n"
            "| `directory` | Input directory path; the directory must exist. |\n"
            "| `output` | Destination path; it does not need to exist yet. |\n"
            "| `integer` | Integer value. |\n"
            "| `number` | Numeric value, including decimals. |\n"
            "| `boolean` | Adds `--name` only when enabled. |\n"
            "| `select` | Choice from `options` label/value objects or `choices` strings. |\n\n"
            "Parameter names are independent of types: `input` is a name, not a type. "
            "Input/output parameters are optional declarations; `required: true` makes a declared value mandatory. "
            "Empty optional values are omitted from command-line arguments. "
            "Use `default` for an initial value and `label` for the form label. "
            "Relative file, directory, and output paths resolve against the project root. "
            "The script creates output files/directories as needed.\n\n"
            "Example input folder for daily NetCDF files in `tool.json`:\n\n"
            '```json\n{"name": "input", "label": "NetCDF folder", "type": "directory", "required": true}\n```\n\n'
            "Select parameters use `options: [{\"label\": \"Fast\", \"value\": \"fast\"}]` "
            'or `choices: ["fast", "safe"]`; use one, not both.\n\n'
            "## Project context and execution\n\n"
            "Parameters named west, south, east, and north are filled automatically from the project AOI bounds. "
            "The aoi_path parameter is filled automatically with workspace/aoi.geojson when available. "
            "Dependencies are PEP 508 package requirements installed into the selected interpreter. "
            "Run in the project working directory. Print INFO/WARNING/ERROR and [WRITE] output paths. "
            "Tools are normal Python programs, not sandboxed.\n",
        )


class SecretStore:
    SERVICE = "HydroHub Custom Tools"

    def __init__(self, backend=None) -> None:
        if backend is None:
            import keyring

            backend = keyring
        self.backend = backend

    @staticmethod
    def _account(project_root: Path, tool_id: str, name: str) -> str:
        project = str(Path(project_root).expanduser().resolve()).casefold().encode("utf-8")
        return f"{hashlib.sha256(project).hexdigest()}:{tool_id}:{name}"

    def get(self, project_root: Path, tool_id: str, name: str) -> str | None:
        return self.backend.get_password(self.SERVICE, self._account(project_root, tool_id, name))

    def set(self, project_root: Path, tool_id: str, name: str, value: str) -> None:
        account = self._account(project_root, tool_id, name)
        if value:
            try:
                self.backend.set_password(self.SERVICE, account, value)
            except Exception as exc:
                raise RuntimeError(f"OS keyring is unavailable: {exc}") from exc
        else:
            try:
                self.backend.delete_password(self.SERVICE, account)
            except Exception:
                pass


class CustomToolStore:
    def __init__(self, project_root: Path, library_root: Path | None = None) -> None:
        self.project_root = Path(project_root)
        self.root = self.project_root / "scripts" / "custom_tools"  # Legacy public path.
        self.library_root = Path(library_root) if library_root else None
        self.config_root = self.project_root / "workspace" / "custom_tools"

    def _definition_root(self) -> Path:
        return self.library_root or self.root

    def install(self, package: Path) -> dict:
        root = self._definition_root()
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package) as archive:
            if sum(info.file_size for info in archive.infolist()) > 20 * 1024 * 1024:
                raise ValueError("Tool package exceeds 20 MB.")
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename:
                    raise ValueError("Package contains an invalid path.")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Symbolic links are not allowed in tool packages.")
            try:
                manifest = validate_manifest(json.loads(archive.read("tool.json")))
                script = archive.read("main.py").decode("utf-8")
            except KeyError as exc:
                raise ValueError("Package must contain tool.json and main.py.") from exc
            compile(script, "main.py", "exec")
            destination = root / manifest["id"]
            if destination.exists():
                raise FileExistsError("This tool ID already exists.")
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                staging = Path(temporary) / "package"
                archive.extractall(staging)
                staging.rename(destination)
        return manifest

    def create(self, manifest: dict | None = None, script: str = SCRIPT) -> dict:
        data = validate_manifest(manifest or MANIFEST)
        destination = self._definition_root() / data["id"]
        if destination.exists():
            raise FileExistsError("This tool ID already exists.")
        compile(script, "main.py", "exec")
        destination.mkdir(parents=True)
        _atomic_text(destination / "tool.json", json.dumps(data, indent=2) + "\n")
        _atomic_text(destination / "main.py", script)
        return {**data, "path": destination, "source": "library" if self.library_root else "legacy"}

    def save_definition(self, original_id: str, manifest_text: str, script: str) -> dict:
        data = validate_manifest(json.loads(manifest_text))
        compile(script, "main.py", "exec")
        if data["id"] != original_id:
            raise ValueError("Tool ID cannot be changed after creation.")
        destination = self._definition_root() / original_id
        if not destination.is_dir():
            raise FileNotFoundError(f"Tool not found: {original_id}")
        _atomic_text(destination / "main.py", script)
        _atomic_text(destination / "tool.json", json.dumps(data, indent=2) + "\n")
        return {**data, "path": destination, "source": "library" if self.library_root else "legacy"}

    def delete(self, tool_id: str) -> None:
        destination = self._definition_root() / tool_id
        if destination.is_dir():
            shutil.rmtree(destination)

    def promote(self, tool_id: str) -> dict:
        if not self.library_root:
            raise ValueError("Reusable tool library is unavailable.")
        source, destination = self.root / tool_id, self.library_root / tool_id
        if not source.is_dir():
            raise FileNotFoundError(f"Legacy tool not found: {tool_id}")
        if destination.exists():
            raise FileExistsError("This tool ID already exists in the reusable library.")
        data = validate_manifest(json.loads((source / "tool.json").read_text(encoding="utf-8")))
        compile((source / "main.py").read_text(encoding="utf-8"), "main.py", "exec")
        self.library_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        return data

    def discover(self) -> tuple[list[dict], list[str]]:
        tools: dict[str, dict] = {}
        errors: list[str] = []
        roots = []
        if self.library_root:
            roots.append((self.library_root, "library"))
        roots.append((self.root, "legacy"))
        for root, source in roots:
            for manifest_path in sorted(root.glob("*/tool.json")):
                try:
                    data = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
                    data.update(path=manifest_path.parent, source=source)
                    script = manifest_path.parent / "main.py"
                    if not script.is_file():
                        raise ValueError("main.py is missing")
                    tools[data["id"]] = data  # Project-local legacy definition wins collisions.
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    errors.append(f"{manifest_path.parent.name}: {exc}")
        return sorted(tools.values(), key=lambda item: item["name"].casefold()), errors

    def load_config(self, tool_id: str) -> dict:
        path = self.config_root / f"{tool_id}.json"
        if not path.exists():
            return validate_config({}, tool_id)
        return validate_config(json.loads(path.read_text(encoding="utf-8")), tool_id)

    def save_config(self, tool_id: str, text: str | dict) -> dict:
        raw = json.loads(text) if isinstance(text, str) else text
        data = validate_config(raw, tool_id)
        _atomic_text(self.config_root / f"{tool_id}.json", json.dumps(data, indent=2) + "\n")
        return data
