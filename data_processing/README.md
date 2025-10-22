## Data Processing Toolkit

This directory groups three toolsets for preparing, validating, and analyzing experiment data. Each subfolder contains standalone CLI scripts with their own README documenting required inputs and parameters.

## Install dependencies

Install once for all submodules in this directory:
```bash
python3 -m pip install -r requirements.txt
```
Includes: pandas, numpy, pyarrow (Parquet), matplotlib, scipy.


- consistency_verification
  - Purpose: post-hoc checks to compare physical (Continuum) telemetry with OpenDC simulation outputs. Includes power trend reconstruction/alignment, latency/throughput comparison, JSONL merging, and publication-ready plots.
  - See: data_processing/consistency_verification/README.md

- calibration
  - Purpose: offline, small-magnitude, interpretable adjustments to inputs/configuration prior to verification. Includes topology core-speed scaling, fragment duration scaling, CPU telemetry calibration, and allocation ratio scaling.
  - See: data_processing/calibration/README.md

- analysis
  - Purpose: utilities to summarize and compare many experiments, export named summaries, plot performance–energy trade-offs, and rank configurations by multi-objective criteria.
  - See: data_processing/analysis/README.md

Notes
- Scripts are designed to be invoked directly with Python from this repository.
- Input format and parameter requirements are documented in each subfolder's README.
- Parquet I/O requires a Parquet engine (pyarrow or fastparquet).

