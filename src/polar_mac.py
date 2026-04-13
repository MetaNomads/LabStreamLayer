"""
polar_mac.py — Polar H10 handler for macOS

Uses bleak (CoreBluetooth) directly. Confirmed working: 131 Hz ECG, HR.
Same public API as polar.py — drop-in replacement on Mac.
"""

import asyncio
import csv
import struct
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

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
        self._calib_event = threading.Event()
        self._calib_result: int = -1

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

    def start_recording(self, session_ts: str):
        # Re-calibrate 5s after recording starts
        self.calibrate(n=5, delay=5.0)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / f"polar_{session_ts}.csv"
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
        self.log_message.emit(f"[Polar] Recording → {path.name}")
        self.log_message.emit("[Polar] Sample rates: ECG=130Hz  HR=1Hz  RR=irregular")
        self._send_cmd(("start_rec",))

    def stop_recording(self):
        self._send_cmd(("stop_rec",))
        self._end_recording()

    def send_marker(self, label: str) -> int:
        """Send marker. Returns calibrated one-way BLE latency in ns."""
        send_ns = time.time_ns()
        if self._writer:
            self._writer.writerow([send_ns, "", "", "", label])
        self._send_cmd(("marker", label))
        return self.calibrated_latency_ns

    def calibrate(self, n: int = 5, delay: float = 10.0):
        """
        Run BLE calibration burst in background.
        delay: seconds to wait before probing (10s on connect, 5s on re-calibrate).
        """
        self._calib_event.clear()
        self._send_cmd(("calibrate", n, delay))

    @property
    def status(self) -> PolarStatus:
        return self._status

    # ── Internal: Qt → asyncio ────────────────────────────────────────────────

    def _send_cmd(self, cmd: tuple):
        if self._loop and self._cmd_queue:
            self._loop.call_soon_threadsafe(self._cmd_queue.put_nowait, cmd)

    def _set_status(self, s: PolarStatus):
        if s != self._status:
            self._status = s
            self.status_changed.emit(s)

    def _end_recording(self):
        if self._csv_file:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._writer = None
            self.log_message.emit("[Polar] Recording closed")
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
        self.log_message.emit("[Polar] Ready (macOS bleak)")

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
                self.ecg_sample.emit(float(r))
                i += 3

        def on_hr(sender, data: bytearray):
            if len(data) < 2:
                return
            flags = data[0]
            bpm = data[1] if not (flags & 0x01) else struct.unpack_from("<H", data, 1)[0]
            if bpm > 0:
                if self._writer and recording:
                    self._writer.writerow([time.time_ns(), "", bpm, "", ""])
                self.hr_sample.emit(bpm)
            if flags & 0x10:
                off = 3 if (flags & 0x01) else 2
                while off + 1 < len(data):
                    rr = round(struct.unpack_from("<H", data, off)[0] * 1000 / 1024, 1)
                    if self._writer and recording:
                        self._writer.writerow([time.time_ns(), "", "", rr, ""])
                    self.rr_sample.emit(rr)
                    off += 2

        def on_disconnect(c: BleakClient):
            self.log_message.emit("[Polar] Device disconnected")
            self._set_status(PolarStatus.IDLE)

        while True:
            cmd = await self._cmd_queue.get()
            action = cmd[0]

            if action == "quit":
                if client and client.is_connected:
                    try:
                        await client.write_gatt_char(PMD_CONTROL, ECG_STOP, response=False)
                        await client.disconnect()
                    except Exception:
                        pass
                break

            elif action == "scan":
                duration = cmd[1]
                self.log_message.emit(f"[Polar] Scanning for {duration:.0f}s...")
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
                            self.log_message.emit(f"[Polar] Found: {name} [{d.address}]")
                    self.log_message.emit(f"[Polar] Scan complete — {len(found)} device(s)")
                except Exception as e:
                    self.log_message.emit(f"[Polar] Scan error: {e}")
                self.devices_found.emit(found)
                self._set_status(PolarStatus.IDLE)

            elif action == "connect":
                device: PolarDevice = cmd[1]
                self._set_status(PolarStatus.SCANNING)

                # If we have the address from scan, connect directly
                # Otherwise scan by serial number
                if device.address and len(device.address) > 10:
                    self.log_message.emit(f"[Polar] Connecting to {device.name}...")
                    try:
                        ble_dev = await BleakScanner.find_device_by_address(
                            device.address, timeout=10.0
                        )
                    except Exception:
                        ble_dev = None
                    if not ble_dev:
                        # Fall back to name scan
                        self.log_message.emit("[Polar] Address not found, scanning by name...")
                        ble_dev = await BleakScanner.find_device_by_filter(
                            lambda bd, _: bd.name and device.serial_number in (bd.name or ""),
                            timeout=10.0,
                        )
                else:
                    self.log_message.emit(f"[Polar] Scanning for {device.serial_number}...")
                    ble_dev = await BleakScanner.find_device_by_filter(
                        lambda bd, _: bd.name and device.serial_number in (bd.name or ""),
                        timeout=10.0,
                    )

                if not ble_dev:
                    self.log_message.emit("[Polar] Device not found — wear strap and retry")
                    self._set_status(PolarStatus.IDLE)
                    continue

                self.log_message.emit(f"[Polar] Connecting to {ble_dev.name}...")
                try:
                    client = BleakClient(ble_dev, disconnected_callback=on_disconnect, timeout=20.0)
                    await client.connect()

                    try:
                        await client.start_notify(HR_CHAR, on_hr)
                        self.log_message.emit("[Polar] HR notify: OK")
                    except Exception as e:
                        self.log_message.emit(f"[Polar] HR notify: {e}")

                    await client.start_notify(PMD_DATA, on_ecg)
                    self.log_message.emit("[Polar] PMD_DATA notify: OK")

                    await client.write_gatt_char(PMD_CONTROL, ECG_SETTINGS, response=True)
                    await asyncio.sleep(0.3)
                    await client.write_gatt_char(PMD_CONTROL, ECG_START, response=True)
                    self.log_message.emit("[Polar] ECG stream started (131 Hz)")

                    self._set_status(PolarStatus.CONNECTED)
                    # Read battery level
                    try:
                        batt = await client.read_gatt_char(BATTERY_CHAR)
                        self.battery_changed.emit(int(batt[0]))
                        self.log_message.emit(f"[Polar] Battery: {int(batt[0])}%")
                    except Exception:
                        pass
                    # Run calibration burst 3s after connect
                    await self._cmd_queue.put(("calibrate", 5, 3.0))

                except Exception as e:
                    self.log_message.emit(f"[Polar] Connect failed: {e}")
                    self._set_status(PolarStatus.IDLE)
                    client = None

            elif action == "disconnect":
                if client and client.is_connected:
                    try:
                        await client.write_gatt_char(PMD_CONTROL, ECG_STOP, response=False)
                        await client.disconnect()
                    except Exception:
                        pass
                client = None
                self.calibrated_latency_ns = -1
                self.calibration_changed.emit(False)
                self._set_status(PolarStatus.IDLE)
                self.log_message.emit("[Polar] Disconnected")

            elif action == "start_rec":
                recording = True

            elif action == "stop_rec":
                recording = False

            elif action == "marker":
                # Just log — no BLE write needed for marker, latency already calibrated
                label = cmd[1]
                self.log_message.emit(f"[Polar] marker sent: {label}")

            elif action == "calibrate":
                n, delay = cmd[1], cmd[2]
                if not client or not client.is_connected:
                    self.log_message.emit("[Polar] Calibration skipped — not connected")
                    self._calib_event.set()
                    continue
                self.log_message.emit(f"[Polar] Calibration in {delay:.0f}s...")
                await asyncio.sleep(delay)
                if not client or not client.is_connected:
                    self.log_message.emit("[Polar] Calibration aborted — disconnected")
                    self._calib_event.set()
                    continue
                self.log_message.emit(f"[Polar] Calibrating BLE latency ({n} probes)...")
                samples = []
                for i in range(n):
                    try:
                        t1 = time.time_ns()
                        await client.write_gatt_char(PMD_CONTROL, ECG_SETTINGS, response=True)
                        t4 = time.time_ns()
                        samples.append(t4 - t1)
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)

                if samples:
                    samples.sort()
                    if len(samples) >= 4:
                        samples = samples[:-1]   # discard top outlier
                    median_rtt = samples[len(samples) // 2]
                    one_way    = median_rtt // 2
                    self._last_ble_latency_ns  = one_way
                    self.calibrated_latency_ns = one_way
                    self._calib_result         = one_way
                    self.log_message.emit(
                        f"[Polar] Calibrated: "
                        f"median RTT={median_rtt/1e6:.1f}ms  "
                        f"one-way={one_way/1e6:.1f}ms  "
                        f"(n={len(samples)} samples)"
                    )
                    self.calibration_changed.emit(True)
                else:
                    self.log_message.emit("[Polar] Calibration failed — no BLE responses")
                self._calib_event.set()
