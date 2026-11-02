"""Shared validation helpers (SPEC §5.8, §7.3, §11.2).

This module exists so the lexer, formatter, and JSON interop layers all
apply exactly the same byte-level rules; keeping them in one place is what
guarantees the four reference implementations stay in lock-step.
"""
from __future__ import annotations

import base64 as _b64
import binascii as _binascii
import re as _re

# SPEC §11.2: the $bigint / $int-big payload shape. ASCII digits with an
# optional leading '-'. No '+', no '_', no whitespace, no non-ASCII digits,
# no base prefix. \A/\Z anchors so a trailing newline cannot sneak past.
INT_PAYLOAD_RE = _re.compile(r"\A-?[0-9]+\Z")


def is_canonical_base64(payload: str) -> bool:
    """RFC 4648 §3.5 canonical check (SPEC §5.8, mandatory).

    A base64 string is canonical iff decoding it and re-encoding the result
    reproduces the original text — this rejects non-zero trailing bits
    (``AB==`` encodes the same byte as ``AA==``) and any other
    non-canonical spelling, so each byte sequence has exactly one base64
    form. Invalid base64 also returns False; callers that want to
    distinguish "invalid" from "non-canonical" should decode first.
    """
    if payload == "":
        return True
    try:
        data = _b64.b64decode(payload, validate=True)
    except (_binascii.Error, ValueError):
        return False
    return _b64.b64encode(data).decode("ascii") == payload


def strict_must_escape(cp: int) -> bool:
    """SPEC §7.3: code points that MUST be escaped (``\\u{HEX}`` or the
    short forms) in strict canonical string output:

    - C0 controls U+0000–U+001F
    - DEL U+007F
    - C1 controls U+0080–U+009F (includes CSI U+009B)
    - BOM U+FEFF and the BMP noncharacters U+FFFE / U+FFFF
    - Bidi override/isolate controls U+202A–U+202E, U+2066–U+2069
    """
    return (
        cp <= 0x1F
        or cp == 0x7F
        or 0x80 <= cp <= 0x9F
        or cp in (0xFEFF, 0xFFFE, 0xFFFF)
        or 0x202A <= cp <= 0x202E
        or 0x2066 <= cp <= 0x2069
    )


def dec_str_to_int(digits: str) -> int:
    """Decimal-digit string → int without tripping CPython's global
    ``sys.int_max_str_digits`` cap (SPEC §11.2 — payloads up to
    ``max_int_digits`` decimal digits must be convertible). Accepts an
    optional leading '-'. Caller validates shape via INT_PAYLOAD_RE and
    length via ``max_int_digits`` BEFORE calling.
    """
    neg = digits.startswith("-")
    if neg:
        digits = digits[1:]
    acc = 0
    i = 0
    n = len(digits)
    while i < n:
        chunk = digits[i:i + 9]
        acc = acc * (10 ** len(chunk)) + int(chunk)
        i += 9
    return -acc if neg else acc


def json_max_nesting(text: str) -> int:
    """Maximum bracket/brace nesting depth of a JSON text, computed with a
    string-aware single pass (quotes and backslash escapes are skipped, so
    brackets inside JSON strings don't count). Used to bound the stdlib
    ``json.loads`` recursion BEFORE it runs (SPEC §9: limits apply to every
    tree-building entry point, lossless JSON decoding included).
    """
    depth = 0
    max_depth = 0
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch in "]}":
            depth -= 1
    return max_depth


def int_digits_exceed(x: int, max_digits: int) -> bool:
    """True when ``abs(x)`` has more than ``max_digits`` decimal digits.

    Works on an already-materialized host int without converting it to a
    decimal string (which CPython's ``int_max_str_digits`` guard may
    refuse, and which costs O(n^2) for huge values). The bit length gives
    an exact lower bound on the digit count; only the single borderline
    case compares against ``10**max_digits`` directly.
    """
    if x == 0:
        return max_digits < 1
    ax = -x if x < 0 else x
    bl = ax.bit_length()
    # log10(2) = 0.30102999566... Use an UNDER-estimate (0.30102) for the
    # lower bound and an OVER-estimate (0.30103) for the upper bound, so
    # both bounds are safe; the window between them (≈ bl*1e-5 digits)
    # falls back to one exact big-int comparison. (An earlier version
    # used 0.30103 for the lower bound — an over-estimate of log10(2) —
    # which falsely rejected in-limit values such as 1<<100000 with
    # max_digits=30103.)
    digits_lb = ((bl - 1) * 30102) // 100000 + 1   # ≤ true digit count
    digits_ub = (bl * 30103) // 100000 + 1          # ≥ true digit count
    if digits_lb > max_digits:
        return True
    if digits_ub <= max_digits:
        return False
    return ax >= 10 ** max_digits
