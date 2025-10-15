#!/usr/bin/env python3
"""
Python Adapter (Wrapper) for ffmpeg
- Non-invasive: starts the real ffmpeg binary without modifying it
- Records precise timestamps and emits a JSONL event per invocation
- Cooperates with whitelist sampler by creating a PID sentinel under logs/$RUN_ID/pids
- Optionally launches within a systemd transient scope to apply CPUQuota/CPUWeight
- Optionally pins CPUs via taskset when CPUSET is provided

Environment (set by worker/dispatcher):
- RUN_ID        : trace run identifier (used for logs directory)
- NODE_ID       : logical node name
- STAGE         : environment stage, e.g., cloud/edge
- TS_ENQUEUE    : enqueue timestamp (ms) recorded by dispatcher (preferred)
- UNIT_NAME     : systemd unit name for shared CPU mode (so quotas can be adjusted later)
- CPU_QUOTA     : integer percent (e.g., 200)
- CPU_WEIGHT    : systemd CPUWeight (1-10000)
- CPUSET        : cpuset string for taskset (e.g., "0-3" or "0,1")

Outputs:
- logs/$RUN_ID/events.ffmpeg.jsonl  (one line per invocation)
- logs/$RUN_ID/pids/<pid>           (sentinel for sampler; removed on exit)
"""
from __future__ import annotations
import os, sys, json, subprocess, shlex, time, signal, uuid
from pathlib import Path

# ---- Helpers ----

def now_ms() -> int:
    return int(time.time() * 1000)


def read_proc_start_epoch_ms(pid: int) -> int:
    """Compute process start time in epoch ms using /proc starttime and boot_time.
    Fallback to wall clock if unavailable.
    """
    try:
        # ticks from process start since boot
        with open(f"/proc/{pid}/stat", "rt") as f:
            stat = f.read().split()
        start_ticks = int(stat[21])  # 22nd field
        clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
        # boot time
        btime = None
        with open("/proc/stat", "rt") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.strip().split()[1])
                    break
        if btime is None:
            raise RuntimeError("no btime")
        start_sec = btime + (start_ticks / float(clk_tck))
        return int(round(start_sec * 1000.0))
    except Exception:
        return now_ms()


def which(cmd: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for p in paths:
        candidate = Path(p) / cmd
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def parse_io_from_args(argv: list[str]) -> tuple[str | None, str | None]:
    """Best-effort parse input (-i next) and output (last positional) from ffmpeg args."""
    inp = None
    outp = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-i" and i + 1 < len(argv):
            inp = argv[i + 1]
            i += 1
        elif not a.startswith("-"):
            outp = a  # keep updating; last non-option wins
        i += 1
    return inp, outp


def file_size(path: str | None) -> int:
    if not path:
        return 0
    try:
        return int(Path(path).stat().st_size)
    except Exception:
        return 0


def emit_event(event_path: Path, rec: dict) -> None:
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with open(event_path, "a", buffering=1) as f:  # line-buffered append
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---- Main wrapper ----

def main(argv: list[str]) -> int:
    ROOT = Path(__file__).resolve().parents[2]
    # Load RUN_ID if not set
    run_id = os.environ.get("RUN_ID")
    if not run_id:
        env_file = ROOT / "run_id.env"
        if env_file.exists():
            try:
                for line in env_file.read_text().splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        k, _, v = line.partition("=")
                        if k.strip() == "RUN_ID":
                            run_id = v.strip()
                            break
            except Exception:
                pass
    if not run_id:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    log_dir = ROOT / "logs" / run_id
    pid_dir = log_dir / "pids"
    events_path = log_dir / "events.ffmpeg.jsonl"
    node_id = os.environ.get("NODE_ID", "vm0")
    stage = os.environ.get("STAGE", "cloud")

    # Extract IO args and sizes (best-effort)
    inp, outp = parse_io_from_args(argv)
    bytes_in = file_size(inp)

    # Build launcher: optional systemd-run scope and taskset
    unit_name = os.environ.get("UNIT_NAME", "")
    cpu_quota = os.environ.get("CPU_QUOTA", "")
    cpu_weight = os.environ.get("CPU_WEIGHT", "")
    cpuset = os.environ.get("CPUSET", "")

    launch_prefix: list[str] = []
    sys_ok = False
    systemd_run = which("systemd-run")
    if systemd_run and (unit_name or cpu_quota or cpu_weight):
        # Try to open a scope with CPUAccounting and provided props
        props = ["-p", "CPUAccounting=1"]
        if cpu_quota:
            props += ["-p", f"CPUQuota={cpu_quota}%"]
        if cpu_weight:
            props += ["-p", f"CPUWeight={cpu_weight}"]
        try:
            # Preflight capability
            test_cmd = [systemd_run, "--scope"] + props + ["--", "true"]
            res = subprocess.run(test_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                sys_ok = True
                if unit_name:
                    launch_prefix += [systemd_run, "--scope", "--unit", unit_name] + props + ["--"]
                else:
                    launch_prefix += [systemd_run, "--scope"] + props + ["--"]
        except Exception:
            sys_ok = False
    # Optional cpuset via taskset
    taskset = which("taskset")
    if cpuset and taskset:
        launch_prefix += [taskset, "-c", cpuset]

    # Build ffmpeg command
    ffmpeg = which("ffmpeg") or "ffmpeg"
    cmd = launch_prefix + [ffmpeg] + argv

    # Spawn with hard CPU affinity guard
    preexec = None
    if cpuset:
        def _parse_cpuset(s: str) -> set:
            res = set()
            for part in s.split(','):
                part = part.strip()
                if not part:
                    continue
                if '-' in part:
                    a, b = part.split('-', 1)
                    try:
                        start = int(a); end = int(b)
                        if start <= end:
                            res.update(range(start, end + 1))
                    except Exception:
                        continue
                else:
                    try:
                        res.add(int(part))
                    except Exception:
                        continue
            return res
        cpu_set = _parse_cpuset(cpuset)
        if cpu_set:
            def _preexec():
                try:
                    os.sched_setaffinity(0, cpu_set)
                except Exception:
                    pass
            preexec = _preexec

    p = subprocess.Popen(cmd, preexec_fn=preexec)
    pid = p.pid
    # Early per-thread affinity enforcement (catch threads that widen mask)
    try:
        if cpuset and 'cpu_set' in locals() and cpu_set:
            deadline = time.time() + 5.0  # guard window
            while time.time() < deadline and p.poll() is None:
                try:
                    for tid_name in os.listdir(f"/proc/{pid}/task"):
                        try:
                            tid = int(tid_name)
                        except Exception:
                            continue
                        try:
                            os.sched_setaffinity(tid, cpu_set)
                        except Exception:
                            pass
                except Exception:
                    pass
                time.sleep(0.1)
    except Exception:
        pass


    # PID sentinel
    try:
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / str(pid)).write_text("")
    except Exception:
        pass

    # Signal handling to forward to child and ensure cleanup
    def _forward(sig, frame):
        try:
            p.send_signal(sig)
        except Exception:
            pass
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _forward)
        except Exception:
            pass

    # Precise start time from /proc; end time from now()
    ts_start = read_proc_start_epoch_ms(pid)
    rc = p.wait()
    ts_end = now_ms()

    # Cleanup PID sentinel
    try:
        (pid_dir / str(pid)).unlink(missing_ok=True)  # py3.8+: use try/except for older
    except Exception:
        try:
            f = pid_dir / str(pid)
            if f.exists():
                f.unlink()
        except Exception:
            pass

    bytes_out = file_size(outp)
    ts_enq_env = os.environ.get("TS_ENQUEUE")
    try:
        ts_enqueue = int(ts_enq_env) if ts_enq_env is not None else ts_start
    except Exception:
        ts_enqueue = ts_start

    # Event record (compatible minimal schema)
    rec = {
        "trace_id": str(uuid.uuid4()),
        "span_id": None,
        "parent_id": None,
        "module_id": "ffmpeg",
        "instance_id": None,
        "ts_enqueue": ts_enqueue,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "node": node_id,
        "stage": stage,
        "method": "CLI",
        "path": "ffmpeg",
        "input": os.path.basename(inp) if inp else None,
        "output": os.path.basename(outp) if outp else None,
        "pid": pid,
        "cpuset": cpuset or None,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "status": rc,
    }
    emit_event(events_path, rec)
    print(f"invocation appended → {events_path}", file=sys.stderr)
    return int(rc)


if __name__ == "__main__":
    # Everything after the script name is meant for ffmpeg
    sys.exit(main(sys.argv[1:]))

