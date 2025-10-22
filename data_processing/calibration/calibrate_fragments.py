#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path
import json
import pandas as pd


def detect_first_fragment_indices(df: pd.DataFrame) -> pd.Index:
    if "task_id" not in df.columns:
        raise SystemExit("--add-fixed-overhead-ms requires 'task_id' column in fragments parquet")
    # Priority order to choose first fragment per task
    if "start" in df.columns:
        order = df["start"]
    elif "order" in df.columns:
        order = df["order"]
    elif "sequence" in df.columns:
        order = df["sequence"]
    elif "fragment_index" in df.columns:
        order = df["fragment_index"]
    else:
        # Fallback: use existing row order within each task
        order = pd.Series(range(len(df)), index=df.index)
    # Ensure numeric for sorting
    order = pd.to_numeric(order, errors="coerce")
    df2 = df.assign(__ord=order)
    # Pick idx with minimal order per task
    first_idx = df2.sort_values(["task_id", "__ord"], kind="mergesort").groupby("task_id", sort=False).head(1).index
    return first_idx


def main():
    ap = argparse.ArgumentParser(description="Calibrate fragments parquet by scaling durations and/or injecting fixed per-task startup overhead (ms) into the first fragment per task.")
    ap.add_argument("--in-fragments", type=Path, required=True)
    ap.add_argument("--out-fragments", type=Path, required=True)
    ap.add_argument("--scale-duration", type=float, default=1.0, help="Multiply 'duration' by this factor (default 1.0)")
    ap.add_argument("--add-fixed-overhead-ms", type=int, default=0, help="Add δ ms to the first fragment per task (requires 'task_id')")
    args = ap.parse_args()

    df = pd.read_parquet(args.in_fragments)
    if "duration" not in df.columns:
        raise SystemExit("Fragments parquet must contain a 'duration' column (ms)")

    df = df.copy()
    # Scale durations
    if args.scale_duration != 1.0:
        df["duration"] = (pd.to_numeric(df["duration"], errors="coerce") * float(args.scale_duration)).round()
    # Inject fixed overhead
    overhead = int(args.add_fixed_overhead_ms)
    changed_tasks = 0
    if overhead > 0:
        idx = detect_first_fragment_indices(df)
        df.loc[idx, "duration"] = pd.to_numeric(df.loc[idx, "duration"], errors="coerce").fillna(0) + overhead
        changed_tasks = len(idx)

    # Persist
    args.out_fragments.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out_fragments, index=False)

    # Manifest
    mani = {
        "input": str(args.in_fragments),
        "output": str(args.out_fragments),
        "scale_duration": float(args.scale_duration),
        "add_fixed_overhead_ms": overhead,
        "changed_tasks": int(changed_tasks)
    }
    with args.out_fragments.with_suffix(args.out_fragments.suffix + ".manifest.json").open("w", encoding="utf-8") as f:
        json.dump(mani, f, ensure_ascii=False, indent=2)

    print(f"[OK] Wrote {args.out_fragments}  (scale={args.scale_duration}, overhead_ms={overhead}, tasks_changed={changed_tasks})")


if __name__ == "__main__":
    main()

