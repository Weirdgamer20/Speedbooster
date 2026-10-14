@echo off
title SpeedBond - Restore Default Windows Metrics
echo ================================================================
echo  SpeedBond: Restoring Windows Default Network Settings
echo ================================================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"\"%~dp0reset_desktop_bonding.bat\"\"' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_desktop_bonding.ps1"
echo.
echo ================================================================
echo  Default settings restored. Press any key to exit...
echo ================================================================
pause >nul
