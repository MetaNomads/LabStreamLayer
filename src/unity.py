"""
unity.py — UDP handler for Unity / LSLConnector.cs

Discovery strategy (cross-subnet capable):
  1. Broadcast DISCOVER on all local subnet broadcast addresses
  2. Unicast DISCOVER to every IP in the local /24 subnet(s) concurrently
  This covers same-subnet via broadcast AND cross-subnet via unicast sweep.

Protocol:
  LSL → DISCOVER   (broadcast + unicast sweep)
  Unity ← HELLO,unity,<name>
  LSL → CONNECT    (unicast to Unity's IP)
  Unity ← CONNECTED,<name>
"""

import ipaddress
import logging
import socket
import threading
from collections import deque
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)
DEFAULT_PORT      = 12345
BROADCAST_ADDRESS = "255.255.255.255"
ACK_TIMEOUT       = 0.5


@dataclass
class UnityDevice:
    ip:   str
    name: str

    @property
    def display_name(self) -> str:
        return f"{self.name}  [{self.ip}]"


class UnityHandler(QObject):

    ping_requested      = pyqtSignal()
    status_changed      = pyqtSignal(str)
    log_message         = pyqtSignal(str)
    calibration_changed = pyqtSignal(bool)
    devices_found       = pyqtSignal(list)
    scan_progress       = pyqtSignal(str)
    data_received       = pyqtSignal(str)   # raw DATA packet for live monitor
    recording_started   = pyqtSignal()      # Unity recorder started → LSL should start too
    recording_stopped   = pyqtSignal()      # Unity recorder stopped
    unity_ack_received  = pyqtSignal(str, object)  # (ping_id, unity_epoch_ns as int)

    def __init__(self, port: int = DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self._port      = port
        self._running   = False
        self._sock:  Optional[socket.socket] = None
        self._out:   Optional[socket.socket] = None
        self._device: Optional[UnityDevice]  = None
        self.calibrated_latency_ns: int = -1
        self._pending_acks: Dict[str, tuple] = {}
        self._ack_lock     = threading.Lock()
        self._scan_results: List[UnityDevice] = []
        self._scan_lock    = threading.Lock()
        self._connect_event = threading.Event()
        self._streaming = False
        self._rtt_buffer = deque(maxlen=20)
        self._continuous_calib_active = False
        self._session_latency_ns: int = -1   # locked at record-start calibration
        self._stream_interval: float = 1.0   # seconds between REQUEST_DATA

    # ── Lifecycle ─────────────────────────────────────────────────────────────

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
        self._device  = None
        self.calibrated_latency_ns = -1
        self._session_latency_ns = -1
        self.calibration_changed.emit(False)
        self.status_changed.emit("disconnected")
        if self._sock: self._sock.close()
        if self._out:  self._out.close()

    # ── Network helpers ───────────────────────────────────────────────────────

    def _get_local_ips(self) -> List[str]:
        """Return all non-loopback IPv4 addresses on this machine."""
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ips.append(ip)
        except Exception:
            pass
        # Fallback
        if not ips:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    ips.append(s.getsockname()[0])
            except Exception:
                pass
        return list(set(ips))

    def _get_subnets(self) -> List[ipaddress.IPv4Network]:
        """Return /24 networks for all local IPs."""
        nets = []
        for ip in self._get_local_ips():
            try:
                net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
                nets.append(net)
            except Exception:
                pass
        return nets

    # ── Discovery ─────────────────────────────────────────────────────────────

    def scan(self, duration: float = 8.0):
        """
        Full cross-subnet scan:
          1. Broadcast DISCOVER on all subnet broadcast addresses
          2. Unicast DISCOVER to every .1-.254 in local /24(s) concurrently
        """
        threading.Thread(target=self._do_scan, args=(duration,), daemon=True).start()

    def _do_scan(self, duration: float):
        if not self._out:
            self.log_message.emit("[Unity] Cannot scan — socket not started")
            self.devices_found.emit([])
            return

        with self._scan_lock:
            self._scan_results.clear()

        subnets = self._get_subnets()
        local_ips = self._get_local_ips()

        # Build probe target list
        # 1. Subnet broadcast addresses (fast, same-subnet only)
        broadcasts = [BROADCAST_ADDRESS]
        for net in subnets:
            bc = str(net.broadcast_address)
            if bc not in broadcasts:
                broadcasts.append(bc)

        # 2. All host IPs in local /24(s) for cross-subnet reach
        unicast_targets = []
        for net in subnets:
            for host in net.hosts():
                ip = str(host)
                if ip not in local_ips:          # skip ourselves
                    unicast_targets.append(ip)

        total = len(unicast_targets) + len(broadcasts)
        self.scan_progress.emit(
            f"Scanning {len(broadcasts)} broadcast + {len(unicast_targets)} unicast addresses..."
        )
        self.log_message.emit(
            f"[Unity] Scanning: {', '.join(broadcasts)}  "
            f"+ {len(unicast_targets)} unicast IPs across {len(subnets)} subnet(s)"
        )

        # Phase 1: broadcast (cheap, fast — covers same subnet)
        for bc in broadcasts:
            try:
                self._out.sendto(b"DISCOVER", (bc, self._port))
            except OSError:
                pass
        time.sleep(0.5)

        # Phase 2: unicast sweep (cross-subnet capable)
        # Use a thread pool — send 50 at a time to avoid socket flooding
        BATCH = 50
        sent  = 0
        for i in range(0, len(unicast_targets), BATCH):
            if not self._running:
                break
            batch = unicast_targets[i:i + BATCH]
            for ip in batch:
                try:
                    self._out.sendto(b"DISCOVER", (ip, self._port))
                    sent += 1
                except OSError:
                    pass
            # Small pause between batches so listener can process replies
            time.sleep(0.08)
            self.scan_progress.emit(
                f"Probing... {sent}/{len(unicast_targets)} IPs"
            )

        # Wait remainder of duration for late replies
        elapsed = 0.5 + (len(unicast_targets) / BATCH) * 0.08
        remaining = duration - elapsed
        if remaining > 0:
            time.sleep(remaining)

        with self._scan_lock:
            found = list(self._scan_results)

        msg = f"Scan complete — {len(found)} device(s) found."
        if not found:
            msg += "  Try entering the IP manually."
        self.scan_progress.emit(msg)
        self.log_message.emit(f"[Unity] {msg}")
        self.devices_found.emit(found)

    # ── Connect ───────────────────────────────────────────────────────────────

    def connect_device(self, device: UnityDevice):
        threading.Thread(target=self._do_connect, args=(device,), daemon=True).start()

    def _do_connect(self, device: UnityDevice):
        self.log_message.emit(f"[Unity] Connecting to {device.display_name}...")
        self._connect_event.clear()
        try:
            self._out.sendto(b"CONNECT", (device.ip, self._port))
        except OSError as e:
            self.log_message.emit(f"[Unity] Connect failed: {e}")
            return
        # Send CONNECT repeatedly — UDP can be lost, and the first
        # packet may be blocked while the OS firewall dialog is shown.
        # Try every 0.5s for up to 6 seconds.
        got = False
        for attempt in range(12):
            if attempt > 0:
                self.log_message.emit(
                    f"[Unity] Retrying CONNECT ({attempt+1}/12)..."
                )
            try:
                self._out.sendto(b"CONNECT", (device.ip, self._port))
            except OSError as e:
                self.log_message.emit(f"[Unity] Send error: {e}")
                return
            got = self._connect_event.wait(timeout=0.5)
            if got:
                break

        if got:
            self._device = device
            self.status_changed.emit("connected")
            self.log_message.emit(f"[Unity] Connected to {device.display_name}")
            threading.Thread(target=self.start_continuous_calibration, daemon=True).start()
            self.start_data_stream()   # stream at current rate while connected
        else:
            self.log_message.emit(
                f"[Unity] No response from {device.ip} after 6s.\n"
                f"  → On the Unity machine ({device.ip}) check:\n"
                f"     1. macOS: System Settings → Firewall → allow Unity Editor\n"
                f"        (or a dialog may have appeared asking to allow connections — click Allow)\n"
                f"     2. Windows: Windows Defender Firewall → allow Unity.exe for UDP\n"
                f"     3. Confirm LSLConnector.cs is on an active GameObject and scene is in Play mode\n"
                f"     4. Confirm udpPort in LSLConnector.cs matches LSL port ({self._port})"
            )

    def disconnect_device(self):
        self._streaming = False
        if self._device and self._out:
            try:
                self._out.sendto(b"DISCONNECT", (self._device.ip, self._port))
            except OSError:
                pass
        self._device = None
        self.calibrated_latency_ns = -1
        self.calibration_changed.emit(False)
        self.status_changed.emit("disconnected")
        self.log_message.emit("[Unity] Disconnected")

    @property
    def is_connected(self) -> bool:
        return self._device is not None

    @property
    def device_ip(self) -> Optional[str]:
        return self._device.ip if self._device else None

    # ── Ping & calibration ────────────────────────────────────────────────────

    def send_command(self, cmd: str):
        """Send a control command to the connected Unity device."""
        if self._out and self._device:
            try:
                self._out.sendto(cmd.encode(), (self._device.ip, self._port))
            except OSError as e:
                logger.warning(f"Unity send_command {cmd}: {e}")

    def broadcast_ping(self, label: str) -> int:
        if not self._out or not self._device:
            return -1
        try:
            self._out.sendto(label.encode(), (self._device.ip, self._port))
            self.log_message.emit(f"[Unity] Ping → {self._device.ip}: {label}")
        except OSError as e:
            logger.warning(f"Unity ping: {e}")
            return -1
        return self.calibrated_latency_ns

    def _single_rtt(self, label: str) -> Optional[int]:
        if not self._out or not self._device:
            return None
        event = threading.Event()
        send_ns = time.time_ns()
        with self._ack_lock:
            self._pending_acks[label] = (send_ns, event)
        try:
            self._out.sendto(label.encode(), (self._device.ip, self._port))
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

    # ── Data polling ─────────────────────────────────────────────────────────

    def start_data_stream(self, rate_hz: float = 3.0):
        """Start polling Unity for data at rate_hz. Stops when disconnected."""
        if self._streaming:
            return   # already running — prevent duplicate threads
        self._streaming = True
        interval = 1.0 / max(rate_hz, 1.0)
        threading.Thread(
            target=self._poll_loop, args=(interval,), daemon=True
        ).start()

    def stop_data_stream(self):
        self._streaming = False

    def set_stream_rate(self, rate_hz: float):
        """Change streaming rate dynamically. Takes effect on next poll cycle."""
        self._stream_interval = 1.0 / max(rate_hz, 0.1)

    def _poll_loop(self, interval: float):
        while self._running and self._device and self._streaming:
            try:
                self._out.sendto(b"REQUEST_DATA", (self._device.ip, self._port))
            except OSError:
                pass
            time.sleep(self._stream_interval)  # reads dynamically
        self._streaming = False

    def _update_latency(self):
        if not self._rtt_buffer:
            return
        samples = sorted(self._rtt_buffer)
        median_rtt = samples[len(samples) // 2]
        self.calibrated_latency_ns = median_rtt // 2
        self.calibration_changed.emit(True)

    def start_continuous_calibration(self):
        """Probe every 5s after 3s initial delay. Updates latency after each sample."""
        if self._continuous_calib_active:
            return
        self._continuous_calib_active = True
        self.log_message.emit("[Unity] Continuous calibration started (1 probe / 5s)")
        time.sleep(3.0)
        probe_n = 0
        while self._running and self._device:
            rtt = self._single_rtt(f"__calib_c{probe_n}__")
            probe_n += 1
            if rtt is not None:
                self._rtt_buffer.append(rtt)
                self._update_latency()
                self.log_message.emit(
                    f"[Unity] Probe: RTT={rtt/1e6:.1f}ms  "
                    f"one-way={self.calibrated_latency_ns/1e6:.1f}ms  "
                    f"(n={len(self._rtt_buffer)})"
                )
            time.sleep(5.0)
        self._continuous_calib_active = False

    def calibrate_for_recording(self):
        """10 probes at 1s intervals. Uses whole buffer for final value."""
        threading.Thread(target=self._record_calib, daemon=True).start()

    def _record_calib(self):
        self.log_message.emit("[Unity] Record-start calibration (10 probes × 1s)...")
        for i in range(10):
            if not self._device:
                break
            rtt = self._single_rtt(f"__calib_r{i}__")
            if rtt is not None:
                self._rtt_buffer.append(rtt)
            time.sleep(1.0)
        self._update_latency()
        self._session_latency_ns = self.calibrated_latency_ns
        self.log_message.emit(
            f"[Unity] Session latency locked: one-way={self._session_latency_ns/1e6:.1f}ms "
            f"(n={len(self._rtt_buffer)} samples)"
        )

    # ── Receive loop ──────────────────────────────────────────────────────────

    def _listen(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
                msg = data.decode("utf-8").strip()
                self._handle(msg, addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle(self, msg: str, src_ip: str):
        # Log every non-DATA message from the connected device so we can trace the chain
        if not msg.startswith("DATA,unity,") and not msg.startswith("HELLO"):
            self.log_message.emit(f"[Unity] ← {src_ip}: {msg[:60]}")

        if msg.startswith("HELLO,unity,"):
            name = msg.split(",", 2)[2] if msg.count(",") >= 2 else src_ip
            dev  = UnityDevice(ip=src_ip, name=name)
            with self._scan_lock:
                if not any(d.ip == src_ip for d in self._scan_results):
                    self._scan_results.append(dev)
                    self.log_message.emit(f"[Unity] Found: {dev.display_name}")
                    self.devices_found.emit(list(self._scan_results))
                    self.scan_progress.emit(
                        f"Found {len(self._scan_results)} device(s): {name} [{src_ip}]"
                    )
            return

        if msg.startswith("CONNECTED"):
            self._connect_event.set()
            return

        if self._device and src_ip != self._device.ip:
            return

        if msg.startswith("DATA,unity,"):
            self.data_received.emit(msg)
            return

        if msg == "RECORDING_STARTED":
            self.log_message.emit(f"[Unity] Recording started — triggering LSL recording")
            self.recording_started.emit()
            return

        if msg == "RECORDING_STOPPED":
            self.log_message.emit(f"[Unity] Recording stopped")
            self.recording_stopped.emit()
            return

        if msg == "PING":
            self.log_message.emit(f"[Unity] Ping trigger from {src_ip}")
            self.ping_requested.emit()
        elif msg.startswith("ACK:"):
            # Format: ACK:<ping_id> or ACK:<ping_id>:<unity_ns>
            rest    = msg[4:]
            parts_a = rest.split(":", 1)
            ping_id   = parts_a[0]
            unity_ns  = int(parts_a[1]) if len(parts_a) > 1 and parts_a[1].isdigit() else 0

            # Unblock calibration waiter
            with self._ack_lock:
                entry = self._pending_acks.get(ping_id)
            if entry:
                entry[1].set()

            # If this is a real ping ACK (not calibration), emit with Unity timestamp
            if ping_id.startswith("ping_") and unity_ns > 0:
                self.unity_ack_received.emit(ping_id, unity_ns)
