#!/usr/bin/env python3
"""
Worker: pulls tasks from per-node queue q:<NODE_ID> and runs ffmpeg via wrapper.
No scheduling events are logged. Only ffmpeg completion is appended by the wrapper.
"""
from __future__ import annotations
import argparse, os, sys, json, shlex, subprocess, signal, threading, socket, psutil
from pathlib import Path
import redis

STOP = threading.Event()

def handle_sigint(sig, frame):
    STOP.set()


def run_task(task: dict, root: Path) -> int:
    # Ensure output directory exists
    out_path = Path(task["output"]) if not str(task["output"]).startswith("/") else Path(task["output"])  # allow absolute
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Build ffmpeg command from task fields (supports vcodec=h264|hevc)
    vcodec = str(task.get("vcodec", "h264")).lower()
    libv = "libx265" if vcodec in ("hevc", "h265") else "libx264"
    scale = str(task.get("scale", "1280:720"))
    preset = str(task.get("preset", "veryfast"))
    crf = str(task.get("crf", 28))
    # Optional thread limits from task (vthreads for codec, fthreads for filters)
    vthreads = str(task.get("vthreads", ""))
    fthreads = str(task.get("fthreads", ""))
    cmd = [
        "bash", str(root / "tools" / "adapters" / "ffmpeg_wrapper.sh"),
        "-i", task["input"],
        "-vf", f"scale={scale}",
        "-c:v", libv,
        "-preset", preset,
        "-crf", str(crf),
    ]
    if vthreads and vthreads.isdigit():
        cmd += ["-threads:v", vthreads]
    if fthreads and fthreads.isdigit():
        cmd += ["-filter_threads", fthreads]
    cmd += [
        "-c:a", "copy",
        str(out_path),
    ]
    env = os.environ.copy()
    # RUN_ID optional at this stage; wrapper will write events if present
    env.setdefault("NODE_ID", os.getenv("NODE_ID", "vm0"))
    env.setdefault("STAGE", os.getenv("STAGE", "cloud"))
    # Pass submission time from dispatcher if present
    if "ts_enqueue" in task:
        try:
            env["TS_ENQUEUE"] = str(int(task["ts_enqueue"]))
        except Exception:
            pass
    # Optional hard limits from task
    if task.get("cpuset"):
        env["CPUSET"] = str(task["cpuset"])
    if task.get("cpu_quota"):
        env["CPU_QUOTA"] = str(task["cpu_quota"])  # percent, e.g., 200
    # RUN_ID passthrough if defined
    if os.getenv("RUN_ID"):
        env["RUN_ID"] = os.getenv("RUN_ID")
    print("EXEC:", shlex.join(cmd))
    return subprocess.call(cmd, env=env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", default="outputs")
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--slots-key", default="slots:available")
    ap.add_argument("--capacity-units", type=int, default=0, help="Total CPU capacity units for this worker (default: logical cores)")
    args = ap.parse_args()

    # Ensure psutil is available early
    try:
        import psutil  # noqa: F401
    except Exception as e:
        print("psutil not available; defaulting capacity to 1:", e, file=sys.stderr)
        class _Ps:
            @staticmethod
            def cpu_count(logical=True):
                return 1
        psutil = _Ps()

    node = os.getenv("NODE_ID") or socket.gethostname()
    qname = f"q:{node}"

    r = redis.Redis.from_url(args.redis)

    root = Path(__file__).resolve().parents[2]
    print(f"Worker node={node} queue={qname} redis={args.redis} parallel={args.parallel}")

    # Initialize concurrency slots and CPU capacity counter
    try:
        # Concurrency slots: at most 'parallel' concurrent tasks
        for _ in range(max(0, args.parallel)):
            r.rpush(args.slots_key, node)
        # CPU capacity units: default to logical cores if not provided
        total_cores = psutil.cpu_count(logical=True) or 1
        cap_units = args.capacity_units if args.capacity_units and args.capacity_units > 0 else total_cores
        cap_key = f"cap:{node}"
        # Set only if absent to avoid clobbering during restarts with running tasks
        try:
            r.setnx(cap_key, cap_units)
        except Exception:
            pass
        print(f"registered slots={args.parallel}, capacity_units={cap_units} for node={node}")
    except Exception as e:
        print("failed to register slots/capacity:", e, file=sys.stderr)

    signal.signal(signal.SIGINT, handle_sigint)

    # CPU core pool for cpuset rotation (optional). Detect 4 cores and parallel=2 -> [0-1, 2-3]
    total_cores = psutil.cpu_count(logical=True) or 1
    core_sets = []
    if args.parallel >= 2 and total_cores >= 4:
        core_sets = [(0, 1), (2, 3)]
    elif args.parallel >= 2 and total_cores == 2:
        core_sets = [(0, 1), (0, 1)]

    # Simple thread pool
    from queue import Queue
    task_q: Queue[dict] = Queue(maxsize=args.parallel * 2)

    def fetch_loop():
        while not STOP.is_set():
            try:
                item = r.blpop(qname, timeout=2)
                if item is None:
                    continue
                _, payload = item
                t = json.loads(payload.decode("utf-8"))
                task_q.put(t)
            except Exception as e:
                print("redis error:", e, file=sys.stderr)

    # Track running workers to rotate cpusets
    running_slots = {}
    slot_lock = threading.Lock()

    def next_cpuset_for(task: dict, slot_idx: int) -> str | None:
        # If task specifies cpuset explicitly, honor it
        if task.get("cpuset"):
            return str(task["cpuset"])
        # Otherwise, if we have predefined core sets and the task advertises cpu_units
        if core_sets and int(task.get("cpu_units", 2)) >= 2:
            a, b = core_sets[slot_idx % len(core_sets)]
            return f"{a}-{b}"
        return None

    def worker_loop(slot_idx: int):
        while not STOP.is_set():
            try:
                t = task_q.get(timeout=1)
            except Exception:
                continue
            try:
                # Inject cpuset dynamically if not set
                dyn_cpuset = next_cpuset_for(t, slot_idx)
                if dyn_cpuset and not t.get("cpuset"):
                    t["cpuset"] = dyn_cpuset
                rc = run_task(t, root)
                if rc != 0:
                    print(f"task failed rc={rc}: {t}", file=sys.stderr)
                else:
                    print(f"task ok: {t['input']} -> {t['output']}")
            finally:
                # Return CPU capacity and one concurrency slot on completion
                try:
                    units = int(t.get("cpu_units", 1))
                except Exception:
                    units = 1
                # increment capacity back
                try:
                    cap_key = f"cap:{node}"
                    r.incrby(cap_key, max(1, units))
                except Exception as e:
                    print("failed to return capacity:", e, file=sys.stderr)
                # return one concurrency slot
                try:
                    r.rpush(args.slots_key, node)
                except Exception as e:
                    print("failed to return slot:", e, file=sys.stderr)
                task_q.task_done()

    fetch_t = threading.Thread(target=fetch_loop, daemon=True)
    fetch_t.start()

    workers = [threading.Thread(target=lambda idx=i: worker_loop(idx), daemon=True) for i in range(args.parallel)]
    for th in workers: th.start()

    try:
        while not STOP.is_set():
            STOP.wait(1)
    finally:
        print("stopping...")

if __name__ == "__main__":
    main()

