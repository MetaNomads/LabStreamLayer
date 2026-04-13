"""
unity.py — UDP handler for Unity communication.

Incoming:  "PING"        → triggers a ping in the GUI
           "ACK:ping_NNN"→ latency echo from Unity (SyncBridge.cs)
Outgoing:  "ping_001"    → broadcast so Unity records receipt timestamp
"""

import logging
import socket
import threading
import time
from typing import Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)
DEFAULT_PORT      = 12345
BROADCAST_ADDRESS = "255.255.255.255"
ACK_TIMEOUT       = 0.5   # seconds to wait for Unity ACK


class UnityHandler(QObject):

    ping_requested      = pyqtSignal()
    status_changed      = pyqtSignal(str)
    log_message         = pyqtSignal(str)
    calibration_changed = pyqtSignal(bool)

    def __init__(self, port: int = DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._port    = port
        self._running = False
        self._sock    = None
        self._out     = None
        self._clients: set = set()
        # Pending ACK tracking: ping_id → (send_ns, threading.Event)
        self._pending_acks: Dict[str, tuple] = {}
        self._ack_lock = threading.Lock()
        self.calibrated_latency_ns: int = -1

    def start(self):
        self._running = True

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        try:
            self._sock.bind(("", self._port))
        except OSError as e:
            self.log_message.emit(f"[Unity] Cannot bind port {self._port}: {e}")
            return

        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        threading.Thread(target=self._listen, daemon=True).start()
        self.log_message.emit(f"[Unity] Listening on UDP port {self._port}")

    def stop(self):
        self._running = False
        self._clients.clear()
        self.calibrated_latency_ns = -1
        self.calibration_changed.emit(False)
        if self._sock:
            self._sock.close()
        if self._out:
            self._out.close()

    def broadcast_ping(self, label: str) -> int:
        """
        Broadcast ping to Unity. Returns calibrated one-way latency in ns,
        or -1 if Unity is not connected.
        """
        if not self._out:
            return -1
        if not self._clients:
            return -1
        try:
            self._out.sendto(label.encode(), (BROADCAST_ADDRESS, self._port))
            self.log_message.emit(f"[Unity] Broadcast: {label}")
        except OSError as e:
            logger.warning(f"Unity broadcast: {e}")
            return -1
        return self.calibrated_latency_ns

    def _single_rtt(self, label: str) -> Optional[int]:
        """
        Send one probe and wait for ACK. Returns RTT in ns or None on timeout.
        """
        if not self._out or not self._clients:
            return None
        event = threading.Event()
        send_ns = time.time_ns()   # Record IMMEDIATELY before sendto
        with self._ack_lock:
            self._pending_acks[label] = (send_ns, event)
        try:
            self._out.sendto(label.encode(), (BROADCAST_ADDRESS, self._port))
        except OSError:
            with self._ack_lock:
                self._pending_acks.pop(label, None)
            return None
        got_ack = event.wait(timeout=ACK_TIMEOUT)
        recv_ns = time.time_ns()
        with self._ack_lock:
            entry = self._pending_acks.pop(label, None)
        if got_ack and entry and entry[0] > 0:
            return recv_ns - entry[0]
        return None

    def calibrate(self, n: int = 5, delay: float = 10.0):
        """
        Run calibration burst in a background thread.
        delay: seconds to wait before probing (10s on connect, 5s on re-calibrate).
        """
        threading.Thread(target=self._do_calibrate, args=(n, delay), daemon=True).start()

    def _do_calibrate(self, n: int, delay: float = 10.0):
        if not self._clients:
            return
        self.log_message.emit(f"[Unity] Calibration in {delay:.0f}s...")
        time.sleep(delay)
        if not self._clients:
            return
        self.log_message.emit(f"[Unity] Calibrating latency ({n} probes)...")
        samples = []
        for i in range(n):
            rtt = self._single_rtt(f"__calib_{i}__")
            if rtt is not None:
                samples.append(rtt)
            time.sleep(0.1)
        if not samples:
            self.log_message.emit("[Unity] Calibration failed — no ACKs")
            return
        samples.sort()
        if len(samples) >= 4:
            samples = samples[:-1]
        median_rtt = samples[len(samples) // 2]
        one_way = median_rtt // 2
        self.calibrated_latency_ns = one_way
        self.log_message.emit(
            f"[Unity] Calibrated: "
            f"median RTT={median_rtt/1e6:.1f}ms  "
            f"one-way={one_way/1e6:.1f}ms  "
            f"(n={len(samples)} samples)"
        )
        self.calibration_changed.emit(True)

    def _listen(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(1024)
                msg = data.decode("utf-8").strip()

                if addr[0] not in self._clients:
                    self._clients.add(addr[0])
                    self.status_changed.emit("connected")
                    self.log_message.emit(f"[Unity] Client connected: {addr[0]}")
                    # Auto-calibrate 3s after first connection
                    self.calibrate(n=5, delay=3.0)

                if msg == "PING":
                    self.log_message.emit(f"[Unity] Ping trigger from {addr[0]}")
                    self.ping_requested.emit()

                elif msg.startswith("ACK:"):
                    ping_id = msg[4:]  # strip "ACK:"
                    with self._ack_lock:
                        entry = self._pending_acks.get(ping_id)
                    if entry:
                        entry[1].set()

            except socket.timeout:
                continue
            except OSError:
                break
