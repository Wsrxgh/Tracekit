#!/usr/bin/env python3
"""
Performance-Energy Scatter Plot for combined_experiments

- X axis: Energy per Task [J] (energy_per_task_mean)
- Y axis: Total Makespan [s] (makespan_ms_mean / 1000)
- Points: 22 configs (0..21) = 2 topologies x 11 allocation policies, averaged over seeds
- Styling (simplified legend system):
  * Marker SHAPE → Allocation Policy (Baseline-IC1, FirstFit, Pack-IC+, Spread-IC-, Spread-vCPU)
  * Marker COLOR → Overcommitment ratio (OC: 1.0, 1.25, 1.5, 2.0)
  * Marker FILL → Topology (filled=4x4, hollow=2x8)
- Output: 1. Simple Experiment/output/combined_experiments/perf_energy_scatter.png

Note: We assume powerSource.energy_usage is already in Joules (J); no unit conversion is applied.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import math
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
# Optional inset support (unused in two-panel mode, kept for fallback)
try:
    from mpl_toolkits.axes_grid1.inset_locator import inset_axes as _inset_axes
except Exception:
    _inset_axes = None

ROOT = Path('.')
COMBINED_JSON = ROOT / '1. Simple Experiment' / 'experiments' / 'combined_experiments.json'
SUMMARY_BY_CONFIG = ROOT / '1. Simple Experiment' / 'output' / 'combined_experiments' / 'summary_by_config.csv'
OUT_PNG = ROOT / '1. Simple Experiment' / 'output' / 'combined_experiments' / 'perf_energy_scatter.png'

# --- Helpers to derive concise policy labels ---

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
        w = policy.get('weighers', []) or []
        for ww in w:
            if not isinstance(ww, dict):
                continue
            if ww.get('type') == 'InstanceCount':
                mult = float(ww.get('multiplier', 0.0))
                return 'IC+' if mult > 0 else 'IC-' if mult < 0 else 'IC0'
            if ww.get('type') == 'VCpu':
                mult = float(ww.get('multiplier', 0.0))
                return 'VCPU+' if mult > 0 else 'VCPU-' if mult < 0 else 'VCPU0'
    except Exception:
        pass
    return 'None'

def policy_short_name(policy: Dict[str, Any], idx: int) -> str:
    ar = _get_alloc_ratio(policy)
    base = f"AR{ar:g}"
    suf: List[str] = []
    if _has_filter(policy, 'VCpuCapacity'):
        suf.append('Cap')
    # InstanceCount filter with limit=1
    try:
        for f in policy.get('filters', []):
            if isinstance(f, dict) and f.get('type') == 'InstanceCount' and 'limit' in f:
                suf.append('IC=1')
                break
    except Exception:
        pass
    wlab = _get_weigher_label(policy)
    tag = '-'.join([base] + suf + ([wlab] if wlab != 'None' else []))
    return tag or f'Policy{idx}'

# --- Load config mapping ---

def _family_name(policy: Dict[str, Any]) -> str:
    # Map to requested family names
    w = _get_weigher_label(policy)
    if w.startswith('IC+'):
        return 'Pack-IC+'
    if w.startswith('IC-'):
        return 'Spread-IC-'
    if w.startswith('VCPU+'):
        return 'Spread-vCPU'
    # No weighers
    # Detect special cases
    if _has_filter(policy, 'VCpuCapacity'):
        # And possibly InstanceCount limit=1
        try:
            for f in policy.get('filters', []):
                if isinstance(f, dict) and f.get('type') == 'InstanceCount' and 'limit' in f:
                    return 'Baseline-IC1'
        except Exception:
            pass
    # If pure filter with no weighers
    return 'FirstFit'

def load_mapping() -> Tuple[List[str], List[str]]:
    with open(COMBINED_JSON, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    topologies = [t.get('pathToFile', str(i)) for i, t in enumerate(cfg.get('topologies', []))]
    policies = cfg.get('allocationPolicies', [])
    policy_labels = []
    for i, p in enumerate(policies):
        fam = _family_name(p)
        oc = _get_alloc_ratio(p)
        policy_labels.append(f"{fam} (OC={oc:g})")
    return topologies, policy_labels

# --- Plot ---

def plot_perf_energy(
    y_high_start: Optional[float] = None,
    y_high_scale: Optional[float] = None,
    y_high_end: Optional[float] = None,
    x_padding_pct: float = 2.0
):
    if not SUMMARY_BY_CONFIG.exists():
        raise SystemExit(f"Summary CSV not found: {SUMMARY_BY_CONFIG}")
    df = pd.read_csv(SUMMARY_BY_CONFIG)
    # Expect columns: config_id, energy_per_task_mean, makespan_ms_mean
    need = {'config_id', 'energy_per_task_mean', 'makespan_ms_mean'}
    if not need.issubset(df.columns):
        raise SystemExit(f"Missing columns in {SUMMARY_BY_CONFIG}: need {sorted(need)}")

    # Load config to extract policy families and OC values
    with open(COMBINED_JSON, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    tops = [t.get('pathToFile', str(i)) for i, t in enumerate(cfg.get('topologies', []))]
    policies = cfg.get('allocationPolicies', [])

    num_policies = len(policies)
    num_topologies = len(tops)
    if num_policies == 0 or num_topologies == 0:
        raise SystemExit('[ERROR] No policies or topologies found in combined_experiments.json')

    # Define mapping: Shape -> Policy Family
    policy_family_shapes = {
        'Baseline-IC1': 'o',      # Circle
        'FirstFit': 's',          # Square
        'Pack-IC+': '^',          # Triangle up
        'Spread-IC-': 'v',        # Triangle down
        'Spread-vCPU': 'D',       # Diamond
    }

    # Define mapping: Color -> OC value
    oc_colors = {
        1.0: '#1f77b4',    # Blue
        1.25: '#ff7f0e',   # Orange
        1.5: '#2ca02c',    # Green
        2.0: '#d62728',    # Red
    }

    fig, (ax_high, ax_low) = plt.subplots(2, 1, figsize=(10.0, 6.8), sharex=True,
                                          gridspec_kw={'height_ratios': [1, 3], 'hspace': 0.05})

    # Build legend handles
    from matplotlib.lines import Line2D

    # Iterate expected config_id 0..21 and plot onto both panels
    energies: List[float] = []
    makespans: List[float] = []

    for _, row in df.iterrows():
        try:
            cid = int(row['config_id'])
        except Exception:
            continue
        if math.isnan(row['energy_per_task_mean']) or math.isnan(row['makespan_ms_mean']):
            continue
        # Energy in J (as-is); makespan from ms to s
        energy = float(row['energy_per_task_mean'])
        makespan_sec = float(row['makespan_ms_mean']) / 1000.0
        energies.append(energy); makespans.append(makespan_sec)

        # Map to topology/policy index using topology×policy grid derived from config
        topo_idx = (cid // num_policies) if num_policies > 0 else 0
        pol_idx = (cid % num_policies) if num_policies > 0 else 0

        # Get policy family and OC value
        policy = policies[pol_idx]
        family = _family_name(policy)
        oc = _get_alloc_ratio(policy)

        # Get marker shape based on policy family
        marker = policy_family_shapes.get(family, 'o')

        # Get color based on OC value
        color = oc_colors.get(oc, '#808080')  # Default gray if OC not found

        # Solid (filled) for first topology (4x4), hollow for others (2x8)
        if topo_idx == 0:
            # Filled marker with transparency
            for axx in (ax_low, ax_high):
                axx.scatter(energy, makespan_sec, s=80, marker=marker, c=[color],
                           edgecolor='black', linewidths=0.8, alpha=0.6, zorder=3)
        else:
            # Hollow marker (no transparency needed)
            for axx in (ax_low, ax_high):
                axx.scatter(energy, makespan_sec, s=80, marker=marker,
                           facecolors='none', edgecolors=color, linewidths=1.5, alpha=1.0, zorder=3)

    # Build three separate legends: Policy Family (shapes), OC (colors), Topology (fill)

    # Legend 1: Allocation Policy (shapes)
    policy_handles = []
    for family_name in ['Baseline-IC1', 'FirstFit', 'Pack-IC+', 'Spread-IC-', 'Spread-vCPU']:
        marker = policy_family_shapes[family_name]
        h = Line2D([0], [0], marker=marker, color='w', label=family_name,
                   markerfacecolor='gray', markeredgecolor='black', markersize=9, linestyle='None')
        policy_handles.append(h)

    # Legend 2: OC values (colors)
    oc_handles = []
    for oc_val in sorted(oc_colors.keys()):
        color = oc_colors[oc_val]
        h = Line2D([0], [0], marker='o', color='w', label=f'OC={oc_val:g}',
                   markerfacecolor=color, markeredgecolor='black', markersize=9, linestyle='None')
        oc_handles.append(h)

    # Legend 3: Topology (fill style)
    topo_handles = []
    for ti, t in enumerate(tops):
        name = Path(str(t)).stem
        if ti == 0:
            # Filled
            h = Line2D([0], [0], marker='o', color='w', label=f'{name} (filled)',
                      markerfacecolor='gray', markeredgecolor='black', markersize=9, linestyle='None')
        else:
            # Hollow
            h = Line2D([0], [0], marker='o', color='w', label=f'{name} (hollow)',
                      markerfacecolor='none', markeredgecolor='gray', markersize=9, linewidth=1.5, linestyle='None')
        topo_handles.append(h)

    # Compute X limits tightly with minimal padding
    e_arr = np.array(energies, dtype=float)
    m_arr = np.array(makespans, dtype=float)
    finite_mask = np.isfinite(e_arr) & np.isfinite(m_arr)
    e_arr = e_arr[finite_mask]; m_arr = m_arr[finite_mask]
    if e_arr.size == 0 or m_arr.size == 0:
        raise SystemExit('No valid data points to plot')

    xmin = float(np.nanmin(e_arr)); xmax = float(np.nanmax(e_arr))
    if not np.isfinite(xmin) or not np.isfinite(xmax):
        raise SystemExit('No valid energy values to plot')
    x_range = max(1.0, xmax - xmin)
    # Use configurable padding (default 1% instead of 5%)
    pad_x = (x_padding_pct / 100.0) * x_range
    for axx in (ax_low, ax_high):
        axx.set_xlim(max(0.0, xmin - pad_x), xmax + pad_x)
        axx.margins(x=0)

    # Determine y-axis break automatically at the largest gap (data-driven)
    ys = np.sort(m_arr)
    if ys.size >= 2:
        diffs = np.diff(ys)
        j = int(np.nanargmax(diffs))
        y_low_min = float(ys[0]); y_low_max = float(ys[j])
        y_high_min = float(ys[j+1]); y_high_max = float(ys[-1])
    else:
        y_low_min = y_low_max = y_high_min = y_high_max = float(ys[0] if ys.size else 0.0)

    # Ensure non-degenerate ranges
    if y_low_max <= y_low_min:
        y_low_max = y_low_min + 1.0
    if y_high_max <= y_high_min:
        y_high_max = y_high_min + 1.0

    # Apply custom y_high settings if provided (with new defaults)
    # Default: y_high_start=9400, y_high_end=9600
    if y_high_start is None:
        y_high_start = 9400.0
    if y_high_end is None:
        y_high_end = 9600.0

    y_high_min = y_high_start
    y_high_max = y_high_end

    # If user explicitly provides y_high_scale, apply it
    if y_high_scale is not None:
        original_range = y_high_max - y_high_min
        new_range = original_range * y_high_scale
        y_high_max = y_high_min + new_range

    # Padding for readability
    pad_low = 0.05 * (y_low_max - y_low_min)
    pad_high = 0.05 * (y_high_max - y_high_min)

    ax_low.set_ylim(y_low_min - pad_low, y_low_max + pad_low)
    ax_high.set_ylim(y_high_min - pad_high, y_high_max + pad_high)

    # Disable offset notation (scientific notation) on y-axis to show full values
    ax_low.yaxis.get_major_formatter().set_useOffset(False)
    ax_high.yaxis.get_major_formatter().set_useOffset(False)

    # Broken-axis styling
    ax_high.spines['bottom'].set_visible(False)
    ax_low.spines['top'].set_visible(False)
    ax_high.tick_params(labeltop=False)
    ax_low.xaxis.tick_bottom()
    # Diagonal break marks
    d = .008
    kwargs = dict(transform=ax_high.transAxes, color='k', clip_on=False)
    ax_high.plot((-d, +d), (-d, +d), **kwargs)
    ax_high.plot((1 - d, 1 + d), (-d, +d), **kwargs)
    kwargs = dict(transform=ax_low.transAxes, color='k', clip_on=False)
    ax_low.plot((-d, +d), (1 - d, 1 + d), **kwargs)
    ax_low.plot((1 - d, 1 + d), (1 - d, 1 + d), **kwargs)
    ax_low.plot((1 - d, 1 + d), (1 - d, 1 + d), **kwargs)

    # Labels, grid; y-label only once, centered across both panels
    ax_low.set_xlabel('Energy per Task [J]')
    for axx in (ax_low, ax_high):
        axx.grid(True, alpha=0.35)
    # Place a single y-label centered vertically across both subplots
    fig.text(0.04, 0.5, 'Total Makespan [s]', va='center', rotation='vertical', fontsize=12)

    from matplotlib.lines import Line2D
    # Three legends stacked vertically on the right side
    # Legend 1: Allocation Policy (top - upper panel)
    leg1 = ax_high.legend(handles=policy_handles, title='Allocation Policy (Shape)',
                         loc='upper left', bbox_to_anchor=(1.02, 1.0), fontsize=9, title_fontsize=10)
    ax_high.add_artist(leg1)

    # Legend 2: OC values (middle - lower panel top)
    leg2 = ax_low.legend(handles=oc_handles, title='Overcommitment (Color)',
                         loc='upper left', bbox_to_anchor=(1.02, 1.0), fontsize=9, title_fontsize=10)
    ax_low.add_artist(leg2)

    # Legend 3: Topology (bottom - lower panel bottom)
    ax_low.legend(handles=topo_handles, title='Topology (Fill)',
                 loc='lower left', bbox_to_anchor=(1.02, 0.5), fontsize=9, title_fontsize=10)
    # No additional annotation needed; axis labels and ticks show the start values clearly

    fig.tight_layout()

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=200, bbox_inches='tight')
    plt.close(fig)
    print(f'Saved: {OUT_PNG}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Plot Performance-Energy scatter with customizable broken y-axis and tight x-axis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default (y-axis: 9400-9600s, x-padding: 2%%)
  python analysis/plot_performance_energy.py

  # Custom upper y-axis starting point
  python analysis/plot_performance_energy.py --y-high-start 9000

  # Custom upper y-axis range (start and end)
  python analysis/plot_performance_energy.py --y-high-start 9000 --y-high-end 10000

  # Scale upper y-axis range (e.g., 1.5x wider)
  python analysis/plot_performance_energy.py --y-high-scale 1.5

  # Tighter x-axis (1%% padding on each side)
  python analysis/plot_performance_energy.py --x-padding 1.0

  # Very tight x-axis (0.5%% padding)
  python analysis/plot_performance_energy.py --x-padding 0.5
        """
    )
    parser.add_argument(
        '--y-high-start',
        type=float,
        default=9400.0,
        help='Starting point (minimum value) for upper y-axis panel [seconds]. Default: 9400.'
    )
    parser.add_argument(
        '--y-high-scale',
        type=float,
        default=None,
        help='Scale factor for upper y-axis range (e.g., 1.5 = 150%% of original range). Ignored if --y-high-end is set.'
    )
    parser.add_argument(
        '--y-high-end',
        type=float,
        default=9600.0,
        help='Ending point (maximum value) for upper y-axis panel [seconds]. Default: 9600.'
    )
    parser.add_argument(
        '--x-padding',
        type=float,
        default=2.0,
        help='X-axis padding as percentage of data range (default: 2.0%%). Lower values = tighter fit. Try 0.5 or 1.0 for tighter.'
    )
    parser.add_argument(
        '--summary-by-config',
        type=Path,
        default=SUMMARY_BY_CONFIG,
        help='Path to summary_by_config.csv (default: 1. Simple Experiment/output/combined_experiments/summary_by_config.csv)'
    )
    parser.add_argument(
        '--combined-json',
        type=Path,
        default=COMBINED_JSON,
        help='Path to combined_experiments.json (default: 1. Simple Experiment/experiments/combined_experiments.json)'
    )
    parser.add_argument(
        '--out-png',
        type=Path,
        default=OUT_PNG,
        help='Output PNG path (default: 1. Simple Experiment/output/combined_experiments/perf_energy_scatter.png)'
    )


    args = parser.parse_args()

    # Override default paths if provided
    # Update module-level paths via globals() to avoid linter complaints
    globals()['COMBINED_JSON'] = args.combined_json
    globals()['SUMMARY_BY_CONFIG'] = args.summary_by_config
    globals()['OUT_PNG'] = args.out_png

    plot_perf_energy(
        y_high_start=args.y_high_start,
        y_high_scale=args.y_high_scale,
        y_high_end=args.y_high_end,
        x_padding_pct=args.x_padding
    )

