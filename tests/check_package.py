#!/usr/bin/env python3
"""Package completeness check.

Guards against the release-blocking bug class found in review: a module
referenced by the code but missing from the shipped package, or version
strings drifting apart between the spec and the four implementations.

Exit status 0 = package complete and version-consistent.
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
fails = 0


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"ok   {name}")
    else:
        fails += 1
        print(f"FAIL {name}" + (f" — {detail}" if detail else ""))


# --- 1. every intra-package Python import resolves to a shipped file -----
pkg = os.path.join(ROOT, "impl-python", "src", "weft")
missing = []
for fn in sorted(os.listdir(pkg)):
    if not fn.endswith(".py"):
        continue
    tree = ast.parse(open(os.path.join(pkg, fn), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            mod = node.module.split(".")[0]
            if not os.path.exists(os.path.join(pkg, mod + ".py")):
                missing.append(f"{fn}: from .{node.module} import ...")
check("python intra-package imports all resolve", not missing, "; ".join(missing))

sys.path.insert(0, os.path.join(ROOT, "impl-python", "src"))
try:
    import weft
    check("python package imports cleanly", True)
    py_version = weft.__version__
except Exception as e:  # pragma: no cover
    check("python package imports cleanly", False, repr(e))
    py_version = "?"

# --- 2. required files present -------------------------------------------
REQUIRED = [
    "spec/SPEC.md", "CHANGELOG.md", "README.md", "SECURITY.md", "LICENSE",
    "impl-python/src/weft/__init__.py", "impl-python/src/weft/validators.py",
    "impl-python/tests/selftest.py",
    "impl-typescript/src/index.ts", "impl-typescript/package.json",
    "impl-typescript/tsconfig.json", "impl-typescript/tests/selftest.js",
    "impl-rust/Cargo.toml", "impl-rust/src/lib.rs", "impl-rust/src/bin/selftest.rs",
    "impl-go/go.mod", "impl-go/weft.go", "impl-go/selftest/main.go",
    "tests/conformance/run.py", "tests/conformance/gen_corpus.py",
    "tests/conformance/README.md",
    "tests/conformance/corpus/valid.json", "tests/conformance/corpus/invalid.json",
    "tests/conformance/corpus/json_valid.json",
    "tests/conformance/corpus/json_invalid.json",
    "tests/fuzz/diff_fuzz.py",
    ".github/workflows/ci.yml",
]
absent = [p for p in REQUIRED if not os.path.exists(os.path.join(ROOT, p))]
check("all required files present", not absent, "missing: " + ", ".join(absent))

# --- 3. version consistency ------------------------------------------------
def read(p):
    return open(os.path.join(ROOT, p), encoding="utf-8").read()

versions = {"python __version__": py_version}
m = re.search(r'"version":\s*"([^"]+)"', read("impl-typescript/package.json"))
versions["typescript package.json"] = m.group(1) if m else "?"
m = re.search(r'export const VERSION = "([^"]+)"', read("impl-typescript/src/index.ts"))
versions["typescript VERSION"] = m.group(1) if m else "?"
m = re.search(r'^version = "([^"]+)"', read("impl-rust/Cargo.toml"), re.M)
versions["rust Cargo.toml"] = m.group(1) if m else "?"
m = re.search(r'pub const VERSION: &str = "([^"]+)"', read("impl-rust/src/lib.rs"))
versions["rust VERSION"] = m.group(1) if m else "?"
m = re.search(r'const Version = "([^"]+)"', read("impl-go/weft.go"))
versions["go Version"] = m.group(1) if m else "?"
m = re.search(r'spec revision\s+([0-9.]+)', read("spec/SPEC.md"))
versions["spec revision"] = m.group(1) if m else "?"

uniq = set(versions.values())
check("version strings agree across spec + 4 impls", len(uniq) == 1,
      ", ".join(f"{k}={v}" for k, v in versions.items()))

# --- 4. conformance corpus is non-trivial ----------------------------------
for f, lo in [("valid.json", 50), ("invalid.json", 20),
              ("json_valid.json", 5), ("json_invalid.json", 10)]:
    p = os.path.join(ROOT, "tests", "conformance", "corpus", f)
    n = len(json.load(open(p))) if os.path.exists(p) else 0
    check(f"corpus {f} has >= {lo} cases ({n})", n >= lo)

print()
if fails:
    print(f"{fails} FAILURES")
    sys.exit(1)
print("PACKAGE COMPLETENESS OK")
