from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from hydrohub.core.process_manager import ProcessManager
from hydrohub.plugins.registry import ToolDefinition


ACTIVE_STATES = {
    "WAITING_FOR_ENV",
    "CHECKING_DEPENDENCIES",
    "AWAITING_APPROVAL",
    "INSTALLING",
    "STARTING",
    "RUNNING",
    "STOPPING",
}


@dataclass
class CustomToolSession:
    id: str
    tool_id: str
    tool_name: str
    project_name: str
    project_root: Path
    command: str
    manager: ProcessManager
    interpreter: Path
    arguments: list[str]
    environment: dict[str, str]
    tool: dict
    project: object
    dependencies: tuple[str, ...] = ()
    status: str = "STARTING"
    output: str = ""
    manifest_path: str = ""
    exit_code: int | None = None
    setup_process: QProcess | None = None
    setup_report: Path | None = None
    pending_install: list[str] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.manager.is_running or self.status in ACTIVE_STATES


class CustomToolSessionHub(QObject):
    session_added = Signal(str)
    session_changed = Signal(str)
    session_output = Signal(str, str)
    session_finished = Signal(str, int, str)
    dependency_install_required = Signal(str, object, str)
    idle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sessions: dict[str, CustomToolSession] = {}
        self._dependency_queues: dict[str, list[str]] = {}
        self._dependency_active: dict[str, str] = {}
        self._pending_launches: dict[str, list[str]] = {}
        self._pending_restarts: set[str] = set()

    @property
    def has_running(self) -> bool:
        return any(session.is_running for session in self.sessions.values())

    @staticmethod
    def _interpreter_key(interpreter: Path) -> str:
        return str(interpreter.expanduser().resolve()).casefold()

    def start(
        self,
        tool: dict,
        project,
        interpreter: Path,
        arguments: list[str],
        environment: dict[str, str],
    ) -> CustomToolSession:
        interpreter = Path(interpreter)
        if not interpreter.exists():
            raise FileNotFoundError(f"Executable not found: {interpreter}")
        session_id = uuid.uuid4().hex
        manager = ProcessManager(self)
        dependencies = tuple(tool.get("dependencies", ()))
        session = CustomToolSession(
            session_id,
            tool["id"],
            tool["name"],
            project.name,
            project.root,
            " ".join([str(interpreter), *arguments]),
            manager,
            interpreter,
            list(arguments),
            dict(environment),
            tool,
            project,
            dependencies,
            "WAITING_FOR_ENV" if dependencies else "STARTING",
        )
        self.sessions[session_id] = session
        manager.output_received.connect(lambda text, sid=session_id: self._output(sid, text))
        manager.state_changed.connect(lambda state, sid=session_id: self._state(sid, state))
        manager.finished.connect(
            lambda code, manifest, sid=session_id: self._finished(sid, code, manifest)
        )
        self.session_added.emit(session_id)
        if dependencies:
            key = self._interpreter_key(interpreter)
            self._dependency_queues.setdefault(key, []).append(session_id)
            self._append(session, f"[dependencies] Waiting to check {', '.join(dependencies)}\n")
            self._advance_dependency_queue(key)
        elif self._interpreter_key(interpreter) in self._dependency_active:
            session.status = "WAITING_FOR_ENV"
            self._pending_launches.setdefault(self._interpreter_key(interpreter), []).append(
                session_id
            )
            self._append(session, "[dependencies] Waiting for interpreter update to finish.\n")
        else:
            self._launch_tool(session)
        return session

    def approve_install(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or session.status != "AWAITING_APPROVAL":
            return
        self._start_setup_process(session, "INSTALLING")

    def reject_install(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or session.status != "AWAITING_APPROVAL":
            return
        self._append(session, "[dependencies] Installation cancelled.\n")
        self._finalize_setup(session, "CANCELLED", -1)

    def stop(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session or not session.is_running:
            return
        if session.status in {"WAITING_FOR_ENV", "AWAITING_APPROVAL"}:
            self._append(session, "[dependencies] Session cancelled.\n")
            self._finalize_setup(session, "CANCELLED", -1)
            return
        if session.setup_process and session.setup_process.state() != QProcess.ProcessState.NotRunning:
            session.status = "STOPPING"
            self.session_changed.emit(session_id)
            process = session.setup_process
            process.terminate()
            QTimer.singleShot(
                3000,
                lambda p=process: p.kill()
                if p.state() != QProcess.ProcessState.NotRunning
                else None,
            )
            return
        session.manager.stop()

    def restart(self, session_id: str) -> CustomToolSession | None:
        session = self.sessions.get(session_id)
        if not session:
            return None
        if session.is_running:
            if session_id not in self._pending_restarts:
                self._pending_restarts.add(session_id)
                self._append(session, "[restart] Stopping current process before restart.\n")
                self.stop(session_id)
            return None
        return self._restart(session)

    def close(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session or session.is_running:
            return False
        self.sessions.pop(session_id)
        self._pending_restarts.discard(session_id)
        session.manager.deleteLater()
        if session.setup_process:
            session.setup_process.deleteLater()
        self._remove_from_queue(session)
        self.session_changed.emit(session_id)
        return True

    def stop_all(self) -> None:
        for session_id in list(self.sessions):
            self.stop(session_id)

    def _launch_tool(self, session: CustomToolSession) -> bool:
        definition = ToolDefinition(
            f"custom.{session.tool_id}",
            session.tool_name,
            "custom",
            str(session.tool["path"] / session.tool.get("entrypoint", "main.py")),
            session.tool.get("description", ""),
            (),
            version=str(session.tool.get("version", "1")),
        )
        try:
            session.manager.run_command(
                definition,
                session.interpreter,
                session.arguments,
                session.project.root,
                session.project.manifests_dir,
                extra_environment=session.environment,
            )
        except (OSError, RuntimeError) as exc:
            self._append(session, f"ERROR: Tool could not start: {exc}\n")
            session.status = "ERROR"
            session.exit_code = -1
            self.session_finished.emit(session.id, -1, "")
            self.session_changed.emit(session.id)
            self._emit_idle_if_needed()
            return False
        return True

    def _advance_dependency_queue(self, key: str) -> None:
        if key in self._dependency_active:
            return
        queue = self._dependency_queues.get(key, [])
        while queue and queue[0] not in self.sessions:
            queue.pop(0)
        if not queue:
            self._dependency_queues.pop(key, None)
            return
        session = self.sessions[queue[0]]
        execution_active = any(
            other.id != session.id
            and self._interpreter_key(other.interpreter) == key
            and other.manager.is_running
            for other in self.sessions.values()
        )
        if execution_active:
            session.status = "WAITING_FOR_ENV"
            self.session_changed.emit(session.id)
            return
        self._dependency_active[key] = session.id
        self._start_setup_process(session, "CHECKING_DEPENDENCIES")

    def _start_setup_process(self, session: CustomToolSession, phase: str) -> None:
        if session.setup_process:
            session.setup_process.deleteLater()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        session.setup_process = process
        session.status = phase
        session.pending_install = []
        if phase == "CHECKING_DEPENDENCIES":
            session.setup_report = session.project.manifests_dir / f".dependency_{session.id}.json"
            session.project.manifests_dir.mkdir(parents=True, exist_ok=True)
            arguments = [
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--disable-pip-version-check",
                "--report",
                str(session.setup_report),
                *session.dependencies,
            ]
            self._append(session, "[dependencies] Checking selected interpreter.\n")
        else:
            arguments = [
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *session.dependencies,
            ]
            self._append(session, f"[dependencies] Installing {', '.join(session.dependencies)}\n")
        process.readyReadStandardOutput.connect(
            lambda sid=session.id, p=process: self._read_setup_output(sid, p)
        )
        process.finished.connect(
            lambda code, _status, sid=session.id, current_phase=phase, p=process: self._setup_finished(
                sid, current_phase, code, p
            )
        )
        process.errorOccurred.connect(
            lambda error, sid=session.id, current_phase=phase, p=process: self._setup_error(
                sid, current_phase, error, p
            )
        )
        self.session_changed.emit(session.id)
        process.start(str(session.interpreter), arguments)

    def _read_setup_output(self, session_id: str, process: QProcess) -> None:
        session = self.sessions.get(session_id)
        if not session or session.setup_process is not process:
            return
        text = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if text:
            self._append(session, text)

    def _setup_error(
        self, session_id: str, phase: str, error: QProcess.ProcessError, process: QProcess
    ) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        session = self.sessions.get(session_id)
        if not session or session.setup_process is not process or session.status != phase:
            return
        self._append(session, f"[dependencies] ERROR: {process.errorString()}\n")
        self._finalize_setup(session, "ERROR", -1)

    def _setup_finished(
        self, session_id: str, phase: str, exit_code: int, process: QProcess
    ) -> None:
        session = self.sessions.get(session_id)
        if not session or session.setup_process is not process or session.status not in {phase, "STOPPING"}:
            return
        self._read_setup_output(session_id, process)
        if session.status == "STOPPING":
            self._finalize_setup(session, "CANCELLED", exit_code or -1)
            return
        if exit_code:
            self._append(session, f"[dependencies] {phase.lower()} failed with exit code {exit_code}.\n")
            self._finalize_setup(session, "ERROR", exit_code)
            return
        if phase == "INSTALLING":
            self._append(session, "[dependencies] Installation complete. Starting tool.\n")
            self._launch_tool(session)
            self._release_dependency_slot(session)
            return
        try:
            report = json.loads(session.setup_report.read_text(encoding="utf-8"))
            installs = report.get("install", [])
            session.pending_install = [
                f"{item.get('metadata', {}).get('name', 'package')}=={item.get('metadata', {}).get('version', '?')}"
                for item in installs
            ]
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            self._append(session, f"[dependencies] ERROR: Cannot read pip report: {exc}\n")
            self._finalize_setup(session, "ERROR", -1)
            return
        finally:
            if session.setup_report:
                session.setup_report.unlink(missing_ok=True)
        if session.pending_install:
            session.status = "AWAITING_APPROVAL"
            self.session_changed.emit(session.id)
            self.dependency_install_required.emit(
                session.id, list(session.pending_install), str(session.interpreter)
            )
            return
        self._append(session, "[dependencies] Requirements already satisfied. Starting tool.\n")
        self._launch_tool(session)
        self._release_dependency_slot(session)

    def _finalize_setup(self, session: CustomToolSession, status: str, exit_code: int) -> None:
        session.status = status
        session.exit_code = exit_code
        if session.setup_report:
            session.setup_report.unlink(missing_ok=True)
        self.session_finished.emit(session.id, exit_code, "")
        self.session_changed.emit(session.id)
        self._release_dependency_slot(session)
        self._restart_if_pending(session)
        self._emit_idle_if_needed()

    def _remove_from_queue(self, session: CustomToolSession) -> None:
        key = self._interpreter_key(session.interpreter)
        queue = self._dependency_queues.get(key, [])
        if session.id in queue:
            queue.remove(session.id)
        if self._dependency_active.get(key) == session.id:
            self._dependency_active.pop(key, None)
        pending = self._pending_launches.get(key, [])
        if session.id in pending:
            pending.remove(session.id)
        if not pending:
            self._pending_launches.pop(key, None)

    def _launch_pending(self, key: str) -> None:
        if key in self._dependency_active:
            return
        pending = self._pending_launches.pop(key, [])
        for session_id in pending:
            session = self.sessions.get(session_id)
            if session and session.status == "WAITING_FOR_ENV":
                self._launch_tool(session)

    def _release_dependency_slot(self, session: CustomToolSession) -> None:
        key = self._interpreter_key(session.interpreter)
        self._remove_from_queue(session)
        self._launch_pending(key)
        self._advance_dependency_queue(key)

    def _append(self, session: CustomToolSession, text: str) -> None:
        session.output += text
        self.session_output.emit(session.id, text)

    def _output(self, session_id: str, text: str) -> None:
        session = self.sessions.get(session_id)
        if session:
            self._append(session, text)

    def _state(self, session_id: str, state: str) -> None:
        session = self.sessions.get(session_id)
        if session:
            session.status = state
            self.session_changed.emit(session_id)

    def _finished(self, session_id: str, code: int, manifest: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        session.exit_code = code
        session.manifest_path = manifest
        self.session_finished.emit(session_id, code, manifest)
        self.session_changed.emit(session_id)
        for key in list(self._pending_launches):
            self._launch_pending(key)
        for key in list(self._dependency_queues):
            self._advance_dependency_queue(key)
        self._restart_if_pending(session)
        self._emit_idle_if_needed()

    def _restart_if_pending(self, session: CustomToolSession) -> None:
        if session.id in self._pending_restarts:
            self._restart(session)

    def _restart(self, session: CustomToolSession) -> CustomToolSession:
        self._pending_restarts.discard(session.id)
        return self.start(
            dict(session.tool),
            session.project,
            session.interpreter,
            list(session.arguments),
            dict(session.environment),
        )

    def _emit_idle_if_needed(self) -> None:
        if not self.has_running:
            self.idle.emit()
