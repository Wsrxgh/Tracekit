#!/usr/bin/env python3
"""
Reconstruct per-second average power from cumulative energy counters in Continuum power CSVs.

Input CSV columns (example):
  ts,cloud0_gxie_energy_uj,cloud0_gxie_power_w
- ts: second-level timestamp (integer seconds, epoch or relative)
- *_energy_uj: cumulative energy in microjoules (monotonic non-decreasing)
- *_power_w: provided "instantaneous" power, often zero on non-update seconds; DO NOT trust as-is

We compute corrected average power over each interval between consecutive energy updates:
  P_avg[ (t_{k-1}, t_k] ) = (E_k - E_{k-1}) / 1e6 / (t_k - t_{k-1})  (Watts)
Then we fill that power for every second s in (t_{k-1}, t_k]. This avoids spikes and zeros.

Usage examples:
  python reconstruct_power.py --dir 20250904001/power
  python reconstruct_power.py --file 20250904001/power/power_monitoring_cloud0_gxie.csv

Outputs new CSVs next to inputs with suffix: *_corrected.csv with columns:
  ts, energy_uj, power_w_orig, power_avg_w
and guarantees that sum(power_avg_w over seconds) * 1s ~= final_energy - initial_energy (in Joules).
"""

from __future__ import annotations
import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional

import pandas as pd  # for reading Parquet and computing trend metrics
import math

import matplotlib.pyplot as plt

MICRO = 1_000_000.0


@dataclass
class Sample:
    ts: int  # seconds
    energy_uj: int
    power_w_orig: float


def read_power_csv(path: Path) -> Tuple[str, List[Sample]]:
    """Read CSV and return (prefix, samples). Prefix is the common column prefix, e.g., 'cloud0_gxie'."""
    samples: List[Sample] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        header = next(r)
        if len(header) < 3:
            raise SystemExit(f"Unexpected header in {path}: {header}")
        ts_col, energy_col, power_col = header[0], header[1], header[2]
        # Infer prefix from energy column like '<prefix>_energy_uj'
        if not energy_col.endswith("_energy_uj"):
            raise SystemExit(f"Second column should end with '_energy_uj', got '{energy_col}'")
        prefix = energy_col[: -len("_energy_uj")]
        for row in r:
            if not row:
                continue
            try:
                ts = int(row[0])
                e_uj = int(float(row[1]))
                p_w = float(row[2])
            except Exception:
                continue
            samples.append(Sample(ts=ts, energy_uj=e_uj, power_w_orig=p_w))
    # Ensure sorted by ts
    samples.sort(key=lambda s: s.ts)
    return prefix, samples


def reconstruct_power(samples: List[Sample]) -> List[Tuple[int, int, float, float]]:
    """Return rows: (ts, energy_uj, power_w_orig, power_avg_w)
    power_avg_w is the interval-average power distributed uniformly across all seconds
    between two consecutive energy counter CHANGES (not just consecutive samples).
    """
    if not samples:
        return []
    rows: List[Tuple[int, int, float, float]] = []

    # Initialize with the first observed point
    first = samples[0]
    rows.append((first.ts, first.energy_uj, first.power_w_orig, 0.0))
    last_change_ts = first.ts
    last_change_energy = first.energy_uj
    last_p_avg = 0.0

    for cur in samples[1:]:
        if cur.ts <= last_change_ts:
            # non-increasing time; reset the anchor
            last_change_ts = cur.ts
            last_change_energy = cur.energy_uj
            last_p_avg = 0.0
            rows.append((cur.ts, cur.energy_uj, 0.0, 0.0))
            continue

        if cur.energy_uj == last_change_energy:
            # energy unchanged; defer until we see a change to back-fill this interval
            continue

        # energy changed at cur.ts; compute average over (last_change_ts, cur.ts]
        dt = cur.ts - last_change_ts
        dE_uj = cur.energy_uj - last_change_energy
        if dE_uj < 0:
            # counter reset/anomaly: fill zeros for safety
            for s in range(last_change_ts + 1, cur.ts + 1):
                rows.append((s, last_change_energy if s < cur.ts else cur.energy_uj, 0.0, 0.0))
            last_change_ts = cur.ts
            last_change_energy = cur.energy_uj
            last_p_avg = 0.0
            continue

        p_avg_w = (dE_uj / MICRO) / dt  # W
        # Back-fill every second from (last_change_ts, cur.ts]
        for s in range(last_change_ts + 1, cur.ts):
            rows.append((s, last_change_energy, 0.0, p_avg_w))
        rows.append((cur.ts, cur.energy_uj, 0.0, p_avg_w))

        last_change_ts = cur.ts
        last_change_energy = cur.energy_uj
        last_p_avg = p_avg_w

    # If there are trailing samples with no further energy change, append them with last known energy and 0 power
    last_sample_ts = samples[-1].ts
    if last_sample_ts > last_change_ts:
        for s in range(last_change_ts + 1, last_sample_ts + 1):
            rows.append((s, last_change_energy, 0.0, 0.0))

    # Ensure rows are sorted and deduplicated by ts
    rows.sort(key=lambda r: r[0])
    dedup = []
    seen = set()
    for r in rows:
        if r[0] in seen:
            continue
        dedup.append(r)
        seen.add(r[0])
    return dedup

# ---- Trend comparison against powerSource.parquet ----

def _detect_ts_col(df: pd.DataFrame, override: Optional[str] = None) -> str:
    if override is not None:
        if override not in df.columns:
            raise SystemExit(f"Timestamp column '{override}' not found in Parquet columns: {list(df.columns)}")
        return override
    # Prefer absolute/epoch timestamps first
    preferred = ["timestamp_absolute", "ts_absolute", "timestamp_epoch", "ts_epoch",
                 "timestamp_ms", "time_ms", "ts_ms", "ts", "timestamp", "time"]
    for c in preferred:
        if c in df.columns:
            return c
    raise SystemExit("Cannot find a timestamp column in powerSource.parquet; pass --source-ts-col")


def _to_second_int(ts_series: pd.Series) -> pd.Series:
    s = pd.to_numeric(ts_series, errors="coerce")
    try:
        mx = float(s.max(skipna=True))
    except Exception:
        mx = float('nan')
    if mx == mx and mx > 1e11:
        # milliseconds -> seconds
        return (s / 1000.0).round().astype("int64")
    return s.round().astype("int64")


def _zscore(x: pd.Series) -> pd.Series:
    x = pd.to_numeric(x, errors="coerce")
    m = x.mean()
    sd = x.std(ddof=0)
    if pd.isna(sd) or sd == 0:
        return pd.Series([0.0] * len(x), index=x.index)
    return (x - m) / sd


def compare_trend(total_csv: Path, source_parquet: Path, source_ts_col: Optional[str], export_csv: Optional[Path], max_lag_sec: int = 600, export_stats: Optional[Path] = None) -> Optional[dict]:
    if not total_csv.exists():
        print(f"[Trend] Total corrected CSV not found: {total_csv}")
        return None
    if not source_parquet.exists():
        print(f"[Trend] powerSource.parquet not found: {source_parquet}")
        return None
    # Load total per-second power
    dft = pd.read_csv(total_csv)
    if not {"ts", "total_power_avg_w"}.issubset(dft.columns):
        print(f"[Trend] Unexpected columns in {total_csv}")
        return
    dft = dft[["ts", "total_power_avg_w"]].copy()
    dft["ts"] = pd.to_numeric(dft["ts"], errors="coerce").astype("Int64")
    dft["total_power_avg_w"] = pd.to_numeric(dft["total_power_avg_w"], errors="coerce")
    dft = dft.dropna(subset=["ts", "total_power_avg_w"])  # keep Int64

    # Load source parquet
    dfs = pd.read_parquet(source_parquet)
    ts_col = _detect_ts_col(dfs, source_ts_col)
    # Detect power column name flexibly
    power_candidates = [
        "power_draw", "power", "power_w", "total_power", "power_value", "value", "power_draw_w"
    ]
    power_col = None
    for c in power_candidates:
        if c in dfs.columns:
            power_col = c
            break
    if power_col is None:
        print(f"[Trend] No suitable power column found in powerSource.parquet. Tried: {power_candidates}")
        return
    dfs = dfs[[ts_col, power_col]].copy()
    dfs["ts"] = _to_second_int(dfs[ts_col])
    dfs[power_col] = pd.to_numeric(dfs[power_col], errors="coerce")
    dfs = dfs.dropna(subset=["ts", power_col])  # keep Int64 ts
    # Aggregate to per-second mean
    dfs_agg = dfs.groupby("ts", dropna=True, as_index=False)[power_col].mean().rename(columns={power_col: "power_draw"})

    # Align on intersection of seconds (zero-lag)
    merged0 = pd.merge(dft, dfs_agg, on="ts", how="inner")
    if merged0.empty:
        print("[Trend] No overlapping seconds between total corrected and powerSource; skipping.")
        return None

    # Normalize zero-lag
    merged0["z_total"] = _zscore(merged0["total_power_avg_w"]).astype(float)
    merged0["z_source"] = _zscore(merged0["power_draw"]).astype(float)
    pearson_r0 = float(pd.Series(merged0["z_total"]).corr(pd.Series(merged0["z_source"]).astype(float), method="pearson"))
    spearman_r0 = float(pd.Series(merged0["z_total"]).corr(pd.Series(merged0["z_source"]).astype(float), method="spearman"))

    # Lag search to maximize Pearson correlation on z-scored series
    tot = dft[["ts"]].copy()
    tot["z_total"] = _zscore(dft.set_index("ts")["total_power_avg_w"]).reset_index(drop=False)["total_power_avg_w"]
    src = dfs_agg[["ts"]].copy()
    src["z_source"] = _zscore(dfs_agg.set_index("ts")["power_draw"]).reset_index(drop=False)["power_draw"]

    best = {"lag": 0, "r": -1.0, "n": 0}
    best_spear = None
    best_diff_r = None
    for lag in range(-int(max_lag_sec), int(max_lag_sec) + 1):
        shifted = src.copy()
        shifted["ts"] = shifted["ts"] - lag  # align source at t+lag with total at t
        m = pd.merge(tot, shifted, on="ts", how="inner").dropna(subset=["z_total", "z_source"])
        if len(m) < 5:
            continue
        r = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source"]).astype(float), method="pearson"))
        if math.isnan(r):
            continue
        if r > best["r"]:
            best = {"lag": lag, "r": r, "n": int(len(m))}
            best_spear = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source"]).astype(float), method="spearman"))
            dz_tot = m["z_total"].diff().dropna()
            dz_src = m["z_source"].diff().dropna()
            min_len = min(len(dz_tot), len(dz_src))
            best_diff_r = float(pd.Series(dz_tot.iloc[-min_len:]).corr(pd.Series(dz_src.iloc[-min_len:]), method="pearson")) if min_len > 1 else float("nan")
            merged_best = m  # keep for export

    print("\n=== Power Trend Comparison (total_corrected vs powerSource.parquet) ===")
    print(f"Zero-lag aligned seconds: {len(merged0)}  Pearson r0: {pearson_r0:.4f}  Spearman r0: {spearman_r0:.4f}")
    if best["n"] > 0:
        print(f"Best-lag within ±{max_lag_sec}s: lag={best['lag']}s  n={best['n']}  r={best['r']:.4f}  spearman={best_spear:.4f}  diff_r={best_diff_r:.4f}")
    else:
        print("Best-lag search found no overlap.")

    # Export aligned CSV at best lag (preferred), otherwise zero-lag
    if export_csv is not None:
        export_csv.parent.mkdir(parents=True, exist_ok=True)
        if best["n"] > 0:
            out_df = merged_best[["ts", "z_total", "z_source"]].copy()
            out_df["lag_sec"] = best["lag"]
            # Attach original-scale values for reference by re-merging
            out_df = out_df.merge(merged0[["ts", "total_power_avg_w"]], on="ts", how="left")
            out_df = out_df.merge(dfs_agg[["ts", "power_draw"]], on="ts", how="left")
        else:
            out_df = merged0[["ts", "z_total", "z_source", "total_power_avg_w", "power_draw"]].copy()
            out_df["lag_sec"] = 0
        out_cols = ["ts", "total_power_avg_w", "power_draw", "z_total", "z_source", "lag_sec"]
        out_df[out_cols].to_csv(export_csv, index=False)
        print(f"[Trend] Exported aligned trend CSV to: {export_csv}")

    if export_stats is not None:
        export_stats.parent.mkdir(parents=True, exist_ok=True)
        with export_stats.open("w", encoding="utf-8") as f:
            f.write(f"zero_lag_seconds,{len(merged0)}\n")
            f.write(f"pearson_r0,{pearson_r0:.6f}\n")
            f.write(f"spearman_r0,{spearman_r0:.6f}\n")
            if best["n"] > 0:
                f.write(f"best_lag_sec,{best['lag']}\n")
                f.write(f"best_lag_n,{best['n']}\n")
                f.write(f"best_lag_pearson_r,{best['r']:.6f}\n")
                f.write(f"best_lag_spearman_r,{best_spear:.6f}\n")
                f.write(f"best_lag_diff_pearson_r,{best_diff_r:.6f}\n")
            else:
                f.write("best_lag_sec,\n")
                f.write("best_lag_n,0\n")
                f.write("best_lag_pearson_r,\n")
                f.write("best_lag_spearman_r,\n")
                f.write("best_lag_diff_pearson_r,\n")
    # Build metrics dict to return
    metrics = {
        "zero_lag_seconds": int(len(merged0)),
        "pearson_r0": float(pearson_r0),
        "spearman_r0": float(spearman_r0),
        "best_lag_sec": int(best["lag"]) if best["n"] > 0 else None,
        "best_lag_n": int(best["n"]),
        "best_lag_pearson_r": float(best["r"]) if best["n"] > 0 else None,
        "best_lag_spearman_r": float(best_spear) if best["n"] > 0 else None,
        "best_lag_diff_pearson_r": float(best_diff_r) if best["n"] > 0 else None,
        "export_csv": str(export_csv) if export_csv is not None else None,
        "export_stats": str(export_stats) if export_stats is not None else None,
    }
    return metrics


def compare_trend_advanced(total_csv: Path,
                            source_parquet: Path,
                            source_ts_col: Optional[str],
                            export_csv: Optional[Path],
                            max_lag_sec: int = 600,
                            export_stats: Optional[Path] = None,
                            invocations_path: Optional[Path] = None,
                            activity_padding_sec: int = 0,
                            segment_sec: int = 0,
                            smooth_sec: int = 0) -> Optional[dict]:
    """Enhanced compare_trend with optional active-window restriction, smoothing and segmented best-lag correlation."""
    if not total_csv.exists():
        print(f"[Trend] Total corrected CSV not found: {total_csv}")
        return None
    if not source_parquet.exists():
        print(f"[Trend] powerSource.parquet not found: {source_parquet}")
        return None

    # Load total per-second power
    dft = pd.read_csv(total_csv)
    if not {"ts", "total_power_avg_w"}.issubset(dft.columns):
        print(f"[Trend] Unexpected columns in {total_csv}")
        return None
    dft = dft[["ts", "total_power_avg_w"]].copy()
    dft["ts"] = pd.to_numeric(dft["ts"], errors="coerce").astype("int64")
    dft["total_power_avg_w"] = pd.to_numeric(dft["total_power_avg_w"], errors="coerce")
    dft = dft.dropna(subset=["ts", "total_power_avg_w"])  # keep Int64

    # Load source parquet
    dfs = pd.read_parquet(source_parquet)
    ts_col = _detect_ts_col(dfs, source_ts_col)
    # Detect power column name flexibly
    power_candidates = [
        "power_draw", "power", "power_w", "total_power", "power_value", "value", "power_draw_w"
    ]
    power_col = None
    for c in power_candidates:
        if c in dfs.columns:
            power_col = c
            break
    if power_col is None:
        print(f"[Trend] No suitable power column found in powerSource.parquet. Tried: {power_candidates}")
        return None
    dfs = dfs[[ts_col, power_col]].copy()
    dfs["ts"] = _to_second_int(dfs[ts_col])
    dfs[power_col] = pd.to_numeric(dfs[power_col], errors="coerce")
    dfs = dfs.dropna(subset=["ts", power_col])  # keep Int64 ts

    # Aggregate to per-second mean
    dfs_agg = dfs.groupby("ts", dropna=True, as_index=False)[power_col].mean().rename(columns={power_col: "power_draw"})

    # Ensure integer ts type for safe merges
    dft["ts"] = dft["ts"].astype("int64")
    dfs_agg["ts"] = dfs_agg["ts"].astype("int64")

    # Optional: restrict to active window from invocations
    active_window = None
    if invocations_path is not None and Path(invocations_path).exists():
        try:
            df_inv = pd.read_json(invocations_path, lines=True)
            t0 = pd.to_numeric(df_inv.get("ts_enqueue"), errors="coerce").dropna()
            t1 = pd.to_numeric(df_inv.get("ts_end"), errors="coerce").dropna()
            if not t0.empty and not t1.empty:
                start_s = float(t0.min()) / 1000.0
                end_s = float(t1.max()) / 1000.0
                start_s -= max(0, int(activity_padding_sec))
                end_s += max(0, int(activity_padding_sec))
                active_window = (int(start_s), int(end_s))
        except Exception as e:
            print(f"[Trend] Failed to read invocations for active window: {e}")
    if active_window is not None:
        lo, hi = active_window
        dft = dft[(dft["ts"] >= lo) & (dft["ts"] <= hi)].copy()
        dfs_agg = dfs_agg[(dfs_agg["ts"] >= lo) & (dfs_agg["ts"] <= hi)].copy()
        # ensure ts remains int64 after filtering
        dft["ts"] = dft["ts"].astype("int64")
        dfs_agg["ts"] = dfs_agg["ts"].astype("int64")

    # Optional: smoothing (moving average over seconds)
    if smooth_sec and int(smooth_sec) > 1:
        win = int(smooth_sec)
        dft = dft.sort_values("ts").copy()
        dfs_agg = dfs_agg.sort_values("ts").copy()
        dft["total_power_avg_w"] = (
            pd.Series(dft["total_power_avg_w"].values, index=dft["ts"].values)
            .rolling(window=win, center=True, min_periods=1).mean().values
        )
        dfs_agg["power_draw"] = (
            pd.Series(dfs_agg["power_draw"].values, index=dfs_agg["ts"].values)
            .rolling(window=win, center=True, min_periods=1).mean().values
        )

    # Align on intersection of absolute seconds (zero-lag)
    merged0 = pd.merge(dft, dfs_agg, on="ts", how="inner")
    if merged0.empty:
        print("[Trend] No overlapping seconds between total corrected and powerSource; skipping.")
        return None

    # Normalize zero-lag
    merged0["z_total"] = _zscore(merged0["total_power_avg_w"]).astype(float)
    merged0["z_source"] = _zscore(merged0["power_draw"]).astype(float)
    pearson_r0 = float(pd.Series(merged0["z_total"]).corr(pd.Series(merged0["z_source"]).astype(float), method="pearson"))
    spearman_r0 = float(pd.Series(merged0["z_total"]).corr(pd.Series(merged0["z_source"]).astype(float), method="spearman"))

    # Lag search to maximize Pearson correlation on z-scored series (global)
    tot = dft[["ts"]].copy()
    tot["z_total"] = _zscore(dft.set_index("ts")["total_power_avg_w"]).reset_index(drop=False)["total_power_avg_w"]
    src = dfs_agg[["ts"]].copy()
    src["z_source"] = _zscore(dfs_agg.set_index("ts")["power_draw"]).reset_index(drop=False)["power_draw"]

    best = {"lag": 0, "r": -1.0, "n": 0}
    best_spear = None
    best_diff_r = None
    merged_best = None
    for lag in range(-int(max_lag_sec), int(max_lag_sec) + 1):
        shifted = src.copy()
        shifted["ts"] = shifted["ts"] - lag  # align source at t+lag with total at t
        m = pd.merge(tot, shifted, on="ts", how="inner").dropna(subset=["z_total", "z_source"])
        if len(m) < 5:
            continue
        r = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source"]).astype(float), method="pearson"))
        if math.isnan(r):
            continue
        if r > best["r"]:
            best = {"lag": lag, "r": r, "n": int(len(m))}
            best_spear = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source"]).astype(float), method="spearman"))
            dz_tot = m["z_total"].diff().dropna()
            dz_src = m["z_source"].diff().dropna()
            min_len = min(len(dz_tot), len(dz_src))
            best_diff_r = float(pd.Series(dz_tot.iloc[-min_len:]).corr(pd.Series(dz_src.iloc[-min_len:]), method="pearson")) if min_len > 1 else float("nan")
            merged_best = m  # keep for export

    # Optional: segmented correlation with per-segment best-lag
    seg_mean_r = None
    seg_min_r = None
    seg_std_r = None
    if segment_sec and int(segment_sec) > 0:
        seg = int(segment_sec)
        # Use zero-lag aligned z series for time bounds
        zs = pd.merge(tot, src, on="ts", how="inner").dropna(subset=["z_total", "z_source"]).sort_values("ts")
        if not zs.empty:
            tmin, tmax = int(zs["ts"].min()), int(zs["ts"].max())
            rs = []
            step = seg
            for s in range(tmin, tmax + 1, step):
                e = s + seg - 1
                sub_tot = tot[(tot["ts"] >= s) & (tot["ts"] <= e)].copy()
                sub_src = src[(src["ts"] >= s) & (src["ts"] <= e)].copy()
                if len(sub_tot) < 5 or len(sub_src) < 5:
                    continue
                best_local = -1.0
                for lag in range(-int(max_lag_sec), int(max_lag_sec) + 1):
                    sh = sub_src.copy()
                    sh["ts"] = sh["ts"] - lag
                    mm = pd.merge(sub_tot, sh, on="ts", how="inner").dropna(subset=["z_total", "z_source"])
                    if len(mm) < 5:
                        continue
                    rloc = float(pd.Series(mm["z_total"]).corr(pd.Series(mm["z_source"]).astype(float), method="pearson"))
                    if math.isnan(rloc):
                        continue
                    if rloc > best_local:
                        best_local = rloc
                if best_local >= 0:
                    rs.append(best_local)
            if rs:
                seg_mean_r = float(pd.Series(rs).mean())
                seg_min_r = float(pd.Series(rs).min())
                seg_std_r = float(pd.Series(rs).std(ddof=0)) if len(rs) > 1 else 0.0

    print("\n=== Power Trend Comparison (total_corrected vs powerSource.parquet) [Advanced] ===")
    print(f"Zero-lag aligned seconds: {len(merged0)}  Pearson r0: {pearson_r0:.4f}  Spearman r0: {spearman_r0:.4f}")
    if best["n"] > 0:
        print(f"Best-lag within ±{max_lag_sec}s: lag={best['lag']}s  n={best['n']}  r={best['r']:.4f}  spearman={best_spear:.4f}  diff_r={best_diff_r:.4f}")
    else:
        print("Best-lag search found no overlap.")
    if seg_mean_r is not None:
        print(f"Segmented (size={segment_sec}s) best-lag Pearson r: mean={seg_mean_r:.4f}  min={seg_min_r:.4f}  std={seg_std_r:.4f}")

    # Export aligned CSV at best lag (preferred), otherwise zero-lag
    if export_csv is not None:
        export_csv.parent.mkdir(parents=True, exist_ok=True)
        if best["n"] > 0 and merged_best is not None:
            out_df = merged_best[["ts", "z_total", "z_source"]].copy()
            out_df["lag_sec"] = best["lag"]
            # Attach original-scale values for reference by re-merging on ts
            out_df = out_df.merge(dft[["ts", "total_power_avg_w"]], on="ts", how="left")
            out_df = out_df.merge(dfs_agg[["ts", "power_draw"]], on="ts", how="left")
        else:
            out_df = merged0[["ts", "z_total", "z_source", "total_power_avg_w", "power_draw"]].copy()
            out_df["lag_sec"] = 0
        out_cols = ["ts", "total_power_avg_w", "power_draw", "z_total", "z_source", "lag_sec"]
        out_df[out_cols].to_csv(export_csv, index=False)
        print(f"[Trend] Exported aligned trend CSV to: {export_csv}")

    if export_stats is not None:
        export_stats.parent.mkdir(parents=True, exist_ok=True)
        with export_stats.open("w", encoding="utf-8") as f:
            f.write(f"zero_lag_seconds,{len(merged0)}\n")
            f.write(f"pearson_r0,{pearson_r0:.6f}\n")
            f.write(f"spearman_r0,{spearman_r0:.6f}\n")
            if best["n"] > 0:
                f.write(f"best_lag_sec,{best['lag']}\n")
                f.write(f"best_lag_n,{best['n']}\n")
                f.write(f"best_lag_pearson_r,{best['r']:.6f}\n")
                f.write(f"best_lag_spearman_r,{best_spear:.6f}\n")
                f.write(f"best_lag_diff_pearson_r,{best_diff_r:.6f}\n")
            else:
                f.write("best_lag_sec,\n")
                f.write("best_lag_n,0\n")
                f.write("best_lag_pearson_r,\n")
                f.write("best_lag_spearman_r,\n")
                f.write("best_lag_diff_pearson_r,\n")
            if segment_sec and seg_mean_r is not None:
                f.write(f"segment_sec,{int(segment_sec)}\n")
                f.write(f"segment_mean_r,{seg_mean_r:.6f}\n")
                f.write(f"segment_min_r,{seg_min_r:.6f}\n")
                f.write(f"segment_std_r,{seg_std_r:.6f}\n")
            if active_window is not None:
                f.write(f"active_window_start,{active_window[0]}\n")
                f.write(f"active_window_end,{active_window[1]}\n")
                f.write(f"activity_padding_sec,{activity_padding_sec}\n")
            if smooth_sec and int(smooth_sec) > 1:
                f.write(f"smooth_sec,{int(smooth_sec)}\n")

    # Build metrics dict to return
    metrics = {
        "zero_lag_seconds": int(len(merged0)),
        "pearson_r0": float(pearson_r0),
        "spearman_r0": float(spearman_r0),
        "best_lag_sec": int(best["lag"]) if best["n"] > 0 else None,
        "best_lag_n": int(best["n"]),
        "best_lag_pearson_r": float(best["r"]) if best["n"] > 0 else None,
        "best_lag_spearman_r": float(best_spear) if best["n"] > 0 else None,
        "best_lag_diff_pearson_r": float(best_diff_r) if best["n"] > 0 else None,
        "segment_sec": int(segment_sec) if segment_sec else None,
        "segment_mean_r": float(seg_mean_r) if seg_mean_r is not None else None,
        "segment_min_r": float(seg_min_r) if seg_min_r is not None else None,
        "segment_std_r": float(seg_std_r) if seg_std_r is not None else None,
        "active_window": tuple(active_window) if active_window is not None else None,
        "smooth_sec": int(smooth_sec) if smooth_sec else None,
        "export_csv": str(export_csv) if export_csv is not None else None,
        "export_stats": str(export_stats) if export_stats is not None else None,
    }
    return metrics



def write_corrected_csv(out_path: Path, prefix: str, rows: List[Tuple[int, int, float, float]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", f"{prefix}_energy_uj", f"{prefix}_power_w_orig", f"{prefix}_power_avg_w"])
        for ts, e_uj, p_orig, p_avg in rows:
            w.writerow([ts, e_uj, p_orig, f"{p_avg:.6f}"])


def write_power_audit_report(path: Path, total_csv: Path, source_parquet: Path, m: dict, pass_threshold: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    best_r = m.get("best_lag_pearson_r")
    passed = (best_r is not None) and (best_r >= pass_threshold)
    # Alternative standards
    alt_pearson075 = (best_r is not None) and (best_r >= 0.75)
    alt_spear075 = (m.get("best_lag_spearman_r") is not None) and (m.get("best_lag_spearman_r") >= 0.75)
    alt_diff060 = (m.get("best_lag_diff_pearson_r") is not None) and (m.get("best_lag_diff_pearson_r") >= 0.60)
    seg_mean = m.get("segment_mean_r")
    alt_seg075 = (seg_mean is not None) and (seg_mean >= 0.75)
    with path.open("w", encoding="utf-8") as f:
        f.write("## Power Trend Audit\n\n")
        f.write(f"- Total corrected CSV: {total_csv}\n")
        f.write(f"- Source parquet: {source_parquet}\n")
        if m.get("active_window") is not None:
            aw = m.get("active_window")
            f.write(f"- Active window: [{aw[0]}, {aw[1]}] (padding={m.get('activity_padding_sec','')})\n")
        if m.get("smooth_sec"):
            f.write(f"- Smoothing (sec): {m.get('smooth_sec')}\n")
        if m.get("segment_sec"):
            f.write(f"- Segment size (sec): {m.get('segment_sec')}\n")
        f.write(f"- Zero-lag aligned seconds: {m.get('zero_lag_seconds', 0)}\n")
        f.write(f"- Zero-lag Pearson r: {m.get('pearson_r0')}\n")
        f.write(f"- Zero-lag Spearman r: {m.get('spearman_r0')}\n")
        f.write("\n")
        f.write(f"- Best lag (s): {m.get('best_lag_sec')}\n")
        f.write(f"- Best-lag aligned seconds: {m.get('best_lag_n')}\n")
        f.write(f"- Best-lag Pearson r: {m.get('best_lag_pearson_r')}\n")
        f.write(f"- Best-lag Spearman r: {m.get('best_lag_spearman_r')}\n")
        f.write(f"- Best-lag diff Pearson r: {m.get('best_lag_diff_pearson_r')}\n")
        if seg_mean is not None:
            f.write(f"- Segmented Pearson r (mean/min/std): {seg_mean} / {m.get('segment_min_r')} / {m.get('segment_std_r')}\n")
        f.write("\n")

        f.write(f"- Decision threshold (Pearson): {pass_threshold}\n")
        f.write(f"- Verdict: {'PASS' if passed else 'FAIL'}\n")
        f.write("\n")
        f.write("### Alternative Standards\n")
        f.write(f"- Pearson (best-lag) >= 0.75: {'PASS' if alt_pearson075 else 'FAIL'}\n")
        f.write(f"- Spearman (best-lag) >= 0.75: {'PASS' if alt_spear075 else 'FAIL'}\n")
        f.write(f"- Pearson of first-differences >= 0.60: {'PASS' if alt_diff060 else 'FAIL'}\n")
        if m.get("segment_sec"):
            f.write(f"- Segmented Pearson mean >= 0.75: {'PASS' if alt_seg075 else 'FAIL'}\n")
    print(f"Wrote power audit report: {path} (verdict: {'PASS' if passed else 'FAIL'})")


def write_power_audit_report_simple(path: Path, m: dict, global_threshold: float = 0.75, seg_threshold: float = 0.90) -> None:
    """Write a minimal audit including only Global Pearson (smoothed) and Segmented mean Pearson (smoothed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    global_r = m.get("best_lag_pearson_r")  # this is on smoothed series if smooth_sec>1
    seg_mean = m.get("segment_mean_r")
    with path.open("w", encoding="utf-8") as f:
        f.write("## Power Trend Audit (Simple)\n\n")
        if m.get("smooth_sec"):
            f.write(f"- Smoothing window: {m.get('smooth_sec')}s\n")
        if m.get("segment_sec"):
            f.write(f"- Segment size: {m.get('segment_sec')}s\n")
        f.write("\n")
        f.write(f"- Global Pearson (smoothed best-lag): {global_r}\n")
        f.write(f"- Segmented mean Pearson (smoothed): {seg_mean}\n")
        f.write("\n")
        f.write(f"- Thresholds: global>={global_threshold}, segmented_mean>={seg_threshold}\n")
        verdict = (global_r is not None and global_r >= global_threshold) and (seg_mean is not None and seg_mean >= seg_threshold)
        f.write(f"- Verdict: {'PASS' if verdict else 'FAIL'}\n")
    print(f"Wrote simple power audit report: {path}")


def plot_global_from_alignment(aligned_csv: Path, out_path: Path, title: str = "Power Trend (Global)") -> None:
    import numpy as np
    import matplotlib.pyplot as plt
    df = pd.read_csv(aligned_csv)
    if not {"ts", "total_power_avg_w", "power_draw"}.issubset(df.columns):
        print(f"[Plot] Missing required columns in {aligned_csv}")
        return
    df = df.sort_values("ts")
    x = pd.to_numeric(df["ts"], errors="coerce").to_numpy()
    y1 = pd.to_numeric(df["total_power_avg_w"], errors="coerce").to_numpy()
    y2 = pd.to_numeric(df["power_draw"], errors="coerce").to_numpy()
    m = np.isfinite(x) & np.isfinite(y1) & np.isfinite(y2)
    x, y1, y2 = x[m], y1[m], y2[m]
    if x.size == 0:
        print("[Plot] No valid points to plot global trend.")
        return
    fig, ax1 = plt.subplots(figsize=(10, 4))
    ax2 = ax1.twinx()
    ax1.plot(x, y1, color="tab:blue", label="total_power_avg_w")
    ax2.plot(x, y2, color="tab:orange", label="power_draw (source)")
    ax1.set_xlabel("ts (s)")
    ax1.set_ylabel("Total power (W)", color="tab:blue")
    ax2.set_ylabel("Source power (W)", color="tab:orange")
    ax1.set_title(title)
    ax1.grid(alpha=0.3, linestyle=":")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_segment_bars_from_alignment(aligned_csv: Path, out_path: Path, segment_sec: int, max_lag_sec: int = 600, title: str = "Segmented Pearson r (best-lag)") -> None:
    import numpy as np
    import matplotlib.pyplot as plt
    df = pd.read_csv(aligned_csv)
    req = {"ts", "z_total", "z_source"}
    if not req.issubset(df.columns):
        print(f"[Plot] Missing columns {req} in {aligned_csv}")
        return
    df = df.dropna(subset=["ts", "z_total", "z_source"]).sort_values("ts")
    if df.empty:
        print("[Plot] No data to plot segmented bars.")
        return
    tmin, tmax = int(df["ts"].min()), int(df["ts"].max())
    xs, rs = [], []
    for s in range(tmin, tmax + 1, int(segment_sec)):
        e = s + int(segment_sec) - 1
        sub = df[(df.ts >= s) & (df.ts <= e)].copy()
        if len(sub) < 5:
            continue
        best = -1.0
        for lag in range(-int(max_lag_sec), int(max_lag_sec) + 1):
            sub2 = sub.copy(); sub2["ts"] = sub2["ts"] - lag
            m = sub.merge(sub2[["ts", "z_source"]], on="ts", how="inner", suffixes=("", "_s"))
            if len(m) < 5:
                continue
            r = float(pd.Series(m["z_total"]).corr(pd.Series(m["z_source_s"]).astype(float), method="pearson"))
            if not math.isnan(r) and r > best:
                best = r
        if best >= 0:
            xs.append(s)
            rs.append(best)
    if not rs:
        print("[Plot] No segments produced.")
        return
    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.bar(range(len(rs)), rs, color="tab:green")
    ax.set_ylabel("Pearson r")
    ax.set_xlabel("Segment index")
    ax.set_ylim(0.0, 1.0)
    ax.set_title(title + f" (segment={segment_sec}s)")
    ax.grid(alpha=0.3, axis="y", linestyle=":")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[Plot] Wrote segmented bars: {out_path}")



def rows_to_power_map(rows: List[Tuple[int, int, float, float]]):
    """Build a map ts -> power_avg_w and return (power_map, min_ts, max_ts)."""
    power_map = {}
    if not rows:
        return power_map, None, None
    for ts, _e, _po, p_avg in rows:
        power_map[int(ts)] = float(p_avg)
    return power_map, int(rows[0][0]), int(rows[-1][0])


def write_total_corrected_csv(out_path: Path, series: List[Tuple[int, float, float]]) -> None:
    """Write aggregated total series: (ts, total_power_avg_w, total_energy_j)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "total_power_avg_w", "total_energy_j"])  # energy in Joules
        for ts, p_tot, e_tot in series:
            w.writerow([ts, f"{p_tot:.6f}", f"{e_tot:.6f}"])


def process_file(path: Path) -> Path:
    prefix, samples = read_power_csv(path)
    rows = reconstruct_power(samples)
    out_path = path.with_name(path.stem + "_corrected.csv")
    write_corrected_csv(out_path, prefix, rows)
    # quick energy check
    if samples:
        total_E_J = (samples[-1].energy_uj - samples[0].energy_uj) / MICRO
        total_from_power = sum(float(r[3]) for r in rows)  # W * 1s per row
        # Not printing here to keep output clean; could log if desired
    return out_path



# ---- Updated evaluation mode (merged from compute_power_trend_updated) ----

def _run_updated_evaluation(
    total_csv: Path,
    source_parquet: Path,
    source_ts_col: Optional[str],
    invocations: Optional[Path],
    out_dir: Path,
    smooth_sec: int = 60,
    segment_sec: int = 300,
    max_lag_sec: int = 600,
    activity_padding_sec: int = 60,
) -> None:
    """
    Produce the standardized trio of outputs that compute_power_trend_updated.py generated:
    - trend_alignment_updated.csv (smoothed best-lag alignment with z-scores)
    - trend_stats_updated.txt (raw + smoothed metrics, segmented stats)
    - power_audit_updated.md (with suggested standards & PASS/FAIL)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) RAW metrics (no smoothing, no segmentation for stats)
    m_raw = compare_trend_advanced(
        total_csv,
        source_parquet,

        source_ts_col,
        export_csv=None,
        max_lag_sec=max_lag_sec,
        export_stats=None,
        invocations_path=invocations,
        activity_padding_sec=activity_padding_sec,
        segment_sec=0,
        smooth_sec=0,
    )

    # 2) SMOOTHED metrics (produce alignment CSV and segmented stats)
    aligned_csv = out_dir / "trend_alignment_updated.csv"
    m_sm = compare_trend_advanced(
        total_csv,
        source_parquet,
        source_ts_col,
        export_csv=aligned_csv,
        max_lag_sec=max_lag_sec,
        export_stats=None,
        invocations_path=invocations,
        activity_padding_sec=activity_padding_sec,
        segment_sec=segment_sec,
        smooth_sec=smooth_sec,
    )

    # 3) Write combined stats file
    stats_path = out_dir / "trend_stats_updated.txt"
    with stats_path.open("w", encoding="utf-8") as f:
        f.write(f"raw_zero_lag_r,{m_raw['pearson_r0'] if m_raw else ''}\n")
        f.write(f"raw_best_lag_r,{m_raw['best_lag_pearson_r'] if m_raw else ''}\n")
        f.write(f"raw_best_lag,{m_raw['best_lag_sec'] if m_raw else ''}\n")
        f.write(f"raw_spearman0,{m_raw['spearman_r0'] if m_raw else ''}\n")
        f.write(f"raw_diff_r,{m_raw['best_lag_diff_pearson_r'] if m_raw else ''}\n")
        f.write(f"sm_zero_lag_r,{m_sm['pearson_r0'] if m_sm else ''}\n")
        f.write(f"sm_best_lag_r,{m_sm['best_lag_pearson_r'] if m_sm else ''}\n")
        f.write(f"sm_best_lag,{m_sm['best_lag_sec'] if m_sm else ''}\n")
        f.write(f"sm_spearman0,{m_sm['spearman_r0'] if m_sm else ''}\n")
        f.write(f"sm_diff_r,{m_sm['best_lag_diff_pearson_r'] if m_sm else ''}\n")
        # segmented on smoothed
        f.write(f"seg_mean_r,{m_sm['segment_mean_r'] if m_sm and m_sm.get('segment_mean_r') is not None else ''}\n")
        f.write(f"seg_min_r,{m_sm['segment_min_r'] if m_sm and m_sm.get('segment_min_r') is not None else ''}\n")
        f.write(f"seg_std_r,{m_sm['segment_std_r'] if m_sm and m_sm.get('segment_std_r') is not None else ''}\n")
        f.write(f"seg_n,{m_sm['best_lag_n'] if m_sm else ''}\n")

    # 4) Write concise audit with suggested standards (same thresholds as compute_power_trend_updated)
    audit_path = out_dir / "power_audit_updated.md"
    with audit_path.open("w", encoding="utf-8") as f:
        f.write("## Power Trend Audit (Updated)\n\n")
        f.write(f"- Raw zero-lag Pearson r: {m_raw['pearson_r0'] if m_raw else ''}\n")
        f.write(f"- Raw best-lag Pearson r: {m_raw['best_lag_pearson_r'] if m_raw else ''} (lag={m_raw['best_lag_sec'] if m_raw else ''})\n")
        f.write(f"- Raw Spearman r: {m_raw['spearman_r0'] if m_raw else ''}\n")
        f.write(f"- Raw first-diff Pearson r: {m_raw['best_lag_diff_pearson_r'] if m_raw else ''}\n")\

        f.write(f"- Smoothed zero-lag Pearson r: {m_sm['pearson_r0'] if m_sm else ''}\n")
        f.write(f"- Smoothed best-lag Pearson r: {m_sm['best_lag_pearson_r'] if m_sm else ''} (lag={m_sm['best_lag_sec'] if m_sm else ''})\n")
        f.write(f"- Smoothed Spearman r: {m_sm['spearman_r0'] if m_sm else ''}\n")
        f.write(f"- Smoothed first-diff Pearson r: {m_sm['best_lag_diff_pearson_r'] if m_sm else ''}\n")
        f.write(f"- Segmented (size={int(segment_sec)}s) best-lag Pearson r mean/min/std: {m_sm['segment_mean_r'] if m_sm else ''} / {m_sm['segment_min_r'] if m_sm else ''} / {m_sm['segment_std_r'] if m_sm else ''} (n={m_sm['best_lag_n'] if m_sm else ''})\n")
        f.write("\n### Suggested Standards\n")
        # thresholds: global pearson/spearman 0.75, diff 0.60, segmented mean 0.75
        sm_pass = (m_sm is not None and m_sm.get('best_lag_pearson_r') is not None and m_sm['best_lag_pearson_r'] >= 0.75)
        sm_spear_pass = (m_sm is not None and m_sm.get('best_lag_spearman_r') is not None and m_sm['best_lag_spearman_r'] >= 0.75)
        sm_diff_pass = (m_sm is not None and m_sm.get('best_lag_diff_pearson_r') is not None and m_sm['best_lag_diff_pearson_r'] >= 0.60)
        seg_mean = m_sm.get('segment_mean_r') if m_sm else None
        seg_pass = (seg_mean is not None) and (seg_mean >= 0.75)
        f.write(f"- Global Pearson (smoothed) >= 0.75: {'PASS' if sm_pass else 'FAIL'}\n")
        f.write(f"- Global Spearman (smoothed) >= 0.75: {'PASS' if sm_spear_pass else 'FAIL'}\n")
        f.write(f"- First-diff Pearson (smoothed) >= 0.60: {'PASS' if sm_diff_pass else 'FAIL'}\n")
        f.write(f"- Segmented mean Pearson (smoothed) >= 0.75: {'PASS' if seg_pass else 'FAIL'}\n")

    print("[Updated] WROTE:")
    print(aligned_csv)
    print(stats_path)
    print(audit_path)

def main():
    ap = argparse.ArgumentParser(description="Reconstruct per-second average power from cumulative energy (µJ)")
    ap.add_argument("--dir", type=Path, default=Path("20250904001/power"), help="Directory containing power_monitoring_*.csv")
    ap.add_argument("--file", type=Path, default=None, help="Process a single file instead of a directory")
    # Trend comparison options (OpenDC powerSource.parquet)
    ap.add_argument("--source-parquet", type=Path, default=None, help="Path to powerSource.parquet (default: <dir>/powerSource.parquet)")
    ap.add_argument("--source-ts-col", type=str, default=None, help="Timestamp column name in powerSource.parquet (auto-detect if omitted)")
    ap.add_argument("--export-trend", type=Path, default=None, help="Export aligned per-second trend CSV to this path")
    ap.add_argument("--export-trend-stats", type=Path, default=None, help="Export trend metrics to this path (CSV or TXT)")
    ap.add_argument("--trend-max-lag-sec", type=int, default=600, help="Max absolute lag (seconds) to search for best trend alignment")
    ap.add_argument("--trend-pass-threshold", type=float, default=0.85, help="Threshold on best-lag Pearson r to PASS trend check (standard=0.85)")
    ap.add_argument("--power-report", type=Path, default=None, help="Write power trend audit report (.md). Default: <dir>/power_audit.md")
    ap.add_argument("--write-per-node", action="store_true", help="Also write per-node *_corrected.csv files (default: only total)")
    # Advanced alignment options
    ap.add_argument("--active-from-invocations", type=Path, default=None, help="Path to invocations_merged.jsonl to restrict trend to active window")
    ap.add_argument("--activity-padding-sec", type=int, default=0, help="Padding seconds added to both sides of active window")
    ap.add_argument("--segment-sec", type=int, default=0, help="If >0, compute segmented best-lag Pearson r per segment of this size and report mean/min/std")
    ap.add_argument("--smooth-sec", type=int, default=0, help="If >0, moving-average smoothing window (seconds) applied before z-score")
    ap.add_argument("--power-report-mode", choices=["simple","full"], default="simple", help="Report mode: simple (default) or full")
    ap.add_argument("--seg-pass-threshold", type=float, default=0.90, help="PASS threshold for segmented mean Pearson (default 0.90)")
    ap.add_argument("--plot-global-out", type=Path, default=None, help="If set, write global trend plot PNG to this path")
    ap.add_argument("--plot-segmented-out", type=Path, default=None, help="If set, write segmented Pearson bars PNG to this path")
    # Optional: reuse existing paper plotting functions
    ap.add_argument("--plot-power-z-out", type=Path, default=None, help="If set, also create z-score power trend plot via plots_for_paper.py")
    ap.add_argument("--cpu-pairs-csv", type=Path, default=None, help="If set, draw CPU usage scatter via plots_for_paper.py")
    ap.add_argument("--plot-cpu-scatter-out", type=Path, default=None, help="Output path for CPU scatter plot (with --cpu-pairs-csv)")

    # Updated evaluation mode (standardized outputs; replaces compute_power_trend_updated.py)
    ap.add_argument("--updated-out-dir", type=Path, default=None, help="If set, also produce updated evaluation outputs (trend_alignment_updated.csv, trend_stats_updated.txt, power_audit_updated.md)")
    ap.add_argument("--updated-smooth-sec", type=int, default=60, help="Smoothing window (sec) for updated evaluation; default 60")
    ap.add_argument("--updated-segment-sec", type=int, default=300, help="Segment size (sec) for updated evaluation; default 300")
    ap.add_argument("--updated-max-lag-sec", type=int, default=600, help="Max absolute lag (sec) for updated evaluation; default 600")
    ap.add_argument("--updated-activity-padding-sec", type=int, default=60, help="Padding around active window when using --active-from-invocations in updated evaluation; default 60")

    args = ap.parse_args()

    if args.file is not None:
        out = process_file(args.file)
        print(f"Wrote: {out}")
        return
    if not args.dir.exists():
        raise SystemExit(f"Directory not found: {args.dir}")

    files = sorted(p for p in args.dir.glob("power_monitoring_*.csv"))
    if not files:
        raise SystemExit(f"No files matched in {args.dir}")

    power_maps = []
    min_ts_global = None
    max_ts_global = None

    for p in files:
        # reconstruct per-file corrected, but only write if requested
        prefix, samples = read_power_csv(p)
        rows = reconstruct_power(samples)
        if args.write_per_node:
            out = p.with_name(p.stem + "_corrected.csv")
            write_corrected_csv(out, prefix, rows)
            print(f"Wrote: {out}")
        # build map for aggregation
        m, tmin, tmax = rows_to_power_map(rows)
        if tmin is None or tmax is None:
            continue
        power_maps.append(m)
        min_ts_global = tmin if min_ts_global is None else min(min_ts_global, tmin)
        max_ts_global = tmax if max_ts_global is None else max(max_ts_global, tmax)

    # aggregate across files into total power and total energy (J)
    if power_maps and min_ts_global is not None and max_ts_global is not None:
        total_series = []  # (ts, total_power_w, total_energy_J)
        e_tot = 0.0
        for ts in range(min_ts_global, max_ts_global + 1):
            p_tot = 0.0
            for m in power_maps:
                p_tot += m.get(ts, 0.0)
            e_tot += p_tot  # W * 1s -> J
            total_series.append((ts, p_tot, e_tot))
        out_total = args.dir / "power_total_corrected.csv"
        write_total_corrected_csv(out_total, total_series)
        print(f"Wrote total: {out_total}")

        # Compare trend vs powerSource.parquet if available
        src_parquet = args.source_parquet if args.source_parquet is not None else (args.dir / "powerSource.parquet")
        metrics = compare_trend_advanced(
            out_total,
            src_parquet,
            args.source_ts_col,
            args.export_trend,
            args.trend_max_lag_sec,
            args.export_trend_stats,
            args.active_from_invocations,
            args.activity_padding_sec,
            args.segment_sec,
            args.smooth_sec,
        )
        # Write audit report
        if metrics is not None:
            report_path = args.power_report if args.power_report is not None else (args.dir / ("power_audit_simple.md" if args.power_report_mode=="simple" else "power_audit.md"))
            if args.power_report_mode == "simple":
                write_power_audit_report_simple(report_path, metrics, global_threshold=args.trend_pass_threshold, seg_threshold=args.seg_pass_threshold)
            else:
                write_power_audit_report(report_path, out_total, src_parquet, metrics, args.trend_pass_threshold)
            # Optional plots
            aligned_csv = metrics.get("export_csv")
            if aligned_csv:
                if args.plot_global_out:
                    plot_global_from_alignment(Path(aligned_csv), args.plot_global_out)
                if args.plot_segmented_out and args.segment_sec and int(args.segment_sec) > 0:
                    plot_segment_bars_from_alignment(Path(aligned_csv), args.plot_segmented_out, segment_sec=int(args.segment_sec), max_lag_sec=int(args.trend_max_lag_sec))
                # Reuse existing paper plotting functions if requested
                if args.plot_power_z_out is not None:
                    try:
                        try:
                            # Case 1: run from repo root with top-level package available
                            from consistency_verification.plots_for_paper import plot_power_trend, plot_cpu_scatter_from_pairs
                        except Exception:
                            # Case 2: run this file directly from within the consistency_verification folder
                            # Add the sibling folder to sys.path and import module by filename
                            import sys
                            from pathlib import Path as _P
                            sys.path.insert(0, str(_P(__file__).resolve().parents[1]))  # .../consistency_verification
                            from plots_for_paper import plot_power_trend, plot_cpu_scatter_from_pairs  # type: ignore
                        if plot_power_trend is not None:
                            plot_power_trend(
                                out_total,
                                src_parquet,
                                args.plot_power_z_out,
                                power_source_ts_col=(args.source_ts_col if args.source_ts_col else None),
                                use_best_lag=True,
                                max_lag_sec=int(args.trend_max_lag_sec),
                                x_units="seconds",
                            )
                            print(f"[Plot] Wrote z-score power trend via plots_for_paper: {args.plot_power_z_out}")
                            if args.cpu_pairs_csv is not None and args.plot_cpu_scatter_out is not None and 'plot_cpu_scatter_from_pairs' in locals():
                                plot_cpu_scatter_from_pairs(args.cpu_pairs_csv, args.plot_cpu_scatter_out)
                            print(f"[Plot] Wrote CPU scatter via plots_for_paper: {args.plot_cpu_scatter_out}")
                    except Exception as e:
                        print(f"[Plot] Failed plots_for_paper calls: {e}")



        # Standardized updated evaluation (merged functionality)
        if args.updated_out_dir is not None:
            _run_updated_evaluation(
                out_total,
                src_parquet,
                args.source_ts_col,
                args.active_from_invocations,
                args.updated_out_dir,
                smooth_sec=int(args.updated_smooth_sec),
                segment_sec=int(args.updated_segment_sec),
                max_lag_sec=int(args.updated_max_lag_sec),
                activity_padding_sec=int(args.updated_activity_padding_sec),
            )

if __name__ == "__main__":
    main()

