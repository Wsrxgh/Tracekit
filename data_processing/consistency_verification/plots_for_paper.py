#!/usr/bin/env python3
"""
Plots for paper sections 5.2.1–5.2.4 based on algorithms/values from
compare_latency_throughput.py and power/reconstruct_power.py.

Generates four plots:
1) Task Wait Time Distribution (CDF): Continuum vs OpenDC
2) Cumulative Completion Curve: Continuum vs OpenDC
3) Instantaneous CPU Demand (scatter): Physical (proc_metrics) vs Simulation (fragments)
4) Total Power Draw Trend (Z-score): Continuum (reconstructed total) vs OpenDC (powerSource.parquet)

Inputs (typical):
--task-parquet <.../task.parquet>
--invocations <.../invocations_merged.jsonl>
--proc-metrics <.../proc_metrics_merged.jsonl>
--fragments <.../fragments.parquet>
--power-total <.../power/power_total_corrected.csv>
--power-source <.../power/powerSource.parquet>

Outputs: PNG files written to --out-dir (default: consistency_verification/out_plots)

Dependencies: pandas, numpy, matplotlib. For Parquet, install pyarrow or fastparquet.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import List, Tuple, Optional

import math
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

MINUTE_MS = 60_000

# ---------- Utilities (mirroring logic from compare_latency_throughput.py) ----------

def percentiles(data: List[float], ps: List[float]):
    if not data:
        return {p: float('nan') for p in ps}
    xs = sorted(data)
    n = len(xs)
    out = {}
    for p in ps:
        if p <= 0: out[p] = xs[0]; continue
        if p >= 1: out[p] = xs[-1]; continue
        idx = (n - 1) * p
        lo = int(math.floor(idx)); hi = int(math.ceil(idx))
        out[p] = xs[lo] if lo == hi else (xs[lo] * (1 - (idx - lo)) + xs[hi] * (idx - lo))
    return out

def compute_bins(start_ms: float, end_ms: float, step_ms: int) -> List[int]:
    if end_ms < start_ms: return []
    num_bins = int((end_ms - start_ms) // step_ms) + 1
    return [int(start_ms + i * step_ms) for i in range(num_bins)]

def throughput_per_step(finish_times_ms: List[float], bins_ms: List[int], step_ms: int) -> List[int]:
    counts = [0] * len(bins_ms)
    if not finish_times_ms or not bins_ms: return counts
    start_ms = bins_ms[0]
    for fin in finish_times_ms:
        if fin < start_ms: continue
        idx = int((fin - start_ms) // step_ms)
        if 0 <= idx < len(counts): counts[idx] += 1
    return counts

def cumsum_int(xs: List[int]) -> List[int]:
    out = []
    s = 0
    for v in xs: s += v; out.append(s)
    return out

# ---------- Loaders (same sources as analysis scripts) ----------

def load_opendc_from_parquet(task_parquet: Path) -> Tuple[List[float], List[float], List[float]]:
    df = pd.read_parquet(task_parquet)
    req = {"submission_time", "schedule_time", "finish_time", "task_state"}
    missing = sorted(list(req - set(df.columns)))
    if missing:
        raise SystemExit(f"Parquet missing required columns: {missing}")
    df = df[df["task_state"] == "COMPLETED"][["submission_time", "schedule_time", "finish_time"]].dropna()
    for col in ["submission_time", "schedule_time", "finish_time"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    return (
        df["submission_time"].astype(float).tolist(),
        df["schedule_time"].astype(float).tolist(),
        df["finish_time"].astype(float).tolist(),
    )

def load_invocation_waits_and_fins(jsonl_path: Path) -> Tuple[List[float], List[float], List[float]]:
    waits, subs, fins = [], [], []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip(): continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_enq = obj.get("ts_enqueue"); ts_st = obj.get("ts_start"); ts_end = obj.get("ts_end")
            if ts_enq is None or ts_st is None: continue
            try:
                waits.append(float(ts_st) - float(ts_enq))
                subs.append(float(ts_enq))
                if ts_end is not None: fins.append(float(ts_end))
            except Exception:
                continue
    return waits, subs, fins

# ---------- Plot 1: Task Wait Time Distribution (CDF) ----------

def plot_wait_cdf(o_subs: List[float], o_scheds: List[float], c_waits: List[float], out_path: Path):
    waits_task = [sch - sub for sch, sub in zip(o_scheds, o_subs)]
    waits_task_s = np.array(waits_task, dtype=float) / 1000.0
    waits_inv_s = np.array(c_waits, dtype=float) / 1000.0
    waits_task_s = waits_task_s[np.isfinite(waits_task_s)]
    waits_inv_s = waits_inv_s[np.isfinite(waits_inv_s)]
    waits_task_s = waits_task_s[waits_task_s >= 0]
    waits_inv_s = waits_inv_s[waits_inv_s >= 0]
    def ecdf(x: np.ndarray):
        if x.size == 0:
            return np.array([0.0]), np.array([0.0])
        xs = np.sort(x)
        ys = np.linspace(1.0/len(xs), 1.0, len(xs))
        return xs, ys
    x1, y1 = ecdf(waits_inv_s)
    x2, y2 = ecdf(waits_task_s)
    # Two-panel layout: top for CDF, bottom for residual (OpenDC - Continuum)
    fig, (ax, axr) = plt.subplots(2, 1, figsize=(8, 6.5), sharex=True, gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.05})
    # Physical (Continuum): thick grey solid
    ax.plot(x1, y1, label="Physical (Continuum)", color='grey', linewidth=3, zorder=2)
    # Simulation (OpenDC): thin blue dashed with sparse markers
    markevery = max(1, len(x2)//20)
    ax.plot(x2, y2, label="Simulation (OpenDC)", color='#1f77b4', linestyle='--', linewidth=1.5, marker='o', markersize=3, markevery=markevery, zorder=3)
    for y in [0.5, 0.95, 0.99]:
        ax.axhline(y, color='gray', alpha=0.2, linestyle='--', linewidth=1)
    ax.set_ylabel("Cumulative Probability")
    ax.set_ylim(0.0, 1.0)
    ax.grid(True, alpha=0.3)
    ax.legend()
    # Ensure x starts at 0 and touches left axis (no left margin)
    try:
        ax.set_xlim(left=0)
        ax.margins(x=0)
    except Exception:
        pass
    # Residual panel
    xs = np.sort(np.unique(np.concatenate([x1, x2])))
    y1i = np.interp(xs, x1, y1, left=0.0, right=1.0)
    y2i = np.interp(xs, x2, y2, left=0.0, right=1.0)
    resid = y2i - y1i
    axr.axhline(0.0, color='black', linewidth=1)
    axr.plot(xs, resid, color='#ff7f0e', linewidth=1.2)
    axr.set_ylabel("Δ")
    axr.grid(True, alpha=0.3)
    axr.set_xlabel("Task Wait Time [s]")
    try:
        axr.set_xlim(left=0)
        axr.margins(x=0)
    except Exception:
        pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(); fig.savefig(out_path, dpi=150); plt.close(fig)

# ---------- Plot 2: Cumulative Completion Curve ----------

def plot_cumulative(o_subs: List[float], o_fins: List[float], c_subs: List[float], c_fins: List[float], bin_ms: int, out_path: Path, x_units: str = "seconds"):
    if not (o_fins and c_fins):
        print("[Cumulative] Missing finish times; skip plot.")
        return
    o_start = min(o_subs)
    c_start = min(c_subs) if c_subs else 0.0
    o_fins_rel = [fin - o_start for fin in o_fins]
    c_fins_rel = [fin - c_start for fin in c_fins]
    rel_end = max(max(o_fins_rel), max(c_fins_rel))
    bins = compute_bins(0.0, rel_end, bin_ms)
    o_thr = throughput_per_step(o_fins_rel, bins, bin_ms)
    c_thr = throughput_per_step(c_fins_rel, bins, bin_ms)
    o_cum = cumsum_int(o_thr)
    c_cum = cumsum_int(c_thr)
    t = np.array(bins, dtype=float)
    if x_units == "minutes": t = t / 60000.0; xlabel = "Time [min]"
    else: t = t / 1000.0; xlabel = "Time [s]"
    # Two-panel layout: top for cumulative, bottom for residual (OpenDC - Continuum)
    fig, (ax, axr) = plt.subplots(2, 1, figsize=(8, 6.5), sharex=True, gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.05})
    # Physical (Continuum): thick grey solid (step plot)
    ax.step(t[:len(c_cum)], c_cum, where='post', label="Physical (Continuum)", color='grey', linewidth=3, zorder=2)
    # Simulation (OpenDC): thin blue dashed with sparse markers
    markevery = max(1, len(o_cum)//20)
    ax.step(t[:len(o_cum)], o_cum, where='post', label="Simulation (OpenDC)", color='#1f77b4', linestyle='--', linewidth=1.5, zorder=3)
    ax.plot(t[:len(o_cum)], o_cum, color='#1f77b4', linestyle='--', linewidth=0.0, marker='o', markersize=3, markevery=markevery, zorder=4)
    ax.set_ylabel("Cumulative Completed Tasks")
    ax.grid(True, alpha=0.3)
    ax.legend()
    # Ensure x starts at 0 and touches left axis (no left margin)
    try:
        ax.set_xlim(left=0)
        ax.margins(x=0)
    except Exception:
        pass
    # Residual panel (align lengths)
    n = int(min(len(o_cum), len(c_cum), len(t)))
    d = (np.asarray(o_cum[:n], dtype=float) - np.asarray(c_cum[:n], dtype=float))
    axr.axhline(0.0, color='black', linewidth=1)
    axr.plot(t[:n], d, color='#ff7f0e', linewidth=1.2)
    axr.set_ylabel("Δ tasks")
    axr.grid(True, alpha=0.3)
    axr.set_xlabel(xlabel)
    try:
        axr.set_xlim(left=0)
        axr.margins(x=0)
    except Exception:
        pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(); fig.savefig(out_path, dpi=150); plt.close(fig)

# ---------- Plot 3: Instantaneous CPU Demand (Scatter) ----------

def detect_frag_ts_col(df: pd.DataFrame, override: Optional[str] = None) -> str:
    if override is not None:
        if override not in df.columns:
            raise SystemExit(f"Fragments timestamp column '{override}' not found. Available: {list(df.columns)}")
        return override
    for c in ["timestamp_absolute", "ts_ms", "timestamp_ms", "start", "ts", "time_ms", "time"]:
        if c in df.columns: return c
    raise SystemExit("Cannot find a timestamp column in fragments.parquet; pass --fragments-ts-col explicitly.")

def detect_id_col(df: pd.DataFrame, override: Optional[str] = None) -> str:
    if override is not None:
        if override not in df.columns: raise SystemExit(f"ID column '{override}' not found in fragments: {list(df.columns)}")
        return override
    for c in ["task_id", "id", "pid"]:
        if c in df.columns: return c
    raise SystemExit("Cannot find an ID column in fragments.parquet; pass --fragments-id-col explicitly.")

def plot_cpu_scatter(proc_metrics: Path, fragments: Path, out_path: Path, max_align_delta_ms: int = 500, fragments_ts_col: Optional[str] = None, fragments_id_col: Optional[str] = None, fragments_ts_scale: float = 1.0, fragments_ts_offset_ms: float = 0.0):
    try:
        dfp = pd.read_json(proc_metrics, lines=True)
    except Exception as e:
        print(f"[CPU] Failed to read proc_metrics: {e}"); return
    need = {"pid", "cpu_ms", "dt_ms", "ts_ms", "cpu_freq_mhz"}
    miss = sorted(list(need - set(dfp.columns)))
    if miss:
        print(f"[CPU] Missing columns in proc_metrics: {miss}"); return
    dfp = dfp.copy()
    for c in ["dt_ms", "cpu_ms", "cpu_freq_mhz", "ts_ms", "pid"]:
        dfp[c] = pd.to_numeric(dfp[c], errors="coerce")
    dfp = dfp.dropna(subset=["pid", "cpu_ms", "dt_ms", "ts_ms", "cpu_freq_mhz"])
    dfp = dfp[dfp["dt_ms"] > 0]
    dfp["pid"] = dfp["pid"].astype("int64")
    dfp["cores_used"] = dfp["cpu_ms"] / dfp["dt_ms"]
    dfp["cpu_usage_mhz"] = dfp["cores_used"] * dfp["cpu_freq_mhz"]

    dff = pd.read_parquet(fragments)
    ts_col = detect_frag_ts_col(dff, fragments_ts_col)
    id_col = detect_id_col(dff, fragments_id_col)
    dff = dff[[id_col, ts_col, "cpu_usage"]].copy()
    dff[id_col] = pd.to_numeric(dff[id_col], errors="coerce")
    dff[ts_col] = pd.to_numeric(dff[ts_col], errors="coerce")
    dff["cpu_usage"] = pd.to_numeric(dff["cpu_usage"], errors="coerce")
    dff = dff.dropna(subset=[id_col, ts_col, "cpu_usage"]).copy()
    dff[id_col] = dff[id_col].astype("int64")
    # normalize fragment timestamp to ms (optional scale+offset if needed)
    dff["frag_ts_ms"] = (dff[ts_col].astype(float) * float(fragments_ts_scale)) + float(fragments_ts_offset_ms)

    merged_parts = []
    common = sorted(set(dfp["pid"].unique()).intersection(set(dff[id_col].unique())))
    for pid in common:
        lf = dfp[dfp["pid"] == pid][["pid", "ts_ms", "cpu_usage_mhz"]].sort_values("ts_ms")
        rf = dff[dff[id_col] == pid][[id_col, "frag_ts_ms", "cpu_usage"]].sort_values("frag_ts_ms")
        if lf.empty or rf.empty: continue
        part = pd.merge_asof(lf, rf, left_on="ts_ms", right_on="frag_ts_ms", direction="nearest", allow_exact_matches=True)
        part["pid"] = int(pid)
        part = part[["pid", "ts_ms", "frag_ts_ms", "cpu_usage_mhz", "cpu_usage"]]
        merged_parts.append(part)
    merged = pd.concat(merged_parts, ignore_index=True) if merged_parts else pd.DataFrame()
    merged = merged.dropna(subset=["cpu_usage_mhz", "cpu_usage"]).copy()
    if merged.empty:
        print("[CPU] No matched pairs after nearest alignment; skip plot.")
        return
    merged["delta_ms"] = (merged["ts_ms"] - merged["frag_ts_ms"]).abs()
    merged = merged[merged["delta_ms"] <= int(max_align_delta_ms)].copy()
    if merged.empty:
        print("[CPU] All pairs dropped by |Δt| filter; skip plot.")
        return
    x = merged["cpu_usage_mhz"].astype(float).to_numpy()
    y = merged["cpu_usage"].astype(float).to_numpy()
    # Unified axes: start from 0 and use common max for y=x
    x = x[np.isfinite(x)]; y = y[np.isfinite(y)]
    maxv = float(np.nanmax([np.nanmax(x), np.nanmax(y)])) if x.size and y.size else 1.0
    plt.figure(figsize=(6,6))
    plt.scatter(x, y, s=4, alpha=0.3, label="Aligned pairs")
    plt.plot([0.0, maxv], [0.0, maxv], 'r--', label='y = x (identity)')
    plt.xlim(0.0, maxv)
    plt.ylim(0.0, maxv)
    ax = plt.gca()
    try:
        ax.set_aspect('equal', adjustable='box')
        ax.margins(x=0, y=0)
    except Exception:
        pass
    plt.xlabel("Physical CPU Usage [MHz]")
    plt.ylabel("Simulated CPU Usage [MHz]")
    plt.grid(True, alpha=0.3); plt.legend(); plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150); plt.close()


# Alternative: plot CPU scatter from pre-matched pairs CSV (exported by compare_latency_throughput.py)

def plot_cpu_scatter_from_pairs(pairs_csv: Path, out_path: Path) -> None:
    try:
        df = pd.read_csv(pairs_csv)
    except Exception as e:
        print(f"[CPU] Failed to read pairs CSV: {e}")
        return
    if not {"cpu_usage_mhz", "cpu_usage"}.issubset(df.columns):
        print(f"[CPU] Pairs CSV missing required columns in {pairs_csv}; need ['cpu_usage_mhz','cpu_usage']")
        return
    x = pd.to_numeric(df["cpu_usage_mhz"], errors="coerce").to_numpy()
    y = pd.to_numeric(df["cpu_usage"], errors="coerce").to_numpy()
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]; y = y[mask]
    if x.size == 0:
        print("[CPU] No valid points in pairs CSV; skip plot.")
        return
    # Unified axes: start from 0 and use common max for y=x
    ref_max = float(np.nanmax([np.nanmax(x), np.nanmax(y)])) if x.size and y.size else 1.0
    plt.figure(figsize=(6,6))
    plt.scatter(x, y, s=4, alpha=0.3, label="Aligned pairs")
    plt.plot([0.0, ref_max], [0.0, ref_max], 'r--', label='y = x (identity)')
    plt.xlim(0.0, ref_max)
    plt.ylim(0.0, ref_max)
    ax = plt.gca()
    try:
        ax.set_aspect('equal', adjustable='box')
        ax.margins(x=0, y=0)
    except Exception:
        pass
    plt.xlabel("Physical CPU Usage [MHz]")
    plt.ylabel("Simulated CPU Usage [MHz]")
    plt.grid(True, alpha=0.3); plt.legend(); plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150); plt.close()

# ---------- Plot 4: Total Power Draw Trend (Z-score) ----------

def _zscore(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    m = s.mean(); sd = s.std(ddof=0)
    if pd.isna(sd) or sd == 0: return pd.Series([0.0] * len(s), index=s.index)
    return (s - m) / sd

def _detect_ts_col_power(df: pd.DataFrame, override: Optional[str] = None) -> str:
    if override is not None:
        if override not in df.columns: raise SystemExit(f"Timestamp column '{override}' not in powerSource.parquet: {list(df.columns)}")
        return override
    for c in ["ts", "timestamp", "time", "timestamp_ms", "time_ms", "timestamp_absolute"]:
        if c in df.columns: return c
    raise SystemExit("Cannot find a timestamp column in powerSource.parquet; pass --power-source-ts-col.")

def _to_second_int(ts: pd.Series) -> pd.Series:
    s = pd.to_numeric(ts, errors="coerce")
    if s.max(skipna=True) and float(s.max()) > 1e11:
        return (s / 1000.0).round().astype("Int64")
    return s.round().astype("Int64")

def plot_power_trend(power_total_csv: Path, power_source_parquet: Path, out_path: Path, power_source_ts_col: Optional[str] = None, use_best_lag: bool = True, max_lag_sec: int = 600, x_units: str = "minutes"):
    dft = pd.read_csv(power_total_csv)
    if not {"ts", "total_power_avg_w"}.issubset(dft.columns):
        print(f"[Power] CSV missing columns in {power_total_csv}"); return
    dft = dft[["ts", "total_power_avg_w"]].copy()
    dft["ts"] = pd.to_numeric(dft["ts"], errors="coerce").astype("Int64")
    dft["total_power_avg_w"] = pd.to_numeric(dft["total_power_avg_w"], errors="coerce")
    dft = dft.dropna(subset=["ts", "total_power_avg_w"])  # keep Int64 ts

    dfs = pd.read_parquet(power_source_parquet)
    ts_col = _detect_ts_col_power(dfs, power_source_ts_col)
    power_candidates = ["power_draw", "power", "power_w", "total_power", "power_value", "value", "power_draw_w"]
    power_col = next((c for c in power_candidates if c in dfs.columns), None)
    if power_col is None:
        print(f"[Power] No suitable power column in powerSource.parquet. Tried: {power_candidates}"); return
    dfs = dfs[[ts_col, power_col]].copy()
    dfs["ts"] = _to_second_int(dfs[ts_col])
    dfs[power_col] = pd.to_numeric(dfs[power_col], errors="coerce")
    dfs = dfs.dropna(subset=["ts", power_col])
    dfs_agg = dfs.groupby("ts", dropna=True, as_index=False)[power_col].mean().rename(columns={power_col: "power_draw"})

    # z-score series
    tot = dft[["ts"]].copy(); tot["z_total"] = _zscore(dft.set_index("ts")["total_power_avg_w"]).reset_index(drop=False)["total_power_avg_w"]
    src = dfs_agg[["ts"]].copy(); src["z_source"] = _zscore(dfs_agg.set_index("ts")["power_draw"]).reset_index(drop=False)["power_draw"]

    lag = 0
    if use_best_lag:
        best = {"lag": 0, "r": -1.0, "n": 0}
        for L in range(-int(max_lag_sec), int(max_lag_sec)+1):
            shifted = src.copy(); shifted["ts"] = shifted["ts"] - L
            m = pd.merge(tot, shifted, on="ts", how="inner").dropna(subset=["z_total", "z_source"])
            if len(m) < 5: continue
            r = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source"]).astype(float), method="pearson"))
            if not math.isnan(r) and r > best["r"]: best = {"lag": L, "r": r, "n": int(len(m))}
        lag = int(best["lag"]) if best["n"] > 0 else 0

    shifted = src.copy(); shifted["ts"] = shifted["ts"] - lag
    merged = pd.merge(tot, shifted, on="ts", how="inner").dropna(subset=["z_total", "z_source"]).sort_values("ts")
    if merged.empty:
        print("[Power] No overlapping seconds after alignment; skip plot.")
        return

    t = merged["ts"].astype(float).to_numpy()
    if x_units == "minutes": x = (t - t.min()) / 60.0; xlabel = "Time [min]"
    else: x = (t - t.min()); xlabel = "Time [s]"
    # Two-panel layout: top for z-trend, bottom for residual (OpenDC - Continuum)
    fig, (ax, axr) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.05})
    # Physical (Continuum): thick grey solid
    y_src = merged["z_source"].astype(float).to_numpy()
    ax.plot(x, y_src, label=f"Physical (Continuum) z, lag={lag}s", color='grey', linewidth=3, zorder=2)
    # Simulation (OpenDC): thin blue dashed
    y_tot = merged["z_total"].astype(float).to_numpy()
    ax.plot(x, y_tot, label="Simulation (OpenDC) z", color='#1f77b4', linestyle='--', linewidth=1.5, zorder=3)
    ax.set_ylabel("Normalized Total Power (Z-score)")
    ax.grid(True, alpha=0.3); ax.legend()
    # Ensure x starts at 0 and touches left axis
    try:
        ax.set_xlim(left=0)
        ax.margins(x=0)
    except Exception:
        pass
    # Residual panel
    resid = y_tot - y_src
    axr.axhline(0.0, color='black', linewidth=1)
    axr.plot(x, resid, color='#ff7f0e', linewidth=1.0)
    # Optional shading to emphasize sign
    try:
        axr.fill_between(x, 0, resid, where=(resid>=0), color='#1f77b4', alpha=0.12, step=None)
        axr.fill_between(x, 0, resid, where=(resid<0), color='grey', alpha=0.12, step=None)
    except Exception:
        pass
    axr.set_ylabel("Δ z")
    axr.grid(True, alpha=0.3)
    axr.set_xlabel(xlabel)
    try:
        axr.set_xlim(left=0)
        axr.margins(x=0)
    except Exception:
        pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(); fig.savefig(out_path, dpi=150); plt.close(fig)

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Generate paper plots (5.2.1–5.2.4)")
    ap.add_argument("--task-parquet", type=Path, required=True)
    ap.add_argument("--invocations", type=Path, required=True)
    ap.add_argument("--proc-metrics", type=Path, required=True)
    ap.add_argument("--fragments", type=Path, required=False, help="Optional. If omitted, provide --cpu-pairs-csv or use --skip-cpu-scatter.")
    ap.add_argument("--power-total", type=Path, required=True, help="power_total_corrected.csv from reconstruct_power.py")
    ap.add_argument("--power-source", type=Path, required=True, help="powerSource.parquet from OpenDC output")
    ap.add_argument("--out-dir", type=Path, default=Path("consistency_verification/out_plots"))
    ap.add_argument("--bin-ms", type=int, default=MINUTE_MS)
    ap.add_argument("--x-units", choices=["seconds", "minutes"], default="seconds", help="X units for time series plots")
    # CPU alignment options
    ap.add_argument("--max-align-delta-ms", type=int, default=500)
    ap.add_argument("--fragments-ts-col", type=str, default=None)
    ap.add_argument("--fragments-id-col", type=str, default=None)
    ap.add_argument("--fragments-ts-scale", type=float, default=1.0, help="Scale factor to convert fragments ts to ms if needed")
    ap.add_argument("--fragments-ts-offset-ms", type=float, default=0.0, help="Offset (ms) to add to fragments ts")
    # Power options
    ap.add_argument("--power-source-ts-col", type=str, default=None)
    ap.add_argument("--power-use-best-lag", action="store_true")
    ap.add_argument("--power-max-lag-sec", type=int, default=600)
    # Optional CPU scatter fallbacks/controls
    ap.add_argument("--cpu-pairs-csv", type=Path, default=None, help="CSV exported by compare_latency_throughput.py --export-proc-task")
    ap.add_argument("--skip-cpu-scatter", action="store_true")
    args = ap.parse_args()

    # Load core data
    o_subs, o_scheds, o_fins = load_opendc_from_parquet(args.task_parquet)
    c_waits, c_subs, c_fins = load_invocation_waits_and_fins(args.invocations)

    # 1) Wait CDF
    plot_wait_cdf(o_subs, o_scheds, c_waits, args.out_dir / "wait_cdf.png")

    # 2) Cumulative completion
    plot_cumulative(o_subs, o_fins, c_subs, c_fins, args.bin_ms, args.out_dir / "cumulative_completion.png", x_units=args.x_units)

    # 3) CPU scatter
    cpu_out = args.out_dir / "cpu_scatter.png"
    if args.skip_cpu_scatter:
        print("[CPU] Skipped by --skip-cpu-scatter")
    elif args.cpu_pairs_csv is not None and Path(args.cpu_pairs_csv).exists():
        plot_cpu_scatter_from_pairs(args.cpu_pairs_csv, cpu_out)
    else:
        try:
            plot_cpu_scatter(
                args.proc_metrics, args.fragments, cpu_out,
                max_align_delta_ms=args.max_align_delta_ms,
                fragments_ts_col=args.fragments_ts_col,
                fragments_id_col=args.fragments_id_col,
                fragments_ts_scale=args.fragments_ts_scale,
                fragments_ts_offset_ms=args.fragments_ts_offset_ms,
            )
        except SystemExit as e:
            print(f"[CPU] {e}. Provide --fragments-ts-col, or run compare_latency_throughput.py with --export-proc-task and pass --cpu-pairs-csv, or use --skip-cpu-scatter.")

    # 4) Power trend (z)
    plot_power_trend(
        args.power_total, args.power_source, args.out_dir / "power_trend_z.png",
        power_source_ts_col=args.power_source_ts_col,
        use_best_lag=args.power_use_best_lag,
        max_lag_sec=args.power_max_lag_sec,
        x_units=args.x_units,
    )

    print(f"Saved plots to: {args.out_dir}")

if __name__ == "__main__":
    main()

