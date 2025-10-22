#!/usr/bin/env python3
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Dict
import math
import json
import pandas as pd

"""
Flexible, configurable ranking for combined_experiments results.

Features
- Multi-objective support via:
  1) Pareto non-dominated filtering (minimize or maximize per metric)
  2) Weighted sum scoring with normalization (min-max)
  3) Lexicographic ordering by priority list
- Works directly on summary_by_config.csv produced by analysis/compare_combined_results.py

Example usages
1) Pareto front on makespan & energy per task, then weighted sum to pick Top-5:
   python analysis/rank_configs.py \
     --csv "1. Simple Experiment/output/combined_experiments/summary_by_config.csv" \
     --metrics "makespan_ms_mean:asc:0.5,energy_per_task_mean:asc:0.5" \
     --method weighted --pareto-first --top-k 5 --print

2) Pure lexicographic (makespan first, then energy, then p95 turnaround):
   python analysis/rank_configs.py \
     --csv "1. Simple Experiment/output/combined_experiments/summary_by_config.csv" \
     --metrics "makespan_ms_mean:asc,energy_per_task_mean:asc,p95_turn_ms_mean:asc" \
     --method lexicographic --top-k 5 --print

3) Direct Pareto front export (minimize both):
   python analysis/rank_configs.py \
     --csv "1. Simple Experiment/output/combined_experiments/summary_by_config.csv" \
     --metrics "makespan_ms_mean:asc,energy_per_task_mean:asc" \
     --method pareto --out "pareto_front.csv" --print
"""

@dataclass
class MetricSpec:
    name: str
    direction: str  # 'asc' (minimize) or 'desc' (maximize)
    weight: float = 1.0


def parse_metrics_spec(spec: str) -> List[MetricSpec]:
    metrics: List[MetricSpec] = []
    if not spec:
        return metrics
    for token in spec.split(','):
        token = token.strip()
        if not token:
            continue
        parts = token.split(':')
        if len(parts) == 1:
            name, direction, weight = parts[0], 'asc', 1.0
        elif len(parts) == 2:
            name, direction = parts[0].strip(), parts[1].strip().lower()
            weight = 1.0
        else:
            name, direction, w = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
            try:
                weight = float(w)
            except Exception:
                weight = 1.0
        if direction not in ('asc', 'desc'):
            direction = 'asc'
        metrics.append(MetricSpec(name=name, direction=direction, weight=weight))
    return metrics


def ensure_columns(df: pd.DataFrame, metrics: List[MetricSpec]) -> List[str]:
    missing = [m.name for m in metrics if m.name not in df.columns]
    return missing


def pareto_filter(df: pd.DataFrame, metrics: List[MetricSpec]) -> pd.DataFrame:
    """Return non-dominated rows. O(n^2), adequate for small counts.
    A dominates B if it is no worse in all metrics and strictly better in at least one
    (considering direction per metric).
    """
    if not metrics:
        return df.copy()
    idx = df.index.tolist()
    dominated = set()  # type: ignore[var-annotated]
    for i in range(len(idx)):
        if idx[i] in dominated:
            continue
        ai = df.loc[idx[i]]
        for j in range(len(idx)):
            if i == j or idx[j] in dominated:
                continue
            aj = df.loc[idx[j]]
            # Is j dominated by i?
            ge_all = True
            gt_any = False
            for m in metrics:
                vi = ai[m.name]
                vj = aj[m.name]
                if pd.isna(vi) or pd.isna(vj):
                    ge_all = False
                    break
                if m.direction == 'asc':  # minimize
                    if vi > vj:
                        ge_all = False
                        break
                    if vi < vj:
                        gt_any = True
                else:  # maximize
                    if vi < vj:
                        ge_all = False
                        break
                    if vi > vj:
                        gt_any = True
            if ge_all and gt_any:
                dominated.add(idx[j])
    keep = [i for i in idx if i not in dominated]
    return df.loc[keep].copy()


def minmax_normalize(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors='coerce')
    s = s.astype(float)
    vmin = s.min()
    vmax = s.max()
    if not math.isfinite(float(vmin)) or not math.isfinite(float(vmax)):
        return pd.Series([float('nan')] * len(series), index=series.index)
    if vmax == vmin:
        # constant column -> neutral utility 1.0
        return pd.Series([1.0] * len(series), index=series.index)
    return (s - vmin) / (vmax - vmin)


def weighted_score(df: pd.DataFrame, metrics: List[MetricSpec]) -> pd.Series:
    if not metrics:
        return pd.Series([0.0] * len(df), index=df.index)
    total_w = sum(max(0.0, m.weight) for m in metrics) or 1.0
    score = pd.Series([0.0] * len(df), index=df.index, dtype=float)
    for m in metrics:
        norm = minmax_normalize(df[m.name])
        # utility: higher is better
        if m.direction == 'asc':
            util = 1.0 - norm  # minimize -> smaller is better
        else:
            util = norm  # maximize -> larger is better
        w = max(0.0, m.weight) / total_w
        score = score + w * util
    return score


def lexicographic_sort(df: pd.DataFrame, metrics: List[MetricSpec]) -> pd.DataFrame:
    if not metrics:
        return df.copy()
    # pandas sort_values supports per-column ascending flags
    by = [m.name for m in metrics]
    ascending = [True if m.direction == 'asc' else False for m in metrics]
    return df.sort_values(by=by, ascending=ascending, kind='mergesort')


def main():
    ap = argparse.ArgumentParser(description="Rank configs with configurable multi-objective scoring (Pareto, weighted, lexicographic)")
    ap.add_argument('--csv', type=Path, default=Path('1. Simple Experiment/output/combined_experiments/summary_by_config.csv'))
    ap.add_argument('--metrics', type=str, default='makespan_ms_mean:asc:0.5,energy_per_task_mean:asc:0.5',
                    help='Comma-separated metric specs: name:asc|desc[:weight]. Example: "makespan_ms_mean:asc:0.7,energy_per_task_mean:asc:0.3"')
    ap.add_argument('--method', choices=['pareto','weighted','lexicographic'], default='weighted')
    ap.add_argument('--pareto-first', action='store_true', help='Apply Pareto filter before ranking (for weighted/lexicographic)')
    ap.add_argument('--top-k', type=int, default=5)
    ap.add_argument('--out', type=Path, default=None, help='Output CSV path (default: <csv_dir>/ranking_<method>.csv)')
    ap.add_argument('--print', action='store_true')
    args = ap.parse_args()

    df = pd.read_csv(args.csv)

    metrics = parse_metrics_spec(args.metrics)
    missing = ensure_columns(df, metrics)
    if missing:
        raise SystemExit(f"Missing metric columns in {args.csv}: {missing}")

    # Keep only needed columns + config_id
    cols = ['config_id'] + [m.name for m in metrics]
    work = df[cols].copy()

    # Pareto filter if requested or if method=pareto
    if args.method == 'pareto' or args.pareto_first:
        front = pareto_filter(work, metrics)
    else:
        front = work

    if args.method == 'pareto':
        ranked = front.copy()
        ranked.insert(0, 'rank', range(1, len(ranked)+1))
    elif args.method == 'weighted':
        score = weighted_score(front, metrics)
        ranked = front.copy()
        ranked.insert(1, 'score', score)
        ranked = ranked.sort_values(by='score', ascending=False, kind='mergesort')
        ranked.insert(0, 'rank', range(1, len(ranked)+1))
    elif args.method == 'lexicographic':
        ranked = lexicographic_sort(front, metrics)
        ranked.insert(0, 'rank', range(1, len(ranked)+1))
    else:
        raise SystemExit(f"Unknown method: {args.method}")

    if args.top_k and args.top_k > 0:
        ranked_top = ranked.head(args.top_k).copy()
    else:
        ranked_top = ranked

    out = args.out
    if out is None:
        tag = 'pareto' if args.method == 'pareto' else args.method
        out = args.csv.parent / f"ranking_{tag}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    ranked_top.to_csv(out, index=False)

    print(f"Wrote: {out}")
    if args.print:
        try:
            pd.options.display.float_format = lambda v: f"{v:,.3f}"
        except Exception:
            pass
        print(ranked_top.to_string(index=False))


if __name__ == '__main__':
    main()

