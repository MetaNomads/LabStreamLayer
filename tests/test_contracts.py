"""Tests for src/contracts.py — the contracts library itself."""

import os
import sys
import pytest

import contracts
from contracts import requires, ensures, Contract, ContractViolation


# ── @requires ────────────────────────────────────────────────────────────────

def test_requires_passes_when_predicate_true():
    @requires(lambda x: x > 0, "x must be positive")
    def f(x): return x * 2
    assert f(3) == 6


def test_requires_raises_when_predicate_false():
    @requires(lambda x: x > 0, "x must be positive")
    def f(x): return x * 2
    with pytest.raises(ContractViolation) as exc:
        f(-1)
    assert "x must be positive" in str(exc.value)
    assert "x=-1" in str(exc.value)


def test_requires_predicate_exception_wraps():
    @requires(lambda x: x.no_such_attr, "should fail")
    def f(x): return x
    with pytest.raises(ContractViolation) as exc:
        f(42)
    assert "predicate raised" in str(exc.value)


def test_requires_works_on_methods():
    class C:
        @requires(lambda self, x: x != 0, "x must not be zero")
        def divide(self, x): return 100 / x
    c = C()
    assert c.divide(4) == 25
    with pytest.raises(ContractViolation):
        c.divide(0)


# ── @ensures ─────────────────────────────────────────────────────────────────

def test_ensures_passes_when_predicate_true():
    @ensures(lambda result, *_: isinstance(result, dict), "must return dict")
    def f(): return {"x": 1}
    assert f() == {"x": 1}


def test_ensures_raises_when_predicate_false():
    @ensures(lambda result, *_: isinstance(result, dict), "must return dict")
    def f(): return [1, 2]
    with pytest.raises(ContractViolation) as exc:
        f()
    assert "[1, 2]" in str(exc.value)


# ── Contract.check / not_none / in_range ─────────────────────────────────────

def test_check_true():
    Contract.check(2 + 2 == 4, "math is broken")


def test_check_false():
    with pytest.raises(ContractViolation):
        Contract.check(False, "expected failure")


def test_not_none_passes():
    assert Contract.not_none(0) == 0
    assert Contract.not_none("") == ""


def test_not_none_raises():
    with pytest.raises(ContractViolation):
        Contract.not_none(None, name="device")


def test_in_range_passes():
    Contract.in_range(5, 0, 10)


def test_in_range_raises():
    with pytest.raises(ContractViolation):
        Contract.in_range(15, 0, 10, name="latency_ms")


# ── Disable via env var ──────────────────────────────────────────────────────

def test_contracts_can_be_disabled(monkeypatch):
    """When LSL_CONTRACTS=off, contracts no-op."""
    import importlib
    monkeypatch.setenv("LSL_CONTRACTS", "off")
    importlib.reload(contracts)

    @contracts.requires(lambda x: x > 0)
    def f(x): return x
    assert f(-1) == -1   # would have raised with contracts on
    contracts.Contract.check(False, "should not raise when disabled")

    # Restore for the rest of the suite
    monkeypatch.setenv("LSL_CONTRACTS", "on")
    importlib.reload(contracts)
