"""Tests for src/invariants.py — the runtime state-consistency checker."""

from unittest.mock import MagicMock
import pytest


@pytest.fixture
def system(tmp_path):
    """Build a SystemInvariants with mocked handlers and a real SyncLogger."""
    from sync_logger import SyncLogger
    from invariants import SystemInvariants

    sl = SyncLogger(tmp_path)
    sl.start_session("testts", tmp_path)

    eb = MagicMock(name="EmotiBit")
    eb.given_up = False
    eb.effective_latency_ns = 5_000_000
    eb.is_writing = True
    eb.seconds_since_recording_start = 5.0
    pol = MagicMock(name="Polar")
    pol.given_up = False
    pol.effective_latency_ns = 5_000_000
    un = MagicMock(name="Unity")
    un.effective_latency_ns = 5_000_000

    state = {"recording": True, "required": {"emotibit", "polar"}}
    inv = SystemInvariants(
        emotibit=eb, polar=pol, unity=un, sync_logger=sl,
        is_recording_fn=lambda: state["recording"],
        required_fn=lambda d: d in state["required"],
        sample_silence_s={"emotibit": 5.0, "polar": 2.0, "unity": 6.0},
    )
    yield inv, eb, pol, un, sl, state
    sl.close()


# ── logger_open_during_record ────────────────────────────────────────────────

def test_no_violation_when_logger_open_and_recording(system):
    inv, *_ = system
    violations = inv.check_all()
    assert all(v.name != "logger_open_during_record" for v in violations)


def test_violation_when_logger_closed_during_record(system):
    inv, _eb, _pol, _un, sl, _state = system
    sl.close()
    violations = inv.check_all()
    names = {v.name for v in violations}
    assert "logger_open_during_record" in names
    v = next(v for v in violations if v.name == "logger_open_during_record")
    assert v.severity == "critical"
    assert v.repair_strategy == "reopen_sync_logger"


def test_no_violation_when_not_recording(system):
    inv, _eb, _pol, _un, sl, state = system
    sl.close()
    state["recording"] = False
    violations = inv.check_all()
    # Closing the logger shouldn't be a violation outside recording.
    assert all(v.name != "logger_open_during_record" for v in violations)


# ── required_sensors_alive ───────────────────────────────────────────────────

def test_violation_when_required_sensor_given_up(system):
    inv, eb, _pol, _un, _sl, _state = system
    eb.given_up = True
    violations = inv.check_all()
    names = {v.name for v in violations}
    assert "required_sensors_alive" in names


def test_no_violation_when_unrequired_sensor_given_up(system):
    inv, _eb, pol, _un, _sl, state = system
    state["required"] = {"emotibit"}   # polar no longer required
    pol.given_up = True
    violations = inv.check_all()
    assert all("polar" not in v.extra.get("sensors", []) for v in violations)


# ── emotibit_writing_within_grace ────────────────────────────────────────────

def test_violation_when_emotibit_not_writing_after_grace(system):
    inv, eb, _pol, _un, _sl, _state = system
    eb.is_writing = False
    eb.seconds_since_recording_start = 20.0   # past 15s grace
    violations = inv.check_all()
    assert any(v.name == "emotibit_writing_within_grace" for v in violations)


def test_no_violation_during_grace_period(system):
    inv, eb, _pol, _un, _sl, _state = system
    eb.is_writing = False
    eb.seconds_since_recording_start = 5.0   # in grace
    violations = inv.check_all()
    assert all(v.name != "emotibit_writing_within_grace" for v in violations)


# ── no_handler_stuck_calibrating ─────────────────────────────────────────────

def test_violation_when_required_sensor_uncalibrated(system):
    inv, eb, _pol, _un, _sl, _state = system
    eb.effective_latency_ns = -1
    violations = inv.check_all()
    assert any(v.name == "no_handler_stuck_calibrating" for v in violations)


# ── format_violations ────────────────────────────────────────────────────────

def test_format_violations_empty():
    from invariants import format_violations
    assert format_violations([]) == "[invariants] all green"


def test_format_violations_renders():
    from invariants import format_violations, Violation
    s = format_violations([
        Violation(name="x", description="something broke", severity="critical"),
    ])
    assert "x" in s and "something broke" in s


# ── register custom invariant ────────────────────────────────────────────────

def test_register_custom_invariant(system):
    inv, *_ = system
    from invariants import Violation
    inv.register("custom_x", lambda: Violation(
        name="custom_x", description="hello", severity="warn"
    ))
    violations = inv.check_all()
    assert any(v.name == "custom_x" for v in violations)


def test_buggy_invariant_does_not_crash_check_all(system):
    inv, *_ = system
    inv.register("buggy", lambda: 1/0)   # ZeroDivisionError
    violations = inv.check_all()
    bug = next(v for v in violations if v.name == "buggy")
    assert bug.severity == "error"
    assert "ZeroDivisionError" in bug.description


# ── unity_parser_overloaded ──────────────────────────────────────────────────

def test_unity_parser_overloaded_fires_at_threshold(tmp_path):
    """When the Unity parser has seen 5+ distinct error types, the invariant
    must fire and route to the reset_unity_parser strategy."""
    from unittest.mock import MagicMock
    from sync_logger import SyncLogger
    from invariants  import SystemInvariants

    sl = SyncLogger(tmp_path); sl.start_session("ts", tmp_path)
    parser_seen = {"NameError", "ValueError", "TypeError", "KeyError", "IndexError"}
    inv = SystemInvariants(
        emotibit=MagicMock(given_up=False, effective_latency_ns=1, is_writing=True,
                            seconds_since_recording_start=5.0),
        polar=MagicMock(given_up=False, effective_latency_ns=1),
        unity=MagicMock(effective_latency_ns=1),
        sync_logger=sl,
        is_recording_fn=lambda: True,
        required_fn=lambda d: True,
        sample_silence_s={"emotibit": 5.0, "polar": 2.0, "unity": 6.0},
        parser_seen_set_fn=lambda: parser_seen,
    )
    violations = inv.check_all()
    overload = next((v for v in violations if v.name == "unity_parser_overloaded"), None)
    assert overload is not None
    assert overload.repair_strategy == "reset_unity_parser"
    # The same set object must be threaded so the strategy can clear it.
    assert overload.extra["parser_seen_set"] is parser_seen
    sl.close()


def test_unity_parser_overloaded_silent_below_threshold(tmp_path):
    from unittest.mock import MagicMock
    from sync_logger import SyncLogger
    from invariants  import SystemInvariants

    sl = SyncLogger(tmp_path); sl.start_session("ts", tmp_path)
    parser_seen = {"NameError"}   # 1 error type — below threshold
    inv = SystemInvariants(
        emotibit=MagicMock(given_up=False, effective_latency_ns=1, is_writing=True,
                            seconds_since_recording_start=5.0),
        polar=MagicMock(given_up=False, effective_latency_ns=1),
        unity=MagicMock(effective_latency_ns=1),
        sync_logger=sl,
        is_recording_fn=lambda: True,
        required_fn=lambda d: True,
        sample_silence_s={"emotibit": 5.0, "polar": 2.0, "unity": 6.0},
        parser_seen_set_fn=lambda: parser_seen,
    )
    violations = inv.check_all()
    assert all(v.name != "unity_parser_overloaded" for v in violations)
    sl.close()
