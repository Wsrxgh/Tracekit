#!/usr/bin/env python3
"""
Explicit application entry: thin wrapper that forwards to system ffmpeg.
This is NOT the adapter; it is a convenience entry to make the application
boundary explicit in the repository. The real application binary remains
/usr/bin/ffmpeg (or ffmpeg on PATH).

Usage:
  python3 tools/apps/ffmpeg_app.py <ffmpeg-args>

This script simply execs ffmpeg with the provided args and returns its exit code.
"""
from __future__ import annotations
import os, sys, subprocess

def which(cmd: str) -> str:
    for p in os.environ.get("PATH", "").split(os.pathsep):
        f = os.path.join(p, cmd)
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return cmd  # fallback to relying on PATH resolution


def main(argv: list[str]) -> int:
    ffmpeg = which("ffmpeg")
    # Avoid shell=True to prevent injection; pass args as-is
    return subprocess.call([ffmpeg] + argv)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

