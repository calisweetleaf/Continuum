"""
RFC 8785 JSON Canonicalization Scheme (JCS) — stdlib-only implementation.

Source: written in-house for cherry_ttt (D3 ruling, amended 2026-07-05:
    no third-party jcs/rfc8785 dependency; ~150 LOC of stdlib beats a pin).
Integrated: 2026-07-05
Purpose: The single definition of action identity (D3). ActionCandidate
    .canonical() = sha256(tool_id + jcs(args))[:16]. Transposition tables,
    speculative acceptance tests, and dedup all hash through this module —
    one definition, one test (tests/test_p0_types.py, RFC test vectors).

Deliberate, documented deviation from strict JCS: Python ints are
serialized exactly as their decimal digits rather than being coerced
through IEEE 754 doubles. Strict JCS would collide 2**53 and 2**53 + 1
into the same canonical string — a dedup bug for an action-identity
function. For every int exactly representable as a double with |x| < 1e21
the output is byte-identical to JCS, so the RFC test vectors still pass.
Floats follow ECMAScript Number::toString (ES2020 7.1.12.1) exactly.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from .errors import CanonicalizationError

_ESCAPES: dict[int, str] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _es_number(x: float) -> str:
    """Serialize a finite float per ECMAScript Number::toString (radix 10).

    Args:
        x: A finite Python float (NaN/Infinity must be rejected upstream).

    Returns:
        The shortest round-trip decimal string with ES2020 exponent rules:
        plain notation for 1e-6 <= |x| < 1e21, exponential outside, "0" for
        both zeros.
    """
    if x == 0.0:
        return "0"  # covers -0.0: ECMAScript renders negative zero as "0"
    sign = "-" if x < 0 else ""
    # repr() yields the shortest digit string that round-trips the double;
    # Decimal parses it into (digits, exponent) without float re-rounding.
    tup = Decimal(repr(abs(x))).as_tuple()
    digits = list(tup.digits)
    exp = int(tup.exponent)
    while len(digits) > 1 and digits[-1] == 0:  # strip trailing zeros
        digits.pop()
        exp += 1
    k = len(digits)          # number of significant digits
    n = exp + k              # position of decimal point (ES spec variable)
    dstr = "".join(str(d) for d in digits)
    if k <= n <= 21:
        return sign + dstr + "0" * (n - k)
    if 0 < n <= 21:
        return sign + dstr[:n] + "." + dstr[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + dstr
    # exponential notation
    e = n - 1
    esign = "+" if e >= 0 else "-"
    mant = dstr[0] if k == 1 else dstr[0] + "." + dstr[1:]
    return sign + mant + "e" + esign + str(abs(e))


def _string(s: str) -> str:
    """Escape a string per JCS: minimal escapes, control chars as lowercase hex."""
    out: list[str] = ['"']
    for ch in s:
        cp = ord(ch)
        esc = _ESCAPES.get(cp)
        if esc is not None:
            out.append(esc)
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(v: Any, out: list[str]) -> None:
    """Append the canonical serialization of one JSON value to out.

    Side effect: mutates out in place (single-pass builder, no quadratic
    string concatenation).
    """
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, str):
        out.append(_string(v))
    elif isinstance(v, int):  # bool already handled above
        out.append(str(v))
    elif isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise CanonicalizationError(
                f"non-finite float {v!r} is not canonicalizable under RFC 8785; "
                "round or reject it at the schema boundary (D3)"
            )
        out.append(_es_number(v))
    elif isinstance(v, Mapping):
        keys = list(v.keys())
        for key in keys:
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object key {key!r} is {type(key).__name__}, not str; "
                    "JCS objects require string keys"
                )
        # RFC 8785 sorts keys by UTF-16 code units, not Unicode code points.
        keys.sort(key=lambda item: item.encode("utf-16-be"))
        out.append("{")
        for i, key in enumerate(keys):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialize(v[key], out)
        out.append("}")
    elif isinstance(v, Sequence):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        raise CanonicalizationError(
            f"type {type(v).__name__} is not JSON-serializable; canonical "
            "identity is defined only over JSON values (D3)"
        )


def canonicalize(value: Any) -> str:
    """Return the RFC 8785 canonical JSON text of value.

    Args:
        value: Any JSON-compatible Python value (None, bool, int, float,
            str, Sequence, Mapping). Ints serialize exactly (see module
            docstring for the documented deviation).

    Returns:
        Canonical JSON as a str; encode UTF-8 before hashing.

    Raises:
        CanonicalizationError: On NaN/Infinity, non-string keys, or
            non-JSON types.
    """
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)
