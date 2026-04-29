# LSL Repository — Second-Pass Audit (New Panel)

**Audited at:** 2026-04-28
**Repo HEAD:** `LabStreamLayer/` (post-Phase-1-through-7 remediation)
**Companion to:** `PANEL_AUDIT_REPORT.md` (first pass) and `PANEL_REMEDIATION_REPORT.md` (the fix log)
**Standard:** ruthless, specific, no hedging, intermediate-tier bugs only.

A different panel reads the post-fix code. The remit is twofold:
1. Verify the four CRITICAL findings from pass 1 are mechanically gone.
2. Find what the original panel missed, what the fixes introduced, and what's still smoking.

---

## The new panel

Different lenses on purpose — six new specialists, no overlap with the original lineup.

### 1. Senior Python Concurrency Engineer
You think in event loops, threads, GILs, `asyncio.Queue` lifecycles, `threading.Event` correctness, and the four-way braid of bleak's asyncio loop, Qt's main loop, the EmotiBit UDP receive thread, and CSV file I/O. You ask: *can a queued task fire after the user said "stop"? Does this `time.sleep(N)` block shutdown for N seconds? Is this private attribute mutated from a thread that doesn't hold the lock?*

### 2. BLE / Embedded Firmware Engineer
You wrote firmware. You know what GATT characteristics actually do at the radio layer — that `BATTERY_CHAR` reads on a Polar H10 may serve from on-device cache and bypass the BLE link, that PMD_CONTROL writes during streaming compete for the same connection-event budget. You ask: *what does this BLE call actually measure? Is the calibration probe touching the radio, or just the SoC's RAM?*

### 3. SRE / Observability Engineer
You've debugged a service at 3 a.m. with the only artifact being a logfile. You think in cardinality, alerting fatigue, what's exposed via metrics versus buried in logs, and the difference between a warning that means "you must act" and one that means "FYI". You ask: *what shows up if I open a finished session folder cold? What pages me when it matters? What spams my log uselessly?*

### 4. Data Integrity / Forensics Engineer
You build the analysis pipeline that runs over thousands of session folders. You care about schema stability, sentinel values, parseability six months from now, and whether the CSV self-describes. You ask: *can my pandas reader handle every row this code might emit? Are sentinel values consistent? Does the meta file have everything I need to reproduce the alignment?*

### 5. Quest / Android Platform Engineer
You ship apps to Meta Quest. You know the OVR / XR SDK lifecycle: don/doff fires `OnApplicationPause` on most builds and `OnApplicationFocus` on others; Quest backgrounds the app aggressively under battery saver; UDP sockets survive pause but the main thread does not. You ask: *what events does the OS actually deliver, and does this code handle both don/doff signal flavours?*

### 6. Adversarial QA / Chaos Engineer
You actively try to break the system. Restart the LSL host on a different IP. Disconnect the network mid-calibration. Run two simultaneous sessions in the same lab. Click Start twice in quick succession. You ask: *what's the failure mode the fix author didn't think to test?*

Each speaks in their own voice. No collapse to consensus.

---

## Validation: are the pass-1 CRITICALs actually fixed?

The new panel agrees, in writing:

- **#1 EmotiBit calibration thread death** — fixed. `_rtt_buffer`, `_continuous_calib_active`, `_session_latency_ns`, `_last_sample_ns`, `_given_up` are all in `__init__` (`emotibit.py:162-168`). The dead post-`return` block in the property is removed. **CONFIRMED.**
- **#2 `add_manual_device` AttributeError** — fixed. Alias added at `emotibit.py:280-282`. The Adversarial QA reviewer notes that the original panel only flagged this for the EmotiBit dialog; the same name doesn't appear in `polar_mac.py`/`unity.py`, so no parallel bug to chase. **CONFIRMED.**
- **#3 SyncBridge ACK format** — fixed. `SyncBridge.cs:149` emits `$"ACK:{msg}:{ns}"`. `unity.py`'s gate (`unity_ns > 0`) now passes. **CONFIRMED.**
- **#4 README ↔ code drift** — fixed. README and code agree on `~/LabStreamLayer_Recordings/` and `syncLog_<ts>.csv`. **CONFIRMED.**

The four ship-stoppers are off the board.

The panel now turns to what's still wrong.

---

## Findings

```
SEVERITY: HIGH
CATEGORY:  data-loss
REVIEWER:  Adversarial QA
FILE:      src/unity.py:507-527
WHAT:      The new "must come from connected device IP" gate at line 507 (`if self._device is None: return`) sits *above* the `RECONNECT,` handler at line 518. The RECONNECT path exists specifically for the case where Unity domain-reloads and needs to *restore* a connection — i.e., the host might already have `_device = None` while Unity asks to reconnect. This is now unreachable when `_device is None`. **REGRESSION introduced by the pass-1 fix.**
FIELD FAILURE: Subject is in headset, Unity Editor session domain-reloads (asset import, code recompile while in Play mode — common in development), Unity emits `RECONNECT,<name>`. Host has cleared `_device` for some reason (e.g. operator clicked Disconnect by mistake, or Unity sent DISCONNECT and then RECONNECT). The RECONNECT is dropped. Live monitor stays dark. The operator must manually re-pick the Unity device.
EVIDENCE:
    if self._device is None:
        return  # unconnected — drop everything else      <-- gate
    if src_ip != self._device.ip:
        return
    ...
    if msg.startswith("RECONNECT,"):                      <-- now unreachable when _device is None
        ...
        self._device = dev
FIX:       Hoist the RECONNECT handler ABOVE the gate, OR allow RECONNECT specifically through the gate. Recommended: handle RECONNECT alongside HELLO/CONNECTED at lines 476-500 since all three are connection-restoration messages, not data messages.
```

```
SEVERITY: HIGH
CATEGORY:  disconnect
REVIEWER:  SRE
FILE:      src/emotibit.py:602-638  (`_auto_reconnect`)  +  emotibit.py:282-298 (`connect`)
WHAT:      `_auto_reconnect` is bounded with backoff — good — but `connect()` is faith-based: it sets `self._connected = device` and spawns a heartbeat thread without any actual liveness check. So the FIRST retry inside `_auto_reconnect` calls `connect()`, which immediately sets `_connected = device`, which makes the next iteration's `if self._connected: break` short-circuit out. The bounded retry never actually retries.
FIELD FAILURE: The EmotiBit goes offline (battery brownout, WiFi AP swap). The heartbeat keeps blindly broadcasting EC packets to a ghost IP. There is no event that ever clears `_connected`, because nothing detects the silence at the handler layer. `_auto_reconnect` is only triggered by the heartbeat exiting (line 590), which happens only when `_connected` is None — which is only set by explicit `disconnect()`. So in practice `_auto_reconnect` runs after USER-initiated disconnect, not after device-initiated drop. The "bounded retry on disconnect" feature is essentially unused.
EVIDENCE:
    # _heartbeat_loop:
    while self._running and self._connected:   # ← blindly true, no liveness check
        self._udp_send(self._pkt("EC", payload, data_len=4), self._connected.ip)
        ...
    # _auto_reconnect runs only when this exits, i.e. only when _connected is None.
FIX:       Add a real silence-detector to the EmotiBit handler. Inside `_heartbeat_loop`, after sending EC, check `self.seconds_since_last_sample` (already exists). If > N seconds (say 10), set `_connected = None` and break. That triggers the existing auto-reconnect path. The watchdog at the UI layer surfaces the gap separately. Two independent layers, neither relies on the other being correct.
```

```
SEVERITY: HIGH
CATEGORY:  race
REVIEWER:  Concurrency
FILE:      src/polar_mac.py:368-383  (`on_disconnect` reconnect schedule)  +  polar_mac.py:300-307 (`quit` / `disconnect`)
WHAT:      `on_disconnect` schedules a reconnect via `self._loop.call_later(5.0, lambda: ...)` which puts `("connect", dev)` onto `self._cmd_queue`. If the user clicks Disconnect *during* that 5-second window, the manual disconnect runs (drains client) but the queued `connect` entry is still pending. It fires 5 seconds later and reconnects to a device the user just told us not to.
FIELD FAILURE: Operator decides to swap straps mid-session. They click Disconnect on the current strap. 5 seconds after the bleak disconnect callback fired (which is asynchronous — they might not even know it fired), the reconnect lambda runs and tries to reconnect to the OLD strap. The new strap pairing fails or races with the resurrected old one.
EVIDENCE:
    self._loop.call_later(5.0, lambda d=dev: self._loop.create_task(
        self._cmd_queue.put(("connect", d))
    ))
    # No way to cancel this when user-initiated disconnect arrives.
FIX:       Save the `call_later` handle (`_pending_reconnect`); on user-initiated `disconnect`, cancel it: `if self._pending_reconnect: self._pending_reconnect.cancel()`. Also: drain the cmd queue of any pending `("connect", _)` entries before processing user disconnect — or better, use a generation counter on the queue so old entries become no-ops.
```

```
SEVERITY: HIGH
CATEGORY:  reproducibility
REVIEWER:  Quest Platform
FILE:      SyncBridge.cs (post-fix, OnApplicationPause)
WHAT:      `OnApplicationPause(bool paused)` catches don/doff on most Quest builds. But on Quest 2/3 with newer Meta XR SDK builds, the don/doff signal can fire as `OnApplicationFocus(bool focus)` instead — particularly for short doffs (under the proximity-sensor "soft pause" threshold). The current handler only listens for Pause.
FIELD FAILURE: A 5-second mid-session doff (subject lifts the headset to scratch their face) doesn't trigger Pause on some Quest builds; it triggers Focus. No marker is written. The data has a 5-s gap with no annotation. Cog-Sci downstream sees clean data and doesn't know the subject was uninstrumented for that interval.
EVIDENCE: Only `void OnApplicationPause(bool paused)` is implemented; no `OnApplicationFocus` companion.
FIX:       Add a parallel `void OnApplicationFocus(bool hasFocus)` that emits the same `headset_doffed`/`headset_donned` markers. Idempotent on the receive side already (it just logs), so duplicate signals are harmless. While there: emit `app_quitting` from `OnApplicationQuit` so the host knows when the Unity side died vs went to background.
```

```
SEVERITY: HIGH
CATEGORY:  retry
REVIEWER:  Adversarial QA
FILE:      SyncBridge.cs:35-42 + Listen()
WHAT:      `_lockedHostIp` is set on first DISCOVER/CONNECT/PING, but it is NEVER released. If the LSL host's machine reboots, gets a new DHCP lease, or roams to a different SSID, SyncBridge will keep ignoring all messages from the new IP forever — until the Quest app itself is force-quit. There is no liveness expiry on the lock.
FIELD FAILURE: Lab WiFi flaps. Host machine drops to wired ethernet, gets a new IP. SyncBridge is permanently mute to it. Operator's only fix is to take off the Quest, force-quit, restart — disrupting the subject.
EVIDENCE: `_lockedHostIp = srcIp;` set once; no setter elsewhere.
FIX:       Track `_lastHostMessageTimeUnixMs`; if no message has been received from `_lockedHostIp` for >N seconds (say 30), release the lock and re-latch on the next valid sender. Or: release the lock on `OnApplicationFocus(true)` after a long pause (the subject probably moved rooms).
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Concurrency
FILE:      src/emotibit.py:616  (`time.sleep(delay)` inside `_auto_reconnect`)
WHAT:      The bounded-retry loop blocks on `time.sleep(60)` for the long backoffs. If `self._running` becomes False during that sleep (operator closes the app), the thread holds the interpreter for up to 60 s before honouring shutdown.
FIELD FAILURE: Operator quits the app at the end of a session that ended in DEGRADED state. The window appears to hang for 60 s before closing. Operator force-quits, possibly losing buffered writes.
EVIDENCE: Plain `time.sleep(delay)`; no Event for shutdown signalling.
FIX:       Use a `threading.Event` initialised once in `__init__` (`self._shutdown_event = threading.Event()`); replace `time.sleep(delay)` with `if self._shutdown_event.wait(delay): return`. Set the event in `stop()` so all sleeping retry loops wake immediately.
```

```
SEVERITY: MEDIUM
CATEGORY:  operator-feedback
REVIEWER:  SRE
FILE:      src/main_window.py:1631-1639  (`_on_unity_data` — `_unity_parse_warned` latch)
WHAT:      Once `_unity_parse_warned = True`, no further parse errors are logged for the whole session. This is fix-pass intent (avoid log spam) but the latch is too sticky. If the Unity packet shape changes mid-session (someone toggles a flag in the Unity scene), the new parse failures are invisible.
FIELD FAILURE: Long sessions where Unity adds/removes channels in response to experiment state. The first error gets logged; subsequent distinct errors are silent. Operator sees graphs go flat for one channel, has no way to investigate.
EVIDENCE:
    if not getattr(self, "_unity_parse_warned", False):
        self._unity_parse_warned = True
        self._log(f"[Unity] DATA parse error (further errors suppressed this session): {e}")
FIX:       Throttle by message text rather than by latch: `self._unity_parse_seen = {}; key = type(e).__name__; if key not in self._unity_parse_seen: ...`. Or rate-limit: at most one log per N seconds per error type. The current "log once and forever shut up" is the right shape but the right key is "kind of error" not "did we ever log anything".
```

```
SEVERITY: MEDIUM
CATEGORY:  reproducibility
REVIEWER:  Data Integrity
FILE:      src/main_window.py:_write_gap_marker  +  src/sync_logger.py — overall schema
WHAT:      Schema inconsistency in syncLog rows. The `latency_ns` column carries three different meanings depending on `event`:
           - `ping_sent`/`ping_received`: real latency or `-1` if device not connected.
           - `sensor_silent`/`sensor_resumed` (NEW): `-1` (sentinel — but here `-1` means "not applicable", not "device not connected"; the device is in fact what we're talking about).
           - `sensor_lost`/`sensor_recovered` (mentioned in remediation report but not actually emitted into the syncLog by emotibit.py — `_auto_reconnect` only logs to the operator log, not the syncLog).
FIELD FAILURE: Analysis pipeline reads `syncLog_*.csv` and aggregates `latency_ns` to compute clock drift. It naively excludes `-1` rows but now sees a mix of "device not connected" and "not applicable" both as `-1`. Drift estimates are biased by gap-marker rows. Worse: `sensor_lost`/`sensor_recovered` events are documented in the README but the code never writes them — silent documentation drift.
EVIDENCE: README:60+ documents `sensor_lost`/`sensor_recovered` as syncLog events; `emotibit.py:611,621,635` only emit them via `log_message` to the operator log, never via the SyncLogger.
FIX:       (1) Use empty (`""`) for `latency_ns` on sensor events to distinguish from `-1`. (2) Actually emit `sensor_lost`/`sensor_recovered` rows to the syncLog from emotibit.py — the watchdog only catches silence-on-the-wire, not "we gave up reconnecting", which deserves its own row. (3) Add a `SyncLogger.write_event(machine, event, **kw)` public method so the watchdog and handlers stop reaching into `_writer` directly (still happening at `main_window.py:_write_gap_marker`).
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Data Integrity
FILE:      src/main_window.py:_write_session_meta  (private-attribute reach-arounds)
WHAT:      The pass-1 audit explicitly flagged `self._unity._session_latency_ns` reach-arounds. The pass-1 fix introduced `effective_latency_ns` for that one case but `_write_session_meta` reaches into `_emotibit._connected.device_id` (line ~1503), `_polar._connected.display_name`, etc. The pattern was not eliminated, only relocated.
FIELD FAILURE: When EmotiBitHandler's internal connection model is refactored (likely in any future state-machine work), session_meta.json silently fails to write because of an AttributeError caught by the wrapping `except Exception as e` in `_start_rec`. The session continues without metadata.
EVIDENCE: `self._emotibit._connected.device_id if self._emotibit._connected else None`
FIX:       Add a `public_summary() -> dict` method to each handler that returns `{"ip": ..., "device_id": ..., "session_latency_ns": ...}` honestly. Call that in `_write_session_meta`. Stops the reach-around pattern at its source.
```

```
SEVERITY: MEDIUM
CATEGORY:  timing
REVIEWER:  BLE/Firmware
FILE:      src/polar_mac.py:412-435  (calibrate_for_recording — BATTERY_CHAR variant)
WHAT:      The pass-1 fix changed Polar calibration from PMD_CONTROL writes to `read_gatt_char(BATTERY_CHAR)`. Better — but: BLE characteristic reads on the H10 are served from the device's GATT cache, not from a fresh radio round-trip, when the cache is hot. The H10 firmware caches battery for several seconds. So the measured RTT is not "BLE link round-trip" but "BLE cache lookup round-trip" — typically <5 ms even when the actual radio is congested.
FIELD FAILURE: The "calibrated" Polar latency is artificially low — it measures the GATT cache hit, not the data path. Post-hoc alignment using `polar_latency_ns` underestimates the true clock offset. ECG samples are slightly miscorrelated with markers. Probably <5 ms in practice but documented as the actual link RTT and used as such.
EVIDENCE: `await client.read_gatt_char(BATTERY_CHAR)` — bleak does no cache busting. The H10 BATTERY_CHAR's notify rate is ~1 Hz, value cached between updates.
FIX:       Acknowledge it in the README and `session_meta.json`: rename the field `polar_calibration_method = "battery_char_read"` and document what it measures. For better link-RTT measurement, use a `write_gatt_char(..., response=True)` to a non-control characteristic that requires a radio round-trip (response-required writes always do). Or: derive latency post-hoc from the per-ECG-sample timestamp drift — the data is there.
```

```
SEVERITY: MEDIUM
CATEGORY:  operator-feedback
REVIEWER:  SRE
FILE:      src/main_window.py:SAMPLE_SILENCE_S = 3.0
WHAT:      Hard-coded silence threshold 3.0 s applies to all sensors. EmotiBit over WiFi can blip 2-3 s under access-point load with no actual data loss (UDP just queues). At 3.0 s the watchdog will fire spurious `sensor_silent` rows, then `sensor_resumed` 1 s later. SyncLog gets noisy with transient gap markers.
FIELD FAILURE: A clean session in a noisy WiFi lab produces 5-15 spurious silent/resumed pairs. Real disconnects get lost in the noise. Cog-Sci reviewer (from pass 1) is going to look at a syncLog full of false positives and stop trusting the gap markers.
EVIDENCE: Single global constant, no per-sensor override.
FIX:       Make per-sensor: `SAMPLE_SILENCE_S = {"emotibit": 5.0, "polar": 2.0, "unity": 6.0}`. Polar at 130 Hz can't legitimately be silent for 2 s; EmotiBit over WiFi can. Tune empirically against real session traces.
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Concurrency
FILE:      src/main_window.py:RECONNECT_BACKOFF_S
WHAT:      The pass-1 fix declared `RECONNECT_BACKOFF_S = (5, 10, 20, 40, 60, 60, 60, 60, 60, 60)` as a module-level tunable in main_window.py. It is never referenced. emotibit.py defines its own `_RECONNECT_BACKOFF_S` privately. Polar mac uses a hard-coded `5.0` in `call_later`. Three different sources of truth for "reconnect timing", one of them dead.
FIELD FAILURE: Future maintainer changes the tunable in main_window.py expecting it to take effect; nothing changes; they conclude their understanding of the codebase is wrong and stop trusting it.
EVIDENCE: `grep RECONNECT_BACKOFF_S src/main_window.py` shows only the definition.
FIX:       Either (a) wire main_window's constant into `EmotiBitHandler.__init__` as a parameter and have polar_mac accept the same, OR (b) remove the dead constant from main_window.py. Don't ship two declarations.
```

```
SEVERITY: MEDIUM
CATEGORY:  data-loss
REVIEWER:  Quest Platform
FILE:      SyncBridge.cs:OnApplicationPause + main_window.py — host-side handling
WHAT:      SyncBridge sends `headset_doffed` / `headset_donned` to the host. unity.py:511-513 logs them. But they are NOT written into the syncLog or any other persistent record. The operator-visible log is appended in-memory only and is not part of the recording artifact.
FIELD FAILURE: Post-hoc analysis cannot tell whether a 30-s gap in head-tracking data was a network problem or a doff. The information was received and shown to the operator — and then thrown away.
EVIDENCE: `unity.py:511-513` calls `self._try_emit(self.log_message, ...)` only.
FIX:       In main_window.py: connect a slot to a new `unity.headset_state_changed` pyqtSignal (str). The slot writes a row into the syncLog: `self._sync_logger._writer.writerow(["unity", "headset_doffed", "", time.time_ns(), -1])`. (Or — preferred — through the `SyncLogger.write_event` API recommended in finding above.)
```

```
SEVERITY: LOW
CATEGORY:  hygiene
REVIEWER:  Data Integrity
FILE:      src/main.py:_git_sha
WHAT:      `_git_sha()` runs `git rev-parse` from the script directory. When the app is bundled via PyInstaller (the documented install path — README §1), `__file__` is `sys._MEIPASS` which has no `.git`. The function silently returns `"unknown"`. The operator running a packaged build has no warning that they don't actually know which version they're running.
FIELD FAILURE: Six-month-old session folder, `session_meta.json` reads `"git_sha": "unknown"`. Cannot reproduce. Cannot tell which Python version, which fix-pass, or which feature flag was active.
EVIDENCE: `getattr(sys, 'frozen', False)` is True under PyInstaller; `_git_sha()` doesn't check for it.
FIX:       At build time (`scripts/build_install_app.sh`), capture the git SHA into a generated `_version.py` that is bundled. At runtime, prefer that file over the live `git` call. Print a single warning at startup if `_version.py` is missing AND `git` returns `unknown`.
```

```
SEVERITY: LOW
CATEGORY:  hygiene
REVIEWER:  Adversarial QA
FILE:      src/polar_mac.py:285  ("on_disconnect = None  # bound just before client = BleakClient")
WHAT:      Dead `None` assignment at module scope above the connect handler — a leftover from refactor. The actual `on_disconnect` is built inside the `elif action == "connect":` block. The outer `None` shadows nothing and confuses readers.
FIX:       Delete the line.
```

```
SEVERITY: LOW
CATEGORY:  hygiene
REVIEWER:  Concurrency
FILE:      src/emotibit.py:_handle  (EM RS=RB doesn't update _last_sample_ns)
WHAT:      `_last_sample_ns` is updated in the PR (PPG) and HR tag handlers but not in EM (status). If a device is alive but only sending status updates (e.g. recording paused but heartbeat ongoing), the silence watchdog fires false positives.
FIX:       In the EM handler at `_parse_line`, when `RS=RB` is observed and `_is_writing` becomes True, also `self._last_sample_ns = time.time_ns()`. Or document that _last_sample_ns specifically means "data sample, not heartbeat" and tune watchdog thresholds accordingly.
```

---

## Reaffirmed deferred items from pass 1

The original panel deferred these. The new panel agrees they should not have been folded into the pass-1 fix sweep but flags them as the next-most-important work:

1. **Polar Windows path (`polar.py` + `polar_subprocess.py`) still has every disconnect / retry weakness from pass 1.** None of the pass-1 fixes touched the Windows path. If the lab uses any Windows host, all of items #6, #7, #19 from the original audit are still live. The new Polar reviewer recommends timeboxing the bleak migration this week.

2. **No formal `IDLE → CALIBRATING → ARMED → RECORDING` state machine.** Auto-ping has been moved off the calibration window (good), but `_start_rec` still parallelizes Polar/EmotiBit/Unity start without a transactional gate. Concurrency reviewer notes this is the single remaining structural risk.

3. **No subject-ID modal.** `session_meta.json` captures git SHA and tunables but no subject metadata. Cog-Sci concerns from pass 1 are unchanged.

4. **No NTP-style drift correction.** The data is now in the syncLog to fit one offline (per-ping latency × time-since-start). Worth a small `scripts/fit_drift.py` ship.

---

## TOP 10 SHIP-STOPPERS (post-fix)

Re-ranked by likelihood of recurrence in the next session, given everything that has and hasn't been fixed.

1. **Unity RECONNECT path is now unreachable when `_device is None`** — regression introduced by the new IP gate. Highest because it actively breaks a working feature.
2. **EmotiBit `_auto_reconnect` is bounded but never actually triggers** under real network drops, because `connect()` is faith-based and the heartbeat has no liveness check. The fix author shipped a guard against the wrong failure mode.
3. **Polar `on_disconnect` reconnect schedule cannot be cancelled by user-initiated disconnect** — race window of 5 s in which Disconnect-then-Reconnect-to-different-strap can resurrect the old strap.
4. **`headset_doffed`/`headset_donned` are received but not persisted to syncLog** — the gap reason is shown to the operator and then forgotten.
5. **SyncBridge `_lockedHostIp` never releases** — host-IP roam requires Quest force-quit.
6. **OnApplicationFocus not handled** — Quest builds that signal don/doff via Focus produce silent gaps.
7. **`sensor_lost`/`sensor_recovered` documented in README but never written into syncLog** — emotibit.py logs only, doesn't emit a row.
8. **`time.sleep(60)` in `_auto_reconnect`** — up to 60-s app-shutdown delay if a long-backoff retry is sleeping at quit time.
9. **SAMPLE_SILENCE_S = 3.0 single-value tuning** — produces noisy gap markers in real WiFi, contaminating the syncLog. Per-sensor tuning needed.
10. **Polar Windows path** — still has all pass-1 disconnect/retry weaknesses; deferred but not addressed.

---

## SILENT-FAILURE INVENTORY (post-fix)

Smaller than pass 1, but not empty.

1. **Heartbeat-without-liveness on EmotiBit.** The handler can be in "connected" state for hours after the device goes offline. Watchdog catches the silence but doesn't trigger reconnect.
2. **`_unity_parse_warned` latches forever within a session.** New parser failures after the first are invisible.
3. **PyInstaller `git_sha = "unknown"` with no warning.** Sessions get logged with unknowable build identity.
4. **Polar `on_disconnect`-scheduled reconnects fire after user disconnect.** Silent because the subsequent reconnect "succeeds" without operator intent.
5. **session_meta.json failure on `_emotibit._connected.device_id` AttributeError** is caught by `_start_rec`'s `except Exception as e` (the `_log` only). The session runs without metadata; the log line is one of hundreds.
6. **`sensor_lost` documented but never emitted to the syncLog.** Operator log shows it; analysis pipeline reading the syncLog never sees it.
7. **BLE GATT cache hit treated as link-RTT.** Polar calibration says it's measuring radio latency; it's measuring SoC RAM latency.

---

## DISAGREEMENT LOG

**On the EmotiBit liveness-detection gap (finding #2).**
The Concurrency reviewer wants the fix in the heartbeat loop (`if seconds_since_last_sample > 10: self._connected = None; break`). The SRE reviewer prefers a separate "liveness" timer that's independent of the heartbeat sender, on the grounds that one thread should not be both producer and watchdog. The Adversarial QA reviewer notes that mixing the two has caused exactly this class of bug before (silent failure of the watchdog because the writer thread crashes). **Panel recommendation:** separate timer. Add a `_liveness_check_loop` daemon thread that runs every 2 s and clears `_connected` after 10 s of silence. The heartbeat thread becomes pure send-side.

**On removing or wiring up `RECONNECT_BACKOFF_S` (finding #12).**
Concurrency reviewer wants it deleted (dead constant is worse than no constant). Data Integrity reviewer wants it wired up so the timing is tunable from one place. Both agree the current state is unacceptable. **Panel recommendation:** wire it up. Pass it as a constructor argument to both handler classes.

**On the silence threshold (finding #11).**
SRE wants per-sensor tuning *now*. Adversarial QA wants empirical thresholds derived from a clean session, not guessed. Data Integrity points out that the threshold appears in `session_meta.json` already (`SAMPLE_SILENCE_S` is one of the tunables written), so post-hoc analysis can compensate even if the live threshold is wrong. **Panel recommendation:** ship per-sensor defaults now (`emotibit: 5.0, polar: 2.0, unity: 6.0`); plan an empirical tune-up next time you have 2-3 baseline sessions to look at.

**On whether the Polar BATTERY_CHAR calibration is good enough (finding #10).**
BLE/Firmware reviewer wants a response-required write to a non-control characteristic to force a radio round-trip. SRE notes this has its own risks (writing to the wrong characteristic during streaming, similar to the original PMD_CONTROL bug). Data Integrity reviewer says: document what the number means, ship as is, and address in post-hoc analysis. **Panel recommendation:** Data Integrity wins — document, ship, fix in post-processing.

---

## What we did NOT find / where the new code is good

In the spirit of not inventing issues:

- The transition-only emission of `sensor_silent`/`sensor_resumed` (only on enter/exit, not every watchdog tick) is exactly the right shape. SRE: "this is the difference between a useful syncLog and a syncLog full of noise."
- Default-arg capture of `device` in the Polar `on_disconnect` rebuild is correct and idiomatic.
- The named-constants block at the top of `main_window.py` (FIRST_PING_DELAY_MS etc.) is the right pattern even with one dead constant. Keep extending it.
- `SyncBridge.cs` source-IP latching on first-DISCOVER is the right shape; just needs a release valve.
- The `effective_latency_ns` property is the right abstraction — extend it everywhere `_session_latency_ns` is read.
- The version banner in `main.py` is a real win for forensics — even with the PyInstaller hole, sessions started from `python3 src/main.py` now self-identify.

---

## Standard reminder

Would we let a graduate student run a 60-subject, 90-minute-per-session study on this code tomorrow with no babysitter?

**Closer to yes — but not yet.** Items #1, #2, and #3 in the Top-10 above are individually small fixes (each <30 lines) and together address the highest-impact remaining failure modes. With those landed, the answer is yes for a Mac-only EmotiBit + Polar + Quest setup. For Windows hosts, the Polar Windows-path debt is unaddressed and the answer remains no.

— End of pass-2 audit —
