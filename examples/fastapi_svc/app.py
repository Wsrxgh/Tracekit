from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
import os, json, time, uuid, requests, pathlib, gzip, hashlib, sqlite3
from tracekit import setup_tracing, child_headers, LOG_DIR, MODULE_ID, NODE_ID, STAGE
# Optional downstream forwarding for real endpoints
DEFAULT_NEXT_URL = os.getenv("DEFAULT_NEXT_URL", "").strip() or None

def _next_url(req: Request):
    # Priority: per-request header overrides env default
    return req.headers.get("X-Next-Url") or DEFAULT_NEXT_URL

def _forward_bytes(next_url: str, trace_id: str, payload: bytes, content_type: str | None = None) -> None:
    if not next_url:
        return
    headers = child_headers(trace_id)
    if content_type:
        headers["Content-Type"] = content_type
    try:
        requests.post(next_url, headers=headers, data=payload, timeout=5)
    except Exception:
        # best-effort forwarding; do not fail the original request
        pass


app = FastAPI()
# attach tracing (middleware + lifecycle) early
setup_tracing(app)

# Use LOG_DIR from tracekit; initialize SQLite for KV endpoints
DB_PATH = LOG_DIR / "app.db"
SQL_CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False)
SQL_CONN.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v BLOB)")
SQL_CONN.commit()
# tracing, placement, system stats are handled by tracekit



def busy_cpu_ms(target_ms: int):
    """用 CPU 进程时间自旋，更贴近真实 CPU 占用，而非墙钟。"""
    if target_ms <= 0: return
    start = time.process_time()
    target = start + target_ms / 1000.0
    x = 0.0
    while time.process_time() < target:
        x = x * 1.0000001 + 1.0


@app.api_route("/work", methods=["GET", "POST"])
async def work(request: Request):
    qp = request.query_params
    cpu_ms = int(qp.get("cpu_ms", "0"))
    resp_kb = int(qp.get("resp_kb", "0"))
    call_url = qp.get("call_url", "")
    base_trace = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    trace_id = qp.get("trace_id") or base_trace

    body_bytes = await request.body()

    # 可选下游调用：把参数和 trace_id 透传
    if call_url:
        sep = "&" if "?" in call_url else "?"
        cascaded = f"{call_url}{sep}trace_id={trace_id}"
        try:
            requests.post(
                cascaded,
                headers=child_headers(trace_id),
                data=body_bytes,
                timeout=5,
            )
        except Exception:
            pass

    # 本地 CPU 开销（使用进程时间）
    busy_cpu_ms(cpu_ms)

    # 构造响应体（下行字节）
    bytes_out = max(0, resp_kb * 1024)
    body = b"x" * bytes_out
    return PlainTextResponse(body, headers={"X-Trace-Id": trace_id})


# ===== 真实工作负载端点 =====
@app.post("/json/validate")
async def json_validate(request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    try:
        data = await request.json()
        assert isinstance(data, dict)
        _ = json.dumps(data)
        status = 200
        ok = True
        # optional forward original JSON if next hop configured
        nx = _next_url(request)
        if nx:
            _forward_bytes(nx, trace_id, json.dumps(data).encode("utf-8"), content_type="application/json")
    except Exception:
        status = 400
        ok = False
    return PlainTextResponse("ok" if ok else "bad json", status_code=status, headers={"X-Trace-Id": trace_id})


@app.post("/blob/gzip")
async def blob_gzip(request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    raw = await request.body()
    gz = gzip.compress(raw, compresslevel=6)
    # optional forward compressed bytes
    nx = _next_url(request)
    if nx:
        _forward_bytes(nx, trace_id, gz, content_type="application/gzip")
    return Response(content=gz, media_type="application/gzip", headers={"X-Trace-Id": trace_id})


@app.post("/blob/gunzip")
async def blob_gunzip(request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    gz = await request.body()
    try:
        raw = gzip.decompress(gz)
        status = 200
        nx = _next_url(request)
        if nx:
            _forward_bytes(nx, trace_id, raw, content_type="application/octet-stream")
    except Exception:
        raw = b""
        status = 400
    return Response(content=raw, media_type="application/octet-stream", status_code=status, headers={"X-Trace-Id": trace_id})


@app.post("/hash/sha256")
async def hash_sha256(request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    data = await request.body()
    h = hashlib.sha256(data).hexdigest().encode()
    # typically terminal; but allow optional forward of the hash string
    nx = _next_url(request)
    if nx:
        _forward_bytes(nx, trace_id, h, content_type="text/plain")
    return Response(content=h, media_type="text/plain", headers={"X-Trace-Id": trace_id})


@app.post("/kv/set/{key}")
async def kv_set(key: str, request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    val = await request.body()
    SQL_CONN.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (key, val))
    SQL_CONN.commit()
    # usually sink; allow optional forward (e.g., to another store or audit)
    nx = _next_url(request)
    if nx:
        _forward_bytes(nx, trace_id, val)
    return PlainTextResponse("OK", headers={"X-Trace-Id": trace_id})


@app.get("/kv/get/{key}")
async def kv_get(key: str, request: Request):
    trace_id = getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    cur = SQL_CONN.execute("SELECT v FROM kv WHERE k=?", (key,))
    row = cur.fetchone()
    val = row[0] if row else b""
    # optionally forward the fetched value
    nx = _next_url(request)
    if nx and row:
        _forward_bytes(nx, trace_id, val, content_type="application/octet-stream")
    return Response(content=val if row else b"", media_type="application/octet-stream", status_code=200 if row else 404, headers={"X-Trace-Id": trace_id})

