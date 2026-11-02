# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅        |

## Reporting a vulnerability

Please report security issues **privately** — do not open a public
issue for anything exploitable.

* Preferred: GitHub **Private Vulnerability Reporting** on this
  repository (Security → Report a vulnerability).
* If that is unavailable, contact the maintainer listed in the
  repository profile.

Please include: the affected implementation (Python / TypeScript /
Rust / Go) and version, a minimal reproducing input, and the observed
vs. expected behavior. You should receive an acknowledgment within
7 days.

Specification-level issues (ambiguities that could cause conforming
implementations to diverge in an exploitable way — see SPEC §10.9) may
be reported the same way; if they are not exploitable, a public issue
is fine.

## Threat model

Weft parsers are designed to accept **arbitrary untrusted input**.
The guarantees the reference implementations make (SPEC §9, §10):

* **Bounded resources.** All limits (`max_depth`, `max_size_bytes`,
  `max_int_digits`, `max_items_per_coll`, `max_string_bytes`,
  `max_total_items`) are enforced on *every* tree-building entry point —
  the Weft parser **and** the lossless/lossy JSON readers — and
  oversized input is rejected early, before unbounded work happens.
* **No stack overflow.** Parsing, formatting, equality, and JSON
  conversion are iterative or depth-capped in all four implementations;
  deep input yields `WEFT_LIMIT_DEPTH`, never a crash (in Go, never a
  goroutine stack abort).
* **No host-integer traps.** Huge integer literals are length-checked
  before conversion; the Python implementation converts in chunks and is
  immune to `int_max_str_digits`.
* **UTF-8 strictness.** Lone surrogates are rejected everywhere,
  including inside `\u`-escapes of embedded JSON (the Go reader pre-scans,
  because `encoding/json` would silently replace them with U+FFFD).
* **No prototype pollution.** The TypeScript lossy JSON mapping writes
  `__proto__` / `constructor` keys as own data properties on
  null-prototype objects.
* **Canonical-form integrity.** §7.3 strict canonical output is
  byte-identical across the four implementations (enforced by the
  conformance corpus and differential fuzzing in CI), so signatures over
  canonical bytes cannot be desynchronized by implementation choice.

Anything that violates one of these guarantees on arbitrary input is a
security bug — please report it.

## Out of scope

* Resource exhaustion when the caller **raises** the documented limits
  beyond what their environment can serve.
* Behavior of non-canonical (pretty) output byte layout, which is
  explicitly implementation-defined (SPEC §7.2).
