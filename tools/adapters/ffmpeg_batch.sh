#!/usr/bin/env bash
# Run a batch of ffmpeg transcodes using the wrapper, optionally in parallel.
# This is a minimal per-node runner; run it on cloud0/cloud1/cloud2 respectively.
# Usage:
#   RUN_ID=2025... NODE_ID=cloud1 STAGE=cloud \
#   tools/adapters/ffmpeg_batch.sh <inputs_dir> <outputs_dir> <scale> <preset> <crf> [parallel]
# Example:
#   RUN_ID=$RUN_ID NODE_ID=cloud1 STAGE=cloud tools/adapters/ffmpeg_batch.sh inputs outputs 1280:720 veryfast 28 2
set -euo pipefail

if [ $# -lt 5 ]; then
  echo "Usage: $0 <inputs_dir> <outputs_dir> <scale WxH> <preset> <crf> [parallel]" >&2
  exit 2
fi
IN_DIR="$1"; shift
OUT_DIR="$1"; shift
SCALE="$1"; shift
PRESET="$1"; shift
CRF="$1"; shift
PAR=${1:-1}

mkdir -p "$OUT_DIR"

# Build a task list: input_path<TAB>output_path
TASKS=$(mktemp)
find "$IN_DIR" -maxdepth 1 -type f -name '*.mp4' | while read -r f; do
  base=$(basename "$f" .mp4)
  echo -e "$f\t$OUT_DIR/${base}_$(echo "$SCALE" | tr ':' 'x')_crf${CRF}.mp4"
done > "$TASKS"

run_one() {
  IFS=$'\t' read -r IN OUT <<<"$1"
  RUN_ID="${RUN_ID:-}" NODE_ID="${NODE_ID:-vm0}" STAGE="${STAGE:-cloud}" \
  bash "$(dirname "$0")/ffmpeg_wrapper.sh" -i "$IN" -o "$OUT" -- \
    -vf "scale=${SCALE}" -c:v libx264 -preset "$PRESET" -crf "$CRF" -c:a copy
}

export -f run_one
export RUN_ID NODE_ID STAGE SCALE PRESET CRF

# Use xargs parallelism if PAR>1; otherwise run sequentially
if [ "$PAR" -gt 1 ]; then
  cat "$TASKS" | xargs -P "$PAR" -I{} bash -lc 'run_one "$@"' _ {}
else
  while IFS= read -r line; do
    run_one "$line"
  done < "$TASKS"
fi

rm -f "$TASKS"
echo "Batch done on node=${NODE_ID:-vm0}"

