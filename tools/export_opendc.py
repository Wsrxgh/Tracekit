#!/usr/bin/env python3
"""
Convert Tracekit traces to OpenDC format (Tasks + Fragments parquet files)

This script processes multi-node trace data and generates:
- tasks.parquet: Task-level information (id, submission_time, duration, resource requirements)
- fragments.parquet: Fine-grained resource usage over time

Usage:
    python3 tools/export_opendc.py --input logs/20250825T1325000 --output opendc_traces/
"""

import argparse
import json
import pandas as pd
import numpy as np
import math
from pathlib import Path
from collections import defaultdict
import glob
import pyarrow as pa
import pyarrow.parquet as pq

def load_node_data(log_dir):
    """Load all trace data from a single node directory
    Note on head-gap handling: We also load proc_metrics.jsonl to optionally
    synthesize the very first fragment [ts_start, ts1) for apps where one task
    equals one fresh PID (e.g., ffmpeg per-invocation). This is not suitable
    for long-lived processes serving multiple tasks concurrently.
    """
    node_dir = Path(log_dir)

    # Load node metadata
    node_meta = json.load(open(node_dir / "node_meta.json"))

    # Load invocations (tasks)
    invocations = []
    inv_file = node_dir / "cctf" / "invocations.jsonl"
    if inv_file.exists():
        with open(inv_file) as f:
            for line in f:
                if line.strip():
                    invocations.append(json.loads(line))

    # Load process CPU usage (diffed)
    proc_cpu = []
    cpu_file = node_dir / "cctf" / "proc_cpu.jsonl"
    if cpu_file.exists():
        with open(cpu_file) as f:
            for line in f:
                if line.strip():
                    proc_cpu.append(json.loads(line))

    # Load raw process snapshots (for head-gap synthesis)
    proc_metrics = []
    pm_file = node_dir / "cctf" / "proc_metrics.jsonl"
    if pm_file.exists():
        with open(pm_file) as f:
            for line in f:
                if line.strip():
                    try:
                        proc_metrics.append(json.loads(line))
                    except Exception:
                        pass

    # Load process memory usage
    proc_rss = []
    rss_file = node_dir / "cctf" / "proc_rss.jsonl"
    if rss_file.exists():
        with open(rss_file) as f:
            for line in f:
                if line.strip():
                    proc_rss.append(json.loads(line))

    return {
        'node_meta': node_meta,
        'invocations': invocations,
        'proc_cpu': proc_cpu,
        'proc_metrics': proc_metrics,
        'proc_rss': proc_rss
    }

def _safe_freq_mhz(node_meta) -> float:
    try:
        v = float(node_meta.get('cpu_freq_mhz'))
        return v if v > 0 else 2400.0
    except Exception:
        return 2400.0

essential_min_mhz = 0.1  # lower bound to avoid zeros in downstream tools

def calculate_cpu_requirements(invocation, node_meta, proc_cpu_data):
    """Calculate CPU count and capacity for a task based on actual CPU usage

    Returns: (cpu_count, cpu_capacity_per_core)
    """
    pid = invocation['pid']
    task_start = invocation['ts_start']
    task_end = invocation['ts_end']
    task_duration_ms = max(0, task_end - task_start)

    # Find CPU samples for this PID during task execution
    task_cpu_samples = [
        sample for sample in proc_cpu_data
        if sample.get('pid') == pid and task_start <= sample.get('ts_ms', task_start) <= task_end
    ]

    freq_mhz = _safe_freq_mhz(node_meta)
    cores_cap = node_meta.get('cpu_cores')
    try:
        cores_cap = int(cores_cap) if cores_cap is not None else None
        if cores_cap is not None and cores_cap <= 0:
            cores_cap = None
    except Exception:
        cores_cap = None

    if not task_cpu_samples:
        # Fallback: assume single core, moderate usage
        return 1, max(freq_mhz * 0.5, essential_min_mhz)

    # Calculate peak core usage using actual dt if available
    def sample_cores(s):
        dt = s.get('dt_ms', 1000)
        dt = max(int(dt) if dt is not None else 1000, 1)
        cpu_ms = float(s.get('cpu_ms', 0.0))
        return max(0.0, cpu_ms / dt)

    peak_cores = max(sample_cores(s) for s in task_cpu_samples)

    # Determine how many cores were actually used (round to nearest, clamp to available cores if known)
    cores_used = max(1, int(peak_cores + 0.5))
    if cores_cap is not None:
        cores_used = min(cores_used, cores_cap)

    # Calculate total CPU time used during task execution
    total_cpu_ms = sum(float(sample.get('cpu_ms', 0.0)) for sample in task_cpu_samples)

    # Calculate average utilization per core
    if task_duration_ms > 0 and cores_used > 0:
        avg_utilization_per_core = (total_cpu_ms / task_duration_ms) / cores_used
        cpu_capacity_per_core = freq_mhz * min(max(avg_utilization_per_core, 0.0), 1.0)
    else:
        cpu_capacity_per_core = freq_mhz * 0.1

    return cores_used, max(cpu_capacity_per_core, 1.0)

def calculate_mem_capacity(invocation, proc_rss_data):
    """Calculate memory capacity for a task based on peak RSS usage

    Returns memory capacity in KB (as required by OpenDC)
    """
    pid = invocation['pid']
    task_start = invocation['ts_start']
    task_end = invocation['ts_end']

    # Find memory samples for this PID during task execution
    task_mem_samples = [
        sample['rss_kb'] for sample in proc_rss_data
        if sample['pid'] == pid and task_start <= sample['ts_ms'] <= task_end
    ]

    if not task_mem_samples:
        # Fallback: estimate based on data size
        data_size_kb = (invocation.get('bytes_in', 0) + invocation.get('bytes_out', 0)) / 1024
        return max(int(data_size_kb * 2), 65536)  # Assume 2x data size, minimum 64MB = 65536KB

    # Use peak memory usage (already in KB)
    peak_rss_kb = max(task_mem_samples)

    return max(int(peak_rss_kb), 1024)  # Minimum 1MB = 1024KB

def generate_tasks(all_node_data):
    """Generate OpenDC Tasks dataframe"""
    tasks = []
    task_id = 1
    
    for node_name, node_data in all_node_data.items():
        node_meta = node_data['node_meta']
        invocations = node_data['invocations']
        proc_cpu_data = node_data['proc_cpu']
        proc_rss_data = node_data['proc_rss']
        
        for inv in invocations:
            # Calculate resource requirements
            cpu_count, cpu_capacity = calculate_cpu_requirements(inv, node_meta, proc_cpu_data)
            mem_capacity = calculate_mem_capacity(inv, proc_rss_data)

            task = {
                'id': task_id,
                'submission_time': inv['ts_enqueue'],  # epochMillis as int64
                'duration': inv['ts_end'] - inv['ts_start'],  # milliseconds
                'cpu_count': cpu_count,  # Actual cores used
                'cpu_capacity': cpu_count * cpu_capacity,  # Total CPU capacity (MHz) = cores × per_core_capacity
                'mem_capacity': mem_capacity   # KB (as required by OpenDC)
            }
            tasks.append(task)
            task_id += 1
    
    tasks_df = pd.DataFrame(tasks)

    # Ensure correct data types for OpenDC compatibility (matching TASK_SCHEMA_V2)
    tasks_df['id'] = tasks_df['id'].astype('int32')                    # INT32
    tasks_df['submission_time'] = tasks_df['submission_time'].astype('int64')  # INT64 (timestamp millis)
    tasks_df['duration'] = tasks_df['duration'].astype('int64')       # INT64
    tasks_df['cpu_count'] = tasks_df['cpu_count'].astype('int32')     # INT32
    tasks_df['cpu_capacity'] = tasks_df['cpu_capacity'].astype('float64')  # DOUBLE (float64 = double)
    tasks_df['mem_capacity'] = tasks_df['mem_capacity'].astype('int64')    # INT64

    return tasks_df

def generate_fragments(all_node_data, tasks_df):
    """Generate OpenDC Fragments dataframe and per-task peak cpu_usage (MHz).
    Returns (fragments_df, peak_by_task: dict[int,float], task_node_info: dict[int, dict]).
    """
    fragments = []
    peak_by_task = {}
    task_node_info = {}

    # Create mapping from PID to task_id and task info
    pid_to_task_info = {}
    task_id = 1

    for node_name, node_data in all_node_data.items():
        node_meta_local = node_data['node_meta']
        for inv in node_data['invocations']:
            pid_to_task_info[inv['pid']] = {
                'task_id': task_id,
                'task_start': inv['ts_start'],
                'task_end': inv['ts_end']
            }
            # Record per-task node specs for later cpu_count recompute
            task_node_info[task_id] = {
                'cpu_freq_mhz': node_meta_local.get('cpu_freq_mhz'),
                'cpu_cores': node_meta_local.get('cpu_cores'),
            }
            task_id += 1

    # Process CPU samples to generate fragments
    for node_name, node_data in all_node_data.items():
        node_meta = node_data['node_meta']
        proc_cpu_data = node_data['proc_cpu']
        proc_metrics = node_data.get('proc_metrics', [])

        # Group CPU samples by PID
        cpu_by_pid = defaultdict(list)
        for sample in proc_cpu_data:
            cpu_by_pid[sample['pid']].append(sample)

        # Group proc_metrics first snapshots per PID (for head-gap synthesis)
        first_snapshot = {}
        if proc_metrics:
            for snap in proc_metrics:
                pid = snap.get('pid')
                ts = snap.get('ts_ms')
                if pid is None or ts is None:
                    continue
                # Keep the earliest snapshot per PID
                if pid not in first_snapshot or ts < first_snapshot[pid]['ts_ms']:
                    first_snapshot[pid] = snap

        for pid, cpu_samples in cpu_by_pid.items():
            if pid not in pid_to_task_info:
                continue

            task_info = pid_to_task_info[pid]
            task_id = task_info['task_id']
            task_start = task_info['task_start']
            task_end = task_info['task_end']

            # Filter samples to task execution window (by sample ts)
            task_cpu_samples = [
                sample for sample in cpu_samples
                if task_start <= sample['ts_ms'] <= task_end
            ]

            # Optionally synthesize a head fragment later using the first proc_cpu interval
            # (more conservative: uses the first observed cores and respects cpu_cores cap).

            if not task_cpu_samples:
                # Generate synthetic fragment for tasks without CPU data
                task_duration = task_end - task_start
                task_row = tasks_df[tasks_df['id'] == task_id].iloc[0]

                fragment = {
                    'id': task_id,
                    'duration': task_duration,
                    'cpu_count': 1,
                    'cpu_usage': task_row['cpu_capacity'] * 0.5  # Assume 50% utilization
                }
                fragments.append(fragment)
                continue

            # Get task info for CPU capacity calculation
            task_row = tasks_df[tasks_df['id'] == task_id].iloc[0]
            task_cpu_capacity = task_row['cpu_capacity']

            # Sort samples by timestamp
            task_cpu_samples.sort(key=lambda x: x['ts_ms'])

            # Synthesize head fragment using first proc_cpu cores (clamped), covering [task_start, first_window_start)
            first_sample = task_cpu_samples[0]
            dt0 = int(first_sample.get('dt_ms', 0)) if first_sample.get('dt_ms') is not None else 0
            if dt0 > 0:
                first_ts = int(first_sample['ts_ms'])
                first_win_start = first_ts - dt0
                head_duration = max(0, first_win_start - task_start)
                if head_duration > 0:
                    first_cores = max(0.0, float(first_sample['cpu_ms']) / float(dt0))
                    first_cores = min(first_cores, float(node_meta.get('cpu_cores', first_cores)))
                    head_mhz = max(first_cores * _safe_freq_mhz(node_meta), essential_min_mhz)
                    fragments.append({'id': task_id, 'duration': int(head_duration), 'cpu_usage': float(head_mhz)})

            # Then append fragments for each proc_cpu interval (clip first interval to its own window start)
            for i, sample in enumerate(task_cpu_samples):
                dt = int(sample.get('dt_ms', 0)) if sample.get('dt_ms') is not None else 0
                if dt <= 0:
                    # Fallback to ts diff if available
                    if i == 0:
                        continue
                    prev_sample = task_cpu_samples[i - 1]
                    dt = int(sample['ts_ms'] - prev_sample['ts_ms'])
                    if dt <= 0:
                        continue

                win_start = sample['ts_ms'] - dt
                # For the first interval, ensure we don't overlap the synthesized head fragment
                clip_start = max(task_start, win_start)
                duration = int(sample['ts_ms'] - clip_start)
                if duration <= 0:
                    continue
                # Proportionally adjust cpu_ms if clipped
                cpu_ms_adj = sample['cpu_ms'] * (duration / dt) if duration != dt else sample['cpu_ms']

                # Calculate CPU usage and clamp to available cores
                cores_used = max(0.0, float(cpu_ms_adj) / float(duration))
                cores_used = min(cores_used, float(node_meta.get('cpu_cores', cores_used)))
                avg_mhz_demand = max(cores_used * _safe_freq_mhz(node_meta), essential_min_mhz)

                fragments.append({'id': task_id, 'duration': int(duration), 'cpu_usage': float(avg_mhz_demand)})
                # track peak per task
                prev_peak = peak_by_task.get(task_id)
                if prev_peak is None or avg_mhz_demand > prev_peak:
                    peak_by_task[task_id] = float(avg_mhz_demand)

    # Ensure all tasks have at least one fragment
    tasks_with_fragments = set(f['id'] for f in fragments)
    all_task_ids = set(tasks_df['id'])
    missing_task_ids = all_task_ids - tasks_with_fragments

    if missing_task_ids:
        print(f"Adding synthetic fragments for {len(missing_task_ids)} tasks without CPU data")
        for task_id in missing_task_ids:
            task_row = tasks_df[tasks_df['id'] == task_id].iloc[0]

            # Create a single fragment covering the entire task duration
            # cpu_usage should be average MHz demand
            total_capacity_mhz = task_row['cpu_capacity']

            fragment = {
                'id': task_id,
                'duration': task_row['duration'],
                'cpu_usage': total_capacity_mhz * 0.5  # Assume 50% of total capacity as average
            }
            fragments.append(fragment)

    fragments_df = pd.DataFrame(fragments)

    # Ensure correct data types for OpenDC compatibility (matching FRAGMENT_SCHEMA_V2)
    fragments_df['id'] = fragments_df['id'].astype('int32')           # INT32
    fragments_df['duration'] = fragments_df['duration'].astype('int64')  # INT64
    fragments_df['cpu_usage'] = fragments_df['cpu_usage'].astype('float64')  # DOUBLE (float64 = double)

    return fragments_df, peak_by_task, task_node_info

def main():
    parser = argparse.ArgumentParser(description='Convert Tracekit traces to OpenDC format')
    parser.add_argument('--input', required=True, help='Input directory containing node logs (e.g., logs/20250825T1325000)')
    parser.add_argument('--output', required=True, help='Output directory for OpenDC files')
    args = parser.parse_args()
    
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all node directories
    node_dirs = []
    if input_dir.exists():
        # Look for direct node directory
        if (input_dir / "node_meta.json").exists():
            node_dirs.append(input_dir)
        else:
            # Look for subdirectories with node data
            for subdir in input_dir.iterdir():
                if subdir.is_dir() and (subdir / "node_meta.json").exists():
                    node_dirs.append(subdir)
    
    if not node_dirs:
        print(f"No valid node directories found in {input_dir}")
        return
    
    print(f"Found {len(node_dirs)} node directories")
    
    # Load data from all nodes
    all_node_data = {}
    for node_dir in node_dirs:
        node_name = node_dir.name if node_dir.name != input_dir.name else "single_node"
        print(f"Loading data from {node_dir}")
        all_node_data[node_name] = load_node_data(node_dir)
    
    # Generate Tasks
    print("Generating Tasks...")
    tasks_df = generate_tasks(all_node_data)
    print(f"Generated {len(tasks_df)} tasks")
    
    # Generate Fragments
    print("Generating Fragments...")
    fragments_df, _peak_by_task, task_node_info = generate_fragments(all_node_data, tasks_df)
    print(f"Generated {len(fragments_df)} fragments")

    # Recompute task cpu_capacity/cpu_count using fragments P95 of cpu_usage (MHz)
    p95_series = fragments_df.groupby('id')['cpu_usage'].quantile(0.95)
    tasks_df = tasks_df.merge(p95_series.rename('p95_mhz'), left_on='id', right_index=True, how='left')
    # Update cpu_capacity to P95 (fallback to existing if missing)
    tasks_df['cpu_capacity'] = tasks_df['p95_mhz'].fillna(tasks_df['cpu_capacity'])

    # Derive cpu_count = ceil(cpu_capacity / node_freq_mhz), clamped to node cores
    def _derive_count(row):
        node_info = task_node_info.get(int(row['id']), {})
        freq = float(node_info.get('cpu_freq_mhz') or np.nan)
        cores = int(node_info.get('cpu_cores') or 0)
        if not np.isfinite(freq) or freq <= 0:
            return int(row['cpu_count'])
        count = int(math.ceil(float(row['cpu_capacity']) / freq))
        if cores > 0:
            count = min(count, cores)
        return max(1, count)

    tasks_df['cpu_count'] = tasks_df.apply(_derive_count, axis=1).astype('int32')
    tasks_df.drop(columns=['p95_mhz'], inplace=True)

    # Save to parquet files with explicit required schemas
    tasks_file = output_dir / "tasks.parquet"
    fragments_file = output_dir / "fragments.parquet"

    # Define OpenDC-compatible schemas with required fields
    tasks_schema = pa.schema([
        pa.field('id', pa.int32(), nullable=False),                    # required INT32
        pa.field('submission_time', pa.int64(), nullable=False),       # required INT64 (timestamp)
        pa.field('duration', pa.int64(), nullable=False),              # required INT64
        pa.field('cpu_count', pa.int32(), nullable=False),             # required INT32
        pa.field('cpu_capacity', pa.float64(), nullable=False),        # required DOUBLE
        pa.field('mem_capacity', pa.int64(), nullable=False),          # required INT64
    ])

    fragments_schema = pa.schema([
        pa.field('id', pa.int32(), nullable=False),                    # required INT32
        pa.field('duration', pa.int64(), nullable=False),              # required INT64
        pa.field('cpu_usage', pa.float64(), nullable=False),           # required DOUBLE
    ])

    # Convert to PyArrow tables with explicit schemas
    tasks_table = pa.Table.from_pandas(tasks_df, schema=tasks_schema, preserve_index=False)
    fragments_table = pa.Table.from_pandas(fragments_df, schema=fragments_schema, preserve_index=False)

    # Write parquet files
    pq.write_table(tasks_table, tasks_file)
    pq.write_table(fragments_table, fragments_file)
    
    print(f"Saved tasks to {tasks_file}")
    print(f"Saved fragments to {fragments_file}")
    
    # Print summary statistics
    print("\n=== Summary ===")
    print(f"Total tasks: {len(tasks_df)}")
    print(f"Total fragments: {len(fragments_df)}")
    print(f"Task duration range: {tasks_df['duration'].min():.0f} - {tasks_df['duration'].max():.0f} ms")
    print(f"CPU capacity range: {tasks_df['cpu_capacity'].min():.1f} - {tasks_df['cpu_capacity'].max():.1f} MHz")
    print(f"Memory capacity range: {tasks_df['mem_capacity'].min()} - {tasks_df['mem_capacity'].max()} KB ({tasks_df['mem_capacity'].min()/1024:.1f} - {tasks_df['mem_capacity'].max()/1024:.1f} MB)")

if __name__ == "__main__":
    main()
