from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import xarray as xr


RUN_STATUSES = {"completed", "failed", "superseded"}


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    model_id: str
    project_id: str
    start_date: str
    end_date: str
    output_path: str
    state_output_path: str
    state_input_path: str | None
    config_hash: str
    forcing_source: dict
    status: str
    created_at: str

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.start_date)

    @property
    def end(self) -> datetime:
        return datetime.fromisoformat(self.end_date)


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    start: datetime
    end: datetime
    predecessor: RunRecord | None
    overlaps: tuple[RunRecord, ...]
    config_hash: str
    start_mode: str
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RunChain:
    runs: tuple[RunRecord, ...]
    break_reason: str | None = None

    @property
    def label(self) -> str:
        first, last = self.runs[0], self.runs[-1]
        return f"{first.start.date()} - {last.end.date()} ({len(self.runs)} run)"


def _atomic_json(path: Path, data: dict, *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    sample_size = 1024 * 1024
    offsets = {0, max(0, stat.st_size // 2 - sample_size // 2), max(0, stat.st_size - sample_size)}
    with path.open("rb") as stream:
        for offset in sorted(offsets):
            stream.seek(offset)
            digest.update(stream.read(sample_size))
    return digest.hexdigest()


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))


def _set_toml_key(text: str, section: str, key: str, value) -> str:
    lines = text.splitlines()
    target_header = f"[{section}]" if section else None
    section_start = 0 if not section else None
    section_end = len(lines)
    current = ""
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_start is not None and current == section:
                section_end = index
                break
            current = stripped[1:-1].strip()
            if current == section:
                section_start = index + 1
        elif current == section and key_pattern.match(line):
            if value is None:
                del lines[index]
            else:
                lines[index] = f"{key} = {_toml_value(value)}"
            return "\n".join(lines) + "\n"

    if section_start is None:
        if value is None:
            return "\n".join(lines) + "\n"
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([target_header, f"{key} = {_toml_value(value)}"])
    elif value is not None:
        lines.insert(section_end, f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


class RunRegistry:
    def __init__(self, project_root: Path, model_root: Path, project_name: str = "") -> None:
        self.project_root = project_root.resolve()
        self.project_name = project_name or self.project_root.name
        self.model_root = model_root.resolve()
        self.model_root.relative_to(self.project_root)
        self.base_config = self.model_root / "wflow_sbm.toml"
        if not self.base_config.is_file():
            raise FileNotFoundError(f"Wflow configuration not found: {self.base_config}")
        self.registry_path = self.model_root / "registry.json"
        self.model_meta_path = self.model_root / "model_meta.json"
        self.project_meta_path = self.project_root / "project_meta.json"
        self.project_id = self._ensure_project_meta(project_name)
        self.model_id = self._ensure_model_meta()

    def _ensure_project_meta(self, project_name: str) -> str:
        if self.project_meta_path.exists():
            data = json.loads(self.project_meta_path.read_text(encoding="utf-8"))
        else:
            data = {
                "project_id": uuid.uuid4().hex[:12],
                "name": project_name or self.project_root.name,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(self.project_meta_path, data)
        return str(data["project_id"])

    def _ensure_model_meta(self) -> str:
        current_hash = self.config_hash()
        if self.model_meta_path.exists():
            data = json.loads(self.model_meta_path.read_text(encoding="utf-8"))
            data["base_config_hash"] = current_hash
        else:
            data = {
                "model_id": uuid.uuid4().hex[:12],
                "name": self.model_root.name,
                "description": "",
                "base_config_hash": current_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        _atomic_json(self.model_meta_path, data)
        return str(data["model_id"])

    def config_hash(self) -> str:
        with self.base_config.open("rb") as stream:
            config = deepcopy(tomllib.load(stream))
        config.pop("dir_output", None)
        config.pop("time", None)
        state = config.get("state")
        if isinstance(state, dict):
            state.pop("path_input", None)
            state.pop("path_output", None)
        input_config = config.get("input")
        if isinstance(input_config, dict):
            input_config.pop("path_forcing", None)
        output = config.get("output")
        if isinstance(output, dict):
            for value in output.values():
                if isinstance(value, dict):
                    value.pop("path", None)
        payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load(self) -> dict:
        if not self.registry_path.exists():
            return {"version": 1, "model_id": self.model_id, "runs": []}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            backup = self.registry_path.with_suffix(self.registry_path.suffix + ".bak")
            if not backup.exists():
                raise ValueError(f"Run registry is invalid: {exc}") from exc
            data = json.loads(backup.read_text(encoding="utf-8"))
        if data.get("model_id") != self.model_id:
            raise ValueError("Run registry belongs to a different model_id.")
        return data

    def _save_records(self, records: list[RunRecord]) -> None:
        _atomic_json(
            self.registry_path,
            {"version": 1, "model_id": self.model_id, "runs": [asdict(run) for run in records]},
            backup=True,
        )

    def list_runs(self) -> list[RunRecord]:
        records = [RunRecord(**item) for item in self._load().get("runs", [])]
        return sorted(records, key=lambda run: (run.start, run.created_at, run.run_id))

    def forcing_source(self) -> dict:
        with self.base_config.open("rb") as stream:
            config = tomllib.load(stream)
        configured = config.get("input", {}).get("path_forcing")
        if not configured:
            return {"path": None, "fingerprint": None, "size": None}
        path = Path(str(configured))
        path = path if path.is_absolute() else self.model_root / path
        if not path.is_file():
            return {"path": str(path), "fingerprint": None, "size": None}
        return {
            "path": str(path.resolve()),
            "fingerprint": _file_fingerprint(path),
            "size": path.stat().st_size,
        }

    def overlaps(self, start: datetime, end: datetime) -> list[RunRecord]:
        return [
            run
            for run in self.list_runs()
            if run.status != "superseded" and run.start <= end and run.end >= start
        ]

    def predecessor(self, start: datetime, timestep_seconds: int) -> RunRecord | None:
        expected = start - timedelta(seconds=timestep_seconds)
        candidates = [
            run
            for run in self.list_runs()
            if run.status == "completed"
            and run.end == expected
            and (self.resolve_path(run.state_output_path) or Path()).is_file()
        ]
        return max(candidates, key=lambda run: run.created_at, default=None)

    def suggest_run_id(self, start: datetime, end: datetime) -> str:
        base = f"run_{start:%Y%m%d}_{end:%Y%m%d}"
        existing = {run.run_id for run in self.list_runs()}
        if base not in existing and not (self.model_root / "runs" / base).exists():
            return base
        counter = 2
        while f"{base}_v{counter}" in existing or (self.model_root / "runs" / f"{base}_v{counter}").exists():
            counter += 1
        return f"{base}_v{counter}"

    def _validate_state_input(self, state_path: Path) -> None:
        if not state_path.is_file():
            raise ValueError(f"Warm start state is missing: {state_path}")
        with self.base_config.open("rb") as stream:
            config = tomllib.load(stream)
        variables = config.get("state", {}).get("variables", {})
        if not isinstance(variables, dict) or not variables:
            return
        snow_enabled = bool(config.get("model", {}).get("snow__flag", False))
        required = {
            internal: external
            for internal, external in variables.items()
            if snow_enabled
            or ("snow" not in internal and internal != "soil_surface__temperature")
        }
        static_path = Path(str(config.get("input", {}).get("path_static", "")))
        static_path = static_path if static_path.is_absolute() else self.model_root / static_path
        try:
            with xr.open_dataset(state_path, decode_cf=False) as state:
                missing = sorted(set(required.values()).difference(state.variables))
                if missing:
                    raise ValueError(
                        "Warm start state is missing required variable(s): " + ", ".join(missing)
                    )
                if static_path.is_file():
                    with xr.open_dataset(static_path, decode_cf=False) as static:
                        dimension_aliases = {
                            "x": ("x", "lon", "longitude"),
                            "lon": ("lon", "longitude", "x"),
                            "longitude": ("longitude", "lon", "x"),
                            "y": ("y", "lat", "latitude"),
                            "lat": ("lat", "latitude", "y"),
                            "latitude": ("latitude", "lat", "y"),
                        }
                        for name in required.values():
                            for dimension, size in state[name].sizes.items():
                                if dimension == "time":
                                    continue
                                expected = next(
                                    (
                                        static.sizes[candidate]
                                        for candidate in dimension_aliases.get(dimension, (dimension,))
                                        if candidate in static.sizes
                                    ),
                                    None,
                                )
                                if expected is not None and size != expected:
                                    raise ValueError(
                                        f"Warm start state variable '{name}' has {dimension}={size}; "
                                        f"static grid requires {expected}."
                                    )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Warm start state cannot be read: {state_path}: {exc}") from exc

    def plan(
        self,
        start: datetime,
        end: datetime,
        timestep_seconds: int,
        run_id: str = "",
        start_mode: str = "auto",
    ) -> RunPlan:
        if end <= start:
            raise ValueError("Run end must be after run start.")
        if start_mode not in {"auto", "cold"}:
            raise ValueError("Start mode must be 'auto' or 'cold'.")
        predecessor = self.predecessor(start, timestep_seconds) if start_mode == "auto" else None
        current_hash = self.config_hash()
        warnings = []
        errors = []
        if start_mode == "cold":
            warnings.append("Cold start was explicitly selected.")
        elif predecessor is None:
            warnings.append("No contiguous completed run was found; this run will use a cold start.")
        elif predecessor.config_hash != current_hash:
            errors.append(
                "Warm start blocked: predecessor uses a different model config. "
                "Select forced cold start or restore the matching config."
            )
        else:
            try:
                self._validate_state_input(self.resolve_path(predecessor.state_output_path))
            except ValueError as exc:
                errors.append(str(exc))
        return RunPlan(
            run_id=run_id.strip() or self.suggest_run_id(start, end),
            start=start,
            end=end,
            predecessor=predecessor,
            overlaps=tuple(self.overlaps(start, end)),
            config_hash=current_hash,
            start_mode=start_mode,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        value = run_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
            raise ValueError("Run ID may contain letters, numbers, dot, underscore, and dash only.")
        return value

    def resolve_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.model_root / path

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.model_root).as_posix()
        except ValueError:
            return str(path.resolve())

    def prepare_run(
        self,
        plan: RunPlan,
        *,
        overwrite_run_id: str | None = None,
        backup_output: bool = False,
    ) -> tuple[RunRecord, Path]:
        if plan.errors:
            raise ValueError(" ".join(plan.errors))
        if plan.config_hash != self.config_hash():
            raise ValueError("Model config changed after run planning; review the run plan again.")
        if plan.predecessor:
            if plan.predecessor.config_hash != plan.config_hash:
                raise ValueError("Warm start blocked: predecessor config does not match current model config.")
            self._validate_state_input(self.resolve_path(plan.predecessor.state_output_path))
        run_id = self._validate_run_id(overwrite_run_id or plan.run_id)
        records = self.list_runs()
        existing = next((run for run in records if run.run_id == run_id), None)
        run_dir = self.model_root / "runs" / run_id
        if existing and overwrite_run_id != run_id:
            raise FileExistsError(f"Run ID already exists: {run_id}")
        if run_dir.exists():
            if overwrite_run_id != run_id:
                raise FileExistsError(f"Run output directory already exists: {run_dir}")
            if backup_output:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.move(str(run_dir), str(run_dir.with_name(f"{run_dir.name}.backup_{stamp}")))
            else:
                shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)

        predecessor = plan.predecessor
        state_input = self.resolve_path(predecessor.state_output_path) if predecessor else None
        text = self.base_config.read_text(encoding="utf-8")
        with self.base_config.open("rb") as stream:
            base_data = tomllib.load(stream)
        text = _set_toml_key(text, "", "dir_input", str(self.model_root))
        text = _set_toml_key(text, "", "dir_output", str(run_dir))
        text = _set_toml_key(text, "time", "starttime", plan.start.isoformat())
        text = _set_toml_key(text, "time", "endtime", plan.end.isoformat())
        text = _set_toml_key(text, "state", "path_output", "outstate.nc")
        text = _set_toml_key(text, "state", "path_input", str(state_input) if state_input else None)
        text = _set_toml_key(text, "model", "cold_start__flag", state_input is None)
        for section, output_config in base_data.get("output", {}).items():
            if isinstance(output_config, dict) and output_config.get("path"):
                text = _set_toml_key(
                    text,
                    f"output.{section}",
                    "path",
                    Path(str(output_config["path"])).name,
                )
        config_path = run_dir / "config.toml"
        config_path.write_text(text, encoding="utf-8")

        record = RunRecord(
            run_id=run_id,
            model_id=self.model_id,
            project_id=self.project_id,
            start_date=plan.start.isoformat(),
            end_date=plan.end.isoformat(),
            output_path=self._relative_path(run_dir),
            state_output_path=self._relative_path(run_dir / "outstate.nc"),
            state_input_path=self._relative_path(state_input) if state_input else None,
            config_hash=plan.config_hash,
            forcing_source=self.forcing_source(),
            status="failed",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        records = [run for run in records if run.run_id != run_id]
        records.append(record)
        self._save_records(records)
        return record, config_path

    def finish_run(self, run_id: str, success: bool) -> RunRecord:
        records = self.list_runs()
        updated = None
        result = []
        for run in records:
            if run.run_id == run_id:
                data = asdict(run)
                data["status"] = "completed" if success else "failed"
                updated = RunRecord(**data)
                result.append(updated)
            else:
                result.append(run)
        if updated is None:
            raise KeyError(f"Unknown run ID: {run_id}")
        self._save_records(result)
        return updated

    def chains(self, timestep_seconds: int) -> list[RunChain]:
        candidates = [run for run in self.list_runs() if run.status == "completed"]
        if not candidates:
            return []
        chains = []
        current = [candidates[0]]
        reason = None
        for run in candidates[1:]:
            previous = current[-1]
            expected = previous.end + timedelta(seconds=timestep_seconds)
            if run.start != expected:
                reason = "overlap" if run.start <= previous.end else "gap"
            elif run.config_hash != previous.config_hash:
                reason = "config hash changed"
            elif (
                run.forcing_source.get("path") == previous.forcing_source.get("path")
                and (run.forcing_source.get("fingerprint") or run.forcing_source.get("sha256"))
                != (previous.forcing_source.get("fingerprint") or previous.forcing_source.get("sha256"))
                and (run.forcing_source.get("size") or 0)
                <= (previous.forcing_source.get("size") or 0)
            ):
                reason = "forcing source was revised"
            else:
                reason = None
            if reason:
                chains.append(RunChain(tuple(current), reason))
                current = [run]
            else:
                current.append(run)
        chains.append(RunChain(tuple(current), None))
        return chains

    def output_files(self, run: RunRecord) -> list[Path]:
        folder = self.resolve_path(run.output_path)
        if folder is None or not folder.is_dir():
            return []
        return sorted(
            {
                path.resolve()
                for pattern in ("output*.csv", "output*.nc")
                for path in folder.glob(pattern)
                if path.is_file()
            },
            key=lambda path: (path.suffix != ".csv", path.name.lower()),
        )

    def build_combined_output(self, chain: RunChain) -> Path:
        if len(chain.runs) < 2:
            raise ValueError("At least two contiguous runs are required for a combined output.")
        files_by_run = [self.output_files(run) for run in chain.runs]
        if any(not files for files in files_by_run):
            raise FileNotFoundError("One or more runs do not contain output CSV or NetCDF files.")
        common_names = set(path.name for path in files_by_run[0])
        for files in files_by_run[1:]:
            common_names &= {path.name for path in files}
        if not common_names:
            raise ValueError("Runs do not share a common output filename and cannot be concatenated.")
        preferred = sorted(common_names, key=lambda name: (not name.endswith(".csv"), name.lower()))[0]
        sources = [next(path for path in files if path.name == preferred) for files in files_by_run]
        signature = "|".join(
            f"{run.run_id}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
            for run, path in zip(chain.runs, sources)
        )
        cache_key = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        cache_dir = self.model_root / ".hydrohub" / "combined"
        cache_dir.mkdir(parents=True, exist_ok=True)
        destination = cache_dir / f"combined_{cache_key}{sources[0].suffix.lower()}"
        if destination.exists():
            return destination

        if destination.suffix == ".csv":
            import pandas as pd

            frames = [pd.read_csv(path) for path in sources]
            time_column = next(
                (column for column in frames[0].columns if str(column).lower() in {"time", "date", "datetime", "timestamp"}),
                None,
            )
            if time_column is None:
                raise ValueError("Combined CSV requires a time column.")
            for frame in frames:
                frame[time_column] = pd.to_datetime(frame[time_column], errors="raise")
            combined = pd.concat(frames, ignore_index=True).sort_values(time_column)
            if combined[time_column].duplicated().any():
                raise ValueError("Combined output contains overlapping timestamps.")
            combined.to_csv(destination, index=False)
        else:
            import xarray as xr

            datasets = [xr.open_dataset(path) for path in sources]
            try:
                time_dimension = next(
                    (name for name in ("time", "date", "datetime") if name in datasets[0].coords),
                    None,
                )
                if time_dimension is None:
                    raise ValueError("Combined NetCDF requires a time coordinate.")
                combined = xr.concat(datasets, dim=time_dimension).sortby(time_dimension)
                values = combined[time_dimension].values
                if len(values) != len(set(values.tolist())):
                    raise ValueError("Combined output contains overlapping timestamps.")
                combined.to_netcdf(destination)
                combined.close()
            finally:
                for dataset in datasets:
                    dataset.close()
        return destination

    def export_combined(self, chain: RunChain, destination: Path) -> Path:
        source = self.build_combined_output(chain)
        if source.suffix == ".csv" or destination.suffix.lower() in {".csv", ".xlsx"}:
            from hydrohub.results.export import export_discharge
            if destination.suffix.lower() not in {".csv", ".xlsx"}:
                destination = destination.with_suffix(".csv")
            export_discharge(source, destination, self.project_name)
            return destination
        destination = destination.with_suffix(source.suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
