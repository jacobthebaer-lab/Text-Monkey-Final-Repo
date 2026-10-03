#!/bin/bash
set -eu
umask 077
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT
for attempt in {1..50}; do
  [ -S /tmp/.X11-unix/X99 ] && break
  kill -0 "$xvfb_pid"
  sleep 0.1
done
[ -S /tmp/.X11-unix/X99 ] || exit 2
# Read-only by default. Forward SIGTERM to the worker without auto-resume.
"$@" &
worker_pid=$!
trap 'kill -TERM "$worker_pid" 2>/dev/null || true' TERM INT
wait "$worker_pid"
