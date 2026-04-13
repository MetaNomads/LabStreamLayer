#!/usr/bin/env bash
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -d "$REPO_DIR/venv" ]; then
    echo "❌ Not installed yet."
    echo "   Double-click 'Install LabStreamLayer.app' first."
    read -p "Press Enter to close..."
    exit 1
fi

# Deactivate Conda if active — its PyQt6/_socket conflict with the venv's Qt
if [ -n "$CONDA_DEFAULT_ENV" ]; then
    echo "[LSL] Deactivating conda env: $CONDA_DEFAULT_ENV"
    source "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" 2>/dev/null && conda deactivate 2>/dev/null || true
fi

# Clear any Conda/system Python path injection
unset PYTHONPATH
unset PYTHONHOME

cd "$REPO_DIR/src"
exec "$REPO_DIR/venv/bin/python3" main.py
