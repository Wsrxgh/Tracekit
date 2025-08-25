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
# Ensure CCTF output directory is defined early (used by multiple sections)
cctf_dir = LOGS / "cctf"
cctf_dir.mkdir(exist_ok=True)


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

# ---------- 2) CPU（mpstat 文本） + MEM（vmstat） ----------
resources_out = LOGS / "resources.jsonl"
with open(resources_out, "w", encoding="utf-8") as rout:
    # CPU：仅解析 mpstat 文本输出 cpu.log，取 all 汇总行，cpu_util = 100 - idle
    cpu_log = LOGS / "cpu.log"
    if cpu_log.exists():
        day = datetime.fromtimestamp(cpu_log.stat().st_mtime).strftime("%Y-%m-%d")
        for line in open(cpu_log, "r", errors="ignore"):
            if " all " in line and "%" in line:
                m = re.search(r'(\d{1,2}:\d{2}:\d{2})(?:\s*[AP]M)?', line)
                if not m:
                    continue
                tstr = m.group(1)
                ts = int(datetime.strptime(f"{day} {tstr}", "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                nums = [float(x) for x in re.findall(r'(\d+\.\d+)', line)]
                if nums:
                    idle = nums[-1]
                    util = max(0.0, min(100.0, 100.0 - idle))
                    rout.write(json.dumps({
                        "ts_ms": ts, "node": NODE_ID, "stage": STAGE, "cpu_util": round(util, 2)
                    }) + "\n")

    # MEM：vmstat -Sm 1（保持你原来的解析逻辑）
    mem_log = LOGS / "mem.log"
    if mem_log.exists():
        for line in open(mem_log, "r", errors="ignore"):
            if line.strip().startswith("r"):
                continue
            cols = line.split()
            if len(cols) >= 7 and cols[0].isdigit():
                ts = int(time.time() * 1000)  # vmstat 无时间戳，取当前时间近似
                free_mb = int(cols[3])
                rout.write(json.dumps({
                    "ts_ms": ts, "node": NODE_ID, "stage": STAGE, "mem_free_mb": free_mb
                }) + "\n")


# ---------- 3) NET（ifstat -t） ----------
# ---------- 3) NET（ifstat） ----------
links_out = LOGS / "links.jsonl"
with open(links_out, "w", encoding="utf-8") as lout:
    net_log = LOGS / "net.log"
    if net_log.exists():
        # 取文件修改日期作为“当天”
        day = datetime.fromtimestamp(net_log.stat().st_mtime).strftime("%Y-%m-%d")
        for line in open(net_log, "r", errors="ignore"):
            line = line.strip()
            # 跳过表头
            if not line or line.startswith("Time") or line.startswith("HH:MM:SS"):
                continue
            # 1) 只有时刻的情况: HH:MM:SS  rx  tx
            m = re.match(r'^(\d{2}:\d{2}:\d{2})\s+([\d.]+)\s+([\d.]+)$', line)
            if m:
                t = m.group(1)
                dt = datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp()*1000)
                rx_kbs = float(m.group(2)); tx_kbs = float(m.group(3))
                lout.write(json.dumps({
                    "ts_ms": ts,
                    "node": NODE_ID, "stage": STAGE,
                    "link": f"{NODE_ID}.nic",
                    "rx_Bps": int(rx_kbs*1024),
                    "tx_Bps": int(tx_kbs*1024)
                })+"\n")
                continue
            # 2) 带日期的一行式: YYYY-MM-DD HH:MM:SS  rx  tx（保留兼容）
            m2 = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([\d.]+)\s+([\d.]+)$', line)
            if m2:
                dt = datetime.strptime(m2.group(1)+" "+m2.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp()*1000)
                rx_kbs = float(m2.group(3)); tx_kbs = float(m2.group(4))
                lout.write(json.dumps({
                    "ts_ms": ts,
                    "node": NODE_ID, "stage": STAGE,
                    "link": f"{NODE_ID}.nic",
                    "rx_Bps": int(rx_kbs*1024),
                    "tx_Bps": int(tx_kbs*1024)
                })+"\n")
print(f"[parse] links → {links_out}")

# ---------- 6) 标准化 per-PID 采样（proc_metrics → cctf） ----------
proc_metrics = LOGS / "proc_metrics.jsonl"
if proc_metrics.exists():
    cctf_dir.mkdir(exist_ok=True)
    # 直通导出（保留原始单位：rss_kb，utime/stime 为 tick）
    import shutil as _sh
    _sh.copy(proc_metrics, cctf_dir / "proc_metrics.jsonl")
    # 可选：导出按采样差分的 CPU ms（方便后续导出 OpenDC Fragments）
    try:
        CLK_TCK = int(os.popen("getconf CLK_TCK").read().strip() or "100")
    except Exception:
        CLK_TCK = 100
    proc_cpu_out = cctf_dir / "proc_cpu.jsonl"
    proc_rss_out = cctf_dir / "proc_rss.jsonl"
    last = {}
    with open(proc_cpu_out, "w", encoding="utf-8") as cpu_out, open(proc_rss_out, "w", encoding="utf-8") as rss_out:
        for line in open(proc_metrics, "r", encoding="utf-8", errors="ignore"):
            try:
                o = json.loads(line)
            except:
                continue
            ts = o.get("ts_ms"); pid = o.get("pid")
            rss_kb = o.get("rss_kb")
            ut, st = o.get("utime"), o.get("stime")
            if isinstance(ts, int) and isinstance(pid, int):
                if isinstance(rss_kb, int):
                    rss_out.write(json.dumps({"ts_ms": ts, "pid": pid, "rss_kb": rss_kb})+"\n")
                key = pid
                prev = last.get(key)
                if prev and isinstance(ut, int) and isinstance(st, int):
                    dt_ticks = max(0, (ut+st) - (prev[0]+prev[1]))
                    cpu_ms = int(dt_ticks * 1000 / max(1, CLK_TCK))
                    cpu_out.write(json.dumps({"ts_ms": ts, "pid": pid, "cpu_ms": cpu_ms})+"\n")
                if isinstance(ut, int) and isinstance(st, int):
                    last[key] = (ut, st)
    print(f"[parse] copied proc_metrics and derived proc_cpu/proc_rss → {cctf_dir}")

# ---------- 7) 复制/汇总新增制品（placement 与 system_stats） ----------
for fname in ["placement_events.jsonl", "system_stats.jsonl"]:
    src = LOGS / fname
    if src.exists():
        # 放到 cctf/ 下，保持与其它制品并列
        (LOGS/"cctf").mkdir(exist_ok=True)
        import shutil
        shutil.copy(src, LOGS/"cctf"/fname)
        print(f"[parse] copied {fname} → {LOGS/'cctf'/fname}")


# ---------- 4) 复制客户端事件到 run 目录（若存在） ----------
ec_src = LOGS / "events_client.jsonl"
if not ec_src.exists():
    # 兼容旧位置
    if (ROOT / "events_client.jsonl").exists():
        (ROOT / "events_client.jsonl").replace(ec_src)
print(f"[parse] client events at → {ec_src if ec_src.exists() else 'N/A'}")

# ---------- 5) 生成 CCTF（标准化） ----------
cctf_dir = LOGS / "cctf"; cctf_dir.mkdir(exist_ok=True)
# 节点
meta = json.load(open(LOGS / "node_meta.json", "r"))
with open(cctf_dir / "nodes.json", "w") as f:
    json.dump([{
        "node_id": meta["node"],
        "stage": meta["stage"],
        "cpu_cores": meta["cpu_cores"],
        "mem_mb": meta["mem_mb"],
        "cpu_model": meta.get("cpu_model"),
        "cpu_freq_mhz": meta.get("cpu_freq_mhz")
    }], f, indent=2)
# 链路（单网卡示例）
# If link_meta.json exists, carry BW/PR into CCTF link
link_meta_path = LOGS / "link_meta.json"
link_obj = {"u": meta["node"], "v": f'{meta["node"]}.net', "BW_bps": None, "PR_s": None}
if link_meta_path.exists():
    lm = json.load(open(link_meta_path, "r"))
    if isinstance(lm, dict):
        link_obj["BW_bps"] = lm.get("BW_bps")
        link_obj["PR_s"] = lm.get("PR_s")
with open(cctf_dir / "links.json", "w") as f:
    json.dump([link_obj], f, indent=2)
# 模块清单（若存在）
mod_inv = LOGS / "module_inventory.json"
if mod_inv.exists():
    import shutil as _sh
    _sh.copy(mod_inv, cctf_dir / "module_inventory.json")
# 调用
import shutil
shutil.copy(merged_events, cctf_dir / "invocations.jsonl")
# 主机指标
shutil.copy(resources_out, cctf_dir / "host_metrics.jsonl")
# 链路指标
shutil.copy(links_out, cctf_dir / "link_metrics.jsonl")
# 运行元数据（若存在）
rm = LOGS/"run_meta.json"
if rm.exists():
    shutil.copy(rm, cctf_dir/"run_meta.json")
print(f"[parse] CCTF → {cctf_dir}")
