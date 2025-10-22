#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path


def process_line(obj: dict, scale_cpu: float | None, shift_ms: int | None) -> dict:
    if shift_ms is not None and "ts_ms" in obj:
        try:
            obj["ts_ms"] = int(round(float(obj.get("ts_ms")))) + int(shift_ms)
        except Exception:
            pass
    if scale_cpu is not None and "cpu_usage_mhz" in obj:
        try:
            obj["cpu_usage_mhz"] = float(obj.get("cpu_usage_mhz")) * float(scale_cpu)
        except Exception:
            pass
    return obj


def main():
    ap = argparse.ArgumentParser(description="Calibrate proc_metrics JSONL by scaling cpu_usage_mhz (k) and/or shifting ts_ms (Δt ms). Originals are not overwritten.")
    ap.add_argument("--in-jsonl", type=Path, required=True)
    ap.add_argument("--out-jsonl", type=Path, required=True)
    ap.add_argument("--scale-cpu", type=float, default=None, help="Multiply 'cpu_usage_mhz' by k")
    ap.add_argument("--shift-ms", type=int, default=None, help="Add Δt (ms) to 'ts_ms'")
    args = ap.parse_args()

    n_in = n_out = 0
    args.out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.in_jsonl.open("r", encoding="utf-8", errors="replace") as fi, \
         args.out_jsonl.open("w", encoding="utf-8", newline="\n") as fo:
        for line in fi:
            n_in += 1
            s = line.strip()
            if not s:
                fo.write(line)
                continue
            try:
                obj = json.loads(s)
            except Exception:
                fo.write(line)
                continue
            obj = process_line(obj, args.scale_cpu, args.shift_ms)
            fo.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n_out += 1

    mani = {
        "input": str(args.in_jsonl),
        "output": str(args.out_jsonl),
        "scale_cpu": (float(args.scale_cpu) if args.scale_cpu is not None else None),
        "shift_ms": (int(args.shift_ms) if args.shift_ms is not None else None),
        "lines_in": n_in,
        "lines_out": n_out,
    }
    with args.out_jsonl.with_suffix(args.out_jsonl.suffix + ".manifest.json").open("w", encoding="utf-8") as f:
        json.dump(mani, f, ensure_ascii=False, indent=2)

    print(f"[OK] Wrote {args.out_jsonl} (scale_cpu={args.scale_cpu}, shift_ms={args.shift_ms})")


if __name__ == "__main__":
    main()

