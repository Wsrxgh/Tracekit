# Tracekit

A compact guide to install, run, and collect traces. This README focuses on:
1) Preparing new VMs (controller + workers)
2) End‑to‑end test flow (copy‑paste blocks) and full parameter reference
3) Output artifacts

## Install dependencies (Python)

- Minimal (scheduler/worker only):
  ```bash
  python3 -m pip install -r tools/scheduler/requirements.txt
  ```
- Full Tracekit tools (incl. OpenDC export utilities):
  ```bash
  python3 -m pip install -r requirements.txt
  ```

Notes
- ffmpeg and redis-server are OS packages (see Preparation), not pip packages.
- Use a virtual environment if preferred.

## 1) Preparation on a new VM

- Common (all nodes)
  - OS packages
    ```bash
    sudo apt update && sudo apt install -y jq python3 python3-pip redis-tools
    ```
  - Clone repo and create a shared RUN_ID file (distribute the same file to all nodes)
    ```bash
    git clone https://github.com/Wsrxgh/Tracekit.git ~/Tracekit
    cd ~/Tracekit
    echo "RUN_ID=YOUR_RUN_ID" > run_id.env
    ```
- Controller node (cloud0)
  - Install Redis and enable on boot
    ```bash
    sudo apt install -y redis-server && sudo systemctl enable --now redis-server
    ```
  - Secure redis.conf (single requirepass, bind all) and restart
    ```bash
    sudo cp /etc/redis/redis.conf /etc/redis/redis.conf.bak.$(date +%s)
    sudo sed -i -e '/^\s*#\s*requirepass\b/d' -e '/^\s*requirepass\b/d' /etc/redis/redis.conf
    echo 'requirepass 123456' | sudo tee -a /etc/redis/redis.conf >/dev/null
    sudo sed -i 's/^#\?bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
    sudo systemctl restart redis-server
    ```
  > Security note: The password '123456' in the examples is only a placeholder default for quick start. Please set a strong requirepass of your own choice in redis.conf, and remember to update all commands accordingly (both `redis-cli -a` and the `redis://...password=` URLs).

  - Verify connectivity (unauth should fail, auth should PONG)
    ```bash
    redis-cli -h <controller_ip> -p 6379 ping
    redis-cli -a '123456' -h <controller_ip> -p 6379 ping
    ```
- Worker nodes (cloud1..N)
  - Install ffmpeg/ffprobe and Python deps
    ```bash
    sudo apt install -y ffmpeg
    python3 -m pip install -r tools/scheduler/requirements.txt
    ```
  - Optional: time sync client (or ensure systemd-timesyncd/chrony is OK)
    ```bash
    sudo apt install -y chrony
    ```
  - Ensure inputs exist locally (or shared): `inputs/ffmpeg`


## 2) Complete test flow (copy‑paste)

Use the same RUN_ID on all machines:
```bash
cd ~/Tracekit && source run_id.env
```

- On EACH worker (collector first → then worker)
```bash
cd ~/Tracekit && source run_id.env
pkill -f tools/scheduler/worker.py || true; make stop-collect RUN_ID=$RUN_ID || true
sudo rm -rf outputs/* logs/$RUN_ID/pids || true; mkdir -p logs/$RUN_ID/pids
# Defaults: STAGE=cloud, PID whitelist on (logs/$RUN_ID/pids), USE_PY_COLLECT=1, PROC_MATCH='^ffmpeg$|^ffprobe$'
PROC_INTERVAL_MS=1000 VM_IP=<controller_ip> make start-collect RUN_ID=$RUN_ID
# Choose ONE of the following worker modes:
# Shared fair-sharing (no pinning), recommended for throughput:
sudo -E RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
  --outputs outputs --allocation-ratio 1.25 --cpu-binding shared \
  --reset-capacity --clear-queue \
  --redis "redis://<controller_ip>:6379/0?password=123456"
# OR Exclusive core pinning (strict isolation):
# sudo -E RUN_ID=$RUN_ID python3 tools/scheduler/worker.py \
#   --outputs outputs --allocation-ratio 1 --cpu-binding exclusive --parallel 1 \
#   --reset-capacity --clear-queue \
#   --redis "redis://<controller_ip>:6379/0?password=123456"
```

- On CONTROLLER (clean queues → start central → enqueue)
```bash
cd ~/Tracekit && source run_id.env
pkill -f tools/scheduler/scheduler_central.py || true; pkill -f tools/scheduler/dispatcher.py || true
redis-cli -a '123456' -h <controller_ip> -p 6379 DEL q:pending || true
for k in $(redis-cli -a '123456' -h <controller_ip> -p 6379 KEYS 'q:run.*'); do redis-cli -a '123456' -h <controller_ip> -p 6379 DEL "$k"; done
mkdir -p "logs/$RUN_ID"
# Weigher options (choose one):
#   empty (first-fit): pass no --weigher flags
#   instances/min: prefer fewer running tasks (balance)
#   instances/max: prefer more running tasks (pile on busy host)
nohup python3 tools/scheduler/scheduler_central.py \
  --redis "redis://<controller_ip>:6379/0?password=123456" \
  --weigher instances --weigher-order min \
  > "logs/$RUN_ID/central.log" 2>&1 &
# Enqueue tasks (mix/seed/total can be adjusted)
python3 tools/scheduler/dispatcher.py \
  --inputs inputs/ffmpeg --outputs outputs \
  --pending --pending-mode pulse --pulse-size 10 --pulse-interval 300 \
  --mix "medium480p=3,fast1080p=2,hevc1080p=2,light1c=3" \
  --total 100 --seed 20250901 \
  --redis "redis://<controller_ip>:6379/0?password=123456"
```

- Finish on EACH worker (stop + parse)
```bash
cd ~/Tracekit && source run_id.env
make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
# If outputs were created with sudo, you can chown for cleanup:
# sudo chown -R "$USER":"$USER" outputs logs/$RUN_ID || true
```

### Parameter reference (concise)
- Worker (tools/scheduler/worker.py)
  - `--outputs PATH` (default: outputs)
  - `--redis redis://<ip>:6379/0?password=...`
  - `--cpu-binding {shared,exclusive}`
    - shared: systemd-run scope with CPUWeight (fair share by cpu_units)
    - exclusive: cpuset pinning by cpu_units
  - `--allocation-ratio FLOAT` (capacity = floor(ratio × logical_cores))
  - `--parallel INT` (>0 enables slots; omit/0 = cap-only)
  - `--cpuweight-per-vcpu INT` (shared mode; default 100 → 1c=100,2c=200,4c=400)
  - `--reset-capacity`, `--clear-queue` (testing hygiene)
- Central scheduler (tools/scheduler/scheduler_central.py)
  - `--weigher ""|instances|vcpu` + `--weigher-order min|max` (optional)
- Dispatcher (tools/scheduler/dispatcher.py)
  - `--inputs PATH` `--outputs PATH` `--mix STR` `--total INT` `--seed INT`
  - Queueing: `--pending --pending-mode pulse --pulse-size N --pulse-interval SEC`
- Collector (tools/collect_sys.py via env)
  - Defaults: `USE_PY_COLLECT=1`, `STAGE=cloud`, `PROC_PID_DIR=logs/$RUN_ID/pids`, `PROC_MATCH='^ffmpeg$|^ffprobe$'`
  - Typical overrides: `PROC_INTERVAL_MS=1000`, `VM_IP=<controller_ip>`


## 3) Output

Traces are written to `logs/$RUN_ID/CTS/`:
- `invocations.jsonl` — per task: trace_id, pid, ts_enqueue, ts_start, ts_end
- `proc_metrics.jsonl` — per PID time series: ts_ms, dt_ms, cpu_ms, rss_kb
- `nodes.json` — node metadata (node_id, stage, cores, mem, cpu model/freq)
- `audit_report.md` — basic validation

Export to OpenDC (optional):
```bash
python3 tools/export_opendc.py --input logs/$RUN_ID --output opendc_traces/
```
Generated files:
- `tasks.parquet` — task-level info
- `fragments.parquet` — fine-grained usage over time
