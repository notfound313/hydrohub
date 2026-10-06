using Pkg
using Logging
using Wflow


script_dir = dirname(@__FILE__)
model_dir = get(ENV, "HYDROHUB_MODEL_DIR", "")
isempty(model_dir) && error("HYDROHUB_MODEL_DIR is not set")
model_dir = abspath(model_dir)
global_logger(ConsoleLogger(stderr, Logging.Info))
cd(model_dir)

toml_path = get(ENV, "HYDROHUB_CONFIG_PATH", joinpath(model_dir, "wflow_sbm.toml"))
toml_path = abspath(toml_path)
isfile(toml_path) || error("wflow_sbm.toml not found in $model_dir")

println("✓ Menjalankan simulasi: $toml_path")

Wflow.run(toml_path)


println("✓ Simulasi selesai!")
