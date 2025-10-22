## Analysis Tools

This directory contains scripts for comparing one or many experiments and visualizing results. Core dependencies: pandas/numpy; reading Parquet requires pyarrow or fastparquet; some scripts use matplotlib for plotting.

## Install dependencies

From the parent folder (once):
```bash
python3 -m pip install -r ../requirements.txt
```
This installs pandas, numpy, pyarrow (Parquet), matplotlib, scipy used across analysis scripts.


### Scripts and inputs/parameters

- compare_combined_results.py
  - Purpose: scan a combined_experiments raw-output directory and aggregate key metrics by config and by seed.
  - Required inputs:
    - --base-dir <path>: directory with per-config subfolders (0,1,2,...) and per-seed subfolders (seed=<n>) that contain OpenDC outputs
      - For each (config, seed), expected files include task.parquet and powerSource.parquet
      - task.parquet must include submission_time, schedule_time, finish_time (ms) to compute waits and makespan
      - powerSource.parquet must include energy_usage or equivalent columns to derive energy metrics
  - Parameters:
    - --out-dir <path>
    - --print
  - Outputs:
    - summary_by_seed.csv (per config+seed)
    - summary_by_config.csv (aggregated by config)

- compare_experiments.py
  - Purpose: compare makespan and P95 wait time across one or more single-run experiments; prints a summary and writes a CSV.
  - Required inputs:
    - Positional arguments: one or more experiment directories, each containing invocations_merged.jsonl
  - Outputs: analysis/experiment_comparison.csv

- export_named_summary.py
  - Purpose: export a human-readable summary CSV for combined_experiments by mapping config_id to strategy names.
  - Required inputs:
    - --summary-by-config <csv>: summary_by_config.csv produced by compare_combined_results.py
    - --combined-json <json>: combined_experiments.json containing strategy labels per config_id
    - --out-csv <csv>: output CSV path
  - Outputs: summary_by_strategy.csv with strategy/config names and selected metrics

- plot_performance_energy.py
  - Purpose: draw a Performance–Energy scatter plot from combined_experiments summaries; supports broken y-axis and compact x-axis.
  - Required inputs:
    - --summary-by-config <csv>: summary_by_config.csv produced by compare_combined_results.py
    - --combined-json <json>: combined_experiments.json for labeling
    - --out-png <png>: output image path
  - Styling options (optional):
    - --y-high-start/--y-high-end/--y-high-scale
    - --x-padding
  - Outputs: PNG scatter plot

- rank_configs.py
  - Purpose: rank configurations from summary_by_config.csv with multi-objective scoring (Pareto, weighted, lexicographic).
  - Required inputs/parameters:
    - --csv <csv>: path to summary_by_config.csv
    - --metrics <spec>: e.g., makespan_ms_mean:asc:0.5,energy_per_task_mean:asc:0.5
    - --method pareto|weighted|lexicographic
    - --pareto-first (optional)
    - --top-k <int>
    - --out <csv>
    - --print (optional)
  - Outputs: ranking_<method>.csv (and optional console output)
