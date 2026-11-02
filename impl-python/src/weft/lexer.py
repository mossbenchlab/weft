"""Weft lexer.

The lexer consumes a UTF-8 string and produces a stream of tokens.
Each token has a kind and a value, plus source position info (line,
column, byte offset) for error reporting.

The lexer is strict:
  - It rejects BOM anywhere in the document (SPEC §3).
  - It rejects overlong UTF-8 (handled by Python's UTF-8 decoder when
    the input is a str; the ``tokenize_bytes`` entry point adds explicit
    checking).
  - It rejects control characters in strings and comments as specified.
  - It enforces per-token size limits (string length, integer digits).

The lexer does NOT:
  - Enforce depth or total item count (that is the parser's job).
  - Interpret tokens beyond converting their textual form to a Python
    value (strings get escapes processed, integers become ``int``).
"""
from __future__ import annotations

import re
import base64
import binascii
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from .errors import WeftLexError, WeftLimitError, WeftEncodingError
from .limits import Limits, DEFAULT_LIMITS
from .types import BigInt, Binary, Symbol, Keyword, WeftDate, is_valid_symbol_name


# --------------------------------------------------------------------------
# Token kinds
# --------------------------------------------------------------------------

TK_LPAREN = "LPAREN"
TK_RPAREN = "RPAREN"
TK_LBRACK = "LBRACK"
TK_RBRACK = "RBRACK"
TK_LBRACE = "LBRACE"
TK_RBRACE = "RBRACE"
TK_COLONCOLON = "COLONCOLON"
TK_INT = "INT"
TK_BIGINT = "BIGINT"
TK_FLOAT = "FLOAT"
TK_DECIMAL = "DECIMAL"
TK_STRING = "STRING"
TK_BINARY = "BINARY"
TK_DATETIME = "DATETIME"
TK_BOOL = "BOOL"
TK_NIL = "NIL"
TK_NAN = "NAN"
TK_INF = "INF"
TK_NEG_INF = "NEG_INF"
TK_SYMBOL = "SYMBOL"
TK_KEYWORD = "KEYWORD"


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    line: int
    column: int
    offset: int
    byte_offset: int = -1  # populated by the lexer; -1 means "not set"


# --------------------------------------------------------------------------
# Character classes
# --------------------------------------------------------------------------

# Whitespace per SPEC §4.1: U+0009, U+000A, U+000D, U+0020, U+002C
_WS_SET = frozenset("\t\n\r ,")

# Symbol start characters (ASCII portion). Unicode letters above U+007F are
# handled by a positive isalpha() check on the first scanned character.
_SYM_START_ASCII = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_+-*/<>=!?"
)
# Symbol continuation adds digits.
_SYM_CONT_ASCII = _SYM_START_ASCII | frozenset("0123456789")

# Reserved tokens that look like symbols but are not.
_RESERVED_LITERALS = frozenset(("true", "false", "nil", "nan", "inf"))

# Forbidden control characters inside comments and strings (SPEC §4.2, §5.7)
# U+0000-U+0008, U+000B, U+000C, U+000E-U+001F
_FORBIDDEN_CTRL = frozenset(
    chr(c) for c in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20))
)

# Unicode bidirectional override / isolate controls (CVE-2021-42574 Trojan Source).
# These must be rejected in comments where they could reorder visible text.
# U+202A LRE, U+202B RLE, U+202C PDF, U+202D LRO, U+202E RLO,
# U+2066 LRI, U+2067 RLI, U+2068 FSI, U+2069 PDI.
_BIDI_CONTROL = frozenset(
    chr(c) for c in [0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                      0x2066, 0x2067, 0x2068, 0x2069]
)

# C1 controls (U+0080-U+009F) include CSI (U+009B) which is a terminal
# escape injection vector that bypasses the \e-based C0 CSI (U+001B U+005B).
# DEL (U+007F) can also drive terminal state in some emulators.
_C1_AND_DEL = frozenset(chr(c) for c in list(range(0x7F, 0xA0)))

# Characters that must never appear in a comment (combined set).
# SPEC §4.2 + §10.5: C0-except-TAB/LF/CR + DEL + C1 + bidi.
_COMMENT_FORBIDDEN = _FORBIDDEN_CTRL | _BIDI_CONTROL | _C1_AND_DEL


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------

class Lexer:
    """Weft tokenizer with configurable resource limits."""

    def __init__(self, text: str, limits: Limits = DEFAULT_LIMITS) -> None:
        if not isinstance(text, str):
            raise TypeError("Lexer input must be str (decoded UTF-8)")

        # Size early-reject (SPEC §9): ``len(text)`` is a lower bound on
        # UTF-8 byte count. If that already exceeds the limit we skip
        # the per-char encoding scans below — a 2 MB input with
        # ``max_size_bytes=1`` rejects in micro-seconds instead of
        # walking the whole string looking for surrogates we won't use.
        _ml = limits.max_size_bytes
        n = len(text)
        if n > _ml:
            raise WeftLimitError(
                f"document size at least {n} bytes exceeds max_size_bytes={_ml}",
                code="WEFT_LIMIT_SIZE",
            )

        # BOM check (SPEC §3): U+FEFF must not appear anywhere.
        if "﻿" in text:
            idx = text.index("﻿")
            line, col = _pos_from_offset(text, idx)
            raise WeftEncodingError(
                "U+FEFF (BOM) is not permitted anywhere in a Weft document",
                line=line, column=col, offset=idx,
                byte_offset=_byte_offset_at(text, idx),
                code="WEFT_ENCODING_BOM",
            )

        # Noncharacters U+FFFE/U+FFFF
        for bad in ("￾", "￿"):
            if bad in text:
                idx = text.index(bad)
                line, col = _pos_from_offset(text, idx)
                raise WeftEncodingError(
                    f"noncharacter U+{ord(bad):04X} is not permitted",
                    line=line, column=col, offset=idx,
                    byte_offset=_byte_offset_at(text, idx),
                    code="WEFT_ENCODING_NONCHAR",
                )

        # Unpaired surrogates — Python's str allows them, but SPEC forbids
        # them in the on-wire UTF-8. Scan BEFORE calling .encode("utf-8")
        # so a surrogate half surfaces as WeftEncodingError, not as
        # UnicodeEncodeError.
        for i, ch in enumerate(text):
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                line, col = _pos_from_offset(text, i)
                raise WeftEncodingError(
                    f"unpaired surrogate U+{cp:04X} is not permitted",
                    line=line, column=col, offset=i,
                    byte_offset=_byte_offset_at(text, i),
                    code="WEFT_ENCODING_SURROGATE",
                )

        # Exact UTF-8 byte count for the "short-but-multi-byte" case the
        # early reject above couldn't resolve. ``4 * len(text)`` is a
        # tight upper bound on ``len(text.encode("utf-8"))``; when it's
        # already under the limit we skip the actual encode too.
        if n * 4 > _ml:
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError as e:
                raise WeftEncodingError(
                    f"input is not valid UTF-8: {e}",
                    code="WEFT_ENCODING_UTF8",
                ) from None
            if size > _ml:
                raise WeftLimitError(
                    f"document size {size} exceeds max_size_bytes={_ml}",
                    code="WEFT_LIMIT_SIZE",
                )

        self.text = text
        self.pos = 0
        self.byte_pos = 0  # UTF-8 byte offset, kept in step with pos
        self.line = 1
        self.col = 1
        self.limits = limits

    # ---- public API ----

    def tokenize(self) -> Iterator[Token]:
        """Yield tokens until end of input."""
        while True:
            tok = self._next_token()
            if tok is None:
                return
            yield tok

    # ---- primitives ----

    def _peek(self, offset: int = 0) -> Optional[str]:
        p = self.pos + offset
        if p < len(self.text):
            return self.text[p]
        return None

    def _advance(self, n: int = 1) -> None:
        for _ in range(n):
            if self.pos >= len(self.text):
                return
            ch = self.text[self.pos]
            if ch == "\n":
                self.line += 1
                self.col = 1
            elif ch == "\r":
                # CRLF: skip, the \n will handle the line count.
                # Lone CR: counts as a line terminator (SPEC §3).
                if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "\n":
                    pass  # defer line++ to the next \n
                else:
                    self.line += 1
                    self.col = 1
            else:
                self.col += 1
            # Track UTF-8 byte offset in lockstep with the character offset.
            # 1 byte for ASCII, 2 for U+0080..U+07FF, 3 for U+0800..U+FFFF,
            # 4 for U+10000 and above.
            cp = ord(ch)
            if cp < 0x80:
                self.byte_pos += 1
            elif cp < 0x800:
                self.byte_pos += 2
            elif cp < 0x10000:
                self.byte_pos += 3
            else:
                self.byte_pos += 4
            self.pos += 1

    def _error(self, msg: str, code: str = "WEFT_SYNTAX") -> WeftLexError:
        return WeftLexError(
            msg,
            line=self.line, column=self.col,
            offset=self.pos, byte_offset=self.byte_pos,
            code=code,
        )

    def _limit_error(self, msg: str, code: str) -> WeftLimitError:
        return WeftLimitError(
            msg,
            line=self.line, column=self.col,
            offset=self.pos, byte_offset=self.byte_pos,
            code=code,
        )

    # ---- the big loop ----

    def _next_token(self) -> Optional[Token]:
        # Skip whitespace and comments
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch in _WS_SET:
                self._advance()
                continue
            # Non-whitespace Unicode spaces are explicitly forbidden by §4.1
            if _is_rejected_whitespace(ch):
                raise self._error(
                    f"Unicode whitespace U+{ord(ch):04X} is not permitted "
                    f"outside strings and comments",
                    code="WEFT_SYNTAX_WS",
                )
            if ch == ";":
                self._skip_line_comment()
                continue
            if ch == "#" and self._peek(1) == "|":
                self._skip_block_comment()
                continue
            # Not whitespace/comment; break out
            break

        if self.pos >= len(self.text):
            return None

        start_line, start_col, start_off = self.line, self.col, self.pos
        start_byte_off = self.byte_pos
        ch = self.text[self.pos]
        nxt = self._peek(1)

        # Single-character structural tokens
        if ch == "(":
            self._advance()
            return Token(TK_LPAREN, None, start_line, start_col, start_off, start_byte_off)
        if ch == ")":
            self._advance()
            return Token(TK_RPAREN, None, start_line, start_col, start_off, start_byte_off)
        if ch == "[":
            self._advance()
            return Token(TK_LBRACK, None, start_line, start_col, start_off, start_byte_off)
        if ch == "]":
            self._advance()
            return Token(TK_RBRACK, None, start_line, start_col, start_off, start_byte_off)
        if ch == "{":
            self._advance()
            return Token(TK_LBRACE, None, start_line, start_col, start_off, start_byte_off)
        if ch == "}":
            self._advance()
            return Token(TK_RBRACE, None, start_line, start_col, start_off, start_byte_off)
        if ch == ":" and nxt == ":":
            self._advance(2)
            return Token(TK_COLONCOLON, None, start_line, start_col, start_off, start_byte_off)

        # Keyword  :name
        if ch == ":":
            self._advance()
            name = self._read_symbol_name()
            if not name:
                raise WeftLexError(
                    "':' must be followed by a symbol name",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_KEYWORD",
                )
            # SPEC §5.10: a name beginning with sign+digit is reserved for
            # signed-number literals and is not a legal keyword/symbol —
            # ``:+1`` must not parse, even though ``+`` is a symbol-start
            # and ``1`` is a symbol-cont. Rejecting here keeps the lexer's
            # acceptance set in step with the formatter's output set.
            if len(name) >= 2 and name[0] in "+-" and name[1] in "0123456789":
                raise WeftLexError(
                    f"keyword name {name!r} starts with sign+digit, which "
                    "ambiguates with a signed number literal",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_KEYWORD",
                )
            return Token(TK_KEYWORD, name, start_line, start_col, start_off, start_byte_off)

        # String literals (standard, triple, raw)
        if ch == '"':
            # Triple-quoted?
            if self._peek(1) == '"' and self._peek(2) == '"':
                s = self._read_triple_string()
                return Token(TK_STRING, s, start_line, start_col, start_off, start_byte_off)
            s = self._read_dquote_string()
            return Token(TK_STRING, s, start_line, start_col, start_off, start_byte_off)

        if ch == "r" and nxt == '"':
            self._advance()  # consume 'r'
            s = self._read_raw_string()
            return Token(TK_STRING, s, start_line, start_col, start_off, start_byte_off)

        if ch == "b" and nxt == '"':
            self._advance()  # consume 'b'
            b = self._read_binary()
            return Token(TK_BINARY, b, start_line, start_col, start_off, start_byte_off)

        # DateTime  #YYYY-...
        if ch == "#":
            # Could be block-comment start (#|) but we checked above; also
            # could be a reserved tag or a datetime. Check next character.
            if nxt is None:
                raise self._error("stray '#' at end of input", "WEFT_SYNTAX_HASH")
            if nxt == "!":
                raise self._error(
                    "'#!' at document start is reserved for a future shebang form",
                    "WEFT_RESERVED_SHEBANG",
                )
            if ("0" <= nxt <= "9"):
                return self._read_datetime(start_line, start_col, start_off, start_byte_off)
            raise self._error(
                f"'#{nxt}' is a reserved syntax; see SPEC §12",
                "WEFT_RESERVED_HASH",
            )

        # Numbers (signed or unsigned) OR the sign-only reserved tokens:
        # +inf, -inf. Also plain reserved literals: inf, nan.
        if ch in "+-" or ("0" <= ch <= "9"):
            tok = self._read_number_or_sign_literal(start_line, start_col, start_off, start_byte_off)
            if tok is not None:
                return tok
            # fallthrough to symbol scan (e.g. a lone + or - as a symbol)

        # Reserved identifiers and symbols
        if _is_symbol_start(ch):
            name = self._read_symbol_name()
            if name in _RESERVED_LITERALS:
                if name == "true":
                    return Token(TK_BOOL, True, start_line, start_col, start_off, start_byte_off)
                if name == "false":
                    return Token(TK_BOOL, False, start_line, start_col, start_off, start_byte_off)
                if name == "nil":
                    return Token(TK_NIL, None, start_line, start_col, start_off, start_byte_off)
                if name == "nan":
                    return Token(TK_NAN, float("nan"), start_line, start_col, start_off, start_byte_off)
                if name == "inf":
                    return Token(TK_INF, float("inf"), start_line, start_col, start_off, start_byte_off)
            # Reserved sigil checks (SPEC §12)
            if name.startswith("$"):
                raise WeftLexError(
                    f"symbols beginning with '$' are reserved: {name!r}",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_RESERVED_DOLLAR",
                )
            # Reject sign+digit prefixes that would ambiguate with signed numbers.
            if not is_valid_symbol_name(name):
                raise WeftLexError(
                    f"invalid symbol name {name!r}: starts with sign+digit, "
                    "which ambiguates with a signed number literal",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_SYMBOL_AMBIGUOUS",
                )
            return Token(TK_SYMBOL, name, start_line, start_col, start_off, start_byte_off)

        # Catch-all
        raise WeftLexError(
            f"unexpected character U+{ord(ch):04X} ({ch!r})",
            line=start_line, column=start_col, offset=start_off,
            code="WEFT_SYNTAX_CHAR",
        )

    # ---- comments ----

    def _skip_line_comment(self) -> None:
        # Starts at ';' which we consume. Reads to CR or LF (exclusive).
        # CR, LF, and CRLF all terminate a line comment (SPEC §3, §4.2).
        self._advance()  # consume ';'
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "\n" or ch == "\r":
                return
            if ch in _COMMENT_FORBIDDEN:
                raise self._error(
                    f"character U+{ord(ch):04X} not permitted in comment",
                    "WEFT_SYNTAX_COMMENT_CTRL",
                )
            self._advance()

    def _skip_block_comment(self) -> None:
        # Starts at '#|'; consume it.
        self._advance(2)
        depth = 1
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            nxt = self._peek(1)
            if ch == "#" and nxt == "|":
                depth += 1
                self._advance(2)
                continue
            if ch == "|" and nxt == "#":
                depth -= 1
                self._advance(2)
                if depth == 0:
                    return
                continue
            if ch in _COMMENT_FORBIDDEN:
                raise self._error(
                    f"control character U+{ord(ch):04X} not permitted in comment",
                    "WEFT_SYNTAX_COMMENT_CTRL",
                )
            self._advance()
        raise self._error("unterminated block comment", "WEFT_SYNTAX_COMMENT_UNTERMINATED")

    # ---- symbols ----

    def _read_symbol_name(self) -> str:
        start = self.pos
        if self.pos >= len(self.text):
            return ""
        first = self.text[self.pos]
        if not _is_symbol_start(first):
            return ""
        self._advance()
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if _is_symbol_cont(c):
                self._advance()
            else:
                break
        name = self.text[start:self.pos]
        return name

    # ---- strings ----

    def _read_dquote_string(self) -> str:
        self._advance()  # consume opening "
        start_byte = self.pos
        out: list[str] = []
        running_bytes = 0
        max_bytes = self.limits.max_string_bytes
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == '"':
                self._advance()
                s = "".join(out)
                self._check_string_limit(s)
                return s
            if ch in _FORBIDDEN_CTRL:
                raise self._error(
                    f"control character U+{ord(ch):04X} not permitted "
                    "in standard string; use escape",
                    "WEFT_SYNTAX_STR_CTRL",
                )
            if ch == "\n" or ch == "\t":
                raise self._error(
                    "literal tab or newline not permitted in standard string; "
                    'use "..." with escape sequences or """...""" for multiline',
                    "WEFT_SYNTAX_STR_CTRL",
                )
            if ch == "\r":
                raise self._error(
                    "literal CR not permitted in standard string",
                    "WEFT_SYNTAX_STR_CTRL",
                )
            if ch == "\\":
                piece = self._read_escape(allow_newline=False)
                out.append(piece)
                running_bytes += len(piece.encode("utf-8"))
            else:
                out.append(ch)
                cp = ord(ch)
                running_bytes += 1 if cp < 0x80 else 2 if cp < 0x800 else 4 if cp > 0xFFFF else 3
                self._advance()
            # Incremental check so a 100 MB string with ``max_string_bytes=1``
            # aborts after one char, not after accumulating the whole string.
            if running_bytes > max_bytes:
                raise self._limit_error(
                    f"string literal exceeds max_string_bytes={max_bytes}",
                    "WEFT_LIMIT_STRING",
                )
        raise self._error("unterminated string literal", "WEFT_SYNTAX_STR_UNTERMINATED")

    def _read_triple_string(self) -> str:
        # already at first of three '"'; consume all three
        self._advance(3)
        out: list[str] = []
        running_bytes = 0
        max_bytes = self.limits.max_string_bytes
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            # end on three consecutive quotes
            if ch == '"' and self._peek(1) == '"' and self._peek(2) == '"':
                self._advance(3)
                s = "".join(out)
                self._check_string_limit(s)
                return s
            if ch in _FORBIDDEN_CTRL:
                raise self._error(
                    f"control character U+{ord(ch):04X} not permitted "
                    "in triple-quoted string",
                    "WEFT_SYNTAX_STR_CTRL",
                )
            if ch == "\\":
                piece = self._read_escape(allow_newline=True)
                out.append(piece)
                running_bytes += len(piece.encode("utf-8"))
            else:
                out.append(ch)
                cp = ord(ch)
                running_bytes += 1 if cp < 0x80 else 2 if cp < 0x800 else 4 if cp > 0xFFFF else 3
                self._advance()
            if running_bytes > max_bytes:
                raise self._limit_error(
                    f"string literal exceeds max_string_bytes={max_bytes}",
                    "WEFT_LIMIT_STRING",
                )
        raise self._error("unterminated triple-quoted string", "WEFT_SYNTAX_STR_UNTERMINATED")

    def _read_raw_string(self) -> str:
        # already consumed 'r'; now at opening "
        self._advance()  # consume "
        out: list[str] = []
        running_bytes = 0
        max_bytes = self.limits.max_string_bytes
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == '"':
                self._advance()
                s = "".join(out)
                self._check_string_limit(s)
                return s
            if ch in _FORBIDDEN_CTRL:
                raise self._error(
                    f"control character U+{ord(ch):04X} not permitted "
                    "in raw string",
                    "WEFT_SYNTAX_STR_CTRL",
                )
            # Raw strings permit literal newline but no bare "; no escapes
            out.append(ch)
            cp = ord(ch)
            running_bytes += 1 if cp < 0x80 else 2 if cp < 0x800 else 4 if cp > 0xFFFF else 3
            self._advance()
            if running_bytes > max_bytes:
                raise self._limit_error(
                    f"raw string literal exceeds max_string_bytes={max_bytes}",
                    "WEFT_LIMIT_STRING",
                )
        raise self._error("unterminated raw string", "WEFT_SYNTAX_STR_UNTERMINATED")

    def _read_escape(self, *, allow_newline: bool) -> str:
        # Current char is '\'. Consume it; then read the escape body.
        self._advance()  # consume backslash
        if self.pos >= len(self.text):
            raise self._error("truncated escape sequence", "WEFT_SYNTAX_ESCAPE")
        c = self.text[self.pos]
        if c == "n":
            self._advance(); return "\n"
        if c == "t":
            self._advance(); return "\t"
        if c == "r":
            self._advance(); return "\r"
        if c == "\\":
            self._advance(); return "\\"
        if c == '"':
            self._advance(); return '"'
        if c == "0":
            self._advance(); return "\x00"
        if c == "x":
            self._advance()
            h1 = self._read_hex_digits(2)
            cp = int(h1, 16)
            if cp > 0x7F:
                raise self._error(
                    f"\\x{h1} escapes byte values 00-7F only; "
                    "use \\u for higher code points",
                    "WEFT_SYNTAX_ESCAPE_XRANGE",
                )
            return chr(cp)
        if c == "u":
            self._advance()
            if self.pos < len(self.text) and self.text[self.pos] == "{":
                self._advance()
                hexstr = self._read_hex_until("}", min_digits=1, max_digits=6)
                if self.pos >= len(self.text) or self.text[self.pos] != "}":
                    raise self._error("missing } in \\u{...} escape", "WEFT_SYNTAX_ESCAPE")
                self._advance()
                cp = int(hexstr, 16)
            else:
                hexstr = self._read_hex_digits(4)
                cp = int(hexstr, 16)
            if cp > 0x10FFFF:
                raise self._error(
                    f"\\u escape U+{cp:X} exceeds Unicode max U+10FFFF",
                    "WEFT_SYNTAX_ESCAPE_MAX",
                )
            if 0xD800 <= cp <= 0xDFFF:
                raise self._error(
                    f"\\u escape U+{cp:04X} is a surrogate half",
                    "WEFT_SYNTAX_ESCAPE_SURROGATE",
                )
            return chr(cp)
        raise self._error(
            f"unknown escape sequence \\{c}",
            "WEFT_SYNTAX_ESCAPE_UNKNOWN",
        )

    def _read_hex_digits(self, n: int) -> str:
        start = self.pos
        for _ in range(n):
            if self.pos >= len(self.text):
                raise self._error("truncated hex escape", "WEFT_SYNTAX_ESCAPE")
            ch = self.text[self.pos]
            if ch not in "0123456789abcdefABCDEF":
                raise self._error(f"invalid hex digit {ch!r} in escape", "WEFT_SYNTAX_ESCAPE")
            self._advance()
        return self.text[start:self.pos]

    def _read_hex_until(self, terminator: str, *, min_digits: int, max_digits: int) -> str:
        start = self.pos
        count = 0
        while self.pos < len(self.text) and self.text[self.pos] != terminator:
            ch = self.text[self.pos]
            if ch not in "0123456789abcdefABCDEF":
                raise self._error(f"invalid hex digit {ch!r} in \\u{{...}} escape", "WEFT_SYNTAX_ESCAPE")
            self._advance()
            count += 1
            if count > max_digits:
                raise self._error(
                    f"\\u{{...}} escape has more than {max_digits} hex digits",
                    "WEFT_SYNTAX_ESCAPE",
                )
        if count < min_digits:
            raise self._error(
                f"\\u{{...}} escape has fewer than {min_digits} hex digit(s)",
                "WEFT_SYNTAX_ESCAPE",
            )
        return self.text[start:self.pos]

    def _check_string_limit(self, s: str) -> None:
        byte_len = len(s.encode("utf-8"))
        if byte_len > self.limits.max_string_bytes:
            raise self._limit_error(
                f"string length {byte_len} bytes exceeds max_string_bytes="
                f"{self.limits.max_string_bytes}",
                "WEFT_LIMIT_STRING",
            )

    # ---- binary ----

    def _read_binary(self) -> Binary:
        # Already past 'b'; now at opening "
        self._advance()
        # Read until closing "
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] != '"':
            ch = self.text[self.pos]
            if ch in _FORBIDDEN_CTRL or ch in "\n\t\r ":
                raise self._error(
                    "whitespace or control chars not permitted inside binary literal",
                    "WEFT_SYNTAX_BINARY",
                )
            self._advance()
        if self.pos >= len(self.text):
            raise self._error("unterminated binary literal", "WEFT_SYNTAX_BINARY")
        body = self.text[start:self.pos]
        self._advance()  # consume closing "

        if body.startswith("base64:"):
            payload = body[len("base64:"):]
            if payload == "":
                return Binary(data=b"", encoding="base64")
            # SPEC §5.8: canonical check — non-canonical trailing bits
            # (such as 'AB==' which decodes the same bytes as 'AA==') MUST
            # be rejected so a single byte sequence has exactly one base64
            # spelling.
            from .validators import is_canonical_base64
            if not is_canonical_base64(payload):
                raise self._error(
                    f"non-canonical base64 in binary literal "
                    f"(trailing bits must be zero): {payload!r}",
                    "WEFT_SYNTAX_BINARY_BASE64",
                )
            try:
                data = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as e:
                raise self._error(
                    f"invalid base64 in binary literal: {e}",
                    "WEFT_SYNTAX_BINARY_BASE64",
                ) from None
            return Binary(data=data, encoding="base64")

        if body.startswith("hex:"):
            payload = body[len("hex:"):]
            if len(payload) % 2 != 0:
                raise self._error(
                    "hex binary literal must have an even number of digits",
                    "WEFT_SYNTAX_BINARY_HEX",
                )
            try:
                data = bytes.fromhex(payload)
            except ValueError as e:
                raise self._error(
                    f"invalid hex in binary literal: {e}",
                    "WEFT_SYNTAX_BINARY_HEX",
                ) from None
            return Binary(data=data, encoding="hex")

        raise self._error(
            "binary literal must start with 'base64:' or 'hex:'",
            "WEFT_SYNTAX_BINARY",
        )

    # ---- datetime ----

    def _read_datetime(self, start_line: int, start_col: int, start_off: int, start_byte_off: int) -> Token:
        # Already at '#'; consume it
        self._advance()
        # Read up to the next common token boundary: whitespace, closing
        # bracket/paren/brace, "::", ";" (line comment), "#|" (block comment),
        # or EOF.
        start = self.pos
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch in _WS_SET or ch in ")]}":
                break
            # SPEC §4.3: `"` is a token boundary — a string literal may
            # directly follow a datetime ([#2026-11-02"a"]).
            if ch == '"':
                break
            if ch == ":" and self.pos + 1 < len(self.text) and self.text[self.pos + 1] == ":":
                break
            if ch == ";":
                break
            if ch == "#" and self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "|":
                break
            # Opening brackets after a datetime also terminate (e.g. #date[list]).
            if ch in "([{":
                break
            self._advance()
        body = self.text[start:self.pos]
        value = _parse_datetime(body, self._error)
        return Token(TK_DATETIME, value, start_line, start_col, start_off, start_byte_off)

    # ---- numbers ----

    def _read_number_or_sign_literal(
        self, start_line: int, start_col: int, start_off: int, start_byte_off: int
    ) -> Optional[Token]:
        """Parse a number or a signed reserved literal (-inf, +inf).

        Returns None if the current character sequence cannot be interpreted
        as a number (e.g. a bare '+' or '-' that becomes a symbol).
        """
        start = self.pos
        p = self.pos
        text = self.text
        n = len(text)

        sign = ""
        if text[p] in "+-":
            sign = text[p]
            p += 1

        # Check for signed inf
        if p < n and text[p:p+3] == "inf" and (p + 3 == n or not _is_symbol_cont(text[p+3])):
            # Consume whole thing
            for _ in range(p + 3 - self.pos):
                self._advance()
            if sign == "-":
                return Token(TK_NEG_INF, float("-inf"), start_line, start_col, start_off, start_byte_off)
            return Token(TK_INF, float("inf"), start_line, start_col, start_off, start_byte_off)

        # Need at least one digit after any sign
        if p >= n or not ("0" <= text[p] <= "9"):
            # Not a number — it was a lone + or - sign to be parsed as a symbol.
            return None

        # Scan digit prefix and detect base
        if text[p] == "0" and p + 1 < n and text[p+1] in "xXoObB":
            base_char = text[p+1].lower()
            p += 2
            digit_start = p
            allowed = {
                "x": "0123456789abcdefABCDEF_",
                "o": "01234567_",
                "b": "01_",
            }[base_char]
            while p < n and text[p] in allowed:
                p += 1
            # Validate: underscores not leading, trailing, or doubled
            digits_text = text[digit_start:p]
            _validate_underscores(digits_text, self, start_line, start_col, start_off)
            cleaned = digits_text.replace("_", "")
            if not cleaned:
                raise WeftLexError(
                    f"base-{base_char} integer has no digits",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_INT",
                )
            base = {"x": 16, "o": 8, "b": 2}[base_char]
            # Check against decimal-equivalent digit count so a 4,000-digit
            # ``0xf…f`` cannot sneak past max_int_digits only to blow it up
            # when the formatter emits the same value in base 10.
            self._check_int_digits(cleaned, start_line, start_col, start_off, base=base)
            # Non-decimal bases are not covered by Python 3.11+'s int-to-str
            # cap, so ``int(cleaned, base)`` works without touching the
            # host global here.
            try:
                val = int(cleaned, base)
            except ValueError:
                raise WeftLexError(
                    f"invalid base-{base_char} integer",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_INT",
                ) from None
            if sign == "-":
                val = -val
            # BigInt suffix?
            if p < n and text[p] == "n":
                p += 1
                for _ in range(p - self.pos):
                    self._advance()
                # No symbol-continuation after suffix
                if self.pos < n and _is_symbol_cont(text[self.pos]):
                    raise self._error(
                        "unexpected character after 'n' suffix",
                        "WEFT_SYNTAX_INT",
                    )
                return Token(TK_BIGINT, BigInt(val), start_line, start_col, start_off, start_byte_off)
            for _ in range(p - self.pos):
                self._advance()
            if self.pos < n and _is_symbol_cont(text[self.pos]):
                # Reserved for future numeric suffixes (SPEC §12)
                raise self._error(
                    "unexpected character after integer; "
                    "only 'n' (BigInt) and 'm' (Decimal) suffixes are supported",
                    "WEFT_RESERVED_NUMBER_SUFFIX",
                )
            return Token(TK_INT, val, start_line, start_col, start_off, start_byte_off)

        # Decimal number (int, float, decimal) with optional n/m suffix
        int_start = p
        while p < n and (("0" <= text[p] <= "9") or text[p] == "_"):
            p += 1
        int_text = text[int_start:p]
        _validate_underscores(int_text, self, start_line, start_col, start_off)
        int_cleaned = int_text.replace("_", "")
        if not int_cleaned:
            # Should not happen since we required a leading digit above
            raise WeftLexError(
                "number has no digits",
                line=start_line, column=start_col, offset=start_off,
                code="WEFT_SYNTAX_INT",
            )

        is_float = False
        frac_cleaned = ""
        if p < n and text[p] == ".":
            # Must be followed by a digit, not another dot
            if p + 1 >= n or not ("0" <= text[p+1] <= "9"):
                raise WeftLexError(
                    "float literal must have digits after '.' (e.g. 5.0 not 5.)",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_FLOAT_DOT",
                )
            is_float = True
            p += 1
            frac_start = p
            while p < n and (("0" <= text[p] <= "9") or text[p] == "_"):
                p += 1
            frac_text = text[frac_start:p]
            _validate_underscores(frac_text, self, start_line, start_col, start_off)
            frac_cleaned = frac_text.replace("_", "")

        exp_text = ""
        exp_sign = ""
        if p < n and text[p] in "eE":
            is_float = True
            p += 1
            if p < n and text[p] in "+-":
                exp_sign = text[p]
                p += 1
            exp_start = p
            while p < n and (("0" <= text[p] <= "9") or text[p] == "_"):
                p += 1
            exp_raw = text[exp_start:p]
            _validate_underscores(exp_raw, self, start_line, start_col, start_off)
            exp_text = exp_raw.replace("_", "")
            if not exp_text:
                raise WeftLexError(
                    "exponent has no digits",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_FLOAT_EXP",
                )
            # Exponent digits also count against max_int_digits
            self._check_int_digits(exp_text, start_line, start_col, start_off)

        # Decide suffix
        suffix = ""
        if p < n and text[p] in "nm":
            suffix = text[p]
            p += 1
            # After suffix, no symbol-cont permitted
            if p < n and _is_symbol_cont(text[p]):
                # Ambiguous: sign+digits+suffix followed by more sym-cont.
                # Fall back to symbol lexing when a sign was present.
                if sign:
                    return None
                for _ in range(p - self.pos):
                    self._advance()
                raise self._error(
                    f"unexpected character after '{suffix}' suffix",
                    "WEFT_SYNTAX_NUMBER_SUFFIX",
                )
            # Validation: 'n' only on pure integers
            if suffix == "n" and is_float:
                for _ in range(p - self.pos):
                    self._advance()
                raise WeftLexError(
                    "'n' (BigInt) suffix is only valid on integer literals",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_NUMBER_SUFFIX",
                )
        else:
            if p < n and _is_symbol_cont(text[p]):
                # Ambiguous case: a sign-prefixed run that extends past the
                # numeric part. Example: "+0<" or "-abc3". Fall back to
                # symbol lexing so the whole thing is a valid identifier.
                # (Unsigned "42x" is still a syntax error because it starts
                # as a bare number literal.)
                if sign and not is_float and not suffix:
                    return None
                for _ in range(p - self.pos):
                    self._advance()
                # Reject unknown numeric suffix (SPEC §12 reserves them)
                raise self._error(
                    "unexpected character after number; "
                    "only 'n' (BigInt) and 'm' (Decimal) suffixes are supported",
                    "WEFT_RESERVED_NUMBER_SUFFIX",
                )

        # Length check
        total_digits = int_cleaned + frac_cleaned
        self._check_int_digits(total_digits, start_line, start_col, start_off)

        # Build value and advance
        for _ in range(p - self.pos):
            self._advance()

        if suffix == "n":
            val_i = _dec_str_to_int(int_cleaned)
            if sign == "-":
                val_i = -val_i
            return Token(TK_BIGINT, BigInt(val_i), start_line, start_col, start_off, start_byte_off)

        if suffix == "m":
            # Decimal literal — preserve the original textual form.
            text_val = (sign + int_cleaned +
                        ("." + frac_cleaned if frac_cleaned else "") +
                        ("e" + exp_sign + exp_text if exp_text else ""))
            try:
                from .types import Decimal as _WeftDecimal
                return Token(
                    TK_DECIMAL, _WeftDecimal(text_val),
                    start_line, start_col, start_off, start_byte_off,
                )
            except Exception as e:
                raise WeftLexError(
                    f"invalid decimal literal: {e}",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_DECIMAL",
                ) from None

        if is_float:
            text_val = (sign + int_cleaned +
                        ("." + frac_cleaned if frac_cleaned else "") +
                        ("e" + exp_sign + exp_text if exp_text else ""))
            try:
                return Token(TK_FLOAT, float(text_val),
                             start_line, start_col, start_off, start_byte_off)
            except ValueError as e:
                raise WeftLexError(
                    f"invalid float literal: {e}",
                    line=start_line, column=start_col, offset=start_off,
                    code="WEFT_SYNTAX_FLOAT",
                ) from None

        # Plain integer
        val_i = _dec_str_to_int(int_cleaned)
        if sign == "-":
            val_i = -val_i
        return Token(TK_INT, val_i, start_line, start_col, start_off, start_byte_off)

    _BASE_LOG10 = {
        2:  0.30102999566398114,  # log10(2)
        8:  0.9030899869919435,   # log10(8)
        10: 1.0,
        16: 1.2041199826559248,   # log10(16)
    }

    def _check_int_digits(self, cleaned: str, line: int, col: int, off: int, base: int = 10) -> None:
        # SPEC §9.5: ``max_int_digits`` is a decimal-digit budget. For
        # non-decimal bases we must compare against the decimal-equivalent
        # length — ceil(len * log10(base)) is a tight upper bound on
        # str(abs(val)) and avoids any dependency on Python's own
        # ``int_max_str_digits`` (which the formatter would otherwise hit).
        if base == 10:
            dec_len = len(cleaned)
        else:
            import math as _m
            dec_len = int(_m.ceil(len(cleaned) * self._BASE_LOG10[base]))
        if dec_len > self.limits.max_int_digits:
            raise WeftLimitError(
                f"integer has {dec_len} decimal digits "
                f"({len(cleaned)} base-{base} digits), exceeds "
                f"max_int_digits={self.limits.max_int_digits}",
                line=line, column=col, offset=off,
                code="WEFT_LIMIT_INT",
            )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _is_symbol_start(ch: str) -> bool:
    if ch in _SYM_START_ASCII:
        return True
    # Unicode letter above ASCII — but only Unicode *letters*; never digits
    # (SPEC §5.10 restricts digit characters to ASCII to avoid formatter
    # round-trip surprises with ``１`` / ``①`` / ``¼``).
    if ord(ch) > 0x7F and ch.isalpha():
        return True
    return False


def _is_symbol_cont(ch: str) -> bool:
    if ch in _SYM_CONT_ASCII:
        return True
    # Above ASCII: letters only. Unicode digits (``Nd`` / ``No``) and the
    # fraction / Roman-numeral blocks are *not* symbol-cont — the formatter
    # cannot round-trip them, so the lexer must refuse them too.
    if ord(ch) > 0x7F and ch.isalpha():
        return True
    return False


def _is_rejected_whitespace(ch: str) -> bool:
    """Check for Unicode whitespace that is NOT accepted at the top level."""
    if ch in _WS_SET:
        return False
    cp = ord(ch)
    # U+000B VT, U+000C FF
    if cp in (0x0B, 0x0C):
        return True
    # U+00A0 NBSP
    if cp == 0xA0:
        return True
    # U+2000-U+200A, U+2028, U+2029, U+205F, U+3000
    if 0x2000 <= cp <= 0x200A:
        return True
    if cp in (0x2028, 0x2029, 0x205F, 0x3000):
        return True
    return False


def _dec_str_to_int(digits: str) -> int:
    """Convert a decimal-digit string to ``int`` without triggering Python
    3.11+'s global ``sys.int_max_str_digits`` cap.

    ``int(s)`` is the obvious one-liner, but it raises ``ValueError`` on
    strings longer than the host cap (4300 by default). We've already
    validated the length against Weft's own ``max_int_digits`` by the
    time we reach here, so we parse by nine-digit chunks using
    multiplication — ``int(string, 16)`` and friends are not capped,
    nor is ``*`` / ``+`` on ints, so this path is free of the global.
    """
    if not digits:
        return 0
    acc = 0
    i = 0
    n = len(digits)
    while i < n:
        chunk = digits[i:i + 9]
        acc = acc * (10 ** len(chunk)) + int(chunk)
        i += 9
    return acc


def _pos_from_offset(text: str, offset: int) -> Tuple[int, int]:
    line = 1
    col = 1
    n = min(offset, len(text))
    for i in range(n):
        ch = text[i]
        if ch == "\n":
            line += 1
            col = 1
        elif ch == "\r":
            # CRLF: defer to the \n. Lone CR: counts as a line terminator.
            if i + 1 < n and text[i + 1] == "\n":
                pass
            else:
                line += 1
                col = 1
        else:
            col += 1
    return line, col


def _byte_offset_at(text: str, char_offset: int) -> int:
    """UTF-8 byte offset of the character index. Called only on the pre-
    tokenizer encoding checks, so the O(n) walk is acceptable."""
    if char_offset <= 0:
        return 0
    try:
        return len(text[:char_offset].encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        # A surrogate half above the inspected prefix would land here;
        # fall back to the character offset.
        return char_offset


def _validate_underscores(digits_text: str, lexer: Lexer, line: int, col: int, off: int) -> None:
    if "__" in digits_text:
        raise WeftLexError(
            "consecutive underscores not permitted in numeric literal",
            line=line, column=col, offset=off,
            code="WEFT_SYNTAX_NUMBER_UNDERSCORE",
        )
    if digits_text.startswith("_") or digits_text.endswith("_"):
        raise WeftLexError(
            "underscore cannot be first or last digit",
            line=line, column=col, offset=off,
            code="WEFT_SYNTAX_NUMBER_UNDERSCORE",
        )


_DATETIME_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})?)?$"
)


def _parse_datetime(body: str, error_factory):
    import calendar
    m = _DATETIME_RE.match(body)
    if not m:
        raise error_factory(
            f"invalid datetime literal: {body!r} (expected RFC 3339)",
            "WEFT_SYNTAX_DATETIME",
        )
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 1 or year > 9999:
        raise error_factory(
            f"datetime year {year} is out of range 0001-9999",
            "WEFT_SYNTAX_DATETIME_YEAR",
        )
    if month < 1 or month > 12:
        raise error_factory(f"invalid month {month}", "WEFT_SYNTAX_DATETIME")
    max_day = calendar.monthrange(year, month)[1]
    if day < 1 or day > max_day:
        raise error_factory(f"invalid day {day} for month {month}", "WEFT_SYNTAX_DATETIME")
    if m.group(4) is None:
        return WeftDate(year=year, month=month, day=day, has_time=False)
    hour, minute, second = int(m.group(4)), int(m.group(5)), int(m.group(6))
    if hour > 23 or minute > 59 or second > 60:
        raise error_factory(f"invalid time {hour}:{minute}:{second}", "WEFT_SYNTAX_DATETIME")
    frac = m.group(7) or ""
    nano = 0
    if frac:
        nano9 = (frac + "000000000")[:9]
        nano = int(nano9)
    zone = m.group(8)
    tz_off: Optional[int]
    if zone is None:
        tz_off = None
    elif zone == "Z":
        tz_off = 0
    else:
        sign = 1 if zone[0] == "+" else -1
        off_h = int(zone[1:3])
        off_m = int(zone[4:6])
        if off_h > 14 or off_m > 59 or (off_h == 14 and off_m > 0):
            raise error_factory(
                f"timezone offset {zone} out of range (|offset| <= 14:00)",
                "WEFT_SYNTAX_DATETIME",
            )
        tz_off = sign * (off_h * 60 + off_m)
    # Leap seconds (:60) only occur at the end of a UTC day — 23:59:60Z —
    # and in local time they appear at the hour / minute that maps back
    # to 23:59 UTC after applying the stated offset. ``08:59:60+09:00``
    # is thus legal (= 23:59:60Z on the previous day in UTC), while
    # ``00:00:60Z`` is not. If the offset is unknown we fall back to the
    # UTC rule.
    if second == 60:
        off = tz_off if tz_off is not None else 0
        total_minutes = hour * 60 + minute - off
        # Normalise into [0, 1440); UTC 23:59 ≡ 1439.
        total_minutes %= 1440
        if total_minutes != 23 * 60 + 59:
            raise error_factory(
                f"leap-second :60 only valid at 23:59:60 UTC "
                f"(got {hour:02d}:{minute:02d}:60 at offset "
                f"{'Z' if tz_off == 0 else zone or 'local'})",
                "WEFT_SYNTAX_DATETIME",
            )
    return WeftDate(year=year, month=month, day=day, has_time=True,
                    hour=hour, minute=minute, second=second, nano=nano,
                    tz_offset_minutes=tz_off)
