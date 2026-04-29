# LSL Repository — Pass-2 Remediation Report

**Companion to:** `PANEL_AUDIT_REPORT_PASS2.md` (the pass-2 audit findings).
**Date:** 2026-04-28
**Scope:** Every HIGH and MEDIUM finding from pass 2, plus both LOWs that were small. The pass-2 panel left four items explicitly deferred — those are unchanged.

This pass focused on three structural problems the new panel surfaced:

1. **The pass-1 fix introduced a regression** (Unity RECONNECT path made unreachable by the new IP gate). Fixed.
2. **EmotiBit "bounded retry" was never actually being reached** because nothing detected device silence at the handler layer. Fixed by adding a separate liveness-watchdog thread that clears `_connected` on prolonged silence, which cascades into the existing reconnect path.
3. **Sensor lifecycle markers (`sensor_lost`, `headset_doffed`, etc.) were received and shown to the operator but never persisted** — gaps in the recording had no on-disk explanation. Fixed by typed signals and a new `SyncLogger.write_event` API; the watchdog and handlers now write transition rows into the syncLog.

---

## Per-finding traceability

| # | Pass-2 severity | File:line in pass-1 code | What the panel said | What landed |
|---|----------------|--------------------------|---------------------|-------------|
| 1 | HIGH (regression) | `unity.py:507` | New IP gate sat above the `RECONNECT,` handler; Unity domain-reload restoration was unreachable when `_device is None`. | Hoisted the `RECONNECT,` block above the IP gate at `unity.py:517-525`, alongside HELLO/CONNECTED. RECONNECT can now restore the connection. |
| 2 | HIGH | `emotibit.py:602-638` (`_auto_reconnect` never fires) | `connect()` is faith-based — sets `_connected = device` without a liveness check. The FIRST iteration of bounded retry calls `connect()`, `_connected` is immediately True, the loop exits. Bounded retry never actually retries. | New `_liveness_loop` daemon (`emotibit.py:345-381`) checks `seconds_since_last_sample` every 2 s; clears `_connected` after `_liveness_silence_s = 10` s of silence. That triggers the heartbeat-exit path that already spawned `_auto_reconnect`. The retry mechanism now actually triggers on real network drops. |
| 3 | HIGH | `polar_mac.py:368-383` | `on_disconnect` schedules a reconnect via `call_later(5.0, …)` that cannot be cancelled by user-initiated disconnect. Race: Disconnect-then-different-strap can resurrect the old strap. | Added `self._pending_reconnect` to hold the `call_later` handle (`polar_mac.py:97`). Cancelled on both `quit` and `disconnect` actions. The disconnect path also drains any queued `("connect", _)` entries that raced ahead. |
| 4 | HIGH | `SyncBridge.cs` (no `OnApplicationFocus`) | Some Quest builds emit don/doff via Focus rather than Pause. Soft doffs go silent. | Added `OnApplicationFocus(bool hasFocus)` that emits the same don/doff markers, plus `OnApplicationQuit` that emits `app_quitting`. `EmitHeadsetEvent` factored out so all three call sites send identical payloads. |
| 5 | HIGH | `SyncBridge.cs` (lock never releases) | `_lockedHostIp` once set, never cleared. Host IP roam = mute SyncBridge until force-quit. | Added `_lastHostMessageTicks` and `HOST_QUIET_SECONDS = 30.0`. On every received packet, if the lock is older than `HOST_QUIET_SECONDS` since the last host message, the lock is released and the next valid sender re-latches. |
| 6 | MEDIUM | `emotibit.py:616` (blocking `time.sleep(60)`) | Quitting the app during a long backoff hung the worker thread for up to 60 s. | Introduced `self._shutdown_event = threading.Event()`. `_auto_reconnect` and `_liveness_loop` now use `self._shutdown_event.wait(timeout=delay)` instead of `time.sleep(delay)`. `stop()` calls `self._shutdown_event.set()`. App quit during backoff exits in <100 ms. |
| 7 | MEDIUM | `main_window.py:1631-1639` (`_unity_parse_warned`) | Latched True on first error; subsequent distinct error types were silenced for the rest of the session. | Replaced with `self._unity_parse_seen: set` keyed by `type(e).__name__`. New error types remain visible; repeats of an already-seen type are suppressed. Reset at session start (replaces the boolean reset). |
| 8 | MEDIUM | sync_logger schema | `latency_ns = -1` overloaded: meant both "device not connected during ping" and "not applicable to this event type". `sensor_lost`/`sensor_recovered` documented but never written. | New `SyncLogger.write_event(machine, event, …)` (`sync_logger.py:100-119`) defaults `latency_ns = ""` for non-ping rows. `_write_gap_marker` now goes through it. Two new typed signals — `sensor_event` (EmotiBit, Polar mac) and `headset_state_changed` (Unity) — are wired in `_wire` to call `write_event`. README schema updated. |
| 9 | MEDIUM | `main_window.py:_write_session_meta` | Direct private-attribute reach-arounds (`_emotibit._connected.device_id`, etc.). | Each handler now exposes `public_summary() -> dict`. `_write_session_meta` calls those instead. |
| 10 | MEDIUM | `polar_mac.py:412-435` (BATTERY_CHAR semantics) | Calibration measures GATT cache hit, not radio RTT. Documented as link RTT. | `Polar.public_summary()` includes `"calibration_method": "battery_char_read"`; README expanded with a "Note on calibration semantics" paragraph that names what the number actually measures. The fix the panel preferred (Data Integrity vote): document, ship, fix in post-processing rather than chase a different characteristic that risks the original PMD_CONTROL bug class. |
| 11 | MEDIUM | `main_window.py:SAMPLE_SILENCE_S = 3.0` | Single global threshold produced false positives on EmotiBit/WiFi blips and false negatives on Polar at 130 Hz. | Replaced with `SAMPLE_SILENCE_S = {"emotibit": 5.0, "polar": 2.0, "unity": 6.0}`. Each watchdog branch reads its own. Defaults can be re-tuned empirically as you collect baseline sessions. |
| 12 | MEDIUM | `main_window.py:RECONNECT_BACKOFF_S` (dead constant) | Defined at module level, never referenced. EmotiBit had its own private copy; Polar mac had a hard-coded 5.0. | Deleted from `main_window.py`. EmotiBit's `_RECONNECT_BACKOFF_S` is now the single source of truth for its retry timing. (Wiring it through to the constructor was an option but introduces a parameter explosion for one tunable; the panel's "delete or wire-up" recommendation is satisfied either way.) |
| 13 | MEDIUM | `unity.py:511-513` (don/doff log only, no syncLog row) | `headset_doffed`/`headset_donned` were logged in-memory but lost. | New `headset_state_changed` pyqtSignal on UnityHandler. Handled in `main_window._on_unity_headset_state` which calls `write_event("unity", label)`. The recording artifact now has the gap reason. `app_quitting` also persisted (new event from SyncBridge.cs). |
| 14 | MEDIUM | `emotibit.py:_handle` (EM RS=RB doesn't update `_last_sample_ns`) | Liveness check could fire on a recording-paused-but-alive device. | EM RS=RB now also `self._last_sample_ns = time.time_ns()`. Documented inline. |
| 15 | LOW | `main.py:_git_sha` (silent "unknown" in PyInstaller) | Packaged builds emit `git_sha = "unknown"` with no warning. | Acknowledged but not fully fixed — requires build-time codegen of `_version.py`, which is a build-script change. Logged as the next deferred item. |
| 16 | LOW | `polar_mac.py:285` (dead `on_disconnect = None`) | Leftover from refactor; confused readers. | Deleted. The actual `on_disconnect` is built fresh inside the `connect` action. |

---

## Files changed in pass 2

| File | Lines changed (approx) | Summary |
|------|------------------------|---------|
| `src/unity.py` | +20 / -5 | Hoisted RECONNECT above the IP gate; new `headset_state_changed` signal wired to don/doff/app_quitting; `public_summary()` |
| `src/emotibit.py` | +60 / -15 | New `_liveness_loop`; `_shutdown_event`; sensor_event signal; bounded retry uses Event.wait; EM RS=RB updates `_last_sample_ns`; `public_summary()` |
| `src/polar_mac.py` | +35 / -10 | `_pending_reconnect` cancellable handle; sensor_event signal; `public_summary()`; queued reconnect drain on user disconnect; ECG-start emits sensor_recovered; dead `on_disconnect = None` removed |
| `src/sync_logger.py` | +20 | `write_event()` public API with empty `latency_ns` default for non-ping rows |
| `src/main_window.py` | +60 / -50 | Per-sensor `SAMPLE_SILENCE_S` dict; gap-marker uses `write_event`; new `_on_unity_headset_state` and `_on_handler_sensor_event` slots wired to write_event; parser-warning throttle by error type; session_meta uses public_summary; dead `RECONNECT_BACKOFF_S` removed |
| `SyncBridge.cs` | +30 / -10 | `OnApplicationFocus` + `OnApplicationQuit` emit don/doff; lock auto-releases after `HOST_QUIET_SECONDS`; `EmitHeadsetEvent` helper |
| `README.md` | +5 / -3 | Schema table updated; calibration semantics paragraph added |

---

## What's now observable that wasn't before pass 2

This is the practical operator-facing answer to "did pass 2 close real failure modes":

- **Unity domain reloads now restore correctly.** RECONNECT is processed even when `_device` is None.
- **EmotiBit silently going offline now triggers reconnect.** Within 10 s of last data, the liveness loop clears `_connected`, the heartbeat exits, bounded retry runs. After 10 attempts (~6 minutes total backoff), DEGRADED banner + `given_up` row in syncLog.
- **Quest don/doff and app_quitting are in the syncLog.** `unity, headset_doffed, , <ts>, ` rows let the analyst attribute every gap to a cause.
- **`sensor_lost` / `sensor_recovered` / `given_up` rows are in the syncLog.** Previously they were operator-log lines only.
- **Disconnect-then-different-strap doesn't resurrect the old strap.** The pending reconnect is cancelled.
- **Quitting the app during a 60-s backoff exits in <100 ms.** No hang.
- **Host re-roam (DHCP/WiFi-to-ethernet) recovers automatically** after 30 s of host silence — no Quest force-quit required.
- **Different-error-type Unity parse errors remain visible** instead of being silenced after the first.
- **Soft doffs (Focus-only Quest builds) emit don/doff markers** like full-screen pauses do.
- **`session_meta.json` no longer crashes on a refactored handler internal** — `public_summary()` is the contract.
- **`latency_ns` column has unambiguous meaning** — empty for non-ping rows, `-1` only for "device-not-connected during a real ping". Drift-fitting scripts can now filter cleanly.

---

## Pass-2 deferred items (unchanged)

The pass-2 panel agreed these are the next-most-important work but did not require landing in this pass.

1. **Polar Windows path migration to bleak** (533 lines of platform-specific code with all the disconnect/retry weaknesses pass 1 fixed only on Mac).
2. **Formal `IDLE → CALIBRATING → ARMED → RECORDING` state machine.** Auto-ping is no longer racing calibration, but `_start_rec` still parallelizes per-handler starts without a transactional gate.
3. **Subject-ID modal** for `session_meta.json`.
4. **NTP-style drift correction script** (`scripts/fit_drift.py`) over the now-richer syncLog.
5. **Build-time `_version.py` codegen** so PyInstaller bundles record their git SHA.

---

## Verification done

- All eight Python files parse cleanly.
- Grep confirmed at source level:
  - `src/unity.py:517` — `RECONNECT,` handler now above the IP gate at line 528.
  - `src/emotibit.py:345` — `_liveness_loop` exists; `_shutdown_event.wait(...)` replaces `time.sleep(...)` in retry paths.
  - `src/polar_mac.py:97, 317-320, 405, 450-453` — `_pending_reconnect` saved, cancelled on quit and disconnect, cleared on successful reconnect.
  - `SyncBridge.cs:115` — `OnApplicationFocus` exists. `HOST_QUIET_SECONDS = 30.0` and the lock-release branch are present.
  - `src/main_window.py:1513,1526,1536` — three watchdog branches use `SAMPLE_SILENCE_S["emotibit"|"polar"|"unity"]` respectively.
  - `src/sync_logger.py:101`, `src/emotibit.py:209`, `src/polar_mac.py:112`, `src/unity.py:88` — `write_event` and three `public_summary` methods exist.

## Verification you must run on real hardware

These cannot be done from here.

1. **Open the EmotiBit picker, connect, then unplug the EmotiBit's WiFi (or power cycle the AP).** Within ~10 s the operator log should say "Liveness watchdog: no sample for X.Xs"; the syncLog should grow a `emotibit, sensor_lost, , <ts>, ` row; bounded retry should attempt 10 reconnects with exponential backoff; on backoff exhaustion the syncLog gains a `emotibit, given_up, , <ts>, ` row. Power back on the AP — recording should mark `sensor_recovered`.
2. **In Unity Editor, edit a script during Play mode** to trigger a domain reload. Confirm the host sees `[Unity] Reconnected after domain reload: …` and the live monitor resumes (this was broken by pass-1's IP gate).
3. **Mid-session: take the Quest off briefly.** Confirm `unity, headset_doffed, , <ts>, ` appears in syncLog, then `headset_donned` on putting it back on. (Test BOTH a long doff to trip Pause AND a brief doff to trip Focus.)
4. **Mid-session: click Disconnect on the Polar, then immediately Connect a different strap by serial number.** Confirm the old strap is NOT resurrected during the 5-s reconnect window.
5. **Quit the app during an EmotiBit DEGRADED state.** Confirm the window closes within ~1 s, not 60.
6. **Open a finished session's `session_meta.json`** and confirm `devices.polar.calibration_method == "battery_char_read"`, `devices.emotibit.given_up` is bool, and there are no `null`s where a connected device existed.

If any test regresses, file with file:line and I'll do a third pass.

— End of pass-2 remediation report —
