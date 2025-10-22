#!/usr/bin/env bash
# Quick setup for CONTROLLER node (Ubuntu/Debian) with sudo
# - Installs: redis-server, redis-tools, python3, python3-pip, jq
# - Secures Redis with requirepass
# - Optional: exposes Redis on 0.0.0.0 and opens port 6379 via ufw
#
# Usage (examples):
#   REDIS_PASS='StrongPassword!' bash tools/setup/setup_controller.sh
#   REDIS_PASS='StrongPassword!' EXPOSE_REDIS=1 bash tools/setup/setup_controller.sh
#
# Env vars:
#   REDIS_PASS     (required) Redis requirepass value
#   EXPOSE_REDIS   (optional) 1 to bind 0.0.0.0 and open tcp/6379 (default: 0)
#   NONINTERACTIVE (optional) 1 to suppress interactive apt prompts (default: 1)

set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  SUDO=sudo
else
  SUDO=
fi

NONINTERACTIVE="${NONINTERACTIVE:-1}"
if [[ "$NONINTERACTIVE" == "1" ]]; then
  export DEBIAN_FRONTEND=noninteractive
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script currently supports Ubuntu/Debian (apt-get)." >&2
  exit 1
fi

EXPOSE_REDIS="${EXPOSE_REDIS:-0}"
REDIS_PASS="${REDIS_PASS:-}"
if [[ -z "$REDIS_PASS" ]]; then
  echo "ERROR: REDIS_PASS is required. Example: REDIS_PASS='StrongPassword!' bash tools/setup/setup_controller.sh" >&2
  exit 1
fi

# Install packages
$SUDO apt-get update -y
$SUDO apt-get install -y jq python3 python3-pip redis-server redis-tools

# Configure Redis
CONF=/etc/redis/redis.conf
$SUDO cp "$CONF" "$CONF.bak.$(date +%s)"
# Remove existing requirepass lines, then append our requirepass
$SUDO sed -i -e '/^\s*#\s*requirepass\b/d' -e '/^\s*requirepass\b/d' "$CONF"
echo "requirepass $REDIS_PASS" | $SUDO tee -a "$CONF" >/dev/null

if [[ "$EXPOSE_REDIS" == "1" ]]; then
  # Bind to 0.0.0.0 to allow remote connections (ensure you understand the risks)
  $SUDO sed -i -r 's/^#?\s*bind\s+.*/bind 0.0.0.0/' "$CONF" || true
  # Optional firewall rule via ufw, if available
  if command -v ufw >/dev/null 2>&1; then
    $SUDO ufw allow 6379/tcp || true
  else
    # Try to install ufw (non-fatal if fails)
    $SUDO apt-get install -y ufw || true
    if command -v ufw >/dev/null 2>&1; then
      $SUDO ufw allow 6379/tcp || true
    fi
  fi
fi

# Restart and enable Redis
$SUDO systemctl enable --now redis-server
sleep 1

# Basic connectivity check (auth)
if command -v redis-cli >/dev/null 2>&1; then
  set +e
  redis-cli -a "$REDIS_PASS" -h 127.0.0.1 -p 6379 ping | grep -qi PONG
  RC=$?
  set -e
  if [[ $RC -ne 0 ]]; then
    echo "WARN: redis-cli auth PING failed. Please check redis.conf and logs: journalctl -u redis-server" >&2
  else
    echo "OK: Redis is up and requires password (PING=PONG)."
  fi
fi

# Final notes
if [[ "$EXPOSE_REDIS" == "1" ]]; then
  echo "NOTICE: Redis bound to 0.0.0.0 and tcp/6379 may be open. Ensure you have firewall restrictions and strong passwords."
else
  echo "Redis is secured with requirepass. Remote access is not enabled by default (bind not changed)."
fi

