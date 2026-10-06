import unittest
from pathlib import Path


class LeafletAssetsTests(unittest.TestCase):
    def test_local_leaflet_bundle_and_map_api_are_packaged(self) -> None:
        assets = Path(__file__).resolve().parents[1] / "src" / "hydrohub" / "ui" / "assets"

        self.assertGreater((assets / "leaflet.js").stat().st_size, 100_000)
        self.assertGreater((assets / "leaflet.css").stat().st_size, 10_000)
        html = (assets / "map.html").read_text(encoding="utf-8")
        self.assertIn("L.map('map'", html)
        self.assertIn("setAOI", html)
        self.assertIn("addGeoJSON", html)
        self.assertIn("setRaster", html)
        self.assertIn("removeLayer", html)
        self.assertIn("'Peta Netral': neutral", html)
        self.assertIn("street.addTo(map)", html)
        self.assertIn("color: '#0066ff'", html)
        self.assertIn("geometryStyles", html)
        self.assertIn("match: /river/", html)
        self.assertIn("match: /basin/", html)
        self.assertIn("match: /outlet|station|gauge/", html)


if __name__ == "__main__":
    unittest.main()
