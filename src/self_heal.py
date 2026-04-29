"""
self_heal.py — runtime repair daemon for LSL.

Pairs with invariants.py: when an invariant fires with `repair_strategy=<name>`,
RepairTechnician looks up that strategy and runs it. Each repair attempt is
logged AND emits a `system_repair, <strategy>, <outcome>` row into the syncLog
so the audit trail survives.

Philosophy: be conservative. Auto-repair only what is GENUINELY safe. Things
that touch live BLE or Quest state are NOT auto-repaired here — those go through
the existing reconnect/watchdog paths which are already designed for that.

Safe repairs implemented:
  - reopen_sync_logger:    SyncLogger writer is None mid-recording → reopen with
                           a `_recovered_<n>` suffix and write a marker.
  - recreate_session_dir:  Session folder vanished → recreate it.
  - resend_rb:             EmotiBit not writing past grace → re-send RB.
  - trigger_recalibration: Required sensor uncalibrated → call calibrate_for_recording().
  - reset_unity_parser:    Unity parser has logged too many errors → clear the seen-set.

Strategies the technician explicitly DOES NOT attempt:
  - Spawning new threads to replace dead ones (could corrupt state).
  - Restarting BLE — the on_disconnect / auto_reconnect paths own that.
  - Anything that mutates the syncLog file structurally (only appends are safe).

Limits & rate:
  - max_attempts_per_strategy_per_session = 3  (avoid repair loops)
  - cooldown_s = 10 (don't attempt the same repair more than once per 10s)
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable, Dict, List

from invariants import Violation


# ── RepairOutcome ─────────────────────────────────────────────────────────────

class RepairOutcome:
    SUCCESS    = "success"
    SKIPPED    = "skipped"      # rate-limited or attempt-cap reached
    NO_OP      = "no_op"        # strategy ran but determined nothing to do
    FAILED     = "failed"       # strategy ran and threw / returned False


# ── RepairTechnician ──────────────────────────────────────────────────────────

class RepairTechnician:

    def __init__(self, *, emotibit, polar, unity, sync_logger, log_fn: Callable[[str], None]):
        self.emotibit = emotibit
        self.polar    = polar
        self.unity    = unity
        self.sync_logger = sync_logger
        self.log         = log_fn   # one-arg callable for operator-visible log
        self._attempts:  Dict[str, int] = defaultdict(int)
        self._last_run:  Dict[str, float] = defaultdict(float)
        self._max_attempts = 3
        self._cooldown_s   = 10.0
        self._strategies:  Dict[str, Callable[[Violation], str]] = {}
        self._register_defaults()

    def register(self, name: str, fn: Callable[[Violation], str]):
        self._strategies[name] = fn

    def _register_defaults(self):
        self.register("reopen_sync_logger",     self._reopen_sync_logger)
        self.register("recreate_session_dir",   self._recreate_session_dir)
        self.register("resend_rb",              self._resend_rb)
        self.register("trigger_recalibration",  self._trigger_recalibration)
        self.register("reset_unity_parser",     self._reset_unity_parser)

    # ── Top-level driver ─────────────────────────────────────────────────────

    def repair(self, violations: List[Violation]) -> List[tuple]:
        """Attempt repair for every violation that names a known strategy.
        Returns a list of (strategy, outcome, violation_name) tuples for logging.
        Each repair attempt also writes a system_repair row into the syncLog."""
        results: List[tuple] = []
        now = time.monotonic()
        for v in violations:
            strat = v.repair_strategy
            if not strat:
                continue
            if strat not in self._strategies:
                self.log(f"[self-heal] No strategy for '{strat}' — skipping")
                results.append((strat, RepairOutcome.SKIPPED, v.name))
                continue
            # Rate limit & attempt cap
            if self._attempts[strat] >= self._max_attempts:
                results.append((strat, RepairOutcome.SKIPPED, v.name))
                continue
            if now - self._last_run[strat] < self._cooldown_s:
                results.append((strat, RepairOutcome.SKIPPED, v.name))
                continue
            self._attempts[strat] += 1
            self._last_run[strat] = now
            try:
                outcome = self._strategies[strat](v) or RepairOutcome.SUCCESS
            except Exception as e:
                outcome = f"{RepairOutcome.FAILED}:{type(e).__name__}"
                self.log(f"[self-heal] {strat} raised {e}")
            results.append((strat, outcome, v.name))
            self.log(f"[self-heal] {strat} → {outcome} (for {v.name})")
            self._emit_repair_row(strat, outcome, v.name)
        return results

    def reset(self):
        """Clear attempt counters — called at session start."""
        self._attempts.clear()
        self._last_run.clear()

    def _emit_repair_row(self, strategy: str, outcome: str, violation_name: str):
        """Persist the repair attempt into the syncLog so the post-hoc analyst
        can see what self-heal did and when."""
        try:
            self.sync_logger.write_event(
                machine="lsl",
                event="system_repair",
                ping_id=f"{strategy}:{outcome}:{violation_name}",
            )
        except Exception:
            pass  # best-effort

    # ── Strategy implementations ────────────────────────────────────────────

    def _reopen_sync_logger(self, v: Violation) -> str:
        """If the SyncLogger writer is None mid-recording, reopen it with a
        _recovered_<n>.csv suffix so the original file isn't overwritten."""
        sl = self.sync_logger
        if sl._writer is not None:
            return RepairOutcome.NO_OP
        # Find a free recovered-N filename next to the original
        from pathlib import Path
        # We have to reconstruct the session_dir somehow — use _output_dir as a
        # starting point and look for the most recent lsl_* folder.
        try:
            base = sl._output_dir
            sessions = sorted(p for p in Path(base).glob("lsl_*") if p.is_dir())
            if not sessions:
                return RepairOutcome.FAILED
            session_dir = sessions[-1]
            session_ts  = session_dir.name.replace("lsl_", "", 1)
            n = 1
            while (session_dir / f"syncLog_{session_ts}_recovered_{n}.csv").exists():
                n += 1
            recovered_path = session_dir / f"syncLog_{session_ts}_recovered_{n}.csv"
            f = open(recovered_path, "w", newline="", buffering=1)
            import csv
            sl._file   = f
            sl._writer = csv.writer(f)
            sl._writer.writerow(["machine", "event", "ping_id", "local_epoch_ns", "latency_ns"])
            f.flush()
            self.log(f"[self-heal] Reopened sync log → {recovered_path.name}")
            return RepairOutcome.SUCCESS
        except Exception as e:
            self.log(f"[self-heal] reopen_sync_logger failed: {e}")
            return RepairOutcome.FAILED

    def _recreate_session_dir(self, v: Violation) -> str:
        from pathlib import Path
        path = v.extra.get("path")
        if not path:
            return RepairOutcome.NO_OP
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            return RepairOutcome.SUCCESS
        except Exception:
            return RepairOutcome.FAILED

    def _resend_rb(self, v: Violation) -> str:
        try:
            if hasattr(self.emotibit, "start_recording"):
                self.emotibit.start_recording()
                return RepairOutcome.SUCCESS
        except Exception:
            return RepairOutcome.FAILED
        return RepairOutcome.NO_OP

    def _trigger_recalibration(self, v: Violation) -> str:
        sensors = v.extra.get("sensors", [])
        ran = False
        for s in sensors:
            handler = {"emotibit": self.emotibit, "polar": self.polar,
                       "unity": self.unity}.get(s)
            if handler and hasattr(handler, "calibrate_for_recording"):
                try:
                    handler.calibrate_for_recording()
                    ran = True
                except Exception:
                    pass
        return RepairOutcome.SUCCESS if ran else RepairOutcome.NO_OP

    def _reset_unity_parser(self, v: Violation) -> str:
        # Reset the parser-warning seen-set (held on MainWindow as
        # _unity_parse_seen). Caller passes it in v.extra.
        target = v.extra.get("parser_seen_set")
        if isinstance(target, set):
            target.clear()
            return RepairOutcome.SUCCESS
        return RepairOutcome.NO_OP
