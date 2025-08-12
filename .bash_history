command -v docker || which docker
docker --version
lsb_release -a
sudo apt update
sudo apt install -y ca-certificates curl gnupg lsb-release
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker -version
docker --version
docker run --rm hello-world
sudo groupadd docker 2>/dev/null || true
sudo usermod -aG docker $USER
newgrp docker
docker run --rm hello-world
unzip tracekit.zip
sudo apt install unzip
unzip tracekit.zip
make baseline
sudo apt update
sudo apt install -y vegeta
VEG=12.11.1
wget https://github.com/tsenart/vegeta/releases/download/v${VEG}/vegeta_${VEG}_linux_amd64.tar.gz
tar -xzf vegeta_${VEG}_linux_amd64.tar.gz
sudo mv vegeta /usr/local/bin/
vegeta -version
make baseline
sed -n 'l' app/requirements.txt
sed -i 's/\r$//' app/requirements.txt
sed -i '1s/^\xEF\xBB\xBF//' app/requirements.txt
cat > app/requirements.txt <<'EOF'
fastapi==0.111.0
uvicorn==0.30.0
requests==2.32.3
EOF

make build
make baseline
nl -ba app/requirements.txt
cat > app/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY server.py .
RUN pip install --no-cache-dir fastapi==0.111.0 uvicorn==0.30.0 requests==2.32.3 \
    && mkdir -p /logs
ENV NODE_ID=vm0 STAGE=edge LOG_PATH=/logs/events.jsonl
EXPOSE 8080
CMD ["uvicorn","server:app","--host","0.0.0.0","--port","8080","--log-level","warning"]
EOF

make build
make baseline
cat > tools/drive_load.sh <<'EOF'
#!/usr/bin/env bash

set -euo pipefail
# 用法：tools/drive_load.sh <TARGET_IP> [RATE] [DURATION] [CPU_MS] [RESP_KB] [CALL_URL]

IP=${1:?"need target IP"}
RATE=${2:-50}
DUR=${3:-180s}
CPU=${4:-5}
KB=${5:-8}
CALL=${6:-}

command -v vegeta >/dev/null || { echo "vegeta not found"; exit 1; }


ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)

chmod +x tools/drive_load.sh
make load
ps aux | grep collect_sys
ls -lh /home/cloud0_gxie/logs
make baseline SCEN=baseline
make stop
docker rm -f svc 2>/dev/null || true
RID=$(date -u +%Y%m%dT%H%M%SZ)
make baseline RUN_ID=$RID STAGE=cloud RATE=100 DURATION=120s CPU_MS=5 RESP_KB=8
make stop
for d in logs/*; do    [ -d "$d" ] || continue;   if [ -f "$d/events_client.jsonl" ] && [ -f "$d/cpu.log" ]; then echo KEEP "$d"; else echo REMOVE "$d"; fi; done
rm -rf logs/*
RID=$(date -u +%Y%m%dT%H%M%SZ)
make baseline RUN_ID=$RID STAGE=cloud RATE=50 DURATION=10s CPU_MS=5 RESP_KB=8
RUN_ID=20250811T111154Z NODE_ID=vm0 STAGE=cloud python3 tools/parse_sys.py
make clean
make baseline VM_IP=127.0.0.1 IFACE=lo RATE=50 DURATION=10s
make stop
make baseline VM_IP=127.0.0.1 RATE=50 DURATION=10s
make clean
RID=$(date -u +%Y%m%dT%H%M%SZ)
make baseline RUN_ID=$RID VM_IP=127.0.0.1 IFACE= lo RATE=50 DURATION=10s
make baseline VM_IP=127.0.0.1 RATE=50 DURATION=10s
RUN_ID=20250811T120532Z NODE_ID=vm0 STAGE=cloud python3 tools/parse_sys.py
echo 'Terminal capability test'
echo 'Terminal capability test'
echo 'Terminal capability test'
