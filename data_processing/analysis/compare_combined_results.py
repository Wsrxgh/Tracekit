#!/usr/bin/env python3
"""
Compare OpenDC experiment results produced by the combined_experiments config.

It scans a base directory like:
  1. Simple Experiment/output/combined_experiments/raw-output
which contains subfolders per config index (0, 1, 2, ...), and inside each, subfolders per seed:
  <base>/<config_id>/seed=<n>

Per seed, this script now computes (from completed tasks only when task_state is available):
  - Wait time (schedule_time - submission_time): P95 and MEAN
  - Turnaround (finish_time - submission_time): P95 and MEAN
  - Makespan: max(finish_time) - min(submission_time)
  - Energy per task: sum(energy_usage) / number_of_completed_tasks
  - CPU utilization (from host.parquet): sum(cpu_usage) / sum(cpu_capacity) if both present;
    otherwise mean(cpu_usage) if utilization is already a fraction.

Outputs two CSVs:
  - summary_by_seed.csv: one row per (config_id, seed)
  - summary_by_config.csv: aggregated by config_id (mean and std across seeds)

Usage:
  python 1. Simple Experiment/analysis/compare_combined_results.py \
    --base-dir "1. Simple Experiment/output/combined_experiments/raw-output" \
    --out-dir  "1. Simple Experiment/output/combined_experiments" --print
"""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import math
import pandas as pd


def _percentile(xs: List[float], q: float) -> float:
    if not xs:
        return float("nan")
    try:
        return float(pd.Series(xs).quantile(q))
    except Exception:
        xs_sorted = sorted(xs)
        n = len(xs_sorted)
        if n == 1:
            return float(xs_sorted[0])
        idx = (n - 1) * q
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return float(xs_sorted[lo])
        w = idx - lo
        return float(xs_sorted[lo] * (1 - w) + xs_sorted[hi] * w)


def _safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def compute_metrics_for_seed(seed_dir: Path) -> Optional[Dict[str, float]]:
    task_path = seed_dir / "task.parquet"
    power_path = seed_dir / "powerSource.parquet"
    host_path = seed_dir / "host.parquet"

    if not task_path.exists():
        print(f"[WARN] task.parquet not found: {task_path}")
        return None
    try:
        dt = pd.read_parquet(task_path)
    except Exception as e:
        print(f"[WARN] Failed to read {task_path}: {e}")
        return None

    # Ensure required columns exist
    need_task_cols = {"submission_time", "schedule_time", "finish_time"}
    missing = sorted(list(need_task_cols - set(dt.columns)))
    if missing:
        print(f"[WARN] Missing columns in {task_path.name}: {missing}")
        return None

    # Filter to completed tasks if available
    if "task_state" in dt.columns:
        try:
            mask_completed = dt["task_state"].astype(str).str.lower() == "completed"
            dtc = dt[mask_completed].copy()
        except Exception:
            dtc = dt.copy()
    else:
        dtc = dt.copy()

    # Coerce numeric on filtered frame
    for c in ["submission_time", "schedule_time", "finish_time"]:
        dtc[c] = _safe_numeric(dtc[c])

    # Wait times (completed tasks)
    waits = (dtc["schedule_time"] - dtc["submission_time"]).dropna().astype(float)
    p95_wait = _percentile(waits.tolist(), 0.95)
    mean_wait = float(waits.mean()) if len(waits) > 0 else float("nan")

    # Turnaround times (completed tasks) — traditional: finish_time - submission_time
    turns = (dtc["finish_time"] - dtc["submission_time"]).dropna().astype(float)
    p95_turn = _percentile(turns.tolist(), 0.95)
    mean_turn = float(turns.mean()) if len(turns) > 0 else float("nan")

    # Makespan (completed tasks)
    sub_min = pd.to_numeric(dtc["submission_time"], errors="coerce").dropna()
    fin_max = pd.to_numeric(dtc["finish_time"], errors="coerce").dropna()
    makespan = float(fin_max.max() - sub_min.min()) if (not sub_min.empty and not fin_max.empty) else float("nan")

    # Energy per task (sum energy_usage / number of completed tasks)
    energy_per_task = float("nan")
    n_tasks = int(len(dtc))
    if power_path.exists():
        try:
            dp = pd.read_parquet(power_path)
            if "energy_usage" in dp.columns:
                total_energy = float(pd.to_numeric(dp["energy_usage"], errors="coerce").dropna().sum())
                energy_per_task = (total_energy / n_tasks) if n_tasks > 0 else float("nan")
            else:
                print(f"[WARN] Column 'energy_usage' not found in {power_path.name}; skipping energy.")
        except Exception as e:
            print(f"[WARN] Failed to read {power_path}: {e}")

    # CPU utilization from host.parquet
    cpu_util = float("nan")
    if host_path.exists():
        try:
            dh = pd.read_parquet(host_path)
            # Try common column names
            cols = {c.lower(): c for c in dh.columns}
            def pick(*names):
                for n in names:
                    if n in cols:
                        return cols[n]
                return None
            c_usage = pick("cpu_usage", "cpuusage", "cpu_usage_mhz", "cpuusagemhz", "cpuusagemhz")
            c_cap = pick("cpu_capacity", "cpucapacity", "cpu_capacity_mhz", "cpucapacitymhz", "cpucapacitymhz")
            if c_usage is not None and c_cap is not None:
                u = pd.to_numeric(dh[c_usage], errors="coerce").dropna()
                cap = pd.to_numeric(dh[c_cap], errors="coerce").dropna()
                # Align to common index length if needed
                m = min(len(u), len(cap))
                if m > 0:
                    u = u.iloc[:m]
                    cap = cap.iloc[:m]
                    denom = float(cap.sum())
                    cpu_util = float(u.sum() / denom) if denom > 0 else float("nan")
            elif c_usage is not None:
                u = pd.to_numeric(dh[c_usage], errors="coerce").dropna()
                if len(u) > 0:
                    # Assume already a fraction [0,1]
                    cpu_util = float(u.mean())
        except Exception as e:
            print(f"[WARN] Failed to read {host_path}: {e}")

    return {
        "p95_wait_ms": float(p95_wait),
        "mean_wait_ms": float(mean_wait),
        "p95_turn_ms": float(p95_turn),
        "mean_turn_ms": float(mean_turn),
        "makespan_ms": float(makespan),
        "energy_per_task": float(energy_per_task),
        "cpu_utilization": float(cpu_util),
        "n_waits": int(len(waits)),
        "n_tasks": n_tasks,
    }


def scan_and_summarize(base_dir: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rows: List[Dict] = []
    if not base_dir.exists():
        raise SystemExit(f"Base dir not found: {base_dir}")

    # Build config directories and sort them in natural numeric order
    cfg_dirs = [p for p in base_dir.iterdir() if p.is_dir()]
    def _cfg_key(p: Path):
        try:
            return int(p.name)
        except Exception:
            return p.name
    cfg_dirs = sorted(cfg_dirs, key=_cfg_key)

    # Iterate config directories
    for cfg_dir in cfg_dirs:
        config_id = cfg_dir.name
        # Gather seed subdirs and sort numerically by seed value
        seed_dirs = [p for p in cfg_dir.iterdir() if p.is_dir() and p.name.startswith("seed=")]
        def _seed_key(p: Path):
            try:
                return int(p.name.split("=", 1)[-1])
            except Exception:
                return math.inf
        seed_dirs = sorted(seed_dirs, key=_seed_key)

        if not seed_dirs:
            m = compute_metrics_for_seed(cfg_dir)
            if m is not None:
                rows.append({"config_id": config_id, "seed": None, **m})
            continue
        for sd in seed_dirs:
            try:
                seed_str = sd.name.split("=", 1)[-1]
                seed_val: Optional[int]
                try:
                    seed_val = int(seed_str)
                except Exception:
                    seed_val = None
                m = compute_metrics_for_seed(sd)
                if m is not None:
                    rows.append({"config_id": config_id, "seed": seed_val, **m})
            except Exception as e:
                print(f"[WARN] Failed on {sd}: {e}")

    if not rows:
        raise SystemExit(f"No results parsed under {base_dir}")

    by_seed = pd.DataFrame(rows)

    # Sort by_seed in natural config order and seed order for readability
    def _cfg_to_int_or_inf(x):
        try:
            return int(str(x))
        except Exception:
            return math.inf
    by_seed["__cfg_k"] = by_seed["config_id"].apply(_cfg_to_int_or_inf)
    by_seed["__seed_k"] = by_seed["seed"].apply(lambda s: int(s) if pd.notna(s) else math.inf)
    by_seed = by_seed.sort_values(by=["__cfg_k", "__seed_k"], kind="mergesort").drop(columns=["__cfg_k", "__seed_k"]).reset_index(drop=True)

    # Prepare numeric config id for robust numeric sorting
    by_seed["config_id_num"] = pd.to_numeric(by_seed["config_id"], errors="coerce")

    # Aggregate by numeric config_id: mean/std across seeds, plus counts
    agg_funcs = {
        "p95_wait_ms": ["mean", "std"],
        "mean_wait_ms": ["mean", "std"],
        "p95_turn_ms": ["mean", "std"],
        "mean_turn_ms": ["mean", "std"],
        "makespan_ms": ["mean", "std"],
        "energy_per_task": ["mean", "std"],
        "cpu_utilization": ["mean", "std"],
        "n_waits": ["sum"],
        "n_tasks": ["sum"],
    }
    by_cfg = by_seed.groupby("config_id_num", dropna=False, sort=True).agg(agg_funcs)
    by_cfg.columns = [f"{m}_{s}" if s else m for m, s in by_cfg.columns.to_flat_index()]
    by_cfg = by_cfg.reset_index().rename(columns={"config_id_num": "config_id"})
    try:
        by_cfg["config_id"] = by_cfg["config_id"].astype("Int64")
    except Exception:
        pass

    return by_seed, by_cfg


def main():
    ap = argparse.ArgumentParser(description="Compare combined_experiments across configs (P95/mean waits, P95/mean turnaround, makespan, energy per task, CPU util)")
    ap.add_argument("--base-dir", type=Path, default=Path("1. Simple Experiment/output/combined_experiments/raw-output"))
    ap.add_argument("--out-dir", type=Path, default=None, help="Where to write CSV summaries (default: parent of base-dir)")
    ap.add_argument("--print", action="store_true", help="Print a compact table to stdout")
    args = ap.parse_args()

    out_dir = args.out_dir if args.out_dir is not None else args.base_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    by_seed, by_cfg = scan_and_summarize(args.base_dir)

    path_seed = out_dir / "summary_by_seed.csv"
    path_cfg = out_dir / "summary_by_config.csv"
    by_seed.to_csv(path_seed, index=False)
    by_cfg.to_csv(path_cfg, index=False)
    print(f"Wrote: {path_seed}")
    print(f"Wrote: {path_cfg}")

    if args.print:
        cols = [
            "config_id",
            "p95_wait_ms_mean", "mean_wait_ms_mean",
            "p95_turn_ms_mean", "mean_turn_ms_mean",
            "makespan_ms_mean", "energy_per_task_mean",
            "cpu_utilization_mean",
        ]
        show = by_cfg.reindex(columns=[c for c in cols if c in by_cfg.columns])
        try:
            pd.options.display.float_format = lambda v: f"{v:,.3f}"
        except Exception:
            pass
        print("\n=== Summary by config (means) ===")
        print(show.to_string(index=False))


if __name__ == "__main__":
    main()

