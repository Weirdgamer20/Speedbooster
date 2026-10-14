#!/usr/bin/env bash
# =============================================================================
# SpeedBond WSL Startup Script
# Runs the SpeedBond backend (web server + bonded downloader) inside WSL2.
# The web UI is reachable from Windows at http://localhost:5000
# =============================================================================
set -e

SPEEDBOND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SPEEDBOND_DIR/speedbond.log"
PID_FILE="$SPEEDBOND_DIR/speedbond.pid"

cd "$SPEEDBOND_DIR"

echo "============================================================"
echo "  SpeedBond WSL Backend Startup"
echo "  Dir:  $SPEEDBOND_DIR"
echo "  Log:  $LOG_FILE"
echo "============================================================"

# Pre-flight: check interfaces
echo "[preflight] Detecting network interfaces..."
python3 wsl_preflight.py 2>&1

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[warn] SpeedBond already running (PID $OLD_PID). Kill it first with: kill $OLD_PID"
        exit 1
    fi
    rm -f "$PID_FILE"
fi

echo ""
echo "[start] Launching SpeedBond server on port 5000..."
nohup python3 main.py --web-port 5000 >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > "$PID_FILE"

sleep 1
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[ok] SpeedBond running (PID $SERVER_PID)"
    echo "     Web UI:  http://localhost:5000"
    echo "     Logs:    tail -f $LOG_FILE"
else
    echo "[error] SpeedBond failed to start. Check: $LOG_FILE"
    cat "$LOG_FILE" | tail -20
    exit 1
fi
