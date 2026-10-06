# Automation workflow

Automation runs in the background while HydroHub is open. Closing or switching
the Automation tab does not stop its scheduler. Exiting HydroHub cancels active
jobs; the scheduler does not run when the application is closed.

## Build and save

1. Create an automation, enter its name and ID, and add project/model targets.
2. Choose a trigger. Manual has no schedule fields. Monthly needs a day and
   time; interval needs positive days and a time. Cron uses five fields
   (minute, hour, day, month, weekday). The time control edits single-time cron
   expressions; multi-time expressions are edited directly.
3. Add tools as nodes. Drag nodes to position them. Drag a right/output port to
   a left/input port to connect them. Choose a connection condition before
   dragging, or enter node IDs in the properties panel for keyboard access.
4. Select a node, configure its properties, and click **Apply node properties**.
   `Next node IDs` is a comma-separated list. Parameters are a YAML mapping.
   Status branches are mappings such as `warning: [review]`.
5. Generate YAML to inspect it, then save. Save also generates pending canvas
   or form changes. After editing YAML directly, use **Load YAML into canvas**
   before editing the visual workflow again. Import/export uses the same YAML.

Workflow validation requires one entry node, unique IDs, valid connections,
and an acyclic connected graph. Execution follows connections, not array order.
Targets run independently, subject to the automation's worker limit. Jobs for
the same model cannot execute concurrently, even across automations.

## Node configuration

- **Global:** use the node's parameters for every target model.
- **Local:** load configuration at execution time. HydroMT uses the project's
  saved recipe/catalog choices and runtime catalog resolution. Wflow uses the
  selected model's TOML and Run Registry period/state. Custom tools use their
  project-local custom-tool parameter configuration. Other built-ins can read
  explicit per-tool parameters from `automation_parameters` in the existing
  `workspace/ui_state.json`. Without local download configuration, choose
  Global and supply its `tool_id` and `arguments`.
- HydroMT and Julia use saved project executable paths unless a Global node
  specifies an `executable`. Node timeout and failure policy are independent
  of parameter mode.
- Legacy `steps` definitions migrate to connected `nodes` with Global mode,
  retaining their previous parameters and execution order.

Example:

```yaml
automation:
  id: monthly_model
  name: Monthly model update
  trigger:
    type: schedule
    mode: monthly_on_date
    day_of_month: 4
    time: '02:00'
  concurrency:
    max_parallel_models: 2
  targets:
    - project_id: project_id_from_registry
      model_id: model_id_from_registry
  nodes:
    - id: check
      tool: builtin.check_last_run_date
      config_mode: global
      next: [simulate]
    - id: simulate
      tool: builtin.wflow_run
      config_mode: local
      next: [index]
    - id: index
      tool: builtin.compute_index
      config_mode: global
      params: {index: SSFI, timescale: 3}
      next: []
  on_failure: {strategy: stop_model, retry_count: 0}
```

`next_on_status` maps `ok`, `warning`, or `error` to successor lists. A matching
status branch replaces `next`; otherwise `next` is used. A returned error or
exception stops the model unless handled by an explicit error branch or the
failure policy. Policies may be overridden per node (`stop_model`, `skip_step`,
or `retry` with `retry_count`). Converging branches execute each reachable node
once. Nodes on unselected branches are shown as skipped.

## Tools and monitoring

Installed custom-tool packages remain available. Automation tools receive JSON
with `params`, `context`, `project_root`, and `model_root`; return a JSON object
whose `status` is `ok`, `warning`, or `error`. Normal custom-tool CLI execution
outside automation is unchanged.

The Tool registry also accepts a JSON registration file:

```json
{
  "tool_id": "custom.qc",
  "name": "Forcing QC",
  "description": "Inspect forcing quality",
  "executable": {"kind": "python_script", "path": "tools/qc.py", "entry": "main"},
  "input_schema": {"forcing_path": "string"},
  "output_schema": {"status": "string", "notes": "string"},
  "timeout_seconds": 1800
}
```

Relative executable paths resolve against each target project's root. A Python
`entry` function accepts the request dictionary and returns the result dictionary;
without `entry`, the script reads JSON stdin and writes JSON stdout. CLI binaries
use the same stdin/stdout contract; HTTP endpoints receive a JSON POST and return
JSON. Optional executable `arguments` is a list of strings for script/CLI mode.
Project-configured interpreter, environment values, and OS-keyring secrets apply
to custom subprocesses. Input/output schema syntax is checked before save; actual
input and output values are checked during execution.
Input-schema values may come from previous-node context or configured parameters
(parameters take precedence); the request preserves separate `params` and
`context` objects. Context-produced values cannot be checked before execution.

The Run monitor displays a canvas and saved live log for each selected model job.
The viewer retains the latest 10,000 lines; the complete log remains at the path
shown in the monitor.
Overall statuses are OFF, Idle, Running, and Finish; detailed outcomes remain in
node states and logs. Completed active automations briefly show Finish before
returning to Idle. Enable starts a future schedule, not an immediate historical
run; missed triggers are coalesced into one job batch.

Delete removes only the definition and schedule, after confirmation. Running
automations must finish or be cancelled first. Saved job logs and Run Registry
history are retained.
