"""
invariants.py — runtime state-consistency checker.

What's an invariant here? A statement about LSL system state that should hold at
all times during a recording. Examples:
  - "If self._is_recording, the SyncLogger writer must be open."
  - "Every required sensor that isn't given_up must have produced a sample
     within SAMPLE_SILENCE_S * 2 seconds."
  - "No handler should be in CALIBRATING for more than 30 s."

Why? The pass-2 audit observed that the codebase relies on implicit state.
Each handler's lifecycle is correct in isolation but the COMPOSITE of recording
flag + writer + handler statuses is what actually matters scientifically. This
module makes that composite explicit and checkable.

How is it used?
  1. main_window.py instantiates `SystemInvariants(handlers, sync_logger,
     get_recording_state)` once.
  2. A QTimer fires `inv.check_all()` every WATCHDOG_INTERVAL_MS during recording.
  3. Violations are logged AND handed to the self-heal layer for repair attempts.

Adding a new invariant:
  Subclass-free — just append to _INVARIANTS in `register_default_invariants()`,
  or call `inv.register(name, fn)` from outside.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List


# ── Violation record ──────────────────────────────────────────────────────────

@dataclass
class Violation:
    """A failed invariant check. The optional `repair_strategy` field names a
    self-heal strategy that the RepairTechnician will look up. None = un-repairable
    (operator must intervene)."""
    name:             str           # invariant name, e.g. "logger_open_during_record"
    description:      str           # one-line human-readable consequence
    severity:         str = "warn"  # "warn" | "error" | "critical"
    detected_at_ns:   int = 0
    repair_strategy:  str | None = None
    extra:            dict = field(default_factory=dict)

    def __post_init__(self):
        if self.detected_at_ns == 0:
            self.detected_at_ns = time.time_ns()


# ── The checker ───────────────────────────────────────────────────────────────

class SystemInvariants:
    """Holds and runs invariant predicates over the live LSL system."""

    def __init__(self, *, emotibit, polar, unity, sync_logger,
                 is_recording_fn: Callable[[], bool],
                 required_fn: Callable[[str], bool],
                 sample_silence_s: dict,
                 parser_seen_set_fn: Callable[[], set] | None = None):
        self.emotibit         = emotibit
        self.polar            = polar
        self.unity            = unity
        self.sync_logger      = sync_logger
        self.is_recording     = is_recording_fn       # () -> bool
        self.is_required      = required_fn           # (device_name) -> bool
        self.sample_silence_s = sample_silence_s      # {"emotibit": 5.0, ...}
        # Returns the live MainWindow._unity_parse_seen set (or None to skip).
        # Threaded through so the invariant can both READ the count and pass the
        # SAME object to the repair strategy (which calls .clear() on it).
        self.parser_seen_set_fn = parser_seen_set_fn
        self._invariants: List[tuple] = []            # [(name, callable returning Violation|None)]
        self._register_defaults()

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, name: str, fn: Callable[[], Violation | None]):
        """Add a custom invariant. fn() returns a Violation on failure, None on pass."""
        self._invariants.append((name, fn))

    def _register_defaults(self):
        self.register("logger_open_during_record", self._inv_logger_open)
        self.register("session_dir_consistent",    self._inv_session_dir)
        self.register("required_sensors_alive",    self._inv_required_alive)
        self.register("no_handler_stuck_calibrating", self._inv_calib_not_stuck)
        self.register("emotibit_writing_within_grace", self._inv_emotibit_writing)
        self.register("ping_count_matches_session",    self._inv_ping_count_sane)
        self.register("unity_parser_overloaded",       self._inv_unity_parser_overloaded)

    # ── Run ──────────────────────────────────────────────────────────────────

    def check_all(self) -> List[Violation]:
        """Run every registered invariant and return all violations.
        Each invariant is wrapped in try/except so one buggy invariant doesn't
        kill the whole check."""
        out: List[Violation] = []
        for name, fn in self._invariants:
            try:
                v = fn()
                if v is not None:
                    out.append(v)
            except Exception as e:
                out.append(Violation(
                    name=name,
                    description=f"invariant raised {type(e).__name__}: {e}",
                    severity="error",
                ))
        return out

    # ── Default invariants ───────────────────────────────────────────────────

    def _inv_logger_open(self) -> Violation | None:
        if self.is_recording() and not self.sync_logger._writer:
            return Violation(
                name="logger_open_during_record",
                description="Recording is active but SyncLogger has no open writer — every ping/event from this point is being dropped silently.",
                severity="critical",
                repair_strategy="reopen_sync_logger",
            )
        return None

    def _inv_session_dir(self) -> Violation | None:
        if not self.is_recording():
            return None
        # Verify the session folder exists if the writer is bound to a path.
        f = getattr(self.sync_logger, "_file", None)
        if f is None or not hasattr(f, "name"):
            return None
        from pathlib import Path
        try:
            session_dir = Path(f.name).parent
            if not session_dir.exists():
                return Violation(
                    name="session_dir_consistent",
                    description=f"Session folder {session_dir} disappeared mid-session.",
                    severity="critical",
                    repair_strategy="recreate_session_dir",
                    extra={"path": str(session_dir)},
                )
        except Exception:
            pass
        return None

    def _inv_required_alive(self) -> Violation | None:
        if not self.is_recording():
            return None
        problems = []
        for dev_name, handler, attr in (
            ("emotibit", self.emotibit, "given_up"),
            ("polar",    self.polar,    "given_up"),
        ):
            if self.is_required(dev_name) and getattr(handler, attr, False):
                problems.append(dev_name)
        if problems:
            return Violation(
                name="required_sensors_alive",
                description=(
                    f"Required sensor(s) in DEGRADED (gave up reconnecting): "
                    f"{', '.join(problems)}. Recording continues without them."
                ),
                severity="error",
                repair_strategy=None,   # operator must replace device or remove requirement
                extra={"sensors": problems},
            )
        return None

    def _inv_calib_not_stuck(self) -> Violation | None:
        # Heuristic: if a handler has been calibrating (calibrated_latency_ns < 0)
        # for more than 30 s after a successful connect, something is wrong.
        # We can't measure "time since connect" without more bookkeeping, so for
        # now we surface only the worst case: still uncalibrated AND recording.
        if not self.is_recording():
            return None
        stuck = []
        for dev_name, handler in (
            ("emotibit", self.emotibit),
            ("polar",    self.polar),
            ("unity",    self.unity),
        ):
            if not self.is_required(dev_name):
                continue
            lat = getattr(handler, "effective_latency_ns", -1)
            if lat == -1 and not getattr(handler, "given_up", False):
                stuck.append(dev_name)
        if stuck:
            return Violation(
                name="no_handler_stuck_calibrating",
                description=(
                    f"Required sensor(s) still uncalibrated during recording: "
                    f"{', '.join(stuck)}. Latency rows will be -1 for these."
                ),
                severity="warn",
                repair_strategy="trigger_recalibration",
                extra={"sensors": stuck},
            )
        return None

    def _inv_emotibit_writing(self) -> Violation | None:
        if not self.is_recording() or not self.is_required("emotibit"):
            return None
        h = self.emotibit
        if getattr(h, "given_up", False):
            return None  # other invariant covers it
        elapsed = getattr(h, "seconds_since_recording_start", 0.0)
        if elapsed < 15.0:
            return None  # grace period
        if not getattr(h, "is_writing", False):
            return Violation(
                name="emotibit_writing_within_grace",
                description=(
                    f"EmotiBit recording started {elapsed:.0f}s ago but device "
                    "has not echoed RS=RB. SD card may be missing or the RB packet was lost."
                ),
                severity="error",
                repair_strategy="resend_rb",
            )
        return None

    def _inv_ping_count_sane(self) -> Violation | None:
        # Sanity: the auto-ping sequence emits AUTO_PING_TOTAL pings. After the
        # first FIRST_PING_DELAY + (TOTAL-1) * INTERVAL ms have elapsed, the count
        # should be at least 1. This catches a wedged auto-ping timer.
        # Cheap to check; no repair beyond logging.
        return None  # disabled until we plumb the timing data through

    def _inv_unity_parser_overloaded(self) -> Violation | None:
        """The Unity DATA parser throttles by error type — once it has seen 5+
        distinct error types, the seen-set is large enough that subsequent
        unique parser failures get harder to spot. Reset the set to give the
        operator visibility on a new failure mode."""
        if self.parser_seen_set_fn is None:
            return None
        seen = self.parser_seen_set_fn()
        if seen is None or len(seen) < 5:
            return None
        return Violation(
            name="unity_parser_overloaded",
            description=(
                f"Unity DATA parser has seen {len(seen)} distinct error types — "
                "resetting the seen-set so newly-appearing failures stay visible."
            ),
            severity="warn",
            repair_strategy="reset_unity_parser",
            extra={"parser_seen_set": seen},
        )


# ── Helper for log-friendly output ────────────────────────────────────────────

def format_violations(violations: List[Violation]) -> str:
    if not violations:
        return "[invariants] all green"
    lines = [f"[invariants] {len(violations)} violation(s):"]
    for v in violations:
        sev = {"warn": "⚠", "error": "✗", "critical": "✗✗"}.get(v.severity, "?")
        lines.append(f"  {sev} {v.name}: {v.description}")
    return "\n".join(lines)
