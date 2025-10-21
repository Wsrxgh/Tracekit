# ===== 用户可改参数（可被 scenarios/*.env 覆盖） =====
-include run_id.env

RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%SZ)
# Use Python collector by default (set to 0 to fallback to shell collector)
USE_PY_COLLECT ?= 1

.EXPORT_ALL_VARIABLES:

RATE ?= 50
DURATION ?= 60s
CPU_MS ?= 5
RESP_KB ?= 8
CALL_URL ?=
NODE_ID ?= $(shell hostname)
STAGE ?= cloud
WORKERS ?= 1
IFACE ?=
IMG ?= tunable-svc:0.1.0
VM_IP ?= 127.0.0.1


# Default PID whitelist directory for collector (auto-created)
PROC_PID_DIR ?= logs/$(RUN_ID)/pids
# Default to sampling only ffmpeg/ffprobe; override as needed
PROC_MATCH ?= ^ffmpeg$|^ffprobe$

LOG_DIR := logs/$(RUN_ID)


.PHONY: setup build run start-collect load stop-collect parse baseline stop clean real-json real-gzip real-hash real-kvset real-kvget

setup:
	@sudo apt update && sudo apt install -y sysstat ifstat curl jq || true
	@if ! command -v vegeta >/dev/null; then \
	  V=12.11.1; wget -q https://github.com/tsenart/vegeta/releases/download/v$$V/vegeta_$$V_linux_amd64.tar.gz && \
	  tar -xzf vegeta_$$V_linux_amd64.tar.gz && sudo mv vegeta /usr/local/bin/ && rm -f vegeta_$$V_linux_amd64.tar.gz; \
	fi
	@mkdir -p $(LOG_DIR) logs
	@echo "setup done"

build:
	@echo "(no example app build; focusing on ffmpeg + system collectors)"

run:
	mkdir -p $(LOG_DIR)
	docker rm -f svc >/dev/null 2>&1 || true
	docker run -d --name svc -p 8080:8080 \
	  --cpus="1.0" \
	  -e NODE_ID=$(NODE_ID) -e STAGE=$(STAGE) \
	  -e LOG_PATH=/logs \
	  -e WORKERS=$(WORKERS) \
	  -e DEFAULT_NEXT_URL=$(DEFAULT_NEXT_URL) \
	  -v $(PWD)/$(LOG_DIR):/logs $(IMG)
	# 健康检查已禁用（按需求移除自动 /work 探活）
	@echo "container started (healthcheck=disabled) (run_id=$(RUN_ID))"
	@echo $(RUN_ID) > $(LOG_DIR)/.run_id
start-collect:
ifeq ($(USE_PY_COLLECT),1)
	@RUN_ID=$(RUN_ID) NODE_ID=$(NODE_ID) STAGE=$(STAGE) IFACE=$(IFACE) VM_IP=$(VM_IP) \
	 PROC_SAMPLING=$$PROC_SAMPLING PROC_REFRESH=$$PROC_REFRESH PROC_INTERVAL_MS=$$PROC_INTERVAL_MS \
	 PROC_MATCH="$$PROC_MATCH" PROC_PID_DIR="$$PROC_PID_DIR" STOP_ALL="$$STOP_ALL" \
	 python3 tools/collect_sys.py start
else
	@RUN_ID=$(RUN_ID) NODE_ID=$(NODE_ID) STAGE=$(STAGE) IFACE=$(IFACE) VM_IP=$(VM_IP) \
	 PROC_SAMPLING=$$PROC_SAMPLING PROC_REFRESH=$$PROC_REFRESH PROC_INTERVAL_MS=$$PROC_INTERVAL_MS \
	 PROC_MATCH="$$PROC_MATCH" bash tools/collect_sys.sh start
endif

load:
	@echo "[deprecated] load target removed: tools/drive_load.sh has been deleted; please use your own load generator."


stop-collect:
ifeq ($(USE_PY_COLLECT),1)
	@RUN_ID=$(RUN_ID) STOP_ALL="$$STOP_ALL" python3 tools/collect_sys.py stop
else
	@RUN_ID=$(RUN_ID) bash tools/collect_sys.sh stop
endif

parse:
	@RUN_ID=$(RUN_ID) NODE_ID=$(NODE_ID) STAGE=$(STAGE) python3 tools/parse_sys.py

	@echo "\nArtifacts (logs/$(RUN_ID)):"
	@echo "  events.jsonl (服务端), events_client.jsonl (客户端)"

	@echo "  node_meta.json, run_meta.json"
	@echo "  placement_events.jsonl (开始/停止/扩缩容)、system_stats.jsonl (实例并发)"
	@echo "  CTS/{nodes.json,invocations.jsonl,proc_metrics.jsonl,audit_report.md}"

baseline: setup build run start-collect load stop-collect parse

export-opendc:
	@python3 tools/export_opendc.py --input logs/$(RUN_ID) --output opendc_traces/
	@echo "OpenDC traces exported to opendc_traces/"

stop:
	-docker rm -f svc >/dev/null 2>&1 || true
	@RUN_ID=$(RUN_ID) bash tools/collect_sys.sh stop || true

clean: stop
	-rm -rf logs/$(RUN_ID)

# ===== Real workload one-click targets =====
REAL_RATE ?= 40
REAL_DUR ?= 20s
REAL_SIZE ?= 8192

real-json:
	@echo "[deprecated] real-json removed: tools/drive_real.sh has been deleted."

real-gzip:
	@echo "[deprecated] real-gzip removed: tools/drive_real.sh has been deleted."

real-hash: setup build run start-collect
	@echo "[deprecated] real-hash removed: tools/drive_real.sh has been deleted."

real-kvset: setup build run start-collect
	@echo "[deprecated] real-kvset removed: tools/drive_real.sh has been deleted."

real-kvget: setup build run start-collect
	@echo "[deprecated] real-kvget removed: tools/drive_real.sh has been deleted."

# run all real endpoints sequentially (lightweight)
real-all: setup build run start-collect
	@echo "[deprecated] real-all removed: tools/drive_real.sh has been deleted."
