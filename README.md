# Tracekit

A comprehensive cloud application tracing and performance monitoring framework designed for collecting detailed execution traces compatible with OpenDC simulator format.

## Overview

Tracekit provides multi-layer observability for cloud applications:
- **Application-level**: Request/response tracing with timing and resource usage
- **Process-level**: Per-PID CPU and memory monitoring
- **System-level**: Host CPU, memory, and network metrics
- **Infrastructure**: Node topology and link characteristics

## Key Features

- 🔍 **Multi-layer monitoring**: From application requests down to system resources
- 📊 **OpenDC compatibility**: Generates traces suitable for datacenter simulation
- 🔌 **Flexible adapters**: Support for HTTP services, batch jobs, CLI tools
- 📈 **High-precision timing**: Millisecond-level timestamps for accurate analysis
- 🐳 **Container-aware**: Works with Docker containers and bare metal
- 🌐 **Multi-node**: Coordinate tracing across distributed deployments


---

## End-to-end multi-node trace collection test (current flow)
This section documents the steps with a controller node (cloud0) and two workers (cloud1, cloud2). Redis runs on cloud0 and REQUIRES PASSWORD. All nodes share the same repo layout.

Important: Redis requires auth. Always use URLs like `redis://:Wsr123@HOST:6379/0`.

PID whitelist sampling (now default with Python collector)
- By default we use the Python collector (USE_PY_COLLECT=1). If you set `PROC_PID_DIR=logs/$RUN_ID/pids` when starting collectors, it will only track PIDs listed in that directory (created by the ffmpeg wrapper). This avoids scanning /proc every tick and stabilizes sub-second sampling.
- When not set, the sampler falls back to regex scanning.
- How to enable:
  - The wrapper automatically creates/removes a sentinel file per ffmpeg PID under `logs/$RUN_ID/pids`. Start collectors with `PROC_PID_DIR=logs/$RUN_ID/pids` to enable whitelist mode.
- When to use:
  - Recommended for batch/worker-driven workloads (ffmpeg jobs) where workers launch target processes; yields lower dt_ms and minimal overhead.
  - For generic hosts where you cannot modify how processes are launched, keep regex scanning (no PROC_PID_DIR).

1) Generate and distribute a RUN_ID (cloud0)
```bash
cd ~/Tracekit
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
scp run_id.env <cloud1_user>@192.168.133.3:~/Tracekit/
scp run_id.env <cloud2_user>@192.168.133.4:~/Tracekit/
```

2) Start collectors on workers (cloud1 & cloud2)
- Always `source run_id.env` on each worker.
- Default is Python collector; whitelist mode recommended (PROC_PID_DIR=logs/$RUN_ID/pids) for stable ~200ms sampling.
```bash
# cloud1
cd ~/Tracekit && source run_id.env
USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids \
PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud VM_IP=192.168.133.2

# cloud2
cd ~/Tracekit && source run_id.env
USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids \
PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID NODE_ID=cloud2 STAGE=cloud VM_IP=192.168.133.2
```
Expected on each worker:
```
proc sampler started (mode=whitelist, interval=0.200s) → logs/$RUN_ID/proc_metrics.jsonl
```


### Full test flow (controller cloud0 + workers cloud1/cloud2)

1) Optional: prepare short input on cloud0
```bash
cd ~/Tracekit && mkdir -p inputs/ffmpeg_quick && bash -lc 'ls inputs/ffmpeg/*.mp4 | head -n 2 | xargs -I{} cp -a "{}" inputs/ffmpeg_quick/'
```

2) On each worker, stop old collectors and clean outputs
```bash
# cloud1
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 STOP_ALL=1 make stop-collect RUN_ID=$RUN_ID && rm -rf outputs/*
# cloud2
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 STOP_ALL=1 make stop-collect RUN_ID=$RUN_ID && rm -rf outputs/*
```

3) Start collectors (Python, whitelist, 200ms)
```bash
# cloud1
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 \
PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids PROC_MATCH='^ffmpeg$|^ffprobe$' \
make start-collect RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud VM_IP=192.168.133.2
# cloud2
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 \
PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids PROC_MATCH='^ffmpeg$|^ffprobe$' \
make start-collect RUN_ID=$RUN_ID NODE_ID=cloud2 STAGE=cloud VM_IP=192.168.133.2
```

4) Start workers
```bash
# cloud1
cd ~/Tracekit && source run_id.env && NODE_ID=cloud1 RUN_ID=$RUN_ID python3 tools/scheduler/worker.py --outputs outputs --parallel 1 --redis "redis://:Wsr123@192.168.133.2:6379/0"
# cloud2
cd ~/Tracekit && source run_id.env && NODE_ID=cloud2 RUN_ID=$RUN_ID python3 tools/scheduler/worker.py --outputs outputs --parallel 1 --redis "redis://:Wsr123@192.168.133.2:6379/0"
```

5) On controller cloud0: start central scheduler and dispatch tasks
```bash
cd ~/Tracekit && nohup python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0" > logs/scheduler_central.log 2>&1 &
cd ~/Tracekit && mkdir -p outputs && python3 tools/scheduler/dispatcher.py --inputs inputs/ffmpeg_quick --outputs outputs --policy rr3 --pending --pending-max 6 --batch-size 1 --dribble-interval 0.1 --redis "redis://:Wsr123@127.0.0.1:6379/0"
```

6) Stop collectors and parse on each worker when done
```bash
# cloud1
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 STOP_ALL=1 make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud
# cloud2
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 STOP_ALL=1 make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID NODE_ID=cloud2 STAGE=cloud
```

7) Optional: export OpenDC on cloud0
```bash
cd ~/Tracekit && python3 tools/export_opendc.py --input logs/$RUN_ID --output opendc_traces_$RUN_ID
```

Notes:
- The wrapper creates PID sentinels in `logs/$RUN_ID/pids`. Whitelist mode significantly stabilizes dt_ms.
- If you need to fallback to the shell collector, run with `USE_PY_COLLECT=0`.
- For very short-lived processes, consider setting `PROC_INTERVAL_MS=100` and/or enabling future inotify instant sampling (TBD).

3) Start workers to process tasks (cloud1 & cloud2)
- Pass RUN_ID so that ffmpeg wrapper writes events into logs/$RUN_ID.
```bash
# cloud1
cd ~/Tracekit && source run_id.env
NODE_ID=cloud1 RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs --parallel 1 \
  --redis "redis://:Wsr123@192.168.133.2:6379/0"

# cloud2
cd ~/Tracekit && source run_id.env
NODE_ID=cloud2 RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs --parallel 1 \
  --redis "redis://:Wsr123@192.168.133.2:6379/0"
```

4) Start central scheduler on controller (cloud0)
- Foreground (block terminal):
```bash
cd ~/Tracekit
python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0"
```
- Or background (recommended):
```bash
cd ~/Tracekit
nohup python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0" > logs/scheduler_central.log 2>&1 &
```

5) Dribble tasks from controller into global pending (FIFO)
- Inputs are under `inputs/ffmpeg` on cloud0. Ensure workers can access the same paths (sync if no shared storage).
- Pending queue limit is 6; submission interval defaults to 100ms; submission_time (ts_enqueue) is strictly increasing.
```bash
cd ~/Tracekit
python3 tools/scheduler/dispatcher.py \
  --inputs inputs/ffmpeg \
  --outputs outputs \
  --policy rr3 \
  --pending \
  --pending-max 6 \
  --batch-size 1 \
  --dribble-interval 0.1 \
  --redis "redis://:Wsr123@127.0.0.1:6379/0"
```

6) Stop collectors and parse (workers) after queues are empty
- Check on cloud0 `LLEN q:pending`, `LLEN q:cloud1`, `LLEN q:cloud2` are all 0.
```bash
# cloud1
cd ~/Tracekit && source run_id.env
make stop-collect RUN_ID=$RUN_ID
make parse RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud

# cloud2
cd ~/Tracekit && source run_id.env
make stop-collect RUN_ID=$RUN_ID
make parse RUN_ID=$RUN_ID NODE_ID=cloud2 STAGE=cloud
```
Artifacts under logs/$RUN_ID/cctf/: invocations.jsonl, proc_cpu.jsonl, proc_rss.jsonl, etc.

7) Export to OpenDC (cloud0, optional)
```bash
cd ~/Tracekit
python3 tools/export_opendc.py --input logs/$RUN_ID --output opendc_traces_$RUN_ID
```

Troubleshooting (private flow)
- `Authentication required` → Ensure Redis URL includes password `redis://:Wsr123@HOST:6379/0`.
- `Connection refused` on workers → On cloud0, Redis must listen on 0.0.0.0 and open 6379; then restart. Also verify URL is not defaulting to localhost.
- Dispatcher multiline commands must use `\` for line continuation; otherwise each `--flag` is treated as a separate command.
- If outputs/ already contains target files, ffmpeg may prompt; clean with `rm -rf outputs/*` on workers before tests.

## Output

The framework generates standardized traces in `logs/$RUN_ID/cctf/`:
- `invocations.jsonl`: Application-level task execution records
- `proc_cpu.jsonl`, `proc_rss.jsonl`: Process-level resource usage
- `host_metrics.jsonl`: System-level CPU/memory time series
- `nodes.json`, `links.json`: Infrastructure topology
- Additional files for placement events, network metrics, etc.

## OpenDC Integration

Convert collected traces to OpenDC format for datacenter simulation:

```bash
# Export traces to OpenDC format
python3 tools/export_opendc.py --input logs/YOUR_RUN_ID --output opendc_traces/

# Verify the exported files
python3 tools/verify_opendc.py --input opendc_traces/
```

Generated files:
- **`tasks.parquet`**: Task-level information (submission time, duration, CPU/memory requirements)
- **`fragments.parquet`**: Fine-grained resource usage over time

See `docs/README.md` for detailed format specifications.

## Documentation

- [Detailed Documentation](docs/README.md) - Complete setup and usage guide
- [FastAPI Example](examples/fastapi_svc/README.md) - HTTP service tracing
- [Scheduler Integration](tools/scheduler/README.md) - Batch job monitoring

## Requirements

- Linux system with standard monitoring tools (mpstat, vmstat, ifstat)
- Python 3.8+
- Docker (optional, for container monitoring)
- vegeta (optional, for load generation)

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
