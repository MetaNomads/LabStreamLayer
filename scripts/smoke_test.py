#!/usr/bin/env python3
"""
smoke_test.py — Hardware-free smoke test for the LSL panel-audited fixes.

Run from anywhere:
    python3 scripts/smoke_test.py

Exits 0 on all-pass, 1 on any failure. Runs in ~2 seconds.

This catches every bug the panel found that does NOT require a real BLE radio,
a real UDP peer, or a real Quest. That's most of them — including the four
CRITICALs from pass 1, the regression introduced in pass 1, and the schema /
wire-format / source-IP-gate fixes from pass 2.

What it does NOT catch (do these once, on real hardware):
  - The liveness watchdog actually triggering after 10s of silence.
  - Polar BLE pairing race on Windows.
  - Quest don/doff via Pause vs Focus.
  - The sensor_lost row actually appearing in syncLog after a real disconnect.

Strategy: stub PyQt6 and bleak in sys.modules so the handler classes import
cleanly even on a Python that has neither installed. Then call the previously
broken code paths and assert they don't crash.
"""

import os
import sys
import time
import types
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC  = REPO / "src"

# ── 0. Stub PyQt6 and bleak so we don't need them installed ────────────────────

class _FakeSignal:
    """Mimics QtCore.pyqtSignal — slots are plain callables."""
    def __init__(self, *args, **kwargs):
        self._slots = []
    def connect(self, fn):
        self._slots.append(fn)
    def disconnect(self, fn=None):
        if fn is None:
            self._slots.clear()
        elif fn in self._slots:
            self._slots.remove(fn)
    def emit(self, *args):
        for s in list(self._slots):
            try: s(*args)
            except Exception: pass

class _FakeQObject:
    def __init__(self, *args, **kwargs): pass

def _pyqtSignal(*args, **kwargs):
    # Class-level attribute — return a per-instance descriptor-ish factory.
    return _FakeSignal()

def _pyqtSlot(*args, **kwargs):
    return lambda fn: fn

qtcore = types.ModuleType("PyQt6.QtCore")
qtcore.QObject     = _FakeQObject
qtcore.pyqtSignal  = _pyqtSignal
qtcore.pyqtSlot    = _pyqtSlot
qt = types.ModuleType("PyQt6")
qt.QtCore = qtcore
sys.modules["PyQt6"] = qt
sys.modules["PyQt6.QtCore"] = qtcore

# bleak stub — only needed so polar_mac imports cleanly.
bleak = types.ModuleType("bleak")
class _BleakClient:    pass
class _BleakScanner:   pass
bleak.BleakClient  = _BleakClient
bleak.BleakScanner = _BleakScanner
sys.modules["bleak"] = bleak

sys.path.insert(0, str(SRC))


# ── 1. Tiny test runner (no pytest dep) ────────────────────────────────────────

results = []
def TEST(name):
    def deco(fn):
        try:
            fn()
            results.append(("PASS", name, ""))
            print(f"  \033[32mPASS\033[0m  {name}")
        except AssertionError as e:
            results.append(("FAIL", name, str(e)))
            print(f"  \033[31mFAIL\033[0m  {name}: {e}")
        except Exception as e:
            import traceback
            tb = traceback.format_exc(limit=2).strip().splitlines()[-1]
            results.append(("ERROR", name, f"{type(e).__name__}: {e}"))
            print(f"  \033[33mERR \033[0m  {name}: {type(e).__name__}: {e}")
        return fn
    return deco


print("LSL panel-audit smoke test")
print("=" * 60)


# ── 2. Tests ───────────────────────────────────────────────────────────────────

@TEST("PASS-1 CRITICAL #1: EmotiBit __init__ defines previously-dead attrs")
def _():
    from emotibit import EmotiBitHandler
    h = EmotiBitHandler()
    for name in ("_rtt_buffer", "_continuous_calib_active",
                 "_session_latency_ns", "_last_sample_ns", "_given_up",
                 "_shutdown_event"):
        assert hasattr(h, name), f"missing {name}"
    # The original bug parked these inside a property after `return` — they
    # never executed. If this passes, the dead-after-return is gone.

@TEST("PASS-1 CRITICAL #1: EmotiBit effective_latency_ns property exists")
def _():
    from emotibit import EmotiBitHandler
    h = EmotiBitHandler()
    assert h.effective_latency_ns == -1   # not yet calibrated

@TEST("PASS-1 CRITICAL #2: EmotiBit add_manual_device alias works")
def _():
    from emotibit import EmotiBitHandler
    h = EmotiBitHandler()
    # main_window.py:526 calls add_manual_device. Pre-fix: AttributeError.
    assert callable(h.add_manual_device)
    # Invalid IP returns None without spawning the arp subprocess thread.
    assert h.add_manual_device("not-an-ip") is None

@TEST("PASS-1 CRITICAL #3: ACK with unity_ns parses and fires unity_ack_received")
def _():
    from unity import UnityHandler, UnityDevice
    h = UnityHandler()
    h._device = UnityDevice(ip="10.0.0.1", name="X")
    fired = []
    h.unity_ack_received.connect(lambda pid, ns: fired.append((pid, int(ns))))
    h._handle("ACK:ping_007:1234567890", "10.0.0.1")
    assert fired == [("ping_007", 1234567890)], f"got {fired}"

@TEST("PASS-1 CRITICAL #3: Old ACK format (no ns) does NOT fire unity_ack_received")
def _():
    from unity import UnityHandler, UnityDevice
    h = UnityHandler()
    h._device = UnityDevice(ip="10.0.0.1", name="X")
    fired = []
    h.unity_ack_received.connect(lambda pid, ns: fired.append((pid, ns)))
    h._handle("ACK:ping_007", "10.0.0.1")   # the pre-fix wire format
    assert fired == [], f"old ACK format unexpectedly fired: {fired}"

@TEST("PASS-2 REGRESSION FIX: Unity RECONNECT processed when _device is None")
def _():
    from unity import UnityHandler
    h = UnityHandler()
    assert h._device is None
    h._handle("RECONNECT,Quest_42", "192.168.1.50")
    # If the IP gate sat above the RECONNECT handler (the pass-1 bug), this
    # would still be None.
    assert h._device is not None, "RECONNECT did not restore _device"
    assert h._device.ip == "192.168.1.50"
    assert h._device.name == "Quest_42"

@TEST("Source-IP gate: Unity PING from wrong IP is dropped")
def _():
    from unity import UnityHandler, UnityDevice
    h = UnityHandler()
    h._device = UnityDevice(ip="10.0.0.1", name="real")
    fired = [0]
    h.ping_requested.connect(lambda: fired.__setitem__(0, fired[0] + 1))
    h._handle("PING", "10.0.0.99")   # wrong IP
    assert fired[0] == 0, "PING from wrong IP was honoured (cross-experiment risk)"
    h._handle("PING", "10.0.0.1")    # right IP
    assert fired[0] == 1, "PING from correct IP was dropped"

@TEST("Source-IP gate: EmotiBit HH from wrong IP doesn't set _hh_event")
def _():
    from emotibit import EmotiBitHandler, EmotiBitDevice
    h = EmotiBitHandler()
    h._connected = EmotiBitDevice(ip="10.0.0.1")
    h._hh_event.clear()
    # Note: HH-discovery side-effect spawns a daemon arp thread — harmless.
    h._parse_line("123,1,0,HH,1,100,DI,foo,DP,3131", "10.0.0.99")
    assert not h._hh_event.is_set(), "HH from wrong IP set the calibration event"
    h._parse_line("123,1,0,HH,1,100,DI,foo,DP,3131", "10.0.0.1")
    assert h._hh_event.is_set(), "HH from correct IP did NOT set the event"

@TEST("Headset don/doff and app_quitting raise headset_state_changed")
def _():
    from unity import UnityHandler, UnityDevice
    h = UnityHandler()
    h._device = UnityDevice(ip="10.0.0.1", name="X")
    seen = []
    h.headset_state_changed.connect(seen.append)
    for label in ("headset_doffed", "headset_donned", "app_quitting"):
        h._handle(label, "10.0.0.1")
    assert seen == ["headset_doffed", "headset_donned", "app_quitting"], f"got {seen}"

@TEST("public_summary returns a dict on every handler")
def _():
    from emotibit import EmotiBitHandler
    from unity import UnityHandler
    from polar_mac import PolarHandler
    assert isinstance(EmotiBitHandler().public_summary(), dict)
    assert isinstance(UnityHandler().public_summary(), dict)
    assert isinstance(PolarHandler(Path("/tmp")).public_summary(), dict)

@TEST("Polar public_summary records calibration_method honestly")
def _():
    from polar_mac import PolarHandler
    s = PolarHandler(Path("/tmp")).public_summary()
    # Pass-2 panel said: document that BATTERY_CHAR reads measure cache, not radio.
    assert s.get("calibration_method") == "battery_char_read", \
        f"calibration_method should be set: {s}"

@TEST("SyncLogger.write_event uses empty latency_ns for non-ping rows")
def _():
    import csv
    from sync_logger import SyncLogger
    with tempfile.TemporaryDirectory() as d:
        sl = SyncLogger(Path(d))
        sl.start_session("test", Path(d))
        sl.write_event("emotibit", "sensor_lost")
        sl.write_event("unity",    "headset_doffed")
        sl.close()
        rows = list(csv.reader(open(Path(d) / "syncLog_test.csv")))
        # rows[0] is the header. rows[1+] are events.
        for row in rows[1:]:
            assert row[4] == "", \
                f"non-ping row latency_ns should be '' (not -1): {row}"
            # local_epoch_ns auto-filled
            assert row[3] != "", f"local_epoch_ns should auto-fill: {row}"

@TEST("Concurrency: shutdown_event wakes a sleeping retry loop fast")
def _():
    from emotibit import EmotiBitHandler
    h = EmotiBitHandler()
    woke = []
    def waiter():
        if h._shutdown_event.wait(timeout=10.0):
            woke.append(time.monotonic())
    threading.Thread(target=waiter, daemon=True).start()
    time.sleep(0.05)
    t0 = time.monotonic()
    h.stop()                 # sets _shutdown_event
    deadline = t0 + 0.5
    while not woke and time.monotonic() < deadline:
        time.sleep(0.01)
    assert woke, "Retry-loop waiter did not wake within 0.5s of stop()"
    assert woke[0] - t0 < 0.5, f"Wake took {(woke[0]-t0)*1000:.0f}ms (>500ms)"


# ── 3. Static (grep-style) checks — catch wire-format / config drift ──────────

@TEST("STATIC: SyncBridge.cs ACK includes :{ns} in the wire format")
def _():
    text = (REPO / "SyncBridge.cs").read_text()
    assert '$"ACK:{msg}:{ns}"' in text, "SyncBridge.cs ACK does not include :{ns}"

@TEST("STATIC: SyncBridge.cs has OnApplicationFocus AND OnApplicationPause")
def _():
    text = (REPO / "SyncBridge.cs").read_text()
    assert "OnApplicationFocus" in text, "missing OnApplicationFocus"
    assert "OnApplicationPause" in text, "missing OnApplicationPause"

@TEST("STATIC: SyncBridge.cs has lock-release timer (HOST_QUIET_SECONDS)")
def _():
    text = (REPO / "SyncBridge.cs").read_text()
    assert "HOST_QUIET_SECONDS" in text, "lock auto-release missing"

@TEST("STATIC: README and code agree on output dir name")
def _():
    readme = (REPO / "README.md").read_text()
    main_window = (SRC / "main_window.py").read_text()
    assert "LabStreamLayer_Recordings" in readme
    assert "LabStreamLayer_Recordings" in main_window
    assert "SyncBridge_Recordings" not in main_window, \
        "main_window still uses old SyncBridge_Recordings dir name"

@TEST("STATIC: main_window has per-sensor SAMPLE_SILENCE_S dict")
def _():
    text = (SRC / "main_window.py").read_text()
    assert 'SAMPLE_SILENCE_S = {' in text
    for k in ('"emotibit"', '"polar"', '"unity"'):
        assert k in text, f"per-sensor key {k} missing from SAMPLE_SILENCE_S"

@TEST("STATIC: dead RECONNECT_BACKOFF_S in main_window.py is removed")
def _():
    text = (SRC / "main_window.py").read_text()
    # The pass-2 panel flagged this as a dead module-level constant (the live
    # one lives in emotibit.py with a leading underscore).
    assert "RECONNECT_BACKOFF_S = (" not in text, \
        "Dead RECONNECT_BACKOFF_S constant still present in main_window.py"

@TEST("STATIC: emotibit.py has _liveness_loop (pass-2 fix)")
def _():
    text = (SRC / "emotibit.py").read_text()
    assert "def _liveness_loop" in text, \
        "missing the liveness watchdog that makes bounded retry actually trigger"

@TEST("STATIC: polar_mac.py uses BATTERY_CHAR for calibration (not PMD_CONTROL)")
def _():
    text = (SRC / "polar_mac.py").read_text()
    # Scope to JUST the calibrate_for_recording branch (stop at next elif).
    idx = text.find('elif action == "calibrate_for_recording":')
    assert idx > 0, "calibrate_for_recording branch missing"
    end = text.find('elif action ==', idx + 50)   # next elif
    block = text[idx : end if end > 0 else idx + 2000]
    # Strip comments before scanning so the explanatory note in the docstring
    # doesn't trip the regex.
    code_only = "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )
    assert "BATTERY_CHAR" in code_only, "calibration not reading BATTERY_CHAR"
    assert "PMD_CONTROL, ECG_SETTINGS" not in code_only, \
        "calibration still writes ECG_SETTINGS to PMD_CONTROL during streaming"

@TEST("STATIC: polar_mac has cancellable _pending_reconnect (pass-2 fix)")
def _():
    text = (SRC / "polar_mac.py").read_text()
    assert "_pending_reconnect" in text, \
        "Polar reconnect handle not stored — user disconnect can't cancel pending reconnect"
    assert "_pending_reconnect.cancel()" in text, \
        "_pending_reconnect handle exists but is never cancelled"


# ── 4. Summary ────────────────────────────────────────────────────────────────

print("=" * 60)
fails  = sum(1 for r in results if r[0] in ("FAIL", "ERROR"))
passes = sum(1 for r in results if r[0] == "PASS")
print(f"\n{passes}/{len(results)} passed", end="")
if fails:
    print(f"  —  {fails} FAILED")
    for status, name, msg in results:
        if status != "PASS":
            print(f"  [{status}] {name}: {msg}")
    sys.exit(1)
else:
    print("  —  all green")
    sys.exit(0)
