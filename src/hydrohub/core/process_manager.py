from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from hydrohub.plugins.registry import ToolDefinition


class ProcessSlots(QObject):
    changed = Signal()

    def __init__(self, limit: int = 3, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.limit = limit
        self.active: set[ProcessManager] = set()

    def acquire(self, manager: ProcessManager) -> None:
        if len(self.active) >= self.limit:
            raise RuntimeError(f"Maximum {self.limit} processes are already running.")
        self.active.add(manager)
        self.changed.emit()

    def release(self, manager: ProcessManager) -> None:
        if manager in self.active:
            self.active.remove(manager)
            self.changed.emit()


class ProcessManager(QObject):
    output_received = Signal(str)
    state_changed = Signal(str)
    finished = Signal(int, str)

    def __init__(
        self, parent: QObject | None = None, *, slots: ProcessSlots | None = None,
        project_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.slots = slots
        self.project_root = project_root
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.started.connect(lambda: self.state_changed.emit("RUNNING"))
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._process_error)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._process.kill)
        self._finalized = True
        self._tool: ToolDefinition | None = None
        self._started_at: str | None = None
        self._manifest_dir: Path | None = None
        self._log_file: Path | None = None
        self._buffer: list[str] = []
        self._parameters: list[str] = []
        self._run_id = ""

    @property
    def is_running(self) -> bool:
        return not self._finalized or self._process.state() != QProcess.ProcessState.NotRunning

    def run_tool(
        self,
        tool: ToolDefinition,
        interpreter: Path,
        working_directory: Path,
        manifest_dir: Path,
        extra_environment: dict[str, str] | None = None,
        arguments: list[str] | None = None,
    ) -> None:
        self._start_process(
            tool,
            interpreter,
            ["-u", "-m", tool.module, *(arguments or [])],
            list(arguments or []),
            working_directory,
            manifest_dir,
            extra_environment,
        )

    def run_command(
        self,
        tool: ToolDefinition,
        executable: Path,
        arguments: list[str],
        working_directory: Path,
        manifest_dir: Path,
        extra_environment: dict[str, str] | None = None,
    ) -> None:
        self._start_process(
            tool,
            executable,
            arguments,
            arguments,
            working_directory,
            manifest_dir,
            extra_environment,
        )

    def _start_process(
        self,
        tool: ToolDefinition,
        executable: Path,
        process_arguments: list[str],
        manifest_parameters: list[str],
        working_directory: Path,
        manifest_dir: Path,
        extra_environment: dict[str, str] | None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Another tool is already running.")
        if not executable.exists():
            raise FileNotFoundError(f"Executable not found: {executable}")

        manifest_dir.mkdir(parents=True, exist_ok=True)
        working_directory.mkdir(parents=True, exist_ok=True)
        self._tool = tool
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._manifest_dir = manifest_dir
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_id = tool.id.replace(".", "_")
        self._run_id = f"{stamp}_{uuid.uuid4().hex[:8]}"
        self._log_file = manifest_dir / f"{self._run_id}_{safe_id}.log"
        self._buffer = []
        self._parameters = manifest_parameters
        self._finalized = False

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        for key, value in (extra_environment or {}).items():
            environment.insert(key, value)
        self._process.setProcessEnvironment(environment)
        self._process.setWorkingDirectory(str(working_directory))
        if self.slots:
            try:
                self.slots.acquire(self)
            except RuntimeError:
                self._finalized = True
                raise
        self.state_changed.emit("STARTING")
        self._process.start(str(executable), process_arguments)

    def stop(self) -> None:
        if not self.is_running:
            return
        self.state_changed.emit("STOPPING")
        self._process.terminate()
        self._kill_timer.start(3000)

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._buffer.append(f"ERROR: {self._process.errorString()}\n")
            self.output_received.emit(self._buffer[-1])
            self._on_finished(-1, QProcess.ExitStatus.CrashExit)

    def _read_output(self) -> None:
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if text:
            self._buffer.append(text)
            self.output_received.emit(text)

    def _on_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._kill_timer.stop()
        self._read_output()
        log_text = "".join(self._buffer)
        log_lines = [line.strip() for line in log_text.splitlines() if line.strip()]
        warning_lines = [line for line in log_lines if "warning" in line.lower()]
        error_lines = [
            line
            for line in log_lines
            if "error" in line.lower() or "traceback" in line.lower()
        ]
        output_lines = [
            line
            for line in log_lines
            if line.startswith(("[WRITE]", "[OK]", "CREATED", "CREATED:"))
        ]
        status = "ERROR" if exit_code else ("WARNING" if warning_lines else "SUCCESS")
        self.state_changed.emit(status)
        if self._log_file:
            self._log_file.write_text(log_text, encoding="utf-8")

        manifest_path = ""
        if self._tool and self._manifest_dir:
            completed_at = datetime.now(timezone.utc)
            started_at = datetime.fromisoformat(self._started_at) if self._started_at else completed_at
            manifest = {
                "tool_id": self._tool.id,
                "tool_name": self._tool.name,
                "category": self._tool.category,
                "module": self._tool.module,
                "tool_version": self._tool.version,
                "command": [self._process.program(), *self._process.arguments()],
                "parameters": self._parameters,
                "working_directory": self._process.workingDirectory(),
                "started_at": self._started_at,
                "completed_at": completed_at.isoformat(),
                "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
                "exit_code": exit_code,
                "status": status,
                "summary": log_lines[-8:],
                "outputs": output_lines,
                "warnings": warning_lines,
                "errors": error_lines[-12:],
                "log_file": str(self._log_file) if self._log_file else None,
                "environment_profile": {"python": self._process.program()},
                "platform": os.name,
            }
            safe_id = self._tool.id.replace(".", "_")
            path = self._manifest_dir / f"{self._run_id}_{safe_id}.json"
            path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            manifest_path = str(path)
        if self.slots:
            self.slots.release(self)
        self.finished.emit(exit_code, manifest_path)
