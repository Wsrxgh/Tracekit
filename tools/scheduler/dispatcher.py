#!/usr/bin/env python3
"""
Dispatcher: assigns local input files to per-node Redis queues by a simple round-robin policy.
No scheduling events are logged; routing is deterministic.
"""
import argparse, os, sys, json, time, subprocess
from pathlib import Path
import redis

DEFAULT_NODES = ["cloud0", "cloud1", "cloud2"]


def list_inputs(dir_path: Path) -> list[Path]:
    files = [p for p in dir_path.glob("*.mp4") if p.is_file()]
    files.sort(key=lambda p: p.name)
    return files


def rr3_assign(files: list[Path], nodes: list[str]) -> dict[str, list[dict]]:
    tasks = {n: [] for n in nodes}
    for idx, p in enumerate(files, start=1):
        n = nodes[(idx - 1) % len(nodes)]
        base = p.stem
        out = f"outputs/{base}_720p_crf28.mp4"  # default; workers can override
        tasks[n].append({
            "input": str(p),
            "output": out,
            "scale": "1280:720",
            "preset": "veryfast",
            "crf": 28,
        })
    return tasks

def probe_duration_seconds(path: Path) -> float:
    try:
        # ffprobe prints duration in seconds (float)
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nokey=1:noprint_wrappers=1", str(path)
        ], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        return float(out)
    except Exception:
        return 0.0


def duration_greedy_assign(files: list[Path], nodes: list[str]) -> dict[str, list[dict]]:
    # LPT-like greedy using duration as weight
    weights = []
    for p in files:
        d = probe_duration_seconds(p)
        weights.append((d, p))
    # sort by duration desc
    weights.sort(key=lambda x: x[0], reverse=True)
    # running load per node
    load = {n: 0.0 for n in nodes}
    tasks = {n: [] for n in nodes}
    for d, p in weights:
        # choose node with min load
        n = min(nodes, key=lambda k: load[k])
        base = p.stem
        out = f"outputs/{base}_720p_crf28.mp4"
        tasks[n].append({
            "input": str(p),
            "output": out,
            "scale": "1280:720",
            "preset": "veryfast",
            "crf": 28,
        })
        load[n] += d
    return tasks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--scale", default="1280:720")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--nodes", default=",".join(DEFAULT_NODES))
    ap.add_argument("--policy", default="rr3")
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    args = ap.parse_args()

    inputs_dir = Path(args.inputs)
    outputs_dir = Path(args.outputs)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    if not nodes:
        print("no nodes provided", file=sys.stderr)
        sys.exit(2)

    files = list_inputs(inputs_dir)
    if not files:
        print("no mp4 files under inputs", file=sys.stderr)
        sys.exit(1)

    # assign
    tasks = {n: [] for n in nodes}
    if args.policy == "rr3":
        tasks = rr3_assign(files, nodes)
    elif args.policy in ("duration-greedy", "lpt-duration"):
        tasks = duration_greedy_assign(files, nodes)
    else:
        print(f"unknown policy: {args.policy}", file=sys.stderr)
        sys.exit(2)

    # override params from CLI and rewrite outputs into provided dir
    for n in tasks:
        for t in tasks[n]:
            t["scale"] = args.scale
            t["preset"] = args.preset
            t["crf"] = args.crf
            base = Path(t["input"]).stem
            t["output"] = str(outputs_dir / f"{base}_{args.scale.replace(':','x')}_crf{args.crf}.mp4")

    # enqueue
    r = redis.Redis.from_url(args.redis)
    total = 0
    for n in nodes:
        q = f"q:{n}"
        for t in tasks[n]:
            r.rpush(q, json.dumps(t))
            total += 1
    print(f"enqueued {total} tasks: " + ", ".join(f"{n}={len(tasks[n])}" for n in nodes))

if __name__ == "__main__":
    main()

