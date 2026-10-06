from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class EnvironmentCheck:
    component: str
    status: str
    version: str
    executable: str


class EnvironmentManager:
    def diagnose(self) -> list[EnvironmentCheck]:
        return [
            self._check("Python", sys.executable, ["--version"]),
            self._check("GDAL", shutil.which("gdalinfo"), ["--version"]),
            self._check("HydroMT", shutil.which("hydromt"), ["--version"]),
            self._check("Julia", shutil.which("julia"), ["--version"]),
        ]

    @staticmethod
    def _check(component: str, executable: str | None, arguments: list[str]) -> EnvironmentCheck:
        if not executable:
            return EnvironmentCheck(component, "NOT FOUND", "", "")
        try:
            result = subprocess.run(
                [executable, *arguments], capture_output=True, text=True, timeout=8, check=False
            )
            version = (result.stdout or result.stderr).strip().splitlines()[0]
            status = "READY" if result.returncode == 0 else "WARNING"
            return EnvironmentCheck(component, status, version, executable)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EnvironmentCheck(component, "ERROR", str(exc), executable)
