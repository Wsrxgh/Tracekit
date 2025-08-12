import os, json, time, uuid, pathlib
from threading import Lock, Event, Thread
from typing import Optional
from contextvars import ContextVar
from fastapi import Request, FastAPI
from fastapi.responses import Response

# --- Environment & paths ---
NODE_ID = os.getenv("NODE_ID", "vm0")
STAGE = os.getenv("STAGE", "cloud")
APP_ID = os.getenv("APP_ID", "app")
MODULE_ID = os.getenv("MODULE_ID", "svc")
PID = os.getpid()

LOG_DIR = pathlib.Path(os.getenv("LOG_PATH", "/logs")).resolve()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Files
EVENTS_FH = open(LOG_DIR / f"events.{PID}.jsonl", "a", buffering=1, encoding="utf-8")
PLACEMENTS_FH = open(LOG_DIR / "placement_events.jsonl", "a", buffering=1, encoding="utf-8")
SYSSTATS_FH = open(LOG_DIR / "system_stats.jsonl", "a", buffering=1, encoding="utf-8")

# --- Utilities ---
def now_ms() -> int:
    return int(time.time() * 1000)

def _write_jsonl(fh, obj: dict):
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

# --- Instance identity helpers ---
def _container_id_suffix() -> Optional[str]:
    try:
        for line in open("/proc/self/cgroup", "r"):
            parts = line.strip().split(":")
            if len(parts) == 3 and parts[2]:
                suf = parts[2].split("/")[-1]
                if len(suf) >= 12:
                    return suf[-12:]
    except Exception:
        pass
    return None

INSTANCE_ID = _container_id_suffix() or f"pid-{PID}"

# --- Concurrency tracking ---
IN_FLIGHT = 0
IN_FLIGHT_LOCK = Lock()
_stop_stats = Event()

# span context for current request
_span_ctx: ContextVar[Optional[dict]] = ContextVar("span_ctx", default=None)

def get_current_span() -> Optional[dict]:
    return _span_ctx.get()

def child_headers(trace_id_override: Optional[str] = None) -> dict:
    ctx = _span_ctx.get() or {}
    trace_id = trace_id_override or ctx.get("trace_id") or str(uuid.uuid4())
    span_id = ctx.get("span_id") or str(uuid.uuid4())[:16]
    return {
        "X-Trace-Id": trace_id,
        "X-Parent-Span-Id": span_id,
        "X-Span-Id": span_id,
    }

# --- Background system stats ---
def _stats_loop():
    while not _stop_stats.is_set():
        try:
            ts = now_ms()
            with IN_FLIGHT_LOCK:
                inflight = IN_FLIGHT
            _write_jsonl(SYSSTATS_FH, {
                "ts_ms": ts,
                "node": NODE_ID,
                "stage": STAGE,
                "metric": "in_flight",
                "value": inflight,
                "instance_id": INSTANCE_ID,
            })
        except Exception:
            pass
        _stop_stats.wait(1.0)

# --- Middleware / lifecycle ---
def setup_tracing(app: FastAPI):
    @app.middleware("http")
    async def _trace_mw(req: Request, call_next):
        global IN_FLIGHT
        # Enqueue time & ids
        ts_enqueue = now_ms()
        # read body once to get bytes_in; Starlette caches it
        try:
            body = await req.body()
        except Exception:
            body = b""
        bytes_in = len(body)
        trace_id = req.headers.get("X-Trace-Id") or str(uuid.uuid4())
        span_id = str(uuid.uuid4())[:16]
        parent_id = req.headers.get("X-Parent-Span-Id") or req.headers.get("X-Span-Id")

        # increase in-flight
        with IN_FLIGHT_LOCK:
            IN_FLIGHT += 1

        # set context
        _span_ctx.set({
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_id": parent_id,
        })

        # timings
        ts_start = now_ms()
        cpu0 = time.process_time()
        status = 500
        bytes_out = 0
        try:
            resp: Response = await call_next(req)
            status = resp.status_code
            # prefer header content-length, else len of body (if available)
            cl = None
            try:
                cl = int(resp.headers.get("content-length")) if resp.headers.get("content-length") else None
            except Exception:
                cl = None
            if cl is not None:
                bytes_out = cl
            else:
                try:
                    # Response may have body attribute
                    if hasattr(resp, "body") and resp.body is not None:
                        bytes_out = len(resp.body)
                except Exception:
                    bytes_out = 0
            return resp
        finally:
            ts_end = now_ms()
            cpu_ms = int((time.process_time() - cpu0) * 1000)
            with IN_FLIGHT_LOCK:
                IN_FLIGHT -= 1
            rec = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_id": parent_id,
                "module_id": MODULE_ID,
                "instance_id": INSTANCE_ID,
                "ts_enqueue": ts_enqueue,
                "ts_start": ts_start,
                "ts_end": ts_end,
                "node": NODE_ID,
                "stage": STAGE,
                "method": req.method,
                "path": req.url.path,
                "bytes_in": bytes_in,
                "bytes_out": bytes_out,
                "cpu_time_ms": cpu_ms,
                "queue_time_ms": max(0, ts_start - ts_enqueue),
                "service_time_ms": max(0, ts_end - ts_start),
                "rt_ms": max(0, ts_end - ts_enqueue),
                "status": status,
                "pid": PID,
            }
            _write_jsonl(EVENTS_FH, rec)
            # clear context
            _span_ctx.set(None)

    @app.on_event("startup")
    async def _on_startup():
        _write_jsonl(PLACEMENTS_FH, {
            "ts": now_ms(), "app_id": APP_ID, "module_id": MODULE_ID,
            "instance_id": INSTANCE_ID, "node_id": NODE_ID, "event": "start", "stage": STAGE,
        })
        Thread(target=_stats_loop, daemon=True).start()

    @app.on_event("shutdown")
    async def _on_shutdown():
        _stop_stats.set()
        _write_jsonl(PLACEMENTS_FH, {
            "ts": now_ms(), "app_id": APP_ID, "module_id": MODULE_ID,
            "instance_id": INSTANCE_ID, "node_id": NODE_ID, "event": "stop", "stage": STAGE,
        })

__all__ = [
    "setup_tracing", "child_headers", "get_current_span", "LOG_DIR",
    "NODE_ID", "STAGE", "APP_ID", "MODULE_ID"
]

