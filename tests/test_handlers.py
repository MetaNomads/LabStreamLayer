"""Tests for the three sensor handlers' init, public_summary, and source-IP gates."""

import pytest


# ── EmotiBit ─────────────────────────────────────────────────────────────────

class TestEmotiBitHandler:
    def test_init_defines_all_pass1_critical_attrs(self, emotibit_handler):
        h = emotibit_handler
        for name in ("_rtt_buffer", "_continuous_calib_active",
                     "_session_latency_ns", "_last_sample_ns", "_given_up",
                     "_shutdown_event"):
            assert hasattr(h, name), f"missing {name}"

    def test_effective_latency_starts_uncalibrated(self, emotibit_handler):
        assert emotibit_handler.effective_latency_ns == -1

    def test_add_manual_device_alias(self, emotibit_handler):
        # main_window.py:534 uses this name. Pre-fix: AttributeError.
        assert callable(emotibit_handler.add_manual_device)
        assert emotibit_handler.add_manual_device("not-an-ip") is None

    def test_public_summary_is_dict_with_required_keys(self, emotibit_handler):
        s = emotibit_handler.public_summary()
        assert isinstance(s, dict)
        for key in ("ip", "device_id", "session_latency_ns", "given_up"):
            assert key in s, f"public_summary missing {key}"

    def test_hh_event_gated_on_connected_ip(self, emotibit_handler):
        from emotibit import EmotiBitDevice
        h = emotibit_handler
        h._connected = EmotiBitDevice(ip="10.0.0.1")
        h._hh_event.clear()
        # Wrong IP — must NOT set
        h._parse_line("123,1,0,HH,1,100,DI,foo,DP,3131", "10.0.0.99")
        assert not h._hh_event.is_set()
        # Right IP — must set
        h._parse_line("123,1,0,HH,1,100,DI,foo,DP,3131", "10.0.0.1")
        assert h._hh_event.is_set()

    def test_em_rs_rb_updates_last_sample_ns(self, emotibit_handler):
        h = emotibit_handler
        before = h._last_sample_ns
        h._parse_line("123,1,0,EM,1,100,RS,RB", "10.0.0.1")
        assert h._last_sample_ns > before, \
            "EM RS=RB should refresh _last_sample_ns to keep liveness check happy"

    def test_shutdown_event_wakes_quickly(self, emotibit_handler):
        import threading, time
        h = emotibit_handler
        woke = []
        threading.Thread(
            target=lambda: woke.append(h._shutdown_event.wait(timeout=10.0)),
            daemon=True,
        ).start()
        time.sleep(0.05)
        t0 = time.monotonic()
        h.stop()
        deadline = t0 + 0.5
        while not woke and time.monotonic() < deadline:
            time.sleep(0.01)
        assert woke and woke[0] is True
        assert time.monotonic() - t0 < 0.5


# ── Unity ────────────────────────────────────────────────────────────────────

class TestUnityHandler:
    def test_reconnect_processed_when_device_is_none(self, unity_handler):
        # Pass-1 IP gate broke this. Pass-2 fix hoisted RECONNECT above the gate.
        h = unity_handler
        assert h._device is None
        h._handle("RECONNECT,Quest_42", "192.168.1.50")
        assert h._device is not None
        assert h._device.ip == "192.168.1.50"

    def test_ping_dropped_from_wrong_ip(self, unity_handler):
        from unity import UnityDevice
        h = unity_handler
        h._device = UnityDevice(ip="10.0.0.1", name="real")
        fired = [0]
        h.ping_requested.connect(lambda: fired.__setitem__(0, fired[0] + 1))
        h._handle("PING", "10.0.0.99")
        assert fired[0] == 0
        h._handle("PING", "10.0.0.1")
        assert fired[0] == 1

    def test_ack_with_unity_ns_fires_signal(self, unity_handler):
        from unity import UnityDevice
        h = unity_handler
        h._device = UnityDevice(ip="10.0.0.1", name="X")
        fired = []
        h.unity_ack_received.connect(lambda pid, ns: fired.append((pid, int(ns))))
        h._handle("ACK:ping_007:1234567890", "10.0.0.1")
        assert fired == [("ping_007", 1234567890)]

    def test_old_ack_format_does_not_fire(self, unity_handler):
        from unity import UnityDevice
        h = unity_handler
        h._device = UnityDevice(ip="10.0.0.1", name="X")
        fired = []
        h.unity_ack_received.connect(lambda *a: fired.append(a))
        h._handle("ACK:ping_007", "10.0.0.1")
        assert fired == []

    def test_headset_state_signals(self, unity_handler):
        from unity import UnityDevice
        h = unity_handler
        h._device = UnityDevice(ip="10.0.0.1", name="X")
        seen = []
        h.headset_state_changed.connect(seen.append)
        for label in ("headset_doffed", "headset_donned", "app_quitting"):
            h._handle(label, "10.0.0.1")
        assert seen == ["headset_doffed", "headset_donned", "app_quitting"]

    def test_public_summary_is_dict(self, unity_handler):
        s = unity_handler.public_summary()
        assert isinstance(s, dict)
        for key in ("ip", "name", "session_latency_ns", "has_streaming_data"):
            assert key in s


# ── Polar ────────────────────────────────────────────────────────────────────

class TestPolarHandler:
    def test_public_summary_includes_calibration_method(self, polar_handler):
        s = polar_handler.public_summary()
        assert s["calibration_method"] == "battery_char_read", \
            "calibration method must be honestly named (BATTERY_CHAR is cache, not radio RTT)"

    def test_pending_reconnect_attribute_exists(self, polar_handler):
        # Pass-2 fix: cancellable handle for the call_later reconnect.
        assert hasattr(polar_handler, "_pending_reconnect")
        assert polar_handler._pending_reconnect is None  # fresh handler


# ── Contracts on handler methods (G6) ─────────────────────────────────────────

class TestHandlerContracts:
    def test_emotibit_connect_rejects_None_device(self, emotibit_handler):
        from contracts import ContractViolation
        import pytest
        with pytest.raises(ContractViolation):
            emotibit_handler.connect(None)

    def test_emotibit_connect_rejects_device_with_empty_ip(self, emotibit_handler):
        from emotibit import EmotiBitDevice
        from contracts import ContractViolation
        import pytest
        with pytest.raises(ContractViolation):
            emotibit_handler.connect(EmotiBitDevice(ip=""))

    def test_unity_connect_device_rejects_None(self, unity_handler):
        from contracts import ContractViolation
        import pytest
        with pytest.raises(ContractViolation):
            unity_handler.connect_device(None)

    def test_public_summary_postcondition_holds_for_all_handlers(
        self, emotibit_handler, unity_handler, polar_handler
    ):
        # Postconditions on public_summary: must return dict.
        # The decorator enforces this; if a handler breaks, the call raises.
        assert isinstance(emotibit_handler.public_summary(), dict)
        assert isinstance(unity_handler.public_summary(), dict)
        assert isinstance(polar_handler.public_summary(), dict)
