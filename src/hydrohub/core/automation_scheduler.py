"""Application-lifetime scheduler; schedules do not depend on an open tab."""
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QTimer, Signal

from hydrohub.core.automation import AutomationStore, discover_projects, next_trigger, tool_catalog, validate_pipeline
from hydrohub.core.automation_runner import AutomationRunner


class AutomationScheduler(QObject):
    changed = Signal()
    failed = Signal(str)

    def __init__(self, workspace: Path, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.store = AutomationStore(workspace)
        self.runner = AutomationRunner(workspace, self.changed.emit)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def start(self, spec, targets=None):
        projects = discover_projects(self.workspace)
        spec = validate_pipeline(yaml.safe_dump({"automation": spec}), tool_catalog(self.workspace, projects), projects)
        jobs = self.runner.start(spec, projects, targets)
        self.changed.emit()
        return jobs

    def set_enabled(self, spec, enabled):
        due = next_trigger(spec["trigger"], None, datetime.now().astimezone()) if enabled else None
        self.store.update_state(spec["id"], enabled=enabled, next_due=due.isoformat() if due else None)
        self.changed.emit()

    def tick(self):
        now = datetime.now().astimezone()
        for spec in self.store.list():
            try:
                state = self.store.state(spec["id"])
                if not state.get("enabled") or spec["id"] in self.runner.active or spec["trigger"]["type"] == "manual":
                    continue
                due = datetime.fromisoformat(state["next_due"]) if state.get("next_due") else None
                if due is None:
                    self.set_enabled(spec, True)
                elif due <= now:
                    self.start(spec)
                    # Coalesce missed occurrences; never launch a backlog of overlapping runs.
                    following = next_trigger(spec["trigger"], due, now)
                    if following <= now:
                        if spec["trigger"]["mode"] == "interval":
                            interval = timedelta(days=spec["trigger"]["days"])
                            following += ((now - following) // interval + 1) * interval
                        else:
                            following = next_trigger(spec["trigger"], None, now)
                    self.store.update_state(spec["id"], last_trigger=due.isoformat(), next_due=following.isoformat())
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                self.store.update_state(spec["id"], enabled=False)
                self.failed.emit(f"{spec['id']}: {exc}")

    def shutdown(self):
        self.timer.stop()
        for job_id in list(self.runner.jobs):
            self.runner.cancel(job_id)
