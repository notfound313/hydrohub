from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QProgressBar, QSplashScreen, QVBoxLayout, QWidget


class SplashScreen(QSplashScreen):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(520, 300)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)

        content = QWidget(self)
        content.setGeometry(0, 0, 520, 300)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.addStretch()

        title = QLabel("HydroHub")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Hydrological Model Workspace")
        subtitle.setObjectName("muted")
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setFixedHeight(2)
        progress.setTextVisible(False)
        progress.setAccessibleName("Application initialization progress")

        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addWidget(subtitle)
        layout.addStretch()
        layout.addWidget(progress)
        layout.addSpacing(12)
        status = QLabel("Loading built-in tool registry...")
        status.setObjectName("muted")
        layout.addWidget(status)

        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        screen_geo = screen.availableGeometry()
        x = screen_geo.center().x() - self.width() // 2
        y = screen_geo.center().y() - self.height() // 2
        self.move(x, y)