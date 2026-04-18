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


class SyncLogger:

    def __init__(self, output_dir: Path):
        self._output_dir = output_dir
        self._file       = None
        self._writer     = None
        self._count      = 0
        self.log_emotibit = True
        self.log_polar    = True
        self.log_unity    = False

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
        return path

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
            self._writer.writerow(["lsl", "ping_sent", pid, send_ns, 0])

            # Polar — local_epoch_ns = LSL clock at send (BLE has no device echo)
            if self.log_polar and polar_latency_ns >= 0:
                self._writer.writerow(
                    ["polar", "ping_received", pid, polar_send_ns or send_ns, polar_latency_ns]
                )

            # EmotiBit — no receipt timestamp available
            if self.log_emotibit and emotibit_latency_ns >= 0:
                self._writer.writerow(
                    ["emotibit", "ping_received", pid, "", emotibit_latency_ns]
                )

            self._file.flush()

        return pid, send_ns

    def log_unity_ack(self, ping_id: str, unity_epoch_ns: int, latency_ns: int):
        """
        Write Unity row when ACK arrives with Unity's local timestamp.
        Called from unity.py when ACK:ping_NNN:<unity_ns> is received.
        """
        if self._writer and self.log_unity:
            self._writer.writerow(
                ["unity", "ping_received", ping_id, unity_epoch_ns, latency_ns]
            )
            self._file.flush()

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
