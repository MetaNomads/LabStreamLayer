# LSL Repository — Panel Remediation Report

**Companion to:** `PANEL_AUDIT_REPORT.md`
**Date:** 2026-04-28
**Scope:** All CRITICAL and HIGH findings from the panel audit, plus most MEDIUMs and the LOWs that were small.
**Out of scope (by panel decision):** Full Polar Windows rewrite to bleak; subject-ID-prompt UI modal. Both are flagged here for a follow-up decision.

The panel reconvened to read the bugs as a system, not as 25 isolated lines. Four root-cause patterns drove the fix design:

1. **Silent failure as default.** Most fixes either remove a bare `except: pass`, surface a previously-swallowed exception once per session, or replace an unbounded retry with a bounded one that ends in a *visible* DEGRADED banner.
2. **Wire-format ambiguity.** Every protocol with two endpoints (`SyncBridge.cs` ↔ `unity.py`, README ↔ code, `add_manual` ↔ `add_manual_device`) was made to agree.
3. **Calibration entangled with the control plane.** Polar calibration moved off `PMD_CONTROL`; EmotiBit `HH` matching now gated on the connected device IP; Unity DATA/PING/ACK gated on the connected source IP.
4. **Implicit state.** Recording start now declares its tunables as named constants and the watchdog tracks data flow, not just connection enums. (A full `IDLE → CALIBRATING → ARMED → RECORDING` state machine is *not* in this pass — see "Decisions deferred" below.)

---

## Per-finding traceability

Each row maps an audit finding (by file:line in the original code) to the change that addresses it.

| # | Severity | File:line in original | What was wrong | What landed |
|---|----------|----------------------|----------------|-------------|
| 1 | CRITICAL | `emotibit.py:169-175` | Three attribute initializations parked after a `return` inside the `seconds_since_recording_start` property — never executed. EmotiBit calibration thread silently dies on first AttributeError. | Moved `_rtt_buffer`, `_continuous_calib_active`, `_session_latency_ns` (plus new `_last_sample_ns`, `_given_up`, `_reconnect_attempts`) into `__init__`. Removed the dead lines from the property. Added `effective_latency_ns` and `seconds_since_last_sample` public properties. |
| 2 | CRITICAL | `main_window.py:526` | `add_manual_device` doesn't exist on `EmotiBitHandler`. Manual-IP button silently raises AttributeError. | Added `add_manual_device` as a public alias of `add_manual` in `emotibit.py`. Both names work. |
| 3 | CRITICAL | `SyncBridge.cs:108-112` ↔ `unity.py:507-522` | ACK format mismatch: SyncBridge sent `"ACK:" + msg`; unity.py expected `ACK:<ping_id>:<unity_ns>`. Unity row in syncLog never written. | `SyncBridge.cs` now sends `$"ACK:{msg}:{ns}"`. unity.py's receive path is unchanged; the gate now passes; `log_unity_ack` fires; the Unity row exists. |
| 4 | CRITICAL | README ↔ `main_window.py:943` ↔ `sync_logger.py:48` | Output dir name and syncLog filename inconsistent across docs and code. | `main_window.py` now defaults to `~/LabStreamLayer_Recordings/` (matches README). README updated to the actual filenames (`syncLog_<ts>.csv`, `session_meta.json`). |
| 5 | HIGH | `main_window.py:1597-1598` | `_last` referenced before assignment when first DATA token has no `=`. NameError swallowed by bare `except: pass`. Live monitor goes flat silently. | Initialise `_last = None` before the loop. Replace bare-except with throttled `_unity_parse_warned` log so the operator sees the failure once. |
| 6 | HIGH | `emotibit.py:567-574` | `_auto_reconnect` is unbounded; spawns a fresh heartbeat thread on every retry. | Bounded retry with exponential-backoff schedule `(5,10,20,40,60,60,60,60,60,60)`; on exhaustion sets `_given_up = True` and emits a "DEGRADED — gave up reconnecting" log line. Watchdog displays the persistent banner. |
| 7 | HIGH | `polar_mac.py:264-271` | `on_disconnect` closes over `device` from outer-scope; can NameError or chase a stale device. | `on_disconnect` is now built per-connect with `dev=device` default-arg capture — the lambda always uses the device that was just connected, never a stale reference. |
| 8 | HIGH | `polar_mac.py:367-369, 417-429` | Calibration writes `ECG_SETTINGS` to `PMD_CONTROL` while ECG is streaming — competes with the data plane on the same characteristic. | Calibration switched to `read_gatt_char(BATTERY_CHAR)` — read-only, cached, no interference with PMD_CONTROL. |
| 9 | HIGH | `main_window.py:1438-1474` | Watchdog only checked status enum, not whether data is flowing. A "connected but silent" link looked healthy. | New silence-detection: each handler tracks `last_sample_ns` (set in `on_ecg`/`on_hr`/`PR`/`HR`/`DATA,unity,…`); watchdog warns if a required sensor has been silent for `SAMPLE_SILENCE_S = 3.0` s; gap markers `sensor_silent` / `sensor_resumed` written into the syncLog on transitions only. |
| 10 | HIGH | `SyncBridge.cs:23` + `unity.py:504-506` | UDP/12345 honored `PING`/`ping_*` from any source — cross-experiment contamination on a shared subnet. | SyncBridge latches `_lockedHostIp` on first DISCOVER/CONNECT/PING and rejects everything else. unity.py now drops every non-handshake message whose source IP isn't the connected device. |
| 11 | HIGH | `main_window.py:1393` (auto-ping schedule) | Comment said "first ping at t=10s, every 5s" but code did 5s/2s. | Tunables hoisted to named module constants: `FIRST_PING_DELAY_MS = 10_000`, `PING_INTERVAL_MS = 5_000`, `AUTO_PING_TOTAL = 3`. Comment, code, log line all use the same constants. First auto-ping now reliably lands *after* the 10-s record-start calibration burst. |
| 12 | HIGH | `SyncBridge.cs` lifecycle | No `OnApplicationPause` — Quest don/doff produces a silent gap. | `OnApplicationPause(bool paused)` emits `headset_doffed` / `headset_donned` to the locked host; unity.py forwards as a log line so the operator sees the don/doff in real time and the syncLog records the cause of any gap. |
| 13 | MEDIUM | `unity.py:356-364` | "Stop existing poll loop" used `time.sleep(0.05)` — shorter than the loop's 1 s sleep, so two poll loops can run in parallel. | Replaced with `threading.Event` — `start_data_stream` signals the old loop, the loop's `wait()` exits promptly, the new loop starts cleanly. Also removed the duplicate `start_data_stream` call in `_start_rec`. |
| 14 | MEDIUM | `emotibit.py:640-643` | `_hh_event` set on any HH packet from any device — calibration latches onto the wrong EmotiBit on a multi-device subnet. | HH match now gated on `ip == self._connected.ip` (or no connected device yet). |
| 15 | MEDIUM | `main_window.py:1638-1641` | `_ping` autostarted an orphan session if pressed before recording. | Removed. `_ping` before recording is now a no-op with a clear log line. |
| 16 | MEDIUM | sync_logger / `_start_rec` (no metadata) | No subject id, git sha, device IPs, locked latencies persisted — sessions not reproducible. | New `_write_session_meta` writes `session_meta.json` into each `lsl_<ts>/` folder containing app version, git SHA, platform, Python version, per-device required-flag and IP and locked latency, and all tunables (`FIRST_PING_DELAY_MS` etc). Operator can verify which build generated the data. |
| 17 | MEDIUM | `main_window.py:1499-1501` | Reach-around to private `_unity._session_latency_ns`. | New `effective_latency_ns` property on EmotiBit, Polar, and Unity handlers. `_on_unity_ack` uses it. |
| 18 | LOW | `main.py` | No version banner on launch. | `APP_VERSION = "0.2.0"`, git short SHA, Python version printed on stdout and set as window-title application name. |

---

## Files changed

| File | Lines changed (approx) | Summary |
|------|------------------------|---------|
| `src/emotibit.py` | +60 / -10 | Init fix, `add_manual_device` alias, IP-gated HH, bounded reconnect, `last_sample_ns`, `effective_latency_ns`, `given_up` |
| `src/polar_mac.py` | +35 / -10 | BATTERY_CHAR calibration, on_disconnect default-arg capture, `last_sample_ns`, `effective_latency_ns`, `given_up`, `seconds_since_last_sample` |
| `src/unity.py` | +25 / -10 | source-IP gate, stop-event poll loop, `last_sample_ns`, `effective_latency_ns`, don/doff log |
| `src/main_window.py` | +120 / -30 | Tunable constants, `_last=None` init, throttled parser-error log, watchdog rewrite, gap markers, `_write_session_meta`, output dir rename, autostart removal, `effective_latency_ns` use, duplicate `start_data_stream` removed |
| `src/main.py` | +20 / 0 | Version banner |
| `SyncBridge.cs` | +30 / -3 | ACK now carries `unity_ns`, source-IP lock, OnApplicationPause don/doff |
| `README.md` | rewritten Output Files / syncLog columns sections | Filenames + columns now match code; `sensor_silent`/`sensor_resumed`/`sensor_lost` events documented |

---

## What's now observable that wasn't before

This is the operator-facing answer to "would you let a grad student run a 60-subject study on this":

- **EmotiBit calibration produces a non-`-1` `emotibit_latency_ns`** in the syncLog. (Previously every session had `-1` because the calibration thread was silently dead.)
- **The Unity row appears in `syncLog_<ts>.csv`** when Unity is required. (Previously never appeared.)
- **Manual EmotiBit IP entry works** for cross-subnet setups.
- **Sensor disconnects produce a visible DEGRADED banner** and a `sensor_silent` row in the syncLog with the timestamp, a `sensor_resumed` row when it comes back. The gap is recoverable post-hoc.
- **A "connected-but-silent" stream is detected within `SAMPLE_SILENCE_S = 3.0` s.** Previously: not at all.
- **Manual ping before recording does not create an orphan session folder.**
- **`session_meta.json` lets you tie the data file to a specific git SHA and configuration months later.**
- **Quest headset don/doff is marked into the syncLog** via `headset_doffed`/`headset_donned`, so a 30-s gap mid-session has a known cause.
- **A second LSL host on the same subnet cannot inject pings** into this session.

---

## Decisions deferred

Both deserve their own discussion before landing.

**1. Full state machine `IDLE → CALIBRATING → ARMED → RECORDING`.**
Status: **not implemented**. The panel agreed this is the right design but the change touches the entire `_start_rec` / `_update_start_btn` / Unity-recording-coordination tangle. The fixes in this pass make the *current* timing safe (auto-ping moved past the calibration burst, calibration off the control plane, watchdog catches actual silence). A formal state machine is a follow-up that should also fold in: `check_sd_card` called from ARMING; abort if Polar takes longer than 20 s to confirm streaming; merge the `_on_unity_recording_started` flow with `_start_rec` so there's only one record-start path.

**2. Subject-ID prompt at session start.**
Status: **not implemented**. `session_meta.json` writes everything that is automatically discoverable. A modal dialog asking for `subject_id`, `condition`, `run_number`, `experimenter` would close the loop, but it's a UX change you should sign off on (the modal becomes the gate to start recording — wrong for some pilot workflows). Recommended: add a `QLineEdit` row in the main window left column, persisted in `QSettings`, written into `session_meta.json` at session start. ~30 lines of UI code.

**3. Polar Windows path → bleak.**
Status: **not implemented**. `polar.py` + `polar_subprocess.py` together are 533 lines of platform-specific recovery code. `bleak` now supports Windows via WinRT. Replacing with the macOS-style `polar_mac.py` would unify the codebase to one file and inherit all the recovery logic the panel just hardened. Recommendation: file an issue, time-box a 1-day spike to confirm bleak handles the Polar pairing race on Windows, then either commit to the migration or document why we keep the dual path.

**4. NTP-style drift correction.**
Status: **not implemented**. The README claims `<5 ms` per-ping sync but no second-order drift model is fit. The `syncLog_<ts>.csv` now contains everything needed to fit one offline (per-ping latency × time-since-start gives drift). Recommended: ship a small post-processing script (`scripts/fit_drift.py`) that emits a corrected `aligned_<ts>.csv` from the raw recordings.

---

## Verification done

- All eight Python files parse cleanly under `python3 -c "import ast; ast.parse(open(f).read())"`.
- `emotibit.py` was grepped to confirm `_rtt_buffer`, `_continuous_calib_active`, `_session_latency_ns`, `_last_sample_ns`, `_given_up`, `effective_latency_ns` are now defined in `__init__` and as properties.
- `SyncBridge.cs` was grepped to confirm the new wire format `$"ACK:{msg}:{ns}"` is in place.
- `add_manual_device` resolves on `EmotiBitHandler` via the alias.

## Verification still required by you (cannot be done from here)

1. **Run a session against the real EmotiBit and confirm `emotibit_latency_ns > 0`** in the resulting `syncLog_<ts>.csv`. This is the headline test.
2. **Run a session with Unity required and confirm a Unity row exists** in the syncLog (machine column = `unity`).
3. **Start a session, then walk the Polar strap out of BLE range for >10 s, then back.** Confirm: a `sensor_silent` row at gap-onset, a `sensor_resumed` row at recovery, the live monitor warns "Polar silent X.Xs" during the gap.
4. **Click Send Ping before pressing Start Recording.** Confirm: no orphan session folder is created and the log line says "Ignored — no active recording".
5. **Open the EmotiBit picker and enter a manual IP.** Confirm: device row appears (this used to crash silently).
6. **Take Quest headset off mid-session.** Confirm: `headset_doffed` line in the operator log; `headset_donned` on putting it back on.

If any of these regress, file with file:line and I'll pick up the second pass.

— End of remediation report —
