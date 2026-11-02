"""Weft: an AI-first structured data format.

Public API:
  parse(text, *, limits=DEFAULT_LIMITS)          -> single value
  parse_stream(text, *, limits=DEFAULT_LIMITS)   -> list of values
  format_value(tree, options=None)               -> text
  to_json(tree)                                  -> JSON-serializable
  from_json(value)                               -> Weft tree
  from_json_lossless(value)                      -> Weft tree with Symbol/Keyword/Binary/Sexpr/Typed

Value types:
  Symbol, Keyword, BigInt, Binary, Sexpr, Typed, Decimal, WeftDate

Errors:
  WeftError (base), WeftSyntaxError, WeftLexError, WeftLimitError,
  WeftEncodingError, WeftTypeError, WeftSchemaError

Limits:
  Limits, DEFAULT_LIMITS
"""
from .errors import (
    WeftError, WeftSyntaxError, WeftLexError, WeftLimitError,
    WeftEncodingError, WeftTypeError, WeftSchemaError,
)
from .formatter import FormatOptions, format_inline, format_value
from .json_interop import (
    dumps_json, from_json, from_json_lossless, loads_json,
    to_json, to_json_lossless,
)
from .limits import DEFAULT_LIMITS, Limits
from .parser import parse, parse_stream
from .types import (
    BigInt, Binary, Decimal, Keyword, Sexpr, Symbol, Typed, WeftDate, equals,
)

__version__ = "1.0.1"
__spec_version__ = "1.0"

__all__ = [
    # top-level API
    "parse",
    "parse_stream",
    "format_value",
    "format_inline",
    "FormatOptions",
    "to_json",
    "to_json_lossless",
    "from_json",
    "from_json_lossless",
    "dumps_json",
    "loads_json",
    # types
    "Symbol",
    "Keyword",
    "BigInt",
    "Binary",
    "Sexpr",
    "Typed",
    "Decimal",
    "WeftDate",
    "equals",
    # errors
    "WeftError",
    "WeftSyntaxError",
    "WeftLexError",
    "WeftLimitError",
    "WeftEncodingError",
    "WeftTypeError",
    "WeftSchemaError",
    # limits
    "Limits",
    "DEFAULT_LIMITS",
    # metadata
    "__version__",
    "__spec_version__",
]
