"""
core/polar.py — Polar H10 handler

Architecture: spawns polar_subprocess.py as a child process.
- The subprocess handles all BLE operations (simplepyble + winrt)
- Main app communicates via JSON lines over stdin/stdout
- Avoids Python 3.14 asyncio/WinRT incompatibilities in main process

Subprocess: polar_subprocess.py (same directory as main.py)
Python required for subprocess: 3.x (same Python or py launcher)

Connection flow (from Polar SDK analysis):
  1. Detect if paired → if not, register silent pairing handler
  2. simplepyble: scan → connect → subscribe PMD_DATA + HR (no auth)
  3. simplepyble write_request PMD_CONTROL → triggers pairing
  4. Silent pairing handler auto-accepts → Windows takes connection
  5. Release simplepyble objects
  6. winrt: write ECG_SETTINGS + ECG_START
  7. ECG + HR data flows
"""

import csv
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent
SUBPROCESS_SCRIPT = _HERE / "polar_subprocess.py"

PYTHON_EXE = [sys.executable]


class PolarStatus(Enum):
    IDLE      = auto()
    SCANNING  = auto()
    CONNECTED = auto()
    RECORDING = auto()


@dataclass
class PolarDevice:
    name:    str
    address: str

    @property
    def display_name(self) -> str:
        return f"{self.name}  [{self.address}]"


class PolarHandler(QObject):

    status_changed = pyqtSignal(PolarStatus)
    devices_found  = pyqtSignal(list)
    log_message    = pyqtSignal(str)

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self._output_dir = output_dir
        self._proc:   Optional[subprocess.Popen] = None
        self._status  = PolarStatus.IDLE
        self._csv_file   = None
        self._writer     = None

    def start(self):
        self._launch_subprocess()

    def stop(self):
        self._send({"cmd": "quit"})
        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._end_recording()

    def scan(self):
        self._send({"cmd": "scan"})
        self._set_status(PolarStatus.SCANNING)

    def connect_device(self, device: PolarDevice):
        self._send({"cmd": "connect", "address": device.address})

    def disconnect(self):
        self._send({"cmd": "disconnect"})
        self._set_status(PolarStatus.IDLE)

    def start_recording(self, session_ts: str):
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / f"polar_{session_ts}.csv"
        self._csv_file = open(path, "w", newline="", buffering=1)
        self._writer   = csv.writer(self._csv_file)
        self._writer.writerow(["utc_epoch_ns", "ecg_uv", "hr_bpm", "rr_ms", "marker"])
        self._set_status(PolarStatus.RECORDING)
        self.log_message.emit(f"[Polar] Recording → {path.name}")
        self._send({"cmd": "start_rec", "session_ts": session_ts})

    def stop_recording(self):
        self._send({"cmd": "stop_rec"})
        self._end_recording()

    def send_marker(self, label: str):
        self._send({"cmd": "marker", "label": label})
        if self._writer:
            self._writer.writerow([time.time_ns(), "", "", "", label])

    # ── Subprocess ────────────────────────────────────────────────────────────

    def _launch_subprocess(self):
        if not SUBPROCESS_SCRIPT.exists():
            self.log_message.emit(f"[Polar] Missing: {SUBPROCESS_SCRIPT}")
            return
        cmd = PYTHON_EXE + [str(SUBPROCESS_SCRIPT)]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            self.log_message.emit(f"[Polar] Subprocess started (pid={self._proc.pid})")
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        except Exception as e:
            self.log_message.emit(f"[Polar] Failed to start subprocess: {e}")

    def _send(self, cmd: dict):
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(json.dumps(cmd) + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                pass

    def _read_stdout(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line: continue
            try:
                self._handle(json.loads(line))
            except json.JSONDecodeError:
                self.log_message.emit(f"[Polar] {line}")

    def _read_stderr(self):
        for line in self._proc.stderr:
            line = line.strip()
            if line:
                self.log_message.emit(f"[Polar:err] {line}")

    def _handle(self, msg: dict):
        t = msg.get("type")
        if t == "status":
            self.log_message.emit(f"[Polar] {msg['msg']}")
        elif t == "error":
            self.log_message.emit(f"[Polar] ERROR: {msg['msg']}")
            self._set_status(PolarStatus.IDLE)
        elif t == "device":
            dev = PolarDevice(name=msg["name"], address=msg["address"])
            self.devices_found.emit([dev])
        elif t == "connected":
            self.log_message.emit(f"[Polar] Connected MTU={msg.get('mtu','?')}")
            if self._status != PolarStatus.RECORDING:
                self._set_status(PolarStatus.CONNECTED)
        elif t == "disconnected":
            self._end_recording()
            self._set_status(PolarStatus.IDLE)
        elif t == "ecg" and self._writer:
            self._writer.writerow([msg["ts_ns"], msg["uv"], "", "", ""])
        elif t == "hr" and self._writer:
            self._writer.writerow([msg["ts_ns"], "", msg["bpm"], "", ""])
        elif t == "rr" and self._writer:
            self._writer.writerow([msg["ts_ns"], "", "", msg["ms"], ""])

    def _end_recording(self):
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._writer   = None
            self.log_message.emit("[Polar] Recording stopped")
        if self._status == PolarStatus.RECORDING:
            self._set_status(PolarStatus.CONNECTED)

    def _set_status(self, s: PolarStatus):
        if s != self._status:
            self._status = s
            self.status_changed.emit(s)
