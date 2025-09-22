#!/usr/bin/env python3
"""
Worker: pulls tasks from per-node queue q:<NODE_ID> and runs ffmpeg via wrapper.
No scheduling events are logged. Only ffmpeg completion is appended by the wrapper.
"""
from __future__ import annotations
import argparse, os, sys, json, shlex, subprocess, signal, threading, socket, psutil, time
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
    if task.get("cpu_weight"):
        env["CPU_WEIGHT"] = str(task["cpu_weight"])  # systemd CPUWeight for shared mode
    if task.get("unit_name"):
        env["UNIT_NAME"] = str(task["unit_name"])
    # RUN_ID passthrough if defined
    if os.getenv("RUN_ID"):
        env["RUN_ID"] = os.getenv("RUN_ID")
    print("EXEC:", shlex.join(cmd))
    return subprocess.call(cmd, env=env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", default="outputs")
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--parallel", type=int, default=0)
    ap.add_argument("--slots-key", default="slots:available")
    ap.add_argument("--capacity-units", type=int, default=0, help="Total CPU capacity units for this worker (default: logical cores)")
    ap.add_argument("--allocation-ratio", type=float, default=1.0, help="Overprovision ratio; cap defaults to floor(ratio * logical_cores) when capacity-units not set")
    ap.add_argument("--cpu-binding", choices=["exclusive", "shared"], default="exclusive", help="exclusive: inject cpuset; shared: inject CPUWeight, no cpuset")
    ap.add_argument("--cpuweight-per-vcpu", type=int, default=100, help="CPUWeight per vCPU when --cpu-binding=shared (systemd CFS weight)")
    ap.add_argument("--reset-capacity", action="store_true", help="Force reset cap:<node> to current computed capacity on startup (override stale state)")
    ap.add_argument("--clear-queue", action="store_true", help="Delete q:<node> on startup (useful for clean tests)")
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
        # Purge stale slot tokens for this node to avoid central blocking on leftovers
        try:
            r.lrem(args.slots_key, 0, node)
        except Exception:
            pass
        # Optionally clear this node's queue for clean tests
        if args.clear_queue:
            try:
                r.delete(qname)
            except Exception:
                pass
        # Concurrency slots: only if parallel>0 (else rely solely on capacity)
        if args.parallel and args.parallel > 0:
            for _ in range(args.parallel):
                r.rpush(args.slots_key, node)
        # CPU capacity units: default to floor(allocation_ratio * logical cores) if not provided
        total_cores = psutil.cpu_count(logical=True) or 1
        if args.capacity_units and args.capacity_units > 0:
            cap_units = int(args.capacity_units)
        else:
            cap_units = max(1, int((args.allocation_ratio if args.allocation_ratio and args.allocation_ratio > 0 else 1.0) * total_cores))
        cap_key = f"cap:{node}"
        # Reset or set if absent
        try:
            if args.reset_capacity:
                r.set(cap_key, cap_units)
            else:
                r.setnx(cap_key, cap_units)
            # Record physical cores and ratio for reference/monitoring
            try:
                r.set(f"phys:{node}", total_cores)
                r.set(f"ratio:{node}", args.allocation_ratio if args.allocation_ratio else 1.0)
            except Exception:
                pass
        except Exception:
            pass
        try:
            r.set(f"cap_total:{node}", cap_units)
        except Exception:
            pass
        print(f"registered slots={args.parallel}, capacity_units={cap_units}, phys_cores={total_cores}, ratio={args.allocation_ratio} for node={node}")
    except Exception as e:
        print("failed to register slots/capacity:", e, file=sys.stderr)

    signal.signal(signal.SIGINT, handle_sigint)

    # CPU core pools for cpuset injection (works regardless of parallel)
    total_cores = psutil.cpu_count(logical=True) or 1
    # Precompute rotation groups for 1c/2c/4c (contiguous blocks); fallback to clamp if not enough cores
    groups_1c = [[i] for i in range(total_cores)] if total_cores >= 1 else [[0]]
    if total_cores >= 2:
        groups_2c = [[i, i+1] for i in range(0, total_cores-1, 2)]
    else:
        groups_2c = [[0]]
    if total_cores >= 4:
        groups_4c = [list(range(i, min(total_cores, i+4))) for i in range(0, total_cores, 4)]
    else:
        groups_4c = [list(range(0, min(total_cores, 4)))]

    # Simple thread pool
    from queue import Queue
    # If parallel<=0, allow unbounded queue; else small multiple
    task_q: Queue[dict] = Queue(maxsize=0 if (not args.parallel or args.parallel <= 0) else args.parallel * 2)

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
        # Decide based on requested cpu_units (strict core cap via cpuset)
        try:
            units = int(task.get("cpu_units", 1))
        except Exception:
            units = 1
        units = max(1, units)
        if total_cores <= 0:
            return None
        if units >= total_cores:
            # Clamp to all cores
            return f"0-{max(0, total_cores-1)}"
        if units == 1:
            grp = groups_1c[slot_idx % len(groups_1c)]
            return ",".join(str(x) for x in grp)
        if units == 2:
            grp = groups_2c[slot_idx % len(groups_2c)]
            return f"{grp[0]}-{grp[-1]}"
        if units == 4:
            grp = groups_4c[slot_idx % len(groups_4c)]
            return f"{grp[0]}-{grp[-1]}"
        # Generic fallback: contiguous block from 0 of length 'units'
        end = min(total_cores, units) - 1
        return f"0-{end}"

    # ----- Shared-mode Max-Min Fairness helpers -----
    active_units = {}
    au_lock = threading.Lock()

    def _sanitize_unit_component(s: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "-" for c in s)

    def gen_unit_name(slot_idx: int) -> str:
        ts = int(time.time() * 1000)
        base = _sanitize_unit_component(node)
        return f"tk-{base}-{ts}-{slot_idx}.scope"

    def waterfill_lambda(reqs, C: float) -> float:
        try:
            C = max(0.0, float(C))
            a = sorted(max(0.0, float(x)) for x in reqs)
        except Exception:
            return 0.0
        n = len(a)
        if n <= 0:
            return 0.0
        prefix = 0.0
        for k in range(n):
            remaining = n - k
            lam = (C - prefix) / remaining if remaining > 0 else 0.0
            if lam <= a[k]:
                return max(0.0, lam)
            prefix += a[k]
        return a[-1]

    def compute_shares_map(units_map: dict, C: float) -> dict:
        # units_map: unit_name -> requested vCPUs (r_i)
        if not units_map:
            return {}
        lam = waterfill_lambda(units_map.values(), C)
        return {u: min(max(0.0, float(r)), lam) for u, r in units_map.items()}


    def worker_loop(slot_idx: int):
        while not STOP.is_set():
            try:
                t = task_q.get(timeout=1)
            except Exception:
                continue
            try:
                # Inject CPU controls depending on binding mode
                unit_name = None
                if args.cpu_binding == "exclusive":
                    # Inject cpuset dynamically if not set
                    dyn_cpuset = next_cpuset_for(t, slot_idx)
                    if dyn_cpuset and not t.get("cpuset"):
                        t["cpuset"] = dyn_cpuset
                else:
                    # shared mode: Max-Min Fairness via dynamic CPUQuota; also keep CPU_WEIGHT as soft hint
                    try:
                        units = int(t.get("cpu_units", 1))
                    except Exception:
                        units = 1
                    units = max(1, units)
                    t.pop("cpuset", None)
                    t["cpu_weight"] = max(1, int(args.cpuweight_per_vcpu) * units)
                    # Generate a stable scope unit name for this ffmpeg so we can adjust quota later
                    unit_name = gen_unit_name(slot_idx)
                    t["unit_name"] = unit_name
                    # Compute water-filling shares including this new task, then:
                    # - apply new CPUQuota to existing units via systemctl set-property
                    # - pass initial CPU_QUOTA to the new task's env (wrapper will start it with that quota)
                    with au_lock:
                        # Build a temp map including the new unit
                        temp_units = dict(active_units)
                        temp_units[unit_name] = units
                        shares = compute_shares_map(temp_units, total_cores)
                        # Apply to existing units
                        for u, share in shares.items():
                            if u == unit_name:
                                continue
                            quota_pct = max(1, int(round(share * 100.0)))
                            try:
                                subprocess.call(["systemctl", "set-property", u, f"CPUQuota={quota_pct}%"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass
                        # Set initial quota for the new unit (wrapper will read env CPU_QUOTA)
                        t["cpu_quota"] = max(1, int(round(shares.get(unit_name, units) * 100.0)))
                        # Register new unit as active
                        active_units[unit_name] = units
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
                # Fairness: on completion, recompute and apply quotas for remaining units (shared mode)
                if args.cpu_binding == "shared":
                    try:
                        u_name = t.get("unit_name")
                        with au_lock:
                            if u_name:
                                active_units.pop(u_name, None)
                            if active_units:
                                shares = compute_shares_map(active_units, total_cores)
                                for u, share in shares.items():
                                    quota_pct = max(1, int(round(share * 100.0)))
                                    try:
                                        subprocess.call(["systemctl", "set-property", u, f"CPUQuota={quota_pct}%"],
                                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                # increment capacity back
                try:
                    cap_key = f"cap:{node}"
                    r.incrby(cap_key, max(1, units))
                except Exception as e:
                    print("failed to return capacity:", e, file=sys.stderr)
                # return one concurrency slot (only if slots are used)
                if args.parallel and args.parallel > 0:
                    try:
                        r.rpush(args.slots_key, node)
                    except Exception as e:
                        print("failed to return slot:", e, file=sys.stderr)
                # decrement running-instance counter (clamp to >=0)
                try:
                    v = r.decrby(f"run_count:{node}", 1)
                    try:
                        v = int(v)
                    except Exception:
                        v = 0
                    if v is not None and v < 0:
                        r.set(f"run_count:{node}", 0)
                except Exception:
                    pass
                task_q.task_done()

    fetch_t = threading.Thread(target=fetch_loop, daemon=True)
    fetch_t.start()

    # Determine worker thread count: if parallel<=0, use cap_units threads; else use parallel
    num_threads = args.parallel if (args.parallel and args.parallel > 0) else max(1, cap_units)
    workers = [threading.Thread(target=lambda idx=i: worker_loop(idx), daemon=True) for i in range(num_threads)]
    for th in workers: th.start()

    try:
        while not STOP.is_set():
            STOP.wait(1)
    finally:
        print("stopping...")

if __name__ == "__main__":
    main()

