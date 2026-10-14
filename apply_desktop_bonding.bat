@echo off
title SpeedBond - Native Desktop Network Balancing
echo ================================================================
echo  SpeedBond: Applying Native Kernel Network Balancing
echo ================================================================
echo.

:: Check for admin rights and auto-elevate
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"\"%~dp0apply_desktop_bonding.bat\"\"' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_desktop_bonding.ps1"
echo.
echo ================================================================
echo  Press any key to close this window...
echo ================================================================
pause >nul
