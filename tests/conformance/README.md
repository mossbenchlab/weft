# Weft Conformance Suite

Language-neutral, byte-exact conformance tests shared by all four
reference implementations (Python / TypeScript / Rust / Go).

## Layout

```
tests/conformance/
├── candidates.json      # hand-written candidate cases (input + intent)
├── gen_corpus.py        # regenerates corpus/ by 4-way agreement
├── run.py               # runs the corpus against any/all implementations
├── corpus/
│   ├── valid.json        # documents every impl MUST parse; canonical bytes recorded
│   ├── invalid.json      # documents every impl MUST reject (error code when unanimous)
│   ├── json_valid.json   # lossless-JSON inputs every impl MUST accept
│   └── json_invalid.json # lossless-JSON inputs every impl MUST reject
└── drivers/
    ├── driver.py         # Python driver
    ├── driver.ts         # TypeScript driver (compiled to drivers/tsout/)
    ├── go-driver/        # Go driver (built to drivers/bin-go)
    └── rust-driver/      # Rust driver (cargo; target/debug/weftdrv)
```

## Driver protocol

Every driver is a stdin→stdout filter: `argv[1]` is the mode, stdin is
the input document (UTF-8), stdout is exactly one of:

```
OK\n<output>          # success; <output> compared byte-for-byte
ERR <CODE>            # rejected with a Weft error code
CRASH <info>          # anything else (uncaught exception, assertion)
```

Modes:

| mode          | meaning                                                            |
|---------------|--------------------------------------------------------------------|
| `canon`       | parse → §7.3 strict-canonical format                               |
| `canon2`      | parse → canonical → parse → canonical (idempotence)                |
| `fmt`         | parse → default (pretty) format                                    |
| `jroundtrip`  | parse → lossless JSON → parse back → canonical; asserts identity   |
| `tolossless`  | parse → §11.2 lossless JSON (key-sorted, for stable comparison)    |
| `fromlossless`| lossless JSON text → parse (`loads_json`) → canonical              |

`jroundtrip` identity = **canonical byte equality** of the value before
and after the JSON round trip. `equals()` (SPEC §5.16) is additionally
asserted, except for documents containing float NaN: §5.16 defines
`NaN ≢ NaN`, so canonical bytes are the only valid identity predicate
there.

## Corpus semantics

* `valid.json`: each case records `input` and `canonical`. Every
  implementation must produce byte-identical `canonical` output for
  `canon`, `canon2` **and** `jroundtrip`. This is the §7.3 cross-
  implementation signature guarantee under test.
* `invalid.json`: each case records `input` and, when all four
  implementations agree, `code`. Implementations must reject the input;
  if `code` is present the error code must match exactly.
* `json_valid.json` / `json_invalid.json`: same, for the §11.2 lossless
  JSON reader (`fromlossless` mode), including duplicate-key rejection,
  lone-surrogate rejection, digit limits and nesting limits.

## Regenerating the corpus (maintainers)

```
python3 tests/conformance/gen_corpus.py
```

reads `candidates.json`, runs every case through all four drivers, and
writes `corpus/` **only if all four implementations agree** (same
outcome kind; byte-equal output on success). Any disagreement is
printed and the corpus is left untouched — a disagreement is a bug in
at least one implementation, never something to paper over. Error codes
are recorded only when unanimous.

## Running

```
python3 tests/conformance/run.py --build        # build drivers + run all 4
python3 tests/conformance/run.py --impl py,rs   # subset
```

Exit status 0 = all checks passed.

## Policy: SPEC MUSTs → tests

Every normative MUST added to SPEC.md needs at least one corpus case
exercising it (positive and negative where applicable). When fixing a
bug found in any single implementation, add the reproducer to
`candidates.json` and regenerate — the 4-way agreement check then
guards all implementations, not just the one that was broken.
