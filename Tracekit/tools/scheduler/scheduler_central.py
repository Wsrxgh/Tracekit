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
import argparse, json, sys, time
import redis


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--pending", default="q:pending")
    ap.add_argument("--slots", default="slots:available")
    ap.add_argument("--scan-slots", type=int, default=0, help="Max distinct hosts to scan per cycle; 0=all available tokens")
    ap.add_argument("--scan-pending", type=int, default=1, help="How many pending tasks to consider from head (FIFO if 1)")
    # Weigher configuration: "" (default first-fit), "instances", or "vcpu"; order: "min" or "max"
    ap.add_argument("--weigher", default="", choices=["", "instances", "vcpu"], help="Host selection weigher when multiple nodes are feasible")
    ap.add_argument("--weigher-order", default="min", choices=["min", "max"], help="Prefer smaller (min) or larger (max) metric values")

    args = ap.parse_args()

    def _cap_free(nid: str) -> int:
        try:
            return int(r.get(f"cap:{nid}") or 0)
        except Exception:
            return 0

    def _cap_total(nid: str) -> int:
        try:
            v = r.get(f"cap_total:{nid}")
            return int(v) if v is not None else 0
        except Exception:
            return 0

    def _metric_instances(nid: str) -> int:
        try:
            return int(r.get(f"run_count:{nid}") or 0)
        except Exception:
            return 0

    def _metric_vcpu(nid: str) -> int:
        cf = _cap_free(nid)
        ct = _cap_total(nid)
        if ct <= 0:
            return 0
        used = ct - cf
        return max(0, used)

    def _choose_host(feasible_hosts: list[str]) -> str | None:
        if not feasible_hosts:
            return None
        if not args.weigher:
            return sorted(feasible_hosts)[0]
        # Build (metric, nid)
        items = []
        for nid in feasible_hosts:
            if args.weigher == "instances":
                m = _metric_instances(nid)
            else:  # vcpu
                m = _metric_vcpu(nid)
            items.append((m, nid))
        reverse = True if args.weigher_order == "max" else False
        items.sort(key=lambda x: (x[0], x[1]), reverse=reverse)
        return items[0][1] if items else None


    r = redis.Redis.from_url(args.redis)
    print(f"central-scheduler: redis={args.redis} pending={args.pending} slots={args.slots}")

    while True:
        try:
            # Strict FIFO: only consider head of pending
            task_raw = r.lindex(args.pending, 0)
            if task_raw is None:
                time.sleep(0.05)
                continue
            try:
                tpeek = json.loads(task_raw)
                need = int(tpeek.get("cpu_units", 1))
            except Exception:
                need = 1

            # Snapshot available slots non-blocking and build token counts per node
            n = r.llen(args.slots) or 0
            if n <= 0:
                # No slots gating: dispatch purely by remaining CPU capacity (cap:<node>)
                try:
                    cap_keys = r.keys("cap:*") or []
                except Exception:
                    cap_keys = []
                hosts = []
                for k in cap_keys:
                    try:
                        s = k.decode("utf-8")
                        if s.startswith("cap:"):
                            hosts.append(s[4:])
                    except Exception:
                        continue
                hosts = sorted(set(hosts))
                feasible = []
                for nid in hosts:
                    if _cap_free(nid) >= need:
                        feasible.append(nid)
                chosen = _choose_host(feasible)
                if not chosen:
                    # Head-of-line blocking by capacity
                    time.sleep(0.05)
                    continue
                # Dispatch without consuming a slot token
                cap_key = f"cap:{chosen}"
                cap_free = _cap_free(chosen)
                if cap_free < need:
                    time.sleep(0.05)
                    continue
                new_free = cap_free - need
                r.set(cap_key, new_free)
                r.lpop(args.pending)
                qnode = f"q:{chosen}"
                r.incr(f"run_count:{chosen}")
                r.rpush(qnode, task_raw)
                try:
                    print(f"dispatch(no-slots) -> node={chosen} input={tpeek.get('input')} output={tpeek.get('output')} cpu_units={need} cap_left={new_free}")
                except Exception:
                    print(f"dispatch(no-slots) -> node={chosen} raw_task={task_raw[:80]!r}")
                continue
            # Limit scan by --scan-slots if set (>0)
            max_scan = n if int(args.scan_slots) <= 0 else min(n, int(args.scan_slots))
            # Get rightmost max_scan tokens snapshot (BRPOP takes from right). LRANGE uses [start,end]
            # Rightmost k -> indices [n-k, n-1]
            start = max(0, n - max_scan)
            tokens = r.lrange(args.slots, start, n - 1)
            counts = {}
            order = []
            for raw in tokens:
                nid = raw.decode("utf-8")
                counts[nid] = counts.get(nid, 0) + 1
                if nid not in order:
                    order.append(nid)

            # Stable host order with optional weigher
            hosts = sorted(order)
            feasible = []
            for nid in hosts:
                if counts.get(nid, 0) > 0 and _cap_free(nid) >= need:
                    feasible.append(nid)
            chosen = _choose_host(feasible)

            if not chosen:
                # Attempt robust fallback: ignore slots and dispatch by capacity-only to avoid stale-token deadlocks
                try:
                    cap_keys = r.keys("cap:*") or []
                except Exception:
                    cap_keys = []
                hosts = []
                for k in cap_keys:
                    try:
                        s = k.decode("utf-8")
                        if s.startswith("cap:"):
                            hosts.append(s[4:])
                    except Exception:
                        continue
                hosts = sorted(set(hosts))
                feasible = [nid for nid in hosts if _cap_free(nid) >= need]
                nid = _choose_host(feasible)
                if nid:
                    # Dispatch without consuming a slot token (cap-only path)
                    cap_free = _cap_free(nid)
                    new_free = cap_free - need
                    r.set(f"cap:{nid}", new_free)
                    r.lpop(args.pending)
                    qnode = f"q:{nid}"
                    r.incr(f"run_count:{nid}")
                    r.rpush(qnode, task_raw)
                    try:
                        print(f"dispatch(fallback-no-slots) -> node={nid} input={tpeek.get('input')} output={tpeek.get('output')} cpu_units={need} cap_left={new_free}")
                    except Exception:
                        print(f"dispatch(fallback-no-slots) -> node={nid} raw_task={task_raw[:80]!r}")
                else:
                    # Still nothing feasible
                    time.sleep(0.05)
                    continue

            # Consume one token from chosen node (remove from rightmost segment if present; fallback full list remove)
            removed = False
            # Try RPOPLPUSH loop up to max_scan to move non-chosen tokens to front and expose chosen at tail
            # This is O(k) where k<=max_scan, keeps list mostly intact and avoids long scans.
            for _ in range(max_scan):
                tail = r.rpoplpush(args.slots, args.slots)
                if tail is None:
                    break
                if tail.decode("utf-8") == chosen:
                    # We moved chosen from tail to head; now pop head to consume it
                    r.lpop(args.slots)
                    removed = True
                    break
            if not removed:
                # Fallback: remove one occurrence anywhere
                r.lrem(args.slots, 1, chosen)

            # Re-check cap and dispatch
            cap_key = f"cap:{chosen}"
            try:
                cap_free = int(r.get(cap_key) or 0)
            except Exception:
                cap_free = 0
            if cap_free < need:
                # Capacity changed; abort (token remains consumed, but worker will return it on next completion)
                # To be safe, give the slot back immediately
                r.rpush(args.slots, chosen)
                time.sleep(0.05)
                continue

            new_free = cap_free - need
            r.set(cap_key, new_free)
            r.lpop(args.pending)
            qnode = f"q:{chosen}"
            r.incr(f"run_count:{chosen}")
            r.rpush(qnode, task_raw)
            try:
                print(f"dispatch -> node={chosen} input={tpeek.get('input')} output={tpeek.get('output')} cpu_units={need} cap_left={new_free}")
            except Exception:
                print(f"dispatch -> node={chosen} raw_task={task_raw[:80]!r}")
            continue
        except KeyboardInterrupt:
            print("stopping central scheduler...")
            break
        except Exception as e:
            print("scheduler error:", e, file=sys.stderr)


if __name__ == "__main__":
    main()

