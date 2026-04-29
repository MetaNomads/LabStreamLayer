# LSL Repository — Six-Reviewer Panel Audit Report

**Audited at:** 2026-04-28
**Repo HEAD:** `LabStreamLayer/` (LSL pipeline: EmotiBit + Polar H10 + Unity / Meta XR)
**Scope:** `SyncBridge.cs`, `src/main.py`, `src/main_window.py`, `src/sync_logger.py`, `src/emotibit.py`, `src/polar.py` (Windows), `src/polar_mac.py`, `src/polar_subprocess.py`, `src/unity.py`, `README.md`
**Total LOC reviewed:** 4,483

The panel went line by line. The following findings are the meaty middle — the bugs that will silently sink a real subject session — not cosmetic style. Reviewers disagree where they disagree; consensus is not enforced.

---

## Findings

```
SEVERITY: CRITICAL
CATEGORY:  data-loss
REVIEWER:  EmotiBit
FILE:      src/emotibit.py:169-175
WHAT:      Three instance attributes — `_rtt_buffer`, `_continuous_calib_active`, `_session_latency_ns` — are declared inside the `seconds_since_recording_start` property *after* its `return`, so they are NEVER executed. They were copy-pasted into the wrong scope. They are also missing from `__init__`.
FIELD FAILURE: First call to `start_continuous_calibration()` (spawned as a daemon thread on connect, line 280) raises AttributeError on `self._continuous_calib_active` and the thread dies silently. EmotiBit calibration is therefore broken on every run since this code shipped. `_session_latency_ns` is never set; `send_marker` reads it on every ping → AttributeError swallowed by `_ping`'s try/except → every `emotibit_latency_ns` in `syncLog_*.csv` is `-1`. The user has been recording with the EmotiBit clock-sync silently disabled.
EVIDENCE:
    @property
    def seconds_since_recording_start(self) -> float:
        if self._recording_start_ns == 0: return 0.0
        return (time.time_ns() - self._recording_start_ns) / 1e9
        self._rtt_buffer = deque(maxlen=20)      # rolling RTT samples
        self._continuous_calib_active = False
        self._session_latency_ns: int = -1
FIX:       Move those three lines into `__init__`. Add a unit/integration test that asserts `_session_latency_ns` exists after instantiation. While there, add `self._rtt_buffer = deque(maxlen=20)` and friends and re-run a full session — verify `emotibit_latency_ns` is non-negative in the output CSV.
```

```
SEVERITY: CRITICAL
CATEGORY:  data-loss
REVIEWER:  Lead
FILE:      src/main_window.py:526
WHAT:      Calls `self._handler.add_manual_device(ip)` but `EmotiBitHandler` only defines `add_manual()` (emotibit.py:257). AttributeError every time the operator types a manual IP for the EmotiBit.
FIELD FAILURE: When the EmotiBit is on a different subnet (cross-subnet WiFi, exactly the case the dialog text invites), the operator clicks Add → exception silently caught nowhere visible (it propagates out of the slot and Qt logs to stderr only). Operator sees nothing happen and gives up. Session aborts.
EVIDENCE: `dev = self._handler.add_manual_device(ip)`  vs  `def add_manual(self, ip: str)`
FIX:       Rename one to match the other. Add a smoke test that exercises every dialog button — manual IP, Scan, Connect, Disconnect — on a stub handler.
```

```
SEVERITY: CRITICAL
CATEGORY:  reproducibility
REVIEWER:  Cog-Sci
FILE:      SyncBridge.cs:108-112  +  src/unity.py:507-522
WHAT:      Protocol mismatch between Unity and the host. SyncBridge.cs echoes `"ACK:" + msg` (just the ping id, e.g. `ACK:ping_001`). unity.py:521 only emits `unity_ack_received` when the ACK contains `:<unity_ns>` — `unity_ns > 0`. So on the actual `SyncBridge.cs` shipped in this repo, that path NEVER fires.
FIELD FAILURE: The Unity row in `syncLog_*.csv` is never written even when `log_unity = True`. The README and the SyncLogger docstring claim a Unity-clock-aligned row per ping; it does not exist. Post-hoc Unity↔physiology alignment is impossible from the sync log. The experimenter believes they have four-machine synchronization; they have three.
EVIDENCE: `byte[] ack = Encoding.UTF8.GetBytes("ACK:" + msg);` (no unity_ns appended).  `if ping_id.startswith("ping_") and unity_ns > 0:` (gate fails because unity_ns is always 0).
FIX:       Decide the wire format, document it in ONE place, and make both sides match. Recommended: `ACK:<ping_id>:<utc_epoch_ns>`. Update SyncBridge.cs to append `":" + ToUnixNs(DateTime.UtcNow)`. Add a test that round-trips a ping and asserts the Unity row is present.
```

```
SEVERITY: CRITICAL
CATEGORY:  reproducibility
REVIEWER:  Lead
FILE:      README.md:59-79  vs  src/main_window.py:943, src/sync_logger.py:48
WHAT:      Documentation is drifted from the code in two places that matter for finding the data:
           (1) Output dir is `~/SyncBridge_Recordings/` in code, `~/LabStreamLayer_Recordings/` in README.
           (2) Sync-log filename is `syncLog_<session>.csv` in code, `marker_outlog_<session>.csv` in README, with different column names.
FIELD FAILURE: Operator follows the README post-session, can't find the data, panics; or downstream analysis script written against `marker_outlog_*.csv` columns silently misses every file.
EVIDENCE: `self._output_dir = Path.home() / "SyncBridge_Recordings"`  vs  README "Recordings saved to `~/LabStreamLayer_Recordings/`".
FIX:       Pick one name. Update both. Lock down with a test that reads README, parses the path/filename strings, and asserts they match constants in code.
```

```
SEVERITY: HIGH
CATEGORY:  data-loss
REVIEWER:  Unity
FILE:      src/main_window.py:1583-1628
WHAT:      `_on_unity_data` parses Unity DATA packets. Inside the for-loop it references `_last` in the `elif` branch, but `_last` is only assigned in the `if "=" in token` branch. If the first token of the packet has no `=`, NameError on iteration 1. The whole function is wrapped in a bare `except Exception: pass` (`# never crash the UI on a bad packet`).
FIELD FAILURE: Any malformed/edge-case Unity packet silently kills its parsing. The "Unity is streaming data" gate (`has_streaming_data`) does flip True (set in unity.py:479 BEFORE main_window parses), but the live-monitor graphs go flat without explanation. Operator sees "data is flowing" green light + dead Unity graphs and assumes it's a UI bug, presses Start anyway. The data may still be fine, but the *operator-visible signal* is now lying.
EVIDENCE:
    for token in raw.split(","):
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k.strip()] = [float(v)]
            _last = k.strip()
        elif _last:           # ← _last possibly undefined here
            fields[_last].append(float(token))
FIX:       Initialise `_last = None` before the loop. Stop using bare `except Exception: pass` — at minimum log to `self._log` once per session-id with throttling.
```

```
SEVERITY: HIGH
CATEGORY:  retry
REVIEWER:  EmotiBit
FILE:      src/emotibit.py:567-574  (`_auto_reconnect`)
WHAT:      The auto-reconnect loop has no max-attempt count, no backoff, no circuit breaker. It reconnects every 5 s forever as long as `_running` and `_connected is None`.
FIELD FAILURE: If the EmotiBit's WiFi access point goes down for the rest of the session (the most realistic failure mode), this loop spins forever logging "Reconnecting…" every 5 s and the operator has no way to know whether the device is genuinely gone or transiently dropping. Worse — `connect()` re-registers a new heartbeat thread (line 279) on every call. A 30-minute outage spawns ~360 reconnect attempts and ~360 heartbeat threads, each of which the next successful connect orphans. Memory and socket sends climb.
EVIDENCE: `while self._running and not self._connected:`  with only `time.sleep(5.0)` and no escape.
FIX:       Bounded retry with exponential backoff (5s, 10s, 20s, 40s, capped at 60s, give up after N=10 attempts). When giving up, surface a clear "EMOTIBIT LOST — recording in degraded mode" banner via the operator-visible warning panel. Also: `connect()` should refuse to re-fire heartbeat/calib threads if they already exist.
```

```
SEVERITY: HIGH
CATEGORY:  race
REVIEWER:  RT-CS
FILE:      src/polar_mac.py:264-271  (`on_disconnect`)
WHAT:      The bleak `disconnected_callback` references `device` from the enclosing async function's local scope — `device` is the *most recent* dataclass assigned by `action == "connect"`. (1) Before any successful connect, `device` is undefined → NameError. (2) After multiple connect/disconnect cycles, the wrong device may be used for reconnect. (3) `lambda: self._loop.create_task(self._cmd_queue.put(...))` is scheduled on the asyncio loop via `call_later`, but `_cmd_queue.put` is a coroutine — calling `create_task` on it inside a lambda is the right shape, but the lambda also evaluates `device else None` at *callback time*, not at disconnect time, so a later connect to a different device redirects all in-flight reconnects to the wrong device.
FIELD FAILURE: Subject changes Polar straps mid-session (or experimenter swaps devices between subjects without restarting the app); next BLE drop reconnects to the *last* device variable, not the one that just dropped. Possible: silent NameError, or reconnect target mismatch, or two reconnects fighting.
EVIDENCE: `def on_disconnect(c: BleakClient): … self._cmd_queue.put(("connect", device)) if device else None`
FIX:       Pass the device explicitly: `disconnected_callback=functools.partial(on_disconnect, device=device)`. Or capture it in a default arg: `def on_disconnect(c, dev=device):`. Or — better — keep current device on `self._current_device` and reference it.
```

```
SEVERITY: HIGH
CATEGORY:  data-loss
REVIEWER:  Polar
FILE:      src/polar_mac.py:367-369, 417-429
WHAT:      Two BLE writes to `PMD_CONTROL` happen *while* the ECG stream is already running:
           (1) On connect (line 369) we write ECG_START; this is correct.
           (2) During `calibrate_for_recording` (lines 422-428) we write ECG_SETTINGS to PMD_CONTROL ten times at 1 Hz with `response=True`, *while ECG samples are streaming*.
           PMD_CONTROL on H10 is a control endpoint; writing ECG_SETTINGS once is "request available measurement types". Writing it ten times during an active ECG session is undefined and competes with notification bandwidth on the same connection.
FIELD FAILURE: ECG samples can be dropped or duplicated during the 10-second calibration window at the *very start* of a recording — exactly the window where the operator and subject are synchronizing. This calibration window is also where the first auto-ping fires (t=5 s, main_window.py:1393).
EVIDENCE: `await client.write_gatt_char(PMD_CONTROL, ECG_SETTINGS, response=True)` inside the calibrate loop.
FIX:       Calibrate against a non-control characteristic (`BATTERY_CHAR` is already used for the keepalive — same approach for calibration is fine and that's exactly what `_continuous_probe` already does — line 163). Replace the PMD_CONTROL write with `client.read_gatt_char(BATTERY_CHAR)` and verify ECG continuity is unaffected. Also consider running record-start calibration *before* `start_recording()` instead of after (currently main_window.py:1377-1378 starts recording then calibrates).
```

```
SEVERITY: HIGH
CATEGORY:  disconnect
REVIEWER:  Polar
FILE:      src/polar.py:78-86 (Windows variant) and src/polar_subprocess.py:329-331
WHAT:      Windows path has effectively no disconnect handling.
           (1) `polar.py:stop()` sends `{"cmd": "quit"}` and waits 5 s. There is no ECG_STOP. The H10 keeps streaming until the BLE link drops by timeout.
           (2) `polar_subprocess.py:disconnect` action sends `{"type":"disconnected"}` and *returns* — it doesn't release the BLE connection, doesn't write ECG_STOP, doesn't disconnect simplepyble or winrt.
           (3) No `disconnected_callback` equivalent on Windows. If the strap walks out of BLE range, nothing is detected; data simply stops; the recorder writes a zero-length tail.
FIELD FAILURE: On a Windows-host run, a subject who walks out of range and back gets: silence, no marker, no warning, no reconnect. Then on stop, the H10 stays in ECG mode draining its battery for the next session.
EVIDENCE: `polar.py` has 198 lines vs `polar_mac.py` 435 lines — 237 lines of recovery logic are absent on Windows.
FIX:       Either (a) deprecate the simplepyble/winrt path and use bleak on Windows too (bleak supports Windows via WinRT now and would mean one code path), or (b) port the macOS reconnect/keepalive/probe loops into the subprocess with disconnect detection via simplepyble's `set_callback_on_disconnected` (if supported) or polled connection-state checks.
```

```
SEVERITY: HIGH
CATEGORY:  operator-feedback
REVIEWER:  Cog-Sci
FILE:      src/main_window.py:1438-1474  (`_watchdog_check`)
WHAT:      The watchdog only detects EmotiBit-not-writing. For Polar and Unity it only checks status enum/connected flag — not whether *data is actually arriving*. There is no per-stream "last sample age" counter.
FIELD FAILURE: A BLE link that's "connected" but silent (CoreBluetooth idle, post-pairing zombie, simplepyble subscription dropped) shows green to the operator. The recorded `polar_*.csv` will have a long stretch of nothing in the middle and no marker explaining it. This is exactly the silent-data-loss failure mode flagged in the audit prompt.
EVIDENCE: For Polar: `if self._polar.status not in (PolarStatus.RECORDING, PolarStatus.CONNECTED): warnings.append("disconnected")` — checks status, not flow. There is no `last_sample_ns` tracked anywhere on the handlers other than EmotiBit's `_last_writing_ns` (which is for the SD card echo, not data flow).
FIX:       Add `last_sample_ns` to each handler, updated in the on_ecg / on_hr / DATA-receive paths. Watchdog: `if last_sample_ns and (now - last_sample_ns) > THRESH: warn("Polar silent for X s")`. Also write a marker `sensor_silent_<device>` into the CSV at silence onset and `sensor_resumed_<device>` on recovery so post-hoc the gap is explicit.
```

```
SEVERITY: HIGH
CATEGORY:  timing
REVIEWER:  RT-CS
FILE:      SyncBridge.cs:46, 105, 120-124
WHAT:      Timestamps come from `DateTime.UtcNow.Ticks * 100` — Unity host wall clock, not monotonic and not synced. Two compounding issues:
           (1) Quest devices are notorious for clock drift / NTP-not-running. Inter-machine alignment relies on Unity's wall clock matching the LSL machine's. Without explicit NTP discipline, drifts of 100s of ms over a 60-min session are realistic.
           (2) `DateTime.UtcNow` resolution on Windows is ~15 ms historically. On Quest/Android the CLR resolution should be 1 ms-ish, but it's not guaranteed.
FIELD FAILURE: Unity ping receipt timestamps and physiological timestamps will diverge linearly over a long session, undermining the per-ping calibration the README promises. Cross-machine alignment error grows beyond the "<5 ms" claim in the README.
EVIDENCE: README:107 "typical error < 5 ms"  vs  no NTP sync, no monotonic clock, no drift correction, no second-pass alignment.
FIX:       Either (a) document explicitly that the host LSL machine and the Quest must be NTP-synced via the same time server (and verify on session start by reading `time.time_ns()` on both and writing the offset into the syncLog header), or (b) use the per-ping latency measurements to fit a linear drift model post-hoc (the data is there in the syncLog — just nobody is using it). Currently the docstring says "NTP-style clock synchronisation applied per-ping" but only the offset is computed, not drift.
```

```
SEVERITY: HIGH
CATEGORY:  race
REVIEWER:  Lead
FILE:      src/main_window.py:1376-1391  (`_start_rec`)
WHAT:      Recording start is not transactional. Each handler is told to start independently. There is no "all systems confirmed recording" gate. If Polar succeeds but EmotiBit's `RB` echo never arrives, recording proceeds with a 15 s grace before the watchdog re-sends RB (line 1452). Three pings have already fired in those 15 s.
FIELD FAILURE: The first 15 s of every session is at risk: auto-pings 1 and possibly 2 fire while EmotiBit is still pending its first RB echo, so those pings have `emotibit_latency_ns = -1` (also see the CRITICAL bug above which makes it -1 anyway). Even worse, the EmotiBit may not actually be on the SD card yet — the device only confirms with EM `RS=RB` once the SD write begins.
EVIDENCE:
    if self._row_eb.is_required:
        self._emotibit.start_recording()
        self._emotibit.calibrate_for_recording()
        # Non-blocking SD check — watchdog will warn if no echo within grace period
    …
    QTimer.singleShot(5000, self._start_auto_ping_sequence)  # first ping at t=5s
FIX:       State-machine: `IDLE → CALIBRATING → ARMING → ARMED → RECORDING`. Advance to RECORDING only when every required device has confirmed (Polar streaming for ≥N samples, EmotiBit RB echo received, Unity ACK round-tripped at least once). Gate auto-ping on RECORDING. If ARMING times out, abort with a clear failure UI — the failure page already exists at stack index 2.
```

```
SEVERITY: HIGH
CATEGORY:  hygiene
REVIEWER:  Lead
FILE:      src/main_window.py:1393  vs  comment line 955, 1392
WHAT:      Comments and code disagree on auto-ping schedule. Comments say "First auto-ping at t=10s, then every 5s for 3 total" (lines 955, 1392). Code does `QTimer.singleShot(5000, …)` — first ping at t=5 s, then `_auto_ping_timer.setInterval(2000)` — 2 s between pings (line 957), not 5 s.
FIELD FAILURE: Either the comment is wrong (low risk) or someone tuned the schedule and forgot to update the comment, *and* may have intended 5s spacing not 2s. Either way: anyone reading the code to decide "when does my stimulus need to be running by" gets the wrong answer. The first auto-ping at t=5s also fires inside the Polar 10-probe BLE calibration burst (1s × 10 = 10 s), guaranteeing collision.
EVIDENCE: `# First auto-ping at t=10s, then every 5s for 3 total` (line 1392) immediately above `QTimer.singleShot(5000, …)`; and `self._auto_ping_timer.setInterval(2000)   # 2s between pings` (line 957).
FIX:       Pick the schedule, write it as named constants (`FIRST_PING_DELAY_S = 10`, `PING_INTERVAL_S = 5`), and use those constants in code, comments, and log lines. Auto-ping must start *after* calibration window ends.
```

```
SEVERITY: HIGH
CATEGORY:  data-loss
REVIEWER:  Unity
FILE:      SyncBridge.cs:23, src/unity.py:504-506
WHAT:      `bridgeIP = "255.255.255.255"` (broadcast) is the default. SyncBridge's `Listen` accepts ANY incoming `ping_*` UDP packet on port 12345 and ANY incoming `PING` triggers `ping_requested` on the Python side (unity.py:504-506), which then runs `_ping()` and inserts a row into the syncLog.
FIELD FAILURE: On a shared lab subnet, any other instance of LSL or any tooling that emits "PING" / "ping_NNN" on UDP/12345 will be logged into *this* session's data. Worse, since the ACK is sent back to the *source IP* of the ping, you can get cross-session contamination between two simultaneous experiments in the same lab.
EVIDENCE: `if (msg.StartsWith("ping_")) { … _send.Send(ack, ack.Length, ep.Address.ToString(), udpPort); }` — no allowlist. unity.py:504 emits `ping_requested` on bare `"PING"` from anywhere.
FIX:       Bind SyncBridge to a specific peer IP (configured at session start). Reject packets whose source IP is not the LSL host. Same on the Python side: only honor PING from the connected device IP.
```

```
SEVERITY: MEDIUM
CATEGORY:  race
REVIEWER:  RT-CS
FILE:      src/unity.py:356-364  (`start_data_stream`)
WHAT:      The "stop existing loop before starting a new one" mechanism is `self._streaming = False; time.sleep(0.05); self._streaming = True; threading.Thread(_poll_loop).start()`. 50 ms is shorter than the worst-case sleep inside `_poll_loop` (`time.sleep(self._stream_interval)` — default 1 s). So the old loop is *still asleep* when the new one starts. Both run, both call `_out.sendto`. The Unity device gets duplicate REQUEST_DATA and the LSL graphs sometimes show 2× expected rate.
FIELD FAILURE: Higher network load, occasional dropped UDP, duplicate DATA packets parsed twice into the live monitor. Also: `_start_rec` calls `start_data_stream()` *twice* in succession (main_window.py:1385-1386), guaranteeing a race.
EVIDENCE:
    self._streaming = False
    import time as _time; _time.sleep(0.05)   # let old loop exit
    self._streaming = True
    threading.Thread(target=self._poll_loop, daemon=True).start()
FIX:       Use a stop-event (`threading.Event`) the old loop can check on a short tick (`event.wait(self._stream_interval)` instead of `time.sleep`). Also remove the duplicate call at main_window.py:1385-1386.
```

```
SEVERITY: MEDIUM
CATEGORY:  data-loss
REVIEWER:  EmotiBit
FILE:      src/emotibit.py:640-643, 421-429
WHAT:      `_hh_event` is set on ANY HH packet, regardless of source IP. `_single_rtt` sends HE to broadcast and waits on `_hh_event`.
FIELD FAILURE: In a lab with two EmotiBits on the network (very common — pilot and subject, or two subjects), the one that replies *first* sets the event. Calibration RTT now reflects whichever device happened to respond, not the connected device. emotibit_latency_ns is contaminated.
EVIDENCE: `if tag == "HH":  self._hh_recv_ns = time.time_ns(); self._hh_event.set()` — no IP check, even though the `HH` block immediately below DOES gate on `if tag == "HH" and ip:` for device discovery.
FIX:       Gate the event on `ip == self._connected.ip`. Same fix in `_single_rtt`.
```

```
SEVERITY: MEDIUM
CATEGORY:  reproducibility
REVIEWER:  Cog-Sci
FILE:      src/main_window.py:1638-1675  (`__ping_impl`)
WHAT:      `_ping` autostarts a session log file if the user pings before pressing Start (`if not self._sync_logger._writer: … self._sync_logger.start_session(...)`). This creates an orphan session folder containing only the syncLog and no sensor CSVs.
FIELD FAILURE: Operator clicks Send Ping by mistake before recording → a `lsl_<ts>/syncLog_<ts>.csv` folder is silently created on disk. No subject ID, no condition, no sensor data. Months later, post-hoc analysis sees N+M sessions instead of N. Also: pings logged this way have all latencies wrong because record-start calibration never ran.
EVIDENCE:
    if not self._sync_logger._writer:
        self._session_ts = SyncLogger.make_session_timestamp()
        p = self._sync_logger.start_session(self._session_ts)
        self._log(f"Auto-started outlog - {p.name}")
FIX:       Disable the Send Ping button when not recording (already done at line 1395, but `__ping_impl` is also reachable via the auto-ping path and via `unity.ping_requested` and via the autostart code path itself — remove the autostart branch entirely).
```

```
SEVERITY: MEDIUM
CATEGORY:  reproducibility
REVIEWER:  Cog-Sci
FILE:      src/sync_logger.py — entire file
FILE:      src/main_window.py:_start_rec
WHAT:      Session metadata that the experimenter MUST have for analysis is not recorded:
           - subject id, condition, run number, experimenter
           - code version / git SHA
           - device firmware versions, MAC addresses
           - calibrated latency at session start (only logged to console)
           - which devices were "required" vs optional for this session
           - the EmotiBit SD card filename the device chose (it's printed but not persisted)
FIELD FAILURE: Two months later, you have `lsl_2026-01-15_14-23-07/` and no idea which subject this was, what condition they were in, or whether the firmware that ran is the same as the firmware on the bench today.
EVIDENCE: `start_session` writes only column headers. `_start_rec` writes nothing else.
FIX:       Add a `session_meta.json` written into `lsl_<ts>/` at start: subject id (prompt the operator), condition, code git sha (`subprocess.check_output(["git","rev-parse","HEAD"])` with a fallback), per-device fields, the locked `_session_latency_ns` for each device. Block Start until subject id is filled in.
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Lead
FILE:      src/main_window.py:1499-1501, 1897, 1906
WHAT:      Reach-arounds into private attributes of other objects: `self._unity._session_latency_ns`, `self._polar._output_dir = ...`. The handlers expose public APIs but main_window bypasses them.
FIELD FAILURE: Refactor risk. Someone renames `_session_latency_ns` in unity.py and a test passes (because there is no test). Production breaks silently — the AttributeError is in a slot wrapped by Qt and may not even propagate to the user.
EVIDENCE: `self._unity._session_latency_ns if self._unity._session_latency_ns >= 0 else self._unity.calibrated_latency_ns`
FIX:       Add a public `effective_latency_ns` property on each handler. Call that. Add a property setter for `output_dir` instead of poking `_output_dir`.
```

```
SEVERITY: MEDIUM
CATEGORY:  timing
REVIEWER:  RT-CS
FILE:      src/emotibit.py:495-509  (`_pkt`)
WHAT:      EmotiBit packets carry `ts = int(time.time() * 1000) & 0xFFFFFFFF` — UNIX milliseconds, masked to 32 bits. Wraps every ~50 days. Everywhere else in this codebase uses `time.time_ns()` (nanoseconds). Mixed units across the codebase.
FIELD FAILURE: A long deployment that runs continuously across the wrap point (~50 days) silently emits packets whose timestamp restarts. More immediate: anyone aligning EmotiBit packet headers to the syncLog must know "this column is ms-since-1970-mod-2^32" — which is documented nowhere.
EVIDENCE: `ts  = int(time.time() * 1000) & 0xFFFFFFFF`
FIX:       Either align to nanoseconds across all transports or document the EmotiBit packet timestamp in a dedicated field of `session_meta.json`.
```

```
SEVERITY: MEDIUM
CATEGORY:  retry
REVIEWER:  Polar
FILE:      src/polar_subprocess.py:218-273  (`connect_and_stream`)
WHAT:      The Windows connect path has multiple failure modes that all silently return:
           - `simplepyble.Adapter.get_adapters()[0]` → IndexError if no BT adapter (no try)
           - `polar = None; if not polar: send error; return` after a failed scan — operator has no actionable hint
           - Step 4 try/except matches the very specific "-2147483634" / "unexpected time" string and treats anything else as terminal — but BLE pairing throws a wide variety of errors
           - winrt fallback is fire-and-forget (line 272) — if it fails, the user sees only the JSON `{"type":"status","msg":"winrt: ECG_START failed"}` line and no UI surfacing
FIELD FAILURE: Windows-side first-time-pairing setup is fragile and silent. Operator on a fresh machine sees "device connected" then no ECG — and has no idea why.
EVIDENCE: `adapter = simplepyble.Adapter.get_adapters()[0]` — bare index. Many silent returns.
FIX:       Wrap each step. Emit specific `{"type":"error","step":"adapter|scan|notify|pair|ecg_start","msg":...}` so the UI can surface a tailored message. Bounded retry on the pairing race (`-2147483634`) instead of one-shot fallback.
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Lead
FILE:      src/emotibit.py:309-338  (`check_sd_card`)
WHAT:      Defined and well-thought-out. Never called. Dead code.
FIELD FAILURE: The SD-card-not-inserted failure case — which produces a recording with no on-device file — is not actively guarded. It's checked at watchdog tick after a 15 s grace, by which time a 60-subject pilot has 14 unusable seconds at the start of every session.
EVIDENCE: `grep -r check_sd_card src/` finds only the definition.
FIX:       Call `check_sd_card()` from the state machine's ARMING phase before transitioning to RECORDING. If it returns False, fail to RECORDING and surface "SD card not detected" to the operator.
```

```
SEVERITY: MEDIUM
CATEGORY:  operator-feedback
REVIEWER:  Cog-Sci
FILE:      src/main_window.py — entire window
WHAT:      The live monitor shows pretty graphs but no numeric per-stream telemetry: observed sample rate, last-sample age, drop count, queue depth. The experimenter can't tell if Polar is at 130 Hz vs 80 Hz right now.
FIELD FAILURE: The "we ran the session, the data looks fine, then post-hoc we found half the ECG samples missing" failure mode the audit prompt called out by name. Without a sample-rate-observed display the operator has no chance of catching this in real time.
EVIDENCE: `StreamGraph` shows shape, not statistics.
FIX:       Add a "STATS" panel per device: observed Hz over last 5 s window, total samples, last-sample-age in ms, current calibrated one-way latency. Color the row red when observed Hz drops below 50% of nominal.
```

```
SEVERITY: MEDIUM
CATEGORY:  hygiene
REVIEWER:  Unity
FILE:      SyncBridge.cs:35-42, OnDestroy
WHAT:      `_inst` is a static singleton with `DontDestroyOnLoad`. There is no scene-load handling for the LSL inlet/outlet (well — this isn't even using LSL, just raw UDP, but the same lifecycle applies). On scene unload, `_log` and sockets persist; on app quit, `OnDestroy` closes them, but if a new instance is added in another scene it gets `Destroy(gameObject)` (good). However: there is no `OnApplicationPause` handling — when the user takes off the Quest headset, OVR pauses the app; UDP receive thread continues but the main thread Update loop stops, so `_q` (the queue) backs up.
FIELD FAILURE: Subject takes Quest off mid-session, gap of 30 s, puts it back on. During the gap, the SyncBridge receive thread accepted ping packets and queued them but Update wasn't running so nothing was logged to disk. On resume, all queued pings flush at once with their *receipt* timestamps, but the disk write has those timestamps in a single millisecond cluster. Looks like a burst that wasn't.
EVIDENCE: No `OnApplicationPause` / `OnApplicationFocus` override. `Update()` drain pattern.
FIX:       Implement `OnApplicationPause(bool paused)`. If paused, emit a "headset_doffed" marker via UDP to the host. On resume emit "headset_donned". Either drain `_q` immediately on resume or stamp queued items with their original receive ns (you already do — receivedUtcEpochNs is captured in Listen) so the write timestamps are correct. Verify: write a marker into the syncLog so post-hoc the gap is visible.
```

```
SEVERITY: LOW
CATEGORY:  hygiene
REVIEWER:  Lead
FILE:      src/main_window.py:1853-1864  (`_on_stream_rate_changed`)
WHAT:      The combo box maps `stream rate Hz → graph redraw interval` 1:1. Stream rate (network polling) and redraw rate (UI repaint) are conceptually different; coupling them means lowering Polar polling rate also makes the live ECG graph janky.
FIX:       Decouple. Operator-facing setting should be "data rate" only.
```

```
SEVERITY: LOW
CATEGORY:  hygiene
REVIEWER:  Lead
FILE:      src/main.py — packaged app `sys._MEIPASS` handling
WHAT:      `if base_dir not in sys.path: sys.path.insert(0, base_dir)` — fine, but the app prints no version, no build hash, no python version on launch. The operator running a packaged `.app` cannot tell which build they have.
FIX:       Print `Lab Stream Layer v<X.Y.Z>  build <git_sha>  Python <ver>` to log on launch. Surface in window title.
```

---

## TOP 10 SHIP-STOPPERS

Ordered by likelihood of recurrence in a real subject session.

1. **EmotiBit calibration is permanently broken.** Dead-after-return code in `seconds_since_recording_start` (emotibit.py:169-175) leaves `_rtt_buffer`, `_continuous_calib_active`, `_session_latency_ns` undefined. Every shipped session has `emotibit_latency_ns = -1`. The "NTP-style sync per ping" in the README is a lie for the EmotiBit channel today.

2. **Unity row in syncLog is never written.** SyncBridge.cs ACK format (`ACK:<id>`) doesn't carry a unity_ns; unity.py only emits `unity_ack_received` when unity_ns > 0. Cross-machine alignment to Unity is impossible with current shipped data.

3. **Manual EmotiBit IP entry crashes.** `add_manual_device` doesn't exist — the cross-subnet workflow is broken (main_window.py:526).

4. **Recording start is not transactional.** `_start_rec` fires Polar/EmotiBit/Unity start independently, then schedules an auto-ping at t=5 s. There is no "all systems confirmed recording" gate. The first 5-15 s of every session is at risk.

5. **Auto-ping fires *inside* the Polar BLE calibration burst.** First ping at t=5 s; Polar calibration is 10 × 1 s of writes to PMD_CONTROL while ECG is streaming. ECG is at risk of corruption during the operator's first sync ping.

6. **Polar BLE calibration writes to PMD_CONTROL during active ECG stream.** `calibrate_for_recording` repeats the ECG_SETTINGS write 10 times while data flows. Use the battery characteristic instead, like `_continuous_probe` already does.

7. **No per-stream "last sample age" or observed-rate watchdog.** Status is "connected" boolean only. A silent BLE link or zombied UDP stream looks healthy until the data is reviewed — the exact silent failure pattern flagged in the audit prompt.

8. **Sensor disconnects are not marked into the recording.** EmotiBit auto-reconnect is unbounded; Polar Windows disconnect handling is absent; gaps are not annotated. Post-hoc you cannot tell silence-because-data from silence-because-link.

9. **Open broadcast on UDP/12345 accepts any sender.** Cross-experiment contamination is possible on a shared lab subnet because SyncBridge.cs and unity.py both honor `PING` / `ping_NNN` from any source.

10. **Documentation drift hides the data.** Output folder name and sync-log filename in the README don't match the code. The first thing an analyst does after a session is wrong.

---

## SILENT-FAILURE INVENTORY

This is the single most important section. Every code path here can produce a "successful" recording with missing or wrong data and *no error to the operator*.

1. **EmotiBit calibration AttributeError thread death** (emotibit.py:169-175 → 280, 465). Caught nowhere visible to the user.

2. **Unity DATA parse NameError swallow** (main_window.py:1597 + bare except 1627). Live monitor goes silent without logging.

3. **Unity ACK without unity_ns** (SyncBridge.cs:110 + unity.py:521). `log_unity_ack` is never called; no "Unity row missing" warning.

4. **`add_manual_device` AttributeError** (main_window.py:526). The slot's exception escapes Qt's slot wrapper and is logged only to stderr — invisible inside the packaged app.

5. **`_ping` per-device try/excepts** (main_window.py:1647-1664). Every device's marker call is independently caught and the only signal of failure is `eb_lat = -1` in the syncLog — which can also mean "device wasn't required". You cannot distinguish "EmotiBit not connected" from "EmotiBit threw an exception sending the marker" from the syncLog.

6. **EmotiBit auto-reconnect spinner** (emotibit.py:567). Forever loop, no operator-visible "I have given up" state. Recording continues with EmotiBit silent.

7. **Polar Windows quit without ECG_STOP** (polar.py:78-86). Strap stays in ECG mode; next session may pair with a stuck connection.

8. **Polar disconnected_callback NameError before first connect** (polar_mac.py:265-271). Caught by bleak's internal exception handling — invisible.

9. **Watchdog only checks status, not data flow** (main_window.py:1438-1474). A silent connected link passes.

10. **EmotiBit `_hh_event` set by any device** (emotibit.py:640-643). Calibration RTT contaminated by a second EmotiBit on the network — no warning.

11. **`_on_unity_data` swallows everything** (`except Exception: pass`, line 1627-1628). One bad packet → silent live-monitor death for the rest of the session.

12. **Auto-orphan session folder via `_ping` autostart** (main_window.py:1638-1641). Click Send Ping before Start → empty session folder created without operator awareness.

13. **`_save_settings` is never called** — search shows it's defined but never invoked. `closeEvent` only stops handlers; settings written via `_save_settings` would be lost if the app crashes (and even on normal close it isn't called from `closeEvent`).

14. **Stream rate combo conflates network and UI rates** (main_window.py:1853-1864). Lowering data rate degrades graphs; operator may interpret jankiness as data loss.

---

## DISAGREEMENT LOG

**Polar BLE calibration mechanism.**
Polar reviewer wants `BATTERY_CHAR` reads (matches existing `_continuous_probe`). RT-CS reviewer points out that battery reads are ~10 ms RTT but BATTERY_CHAR is cached on the device — calibration measures GATT cache RTT not actual stream RTT. Lead reviewer counters that for the purpose of clock alignment any consistent BLE characteristic read is fine; the absolute latency value matters less than that it's a stable measure across the session. **Panel recommendation:** use BATTERY_CHAR for calibration; document that the calibration measures BLE link RTT, not data-path latency. Add a one-time per-session check that compares `_continuous_probe` median to `_record_calib` median and warn if they differ by >50%.

**Auto-reconnect behavior.**
EmotiBit reviewer wants unbounded retries (subject brownouts on long sessions are recoverable). Cog-Sci reviewer wants bounded retry + visible "DEGRADED" state because experimentalists need to know to abandon a subject vs continue. **Panel recommendation:** bounded retry (10 attempts with exponential backoff capped at 60 s), then a *prominent persistent banner*, but recording continues. Marker `sensor_lost_<device>` written at first failed retry; `sensor_recovered_<device>` written on recovery. The operator can choose whether to abort.

**Where to put record-start calibration relative to record-start.**
Polar/EmotiBit reviewers want calibration *before* `start_recording` so the BLE/UDP link is quiescent during calibration. Cog-Sci reviewer notes that calibration time delays subject readiness and that participants get fidgety. Lead reviewer notes the current `_start_rec` parallelizes them which is fastest but exposes the active-stream-during-calibration bug. **Panel recommendation:** state-machine `CALIBRATING → ARMED → RECORDING`. Calibration first (fast — 3 s with the existing `_continuous_probe` median is already calibrated by then for a connected device), then start_recording, then unblock auto-ping. Net session-start delay ≈ unchanged, correctness improved.

**Whether to deprecate the Windows polar.py path.**
Polar reviewer wants to delete `polar.py`/`polar_subprocess.py` and use `bleak` on Windows (one code path, half the LOC). Lead reviewer notes the Windows path was written specifically to handle a Python 3.14 / WinRT incompatibility (per the docstring at `polar.py:1-19`) and ripping it out may regress users on that combo. **Panel recommendation:** keep both for now, but extract a shared `PolarHandlerProtocol` interface and write the auto-reconnect / silence-detection logic *once* in main_window's watchdog rather than per-platform.

---

## What we did NOT find / where the code is good

In the spirit of not inventing issues:

- The `_try_emit` wrapper around Qt signals is correct and exactly the right defensive pattern. Keep it.
- `polar_mac.py`'s on_disconnect → 5s reconnect schedule via `loop.call_later` is the right shape, even though the closure-over-`device` is buggy.
- The README's per-ping NTP-style sync architecture description is conceptually correct; the implementation just doesn't deliver on the Unity channel and silently fails on EmotiBit.
- The state separation between `_session_latency_ns` (locked at start) and `calibrated_latency_ns` (rolling) is the right model. Stick to it.
- `EmotiBitHandler._send_ctrl` correctly preferring TCP and falling back to UDP is good defensive design.

---

## Standard reminder

Would we let a graduate student run a 60-subject, 90-minute-per-session study on this code tomorrow with no babysitter and no re-runs allowed?

**No.** Specifically: items 1, 2, 4, and 7 in the Top-10 must be fixed before that study. Items 3, 5, 6, 8, 9, 10 must be fixed before the study scales beyond a handful of subjects. The Silent-Failure Inventory items are individually small but collectively explain every "we have a recording but the data is wrong" event the user reported.

Begin remediation with the four CRITICAL findings; they are mechanical fixes (a handful of lines each) and should land in a single morning of work.

— End of audit —
