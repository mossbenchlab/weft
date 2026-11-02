# Weft-Schema `:pattern` — Normative Pattern Grammar

Status: normative companion to SPEC.md for the Weft-Schema validator's
`:pattern` constraint. The schema validator currently ships with the
Python reference implementation only; any future implementation of
Weft-Schema MUST implement exactly this subset, so that a schema
validates identically everywhere.

## Design requirement

A `:pattern` is matched with **linear-time semantics**: the engine MUST
NOT backtrack, and matching MUST complete in
O(len(text) × states × log ranges) with states bounded by the compile
limits below. Wall-clock timeouts around a backtracking host engine are
**not** a conforming implementation — they cannot interrupt a host
regex that holds the interpreter lock.

Matching is anchored full-match: the pattern must consume the entire
string. `fullmatch` returns a boolean; capture groups do not exist.

## Supported syntax

| Form | Meaning |
|------|---------|
| `x` (literal) | one code point |
| `.` | any code point except U+000A |
| `\n \t \r \f \v \0` | the usual control escapes |
| `\xHH` | code point U+00HH (exactly 2 ASCII hex digits) |
| `\uHHHH` | BMP code point (exactly 4 ASCII hex digits) |
| `\u{H…}` | code point, 1–6 ASCII hex digits; > U+10FFFF and surrogates rejected |
| `\d` / `\D` | `[0-9]` and its complement (**ASCII only**) |
| `\w` / `\W` | `[A-Za-z0-9_]` and its complement (**ASCII only**) |
| `\s` / `\S` | `[ \t\n\v\f\r]` and its complement (**ASCII only**) |
| `\.` etc. | escaped punctuation is that literal character |
| `[...]` / `[^...]` | character class; ranges `a-z`; class escapes allowed; a single-code-point escape (e.g. `\x61`) is a valid range endpoint, a multi-character class (`\d`) is not |
| `e*` `e+` `e?` | repetition (a trailing `?` lazy marker is accepted and ignored — greediness cannot change a boolean full match) |
| `e{m}` `e{m,}` `e{m,n}` | counted repetition, ASCII digits only, each count ≤ 1000 |
| `(...)` and `(?:...)` | grouping (identical semantics — no captures) |
| `a\|b` | alternation |
| `^` / `$` | assert start / end of input (redundant under full-match but accepted anywhere) |

A `{` that does not form a well-formed counted repetition is a literal
`{` (same behavior as mainstream engines).

## Rejected at compile time (`WEFT_SCHEMA_PATTERN`)

- backreferences (`\1` … `\9`)
- lookaround (`(?=` `(?!` `(?<=` `(?<!`)
- named groups and inline flags (`(?P<` `(?<name>` `(?i)` …)
- alphabetic escapes outside the table above (`\p`, `\b`, `\e`, …)
- hex escapes containing anything but ASCII `0-9a-fA-F` (host integer
  parsers that tolerate `+`, `_`, or non-ASCII digits MUST NOT be
  exposed to the pattern grammar)
- malformed classes, ranges with reversed endpoints, dangling
  quantifiers, unterminated groups

## Compile-time resource limits (all violations: `WEFT_SCHEMA_PATTERN`)

| Limit | Value |
|-------|-------|
| pattern length | ≤ 10,000 code points |
| compiled NFA states | ≤ 10,000 |
| each `{m,n}` count | ≤ 1,000 |
| group nesting depth | ≤ 64 |

Character-class ranges MUST be sorted and coalesced at compile time and
matched by binary search, so duplicated or overlapping class members
(e.g. `[aaaa…a]`) cannot multiply the per-character matching cost.

**Every diagnostic — invalid, unsupported, or oversized — is reported
as a schema error (`WEFT_SCHEMA_PATTERN`); host exceptions MUST NOT
escape pattern compilation or matching.** A pattern failing to compile
makes the `:pattern` constraint report a violation for the field; it
never aborts the host process or caller.

## Semantics notes

- Matching operates on Unicode scalar values (code points), not UTF-16
  units or bytes.
- `\d \w \s` are pinned to the ASCII sets above so every host matches
  identical strings; Unicode-aware classes are deliberately excluded
  from 1.0.
- `.` excludes only U+000A, matching the common default.
- Equal-length `{m,n}` expansion is structural: the compiled size grows
  with `m`/`n`, which is why counts are capped and the state cap exists.
