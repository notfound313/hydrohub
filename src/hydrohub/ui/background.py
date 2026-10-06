"""Workers return data only; QObject receivers are always queued to the UI thread."""
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, function, signals):
        super().__init__()
        self.function, self.signals = function, signals
        self.cancelled = False

    def run(self):
        try:
            self.signals.done.emit(None if self.cancelled else self.function())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class BackgroundJob(QObject):
    _active = set()
    _pool = None

    def __init__(self, parent, function, completed, failed):
        # Keep signal objects alive until the runnable finishes, even if its page
        # is destroyed during a project switch.
        super().__init__()
        self._cancelled = False
        if parent is not None:
            parent.destroyed.connect(self.cancel)
        self.completed, self.failed = completed, failed
        self.signals = _Signals(self)
        self.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.task = _Task(function, self.signals)

    def start(self):
        self._active.add(self)
        if BackgroundJob._pool is None:
            BackgroundJob._pool = QThreadPool()
            # Serialize heavy readers: bounded peak RAM and no concurrent HDF5 IO.
            BackgroundJob._pool.setMaxThreadCount(1)
        BackgroundJob._pool.start(self.task)

    def cancel(self, *_args):
        self._cancelled = True
        self.task.cancelled = True
        self.completed = self.failed = None

    @Slot(object)
    def _done(self, value):
        try:
            if not self._cancelled:
                self.completed(value)
            elif hasattr(value, "close"):
                value.close()
        except Exception as exc:
            if not self._cancelled:
                self.failed(str(exc))
        finally:
            self._active.discard(self)
            self.completed = self.failed = None
            self.task.function = None
            self.deleteLater()

    @Slot(str)
    def _failed(self, message):
        try:
            if not self._cancelled:
                self.failed(message)
        finally:
            self._active.discard(self)
            self.completed = self.failed = None
            self.task.function = None
            self.deleteLater()
