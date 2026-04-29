"""
polar_mac.py — Polar H10 handler for macOS

Uses bleak (CoreBluetooth) directly. Confirmed working: 131 Hz ECG, HR.
Same public API as polar.py — drop-in replacement on Mac.
"""

import asyncio
import csv
import struct
import threading
from collections import deque
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from contracts import requires, ensures, Contract

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    raise ImportError("bleak not installed. Run: pip install bleak")

# ── UUIDs ─────────────────────────────────────────────────────────────────────

PMD_CONTROL = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA    = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
HR_CHAR     = "00002a37-0000-1000-8000-00805f9b34fb"

BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR    = "00002a19-0000-1000-8000-00805f9b34fb"

ECG_SETTINGS = bytes([0x01, 0x00])
ECG_START    = bytes([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_STOP     = bytes([0x03, 0x00])


# ── Data model ────────────────────────────────────────────────────────────────

class PolarStatus(Enum):
    IDLE      = auto()
    SCANNING  = auto()
    CONNECTED = auto()
    RECORDING = auto()


@dataclass
class PolarDevice:
    name:          str
    address:       str       # CoreBluetooth UUID on Mac
    serial_number: str = ""  # e.g. "EA835125"

    @property
    def display_name(self) -> str:
        sn = f"  SN:{self.serial_number}" if self.serial_number else ""
        return f"{self.name}{sn}"


# ── Handler ───────────────────────────────────────────────────────────────────

class PolarHandler(QObject):

    status_changed      = pyqtSignal(PolarStatus)
    devices_found       = pyqtSignal(list)
    log_message         = pyqtSignal(str)
    ecg_sample          = pyqtSignal(float)
    hr_sample           = pyqtSignal(int)
    rr_sample           = pyqtSignal(float)
    calibration_changed = pyqtSignal(bool)
    battery_changed     = pyqtSignal(int)     # 0-100 percent
    sensor_event        = pyqtSignal(str)     # "sensor_lost" | "sensor_recovered"

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self._output_dir = output_dir
        self._status     = PolarStatus.IDLE
        self._loop:       Optional[asyncio.AbstractEventLoop] = None
        self._thread:     Optional[threading.Thread]          = None
        self._cmd_queue:  Optional[asyncio.Queue]             = None
        self._csv_file              = None
        self._writer                = None
        self._last_ble_latency_ns:  int = -1
        self.calibrated_latency_ns: int = -1
        self.has_streaming_data:    bool = False
        self._calib_event = threading.Event()
        self._rtt_buffer        = deque(maxlen=20)  # rolling RTT samples
        self._session_latency_ns: int = -1           # locked at record-start calibration
        self._calib_result: int = -1
        self._last_sample_ns: int = 0                # for silent-stream watchdog
        self._given_up: bool = False
        # Handle for the asyncio call_later that schedules a reconnect after
        # bleak's disconnected_callback fires. Held so the user-initiated
        # `disconnect` action can cancel it (otherwise a 5s race window lets a
        # canceled reconnect resurrect the strap the user just dropped).
        self._pending_reconnect = None

    @property
    def seconds_since_last_sample(self) -> float:
        if self._last_sample_ns == 0: return 0.0
        return (time.time_ns() - self._last_sample_ns) / 1e9

    @property
    def effective_latency_ns(self) -> int:
        return self._session_latency_ns if self._session_latency_ns >= 0 else self.calibrated_latency_ns

    @property
    def given_up(self) -> bool:
        return self._given_up

    @ensures(lambda result, *_args, **_kw: isinstance(result, dict),
             "public_summary must return a dict")
    def public_summary(self) -> dict:
        """Honest snapshot for session_meta.json — replaces the
        `self._polar._connected.display_name` reach-around."""
        dev = getattr(self, "_connected", None)
        return {
            "device":  getattr(dev, "display_name", None) if dev else None,
            "address": getattr(dev, "address", None) if dev else None,
            "session_latency_ns":   self.effective_latency_ns,
            "calibration_method":   "battery_char_read",  # see PASS2 audit finding
            "given_up":             self._given_up,
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._send_cmd(("quit",))
        self._end_recording()

    def scan(self, duration: float = 8.0):
        """Scan for nearby Polar H10 devices and emit devices_found."""
        self._set_status(PolarStatus.SCANNING)
        self._send_cmd(("scan", duration))

    def connect_device(self, device: PolarDevice):
        self._send_cmd(("connect", device))

    def disconnect(self):
        self._send_cmd(("disconnect",))

    @requires(lambda self, session_ts, session_dir=None:
              isinstance(session_ts, str) and len(session_ts) > 0,
              "session_ts must be a non-empty string")
    def start_recording(self, session_ts: str, session_dir: "Path | None" = None):
        folder = session_dir if session_dir else self._output_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"polar_{session_ts}.csv"
        self._csv_file = open(path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._csv_file)
        # Sample frequency metadata
        self._csv_file.write("# device,Polar H10\n")
        self._csv_file.write("# ecg_sample_rate_hz,130\n")
        self._csv_file.write("# hr_sample_rate_hz,1\n")
        self._csv_file.write("# rr_sample_rate_hz,irregular (one per heartbeat ~0.7-2 Hz)\n")
        self._csv_file.write("# ecg_unit,microvolts (uV)\n")
        self._csv_file.write("# rr_unit,milliseconds\n")
        self._writer.writerow(["utc_epoch_ns", "ecg_uv", "hr_bpm", "rr_ms", "marker"])
        self._set_status(PolarStatus.RECORDING)
        self._try_emit(self.log_message, f"[Polar] Recording → {path.name}")
        self._try_emit(self.log_message, "[Polar] Sample rates: ECG=130Hz  HR=1Hz  RR=irregular")
        self._send_cmd(("start_rec",))

    def stop_recording(self):
        self._send_cmd(("stop_rec",))
        self._end_recording()

    @requires(lambda self, label: isinstance(label, str) and len(label) > 0,
              "marker label must be a non-empty string")
    @ensures(lambda result, *_args, **_kw: isinstance(result, tuple) and len(result) == 2,
             "send_marker must return a 2-tuple")
    def send_marker(self, label: str) -> tuple:
        """Send marker. Returns (send_ns, calibrated_one_way_latency_ns)."""
        send_ns = time.time_ns()
        if self._writer:
            self._writer.writerow([send_ns, "", "", "", label])
        self._send_cmd(("marker", label))
        lat = self._session_latency_ns if self._session_latency_ns >= 0 else self.calibrated_latency_ns
        return send_ns, lat

    def calibrate_for_recording(self):
        """10 BLE probes at 1s intervals. Uses whole buffer for final value."""
        self._send_cmd(("calibrate_for_recording",))

    def calibrate(self, n: int = 5, delay: float = 10.0):
        """
        Run BLE calibration burst in background.
        delay: seconds to wait before probing (10s on connect, 5s on re-calibrate).
        """
        self._calib_event.clear()
        self._send_cmd(("calibrate", n, delay))

    async def _continuous_probe(self, client):
        """Probe BLE latency every 5s. Started as asyncio task after connect."""
        await asyncio.sleep(3.0)
        self._try_emit(self.log_message, "[Polar] Continuous calibration started (1 probe / 5s)")
        while client.is_connected:
            try:
                t1 = time.time_ns()
                await client.read_gatt_char(BATTERY_CHAR)
                rtt = time.time_ns() - t1
                self._rtt_buffer.append(rtt)
                self._update_latency()
                self._try_emit(self.log_message, 
                    f"[Polar] Probe: RTT={rtt/1e6:.1f}ms  "
                    f"one-way={self.calibrated_latency_ns/1e6:.1f}ms  "
                    f"(n={len(self._rtt_buffer)})"
                )
            except Exception:
                pass
            await asyncio.sleep(5.0)

    def _update_latency(self):
        if not self._rtt_buffer:
            return
        samples = sorted(self._rtt_buffer)
        median_rtt = samples[len(samples) // 2]
        self._last_ble_latency_ns  = median_rtt // 2
        self.calibrated_latency_ns = median_rtt // 2
        self._try_emit(self.calibration_changed, True)

    @property
    def status(self) -> PolarStatus:
        return self._status

    # ── Internal: Qt → asyncio ────────────────────────────────────────────────

    def _send_cmd(self, cmd: tuple):
        if self._loop and self._cmd_queue:
            self._loop.call_soon_threadsafe(self._cmd_queue.put_nowait, cmd)

    def _try_emit(self, signal, *args):
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _set_status(self, s: PolarStatus):
        if s != self._status:
            self._status = s
            self._try_emit(self.status_changed, s)

    def _end_recording(self):
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._writer = None
            self._try_emit(self.log_message, "[Polar] Recording closed")
        if self._status == PolarStatus.RECORDING:
            self._set_status(PolarStatus.CONNECTED)

    # ── asyncio loop (background thread) ─────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_main())
        self._loop.close()

    async def _async_main(self):
        self._cmd_queue = asyncio.Queue()
        self._try_emit(self.log_message, "[Polar] Ready (macOS bleak)")

        client:    Optional[BleakClient] = None
        recording: bool = False

        def on_ecg(sender, data: bytearray):
            i = 10
            while i + 2 < len(data):
                r = data[i] | (data[i+1] << 8) | (data[i+2] << 16)
                if r & 0x800000:
                    r -= 0x1000000
                if self._writer and recording:
                    self._writer.writerow([time.time_ns(), r, "", "", ""])
                self.has_streaming_data = True
                self._last_sample_ns = time.time_ns()
                self._try_emit(self.ecg_sample, float(r))
                i += 3

        def on_hr(sender, data: bytearray):
            if len(data) < 2:
                return
            flags = data[0]
            bpm = data[1] if not (flags & 0x01) else struct.unpack_from("<H", data, 1)[0]
            if bpm > 0:
                if self._writer and recording:
                    self._writer.writerow([time.time_ns(), "", bpm, "", ""])
                self._last_sample_ns = time.time_ns()
                self._try_emit(self.hr_sample, bpm)
            if flags & 0x10:
                off = 3 if (flags & 0x01) else 2
                while off + 1 < len(data):
                    rr = round(struct.unpack_from("<H", data, off)[0] * 1000 / 1024, 1)
                    if self._writer and recording:
                        self._writer.writerow([time.time_ns(), "", "", rr, ""])
                    self._try_emit(self.rr_sample, rr)
                    off += 2

        # on_disconnect is built freshly inside the connect handler so it captures
        # the current device via default-arg, not enclosing-scope closure.

        async def keepalive(client):
            """Read battery every 10s to prevent CoreBluetooth idle timeout."""
            while client.is_connected:
                try:
                    await client.read_gatt_char(BATTERY_CHAR)
                except Exception:
                    pass
                await asyncio.sleep(10.0)

        while True:
            cmd = await self._cmd_queue.get()
            action = cmd[0]

            if action == "quit":
                # Cancel any pending reconnect first so it can't fire after quit.
                if self._pending_reconnect is not None:
                    try: self._pending_reconnect.cancel()
                    except Exception: pass
                    self._pending_reconnect = None
                if client and client.is_connected:
                    try:
                        await client.write_gatt_char(PMD_CONTROL, ECG_STOP, response=False)
                        await client.disconnect()
                    except Exception:
                        pass
                break

            elif action == "scan":
                duration = cmd[1]
                self._try_emit(self.log_message, f"[Polar] Scanning for {duration:.0f}s...")
                found = []
                try:
                    devices = await BleakScanner.discover(timeout=duration)
                    for d in devices:
                        name = d.name or ""
                        if "Polar" in name or "H10" in name or "H9" in name:
                            # Extract serial number from name e.g. "Polar H10 EA835125"
                            parts = name.split()
                            sn = parts[-1] if len(parts) > 2 else ""
                            pd = PolarDevice(
                                name=name,
                                address=str(d.address),
                                serial_number=sn,
                            )
                            found.append(pd)
                            self._try_emit(self.log_message, f"[Polar] Found: {name} [{d.address}]")
                    self._try_emit(self.log_message, f"[Polar] Scan complete — {len(found)} device(s)")
                except Exception as e:
                    self._try_emit(self.log_message, f"[Polar] Scan error: {e}")
                self._try_emit(self.devices_found, found)
                self._set_status(PolarStatus.IDLE)

            elif action == "connect":
                device: PolarDevice = cmd[1]
                self._set_status(PolarStatus.SCANNING)

                # If we have the address from scan, connect directly
                # Otherwise scan by serial number
                if device.address and len(device.address) > 10:
                    self._try_emit(self.log_message, f"[Polar] Connecting to {device.name}...")
                    try:
                        ble_dev = await BleakScanner.find_device_by_address(
                            device.address, timeout=10.0
                        )
                    except Exception:
                        ble_dev = None
                    if not ble_dev:
                        # Fall back to name scan
                        self._try_emit(self.log_message, "[Polar] Address not found, scanning by name...")
                        ble_dev = await BleakScanner.find_device_by_filter(
                            lambda bd, _: bd.name and device.serial_number in (bd.name or ""),
                            timeout=10.0,
                        )
                else:
                    self._try_emit(self.log_message, f"[Polar] Scanning for {device.serial_number}...")
                    ble_dev = await BleakScanner.find_device_by_filter(
                        lambda bd, _: bd.name and device.serial_number in (bd.name or ""),
                        timeout=10.0,
                    )

                if not ble_dev:
                    self._try_emit(self.log_message, "[Polar] Device not found — wear strap and retry")
                    self._set_status(PolarStatus.IDLE)
                    continue

                self._try_emit(self.log_message, f"[Polar] Connecting to {ble_dev.name}...")
                # Build on_disconnect freshly so it captures THIS device explicitly
                # (default-arg capture, not enclosing-scope closure).
                _captured_device = device
                def on_disconnect(c, dev=_captured_device):
                    self._try_emit(self.log_message,
                        f"[Polar] sensor_lost — {dev.name} ({getattr(dev,'address','?')}); "
                        f"reconnecting in 5s"
                    )
                    self._try_emit(self.sensor_event, "sensor_lost")
                    self._set_status(PolarStatus.IDLE)
                    if self._loop and self._loop.is_running():
                        # Save the call_later handle so a user-initiated
                        # `disconnect` action can cancel the pending reconnect
                        # before it fires (otherwise the 5s window lets the
                        # old strap come back to life after a deliberate swap).
                        def _do_reconnect(d=dev):
                            self._loop.create_task(self._cmd_queue.put(("connect", d)))
                        self._pending_reconnect = self._loop.call_later(5.0, _do_reconnect)
                try:
                    client = BleakClient(ble_dev, disconnected_callback=on_disconnect, timeout=20.0)
                    await client.connect()

                    try:
                        await client.start_notify(HR_CHAR, on_hr)
                        self._try_emit(self.log_message, "[Polar] HR notify: OK")
                    except Exception as e:
                        self._try_emit(self.log_message, f"[Polar] HR notify: {e}")

                    await client.start_notify(PMD_DATA, on_ecg)
                    self._try_emit(self.log_message, "[Polar] PMD_DATA notify: OK")

                    await client.write_gatt_char(PMD_CONTROL, ECG_SETTINGS, response=True)
                    await asyncio.sleep(0.3)
                    await client.write_gatt_char(PMD_CONTROL, ECG_START, response=True)
                    self._try_emit(self.log_message, "[Polar] ECG stream started (131 Hz)")
                    # If we previously emitted sensor_lost (via on_disconnect),
                    # emit sensor_recovered now so syncLog records the gap close.
                    self._try_emit(self.sensor_event, "sensor_recovered")
                    # Pending reconnect (if any) just consumed itself.
                    self._pending_reconnect = None

                    self._set_status(PolarStatus.CONNECTED)
                    # Read battery level
                    try:
                        batt = await client.read_gatt_char(BATTERY_CHAR)
                        self._try_emit(self.battery_changed, int(batt[0]))
                        self._try_emit(self.log_message, f"[Polar] Battery: {int(batt[0])}%")
                    except Exception:
                        pass
                    # Start continuous calibration probe every 5s
                    asyncio.ensure_future(self._continuous_probe(client))
                    # Keepalive to prevent CoreBluetooth idle timeout
                    asyncio.ensure_future(keepalive(client))

                except Exception as e:
                    self._try_emit(self.log_message, f"[Polar] Connect failed: {e}")
                    self._set_status(PolarStatus.IDLE)
                    client = None

            elif action == "disconnect":
                # User-initiated disconnect — cancel any auto-reconnect that
                # bleak's disconnected_callback may have just scheduled.
                if self._pending_reconnect is not None:
                    try: self._pending_reconnect.cancel()
                    except Exception: pass
                    self._pending_reconnect = None
                # Also drain any already-queued ("connect", _) entries that
                # raced ahead of us.
                try:
                    while not self._cmd_queue.empty():
                        peek = self._cmd_queue.get_nowait()
                        if peek and peek[0] == "connect":
                            self._try_emit(self.log_message,
                                "[Polar] Dropped queued reconnect (user-initiated disconnect)"
                            )
                        else:
                            # Not a connect — put it back at the front.
                            await self._cmd_queue.put(peek)
                            break
                except Exception:
                    pass
                if client and client.is_connected:
                    try:
                        await client.write_gatt_char(PMD_CONTROL, ECG_STOP, response=False)
                        await client.disconnect()
                    except Exception:
                        pass
                client = None
                self.calibrated_latency_ns = -1
                self._session_latency_ns = -1
                self.has_streaming_data    = False
                self._try_emit(self.calibration_changed, False)
                self._set_status(PolarStatus.IDLE)
                self._try_emit(self.log_message, "[Polar] Disconnected")

            elif action == "start_rec":
                recording = True

            elif action == "stop_rec":
                recording = False

            elif action == "marker":
                # Just log — no BLE write needed for marker, latency already calibrated
                label = cmd[1]
                self._try_emit(self.log_message, f"[Polar] marker sent: {label}")

            elif action == "calibrate_for_recording":
                # 10 BLE probes at 1s intervals against BATTERY_CHAR (read-only,
                # cached on device) — does NOT compete with the active ECG stream
                # for the PMD_CONTROL endpoint. Previous version wrote ECG_SETTINGS
                # to PMD_CONTROL during streaming, which can drop ECG samples.
                if not client or not client.is_connected:
                    self._try_emit(self.log_message, "[Polar] Record-start calibration skipped — not connected")
                    continue
                self._try_emit(self.log_message, "[Polar] Record-start calibration (10 probes × 1s, BATTERY_CHAR)...")
                for _ in range(10):
                    try:
                        t1 = time.time_ns()
                        await client.read_gatt_char(BATTERY_CHAR)
                        self._rtt_buffer.append(time.time_ns() - t1)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                self._update_latency()
                self._session_latency_ns = self.calibrated_latency_ns
                self._try_emit(self.log_message,
                    f"[Polar] Session latency locked: one-way={self._session_latency_ns/1e6:.1f}ms "
                    f"(n={len(self._rtt_buffer)} samples)"
                )
