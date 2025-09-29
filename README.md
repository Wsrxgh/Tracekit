# Tracekit

A comprehensive cloud application tracing and performance monitoring framework designed for collecting detailed execution traces compatible with OpenDC simulator format.

## Overview

Tracekit provides multi-layer observability for cloud applications:
- **Application-level**: Request/response tracing with timing and resource usage
- **Process-level**: Per-PID CPU and memory monitoring
- **System-level (deprecated)**: Host CPU/memory/network metrics are no longer produced in the core flow
- **Infrastructure**: Node topology (link metrics deprecated/not produced)

## Key Features

- 🔍 **Multi-layer monitoring**: From application requests down to system resources
- 📊 **OpenDC compatibility**: Generates traces suitable for datacenter simulation
- 🔌 **Flexible adapters**: Support for HTTP services, batch jobs, CLI tools
- 📈 **High-precision timing**: Millisecond-level timestamps for accurate analysis
- 🐳 **Container-aware**: Works with Docker containers and bare metal
- 🌐 **Multi-node**: Coordinate tracing across distributed deployments

## Adapter and application boundary (updated)

- Application (SUT): the real executable you run (here, system ffmpeg in /usr/bin/ffmpeg)
- Adapter (non-intrusive wrapper): tools/adapters/ffmpeg_wrapper.py (Python)
  - Launches ffmpeg, writes precise ts_start (from /proc starttime) and ts_end, and creates/removes PID sentinels under logs/$RUN_ID/pids for whitelist collection
  - Optionally uses systemd-run --scope to apply CPUQuota/CPUWeight from the first time slice (shared mode)
- Explicit app entry (optional): tools/apps/ffmpeg_app.py simply forwards args to system ffmpeg; provided to make the app boundary explicit in-repo
- Scheduling/orchestration: dispatcher.py, scheduler_central.py, worker.py (control plane, not part of the adapter)
- Contract between layers:
  - Worker/dispatcher → Adapter: pass TS_ENQUEUE (if available) and resource hints via ENV; Adapter handles timing and PID sentinels
  - Adapter → Collector: PID sentinels only; Collector samples /proc in whitelist mode (PROC_PID_DIR)



## Variables and placeholders used in commands

- RUN_ID: Shared identifier across all nodes for a test run. Put it in run_id.env and `source run_id.env` on every machine.
- <controller_ip>: The IP or DNS name of the controller node (cloud0) where Redis runs. Replace with your actual address, e.g., 10.0.0.5.
- NODE_ID: Optional. Defaults to the machine hostname for collectors and workers. Set explicitly only if you have a naming scheme.
- STAGE: Environment tag (e.g., cloud). Defaults to `cloud` in examples.
- PROC_PID_DIR: Enable PID whitelist mode (recommended): `logs/$RUN_ID/pids`.
- USE_PY_COLLECT: 1 to use Python collector (default), 0 to fallback to shell collector.

### Example topology (1 controller + 4 workers)
- Controller (cloud0): runs Redis, central scheduler, dispatcher
- Workers (cloud1..cloud4): each runs collector + worker; workers register slot tokens according to `--parallel`
- Flow: dispatcher → q:pending → central scheduler → q:<node> → worker executes ffmpeg; collectors write system/proc metrics

### New VM setup checklist
- Common (all nodes):
  - OS packages: `sudo apt update && sudo apt install -y jq python3 python3-pip redis-tools`  # systemd-run support is optional
  - Clone repo to `~/Tracekit` and ensure `run_id.env` present on each node
- Controller node (cloud0):
  - Install Redis server (if not present): `sudo apt install -y redis-server`
  - Configure `/etc/redis/redis.conf`:
    - `bind 0.0.0.0`
    - `requirepass Wsr123`
  - Restart and verify:
    - `sudo systemctl restart redis-server`
    - `redis-cli -a 'Wsr123' -h 127.0.0.1 -p 6379 ping` → PONG
  - Open firewall for 6379 if needed (ufw/sg)
- Workers:
  - Verify connectivity: `redis-cli -a 'Wsr123' -h <controller_ip> -p 6379 ping` → PONG
  - Ensure inputs directory exists and contains files matching controller paths (sync if no shared storage)


### Time synchronization (auto, best‑effort)
- Collectors attempt a best‑effort time sync at start using the public NTP pool (pool.ntp.org) when sudo is available.
- If no sudo privileges are available, the sync step is skipped silently; the system’s existing chrony/timesyncd continues to maintain time.
- Environment variables:
  - TIME_SYNC=1 (default on)
  - NTP_POOL=pool.ntp.org (override if you run an internal NTP server)
- Logs: each worker writes logs/$RUN_ID/timesync.log recording before/after UTC timestamps and client outputs.


### Concurrency and capacity (READ THIS)
- **parallel** (per worker): maximum number of tasks that can run concurrently on that node
- **cpu_units** (per task): CPU capacity required by a task; defaults from profiles:
  - fast1080p=2, medium480p=2, hevc1080p=4
- **capacity-units** (per worker): total CPU capacity of the node exposed to the scheduler (default = logical cores). Set explicitly to keep tests reproducible, e.g., --capacity-units 4
- **Dispatch rule**: a task is dispatched to a node only if BOTH are satisfied:
  - the node has at least 1 free concurrency slot (parallel)
  - the node has remaining capacity >= task.cpu_units
- On completion, the task returns 1 concurrency slot and cpu_units capacity to the node.

### Scheduling algorithm (Strict FIFO + First-Fit)
- **Strict FIFO**: only considers head of q:pending queue
- **First-Fit**: scans available nodes in stable order (sorted by node_id), dispatches to first feasible host
- **Head-of-line blocking**: if head task (e.g., hevc needing 4 units) cannot be placed on any node, it blocks the queue until resources become available
- **Non-blocking scan**: uses LLEN/LRANGE to snapshot available slot tokens, avoids spin on single-token BRPOP

### Mixed profiles and reproducibility
- Use --mix to specify ratios, --total for total number of tasks, and --seed for a reproducible sequence.
- Example: --mix "fast1080p=40,medium480p=40,hevc1080p=20" --total 5 --seed 20250901
- Built-in profiles:
  - **fast1080p**: 1920x1080, H.264, preset=fast, cpu_units=2, cpuset=0-1, vthreads=2
  - **medium480p**: 854x480, H.264, preset=medium, cpu_units=2, cpuset=0-1, vthreads=2
  - **hevc1080p**: 1920x1080, HEVC, preset=medium, cpu_units=4, vthreads=4 (no cpuset binding)

### Test inputs generator
- Use tools/generate_test_videos.py to synthesize 1080p/30fps inputs with controlled complexity (H.264 yuv420p, ~8 Mbps, GOP~90)
- Noise distribution: 1/3 no noise, 1/3 low noise, 1/3 medium noise across different patterns
- .gitignore ignores inputs/ffmpeg/


### CPU overprovisioning and CPU binding modes (NEW)

This release adds admission-side CPU overprovisioning and execution-side CPU binding modes while preserving the original scheduler.

Key flags on worker (tools/scheduler/worker.py):
- --allocation-ratio FLOAT
  - Overprovisioning factor for admission/capacity. Effective capacity units (cap) = capacity-units if explicitly set, otherwise floor(allocation-ratio × logical_cores).
  - Default: 1.0 (no overprovisioning).
- --cpu-binding {exclusive,shared}
  - exclusive: strict core binding (cpuset) per task based on cpu_units (baseline behavior).
  - shared: no cpuset; uses Linux CFS weight via systemd-run CPUWeight to share CPU proportionally to cpu_units.
  - Default: exclusive.
- --parallel INT
  - >0: use slot tokens + capacity gating (original behavior). 0 or not provided: CAP-ONLY mode (no slots); central scheduler dispatches purely by remaining cap:<node>.
  - Default: 0 (cap-only). For legacy behavior, set a positive --parallel.
- --capacity-units INT
  - Overrides capacity calculation if set (use with care; otherwise prefer --allocation-ratio).
- --cpuweight-per-vcpu INT
  - When cpu-binding=shared, sets CPUWeight = cpuweight-per-vcpu × cpu_units. Default: 100 (1c=100, 2c=200, 4c=400).

Recipes:
- Strict capacity + exclusive pinning (baseline)
  ```bash
  # On each worker (example values)
  python3 tools/scheduler/worker.py \
    --outputs outputs \
    --redis "redis://:Wsr123@<controller_ip>:6379/0" \
    --parallel 4 \
    --allocation-ratio 1.0 \
    --cpu-binding exclusive
  ```
- 1.5× overprovisioning + shared fair-sharing (no core pinning) + cap-only dispatch
  ```bash
  # On each worker (do NOT pass --parallel)
  python3 tools/scheduler/worker.py \
    --outputs outputs \
    --redis "redis://:Wsr123@<controller_ip>:6379/0" \
    --allocation-ratio 1.5 \
    --cpu-binding shared
  ```


### Robustness against stale Redis state (NEW)

To avoid occasional deadlocks caused by stale Redis keys left from previous runs:
- Worker startup now purges old slot tokens for itself from `slots:available` automatically.
- Optional flags on worker:
  - `--reset-capacity`: Force reset `cap:<node>` to the current computed capacity on startup (override stale values)
  - `--clear-queue`: Delete `q:<node>` on startup for a clean run (use only in testing)
- Central scheduler fallback: even if `slots:available` contains leftover tokens, the scheduler will fall back to capacity-only dispatch when no feasible token can be used. This prevents blocking on stale tokens.

### Complete test flow (Python Adapter, sudo workers, shared fair-sharing)

Use this when you want Max‑Min fairness (shared mode with dynamic CPUQuota via systemd), Python adapter precision, and a clean, repeatable run.

1) On ALL workers (start collectors first, then workers with sudo)
```bash
cd ~/Tracekit && source run_id.env
# Clean previous artifacts on this worker
USE_PY_COLLECT=1 STOP_ALL=1 make stop-collect RUN_ID=$RUN_ID || true; pkill -f tools/scheduler/worker.py || true
rm -rf outputs/* logs/$RUN_ID/pids || true; mkdir -p logs/$RUN_ID/pids

# Start collector in PID whitelist mode (recommended) at 1000ms
USE_PY_COLLECT=1 PROC_INTERVAL_MS=1000 \
  PROC_PID_DIR="logs/$RUN_ID/pids" PROC_MATCH='^ffmpeg$|^ffprobe$' \
  NODE_ID=$(hostname) STAGE=cloud VM_IP=<controller_ip> \
  make start-collect RUN_ID=$RUN_ID

# Start worker (shared mode + 1.25 overprovision). Use sudo so CPUQuota/CPUWeight can be applied.
sudo -E RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs \
  --allocation-ratio 1.25 \
  --cpu-binding shared \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"
# Optional for clean tests: add --reset-capacity and/or --clear-queue
```

2) On controller (clean queues, start central with weigher, then enqueue)
```bash
cd ~/Tracekit && source run_id.env
# Kill old control-plane processes
pkill -f tools/scheduler/scheduler_central.py || true; pkill -f tools/scheduler/dispatcher.py || true
# Clean Redis queues from last run
redis-cli -a 'Wsr123' -h <controller_ip> DEL q:pending || true
for k in $(redis-cli -a 'Wsr123' -h <controller_ip> KEYS 'q:run.*'); do redis-cli -a 'Wsr123' -h <controller_ip> DEL "$k"; done

# Start central with weigher=instances (prefer fewer running instances)
mkdir -p "logs/$RUN_ID"
nohup python3 tools/scheduler/scheduler_central.py \
  --redis "redis://:Wsr123@<controller_ip>:6379/0" \
  --weigher instances --weigher-order min \
  > "logs/$RUN_ID/central.log" 2>&1 &

# Enqueue 20 tasks (seed unchanged)
python3 tools/scheduler/dispatcher.py \
  --inputs inputs/ffmpeg \
  --outputs outputs \
  --pending --pending-mode pulse --pulse-size 10 --pulse-interval 100 \
  --mix "fast1080p=2,medium480p=3,hevc1080p=2,light1c=3" \
  --total 20 --seed 20250901 \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"
```

3) Stop collectors and parse (each worker)
```bash
cd ~/Tracekit && source run_id.env
make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
```

Parameters and defaults (summary):
- Worker (tools/scheduler/worker.py):
  - --outputs=outputs, --redis=redis://localhost:6379/0
  - --parallel=0 (cap-only), --allocation-ratio=1.0, --capacity-units=0 (use ratio×logical_cores)
  - --cpu-binding=exclusive (use shared for fair sharing), --cpuweight-per-vcpu=100
  - --reset-capacity (off), --clear-queue (off)
- Central (tools/scheduler/scheduler_central.py):
  - --redis required; --weigher="" (first-fit) | instances | vcpu; --weigher-order=min|max (default=min)
- Dispatcher (tools/scheduler/dispatcher.py):
  - --inputs required; --outputs=outputs; --mix; --total; --seed; --pending-mode=pulse; --pulse-size=10; --pulse-interval=100
- Collector (tools/collect_sys.py via env):
  - PROC_PID_DIR=logs/$RUN_ID/pids (whitelist on); PROC_INTERVAL_MS=200 (default; recommend 1000)
  - PROC_MATCH='^ffmpeg$|^ffprobe$'; USE_PY_COLLECT=1


### Host weigher (optional)

Central scheduler can optionally choose among multiple feasible hosts using a weigher instead of pure first-fit.

Flags:
- `--weigher`: "" (default first-fit), `instances`, or `vcpu`
  - `instances`: prefers hosts by the number of running tasks (run_count:<node>)
  - `vcpu`: prefers hosts by used vCPU = cap_total:<node> - cap:<node>
- `--weigher-order`: `min` or `max`
  - `min`: prefer smaller metric (e.g., fewer running tasks or lower used vCPU)
  - `max`: prefer larger metric

Notes:
- Only hosts that are FEASIBLE for the head task are considered (i.e., cap:<node> ≥ task.cpu_units and, in slots mode, the host must also have a slot token).
- Weigher affects tie-breaking across hosts; admission still obeys Strict FIFO on q:pending.

Examples:
```bash
python3 tools/scheduler/scheduler_central.py \
  --weigher instances --weigher-order min \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"
```
```bash
python3 tools/scheduler/scheduler_central.py \
  --weigher vcpu --weigher-order min \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"
```

Notes:
- In shared mode, if systemd-run is available, ffmpeg processes are launched in a scope with CPUWeight (and optional CPUQuota if provided by the task). If systemd-run is unavailable, tasks still run without cpuset binding (best-effort fair sharing by the OS).
- In exclusive mode, worker injects cpuset per task unless the task already specifies a cpuset (then it is honored).
- Central scheduler is unchanged. If no slot tokens exist (because workers didn’t register any by leaving --parallel unset/0), the scheduler automatically dispatches by remaining cap only.
- Dispatcher flags are unchanged; task cpu_units from profiles keep their meaning in both modes.

---

## [DEPRECATED] Legacy end-to-end multi-node trace collection test
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
PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID STAGE=cloud VM_IP=<controller_ip>

# cloud2
cd ~/Tracekit && source run_id.env
USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids \
PROC_MATCH='^ffmpeg$|^ffprobe$' make start-collect RUN_ID=$RUN_ID STAGE=cloud VM_IP=<controller_ip>
```
Expected on each worker:
```
proc sampler started (mode=whitelist, interval=0.200s) → logs/$RUN_ID/proc_metrics.jsonl
# During run:
ls logs/$RUN_ID/pids              # should list ffmpeg PIDs while running
tail -f logs/$RUN_ID/proc_metrics.jsonl  # should append continuously
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
make start-collect RUN_ID=$RUN_ID STAGE=cloud VM_IP=${CONTROLLER_IP}
# cloud2
cd ~/Tracekit && source run_id.env && USE_PY_COLLECT=1 PROC_SAMPLING=1 PROC_REFRESH=1 \
PROC_INTERVAL_MS=200 PROC_PID_DIR=logs/$RUN_ID/pids PROC_MATCH='^ffmpeg$|^ffprobe$' \
make start-collect RUN_ID=$RUN_ID STAGE=cloud VM_IP=${CONTROLLER_IP}
```

4) Start workers
```bash
# cloud1
cd ~/Tracekit && source run_id.env && RUN_ID=$RUN_ID python3 tools/scheduler/worker.py --outputs outputs --parallel 1 --capacity-units 4 --redis "redis://:Wsr123@${CONTROLLER_IP}:6379/0"
# cloud2
cd ~/Tracekit && source run_id.env && RUN_ID=$RUN_ID python3 tools/scheduler/worker.py --outputs outputs --parallel 1 --capacity-units 4 --redis "redis://:Wsr123@${CONTROLLER_IP}:6379/0"
```

5) On controller cloud0: start central scheduler and dispatch tasks
```bash
cd ~/Tracekit && nohup python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0" > logs/scheduler_central.log 2>&1 &
# Dispatch mixed profiles with reproducible sequence (example, 5 tasks):
cd ~/Tracekit && python3 tools/scheduler/dispatcher.py \
  --inputs inputs/ffmpeg \
  --outputs outputs \
  --policy rr3 \
  --pending --pending-max 6 --batch-size 1 --dribble-interval 0.1 \
  --mix "fast1080p=40,medium480p=40,hevc1080p=20" --total 5 --seed 20250901 \
  --redis "redis://:Wsr123@127.0.0.1:6379/0"
```

6) Stop collectors and parse on each worker when done
```bash
# cloud1
cd ~/Tracekit && source run_id.env && make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
# cloud2
cd ~/Tracekit && source run_id.env && make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
```

#### Cautions (read before running)
- Node naming consistency:
  - Workers default NODE_ID to hostname. Do not mix legacy NODE_ID values (cloud1/cloud2) with hostname-based workers in the same run.
  - Symptom: slots:available shows both old and new names; central scheduler dispatches to q:old_name but no worker is listening.
  - Fix (controller):
    ```bash
    redis-cli -a 'Wsr123' LRANGE slots:available 0 -1
    # If old queues exist, move tasks back to pending, e.g.:
    while [ "$(redis-cli -a 'Wsr123' LLEN q:cloud1)" -gt 0 ]; do redis-cli -a 'Wsr123' RPOPLPUSH q:cloud1 q:pending >/dev/null; done
    redis-cli -a 'Wsr123' DEL slots:available
    ```
  - Then restart central scheduler and workers.
- Central scheduler: keep a single instance
  - Start once per controller node. If you need to restart, first stop old ones:
    ```bash
    pkill -f tools/scheduler/scheduler_central.py || true
    nohup python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0" > logs/scheduler_central.log 2>&1 &
    ```
  - Check running instances: `pgrep -fl scheduler_central.py`
- Capacity got stuck (tasks not dispatched):
  - Check remaining capacity and slots:
    ```bash
    redis-cli -a 'Wsr123' GET cap:<hostname>
    redis-cli -a 'Wsr123' LRANGE slots:available 0 -1
    ```
  - If needed, reset capacity (example 4 units) and restart scheduler/workers:
    ```bash
    redis-cli -a 'Wsr123' SET cap:<hostname> 4
    pkill -f tools/scheduler/scheduler_central.py || true
    nohup python3 tools/scheduler/scheduler_central.py --redis "redis://:Wsr123@127.0.0.1:6379/0" > logs/scheduler_central.log 2>&1 &
    ```

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
RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs --parallel 1 \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"

# cloud2
cd ~/Tracekit && source run_id.env
RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs --parallel 1 \
  --redis "redis://:Wsr123@<controller_ip>:6379/0"
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
Artifacts under logs/$RUN_ID/CTS/: invocations.jsonl, proc_metrics.jsonl, nodes.json, audit_report.md.

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

The framework generates standardized traces in `logs/$RUN_ID/CTS/`:
- `invocations.jsonl`: Application-level task execution records (trace_id, pid, ts_enqueue, ts_start, ts_end)
- `proc_metrics.jsonl`: Process-level time series (ts_ms, pid, dt_ms, cpu_ms, rss_kb)
- `nodes.json`: Node metadata (node_id, stage, cpu_cores, mem_mb, cpu_model, cpu_freq_mhz)
- `audit_report.md`: Validation summary (field completeness, temporal consistency, cross-reference)
- Note: legacy network/link artifacts are deprecated and not produced: links.json/links.jsonl, link_meta.json, link_metrics.jsonl, system_stats.jsonl, placement_events.jsonl.

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
- [Scheduler Integration](tools/scheduler/README.md) - Batch job monitoring

## Requirements

- Linux system
- Python 3.8+
- redis-tools (for quick connectivity checks)
- systemd (optional; for systemd-run CPUWeight/CPUQuota in shared mode)
- jq (optional; convenience for inspecting JSON files)
- vegeta (optional; for load generation if you choose to use it)

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
