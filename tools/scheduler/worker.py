#!/usr/bin/env python3
"""
Worker: pulls tasks from per-node queue q:<NODE_ID> and runs ffmpeg via wrapper.
No scheduling events are logged. Only ffmpeg completion is appended by the wrapper.
"""
from __future__ import annotations
import argparse, os, sys, json, shlex, subprocess, signal, threading, socket
from pathlib import Path
import redis

STOP = threading.Event()

def handle_sigint(sig, frame):
    STOP.set()


def run_task(task: dict, root: Path) -> int:
    # Ensure output directory exists
    out_path = Path(task["output"]) if not str(task["output"]).startswith("/") else Path(task["output"])  # allow absolute
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash", str(root / "tools" / "adapters" / "ffmpeg_wrapper.sh"),
        "-i", task["input"],
        "-vf", f"scale={task['scale']}",
        "-c:v", "libx264",
        "-preset", str(task["preset"]),
        "-crf", str(task["crf"]),
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
    args = ap.parse_args()

    node = os.getenv("NODE_ID") or socket.gethostname()
    qname = f"q:{node}"

    r = redis.Redis.from_url(args.redis)

    root = Path(__file__).resolve().parents[2]
    print(f"Worker node={node} queue={qname} redis={args.redis} parallel={args.parallel}")

    # Register available slots tokens according to parallel
    try:
        for _ in range(max(0, args.parallel)):
            r.rpush(args.slots_key, node)
        print(f"registered {args.parallel} slots for node={node} into {args.slots_key}")
    except Exception as e:
        print("failed to register slots:", e, file=sys.stderr)

    signal.signal(signal.SIGINT, handle_sigint)

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

    def worker_loop():
        while not STOP.is_set():
            try:
                t = task_q.get(timeout=1)
            except Exception:
                continue
            try:
                rc = run_task(t, root)
                if rc != 0:
                    print(f"task failed rc={rc}: {t}", file=sys.stderr)
                else:
                    print(f"task ok: {t['input']} -> {t['output']}")
            finally:
                # Return one slot token on completion
                try:
                    r.rpush(args.slots_key, node)
                except Exception as e:
                    print("failed to return slot:", e, file=sys.stderr)
                task_q.task_done()

    fetch_t = threading.Thread(target=fetch_loop, daemon=True)
    fetch_t.start()

    workers = [threading.Thread(target=worker_loop, daemon=True) for _ in range(args.parallel)]
    for th in workers: th.start()

    try:
        while not STOP.is_set():
            STOP.wait(1)
    finally:
        print("stopping...")

if __name__ == "__main__":
    main()

