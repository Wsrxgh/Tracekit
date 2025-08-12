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

Note: Health check uses /work, but real-* targets load only real endpoints.

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
- Deploy the same image on three VMs; set stage/node and logs separately:
  docker run -d -p 8080:8080 \
    -e NODE_ID=cloud0 -e STAGE=cloud -e LOG_PATH=/logs \
    -v $PWD/logs/cloud:/logs tunable-svc:0.1.0

  docker run -d -p 8080:8080 \
    -e NODE_ID=edge0 -e STAGE=edge -e LOG_PATH=/logs \
    -v $PWD/logs/edge:/logs tunable-svc:0.1.0

  docker run -d -p 8080:8080 \
    -e NODE_ID=ep0 -e STAGE=endpoint -e LOG_PATH=/logs \
    -v $PWD/logs/endpoint:/logs tunable-svc:0.1.0

- Optional multi-hop with real endpoints (per layer):
  - On endpoint VM:   DEFAULT_NEXT_URL=http://<edge_ip>:8080/blob/gzip
  - On edge VM:       DEFAULT_NEXT_URL=http://<cloud_ip>:8080/kv/set/k1
  - On cloud VM:      (unset)
- Or set X-Next-Url per request to control the chain dynamically.

Recommended practices for multi-VM:
- Time sync (chrony/ntp) on all VMs (enables better ordering/merge)
- Ensure TCP/8080 reachable between VMs (via host bridge as you planned)
- Use a shared RUN_ID across VMs when collecting/parsing for the same experiment

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
