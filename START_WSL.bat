@echo off
:: =============================================================================
:: SpeedBond — Launch WSL Backend from Windows
:: Double-click this to start SpeedBond inside WSL2 Ubuntu.
:: The web UI will be at http://localhost:5000
:: =============================================================================
echo Starting SpeedBond in WSL2...
wsl -d Ubuntu-26.04 -- bash -c "cd /home/tg/speedbond && bash wsl_start.sh"
pause
