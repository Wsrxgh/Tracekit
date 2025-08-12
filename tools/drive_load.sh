#!/usr/bin/env bash
set -euo pipefail
IP=${1:?"need target IP"}
RATE=${2:-50}
DUR=${3:-180s}
CPU=${4:-5}
KB=${5:-8}
CALL=${6:-}

command -v vegeta >/dev/null || { echo "vegeta not found"; exit 1; }

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_DIR="$ROOT_DIR/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
cd "$ROOT_DIR"

# 等服务就绪
for i in {1..60}; do
  curl -sf "http://$IP:8080/work" >/dev/null && break
  sleep 0.5
  [[ $i -eq 60 ]] && { echo "service not ready"; exit 1; }
done

# 上行载荷（作为 bytes_in）
REQ_BODY="$LOG_DIR/body.bin"
head -c $((KB*1024)) /dev/zero > "$REQ_BODY"

# 组装请求（POST + 查询参数）
if [ -n "$CALL" ]; then
  Q=$(python3 - <<'PY'
import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))
PY
"$CALL")
  URL="http://$IP:8080/work?cpu_ms=$CPU&resp_kb=$KB&call_url=$Q"
else
  URL="http://$IP:8080/work?cpu_ms=$CPU&resp_kb=$KB"
fi
REQ="POST $URL"

# attack：输出二进制 + 报告
set +e
echo "$REQ" | vegeta attack -body="$REQ_BODY" -rate="$RATE" -duration="$DUR" -output "$LOG_DIR/results.bin"
vegeta report "$LOG_DIR/results.bin"
set -e

# 逐请求 JSON
( vegeta encode -to=json "$LOG_DIR/results.bin" > "$LOG_DIR/events_client.jsonl" ) \
  || ( vegeta dump -json "$LOG_DIR/results.bin" > "$LOG_DIR/events_client.jsonl" )

# 记录本次运行元信息（补充场景/版本/配置）
GIT_SHA=$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
cat > "$LOG_DIR/run_meta.json" <<META
{
  "run_id": "$RUN_ID",
  "target_ip": "$IP",
  "rate": "$RATE",
  "duration": "$DUR",
  "cpu_ms": $CPU,
  "resp_kb": $KB,
  "call_url": "$(echo "$CALL" | sed 's/"/\\"/g')",
  "scen": "${SCEN:-baseline}",
  "img": "${IMG:-tunable-svc:0.1.0}",
  "git_sha": "$GIT_SHA",
  "workers": "${WORKERS:-1}"
}
META

echo "client events → $LOG_DIR/events_client.jsonl"
