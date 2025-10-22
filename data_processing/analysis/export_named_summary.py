#!/usr/bin/env python3
"""
Export a named summary CSV for combined_experiments by replacing config_id with
human-readable strategy names (same as the plot), and selecting/formatting key metrics.

Input:
- 1. Simple Experiment/output/combined_experiments/summary_by_config.csv
- 1. Simple Experiment/experiments/combined_experiments.json

Output:
- 1. Simple Experiment/output/combined_experiments/summary_by_strategy.csv

Columns in output:
- Strategy                (e.g., "Pack-IC+ (OC=1.5)")
- Total Makespan [s]      (makespan_ms_mean / 1000, 2 decimals)
- Energy per Task [J]     (energy_per_task_mean as-is)
- P95 Task Wait Time [s]  (p95_wait_ms_mean / 1000, 2 decimals)
- P95 Task Turnaround Time [s] (p95_turn_ms_mean / 1000, 2 decimals)
- Average CPU Utilization (cpu_utilization_mean as-is)

Note: We format time-based values to 2 decimals. Energy/utilization are kept as-is.
"""
from __future__ import annotations
import json
import argparse
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sys
import math
import pandas as pd

ROOT = Path('.')
COMBINED_JSON = ROOT / '1. Simple Experiment' / 'experiments' / 'combined_experiments.json'
SUMMARY_BY_CONFIG = ROOT / '1. Simple Experiment' / 'output' / 'combined_experiments' / 'summary_by_config.csv'
OUT_CSV = ROOT / '1. Simple Experiment' / 'output' / 'combined_experiments' / 'summary_by_strategy.csv'


def _get_alloc_ratio(policy: Dict[str, Any]) -> float:
    try:
        for f in policy.get('filters', []):
            if isinstance(f, dict) and f.get('type') == 'VCpu':
                return float(f.get('allocationRatio', 1.0))
    except Exception:
        pass
    return 1.0


def _has_filter(policy: Dict[str, Any], t: str) -> bool:
    try:
        return any(isinstance(f, dict) and f.get('type') == t for f in policy.get('filters', []))
    except Exception:
        return False


def _get_weigher_label(policy: Dict[str, Any]) -> str:
    try:
        for w in policy.get('weighers', []):
            if not isinstance(w, dict):
                continue
            t = w.get('type')
            mult = float(w.get('multiplier', 0.0)) if 'multiplier' in w else 0.0
            if t == 'InstanceCount':
                return 'IC+' if mult > 0 else 'IC-' if mult < 0 else 'IC0'
            if t == 'VCpu':
                return 'VCPU+' if mult > 0 else 'VCPU-' if mult < 0 else 'VCPU0'
    except Exception:
        pass
    return 'None'


def _family_name(policy: Dict[str, Any]) -> str:
    w = _get_weigher_label(policy)
    if w.startswith('IC+'):
        return 'Pack-IC+'
    if w.startswith('IC-'):
        return 'Spread-IC-'
    if w.startswith('VCPU+'):
        return 'Spread-vCPU'
    # No weighers → check special cases
    if _has_filter(policy, 'VCpuCapacity'):
        try:
            for f in policy.get('filters', []):
                if isinstance(f, dict) and f.get('type') == 'InstanceCount' and 'limit' in f:
                    return 'Baseline-IC1'
        except Exception:
            pass
    return 'FirstFit'


def load_policy_labels(combined_json: Path) -> List[str]:
    with open(combined_json, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    policies = cfg.get('allocationPolicies', [])
    labels: List[str] = []
    for p in policies:
        fam = _family_name(p)
        oc = _get_alloc_ratio(p)
        labels.append(f"{fam} (OC={oc:g})")
    return labels


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Export a named summary CSV for combined_experiments (paths configurable; defaults preserved)")
    ap.add_argument('--summary-by-config', type=Path, default=SUMMARY_BY_CONFIG,
                    help="Path to summary_by_config.csv (default: 1. Simple Experiment/output/combined_experiments/summary_by_config.csv)")
    ap.add_argument('--combined-json', type=Path, default=COMBINED_JSON,
                    help="Path to combined_experiments.json (default: 1. Simple Experiment/experiments/combined_experiments.json)")
    ap.add_argument('--out-csv', type=Path, default=OUT_CSV,
                    help="Output CSV path (default: 1. Simple Experiment/output/combined_experiments/summary_by_strategy.csv)")
    args = ap.parse_args(argv[1:] if isinstance(argv, list) else None)

    if not args.summary_by_config.exists():
        print(f"[ERROR] Summary CSV not found: {args.summary_by_config}")
        return 2
    if not args.combined_json.exists():
        print(f"[ERROR] Combined JSON not found: {args.combined_json}")
        return 2

    df = pd.read_csv(args.summary_by_config)
    required = {
        'config_id',
        'makespan_ms_mean',
        'energy_per_task_mean',
        'p95_wait_ms_mean',
        'p95_turn_ms_mean',
        'cpu_utilization_mean',
    }
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"[ERROR] Missing columns in {args.summary_by_config}: {missing}")
        return 2

    labels = load_policy_labels(args.combined_json)
    num_policies = len(labels)
    if num_policies == 0:
        print("[ERROR] No allocation policies found in combined_experiments.json")
        return 2

    # Map config_id → policy label for a topology × policy grid.
    # Assuming configs are enumerated as: for each topology, for each policy -> increasing config_id.
    # Then: config_id = topo_idx * num_policies + pol_idx  => pol_idx = config_id % num_policies
    def id_to_label(cid: int) -> str:
        try:
            cid_int = int(cid)
            pol_idx = cid_int % num_policies
            return labels[pol_idx]
        except Exception:
            return f"Policy{cid}"


    # Load topology labels for the Topology column
    try:
        with open(args.combined_json, 'r', encoding='utf-8') as f:
            _cfg_topo = json.load(f)
        _topo_entries = _cfg_topo.get('topologies', []) or []
    except Exception:
        _topo_entries = []
    topo_labels: List[str] = []
    for t in _topo_entries:
        try:
            p = t.get('pathToFile', '') if isinstance(t, dict) else str(t)
        except Exception:
            p = ''
        name = Path(p).stem if isinstance(p, str) and p else 'Topology'
        topo_labels.append(name)
    num_topologies = len(topo_labels)

    out_rows = []
    for _, r in df.iterrows():
        try:
            cid = int(r['config_id'])
        except Exception:
            continue
        strat = id_to_label(cid)
        makespan_s = float(r['makespan_ms_mean']) / 1000.0
        p95_wait_s = float(r['p95_wait_ms_mean']) / 1000.0
        p95_turn_s = float(r['p95_turn_ms_mean']) / 1000.0
        energy_j = r['energy_per_task_mean']
        cpu_util = r['cpu_utilization_mean']
        # Determine topology label
        topo_idx = cid // num_policies
        if num_topologies > 0 and 0 <= topo_idx < num_topologies:
            topo_name = topo_labels[topo_idx]
        else:
            topo_name = f"Topology{topo_idx}"

        out_rows.append({
            'Topology': topo_name,
            'Strategy': strat,
            'Total Makespan [s]': f"{makespan_s:.2f}",
            'Energy per Task [J]': energy_j,
            'P95 Task Wait Time [s]': f"{p95_wait_s:.2f}",
            'P95 Task Turnaround Time [s]': f"{p95_turn_s:.2f}",
            'Average CPU Utilization': cpu_util,
        })

    out_df = pd.DataFrame(out_rows)
    # Optional: stable ordering by original config_id (implicitly preserved by iteration)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.out_csv, index=False)
    print(f"Saved: {args.out_csv}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

