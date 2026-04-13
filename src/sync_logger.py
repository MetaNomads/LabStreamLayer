"""
sync_logger.py — Writes pingLog_DATETIME.csv

Format: one row per event, long/tidy format.

Columns:
  machine        — lsl, emotibit, polar, unity
  event          — ping_sent (lsl only), ping_received (devices)
  ping_id        — e.g. ping_001
  lsl_epoch_ns   — LSL machine UTC ns at send time (only for lsl row, null for devices)
  latency_ns     — one-way travel time in ns (0 for lsl row, calibrated value for devices)

Only rows for devices that are checked as required are written.

Example (emotibit + polar required, unity not):
  machine,   event,          ping_id,   lsl_epoch_ns,         latency_ns
  lsl,       ping_sent,      ping_001,  1744500000000000000,  0
  emotibit,  ping_received,  ping_001,  ,                     6200000
  polar,     ping_received,  ping_001,  ,                     4800000

Post-processing:
  device_receive_time_in_lsl_clock    = lsl_epoch_ns + latency_ns
  device_receive_time_in_device_clock = device_recorded_time - latency_ns
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
        # Which devices to log — set by main_window before session start
        self.log_emotibit = True
        self.log_polar    = True
        self.log_unity    = False

    def start_session(self, session_ts: str) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._count = 0
        # File name: pingLog_YYYY-MM-DD_HH-MM-SS
        path = self._output_dir / f"pingLog_{session_ts}.csv"
        self._file   = open(path, "w", newline="", buffering=1)
        self._writer = csv.writer(self._file)
        self._writer.writerow(["machine", "event", "ping_id", "lsl_epoch_ns", "latency_ns"])
        return path

    def log_ping(
        self,
        emotibit_latency_ns: int = -1,
        polar_latency_ns:    int = -1,
        unity_latency_ns:    int = -1,
    ) -> tuple:
        """
        Write one ping. LSL row always written. Device rows only if checked.
        lsl_epoch_ns is populated only for the lsl row (null for devices).
        Returns (ping_id, send_ns).
        """
        self._count += 1
        pid     = f"ping_{self._count:03d}"
        send_ns = time.time_ns()

        if self._writer:
            # LSL row — origin, lsl_epoch_ns populated, latency=0
            self._writer.writerow(["lsl", "ping_sent", pid, send_ns, 0])

            # Device rows — lsl_epoch_ns is null, latency is one-way
            if self.log_emotibit:
                self._writer.writerow(["emotibit", "ping_received", pid, "", emotibit_latency_ns])
            if self.log_polar:
                self._writer.writerow(["polar", "ping_received", pid, "", polar_latency_ns])
            if self.log_unity:
                self._writer.writerow(["unity", "ping_received", pid, "", unity_latency_ns])

            self._file.flush()

        return pid, send_ns

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
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
