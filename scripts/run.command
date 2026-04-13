#!/usr/bin/env bash
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -d "$REPO_DIR/venv" ]; then
    echo "❌ Not installed yet."
    echo "   Double-click 'Install LabStreamLayer.app' first."
    read -p "Press Enter to close..."
    exit 1
fi

cd "$REPO_DIR/src"
"$REPO_DIR/venv/bin/python" main.py
