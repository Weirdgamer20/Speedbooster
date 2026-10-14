@echo off
:: =============================================================================
:: SpeedBond — Sync Windows edits into WSL then run benchmark
:: =============================================================================
echo Syncing source to WSL...
wsl -d Ubuntu-26.04 -- bash -c "bash /home/tg/speedbond/wsl_sync.sh"
echo.
echo Running dual-WAN benchmark inside WSL...
wsl -d Ubuntu-26.04 -- bash -c "cd /home/tg/speedbond && python3 benchmark.py --url https://speed.hetzner.de/100MB.bin --dest-dir ./bench_output 2>&1"
pause
