## Calibration Toolkit

Offline, small-magnitude, and interpretable calibration scripts to adjust inputs/configuration before verification. Tools never overwrite originals: each script writes calibrated artifacts alongside a sidecar <output>.manifest.json that records parameters and change counts.

Dependencies:
- Python stdlib (argparse, json, pathlib)
- pandas (only for calibrate_fragments.py)

### Scripts and inputs/parameters

- calibrate_topology_corespeed.py
  - Purpose: scale cpu.coreSpeed (MHz) in topology JSONs by a factor (system-wide execution speed calibration).
  - Required inputs:
    - --topology <path(s)|glob(s)>: one or more topology JSON files or globs
    - --factor <float>: multiplicative scale factor
  - Optional:
    - --suffix <str>: file name suffix for outputs
    - --out-dir <path>: directory to write calibrated JSONs
  - Outputs: one new JSON per input with the chosen suffix, plus <json>.manifest.json
  - Input requirements: JSON must contain hosts with cpu.coreSpeed (integer MHz).

- calibrate_fragments.py
  - Purpose: calibrate workload fragments.parquet via duration scaling and/or injecting a fixed per-task startup overhead (ms) into the first fragment per task.
  - Required inputs:
    - --in-fragments <parquet>: input fragments file
    - --out-fragments <parquet>: output calibrated fragments file
  - Optional:
    - --scale-duration <float>: multiply the duration column
    - --add-fixed-overhead-ms <int>: add δ ms to the first fragment per task
  - Outputs: calibrated parquet and <parquet>.manifest.json (records scaling/overhead and affected task counts)
  - Input requirements:
    - Must contain a column duration (milliseconds).
    - For overhead injection, the file must allow identifying the first fragment per task (e.g., via task_id and an order indicator such as start/order/sequence/fragment_index).

- calibrate_proc_metrics.py
  - Purpose: calibrate proc_metrics JSONL by scaling cpu_usage_mhz and/or shifting ts_ms (time base) by a constant offset (ms).
  - Required inputs:
    - --in-jsonl <path>: input proc_metrics_merged.jsonl
    - --out-jsonl <path>: output calibrated JSONL
  - Optional:
    - --scale-cpu <float>: multiply cpu_usage_mhz by k
    - --shift-ms <int>: add Δt (ms) to ts_ms
  - Outputs: calibrated JSONL and <jsonl>.manifest.json (records k/Δt and line counts)
  - Input requirements: each JSON line should contain ts_ms and cpu_usage_mhz; invalid JSON lines are passed through unchanged.

- calibrate_experiment_allocation.py
  - Purpose: adjust VCpu/Ram allocationRatio in experiment JSON by a fixed ratio r (capacity posture calibration).
  - Required inputs:
    - --in-experiment <json>: input experiment configuration
    - --out-experiment <json>: output calibrated configuration
    - --ratio <float>: multiplicative ratio applied to allocationRatio
  - Outputs: calibrated JSON and <json>.manifest.json (records modified entries)
  - Input requirements: experiment JSON must contain filters with type in {"VCpu","Ram"} and key allocationRatio.

### Usage notes (concise)
- Makespan or cumulative curve shift: prefer coreSpeed scaling (calibrate_topology_corespeed.py).
- Wait-time distribution mismatch (p50/p95/p99, KS): prefer duration scaling; add first-fragment fixed overhead for short-task bias if needed.
- CPU amplitude/shape: use --scale-cpu for amplitude; use --shift-ms for constant time-base offsets.
- Adjust one knob at a time and keep magnitudes small; re-verify after each change.

