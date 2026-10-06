from __future__ import annotations

import sys
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from hydrohub.ui.main_window import MainWindow
from hydrohub.ui.splash import SplashScreen
from hydrohub.ui.theme import APP_STYLESHEET


def main() -> int:
    app = QApplication(sys.argv)
    import pyqtgraph as pg
    use_opengl = (
        os.environ.get("HYDROHUB_RENDERER") == "opengl"
        and app.platformName() not in {"offscreen", "minimal"}
    )
    pg.setConfigOptions(useOpenGL=use_opengl, antialias=False)
    app.setApplicationName("HydroHub")
    app.setOrganizationName("HydroHub")
    app.setStyleSheet(APP_STYLESHEET)

    workspace = Path(__file__).resolve().parents[2]
    window = MainWindow(workspace)
    splash = SplashScreen()
    splash.show()

    def open_window() -> None:
        window.show()
        splash.finish(window)

    QTimer.singleShot(900, open_window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
