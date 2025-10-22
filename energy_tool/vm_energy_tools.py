
import argparse, time, os, csv, sys, threading, select
from collections import deque
from datetime import datetime

def label_for_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if "scaphandre" in parts:
        try:
            i = parts.index("scaphandre")
            if i + 1 < len(parts):
                return parts[i + 1]
        except ValueError:
            pass
    return os.path.basename(os.path.dirname(path))

def read_energy_uj(path: str) -> int:
    with open(path, "r") as f:
        return int(f.read().strip())

def moving_avg(win, val, n):
    win.append(val)
    if len(win) > n: win.pop(0)
    return sum(win)/len(win) if win else 0.0

def cmd_ts(files, interval, count, out_path, smooth, manual_stop, separate_files):
    labels = [label_for_path(p) for p in files]
    prev_e = {}
    for p in files:
        try: prev_e[p] = read_energy_uj(p)
        except Exception: prev_e[p] = 0
    prev_t = time.time()

    # Create timestamped output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_dir = os.path.join("csv", f"run_{timestamp}")
    os.makedirs(csv_dir, exist_ok=True)

    # Compute output path within the timestamped directory
    base_name = os.path.splitext(os.path.basename(os.path.expanduser(out_path)))[0]
    ext = os.path.splitext(os.path.basename(os.path.expanduser(out_path)))[1] or '.csv'

    if separate_files:
        # Create a separate output file for each VM
        out_files = {}
        for p, lab in zip(files, labels):
            # Generate per-VM filename under the timestamped directory
            vm_out_path = os.path.join(csv_dir, f"{base_name}_{lab}{ext}")
            out_files[p] = vm_out_path

            # Write CSV header
            header = ["ts", f"{lab}_energy_uj", f"{lab}_power_w"]
            if smooth and smooth > 1:
                header += [f"{lab}_power_w_sma{smooth}"]
            with open(vm_out_path, "w", newline="") as f:
                csv.writer(f).writerow(header)
    else:
        # Single-file mode; also saved under the timestamped directory
        out_path = os.path.join(csv_dir, f"{base_name}{ext}")
        header = ["ts"]
        for lab in labels:
            header += [f"{lab}_energy_uj", f"{lab}_power_w"]
            if smooth and smooth > 1:
                header += [f"{lab}_power_w_sma{smooth}"]
        with open(out_path, "w", newline="") as f:
            csv.writer(f).writerow(header)

    smoothers = {p: [] for p in files}

    # Manual-stop mode setup
    stop_flag = threading.Event()
    if manual_stop:
        print("Sampling started. Press Enter to stop...")
        def wait_for_enter():
            input()
            stop_flag.set()
        threading.Thread(target=wait_for_enter, daemon=True).start()
        max_iterations = float('inf')  # Loop indefinitely until manually stopped
    else:
        max_iterations = count

    iteration = 0
    while iteration < max_iterations and not stop_flag.is_set():
        time.sleep(interval)
        t = time.time()
        dt = max(t - prev_t, 1e-9)

        if separate_files:
            # Write data to each VM's file separately
            for p, lab in zip(files, labels):
                try: e_now = read_energy_uj(p)
                except Exception: e_now = prev_e.get(p, 0)
                de_uj = max(e_now - prev_e.get(p, 0), 0)
                pw = (de_uj / 1_000_000.0) / dt   # uJ -> J, then divide by seconds = W

                row = [int(t), e_now, f"{pw:.6f}"]
                if smooth and smooth > 1:
                    sma = moving_avg(smoothers[p], pw, smooth)
                    row += [f"{sma:.6f}"]

                with open(out_files[p], "a", newline="") as f:
                    csv.writer(f).writerow(row)
                prev_e[p] = e_now
        else:
            # Single-file mode (original behavior)
            row = [int(t)]
            for p, lab in zip(files, labels):
                try: e_now = read_energy_uj(p)
                except Exception: e_now = prev_e.get(p, 0)
                de_uj = max(e_now - prev_e.get(p, 0), 0)
                pw = (de_uj / 1_000_000.0) / dt   # uJ -> J, then divide by seconds = W
                row += [e_now, f"{pw:.6f}"]
                if smooth and smooth > 1:
                    sma = moving_avg(smoothers[p], pw, smooth)
                    row += [f"{sma:.6f}"]
                prev_e[p] = e_now

            with open(out_path, "a", newline="") as f:
                csv.writer(f).writerow(row)

        prev_t = t
        iteration += 1

        # Show progress (only in non-manual-stop mode)
        if not manual_stop and iteration % 10 == 0:
            print(f"Sampled {iteration}/{count} times...")

    if manual_stop:
        print(f"Sampling stopped manually after {iteration} samples.")

    if separate_files:
        print(f"CSV files written to directory: {csv_dir}")
        for p, lab in zip(files, labels):
            print(f"  {lab} -> {out_files[p]}")
    else:
        print(f"CSV written -> {out_path}")

    print(f"All files saved in: {csv_dir}")

def cmd_once(files, duration, until_enter):
    labels = [label_for_path(p) for p in files]
    start = {}
    for p in files:
        try: start[p] = read_energy_uj(p)
        except Exception:
            print(f"[ERROR] Cannot read {p}.", file=sys.stderr); return 2
    t0 = time.time()
    if duration is not None: time.sleep(duration)
    else: input("Press Enter to stop measurement...")
    t1 = time.time(); dt = max(t1 - t0, 1e-9)
    print(f"Duration: {dt:.3f}s")
    for p, lab in zip(files, labels):
        end = read_energy_uj(p)
        de_uj = max(end - start[p], 0)
        joules = de_uj / 1_000_000.0
        avg_w = joules / dt
        print(f"{lab}: {joules:.6f} J   avg {avg_w:.3f} W   ({p})")
    return 0

def main():
    ap = argparse.ArgumentParser(description="Per-VM energy tools (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_ts = sub.add_parser("ts", help="Sample power time series to CSV")
    ap_ts.add_argument("--files", nargs="+", required=True, help="Paths to each VM's energy_uj file")
    ap_ts.add_argument("-i","--interval", type=float, default=1.0, help="Sampling interval in seconds (default: 1)")
    ap_ts.add_argument("-n","--count", type=int, default=120, help="Number of samples (default: 120; ignored in manual-stop mode)")
    ap_ts.add_argument("-o","--out", default="~/power_vm.csv", help="Output CSV path")
    ap_ts.add_argument("--smooth", type=int, default=0, help="Optional simple moving average window size (samples), 0=off")
    ap_ts.add_argument("--manual-stop", action="store_true", help="Manual-stop mode: press Enter to stop sampling")
    ap_ts.add_argument("--separate-files", action="store_true", help="Generate a separate CSV for each VM")

    ap_once = sub.add_parser("once", help="Measure total energy and average power over an interval")
    ap_once.add_argument("--files", nargs="+", required=True, help="Paths to each VM's energy_uj file")
    ap_once.add_argument("-d","--duration", type=float, default=None, help="Measurement duration in seconds")
    ap_once.add_argument("--until-enter", action="store_true", help="Stop measurement when Enter is pressed")

    args = ap.parse_args()

    files = [os.path.expanduser(p) for p in (args.files or [])]
    for p in files:
        if not os.path.isfile(p):
            print(f"[ERROR] Not a file: {p}", file=sys.stderr); sys.exit(2)

    if args.cmd == "ts":
        cmd_ts(files, args.interval, args.count, args.out, args.smooth, args.manual_stop, args.separate_files)
    elif args.cmd == "once":
        if (args.duration is None) and (not args.until_enter):
            print("Either --duration or --until-enter is required.", file=sys.stderr); sys.exit(2)
        sys.exit(cmd_once(files, args.duration, args.until_enter))

if __name__ == "__main__":
    main()
