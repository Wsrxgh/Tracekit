#!/usr/bin/env python3
from __future__ import annotations
import argparse
import glob
import json
from pathlib import Path
from typing import List, Dict, Any


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="Scale cpu.coreSpeed (MHz) in topology JSONs by a factor (a_speed). Originals are not overwritten.")
    ap.add_argument("--topology", nargs="+", help="Topology JSON path(s) or glob(s)")
    ap.add_argument("--factor", type=float, required=True, help="Scale factor, e.g., 0.985 for -1.5% speed")
    ap.add_argument("--suffix", type=str, default="_cal", help="Suffix to append before .json (default: _cal)")
    ap.add_argument("--out-dir", type=Path, default=None, help="Optional output directory; filenames get suffix applied")
    args = ap.parse_args()

    # Expand globs
    paths: List[Path] = []
    for pat in args.topology:
        matches = glob.glob(pat)
        if not matches:
            print(f"[WARN] No match: {pat}")
        paths += [Path(m) for m in matches]
    if not paths:
        raise SystemExit("No input topology files.")

    factor = float(args.factor)

    manifest = {"factor": factor, "outputs": []}

    for p in paths:
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to read {p}: {e}")
            continue

        changed = False
        changes: List[Dict[str, int]] = []
        for cluster in data.get("clusters", []) or []:
            for host in cluster.get("hosts", []) or []:
                cpu = host.get("cpu") or {}
                if isinstance(cpu, dict) and "coreSpeed" in cpu:
                    old = cpu["coreSpeed"]
                    try:
                        new_val = int(round(float(old) * factor))
                        cpu["coreSpeed"] = new_val
                        changed = True
                        changes.append({"old": int(old), "new": int(new_val)})
                    except Exception:
                        continue
        if not changed:
            print(f"[INFO] No coreSpeed found in {p}, skipping write.")
            continue

        out_name = p.stem + args.suffix + p.suffix
        out_path = (args.out_dir / out_name) if args.out_dir else (p.with_name(out_name))
        write_json(out_path, data)

        mani_path = out_path.with_suffix(out_path.suffix + ".manifest.json")
        write_json(mani_path, {"input": str(p), "output": str(out_path), "factor": factor, "num_hosts": len(changes), "changes": changes})
        manifest["outputs"].append({"input": str(p), "output": str(out_path)})
        print(f"[OK] Wrote {out_path}  (hosts updated: {len(changes)})")

    print("[DONE] coreSpeed calibration complete.")


if __name__ == "__main__":
    main()

