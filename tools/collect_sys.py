#!/usr/bin/env python3
# Lightweight Python collector: start/stop host samplers and per-PID sampler
# Features:
# - start/stop subcommands (like collect_sys.sh)
# - per-PID sampling: whitelist via PROC_PID_DIR or fallback regex scan via PROC_MATCH
# - interval default 200ms (PROC_INTERVAL_MS), with sleep compensation
# - RSS via /proc/<pid>/statm (resident pages * 4KB)
# - STOP_ALL support: kill stray mpstat/vmstat on stop
# - Process-group friendly kill on stop

from __future__ import annotations
import os, sys, time, json, signal, subprocess, re, socket
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = os.getenv("RUN_ID", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
LOG_DIR = ROOT / "logs" / RUN_ID
LOG_DIR.mkdir(parents=True, exist_ok=True)
NODE_ID = os.getenv("NODE_ID") or socket.gethostname()
STAGE = os.getenv("STAGE", "cloud")
VM_IP = os.getenv("VM_IP", "127.0.0.1")
IFACE = os.getenv("IFACE", "")
PROC_MATCH = os.getenv("PROC_MATCH", "python|uvicorn|gunicorn|ffmpeg|onnx|onnxruntime|java|node|nginx|torchserve")

def _getenv_int(name: str, default: int) -> int:
    val = os.getenv(name, None)
    try:
        return int(val) if (val is not None and str(val).strip() != "") else int(default)
    except Exception:
        return int(default)

PROC_INTERVAL_MS = _getenv_int("PROC_INTERVAL_MS", 200)
PROC_PID_DIR = os.getenv("PROC_PID_DIR", "")
PROC_REFRESH = os.getenv("PROC_REFRESH", "1")
STOP_ALL = os.getenv("STOP_ALL", "0")

OUT_PROC = LOG_DIR / "proc_metrics.jsonl"
PID_MPSTAT = LOG_DIR / "mpstat.pid"
PID_IFSTAT = LOG_DIR / "ifstat.pid"
PID_VMSTAT = LOG_DIR / "vmstat.pid"
PID_PROCMON = LOG_DIR / "procmon.pid"

COMM_REGEX = re.compile(PROC_MATCH, re.IGNORECASE)


def _default_iface() -> str:
    if IFACE:
        return IFACE
    # Best-effort: use default route iface to cloud0
    try:
        if VM_IP and VM_IP not in ("127.0.0.1", "localhost"):
            cmd = "ip route get {} | awk '/dev/ {{for(i=1;i<=NF;i++) if ($i==\"dev\") print $(i+1)}}' | head -n1".format(VM_IP)
            out = subprocess.check_output(["bash", "-lc", cmd], text=True).strip()
            if out:
                return out
    except Exception:
        pass
    try:
        out = subprocess.check_output(["bash", "-lc", "ip route show default | awk '/default/ {print $5; exit}'"], text=True).strip()
        return out or "lo"
    except Exception:
        return "lo"



def write_node_meta() -> None:
    host = subprocess.check_output(["bash", "-lc", "hostname"], text=True).strip()
    # cpu cores
    try:
        cores = int(subprocess.check_output(["bash", "-lc", "nproc"], text=True).strip())
    except Exception:
        cores = 1
    # mem MB
    try:
        mem_kb = int(subprocess.check_output(["bash", "-lc", "awk '/MemTotal/ {print $2}' /proc/meminfo"], text=True).strip())
        mem_mb = mem_kb // 1024
    except Exception:
        mem_mb = 0
    # cpu model & max MHz
    cpu_model = subprocess.check_output(["bash", "-lc", "lscpu | awk -F: '/Model name/ {sub(/^ +/,\"\",$2); print $2; exit}'"], text=True).strip()
    if not cpu_model:
        try:
            cpu_model = subprocess.check_output(["bash", "-lc", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //'"], text=True).strip()
        except Exception:
            cpu_model = ""
    # Prefer explicit override via env
    env_mhz = os.getenv("CPU_FREQ_MHZ")
    cpu_mhz = 0
    if env_mhz:
        try:
            cpu_mhz = int(float(env_mhz))
        except Exception:
            cpu_mhz = 0
    # Try lscpu CPU max MHz first
    if cpu_mhz <= 0:
        try:
            cpu_mhz_str = subprocess.check_output(["bash", "-lc", "lscpu | awk -F: '/CPU max MHz/ {sub(/^ +/,\"\",$2); print $2; exit}'"], text=True).strip()
            cpu_mhz = int(float(cpu_mhz_str)) if cpu_mhz_str else 0
        except Exception:
            cpu_mhz = 0
    # Fallback to current CPU MHz from lscpu (often stable on KVM/QEMU VMs)
    if cpu_mhz <= 0:
        try:
            cpu_mhz_str2 = subprocess.check_output(["bash", "-lc", "lscpu | awk -F: '/^CPU MHz:/ {sub(/^ +/,\"\",$2); print $2; exit}'"], text=True).strip()
            cpu_mhz = int(float(cpu_mhz_str2)) if cpu_mhz_str2 else 0
        except Exception:
            cpu_mhz = 0
    # Final fallback: /proc/cpuinfo
    if cpu_mhz <= 0:
        try:
            cpu_mhz = int(float(subprocess.check_output(["bash", "-lc", "awk -F: '/cpu MHz/ {gsub(/^ +/,\"\",$2); print $2; exit}' /proc/cpuinfo"], text=True).strip()))
        except Exception:
            cpu_mhz = 0
    obj = {
        "run_id": RUN_ID, "node": NODE_ID, "stage": STAGE, "host": host,
        "iface": _default_iface(), "cpu_cores": cores, "mem_mb": mem_mb,
        "cpu_model": cpu_model, "cpu_freq_mhz": cpu_mhz,
    }
    (LOG_DIR / "node_meta.json").write_text(json.dumps(obj))




def _pg_kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, 0)
    except Exception:
        return
    # Try kill process
    try:
        os.kill(pid, sig)
    except Exception:
        pass
    # Kill process group
    try:
        os.killpg(pid, sig)
    except Exception:
        pass


def start_host_samplers() -> None:
    # Write node metadata first (host CPU/MEM samplers disabled)
    write_node_meta()


def list_pids_whitelist() -> List[int]:
    if not PROC_PID_DIR:
        return []
    pdir = (ROOT / PROC_PID_DIR) if not PROC_PID_DIR.startswith("/") else Path(PROC_PID_DIR)
    pdir.mkdir(parents=True, exist_ok=True)
    pids: List[int] = []
    try:
        for f in pdir.iterdir():
            if not f.is_file():
                continue
            try:
                pid = int(f.name)
            except ValueError:
                continue
            # validate comm matches
            try:
                with open(f"/proc/{pid}/comm", "r") as fh:
                    comm = fh.read().strip()
                if not COMM_REGEX.search(comm or ""):
                    # stale or alien pid; remove sentinel
                    try:
                        f.unlink()
                    except Exception:
                        pass
                    continue
            except Exception:
                # /proc not present
                try:
                    f.unlink()
                except Exception:
                    pass
                continue
            pids.append(pid)
    except FileNotFoundError:
        return []
    return pids


def list_pids_scan() -> List[int]:
    pids: List[int] = []
    for entry in Path("/proc").iterdir():
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(entry / "comm", "r") as fh:
                comm = fh.read().strip()
            if COMM_REGEX.search(comm or ""):
                pids.append(pid)
        except Exception:
            continue
    return pids


def read_stat(pid: int) -> Optional[tuple[int, int]]:
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            s = fh.read()
        # Extract after last ')'
        r = s.rsplit(')', 1)[-1].strip().split()
        # utime=14, stime=15 in the full stat; in r (after comm & state) they are at indexes 11 and 12
        ut = int(r[11])
        st = int(r[12])
        return ut, st
    except Exception:
        return None


def read_rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/statm", "r") as fh:
            parts = fh.read().split()
        pages = int(parts[1]) if len(parts) > 1 else 0
        return pages * 4
    except Exception:
        return 0


def run_procmon_loop() -> None:
    # Write our pid
    PID_PROCMON.write_text(str(os.getpid()))
    interval_ms = max(1, PROC_INTERVAL_MS)
    out_path = OUT_PROC
    out_fh = open(out_path, "a", buffering=1)
    try:
        while True:
            t0 = time.monotonic_ns()
            ts_ms = int(time.time() * 1000)
            # Choose PID source
            pids = list_pids_whitelist() if PROC_PID_DIR else list_pids_scan()
            for pid in pids:
                st = read_stat(pid)
                if not st:
                    continue
                ut, stime = st
                rss_kb = read_rss_kb(pid)
                rec = {"ts_ms": ts_ms, "pid": pid, "rss_kb": rss_kb, "utime": ut, "stime": stime}
                try:
                    out_fh.write(json.dumps(rec) + "\n")
                except Exception:
                    pass
            t1 = time.monotonic_ns()
            elapsed_ms = (t1 - t0) / 1_000_000.0
            sleep_ms = max(0.0, interval_ms - elapsed_ms)
            time.sleep(sleep_ms / 1000.0)
    except KeyboardInterrupt:
        pass
    finally:
        out_fh.close()



# --- Best-effort time synchronization (public NTP pool) ---
# Runs at start() if TIME_SYNC=1 (default). Requires sudo/root to actually set time.
# If no privileges, skips silently but logs before/after UTC for auditing.
def _time_sync() -> None:
    try:
        if os.getenv("TIME_SYNC", "1") != "1":
            return
        from datetime import datetime, timezone
        import shutil
        log = LOG_DIR / "timesync.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        pool = os.getenv("NTP_POOL", "pool.ntp.org")
        def _now_iso() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"== Time sync start ==\n")
            fh.write(f"Host: {socket.gethostname()}  UTC(before): {_now_iso()}\n")
            # Check privilege: root or passwordless sudo
            def _can_sudo_nopass() -> bool:
                try:
                    r = subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return r.returncode == 0
                except Exception:
                    return False
            is_root = False
            try:
                is_root = (os.geteuid() == 0)
            except Exception:
                is_root = False
            can_priv = is_root or _can_sudo_nopass()
            def run(cmd: str) -> None:
                # Use sudo -n if not root
                prefix = [] if is_root else (["sudo", "-n"] if can_priv else [])
                subprocess.run(["bash", "-lc", cmd], stdout=fh, stderr=subprocess.STDOUT)
            if can_priv:
                if shutil.which("chronyc"):
                    run("chronyc -a 'burst 4/4' || true")
                    run("chronyc -a makestep || true")
                    run("chronyc tracking || true")
                elif shutil.which("timedatectl"):
                    run("timedatectl set-ntp true || true")
                    run("sleep 1; timedatectl timesync-status || true")
                elif shutil.which("ntpdate"):
                    run(f"ntpdate -u {pool} || true")
                elif shutil.which("sntp"):
                    run(f"sntp -sS {pool} || true")
                else:
                    fh.write("WARN: no time sync client found (chronyc/timedatectl/ntpdate/sntp)\n")
            else:
                fh.write("INFO: skip time sync (no sudo privileges); relying on existing system sync.\n")
            fh.write(f"UTC(after):  {_now_iso()}\n")
            fh.write("== Time sync end ==\n")
    except Exception:
        # Never block start on time sync issues
        pass

def start() -> None:
    _time_sync()
    # Host samplers
    start_host_samplers()
    # Per-PID sampler (background)
    # Launch a background python procmon
    cmd = [sys.executable, str(Path(__file__).resolve()), "run-procmon"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(LOG_DIR / "procmon.err", "a"))
    PID_PROCMON.write_text(str(proc.pid))
    print(f"proc sampler started (mode={'whitelist' if PROC_PID_DIR else 'scan'}, interval={PROC_INTERVAL_MS/1000:.3f}s) → {OUT_PROC}")
    print(f"collectors started → {LOG_DIR}")


def stop() -> None:
    # Stop by pid files
    for pf in [PID_PROCMON, PID_MPSTAT, PID_IFSTAT, PID_VMSTAT]:
        try:
            if pf.exists():
                pid = int(pf.read_text().strip())
                _pg_kill(pid, signal.SIGTERM)
                time.sleep(0.2)
                _pg_kill(pid, signal.SIGKILL)
                pf.unlink(missing_ok=True)
        except Exception:
            pass
    # Fallback: kill by patterns
    if STOP_ALL == "1":
        patterns = ["mpstat 1$", "ifstat -i .* -t 1", "vmstat -Sm -t 1"]
        for pat in patterns:
            try:
                subprocess.call(["pkill", "-f", pat])
            except Exception:
                pass
    print("collectors stopped")


def main():
    if len(sys.argv) < 2:
        print("Usage: collect_sys.py [start|stop|run-procmon]", file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "run-procmon":
        run_procmon_loop()
    else:
        print("Unknown command", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

