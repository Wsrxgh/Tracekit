#!/usr/bin/env python3
import json, os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
run_id = os.environ.get("RUN_ID")
logs_root = root/"logs"
if not run_id:
    runs = sorted([p.name for p in logs_root.iterdir() if p.is_dir()])
    run_id = runs[-1] if runs else None
assert run_id, "No RUN_ID and no logs/* found"
LOGS = logs_root/run_id

module_id = os.environ.get("MODULE_ID", "svc")
app_id    = os.environ.get("APP_ID", "app")
service_name = os.environ.get("SERVICE_NAME", module_id)
module_type = os.environ.get("MODULE_TYPE", "MODULE")  # SOURCE|MODULE|SINK

out = LOGS/"module_inventory.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump({
        "app_id": app_id,
        "module_id": module_id,
        "name": service_name,
        "type": module_type,
        "resources": None
    }, f, indent=2)
print(f"module_inventory → {out}")

