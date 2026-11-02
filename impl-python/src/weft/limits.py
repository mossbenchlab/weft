"""Resource limits enforced by Weft parsers.

The defaults are the mandatory lower bounds specified in SPEC.md §9.
Applications may raise these limits via explicit configuration up to
the hard maximums. There is no "unlimited" setting; a conforming
parser rejects any attempt to disable a limit.
"""
from __future__ import annotations
from dataclasses import dataclass


# Mandatory defaults (SPEC.md §9)
DEFAULT_MAX_DEPTH = 128
DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_MAX_ITEMS_PER_COLL = 1 << 20  # 1,048,576
DEFAULT_MAX_STRING_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_INT_DIGITS = 10_000  # spec §9 mandatory default; the formatter's
                                   # safe int-to-str helper handles values up
                                   # to this without touching the host's
                                   # ``sys.int_max_str_digits`` global.
DEFAULT_MAX_TOTAL_ITEMS = 10_000_000

# Hard maximums (SPEC.md §9)
HARD_MAX_DEPTH = 1024
HARD_MAX_SIZE_BYTES = 1 << 30  # 1 GiB
HARD_MAX_ITEMS_PER_COLL = 1 << 24
HARD_MAX_STRING_BYTES = 1 << 30
HARD_MAX_INT_DIGITS = 1_000_000  # requires explicit sys.set_int_max_str_digits
HARD_MAX_TOTAL_ITEMS = 100_000_000


@dataclass(frozen=True)
class Limits:
    """Immutable resource-limit configuration for a parser invocation."""

    max_depth: int = DEFAULT_MAX_DEPTH
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES
    max_items_per_coll: int = DEFAULT_MAX_ITEMS_PER_COLL
    max_string_bytes: int = DEFAULT_MAX_STRING_BYTES
    max_int_digits: int = DEFAULT_MAX_INT_DIGITS
    max_total_items: int = DEFAULT_MAX_TOTAL_ITEMS

    def __post_init__(self) -> None:
        # Reject "unlimited" sentinels. Each limit must be a positive int
        # within the hard-max range.
        checks = [
            ("max_depth", self.max_depth, 1, HARD_MAX_DEPTH),
            ("max_size_bytes", self.max_size_bytes, 1, HARD_MAX_SIZE_BYTES),
            ("max_items_per_coll", self.max_items_per_coll, 1, HARD_MAX_ITEMS_PER_COLL),
            ("max_string_bytes", self.max_string_bytes, 1, HARD_MAX_STRING_BYTES),
            ("max_int_digits", self.max_int_digits, 1, HARD_MAX_INT_DIGITS),
            ("max_total_items", self.max_total_items, 1, HARD_MAX_TOTAL_ITEMS),
        ]
        for name, value, lo, hi in checks:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be int, got {type(value).__name__}")
            if value < lo:
                raise ValueError(f"{name}={value} is below minimum {lo}")
            if value > hi:
                raise ValueError(f"{name}={value} is above hard maximum {hi}")


DEFAULT_LIMITS = Limits()
