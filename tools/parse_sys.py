import os, json, re, glob
import time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = os.environ.get("RUN_ID")
logs_root = ROOT / "logs"

if not RUN_ID:
    # 取最新的 run 目录
    run_dirs = sorted([p.name for p in logs_root.iterdir() if p.is_dir()])
    RUN_ID = run_dirs[-1] if run_dirs else None

assert RUN_ID, "No RUN_ID and no logs/* found"
LOGS = logs_root / RUN_ID
print(f"[parse] RUN_ID={RUN_ID}")
# Ensure CTS output directory is defined early (used by multiple sections)
cts_dir = LOGS / "CTS"
cts_dir.mkdir(exist_ok=True)


# Resolve identity for this parse run
NODE_ID = os.environ.get("NODE_ID", "vm0")
STAGE   = os.environ.get("STAGE", "edge")
# Hard override from node_meta.json if available (authoritative)
try:
    _m = json.load(open(LOGS/"node_meta.json","r"))
    NODE_ID = _m.get("node", NODE_ID)
    STAGE = _m.get("stage", STAGE)
except Exception:
    pass

# ---------- 1) 合并服务端事件 ----------
event_files = sorted(glob.glob(str(LOGS / "events.*.jsonl")))
merged_events = LOGS / "events.jsonl"
with open(merged_events, "w", encoding="utf-8") as out:
    rows = []
    for f in event_files:
        with open(f, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    rows.append(obj)
                except: pass
    # Sort by timestamp - try different timestamp fields
    def get_timestamp(x):
        return x.get("ts_ms") or x.get("ts_enqueue") or x.get("ts_start") or 0
    rows.sort(key=lambda x: (get_timestamp(x), x.get("pid", 0)))
    for r in rows:
        # 补默认字段
        r.setdefault("node", NODE_ID)
        r.setdefault("stage", STAGE)
        out.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"[parse] merged events → {merged_events}")

# ---------- 2) Host-level CPU/MEM sampling (deprecated) ----------
# resources.jsonl is no longer produced; host samplers (mpstat/vmstat) are disabled.


# ---------- 3) NET (disabled) ----------
# Network link collection/parsing is disabled; links.jsonl is no longer produced.


# ---------- 6) 标准化 per-PID 采样（合并为 CTS proc_metrics） ----------
proc_metrics = LOGS / "proc_metrics.jsonl"
if proc_metrics.exists():
    cts_dir = LOGS / "CTS"; cts_dir.mkdir(exist_ok=True)
    # 生成合并后的 CTS proc_metrics：每行包含 {ts_ms, pid, dt_ms, cpu_ms, rss_kb}
    try:
        CLK_TCK = int(os.popen("getconf CLK_TCK").read().strip() or "100")
    except Exception:
        CLK_TCK = 100
    merged_out = cts_dir / "proc_metrics.jsonl"
    last = {}  # pid -> (utime, stime, ts_ms)
    with open(proc_metrics, "r", encoding="utf-8", errors="ignore") as fin, \
         open(merged_out, "w", encoding="utf-8") as mout:
        for line in fin:
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = o.get("ts_ms"); pid = o.get("pid")
            rss_kb = o.get("rss_kb")
            ut, st = o.get("utime"), o.get("stime")
            if not isinstance(ts, int) or not isinstance(pid, int):
                continue
            # 计算差分 CPU
            dt_ms = 0; cpu_ms = 0
            prev = last.get(pid)
            if prev and isinstance(ut, int) and isinstance(st, int):
                prev_ut, prev_st, prev_ts = prev
                if ts != prev_ts:
                    dt_ticks = max(0, (ut+st) - (prev_ut+prev_st))
                    dt_ms = max(0, ts - prev_ts)
                    cpu_ms = int(dt_ticks * 1000 / max(1, CLK_TCK))
                    last[pid] = (ut, st, ts)
                elif ut + st > prev_ut + prev_st:
                    last[pid] = (ut, st, ts)
            elif isinstance(ut, int) and isinstance(st, int):
                last[pid] = (ut, st, ts)
            # 合并后的 CTS 记录（首样本 dt/cpu 为 0 以占位）
            rec = {"ts_ms": ts, "pid": pid, "dt_ms": int(dt_ms), "cpu_ms": int(cpu_ms)}
            if isinstance(rss_kb, int):
                rec["rss_kb"] = rss_kb
            mout.write(json.dumps(rec) + "\n")
    print(f"[parse] derived merged proc_metrics → {cts_dir}")

# ---------- 7) （精简）不再复制 placement/system_stats 到 CTS ----------


# ---------- 4) 复制客户端事件到 run 目录（若存在） ----------
ec_src = LOGS / "events_client.jsonl"
if not ec_src.exists():
    # 兼容旧位置
    if (ROOT / "events_client.jsonl").exists():
        (ROOT / "events_client.jsonl").replace(ec_src)
print(f"[parse] client events at → {ec_src if ec_src.exists() else 'N/A'}")

# ---------- 5) 生成 CTS（精简产物 + 审计） ----------
cts_dir = LOGS / "CTS"; cts_dir.mkdir(exist_ok=True)
# 仅输出 nodes.json
meta = json.load(open(LOGS / "node_meta.json", "r"))
# Normalize freq and memory to reduce near-duplicates (e.g., 2399→2400 MHz; 15997MB→16GiB)
raw_freq = meta.get("cpu_freq_mhz") or 0
norm_freq = int(round(float(raw_freq) / 100.0) * 100) if raw_freq and raw_freq > 0 else raw_freq
raw_mem_mb = meta.get("mem_mb") or 0
norm_mem_mb = int(round(float(raw_mem_mb) / 1024.0) * 1024) if raw_mem_mb and raw_mem_mb > 0 else raw_mem_mb
with open(cts_dir / "nodes.json", "w") as f:
    json.dump([{
        "node_id": meta["node"],
        "stage": meta["stage"],
        "cpu_cores": meta["cpu_cores"],
        "mem_mb": norm_mem_mb,
        "cpu_model": meta.get("cpu_model"),
        "cpu_freq_mhz": norm_freq
    }], f, indent=2)
# 仅输出精简字段的 invocations.jsonl（proc_metrics 已在步骤 6 生成）
# 保留字段：trace_id、pid、ts_enqueue、ts_start、ts_end
with open(merged_events, "r", encoding="utf-8", errors="ignore") as fin, \
     open(cts_dir / "invocations.jsonl", "w", encoding="utf-8") as fout:
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        rec = {
            "trace_id": o.get("trace_id"),
            "pid": o.get("pid"),
            "ts_enqueue": o.get("ts_enqueue"),
            "ts_start": o.get("ts_start"),
            "ts_end": o.get("ts_end"),
        }
        fout.write(json.dumps(rec) + "\n")

# 清理 CTS 目录中非 {invocations.jsonl, proc_metrics.jsonl, nodes.json, audit_report.md} 的文件
allowed = {"invocations.jsonl", "proc_metrics.jsonl", "nodes.json", "audit_report.md"}
for p in cts_dir.iterdir():
    if p.is_file() and p.name not in allowed:
        try:
            p.unlink()
        except Exception:
            pass

# 生成审计报告（英文）
inv_path = cts_dir / "invocations.jsonl"
pm_path = cts_dir / "proc_metrics.jsonl"
audit_lines = []
# 读取 invocations
inv_rows = []
if inv_path.exists():
    with open(inv_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                inv_rows.append(json.loads(line))
            except Exception:
                pass
# 读取 proc_metrics
pm_rows = []
if pm_path.exists():
    with open(pm_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                pm_rows.append(json.loads(line))
            except Exception:
                pass

# 字段完整性与缺失率
from collections import Counter, defaultdict
inv_fields = ["trace_id","ts_enqueue","ts_start","ts_end","pid"]
pm_fields = ["ts_ms","pid","dt_ms","cpu_ms","rss_kb"]  # rss_kb 可选
inv_missing = Counter()
pm_missing = Counter()
for o in inv_rows:
    for k in inv_fields:
        if o.get(k) is None:
            inv_missing[k] += 1
for o in pm_rows:
    for k in pm_fields:
        if o.get(k) is None:
            pm_missing[k] += 1

# 时间单调性
inv_violations = 0
for o in inv_rows:
    te, ts, td = o.get("ts_enqueue"), o.get("ts_start"), o.get("ts_end")
    try:
        if not (int(te) <= int(ts) <= int(td)):
            inv_violations += 1
    except Exception:
        inv_violations += 1

pm_monotonic_viol = 0
pm_negative_dt = 0
last_ts_by_pid = {}
for o in pm_rows:
    pid = o.get("pid")
    ts = o.get("ts_ms")
    dt = o.get("dt_ms")
    if isinstance(dt, int) and dt < 0:
        pm_negative_dt += 1
    prev_ts = last_ts_by_pid.get(pid)
    if prev_ts is not None and isinstance(ts, int):
        if ts <= prev_ts:
            pm_monotonic_viol += 1
    if isinstance(ts, int):
        last_ts_by_pid[pid] = ts

# 交叉引用
inv_pids = {int(o.get("pid")) for o in inv_rows if isinstance(o.get("pid"), int)}
pm_pids = {int(o.get("pid")) for o in pm_rows if isinstance(o.get("pid"), int)}
matched = inv_pids & pm_pids
unmatched = inv_pids - pm_pids
match_rate = (len(matched) / len(inv_pids)) if inv_pids else 0.0

# 生成 Markdown 审计报告
md = []
md.append("# CTS Audit Report\n")
md.append(f"Node: {meta.get('node')}  |  Stage: {meta.get('stage')}\n")
md.append("\n## Summary\n")
md.append(f"Invocations: {len(inv_rows)}\n")
md.append(f"Proc metrics samples: {len(pm_rows)}\n")
md.append(f"Distinct PIDs (invocations): {len(inv_pids)}\n")
md.append(f"Distinct PIDs (proc_metrics): {len(pm_pids)}\n")
md.append(f"PID match rate: {match_rate:.2%}\n")
md.append("\n## Field completeness (missing counts / rate)\n")
for k in inv_fields:
    miss = inv_missing.get(k, 0)
    rate = (miss / len(inv_rows)) if inv_rows else 0
    md.append(f"- invocations.{k}: {miss} ({rate:.2%})\n")
for k in pm_fields:
    miss = pm_missing.get(k, 0)
    rate = (miss / len(pm_rows)) if pm_rows else 0
    md.append(f"- proc_metrics.{k}: {miss} ({rate:.2%})\n")
md.append("\n## Temporal consistency\n")
md.append(f"- invocations ts_enqueue ≤ ts_start ≤ ts_end violations: {inv_violations}\n")
md.append(f"- proc_metrics per-pid strictly increasing ts_ms violations: {pm_monotonic_viol}\n")
md.append(f"- proc_metrics records with dt_ms < 0: {pm_negative_dt}\n")
md.append("\n## Cross-reference\n")
md.append(f"- invocations without matching proc_metrics PID: {len(unmatched)}\n")
if unmatched:
    sample = list(sorted(unmatched))[:10]
    md.append(f"  sample unmatched PIDs: {sample}\n")

(cts_dir / "audit_report.md").write_text("".join(md), encoding="utf-8")
print(f"[parse] CTS (slim) → {cts_dir}; audit_report.md generated")
