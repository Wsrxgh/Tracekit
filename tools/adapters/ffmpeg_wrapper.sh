#!/usr/bin/env bash
# ffmpeg_wrapper.sh (adapter sample)
# Non-invasive example: wrap each ffmpeg invocation to emit an event before/after.
# NOT wired into any pipeline by default. Use when your app is not our FastAPI service.
#
# Usage:
#   RUN_ID=2025... NODE_ID=cloud0 STAGE=cloud \
#   ./tools/adapters/ffmpeg_wrapper.sh -i in.mp4 -vf scale=1280:720 -c:v libx264 out.mp4
#   # events will be appended to logs/$RUN_ID/invocations.jsonl
#
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_DIR="$ROOT/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
NODE_ID=${NODE_ID:-vm0}
STAGE=${STAGE:-cloud}

# find input/output media names (best-effort)
INPUT=""
OUTPUT=""
args=("$@")
for ((i=0;i<${#args[@]};i++)); do
  a="${args[$i]}"
  if [ "$a" = "-i" ] && [ $((i+1)) -lt ${#args[@]} ]; then INPUT="${args[$((i+1))]}"; fi
  if [[ "$a" != -* ]] && [ $i -gt 0 ]; then OUTPUT="$a"; fi
done

TS0=$(date +%s%3N)
BYTES_IN=0
BYTES_OUT=0
if [ -n "$INPUT" ] && [ -f "$INPUT" ]; then BYTES_IN=$(stat -c %s "$INPUT" 2>/dev/null || echo 0); fi

# run ffmpeg
set +e
ffmpeg "$@"
RC=$?
set -e
TS1=$(date +%s%3N)
if [ -n "$OUTPUT" ] && [ -f "$OUTPUT" ]; then BYTES_OUT=$(stat -c %s "$OUTPUT" 2>/dev/null || echo 0); fi

# write record
cat >> "$LOG_DIR/invocations.jsonl" <<JSON
{"trace_id": null, "span_id": null, "parent_id": null,
 "module_id": "ffmpeg", "instance_id": null,
 "ts_enqueue": $TS0, "ts_start": $TS0, "ts_end": $TS1,
 "node": "$NODE_ID", "stage": "$STAGE",
 "method": "CLI", "path": "ffmpeg",
 "bytes_in": $BYTES_IN, "bytes_out": $BYTES_OUT,
 "cpu_time_ms": null, "queue_time_ms": 0,
 "service_time_ms": $((TS1-TS0)), "rt_ms": $((TS1-TS0)),
 "status": $RC }
JSON

echo "invocation appended → $LOG_DIR/invocations.jsonl" >&2
exit $RC

