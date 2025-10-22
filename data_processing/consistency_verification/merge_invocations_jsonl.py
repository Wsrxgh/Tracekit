#!/usr/bin/env python3
"""
Merge multiple JSONL telemetry files from c0..cN subfolders into a single JSONL.
Supported kinds:
- invocations: 20250904001(c*)/CTS/invocations.jsonl (or cctf/invocations.jsonl for legacy)
- proc_metrics: 20250904001(c*)/CTS/proc_metrics.jsonl (or cctf/proc_metrics.jsonl for legacy)

Default behavior:
- Searches under the script's directory (e.g., 20250904001) for the chosen kind
- Sorts inputs by the (cX) numeric index if present
- Concatenates lines (skips empty/whitespace-only lines)
- Writes to <kind>_merged.jsonl in the script's directory by default

Usage examples:
  # Merge invocations (default)
  python merge_invocations_jsonl.py
  python merge_invocations_jsonl.py --what invocations --output invocations_all.jsonl

  # Merge proc_metrics
  python merge_invocations_jsonl.py --what proc_metrics --proc-output proc_metrics_merged.jsonl
  python merge_invocations_jsonl.py --base-dir D:\\opendc-demos\\20250904001 --what both --validate
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional, Dict


def find_input_files(base_dir: Path) -> List[Path]:
    """Find JSONL inputs matching */CTS/invocations.jsonl or */cctf/invocations.jsonl under base_dir.
    Prefer CTS (new naming) over cctf (legacy). Sort by the numeric index inside parentheses when present.
    """
    # Try CTS first (new naming convention)
    pattern_new = "*/CTS/invocations.jsonl"
    candidates = list(base_dir.glob(pattern_new))

    # Fallback to cctf if CTS not found
    if not candidates:
        pattern_old = "*/cctf/invocations.jsonl"
        candidates = list(base_dir.glob(pattern_old))

    def extract_index(p: Path) -> int:
        # Expect folder like 20250904001(c3)
        m = re.search(r"\(c(\d+)\)", p.as_posix())
        return int(m.group(1)) if m else 999999

    candidates.sort(key=extract_index)
    return candidates


def find_proc_metrics_files(base_dir: Path) -> List[Path]:
    """Find JSONL inputs matching */CTS/proc_metrics.jsonl or */cctf/proc_metrics.jsonl under base_dir.
    Prefer CTS (new naming) over cctf (legacy). Sort by the numeric index inside parentheses when present.
    """
    # Try CTS first (new naming convention)
    pattern_new = "*/CTS/proc_metrics.jsonl"
    candidates = list(base_dir.glob(pattern_new))

    # Fallback to cctf if CTS not found
    if not candidates:
        pattern_old = "*/cctf/proc_metrics.jsonl"
        candidates = list(base_dir.glob(pattern_old))

    def extract_index(p: Path) -> int:
        m = re.search(r"\(c(\d+)\)", p.as_posix())
        return int(m.group(1)) if m else 999999

    candidates.sort(key=extract_index)
    return candidates


def iter_nonempty_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                yield line if line.endswith("\n") else (line + "\n")


def iter_validated_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"Invalid JSON at {path}:{ln}: {e}")
            # Re-dump minified to ensure well-formed JSONL
            yield json.dumps(obj, ensure_ascii=False) + "\n"


def merge_files(inputs: List[Path], output: Path, validate: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = output.open("w", encoding="utf-8", newline="\n")
    try:
        for p in inputs:
            source_iter = iter_validated_lines(p) if validate else iter_nonempty_lines(p)
            for line in source_iter:
                writer.write(line)
    finally:
        writer.close()


def iter_json_objects(path: Path, validate: bool = False):
    """Iterate JSON objects from a JSONL file. If validate=True, raise on invalid JSON; else skip invalid lines."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                if validate:
                    raise SystemExit(f"Invalid JSON at {path}:{ln}: {e}")
                else:
                    continue
            yield obj


def merge_proc_metrics_files(inputs: List[Path], output: Path, validate: bool = False) -> None:
    """Merge proc_metrics JSONL files, filtering out entries with dt_ms == 0.
    Additionally, augment each entry with cpu_freq_mhz from the corresponding cX/cctf/nodes.json if available.
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    # Preload cpu_freq per input file
    freq_map: Dict[Path, Optional[int]] = {}
    for p in inputs:
        nodes_path = p.parent / "nodes.json"
        freq_map[p] = _read_cpu_freq_from_nodes(nodes_path)

    with output.open("w", encoding="utf-8", newline="\n") as writer:
        for p in inputs:
            cpu_freq = freq_map.get(p)
            for obj in iter_json_objects(p, validate=validate):
                # filter out dt_ms == 0
                try:
                    if int(obj.get("dt_ms", 0)) == 0:
                        continue
                except Exception:
                    # If dt_ms is malformed, skip conservatively
                    continue
                if cpu_freq is not None and "cpu_freq_mhz" not in obj:
                    obj["cpu_freq_mhz"] = cpu_freq
                writer.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_cpu_freq_from_nodes(nodes_path: Path) -> Optional[int]:
    if not nodes_path.exists():
        return None
    try:
        with nodes_path.open("r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return None

    # Case 1: top-level dict
    if isinstance(data, dict):
        if "cpu_freq_mhz" in data:
            try:
                return int(data["cpu_freq_mhz"])  # type: ignore[arg-type]
            except Exception:
                return None
        # Sometimes wrapped in a list field
        for key in ("nodes", "items", "data"):
            v = data.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "cpu_freq_mhz" in item:
                        try:
                            return int(item["cpu_freq_mhz"])  # type: ignore[arg-type]
                        except Exception:
                            continue
        return None

    # Case 2: top-level list
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "cpu_freq_mhz" in item:
                try:
                    return int(item["cpu_freq_mhz"])  # type: ignore[arg-type]
                except Exception:
                    continue
    return None


def main():
    parser = argparse.ArgumentParser(description="Merge JSONL telemetry from c0..cN subfolders")
    parser.add_argument("--base-dir", type=Path, default=Path(__file__).resolve().parent,
                        help="Directory that contains 20250904001(c*) subfolders (default: script directory).")
    parser.add_argument("--what", choices=["invocations", "proc_metrics", "both"], default="invocations",
                        help="Which kind of files to merge (default: invocations)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output path for merged invocations (default: <base-dir>/invocations_merged.jsonl)")
    parser.add_argument("--proc-output", type=Path, default=None,
                        help="Output path for merged proc_metrics (default: <base-dir>/proc_metrics_merged.jsonl)")
    parser.add_argument("--validate", action="store_true",
                        help="Validate each line as JSON and re-dump (slower but safer).")
    args = parser.parse_args()

    base_dir: Path = args.base_dir

    if args.what in ("invocations", "both"):
        inv_out: Path = args.output or (base_dir / "invocations_merged.jsonl")
        inv_inputs = find_input_files(base_dir)
        if not inv_inputs:
            print(f"No invocations found under {base_dir}. Expected pattern: */CTS/invocations.jsonl or */cctf/invocations.jsonl")
        else:
            print("Found invocations (in order):")
            for p in inv_inputs:
                print("  -", p)
            print(f"Merging into: {inv_out}")
            merge_files(inv_inputs, inv_out, validate=args.validate)
            print("Invocations merge done.")

    if args.what in ("proc_metrics", "both"):
        proc_out: Path = args.proc_output or (base_dir / "proc_metrics_merged.jsonl")
        proc_inputs = find_proc_metrics_files(base_dir)
        if not proc_inputs:
            print(f"No proc_metrics found under {base_dir}. Expected pattern: */CTS/proc_metrics.jsonl or */cctf/proc_metrics.jsonl")
        else:
            print("Found proc_metrics (in order):")
            for p in proc_inputs:
                print("  -", p)
            print(f"Merging into: {proc_out}")
            merge_proc_metrics_files(proc_inputs, proc_out, validate=args.validate)
            print("proc_metrics merge done.")


if __name__ == "__main__":
    main()

