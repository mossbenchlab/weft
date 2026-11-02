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

# --- 1.0.1-rc2 regressions ------------------------------------------------
# ReDoS: hostile pattern + long input must return quickly (linear engine)
import time as _time
from weft.schema import load_schema, validate
_r = load_schema('(schema R (field x String :pattern "a+a+a+a+a+a+a+a+a+a+$"))')
_t0 = _time.monotonic()
_viol = validate(_r, {"x": "a" * 5000 + "!"}, "R")
check("schema pattern linear-time (<1s) and correct",
      _time.monotonic() - _t0 < 1.0 and len(_viol) == 1)
check("unsupported pattern -> violation, not host engine",
      len(validate(load_schema('(schema R (field x String :pattern "(?=a)b"))'),
                   {"x": "b"}, "R")) == 1)

# dumps/loads symmetry (both default to lossless)
check("dumps/loads round trip for bare string",
      weft.loads_json(weft.dumps_json("abc")) == "abc")
check("dumps/loads keeps Float 1.0",
      isinstance(weft.loads_json(weft.dumps_json(weft.parse("1.0"))), float))

# value-level entry limits
from weft import Limits
expect_err("from_json string limit",
           lambda: weft.from_json("ab", limits=Limits(max_string_bytes=1)),
           "WEFT_LIMIT_STRING")
expect_err("from_json_lossless int digits",
           lambda: weft.from_json_lossless(123, limits=Limits(max_int_digits=1)),
           "WEFT_LIMIT_INT")
expect_err("loads_json lone-surrogate host str",
           lambda: weft.loads_json('"' + chr(0xD800) + '"'))

# tight limits must not break legitimate values via JSON tag overhead
_lim = Limits(max_items_per_coll=1)
_sx = weft.parse("(f 0)", limits=_lim)
check("sexpr round trip under max_items_per_coll=1",
      weft.equals(_sx, weft.loads_json(weft.dumps_json(_sx), limits=_lim)))

# raised max_depth is actually usable (no RecursionError)
_deep = "[" * 500 + "0" + "]" * 500
check("depth-500 parse with max_depth=1024",
      weft.format_value(weft.parse(_deep, limits=Limits(max_depth=1024))) != "")

# raw/triple control rejection; hex strictness
expect_err("raw DEL rejected", lambda: weft.parse('r"a\x7fb"'.replace("\\x7f", "\x7f")))
expect_err("hex payload whitespace rejected",
           lambda: weft.loads_json('{"$bin": {"enc": "hex", "data": "00 00"}}'))

# --- 1.0.1-rc3 regressions --------------------------------------------------
# R4-1/2/3: hostile patterns are diagnostics, never host exceptions
from weft.repat import compile_pattern
from weft.errors import WeftSchemaError
for bad in ["\\u{zz}", "\\u{-1}", "a{" + "9" * 4301 + "}",
            "(" * 300 + "a" + ")" * 300, "\\u{+41}", "\\u{1_0}"]:
    try:
        compile_pattern(bad)
        ok = bad == "a{²}"  # literal-brace semantics is fine
    except WeftSchemaError:
        ok = True
    except Exception:
        ok = False
    check(f"pattern {bad[:12]!r} never leaks host exceptions", ok)
check("hex escape works as range endpoint",
      compile_pattern("[\\x61-z]").fullmatch("b"))
import time as _t
_t0 = _t.monotonic()
check("duplicate-range class matches in linear time",
      compile_pattern("([" + "a" * 5000 + "]|b)*").fullmatch("b" * 50000)
      and _t.monotonic() - _t0 < 2.0)

# R4-4: no process-global recursion-limit mutation, even under
# concurrent parses of different depths
import sys as _sys
import threading as _th
_base_limit = _sys.getrecursionlimit()
_errors: list = []
def _deep_parse():
    try:
        v = weft.parse("[" * 500 + "0" + "]" * 500, limits=Limits(max_depth=1024))
        if weft.format_value(v, STRICT) == "":
            _errors.append("empty")
    except Exception as e:  # noqa: BLE001
        _errors.append(repr(e))
def _shallow_parse():
    try:
        for _ in range(200):
            weft.parse("0", limits=Limits(max_depth=128))
    except Exception as e:  # noqa: BLE001
        _errors.append(repr(e))
_threads = [ _th.Thread(target=_deep_parse) for _ in range(4) ] +            [ _th.Thread(target=_shallow_parse) for _ in range(4) ]
for _x in _threads: _x.start()
for _x in _threads: _x.join()
check("concurrent deep+shallow parses all succeed", not _errors)
check("recursion limit untouched after concurrent parses",
      _sys.getrecursionlimit() == _base_limit)

# R4-5: exact digit-count boundary on decoded integers
check("1<<100000 accepted at its exact digit count (30103)",
      weft.from_json(1 << 100000, limits=Limits(max_int_digits=30103)) == 1 << 100000)
expect_err("1<<100000 rejected at 30102 digits",
           lambda: weft.from_json(1 << 100000, limits=Limits(max_int_digits=30102)),
           "WEFT_LIMIT_INT")

# iterative JSON IO: admitted depth reads and writes on any host
_deepdoc = weft.parse("[" * 1024 + "0" + "]" * 1024, limits=Limits(max_depth=1024))
check("1024-deep dumps/loads cycle (iterative JSON IO)",
      weft.equals(_deepdoc, weft.loads_json(weft.dumps_json(_deepdoc),
                                            limits=Limits(max_depth=1024))))

# --- int_max_str_digits immunity ----------------------------------------
from weft.validators import dec_str_to_int
check("chunked int conversion", dec_str_to_int("1" + "0" * 5000) == 10 ** 5000)

print()
if fails:
    print(f"{fails} FAILURES")
    sys.exit(1)
print("ALL PYTHON SELF-TESTS PASSED")
