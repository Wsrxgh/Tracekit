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

## Quick Start (single node)

### 1. Start monitoring
```bash
make collect RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud
```

### 2. Run your application
```bash
# For HTTP services - use the FastAPI example
cd examples/fastapi_svc && make run

# For batch jobs - use adapters
tools/adapters/ffmpeg_wrapper.sh input.mp4 output.mp4

# For existing services - use log adapters
python3 tools/adapters/nginx_access_to_invocations.py --input access.log --output logs/test001/invocations.jsonl
```

### 3. Generate load (optional)
```bash
make load VM_IP=localhost RATE=50 DURATION=60s
```

### 4. Stop and parse
```bash
make stop-collect RUN_ID=test001
make parse RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud
```

---

## End-to-end multi-node trace collection test (current private test flow)
This section documents the exact steps currently used for my private tests with a controller node (cloud0) and two workers (cloud1, cloud2). Redis runs on cloud0 and REQUIRES PASSWORD. All nodes share the same repo layout.

Important: Redis requires auth. Always use URLs like `redis://:Wsr123@HOST:6379/0`.

1) Generate and distribute a RUN_ID (cloud0)
```bash
cd ~/Tracekit
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
scp run_id.env <cloud1_user>@192.168.133.3:~/Tracekit/
scp run_id.env <cloud2_user>@192.168.133.4:~/Tracekit/
```

2) Start collectors on workers (cloud1 & cloud2)
- Always `source run_id.env` on each worker.
- Only match ffmpeg/ffprobe to avoid noise; refresh PID set every tick.
```bash
# cloud1
cd ~/Tracekit && source run_id.env
PROC_SAMPLING=1 PROC_REFRESH=1 PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud VM_IP=192.168.133.2

# cloud2
cd ~/Tracekit && source run_id.env
PROC_SAMPLING=1 PROC_REFRESH=1 PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID NODE_ID=cloud2 STAGE=cloud VM_IP=192.168.133.2
```
Expected on each worker:
```
proc sampler started (mode=host, match='^ffmpeg$|^ffprobe$', interval=1s) → logs/$RUN_ID/proc_metrics.jsonl
```

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
