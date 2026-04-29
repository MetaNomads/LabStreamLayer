"""
contracts.py — Lightweight design-by-contract decorators for LSL.

Three primitives:
    @requires(predicate, msg=...)            — precondition on a method/function
    @ensures(predicate_on_result, msg=...)   — postcondition on the return value
    Contract.check(condition, msg=...)       — inline runtime assertion

A predicate is a callable that takes the same args as the wrapped function
(for @requires) or (result, *args, **kwargs) (for @ensures). It returns truthy
on success, falsy on violation.

Why not pydantic / icontract / dpcontracts?
    Zero new runtime dependencies. The decorators are <100 lines and let the
    test layer assert that contract violations actually fire. Heavier libraries
    couple this codebase to a third-party API for marginal benefit.

Disabling at runtime:
    Set LSL_CONTRACTS=off in the environment to no-op every contract. Use this
    for production releases when you want zero overhead, or in tight inner loops
    where the predicate eval is a non-trivial fraction of work. Tests should
    always run with contracts ON.

On violation:
    Raises ContractViolation (a ValueError subclass). The exception message
    includes the function name, the contract name (if provided), the predicate
    source (when introspectable), and any custom msg. Caught by the same
    `except Exception` blocks the rest of the codebase already uses, so a
    contract violation degrades gracefully into a logged failure rather than
    a crash — but the violation IS logged.
"""

from __future__ import annotations

import functools
import inspect
import os
from typing import Any, Callable

# ── 0. Global enable/disable ──────────────────────────────────────────────────

_DISABLED = os.environ.get("LSL_CONTRACTS", "on").lower() in ("off", "0", "false", "no")


def contracts_enabled() -> bool:
    return not _DISABLED


# ── 1. Exception type ─────────────────────────────────────────────────────────

class ContractViolation(ValueError):
    """Raised when a @requires, @ensures, or Contract.check predicate is False.
    Inherits from ValueError so existing `except (ValueError, ...)` blocks catch
    it without code change. The .contract_name and .predicate_repr attributes
    are introspectable by the invariants reporter."""
    def __init__(self, contract_name: str, predicate_repr: str, msg: str):
        super().__init__(f"[{contract_name}] {predicate_repr}  —  {msg}")
        self.contract_name  = contract_name
        self.predicate_repr = predicate_repr


# ── 2. Helper: render a predicate source for diagnostic messages ──────────────

def _predicate_repr(pred: Callable) -> str:
    """Best-effort source rendering. Returns the lambda body for lambdas, the
    function's qualname for named functions. Used only in error messages so a
    failure is at most 'predicate not introspectable'."""
    try:
        src = inspect.getsource(pred).strip()
        return src.splitlines()[0]
    except (OSError, TypeError):
        return getattr(pred, "__qualname__", repr(pred))


# ── 3. @requires(predicate, msg=...) ──────────────────────────────────────────

def requires(predicate: Callable[..., bool], msg: str = "precondition violated"):
    """Precondition decorator. Predicate is called with the SAME args as the
    wrapped function (after binding to the instance for methods).

    Example:
        @requires(lambda self, ts: ts and not self._is_recording,
                  "must not be recording before start_recording")
        def start_recording(self, ts): ...
    """
    def deco(fn):
        if _DISABLED:
            return fn

        sig = inspect.signature(fn)
        contract_name = f"{fn.__qualname__}:requires"
        predicate_src = _predicate_repr(predicate)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                ok = predicate(*args, **kwargs)
            except Exception as e:
                raise ContractViolation(
                    contract_name, predicate_src,
                    f"predicate raised {type(e).__name__}: {e}"
                ) from e
            if not ok:
                # Bind args to names for readable diagnostics
                try:
                    bound = sig.bind(*args, **kwargs)
                    bound.apply_defaults()
                    arg_repr = ", ".join(f"{k}={v!r}" for k, v in bound.arguments.items()
                                         if k != "self")
                except Exception:
                    arg_repr = "<args unavailable>"
                raise ContractViolation(contract_name, predicate_src,
                                        f"{msg}  (args: {arg_repr})")
            return fn(*args, **kwargs)
        # Introspection markers — `scripts/contracts_audit.py` reads these.
        existing = list(getattr(fn, "_lsl_contracts", ()))
        wrapper._lsl_contracts = tuple(existing + [("requires", predicate_src, msg)])
        return wrapper
    return deco


# ── 4. @ensures(predicate_on_result, msg=...) ─────────────────────────────────

def ensures(predicate: Callable[..., bool], msg: str = "postcondition violated"):
    """Postcondition decorator. Predicate is called as predicate(result, *args, **kw).

    Example:
        @ensures(lambda result, *_: isinstance(result, dict),
                 "public_summary must return a dict")
        def public_summary(self): ...
    """
    def deco(fn):
        if _DISABLED:
            return fn
        contract_name = f"{fn.__qualname__}:ensures"
        predicate_src = _predicate_repr(predicate)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            try:
                ok = predicate(result, *args, **kwargs)
            except Exception as e:
                raise ContractViolation(
                    contract_name, predicate_src,
                    f"predicate raised {type(e).__name__}: {e}  (result was {result!r})"
                ) from e
            if not ok:
                raise ContractViolation(contract_name, predicate_src,
                                        f"{msg}  (result: {result!r})")
            return result
        existing = list(getattr(fn, "_lsl_contracts", ()))
        wrapper._lsl_contracts = tuple(existing + [("ensures", predicate_src, msg)])
        return wrapper
    return deco


def get_contracts(method) -> tuple:
    """Return the tuple of (kind, predicate_src, msg) tuples on a method.
    Empty tuple if no contracts. Used by `scripts/contracts_audit.py`."""
    return tuple(getattr(method, "_lsl_contracts", ()))


# ── 5. Contract.check(...) for inline assertions ──────────────────────────────

class Contract:
    """Inline runtime assertions. Use sparingly — they belong on boundaries,
    not on every line. Prefer @requires/@ensures at function entry/exit."""

    @staticmethod
    def check(condition: bool, msg: str, name: str = "inline"):
        if _DISABLED:
            return
        if not condition:
            raise ContractViolation(name, "Contract.check", msg)

    @staticmethod
    def not_none(value: Any, name: str = "value"):
        if _DISABLED:
            return value
        if value is None:
            raise ContractViolation("not_none", f"{name} is None",
                                     f"{name} must not be None")
        return value

    @staticmethod
    def in_range(value: float, lo: float, hi: float, name: str = "value"):
        if _DISABLED:
            return value
        if not (lo <= value <= hi):
            raise ContractViolation(
                "in_range", f"{lo} <= {value} <= {hi}",
                f"{name}={value} out of [{lo}, {hi}]"
            )
        return value
