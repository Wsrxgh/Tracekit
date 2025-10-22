#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Any, Dict


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="Multiply VCpu/Ram allocationRatio in experiment JSON by a fixed factor r (effective capacity calibration). Originals are not overwritten.")
    ap.add_argument("--in-experiment", type=Path, required=True)
    ap.add_argument("--out-experiment", type=Path, required=True)
    ap.add_argument("--ratio", type=float, required=True, help="Multiply existing allocationRatio by this factor (e.g., 0.97)")
    args = ap.parse_args()

    try:
        cfg = json.load(open(args.in_experiment, "r", encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"Failed to read {args.in_experiment}: {e}")

    r = float(args.ratio)
    changed = []
    for pol in cfg.get("allocationPolicies", []) or []:
        for flt in pol.get("filters", []) or []:
            if isinstance(flt, dict) and flt.get("type") in ("VCpu", "Ram") and "allocationRatio" in flt:
                try:
                    old = float(flt["allocationRatio"])
                    new = round(old * r, 6)
                    flt["allocationRatio"] = new
                    changed.append({"type": flt.get("type"), "old": old, "new": new})
                except Exception:
                    continue

    write_json(args.out_experiment, cfg)

    mani = {
        "input": str(args.in_experiment),
        "output": str(args.out_experiment),
        "ratio": r,
        "changes": changed,
        "num_changes": len(changed)
    }
    with args.out_experiment.with_suffix(args.out_experiment.suffix + ".manifest.json").open("w", encoding="utf-8") as f:
        json.dump(mani, f, ensure_ascii=False, indent=2)

    print(f"[OK] Wrote {args.out_experiment} (filters updated: {len(changed)})")


if __name__ == "__main__":
    main()

