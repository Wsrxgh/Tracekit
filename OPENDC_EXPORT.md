# OpenDC Export Guide

This guide explains how to export Tracekit traces to OpenDC format for datacenter simulation.

## Quick Start

```bash
# 1. Collect traces (example)
make collect RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud
# ... run your workload ...
make stop-collect RUN_ID=test001
make parse RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud

# 2. Export to OpenDC format
make export-opendc RUN_ID=test001

# 3. Verify the output
ls opendc_traces/
# Should contain: tasks.parquet, fragments.parquet
```

## Manual Export

```bash
# Export specific run
python3 tools/export_opendc.py --input logs/YOUR_RUN_ID --output opendc_traces/

# Verify the exported files
python3 tools/verify_opendc.py --input opendc_traces/
```

## Output Files

### `tasks.parquet`
Task-level information with the following fields:
- `id`: Task unique identifier (int32)
- `submission_time`: Task submission time in epochMillis (int64)
- `duration`: Task duration in milliseconds (int64)
- `cpu_count`: Number of CPU cores used (int32)
- `cpu_capacity`: Total CPU capacity required in MHz (float64)
- `mem_capacity`: Memory capacity required in KB (int64)

### `fragments.parquet`
Fine-grained resource usage over time:
- `id`: Task ID that references tasks.parquet (int32)
- `duration`: Fragment duration in milliseconds (int64)
- `cpu_usage`: CPU usage during this fragment in MHz (float64)

## Data Mapping

| Tracekit Source | OpenDC Field | Description |
|-----------------|--------------|-------------|
| `invocations.jsonl` | Tasks table | Task boundaries and resource requirements |
| `proc_cpu.jsonl` | Fragments table | Fine-grained CPU usage over time |
| `proc_rss.jsonl` | Tasks.mem_capacity | Peak memory usage |
| `node_meta.json` | CPU calculations | Node specifications for capacity calculations |

## Example Usage in OpenDC

1. Upload the generated `tasks.parquet` and `fragments.parquet` to OpenDC
2. Configure your datacenter topology
3. Run simulations to analyze:
   - Resource utilization
   - Task scheduling efficiency
   - Infrastructure capacity planning
   - Performance under different configurations

## Troubleshooting

**Empty fragments**: Ensure your workload generates sufficient CPU activity and monitoring data is collected properly.

**Large CPU usage values**: This is normal for multi-core tasks. Values represent total MHz needed across all cores.

**Irregular fragment durations**: This reflects real system sampling intervals (1-3 seconds) and is expected.

For more details, see the main [README.md](README.md) and [docs/README.md](docs/README.md).
