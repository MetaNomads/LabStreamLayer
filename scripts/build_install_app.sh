#!/usr/bin/env bash
# Run this ONCE on your Mac, then commit "Install LabStreamLayer.app" to git.
# Users never need to run this.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$REPO_DIR/Install LabStreamLayer.app" 2>/dev/null || true
osacompile -o "$REPO_DIR/Install LabStreamLayer.app" "$REPO_DIR/scripts/install.applescript"
echo "✓ 'Install LabStreamLayer.app' created at repo root — commit it to git."
