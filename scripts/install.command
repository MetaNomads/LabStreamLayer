#!/usr/bin/env bash
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Lab Stream Layer - Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Deactivate Conda if active to prevent it overriding python3
if [ -n "$CONDA_DEFAULT_ENV" ]; then
    echo "→ Deactivating conda env: $CONDA_DEFAULT_ENV"
    source "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" 2>/dev/null && conda deactivate 2>/dev/null || true
fi
unset PYTHONPATH
unset PYTHONHOME

##############################################
# 1. Detect Python (prefer /usr/bin/python3 over Conda)
##############################################
echo "→ Detecting Python..."
if [ -x "/usr/bin/python3" ]; then
    PYTHON_BIN="/usr/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "❌ ERROR: No python found. Install from https://python.org"
    exit 1
fi
echo "✓ $($PYTHON_BIN --version)  ($PYTHON_BIN)"

##############################################
# 2. Create venv
##############################################
echo "→ Creating virtual environment..."
rm -rf "$REPO_DIR/venv" 2>/dev/null || true
$PYTHON_BIN -m venv "$REPO_DIR/venv"
echo "✓ venv created"

##############################################
# 3. Install dependencies
##############################################
echo "→ Installing dependencies..."
source "$REPO_DIR/venv/bin/activate"
pip install --upgrade pip wheel setuptools --quiet
pip install -r "$REPO_DIR/src/requirements.txt"

##############################################
# 4. Verify
##############################################
echo "→ Verifying..."
python - << 'EOF'
import PyQt6.QtCore
from importlib.metadata import version
print("✓ PyQt6:", PyQt6.QtCore.PYQT_VERSION_STR)
print("✓ bleak:", version("bleak"))
EOF

##############################################
# 5. Build LabStreamLayer.app
##############################################
echo "→ Building LabStreamLayer.app..."
rm -rf "$REPO_DIR/LabStreamLayer.app" 2>/dev/null || true
osacompile -o "$REPO_DIR/LabStreamLayer.app" "$REPO_DIR/scripts/LabStreamLayer.applescript"
echo "✓ LabStreamLayer.app ready"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " ✓ Done! Double-click"
echo "   LabStreamLayer.app to launch."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
