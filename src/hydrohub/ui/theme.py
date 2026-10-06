from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme, setThemeColor

DARK = dict(bg='#202020', surface='#2a2a2b', field='#323234', hover='#3a3a3d',
            rail='#232324', border='#444446', text='#f2f2f2', muted='#c6c6c8',
            accent='#34c7c2', ink='#06302e', soft='#294441')
LIGHT = dict(bg='#f3f3f3', surface='#ffffff', field='#fafafa', hover='#f1f1f1',
             rail='#ececec', border='#d4d4d6', text='#1b1b1c', muted='#4a4a4d',
             accent='#0f8f89', ink='#ffffff', soft='#dcefed')


def stylesheet(mode='dark'):
    return '''
    QWidget { font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Inter'; font-size: 13px; color: %(text)s; background: %(bg)s; }
    QLabel, QCheckBox, QRadioButton { background: transparent; }
    QFrame#sidebar { background: %(rail)s; border-right: 1px solid %(border)s; }
    QFrame#sidebar QLabel, QFrame#sidebar QToolButton, QFrame#sidebar QPushButton { margin-left: 10px; }
    QToolBar, QFrame#topbar { background: %(surface)s; padding: 6px; spacing: 8px; }
    QLabel#brand { font-size: 16px; font-weight: 600; }
    QLabel#eyebrow { color: %(accent)s; font-size: 12px; }
    QLabel#pageTitle { font-size: 22px; font-weight: 600; }
    QLabel#sectionTitle { font-size: 14px; font-weight: 600; }
    QLabel#muted, QLabel#heroLabel { color: %(muted)s; }
    QLabel#heroNumber { color: %(accent)s; font-size: 26px; font-weight: 600; }
    QPushButton, QToolButton { min-height: 28px; padding: 2px 12px; border-radius: 6px; border: 1px solid %(border)s; background: %(surface)s; }
    QPushButton:hover, QToolButton:hover { background: %(hover)s; }
    QPushButton:focus, QToolButton:focus { border-color: %(accent)s; }
    QPushButton:disabled { color: #858589; }
    QPushButton#primary { background: %(accent)s; color: %(ink)s; font-weight: 600; }
    QToolButton#nav { padding: 2px 8px; border: 0; background: transparent; color: %(muted)s; }
    QToolButton#nav:checked { background: %(soft)s; color: %(text)s; }
    QToolButton#nav:hover { background: %(hover)s; }
    QLineEdit, QComboBox, QAbstractSpinBox { min-height: 28px; padding: 2px 8px; background: %(field)s; border: 1px solid %(border)s; border-radius: 6px; }
    QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus { border-color: %(accent)s; }
    QPlainTextEdit, QTextBrowser, QTextEdit { background: %(field)s; border: 1px solid %(border)s; border-radius: 6px; padding: 10px; font-family: 'JetBrains Mono', 'Cascadia Code', monospace; font-size: 12px; }
    QAbstractItemView { background: %(surface)s; alternate-background-color: %(field)s; border: 1px solid %(border)s; border-radius: 6px; selection-background-color: %(soft)s; selection-color: %(text)s; }
    QAbstractItemView::item { min-height: 26px; padding: 3px; }
    QHeaderView::section { background: %(field)s; color: %(muted)s; border: 0; padding: 8px; }
    QTabWidget::pane { border: 0; }
    QTabBar::tab { background: transparent; color: %(muted)s; padding: 10px 12px; border-bottom: 2px solid transparent; }
    QTabBar::tab:selected { color: %(text)s; border-bottom: 2px solid %(accent)s; }
    QTabBar::tab:hover { background: %(hover)s; }
    QFrame#panel, QFrame#heroCard, QFrame#reportPanel { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px; }
    QGroupBox { border: 1px solid %(border)s; border-radius: 8px; margin-top: 14px; padding: 16px; }
    QGroupBox::title { subcontrol-origin: margin; left: 12px; }
    QFrame#terminalPanel, QFrame#terminalPane { background: #111214; border: 1px solid #38383b; border-radius: 8px; }
    QFrame#terminalHeader, QLabel#terminalCommand, QLabel#terminalTitle { background: #191a1c; color: #c6c6c8; padding: 8px; font-family: 'JetBrains Mono', 'Cascadia Code', monospace; }
    QPlainTextEdit#executionTerminal, QTextBrowser#terminalOutput { background: #111214; color: #f2f2f2; border: 0; }
    QLabel#editorBreadcrumb, QLabel#reportValue { color: %(muted)s; font-family: 'JetBrains Mono', 'Cascadia Code', monospace; font-size: 12px; }
    QLabel#editorStatus { background: %(soft)s; color: %(accent)s; }
    QLabel#statusRunning { color: %(accent)s; }
    QLabel#statusSuccess { color: #79b97f; }
    QLabel#statusWarning { color: #bb853c; }
    QLabel#statusError { color: #d0635b; }
    QProgressBar { border: 0; background: %(field)s; border-radius: 4px; min-height: 6px; max-height: 16px; text-align: center; }
    QProgressBar::chunk { background: %(accent)s; border-radius: 4px; }
    QSplitter::handle { background: %(border)s; width: 2px; height: 2px; }
    QScrollBar:vertical { width: 10px; background: %(bg)s; }
    QScrollBar::handle:vertical { background: %(border)s; border-radius: 4px; min-height: 24px; }
    QStatusBar, QMenuBar, QMenu { background: %(surface)s; }
    QMenu::item:selected { background: %(soft)s; }
    ''' % (LIGHT if mode == 'light' else DARK)


APP_STYLESHEET = stylesheet()


class ThemeManager(QObject):
    changed = Signal(str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.settings = QSettings('HydroHub', 'HydroHub')
        self.preference = self.settings.value('appearance/theme', 'system')
        app.styleHints().colorSchemeChanged.connect(self._system_changed)
        self.apply()

    def _system_changed(self, *_args):
        if self.preference == 'system':
            self.apply()

    def set_preference(self, preference):
        self.preference = preference
        self.settings.setValue('appearance/theme', preference)
        self.apply()

    def apply(self):
        mode = self.preference
        if mode == 'system':
            mode = 'light' if self.app.styleHints().colorScheme() == Qt.ColorScheme.Light else 'dark'
        self.mode = mode
        tokens = LIGHT if mode == 'light' else DARK
        setTheme(Theme.LIGHT if mode == 'light' else Theme.DARK)
        setThemeColor(tokens['accent'])
        self.app.setProperty('themeMode', mode)
        self.app.setStyleSheet(stylesheet(mode))
        import pyqtgraph as pg
        pg.setConfigOptions(background=tokens['surface'], foreground=tokens['text'])
        for widget in self.app.allWidgets():
            if isinstance(widget, pg.PlotWidget):
                widget.setBackground(tokens['surface'])
                for name in ('left', 'bottom'):
                    widget.getAxis(name).setPen(tokens['muted'])
                    widget.getAxis(name).setTextPen(tokens['muted'])
        self.changed.emit(mode)


def theme_manager():
    app = QApplication.instance()
    if not hasattr(app, 'hydrohub_theme'):
        app.hydrohub_theme = ThemeManager(app)
    return app.hydrohub_theme
