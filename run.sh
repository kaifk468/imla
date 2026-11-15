#!/usr/bin/env bash
export DISPLAY="${DISPLAY:-:0}"
# Disable auto-repeat for F8 (keycode 74) to prevent ~~~~ characters in terminal
if command -v xset >/dev/null 2>&1; then
    xset -r 74
fi
cd /home/nobroker/Workspace/projects/imla
exec /home/nobroker/Workspace/projects/imla/.venv/bin/python -m imla.main "$@"