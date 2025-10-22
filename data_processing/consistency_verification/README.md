## Consistency Verification Toolkit

Post-hoc consistency checks between OpenDC (simulation) and Continuum (physical) without modifying the simulator kernel. Primary deps: pandas/numpy; reading Parquet requires pyarrow or fastparquet; SciPy is used for some statistics (optional); matplotlib for plotting (optional).

## Install dependencies

From the parent folder (once):
```bash
python3 -m pip install -r ../requirements.txt
```
This installs pandas, numpy, pyarrow (Parquet), matplotlib, scipy.


### Scripts and inputs/parameters

- merge_invocations_jsonl.py
  - Purpose: merge per-chunk (c0..cN) JSONL telemetry into single JSONLs; optionally validate JSON and enrich proc_metrics with cpu_freq_mhz when nodes.json is available.
  - Required inputs:
    - --base-dir <path>: a directory containing subfolders named like 2025XXXX(c*) with CTS/cctf files
  - Parameters:
    - --what invocations|proc_metrics|both
    - --output <path>
    - --proc-output <path>
    - --validate
  - Outputs: invocations_merged.jsonl and/or proc_metrics_merged.jsonl in the chosen locations
  - Input requirements: expected file names per chunk include CTS/invocations.jsonl or cctf/invocations.jsonl; similarly for proc_metrics.

- compare_latency_throughput.py
  - Purpose: unified checks for wait-time distribution (p50/p95/p99, KS) and throughput/cumulative curves (RMSE%, makespan); optional CPU usage alignment (proc_metrics vs task.parquet); can export aligned series and a report.
  - Required inputs:
    - --task-parquet <parquet>: task.parquet with task_state == 'COMPLETED'
    - --invocations <jsonl>: invocations_merged.jsonl
    - Optional: --proc-metrics <jsonl>: proc_metrics_merged.jsonl for CPU alignment
  - Parameters (subset):
    - Wait-time: --ks-alpha, --q50-th-pct, --q95-th-pct, --q99-th-pct
    - Throughput/cumulative: --bin-ms, --rmse-threshold-pct, --rmse-max-lag-bins, --makespan-threshold-pct, --makespan-abs-threshold-sec
    - Export/report: --export, --report
    - CPU alignment: --cpu-smape-th-pct, --cpu-r-th, --cpu-rmse-frac-median-th-pct, --task-ts-col
  - Outputs: console summary; optional CSV exports and markdown report
  - Input requirements: task.parquet must contain submission_time, schedule_time, finish_time (ms); invocations JSONL must contain ts_enqueue, ts_start, ts_end (ms).

- plots_for_paper.py
  - Purpose: generate multiple publication-ready plots (wait-time, throughput, CPU, power, etc.).
  - Required inputs:
    - --task-parquet <parquet>
    - --invocations <jsonl>
    - --proc-metrics <jsonl>
    - --power-total <csv>: total reconstructed power (power_total_corrected.csv)
    - --power-source <parquet>: OpenDC powerSource.parquet
  - Parameters (subset):
    - --out-dir, --bin-ms, --x-units, --max-align-delta-ms, --power-source-ts-col, --power-use-best-lag, --power-max-lag-sec
  - Options: --fragments (alternative source for CPU scatter), --cpu-pairs-csv (pre-matched pairs), --skip-cpu-scatter
  - Outputs: PNG figures written under the chosen directory

- power/reconstruct_power.py
  - Purpose: reconstruct per-second average power from Continuum node CSVs (µJ), aggregate into total power, and validate trend alignment with OpenDC powerSource.parquet (supports smoothing/segmentation/best lag/active-window restriction). Can export reports and a standardized trio of outputs.
  - Required inputs:
    - --dir <path> containing power_monitoring_*.csv, or use --file for a single CSV
    - --source-parquet <parquet> path to OpenDC powerSource.parquet
    - Optional active window: --active-from-invocations <jsonl> and --activity-padding-sec <int>
  - Parameters (subset):
    - Export alignment: --export-trend, --export-trend-stats, --trend-max-lag-sec, --trend-pass-threshold
    - Smoothing/segmentation: --smooth-sec, --segment-sec, --seg-pass-threshold
    - Reporting/plotting: --power-report [--power-report-mode simple|full], --plot-global-out, --plot-segmented-out
  - Standardized evaluation (merged from compute_power_trend_updated.py):
    - Enable with --updated-out-dir <dir>
    - Outputs: trend_alignment_updated.csv, trend_stats_updated.txt (raw + smoothed), power_audit_updated.md (suggested standards and PASS/FAIL)
    - Related parameters: --updated-smooth-sec, --updated-segment-sec, --updated-max-lag-sec, --updated-activity-padding-sec
  - Input requirements:
    - Continuum power CSVs must contain columns: ts (integer seconds), *_energy_uj (monotonic cumulative energy), optional *_power_w.
    - powerSource.parquet must contain a timestamp column (use --source-ts-col to specify if not autodetected).

### Notes
- Parquet I/O requires pyarrow or fastparquet.
- SciPy is optional for statistical tests.
- Plotting requires matplotlib if plots are requested.
