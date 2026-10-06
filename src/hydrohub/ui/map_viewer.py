from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView


class IndonesiaMapWidget(QWebEngineView):
    """Embedded Leaflet map with project AOI and GeoJSON layer hooks."""

    def __init__(self) -> None:
        super().__init__()
        self._ready = False
        self._has_aoi = False
        self._pending_scripts: list[str] = []
        self.setMinimumSize(620, 400)
        self.setAccessibleName("Interactive Leaflet map of Indonesia")
        settings = self.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        self._started = False
        self.loadFinished.connect(self._loaded)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._started:
            self._started = True
            html_path = Path(__file__).with_name("assets") / "map.html"
            self.load(QUrl.fromLocalFile(str(html_path.resolve())))

    def _loaded(self, success: bool) -> None:
        self._ready = success
        if not success:
            return
        pending, self._pending_scripts = self._pending_scripts, []
        for script in pending:
            self.page().runJavaScript(script)

    def _run(self, script: str) -> None:
        if self._ready:
            self.page().runJavaScript(script)
        else:
            self._pending_scripts.append(script)

    def zoom_in(self) -> None:
        self._run("map.zoomIn();")

    def zoom_out(self) -> None:
        self._run("map.zoomOut();")

    def reset_view(self) -> None:
        if self._has_aoi:
            self._run("window.hydrohubMap.fitAoi();")
        else:
            self._run("window.hydrohubMap.fitIndonesia();")

    def reset_indonesia(self) -> None:
        self._run("window.hydrohubMap.fitIndonesia();")

    def invalidate_size(self) -> None:
        self._run("map.invalidateSize();")

    def save_png(self, path: Path) -> None:
        pixmap = self.grab()
        if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
            raise OSError(f"Peta tidak dapat disimpan ke '{path}'.")

    def set_aoi(self, geojson: dict, *, fit: bool = True) -> None:
        payload = json.dumps(geojson, allow_nan=False)
        self._has_aoi = True
        fit_flag = "true" if fit else "false"
        self._run(f"window.hydrohubMap.setAOI({payload}, {fit_flag});")

    def add_geojson(
        self, name: str, geojson: dict, *, choropleth: bool = False, fit: bool = False
    ) -> None:
        safe_name = json.dumps(name)
        payload = json.dumps(geojson, allow_nan=False)
        self._run(
            f"window.hydrohubMap.addGeoJSON({safe_name}, {payload}, "
            f"{str(choropleth).lower()}, {str(fit).lower()});"
        )

    def set_raster_layer(self, name: str, image_url: str, bounds: list) -> None:
        self._run(
            "window.hydrohubMap.setRaster("
            f"{json.dumps(name)}, {json.dumps(image_url)}, {json.dumps(bounds)});"
        )

    def remove_data_layer(self, name: str) -> None:
        self._run(f"window.hydrohubMap.removeLayer({json.dumps(name)});")

    def fit_bounds(self, bounds: list) -> None:
        self._run(f"window.hydrohubMap.fitBounds({json.dumps(bounds)});")

    def focus_marker(
        self, latitude: float, longitude: float, popup_html: str, tooltip: str
    ) -> None:
        self._run(
            "window.hydrohubMap.focusMarker("
            f"{float(latitude)}, {float(longitude)}, {json.dumps(popup_html)}, "
            f"{json.dumps(tooltip)});"
        )

    def clear_data_layers(self) -> None:
        self._run("window.hydrohubMap.clearData();")

    def fit_aoi(self) -> None:
        self._run("window.hydrohubMap.fitAoi();")
