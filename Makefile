# ===== 用户可改参数（可被 scenarios/*.env 覆盖） =====
-include run_id.env

RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%SZ)
.EXPORT_ALL_VARIABLES:

RATE ?= 50
DURATION ?= 60s
CPU_MS ?= 5
RESP_KB ?= 8
CALL_URL ?=
NODE_ID ?= vm0
STAGE ?= edge
WORKERS ?= 1
IFACE ?=
IMG ?= tunable-svc:0.1.0
VM_IP ?= 127.0.0.1

LOG_DIR := logs/$(RUN_ID)

SCEN ?= baseline
-include scenarios/$(SCEN).env

.PHONY: setup build run start-collect load stop-collect parse baseline stop clean real-json real-gzip real-hash real-kvset real-kvget

setup:
	@sudo apt update && sudo apt install -y sysstat ifstat curl jq || true
	@if ! command -v vegeta >/dev/null; then \
	  V=12.11.1; wget -q https://github.com/tsenart/vegeta/releases/download/v$$V/vegeta_$$V_linux_amd64.tar.gz && \
	  tar -xzf vegeta_$$V_linux_amd64.tar.gz && sudo mv vegeta /usr/local/bin/ && rm -f vegeta_$$V_linux_amd64.tar.gz; \
	fi
	@python3 -m pip -q install -r app/requirements.txt || true
	@mkdir -p $(LOG_DIR) logs
	@echo "setup done"

build:
	docker build -t $(IMG) ./app

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
	@RUN_ID=$(RUN_ID) NODE_ID=$(NODE_ID) STAGE=$(STAGE) IFACE=$(IFACE) VM_IP=$(VM_IP) bash tools/collect_sys.sh start

load:
	@RUN_ID=$(RUN_ID) bash tools/drive_load.sh $(VM_IP) $(RATE) $(DURATION) $(CPU_MS) $(RESP_KB) '$(CALL_URL)'


stop-collect:
	@RUN_ID=$(RUN_ID) bash tools/collect_sys.sh stop

parse:
	@RUN_ID=$(RUN_ID) NODE_ID=$(NODE_ID) STAGE=$(STAGE) python3 tools/parse_sys.py
	@python3 tools/write_module_inventory.py
	@echo "\nArtifacts (logs/$(RUN_ID)):"
	@echo "  events.jsonl (服务端), events_client.jsonl (客户端)"
	@echo "  resources.jsonl (CPU/MEM), links.jsonl (网络)"
	@echo "  node_meta.json, run_meta.json, module_inventory.json"
	@echo "  placement_events.jsonl (开始/停止/扩缩容)、system_stats.jsonl (实例并发)"
	@echo "  cctf/{nodes.json,links.json,invocations.jsonl,host_metrics.jsonl,link_metrics.jsonl,placement_events.jsonl,system_stats.jsonl}"

baseline: setup build run start-collect load stop-collect parse

stop:
	-docker rm -f svc >/dev/null 2>&1 || true
	@RUN_ID=$(RUN_ID) bash tools/collect_sys.sh stop || true

clean: stop
	-rm -rf logs/$(RUN_ID)

# ===== Real workload one-click targets =====
REAL_RATE ?= 40
REAL_DUR ?= 20s
REAL_SIZE ?= 8192

real-json: setup build run start-collect
	@RUN_ID=$(RUN_ID) bash tools/drive_real.sh json $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse

real-gzip: setup build run start-collect
	@RUN_ID=$(RUN_ID) bash tools/drive_real.sh gzip $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse

real-hash: setup build run start-collect
	@RUN_ID=$(RUN_ID) bash tools/drive_real.sh hash $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse

real-kvset: setup build run start-collect
	@RUN_ID=$(RUN_ID) bash tools/drive_real.sh kvset $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse

real-kvget: setup build run start-collect
	@RUN_ID=$(RUN_ID) bash tools/drive_real.sh kvget $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse

# run all real endpoints sequentially (lightweight)
real-all: setup build run start-collect
	@echo "Running real-json..." && RUN_ID=$(RUN_ID) bash tools/drive_real.sh json $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@echo "Running real-gzip..." && RUN_ID=$(RUN_ID) bash tools/drive_real.sh gzip $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@echo "Running real-hash..." && RUN_ID=$(RUN_ID) bash tools/drive_real.sh hash $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@echo "Running real-kvset..." && RUN_ID=$(RUN_ID) bash tools/drive_real.sh kvset $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@echo "Running real-kvget..." && RUN_ID=$(RUN_ID) bash tools/drive_real.sh kvget $(VM_IP) $(REAL_RATE) $(REAL_DUR) $(REAL_SIZE)
	@$(MAKE) RUN_ID=$(RUN_ID) stop-collect parse
