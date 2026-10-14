#!/usr/bin/env bash
# =============================================================================
# SpeedBond WSL Sync Script
# Syncs the latest source from the Windows workspace into WSL home directory.
# Run this whenever you edit files on the Windows side.
# =============================================================================
WIN_SRC="/mnt/c/Users/thega/.gemini/antigravity-ide/scratch/speedbond"
WSL_DEST="/home/tg/speedbond"

echo "[sync] Copying $WIN_SRC -> $WSL_DEST"
rsync -av --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    "$WIN_SRC/" "$WSL_DEST/"
echo "[sync] Done."
