#!/usr/bin/env python3
"""
Compare one or more experiment runs on makespan and P95 wait time.

Usage:
    # Compare specific experiments (one or more)
    python analysis/compare_experiments.py EXP_DIR [EXP_DIR ...]

    # No arguments: defaults to comparing 202501014004 and 202501014005
    python analysis/compare_experiments.py

Reads for each EXP_DIR:
- EXP_DIR/invocations_merged.jsonl

Computes for each:
- Makespan: max(ts_end) - min(ts_enqueue)  [in seconds]
- P95 Wait Time: 95th percentile of (ts_start - ts_enqueue)  [in seconds]

Outputs:
- Console summary for all experiments and diffs vs the first (baseline)
- CSV file: analysis/experiment_comparison.csv
"""

from __future__ import annotations
import argparse
import json
import csv
import numpy as np
from pathlib import Path
from typing import List, Dict, Any


def load_invocations(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Load all invocation records from a merged JSONL file."""
    records = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(obj)
            except json.JSONDecodeError:
                continue
    return records


def compute_energy_joules(exp_dir: Path) -> float:
    """Compute total energy consumption (Joules) for an experiment.
    It reads two CSVs under exp_dir/power/:
      - power_vm_cloud0_gxie.csv
      - power_vm_cloud1_gxie.csv
    For each, it finds the *_energy_uj column and returns (max - min) summed across both,
    converted from microjoules (uJ) to joules (J).
    """
    power_dir = exp_dir / "power"
    total_uj = 0.0
    if not power_dir.exists():
        return 0.0

    for fname in ("power_vm_cloud0_gxie.csv", "power_vm_cloud1_gxie.csv"):
        csv_path = power_dir / fname
        if not csv_path.exists():
            continue
        try:
            with csv_path.open("r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                energy_idx = None
                for i, col in enumerate(header):
                    if col.endswith("_energy_uj"):
                        energy_idx = i
                        break
                if energy_idx is None:
                    energy_idx = 1 if len(header) > 1 else None
                if energy_idx is None:
                    continue
                min_v = None
                max_v = None
                for row in reader:
                    if len(row) <= energy_idx:
                        continue
                    try:
                        v = float(row[energy_idx])
                    except ValueError:
                        continue
                    if min_v is None or v < min_v:
                        min_v = v
                    if max_v is None or v > max_v:
                        max_v = v
                if min_v is not None and max_v is not None and max_v >= min_v:
                    total_uj += (max_v - min_v)
        except Exception:
            continue

    return total_uj / 1e6  # microjoule -> joule






def compute_metrics(records: List[Dict[str, Any]]) -> Dict[str, float]:
    """
    Compute makespan and P95 wait time from invocation records.

    Returns:
        dict with keys: makespan_s, p95_wait_s, n_tasks
    """
    if not records:
        return {"makespan_s": 0.0, "p95_wait_s": 0.0, "n_tasks": 0}

    ts_enqueue_list = []
    ts_start_list = []
    ts_end_list = []
    wait_times_ms = []

    for rec in records:
        try:
            enq = float(rec.get("ts_enqueue", 0))
            start = float(rec.get("ts_start", 0))
            end = float(rec.get("ts_end", 0))

            if enq > 0 and start > 0 and end > 0:
                ts_enqueue_list.append(enq)
                ts_start_list.append(start)
                ts_end_list.append(end)
                wait_times_ms.append(start - enq)
        except (ValueError, TypeError):
            continue

    if not ts_enqueue_list or not ts_end_list:
        return {"makespan_s": 0.0, "p95_wait_s": 0.0, "n_tasks": 0}

    # Makespan: from earliest enqueue to latest end (in seconds)
    makespan_ms = max(ts_end_list) - min(ts_enqueue_list)
    makespan_s = makespan_ms / 1000.0

    # P95 wait time (in seconds)
    p95_wait_ms = float(np.percentile(wait_times_ms, 95)) if wait_times_ms else 0.0
    p95_wait_s = p95_wait_ms / 1000.0

    return {
        "makespan_s": makespan_s,
        "p95_wait_s": p95_wait_s,
        "n_tasks": len(ts_enqueue_list)
    }


def main():
    parser = argparse.ArgumentParser(description="Compare experiments on makespan and P95 wait time")
    parser.add_argument("experiments", nargs="*", help="Experiment directories containing invocations_merged.jsonl")
    args = parser.parse_args()

    root = Path(".")

    # Determine experiment directories
    exp_dirs: List[Path] = []
    if args.experiments:
        exp_dirs = [Path(p) for p in args.experiments]
    else:
        exp_dirs = [root / "202501014004", root / "202501014005"]

    # Load and compute metrics for each experiment
    results: List[Dict[str, Any]] = []
    names: List[str] = []

    for exp in exp_dirs:
        inv_path = exp / "invocations_merged.jsonl"
        if not inv_path.exists():
            raise FileNotFoundError(f"Missing: {inv_path}")
        name = exp.name
        print(f"Loading experiment {name}...")
        records = load_invocations(inv_path)
        metrics = compute_metrics(records)
        metrics["energy_j"] = compute_energy_joules(exp)
        results.append(metrics)
        names.append(name)

    # Print summary
    print("\n" + "=" * 70)
    print("EXPERIMENT COMPARISON: " + ", ".join(names))
    print("=" * 70)

    # Per-experiment metrics
    for name, m in zip(names, results):
        print(f"{name}:")
        print(f"  Makespan [s]:       {m['makespan_s']:.2f}")
        print(f"  P95 Wait Time [s]:  {m['p95_wait_s']:.2f}")
        print(f"  Energy [J]:         {m.get('energy_j', 0.0):.2f}")
        print(f"  Number of Tasks:    {m['n_tasks']}")
        print("-" * 70)

    # Differences vs baseline (first experiment), if more than one
    if len(results) >= 2:
        base = results[0]
        base_name = names[0]
        print(f"Differences vs {base_name}:")
        for name, m in zip(names[1:], results[1:]):
            diff_makespan = m["makespan_s"] - base["makespan_s"]
            diff_p95_wait = m["p95_wait_s"] - base["p95_wait_s"]
            diff_energy = m.get("energy_j", 0.0) - base.get("energy_j", 0.0)
            diff_tasks = m["n_tasks"] - base["n_tasks"]
            print(f"  {name}:")
            print(f"    Makespan [s]:      {diff_makespan:+.2f}")
            print(f"    P95 Wait [s]:      {diff_p95_wait:+.2f}")
            print(f"    Energy [J]:        {diff_energy:+.2f}")
            print(f"    Number of Tasks:   {diff_tasks:+}")
        print("=" * 70)

    # Export to CSV
    out_csv = root / "analysis" / "experiment_comparison.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="\n") as f:
        f.write("Experiment,Makespan [s],P95 Wait Time [s],Energy [J],Number of Tasks\n")
        for name, m in zip(names, results):
            f.write(f"{name},{m['makespan_s']:.2f},{m['p95_wait_s']:.2f},{m.get('energy_j', 0.0):.2f},{m['n_tasks']}\n")
        if len(results) >= 2:
            base = results[0]
            base_name = names[0]
            for name, m in zip(names[1:], results[1:]):
                diff_energy = m.get('energy_j', 0.0) - base.get('energy_j', 0.0)
                f.write(
                    f"Difference ({name}-{base_name}),"
                    f"{(m['makespan_s'] - base['makespan_s']):+.2f},"
                    f"{(m['p95_wait_s'] - base['p95_wait_s']):+.2f},"
                    f"{diff_energy:+.2f},"
                    f"{(m['n_tasks'] - base['n_tasks']):+}\n"
                )

    print(f"\nResults saved to: {out_csv}")


if __name__ == "__main__":
    main()

