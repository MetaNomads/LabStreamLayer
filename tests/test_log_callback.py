"""Tests for the SyncLogger schema-validation log callback (H3 wiring)."""

from sync_logger import SyncLogger


def test_log_callback_invoked_on_invalid_row(tmp_path):
    """A row that fails validation routes to the operator log AND stays out of disk."""
    sl = SyncLogger(tmp_path)
    received = []
    sl.set_log_callback(received.append)
    sl.start_session("ts", tmp_path)
    sl._write_row("xbox", "ping_sent", "p1", 0, 0)   # bogus machine
    assert any("dropped invalid row" in m for m in received), \
        f"log callback not called; received: {received}"
    sl.close()


def test_log_callback_silent_on_valid_row(tmp_path):
    sl = SyncLogger(tmp_path)
    received = []
    sl.set_log_callback(received.append)
    sl.start_session("ts", tmp_path)
    sl._write_row("emotibit", "sensor_lost", "", 1234567890, "")
    assert received == [], f"unexpected log message on valid row: {received}"
    sl.close()


def test_log_callback_ctor_arg(tmp_path):
    """Pass through the constructor too."""
    received = []
    sl = SyncLogger(tmp_path, log_callback=received.append)
    sl.start_session("ts", tmp_path)
    sl._write_row("xbox", "ping_sent", "p1", 0, 0)
    assert received and "dropped" in received[0]
    sl.close()


def test_callback_exception_swallowed(tmp_path):
    """If the callback itself raises, sync_logger must not crash."""
    sl = SyncLogger(tmp_path)
    def boom(_msg): raise RuntimeError("test")
    sl.set_log_callback(boom)
    sl.start_session("ts", tmp_path)
    sl._write_row("xbox", "ping_sent", "p1", 0, 0)   # bad row, callback raises — must survive
    sl.close()
