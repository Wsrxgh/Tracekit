#!/usr/bin/env bash
# ffmpeg_wrapper.sh (adapter sample)
# Non-invasive example: wrap each ffmpeg invocation to emit an event before/after.
# NOT wired into any pipeline by default. Use when your app is not our FastAPI service.
#
# Usage:
#   RUN_ID=2025... NODE_ID=cloud0 STAGE=cloud \
#   ./tools/adapters/ffmpeg_wrapper.sh -i in.mp4 -vf scale=1280:720 -c:v libx264 out.mp4
#   # events will be appended to logs/$RUN_ID/events.ffmpeg.jsonl
#
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
# Try to load RUN_ID from repo root if not set
if [ -z "${RUN_ID:-}" ] && [ -f "$ROOT/run_id.env" ]; then . "$ROOT/run_id.env" || true; fi
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_DIR="$ROOT/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
NODE_ID=${NODE_ID:-vm0}
STAGE=${STAGE:-cloud}
EVENT_FILE="$LOG_DIR/events.ffmpeg.jsonl"

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

# generate trace id if absent; run ffmpeg in background to capture PID
if [ -z "${TRACE_ID:-}" ]; then
  if command -v uuidgen >/dev/null 2>&1; then TRACE_ID=$(uuidgen); else TRACE_ID=$(date +%s%N); fi
fi
set +e
# Optional CPU limits: CPUSET="0-1" to pin cores; CPU_QUOTA=200 to cap at 200%
LAUNCH=()
if [ -n "${CPU_QUOTA:-}" ] && command -v systemd-run >/dev/null 2>&1; then
  LAUNCH+=(systemd-run --scope -p CPUQuota=${CPU_QUOTA}%)
fi
if [ -n "${CPUSET:-}" ] && command -v taskset >/dev/null 2>&1; then
  LAUNCH+=(taskset -c "${CPUSET}")
fi
"${LAUNCH[@]}" ffmpeg "$@" &
FFPID=$!
# PID sentinel (optional): advertise PID to sampler if enabled
PID_DIR="$LOG_DIR/pids"
mkdir -p "$PID_DIR" 2>/dev/null || true
: > "$PID_DIR/$FFPID" || true
trap 'rm -f "$PID_DIR/$FFPID" 2>/dev/null || true' EXIT
wait "$FFPID"; RC=$?
set -e
TS1=$(date +%s%3N)
if [ -n "$OUTPUT" ] && [ -f "$OUTPUT" ]; then BYTES_OUT=$(stat -c %s "$OUTPUT" 2>/dev/null || echo 0); fi

# Prefer externally provided enqueue time (from dispatcher/central scheduler) if present
TS_ENQ=${TS_ENQUEUE:-$TS0}
# write record (minimal fields; parse_sys.py will copy events.*.jsonl as invocations)
# Use single line JSON format for proper JSONL
echo "{\"trace_id\": \"${TRACE_ID:-}\", \"span_id\": null, \"parent_id\": null, \"module_id\": \"ffmpeg\", \"instance_id\": null, \"ts_enqueue\": $TS_ENQ, \"ts_start\": $TS0, \"ts_end\": $TS1, \"node\": \"$NODE_ID\", \"stage\": \"$STAGE\", \"method\": \"CLI\", \"path\": \"ffmpeg\", \"input\": \"${in_base:-}\", \"output\": \"${out_base:-}\", \"pid\": ${FFPID:-0}, \"bytes_in\": $BYTES_IN, \"bytes_out\": $BYTES_OUT, \"status\": $RC }" >> "$EVENT_FILE"

echo "invocation appended → $EVENT_FILE" >&2
exit $RC

