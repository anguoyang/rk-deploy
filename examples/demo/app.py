#!/usr/bin/env python3
"""Generic rk-deploy demo application: a heartbeat writer.

It exists only to exercise the deployment machinery (pack, install,
upgrade, rollback, status). It is deliberately not a real product:
every second it writes a small JSON heartbeat into the shared
directory rk-deploy points it at via RKDEPLOY_SHARED.
"""
import json
import os
import time
from pathlib import Path

VERSION = os.environ.get("RKDEPLOY_VERSION", "unknown")
SHARED = Path(os.environ.get("RKDEPLOY_SHARED", "."))
SHARED.mkdir(parents=True, exist_ok=True)

print(f"demo {VERSION} started", flush=True)
beat = 0
while True:
    beat += 1
    (SHARED / "heartbeat.json").write_text(
        json.dumps({"app": "demo", "version": VERSION, "beat": beat}) + "\n")
    time.sleep(1)
