"""
conftest.py — shared pytest fixtures and module stubs for the LSL test suite.

Stubs PyQt6 and bleak in sys.modules so handler imports succeed on any Python.
This is the same trick `scripts/smoke_test.py` uses — extracted here so pytest
collects it once for the whole suite.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC  = REPO / "src"


# ── 0. Stubs ──────────────────────────────────────────────────────────────────

class _FakeSignal:
    def __init__(self, *args, **kwargs):
        self._slots = []
    def connect(self, fn):           self._slots.append(fn)
    def disconnect(self, fn=None):
        if fn is None: self._slots.clear()
        elif fn in self._slots: self._slots.remove(fn)
    def emit(self, *args):
        for s in list(self._slots):
            try: s(*args)
            except Exception: pass


class _FakeQObject:
    def __init__(self, *args, **kwargs): pass


def _install_stubs():
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QObject     = _FakeQObject
    qtcore.pyqtSignal  = lambda *a, **k: _FakeSignal()
    qtcore.pyqtSlot    = lambda *a, **k: (lambda fn: fn)
    qt = types.ModuleType("PyQt6")
    qt.QtCore = qtcore
    sys.modules["PyQt6"]        = qt
    sys.modules["PyQt6.QtCore"] = qtcore

    bleak = types.ModuleType("bleak")
    class _BleakClient:    pass
    class _BleakScanner:   pass
    bleak.BleakClient  = _BleakClient
    bleak.BleakScanner = _BleakScanner
    sys.modules["bleak"] = bleak

    sys.path.insert(0, str(SRC))


_install_stubs()


# ── 1. Force contracts ON for the test suite ─────────────────────────────────

os.environ["LSL_CONTRACTS"] = "on"
# If contracts.py was already imported, re-load it so the env var takes effect.
if "contracts" in sys.modules:
    import importlib; importlib.reload(sys.modules["contracts"])


# ── 2. Common fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def emotibit_handler():
    """Fresh EmotiBitHandler with no sockets bound."""
    from emotibit import EmotiBitHandler
    h = EmotiBitHandler()
    yield h
    # Clean shutdown so background threads exit fast
    h.stop()


@pytest.fixture
def unity_handler():
    from unity import UnityHandler
    h = UnityHandler()
    yield h
    h._running = False


@pytest.fixture
def polar_handler(tmp_path):
    from polar_mac import PolarHandler
    h = PolarHandler(tmp_path)
    yield h


@pytest.fixture
def sync_logger(tmp_path):
    from sync_logger import SyncLogger
    sl = SyncLogger(tmp_path)
    sl.start_session("testts", tmp_path)
    yield sl
    sl.close()
