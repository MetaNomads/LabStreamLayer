"""
core/emotibit.py

Correct EmotiBit protocol based on EmotiBitPacket.cpp source:

TypeTags (4th CSV field):
  HE = Hello EmotiBit  (we broadcast to discover)
  HH = Hello Host      (device replies with CP, DP, DI payload)
  EC = EmotiBit Connect (heartbeat; payload = CP,<port>,DP,<port>)
  RB = Record Begin    (we send to START recording; device echoes back with filename)
  RE = Record End      (we send to STOP recording; device echoes back)
  UN = User Note       (we send; payload = wall_clock,note_text)
  EM = EmotiBit Mode   (device sends status; payload has RS=RB/RE, PS=MN etc)

PayloadLabels (key-value pairs inside packet payload):
  CP = Control Port    (TCP port our server listens on)
  DP = Data Port       (UDP port we receive data on)
  DI = Device ID
  RS = Recording Status (RB=recording, RE=ended)
  PS = Power Status

IMPORTANT: "RS" as a TypeTag means RESET - never send it as a command.
"""

import ipaddress
import platform
import re
import socket
import subprocess
import threading
from collections import deque
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from contracts import requires, ensures, Contract

logger = logging.getLogger(__name__)

EMOTIBIT_PORT      = 3131
HEARTBEAT_INTERVAL = 1.0
TCP_CONTROL_PORT   = 3132   # our TCP server; EmotiBit connects back here
UDP_DATA_PORT      = 3131   # we receive data here (same as main socket)


# ── Network utilities ───────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_broadcast_addresses() -> List[str]:
    broadcasts = {"255.255.255.255"}
    try:
        ip = get_local_ip()
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        broadcasts.add(str(net.broadcast_address))
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
                broadcasts.add(str(net.broadcast_address))
    except Exception:
        pass
    return list(broadcasts)


def arp_mac_lookup(ip: str) -> str:
    try:
        if platform.system() == "Windows":
            subprocess.run(f"ping -n 1 -w 500 {ip}",
                           shell=True, capture_output=True, timeout=2)
            out = subprocess.check_output(
                f"arp -a {ip}", shell=True, timeout=3
            ).decode(errors="ignore")
        else:
            subprocess.run(f"ping -c 1 -W 1 {ip}",
                           shell=True, capture_output=True, timeout=2)
            out = subprocess.check_output(
                f"arp -n {ip}", shell=True, timeout=3
            ).decode(errors="ignore")
        m = re.search(r"([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}", out)
        return m.group(0).upper().replace("-", ":") if m else ""
    except Exception:
        return ""


# ── Data model ──────────────────────────────────────────────────────────────────

class EmotiBitStatus(Enum):
    SCANNING  = auto()
    IDLE      = auto()
    CONNECTED = auto()
    RECORDING = auto()


@dataclass
class EmotiBitDevice:
    ip:        str
    mac:       str = ""
    device_id: str = ""
    data_port: int = EMOTIBIT_PORT

    @property
    def display_name(self) -> str:
        id_part  = self.device_id if self.device_id else self.ip
        mac_part = f"  MAC {self.mac}" if self.mac else ""
        return f"EmotiBit  {id_part}{mac_part}  [{self.ip}]"


# ── Handler ─────────────────────────────────────────────────────────────────────

class EmotiBitHandler(QObject):

    status_changed      = pyqtSignal(EmotiBitStatus)
    devices_updated     = pyqtSignal(list)
    log_message         = pyqtSignal(str)
    ppg_red_sample      = pyqtSignal(float)   # PR typetag — PPG red channel
    hr_sample           = pyqtSignal(float)   # HR typetag — heart rate bpm
    calibration_changed = pyqtSignal(bool)    # True = calibrated, False = reset
    battery_changed     = pyqtSignal(int)     # 0-100 percent
    sensor_event        = pyqtSignal(str)     # "sensor_lost" | "sensor_recovered" | "given_up"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._devices:   Dict[str, EmotiBitDevice] = {}
        self._connected: Optional[EmotiBitDevice]  = None
        self._conn_lock  = threading.Lock()   # guards _connected r/w across threads
        self._status     = EmotiBitStatus.IDLE
        self._pkt_num    = 0
        self._running    = False
        self._scanning   = False
        self._udp:        Optional[socket.socket] = None
        self._tcp_server: Optional[socket.socket] = None
        self._tcp_client: Optional[socket.socket] = None
        self._last_rtt_ns:        int = 10_000_000
        self.calibrated_latency_ns: int = -1
        self.has_streaming_data:    bool = False
        # HH receipt signalling for calibration
        self._hh_event   = threading.Event()
        self._hh_recv_ns: int = 0
        # RB echo signalling for SD card check
        self._rb_event   = threading.Event()
        self.sd_card_ok: Optional[bool] = None  # None=unknown, True=ok, False=failed
        self._is_writing:         bool = False
        self._recording_start_ns: int  = 0
        self._last_writing_ns:    int  = 0    # time.time_ns() when is_writing last confirmed True
        # NOTE: these three were previously parked after a `return` in the
        # `seconds_since_recording_start` property and never executed — leaving
        # _rtt_buffer / _continuous_calib_active / _session_latency_ns undefined
        # and silently killing the calibration thread. They live in __init__ now.
        self._rtt_buffer              = deque(maxlen=20)   # rolling RTT samples
        self._continuous_calib_active = False
        self._session_latency_ns: int = -1                  # locked at record-start
        self._last_sample_ns:     int = 0                   # for silent-stream watchdog
        self._reconnect_attempts: int = 0
        self._reconnect_max:      int = 10
        self._given_up:           bool = False              # surfaced as DEGRADED banner
        # Shutdown signalling — replaces blocking time.sleep in retry loops so
        # the app doesn't hang for up to 60 s on quit during a long backoff.
        self._shutdown_event      = threading.Event()
        # Liveness watchdog — clears _connected after this many seconds of
        # silence, which is the ONLY path that triggers the auto-reconnect
        # mechanism (without this, the heartbeat blindly sends UDP to a ghost
        # device forever and bounded retry never actually retries).
        self._liveness_silence_s: float = 10.0

    @property
    def is_writing(self) -> bool:
        return self._is_writing

    @property
    def seconds_since_last_writing_confirmation(self) -> float:
        """Seconds since EM RS=RB was last received. 0 if never confirmed."""
        if self._last_writing_ns == 0: return 0.0
        return (time.time_ns() - self._last_writing_ns) / 1e9

    @property
    def seconds_since_recording_start(self) -> float:
        if self._recording_start_ns == 0: return 0.0
        return (time.time_ns() - self._recording_start_ns) / 1e9

    @property
    def seconds_since_last_sample(self) -> float:
        """Seconds since the most recent data sample (PR/HR/etc.). 0 if never received."""
        if self._last_sample_ns == 0: return 0.0
        return (time.time_ns() - self._last_sample_ns) / 1e9

    @property
    def effective_latency_ns(self) -> int:
        """Public latency: locked session value if available, else rolling probe value."""
        return self._session_latency_ns if self._session_latency_ns >= 0 else self.calibrated_latency_ns

    @property
    def given_up(self) -> bool:
        return self._given_up

    @ensures(lambda result, *_args, **_kw: isinstance(result, dict),
             "public_summary must return a dict")
    def public_summary(self) -> dict:
        """Honest snapshot of handler state for session_meta.json — no
        private-attribute reach-arounds from callers."""
        with self._conn_lock:
            dev = self._connected
        return {
            "ip":        dev.ip if dev else None,
            "device_id": dev.device_id if dev else None,
            "mac":       dev.mac if dev else None,
            "session_latency_ns": self.effective_latency_ns,
            "given_up":  self._given_up,
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    def start(self):
        self._running = True

        # ── UDP socket bound to port 3131 ────────────────────────────────────
        # Must use the SAME socket for send and receive so that:
        #   source port of HE = 3131
        #   EmotiBit replies HH to port 3131 = our socket sees it
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.settimeout(1.0)
        try:
            self._udp.bind(("", EMOTIBIT_PORT))
            self._try_emit(self.log_message, 
                f"[EmotiBit] UDP bound to port {EMOTIBIT_PORT}"
            )
        except OSError as e:
            self._try_emit(self.log_message, 
                f"[EmotiBit] Cannot bind UDP port {EMOTIBIT_PORT}: {e}\n"
                f"           Close EmotiBit Oscilloscope first, then restart."
            )
            return

        # ── TCP server on port 3132 ──────────────────────────────────────────
        # EmotiBit reads CP value from EC packet and connects back here via TCP
        self._tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_server.settimeout(1.0)
        try:
            self._tcp_server.bind(("", TCP_CONTROL_PORT))
            self._tcp_server.listen(2)
            self._try_emit(self.log_message, 
                f"[EmotiBit] TCP control server on port {TCP_CONTROL_PORT}"
            )
        except OSError as e:
            self._try_emit(self.log_message, 
                f"[EmotiBit] TCP server warning: {e} (commands will use UDP)"
            )
            self._tcp_server = None

        threading.Thread(target=self._udp_loop,        daemon=True).start()
        threading.Thread(target=self._tcp_accept_loop, daemon=True).start()

    def stop(self):
        self._running = False
        self._shutdown_event.set()   # wake any sleeping retry/liveness loops
        for s in (self._udp, self._tcp_server, self._tcp_client):
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    # ── Discovery ────────────────────────────────────────────────────────────────

    def scan(self, duration: float = 5.0):
        self._devices.clear()
        self._scanning = True
        self._set_status(EmotiBitStatus.SCANNING)
        broadcasts = get_broadcast_addresses()
        self._try_emit(self.log_message, 
            f"[EmotiBit] Scanning: {', '.join(broadcasts)}"
        )

        def _loop():
            end = time.time() + duration
            while self._scanning and time.time() < end:
                for bc in broadcasts:
                    self._udp_send(self._pkt("HE"), bc)
                time.sleep(0.5)
            self._scanning = False
            if self._status == EmotiBitStatus.SCANNING:
                self._set_status(EmotiBitStatus.IDLE)
            self._try_emit(self.log_message, 
                f"[EmotiBit] Scan done - {len(self._devices)} device(s) found"
            )

        threading.Thread(target=_loop, daemon=True).start()

    def add_manual_device(self, ip: str) -> Optional[EmotiBitDevice]:
        """Public alias — preferred name. `add_manual` is kept as a back-compat shim."""
        return self.add_manual(ip)

    def add_manual(self, ip: str) -> Optional[EmotiBitDevice]:
        try:
            socket.inet_aton(ip)
        except OSError:
            return None
        if ip not in self._devices:
            dev = EmotiBitDevice(ip=ip, device_id="(manual)")
            self._devices[ip] = dev
            self._try_emit(self.devices_updated, list(self._devices.values()))
            self._try_emit(self.log_message, f"[EmotiBit] Manual device: {ip}")
            threading.Thread(
                target=lambda: self._do_arp(dev), daemon=True
            ).start()
        return self._devices[ip]

    # ── Connection ───────────────────────────────────────────────────────────────

    @requires(lambda self, device: device is not None and bool(getattr(device, "ip", "")),
              "device must be non-None and have a non-empty ip")
    def connect(self, device: EmotiBitDevice):
        was_given_up = self._given_up
        with self._conn_lock:
            self._connected = device
        self._given_up = False
        self._last_sample_ns = 0   # reset so liveness watchdog gives the link a chance to start
        self._set_status(EmotiBitStatus.CONNECTED)
        self._try_emit(self.log_message, f"[EmotiBit] Connecting to {device.display_name}")
        # Heartbeat exits whenever _connected becomes None, so a new one is needed
        # per connect. Calibration is gated so we don't spawn duplicates.
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        if not self._continuous_calib_active:
            threading.Thread(target=self.start_continuous_calibration, daemon=True).start()
        # Liveness watchdog — only one ever runs (gated by attribute).
        if not getattr(self, "_liveness_started", False):
            self._liveness_started = True
            threading.Thread(target=self._liveness_loop, daemon=True).start()
        if was_given_up:
            # Coming back from DEGRADED state via a successful manual reconnect.
            self._try_emit(self.sensor_event, "sensor_recovered")

    def _liveness_loop(self):
        """
        The ONLY path that detects an EmotiBit going offline. The heartbeat
        blindly fires UDP and never observes responses, so without this loop the
        handler stays in 'connected' forever after the device drops, and the
        bounded auto-reconnect (which fires on _connected becoming None) is
        never reached.
        Checks every 2s; if there has been a sample at all (i.e. _last_sample_ns
        != 0) and silence exceeds _liveness_silence_s, clears _connected, which
        cascades into the heartbeat loop exiting and _auto_reconnect kicking in.
        """
        while self._running:
            if self._shutdown_event.wait(timeout=2.0):
                return
            if not self._connected:
                continue
            # Only declare "lost" if we have ever received a sample in this
            # session — otherwise we're in the startup phase before data flows.
            if self._last_sample_ns == 0:
                continue
            silence = (time.time_ns() - self._last_sample_ns) / 1e9
            if silence > self._liveness_silence_s:
                self._try_emit(self.log_message,
                    f"[EmotiBit] Liveness watchdog: no sample for {silence:.1f}s "
                    f"(threshold {self._liveness_silence_s:.0f}s) — clearing _connected to trigger reconnect"
                )
                self._try_emit(self.sensor_event, "sensor_lost")
                with self._conn_lock:
                    self._connected = None
                # The heartbeat loop will now exit on its next iteration and
                # spawn _auto_reconnect via the existing path.

    def _send_tl_now(self):
        """Send TL immediately (called on connect and before recording)."""
        if self._connected:
            wall_clock = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            self._udp_send(self._pkt("TL", wall_clock), self._connected.ip)

    def disconnect(self):
        with self._conn_lock:
            self._connected = None
        self._session_latency_ns = -1
        self.has_streaming_data = False
        self._is_writing = False
        self._recording_start_ns = 0
        self._last_writing_ns = 0
        if self._tcp_client:
            try:
                self._tcp_client.close()
            except Exception:
                pass
            self._tcp_client = None
        self.calibrated_latency_ns = -1
        self._try_emit(self.calibration_changed, False)
        self._set_status(EmotiBitStatus.IDLE)
        self._try_emit(self.log_message, "[EmotiBit] Disconnected")

    # ── Recording & markers ──────────────────────────────────────────────────────

    def check_sd_card(self, retries: int = 5, timeout: float = 2.0) -> bool:
        """
        Send RB and wait for device echo. Retries up to `retries` times.
        Returns True if SD card confirmed, False after all attempts fail.
        Each attempt sends a fresh RB packet and waits `timeout` seconds.
        Total max wait: retries × timeout = 5 × 2s = 10s.
        """
        if not self._connected:
            return False
        for attempt in range(1, retries + 1):
            filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            self._send_ctrl(self._pkt("TL", datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")))
            time.sleep(0.05)
            self._rb_event.clear()
            self.sd_card_ok = None
            self._send_ctrl(self._pkt("RB", filename))
            self._try_emit(self.log_message, 
                f"[EmotiBit] SD card check attempt {attempt}/{retries}..."
            )
            got = self._rb_event.wait(timeout=timeout)
            if got:
                self.sd_card_ok = True
                return True
            if not self._connected:
                return False
        self.sd_card_ok = False
        self._try_emit(self.log_message, 
            "[EmotiBit] ⚠ No SD card response after all attempts"
        )
        return False

    @requires(lambda self: self._running,
              "EmotiBit handler must be running (call start() first)")
    def start_recording(self):
        """
        Start SD card recording.
        Sends RB (Record Begin) up to 3 times to improve UDP delivery reliability.
        Status transitions to RECORDING when device echoes RB back.
        """
        if not self._connected:
            self._try_emit(self.log_message, "[EmotiBit] Not connected")
            return

        filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")

        # Sync device clock first
        self._send_ctrl(self._pkt("TL", datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")))
        time.sleep(0.05)

        # Send RB 3 times — device deduplicates by packet number but
        # improved reliability over lossy WiFi
        rb_pkt = self._pkt("RB", filename)
        self._rb_event.clear()
        self.sd_card_ok = None
        for _ in range(3):
            self._send_ctrl(rb_pkt)
            time.sleep(0.05)
        self._recording_start_ns = time.time_ns()
        self._try_emit(self.log_message, f"[EmotiBit] RB sent — waiting for device echo: {filename}.csv")

    def stop_recording(self):
        """
        Stop SD card recording.
        Sends RE (Record End) up to 3 times to ensure the device receives it
        and properly closes the file.
        """
        if not self._connected:
            return
        re_pkt = self._pkt("RE")
        for _ in range(3):
            self._send_ctrl(re_pkt)
            time.sleep(0.05)
        self._set_status(EmotiBitStatus.CONNECTED)
        self._try_emit(self.log_message, "[EmotiBit] RE (Record End) sent")

    @requires(lambda self, label: isinstance(label, str) and len(label) > 0,
              "marker label must be a non-empty string")
    @ensures(lambda result, *_args, **_kw: (isinstance(result, tuple) and len(result) == 2
                                 and isinstance(result[0], int)),
             "send_marker must return (send_ns:int, latency_ns)")
    def send_marker(self, label: str) -> tuple:
        """
        Send UN (User Note) marker. Returns (send_ns, calibrated_one_way_latency_ns).
        """
        if not self._connected:
            self._try_emit(self.log_message, "[EmotiBit] Cannot send marker - not connected")
            lat = self._session_latency_ns if self._session_latency_ns >= 0 else self.calibrated_latency_ns
            return time.time_ns(), lat

        wall_clock = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        pkt = self._pkt("UN", f"{wall_clock},{label}", data_len=2)
        send_ns = time.time_ns()
        self._send_ctrl(pkt)
        self._send_ctrl(pkt)

        self._try_emit(self.log_message, f"[EmotiBit] marker sent: {label}")
        lat = self._session_latency_ns if self._session_latency_ns >= 0 else self.calibrated_latency_ns
        return send_ns, lat

    def _single_rtt(self) -> Optional[int]:
        """
        Single HE→HH round-trip measurement.

        HE MUST be sent to broadcast — the EmotiBit firmware only replies
        to HE packets arriving on the broadcast address, not unicast.

        We wait on _hh_event which is set by _parse_line when HH arrives
        through the normal _udp_loop, avoiding the recv race condition.

        Returns RTT in nanoseconds, or None on timeout/failure.
        """
        if not self._connected or not self._udp:
            return None
        # Use subnet broadcast (EmotiBit only replies to broadcast HE)
        broadcasts = get_broadcast_addresses()
        bc = broadcasts[0] if broadcasts else "255.255.255.255"
        self._hh_event.clear()
        t1 = time.time_ns()
        try:
            self._udp.sendto(self._pkt("HE"), (bc, EMOTIBIT_PORT))
        except OSError:
            return None
        # Wait for HH via _udp_loop → _parse_line → _hh_event.set()
        got = self._hh_event.wait(timeout=2.0)
        if not got:
            return None
        rtt = self._hh_recv_ns - t1
        return max(1_000_000, min(rtt, 500_000_000))

    def _update_latency(self):
        """Recalculate calibrated_latency_ns from current rolling buffer."""
        if not self._rtt_buffer:
            return
        samples = sorted(self._rtt_buffer)
        median_rtt = samples[len(samples) // 2]
        self.calibrated_latency_ns = median_rtt // 2
        self._try_emit(self.calibration_changed, True)

    def start_continuous_calibration(self):
        """
        Probe HE→HH every 5s. Starts 3s after connect.
        Updates calibrated_latency_ns after every measurement.
        """
        if self._continuous_calib_active:
            return
        self._continuous_calib_active = True
        self._try_emit(self.log_message, "[EmotiBit] Continuous calibration started (1 probe / 5s)")
        time.sleep(3.0)
        while self._running and self._connected:
            rtt = self._single_rtt()
            if rtt is not None:
                self._rtt_buffer.append(rtt)
                self._update_latency()
                self._try_emit(self.log_message, 
                    f"[EmotiBit] Probe: RTT={rtt/1e6:.1f}ms  "
                    f"one-way={self.calibrated_latency_ns/1e6:.1f}ms  "
                    f"(n={len(self._rtt_buffer)})"
                )
            time.sleep(5.0)
        self._continuous_calib_active = False

    def calibrate_for_recording(self):
        """10 probes at 1s intervals starting now. Uses whole buffer for final value."""
        threading.Thread(target=self._record_calib, daemon=True).start()

    def _record_calib(self):
        self._try_emit(self.log_message, "[EmotiBit] Record-start calibration (10 probes × 1s)...")
        for _ in range(10):
            if not self._connected:
                break
            rtt = self._single_rtt()
            if rtt is not None:
                self._rtt_buffer.append(rtt)
            time.sleep(1.0)
        self._update_latency()
        self._session_latency_ns = self.calibrated_latency_ns
        self._try_emit(self.log_message, 
            f"[EmotiBit] Session latency locked: one-way={self._session_latency_ns/1e6:.1f}ms "
            f"(n={len(self._rtt_buffer)} samples)"
        )

    # ── Properties ───────────────────────────────────────────────────────────────

    @property
    def status(self):
        return self._status

    @property
    def device_ip(self):
        return self._connected.ip if self._connected else None

    # ── Internal packet building ──────────────────────────────────────────────────

    def _pkt(self, tag: str, data: str = "", data_len: int = -1) -> bytes:
        """
        Build an EmotiBit CSV packet.
        data_len defaults to 1 if data provided, 0 if not.
        For UN packets, data_len must be 2 (two comma-separated fields).
        """
        ts  = int(time.time() * 1000) & 0xFFFFFFFF
        num = self._pkt_num
        self._pkt_num += 1
        if data_len < 0:
            data_len = 1 if data else 0
        parts = [str(ts), str(num), str(data_len), tag, "1", "100"]
        if data:
            parts.append(data)
        return (",".join(parts) + "\n").encode("utf-8")

    # ── Transport ─────────────────────────────────────────────────────────────────

    def _udp_send(self, pkt: bytes, ip: str):
        if self._udp:
            try:
                self._udp.sendto(pkt, (ip, EMOTIBIT_PORT))
            except OSError as e:
                logger.debug(f"UDP send {ip}: {e}")

    def _send_ctrl(self, pkt: bytes):
        """Send a control command. Prefer TCP back-channel; fall back to UDP."""
        if not self._connected:
            return
        if self._tcp_client:
            try:
                self._tcp_client.sendall(pkt)
                return
            except OSError as e:
                logger.warning(f"TCP send failed: {e}, falling back to UDP")
                self._tcp_client = None
        # UDP fallback
        self._udp_send(pkt, self._connected.ip)

    # ── Heartbeat ─────────────────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        """
        Send EC heartbeat every 1 s with CP and DP payload labels.
        Sends TL (Timestamp Local) immediately on first beat, then every 5 s.
        If the device drops, attempts to auto-reconnect every 5s.
        """
        tl_counter = 0
        last_device = self._connected
        while self._running and self._connected:
            # EC heartbeat
            payload = f"CP,{TCP_CONTROL_PORT},DP,{UDP_DATA_PORT}"
            self._udp_send(self._pkt("EC", payload, data_len=4), self._connected.ip)

            # TL timesync: immediately on first beat, then every 5 s
            if tl_counter == 0 or tl_counter >= 5:
                tl_counter = 0
                wall_clock = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
                self._udp_send(self._pkt("TL", wall_clock), self._connected.ip)

            tl_counter += 1
            time.sleep(HEARTBEAT_INTERVAL)

        # Device dropped — attempt auto-reconnect
        if self._running and not self._connected and last_device:
            self._try_emit(self.log_message, 
                f"[EmotiBit] Connection lost — auto-reconnecting to {last_device.ip}..."
            )
            threading.Thread(
                target=self._auto_reconnect, args=(last_device,), daemon=True
            ).start()

    # Backoff schedule for auto-reconnect (seconds). After this many attempts
    # we surface a persistent DEGRADED banner and stop spinning.
    _RECONNECT_BACKOFF_S = (5, 10, 20, 40, 60, 60, 60, 60, 60, 60)

    def _auto_reconnect(self, device):
        """
        Bounded retry with exponential backoff using shutdown-aware waits.
        Emits sensor_event("sensor_lost") on entry and sensor_event("given_up")
        on exhaustion so the host can write rows into the syncLog (the operator
        log alone is not persistent).
        """
        self._try_emit(self.log_message, "[EmotiBit] sensor_lost — bounded reconnect started")
        self._try_emit(self.sensor_event, "sensor_lost")
        self._reconnect_attempts = 0
        for delay in self._RECONNECT_BACKOFF_S:
            # Shutdown-aware wait — quitting during a 60s backoff exits in <100ms.
            if self._shutdown_event.wait(timeout=delay):
                return
            if not self._running:
                return
            if self._connected:
                # Reconnected through some other path (e.g. manual user click).
                self._try_emit(self.log_message, "[EmotiBit] sensor_recovered (external)")
                self._try_emit(self.sensor_event, "sensor_recovered")
                self._given_up = False
                return
            self._reconnect_attempts += 1
            self._try_emit(self.log_message,
                f"[EmotiBit] Reconnect attempt {self._reconnect_attempts}/"
                f"{len(self._RECONNECT_BACKOFF_S)} → {device.ip}"
            )
            self.connect(device)
            # connect() sets _connected immediately (faith-based for now). The
            # liveness loop will catch the device if it's still actually offline
            # and clear _connected again, dropping us back into this loop.
            # Pause briefly between connect() and next-iteration's connected check
            # to give the liveness loop a chance to invalidate a faux-success.
            if self._shutdown_event.wait(timeout=2.0):
                return
        # Backoff exhausted
        if not self._connected and self._running:
            self._given_up = True
            self._try_emit(self.log_message,
                "[EmotiBit] ⚠ DEGRADED — gave up reconnecting after "
                f"{len(self._RECONNECT_BACKOFF_S)} attempts. Recording continues without EmotiBit."
            )
            self._try_emit(self.sensor_event, "given_up")

    # ── Receive loops ─────────────────────────────────────────────────────────────

    def _udp_loop(self):
        while self._running:
            try:
                data, addr = self._udp.recvfrom(65536)
                self._handle_udp(data, addr[0])
            except socket.timeout:
                continue
            except OSError:
                break

    def _tcp_accept_loop(self):
        if not self._tcp_server:
            return
        while self._running:
            try:
                conn, addr = self._tcp_server.accept()
                self._tcp_client = conn
                self._try_emit(self.log_message, 
                    f"[EmotiBit] TCP back-channel from {addr[0]}"
                )
                threading.Thread(
                    target=self._tcp_read_loop, args=(conn,), daemon=True
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _tcp_read_loop(self, conn: socket.socket):
        buf = b""
        while self._running:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        self._parse_line(line.decode("utf-8", errors="ignore"), "")
            except OSError:
                break
        if self._tcp_client is conn:
            self._tcp_client = None
        self._try_emit(self.log_message, "[EmotiBit] TCP back-channel closed")

    # ── Packet parsing ────────────────────────────────────────────────────────────

    def _handle_udp(self, data: bytes, ip: str):
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return
        for line in text.splitlines():
            self._parse_line(line, ip)

    def _parse_line(self, text: str, ip: str):
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 4:
            return
        tag = parts[3]

        if tag == "HH":
            # Only signal the calibration waiter when the HH is from the device
            # we are currently connected to. Otherwise a second EmotiBit on the
            # same lab subnet (very common) contaminates the RTT measurement.
            with self._conn_lock:
                connected_ip = self._connected.ip if self._connected else None
            if connected_ip is None or ip == connected_ip:
                self._hh_recv_ns = time.time_ns()
                self._hh_event.set()

        if tag == "HH" and ip:
            # Parse CP, DP, DI from payload labels
            device_id = ""
            data_port = EMOTIBIT_PORT
            # Scan payload for keyed values: DI,<id>, DP,<port>
            i = 6  # start after header (6 fixed fields)
            while i < len(parts) - 1:
                key = parts[i]
                val = parts[i + 1]
                if key == "DI":
                    device_id = val
                elif key == "DP":
                    try:
                        data_port = int(val)
                    except ValueError:
                        pass
                i += 2

            # Also try positional parse for older firmware
            if not device_id and len(parts) > 7:
                device_id = parts[7]
            if data_port == EMOTIBIT_PORT and len(parts) > 6:
                try:
                    data_port = int(parts[6])
                except ValueError:
                    pass

            if ip not in self._devices:
                dev = EmotiBitDevice(
                    ip=ip, data_port=data_port, device_id=device_id
                )
                self._devices[ip] = dev
                self._try_emit(self.devices_updated, list(self._devices.values()))
                self._try_emit(self.log_message, 
                    f"[EmotiBit] Found: {dev.display_name}"
                )
                threading.Thread(
                    target=lambda d=dev: self._do_arp(d), daemon=True
                ).start()

        elif tag == "RB":
            # Device confirmed recording started — SD card is writing
            filename = parts[6] if len(parts) > 6 else ""
            self.sd_card_ok = True
            self._rb_event.set()
            self._set_status(EmotiBitStatus.RECORDING)
            self._try_emit(self.log_message, 
                f"[EmotiBit] Recording started: {filename}"
            )

        elif tag == "RE":
            if self._status == EmotiBitStatus.RECORDING:
                self._set_status(EmotiBitStatus.CONNECTED)
                self._try_emit(self.log_message, "[EmotiBit] Recording stopped")

        elif tag == "EM":
            # Device status update — parse RS (Recording Status)
            payload = ','.join(parts[6:])
            self._try_emit(self.log_message, f"[EmotiBit] Status: {payload}")
            # RS=RB means device is actively writing to SD card
            # RS=RE means recording stopped
            parts_map = payload.split(',')
            for i in range(len(parts_map) - 1):
                if parts_map[i] == "RS":
                    if parts_map[i+1] == "RB":
                        self._is_writing = True
                        self._last_writing_ns = time.time_ns()
                        # An EM packet is also evidence the device is alive on
                        # the wire — keep the liveness loop from firing while
                        # data is paused but heartbeat is healthy.
                        self._last_sample_ns = time.time_ns()
                    elif parts_map[i+1] == "RE":
                        self._is_writing = False
                    break

        elif tag == "B%":
            # Battery percent — direct 0-100 value
            try:
                if len(parts) > 6 and parts[6]:
                    pct = round(float(parts[6]))
                    self._try_emit(self.battery_changed, max(0, min(100, pct)))
            except (ValueError, IndexError):
                pass

        elif tag == "BV":
            # Battery voltage fallback (3.5V=~0%, 4.2V=~100% for LiPo)
            try:
                if len(parts) > 6 and parts[6]:
                    v = float(parts[6])
                    pct = round(max(0.0, min(1.0, (v - 3.5) / 0.7)) * 100)
                    self._try_emit(self.battery_changed, pct)
            except (ValueError, IndexError):
                pass

        elif tag == "PR":
            # PPG Red channel — emit each datapoint
            try:
                for v in parts[6:]:
                    if v:
                        self.has_streaming_data = True
                        self._last_sample_ns = time.time_ns()
                        self._try_emit(self.ppg_red_sample, float(v))
            except (ValueError, IndexError):
                pass

        elif tag == "HR":
            # Heart rate — single value
            try:
                if len(parts) > 6 and parts[6]:
                    self._last_sample_ns = time.time_ns()
                    self._try_emit(self.hr_sample, float(parts[6]))
            except (ValueError, IndexError):
                pass

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def _do_arp(self, dev: EmotiBitDevice):
        mac = arp_mac_lookup(dev.ip)
        if mac:
            dev.mac = mac
            self._try_emit(self.devices_updated, list(self._devices.values()))
            self._try_emit(self.log_message, f"[EmotiBit] MAC {dev.ip} -> {mac}")

    def _try_emit(self, signal, *args):
        """Safely emit a signal from any thread. Guards against deleted QObject."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _set_status(self, s: EmotiBitStatus):
        if s != self._status:
            self._status = s
            self._try_emit(self.status_changed, s)
