# Cloud-only Trace Collection Toolkit (App-agnostic)

This toolkit collects cloud-node traces in an app-agnostic way and standardizes them to CCTF. It separates generic collection (host/links/per-PID) from app-adapter traces (invocations), focusing on cloud-only multi-node setups.

## Quick start (single cloud VM)

Prereqs: Docker installed (for the app container); sysstat/ifstat/vmstat on host (make setup)

1) Install host tools (once)
   make setup

2) Start your app container (optional; default name 'svc')
   make run   # or start your own container; the collector is app-agnostic

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

Artifacts (latest RUN_ID under logs/<RUN_ID>/):
- events.jsonl (server app_events), events_client.jsonl
- placement_events.jsonl (start/stop), system_stats.jsonl (in_flight)
- resources.jsonl (CPU/MEM), links.jsonl (NIC rx/tx)
- run_meta.json, node_meta.json, module_inventory.json
- cctf/: nodes.json, links.json, invocations.jsonl, host_metrics.jsonl, link_metrics.jsonl,
         placement_events.jsonl, system_stats.jsonl, run_meta.json, module_inventory.json

Note: No automatic health check. The /work endpoint exists only for manual checks; real-* targets hit real endpoints only.

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


## Trace schema (minimal and sufficient)
This repo emits six record classes to support “replayable scheduling/evaluation + real-run comparison”:
1) run_meta: run_id, rate/duration, scen/img/git_sha/workers
2) node_inventory: node_meta.json (node_id, stage, cpu_cores, mem_mb)
3) module_inventory: module_id/name/type (written by tools/write_module_inventory.py)
4) placement_events.jsonl: ts, app_id, module_id, instance_id, node_id, event=start/stop (scale/migrate later)
5) app_events (events.jsonl): trace_id, span_id, parent_id, ts_enqueue/ts_start/ts_end,
   queue_time_ms, service_time_ms, rt_ms, module_id, instance_id, node, stage,
   method, path, bytes_in/out, cpu_time_ms, status
6) system_stats.jsonl: ts_ms, node_id, stage, metric=in_flight, value

CCTF outputs under logs/<RUN_ID>/cctf/ are ready for simulators.

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



### Step 4: Stop collection and parse (each VM separately)


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
```

### Outputs (files, fields, units)
- node_meta.json: {run_id, node, stage, host, iface, cpu_cores, mem_mb, cpu_model, cpu_freq_mhz(MHz)}
- link_meta.json: {iface, BW_bps(bits/s), PR_s(seconds|null)}
- resources.jsonl: {ts_ms(ms), cpu_util(%), mem_free_mb(MB)}
- links.jsonl: {ts_ms(ms), link, rx_Bps(bytes/s), tx_Bps(bytes/s)}
- proc_metrics.jsonl: {ts_ms(ms), pid, rss_kb(KB), utime(ticks), stime(ticks)}
- cctf/
  - nodes.json, links.json
  - host_metrics.jsonl, link_metrics.jsonl
  - invocations.jsonl (if present)
  - proc_metrics.jsonl (raw), proc_cpu.jsonl {ts_ms, pid, cpu_ms(ms)}, proc_rss.jsonl {ts_ms, pid, rss_kb}


### Step 3: Stop & parse (each VM)
```bash
make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
```
echo '{"a":1}' > /tmp/body.json
curl -X POST -H 'Content-Type: application/json' --data-binary @/tmp/body.json http://<endpoint_ip>:8080/json/validate
```

5) Stop collection and parse (each VM)
```bash
# Cloud
cd /home/cloud0_gxie/Tracekit && make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
# Edge
cd /home/edge0_gxie/Tracekit && make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
# Endpoint
cd /home/endpoint0_gxie/Tracekit && make stop-collect RUN_ID=$RUN_ID && make parse RUN_ID=$RUN_ID
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

