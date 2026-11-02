#!/usr/bin/env python3
"""Python implementation self-test — regression checks for every bug
class fixed in 1.0.1. Exits non-zero on the first failure."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import weft
from weft import FormatOptions

STRICT = FormatOptions(strict_canonical=True)
fails = 0


def check(name, cond):
    global fails
    if cond:
        print(f"ok   {name}")
    else:
        fails += 1
        print(f"FAIL {name}")


def canon(text):
    return weft.format_value(weft.parse(text), STRICT).rstrip("\n")


def expect_err(name, fn, code=None):
    try:
        fn()
    except weft.WeftError as e:
        check(name, code is None or e.code == code)
        return
    except RecursionError:
        check(name + " (RecursionError!)", False)
        return
    check(name + " (no error raised)", False)


# --- lossless JSON reader hardening -------------------------------------
expect_err("json dup keys rejected",
           lambda: weft.loads_json('{"$map": {"a": 1, "a": 2}}'),
           "WEFT_SYNTAX_JSON_DUPKEY")
expect_err("json NaN literal rejected",
           lambda: weft.loads_json('NaN'))
expect_err("json huge int digits rejected",
           lambda: weft.loads_json('{"$bigint": "1' + "0" * 10001 + '"}'),
           )
expect_err("json deep nesting early-rejected",
           lambda: weft.loads_json("[" * 5000 + "]" * 5000),
           "WEFT_LIMIT_DEPTH")
expect_err("json lone surrogate in $str rejected",
           lambda: weft.loads_json('{"$str": "\\ud800"}'))
check("json 1 vs 1.0 lexical",
      isinstance(weft.loads_json("1", lossless=False), int)
      and isinstance(weft.loads_json("1.0", lossless=False), float))

# depth-128 Weft doc survives the JSON round trip (nesting = 2x Weft depth)
deep = "[" * 128 + "]" * 128
v = weft.parse(deep)
import json as _json
jt = _json.dumps(weft.to_json_lossless(v), ensure_ascii=False)
check("depth-128 lossless round trip",
      weft.format_value(weft.loads_json(jt), STRICT).rstrip("\n") == canon(deep))

# --- equality (§5.16) ----------------------------------------------------
check("equals: Integer == BigInt", weft.equals(weft.parse("5"), weft.parse("5n")))
check("equals: NaN != NaN", not weft.equals(weft.parse("nan"), weft.parse("nan")))
check("equals: -0.0 != 0.0", not weft.equals(weft.parse("-0.0"), weft.parse("0.0")))
check("equals: decimal +1.5m == 1.5m", weft.equals(weft.parse("+1.5m"), weft.parse("1.5m")))

# --- canonical formatting -----------------------------------------------
check("float shortest round trip", canon("0.3") == "0.3")
check("strict escapes \\u{...}", canon('"\\u{1}"') == '"\\u{1}"')
check("datetime quote boundary", canon('#2024-01-01') == "#2024-01-01")

# --- parser limits -------------------------------------------------------
expect_err("weft deep nesting early-rejected",
           lambda: weft.parse("[" * 100000), "WEFT_LIMIT_DEPTH")
expect_err("weft huge int digits rejected",
           lambda: weft.parse("1" * 20000), "WEFT_LIMIT_INT")

# --- int_max_str_digits immunity ----------------------------------------
from weft.validators import dec_str_to_int
check("chunked int conversion", dec_str_to_int("1" + "0" * 5000) == 10 ** 5000)

print()
if fails:
    print(f"{fails} FAILURES")
    sys.exit(1)
print("ALL PYTHON SELF-TESTS PASSED")
