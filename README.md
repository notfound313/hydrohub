# HydroHub

HydroHub is a PySide6 desktop workspace for hydrological data preparation,
quality control, HydroMT, and Wflow.jl workflows.

## Run

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m hydrohub
```

The Download, Preparing, and QC workspaces discover the existing scripts under
`sample/` and execute them unchanged through the central process manager.

## Current MVP coverage

- Embedded Leaflet map with OpenStreetMap/terrain basemaps, AOI, GeoJSON layers,
  layer control, zoom, and scale.
- Persistent project registry and standard Wflow project directory creation.
- Resumable nine-step setup state with AOI GeoJSON persistence.
- Project-scoped file tree and explicit-save text editor.
- Environment diagnostics and Global Database initialization.
- Catalog-first Wflow data catalog builder with URI validation.
- Template-driven `wflow.yml` builder and runtime TOML builder with dependency
  gates.
- Project-scoped sample Download, Preparing, and QC execution with logs and
  manifests.
- Navigation shells for HydroMT, model inspection, simulation, graph, table,
  analysis, terminal, and settings. Their Run actions remain gated until their
  required engine adapters and validators are available; the UI does not fake
  successful engine execution.

The older `venv` folder was created from Miniconda Python 3.14 and cannot load
the installed Qt DLLs on this workstation. `.venv` uses the standalone
Python.org runtime and is the supported application environment.
