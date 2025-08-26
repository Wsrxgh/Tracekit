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
from pathlib import Path
from collections import defaultdict
import glob
import pyarrow as pa
import pyarrow.parquet as pq

def load_node_data(log_dir):
    """Load all trace data from a single node directory"""
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
    
    # Load process CPU usage
    proc_cpu = []
    cpu_file = node_dir / "cctf" / "proc_cpu.jsonl"
    if cpu_file.exists():
        with open(cpu_file) as f:
            for line in f:
                if line.strip():
                    proc_cpu.append(json.loads(line))
    
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
        'proc_rss': proc_rss
    }

def calculate_cpu_requirements(invocation, node_meta, proc_cpu_data):
    """Calculate CPU count and capacity for a task based on actual CPU usage

    Returns: (cpu_count, cpu_capacity_per_core)
    """
    pid = invocation['pid']
    task_start = invocation['ts_start']
    task_end = invocation['ts_end']
    task_duration_ms = task_end - task_start

    # Find CPU samples for this PID during task execution
    task_cpu_samples = [
        sample for sample in proc_cpu_data
        if sample['pid'] == pid and task_start <= sample['ts_ms'] <= task_end
    ]

    if not task_cpu_samples:
        # Fallback: assume single core, moderate usage
        return 1, node_meta['cpu_freq_mhz'] * 0.5

    # Calculate peak CPU usage to determine core count
    peak_cpu_ms_per_second = max(sample['cpu_ms'] for sample in task_cpu_samples)

    # Determine how many cores were actually used
    # 1000ms/s = 1 full core, >1000ms/s = multiple cores
    cores_used = max(1, int(peak_cpu_ms_per_second / 1000) + (1 if peak_cpu_ms_per_second % 1000 > 500 else 0))
    cores_used = min(cores_used, node_meta['cpu_cores'])  # Can't exceed available cores

    # Calculate total CPU time used during task execution
    total_cpu_ms = sum(sample['cpu_ms'] for sample in task_cpu_samples)

    # Calculate average utilization per core
    if task_duration_ms > 0 and cores_used > 0:
        avg_utilization_per_core = (total_cpu_ms / task_duration_ms) / cores_used
        cpu_capacity_per_core = node_meta['cpu_freq_mhz'] * min(avg_utilization_per_core, 1.0)
    else:
        cpu_capacity_per_core = node_meta['cpu_freq_mhz'] * 0.1

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
    """Generate OpenDC Fragments dataframe"""
    fragments = []

    # Create mapping from PID to task_id and task info
    pid_to_task_info = {}
    task_id = 1

    for node_name, node_data in all_node_data.items():
        for inv in node_data['invocations']:
            pid_to_task_info[inv['pid']] = {
                'task_id': task_id,
                'task_start': inv['ts_start'],
                'task_end': inv['ts_end']
            }
            task_id += 1

    # Process CPU samples to generate fragments
    for node_name, node_data in all_node_data.items():
        node_meta = node_data['node_meta']
        proc_cpu_data = node_data['proc_cpu']

        # Group CPU samples by PID
        cpu_by_pid = defaultdict(list)
        for sample in proc_cpu_data:
            cpu_by_pid[sample['pid']].append(sample)

        for pid, cpu_samples in cpu_by_pid.items():
            if pid not in pid_to_task_info:
                continue

            task_info = pid_to_task_info[pid]
            task_id = task_info['task_id']
            task_start = task_info['task_start']
            task_end = task_info['task_end']

            # Filter samples to task execution window
            task_cpu_samples = [
                sample for sample in cpu_samples
                if task_start <= sample['ts_ms'] <= task_end
            ]

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

            for i, sample in enumerate(task_cpu_samples):
                # Calculate duration (sampling interval)
                if i < len(task_cpu_samples) - 1:
                    duration = task_cpu_samples[i + 1]['ts_ms'] - sample['ts_ms']
                else:
                    duration = min(1000, task_end - sample['ts_ms'])  # Until task end or 1s

                if duration <= 0:
                    continue

                # Calculate CPU usage: average CPU demand during this fragment
                # cpu_ms is the CPU time used in this duration
                # Convert to equivalent cores, then to average MHz demand
                cores_used = sample['cpu_ms'] / duration  # equivalent cores
                avg_mhz_demand = cores_used * node_meta['cpu_freq_mhz']  # average MHz demand

                fragment = {
                    'id': task_id,
                    'duration': duration,  # milliseconds
                    'cpu_usage': max(avg_mhz_demand, 0.1)  # Average MHz demand during this fragment
                }
                fragments.append(fragment)

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

    return fragments_df

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
    fragments_df = generate_fragments(all_node_data, tasks_df)
    print(f"Generated {len(fragments_df)} fragments")
    
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
