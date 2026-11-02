"""Weft error types.

All exceptions raised by the parser derive from ``WeftError``. Specific
subclasses allow callers to distinguish syntax errors from resource-limit
violations without inspecting message text.
"""
from __future__ import annotations
from typing import Optional


class WeftError(Exception):
    """Base class for all Weft-related errors.

    ``offset`` is the UTF-16-ish Python character index into the input
    text (what ``self.pos`` tracks inside the lexer). ``byte_offset`` is
    the UTF-8 byte position of the error, which is the authoritative position for
    diagnostics: it's the location one would use to point into
    an on-disk file. For ASCII input the two coincide; for text with
    non-ASCII characters, ``byte_offset`` is the authoritative one.
    """

    def __init__(
        self,
        message: str,
        *,
        line: Optional[int] = None,
        column: Optional[int] = None,
        offset: Optional[int] = None,
        byte_offset: Optional[int] = None,
        code: Optional[str] = None,
    ):
        self.message = message
        self.line = line
        self.column = column
        self.offset = offset
        self.byte_offset = byte_offset
        self.code = code
        if line is not None and column is not None:
            super().__init__(f"[line {line}:{column}] {message}")
        else:
            super().__init__(message)


class WeftSyntaxError(WeftError):
    """Raised when the input does not match the Weft grammar."""


class WeftLexError(WeftSyntaxError):
    """Raised during tokenization on invalid characters, strings, or numbers."""


class WeftLimitError(WeftError):
    """Raised when a resource limit (depth, size, item count, etc.) is exceeded.

    Attribute ``code`` is one of:
      - ``WEFT_LIMIT_DEPTH``
      - ``WEFT_LIMIT_SIZE``
      - ``WEFT_LIMIT_ITEMS``
      - ``WEFT_LIMIT_STRING``
      - ``WEFT_LIMIT_INT``
      - ``WEFT_LIMIT_TOTAL``
    """


class WeftEncodingError(WeftError):
    """Raised when the input is not valid UTF-8 or contains a BOM."""


class WeftTypeError(WeftError):
    """Raised when a Weft operation receives a value of the wrong type."""


class WeftSchemaError(WeftError):
    """Raised when a document fails schema validation."""
