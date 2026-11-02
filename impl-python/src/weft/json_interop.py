"""JSON interoperability for Weft (SPEC §11)."""
from __future__ import annotations

import base64
import datetime as _dt
import json
import math
import re
from typing import Any

from .errors import WeftLimitError, WeftSyntaxError, WeftTypeError
from .parser import _recursion_headroom as _rec_headroom
import re as _re
_HEX_RE = _re.compile(r"[0-9a-fA-F]*")
from .types import (
    BigInt, Binary, Decimal, Keyword, Sexpr, Symbol, Typed, WeftDate,
    is_valid_keyword_name,
)


_JSON_SAFE_INT_MIN = -((1 << 53) - 1)
_JSON_SAFE_INT_MAX = (1 << 53) - 1


# Finite-float decimal shape for ``$float`` payloads: optional sign,
# integer part, optional fractional part, optional exponent. Matches the
# formatter's shortest-round-trip output.
_FLOAT_DEC_RE = re.compile(r"\A[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")

# Strict RFC-3339-like shape for ``$dt`` payloads. Anchored with ``\A``/``\Z``
# so trailing whitespace or a comment cannot smuggle in.
_DT_PAYLOAD_RE = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})?)?\Z"
)


def _float_canonical_text(f: float) -> str:
    """Shortest round-trip decimal text matching the formatter's output,
    with the sign of zero preserved so ``-0.0`` survives JSON round-trip."""
    import math as _m
    if f == 0.0 and _m.copysign(1.0, f) < 0:
        return "-0.0"
    # repr() uses Python's shortest-round-trip; ensure a trailing ``.0``
    # for whole values so re-parse yields a Float rather than an Int.
    s = repr(f)
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s


def _bigint_to_dec_str(n: int) -> str:
    """Stringify a potentially-large ``int`` without touching the host's
    global ``sys.int_max_str_digits`` cap (modifying that global would
    open a process-wide window during which other threads could bypass
    the cap too). Delegates to the formatter helper, which uses manual
    divmod for anything the host would otherwise refuse."""
    from .formatter import _int_to_decimal_safe
    return _int_to_decimal_safe(n)


def _weft_date_canonical(d: WeftDate) -> str:
    y = f"{d.year:04d}"
    mo = f"{d.month:02d}"
    da = f"{d.day:02d}"
    if not d.has_time:
        return f"{y}-{mo}-{da}"
    h = f"{d.hour:02d}"
    mi = f"{d.minute:02d}"
    s = f"{d.second:02d}"
    base = f"{y}-{mo}-{da}T{h}:{mi}:{s}"
    if d.nano:
        frac = f"{d.nano:09d}".rstrip("0")
        base += "." + frac
    if d.tz_offset_minutes is None:
        return base
    off = d.tz_offset_minutes
    if off == 0:
        return base + "Z"
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{base}{sign}{off // 60:02d}:{off % 60:02d}"


def _datetime_canonical(d: _dt.datetime) -> str:
    """Canonical textual form for a datetime, matching the formatter.

    Weft's wire form only expresses offsets in whole minutes. If the input
    carries a sub-minute offset we convert to UTC first — otherwise the
    seconds inside the offset would be silently dropped and the instant
    would shift (what the external review flagged as the 30-second JSON
    round-trip skew).
    """
    if d.tzinfo is not None:
        off = d.utcoffset()
        if off is not None:
            # Any sub-minute component — seconds OR microseconds — forces
            # a UTC conversion. ``int(total_seconds())`` would truncate a
            # 0.5-second offset to 0 and silently drop it.
            total_us = off.days * 86_400_000_000 + off.seconds * 1_000_000 + off.microseconds
            if total_us % (60 * 1_000_000) != 0:
                d = d.astimezone(_dt.timezone.utc)
    base = (f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            f"T{d.hour:02d}:{d.minute:02d}:{d.second:02d}")
    if d.microsecond:
        frac = f".{d.microsecond:06d}".rstrip("0")
        base += frac
    if d.tzinfo is None:
        return base
    off = d.utcoffset()
    if off is None:
        return base
    total = int(off.total_seconds())
    if total == 0:
        return base + "Z"
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return base + f"{sign}{hh:02d}:{mm:02d}"


def to_json(value: Any) -> Any:
    """Lossy SPEC §11.3 mapping (rules in _to_json_rec). Wrapped in the
    recursion-headroom guard so any tree the §9 limits admit (hard
    max_depth 1024) converts without a host RecursionError."""
    with _rec_headroom(1024):
        return _to_json_rec(value)


def _to_json_rec(value: Any) -> Any:
    """Convert a Weft tree to a JSON-serializable Python value (LOSSY).

    This is the "friendly" mapping. Symbols and Keywords both become plain
    JSON strings (with ``#sym:``/``#kw:`` prefixes as a hint, but these
    prefixes collide with legitimate string content). ``Sexpr`` and
    ``Typed`` become tagged objects. For lossless round-trips, use
    :func:`to_json_lossless` / :func:`from_json_lossless` instead.
    """
    if value is None:
        return None
    if value is True or value is False:
        return value
    if isinstance(value, BigInt):
        return _bigint_to_dec_str(value.value)
    if isinstance(value, int):
        if _JSON_SAFE_INT_MIN <= value <= _JSON_SAFE_INT_MAX:
            return value
        return _bigint_to_dec_str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "-inf" if value < 0 else "inf"
        return value
    if isinstance(value, Decimal):
        return value.text
    if isinstance(value, str):
        return value
    if isinstance(value, Binary):
        if value.encoding == "base64":
            return "base64:" + base64.b64encode(value.data).decode("ascii")
        return "hex:" + value.data.hex()
    if isinstance(value, WeftDate):
        return _weft_date_canonical(value)
    if isinstance(value, _dt.datetime):
        return _datetime_canonical(value)
    if isinstance(value, _dt.date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    if isinstance(value, Symbol):
        return "#sym:" + value.name
    if isinstance(value, Keyword):
        return "#kw:" + value.name
    if isinstance(value, list):
        return [_to_json_rec(x) for x in value]
    if isinstance(value, dict):
        return {k: _to_json_rec(v) for k, v in value.items()}
    if isinstance(value, Sexpr):
        return {"$sexpr": [value.tag.name] + [_to_json_rec(x) for x in value.items]}
    if isinstance(value, Typed):
        return {"$typed": {"v": _to_json_rec(value.value), "t": _to_json_rec(value.type)}}
    raise WeftTypeError(f"cannot convert {type(value).__name__} to JSON")


# --------------------------------------------------------------------------
# Iterative conversion machinery (SPEC §9: limits on every entry point)
# --------------------------------------------------------------------------

class _TotalCounter:
    __slots__ = ("n", "limit")

    def __init__(self, lim) -> None:
        self.n = 0
        self.limit = lim.max_total_items

    def bump(self) -> None:
        self.n += 1
        if self.n > self.limit:
            raise WeftLimitError(
                f"document has more than max_total_items={self.limit} values",
                code="WEFT_LIMIT_TOTAL")


def _check_depth(depth: int, lim) -> None:
    if depth > lim.max_depth:
        raise WeftLimitError(
            f"tree depth {depth} exceeds max_depth={lim.max_depth}",
            code="WEFT_LIMIT_DEPTH")


def _reject_surrogates(s: str, what: str) -> None:
    """Weft strings are sequences of Unicode scalar values (SPEC §5.7);
    JSON permits lone ``\\uD800``-style escapes, which Python's json module
    passes through as surrogate code points. Reject them here so the Weft
    tree can always be encoded as valid UTF-8."""
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise WeftTypeError(
                f"{what} contains a lone surrogate U+{ord(ch):04X}; "
                "Weft strings are Unicode scalar sequences (SPEC §5.7)")


def _check_coll(n: int, lim) -> None:
    if n > lim.max_items_per_coll:
        raise WeftLimitError(
            f"collection has {n} items, exceeds max_items_per_coll="
            f"{lim.max_items_per_coll}", code="WEFT_LIMIT_ITEMS")


def _check_int_digits(x: int, lim) -> None:
    from .validators import int_digits_exceed
    if int_digits_exceed(x, lim.max_int_digits):
        raise WeftLimitError(
            f"integer exceeds max_int_digits={lim.max_int_digits}",
            code="WEFT_LIMIT_INT")


def _check_str_bytes(s: str, lim) -> None:
    if len(s.encode("utf-8")) > lim.max_string_bytes:
        raise WeftLimitError(
            f"string exceeds max_string_bytes={lim.max_string_bytes}",
            code="WEFT_LIMIT_STRING")


def _run_trampoline(conv, root):
    """Drive a generator-recursive converter iteratively.

    ``conv(node, depth)`` is a generator function that ``yield``s
    ``(child_node, child_depth)`` whenever it needs a child converted, and
    ``return``s the converted value. The driver keeps the generators on an
    explicit heap stack, so the host call stack stays O(1) regardless of
    input nesting — WEFT_LIMIT_DEPTH is raised by the converters long
    before memory becomes a concern.
    """
    stack = [conv(root, 0)]
    send_val = None
    while stack:
        gen = stack[-1]
        try:
            child, child_depth = gen.send(send_val)
        except StopIteration as stop:
            stack.pop()
            send_val = stop.value
            continue
        stack.append(conv(child, child_depth))
        send_val = None
    return send_val


def from_json(value: Any, *, strict: bool = True,
              limits: "Limits" = None) -> Any:
    """Convert a JSON-decoded Python value to a Weft tree (LOSSY, §11.3).

    JSON object keys become Weft keywords. With ``strict=True`` (default),
    keys that are not valid Weft keyword names raise :class:`WeftTypeError`
    (code WEFT_TYPE_JSON_KEY). Resource limits (SPEC §9) are enforced:
    depth, total item count, and per-collection item count. The function is
    iterative (generator trampoline), so adversarially deep input cannot
    exhaust the host call stack.
    """
    from .limits import DEFAULT_LIMITS
    lim = limits or DEFAULT_LIMITS
    counter = _TotalCounter(lim)

    def conv(node: Any, depth: int):
        counter.bump()
        if node is None:
            return None
        if node is True or node is False:
            return node
        if isinstance(node, int):
            _check_int_digits(node, lim)
            return node
        if isinstance(node, float):
            return node
        if isinstance(node, str):
            _reject_surrogates(node, "JSON string")
            _check_str_bytes(node, lim)
            return node
        if isinstance(node, list):
            _check_depth(depth + 1, lim)
            _check_coll(len(node), lim)
            out = []
            for x in node:
                out.append((yield (x, depth + 1)))
            return out
        if isinstance(node, dict):
            _check_depth(depth + 1, lim)
            _check_coll(len(node), lim)
            out_d: dict = {}
            for k, v in node.items():
                if not isinstance(k, str):
                    raise WeftTypeError(
                        f"JSON object key must be a string, got {type(k).__name__}")
                if strict and not is_valid_keyword_name(k):
                    raise WeftTypeError(
                        f"JSON key {k!r} is not a valid Weft keyword; "
                        f"pass strict=False to keep it as-is",
                        code="WEFT_TYPE_JSON_KEY")
                out_d[k] = (yield (v, depth + 1))
            return out_d
        raise WeftTypeError(f"unsupported JSON value: {type(node).__name__}")

    return _run_trampoline(conv, value)


def to_json_lossless(value: Any) -> Any:
    """Lossless SPEC §11.2 mapping (tag table in _to_json_lossless_rec).
    Wrapped in the recursion-headroom guard — see to_json."""
    with _rec_headroom(1024):
        return _to_json_lossless_rec(value)


def _to_json_lossless_rec(value: Any) -> Any:
    """Lossless JSON mapping. Every Weft type becomes a tagged JSON object
    (except for plain-JSON types: null, bool, native-safe int, number).

    Tag conventions (one tag per object, no key collisions):

      {"$sym": "name"}                  Symbol
      {"$kw": "name"}                   Keyword
      {"$bigint": "..."}                BigInt (decimal digits)
      {"$int-big": "..."}               plain int outside the safe range
      {"$dec": "..."}                   Decimal (preserved text)
      {"$str": "..."}                   String (always wrapped, so a payload
                                         like "#sym:x" is never mistaken for
                                         a Symbol)
      {"$bin": {"enc": "base64", "data": "..."}}   Binary
      {"$dt": "ISO-8601"}               DateTime
      {"$float": "nan" | "inf" | "-inf"}  non-finite Float
      {"$sexpr": [tag, items...]}       Sexpr
      {"$typed": {"v": ..., "t": ...}}  Typed
      {"$list": [...]}                  List (wrapped so JSON array can be
                                         reserved for the "it's just a list"
                                         case under $sexpr etc.)
      {"$map": {"k": v, ...}}           Map

    Round-trip property: ``from_json_lossless(_to_json_lossless_rec(x)) == x``
    for every valid Weft tree.
    """
    if value is None: return None
    if value is True: return True
    if value is False: return False
    if isinstance(value, BigInt):
        return {"$bigint": _bigint_to_dec_str(value.value)}
    if isinstance(value, int):
        if _JSON_SAFE_INT_MIN <= value <= _JSON_SAFE_INT_MAX:
            return value
        return {"$int-big": _bigint_to_dec_str(value)}
    if isinstance(value, float):
        # Finite floats are also wrapped so a plain JSON number is
        # unambiguously a Weft Integer on the way back. The payload
        # preserves the shortest Python repr, matching the formatter's
        # canonical float form; sign is kept so ``-0.0`` round-trips.
        if math.isnan(value): return {"$float": "nan"}
        if math.isinf(value): return {"$float": "-inf" if value < 0 else "inf"}
        return {"$float": _float_canonical_text(value)}
    if isinstance(value, Decimal):
        # SPEC §7.2 Decimal canonicalisation: strip leading '+' and
        # normalise exponent marker to lowercase 'e'. The lossless $dec
        # payload holds the canonical text so round-trip through JSON
        # yields the same canonical bytes.
        text = value.text
        if text.startswith("+"):
            text = text[1:]
        text = text.replace("E", "e")
        return {"$dec": text}
    if isinstance(value, str):
        return {"$str": value}
    if isinstance(value, Binary):
        enc = value.encoding
        if enc == "base64":
            return {"$bin": {"enc": "base64", "data": base64.b64encode(value.data).decode("ascii")}}
        return {"$bin": {"enc": "hex", "data": value.data.hex()}}
    if isinstance(value, WeftDate):
        return {"$dt": _weft_date_canonical(value)}
    if isinstance(value, (_dt.datetime, _dt.date)):
        return {"$dt": _datetime_canonical(value) if isinstance(value, _dt.datetime)
                else f"{value.year:04d}-{value.month:02d}-{value.day:02d}"}
    if isinstance(value, Symbol):
        return {"$sym": value.name}
    if isinstance(value, Keyword):
        return {"$kw": value.name}
    if isinstance(value, list):
        return {"$list": [_to_json_lossless_rec(x) for x in value]}
    if isinstance(value, dict):
        return {"$map": {k: _to_json_lossless_rec(v) for k, v in value.items()}}
    if isinstance(value, Sexpr):
        return {"$sexpr": [value.tag.name] + [_to_json_lossless_rec(x) for x in value.items]}
    if isinstance(value, Typed):
        return {"$typed": {"v": _to_json_lossless_rec(value.value),
                            "t": _to_json_lossless_rec(value.type)}}
    raise WeftTypeError(f"cannot losslessly convert {type(value).__name__}")


_LOSSLESS_TAGS = frozenset({
    "$sym", "$kw", "$bigint", "$int-big", "$dec", "$str",
    "$bin", "$dt", "$float", "$sexpr", "$typed", "$list", "$map",
})


def from_json_lossless(value: Any, *, limits: "Limits" = None) -> Any:
    """Strict inverse of :func:`to_json_lossless` (SPEC §11.2.4).

    Accepts ONLY the tagged forms plus plain-JSON null/bool/safe-range
    integers. Bare strings, arrays, floats, out-of-range integers, unknown
    tags, and multi-key objects are rejected.

    SPEC §9: resource limits are enforced on this entry point exactly as
    on ``parse`` — ``max_depth`` (structural levels, WEFT_LIMIT_DEPTH),
    ``max_total_items`` (WEFT_LIMIT_TOTAL), ``max_items_per_coll``
    (WEFT_LIMIT_ITEMS), ``max_string_bytes`` on $str payloads
    (WEFT_LIMIT_STRING), and ``max_int_digits`` on $bigint / $int-big /
    $dec digits (WEFT_LIMIT_INT). The conversion is iterative (generator
    trampoline): adversarially deep input raises WEFT_LIMIT_DEPTH instead
    of exhausting the host call stack.
    """
    from .limits import DEFAULT_LIMITS
    lim = limits or DEFAULT_LIMITS
    counter = _TotalCounter(lim)

    def conv(node: Any, depth: int):
        counter.bump()
        if node is None:
            return None
        if node is True or node is False:
            return node
        if isinstance(node, int) and not isinstance(node, bool):
            if _JSON_SAFE_INT_MIN <= node <= _JSON_SAFE_INT_MAX:
                _check_int_digits(node, lim)
                return node
            raise WeftTypeError(
                "int outside JSON-safe range must use the "
                "{\"$int-big\": ...} tagged form")
        if isinstance(node, float):
            raise WeftTypeError(
                "float must use the {\"$float\": ...} tagged form "
                "(plain JSON number is reserved for Weft Integer)")
        if isinstance(node, str):
            raise WeftTypeError(
                "bare JSON string is not a lossless form; wrap it as "
                "{\"$str\": ...}, {\"$sym\": ...}, or {\"$kw\": ...}")
        if isinstance(node, list):
            raise WeftTypeError(
                "bare JSON array is not a lossless form; use "
                "{\"$list\": [...]} or {\"$sexpr\": [...]}")
        if not isinstance(node, dict):
            raise WeftTypeError(f"unsupported JSON value: {type(node).__name__}")

        if len(node) != 1:
            raise WeftTypeError(
                "lossless JSON object must have exactly one key "
                f"(got {len(node)}: {list(node)!r}); "
                "a plain mapping should be {\"$map\": {...}}")
        tag, payload = next(iter(node.items()))
        if tag not in _LOSSLESS_TAGS:
            raise WeftTypeError(
                f"unknown lossless tag {tag!r}; expected one of "
                f"{sorted(_LOSSLESS_TAGS)}")

        if tag == "$sym":
            if not isinstance(payload, str):
                raise WeftTypeError("$sym payload must be a string")
            try:
                return Symbol(payload)
            except ValueError as e:
                raise WeftTypeError(f"$sym payload invalid: {e}") from e

        if tag == "$kw":
            if not isinstance(payload, str):
                raise WeftTypeError("$kw payload must be a string")
            try:
                return Keyword(payload)
            except ValueError as e:
                raise WeftTypeError(f"$kw payload invalid: {e}") from e

        if tag in ("$bigint", "$int-big"):
            if not isinstance(payload, str):
                raise WeftTypeError(f"{tag} payload must be a string")
            from .validators import INT_PAYLOAD_RE, dec_str_to_int
            if not INT_PAYLOAD_RE.match(payload):
                raise WeftTypeError(
                    f"{tag} payload is not a decimal integer: {payload!r} "
                    "(must match -?[0-9]+)")
            n_digits = len(payload) - (1 if payload.startswith("-") else 0)
            if n_digits > lim.max_int_digits:
                raise WeftLimitError(
                    f"{tag} payload has {n_digits} digits, exceeds "
                    f"max_int_digits={lim.max_int_digits}",
                    code="WEFT_LIMIT_INT")
            # Chunked conversion: immune to CPython's int_max_str_digits
            # global (a valid 10,000-digit payload must decode, W4-05).
            val_i = dec_str_to_int(payload)
            return BigInt(value=val_i) if tag == "$bigint" else val_i

        if tag == "$dec":
            if not isinstance(payload, str):
                raise WeftTypeError("$dec payload must be a string")
            digit_count = sum(c.isdigit() for c in payload)
            if digit_count > lim.max_int_digits:
                raise WeftLimitError(
                    f"$dec payload has {digit_count} digits, exceeds "
                    f"max_int_digits={lim.max_int_digits}",
                    code="WEFT_LIMIT_INT")
            try:
                return Decimal(text=payload)
            except ValueError as e:
                raise WeftTypeError(f"$dec payload invalid: {e}") from e

        if tag == "$str":
            if not isinstance(payload, str):
                raise WeftTypeError("$str payload must be a string")
            _reject_surrogates(payload, "$str payload")
            if len(payload.encode("utf-8", "surrogatepass")) > lim.max_string_bytes:
                raise WeftLimitError(
                    f"$str payload exceeds max_string_bytes="
                    f"{lim.max_string_bytes}", code="WEFT_LIMIT_STRING")
            return payload

        if tag == "$bin":
            if not isinstance(payload, dict):
                raise WeftTypeError("$bin payload must be an object with 'enc' and 'data'")
            if set(payload) != {"enc", "data"}:
                raise WeftTypeError("$bin payload must have exactly {'enc', 'data'}")
            enc = payload.get("enc")
            data = payload.get("data")
            if not isinstance(data, str):
                raise WeftTypeError("$bin.data must be a string")
            if enc == "base64":
                from .validators import is_canonical_base64
                if not is_canonical_base64(data):
                    raise WeftTypeError(
                        f"$bin.data is not canonical base64 (non-zero "
                        f"trailing bits or invalid chars): {data!r}")
                raw = base64.b64decode(data, validate=True)
                return Binary(data=raw, encoding="base64")
            if enc == "hex":
                # Strict hex only — bytes.fromhex tolerates whitespace
                # ("00 00"), which other implementations reject; the
                # payload must be interoperable byte for byte.
                if len(data) % 2 != 0 or not _HEX_RE.fullmatch(data):
                    raise WeftTypeError(
                        f"$bin.data is not valid hex (even count of "
                        f"[0-9a-fA-F] only, no whitespace): {data!r}")
                raw = bytes.fromhex(data)
                return Binary(data=raw, encoding="hex")
            raise WeftTypeError(f"$bin.enc must be 'base64' or 'hex', got {enc!r}")

        if tag == "$dt":
            if not isinstance(payload, str):
                raise WeftTypeError("$dt payload must be a string")
            if not _DT_PAYLOAD_RE.match(payload):
                raise WeftTypeError(f"$dt payload is not a canonical datetime: {payload!r}")
            from .parser import parse
            try:
                result = parse("#" + payload)
                if isinstance(result, Typed):
                    raise WeftTypeError(
                        f"$dt payload must be a datetime scalar, not a "
                        f"type-annotated value: {payload!r}")
                if not isinstance(result, (_dt.datetime, _dt.date, WeftDate)):
                    raise WeftTypeError(f"$dt payload is not a datetime: {payload!r}")
                return result
            except WeftTypeError:
                raise
            except Exception as e:
                raise WeftTypeError(f"$dt payload invalid: {payload!r} ({e})") from e

        if tag == "$float":
            if not isinstance(payload, str):
                raise WeftTypeError("$float payload must be a string")
            if payload == "nan":
                return float("nan")
            if payload == "inf":
                return float("inf")
            if payload == "-inf":
                return float("-inf")
            if not _FLOAT_DEC_RE.match(payload):
                raise WeftTypeError(f"$float payload is not a finite decimal: {payload!r}")
            try:
                f = float(payload)
            except ValueError as e:
                raise WeftTypeError(f"$float payload invalid: {payload!r} ({e})") from e
            if math.isnan(f) or math.isinf(f):
                raise WeftTypeError(f"$float payload parsed to non-finite: {payload!r}")
            return f

        if tag == "$sexpr":
            if not isinstance(payload, list) or not payload:
                raise WeftTypeError("$sexpr payload must be a non-empty list")
            tag_name = payload[0]
            if not isinstance(tag_name, str):
                raise WeftTypeError("$sexpr tag (payload[0]) must be a string")
            try:
                tag_sym = Symbol(tag_name)
            except ValueError as e:
                raise WeftTypeError(f"$sexpr tag is not a valid Symbol name: {e}") from e
            _check_depth(depth + 1, lim)
            _check_coll(len(payload) - 1, lim)
            items = []
            for x in payload[1:]:
                items.append((yield (x, depth + 1)))
            return Sexpr(tag=tag_sym, items=items)

        if tag == "$typed":
            if not isinstance(payload, dict) or set(payload) != {"v", "t"}:
                raise WeftTypeError("$typed payload must be {\"v\":..., \"t\":...}")
            _check_depth(depth + 1, lim)
            inner_type = (yield (payload["t"], depth + 1))
            if not isinstance(inner_type, (Symbol, Sexpr)):
                raise WeftTypeError(
                    f"$typed.t must resolve to a Symbol or Sexpr, "
                    f"got {type(inner_type).__name__}")
            inner_value = (yield (payload["v"], depth + 1))
            try:
                return Typed(value=inner_value, type=inner_type)
            except (TypeError, ValueError) as e:
                raise WeftTypeError(f"$typed payload invalid: {e}") from e

        if tag == "$list":
            if not isinstance(payload, list):
                raise WeftTypeError("$list payload must be an array")
            _check_depth(depth + 1, lim)
            _check_coll(len(payload), lim)
            out = []
            for x in payload:
                out.append((yield (x, depth + 1)))
            return out

        if tag == "$map":
            if not isinstance(payload, dict):
                raise WeftTypeError("$map payload must be an object")
            _check_depth(depth + 1, lim)
            _check_coll(len(payload), lim)
            out_d: dict = {}
            for k, v in payload.items():
                if not isinstance(k, str):
                    raise WeftTypeError("$map key must be a string")
                if not is_valid_keyword_name(k):
                    raise WeftTypeError(
                        f"$map key {k!r} is not a valid Weft keyword name "
                        "(would break structure on re-parse)")
                out_d[k] = (yield (v, depth + 1))
            return out_d

        raise WeftTypeError(f"internal: unhandled lossless tag {tag!r}")

    return _run_trampoline(conv, value)


def dumps_json(value: Any, *, lossless: bool = True,
               indent: int | None = None) -> str:
    """Convenience: Weft tree → JSON string.

    Defaults to the LOSSLESS mapping (SPEC §11.2) so that
    ``loads_json(dumps_json(v))`` round-trips for every value — the two
    convenience functions share the same default mode. Pass
    ``lossless=False`` for the lossy §11.3 mapping (plain JSON types,
    not generally round-trippable).
    """
    j = to_json_lossless(value) if lossless else to_json(value)
    return json.dumps(j, indent=indent, ensure_ascii=False, sort_keys=False)


def loads_json(text: str, *, lossless: bool = True,
               limits: "Limits" = None) -> Any:
    """JSON string → Weft tree. SPEC §11.2 makes the LOSSLESS form the
    default for round-trip operations; pass ``lossless=False`` for the
    lossy §11.3 mapping.

    Hardening applied before/while the stdlib ``json`` parser runs:

    - ``max_size_bytes`` on the input text (WEFT_LIMIT_SIZE).
    - ``max_depth`` via a string-aware bracket pre-scan — bounds the C
      parser's recursion BEFORE it runs (WEFT_LIMIT_DEPTH).
    - Duplicate object keys are rejected (SPEC §11.2.5,
      WEFT_SYNTAX_JSON_DUPKEY) via ``object_pairs_hook``.
    - The non-standard extensions ``NaN`` / ``Infinity`` / ``-Infinity``
      are rejected (SPEC §11.3, WEFT_SYNTAX_JSON).
    - Integer tokens are length-checked against ``max_int_digits`` and
      converted with the chunked converter, so neither a huge literal nor
      CPython's ``int_max_str_digits`` global can produce a crash
      (WEFT_LIMIT_INT).
    """
    from .limits import DEFAULT_LIMITS
    from .validators import dec_str_to_int, json_max_nesting
    lim = limits or DEFAULT_LIMITS

    # A host str may contain lone surrogates (e.g. from a
    # ``surrogatepass``/``surrogateescape`` decode); that is not a valid
    # Unicode scalar sequence, so reject it as a Weft error rather than
    # letting the host's UnicodeEncodeError escape.
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise WeftSyntaxError(
            "JSON input contains lone surrogate code points",
            code="WEFT_SYNTAX_JSON") from None
    if size > lim.max_size_bytes:
        raise WeftLimitError(
            f"JSON input {size} bytes exceeds max_size_bytes={lim.max_size_bytes}",
            code="WEFT_LIMIT_SIZE")
    # The lossless encoding wraps every collection in a tag object
    # ({"$list": [...]}), so a Weft tree of depth D produces up to ~2*D
    # JSON nesting levels. Allow 2*max_depth + slack here; the precise
    # Weft-level depth check happens during conversion (_check_depth).
    nesting = json_max_nesting(text)
    if nesting > lim.max_depth * 2 + 8:
        raise WeftLimitError(
            f"JSON nesting depth {nesting} exceeds limit "
            f"{lim.max_depth * 2 + 8} (max_depth={lim.max_depth})",
            code="WEFT_LIMIT_DEPTH")

    def _pairs(pairs):
        d: dict = {}
        for k, v in pairs:
            if k in d:
                raise WeftSyntaxError(
                    f"duplicate key {k!r} in JSON object",
                    code="WEFT_SYNTAX_JSON_DUPKEY")
            d[k] = v
        return d

    def _int(tok: str) -> int:
        n_digits = len(tok) - (1 if tok[0] == "-" else 0)
        if n_digits > lim.max_int_digits:
            raise WeftLimitError(
                f"JSON integer has {n_digits} digits, exceeds "
                f"max_int_digits={lim.max_int_digits}", code="WEFT_LIMIT_INT")
        return dec_str_to_int(tok)

    def _const(name: str):
        raise WeftSyntaxError(
            f"non-standard JSON extension {name!r} is not accepted "
            "(SPEC §11.3)", code="WEFT_SYNTAX_JSON")

    try:
        # The C scanner in ``json`` obeys the interpreter recursion
        # limit; a max-depth Weft doc produces ~2x JSON nesting, which
        # must parse once the pre-scan admitted it.
        with _rec_headroom(lim.max_depth * 2 + 8):
            j = json.loads(text, object_pairs_hook=_pairs,
                           parse_int=_int, parse_constant=_const)
    except (WeftLimitError, WeftSyntaxError):
        raise
    except json.JSONDecodeError as e:
        raise WeftSyntaxError(f"invalid JSON: {e}", code="WEFT_SYNTAX_JSON") from e
    if lossless:
        return from_json_lossless(j, limits=lim)
    return from_json(j, limits=lim)
