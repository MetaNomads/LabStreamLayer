"""
polar_subprocess.py — Polar H10 BLE worker process

Spawned by core/polar.py. Handles all BLE on Windows using:
- simplepyble: scan, connect, subscribe PMD_DATA + HR (no auth needed)
- winrt: silent pairing + ECG start commands (requires auth)

Connection flow (from Polar SDK analysis):
  1. Register silent pairing handler via winrt (before connecting)
  2. simplepyble: scan → connect → subscribe PMD_DATA + HR
  3. simplepyble write_request PMD_CONTROL → triggers pairing
  4. winrt handler auto-accepts → Windows takes connection (no dialog)
  5. Release simplepyble objects (free WinRT GATT session)
  6. winrt: write ECG_SETTINGS + ECG_START
  7. Data flows: ECG + HR → JSON lines on stdout

Parent → child (stdin JSON):
  {"cmd": "scan"}
  {"cmd": "connect", "address": "A0:9E:1A:EA:83:51"}
  {"cmd": "start_rec", "session_ts": "..."}
  {"cmd": "stop_rec"}
  {"cmd": "marker", "label": "..."}
  {"cmd": "disconnect"}
  {"cmd": "quit"}

Child → parent (stdout JSON):
  {"type": "status", "msg": "..."}
  {"type": "error",  "msg": "..."}
  {"type": "device", "name": "...", "address": "..."}
  {"type": "connected", "mtu": 232}
  {"type": "ecg", "ts_ns": 123, "uv": -142}
  {"type": "hr",  "ts_ns": 123, "bpm": 72}
  {"type": "rr",  "ts_ns": 123, "ms": 820}
  {"type": "disconnected"}
"""

import asyncio
import json
import struct
import sys
import threading
import time
import gc
import uuid as uuidlib

import simplepyble

from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCommunicationStatus, GattSharingMode,
)
from winrt.windows.devices.enumeration import DevicePairingKinds
from winrt.windows.storage.streams import DataWriter

PMD_SERVICE  = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL  = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA     = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
HR_SERVICE   = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR      = "00002a37-0000-1000-8000-00805f9b34fb"

ECG_SETTINGS = bytes([0x01, 0x00])
ECG_START    = bytes([0x02,0x00,0x00,0x01,0x82,0x00,0x01,0x01,0x0E,0x00])
ECG_STOP     = bytes([0x03, 0x00])


def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def log(msg: str):
    send({"type": "status", "msg": msg})


# ── winrt helpers ─────────────────────────────────────────────────────────────

def make_writer(data: bytes):
    w = DataWriter(); w.write_bytes(data); return w.detach_buffer()

async def get_char_winrt(device, svc_uuid: str, char_uuid: str):
    r = await device.get_gatt_services_for_uuid_async(uuidlib.UUID(svc_uuid))
    if r.status != GattCommunicationStatus.SUCCESS or not r.services: return None
    svc = r.services[0]
    try: await svc.open_async(GattSharingMode.SHARED_READ_AND_WRITE)
    except: pass
    cr = await svc.get_characteristics_async()
    if cr.status != GattCommunicationStatus.SUCCESS: return None
    for ch in cr.characteristics:
        if str(ch.uuid).lower() == char_uuid: return ch
    return None

async def winrt_start_ecg(address: str):
    """Use winrt to write ECG commands after Windows has the authenticated connection."""
    addr_int = int(address.replace(":", ""), 16)
    device = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
    if not device:
        log("winrt: device not found"); return False

    log(f"winrt: connected={device.connection_status}  paired={device.device_information.pairing.is_paired}")
    await asyncio.sleep(1.0)

    ctrl = await get_char_winrt(device, PMD_SERVICE, PMD_CONTROL)
    if not ctrl:
        log("winrt: PMD_CONTROL not accessible"); return False

    try:
        r = await asyncio.wait_for(
            ctrl.write_value_with_result_async(make_writer(ECG_SETTINGS)), timeout=6.0)
        log(f"winrt: ECG_SETTINGS {r.status}")
    except (OSError, asyncio.TimeoutError):
        log("winrt: ECG_SETTINGS timeout — continuing")
    await asyncio.sleep(0.3)

    try:
        r = await asyncio.wait_for(
            ctrl.write_value_with_result_async(make_writer(ECG_START)), timeout=6.0)
        log(f"winrt: ECG_START {r.status}")
        return r.status == GattCommunicationStatus.SUCCESS
    except (OSError, asyncio.TimeoutError):
        pass

    # Fallback: write without response
    try:
        await ctrl.write_value_async(make_writer(ECG_START))
        log("winrt: ECG_START sent (no response)")
        return True
    except OSError as e:
        log(f"winrt: ECG_START failed: {e}")
        return False


# ── Recording ─────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self): self.active = False

    def start(self, ts: str):
        self.active = True
        log(f"Recording: {ts}")

    def stop(self):
        self.active = False
        log("Recording stopped")

    def write_ecg(self, uv: int):
        if self.active:
            send({"type": "ecg", "ts_ns": time.time_ns(), "uv": uv})

    def write_hr(self, bpm: int):
        if self.active:
            send({"type": "hr", "ts_ns": time.time_ns(), "bpm": bpm})

    def write_rr(self, ms: float):
        if self.active:
            send({"type": "rr", "ts_ns": time.time_ns(), "ms": ms})

    def write_marker(self, label: str):
        if self.active:
            send({"type": "marker", "ts_ns": time.time_ns(), "label": label})


recorder = Recorder()


# ── BLE callbacks ─────────────────────────────────────────────────────────────

def on_ecg(data: bytes):
    i = 10
    while i + 2 < len(data):
        r = data[i] | (data[i+1]<<8) | (data[i+2]<<16)
        if r & 0x800000: r -= 0x1000000
        recorder.write_ecg(r); i += 3

def on_hr(data: bytes):
    if len(data) < 2: return
    flags = data[0]
    bpm = data[1] if not (flags & 0x01) else struct.unpack_from("<H", data, 1)[0]
    recorder.write_hr(bpm)
    if flags & 0x10:
        off = 3 if flags & 0x01 else 2
        while off + 1 < len(data):
            rr = round(struct.unpack_from("<H", data, off)[0] * 1000 / 1024, 1)
            recorder.write_rr(rr); off += 2


# ── Main connect logic ────────────────────────────────────────────────────────

def connect_and_stream(address: str, cmd_queue: list):
    """
    Full connection sequence:
    1. Register silent pairing handler via winrt
    2. simplepyble connect + subscribe PMD_DATA + HR
    3. Trigger pairing via write_request
    4. Release simplepyble → winrt writes ECG commands
    """

    # Step 1: register silent pairing handler
    pairing_token = [None]
    pairing_device = [None]

    async def register_handler():
        addr_int = int(address.replace(":", ""), 16)
        device = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
        if not device: return

        pairing_device[0] = device
        custom = device.device_information.pairing.custom

        def on_pairing_requested(sender, args):
            log(f"Pairing request auto-accepted (kind={args.pairing_kind})")
            args.accept()

        pairing_token[0] = custom.add_pairing_requested(on_pairing_requested)
        log(f"Silent pairing handler registered (paired={device.device_information.pairing.is_paired})")

    asyncio.run(register_handler())

    # Step 2: simplepyble connect
    adapter = simplepyble.Adapter.get_adapters()[0]
    adapter.scan_for(5000)
    polar = None
    for p in adapter.scan_get_results():
        if address.upper() in p.address().upper():
            polar = p; break
    if not polar:
        send({"type": "error", "msg": "Device not found in scan"})
        return

    polar.connect()
    send({"type": "connected", "mtu": 232})
    log("simplepyble connected")

    # Step 3: subscribe PMD_DATA + HR (no auth needed)
    try:
        polar.notify(PMD_SERVICE, PMD_DATA, on_ecg)
        log("PMD_DATA notify: OK")
    except Exception as e:
        log(f"PMD_DATA notify: {e}")

    try:
        polar.notify(HR_SERVICE, HR_CHAR, on_hr)
        log("HR notify: OK")
    except Exception as e:
        log(f"HR notify: {e}")

    # Step 4: trigger pairing via write_request
    log("Triggering pairing via PMD_CONTROL write...")
    windows_took_over = False
    try:
        polar.write_request(PMD_SERVICE, PMD_CONTROL, ECG_SETTINGS)
        # If we get here, no pairing needed — try ECG_START directly
        polar.write_request(PMD_SERVICE, PMD_CONTROL, ECG_START)
        log("ECG stream started via simplepyble (already authenticated)")
        return  # data flows via callbacks
    except Exception as e:
        if "-2147483634" in str(e) or "unexpected time" in str(e).lower():
            log("Windows took connection after pairing — switching to winrt")
            windows_took_over = True
        else:
            log(f"Write error: {e}")

    # Step 5: release simplepyble before winrt accesses PMD
    if windows_took_over:
        log("Releasing simplepyble objects...")
        try: polar.disconnect()
        except: pass
        del polar
        gc.collect()
        time.sleep(3.0)

        # winrt ECG start
        log("winrt: writing ECG commands...")
        asyncio.run(winrt_start_ecg(address))


# ── Command loop ──────────────────────────────────────────────────────────────

def main():
    log("Polar subprocess ready")
    connected_address = [None]

    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = cmd.get("cmd")

        if action == "quit":
            break

        elif action == "scan":
            log("Scanning for Polar H10...")
            try:
                adapter = simplepyble.Adapter.get_adapters()[0]
                adapter.scan_for(5000)
                found = 0
                for p in adapter.scan_get_results():
                    name = p.identifier() or ""
                    if "Polar" in name or "H10" in name:
                        send({"type": "device", "name": name, "address": p.address()})
                        found += 1
                log(f"Scan complete: {found} device(s) found")
            except Exception as e:
                send({"type": "error", "msg": f"Scan error: {e}"})

        elif action == "connect":
            address = cmd.get("address", "")
            connected_address[0] = address
            t = threading.Thread(
                target=connect_and_stream,
                args=(address, []),
                daemon=True
            )
            t.start()

        elif action == "start_rec":
            recorder.start(cmd.get("session_ts", ""))

        elif action == "stop_rec":
            recorder.stop()

        elif action == "marker":
            recorder.write_marker(cmd.get("label", ""))

        elif action == "disconnect":
            send({"type": "disconnected"})

    send({"type": "disconnected"})


if __name__ == "__main__":
    main()
