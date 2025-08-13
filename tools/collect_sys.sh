#!/usr/bin/env bash
set -euo pipefail

# 统一时区/本地化，避免解析受环境影响
export TZ=UTC
export LANG=C
export LC_ALL=C

# 路径与运行标识
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
LOG_DIR="$ROOT_DIR/logs/$RUN_ID"
mkdir -p "$LOG_DIR"

CMD=${1:-start}
VM_IP=${VM_IP:-127.0.0.1}
IFACE=${IFACE:-}   # 允许用户强制指定（为空则自动选择）

# 轻量依赖提示（不阻断）
command -v mpstat >/dev/null || echo "WARN: mpstat not found (sysstat)"
command -v ifstat >/devnull || echo "WARN: ifstat not found"
command -v vmstat >/dev/null || echo "WARN: vmstat not found (procps)"
command -v jq     >/dev/null || echo "WARN: jq not found (node_meta/link_meta json)"

# 若未显式传入 NODE_ID/STAGE，尝试从正在运行的容器 env 获取（与应用保持一致）
if [ -z "${NODE_ID:-}" ] || [ -z "${STAGE:-}" ]; then
  if command -v docker >/dev/null 2>&1; then
    ENV_LINES=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' svc 2>/dev/null || true)
    if [ -z "${NODE_ID:-}" ]; then NODE_ID=$(echo "$ENV_LINES" | awk -F= '/^NODE_ID=/ {print $2}' | tail -n1); fi
    if [ -z "${STAGE:-}" ]; then STAGE=$(echo "$ENV_LINES" | awk -F= '/^STAGE=/ {print $2}' | tail -n1); fi
  fi
fi
NODE_ID=${NODE_ID:-vm0}
STAGE=${STAGE:-edge}

# 自动选择采集网卡：优先根据到 VM_IP 的路由；若 VM_IP 为空或回环，则取默认路由网卡；最后回退 lo/eth0
if [ -z "$IFACE" ]; then
  if [ -n "${VM_IP:-}" ] && [ "$VM_IP" != "127.0.0.1" ] && [ "$VM_IP" != "localhost" ]; then
    IFACE=$(ip route get "$VM_IP" 2>/dev/null | awk '/dev/ {for(i=1;i<=NF;i++) if ($i=="dev") print $(i+1)}' | head -n1 || true)
  fi
  if [ -z "${IFACE:-}" ]; then
    IFACE=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
  fi
  [ -z "${IFACE:-}" ] && IFACE="lo"
fi
echo "collect: target=${VM_IP:-N/A} iface=$IFACE node=$NODE_ID stage=$STAGE (run_id=$RUN_ID)"

# --- 写节点元信息 ---
node_meta() {
  local OUT="$LOG_DIR/node_meta.json"
  local HOST=$(hostname)
  local CORES=$(nproc 2>/dev/null || echo 1)
  local MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)

  if command -v jq >/dev/null; then
    jq -n --arg host "$HOST" --arg iface "$IFACE" \
          --arg run_id "$RUN_ID" \
          --arg node "$NODE_ID" \
          --arg stage "$STAGE" \
          --argjson cores ${CORES} --argjson mem_mb ${MEM_MB} \
          '{run_id:$run_id,node:$node,stage:$stage,host:$host,iface:$iface,cpu_cores:$cores,mem_mb:$mem_mb}' \
          > "$OUT"
  else
    cat > "$OUT" <<EOF
{"run_id":"$RUN_ID","node":"$NODE_ID","stage":"$STAGE","host":"$HOST","iface":"$IFACE","cpu_cores":$CORES,"mem_mb":$MEM_MB}
EOF
  fi
}

# --- 写链路元信息（可选：口速/传播时延近似） ---
link_meta() {
  local OUT="$LOG_DIR/link_meta.json"
  local SPEED_Mbps=$(cat /sys/class/net/$IFACE/speed 2>/dev/null || echo 0)
  # 转为 bps（部分虚拟网卡 speed 可能为 0，表示未知）
  local BW_BPS
  if [ "${SPEED_Mbps:-0}" -gt 0 ] 2>/dev/null; then
    BW_BPS=$((SPEED_Mbps*1000000))
  else
    BW_BPS=0
  fi

  # 传播时延近似：如果目标不是回环，用 ping 的平均 RTT/2
  local PR_S="null"
  if [ "$VM_IP" != "127.0.0.1" ] && [ "$VM_IP" != "localhost" ]; then
    # 使用 -n 纯数字，-c3 三次，-i0.2 间隔，-w2 总超时
    local RTT_MS=$(ping -n -c3 -i0.2 -w2 "$VM_IP" 2>/dev/null | awk -F'/' '/rtt/ {print $5}')
    if [ -n "${RTT_MS:-}" ]; then
      # 用 awk 计算 (RTT/2)/1000
      PR_S=$(awk -v r="$RTT_MS" 'BEGIN{ printf("%.6f", (r/2.0)/1000.0) }')
    fi
  fi

  if command -v jq >/dev/null; then
    jq -n --arg iface "$IFACE" --argjson bw ${BW_BPS} --argjson pr ${PR_S:-null} \
          '{iface:$iface,BW_bps:$bw,PR_s:$pr}' > "$OUT" || true
  else
    # 无 jq 时简单输出；PR_s 为空时写 null
    if [ "${PR_S:-}" = "null" ] || [ -z "${PR_S:-}" ]; then
      echo "{\"iface\":\"$IFACE\",\"BW_bps\":$BW_BPS,\"PR_s\":null}" > "$OUT"
    else
      echo "{\"iface\":\"$IFACE\",\"BW_bps\":$BW_BPS,\"PR_s\":$PR_S}" > "$OUT"
    fi
  fi
}

if [ "$CMD" = "start" ]; then
  node_meta
  link_meta

  # CPU：固定文本模式，避免 JSON 截断
  nohup bash -c 'mpstat 1 > "$0"' "$LOG_DIR/cpu.log" >/dev/null 2>&1 &
  echo $! > "$LOG_DIR/mpstat.pid"

  # NET：指定网卡 + 带时间戳
  nohup bash -c 'ifstat -i '"$IFACE"' -t 1 > "$0"' "$LOG_DIR/net.log" >/dev/null 2>&1 &
  echo $! > "$LOG_DIR/ifstat.pid"

  # MEM：带时间戳（-t），单位 MB
  nohup bash -c 'vmstat -Sm -t 1 > "$0"' "$LOG_DIR/mem.log" >/dev/null 2>&1 &
  echo $! > "$LOG_DIR/vmstat.pid"

  echo "collectors started → $LOG_DIR"

else
  # 停止各采集器
  for f in mpstat.pid ifstat.pid vmstat.pid; do
    if [ -f "$LOG_DIR/$f" ]; then
      PID=$(cat "$LOG_DIR/$f" 2>/dev/null || echo "")
      if [ -n "${PID:-}" ]; then
        kill "$PID" 2>/dev/null || true
      fi
      rm -f "$LOG_DIR/$f"
    fi
  done
  echo "collectors stopped"
fi
