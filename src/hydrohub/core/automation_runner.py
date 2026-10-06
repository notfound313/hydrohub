"""Independent per-model automation jobs; subprocesses never block Qt."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hydrohub.core.run_registry import RunRegistry
from hydrohub.plugins.registry import ToolRegistry
from hydrohub.core.automation import graph_order, pipeline_nodes, check_schema


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutomationRunner:
    def __init__(self, workspace: Path, changed=None):
        self.root = workspace / ".hydrohub" / "automations" / "jobs"
        self.changed = changed or (lambda: None)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancelled: set[str] = set()
        self.processes: dict[str, subprocess.Popen] = {}
        self.pools: list[ThreadPoolExecutor] = []
        self.active: set[str] = set()
        self.model_locks: dict[str, threading.Lock] = {}
        self.restore()

    def restore(self):
        for path in self.root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["status"] in {"QUEUED", "RUNNING", "STEP_FAILED", "RETRYING"}:
                    job["status"] = "FAILED"
                    job["error"] = "Application closed during execution."
                    job["events"].append({"status": "FAILED", "at": timestamp()})
                    path.write_text(json.dumps(job, indent=2), encoding="utf-8")
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                continue

    def _emit(self, job: dict, status: str, **changes):
        with self.lock:
            job.update(status=status, **changes)
            job["events"].append({"status": status, "at": timestamp(), "step": job.get("step")})
            path = self.root / job["id"] / "job.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
            temporary.replace(path)
        self.changed()

    def start(self, spec: dict, projects: dict, targets: list[dict] | None = None):
        spec = {**spec, "nodes": pipeline_nodes(spec)}
        graph_order(spec["nodes"])
        targets = targets if targets is not None else spec["targets"]
        if not targets:
            return []
        with self.lock:
            if spec["id"] in self.active:
                raise RuntimeError("Automation already running.")
            self.active.add(spec["id"])
        pool = ThreadPoolExecutor(max_workers=spec.get("concurrency", {}).get("max_parallel_models", 1))
        self.pools.append(pool)
        jobs = []
        for target in targets:
            job = {"id": uuid.uuid4().hex, "automation_id": spec["id"], "target": target,
                   "status": "QUEUED", "step": "", "events": [], "error": "", "steps": [],
                   "nodes": spec["nodes"], "node_status": {node["id"]: "Pending" for node in spec["nodes"]}}
            self.jobs[job["id"]] = job
            self._emit(job, "QUEUED")
            jobs.append(job)
            pool.submit(self._run, job, spec, projects)
        def cleanup():
            pool.shutdown(wait=True)
            with self.lock:
                self.active.discard(spec["id"])
                self.pools.remove(pool)
        threading.Thread(target=cleanup, daemon=True).start()
        return jobs

    def cancel(self, job_id: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
                return
            self.cancelled.add(job_id)
            process = self.processes.get(job_id)
            if process:
                process.terminate()
            if job["status"] == "QUEUED":
                self._emit(job, "CANCELLED")

    def _log(self, job: dict, text: str):
        path = self.root / job["id"] / "run.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp()}] {text}\n")
        self.changed()

    def _command(self, job: dict, args: list[str], cwd: Path, env: dict | None = None, timeout: int = 3600, stdin: str | None = None) -> str:
        job_id = job["id"]
        if job_id in self.cancelled:
            raise InterruptedError("Job cancelled")
        timed_out = threading.Event()
        with subprocess.Popen(args, cwd=cwd, env={**os.environ, **(env or {})},
                               stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE if stdin is not None else subprocess.STDOUT, text=True) as process:
            with self.lock:
                self.processes[job_id] = process
            timer = threading.Timer(timeout, lambda: (timed_out.set(), process.kill()) if process.poll() is None else None)
            timer.start()
            stderr_thread = None
            if stdin is not None:
                def stream_stderr():
                    for line in process.stderr:
                        self._log(job, line.rstrip())
                stderr_thread = threading.Thread(target=stream_stderr, daemon=True)
                stderr_thread.start()
            try:
                if stdin is not None:
                    process.stdin.write(stdin)
                    process.stdin.close()
                lines = []
                for line in process.stdout:
                    lines.append(line)
                    self._log(job, line.rstrip())
                process.wait()
            finally:
                timer.cancel()
                if stderr_thread:
                    stderr_thread.join(timeout=1)
                with self.lock:
                    self.processes.pop(job_id, None)
        output = "".join(lines)
        if job_id in self.cancelled:
            raise InterruptedError("Job cancelled")
        if timed_out.is_set():
            raise TimeoutError(f"Tool timed out after {timeout}s")
        if process.returncode:
            raise RuntimeError(f"Exit {process.returncode}: {output[-1000:]}")
        return output

    def _step(self, job: dict, step: dict, project: dict, model: Path, context: dict):
        tool = step["tool"]
        params = step.get("params", {})
        root = project["root"]
        from hydrohub.core.project_manager import ProjectContext
        from hydrohub.core.workflow_manager import WorkflowManager
        workflow = WorkflowManager()
        project_context = ProjectContext(project["name"], root)
        local = workflow.load_ui_state(project_context)
        if step.get("config_mode", "global") == "local":
            if tool == "builtin.hydromt_update":
                with self.lock:
                    params = {"config_ref": str(workflow.recipe_path(project_context, local.get("hydromt_recipe_file", "wflow.yml"))),
                              "catalog_ref": str(workflow.prepare_hydromt_catalog(project_context, local.get("hydromt_catalog_file", "wflow_catalog.yml"))),
                              "force_overwrite": local.get("hydromt_force_overwrite", False), "verbose": local.get("hydromt_verbose", True)}
            elif tool == "builtin.wflow_run":
                params = {}  # Period and state come from this model's TOML and Run Registry.
            elif tool.startswith("custom."):
                from hydrohub.plugins.custom_tools import CustomToolStore
                config = CustomToolStore(root, self.root.parent.parent / "custom_tools").load_config(tool[7:])
                params = config.get("parameters", {})
            else:
                params = local.get("automation_parameters", {}).get(tool, {})
        timeout = step.get("timeout_seconds", params.get("timeout_seconds", 3600))
        registry = RunRegistry(root, model, project["name"])
        if tool == "builtin.check_last_run_date":
            runs = [run for run in registry.list_runs() if run.status == "completed"]
            last = max(runs, key=lambda run: run.end, default=None)
            return {"last_run": last.run_id if last else None,
                    "period_start": (last.end + timedelta(days=1)).isoformat() if last else None}
        if tool == "builtin.download_forcing":
            tool_id = params.get("tool_id")
            definition = next((item for item in ToolRegistry().discover() if item.id == tool_id and item.category == "download"), None)
            if not definition:
                raise ValueError("download_forcing requires valid download tool_id from Tool Registry.")
            options = dict(params.get("arguments", {}))
            if params.get("period") == "auto":
                if not context.get("period_start"):
                    raise ValueError("Automatic period needs a completed prior run or explicit dates.")
                options.setdefault("start_date", context["period_start"][:10])
                options.setdefault("end_date", datetime.now(timezone.utc).date().isoformat())
            arguments = []
            for key, value in options.items():
                arguments.extend([f"--{key.replace('_', '-')}", str(value)])
            self._command(job, [sys.executable, "-u", "-m", definition.module, *arguments], root, timeout=timeout)
            return {"download_tool": tool_id}
        if tool == "builtin.hydromt_update":
            recipe = (root / params.get("config_ref", "wflow.yml")).resolve()
            catalog = (root / params.get("catalog_ref", "data/catalog/wflow_catalog.yml")).resolve()
            if not recipe.is_relative_to(root.resolve()) or not catalog.is_relative_to(root.resolve()) or not recipe.is_file() or not catalog.is_file():
                raise ValueError("HydroMT recipe/catalog missing or outside project.")
            executable = params.get("executable") or local.get("hydromt_executable") or shutil.which("hydromt")
            if not executable:
                raise FileNotFoundError("hydromt executable missing")
            command = [executable, "update", "wflow_sbm", str(model), "-o", str(model), "-i", str(recipe), "-d", str(catalog)]
            if params.get("force_overwrite"):
                command.append("--fo")
            if params.get("verbose"):
                command.append("-v")
            self._command(job, command, root, timeout=timeout)
            return {"model": str(model)}
        if tool == "builtin.wflow_run":
            with registry.base_config.open("rb") as stream:
                config = tomllib.load(stream)["time"]
            start = datetime.fromisoformat(str(params.get("start") or context.get("period_start") or config["starttime"]))
            end = datetime.fromisoformat(str(params.get("end") or config["endtime"]))
            plan = registry.plan(start, end, int(config.get("timestepsecs", 86400)), start_mode="auto" if params.get("use_warm_state", True) else "cold")
            if plan.overlaps:
                raise ValueError("Simulation period overlaps existing run; select a new period.")
            if plan.errors:
                raise ValueError("; ".join(plan.errors))
            julia = params.get("executable") or local.get("julia_executable") or shutil.which("julia")
            if not julia:
                raise FileNotFoundError("julia executable missing")
            script = Path(__file__).parents[1] / "julia" / "run_wflow.jl"
            record, config_path = registry.prepare_run(plan)
            try:
                self._command(job, [julia, str(script)], script.parent,
                              {"HYDROHUB_MODEL_DIR": str(model), "HYDROHUB_CONFIG_PATH": str(config_path), "HYDROHUB_RUN_ID": record.run_id},
                               timeout=int(step.get("timeout_seconds", params.get("timeout_seconds", 86400))))
            except BaseException:
                registry.finish_run(record.run_id, False)
                raise
            registry.finish_run(record.run_id, True)
            return {"run_id": record.run_id, "output_path": str(model / "runs" / record.run_id)}
        if tool == "builtin.compute_index":
            run_id = context.get("run_id")
            if not run_id:
                raise ValueError("compute_index requires previous wflow_run step.")
            outputs = registry.output_files(next(run for run in registry.list_runs() if run.run_id == run_id))
            if not outputs:
                raise FileNotFoundError("Run has no output CSV/NetCDF.")
            destination = model / "runs" / run_id / "ssfi.csv"
            destination.parent.mkdir(parents=True, exist_ok=True)
            script = (
                "import sys\nfrom pathlib import Path\n"
                "from hydrohub.results.analysis import OutputReader, build_ssfi_table\n"
                "reader = OutputReader(Path(sys.argv[1]))\n"
                "try:\n    table = build_ssfi_table(reader, scales=(int(sys.argv[3]),))\n"
                "finally:\n    reader.close()\n"
                "if table.empty:\n    raise ValueError('No SSFI discharge data found.')\n"
                "table.to_csv(sys.argv[2])\n"
            )
            self._command(job, [sys.executable, "-u", "-c", script, str(outputs[0]), str(destination), str(params.get("timescale", 1))], root, timeout=timeout)
            return {"index_path": str(destination)}
        if tool == "builtin.save_to_registry":
            run_id = context.get("run_id")
            if not run_id:
                raise ValueError("save_to_registry requires previous wflow_run step.")
            run = next(run for run in registry.list_runs() if run.run_id == run_id)
            if run.status != "completed":
                raise ValueError("Run not completed.")
            return {"run_id": run_id}
        if tool.startswith("custom."):
            from hydrohub.core.automation import tool_catalog
            from hydrohub.plugins.custom_tools import CustomToolStore
            store = CustomToolStore(root, self.root.parent.parent / "custom_tools")
            definition = tool_catalog(self.root.parent.parent.parent, {"current": project}).get(tool)
            if not definition:
                raise ValueError(f"Custom tool unavailable for project: {tool}")
            params = {**{field["name"]: field["default"] for field in definition.get("parameters", []) if "default" in field}, **params}
            for field in definition.get("parameters", []):
                if field.get("required") and params.get(field["name"]) in (None, ""):
                    raise ValueError(f"Missing local/global parameter: {field['name']}")
            check_schema(definition.get("input_schema", {}), {**context, **params}, f"{tool} input", required=True)
            payload = json.dumps({"params": params, "context": context, "project_root": str(root), "model_root": str(model)})
            config = store.load_config(tool[7:])
            from hydrohub.plugins.custom_tools import SecretStore
            environment = {field["name"]: str(field["default"]) for field in definition.get("environment", []) if "default" in field}
            environment.update(config.get("environment", {}))
            for name in config.get("secrets", []):
                secret = SecretStore().get(root, tool[7:], name)
                if secret:
                    environment[name] = secret
            for field in definition.get("environment", []):
                if field.get("required") and not environment.get(field["name"]):
                    raise ValueError(f"Missing environment variable: {field['name']}")
            executable = definition.get("executable", {})
            custom_timeout = int(step.get("timeout_seconds", definition.get("timeout_seconds", 1800)))
            if executable.get("kind") == "http_endpoint":
                from urllib.request import Request, urlopen
                request = Request(executable["path"], data=payload.encode("utf-8"), headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=custom_timeout) as response:
                    output = response.read().decode("utf-8")
                if job["id"] in self.cancelled:
                    raise InterruptedError("Job cancelled")
                self._log(job, output)
            else:
                path = Path(executable["path"]) if executable else definition["path"] / "main.py"
                path = path if path.is_absolute() else root / path
                if executable.get("kind") == "cli_binary":
                    command = [str(path), *executable.get("arguments", [])]
                elif executable.get("entry"):
                    wrapper = ("import json,runpy,sys; ns=runpy.run_path(sys.argv[1]); "
                               "result=ns[sys.argv[2]](json.load(sys.stdin)); print(json.dumps(result))")
                    command = [config.get("interpreter", sys.executable), "-u", "-c", wrapper, str(path), executable["entry"]]
                else:
                    command = [config.get("interpreter", sys.executable), "-u", str(path), *executable.get("arguments", [])]
                output = self._command(job, command, root, env=environment, timeout=custom_timeout, stdin=payload)
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("status") not in {"ok", "warning", "error"}:
                raise ValueError("Custom tool must output JSON with status ok/warning/error.")
            if definition.get("output_schema"):
                check_schema(definition["output_schema"], result, f"{tool} output", required=True)
            return result
        raise ValueError(f"Unsupported tool: {tool}")

    def _run(self, job: dict, spec: dict, projects: dict):
        target = job["target"]
        key = str(projects[target["project_id"]]["models"][target["model_id"]].resolve())
        with self.lock:
            model_lock = self.model_locks.setdefault(key, threading.Lock())
        # ponytail: per-model lock retained until exit; number of distinct models bounds growth.
        with model_lock:
            self._execute(job, spec, projects)

    def _execute(self, job: dict, spec: dict, projects: dict):
        target = job["target"]
        context = {}
        policy = spec.get("on_failure", {})
        try:
            if job["id"] in self.cancelled:
                return
            project = projects[target["project_id"]]
            model = project["models"][target["model_id"]]
            self._emit(job, "RUNNING")
            nodes = {node["id"]: node for node in spec["nodes"]}
            order = graph_order(spec["nodes"])
            reachable = {order[0]}
            for identifier in order:
                step = nodes[identifier]
                if identifier not in reachable:
                    job["node_status"][identifier] = "Skipped"
                    continue
                policy = step.get("on_failure", spec.get("on_failure", {}))
                if job["id"] in self.cancelled:
                    raise InterruptedError("Job cancelled")
                job["step"] = step["id"]
                job["node_status"][identifier] = "Running"
                self._emit(job, "RUNNING")
                retries = policy.get("retry_count", 0) if policy.get("strategy") == "retry" else 0
                for attempt in range(retries + 1):
                    try:
                        self._log(job, f"START {step['id']} ({step['tool']})")
                        result = self._step(job, step, project, model, context)
                        if job["id"] in self.cancelled:
                            raise InterruptedError("Job cancelled")
                        result_status = result.get("status", "ok")
                        if result_status == "error" and "error" not in step.get("next_on_status", {}):
                            raise RuntimeError(result.get("notes", "Tool returned error."))
                        context.update(result)
                        job["node_status"][identifier] = "Success" if result_status == "ok" else result_status.title()
                        reachable.update(step.get("next_on_status", {}).get(result_status, step.get("next", [])))
                        job["steps"].append({"id": step["id"], "status": result_status.upper(), "at": timestamp(), "output": result})
                        self._log(job, f"SUCCESS {step['id']}")
                        break
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        self._log(job, f"FAILED {step['id']}: {exc}")
                        self._emit(job, "STEP_FAILED", error=str(exc))
                        if attempt < retries:
                            self._emit(job, "RETRYING")
                            self._emit(job, "RUNNING")
                        elif policy.get("strategy") == "skip_step":
                            job["node_status"][identifier] = "Warning"
                            reachable.update(step.get("next_on_status", {}).get("warning", step.get("next", [])))
                            job["steps"].append({"id": step["id"], "status": "WARNING", "at": timestamp(), "error": str(exc)})
                        elif "error" in step.get("next_on_status", {}):
                            job["node_status"][identifier] = "Error"
                            reachable.update(step["next_on_status"]["error"])
                            context.update(status="error", notes=str(exc))
                            job["steps"].append({"id": identifier, "status": "ERROR", "at": timestamp(), "error": str(exc)})
                            break
                        else:
                            job["node_status"][identifier] = "Failed"
                            raise
            self._emit(job, "SUCCESS", step="", error="")
        except InterruptedError:
            if job.get("step"):
                job["node_status"][job["step"]] = "Cancelled"
            self._emit(job, "CANCELLED")
        except Exception as exc:
            self._emit(job, "FAILED", error=str(exc))
        finally:
            with self.lock:
                self.cancelled.discard(job["id"])

    def retry(self, job_id: str, spec: dict, projects: dict):
        job = self.jobs[job_id]
        if job["status"] != "FAILED":
            raise ValueError("Only failed jobs can be retried.")
        return self.start(spec, projects, [job["target"]])
