#!/usr/bin/env python3
"""
Nginx access_log -> invocations.jsonl (adapter sample)

This is a non-invasive example parser. It is NOT wired into any pipeline by default.
Use it when your app is NOT our FastAPI service and you want app-level per-request
records (invocations.jsonl) from an HTTP access log.

Recommended Nginx log_format (includes request_time and sizes):

  log_format tracekit '$remote_addr - $remote_user [$time_local] '
                     '"$request" $status $body_bytes_sent '
                     '"$http_referer" "$http_user_agent" '
                     '$request_length $request_time $http_x_trace_id';
  access_log  /var/log/nginx/access.log  tracekit;

This parser expects lines with the following fields in order:
  time_local, request, status, body_bytes_sent, request_length, request_time, x_trace_id

It outputs records with keys:
  trace_id, span_id, parent_id (None), ts_enqueue(ms), ts_start(ms), ts_end(ms),
  queue_time_ms(0), service_time_ms(ms), rt_ms(ms), method, path, bytes_in, bytes_out, status

Usage:
  python3 tools/adapters/nginx_access_to_invocations.py \
    --input /var/log/nginx/access.log \
    --output logs/20250101T000000Z/invocations.jsonl \
    --node cloud0 --stage cloud

Notes:
- time_local is parsed as UTC if you pass --tz UTC (default). Adjust as needed.
- request_time is seconds with fractions; we convert to ms (int).
- request_length is bytes_in; body_bytes_sent is bytes_out.
- If trace id header is different, pass --trace-header-name to map from another column.
"""
import argparse, json, re, sys
from datetime import datetime, timezone

LINE_RE = re.compile(r"""
    ^(?P<remote>[^ ]+)\s+-\s+(?P<user>[^ ]+)\s+\[(?P<time_local>[^\]]+)\]\s+
    "(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<body_bytes_sent>\d+)\s+
    "(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)"\s+(?P<request_length>\d+)\s+(?P<request_time>[0-9.]+)\s*(?P<trace_id>[^\s"]+)?
""", re.X)

def parse_time_local(s: str, tz: str) -> int:
    # Example: 10/Feb/2025:12:34:56 +0000
    dt = datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z")
    if tz.upper() == "UTC":
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--node", default="vm0")
    ap.add_argument("--stage", default="cloud")
    ap.add_argument("--tz", default="UTC", help="Assume output timezone")
    ap.add_argument("--trace-header-name", default="X-Trace-Id")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8", errors="ignore") as fh, \
         open(args.output, "w", encoding="utf-8") as out:
        for line in fh:
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            g = m.groupdict()
            ts_start = parse_time_local(g["time_local"], args.tz)
            # request_time (s) -> ms
            svc_ms = int(float(g["request_time"]) * 1000)
            ts_end = ts_start + max(0, svc_ms)
            rec = {
                "trace_id": g.get("trace_id") or None,
                "span_id": None,
                "parent_id": None,
                "module_id": None,
                "instance_id": None,
                "ts_enqueue": ts_start,  # no separate queue in access log
                "ts_start": ts_start,
                "ts_end": ts_end,
                "node": args.node,
                "stage": args.stage,
                "method": (g["request"].split(" ")[0] if g.get("request") else None),
                "path": (g["request"].split(" ")[1] if g.get("request") and " " in g["request"] else None),
                "bytes_in": int(g["request_length"]),
                "bytes_out": int(g["body_bytes_sent"]),
                "cpu_time_ms": None,
                "queue_time_ms": 0,
                "service_time_ms": svc_ms,
                "rt_ms": svc_ms,
                "status": int(g["status"]),
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    sys.exit(main())

