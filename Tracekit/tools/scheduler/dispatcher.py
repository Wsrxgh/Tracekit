#!/usr/bin/env python3
"""
Dispatcher: assigns local input files to per-node Redis queues by a simple round-robin policy.
No scheduling events are logged; routing is deterministic.
"""
from __future__ import annotations
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
    # LPT-like greedy using duration as weight (offline assignment)
    weights = []
    for p in files:
        d = probe_duration_seconds(p)
        weights.append((d, p))
    weights.sort(key=lambda x: x[0], reverse=True)
    load = {n: 0.0 for n in nodes}
    tasks = {n: [] for n in nodes}
    for d, p in weights:
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


def duration_pairs(files: list[Path]) -> list[tuple[float, Path]]:
    pairs = []
    for p in files:
        d = probe_duration_seconds(p)
        pairs.append((d, p))
    pairs.sort(key=lambda x: x[0], reverse=True)
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--scale", default="1280:720")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--nodes", default=",".join(DEFAULT_NODES))
    ap.add_argument("--policy", default="rr3")
    # Mixed profiles: fast1080p / medium480p / hevc1080p
    ap.add_argument("--mix", default="", help="e.g., fast1080p=50,medium480p=30,hevc1080p=20")
    ap.add_argument("--total", type=int, default=0, help="Total tasks to generate when using --mix; 0 means per-input one task")
    ap.add_argument("--seed", type=int, default=0, help="Seed to make mixed task sequence reproducible")
    ap.add_argument("--redis", default="redis://localhost:6379/0")

    def parse_mix(mix: str) -> list[tuple[str,int]]:
        pairs = []
        for part in mix.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            try:
                w = int(v)
            except Exception:
                continue
            pairs.append((k.strip(), w))
        return pairs

    def profile_from_name(name: str) -> dict:
        # Returns dict with fields: scale, vcodec, preset, crf
        n = name.strip().lower()
        if n == "fast1080p":
            return {"scale": "1920:1080", "vcodec": "h264", "preset": "fast", "crf": 28, "vthreads": 2, "fthreads": 2, "cpu_units": 2}
        if n == "medium480p":
            return {"scale": "854:480", "vcodec": "h264", "preset": "medium", "crf": 28, "vthreads": 2, "fthreads": 2, "cpu_units": 2}
        if n == "hevc1080p":
            # No fixed cpuset; let worker inject cpuset by cpu_units to strictly cap to 4 cores
            return {"scale": "1920:1080", "vcodec": "hevc", "preset": "medium", "crf": 28, "vthreads": 4, "fthreads": 4, "cpu_units": 4}
        if n in ("light1c", "light480p1c", "light_1c"):
            # Very light profile: 1 core, single-threaded encode & filter (no fixed cpuset to allow rotation)
            return {"scale": "854:480", "vcodec": "h264", "preset": "veryfast", "crf": 28, "vthreads": 1, "fthreads": 1, "cpu_units": 1}
        # fallback: default profile
        return {"scale": "1280:720", "vcodec": "h264", "preset": "veryfast", "crf": 28, "vthreads": 2, "fthreads": 2}

    import random

    # Central pending mode (global FIFO) options
    ap.add_argument("--pending", action="store_true", help="Enqueue to global pending queue q:pending for central scheduler")
    ap.add_argument("--pending-max", type=int, default=6, help="Max length of q:pending; used by fifo mode")
    ap.add_argument("--pending-mode", choices=["pulse", "fifo"], default="pulse", help="pending submission mode: pulse (default) or fifo")
    ap.add_argument("--pulse-size", type=int, default=10, help="Tasks per pulse when pending-mode=pulse")
    ap.add_argument("--pulse-interval", type=float, default=300.0, help="Seconds between pulses when pending-mode=pulse")
    # Dribble mode options (non-pending or online modes)
    ap.add_argument("--drip", action="store_true", help="Enable dribble (small-batch) enqueue loop")
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--dribble-interval", type=float, default=1.0)
    ap.add_argument("--backlog-limit", type=int, default=1, help="Max queued tasks per node (approx by LLEN)")
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
    # Mixed profiles handling
    mix_pairs = parse_mix(args.mix) if args.mix else []
    rng = random.Random(args.seed) if args.seed else random.Random()

    if mix_pairs:
        # Create a list of (profile_name) respecting weights
        if args.total > 0:
            # Proportional split with rounding and remainder fix
            total_w = sum(max(0, w) for _, w in mix_pairs) or 1
            base_counts = {name: (args.total * max(0, w)) // total_w for name, w in mix_pairs}
            assigned = sum(base_counts.values())
            # Distribute remainder by largest fractional part (here approximate by weight order)
            remainder = args.total - assigned
            order = sorted(mix_pairs, key=lambda x: x[1], reverse=True)
            i = 0
            while remainder > 0 and i < len(order):
                base_counts[order[i][0]] += 1
                remainder -= 1
                i += 1
            profile_sequence = []
            for name, _ in mix_pairs:
                profile_sequence += [name] * base_counts.get(name, 0)
            # Shuffle deterministically by seed
            rng.shuffle(profile_sequence)
            # Pair inputs cyclically to reach total
            tasks = {n: [] for n in nodes}
            for idx in range(len(profile_sequence)):
                p = files[idx % len(files)]
                prof_name = profile_sequence[idx]
                prof = profile_from_name(prof_name)
                base = p.stem
                # unique suffix by index
                suffix = f"{prof['scale'].replace(':','x')}_{prof['vcodec']}_{prof['preset']}_n{idx:04d}"
                out = str(outputs_dir / f"{base}_{suffix}.mp4")
                # Choose node by rr across nodes for fairness
                n = nodes[idx % len(nodes)]
                t = {"input": str(p), "output": out, "_seq": idx, **prof}
                tasks[n].append(t)
        else:
            # Per-input choose a profile by weighted random (seeded)
            weights = [max(0, w) for _, w in mix_pairs]
            names = [name for name, _ in mix_pairs]
            total_w = sum(weights) or 1
            # Build cumulative for sampling
            cum = []
            s = 0
            for w in weights:
                s += w
                cum.append(s)
            tasks = {n: [] for n in nodes}
            for idx, p in enumerate(files):
                r = rng.randint(1, s)
                # find bucket
                j = 0
                while j < len(cum) and r > cum[j]:
                    j += 1
                prof_name = names[min(j, len(names)-1)]
                prof = profile_from_name(prof_name)
                base = p.stem
                suffix = f"{prof['scale'].replace(':','x')}_{prof['vcodec']}_{prof['preset']}"
                out = str(outputs_dir / f"{base}_{suffix}.mp4")
                n = nodes[(idx) % len(nodes)]
                t = {"input": str(p), "output": out, "_seq": idx, **prof}
                tasks[n].append(t)
    elif args.policy == "rr3":
        tasks = rr3_assign(files, nodes)
    elif args.policy in ("duration-greedy", "lpt-duration"):
        tasks = duration_greedy_assign(files, nodes)
    elif args.policy in ("duration-online", "online-duration"):
        # Online dribble: decide destination per task based on current backlog and running load
        pairs = duration_pairs(files)  # [(dur, Path), ...] sorted desc
        r = redis.Redis.from_url(args.redis)
        load = {n: 0.0 for n in nodes}  # estimated cumulative load
        total = 0
        idx = 0
        while idx < len(pairs):
            # refresh backlog lengths
            backlog = {n: int(r.llen(f"q:{n}")) for n in nodes}
            sent = 0
            while sent < args.batch_size and idx < len(pairs):
                d, p = pairs[idx]
                # choose nodes whose backlog < limit; if none, break to wait
                candidates = [n for n in nodes if backlog.get(n, 0) < args.backlog_limit]
                if not candidates:
                    break
                # among candidates, prefer smaller backlog, then smaller load
                n = min(candidates, key=lambda k: (backlog.get(k, 0), load.get(k, 0)))
                base = p.stem
                t = {
                    "input": str(p),
                    "output": str(outputs_dir / f"{base}_{args.scale.replace(':','x')}_crf{args.crf}.mp4"),
                    "scale": args.scale, "preset": args.preset, "crf": args.crf,
                }
                r.rpush(f"q:{n}", json.dumps(t))
                backlog[n] = backlog.get(n, 0) + 1
                load[n] = load.get(n, 0) + max(0.0, d)
                total += 1
                sent += 1
                idx += 1
            print(f"[online] batch={sent}, total={total}, backlog=" + ", ".join(f"{k}:{backlog[k]}" for k in nodes))
            if idx < len(pairs):
                time.sleep(max(0.0, args.dribble_interval))
        print(f"[online] done, total enqueued={total}")
        return
    else:
        print(f"unknown policy: {args.policy}", file=sys.stderr)
        sys.exit(2)

    # override params from CLI and rewrite outputs into provided dir (only when not using --mix)
    if not mix_pairs:
        for n in tasks:
            for t in tasks[n]:
                t["scale"] = args.scale
                t["preset"] = args.preset
                t["crf"] = args.crf
                base = Path(t["input"]).stem
                t["output"] = str(outputs_dir / f"{base}_{args.scale.replace(':','x')}_crf{args.crf}.mp4")

    # enqueue
    r = redis.Redis.from_url(args.redis)

    # Global pending mode: pack all tasks into a single list
    if args.pending:
        # Flatten tasks list; if tasks carry _seq, sort by it for strict global FIFO
        global_list = []
        for n in nodes:
            for t in tasks[n]:
                global_list.append(t)
        if any((isinstance(t, dict) and "_seq" in t) for t in global_list):
            global_list.sort(key=lambda x: x.get("_seq", 1<<30))
        idx = 0
        total = 0
        last_enq_ms = 0

        if args.pending_mode == "pulse":
            # Periodic pulse submission: push pulse-size tasks every pulse-interval seconds
            while idx < len(global_list):
                sent = 0
                while sent < max(1, args.pulse_size) and idx < len(global_list):
                    t = global_list[idx]
                    now_ms = int(time.time() * 1000)
                    if now_ms <= last_enq_ms:
                        now_ms = last_enq_ms + 1
                    last_enq_ms = now_ms
                    t["ts_enqueue"] = now_ms
                    r.rpush("q:pending", json.dumps(t))
                    total += 1
                    sent += 1
                    idx += 1
                    # Stagger within a pulse: space tasks by ~1s instead of same timestamp
                    if sent < max(1, args.pulse_size) and idx < len(global_list):
                        time.sleep(1.0)
                print(f"[pending-pulse] enqueued pulse={sent}, total={total}")
                if idx < len(global_list):
                    time.sleep(max(0.0, args.pulse_interval))
            print(f"[pending-pulse] done, total enqueued={total}")
            return
        else:
            # FIFO with pending_max guard and small dribble sleep
            while idx < len(global_list):
                qlen = int(r.llen("q:pending"))
                sent = 0
                while sent < args.batch_size and idx < len(global_list):
                    if qlen >= args.pending_max:
                        break
                    t = global_list[idx]
                    now_ms = int(time.time() * 1000)
                    if now_ms <= last_enq_ms:
                        now_ms = last_enq_ms + 1
                    last_enq_ms = now_ms
                    t["ts_enqueue"] = now_ms
                    r.rpush("q:pending", json.dumps(t))
                    qlen += 1
                    total += 1
                    sent += 1
                    idx += 1
                if sent > 0:
                    print(f"[pending] enqueued batch={sent}, total={total}, qlen={qlen}")
                if idx < len(global_list):
                    time.sleep(max(0.0, args.dribble_interval))
            print(f"[pending] done, total enqueued={total}")
            return

    if not args.drip:
        total = 0
        for n in nodes:
            q = f"q:{n}"
            for t in tasks[n]:
                r.rpush(q, json.dumps(t))
                total += 1
        print(f"enqueued {total} tasks: " + ", ".join(f"{n}={len(tasks[n])}" for n in nodes))
    else:
        # Dribble mode: small batches, refresh backlog (LLEN) between batches over the offline plan
        global_list = []
        for n in nodes:
            for t in tasks[n]:
                global_list.append((n, t))
        idx = 0
        total = 0
        while idx < len(global_list):
            backlog = {n: int(r.llen(f"q:{n}")) for n in nodes}
            sent = 0
            while sent < args.batch_size and idx < len(global_list):
                n, t = global_list[idx]
                if backlog.get(n, 0) < args.backlog_limit:
                    r.rpush(f"q:{n}", json.dumps(t))
                    backlog[n] = backlog.get(n, 0) + 1
                    total += 1
                    sent += 1
                idx += 1
            print(f"[drip] enqueued batch={sent}, total={total}, backlog=" + ", ".join(f"{k}:{backlog[k]}" for k in nodes))
            if idx < len(global_list):
                time.sleep(max(0.0, args.dribble_interval))
        print(f"[drip] done, total enqueued={total}")

if __name__ == "__main__":
    main()

