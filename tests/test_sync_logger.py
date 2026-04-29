"""Tests for SyncLogger schema and write_event semantics."""

import csv
from pathlib import Path

import pytest


def test_start_session_creates_file_with_header(sync_logger, tmp_path):
    path = tmp_path / "syncLog_testts.csv"
    assert path.exists()
    rows = list(csv.reader(open(path)))
    assert rows[0] == ["machine", "event", "ping_id", "local_epoch_ns", "latency_ns"]


def test_log_ping_writes_lsl_row(sync_logger):
    pid, ns = sync_logger.log_ping()
    assert pid == "ping_001"
    assert ns > 0


def test_log_ping_skips_polar_row_when_disabled(sync_logger, tmp_path):
    sync_logger.log_polar = False
    sync_logger.log_ping(polar_send_ns=0, polar_latency_ns=5_000_000)
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    polar_rows = [r for r in rows if r and r[0] == "polar"]
    assert polar_rows == []


def test_log_ping_skips_emotibit_row_when_disabled(sync_logger, tmp_path):
    sync_logger.log_emotibit = False
    sync_logger.log_ping(emotibit_latency_ns=5_000_000)
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    emo_rows = [r for r in rows if r and r[0] == "emotibit"]
    assert emo_rows == []


# ── write_event (the pass-2 schema fix) ───────────────────────────────────────

def test_write_event_uses_empty_latency_for_non_ping(sync_logger, tmp_path):
    sync_logger.write_event("emotibit", "sensor_lost")
    sync_logger.write_event("unity", "headset_doffed")
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    for row in rows[1:]:   # skip header
        assert row[4] == "", \
            f"non-ping rows must have empty latency_ns (not -1): {row}"


def test_write_event_auto_fills_local_epoch_ns(sync_logger, tmp_path):
    sync_logger.write_event("emotibit", "sensor_lost")
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    assert rows[1][3] != "", "local_epoch_ns should be auto-filled"
    assert int(rows[1][3]) > 0


def test_write_event_no_writer_is_safe(tmp_path):
    """Calling write_event before start_session must not crash."""
    from sync_logger import SyncLogger
    sl = SyncLogger(tmp_path)
    # No start_session — _writer is None
    sl.write_event("emotibit", "sensor_lost")   # must be a no-op, not a crash


# ── log_unity_ack ─────────────────────────────────────────────────────────────

def test_log_unity_ack_writes_unity_row(sync_logger, tmp_path):
    sync_logger.log_unity = True
    sync_logger.log_unity_ack("ping_001", 1234567890, 5_000_000)
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    unity_rows = [r for r in rows if r and r[0] == "unity"]
    assert len(unity_rows) == 1
    assert unity_rows[0] == ["unity", "ping_received", "ping_001",
                             "1234567890", "5000000"]


def test_log_unity_ack_respects_log_unity_flag(sync_logger, tmp_path):
    sync_logger.log_unity = False
    sync_logger.log_unity_ack("ping_001", 1234567890, 5_000_000)
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    unity_rows = [r for r in rows if r and r[0] == "unity"]
    assert unity_rows == []


# ── Schema validation (G5) ────────────────────────────────────────────────────

def test_validate_row_accepts_valid():
    from sync_logger import _validate_row
    ok, _ = _validate_row("emotibit", "sensor_lost", "", 1234567890, "")
    assert ok
    ok, _ = _validate_row("lsl", "ping_sent", "ping_001", 1234567890, 0)
    assert ok
    ok, _ = _validate_row("polar", "ping_received", "ping_001", 1234567890, -1)
    assert ok


def test_validate_row_rejects_unknown_machine():
    from sync_logger import _validate_row
    ok, msg = _validate_row("xbox", "ping_sent", "p1", 0, 0)
    assert not ok
    assert "machine" in msg


def test_validate_row_rejects_unknown_event():
    from sync_logger import _validate_row
    ok, msg = _validate_row("emotibit", "exploded", "", 0, "")
    assert not ok
    assert "event" in msg


def test_validate_row_rejects_negative_local_epoch():
    from sync_logger import _validate_row
    ok, msg = _validate_row("lsl", "ping_sent", "p1", -5, 0)
    assert not ok
    assert "local_epoch_ns" in msg


def test_validate_row_rejects_latency_below_minus_one():
    from sync_logger import _validate_row
    ok, msg = _validate_row("polar", "ping_received", "p1", 0, -2)
    assert not ok
    assert "latency_ns" in msg


def test_invalid_row_dropped_not_written(sync_logger, tmp_path, capsys):
    """A row that fails validation must not appear in the file."""
    sync_logger._write_row("xbox", "ping_sent", "p1", 0, 0)   # invalid machine
    rows = list(csv.reader(open(tmp_path / "syncLog_testts.csv")))
    bad = [r for r in rows[1:] if r and r[0] == "xbox"]
    assert bad == []
    err = capsys.readouterr().err
    assert "dropped invalid row" in err
