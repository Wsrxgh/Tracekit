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

# Container name for in-container sampling (app-agnostic). Default 'svc'.
CONTAINER=${CONTAINER:-svc}
# Target process match patterns for per-PID sampling (case-insensitive, '|' separated).
# Defaults cover common servers and batch tools; narrow it (e.g., '^ffmpeg$') to reduce noise, or extend as needed.
PROC_MATCH=${PROC_MATCH:-python|uvicorn|gunicorn|ffmpeg|onnx|onnxruntime|java|node|nginx|torchserve}
# Whether to refresh PID set every sampling tick (0=once at start, 1=refresh each tick). Refreshing is safer for
# processes that respawn/change PID frequently (slightly more overhead due to extra matching per tick).
PROC_REFRESH=${PROC_REFRESH:-0}

# 轻量依赖提示（不阻断）
command -v mpstat >/dev/null || echo "WARN: mpstat not found (sysstat)"
command -v ifstat >/dev/null || echo "WARN: ifstat not found"
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
STAGE=${STAGE:-cloud}

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

  # CPU model/freq (best-effort)
  local CPU_MODEL="$(lscpu 2>/dev/null | awk -F: '/Model name/ {sub(/^ +/,"",$2); print $2}' | head -n1)"
  if [ -z "$CPU_MODEL" ]; then
    CPU_MODEL="$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ //')"
  fi
  local CPU_MHZ="$(lscpu 2>/dev/null | awk -F: '/CPU max MHz/ {sub(/^ +/,"",$2); print int($2)}' | head -n1)"
  if [ -z "$CPU_MHZ" ]; then
    CPU_MHZ="$(awk -F: '/cpu MHz/ {gsub(/^ +/,"",$2); print int($2)}' /proc/cpuinfo 2>/dev/null | head -n1)"
  fi
  [ -z "$CPU_MHZ" ] && CPU_MHZ=0

  if command -v jq >/dev/null; then
    jq -n --arg host "$HOST" --arg iface "$IFACE" \
          --arg run_id "$RUN_ID" \
          --arg node "$NODE_ID" \
          --arg stage "$STAGE" \
          --arg model "$CPU_MODEL" --argjson mhz ${CPU_MHZ} \
          --argjson cores ${CORES} --argjson mem_mb ${MEM_MB} \
          '{run_id:$run_id,node:$node,stage:$stage,host:$host,iface:$iface,cpu_cores:$cores,mem_mb:$mem_mb,cpu_model:$model,cpu_freq_mhz:$mhz}' \
          > "$OUT"
  else
    cat > "$OUT" <<EOF
{"run_id":"$RUN_ID","node":"$NODE_ID","stage":"$STAGE","host":"$HOST","iface":"$IFACE","cpu_cores":$CORES,"mem_mb":$MEM_MB,"cpu_model":"$CPU_MODEL","cpu_freq_mhz":$CPU_MHZ}
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
    # 取最小 RTT（更接近传播+固定栈开销的下界）
    local RTT_MIN_MS=$(ping -n -c3 -i0.2 -w2 "$VM_IP" 2>/dev/null | awk -F'[/= ]' '/rtt/ {print $8}')
    if [ -n "${RTT_MIN_MS:-}" ]; then
      PR_S=$(awk -v r="$RTT_MIN_MS" 'BEGIN{ printf("%.6f", (r/2.0)/1000.0) }')
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

# ---- helpers: stop previous collectors in this LOG_DIR (TERM -> wait -> KILL) ----
stop_collectors() {
  local did=0
  # 1) Try to stop by recorded PIDs (and their process groups)
  for name in mpstat ifstat vmstat procmon; do
    local pf="$LOG_DIR/${name}.pid"
    if [ -f "$pf" ]; then
      local PID=$(cat "$pf" 2>/dev/null || echo "")
      if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        # send TERM to the process and its group to ensure children (e.g., mpstat/ifstat/vmstat) also exit
        kill "$PID" 2>/dev/null || true
        kill -TERM -"$PID" 2>/dev/null || true
        sleep 0.5
        # force kill if still alive
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
        kill -KILL -"$PID" 2>/dev/null || true
        did=1
      fi
      rm -f "$pf" 2>/dev/null || true
    fi
  done
  # 2) Fallback: kill by log file path (handles cases where wrapper died but child remains)
  pkill -f "$LOG_DIR/cpu.log" 2>/dev/null || true
  pkill -f "$LOG_DIR/net.log" 2>/dev/null || true
  pkill -f "$LOG_DIR/mem.log" 2>/dev/null || true
  [ $did -eq 1 ] && echo "previous collectors stopped in $LOG_DIR" || true
}

# --- per-PID sampler: RSS + utime/stime (container if available; else host fallback) ---
start_procmon() {
  local OUT="$LOG_DIR/proc_metrics.jsonl"
  local INTERVAL_MS=${PROC_INTERVAL_MS:-200}
  # fractional seconds sleep for sub-second intervals
  local INTERVAL_S=$(awk -v ms="$INTERVAL_MS" 'BEGIN{ printf("%.3f", ms/1000.0) }')
  # legacy integer seconds (guard only)
  local INTERVAL_SEC=$(( (INTERVAL_MS + 500) / 1000 ))
  [ "$INTERVAL_SEC" -lt 1 ] && INTERVAL_SEC=1

  local MODE="container"
  # Use container mode only if Docker is available AND the target container exists AND is running
  if command -v docker >/dev/null 2>&1; then
    local RUNNING=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
    if [ "$RUNNING" != "true" ]; then MODE="host"; fi
  else
    MODE="host"
  fi

  if [ "$MODE" = "container" ]; then
    # detect PIDs once based on PROC_MATCH (case-insensitive); fallback to 1
    local PIDS=$(docker exec -e PROC_MATCH="${PROC_MATCH}" "$CONTAINER" sh -lc '
PIDS=""; for p in /proc/[0-9]*; do bn=${p##*/}; f="/proc/$bn/comm"; [ -r "$f" ] || continue; c=$(cat "$f"); echo "$c" | grep -Eiq "$PROC_MATCH" || continue; PIDS="$PIDS $bn"; done; echo ${PIDS# }' 2>/dev/null || true)
    [ -z "$PIDS" ] && PIDS="1"
    local INNER_STATIC='TS=$(( $(date +%s) * 1000 + 10#$(date +%N | cut -c1-3) )); for pid in '$PIDS'; do if [ -r /proc/$pid/stat ]; then LINE=$(cat /proc/$pid/stat 2>/dev/null) || continue; REST=${LINE#*) }; set -- $REST; UT=${12}; ST=${13}; PAGES=$(cut -d" " -f2 /proc/$pid/statm 2>/dev/null); RSS=$(( ${PAGES:-0} * 4 )); TH=0; if [ "${PROC_THREADS:-0}" = "1" ]; then TH=$(awk '\''/^Threads:/{print $2}'\'' /proc/$pid/status 2>/dev/null || echo 0); fi; printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s,\"threads\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}" "${TH:-0}"; fi; done'
    local INNER_REFRESH='TS=$(( $(date +%s) * 1000 + 10#$(date +%N | cut -c1-3) )); PIDS=$(for p in /proc/[0-9]*; do bn=${p##*/}; f="/proc/$bn/comm"; [ -r "$f" ] || continue; c=$(cat "$f"); echo "$c" | grep -Eiq "$PROC_MATCH" || continue; echo -n "$bn "; done); for pid in $PIDS; do if [ -r /proc/$pid/stat ]; then LINE=$(cat /proc/$pid/stat 2>/dev/null) || continue; REST=${LINE#*) }; set -- $REST; UT=${12}; ST=${13}; PAGES=$(cut -d" " -f2 /proc/$pid/statm 2>/dev/null); RSS=$(( ${PAGES:-0} * 4 )); TH=0; if [ "${PROC_THREADS:-0}" = "1" ]; then TH=$(awk '\''/^Threads:/{print $2}'\'' /proc/$pid/status 2>/dev/null || echo 0); fi; printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s,\"threads\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}" "${TH:-0}"; fi; done'
    if [ "${PROC_REFRESH}" = "1" ]; then
      nohup bash -c "while :; do docker exec -e PROC_MATCH='$PROC_MATCH' '$CONTAINER' sh -lc '$INNER_REFRESH' >> '$OUT' 2>>'$LOG_DIR/procmon.err'; sleep $INTERVAL_S; done" >/dev/null 2>&1 &
    else
      nohup bash -c "while :; do docker exec '$CONTAINER' sh -lc '$INNER_STATIC' >> '$OUT' 2>>'$LOG_DIR/procmon.err'; sleep $INTERVAL_S; done" >/dev/null 2>&1 &
    fi
    echo $! > "$LOG_DIR/procmon.pid"
    echo "proc sampler started (mode=container, container=$CONTAINER, interval=${INTERVAL_SEC}s) → $OUT"
  else
    # host fallback: sample matching PIDs on the host
    local PIDS_HOST=$(for p in /proc/[0-9]*; do bn=${p##*/}; f="/proc/$bn/comm"; [ -r "$f" ] || continue; c=$(cat "$f"); echo "$c" | grep -Eiq "$PROC_MATCH" || continue; echo -n "$bn "; done)
    [ -z "$PIDS_HOST" ] && PIDS_HOST="1"
    local H_STATIC='TS=$(( $(date +%s) * 1000 + 10#$(date +%N | cut -c1-3) )); for pid in '$PIDS_HOST'; do if [ -r /proc/$pid/stat ]; then LINE=$(cat /proc/$pid/stat 2>/dev/null) || continue; REST=${LINE#*) }; set -- $REST; UT=${12}; ST=${13}; PAGES=$(cut -d" " -f2 /proc/$pid/statm 2>/dev/null); RSS=$(( ${PAGES:-0} * 4 )); if [ "${PROC_THREADS:-0}" = "1" ]; then TH=$(awk '\''/^Threads:/{print $2}'\'' /proc/$pid/status 2>/dev/null || echo 0); printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s,\"threads\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}" "${TH:-0}"; else printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}"; fi; fi; done'
    local H_REFRESH='TS=$(( $(date +%s) * 1000 + 10#$(date +%N | cut -c1-3) )); PIDS=$(for p in /proc/[0-9]*; do bn=${p##*/}; f="/proc/$bn/comm"; [ -r "$f" ] || continue; c=$(cat "$f"); echo "$c" | grep -Eiq "$PROC_MATCH" || continue; echo -n "$bn "; done); for pid in $PIDS; do if [ -r /proc/$pid/stat ]; then LINE=$(cat /proc/$pid/stat 2>/dev/null) || continue; REST=${LINE#*) }; set -- $REST; UT=${12}; ST=${13}; PAGES=$(cut -d" " -f2 /proc/$pid/statm 2>/dev/null); RSS=$(( ${PAGES:-0} * 4 )); if [ "${PROC_THREADS:-0}" = "1" ]; then TH=$(awk '\''/^Threads:/{print $2}'\'' /proc/$pid/status 2>/dev/null || echo 0); printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s,\"threads\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}" "${TH:-0}"; else printf "{\"ts_ms\":%s,\"pid\":%s,\"rss_kb\":%s,\"utime\":%s,\"stime\":%s}\n" "$TS" "$pid" "${RSS:-0}" "${UT:-0}" "${ST:-0}"; fi; fi; done'
    if [ "${PROC_REFRESH}" = "1" ]; then
      nohup bash -c "while :; do PROC_MATCH='$PROC_MATCH' bash -lc '$H_REFRESH' >> '$OUT' 2>>'$LOG_DIR/procmon.err'; sleep $INTERVAL_S; done" >/dev/null 2>&1 &
    else
      nohup bash -c "while :; do bash -lc '$H_STATIC' >> '$OUT' 2>>'$LOG_DIR/procmon.err'; sleep $INTERVAL_S; done" >/dev/null 2>&1 &
    fi
    echo $! > "$LOG_DIR/procmon.pid"
    echo "proc sampler started (mode=host, match='$PROC_MATCH', interval=${INTERVAL_SEC}s) → $OUT"
  fi
}

if [ "$CMD" = "start" ]; then
  # pre-stop in case of repeated starts on the same RUN_ID
  stop_collectors
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

  # per-PID sampler (optional, default on). Set PROC_SAMPLING=0 to disable
  if [ "${PROC_SAMPLING:-1}" = "1" ]; then
    start_procmon || true
  fi

  echo "collectors started → $LOG_DIR"

else
  # 停止各采集器（TERM -> wait -> KILL），并清理 pid 文件
  stop_collectors
  echo "collectors stopped"
fi
