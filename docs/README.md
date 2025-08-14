# Trace Collection and Real Workload Replay - Runbook (Cloud VM -> Endpoint-Edge-Cloud)

This project turns a FastAPI service into a real-workload trace source, standardizes traces to CCTF, and supports one‑click local runs. It is ready to extend to endpoint‑edge‑cloud with minimal changes.

## Quick start (single VM, real endpoints)

Prereqs: Docker installed

1) Install host tools (vegeta/sysstat/ifstat, once)
   make setup

2) Build image (only after code changes)
   make build

3) One‑click lightweight runs (real endpoints)
   - make real-all     # run json→gzip→hash→kvset→kvget sequentially
   - Alternatives: make real-json | real-gzip | real-hash | real-kvset | real-kvget

Note: RUN_ID now respects environment override (Makefile uses `RUN_ID ?=`). You may `echo "RUN_ID=..." > run_id.env` then `source run_id.env` before make targets.

Defaults (override as needed):
- REAL_RATE=40 (req/s), REAL_DUR=20s, REAL_SIZE=8192 bytes
- Override e.g.: make real-json REAL_RATE=60 REAL_DUR=30s REAL_SIZE=16384

Artifacts (latest RUN_ID under logs/<RUN_ID>/):
- events.jsonl (server app_events), events_client.jsonl
- placement_events.jsonl (start/stop), system_stats.jsonl (in_flight)
- resources.jsonl (CPU/MEM), links.jsonl (NIC rx/tx)
- run_meta.json, node_meta.json, module_inventory.json
- cctf/: nodes.json, links.json, invocations.jsonl, host_metrics.jsonl, link_metrics.jsonl,
         placement_events.jsonl, system_stats.jsonl, run_meta.json, module_inventory.json

Note: No automatic health check. The /work endpoint exists only for manual checks; real-* targets hit real endpoints only.

## Real endpoints (business-like)
- POST /json/validate       # JSON parse/validate (returns 200 or 400)
- POST /blob/gzip           # binary gzip
- POST /blob/gunzip         # binary gunzip
- POST /hash/sha256         # returns hex digest
- POST /kv/set/{key}        # SQLite set
- GET  /kv/get/{key}        # SQLite get

Optional downstream forwarding (multi-hop real workload):
- Env DEFAULT_NEXT_URL=http://host:8080/<endpoint>
- Or per-request header: X-Next-Url: http://host:8080/<endpoint>
- If set, each endpoint forwards the locally processed payload to the next hop, with trace headers.
- Default is OFF. Single-node runs behave the same as before.

Baseline (synthetic) endpoint (kept for health/baseline, not used by real-*):
- /work?cpu_ms=..&resp_kb=..&call_url=.. (supports cascading via call_url)

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

## Extend to endpoint‑edge‑cloud (same image, three nodes)

### Prerequisites
- Three VMs with time sync (chrony/ntp) and mutual TCP/8080 reachability via host bridge
- Same codebase on all VMs (git clone or copy)
- Docker installed on all VMs

### Step 1: Start applications (one container per VM)

You can use scenarios to load per-role envs: `SCEN=cloud|edge|endpoint make run`.

Cloud VM:
```bash
SCEN=cloud VM_IP=<cloud_ip> make run
```

Edge VM:
```bash
SCEN=edge VM_IP=<edge_ip> make run
```

Endpoint VM:
```bash
SCEN=endpoint VM_IP=<endpoint_ip> make run
```

Optional manual check (any VM): `curl http://127.0.0.1:8080/work`

### Step 2: Start trace collection (each VM separately)

Set unified RUN_ID across all VMs (Makefile reads run_id.env and honors env):
```bash
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
# copy to all VMs, then on each:
source run_id.env
```

Start system-level collection on each VM (put variables AFTER make for highest precedence):
```bash
# Endpoint
make start-collect RUN_ID=$RUN_ID NODE_ID=ep0   STAGE=endpoint VM_IP=<endpoint_ip> IFACE=<iface>
# Edge
make start-collect RUN_ID=$RUN_ID NODE_ID=edge0 STAGE=edge     VM_IP=<edge_ip>     IFACE=<iface>
# Cloud
make start-collect RUN_ID=$RUN_ID NODE_ID=cloud0 STAGE=cloud   VM_IP=<cloud_ip>    IFACE=<iface>
```
Notes:
- Always start containers first (make run), then start-collect.
- IFACE should be the external NIC (e.g., ens2). If omitted, the script infers via `ip route get <VM_IP>`.
- The collector script pre-stops any previous collectors for the same RUN_ID and uses robust stop (TERM→KILL) to avoid residue.

Notes:
- App-level events are automatically collected by tracekit middleware.
- Request trace_id is now propagated via request.state injected by middleware, so cross-VM trace_id stays consistent while parent/child spans reflect hop relationships.

### Step 3: Trigger real workload chain

Example real chain: JSON validation (endpoint) → GZIP compression (edge) → KV storage (cloud)

From endpoint VM, send JSON request (will auto-forward if DEFAULT_NEXT_URL set):
```bash
echo '{"a":1}' > /tmp/body.json
curl -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/body.json http://<endpoint_ip>:8080/json/validate
```

Alternative (per-request control without DEFAULT_NEXT_URL):
```bash
curl -H 'X-Next-Url: http://<edge_ip>:8080/blob/gzip' \
  -X POST -H 'Content-Type: application/json' \
  --data-binary @/tmp/body.json http://<endpoint_ip>:8080/json/validate
```

### Step 4: Stop collection and parse (each VM separately)


### End-to-end checklist (copy-paste)

Assumptions:
- Use one unified RUN_ID via run_id.env on all VMs
- Put variables AFTER make (highest precedence)
- Replace <cloud_ip>/<edge_ip>/<endpoint_ip>/<iface> with your values (e.g., ens2)

0) Unify RUN_ID (once)
```bash
# On one VM (e.g., endpoint)
echo "RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)" > run_id.env
# Copy to the other two, then on each VM:
source run_id.env && echo $RUN_ID
```

1) Cloud (192.168.x.x)
```bash
cd /home/cloud0_gxie/Tracekit && source run_id.env
# clean any old collectors (all RUN_IDs)
for d in logs/*; do RUN_ID="$(basename "$d")" RUN_ID="$RUN_ID" bash tools/collect_sys.sh stop || true; done
pkill -f 'mpstat 1' || true; pkill -f 'ifstat -i ' || true; pkill -f 'vmstat -Sm -t 1' || true
# container
make build && make stop
SCEN=cloud VM_IP=<cloud_ip> make run
# start collection (variables after make)
make start-collect RUN_ID=$RUN_ID NODE_ID=cloud0 STAGE=cloud VM_IP=<cloud_ip> IFACE=<iface>
# quick check
jq . logs/$RUN_ID/node_meta.json | egrep '"node"|"stage"|"iface"'
```

2) Edge (192.168.x.x)
```bash
cd /home/edge0_gxie/Tracekit && source run_id.env
for d in logs/*; do RUN_ID="$(basename "$d")" RUN_ID="$RUN_ID" bash tools/collect_sys.sh stop || true; done
pkill -f 'mpstat 1' || true; pkill -f 'ifstat -i ' || true; pkill -f 'vmstat -Sm -t 1' || true
make build && make stop
SCEN=edge VM_IP=<edge_ip> make run
make start-collect RUN_ID=$RUN_ID NODE_ID=edge0 STAGE=edge VM_IP=<edge_ip> IFACE=<iface>
jq . logs/$RUN_ID/node_meta.json | egrep '"node"|"stage"|"iface"'
```

3) Endpoint (192.168.x.x)
```bash
cd /home/endpoint0_gxie/Tracekit && source run_id.env
for d in logs/*; do RUN_ID="$(basename "$d")" RUN_ID="$RUN_ID" bash tools/collect_sys.sh stop || true; done
pkill -f 'mpstat 1' || true; pkill -f 'ifstat -i ' || true; pkill -f 'vmstat -Sm -t 1' || true
make build && make stop
SCEN=endpoint VM_IP=<endpoint_ip> make run
make start-collect RUN_ID=$RUN_ID NODE_ID=ep0 STAGE=endpoint VM_IP=<endpoint_ip> IFACE=<iface>
jq . logs/$RUN_ID/node_meta.json | egrep '"node"|"stage"|"iface"'
```

4) Trigger chain (on endpoint)
```bash
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

### Step 5: Merge multi-VM CCTF (optional, future)

Collect all three logs/$RUN_ID/cctf/ directories to one machine and merge:
- nodes.json, links.json: array merge + dedup
- *.jsonl files: concatenate + time-sort
- Result: unified_cctf/ ready for simulator replay

(Merge script tools/merge_cctf.py to be added later)

## Back up before deleting this VM
Minimum:
- Code repository (push to remote Git, or tar.gz)
- CCTF artifacts: logs/<RUN_ID>/cctf/ (for replay)
Optional:
- Full logs/<RUN_ID>/ (raw + CCTF), Docker image (docker save)

## Troubleshooting
- service not ready during real-*:
  - docker logs svc; ensure app/tracekit.py is present in image (Dockerfile COPY app.py tracekit.py .)
- RUN_ID mismatch at parse:
  - Pass RUN_ID through make targets (already fixed); ensure start/stop/parse share the same RUN_ID
- Port 8080 busy:
  - make stop or remove the container; adjust -p hostPort:8080
- Vegeta not found:
  - make setup (installs vegeta)

## Notes and glossary
- “发压/压测” = send sustained requests using vegeta to simulate load
- Health check uses /work; real-* targets use real endpoints only
- Simulators: you can feed logs/<RUN_ID>/cctf into your chosen simulator; merging multi‑VM CCTF can be added later


## TODOs (next steps) and Goals

### Goals
- Short term (cloud only): collect real workload traces and standardize to CCTF; validate replay feasibility
- Mid term (endpoint-edge-cloud): run same image on 3 VMs with real multi-hop forwarding; collect per-VM CCTF and merge; replay end-to-end
- Long term: evaluate scheduling/placement strategies using replayed traces; compare with real runs

### TODOs (not yet done)
- [ ] Multi-VM merge utility: tools/merge_cctf.py to combine nodes/links and time-sort *.jsonl
- [ ] Real endpoints chain examples: docs/scenarios/real-chain.md with X-Next-Url examples
- [ ] Optional: add scale/migrate events to placement_events.jsonl when replicas change
- [ ] Optional: node_inventory: add CPU freq/MIPS and region/zone (env or metadata)
- [ ] Optional: Makefile target for real-chain (endpoint→edge→cloud) once DEFAULT_NEXT_URL is configured per VM
- [ ] Optional: Dockerfile CMD switch to JSON array form (signal handling best practice)
- [ ] Optional: Add OpenTelemetry path later (collector + normalizer) if needed for cross-language services

### Decisions captured
- Keep /work for health/baseline only; real-* targets use only real endpoints
- Trace collection decoupled into app/tracekit.py (middleware + lifecycle + in_flight + placement)
- Real endpoints support optional downstream via DEFAULT_NEXT_URL or X-Next-Url; default OFF for single-node parity
- CCTF minimal-and-sufficient set is the contract for simulators; parse_sys.py writes CCTF under logs/<RUN_ID>/cctf/
- Single image runs across endpoint/edge/cloud; differentiation via STAGE/NODE_ID and optional DEFAULT_NEXT_URL
