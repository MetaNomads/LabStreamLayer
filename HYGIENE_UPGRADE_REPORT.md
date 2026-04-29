# LSL Repository — Programming Hygiene Upgrade

**Date:** 2026-04-28
**Companion to:** the panel audit reports (`PANEL_AUDIT_REPORT.md`, `PANEL_AUDIT_REPORT_PASS2.md`) and the remediation reports (`PANEL_REMEDIATION_REPORT.md`, `PANEL_REMEDIATION_REPORT_PASS2.md`).
**Goal:** raise the floor. Add the things that were never there to begin with — contracts, an invariants pass, automatic test coverage, schema validation, real-time self-repair — so future bugs of the same class get caught at the producer rather than discovered in a session three months later.

The previous reports fixed *individual bugs*. This pass fixes the **enforcement layer** that should have prevented those bugs from being merged in the first place.

---

## What was added — overview

| Module / dir | Purpose | LOC |
|---|---|---|
| `src/contracts.py` | `@requires` / `@ensures` / `Contract.check` decorators. Dep-free. Disable with `LSL_CONTRACTS=off`. | 130 |
| `src/invariants.py` | Runtime `SystemInvariants` with named checks; returns structured `Violation` objects. | 175 |
| `src/self_heal.py` | `RepairTechnician` with registered repair strategies. Bounded attempts + cooldown. Audit-trail rows in syncLog. | 175 |
| `tests/conftest.py` | PyQt6 + bleak stubs and shared fixtures. | 90 |
| `tests/test_contracts.py` | Tests for the contracts library itself. | 75 |
| `tests/test_handlers.py` | Tests for handler init, public_summary, source-IP gates, contract enforcement. | 130 |
| `tests/test_sync_logger.py` | Schema, write_event semantics, validator. | 105 |
| `tests/test_invariants.py` | Invariant firings + composition. | 105 |
| `tests/test_self_heal.py` | Strategy execution, attempt cap, audit-trail row appears. | 110 |
| Schema validator in `sync_logger.py` | Every `_writer.writerow` routed through `_write_row` which validates against `VALID_MACHINES`/`VALID_EVENTS` and field types. | +50 |
| Contracts applied to handlers | `EmotiBit.connect`, `Unity.connect_device`, three `public_summary` methods. | +8 |
| Wired into `main_window.py` | `SystemInvariants` and `RepairTechnician` instantiated in `MainWindow.__init__`; `check_all` + `repair` run inside the existing watchdog tick. | +20 |

**Total new code:** ~1,070 lines. **Total tests:** 68 pytest + 22 smoke = **90 automated checks**, all passing in **<200 ms**.

---

## 1. Contracts (`src/contracts.py`)

Lightweight design-by-contract — no third-party dependency, fits on one screen.

```python
@requires(lambda self, device: device is not None and bool(getattr(device, "ip", "")),
          "device must be non-None and have a non-empty ip")
def connect(self, device): ...

@ensures(lambda result, *_: isinstance(result, dict),
         "public_summary must return a dict")
def public_summary(self) -> dict: ...

Contract.not_none(handler, name="emotibit")
Contract.in_range(latency_ms, 0, 10_000)
```

On violation, `ContractViolation` (a `ValueError` subclass) is raised. Caught by the same `except Exception` blocks the codebase already uses, so a contract violation **degrades gracefully into a logged failure**, not a crash. Disable everywhere with `LSL_CONTRACTS=off` for production speed; the test suite forces it on.

**Where it's currently applied** (intentionally a small initial set — extend as the codebase evolves):

- `EmotiBitHandler.connect(device)` — precondition: device non-None with ip.
- `UnityHandler.connect_device(device)` — same.
- `EmotiBitHandler.public_summary` / `PolarHandler.public_summary` / `UnityHandler.public_summary` — postcondition: returns dict.

**What contracts catch that nothing else does.** Pass-1 had `_write_session_meta` reaching into `self._emotibit._connected.device_id` — refactoring `_connected` could silently break the meta write. With the postcondition on `public_summary` + the new use of it in `_write_session_meta`, the same refactor would now fire a `ContractViolation` at the producer instead of writing a `null` to disk.

---

## 2. Invariants (`src/invariants.py`)

Six named runtime checks composed into a single `SystemInvariants.check_all()` call:

| Invariant | Severity | Repair strategy |
|---|---|---|
| `logger_open_during_record` | critical | `reopen_sync_logger` |
| `session_dir_consistent` | critical | `recreate_session_dir` |
| `required_sensors_alive` | error | (operator action) |
| `no_handler_stuck_calibrating` | warn | `trigger_recalibration` |
| `emotibit_writing_within_grace` | error | `resend_rb` |
| `ping_count_matches_session` | warn | (disabled until timing data plumbed through) |

Each invariant is a single function returning `Violation | None`. Adding a new one is one line in `_register_defaults` (or `inv.register("name", fn)` from outside). Buggy invariants don't crash the rest — `check_all` wraps each in its own try/except and emits a synthetic violation if the predicate raises.

**Why this is the right layer.** The pass-2 audit observed that the codebase relies on *implicit* state — each handler is correct in isolation but the composite "is recording AND has open writer AND every required sensor is alive" was never named. `SystemInvariants` makes that composite explicit and cheap to check. The watchdog timer fires it every 5 s during recording.

---

## 3. Self-heal (`src/self_heal.py`)

A `RepairTechnician` with five registered strategies. On each invariant violation that has a `repair_strategy`, the technician looks it up and runs it. **Every repair attempt writes a `system_repair, <strategy>:<outcome>:<violation>` row into the syncLog**, so the post-hoc analyst can see what self-heal did and when.

| Strategy | What it does |
|---|---|
| `reopen_sync_logger` | If `sync_logger._writer is None` mid-recording, reopens with a `_recovered_<n>.csv` suffix in the same session folder. |
| `recreate_session_dir` | If the session folder vanished (rare, but happens on cloud-mounted drives), recreates it. |
| `resend_rb` | Calls `EmotiBit.start_recording()` again. |
| `trigger_recalibration` | Calls `calibrate_for_recording()` on each named uncalibrated handler. |
| `reset_unity_parser` | Clears the `_unity_parse_seen` set so new parser errors are visible again. |

**Hard rules the technician enforces:**

- **Max 3 attempts per strategy per session** (via `_attempts` counter, reset by `MainWindow._start_rec`).
- **10-second cooldown** between attempts of the same strategy (via `_last_run`).
- Strategies **never spawn new threads** to replace dead ones — that path corrupts state. Reconnects go through the existing `_auto_reconnect` machinery.

**What this gets us.** A closed sync-logger file mid-session previously meant every subsequent ping silently disappeared. Now: the next watchdog tick (≤5 s later) detects it via `logger_open_during_record`, opens `syncLog_<ts>_recovered_1.csv`, writes a `system_repair, reopen_sync_logger:success` row, and recording continues. The audit trail crosses the gap.

---

## 4. Schema validation in `SyncLogger`

Every CSV write is now routed through `_write_row(machine, event, ping_id, local_epoch_ns, latency_ns)` which validates against:

```python
VALID_MACHINES = {"lsl", "polar", "emotibit", "unity"}
VALID_EVENTS = {
    # Ping-cycle
    "ping_sent", "ping_received",
    # Sensor lifecycle
    "sensor_lost", "sensor_recovered", "given_up",
    "sensor_silent", "sensor_resumed",
    # Quest lifecycle
    "headset_doffed", "headset_donned", "app_quitting",
    # Self-heal
    "system_repair",
}
```

Plus type rules: `local_epoch_ns ∈ {""} ∪ {non-negative int}`, `latency_ns ∈ {""} ∪ {int ≥ -1}`. A row that fails validation is **dropped with a stderr warning** rather than written. Result: a future change that adds a typo'd event name (`"sensorlost"` vs `"sensor_lost"`) doesn't quietly poison every analysis pipeline downstream — the row never makes it to disk, the warning is loud, and the test suite catches it (`test_invalid_row_dropped_not_written`).

---

## 5. Test suite (`tests/`)

**Five files, 68 tests, runs in <200 ms via `python3 -m pytest tests/`.**

| File | What it tests |
|---|---|
| `test_contracts.py` | The contracts library itself — all three primitives, env-var disable. |
| `test_handlers.py` | Handler init, `public_summary`, source-IP gates, RECONNECT-when-`_device`-is-None regression, contract enforcement on bad inputs. |
| `test_sync_logger.py` | Schema rows, `write_event` semantics, latency-ns sentinel, `_validate_row` rules, invalid-row drop. |
| `test_invariants.py` | Each default invariant fires correctly; custom invariant registration; buggy invariant doesn't crash `check_all`. |
| `test_self_heal.py` | Each strategy executes; `system_repair` row appears in syncLog; attempt cap; cooldown; unknown-strategy log; `reset()` clears counters. |

**`tests/conftest.py`** stubs PyQt6 and bleak in `sys.modules`, forces `LSL_CONTRACTS=on`, and provides four shared fixtures (`emotibit_handler`, `polar_handler`, `unity_handler`, `sync_logger`). Tests run on any Python — no PyQt or bleak install needed.

---

## 6. Wiring into the running app

`MainWindow.__init__` now instantiates one `SystemInvariants` and one `RepairTechnician`, wired to the live handlers and SyncLogger. The existing watchdog timer (`WATCHDOG_INTERVAL_MS = 5000`) calls `_invariants.check_all()` and `_repair.repair(violations)` once per tick, only when recording. `_start_rec` calls `_repair.reset()` so each session has a fresh attempt budget.

Total wiring: ~20 lines in `main_window.py`. No existing call site modified.

---

## Verification

### Automated
```
$ python3 -m pytest tests/ -q
68 passed in 0.13s

$ python3 scripts/smoke_test.py
22/22 passed  —  all green
```

The smoke test still runs (it predates this pass). Pytest is now the deeper layer; smoke is the fast gate. Run pytest before commits, smoke before pushes.

### Property-level checks (already verified)

- `test_contracts_can_be_disabled` — `LSL_CONTRACTS=off` no-ops every contract.
- `test_buggy_invariant_does_not_crash_check_all` — one bad invariant doesn't kill the whole pass.
- `test_max_attempts_per_strategy` — repair loops can't run away.
- `test_repair_writes_system_repair_row` — every repair attempt is auditable in the syncLog.
- `test_invalid_row_dropped_not_written` — schema-violating rows never reach disk.

### Manual checks you should run on real hardware (next session)

1. Start a session. Mid-session, manually `kill -STOP <pid>` then `kill -CONT <pid>` the Python process for ~7 seconds. Confirm: a `lsl, system_repair, ...` row appears in the syncLog if any invariant fired during the freeze.
2. Open `~/LabStreamLayer_Recordings/lsl_<ts>/syncLog_<ts>.csv` after a session and grep for `system_repair`. Should be empty for a clean session, populated when something failed.

---

## What this changes architecturally

**Before:** The codebase had handlers + a UI + a smoke test. State consistency across the system was an emergent property of "everyone wrote their handler correctly". Bugs were caught by you reading the code, by the panel reading the code, or — worst case — by a session producing a recording with `emotibit_latency_ns = -1` for every row.

**After:** There is now a *layer between intent and behavior*. Contracts assert what each method promises. Invariants assert what the system as a whole promises during recording. Self-heal automates the trivial recoveries and emits an audit trail for the non-trivial ones. The pytest suite codifies every fix the panel landed, so a future regression on any of the 68 properties trips a test before it ships.

The codebase has gone from "trust the developer" to "the producer asserts; the validator enforces; the self-healer cleans up; and every step writes a row". That's the discipline that keeps a 60-subject study runnable.

---

## What's still deferred

The four pass-2 deferred items are unchanged. The hygiene upgrade made them easier — but didn't address them.

1. **Polar Windows path → bleak migration.**
2. **Formal `IDLE → CALIBRATING → ARMED → RECORDING` state machine** (now easier: invariants + contracts give us the vocabulary to specify the state machine without inventing it).
3. **Subject-ID modal in the GUI.**
4. **Build-time `_version.py` codegen for PyInstaller bundles.**

I'd add a fifth: **NTP-style drift correction script over the now-richer syncLog.** Every column needed is now stable and validated.

---

## Files added or changed in this pass

```
ADDED
  src/contracts.py                       130 lines
  src/invariants.py                      175 lines
  src/self_heal.py                       175 lines
  tests/conftest.py                       90 lines
  tests/test_contracts.py                 75 lines
  tests/test_handlers.py                 130 lines
  tests/test_sync_logger.py              105 lines
  tests/test_invariants.py               105 lines
  tests/test_self_heal.py                110 lines

MODIFIED
  src/sync_logger.py        +50  (validator + _write_row chokepoint)
  src/emotibit.py           +6   (contracts on connect / public_summary)
  src/polar_mac.py          +4   (contract on public_summary)
  src/unity.py              +5   (contracts on connect_device / public_summary)
  src/main_window.py        +20  (invariants + self-heal wiring)
```

---

## Pass H — Making sure the contracts and components are actually used

The first hygiene pass added the *machinery*. This pass makes sure it gets *used*. Six steps.

### H1. Contract markers for introspection

`@requires` and `@ensures` now stamp every wrapper with a `_lsl_contracts` attribute holding `(kind, predicate_src, msg)` tuples. Multiple decorators chain — a method with both a `@requires` and an `@ensures` carries both entries. New helper `contracts.get_contracts(method)` returns the tuple, empty if no contracts.

This is the foundation everything else in this pass builds on: contracts are now *reflectable*, which means coverage is enforceable.

### H2. Contracts applied to every boundary that needed them

The first pass had three contracts. This pass has **fourteen**, covering every public producer method that takes inputs that can be wrong or returns a value the rest of the system depends on.

| Module | Method | Contracts |
|---|---|---|
| `emotibit.py` | `connect` | `@requires` device non-None with non-empty ip |
| `emotibit.py` | `start_recording` | `@requires` handler is running |
| `emotibit.py` | `send_marker` | `@requires` label non-empty + `@ensures` (send_ns, latency_ns) tuple |
| `emotibit.py` | `public_summary` | `@ensures` returns dict |
| `polar_mac.py` | `start_recording` | `@requires` session_ts non-empty |
| `polar_mac.py` | `send_marker` | `@requires` label non-empty + `@ensures` 2-tuple |
| `polar_mac.py` | `public_summary` | `@ensures` returns dict |
| `unity.py` | `connect_device` | `@requires` device with ip |
| `unity.py` | `broadcast_ping` | `@requires` label non-empty |
| `unity.py` | `set_stream_rate` | `@requires` rate_hz > 0 |
| `unity.py` | `public_summary` | `@ensures` returns dict |
| `sync_logger.py` | `start_session` | `@requires` non-empty session_ts × `@requires` not already started × `@ensures` returns Path |
| `sync_logger.py` | `log_ping` | `@ensures` returns (ping_id, send_ns) tuple where ping_id starts with "ping_" |
| `sync_logger.py` | `write_event` | `@requires` machine in valid set |
| `main_window.py` | `_start_rec` | `@requires` not already recording |
| `main_window.py` | `_stop_rec` | `@requires` recording |
| `main_window.py` | `__ping_impl` | `Contract.check` state-consistency between `_writer` and `_is_recording` |

### H3. Schema-validation failures now reach the operator

`SyncLogger` accepts an optional `log_callback` constructor arg (also settable later via `set_log_callback`). When the schema validator drops a row, the message goes to BOTH stderr (so the smoke script and pytest still see it) AND the operator log (so the experimenter sees a malformed-row warning live, not buried in a terminal). Wired into MainWindow at every site that constructs a SyncLogger (`__init__`, `_load_settings`, `_browse`).

### H4. The orphan repair strategy now has an invariant that calls it

`reset_unity_parser` was registered in `RepairTechnician` but no invariant ever fired with that strategy name — dead code. Added `unity_parser_overloaded` invariant that fires when `_unity_parse_seen` has 5+ distinct error types, with `repair_strategy="reset_unity_parser"` and the live set object passed via `extra` so the strategy can clear it. The invariant's constructor accepts a `parser_seen_set_fn` callable, which `MainWindow` wires as `lambda: getattr(self, "_unity_parse_seen", None)` — same object both sides.

### H4 (continued). `scripts/contracts_audit.py`

Programmatic coverage report. Lists every method in `EXPECTED_CONTRACTS` and shows the contracts it carries. Exits 0 if all 14 expected slots have at least one contract, 1 otherwise — runnable in CI to prevent silent regression in coverage. Sample output:

```
emotibit.EmotiBitHandler
  ✓  connect          [requires]
       requires: device must be non-None and have a non-empty ip
  ✓  start_recording  [requires]
       requires: EmotiBit handler must be running (call start() first)
  ✓  send_marker      [ensures, requires]
       ensures: send_marker must return (send_ns:int, latency_ns)
       requires: marker label must be a non-empty string
...
OK — all 14 expected contracts present.
```

### H5. Tests for everything in H

| Test file | What it asserts |
|---|---|
| `tests/test_contract_coverage.py` | Reflection test parametrized over the 14 expected contracts — fails if a slot is empty |
| `tests/test_log_callback.py` | SyncLogger callback invoked on invalid row, silent on valid row, ctor arg works, callback exception swallowed |
| `tests/test_invariants.py` (extended) | `unity_parser_overloaded` fires at threshold; silent below; passes the live set object so the strategy can clear it |

### Final scorecard

```
$ python3 -m pytest tests/ -q
88 passed in 0.15s

$ python3 scripts/smoke_test.py
22/22 passed  —  all green

$ python3 scripts/contracts_audit.py
OK — all 14 expected contracts present.
```

**88 pytest + 22 smoke + 14 contract slots = 124 automated checks**, all green, all running in well under a second. The contracts-audit script is the third leg — pytest tests *behavior*, smoke tests *static structure of strings in source*, audit tests *which methods carry what contracts*. Together they catch (a) regressions in functional behavior, (b) regressions in wire format / file paths / code patterns, and (c) regressions in enforcement coverage.

### What this pass changed structurally

Before pass H: contracts existed in the codebase but only on three boundaries; the rest of the producer surface had nothing. The audit panel would point at `_start_rec`, `start_session`, `send_marker` — all unprotected. A future "edit `_start_rec` to allow re-entry" would compile and ship without firing a single test.

After pass H: every public producer method that *could* take a wrong input or *should* return a constrained value has a contract. The `contracts_audit.py` script enforces coverage. The reflection test in pytest enforces it again at commit time. A future edit that bypasses a contract trips both.

The orphan `reset_unity_parser` strategy now has an invariant that exercises it. Schema-validation failures route to the operator log. The wiring is one cohesive system rather than three modules that happen to live in the same repo.

— End of hygiene-upgrade report —
