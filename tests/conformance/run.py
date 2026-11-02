#!/usr/bin/env python3
"""Weft conformance runner.

Runs the language-neutral corpus against one or more implementations and
reports failures. Exit code 0 iff everything passes.

Usage:
  python3 tests/conformance/run.py [--impl py,ts,rs,go] [--build]

Case kinds:
  corpus/valid.json        parse → strict canonical must equal `canonical`;
                           also checked: idempotence (canon2) and lossless
                           JSON round-trip identity (jroundtrip) reproduce
                           the same bytes.
  corpus/invalid.json      parse must fail; `code` (if present) must match.
  corpus/json_valid.json   lossless JSON decode → strict canonical.
  corpus/json_invalid.json lossless JSON decode must fail; `code` optional.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def build_ts(force: bool = False) -> str:
    out = os.path.join(HERE, "drivers", "tsout")
    drv = os.path.join(out, "tests", "conformance", "drivers", "driver.js")
    if force or not os.path.exists(drv):
        globals_d = os.path.join(HERE, "drivers", "globals.d.ts")
        with open(globals_d, "w") as fp:
            fp.write("declare const Buffer: any;\n"
                     "declare function atob(s: string): string;\n"
                     "declare function btoa(s: string): string;\n"
                     "declare const process: any;\n"
                     "declare function require(m: string): any;\n")
        subprocess.run(
            ["tsc", "--ignoreConfig", "--target", "ES2022", "--module", "commonjs",
             "--skipLibCheck", "--rootDir", ROOT, "--outDir", out,
             globals_d, os.path.join(HERE, "drivers", "driver.ts")],
            check=True, cwd=ROOT)
    return drv


def impl_commands(wanted, build):
    cmds = {}
    if "py" in wanted:
        cmds["py"] = [sys.executable, os.path.join(HERE, "drivers", "driver.py")]
    if "ts" in wanted:
        cmds["ts"] = ["node", build_ts(force=build)]
    if "go" in wanted:
        bin_go = os.path.join(HERE, "drivers", "bin-go")
        if build or not os.path.exists(bin_go):
            subprocess.run(["go", "build", "-o", bin_go, "./cmd"],
                           cwd=os.path.join(HERE, "drivers", "go-driver"), check=True)
        cmds["go"] = [bin_go]
    if "rs" in wanted:
        bin_rs = os.path.join(HERE, "drivers", "rust-driver", "target", "debug", "weftdrv")
        if build or not os.path.exists(bin_rs):
            subprocess.run(["cargo", "build"],
                           cwd=os.path.join(HERE, "drivers", "rust-driver"), check=True)
        cmds["rs"] = [bin_rs]
    return cmds


def run_driver(cmd, mode, data: str, timeout=60) -> str:
    p = subprocess.run(cmd + [mode], input=data.encode("utf-8"),
                       capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    if not out and p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return f"FATAL({p.returncode}) " + (tail[-1][:160] if tail else "")
    return out


def load(name):
    path = os.path.join(HERE, "corpus", name)
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", default="py,ts,rs,go")
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()
    cmds = impl_commands(args.impl.split(","), args.build)

    failures = []
    counts = {i: 0 for i in cmds}

    def check(impl, case_id, mode, data, expect_ok, expected=None, code=None):
        out = run_driver(cmds[impl], mode, data)
        counts[impl] += 1
        if expect_ok:
            if not out.startswith("OK\n"):
                failures.append((impl, case_id, mode, f"expected OK, got {out.strip()[:120]!r}"))
            elif expected is not None and out[3:] != expected:
                failures.append((impl, case_id, mode,
                                 f"output mismatch: got {out[3:]!r} want {expected!r}"))
        else:
            if not out.startswith("ERR"):
                failures.append((impl, case_id, mode, f"expected error, got {out.strip()[:120]!r}"))
            elif code is not None and out.strip() != "ERR " + code:
                failures.append((impl, case_id, mode,
                                 f"error code mismatch: got {out.strip()!r} want 'ERR {code}'"))

    for case in load("valid.json"):
        for impl in cmds:
            check(impl, case["id"], "canon", case["input"], True, case["canonical"])
            check(impl, case["id"], "canon2", case["input"], True, case["canonical"])
            check(impl, case["id"], "jroundtrip", case["input"], True, case["canonical"])

    for case in load("invalid.json"):
        for impl in cmds:
            check(impl, case["id"], "canon", case["input"], False, code=case.get("code"))

    for case in load("json_valid.json"):
        for impl in cmds:
            check(impl, case["id"], "fromlossless", case["json"], True, case["canonical"])

    for case in load("json_invalid.json"):
        for impl in cmds:
            check(impl, case["id"], "fromlossless", case["json"], False, code=case.get("code"))

    for impl, n in counts.items():
        print(f"{impl}: {n} checks")
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for impl, cid, mode, msg in failures[:60]:
            print(f"  [{impl}] {cid} ({mode}): {msg}")
        sys.exit(1)
    print("\nALL CONFORMANCE CHECKS PASSED")


if __name__ == "__main__":
    main()
