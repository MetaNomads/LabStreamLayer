#!/usr/bin/env python3
"""
contracts_audit.py — programmatic coverage report for @requires/@ensures.

For every public method on the audited classes, prints whether it has at least
one contract attached, and what the contracts say. A red row in the output is
a public method with no contract — i.e. a producer with no enforcement layer.

Run from anywhere:
    python3 scripts/contracts_audit.py

Exits 0 if every method in the EXPECTED_CONTRACTS list has at least one
contract, 1 otherwise. Use in CI to prevent silent regressions in coverage.
"""

from __future__ import annotations

import inspect
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC  = REPO / "src"


# Stubs (same as conftest.py — duplicated so this script runs without pytest).
def _install_stubs():
    class _S:
        def __init__(self, *a, **k): self._slots = []
        def connect(self, fn): self._slots.append(fn)
        def disconnect(self, fn=None): pass
        def emit(self, *a):
            for s in self._slots:
                try: s(*a)
                except Exception: pass
    class _Q:
        def __init__(self, *a, **k): pass
    qt     = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QObject     = _Q
    qtcore.pyqtSignal  = lambda *a, **k: _S()
    qtcore.pyqtSlot    = lambda *a, **k: (lambda fn: fn)
    sys.modules["PyQt6"]        = qt
    sys.modules["PyQt6.QtCore"] = qtcore
    bleak = types.ModuleType("bleak")
    class _BC: pass
    class _BS: pass
    bleak.BleakClient  = _BC
    bleak.BleakScanner = _BS
    sys.modules["bleak"] = bleak
    sys.path.insert(0, str(SRC))


_install_stubs()
os.environ["LSL_CONTRACTS"] = "on"


# ── Methods that MUST have at least one @requires or @ensures ────────────────
# When you add a public producer method that takes inputs that can be wrong
# or returns a value the rest of the system depends on, add it to this list.
# CI runs this script and fails if any required-contract slot is empty.
EXPECTED_CONTRACTS = [
    # (module, class, method)
    ("emotibit",   "EmotiBitHandler", "connect"),
    ("emotibit",   "EmotiBitHandler", "start_recording"),
    ("emotibit",   "EmotiBitHandler", "send_marker"),
    ("emotibit",   "EmotiBitHandler", "public_summary"),
    ("polar_mac",  "PolarHandler",    "start_recording"),
    ("polar_mac",  "PolarHandler",    "send_marker"),
    ("polar_mac",  "PolarHandler",    "public_summary"),
    ("unity",      "UnityHandler",    "connect_device"),
    ("unity",      "UnityHandler",    "broadcast_ping"),
    ("unity",      "UnityHandler",    "set_stream_rate"),
    ("unity",      "UnityHandler",    "public_summary"),
    ("sync_logger","SyncLogger",      "start_session"),
    ("sync_logger","SyncLogger",      "log_ping"),
    ("sync_logger","SyncLogger",      "write_event"),
]


def main() -> int:
    from contracts import get_contracts

    print("Contract coverage audit")
    print("=" * 70)

    missing = []

    by_class: dict = {}
    for module_name, cls_name, method_name in EXPECTED_CONTRACTS:
        by_class.setdefault((module_name, cls_name), []).append(method_name)

    for (module_name, cls_name), methods in by_class.items():
        try:
            mod = __import__(module_name)
            cls = getattr(mod, cls_name)
        except Exception as e:
            print(f"  ! could not import {module_name}.{cls_name}: {e}")
            for m in methods:
                missing.append((module_name, cls_name, m, "import-failed"))
            continue
        print(f"\n{module_name}.{cls_name}")
        for method_name in methods:
            method = getattr(cls, method_name, None)
            if method is None:
                print(f"  ?  {method_name}: method not found")
                missing.append((module_name, cls_name, method_name, "missing"))
                continue
            contracts = get_contracts(method)
            if not contracts:
                print(f"  \033[31m✗\033[0m  {method_name}: NO CONTRACT")
                missing.append((module_name, cls_name, method_name, "no-contract"))
                continue
            kinds = ", ".join(k for k, _, _ in contracts)
            print(f"  \033[32m✓\033[0m  {method_name}  [{kinds}]")
            for kind, _src, msg in contracts:
                print(f"       {kind}: {msg}")

    print("\n" + "=" * 70)
    if missing:
        print(f"FAIL — {len(missing)} expected contract(s) missing:")
        for m in missing:
            print(f"  {m[0]}.{m[1]}.{m[2]}  ({m[3]})")
        return 1
    print(f"OK — all {len(EXPECTED_CONTRACTS)} expected contracts present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
