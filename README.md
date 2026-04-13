# Lab Stream Layer

Multi-device physiological data synchronisation tool for macOS.

Connects **Polar H10** (ECG + HR), **EmotiBit**, and **Unity** — synchronises them with a shared ping marker system and per-ping latency measurement.

---

## Getting Started

### Step 1 — One-time developer setup (you, not users)

After cloning the repo, build the install app once and commit it:

```bash
bash scripts/build_install_app.sh
git add "Install LabStreamLayer.app"
git commit -m "Add install app"
git push
```

This compiles `scripts/install.applescript` into `Install LabStreamLayer.app` at the repo root. Users never need to run this step.

### Step 2 — User install (one-time per machine)

1. Clone or download the repo
2. Double-click **`Install LabStreamLayer.app`**
3. A Terminal window opens and runs the installer automatically
4. When done, **`LabStreamLayer.app`** appears at the repo root

### Step 3 — Launch

Double-click **`LabStreamLayer.app`** to run.

---

## Requirements

- macOS 11+
- Python 3.9+ (`python3` on PATH — system Python works, Anaconda not needed)
- Python packages installed automatically by the installer: `PyQt6`, `bleak`

---

## Devices

| Device | Connection | Data | Sample Rate |
|--------|-----------|------|-------------|
| Polar H10 | BLE (CoreBluetooth) | ECG | 130 Hz |
| Polar H10 | BLE (CoreBluetooth) | HR | 1 Hz |
| Polar H10 | BLE (CoreBluetooth) | RR intervals | ~1 Hz (irregular) |
| EmotiBit | UDP / WiFi | PPG, EDA, Temp, IMU | Device-dependent |
| Unity | UDP | Ping timestamps | On ping |

---

## Output Files

Recordings saved to `~/LabStreamLayer_Recordings/`:

| File | Contents |
|------|----------|
| `polar_<session>.csv` | ECG (µV), HR (bpm), RR (ms), markers |
| `marker_outlog_<session>.csv` | Ping sync log with per-device latency |
| `unity_ping_log_<session>.csv` | Unity receipt timestamps |

### marker_outlog columns

```
ping_id, sls_clock, emotibit_latency_ns, polar_latency_ns, unity_latency_ns
```

| Column | Description |
|--------|-------------|
| `ping_id` | e.g. `ping_001` |
| `sls_clock` | SLS machine UTC time at send (nanoseconds) |
| `emotibit_latency_ns` | One-way UDP latency to EmotiBit (RTT/2) |
| `polar_latency_ns` | One-way BLE latency to Polar H10 (RTT/2) |
| `unity_latency_ns` | One-way UDP latency to Unity (RTT/2) |

**Post-processing alignment:**
```
device_receive_time_in_host_clock   = sls_clock + latency_ns
device_receive_time_in_device_clock = device_recorded_time - latency_ns
```

Latency is `-1` when the device is not connected.

---

## Unity Integration

1. Add `SyncBridge.cs` to a persistent GameObject in your Unity scene
2. Unity automatically logs ping receipt times and echoes ACKs for latency measurement
3. Call `SyncBridge.SendPing()` from game code to trigger a ping from Unity

---

## Sync Architecture

Each ping triggers a fresh latency measurement per device:

- **EmotiBit**: SLS sends `HE`, EmotiBit replies `HH` → RTT measured, `/2` = one-way
- **Polar**: BLE `write_gatt_char(response=True)` includes H10 ACK → RTT measured, `/2` = one-way  
- **Unity**: SLS broadcasts `ping_NNN`, Unity echoes `ACK:ping_NNN` → RTT measured, `/2` = one-way

This is NTP-style clock synchronisation applied per-ping. Assumes symmetric network paths (valid for local WiFi/BLE, typical error < 5 ms).
