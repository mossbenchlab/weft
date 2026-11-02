#!/usr/bin/env python3
"""4-way differential fuzzer.

Generates random Weft documents (structural fuzz) and random float64
values (shortest-round-trip fuzz), runs each through all four reference
implementations via the conformance drivers, and asserts byte-exact
agreement in canonical output and identical accept/reject behavior.

Usage:
    python3 tests/fuzz/diff_fuzz.py --structs 500 --floats 1000 --seed 20261102

Exit status 0 = full agreement on every generated case.
"""
import argparse
import os
import random
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "..", "conformance")
sys.path.insert(0, CONF)
from run import impl_commands  # noqa: E402  (reuses driver discovery/build)

ATOMS = [
    "nil", "true", "false", "0", "-1", "42", "9007199254740993n", "-0.0",
    "1.5", "0.1", "1e308", "-1e-308", "nan", "inf", "-inf", "1.25m", "-0.50m",
    '"hello"', '""', '"\\u{1F600}"', '"line\\nbreak"', ':kw', ':a.b/c',
    "sym", "#2024-06-15", "#2024-06-15T12:00:00Z", "#2024-06-15T12:00:00+09:00",
    'b"base64:aGVsbG8="', 'b"hex:deadbeef"', '"日本語テキスト"', '"emoji 😀 mix"',
]


def gen_value(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.4:
        return rng.choice(ATOMS)
    kind = rng.randrange(4)
    n = rng.randrange(4)
    children = [gen_value(rng, depth - 1) for _ in range(n)]
    if kind == 0:
        return "[" + " ".join(children) + "]"
    if kind == 1:
        pairs = " ".join(f":k{i} {c}" for i, c in enumerate(children))
        return "{" + pairs + "}"
    if kind == 2:
        return "(op " + " ".join(children) + ")"
    return "::T" + (gen_value(rng, depth - 1) if rng.random() < 0.7
                    else "[" + " ".join(children) + "]")


def gen_float(rng: random.Random) -> str:
    while True:
        bits = rng.getrandbits(64)
        (f,) = struct.unpack("<d", struct.pack("<Q", bits))
        if f != f or f in (float("inf"), float("-inf")):
            continue
        return repr(f)


def run_driver(cmd, mode, text):
    try:
        p = subprocess.run(cmd + [mode], input=text.encode("utf-8"),
                           capture_output=True, timeout=30)
        return p.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "CRASH timeout"


def outcome_kind(out: str) -> str:
    if out.startswith("OK\n"):
        return "OK"
    if out.startswith("ERR "):
        return "ERR"
    return "CRASH:" + out[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--structs", type=int, default=500)
    ap.add_argument("--floats", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()

    cmds = impl_commands(["py", "ts", "go", "rs"], args.build)
    rng = random.Random(args.seed)
    cases = []
    for i in range(args.structs):
        cases.append((f"struct-{i}", gen_value(rng, rng.randrange(1, 6))))
    for i in range(args.floats):
        cases.append((f"float-{i}", gen_float(rng)))

    disagreements = 0
    for cid, text in cases:
        for mode in ("canon", "jroundtrip"):
            outs = {name: run_driver(cmd, mode, text) for name, cmd in cmds.items()}
            kinds = {outcome_kind(o) for o in outs.values()}
            ok_outs = {o for o in outs.values() if o.startswith("OK\n")}
            agreed = len(kinds) == 1 and (not ok_outs or len(ok_outs) == 1)
            if not agreed:
                disagreements += 1
                print(f"== {cid} ({mode}): {text[:100]!r}")
                for name, o in sorted(outs.items()):
                    print(f"   {name}: {o[:140]!r}")
                if disagreements >= 20:
                    print("... stopping after 20 disagreements")
                    sys.exit(1)

    total = len(cases) * 2
    if disagreements:
        print(f"{disagreements} DISAGREEMENTS over {total} checks")
        sys.exit(1)
    print(f"FUZZ OK: {len(cases)} cases x 2 modes x 4 impls = {total * 4} runs, full agreement")


if __name__ == "__main__":
    main()
