"""
sync_logger.py — Writes syncLog_DATETIME.csv inside lsl_DATETIME/ session folder

Columns: machine, event, ping_id, local_epoch_ns, latency_ns

  machine        — lsl | polar | emotibit | unity
  event          — ping_sent | ping_received
  ping_id        — e.g. ping_001
  local_epoch_ns — the clock of the machine that wrote this row:
                     lsl row     : LSL clock at send time
                     polar row   : LSL clock at send time (BLE, no device echo)
                     emotibit row: empty (no receipt confirmation)
                     unity row   : Unity clock at receipt (returned in ACK)
  latency_ns     — calibrated one-way latency (0 for lsl row)

Example:
  lsl,      ping_sent,     ping_001, 1744500000000000000, 0
  polar,     ping_received, ping_001, 1744500000004800000, 4800000
  emotibit,  ping_received, ping_001, ,                   6200000
  unity,     ping_received, ping_001, 1744500123456789000, 4400000
"""

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

from contracts import requires, ensures, Contract

# ── Row schema ────────────────────────────────────────────────────────────────
# Single source of truth for the syncLog CSV schema. Used by every write path
# below so a row that doesn't match is caught at the producer, not by a
# downstream pandas reader six months later.

VALID_MACHINES = {"lsl", "polar", "emotibit", "unity"}
VALID_EVENTS   = {
    # Ping-cycle events
    "ping_sent", "ping_received",
    # Sensor lifecycle (handler-emitted)
    "sensor_lost", "sensor_recovered", "given_up",
    # Watchdog gap detection
    "sensor_silent", "sensor_resumed",
    # Quest lifecycle
    "headset_doffed", "headset_donned", "app_quitting",
    # Self-heal
    "system_repair",
}


def _validate_row(machine, event, ping_id, local_epoch_ns, latency_ns):
    """Returns (ok: bool, msg: str). Cheap — runs on every write."""
    if machine not in VALID_MACHINES:
        return False, f"machine '{machine}' not in {VALID_MACHINES}"
    if event not in VALID_EVENTS:
        return False, f"event '{event}' not in known events"
    # local_epoch_ns must be empty or a non-negative int (or castable)
    if local_epoch_ns != "":
        try:
            v = int(local_epoch_ns)
            if v < 0:
                return False, f"local_epoch_ns must be >=0 (got {v})"
        except (TypeError, ValueError):
            return False, f"local_epoch_ns must be int or '' (got {local_epoch_ns!r})"
    # latency_ns: empty for non-ping rows, int (>= -1) for ping rows
    if latency_ns != "":
        try:
            v = int(latency_ns)
            if v < -1:
                return False, f"latency_ns must be >= -1 (got {v})"
        except (TypeError, ValueError):
            return False, f"latency_ns must be int or '' (got {latency_ns!r})"
    return True, ""


class SyncLogger:

    def __init__(self, output_dir: Path, log_callback=None):
        """
        log_callback: optional one-arg callable that receives string messages
        when the schema validator drops a row. Wire MainWindow._log into it so
        the operator sees bad rows instead of just stderr.
        """
        self._output_dir  = output_dir
        self._file        = None
        self._writer      = None
        self._count       = 0
        self._log_callback = log_callback
        self.log_emotibit = True
        self.log_polar    = True
        self.log_unity    = False

    def set_log_callback(self, fn):
        """Allow late binding (MainWindow can wire its log AFTER construction)."""
        self._log_callback = fn

    def _write_row(self, machine, event, ping_id, local_epoch_ns, latency_ns):
        """Single chokepoint for every CSV row. Validates before writing —
        a producer that emits a malformed row gets a logged warning here, so
        the syncLog stays self-consistent even when a future change adds a
        bad call site by accident."""
        if not self._writer:
            return
        ok, msg = _validate_row(machine, event, ping_id, local_epoch_ns, latency_ns)
        if not ok:
            full = (f"[sync_logger] dropped invalid row: {msg}  "
                    f"({machine}, {event}, {ping_id}, {local_epoch_ns}, {latency_ns})")
            # Surface to the operator log if wired; ALSO print to stderr so
            # tests, the smoke script, and any non-GUI invocation still see it.
            if self._log_callback is not None:
                try: self._log_callback(full)
                except Exception: pass
            import sys
            print(full, file=sys.stderr)
            return
        try:
            self._writer.writerow([machine, event, ping_id, local_epoch_ns, latency_ns])
            self._file.flush()
        except Exception:
            pass

    @requires(lambda self, session_ts, session_dir=None:
              isinstance(session_ts, str) and len(session_ts) > 0,
              "session_ts must be a non-empty string")
    @requires(lambda self, session_ts, session_dir=None: self._writer is None,
              "start_session must not be called when a session is already open — close() first")
    @ensures(lambda result, *_args, **_kw: hasattr(result, "exists"),
             "start_session must return a Path")
    def start_session(self, session_ts: str, session_dir: "Path | None" = None) -> Path:
        """
        session_dir: pre-created folder (lsl_TIMESTAMP/).
        Falls back to _output_dir if not provided.
        """
        folder = session_dir if session_dir else self._output_dir
        folder.mkdir(parents=True, exist_ok=True)
        self._count = 0
        path = folder / f"syncLog_{session_ts}.csv"
        self._file   = open(path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(["machine", "event", "ping_id", "local_epoch_ns", "latency_ns"])
        self._file.flush()
        return path

    @ensures(lambda result, *_args, **_kw: (isinstance(result, tuple) and len(result) == 2
                                 and isinstance(result[0], str)
                                 and result[0].startswith("ping_")),
             "log_ping must return (ping_id:str, send_ns:int)")
    def log_ping(
        self,
        polar_send_ns:       int = 0,
        polar_latency_ns:    int = -1,
        emotibit_latency_ns: int = -1,
    ) -> tuple:
        """
        Write LSL + Polar + EmotiBit rows at ping send time.
        Returns (ping_id, send_ns).
        Unity row written separately via log_unity_ack() when ACK arrives.
        """
        self._count += 1
        pid     = f"ping_{self._count:03d}"
        send_ns = time.time_ns()

        if self._writer:
            # LSL row
            self._write_row("lsl", "ping_sent", pid, send_ns, 0)

            # Polar — local_epoch_ns = LSL clock at send (BLE has no device echo)
            if self.log_polar and polar_latency_ns >= 0:
                self._write_row("polar", "ping_received", pid,
                                polar_send_ns or send_ns, polar_latency_ns)

            # EmotiBit — no receipt timestamp available
            if self.log_emotibit and emotibit_latency_ns >= 0:
                self._write_row("emotibit", "ping_received", pid, "", emotibit_latency_ns)

        return pid, send_ns

    def log_unity_ack(self, ping_id: str, unity_epoch_ns: int, latency_ns: int):
        """
        Write Unity row when ACK arrives with Unity's local timestamp.
        Called from unity.py when ACK:ping_NNN:<unity_ns> is received.
        """
        if self._writer and self.log_unity:
            self._write_row("unity", "ping_received", ping_id, unity_epoch_ns, latency_ns)

    @requires(lambda self, machine, event, ping_id="", local_epoch_ns="", latency_ns="":
              machine in ("lsl", "polar", "emotibit", "unity"),
              "machine must be one of lsl/polar/emotibit/unity")
    def write_event(self, machine: str, event: str, ping_id: str = "",
                    local_epoch_ns: "int|str" = "", latency_ns: "int|str" = ""):
        """
        Public path for non-ping rows: sensor_silent / sensor_resumed /
        sensor_lost / sensor_recovered / headset_doffed / headset_donned /
        app_quitting. `latency_ns` defaults to "" (empty) for these — empty
        means "not applicable", as opposed to -1 which means "device not
        connected during a real ping". This matters for analysis pipelines
        that aggregate latency across rows.
        """
        if not self._writer:
            return
        if local_epoch_ns == "":
            local_epoch_ns = time.time_ns()
        self._write_row(machine, event, ping_id, local_epoch_ns, latency_ns)

    def close(self):
        if self._file:
            self._file.flush()
            self._file.close()
            self._file   = None
            self._writer = None

    @property
    def ping_count(self) -> int:
        return self._count

    @staticmethod
    def make_session_timestamp() -> str:
        from zoneinfo import ZoneInfo
        return datetime.now(tz=ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d_%H-%M-%S")
