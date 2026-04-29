"""Tests for src/self_heal.py — the runtime repair daemon."""

import csv
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_system(tmp_path):
    """Build a RepairTechnician with a real SyncLogger and mock handlers."""
    from sync_logger import SyncLogger
    from self_heal  import RepairTechnician

    session_dir = tmp_path / "lsl_testts"
    session_dir.mkdir()
    sl = SyncLogger(tmp_path)
    sl.start_session("testts", session_dir)

    eb  = MagicMock()
    pol = MagicMock()
    un  = MagicMock()
    logged = []

    rt = RepairTechnician(
        emotibit=eb, polar=pol, unity=un, sync_logger=sl,
        log_fn=logged.append,
    )
    rt._cooldown_s = 0.0   # disable rate-limit for tests
    yield rt, eb, pol, un, sl, logged, session_dir
    sl.close()


# ── reopen_sync_logger ───────────────────────────────────────────────────────

def test_reopen_sync_logger_when_writer_is_none(fake_system):
    rt, _eb, _pol, _un, sl, logged, session_dir = fake_system
    from invariants import Violation

    sl.close()   # writer goes to None
    assert sl._writer is None

    results = rt.repair([Violation(
        name="logger_open_during_record",
        description="x", severity="critical",
        repair_strategy="reopen_sync_logger",
    )])

    assert results[0] == ("reopen_sync_logger", "success", "logger_open_during_record")
    assert sl._writer is not None
    # A recovered file should now exist next to the original
    recovered = list(session_dir.glob("syncLog_testts_recovered_*.csv"))
    assert len(recovered) == 1


def test_reopen_sync_logger_no_op_when_writer_open(fake_system):
    rt, _eb, _pol, _un, _sl, _logged, _session_dir = fake_system
    from invariants import Violation
    results = rt.repair([Violation(
        name="logger_open_during_record",
        description="x", repair_strategy="reopen_sync_logger",
    )])
    assert results[0] == ("reopen_sync_logger", "no_op", "logger_open_during_record")


# ── system_repair row appears in syncLog ─────────────────────────────────────

def test_repair_writes_system_repair_row(fake_system, tmp_path):
    rt, _eb, _pol, _un, sl, _logged, _session_dir = fake_system
    from invariants import Violation
    rt.repair([Violation(name="x", description="y", repair_strategy="reset_unity_parser",
                          extra={"parser_seen_set": set()})])
    rows = list(csv.reader(open(_session_dir.parent / "lsl_testts" / "syncLog_testts.csv")))
    repair_rows = [r for r in rows if r and r[1] == "system_repair"]
    assert len(repair_rows) == 1
    assert repair_rows[0][2].startswith("reset_unity_parser:")


# ── reset_unity_parser ───────────────────────────────────────────────────────

def test_reset_unity_parser_clears_set(fake_system):
    rt, *_ = fake_system
    from invariants import Violation
    seen = {"NameError", "ValueError"}
    results = rt.repair([Violation(
        name="x", description="y", repair_strategy="reset_unity_parser",
        extra={"parser_seen_set": seen},
    )])
    assert results[0][1] == "success"
    assert seen == set()


# ── trigger_recalibration ────────────────────────────────────────────────────

def test_trigger_recalibration_calls_handler(fake_system):
    rt, eb, pol, _un, _sl, _logged, _session_dir = fake_system
    from invariants import Violation
    eb.calibrate_for_recording = MagicMock()
    pol.calibrate_for_recording = MagicMock()
    rt.repair([Violation(
        name="x", description="y", repair_strategy="trigger_recalibration",
        extra={"sensors": ["emotibit", "polar"]},
    )])
    eb.calibrate_for_recording.assert_called_once()
    pol.calibrate_for_recording.assert_called_once()


# ── resend_rb ────────────────────────────────────────────────────────────────

def test_resend_rb_invokes_emotibit_start_recording(fake_system):
    rt, eb, _pol, _un, _sl, _logged, _session_dir = fake_system
    from invariants import Violation
    eb.start_recording = MagicMock()
    rt.repair([Violation(name="x", description="y", repair_strategy="resend_rb")])
    eb.start_recording.assert_called_once()


# ── attempt cap ──────────────────────────────────────────────────────────────

def test_max_attempts_per_strategy(fake_system):
    rt, *_ = fake_system
    from invariants import Violation
    rt._max_attempts = 2
    rt._cooldown_s   = 0.0
    v = Violation(name="x", description="y", repair_strategy="reset_unity_parser",
                  extra={"parser_seen_set": set()})
    out1 = rt.repair([v])[0][1]
    out2 = rt.repair([v])[0][1]
    out3 = rt.repair([v])[0][1]   # past cap
    assert out1 == "success"
    assert out2 == "success"
    assert out3 == "skipped"


# ── unknown strategy ─────────────────────────────────────────────────────────

def test_unknown_strategy_logged(fake_system):
    rt, _eb, _pol, _un, _sl, logged, _session_dir = fake_system
    from invariants import Violation
    rt.repair([Violation(name="x", description="y", repair_strategy="not_a_real_strategy")])
    assert any("No strategy" in line for line in logged)


# ── reset() clears counters ──────────────────────────────────────────────────

def test_reset_clears_counters(fake_system):
    rt, *_ = fake_system
    from invariants import Violation
    rt._max_attempts = 1
    v = Violation(name="x", description="y", repair_strategy="reset_unity_parser",
                  extra={"parser_seen_set": set()})
    rt.repair([v])
    assert rt.repair([v])[0][1] == "skipped"   # capped
    rt.reset()
    assert rt.repair([v])[0][1] == "success"   # counter cleared
