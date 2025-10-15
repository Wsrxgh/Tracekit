# Tracekit

A comprehensive cloud application tracing and performance monitoring framework designed for collecting detailed execution traces compatible with OpenDC simulator format.
## Quick Start

### 1) Preparation (new VM)
- Common (all nodes):
  - OS packages: `sudo apt update && sudo apt install -y jq python3 python3-pip redis-tools`
  - Clone repo to `~/Tracekit` and prepare a shared `run_id.env` (same on all nodes)
- Controller (controller node):
  - Install and enable Redis server: `sudo apt install -y redis-server && sudo systemctl enable --now redis-server`
  - Harden redis.conf (ensure exactly one requirepass and bind 0.0.0.0), then restart:
    ```bash
    sudo cp /etc/redis/redis.conf /etc/redis/redis.conf.bak.$(date +%s)
    sudo sed -i -e '/^\s*#\s*requirepass\b/d' -e '/^\s*requirepass\b/d' /etc/redis/redis.conf
    echo 'requirepass Wsr123' | sudo tee -a /etc/redis/redis.conf >/dev/null
    sudo sed -i 's/^#\?bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
    sudo systemctl restart redis-server
    ```
  - Verify behavior (unauth should be NOAUTH; with password should be PONG):
    ```bash
    redis-cli -h <controller_ip> -p 6379 ping
    redis-cli -a 'Wsr123' -h <controller_ip> -p 6379 ping
    ```
  - Open firewall for 6379 if needed (ufw/security group)
- Workers (cloud1..N):
  - Install ffmpeg/ffprobe: `sudo apt install -y ffmpeg`
  - Python packages: `python3 -m pip install -r tools/scheduler/requirements.txt`  (psutil is included)
  - Optional time sync client: `sudo apt install -y chrony`（或确保 systemd-timesyncd 正常）
  - Verify connectivity (both unauth and auth):
    ```bash
    redis-cli -h <controller_ip> -p 6379 ping         # expect: (error) NOAUTH Authentication required.
    redis-cli -a 'Wsr123' -h <controller_ip> -p 6379 ping  # expect: PONG
    ```
  - Ensure `inputs/ffmpeg` 可访问（如无共享存储需同步到各 worker）

### 2) Test flow (summary)
- On ALL workers（先采集器，后 worker；shared 模式建议 sudo 运行 worker）
  1) 清理上次运行产物，启动采集器（Python + PID 白名单）
  2) 启动 worker（推荐 shared 模式 + allocation-ratio 适度超分配）
- On controller（cloud0）
  3) 启动 central scheduler（可选 weigher）并投递任务（dispatcher 支持 mix/total/seed）
- 收尾
  4) 各 worker 停止采集并解析（make parse）；可选导出 OpenDC
- 详细命令见下方“Complete test flow（共享公平）”。

### 3) Parameters and defaults (summary)
- 常用运行级变量
  - RUN_ID：必须，跨节点统一；通过 `run_id.env` 分发
  - NODE_ID：默认主机名，通常不需要手动设置
  - STAGE：示例采用 `cloud`，不强制
- Worker（tools/scheduler/worker.py）
  - `--outputs=outputs`, `--redis=redis://localhost:6379/0` (with password: `redis://<controller_ip>:6379/0?password=<pass>`)
  - `--parallel=0`（仅容量模式），`--allocation-ratio=1.0`，`--capacity-units=0`（默认按 ratio×逻辑核数）
  - `--cpu-binding=exclusive`（绑核；如需公平共享用 `shared`），`--cpuweight-per-vcpu=100`
  - `--reset-capacity`（默认关），`--clear-queue`（默认关）
  - 一般不需要手动设：`--capacity-units`（让程序按 ratio 计算即可）、`--slots-key`
- Central（tools/scheduler/scheduler_central.py）
  - `--redis`，`--weigher=""|instances|vcpu`，`--weigher-order=min|max（默认 min）`
- Dispatcher（tools/scheduler/dispatcher.py）
  - `--inputs`（必填），`--outputs=outputs`，`--mix`，`--total`，`--seed`
  - 中央队列：`--pending` + `--pending-mode=pulse（默认）`，`--pending-max=6`，`--pulse-size=10`，`--pulse-interval=300.0`
  - 滴灌模式：`--drip` + `--batch-size=1`，`--dribble-interval=1.0`，`--backlog-limit=1`
- Collector（tools/collect_sys.py，经环境变量）
  - `PROC_PID_DIR=logs/$RUN_ID/pids`（启用白名单，推荐）
  - `PROC_INTERVAL_MS=200`（默认；长任务可用 1000ms 降低开销）
  - `PROC_MATCH='^ffmpeg$|^ffprobe$'`，`USE_PY_COLLECT=1`


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
    --redis "redis://<controller_ip>:6379/0?password=Wsr123" \
    --parallel 4 \
    --allocation-ratio 1.0 \
    --cpu-binding exclusive
  ```
- 1.5× overprovisioning + shared fair-sharing (no core pinning) + cap-only dispatch
  ```bash
  # On each worker (do NOT pass --parallel)
  python3 tools/scheduler/worker.py \
    --outputs outputs \
    --redis "redis://<controller_ip>:6379/0?password=Wsr123" \
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
  --redis "redis://<controller_ip>:6379/0?password=Wsr123"
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
  --redis "redis://<controller_ip>:6379/0?password=Wsr123" \
  --weigher instances --weigher-order min \
  > "logs/$RUN_ID/central.log" 2>&1 &

# Enqueue 20 tasks (seed unchanged)
python3 tools/scheduler/dispatcher.py \
  --inputs inputs/ffmpeg \
  --outputs outputs \
  --pending --pending-mode pulse --pulse-size 10 --pulse-interval 100 \
  --mix "fast1080p=2,medium480p=3,hevc1080p=2,light1c=3" \
  --total 20 --seed 20250901 \
  --redis "redis://<controller_ip>:6379/0?password=Wsr123"
```

3) Stop collectors and parse (each worker)
```bash
cd ~/Tracekit && source run_id.env
make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
```



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
  --redis "redis://<controller_ip>:6379/0?password=Wsr123"
```
```bash
python3 tools/scheduler/scheduler_central.py \
  --weigher vcpu --weigher-order min \
  --redis "redis://<controller_ip>:6379/0?password=Wsr123"
```

Notes:
- In shared mode, if systemd-run is available, ffmpeg processes are launched in a scope with CPUWeight (and optional CPUQuota if provided by the task). If systemd-run is unavailable, tasks still run without cpuset binding (best-effort fair sharing by the OS).
- In exclusive mode, worker injects cpuset per task unless the task already specifies a cpuset (then it is honored).
- Central scheduler is unchanged. If no slot tokens exist (because workers didn’t register any by leaving --parallel unset/0), the scheduler automatically dispatches by remaining cap only.
- Dispatcher flags are unchanged; task cpu_units from profiles keep their meaning in both modes.

---



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
# The script prints a summary and writes small_datacenter.json
```

Generated files:
- **`tasks.parquet`**: Task-level information (submission time, duration, CPU/memory requirements)
- **`fragments.parquet`**: Fine-grained resource usage over time

## Documentation

- [Scheduler Integration](tools/scheduler/README.md) - Minimal design overview; for end-to-end flow, use this README.

## Requirements

- Linux system
- Python 3.8+
- redis-tools (for quick connectivity checks)
- systemd (optional; for systemd-run CPUWeight/CPUQuota in shared mode)
- jq (optional; convenience for inspecting JSON files)
- vegeta (optional; for load generation if you choose to use it)

- Workers: install ffmpeg/ffprobe: `sudo apt install -y ffmpeg`
- Python packages for control plane and workers:
  - `python3 -m pip install -r tools/scheduler/requirements.txt`  # includes psutil
- Optional (for OpenDC export on the controller): `python3 -m pip install pandas numpy pyarrow`

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
