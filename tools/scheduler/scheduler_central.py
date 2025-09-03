#!/usr/bin/env python3
"""
Central scheduler (baseline):
- Single global FIFO pending queue q:pending
- Per-node work queues q:<node>
- Available-slot pool slots:available (one token per free slot). Token value is node_id.

Dispatch loop:
  BRPOP slots:available -> node_id
  LPOP  q:pending -> task
  if no task: RPUSH node_id back to slots:available and continue
  else: RPUSH task to q:<node_id>

Notes:
- Submission time (ts_enqueue) is set by dispatcher when pushing into q:pending.
- No advanced policy: first available node takes the next task.
- Redis URL comes from --redis (default redis://localhost:6379/0)
- Nodes list is not required; tokens arriving are the source of truth.
"""
from __future__ import annotations
import argparse, json, sys
import redis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--pending", default="q:pending")
    ap.add_argument("--slots", default="slots:available")
    args = ap.parse_args()

    r = redis.Redis.from_url(args.redis)
    print(f"central-scheduler: redis={args.redis} pending={args.pending} slots={args.slots}")

    while True:
        try:
            # Get one concurrency slot token
            tok = r.brpop(args.slots, timeout=5)
            if tok is None:
                continue
            _, raw_node = tok
            node_id = raw_node.decode("utf-8")

            # Peek the next pending task (do not remove yet)
            task_raw = r.lindex(args.pending, 0)
            if task_raw is None:
                # No task; return the slot token
                r.rpush(args.slots, node_id)
                continue

            # Inspect cpu_units requirement
            try:
                tpeek = json.loads(task_raw)
                need = int(tpeek.get("cpu_units", 1))
            except Exception:
                need = 1

            # Check CPU capacity for this node
            cap_key = f"cap:{node_id}"
            try:
                cap_free = int(r.get(cap_key) or 0)
            except Exception:
                cap_free = 0

            if cap_free >= need:
                # Reserve capacity and dispatch
                new_free = cap_free - need
                r.set(cap_key, new_free)
                r.lpop(args.pending)
                qnode = f"q:{node_id}"
                r.rpush(qnode, task_raw)
                try:
                    print(f"dispatch -> node={node_id} input={tpeek.get('input')} output={tpeek.get('output')} cpu_units={need} cap_left={new_free}")
                except Exception:
                    print(f"dispatch -> node={node_id} raw_task={task_raw[:80]!r}")
                continue

            # Not enough CPU capacity; return the slot and retry later
            r.rpush(args.slots, node_id)
            continue
        except KeyboardInterrupt:
            print("stopping central scheduler...")
            break
        except Exception as e:
            print("scheduler error:", e, file=sys.stderr)


if __name__ == "__main__":
    main()

