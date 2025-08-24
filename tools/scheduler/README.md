Minimal cloud-like scheduler (no Kubernetes)

Roles:
- cloud0: Redis server + dispatcher
- cloud1/cloud2: worker nodes that pull tasks and run ffmpeg via tools/adapters/ffmpeg_wrapper.sh

Design:
- Per-node queues: q:cloud0, q:cloud1, q:cloud2 (avoids scheduling event logs; routing decided by dispatcher)
- Routing policy: file-name sorted round-robin (NR % 3)
- No scheduling events are recorded. Only ffmpeg completion is appended by the wrapper to logs/$RUN_ID/events.ffmpeg.jsonl.

Install (once on each VM):
- sudo apt install -y python3-pip redis-server  # redis-server only on cloud0 if preferred
- python3 -m pip install -r tools/scheduler/requirements.txt

Start Redis on cloud0:
- sudo systemctl enable --now redis-server || sudo service redis-server start

Dispatch tasks (cloud0):
- python3 tools/scheduler/dispatcher.py --inputs inputs --outputs outputs --scale 1280:720 --preset veryfast --crf 28 --nodes cloud0,cloud1,cloud2 --policy rr3

Run workers (cloud1/cloud2):
- NODE_ID=cloud1 python3 tools/scheduler/worker.py --outputs outputs --parallel 2
- NODE_ID=cloud2 python3 tools/scheduler/worker.py --outputs outputs --parallel 2

Notes:
- Dispatcher enqueues per-node tasks (no central queue). This minimizes runtime coordination and avoids logging scheduling events.
- Workers BRPOP their own node queue. On SIGINT, they exit gracefully after current tasks.
- Inputs need to be present on the worker node's filesystem. Use scp/rsync to sync files to cloud1/cloud2.

