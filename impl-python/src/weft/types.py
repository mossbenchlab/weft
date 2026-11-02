"""Weft value types.

A Weft tree is a Python object whose shape maps to the SPEC categories:

- ``None``                 → Null
- ``bool``                 → Bool
- ``int``                  → Integer (also BigInt; see ``BigInt`` for the
                              tagged form that preserves the ``n`` suffix)
- ``float``                → Float
- ``Decimal``              → Decimal (exact, with preserved significant digits)
- ``str``                  → String
- ``Binary``               → Binary blob
- ``datetime.datetime`` /
  ``datetime.date``        → DateTime
- ``Symbol``               → Symbol
- ``Keyword``              → Keyword
- ``list``                 → List
- ``dict``                 → Map (keys are strings; the leading ``:``
                              is implicit)
- ``Sexpr``                → S-expression
- ``Typed``                → TypedValue

The parser distinguishes BigInt from Integer by wrapping BigInt values
in the ``BigInt`` tag. The two parse to the same abstract value, but
round-tripping preserves the suffix.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal as _Decimal
from typing import Any, Dict, List, Optional, Union


class Decimal:
    """Exact decimal number, textually preserved.

    Weft Decimal values preserve their original significant digits and
    exponent form so that round-trips produce the same text. For arithmetic,
    callers should convert to the standard-library ``decimal.Decimal`` via
    ``to_stdlib()``.
    """

    __slots__ = ("text",)

    # ``\A ... \Z`` anchors (not ``^ ... $``) so a trailing newline cannot
    # sneak past the regex — ``$`` without ``re.MULTILINE`` matches right
    # before a terminating ``\n``. The pattern is also ASCII-only on the
    # digit classes to refuse full-width digits like ``１`` which the
    # lexer rejects.
    _VALID_RE = __import__("re").compile(
        r"\A[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
    )

    def __init__(self, text: str) -> None:
        if not isinstance(text, str) or not Decimal._VALID_RE.match(text):
            raise ValueError(f"invalid decimal text: {text!r}")
        # Canonicalise the exponent marker to lower-case so two inputs
        # that denote the same number — ``1E2`` and ``1e2`` — share
        # ``.text`` and compare equal. The lexer already emits lower-case
        # when parsing source text; this keeps directly-constructed
        # Decimals and parsed Decimals on the same canonical form, so a
        # lossless-JSON round-trip through ``$dec`` is identity-preserving.
        # SPEC §7.2 canonicalisation at construction time: a leading
        # '+' is dropped (matching the Integer rule) and the exponent
        # marker is lowercased. §5.16 defines Decimal equality on this
        # normalised text, so normalising here makes __eq__ correct for
        # ``Decimal("+1.5") == Decimal("1.5")`` without a separate path.
        if text.startswith("+"):
            text = text[1:]
        if "E" in text:
            text = text.replace("E", "e")
        self.text = text

    def to_stdlib(self) -> _Decimal:
        return _Decimal(self.text)

    def __repr__(self) -> str:
        return f"Decimal({self.text!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Decimal):
            # Textual equality — two Decimals with different sig-digit
            # expression are distinct values in Weft.
            return self.text == other.text
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Decimal", self.text))


_SYM_START_ASCII = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_+-*/<>=!?")
_ASCII_DIGITS = frozenset("0123456789")


def _is_sym_start(ch: str) -> bool:
    if ch in _SYM_START_ASCII:
        return True
    # Unicode letter above ASCII
    if ord(ch) > 0x7F and ch.isalpha():
        return True
    return False


def _is_sym_cont(ch: str) -> bool:
    if _is_sym_start(ch):
        return True
    # ASCII digits ONLY (Unicode digits like ①, １ are NOT symbol-cont)
    return ch in _ASCII_DIGITS


def _validate_identifier_name(name: str, label: str) -> None:
    """Validate a Symbol or Keyword name per SPEC §5.10. Raises ValueError.

    Rules enforced:
      - Non-empty string
      - First char is a symbol-start (ASCII letter/op/underscore OR Unicode letter)
      - Remaining chars are symbol-cont (above + ASCII digit)
      - Not a reserved literal (Symbol only)
      - Not sign+digit (ambiguates with signed number)
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label} name must be a non-empty string")
    if not _is_sym_start(name[0]):
        raise ValueError(f"{label} name {name!r}: invalid first character {name[0]!r}")
    for i, ch in enumerate(name[1:], 1):
        if not _is_sym_cont(ch):
            raise ValueError(f"{label} name {name!r}: invalid character {ch!r} at index {i}")
    if label == "Symbol" and name in ("true", "false", "nil", "nan", "inf", "-inf", "+inf"):
        raise ValueError(f"{label} name {name!r} is reserved as a literal")
    if len(name) >= 2 and name[0] in "+-" and name[1] in "0123456789":
        raise ValueError(
            f"{label} name {name!r} starts with sign+digit, which "
            "ambiguates with a signed number literal"
        )


def _validate_symbol_name(name: str) -> None:
    _validate_identifier_name(name, "Symbol")


def _validate_keyword_name(name: str) -> None:
    _validate_identifier_name(name, "Keyword")


def is_valid_symbol_name(name: str) -> bool:
    try:
        _validate_symbol_name(name)
        return True
    except ValueError:
        return False


def is_valid_keyword_name(name: str) -> bool:
    try:
        _validate_keyword_name(name)
        return True
    except ValueError:
        return False


def is_valid_map_key(name: str) -> bool:
    """True iff a string is valid as a Weft map key (same rules as Keyword name)."""
    return is_valid_keyword_name(name)


class Symbol:
    """A Weft symbol (identifier).

    Reserved: a symbol name cannot be ``true``, ``false``, ``nil``, ``nan``,
    ``inf``, or ``-inf`` (they are literals), and cannot start with
    ``+`` or ``-`` immediately followed by a digit (ambiguous with signed
    number literals).
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        _validate_symbol_name(name)
        self.name = name

    def __repr__(self) -> str:
        return f"Symbol({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Symbol) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("Symbol", self.name))


class Keyword:
    """A Weft keyword (``:name`` form, used as map keys).

    Keyword names follow the same rules as Symbol names (SPEC §5.10, §5.11).
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        _validate_keyword_name(name)
        self.name = name

    def __repr__(self) -> str:
        return f"Keyword({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Keyword) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("Keyword", self.name))


@dataclass(frozen=True)
class BigInt:
    """An integer that was written with the ``n`` suffix (BigInt literal).

    Semantically equivalent to a plain ``int``. The tag lets a formatter
    re-emit the ``n`` suffix.
    """

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TypeError("BigInt.value must be int")


@dataclass(frozen=True)
class WeftDate:
    """Weft DateTime value.

    Stores up to nanosecond precision. Years are 1..9999.
    ``has_time=False`` represents a date-only value.
    """

    year: int
    month: int
    day: int
    has_time: bool = False
    hour: int = 0
    minute: int = 0
    second: int = 0
    nano: int = 0  # 0..999_999_999
    tz_offset_minutes: Optional[int] = None  # None = no zone

    def __post_init__(self) -> None:
        if not (1 <= self.year <= 9999):
            raise ValueError(f"year {self.year} out of range 1-9999")
        if not (1 <= self.month <= 12):
            raise ValueError(f"month {self.month} out of range")
        if not (1 <= self.day <= 31):
            raise ValueError(f"day {self.day} out of range")
        if not (0 <= self.hour <= 23):
            raise ValueError(f"hour {self.hour} out of range")
        if not (0 <= self.minute <= 59):
            raise ValueError(f"minute {self.minute} out of range")
        if not (0 <= self.second <= 60):
            raise ValueError(f"second {self.second} out of range")
        if not (0 <= self.nano <= 999_999_999):
            raise ValueError(f"nano {self.nano} out of range")

    def to_stdlib_datetime(self):
        """Lossy conversion to a stdlib datetime (microsecond precision)."""
        if not self.has_time:
            return _dt.date(self.year, self.month, self.day)
        micro = self.nano // 1000
        tzinfo = None
        if self.tz_offset_minutes is not None:
            tzinfo = _dt.timezone(_dt.timedelta(minutes=self.tz_offset_minutes))
        return _dt.datetime(self.year, self.month, self.day,
                             self.hour, self.minute, self.second, micro, tzinfo=tzinfo)


@dataclass(frozen=True)
class Binary:
    """A binary blob with its original textual encoding tag.

    ``encoding`` is either ``"base64"`` or ``"hex"``; this is preserved so
    that a round-trip emits the same form the input used.
    """

    data: bytes
    encoding: str = "base64"

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)):
            raise TypeError("Binary.data must be bytes")
        if self.encoding not in ("base64", "hex"):
            raise ValueError(f"Binary.encoding must be 'base64' or 'hex', got {self.encoding!r}")


@dataclass(frozen=False)
class Sexpr:
    """A tagged S-expression: ``(tag items...)``."""

    tag: Symbol
    items: List[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.tag, Symbol):
            raise TypeError("Sexpr.tag must be a Symbol")
        if not isinstance(self.items, list):
            raise TypeError("Sexpr.items must be a list")

    def positional_and_keyword(self) -> "tuple[list, dict[str, Any]]":
        """Split items into positional args and keyword-value pairs.

        A ``Keyword`` item is treated as the beginning of a key-value pair;
        the following item is its value. All other items are positional.
        A trailing keyword with no value raises WeftSyntaxError.
        """
        from .errors import WeftSyntaxError
        pos: list = []
        kwargs: dict[str, Any] = {}
        i = 0
        while i < len(self.items):
            it = self.items[i]
            if isinstance(it, Keyword):
                if i + 1 >= len(self.items):
                    raise WeftSyntaxError(
                        f"keyword :{it.name} has no value"
                    )
                kwargs[it.name] = self.items[i + 1]
                i += 2
            else:
                pos.append(it)
                i += 1
        return pos, kwargs

    def __repr__(self) -> str:
        return f"Sexpr({self.tag.name!r}, {self.items!r})"


@dataclass(frozen=False)
class Typed:
    """A value annotated with a type expression."""

    value: Any
    type: Union[Symbol, Sexpr]

    def __post_init__(self) -> None:
        if not isinstance(self.type, (Symbol, Sexpr)):
            raise TypeError(
                "Typed.type must be a Symbol or a Sexpr (type expression)"
            )

    def __repr__(self) -> str:
        return f"Typed({self.value!r}::{self.type!r})"



# --------------------------------------------------------------------------
# Equality (SPEC §5.16)
# --------------------------------------------------------------------------

def _float_bits(f: float) -> bytes:
    import struct as _struct
    return _struct.pack("<d", f)


def equals(a: Any, b: Any) -> bool:
    """Weft value equality ``a ≡ b`` per SPEC §5.16.

    - Integer/BigInt compare numerically across the two host shapes
      (``42 ≡ 42n``); every other category never equals a different one.
    - Float equality is IEEE-754 bit identity, except that any NaN is
      unequal to everything including itself; ``-0.0 ≢ 0.0``.
    - Decimal equality is normalised-text equality (leading ``+``
      stripped, exponent marker lowercased — applied by the constructor).
    - Binary compares content bytes; the base64/hex tag is presentation.
    - DateTime compares shape (date-only / naive / offset) and all fields;
      offsets compare numerically in minutes (``+00:00`` ≡ ``Z``).
    - Map equality is unordered; List/Sexpr are ordered.

    Implemented iteratively (explicit work stack) so adversarially deep
    trees cannot exhaust the host call stack.
    """
    stack: list = [(a, b)]
    while stack:
        x, y = stack.pop()
        # Nil
        if x is None or y is None:
            if not (x is None and y is None):
                return False
            continue
        # Bool (check before int: bool is an int subclass)
        xb, yb = isinstance(x, bool), isinstance(y, bool)
        if xb or yb:
            if not (xb and yb and x == y):
                return False
            continue
        # Integer / BigInt (cross-type numeric equality, SPEC §5.4)
        xi = x.value if isinstance(x, BigInt) else (x if isinstance(x, int) else None)
        yi = y.value if isinstance(y, BigInt) else (y if isinstance(y, int) else None)
        if xi is not None or yi is not None:
            if xi is None or yi is None or xi != yi:
                return False
            continue
        if isinstance(x, float) or isinstance(y, float):
            if not (isinstance(x, float) and isinstance(y, float)):
                return False
            if x != x or y != y:  # NaN never equals anything (SPEC §5.16)
                return False
            if _float_bits(x) != _float_bits(y):
                return False
            continue
        if isinstance(x, Decimal) or isinstance(y, Decimal):
            if not (isinstance(x, Decimal) and isinstance(y, Decimal) and x.text == y.text):
                return False
            continue
        if isinstance(x, str) or isinstance(y, str):
            if not (isinstance(x, str) and isinstance(y, str) and x == y):
                return False
            continue
        if isinstance(x, Binary) or isinstance(y, Binary):
            if not (isinstance(x, Binary) and isinstance(y, Binary)
                    and bytes(x.data) == bytes(y.data)):
                return False
            continue
        if isinstance(x, WeftDate) or isinstance(y, WeftDate):
            if not (isinstance(x, WeftDate) and isinstance(y, WeftDate)):
                return False
            if (x.year, x.month, x.day, x.has_time, x.hour, x.minute,
                x.second, x.nano, x.tz_offset_minutes) != \
               (y.year, y.month, y.day, y.has_time, y.hour, y.minute,
                y.second, y.nano, y.tz_offset_minutes):
                return False
            continue
        if isinstance(x, Symbol) or isinstance(y, Symbol):
            if not (isinstance(x, Symbol) and isinstance(y, Symbol) and x.name == y.name):
                return False
            continue
        if isinstance(x, Keyword) or isinstance(y, Keyword):
            if not (isinstance(x, Keyword) and isinstance(y, Keyword) and x.name == y.name):
                return False
            continue
        if isinstance(x, list) or isinstance(y, list):
            if not (isinstance(x, list) and isinstance(y, list) and len(x) == len(y)):
                return False
            stack.extend(zip(x, y))
            continue
        if isinstance(x, dict) or isinstance(y, dict):
            if not (isinstance(x, dict) and isinstance(y, dict)
                    and x.keys() == y.keys()):
                return False
            for k in x:
                stack.append((x[k], y[k]))
            continue
        if isinstance(x, Sexpr) or isinstance(y, Sexpr):
            if not (isinstance(x, Sexpr) and isinstance(y, Sexpr)
                    and x.tag.name == y.tag.name
                    and len(x.items) == len(y.items)):
                return False
            stack.extend(zip(x.items, y.items))
            continue
        if isinstance(x, Typed) or isinstance(y, Typed):
            if not (isinstance(x, Typed) and isinstance(y, Typed)):
                return False
            stack.append((x.value, y.value))
            stack.append((x.type, y.type))
            continue
        return False
    return True


# Convenience constants
TRUE = True
FALSE = False
NIL = None
NAN = float("nan")
INF = float("inf")
NEG_INF = float("-inf")


__all__ = [
    "equals",
    "Symbol",
    "Keyword",
    "BigInt",
    "Binary",
    "WeftDate",
    "Sexpr",
    "Typed",
    "Decimal",
    "TRUE",
    "FALSE",
    "NIL",
    "NAN",
    "INF",
    "NEG_INF",
]
