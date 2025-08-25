# Tracekit

A comprehensive cloud application tracing and performance monitoring framework designed for collecting detailed execution traces compatible with OpenDC simulator format.

## Overview

Tracekit provides multi-layer observability for cloud applications:
- **Application-level**: Request/response tracing with timing and resource usage
- **Process-level**: Per-PID CPU and memory monitoring  
- **System-level**: Host CPU, memory, and network metrics
- **Infrastructure**: Node topology and link characteristics

## Key Features

- 🔍 **Multi-layer monitoring**: From application requests down to system resources
- 📊 **OpenDC compatibility**: Generates traces suitable for datacenter simulation
- 🔌 **Flexible adapters**: Support for HTTP services, batch jobs, CLI tools
- 📈 **High-precision timing**: Millisecond-level timestamps for accurate analysis
- 🐳 **Container-aware**: Works with Docker containers and bare metal
- 🌐 **Multi-node**: Coordinate tracing across distributed deployments

## Quick Start

### 1. Start monitoring
```bash
make collect RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud
```

### 2. Run your application
```bash
# For HTTP services - use the FastAPI example
cd examples/fastapi_svc && make run

# For batch jobs - use adapters
tools/adapters/ffmpeg_wrapper.sh input.mp4 output.mp4

# For existing services - use log adapters
python3 tools/adapters/nginx_access_to_invocations.py --input access.log --output logs/test001/invocations.jsonl
```

### 3. Generate load (optional)
```bash
make load VM_IP=localhost RATE=50 DURATION=60s
```

### 4. Stop and parse
```bash
make stop-collect RUN_ID=test001
make parse RUN_ID=test001 NODE_ID=cloud1 STAGE=cloud
```

## Output

The framework generates standardized traces in `logs/$RUN_ID/cctf/`:
- `invocations.jsonl`: Application-level task execution records
- `proc_cpu.jsonl`, `proc_rss.jsonl`: Process-level resource usage
- `host_metrics.jsonl`: System-level CPU/memory time series
- `nodes.json`, `links.json`: Infrastructure topology
- Additional files for placement events, network metrics, etc.

## OpenDC Integration

Collected traces can be converted to OpenDC format for datacenter simulation:
- **Tasks**: Derived from `invocations.jsonl` (task boundaries, resource requirements)
- **Fragments**: Generated from `proc_cpu.jsonl` (fine-grained resource usage over time)

See `docs/README.md` for detailed format specifications and conversion guidelines.

## Documentation

- [Detailed Documentation](docs/README.md) - Complete setup and usage guide
- [FastAPI Example](examples/fastapi_svc/README.md) - HTTP service tracing
- [Scheduler Integration](tools/scheduler/README.md) - Batch job monitoring

## Requirements

- Linux system with standard monitoring tools (mpstat, vmstat, ifstat)
- Python 3.8+
- Docker (optional, for container monitoring)
- vegeta (optional, for load generation)

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
