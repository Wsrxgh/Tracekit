#!/usr/bin/env python3
"""
Unified analysis: wait-time distribution + throughput/cumulative comparison
between OpenDC (task.parquet) and Continuum (invocations_merged.jsonl).

Notes:
- All timestamps are in milliseconds; no unit conversion is performed unless --units seconds is set for display only
- OpenDC side reads Parquet and filters to task_state == 'COMPLETED'
- Continuum side uses invocations JSONL (ts_enqueue, ts_start, ts_end)

Outputs:
1) Wait-time distribution metrics (p50/p95/p99, KS test)
2) Throughput per minute and cumulative curve RMSE + makespan comparison
3) Optional export of aligned throughput/cumulative series

Usage:
  python 20250904001/compare_latency_throughput.py \
    --task-parquet 20250904001/task.parquet \
    --invocations 20250904001/invocations_merged.jsonl \
    --export 20250904001/cumulative_comparison.csv

Dependencies:
- pandas (for Parquet). Requires an engine (pyarrow or fastparquet). If missing, the script will explain how to install.
- standard library otherwise
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Optional

import pandas as pd

MINUTE_MS = 60_000

# ----------------------------- New helpers for proc_metrics vs task -----------------------------

def safe_pearsonr(x: List[float], y: List[float]) -> Tuple[float, Optional[float]]:
    try:
        import importlib
        stats = importlib.import_module("scipy.stats")  # type: ignore
        r, p = stats.pearsonr(x, y)
        return float(r), float(p)
    except Exception:
        # Fallback: pandas corr (no p-value)
        if not x or not y:
            return float("nan"), None
        n = min(len(x), len(y))
        if n < 2:
            return float("nan"), None
        s1 = pd.Series(x[:n])
        s2 = pd.Series(y[:n])
        return float(s1.corr(s2)), None


def compute_mape(y_true: List[float], y_pred: List[float]) -> float:
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return float("nan")
    s = 0.0
    cnt = 0
    for i in range(n):
        a = y_true[i]
        b = y_pred[i]
        if a == 0:
            # skip zero-actual to avoid div-by-zero exploding MAPE
            continue
        s += abs((b - a) / a)
        cnt += 1
    return (s / cnt * 100.0) if cnt > 0 else float("nan")




def compute_smape(y_true: List[float], y_pred: List[float]) -> float:
    n = min(len(y_true), len(y_pred))
    if n == 0:
        return float("nan")
    s = 0.0
    cnt = 0
    for i in range(n):
        a = abs(y_true[i])
        b = abs(y_pred[i])
        denom = (a + b)
        if denom <= 0:
            continue
        s += abs(b - a) / (denom / 2.0)
        cnt += 1
    return (s / cnt * 100.0) if cnt > 0 else float("nan")

def rmse_list(a: List[float], b: List[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return float("nan")
    se = 0.0
    for i in range(n):
        d = (a[i] - b[i])
        se += d * d
    return (se / n) ** 0.5


def detect_task_ts_col(df: pd.DataFrame, override: Optional[str] = None) -> str:
    if override is not None:
        if override not in df.columns:
            raise SystemExit(f"Task timestamp column '{override}' not found in Parquet columns: {list(df.columns)}")
        return override
    # look for common candidates
    for c in ["timestamp_absolute", "ts_ms", "timestamp_ms", "ts", "timestamp", "time_ms", "time"]:
        if c in df.columns:
            return c
    raise SystemExit("Cannot find a timestamp column in task.parquet. Please pass --task-ts-col explicitly.")


# ----------------------------- Utilities -----------------------------

def percentiles(data: List[float], ps: Iterable[float]) -> Dict[float, float]:
    if not data:
        return {p: float("nan") for p in ps}
    xs = sorted(data)
    n = len(xs)
    out: Dict[float, float] = {}
    for p in ps:
        if p <= 0:
            out[p] = xs[0]
            continue
        if p >= 1:
            out[p] = xs[-1]
            continue
        idx = (n - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            out[p] = xs[lo]
        else:
            w = idx - lo
            out[p] = xs[lo] * (1 - w) + xs[hi] * w
    return out


def ks_2samp_basic(x: List[float], y: List[float]) -> Tuple[float, float]:
    """Return (D, p_value). Uses SciPy if available; otherwise asymptotic approx.
    """
    try:
        import importlib  # type: ignore
        scipy_stats = importlib.import_module("scipy.stats")  # type: ignore
        res = scipy_stats.ks_2samp(x, y, alternative="two-sided", mode="auto")
        return float(res.statistic), float(res.pvalue)
    except Exception:
        pass

    xs = sorted(x)
    ys = sorted(y)
    n = len(xs)
    m = len(ys)
    if n == 0 or m == 0:
        return float("nan"), float("nan")
    i = j = 0
    cdf_x = cdf_y = 0.0
    d = 0.0
    while i < n and j < m:
        vx = xs[i]
        vy = ys[j]
        if vx <= vy:
            while i < n and xs[i] == vx:
                i += 1
            cdf_x = i / n
        if vy <= vx:
            while j < m and ys[j] == vy:
                j += 1
            cdf_y = j / m
        d = max(d, abs(cdf_x - cdf_y))
    if i < n:
        d = max(d, abs(1.0 - cdf_y))
    if j < m:
        d = max(d, abs(cdf_x - 1.0))

    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d

    def kolmogorov_smirnov_q(lmbd: float) -> float:
        if not math.isfinite(lmbd) or lmbd <= 0:
            return 1.0
        s = 0.0
        k = 1
        while True:
            term = 2.0 * ((-1) ** (k - 1)) * math.exp(-2.0 * (k * k) * (lmbd * lmbd))
            s_prev = s
            s += term
            if abs(s - s_prev) < 1e-12 or k > 1000:
                break
            k += 1
        return max(0.0, min(1.0, s))

    p = kolmogorov_smirnov_q(lam)
    return d, p


def format_units(v: float, units: str) -> str:
    if units == "seconds":
        return f"{v/1000.0:.6f}"
    return f"{v:.3f}"

# ----------------------------- Loaders -----------------------------

def load_opendc_from_parquet(task_parquet: Path) -> Tuple[List[float], List[float], List[float]]:
    """Load OpenDC task data from Parquet, filter task_state=='COMPLETED'.
    Returns (submission_times, schedule_times, finish_times) in ms (floats).
    Required columns: submission_time, schedule_time, finish_time, task_state
    """
    try:
        df = pd.read_parquet(task_parquet)
    except ImportError as e:
        raise SystemExit(
            "Failed to read Parquet. Install a Parquet engine, e.g.:\n"
            "  pip install pyarrow\n"
            "or  pip install fastparquet\n"
            f"Original error: {e}"
        )
    except Exception as e:
        raise SystemExit(f"Error reading {task_parquet}: {e}")

    required = {"submission_time", "schedule_time", "finish_time", "task_state"}
    missing = sorted(list(required - set(df.columns)))
    if missing:
        raise SystemExit(f"Parquet missing required columns: {missing}")

    df = df[df["task_state"] == "COMPLETED"].copy()
    df = df[["submission_time", "schedule_time", "finish_time"]].dropna()

    # ensure numeric
    for col in ["submission_time", "schedule_time", "finish_time"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()

    subs = df["submission_time"].astype(float).tolist()
    scheds = df["schedule_time"].astype(float).tolist()
    fins = df["finish_time"].astype(float).tolist()
    return subs, scheds, fins


def load_invocation_waits_and_fins(jsonl_path: Path) -> Tuple[List[float], List[float], List[float]]:
    waits: List[float] = []
    subs: List[float] = []
    fins: List[float] = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_enq = obj.get("ts_enqueue")
            ts_st  = obj.get("ts_start")
            ts_end = obj.get("ts_end")
            if ts_enq is None or ts_st is None:
                continue
            try:
                waits.append(float(ts_st) - float(ts_enq))
                subs.append(float(ts_enq))
                if ts_end is not None:
                    fins.append(float(ts_end))
            except Exception:
                continue
    return waits, subs, fins

# ----------------------------- Throughput helpers -----------------------------

def compute_bins(start_ms: float, end_ms: float, step_ms: int) -> List[int]:
    if end_ms < start_ms:
        return []
    num_bins = int((end_ms - start_ms) // step_ms) + 1
    return [int(start_ms + i * step_ms) for i in range(num_bins)]


def throughput_per_step(finish_times_ms: List[float], bins_ms: List[int], step_ms: int) -> List[int]:
    counts = [0] * len(bins_ms)
    if not finish_times_ms or not bins_ms:
        return counts
    start_ms = bins_ms[0]
    for fin in finish_times_ms:
        if fin < start_ms:
            continue
        idx = int((fin - start_ms) // step_ms)
        if 0 <= idx < len(counts):
            counts[idx] += 1
    return counts


def cumsum_int(xs: List[int]) -> List[int]:
    out: List[int] = []
    s = 0
    for v in xs:
        s += v
        out.append(s)
    return out


def rmse(a: List[float], b: List[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return float("nan")
    se = 0.0
    for i in range(n):
        d = a[i] - b[i]
        se += d * d
    return (se / n) ** 0.5

# ----------------------------- Main -----------------------------

def main():
    p = argparse.ArgumentParser(description="Unified wait and throughput comparison (ms units; OpenDC Parquet filtered to COMPLETED)")
    p.add_argument("--task-parquet", type=Path, default=Path("20250904001/task.parquet"))
    p.add_argument("--invocations", type=Path, default=Path("20250904001/invocations_merged.jsonl"))
    p.add_argument("--drop-negative", action="store_true", help="Drop negative waits (if any)")
    p.add_argument("--units", choices=["ms", "seconds"], default="ms", help="Display units for waits")
    p.add_argument("--bin-ms", type=int, default=MINUTE_MS, help="Throughput bin size in ms (default 60,000)")
    p.add_argument("--export", type=Path, default=None, help="Export aligned throughput/cumulative CSV to this path")
    # Audit report options
    p.add_argument("--report", type=Path, default=None, help="Write a Markdown audit report to this path")
    # Wait distribution thresholds
    p.add_argument("--ks-alpha", type=float, default=0.05, help="KS test alpha threshold (default 0.05)")
    p.add_argument("--q50-th-pct", type=float, default=5.0, help="Threshold for p50 relative error % (default 5.0)")
    p.add_argument("--q95-th-pct", type=float, default=10.0, help="Threshold for p95 relative error % (default 10.0)")
    p.add_argument("--q99-th-pct", type=float, default=20.0, help="Threshold for p99 relative error % (default 20.0)")
    # Throughput/cumulative thresholds
    p.add_argument("--rmse-threshold-pct", type=float, default=3.0, help="Threshold for cumulative RMSE % after best-lag alignment (default 3.0)")
    p.add_argument("--rmse-max-lag-bins", type=int, default=10, help="Max absolute lag in bins to search for best RMSE alignment")
    p.add_argument("--makespan-threshold-pct", type=float, default=1.0, help="Threshold for makespan error % if duration >= 10 minutes (default 1.0)")
    p.add_argument("--makespan-abs-threshold-sec", type=float, default=6.0, help="Absolute makespan error threshold (seconds) if duration < 10 minutes (default 6s)")
    p.add_argument("--max-align-delta-ms", type=int, default=500,
                   help="Max allowed absolute time difference |Δt| for nearest match in ms (default 500)")

    # Proc vs Task (cpu_usage_mhz) options
    p.add_argument("--proc-metrics", type=Path, default=Path("20250904001/proc_metrics_merged.jsonl"),
                   help="Path to merged proc_metrics JSONL (default 20250904001/proc_metrics_merged.jsonl); if missing, this section is skipped")
    p.add_argument("--task-ts-col", type=str, default=None, help="Timestamp column name in task.parquet (auto-detect if omitted)")
    p.add_argument("--export-proc-task", type=Path, default=None,
                   help="Export matched pairs of (computed cpu_usage_mhz vs task cpu_usage) to CSV")
    p.add_argument("--plot-cpu-scatter", type=Path, default=None,
                   help="If set, save CPU usage scatter (task cpu_usage vs proc cpu_usage_mhz) to this PNG path")
    # Proc vs Task thresholds (standard)
    p.add_argument("--cpu-smape-th-pct", type=float, default=5.0, help="sMAPE threshold % (default 5.0)")
    p.add_argument("--cpu-r-th", type=float, default=0.95, help="Pearson r threshold (default 0.95)")
    p.add_argument("--cpu-rmse-frac-median-th-pct", type=float, default=10.0, help="RMSE threshold as % of task-side median cpu_usage_mhz (default 10%)")

    args = p.parse_args()

    # Load OpenDC Parquet and filter COMPLETED
    o_subs, o_scheds, o_fins = load_opendc_from_parquet(args.task_parquet)

    # Load Continuum waits and finishes
    c_waits, c_subs, c_fins = load_invocation_waits_and_fins(args.invocations)

    # Wait distributions
    waits_task = [sch - sub for sch, sub in zip(o_scheds, o_subs)]
    waits_inv = c_waits

    if args.drop_negative:
        before_t, before_i = len(waits_task), len(waits_inv)
        waits_task = [w for w in waits_task if w >= 0]
        waits_inv = [w for w in waits_inv if w >= 0]
        print(f"Dropped negatives: task {before_t-len(waits_task)}, inv {before_i-len(waits_inv)}")

    print("=== Wait-time Distribution (COMPLETED only on OpenDC) ===")
    print(f"Loaded waits: OpenDC {len(waits_task)} items, Continuum {len(waits_inv)} items")

    ps = [0.50, 0.95, 0.99]
    q_task = percentiles(waits_task, ps)
    # === New: proc_metrics (computed cpu_usage_mhz) vs task.parquet cpu_usage ===
    try:
        proc_path = args.proc_metrics
    except AttributeError:
        proc_path = None
    proc_section_ran = False
    proc_stats = None
    if proc_path is not None and isinstance(proc_path, Path) and proc_path.exists():
        try:
            dfp = pd.read_json(proc_path, lines=True)
        except Exception as e:
            print(f"\n[proc_metrics] Failed to read {proc_path}: {e}")
            dfp = None
        if dfp is not None:
            needed_p = {"pid", "cpu_ms", "dt_ms", "ts_ms"}
            miss_p = sorted(list(needed_p - set(dfp.columns)))
            if miss_p:
                print(f"\n[proc_metrics] Missing columns {miss_p}; skipping this section.")
            else:
                # drop invalids and compute cpu_usage_mhz
                dfp = dfp.copy()
                # prefer rows with cpu_freq_mhz; if missing, cannot compute usage in MHz
                if "cpu_freq_mhz" not in dfp.columns:
                    print("\n[proc_metrics] Column 'cpu_freq_mhz' not found; skipping this section.")
                else:
                    dfp["dt_ms"] = pd.to_numeric(dfp["dt_ms"], errors="coerce")
                    dfp["cpu_ms"] = pd.to_numeric(dfp["cpu_ms"], errors="coerce")
                    dfp["cpu_freq_mhz"] = pd.to_numeric(dfp["cpu_freq_mhz"], errors="coerce")
                    dfp["ts_ms"] = pd.to_numeric(dfp["ts_ms"], errors="coerce")
                    dfp = dfp.dropna(subset=["pid", "cpu_ms", "dt_ms", "ts_ms", "cpu_freq_mhz"]).copy()
                    dfp = dfp[dfp["dt_ms"] > 0]
                    dfp["cores_used"] = dfp["cpu_ms"] / dfp["dt_ms"]
                    dfp["cpu_usage_mhz"] = dfp["cores_used"] * dfp["cpu_freq_mhz"]
                    # load task parquet (no task_state filter)
                    try:
                        dft = pd.read_parquet(args.task_parquet)
                    except Exception as e:
                        print(f"\n[proc_metrics] Failed to read task parquet: {e}")
                        dft = None
                    if dft is not None:
                        # required fields
                        if "task_id" not in dft.columns or "cpu_usage" not in dft.columns:
                            print("\n[proc_metrics] task.parquet needs columns 'task_id' and 'cpu_usage'; skipping.")
                        else:
                            # detect timestamp column and optional RUNNING state column
                            ts_col = detect_task_ts_col(dft, args.task_ts_col)
                            running_col = None
                            for c in ["state", "task_state", "status"]:
                                if c in dft.columns:
                                    running_col = c
                                    break
                            # if we have a state/status column, keep only RUNNING rows (string contains 'RUNNING')
                            if running_col is not None:
                                try:
                                    mask_running = dft[running_col].astype(str).str.upper().str.contains("RUNNING")
                                    dft = dft[mask_running].copy()
                                except Exception:
                                    pass
                            # select minimal columns and ensure numeric
                            dft = dft[["task_id", ts_col, "cpu_usage"]].copy()
                            dft[ts_col] = pd.to_numeric(dft[ts_col], errors="coerce")
                            dft["cpu_usage"] = pd.to_numeric(dft["cpu_usage"], errors="coerce")
                            dft = dft.dropna(subset=["task_id", ts_col, "cpu_usage"])
                            # align types for join key and timestamps
                            dft = dft.rename(columns={"task_id": "pid", ts_col: "task_ts"})
                            dfp["pid"] = pd.to_numeric(dfp["pid"], errors="coerce")
                            dft["pid"] = pd.to_numeric(dft["pid"], errors="coerce")
                            dft["task_ts"] = pd.to_numeric(dft["task_ts"], errors="coerce")
                            dfp["ts_ms"] = pd.to_numeric(dfp["ts_ms"], errors="coerce")
                            # drop rows with NA keys
                            dfp = dfp.dropna(subset=["pid", "ts_ms"]).copy()
                            dft = dft.dropna(subset=["pid", "task_ts"]).copy()
                            # ensure integer dtype for pid
                            dfp["pid"] = dfp["pid"].astype("int64")
                            dft["pid"] = dft["pid"].astype("int64")
                            # sort for per-pid processing
                            dfp = dfp.sort_values(["pid", "ts_ms"]).reset_index(drop=True)
                            dft = dft.sort_values(["pid", "task_ts"]).reset_index(drop=True)

                            # Relative-time alignment per pid:
                            # For each pid, define t0_task = first task_ts (after RUNNING filter),
                            # t0_proc = first ts_ms on proc side, then align on (t_rel_proc vs t_rel_task)
                            merged_parts = []
                            common_pids = sorted(set(dfp["pid"].unique()).intersection(set(dft["pid"].unique())))
                            for _pid in common_pids:
                                lf = dfp[dfp["pid"] == _pid][["pid", "ts_ms", "cpu_usage_mhz"]].sort_values("ts_ms")
                                rf = dft[dft["pid"] == _pid][["pid", "task_ts", "cpu_usage"]].sort_values("task_ts")
                                if lf.empty or rf.empty:
                                    continue
                                # establish per-side t0
                                t0_proc = float(lf["ts_ms"].iloc[0])
                                t0_task = float(rf["task_ts"].iloc[0])
                                lf2 = lf.copy()
                                rf2 = rf.copy()
                                lf2["t_rel_proc"] = lf2["ts_ms"].astype(float) - t0_proc
                                rf2["t_rel_task"] = rf2["task_ts"].astype(float) - t0_task
                                # nearest join on relative time
                                part = pd.merge_asof(
                                    lf2[["pid", "ts_ms", "t_rel_proc", "cpu_usage_mhz"]].sort_values("t_rel_proc"),
                                    rf2[["pid", "task_ts", "t_rel_task", "cpu_usage"]].sort_values("t_rel_task"),
                                    left_on="t_rel_proc", right_on="t_rel_task",
                                    direction="nearest", allow_exact_matches=True
                                )
                                part["pid"] = int(_pid)
                                # compute |Δt_rel| and filter
                                part["delta_ms"] = (part["t_rel_proc"] - part["t_rel_task"]).abs()
                                merged_parts.append(part[["pid", "ts_ms", "task_ts", "t_rel_proc", "t_rel_task", "cpu_usage_mhz", "cpu_usage", "delta_ms"]])
                            merged = pd.concat(merged_parts, ignore_index=True) if merged_parts else pd.DataFrame()
                            merged = merged.dropna(subset=["cpu_usage_mhz", "cpu_usage"]).copy()
                            if merged.empty:
                                print("\n[proc_metrics] No matched pairs after relative-time alignment; skipping metrics.")
                            else:
                                # No |Δt_rel| filtering (use all nearest relative-time pairs)
                                before_n = int(len(merged))
                                dropped_n = 0
                                proc_section_ran = True
                                y_pred = merged["cpu_usage_mhz"].astype(float).tolist()
                                y_true = merged["cpu_usage"].astype(float).tolist()
                                smape_v = compute_smape(y_true, y_pred)
                                rmse_v = rmse_list(y_true, y_pred)
                                r_v, r_p = safe_pearsonr(y_true, y_pred)
                                med_true = float(pd.Series(y_true).median()) if y_true else float("nan")
                                rmse_frac_median_pct = (rmse_v / med_true * 100.0) if (med_true and med_true == med_true and med_true != 0.0) else float("nan")
                                cpu_pass = ((smape_v == smape_v) and (smape_v <= args.cpu_smape_th_pct)
                                            and (r_v == r_v) and (r_v >= args.cpu_r_th)
                                            and (rmse_frac_median_pct == rmse_frac_median_pct) and (rmse_frac_median_pct <= args.cpu_rmse_frac_median_th_pct))
                                # show
                                print("\n=== Proc vs Task CPU Usage (per pid, relative-time from first timestamp; task RUNNING window) ===")
                                print(f"Pairs matched: {len(merged)}  (no |Δt_rel| filter)")
                                print(f"sMAPE: {smape_v:.3f}%   RMSE: {rmse_v:.3f} MHz   Pearson r: {r_v:.4f}" + (f" (p={r_p:.3g})" if r_p is not None else ""))
                                print(f"RMSE as % of task-side median: {rmse_frac_median_pct:.3f}%  -> {'PASS' if cpu_pass else 'FAIL'}")
                                # export
                                if args.export_proc_task is not None:
                                    outp = args.export_proc_task
                                    outp.parent.mkdir(parents=True, exist_ok=True)
                                    merged_out = merged[["pid", "ts_ms", "task_ts", "t_rel_proc", "t_rel_task", "cpu_usage_mhz", "cpu_usage", "delta_ms"]].copy()
                                # optional scatter plot
                                if args.plot_cpu_scatter is not None:
                                    try:
                                        import matplotlib.pyplot as plt
                                        fig, ax = plt.subplots(figsize=(4.5, 4.5))
                                        x = merged["cpu_usage"].astype(float).values
                                        y = merged["cpu_usage_mhz"].astype(float).values
                                        ax.scatter(x, y, s=6, alpha=0.4, color="tab:blue")
                                        lim_lo = float(min(x.min(), y.min()))
                                        lim_hi = float(max(x.max(), y.max()))
                                        ax.plot([lim_lo, lim_hi], [lim_lo, lim_hi], color="tab:gray", linestyle=":", linewidth=1)
                                        ax.set_xlabel("task cpu_usage (MHz)")
                                        ax.set_ylabel("proc cpu_usage_mhz (MHz)")
                                        ax.set_title(f"CPU usage scatter (n={len(merged)})\nr={r_v:.3f}, sMAPE={smape_v:.2f}%, RMSE={rmse_v:.1f} MHz")
                                        ax.grid(alpha=0.3, linestyle=":")
                                        args.plot_cpu_scatter.parent.mkdir(parents=True, exist_ok=True)
                                        fig.tight_layout()
                                        fig.savefig(args.plot_cpu_scatter, dpi=150)
                                        plt.close(fig)
                                        print(f"Saved CPU usage scatter: {args.plot_cpu_scatter}")
                                    except Exception as e:
                                        print(f"[plot] Failed to save CPU scatter: {e}")

                                    merged_out.to_csv(outp, index=False)
                                    print(f"Exported proc-task matched pairs to: {outp}")
                                proc_stats = {
                                    "pairs": int(len(merged)),
                                    "smape_pct": float(smape_v),
                                    "rmse_mhz": float(rmse_v),
                                    "pearson_r": float(r_v),
                                    "pearson_p": (float(r_p) if r_p is not None else None),
                                    "rmse_frac_median_pct": float(rmse_frac_median_pct),
                                    "max_align_delta_ms": None,
                                    "dropped_by_dt": int(dropped_n),
                                    "pass": bool(cpu_pass),
                                }
    # end proc section

    q_inv = percentiles(waits_inv, ps)

    d, pval = ks_2samp_basic(waits_task, waits_inv)

    header = ["Metric", "OpenDC", "Continuum"]
    rows = [("p50", q_task[0.50], q_inv[0.50]), ("p95", q_task[0.95], q_inv[0.95]), ("p99", q_task[0.99], q_inv[0.99])]
    colw0 = max(len(h) for h in header)
    colw1 = max(10, *(len(format_units(v, args.units)) for _, v, _ in rows))
    colw2 = max(10, *(len(format_units(v, args.units)) for _, _, v in rows))
    print(f"{header[0]:<{colw0}}  {header[1]:>{colw1}}  {header[2]:>{colw2}}  (units: {args.units})")
    # Initialize throughput metrics for reporting defaults
    rmse_pct = float("nan")
    rmse_pct_zero = float("nan")
    abs_rmse_zero = float("nan")
    best_lag_bins = 0
    makespan_err_pct = float("nan")
    makespan_err_abs_sec = float("nan")
    short_run = False

    for lab, v1, v2 in rows:
        print(f"{lab:<{colw0}}  {format_units(v1, args.units):>{colw1}}  {format_units(v2, args.units):>{colw2}}")

    print("\nKolmogorov-Smirnov 2-sample test:")
    print(f"D statistic: {d:.6f}")
    print(f"p-value:     {pval:.6g}  (alpha=0.05 usual threshold)")

    # Throughput/cumulative (use relative timelines anchored at each side's min submission)
    throughput_available = bool(o_fins and c_fins)
    if not throughput_available:
        print("\n[Throughput] No finish times loaded from one or both sources; skipping throughput analysis.")
    else:
        o_start = min(o_subs)
        c_start = min(c_subs) if c_subs else 0.0
        o_fins_rel = [fin - o_start for fin in o_fins]
        c_fins_rel = [fin - c_start for fin in c_fins]
        rel_end = max(max(o_fins_rel), max(c_fins_rel))

        bins = compute_bins(0.0, rel_end, args.bin_ms)
        o_thr = throughput_per_step(o_fins_rel, bins, args.bin_ms)
        c_thr = throughput_per_step(c_fins_rel, bins, args.bin_ms)

        o_cum = cumsum_int(o_thr)
        c_cum = cumsum_int(c_thr)

        # Zero-lag RMSE and % (denominator = final completion total on Continuum side)
        abs_rmse_zero = rmse([float(x) for x in c_cum], [float(x) for x in o_cum])
        denom_tasks = float(len(c_fins)) if len(c_fins) > 0 else float("nan")
        rmse_pct_zero = (abs_rmse_zero / denom_tasks * 100.0) if (denom_tasks == denom_tasks and denom_tasks > 0) else float("nan")

        # Best-lag search in integer bins to minimize RMSE
        best_rmse = abs_rmse_zero
        best_rmse_pct = rmse_pct_zero
        best_lag_bins = 0
        max_lag = int(max(0, args.rmse_max_lag_bins))
        for lag in range(-max_lag, max_lag + 1):
            if lag == 0:
                continue
            if lag > 0:
                # shift Continuum forward by 'lag' bins
                a = c_cum[lag:]
                b = o_cum[:len(a)]
            else:
                # lag < 0: shift OpenDC forward by -lag bins
                a = c_cum[:len(c_cum) + lag]
                b = o_cum[-lag:]
            n = min(len(a), len(b))
            if n <= 1:
                continue
            r = rmse([float(x) for x in a[:n]], [float(x) for x in b[:n]])
            if (r == r) and (best_rmse != best_rmse or r < best_rmse):
                best_rmse = r
                best_lag_bins = lag
        # compute % for best
        best_rmse_pct = (best_rmse / denom_tasks * 100.0) if (best_rmse == best_rmse and denom_tasks == denom_tasks and denom_tasks > 0) else float("nan")

        # Makespan error with dual-threshold rule
        c_makespan = max(c_fins) - min(c_subs) if c_subs else float("nan")
        o_makespan = max(o_fins) - min(o_subs)
        makespan_err_pct = (abs(c_makespan - o_makespan) / c_makespan * 100.0) if (isinstance(c_makespan, float) and c_makespan > 0) else float("nan")
        makespan_err_abs_sec = (abs(c_makespan - o_makespan) / 1000.0) if (isinstance(c_makespan, float) and isinstance(o_makespan, float)) else float("nan")
        short_run = (isinstance(c_makespan, float) and c_makespan == c_makespan and c_makespan < 600_000.0)

        print("\n=== Throughput/Cumulative Comparison (relative axis; per-bin counts) ===")
        print(f"Relative span (ms): 0 .. {int(rel_end)}  Bins: {len(bins)}  Bin size: {args.bin_ms} ms")
        print(f"Total tasks  Continuum: {len(c_fins)}  OpenDC: {len(o_fins)}")
        print("\nCumulative curves RMSE (denom = final completion count on Continuum):")
        print(f"  Zero-lag RMSE (tasks): {abs_rmse_zero:.3f}   RMSE%: {rmse_pct_zero:.2f}%")
        print(f"  Best-lag  RMSE (tasks): {best_rmse:.3f}   RMSE%: {best_rmse_pct:.2f}%   Lag (bins): {best_lag_bins}")
        print("\nMakespan:")
        if short_run:
            print(f"  Duration < 10 min -> use absolute threshold. |Δ| = {makespan_err_abs_sec:.3f}s")
        else:
            print(f"  Use percentage threshold. Error = {makespan_err_pct:.2f}%")

        # overwrite rmse_pct with best-lag value for downstream PASS/FAIL
        rmse_pct = best_rmse_pct

    if args.export is not None:
        out_path = args.export
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_ms", "continuum_throughput", "opendc_throughput", "continuum_cumulative", "opendc_cumulative"])
            for i, t in enumerate(bins):
                w.writerow([
                    t,
                    c_thr[i] if i < len(c_thr) else 0,
                    o_thr[i] if i < len(o_thr) else 0,
                    c_cum[i] if i < len(c_cum) else (c_cum[-1] if c_cum else 0),
                    o_cum[i] if i < len(o_cum) else (o_cum[-1] if o_cum else 0),
                ])
        print(f"\nExported aligned throughput/cumulative series to: {out_path}")

    # Optional Markdown audit report
    if args.report is not None:
        def fmt_num(v: Optional[float], suffix: str = "", nd: int = 2) -> str:
            if v is None:
                return "NA"
            try:
                if isinstance(v, float) and (v != v):  # NaN check
                    return "NA"
                return (f"{v:.{nd}f}{suffix}")
            except Exception:
                return str(v)

        ks_pass = (pval == pval) and (pval >= args.ks_alpha)  # pval==pval filters NaN
        # RMSE pass uses best-lag RMSE percentage
        rmse_pass = (rmse_pct == rmse_pct) and (rmse_pct <= args.rmse_threshold_pct)
        # Makespan pass uses absolute threshold if short run (<10 min), else percentage
        if short_run:
            mksp_pass = (makespan_err_abs_sec == makespan_err_abs_sec) and (makespan_err_abs_sec <= args.makespan_abs_threshold_sec)
        else:
            mksp_pass = (makespan_err_pct == makespan_err_pct) and (makespan_err_pct <= args.makespan_threshold_pct)

        # Quantile relative errors (relative to Continuum)
        def _rel_pct(a: float, b: float) -> float:
            try:
                denom = abs(b)
                if denom <= 0:
                    return float("nan")
                return abs(a - b) / denom * 100.0
            except Exception:
                return float("nan")
        p50_err_pct = _rel_pct(q_task.get(0.50, float("nan")), q_inv.get(0.50, float("nan")))
        p95_err_pct = _rel_pct(q_task.get(0.95, float("nan")), q_inv.get(0.95, float("nan")))
        p99_err_pct = _rel_pct(q_task.get(0.99, float("nan")), q_inv.get(0.99, float("nan")))
        q_pass = (
            (p50_err_pct == p50_err_pct and p50_err_pct <= args.q50_th_pct) and
            (p95_err_pct == p95_err_pct and p95_err_pct <= args.q95_th_pct) and
            (p99_err_pct == p99_err_pct and p99_err_pct <= args.q99_th_pct)
        )

        lines = []
        lines.append("# Audit Report: OpenDC vs Continuum")
        lines.append("")
        lines.append("## Inputs")
        lines.append(f"- OpenDC tasks (Parquet, COMPLETED only): {args.task_parquet}")
        lines.append(f"- Continuum invocations (JSONL): {args.invocations}")
        if args.export is not None:
            lines.append(f"- Aligned throughput/cumulative CSV: {args.export}")
        if proc_section_ran:
            lines.append(f"- Proc metrics merged: {args.proc_metrics}")
            if args.export_proc_task is not None:
                lines.append(f"- Proc-Task matched CSV: {args.export_proc_task}")
        lines.append("")

        lines.append("## 1) Wait-time Distribution")
        lines.append(f"- Samples: OpenDC={len(waits_task)}, Continuum={len(waits_inv)}")
        lines.append(f"- Quantiles ({args.units}):")
        lines.append(f"  - p50: OpenDC={format_units(q_task[0.50], args.units)}, Continuum={format_units(q_inv[0.50], args.units)}")
        lines.append(f"  - p95: OpenDC={format_units(q_task[0.95], args.units)}, Continuum={format_units(q_inv[0.95], args.units)}")
        lines.append(f"  - p99: OpenDC={format_units(q_task[0.99], args.units)}, Continuum={format_units(q_inv[0.99], args.units)}")
        lines.append(f"- Quantile relative errors (w.r.t Continuum): p50={fmt_num(p50_err_pct, '%')}, p95={fmt_num(p95_err_pct, '%')}, p99={fmt_num(p99_err_pct, '%')} -> {'PASS' if q_pass else 'FAIL'} (thresholds p50<={args.q50_th_pct}%, p95<={args.q95_th_pct}%, p99<={args.q99_th_pct}%)")
        lines.append(f"- KS test: D={d:.6f}, p={pval:.6g}, alpha={args.ks_alpha} -> {'PASS' if ks_pass else 'FAIL'}")
        lines.append("")

        if throughput_available:
            lines.append("## 2) Throughput / Cumulative Completion")
            lines.append(f"- Bin size: {args.bin_ms} ms; Bins: {len(bins)}")
            lines.append(f"- Total tasks: Continuum={len(c_fins)}, OpenDC={len(o_fins)}")
            lines.append("- Cumulative RMSE (denom = final completion count on Continuum):")
            lines.append(f"  - Zero-lag: {fmt_num(abs_rmse_zero, ' tasks', 3)} ({fmt_num(rmse_pct_zero, '%')})")
            lines.append(f"  - Best-lag: {fmt_num(best_rmse, ' tasks', 3)} ({fmt_num(rmse_pct, '%')}) at lag_bins={best_lag_bins} -> {'PASS' if rmse_pass else 'FAIL'} (threshold {args.rmse_threshold_pct}%)")
            if short_run:
                lines.append(f"- Makespan: Continuum={fmt_num(float(c_makespan) if isinstance(c_makespan, float) else float('nan'))} ms, "
                             f"OpenDC={fmt_num(float(o_makespan))} ms, |Δ|={fmt_num(makespan_err_abs_sec, 's')} -> {'PASS' if mksp_pass else 'FAIL'} (abs threshold {args.makespan_abs_threshold_sec}s)")
            else:
                lines.append(f"- Makespan: Continuum={fmt_num(float(c_makespan) if isinstance(c_makespan, float) else float('nan'))} ms, "
                             f"OpenDC={fmt_num(float(o_makespan))} ms, Error={fmt_num(makespan_err_pct, '%')} -> {'PASS' if mksp_pass else 'FAIL'} (threshold {args.makespan_threshold_pct}%)")
            lines.append("")
        if proc_section_ran and proc_stats is not None:
            lines.append("## 3) Proc vs Task CPU Usage")
            lines.append(f"- Max |Δt| filter: {proc_stats.get('max_align_delta_ms', args.max_align_delta_ms)} ms; Dropped: {proc_stats.get('dropped_by_dt', 'NA')}")
            lines.append(f"- Pairs matched: {proc_stats['pairs']}")
            lines.append(f"- sMAPE: {fmt_num(proc_stats.get('smape_pct'), '%', 3)}")
            lines.append(f"- RMSE: {fmt_num(proc_stats.get('rmse_mhz'), ' MHz', 3)}  (as % of task-side median: {fmt_num(proc_stats.get('rmse_frac_median_pct'), '%', 3)})")
            rtxt = f"{proc_stats['pearson_r']:.4f}" if proc_stats['pearson_r'] == proc_stats['pearson_r'] else "NA"
            if proc_stats.get('pearson_p') is not None:
                rtxt += f" (p={proc_stats['pearson_p']:.3g})"
            lines.append(f"- Pearson r: {rtxt}")
            lines.append(f"- Verdict: {'PASS' if proc_stats.get('pass') else 'FAIL'} (thresholds: sMAPE<={args.cpu_smape_th_pct}%, r>={args.cpu_r_th}, RMSE% median<={args.cpu_rmse_frac_median_th_pct}%)")
            lines.append("")


        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('w', encoding='utf-8') as rf:
            rf.write("\n".join(lines) + "\n")
        print(f"\nWrote audit report: {args.report}")


if __name__ == "__main__":
    main()

