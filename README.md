# Tracekit Repository Overview (Tools for Thesis Experiments)

This repository contains several categories of programs and scripts used in the thesis experiments, covering:
- Application deployment and trace collection within the Continuum framework (Tracekit)
- VM energy data collection (energy_tool, relying on Scaphandre/QEMU)
- Data processing, simulation, and analysis (data_processing, with OpenDC)

Recommended reading/order: run experiments on Continuum with Tracekit and collect traces → use energy_tool to collect energy → simulate/align in OpenDC → run consistency checks and analysis with data_processing.

## Directory structure and purpose

```
.
├─ Tracekit/          Application deployment and trace collection (within Continuum)
├─ energy_tool/       VM energy collection/export (energy_uj from Scaphandre/QEMU)
└─ data_processing/   Data processing, alignment/validation, and analysis (with OpenDC outputs)
```

### Tracekit/
- Purpose: Deploy and schedule applications in the Continuum framework and collect runtime traces (e.g., task-level events, process metrics, node metadata).
- Highlights:
  - One-stop end-to-end flow (prepare → collect → export), supporting multiple workers and a central scheduler.
  - Can export `tasks.parquet` / `fragments.parquet` for OpenDC to replay/simulate.
- Entry doc: `Tracekit/README.md` (VM preparation, Redis config, collection/scheduling commands, parameter reference, and export examples).

> Note: Tracekit’s application deployment and trace collection are done within the Continuum framework. See Continuum: https://github.com/atlarge-research/continuum

### energy_tool/
- Purpose: Sample or time-window measure each VM’s `energy_uj` (microjoules) counter to produce power/energy CSVs for later analysis or alignment with simulation outputs.
- Dependency: Requires Scaphandre’s QEMU exporter to provide each VM’s `energy_uj` file.
- Script and functions: `vm_energy_tools.py`
  - `ts`: periodic sampling of energy counters and conversion to power (W); outputs time-series CSVs.
  - `once`: one-shot measurement over a fixed duration or until manual stop; reports total energy and average power.
- Entry doc: `energy_tool/README.md`

> Note: Scaphandre (QEMU exporter) project: https://github.com/hubblo-org/scaphandre

### data_processing/
- Purpose: Clean, merge, align, and analyze data collected by Tracekit and the energy tools; can produce paper-ready plots and summary tables.
- Subfolders:
  - `consistency_verification/`: Validate consistency between Continuum (physical) and OpenDC (simulation) outputs; includes invocations/proc_metrics merges, wait-time/throughput comparisons, power trend reconstruction and alignment, and plotting scripts. See the subfolder README.
  - `calibration/`: Offline, small-magnitude, interpretable adjustments to inputs and parameters (e.g., topology/core frequency scaling, fragment duration scaling, CPU telemetry calibration, allocation ratio tuning).
  - `analysis/`: Utilities for multi-experiment aggregation/comparison, named summary exports, performance–energy scatter plots, and configuration ranking.
- Entry doc: `data_processing/README.md` and each subfolder’s README.

> Note: After collecting data, simulation and “Data process” should be performed in the OpenDC simulator. Examples/demos: https://github.com/atlarge-research/opendc-demos

## Suggested workflow (illustrative)
1) Use Tracekit on Continuum to deploy and run benchmarks/applications and generate runtime traces:
   - Node metadata (nodes.json), task-level events (invocations.jsonl), process metrics (proc_metrics.jsonl), etc.
   - Optional: export OpenDC-readable `tasks.parquet` and `fragments.parquet`.
2) On the host/collector nodes, start Scaphandre (QEMU exporter) to produce each VM’s `energy_uj` file; use `energy_tool` to either sample power time series or measure total energy/average power over intervals.
3) In OpenDC, replay/simulate the Tracekit-exported traces and align with the energy CSVs for trend validation; use `data_processing` for alignment checks, statistical analysis, and paper plots.

## External projects and references
- Continuum (deployment and physical-side telemetry)
  - https://github.com/atlarge-research/continuum
- Scaphandre (energy collection, QEMU exporter mode)
  - https://github.com/hubblo-org/scaphandre
- OpenDC demos (simulation and examples)
  - https://github.com/atlarge-research/opendc-demos

## Notes
- Time sync: enable NTP on all nodes (e.g., systemd-timesyncd/chrony) to make physical vs. simulation timelines easier to align.
- Monotonic `energy_uj`: if the counter resets, negative deltas should be handled (the tool clamps them). A sampling interval ≥ 0.5s is recommended to reduce noise.
- Security: the Redis password shown in `Tracekit/README.md` is for examples only. Use a strong password and restrict access in real deployments.

## Quick links
- Running and collecting: see `Tracekit/README.md`
- Energy sampling: see `energy_tool/README.md`
- Data processing/validation/analysis: see `data_processing/README.md` and subfolder READMEs

