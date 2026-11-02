"""Iterative JSON reader and writer.

``weft`` cannot delegate JSON text handling to the host's ``json``
module for conforming behavior:

* the stdlib scanner silently last-wins duplicate keys unless hooked,
  and its C implementation recurses on the C stack with a host- and
  version-dependent depth tolerance (CPython 3.12 ties it to
  ``sys.getrecursionlimit()``; 3.13 to the real C stack), so the depth
  a §9 ``max_depth`` setting admits may or may not decode depending on
  the interpreter — and the only stdlib workaround is mutating the
  process-wide recursion limit, which is not thread-safe;
* limits must apply **while reading** (SPEC §9), not after a full tree
  is built.

Both directions here run on explicit heap stacks: no recursion, no
``sys.setrecursionlimit``, no timeouts. The reader is the same
token-level design as the TypeScript/Rust/Go lexical readers:

* duplicate object keys → ``WEFT_SYNTAX_JSON_DUPKEY`` (SPEC §11.2.5),
* object key order preserved (plain insertion-ordered dict),
* the ``1`` vs ``1.0`` lexical distinction survives (integer tokens
  become int via the chunked converter, fraction/exponent tokens become
  float),
* ``NaN`` / ``Infinity`` extensions rejected (not JSON),
* lone surrogates in ``\\u`` escapes rejected (§5.7),
* structural caps while reading: nesting ≤ ``2*max_depth+8``, elements
  per array/object ≤ ``2*max_items_per_coll+8``, integer-token digits ≤
  ``max_int_digits`` (representation-level bounds per SPEC §9; the
  exact Weft limits are enforced at conversion).
"""
from __future__ import annotations

import decimal as _decimal
import math as _math
import re as _re
from typing import Any, List, Optional

from .errors import WeftLimitError, WeftSyntaxError
from .limits import Limits

_WS = " \t\n\r"
_NUM_RE = _re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
# Longest run of plain (unescaped, non-terminator) string characters.
_CHUNK_RE = _re.compile(r'[^"\\\x00-\x1f]+')
_HEX4_RE = _re.compile(r"[0-9a-fA-F]{4}")


def int_to_dec_str(x: int) -> str:
    """Decimal digits of ``x`` without tripping CPython's
    ``int_max_str_digits`` guard (the C ``int``→``Decimal`` conversion
    and ``Decimal.__str__`` are not subject to it)."""
    return str(_decimal.Decimal(x))


class _Reader:
    def __init__(self, text: str, limits: Limits):
        self.s = text
        self.i = 0
        self.limits = limits
        self.max_nest = limits.max_depth * 2 + 8
        self.max_elems = limits.max_items_per_coll * 2 + 8

    def err(self, msg: str, code: str = "WEFT_SYNTAX_JSON") -> WeftSyntaxError:
        return WeftSyntaxError(f"{msg} (offset {self.i})", code=code)

    def ws(self) -> None:
        s, i, n = self.s, self.i, len(self.s)
        while i < n and s[i] in _WS:
            i += 1
        self.i = i

    def parse(self) -> Any:
        self.ws()
        v = self._value_iter()
        self.ws()
        if self.i != len(self.s):
            raise self.err("unexpected trailing content in JSON")
        return v

    # --- iterative value loop -------------------------------------------

    def _value_iter(self) -> Any:
        # Frames: ['L', list] | ['O', dict, pending_key_or_None]
        stack: List[list] = []
        value: Any = None
        have_value = False

        while True:
            if not have_value:
                # read the start of one value
                self.ws()
                if self.i >= len(self.s):
                    raise self.err("unexpected end of JSON")
                c = self.s[self.i]
                if c == "[":
                    self.i += 1
                    if len(stack) >= self.max_nest:
                        raise WeftLimitError(
                            f"JSON nesting exceeds limit {self.max_nest} "
                            f"(max_depth={self.limits.max_depth})",
                            code="WEFT_LIMIT_DEPTH")
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == "]":
                        self.i += 1
                        value, have_value = [], True
                        continue
                    stack.append(["L", []])
                    continue
                if c == "{":
                    self.i += 1
                    if len(stack) >= self.max_nest:
                        raise WeftLimitError(
                            f"JSON nesting exceeds limit {self.max_nest} "
                            f"(max_depth={self.limits.max_depth})",
                            code="WEFT_LIMIT_DEPTH")
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == "}":
                        self.i += 1
                        value, have_value = {}, True
                        continue
                    frame = ["O", {}, None]
                    self._read_object_key(frame)
                    stack.append(frame)
                    continue
                value = self._scalar(c)
                have_value = True
                continue

            # a value is complete; feed it to the enclosing frame
            if not stack:
                return value
            frame = stack[-1]
            if frame[0] == "L":
                frame[1].append(value)
                if len(frame[1]) > self.max_elems:
                    raise WeftLimitError(
                        f"JSON array exceeds structural bound "
                        f"(max_items_per_coll={self.limits.max_items_per_coll})",
                        code="WEFT_LIMIT_ITEMS")
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == ",":
                    self.i += 1
                    have_value = False
                    continue
                if self.i < len(self.s) and self.s[self.i] == "]":
                    self.i += 1
                    stack.pop()
                    # value stays = the finished list
                    value = frame[1]
                    continue
                raise self.err("expected ',' or ']' in JSON array")
            else:  # object
                frame[1][frame[2]] = value
                frame[2] = None
                if len(frame[1]) > self.max_elems:
                    raise WeftLimitError(
                        f"JSON object exceeds structural bound "
                        f"(max_items_per_coll={self.limits.max_items_per_coll})",
                        code="WEFT_LIMIT_ITEMS")
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == ",":
                    self.i += 1
                    self._read_object_key(frame)
                    have_value = False
                    continue
                if self.i < len(self.s) and self.s[self.i] == "}":
                    self.i += 1
                    stack.pop()
                    value = frame[1]
                    continue
                raise self.err("expected ',' or '}' in JSON object")

    def _read_object_key(self, frame: list) -> None:
        self.ws()
        if self.i >= len(self.s) or self.s[self.i] != '"':
            raise self.err("expected string key in JSON object")
        k = self._string()
        if k in frame[1]:
            raise WeftSyntaxError(
                f"duplicate key {k!r} in JSON object",
                code="WEFT_SYNTAX_JSON_DUPKEY")
        frame[2] = k
        self.ws()
        if self.i >= len(self.s) or self.s[self.i] != ":":
            raise self.err("expected ':' in JSON object")
        self.i += 1

    # --- scalars ----------------------------------------------------------

    def _scalar(self, c: str) -> Any:
        s = self.s
        if c == '"':
            return self._string()
        if c == "t":
            if s.startswith("true", self.i):
                self.i += 4
                return True
            raise self.err("invalid JSON literal")
        if c == "f":
            if s.startswith("false", self.i):
                self.i += 5
                return False
            raise self.err("invalid JSON literal")
        if c == "n":
            if s.startswith("null", self.i):
                self.i += 4
                return None
            raise self.err("invalid JSON literal")
        if c == "-" or "0" <= c <= "9":
            return self._number()
        if c in "NI" or (c == "-" and s.startswith(("-Infinity",), self.i)):
            raise self.err("non-standard JSON extension is not accepted (SPEC §11.3)")
        raise self.err(f"unexpected character {c!r} in JSON")

    def _number(self) -> Any:
        m = _NUM_RE.match(self.s, self.i)
        if m is None or m.start() != self.i:
            raise self.err("invalid JSON number")
        tok = m.group(0)
        self.i = m.end()
        if "." in tok or "e" in tok or "E" in tok:
            # Lexically a float (SPEC §11.2.1 rule 4 / §11.3). Overflow
            # follows the host float() rule (→ ±inf), matching the other
            # implementations' strconv/Number behavior.
            return float(tok)
        digits = len(tok) - (1 if tok[0] == "-" else 0)
        if digits > self.limits.max_int_digits:
            raise WeftLimitError(
                f"JSON integer has {digits} digits, exceeds "
                f"max_int_digits={self.limits.max_int_digits}",
                code="WEFT_LIMIT_INT")
        from .validators import dec_str_to_int
        return dec_str_to_int(tok)

    def _string(self) -> str:
        # at the opening quote
        self.i += 1
        s = self.s
        out: List[str] = []
        pending_high = -1

        def flush_pending():
            if pending_high >= 0:
                raise self.err(
                    "lone surrogate in JSON string escape "
                    "(Weft strings are Unicode scalar sequences)")

        while True:
            if self.i >= len(s):
                raise self.err("unterminated JSON string")
            c = s[self.i]
            if c == '"':
                flush_pending()
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = s[self.i + 1] if self.i + 1 < len(s) else ""
                if e == "u":
                    m = _HEX4_RE.match(s, self.i + 2)
                    if m is None:
                        raise self.err("invalid \\u escape in JSON")
                    cu = int(m.group(0), 16)
                    self.i += 6
                    if 0xD800 <= cu <= 0xDBFF:
                        flush_pending()
                        pending_high = cu
                        continue
                    if 0xDC00 <= cu <= 0xDFFF:
                        if pending_high < 0:
                            raise self.err(
                                "lone surrogate in JSON string escape "
                                "(Weft strings are Unicode scalar sequences)")
                        out.append(chr(0x10000
                                       + ((pending_high - 0xD800) << 10)
                                       + (cu - 0xDC00)))
                        pending_high = -1
                        continue
                    flush_pending()
                    out.append(chr(cu))
                    continue
                flush_pending()
                mapped = _ESCAPES.get(e)
                if mapped is None:
                    raise self.err(f"invalid JSON escape \\{e!r}")
                out.append(mapped)
                self.i += 2
                continue
            flush_pending()
            m = _CHUNK_RE.match(s, self.i)
            if m is None:
                # a control character (< 0x20) reached here
                raise self.err("unescaped control character in JSON string")
            out.append(m.group(0))
            self.i = m.end()


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t"}


def parse_json(text: str, limits: Optional[Limits] = None) -> Any:
    """Parse JSON text iteratively with SPEC §9 limits applied while
    reading. Returns plain Python values (dict preserves key order)."""
    from .limits import DEFAULT_LIMITS
    return _Reader(text, limits or DEFAULT_LIMITS).parse()


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------

_WRITE_ESC = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r",
              "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _write_str(s: str, out: List[str]) -> None:
    out.append('"')
    start = 0
    for i, ch in enumerate(s):
        esc = _WRITE_ESC.get(ch)
        if esc is None and ch >= " ":
            continue
        out.append(s[start:i])
        out.append(esc if esc is not None else f"\\u{ord(ch):04x}")
        start = i + 1
    out.append(s[start:])
    out.append('"')


def dump_json(value: Any, *, indent: Optional[int] = None,
              sort_keys: bool = False) -> str:
    """Serialize a JSON-ready tree (None/bool/int/float/str/list/dict)
    iteratively — no recursion, so any §9-admitted depth serializes on
    every host. Output is UTF-8-friendly (non-ASCII stays literal)."""
    out: List[str] = []
    # Work stack of closures would recurse via Python frames; use an
    # explicit todo stack of (kind, payload) entries instead.
    TEXT, VALUE = 0, 1
    todo: List[tuple] = [(VALUE, value, 0)]
    nl = "\n" if indent else ""

    def pad(level: int) -> str:
        return " " * (indent * level) if indent else ""

    while todo:
        kind, item, level = todo.pop()
        if kind == TEXT:
            out.append(item)
            continue
        v = item
        if v is None:
            out.append("null")
        elif v is True:
            out.append("true")
        elif v is False:
            out.append("false")
        elif isinstance(v, int):
            out.append(int_to_dec_str(v))
        elif isinstance(v, float):
            if _math.isnan(v) or _math.isinf(v):
                raise ValueError("non-finite float is not valid JSON")
            out.append(repr(v))
        elif isinstance(v, str):
            _write_str(v, out)
        elif isinstance(v, list):
            if not v:
                out.append("[]")
                continue
            out.append("[" + nl)
            parts: List[tuple] = []
            for idx, x in enumerate(v):
                if idx:
                    parts.append((TEXT, "," + nl + pad(level + 1) if indent
                                  else ", ", 0))
                elif indent:
                    parts.append((TEXT, pad(level + 1), 0))
                parts.append((VALUE, x, level + 1))
            todo.append((TEXT, nl + pad(level) + "]" if indent else "]", 0))
            todo.extend(reversed(parts))
        elif isinstance(v, dict):
            if not v:
                out.append("{}")
                continue
            out.append("{" + nl)
            keys = sorted(v.keys()) if sort_keys else list(v.keys())
            parts = []
            for idx, k in enumerate(keys):
                if not isinstance(k, str):
                    raise TypeError("JSON object keys must be strings")
                if idx:
                    parts.append((TEXT, "," + nl + pad(level + 1) if indent
                                  else ", ", 0))
                elif indent:
                    parts.append((TEXT, pad(level + 1), 0))
                kout: List[str] = []
                _write_str(k, kout)
                parts.append((TEXT, "".join(kout) + ": ", 0))
                parts.append((VALUE, v[k], level + 1))
            todo.append((TEXT, nl + pad(level) + "}" if indent else "}", 0))
            todo.extend(reversed(parts))
        else:
            raise TypeError(f"not JSON-serializable: {type(v).__name__}")
    return "".join(out)
