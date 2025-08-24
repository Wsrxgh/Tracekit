# Cloud-only Trace Collection Toolkit (App-agnostic)

This toolkit collects cloud-node traces in an app-agnostic way and standardizes them to CCTF. It separates generic collection (host/links/per-PID) from app-adapter traces (invocations), focusing on cloud-only multi-node setups.

## Quick start (single cloud VM)

Prereqs: Docker installed (for the app container); sysstat/ifstat/vmstat on host (make setup)

1) Install host tools (once)
   make setup

2) Start your app container (optional; default name 'svc')
   make run   # This uses the example service under examples/fastapi_svc/. You can skip this; the collector is app-agnostic.

3) Start trace collection (generic, app-agnostic)
   make start-collect RUN_ID=$RUN_ID NODE_ID=cloud0 STAGE=cloud VM_IP=127.0.0.1 IFACE=lo PROC_SAMPLING=1 CONTAINER=svc
   # Options:
   #   IFACE=<nic> VM_IP=<peer-or-gateway-ip>  # to derive NIC and PR_s from ping
   #   PROC_MATCH='python|ffmpeg|onnxruntime|java|node|nginx|torchserve'  # per-PID filter
   #   PROC_REFRESH=1  # refresh PID set each second (for respawning processes)

4) Stop & parse
   make stop-collect RUN_ID=$RUN_ID
   make parse RUN_ID=$RUN_ID

Note: RUN_ID is honored from environment. You may `echo "RUN_ID=..." > run_id.env` then `source run_id.env` before running make targets.



## What gets collected (generic)
- node_meta.json
  - run_id (string)
  - node (string), stage (string, default cloud), host (string), iface (string)
  - cpu_cores (int), mem_mb (int)
  - cpu_model (string), cpu_freq_mhz (int, MHz)
- link_meta.json
  - BW_bps (int, bits/s), PR_s (float or null, seconds)
- resources.jsonl (host CPU/MEM time series)
  - {ts_ms:int, cpu_util:float%} from mpstat; {ts_ms:int, mem_free_mb:int}
- links.jsonl (NIC time series)
  - {ts_ms:int, link:"<node>.nic", rx_Bps:int, tx_Bps:int}
- proc_metrics.jsonl (per-PID, optional but recommended)
  - {ts_ms:int, pid:int, rss_kb:int, utime:int, stime:int}  # utime/stime in ticks (CLK_TCK≈100)
- cctf/
  - nodes.json (copy of node identity + cpu_model/cpu_freq_mhz)
  - links.json (NIC edge with BW_bps/PR_s)
  - host_metrics.jsonl, link_metrics.jsonl (standardized)
  - invocations.jsonl (if present; from app adapter)
  - proc_metrics.jsonl, proc_cpu.jsonl, proc_rss.jsonl (if present)


### Generic vs. App-adapter layers
- Generic (always-on, app-agnostic): node_meta, link_meta, resources (cpu/mem), links (nic), per-PID proc_metrics
- App-adapter (provides task boundaries; choose one if needed):
  - HTTP/gRPC: access_log adapter (Nginx/Envoy) → invocations.jsonl
  - Batch/CLI (e.g., FFmpeg): lightweight wrapper → invocations.jsonl
  - Queue/Jobs: scheduler events export → invocations.jsonl
Note: You can run only the generic layer to get host/links/proc metrics; add an adapter later when you need per-task traces.




## Cloud-only multi-node workflow (how to run)

### Prerequisites
- N cloud VMs with time sync (chrony/ntp)
- Same codebase on all VMs (git clone or copy)
- Docker installed on all VMs (for your app container, optional)

### Step 1: Start application container (optional)
- The collector is app-agnostic. If you have an app container, start it now (default name 'svc').
- Otherwise you can still run generic collectors (host/links/proc) without an app.


### Step 2: Start trace collection (each cloud VM)

Set unified RUN_ID across all VMs (optional but recommended):
```bash
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
# copy to all VMs, then on each:
source run_id.env
```

Start collection on each VM (put variables AFTER make for highest precedence):
```bash
make start-collect RUN_ID=$RUN_ID NODE_ID=cloud0 STAGE=cloud VM_IP=<gateway_or_peer_ip> IFACE=<iface> \
  PROC_SAMPLING=1 CONTAINER=svc PROC_MATCH='python|ffmpeg|onnxruntime|java|node|nginx|torchserve' PROC_REFRESH=0
```
Notes:
- Start your app container first if needed (make run), then start-collect.
- IFACE should be the external NIC (e.g., ens2). If omitted, the script infers via `ip route get <VM_IP>`.
- The collector pre-stops any previous collectors for the same RUN_ID and uses robust stop (TERM→KILL).
- App-level events: if you use our FastAPI service, invocations are emitted automatically; otherwise, use adapters under tools/adapters/ to generate invocations.jsonl when needed.



### Step 3: Stop & parse (each cloud VM)
```bash
make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
```


### End-to-end checklist (multi-node, cloud-only)

Assumptions:
- Use one unified RUN_ID via run_id.env on all VMs
- Put variables AFTER make (highest precedence)
- Replace <gateway_or_peer_ip>/<iface> per VM (e.g., ens2)

0) Unify RUN_ID (once)
```bash
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
# distribute to all VMs
```

On each cloud VM (repeat per node):
```bash
source run_id.env
# clean any old collectors for all runs (optional)
for d in logs/*; do RUN_ID="$(basename "$d")" RUN_ID="$RUN_ID" bash tools/collect_sys.sh stop || true; done
# (optional) build & run your app container
make build && make stop || true
SCEN=cloud VM_IP=<this_vm_ip> make run || true
# start collection (variables after make)
make start-collect RUN_ID=$RUN_ID NODE_ID=<cloudX> STAGE=cloud VM_IP=<gateway_or_peer_ip> IFACE=<iface> \
  PROC_SAMPLING=1 CONTAINER=svc PROC_MATCH='python|ffmpeg|onnxruntime|java|node|nginx|torchserve' PROC_REFRESH=0
# quick check
jq . logs/$RUN_ID/node_meta.json | egrep '"node"|"stage"|"iface"'
### Artifacts and field reference (detailed)
Below documents every file the collector emits under logs/<RUN_ID>/ and its standardized counterparts under logs/<RUN_ID>/cctf/.

- node_meta.json (one per node)
  - run_id: string, this run id
  - node: string, logical node id you pass as NODE_ID (e.g., cloud0)
  - stage: string, role label (default "cloud")
  - host: string, hostname
  - iface: string, NIC name used for link measurements (e.g., ens2)
  - cpu_cores: int, number of CPU cores (nproc)
  - mem_mb: int, total memory in MB
  - cpu_model: string, CPU model name (best-effort)
  - cpu_freq_mhz: int, per-core max frequency in MHz (best-effort)

- link_meta.json (per node, best-effort link characteristics)
  - iface: string, NIC name
  - BW_bps: int, line rate in bits per second (from /sys/class/net/<iface>/speed × 1e6; may be 0 if unknown)
  - PR_s: float|null, one-way propagation delay in seconds approximated as ping RTT_min / 2 to VM_IP; may be null if VM_IP is loopback or ping is blocked. Not used by OpenDC Workload.

- cpu.log (raw mpstat 1s text), mem.log (raw vmstat -Sm -t 1s text), net.log (raw ifstat -t 1s text)
  - Raw collectors’ outputs for CPU/MEM/NIC. parse_sys.py transforms them into resources.jsonl and links.jsonl.

- resources.jsonl (host time series from parse)
  - { ts_ms: int ms, node: string, stage: string, cpu_util: float percent } (from mpstat)
  - { ts_ms: int ms, node: string, stage: string, mem_free_mb: int MB } (from vmstat)

- links.jsonl (NIC throughput from parse)
  - { ts_ms: int ms, node: string, stage: string, link: "<node>.nic", rx_Bps: int bytes/s, tx_Bps: int bytes/s }

- events.*.jsonl (per-process app events written by the sample app), events.jsonl (merged)
  - trace_id: string UUID of the request chain (for correlation; not required by OpenDC)
  - span_id: string UUID (short) for this service span (diagnostics; not required by OpenDC)
  - parent_id: string|null upstream span id if any (diagnostics)
  - module_id: string (logical module name; default "svc")
  - instance_id: string (container/pid identity suffix)
  - ts_enqueue: int ms when request arrived
  - ts_start: int ms when processing started
  - ts_end: int ms when processing ended
  - queue_time_ms: int (=ts_start - ts_enqueue)
  - service_time_ms: int (=ts_end - ts_start) → maps to OpenDC Tasks.duration
  - rt_ms: int (=ts_end - ts_enqueue)
  - cpu_time_ms: int (process CPU time measured inside the app; optional calibration)
  - method: string (HTTP method), path: string (route)
  - bytes_in: int, bytes_out: int (payload sizes)
  - status: int HTTP status
  - node, stage: labels; pid: int process id

- placement_events.jsonl (instance lifecycle)
  - { ts: int ms, app_id: string, module_id: string, instance_id: string, node_id: string, stage: string, event: "start"|"stop" }

- system_stats.jsonl (per-instance stats)
  - { ts_ms: int ms, node: string, stage: string, metric: "in_flight", value: int, instance_id: string }

- proc_metrics.jsonl (per-PID sampler; optional but recommended)
  - { ts_ms: int ms, pid: int, rss_kb: int KB, utime: int ticks, stime: int ticks }
  - utime/stime are cumulative CPU ticks (Linux CLK_TCK≈100):
    - cpu_ms over a 1s interval ≈ (Δutime + Δstime) × 1000 / CLK_TCK
  - rss_kb is resident set size (KB). Use peak/P95 in a task window for mem_capacity.

cctf/ (standardized for simulators)
- nodes.json: [{ node_id, stage, cpu_cores, mem_mb, cpu_model, cpu_freq_mhz }]
- links.json: [{ u: node_id, v: "<node_id>.net", BW_bps, PR_s }]
- host_metrics.jsonl: standardized resources (cpu_util/mem_free_mb)
- link_metrics.jsonl: standardized links (rx_Bps/tx_Bps)
- invocations.jsonl: copy of merged events.jsonl
- proc_metrics.jsonl: copy of per-PID raw series
- proc_cpu.jsonl: { ts_ms, pid, cpu_ms } derived via Δticks and CLK_TCK
- proc_rss.jsonl: { ts_ms, pid, rss_kb } extracted from proc_metrics
- placement_events.jsonl, system_stats.jsonl: copied snapshots

OpenDC mapping (for later export)
- Tasks: id (e.g., node:span_id), submission_time=ts_enqueue, duration=service_time_ms,
  cpu_count=1, cpu_capacity≈cpu_freq_mhz×(cpu_time_ms/duration) or a calibrated constant,
  mem_capacity≈peak_or_P95(rss_kb_window)/1024 (MB)
- Fragments: id, duration=collector interval (ms), cpu_count=1,
  cpu_usage≈TASK_CPU_CAPACITY×(cpu_ms/duration_ms) using cctf/proc_cpu.jsonl
- Network (links/link_metrics/link_meta) is not used by OpenDC Workload; keep for calibration only.

```





6) Quick checks
```bash
# stage/node/iface labels
jq . logs/$RUN_ID/node_meta.json | egrep '"node"|"stage"|"iface"'
# link labels per node
head -n 3 logs/$RUN_ID/links.jsonl
head -n 3 logs/$RUN_ID/cctf/link_metrics.jsonl
# cross-VM trace_id (grab on endpoint, then search on edge/cloud)
EP_TID=$(grep '"/json/validate"' /home/endpoint0_gxie/Tracekit/logs/$RUN_ID/events.*.jsonl | tail -n1 | jq -r .trace_id)
grep "$EP_TID" /home/edge0_gxie/Tracekit/logs/$RUN_ID/events.*.jsonl | tail -n2
grep "$EP_TID" /home/cloud0_gxie/Tracekit/logs/$RUN_ID/events.*.jsonl | tail -n2
# ensure collectors stopped
ps -ef | egrep 'mpstat 1|ifstat -i|vmstat -Sm -t 1' | grep -v grep || echo "all collectors stopped"
```

```bash
make stop-collect RUN_ID=$RUN_ID
make parse RUN_ID=$RUN_ID
```

Each VM produces:
- logs/$RUN_ID/events.jsonl (app_events with trace_id/span_id/parent_id across VMs)
- logs/$RUN_ID/cctf/ (standardized for simulators)

### Merge multi-VM CCTF (optional)
Collect logs/$RUN_ID/cctf/ from all nodes onto one machine and merge:
- nodes.json, links.json: array merge + dedup
- *.jsonl files: concatenate + time-sort
- Result: unified_cctf/$RUN_ID ready for simulator replay

## Troubleshooting (quick)
- RUN_ID mismatch at parse: ensure start/stop/parse share the same RUN_ID
- Port 8080 busy: make stop or adjust -p hostPort:8080
- Host tools missing: make setup (installs vegeta/sysstat/ifstat)
- No per-PID lines: check logs/<RUN>/procmon.err; tune CONTAINER/PROC_MATCH/PROC_REFRESH

## Notes
- Generic vs App-adapter layers are decoupled. You can run the generic collector alone.
- cctf/ outputs can be consumed by simulators or converted to OpenDC later.

