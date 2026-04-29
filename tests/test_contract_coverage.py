"""
Reflection tests — assert that contracts are PRESENT on the methods they
should be present on. The contracts_audit.py script is the human-readable
view; this test is the CI gate.
"""

import pytest

from contracts import get_contracts


# (module, class, method, expected_kinds_subset)
EXPECTED = [
    ("emotibit",   "EmotiBitHandler", "connect",         {"requires"}),
    ("emotibit",   "EmotiBitHandler", "start_recording", {"requires"}),
    ("emotibit",   "EmotiBitHandler", "send_marker",     {"requires", "ensures"}),
    ("emotibit",   "EmotiBitHandler", "public_summary",  {"ensures"}),
    ("polar_mac",  "PolarHandler",    "start_recording", {"requires"}),
    ("polar_mac",  "PolarHandler",    "send_marker",     {"requires", "ensures"}),
    ("polar_mac",  "PolarHandler",    "public_summary",  {"ensures"}),
    ("unity",      "UnityHandler",    "connect_device",  {"requires"}),
    ("unity",      "UnityHandler",    "broadcast_ping",  {"requires"}),
    ("unity",      "UnityHandler",    "set_stream_rate", {"requires"}),
    ("unity",      "UnityHandler",    "public_summary",  {"ensures"}),
    ("sync_logger","SyncLogger",      "start_session",   {"requires", "ensures"}),
    ("sync_logger","SyncLogger",      "log_ping",        {"ensures"}),
    ("sync_logger","SyncLogger",      "write_event",     {"requires"}),
]


@pytest.mark.parametrize("module_name,cls_name,method_name,expected_kinds", EXPECTED)
def test_method_has_expected_contract_kinds(module_name, cls_name, method_name, expected_kinds):
    mod = __import__(module_name)
    cls = getattr(mod, cls_name)
    method = getattr(cls, method_name)
    contracts = get_contracts(method)
    kinds = {kind for kind, _src, _msg in contracts}
    missing = expected_kinds - kinds
    assert not missing, (
        f"{module_name}.{cls_name}.{method_name} missing contract kinds: {missing}. "
        f"Has: {kinds}"
    )
