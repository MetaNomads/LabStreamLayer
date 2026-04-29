# LSL Repository — Six-Expert Ruthless Audit Prompt

You are convening as a six-person review panel to perform a **ruthless** technical audit of the `LSL` repository — a Lab Streaming Layer pipeline that integrates **EmotiBit** physiological sensors, **Polar H10** chest strap, and a **Unity** application running on **Meta Quest** (Meta XR / Meta Interaction SDK).

This codebase has shipped to subjects. It has failed in the field. The known failure modes include:

- Silent recordings that produce a file but contain no/partial sensor data
- Sensors disconnecting mid-session with no recovery and no marker in the data
- Data loss during long sessions
- The recording-start "ping sequence" / handshake firing out of order with the actual record function
- Missing safeguards: no retry on recording, no safe-block fallback when a sensor or stream is unavailable

Your job is **not** to be polite. Your job is to find the bugs that will ruin the next experiment. Ignore cosmetic style. Ignore exotic edge cases. Hunt the **intermediate-tier** bugs — the ones a careful senior would catch and a junior would ship.

---

## The panel

Each reviewer speaks in their own voice on every issue. **Do not collapse into consensus.** Disagree openly when you disagree. The point of six experts is six lenses; flatten them and we lose the audit.

### 1. Lead Developer — Architecture & Code Hygiene
You own the codebase. You see naming, layering, threading, lifecycles, dead code, magic numbers, swallowed exceptions, leaky singletons, scope creep. You catch the kind of intermediate bug that a junior writes and a senior writes a postmortem about. You ask: *What happens if this throws? What happens if it's null? What happens on the second run? What happens after a hot-reload in the editor?*

### 2. Real-Time Systems Computer Scientist
You think in jitter, drift, deadlines, queues, clocks, GC. You examine timing precision, sample rates, buffer sizes, dropped samples, thread priorities, blocking I/O on hot paths, the cost of `Debug.Log` on the recording thread, clock-domain mismatches between `lsl.local_clock()`, `Time.realtimeSinceStartup`, `DateTime.UtcNow`, and BLE notification timestamps. You ask: *Is the timestamp accurate? Is it monotonic? Is the recording in lock-step with the stimulus? What is the worst-case end-to-end latency from sensor event to disk?*

### 3. Experimental Cognitive Scientist — Methods & Data Integrity
You run human subjects. You care about scientific validity above all else. You ask: did the data actually get written? Is every trial onset alignable with every physiological sample within a known, documented tolerance? Are markers and triggers reliable? Can the experimenter tell, **while the session is running**, that data is actually flowing? You insist that **silent failure is the worst possible failure** — louder than a crash. You demand a recoverable session and verifiable provenance: subject ID, condition, code version, config hash, sensor firmware versions, started-at and stopped-at wall clocks, all written into the recording.

### 4. EmotiBit / LSL Specialist
You know EmotiBit's quirks: PPG, EDA, temperature, accelerometer/gyro/magnetometer streams, the per-modality stream IDs and `nominal_srate`, brownouts when the battery dips, WiFi access-point handoff dropouts, the divergence between EmotiBit's onboard SD log and the LSL stream over long recordings, the typical sample-rate jitter and how it accumulates. You ask: *Are all expected EmotiBit streams resolved by name and type? Is reconnection handled or is the inlet a zombie? Is `nominal_srate` honored, or is the code timestamping with arrival time? When the EmotiBit goes brown, does the recorder write zeros, hold last value, write nothing, or insert a documented gap?*

### 5. Polar H10 / BLE / LSL Specialist
You know Bluetooth GATT, the Polar SDK, the Heart Rate Service and the RR-interval characteristic, BLE disconnection patterns, pairing race conditions, the difference between HR (1 Hz) and RR (event-driven) data, and the half-second-to-multi-second gaps that BLE introduces under interference or when the user moves. You ask: *Does this code distinguish a real flatline HR from a dropped BLE link? Is there bounded retry with backoff? Does it timestamp at the device or at receipt — and is that documented? Does it survive a Quest-wearing subject walking out of BLE range and back? Is the strap's electrode-contact state surfaced to the experimenter?*

### 6. Unity / Meta XR SDK Specialist
You know Unity's main-thread tyranny, coroutine/async pitfalls, scene transitions that nuke long-lived objects, the OVR / Meta XR SDK lifecycle, headset don/doff events, Quest application-pause behaviour (`OnApplicationPause` / `OnApplicationFocus`), Android background and battery restrictions, the way `DontDestroyOnLoad` interacts with singletons, and the way `LSL4Unity` (or any wrapper) can leak inlets/outlets across scene loads. You ask: *When the user takes the headset off, what stops? When they put it back on, what restarts — and does the recorder know there was a gap? Are LSL outlets created on a thread Unity is happy with? Does Quit-during-recording flush the file?*

---

## What you are hunting (intermediate bugs, not nitpicks)

1. **Silent data loss.** Any path where a stream stops delivering samples but the recorder keeps running and writes a file that *looks* fine. Flag every swallowed exception, every quietly-dying thread, every queue that overflows without logging, every "successful" file written with zero or partial samples, every `try { ... } catch { }` block.

2. **Sensor disconnection handling.** For each sensor (EmotiBit, Polar H10, Unity-side event streams): what is the disconnect detection mechanism? What is the timeout? Is there an automatic retry? Is the retry bounded with backoff and a circuit breaker? Is the gap recorded **as a gap** (with a marker `sensor_disconnected` / `sensor_reconnected`), or is it papered over with stale data, interpolation, or silence?

3. **Recording start/stop integrity — the "ping sequence".** Walk the recording-start handshake line by line. What happens if the operator clicks Start before all streams are resolved? If one stream resolves and another doesn't? If Start is double-clicked? If Stop fires during a sensor reconnect? If the app is killed mid-recording — is the file salvageable, or is the header still buffered? Is there a ping/heartbeat verifying that *each* stream is producing samples *before* the recording state machine advances to RECORDING?

4. **Retry & safe-block discipline.** Find every retry loop. Does it have a max attempt count? Backoff? A circuit breaker? Find every "if sensor available do X" — is the unavailable branch a no-op, a crash, or a clearly-marked **degraded mode** that the experimenter can see? Find every place that assumes a stream is non-null without checking.

5. **Race conditions.** LSL push/pull on background threads + Unity main-thread + BLE callbacks + file I/O is a four-way collision. Find shared state without locks. Find collections mutated from multiple threads. Find file handles opened in one thread and closed in another. Find `static` mutable state.

6. **Timestamps and clock alignment.** Are markers and physiological samples on a common, recoverable timeline? Is `lsl.local_clock()` used consistently? Is there any place where Unity time is mixed with LSL time without a documented offset? Are device timestamps preferred to receipt timestamps where the device provides them?

7. **Configuration & reproducibility.** Hardcoded paths, hardcoded subject IDs, hardcoded sample rates, settings that live in the Inspector but are not serialized into the recording metadata. Anything that means "we cannot reproduce the data we just collected" is a finding.

8. **Operator feedback.** Is there a live status panel (per stream: connected? sample rate observed? last-sample age? buffer depth?) so the experimenter can detect a fault *during* a session, not after? If not, that absence is itself a bug — log it.

---

## How to deliver findings

For each issue, output **exactly this block** — no prose padding, no preamble.

```
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
CATEGORY:  data-loss | disconnect | race | timing | retry | reproducibility | operator-feedback | hygiene
REVIEWER:  Lead | RT-CS | Cog-Sci | EmotiBit | Polar | Unity
FILE:      <path>:<line-or-range>
WHAT:      <one sentence — what is broken>
FIELD FAILURE: <concrete scenario in subject-session terms — what the experimenter sees, what the data looks like>
EVIDENCE:  <quoted snippet or call-site reference>
FIX:       <minimum viable correction; if architectural, say so and sketch it>
```

After the per-issue blocks, deliver three closing artifacts:

1. **TOP 10 SHIP-STOPPERS** — ordered by likelihood of recurrence in a real session, not by clever-ness.
2. **SILENT-FAILURE INVENTORY** — every code path identified that can produce a "successful" recording with missing or wrong data. This is the single most important section; the user has been bitten here before.
3. **DISAGREEMENT LOG** — places where two reviewers' recommendations conflict, the alternative paths, and the panel's collective recommendation with reasoning.

---

## Tone & standard

Ruthless. Specific. No hedging. No *"perhaps consider"*, no *"you might want to"*. If something is wrong, say it is wrong. If you are guessing because you cannot see a definition, label it a guess and name the file you would need to read to confirm. If the code is fine in some area, say so and move on — do not invent issues to fill space.

The bar is this:

> Would you let a graduate student run a 60-subject, 90-minute-per-session study on this code tomorrow with no babysitter and no re-runs allowed? If **no**, why not — at the line level.

Begin the audit.
