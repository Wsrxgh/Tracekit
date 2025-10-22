# vm_energy_tools

Small read‑only utilities to sample per‑VM energy files (energy_uj) and write CSVs for analysis.

- Language: Python 3
- Scope: Read‑only (does not require root). No installation of extra packages is needed.
- Inputs: One or more energy_uj files (e.g., exported by tools like Scaphandre). Each file is assumed to be a monotonically increasing energy counter in micro‑joules (µJ).
- Outputs: CSV files under a timestamped directory: `csv/run_YYYYMMDD_HHMMSS/`

## What it does
- Label detection: For an input path like `/var/lib/scaphandre/<vm_name>/energy_uj`, the label will be `<vm_name>`. Otherwise, the parent directory name is used as the label.
- Two commands:
  - `ts`: Periodically sample energy counters, derive point‑in‑time power in watts, and write CSV time series.
  - `once`: Measure total energy and average power over a fixed interval.

## Installation

- Python dependencies: none (stdlib only). No `pip install` step is required.
- External requirement: Scaphandre (QEMU) to expose per-VM `energy_uj` files.
  - See: https://github.com/hubblo-org/scaphandre
- Ensure you have read access to the VM `energy_uj` files before running.

## Requirements
- Python 3.7+
- Access to the VM energy counter files (read permission)

## Usage

### 1) Time series sampling (ts)
Collect power time series into CSV. By default, a single CSV is produced. You can also generate one CSV per VM.

Examples:

- Single CSV containing all VMs

```bash
python3 vm_energy_tools.py ts \
  --files /path/to/vmA/energy_uj /path/to/vmB/energy_uj \
  --interval 1 \
  --count 120 \
  --out ~/power_vm.csv
```

- Separate CSV per VM under the same run directory

```bash
python3 vm_energy_tools.py ts \
  --files /path/to/vmA/energy_uj /path/to/vmB/energy_uj \
  --interval 0.5 \
  --manual-stop \
  --separate-files \
  --out ~/power_vm.csv
# Press Enter to stop when using --manual-stop
```

Options:
- `--files`: Paths to each VM's `energy_uj` file (required; one or more)
- `--interval` / `-i`: Sampling interval in seconds (default: 1)
- `--count` / `-n`: Number of samples (default: 120). Ignored when `--manual-stop` is used
- `--out` / `-o`: Output CSV path (the actual file(s) will be placed under a timestamped directory)
- `--smooth`: Optional simple moving average window size (in samples), `0` means off
- `--manual-stop`: Press Enter to stop sampling (infinite run)
- `--separate-files`: Generate one CSV per VM (instead of one combined CSV)

Output layout:
- The tool creates a timestamped directory `csv/run_YYYYMMDD_HHMMSS/`
- Combined mode: a single CSV with columns: `ts`, `<label>_energy_uj`, `<label>_power_w`, optionally `<label>_power_w_sma<N>` for each label
- Separate mode: one CSV per label with header `ts`, `<label>_energy_uj`, `<label>_power_w` and optional SMA column
- `ts` is a UNIX timestamp (seconds). Power is computed as `Δenergy (J) / Δtime (s)`; energy_uj is converted from micro‑joules to joules.

### 2) One‑shot measurement (once)
Measure total energy and average power over a fixed duration or until Enter is pressed.

Examples:

- Fixed duration (10 seconds):
```bash
python3 vm_energy_tools.py once \
  --files /path/to/vmA/energy_uj /path/to/vmB/energy_uj \
  --duration 10
```

- Manual end (press Enter):
```bash
python3 vm_energy_tools.py once \
  --files /path/to/vmA/energy_uj \
  --until-enter
```

Output:
- Prints the measured duration, total energy per label (J), and average power per label (W)

## Notes and tips
- energy_uj is expected to be monotonically increasing. If it resets, negative deltas are clamped to zero.
- Labels: If the path contains a `scaphandre/<label>/...` segment, that `<label>` is used; otherwise the parent directory name of the file is used.
- The script is read‑only and safe to run on shared systems.

## Examples of labels and paths
```
/var/lib/scaphandre/vm-001/energy_uj   -> label: vm-001
/data/vmA/energy_uj                    -> label: vmA
```

## License
MIT (or align with your repository's default license).
