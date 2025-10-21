#!/usr/bin/env python3
"""
Filter OpenDC traces to keep only the earliest N tasks by submission_time
and their corresponding fragments.

Usage:
    python3 tools/filter_opendc_topn.py --input opendc_traces_DIR --output out_DIR --topn 20

This script expects input directory to contain tasks.parquet and fragments.parquet
as produced by tools/export_opendc.py
"""
import argparse
from pathlib import Path
import json
import pyarrow as pa
import pyarrow.parquet as pq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='Input directory with tasks.parquet and fragments.parquet')
    ap.add_argument('--output', required=True, help='Output directory for filtered parquet files')
    ap.add_argument('--topn', type=int, default=20, help='Number of earliest tasks by submission_time to keep')
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    tasks_file = inp / 'tasks.parquet'
    frags_file = inp / 'fragments.parquet'

    if not tasks_file.exists() or not frags_file.exists():
        raise SystemExit(f"Missing tasks/fragments parquet under {inp}")

    # Read tasks as Arrow table
    tasks_tab = pq.read_table(tasks_file)
    # Convert to list of dict for easy Python-side sorting without pandas dependency
    tasks_list = tasks_tab.to_pylist()
    # Guard: if empty
    if not tasks_list:
        # Write empty outputs with same schema
        pq.write_table(tasks_tab, out / 'tasks.parquet')
        pq.write_table(pq.read_table(frags_file).slice(0, 0), out / 'fragments.parquet')
        return

    # Sort by submission_time ascending and take first N
    tasks_list.sort(key=lambda r: int(r.get('submission_time', 0)))
    keep = tasks_list[: max(0, args.topn)]
    keep_ids = {int(r['id']) for r in keep if r.get('id') is not None}

    # Rebuild tasks table from keep with the same schema/field order
    # Ensure we keep the same fields exactly
    fields = [f.name for f in tasks_tab.schema]
    keep_rows = []
    for r in keep:
        keep_rows.append({k: r.get(k) for k in fields})
    tasks_keep_tab = pa.Table.from_pylist(keep_rows, schema=tasks_tab.schema)

    # Read fragments and filter by id in keep_ids
    frags_tab = pq.read_table(frags_file)
    # Use pyarrow compute to filter efficiently
    import pyarrow.compute as pc
    if len(keep_ids) == 0:
        frags_keep_tab = frags_tab.slice(0, 0)
    else:
        ids_arr = pa.array(sorted(keep_ids), type=frags_tab.schema.field('id').type)
        mask = pc.is_in(frags_tab['id'], value_set=ids_arr)
        frags_keep_tab = frags_tab.filter(mask)

    # Write outputs
    pq.write_table(tasks_keep_tab, out / 'tasks.parquet')
    pq.write_table(frags_keep_tab, out / 'fragments.parquet')

    # Copy small_datacenter.json if present
    sdc = inp / 'small_datacenter.json'
    if sdc.exists():
        try:
            (out / 'small_datacenter.json').write_text(sdc.read_text())
        except Exception:
            pass

    # Print summary
    print(f"Filtered tasks: {len(keep_ids)} kept out of {tasks_tab.num_rows}")
    print(f"Fragments kept: {frags_keep_tab.num_rows} out of {frags_tab.num_rows}")


if __name__ == '__main__':
    main()

