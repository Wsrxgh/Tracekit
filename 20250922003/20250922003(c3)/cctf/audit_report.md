# CCTF Audit Report
Node: vm0  |  Stage: cloud

## Summary
Invocations: 2
Proc metrics samples: 1370
Distinct PIDs (invocations): 2
Distinct PIDs (proc_metrics): 2
PID match rate: 100.00%

## Field completeness (missing counts / rate)
- invocations.trace_id: 0 (0.00%)
- invocations.ts_enqueue: 0 (0.00%)
- invocations.ts_start: 0 (0.00%)
- invocations.ts_end: 0 (0.00%)
- invocations.pid: 0 (0.00%)
- proc_metrics.ts_ms: 0 (0.00%)
- proc_metrics.pid: 0 (0.00%)
- proc_metrics.dt_ms: 0 (0.00%)
- proc_metrics.cpu_ms: 0 (0.00%)
- proc_metrics.rss_kb: 0 (0.00%)

## Temporal consistency
- invocations ts_enqueue ≤ ts_start ≤ ts_end violations: 0
- proc_metrics per-pid strictly increasing ts_ms violations: 0
- proc_metrics records with dt_ms < 0: 0

## Cross-reference
- invocations without matching proc_metrics PID: 0
