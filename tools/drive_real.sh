#!/usr/bin/env bash
set -euo pipefail
# Usage: drive_real.sh <endpoint> <IP> [RATE] [DURATION] [SIZE_BYTES]
# endpoint: json | gzip | hash | kvset | kvget

EP=${1:?'need endpoint: json|gzip|hash|kvset|kvget'}
IP=${2:?'need target IP'}
RATE=${3:-50}
DUR=${4:-30s}
SIZE=${5:-16384}
KEY=${KEY:-k1}

command -v vegeta >/dev/null || { echo "vegeta not found"; exit 1; }

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_DIR="$ROOT_DIR/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
cd "$ROOT_DIR"

# wait for service
for i in {1..60}; do
  curl -sf "http://$IP:8080/work" >/dev/null && break
  sleep 0.5
  [[ $i -eq 60 ]] && { echo "service not ready"; exit 1; }
done

# Prepare body files
JSON_BODY="$LOG_DIR/body.json"
BLOB_BODY="$LOG_DIR/blob.bin"

make_json() {
python3 - "$SIZE" > "$JSON_BODY" <<'PY'
import json, os, sys
n = int(sys.argv[1]) if len(sys.argv)>1 else 1024
# construct roughly n bytes json
s = "x" * max(0, n-64)
obj = {"user":"u", "ts":0, "data": s}
print(json.dumps(obj))
PY
}

make_blob() {
  head -c "$SIZE" /dev/urandom > "$BLOB_BODY"
}

case "$EP" in
  json)
    make_json
    URL="http://$IP:8080/json/validate"
    REQ="POST $URL"
    HDRS=(-header "Content-Type: application/json")
    BODY_FILE="$JSON_BODY"
    PAYLOAD_BYTES=$(wc -c < "$JSON_BODY")
    ;;
  gzip)
    make_blob
    URL="http://$IP:8080/blob/gzip"
    REQ="POST $URL"
    HDRS=()
    BODY_FILE="$BLOB_BODY"
    PAYLOAD_BYTES=$(wc -c < "$BLOB_BODY")
    ;;
  hash)
    make_blob
    URL="http://$IP:8080/hash/sha256"
    REQ="POST $URL"
    HDRS=()
    BODY_FILE="$BLOB_BODY"
    PAYLOAD_BYTES=$(wc -c < "$BLOB_BODY")
    ;;
  kvset)
    make_blob
    URL="http://$IP:8080/kv/set/$KEY"
    REQ="POST $URL"
    HDRS=()
    BODY_FILE="$BLOB_BODY"
    PAYLOAD_BYTES=$(wc -c < "$BLOB_BODY")
    ;;
  kvget)
    # ensure a value exists first
    head -c "$SIZE" /dev/urandom > "$BLOB_BODY"
    curl -sf -X POST --data-binary @"$BLOB_BODY" "http://$IP:8080/kv/set/$KEY" >/dev/null || true
    URL="http://$IP:8080/kv/get/$KEY"
    REQ="GET $URL"
    HDRS=()
    BODY_FILE=""
    PAYLOAD_BYTES=0
    ;;
  *) echo "unknown endpoint: $EP"; exit 1;;
 esac

# Attack
set +e
if [ -n "${BODY_FILE}" ]; then
  echo "$REQ" | vegeta attack -rate="$RATE" -duration="$DUR" -output "$LOG_DIR/results.bin" -body "$BODY_FILE" "${HDRS[@]}"
else
  echo "$REQ" | vegeta attack -rate="$RATE" -duration="$DUR" -output "$LOG_DIR/results.bin" "${HDRS[@]}"
fi
vegeta report "$LOG_DIR/results.bin"
set -e

# Per-request JSON
( vegeta encode -to=json "$LOG_DIR/results.bin" > "$LOG_DIR/events_client.jsonl" ) || \
( vegeta dump -json "$LOG_DIR/results.bin" > "$LOG_DIR/events_client.jsonl" )

# run_meta
cat > "$LOG_DIR/run_meta.json" <<META
{
  "run_id": "$RUN_ID",
  "target_ip": "$IP",
  "endpoint": "$EP",
  "rate": "$RATE",
  "duration": "$DUR",
  "payload_bytes": $PAYLOAD_BYTES,
  "scen": "${SCEN:-real-$EP}",
  "img": "${IMG:-tunable-svc:0.1.0}",
  "workers": "${WORKERS:-1}"
}
META

# mark run_id for Makefile to pick up
echo "$RUN_ID" > "$LOG_DIR/.run_id"

echo "client events → $LOG_DIR/events_client.jsonl"

