from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QComboBox
from PySide6.QtCore import Qt
from hydrohub.utils.metadata import get_app_metadata

from hydrohub.ui.theme import theme_manager


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.hydromt_version = QLabel("Checking...")
        self.julia_version = QLabel("Checking...")
        self.theme = QComboBox()
        self.theme.addItems(["System", "Dark", "Light"])
        self.theme.setCurrentText(theme_manager().preference)
        self.theme.currentTextChanged.connect(theme_manager().set_preference)     
        self.theme.setFixedWidth(150)
        form.addRow("HydroMT ", self.hydromt_version)
        form.addRow("Julia ", self.julia_version)
        form.addRow("Appearance", self.theme)        
        layout.addLayout(form)
        layout.addStretch()
        app = get_app_metadata()
        layout.addStretch()
        developed_by = QLabel(
            f"Developed by {app.author} • v{app.version}"
        )
        developed_by.setObjectName("developedBy")
        developed_by.setAlignment(Qt.AlignCenter)
        layout.addWidget(developed_by)


    def set_versions(self, hydromt: str, julia: str) -> None:
        self.hydromt_version.setText(hydromt)
        self.julia_version.setText(julia)

    def refresh_context(self):
        pass
