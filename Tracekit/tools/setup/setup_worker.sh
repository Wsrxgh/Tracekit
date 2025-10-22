#!/usr/bin/env bash
# Quick setup for WORKER node (Ubuntu/Debian) with sudo
# - Installs: ffmpeg, python3, python3-pip, jq (optional: chrony)
# - Sets up Python deps for Tracekit scheduler/worker (minimal) or full Tracekit tools
# - Optional: create Python venv under repo and install into it
#
# Usage examples (run on worker VM):
#   bash tools/setup/setup_worker.sh                                   # minimal, system pip
#   USE_VENV=1 bash tools/setup/setup_worker.sh                        # create .venv and install minimal deps
#   FULL_TRACEKIT=1 bash tools/setup/setup_worker.sh                   # install full Tracekit tools deps
#   INSTALL_CHRONY=1 bash tools/setup/setup_worker.sh                  # also install chrony for time sync
#
# Env vars:
#   REPO_DIR       (optional) path to repo root (default: current directory)
#   USE_VENV       (optional) 1 to create/use REPO_DIR/.venv (default: 1)
#   FULL_TRACEKIT  (optional) 1 to install Tracekit/requirements.txt instead of tools/scheduler/requirements.txt (default: 0)
#   INSTALL_CHRONY (optional) 1 to install chrony (default: 0)
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

REPO_DIR="${REPO_DIR:-$(pwd)}"
USE_VENV="${USE_VENV:-1}"
FULL_TRACEKIT="${FULL_TRACEKIT:-0}"
INSTALL_CHRONY="${INSTALL_CHRONY:-0}"

# Install packages
$SUDO apt-get update -y
PKGS=(ffmpeg python3 python3-pip jq)
if [[ "$INSTALL_CHRONY" == "1" ]]; then
  PKGS+=(chrony)
fi
$SUDO apt-get install -y "${PKGS[@]}"

# Prepare Python environment
PIP_RUN=(python3 -m pip)
if [[ "$USE_VENV" == "1" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found" >&2
    exit 1
  fi
  VENV_DIR="$REPO_DIR/.venv"
  python3 -m venv "$VENV_DIR"
  # shellcheck source=/dev/null
  source "$VENV_DIR/bin/activate"
  PIP_RUN=(pip)
fi

# Install Python dependencies
if [[ "$FULL_TRACEKIT" == "1" ]]; then
  REQ_FILE="$REPO_DIR/Tracekit/requirements.txt"
else
  REQ_FILE="$REPO_DIR/Tracekit/tools/scheduler/requirements.txt"
fi

if [[ -f "$REQ_FILE" ]]; then
  "${PIP_RUN[@]}" install -r "$REQ_FILE"
else
  echo "WARN: Requirements file not found: $REQ_FILE" >&2
fi

# Done
echo "Worker setup completed."
if [[ "$USE_VENV" == "1" ]]; then
  echo "To use the virtualenv: source $REPO_DIR/.venv/bin/activate"
fi

