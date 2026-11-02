"""Weft formatter.

Converts a Weft tree back to text in canonical form. Options control the
layout and the strict-canonical mode used by signature protocols.
"""
from __future__ import annotations

import base64
import datetime as _dt
import math
from dataclasses import dataclass
from typing import Any, Optional

from .errors import WeftTypeError
from .types import (
    BigInt, Binary, Decimal, Keyword, Sexpr, Symbol, Typed, WeftDate,
    is_valid_map_key,
)


def _check_map_key(k: str) -> str:
    """Raise WeftTypeError if a map key is not a valid Weft keyword name."""
    if not is_valid_map_key(k):
        raise WeftTypeError(
            f"map key {k!r} is not a valid Weft keyword name "
            "(would alter structure on re-parse)"
        )
    return k


from .limits import DEFAULT_MAX_INT_DIGITS as _DEFAULT_MAX_INT_DIGITS


@dataclass(frozen=True)
class FormatOptions:
    max_width: int = 80
    indent: int = 2
    sort_keys: bool = False
    strict_canonical: bool = False
    """When true, produces byte-identical output across implementations:
    all strings are standard form, no block comments, no raw strings,
    integers in decimal, floats shortest-round-trip, map keys sorted."""
    max_int_digits: int = _DEFAULT_MAX_INT_DIGITS
    """Decimal-digit budget for integer output. Values exceeding this
    are rejected rather than emitted — the formatter never produces text
    that the parser (with the same limit) could not read back."""


_INT_STR_SAFETY_CTX = None  # lazy import of ``sys``


def _int_to_decimal_safe(n: int) -> str:
    """Decimal string of ``n`` without touching ``sys.int_max_str_digits``.

    For small values (within the host's current cap) we fall through to the
    built-in ``str`` for speed. For larger values we build the digits by
    repeated ``divmod`` against ``10**9``, which the host's int-to-str
    cap does not apply to. Thread-safe: no global state is read or
    written.
    """
    import sys as _sys
    # Fast path: when the host cap is high enough (or unavailable),
    # ``str()`` works directly. The host cap compares against the number
    # of decimal digits in ``n``; a loose upper bound from bit_length is
    # fine for the branch.
    try:
        host_cap = _sys.get_int_max_str_digits()
    except AttributeError:
        return str(n)  # Python < 3.11, no cap at all
    bits = abs(n).bit_length()
    approx_digits = int(bits * 0.30103010299956633) + 1
    if host_cap == 0 or approx_digits <= host_cap:
        return str(n)
    # Slow path: manual divmod. ``10**9`` chunks give nine ASCII digits per
    # iteration and keep the arithmetic cheap.
    neg = n < 0
    x = -n if neg else n
    chunks: list[str] = []
    base = 10 ** 9
    while x >= base:
        x, rem = divmod(x, base)
        chunks.append(f"{rem:09d}")
    # Final (most-significant) chunk — no leading zeros.
    chunks.append(str(x))
    out = "".join(reversed(chunks))
    return ("-" + out) if neg else out


def _bigint_to_decimal_str(n: Any, max_int_digits: int) -> str:
    """Convert an ``int`` / ``BigInt`` to a decimal string, bounded by
    ``max_int_digits``.

    Correctness:

    * Python 3.11+ caps ``str(int)`` globally via ``sys.int_max_str_digits``.
      We do NOT touch that global — flipping it would open a process-wide
      window during which any other thread's ``str(bigint)`` / ``int(str)``
      call bypasses the cap too. Instead, for values that would exceed the
      host cap we do the base conversion manually with repeated ``divmod``
      against ``10**9``. That matches Python's own ``str`` output exactly
      without any global state.
    * The result length is still validated against Weft's own
      ``max_int_digits`` and :class:`WeftLimitError` is raised on overflow,
      so a caller that pre-emptively set the host cap to ``0`` cannot use
      the formatter to emit a value the parser will later reject.
    * A cheap bit-length screen avoids doing the conversion work for
      values that are clearly too large.
    """
    from .errors import WeftLimitError
    val = int(n.value) if hasattr(n, "value") and not isinstance(n, (int, bool)) else int(n)
    if val == 0:
        return "0"
    bits = abs(val).bit_length()
    # log10(2) ≈ 0.30103 — upper bound on decimal digits from bit length.
    approx_digits = int(bits * 0.30103010299956633) + 1
    if approx_digits > max_int_digits + 2:  # +2 for the approx error
        # Fast-path reject before paying for a huge conversion.
        raise WeftLimitError(
            f"integer has at least {approx_digits} digits, "
            f"exceeds max_int_digits={max_int_digits}",
            code="WEFT_LIMIT_INT",
        )
    s = _int_to_decimal_safe(val)
    digit_len = len(s) - (1 if s[0] == "-" else 0)
    if digit_len > max_int_digits:
        raise WeftLimitError(
            f"integer has {digit_len} digits, exceeds "
            f"max_int_digits={max_int_digits}",
            code="WEFT_LIMIT_INT",
        )
    return s


import dataclasses as _dataclasses
from .parser import _recursion_headroom as _rec_headroom


def format_value(value: Any, options: Optional[FormatOptions] = None) -> str:
    """Format a Weft tree as a canonical text document."""
    opt = options or FormatOptions()
    if opt.strict_canonical:
        # SPEC §7.3 strict canonical:
        #   - sort_keys = true (code-point order; handled downstream)
        #   - single-line unbounded-width layout (no soft wrap)
        # The second is achieved by setting max_width to effectively
        # unbounded so _format never falls into _format_*_multi.
        # ``dataclasses.replace`` carries every field forward so new
        # fields added later are preserved verbatim.
        opt = _dataclasses.replace(
            opt,
            sort_keys=True,
            strict_canonical=True,
            max_width=10**12,
        )
    # The same stack headroom rule as parse(): any tree §9 limits admit
    # (hard max_depth 1024) must format without a host RecursionError.
    with _rec_headroom(1024):
        rendered = _format(value, 0, opt)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def format_inline(value: Any, options: Optional[FormatOptions] = None) -> str:
    """Format a Weft value on a single line (no outer newline).

    Raises WeftTypeError if the value cannot be inlined (e.g. contains
    strings with newlines in non-strict mode).

    When ``strict_canonical`` is set, the same normalisation applied by
    :func:`format_value` is applied here too: map keys are sorted. This
    guarantees that for any value, ``format_inline(v, strict)`` and
    ``format_value(v, strict).rstrip('\\n')`` produce the same text when
    the value fits on a single line.
    """
    opt = options or FormatOptions()
    if opt.strict_canonical and not opt.sort_keys:
        opt = _dataclasses.replace(opt, sort_keys=True, strict_canonical=True)
    return _format_inline(value, opt)


# --------------------------------------------------------------------------

def _format(value: Any, indent_level: int, opt: FormatOptions) -> str:
    # Peel off any Typed wrapper(s) iteratively so a deep type-annotation
    # chain cannot blow the Python recursion limit. Innermost type appears
    # first in the source, outermost last; peeling from outside gives us
    # the chain in reverse order, so we reverse when joining.
    type_chain: list = []
    while isinstance(value, Typed):
        type_chain.append(_format_type_expr_inline(value.type, opt))
        value = value.value
    type_suffix = ("::" + "::".join(reversed(type_chain))) if type_chain else ""

    # Strings with newlines inside any nested structure must use the
    # single-line escape form, not the triple-quoted form.
    if isinstance(value, str) and "\n" in value and indent_level > 0 and not opt.strict_canonical:
        return _format_string_dquote_force(value) + type_suffix

    # Try one-line first
    try:
        oneline = _format_inline(value, opt)
    except _MultilineNeeded:
        oneline = None
    if oneline is not None:
        width_here = indent_level * opt.indent + len(oneline) + len(type_suffix)
        if width_here <= opt.max_width and "\n" not in oneline:
            return oneline + type_suffix

    # Multi-line
    if isinstance(value, list):
        return _format_list_multi(value, indent_level, opt) + type_suffix
    if isinstance(value, dict):
        return _format_map_multi(value, indent_level, opt) + type_suffix
    if isinstance(value, Sexpr):
        return _format_sexpr_multi(value, indent_level, opt) + type_suffix
    # Fallback: inline (even if oversized).
    #
    # SPEC §7.2: multi-line strings use the one-line dquote-escape form
    # unconditionally — no triple-quoted output, to avoid the silent
    # re-indentation hazard (W4-04).
    if isinstance(value, str):
        return _format_string_dquote_force(value) + type_suffix
    try:
        return _format_inline(value, opt) + type_suffix
    except _MultilineNeeded:
        raise


def _format_string_dquote_force(s: str) -> str:
    """Format a string with newline escapes, no multi-line variant."""
    out = ['"']
    for c in s:
        cp = ord(c)
        if c == '"':   out.append('\\"')
        elif c == "\\": out.append("\\\\")
        elif c == "\n": out.append("\\n")
        elif c == "\t": out.append("\\t")
        elif c == "\r": out.append("\\r")
        elif c == "\x00": out.append("\\0")
        elif cp < 0x20: out.append(f"\\u{{{cp:X}}}")
        elif cp in (0xFEFF, 0xFFFE, 0xFFFF):
            out.append(f"\\u{{{cp:X}}}")
        else:          out.append(c)
    out.append('"')
    return "".join(out)


class _MultilineNeeded(Exception):
    pass


def _format_inline(value: Any, opt: FormatOptions) -> str:
    # Peel off Typed wrappers iteratively (avoid Python recursion on long chains).
    type_chain: list = []
    while isinstance(value, Typed):
        type_chain.append(_format_type_expr_inline(value.type, opt))
        value = value.value
    type_suffix = ("::" + "::".join(reversed(type_chain))) if type_chain else ""

    s = _format_inline_core(value, opt)
    return s + type_suffix


def _format_type_expr_inline(t: Any, opt: FormatOptions) -> str:
    """Format a type expression (Symbol or Sexpr) in inline form.

    Forces strict (single-line) formatting so an embedded multi-line string
    inside a type expression doesn't blow up with _MultilineNeeded.
    """
    strict_opt = _dataclasses.replace(opt, strict_canonical=True)
    if isinstance(t, Symbol):
        return t.name
    if isinstance(t, Sexpr):
        parts = [t.tag.name]
        for it in t.items:
            parts.append(_format_inline(it, strict_opt))
        return "(" + " ".join(parts) + ")"
    # Shouldn't happen (parser enforces type is Symbol/Sexpr) but be defensive.
    return _format_inline(t, strict_opt)


def _format_inline_core(value: Any, opt: FormatOptions) -> str:
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, BigInt):
        return _format_int(value.value, opt) + "n"
    if isinstance(value, int):
        return _format_int(value, opt)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, Decimal):
        return _format_decimal(value)
    if isinstance(value, str):
        return _format_string_dquote(value, opt)
    if isinstance(value, Binary):
        return _format_binary(value, opt)
    if isinstance(value, WeftDate):
        return _format_weft_date(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return _format_datetime(value)
    if isinstance(value, Symbol):
        return value.name
    if isinstance(value, Keyword):
        return ":" + value.name
    if isinstance(value, list):
        parts = [_format_inline(x, opt) for x in value]
        return "[" + " ".join(parts) + "]"
    if isinstance(value, dict):
        keys = list(value.keys())
        if opt.sort_keys:
            keys = sorted(keys)
        parts = []
        for k in keys:
            parts.append(":" + _check_map_key(k) + " " + _format_inline(value[k], opt))
        return "{" + " ".join(parts) + "}"
    if isinstance(value, Sexpr):
        parts = [value.tag.name]
        for it in value.items:
            parts.append(_format_inline(it, opt))
        return "(" + " ".join(parts) + ")"
    raise WeftTypeError(f"cannot format value of type {type(value).__name__}")


# ---- scalars ----

def _format_int(n: int, opt: FormatOptions) -> str:
    # Canonical: decimal, no separators, no leading + sign.
    # Routed through _bigint_to_decimal_str so that (a) Python's global
    # ``int_max_str_digits`` cap cannot crash us and (b) Weft's own
    # ``max_int_digits`` is enforced on output — the formatter never
    # emits a value the parser (with the same limit) could not read back,
    # even if the host's global cap has been removed.
    return _bigint_to_decimal_str(n, opt.max_int_digits)


def _format_float(f: float) -> str:
    if math.isnan(f):
        return "nan"
    if math.isinf(f):
        return "-inf" if f < 0 else "inf"
    # Shortest round-trip representation
    # Python's repr for floats is shortest-round-trip since 3.1.
    s = repr(f)
    # Ensure we have a decimal point or exponent to distinguish from integer.
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s


def _format_decimal(d: Decimal) -> str:
    # SPEC §7.2 canonicalisations on Decimal text:
    #   (a) drop a leading '+' sign — matches the Integer rule
    #       ('+1.5m' → '1.5m'). The textual form on the Decimal object
    #       may still carry the '+' from input; strip it on output.
    #   (b) exponent marker normalised to lowercase 'e'.
    text = d.text
    if text.startswith("+"):
        text = text[1:]
    text = text.replace("E", "e")
    return text + "m"


def _format_string_dquote(s: str, opt: FormatOptions) -> str:
    """Format a string using "..." form.

    SPEC §7.2 rule: multi-line (contains U+000A) string values are NEVER
    emitted in triple-quoted form by the canonical formatter — the
    newline is escaped to ``\\n`` and the string stays on one line. This
    is because triple-quoted strings inside a soft-wrapped compound get
    silently re-indented by every known Weft formatter, which inserts
    leading spaces into the string body (W4-04 silent data corruption).
    Keeping it inline with ``\\n`` escapes removes the attack surface.
    """
    from .validators import strict_must_escape
    out = ['"']
    for ch in s:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\x00":
            out.append("\\0")
        elif opt.strict_canonical and strict_must_escape(cp):
            # SPEC §7.3: C1 controls, DEL, BOM, BMP noncharacters, bidi
            # overrides all MUST escape in strict canonical.
            out.append(f"\\u{{{cp:X}}}")
        elif cp < 0x20:
            out.append(f"\\u{{{cp:X}}}")
        elif cp in (0xFEFF, 0xFFFE, 0xFFFF):
            # BOM and noncharacters escape in every mode so the output
            # can be read back by any conforming lexer.
            out.append(f"\\u{{{cp:X}}}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _format_string_triple(s: str, indent_level: int, opt: FormatOptions) -> str:
    # Triple-quoted form breaks when:
    #   - the content contains three consecutive quotes ("""), or
    #   - the content ends in a quote (which would merge with the closing """
    #     to form """"), making the closing delimiter ambiguous.
    # In those cases fall back to the single-line dquote escape form.
    if '"""' in s or s.endswith('"') or opt.strict_canonical:
        strict_opt = _dataclasses.replace(opt, strict_canonical=True)
        return _format_string_dquote(s, strict_opt)
    # Keep newlines literal in triple form.
    out = ['"""']
    for ch in s:
        cp = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif ch == "\\":
            out.append("\\\\")
        elif cp < 0x20 and ch != "\r":
            out.append(f"\\u{{{cp:X}}}")
        # BOM and noncharacters are not permitted in Weft source even inside
        # a string; emit them via escape so the output is parseable.
        elif cp in (0xFEFF, 0xFFFE, 0xFFFF):
            out.append(f"\\u{{{cp:X}}}")
        else:
            out.append(ch)
    out.append('"""')
    return "".join(out)


def _format_binary(b: Binary, opt: FormatOptions) -> str:
    # SPEC §5.8 and §7.2: canonical form always emits base64, regardless
    # of input encoding. Equality is defined on the byte sequence, not
    # the textual encoding, so the two spellings have the same abstract
    # value and canonical output picks one.
    s = base64.b64encode(b.data).decode("ascii")
    return 'b"base64:' + s + '"'


def _format_weft_date(d: WeftDate) -> str:
    y = f"{d.year:04d}"
    mo = f"{d.month:02d}"
    da = f"{d.day:02d}"
    if not d.has_time:
        return f"#{y}-{mo}-{da}"
    h = f"{d.hour:02d}"
    mi = f"{d.minute:02d}"
    s = f"{d.second:02d}"
    base = f"{y}-{mo}-{da}T{h}:{mi}:{s}"
    if d.nano:
        frac = f"{d.nano:09d}".rstrip("0")
        base += "." + frac
    if d.tz_offset_minutes is None:
        return "#" + base
    off = d.tz_offset_minutes
    if off == 0:
        return "#" + base + "Z"
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"#{base}{sign}{off // 60:02d}:{off % 60:02d}"


def _format_datetime(d) -> str:
    if isinstance(d, _dt.datetime):
        # If the tzinfo has a non-whole-minute offset (e.g. historical
        # local times with ~30-second offsets), convert to UTC to avoid
        # silently truncating seconds in the emitted offset.
        if d.tzinfo is not None:
            off = d.utcoffset()
            if off is not None and off.total_seconds() % 60 != 0:
                d = d.astimezone(_dt.timezone.utc)
        base = (f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
                f"T{d.hour:02d}:{d.minute:02d}:{d.second:02d}")
        if d.microsecond:
            frac = f".{d.microsecond:06d}".rstrip("0")
            base += frac
        if d.tzinfo is None:
            return "#" + base
        off = d.utcoffset()
        if off is None:
            return "#" + base
        total = int(off.total_seconds())
        if total == 0:
            return "#" + base + "Z"
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        hh = total // 3600
        mm = (total % 3600) // 60
        return "#" + base + f"{sign}{hh:02d}:{mm:02d}"
    # date
    return f"#{d.year:04d}-{d.month:02d}-{d.day:02d}"


# ---- collections (multi-line) ----

def _format_list_multi(items: list, indent_level: int, opt: FormatOptions) -> str:
    if not items:
        return "[]"
    pad_inner = " " * ((indent_level + 1) * opt.indent)
    pad_close = " " * (indent_level * opt.indent)
    lines = []
    for it in items:
        rendered = _format(it, indent_level + 1, opt)
        lines.append(pad_inner + rendered)
    return "[\n" + "\n".join(lines) + "\n" + pad_close + "]"


def _format_map_multi(d: dict, indent_level: int, opt: FormatOptions) -> str:
    if not d:
        return "{}"
    keys = list(d.keys())
    if opt.sort_keys:
        keys = sorted(keys)
    pad_inner = " " * ((indent_level + 1) * opt.indent)
    pad_close = " " * (indent_level * opt.indent)
    lines = []
    for k in keys:
        v = d[k]
        key_s = ":" + _check_map_key(k)
        # Multi-line strings must stay on the same line as the key:
        # re-indenting them would alter their content.
        if isinstance(v, str) and "\n" in v and not opt.strict_canonical:
            v_block = _format(v, indent_level + 1, opt)
            lines.append(pad_inner + key_s + " " + v_block)
            continue
        try:
            v_inline = _format_inline(v, opt)
        except _MultilineNeeded:
            v_inline = None
        if v_inline is not None and len(pad_inner) + len(key_s) + 1 + len(v_inline) <= opt.max_width and "\n" not in v_inline:
            lines.append(pad_inner + key_s + " " + v_inline)
        else:
            v_block = _format(v, indent_level + 1, opt)
            if "\n" in v_block:
                # Put on next line, indented one more
                deeper_pad = " " * ((indent_level + 2) * opt.indent)
                v_lines = v_block.split("\n")
                lines.append(pad_inner + key_s)
                for ln in v_lines:
                    lines.append(deeper_pad + ln if ln else "")
            else:
                lines.append(pad_inner + key_s + " " + v_block)
    return "{\n" + "\n".join(lines) + "\n" + pad_close + "}"


def _format_sexpr_multi(s: Sexpr, indent_level: int, opt: FormatOptions) -> str:
    tag = s.tag.name
    if not s.items:
        return "(" + tag + ")"
    pad_inner = " " * ((indent_level + 1) * opt.indent)
    pad_close = " " * (indent_level * opt.indent)
    lines = []
    for it in s.items:
        rendered = _format(it, indent_level + 1, opt)
        lines.append(pad_inner + rendered)
    return "(" + tag + "\n" + "\n".join(lines) + "\n" + pad_close + ")"
