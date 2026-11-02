# Weft 1.0 — Specification

**Status**: Stable (v1.0, spec revision 1.0.1)
**Last updated**: 2026-11-02
**Authors**: homeaibase-design and Claude

Weft is a textual data format designed for generative AI to read and write.
It replaces the role that JSON plays today in configuration, interchange, and
machine-generated structured data, while being roughly an order of magnitude
smaller in token count and more robust to common attack patterns.

This document is the normative specification. Where the prose and the formal
grammar disagree, the grammar wins.

---

## Table of Contents

1. Design Principles
2. Document Structure
3. Encoding
4. Lexical Elements
   - 4.1. Whitespace
   - 4.2. Comments
   - 4.3. Tokens
5. Values
   - 5.1. Null
   - 5.2. Boolean
   - 5.3. Integer
   - 5.4. BigInt
   - 5.5. Float
   - 5.6. Decimal
   - 5.7. String
   - 5.8. Binary
   - 5.9. DateTime
   - 5.10. Symbol
   - 5.11. Keyword
   - 5.12. List
   - 5.13. Map
   - 5.14. Sexpr
   - 5.15. TypedValue
   - 5.16. Equality
6. Grammar (ABNF)
7. Round-trip Rules
   - 7.1. Shared canonical invariants
   - 7.2. Canonical (default)
   - 7.3. Strict canonical
8. Type Annotations
9. Resource Limits
10. Security Considerations
11. JSON Interoperability
12. Reserved Syntax
13. Versioning and Compatibility
14. IANA Considerations

---

## 1. Design Principles

Weft is bound by five rules that take precedence over convenience:

1. **No code execution during parsing.** The format defines data only. A
   parser never constructs host objects by name, calls user-provided code,
   or evaluates expressions. A valid Weft document cannot invoke
   `os.system`, access a URL, open a file, or import a module. This rule
   is non-negotiable; it exists because of what happened to YAML.

2. **Bounded parsing.** Every parser accepts resource limits (depth, size,
   element count, string length) and must enforce them. The default
   limits are specified in §9 and are tight enough that unlimited input
   is a parser bug, not a configuration choice.

3. **Deterministic encoding.** Any Weft tree has a canonical textual
   form. Two parsers given the same input produce the same tree, and
   two formatters given the same tree produce the same text.

4. **Round-trip stability.** `parse(format(tree)) == tree` and
   `format(parse(text))` is textually equal to `text` when `text` is
   canonical.

5. **AI legibility over human writing comfort.** Where a choice reduces
   output tokens for a generative model, it beats brevity for a human
   typist. Where it reduces error rate for a model, it beats
   familiarity.

---

## 2. Document Structure

A Weft document is a UTF-8 encoded byte sequence. It represents either:

- **A single value**, which is the common case; or
- **A sequence of top-level values**, used for streaming logs and
  record-oriented files.

A parser MUST signal which mode it was invoked in. Mode is not inferred
from content.

An empty document (zero bytes, or only whitespace and comments) is a
valid "sequence of zero values" but is NOT a valid single-value
document.

---

## 3. Encoding

Weft documents are UTF-8. Specifically:

- The byte sequence MUST be valid UTF-8 as defined in RFC 3629.
- Overlong forms (e.g. encoding U+0020 as `C0 A0`) MUST be rejected.
- Unpaired surrogate code points (U+D800–U+DFFF) MUST be rejected.
- The code points U+FFFE and U+FFFF MUST be rejected.
- The byte order mark (U+FEFF) MUST NOT appear anywhere in the
  document. A document beginning with the UTF-8 BOM (bytes `EF BB BF`)
  is rejected.
- Line endings `\n` (U+000A), `\r\n` (U+000D U+000A), and `\r`
  (U+000D) are all accepted as line terminators and are treated as
  equivalent for parsing. On output, formatters MUST emit `\n` only.

A parser MAY accept non-UTF-8 input only when its API documentation
explicitly declares it as an extension. The standard describes
UTF-8 only.

---

## 4. Lexical Elements

### 4.1. Whitespace

The following code points are whitespace:

- U+0009 (horizontal tab)
- U+000A (line feed)
- U+000D (carriage return)
- U+0020 (space)
- U+002C (comma, `,`)

The comma is a whitespace character. This exists so that JSON data
pasted into a Weft document parses without edits. A sequence of any
whitespace characters is semantically equivalent to a single space.

No other whitespace is accepted. In particular, non-breaking space
(U+00A0), form feed (U+000C), vertical tab (U+000B), and the various
Unicode whitespace characters (U+2000–U+200A, U+2028, U+2029, U+205F,
U+3000) are not whitespace in Weft. They may appear inside strings
only.

### 4.2. Comments

Weft has two comment forms:

**Line comments** begin with `;` and extend to the next line
terminator. The terminator is not part of the comment.

```weft
; this is a line comment
(fn foo [])     ; so is this
```

**Block comments** begin with `#|` and end with `|#`. Block comments
nest; `#| outer #| inner |# still outer |#` is one block comment.

```weft
#| this is
   a block comment |#

#| nested #| inside |# comment |#
```

Comments MUST NOT contain the following characters:

- C0 controls except Tab/LF/CR: U+0000–U+0008, U+000B, U+000C,
  U+000E–U+001F
- DEL: U+007F
- C1 controls: U+0080–U+009F (includes U+009B / CSI)
- Bidirectional override and isolate controls:
  U+202A, U+202B, U+202C, U+202D, U+202E,
  U+2066, U+2067, U+2068, U+2069

Tab (U+0009), line feed (U+000A), and carriage return (U+000D) are
permitted. The exclusions above prevent terminal escape injection
("Trojan Source"-style attacks) through comments; a parser MUST
reject a document whose comments contain any excluded code point.

Comments are not preserved by the parser. A parser that preserves
comments for formatting purposes MUST expose that as a separate API.

### 4.3. Tokens

The lexical analyzer produces tokens from the following categories:

| Token      | Example                | Notes                                   |
|------------|------------------------|-----------------------------------------|
| LPAREN     | `(`                    | opens S-expression                      |
| RPAREN     | `)`                    | closes S-expression                     |
| LBRACK     | `[`                    | opens list                              |
| RBRACK     | `]`                    | closes list                             |
| LBRACE     | `{`                    | opens map                               |
| RBRACE     | `}`                    | closes map                              |
| COLONCOLON | `::`                   | type annotation operator                |
| INT        | `42`, `-7`, `0x2A`     | integer literal                         |
| BIGINT     | `123n`, `-7n`          | arbitrary-precision integer             |
| FLOAT      | `3.14`, `1e10`         | binary float (IEEE 754 double)          |
| DECIMAL    | `3.14m`, `0.1m`        | decimal float (exact)                   |
| STRING     | `"abc"`, `"""abc"""`   | text string                             |
| BINARY     | `b"base64:AAEC"`       | binary blob                             |
| DATETIME   | `#2026-11-02T06:00Z`   | date-time, RFC 3339                     |
| BOOL       | `true`, `false`        | boolean                                 |
| NIL        | `nil`                  | null                                    |
| NAN        | `nan`                  | IEEE 754 NaN                            |
| INF        | `inf`, `-inf`          | IEEE 754 infinity                       |
| SYMBOL     | `foo`, `my-fn`, `x?`   | identifier                              |
| KEYWORD    | `:name`, `:age`        | keyword (map key)                       |

Numeric literals, boolean literals, and `nil`, `nan`, `inf`, `-inf`
are not symbols. They are reserved and may not be used as identifiers.
These reserved names MAY appear as keyword or map-key names: `:true`,
`:nil`, `:nan` are parsed as keywords because the leading `:` removes
the ambiguity with a top-level reserved literal.

**Token boundary.** After an INT, BIGINT, FLOAT, DECIMAL, DATETIME,
BINARY, SYMBOL, or KEYWORD token, the next byte (if any) MUST be one
of: whitespace (per §4.1), `(`, `)`, `[`, `]`, `{`, `}`, `"`, `;`,
`#` (opening a datetime or a block comment), `::`, or EOF. Any other
byte following such a token MUST be rejected with a lexical error,
because the previous token has not ended yet.

String and parenthesized tokens (`"..."`, `b"..."`, `(`, `[`, `{`,
`)`, `]`, `}`, `::`) are self-delimiting and need no following
boundary. In particular, `[1"a"]` is valid (the `"` closes the
integer and opens a string); `[1-2]`, `[1abc]`, `[1n2]` are not
(the next byte continues a numeric literal with an illegal shape).

---

## 5. Values

Each value category below defines its literal syntax, its abstract
value, and any constraints on parsers and formatters.

### 5.1. Null

**Syntax**: `nil`

**Value**: the single null value, written `nil`.

Weft has one null; it is distinct from false, from zero, from an
empty string, from an empty list, and from an empty map.

The identifiers `null` and `None` are NOT null in Weft. They are
ordinary symbols.

### 5.2. Boolean

**Syntax**: `true` | `false`

**Value**: `true` or `false`.

### 5.3. Integer

**Syntax**:

```
INT    = SIGN? DIGITS
       | SIGN? "0x" HEX+ ("_" HEX+)*
       | SIGN? "0o" OCT+ ("_" OCT+)*
       | SIGN? "0b" BIN+ ("_" BIN+)*
SIGN   = "+" | "-"
DIGITS = DIGIT+ ("_" DIGIT+)*
DIGIT  = "0".."9"
HEX    = "0".."9" | "a".."f" | "A".."F"
OCT    = "0".."7"
BIN    = "0" | "1"
```

Examples:

```
42
-7
+100
0
0x2A        ; 42
0xDE_AD     ; 57005
0b1010_0101 ; 165
0o777       ; 511
1_000_000
```

An underscore MUST NOT appear:
- At the start of the digit sequence (after any `0x`/`0o`/`0b` prefix),
- At the end of the digit sequence,
- Adjacent to another underscore.

`-0` and `0` represent the same value: zero. There is no negative zero
in Weft integers.

The parser MUST represent integer values with arbitrary precision
internally. Range-checking is a separate concern (see §9) that
applies to the integer's **decimal-equivalent digit count** —
``⌈len * log10(base)⌉`` — not to the raw character count of the
literal. A 4 000-digit ``0xf…f`` is checked against the same
budget as a 4 817-digit decimal value, so the formatter can
re-emit the same number in base 10 without crossing the limit.

Hexadecimal, octal, and binary literals represent non-negative
integers unless preceded by a sign. `-0x10` is the integer `-16`.

### 5.4. BigInt

**Syntax**: `INT` followed immediately by `n`.

Examples:

```
0n
42n
-7n
123456789012345678901234567890n
0xDEADBEEFn
```

BigInt is semantically equivalent to Integer; the suffix exists as a
hint to parsers that produce different host types (e.g. JavaScript's
`BigInt` vs `number`). On the wire there is no difference between
`42` and `42n` — they parse to the same abstract integer value. The
suffix is preserved by formatters that round-trip.

Operations that compare Weft values for equality — including
schema `unique`, map key uniqueness, and set membership — treat
``42`` and ``42n`` as **the same** integer value. The presentation
hint (``n`` suffix, hex/octal/binary form) is not part of the
abstract value and does not create distinctness.

### 5.5. Float

**Syntax**:

```
FLOAT  = SIGN? DIGITS "." DIGITS EXP?
       | SIGN? DIGITS EXP
       | SIGN? "." DIGITS EXP?       ; .5 is NOT valid; use 0.5
                                     ; (this alternative is NOT accepted)
EXP    = ("e" | "E") SIGN? DIGITS
```

The parser MUST reject:
- A float with a leading dot and no integer part (`.5` is not valid;
  write `0.5`).
- A float with a trailing dot and no fractional part (`5.` is not
  valid; write `5.0`).
- Two or more consecutive dots.

The special float values are written literally:

```
nan
inf
-inf
+inf        ; equivalent to inf
```

These reserved spellings are **case-sensitive**: only the exact
lowercase forms `nan`, `inf`, `-inf`, `+inf` are float literals. A
symbol spelled `NaN`, `Nan`, `INF`, `Inf`, `-Inf`, `-INF`, etc. is
NOT a reserved float literal; it parses as an ordinary Symbol
(see §5.10). Likewise `TRUE`, `False`, `NIL` are Symbols, not the
corresponding reserved literals.

Floats are IEEE 754 double precision (binary64). A parser MUST
accept decimal literals up to 17 significant digits without loss
and MUST produce the IEEE 754-correctly-rounded result for values
with more digits.

### 5.6. Decimal

**Syntax**: a Float syntax followed immediately by `m` (for "money").

Examples:

```
3.14m
0.1m
-99.99m
1.5e3m
```

Decimal values are arbitrary-precision decimal fractions. They are
semantically distinct from Float: `0.1m + 0.2m` is exactly `0.3m`,
whereas `0.1 + 0.2` is approximately `0.30000000000000004`.

Decimal values are intended for prices, exchange rates, and other
contexts where IEEE 754 rounding is a bug. A parser MUST represent
them exactly. The abstract value preserves significant digits:
`1.0m`, `1.00m`, and `1.000m` are distinguishable values.

Decimal values do not have NaN or infinity. The literals `nan`,
`inf`, `-inf` are Floats; there is no Decimal counterpart.

### 5.7. String

**Syntax**:

Three forms:

**Standard** (one-line):

```
STRING = DQUOTE SCHAR* DQUOTE
SCHAR  = ESCAPE | <any Unicode scalar value except U+0022, U+005C, or control
                   characters below U+0020>
```

**Triple-quoted** (multi-line):

```
TRIPLE = DQUOTE DQUOTE DQUOTE TCHAR* DQUOTE DQUOTE DQUOTE
TCHAR  = ESCAPE | <any Unicode scalar value except an unescaped sequence
                   of three U+0022 characters>
```

**Raw** (no escapes):

```
RAW    = "r" DQUOTE RCHAR* DQUOTE
RCHAR  = <any Unicode scalar value except U+0022, with the restriction
         below on literal control characters>
```

Raw strings have no escape processing: `\n` inside `r"..."` is two
characters, backslash and `n`. A raw string MUST NOT contain a bare
`"`. There is no way to include `"` in a raw string. If a `"` is
needed, use the standard form.

**Control characters inside raw and triple strings.** The following
code points MAY appear literally inside TRIPLE and RAW strings:
U+0009 (TAB), U+000A (LF), U+000D (CR). All other code points below
U+0020, as well as U+007F (DEL) and U+0080–U+009F (C1 controls),
MUST be rejected inside TRIPLE and RAW. Bidirectional override and
isolate controls (U+202A–U+202E, U+2066–U+2069) are permitted inside
strings — unlike comments (§4.2) — because they are user data; the
strict canonical formatter escapes them on output (§7).

**Line ending preservation.** The literal content of TRIPLE and RAW
strings is preserved exactly as the source bytes read, after UTF-8
decoding. A parser MUST NOT normalise CR, CRLF, or LF inside these
strings. Two documents that differ only in the line endings used
inside a string are distinct values. The §3 statement that
CR/CRLF/LF are equivalent **applies only to inter-token whitespace
and line comments**, not to string contents.

**Escapes** (in STRING and TRIPLE only):

```
ESCAPE = "\"  ( "n" | "t" | "r" | "\\" | "\"" | "0"
              | "x" HEX HEX
              | "u" HEX HEX HEX HEX
              | "u{" HEX{1,6} "}" )
```

- `\n` → U+000A
- `\t` → U+0009
- `\r` → U+000D
- `\\` → U+005C
- `\"` → U+0022
- `\0` → U+0000
- `\xHH` → the byte value `HH` (00–FF) interpreted as a Unicode code
  point. Code points above U+007F require the longer `\u` forms.
  `\x80` through `\xFF` MUST be rejected.
- `\uHHHH` → the Unicode scalar U+HHHH. Surrogate halves
  (U+D800–U+DFFF) MUST be rejected.
- `\u{H...}` → the Unicode scalar U+H..., 1 to 6 hex digits.
  Values above U+10FFFF MUST be rejected. Surrogate halves MUST
  be rejected.

Any other escape sequence MUST cause a parse error. The 1.0 parser
is strict: `\e`, `\a`, `\p`, `\s`, and so on are all errors. This
differs from JSON's permissive handling.

Control characters U+0000 through U+001F (except as produced by
escape sequences in STRING and TRIPLE, and the single exception
for U+000A inside TRIPLE and RAW) MUST NOT appear literally in a
string. A literal tab or newline inside `"..."` is a parse error.
Inside `"""..."""` and `r"..."`, literal newlines are permitted;
literal tabs are permitted inside both.

**Normalization**: strings are sequences of Unicode scalar values.
A parser MUST NOT normalize them (NFC, NFKC, or otherwise). Two
strings compare equal iff their scalar sequences are identical.

### 5.8. Binary

**Syntax**:

```
BINARY = "b" DQUOTE ("base64:" B64CHAR* | "hex:" HEXCHAR*) DQUOTE
```

Examples:

```
b"base64:AAECAwQF"              ; bytes 00 01 02 03 04 05
b"hex:deadbeef"                 ; bytes de ad be ef
b"base64:"                      ; empty
```

The base64 encoding is the standard alphabet (RFC 4648, §4), with
padding required. **The canonical check of §3.5 of RFC 4648 is
MANDATORY**: any trailing bits that are not part of a full output
byte MUST be zero. In particular, `b"base64:AB=="` MUST be rejected
(the second input character `B` has excess non-zero bits beyond the
single encoded byte). This rules out multiple base64 strings
encoding the same byte sequence, which would otherwise break
equality (§5.16) and signature verification.

The hex encoding is case-insensitive and MUST contain an even
number of digits.

A parser MUST reject binary literals that fail to decode or fail
the canonical check. Whitespace inside the literal (e.g. a space in
`base64:AA EC`) is not permitted.

Equality is defined on the underlying byte sequence (§5.16): the
same bytes encoded as `base64:AQ==` and `hex:01` are equal values.
The canonical formatter (§7) emits `base64:` form; strict canonical
(§7.3) likewise.

### 5.9. DateTime

**Syntax**: `#` followed by a date, optionally followed by `T` and a
time, which is optionally followed by a time-zone designator.

```
DATETIME = "#" DATE [ "T" TIME [ ZONE ] ]
DATE     = YYYY "-" MM "-" DD
TIME     = HH ":" MM ":" SS ("." FRAC)?
ZONE     = "Z" / (("+" / "-") HH ":" MM)
```

A datetime has one of three shapes:

- **Date only**: `#2026-11-02`. Represents a calendar date; has no
  time, no offset.
- **Naive datetime**: `#2026-11-02T06:00:00`. Represents a wall-clock
  reading; has a time but no offset. Two naive datetimes compare
  equal iff all fields match; a naive datetime is NEVER equal to an
  offset-bearing datetime of the same wall-clock reading.
- **Offset datetime**: `#2026-11-02T06:00:00Z` or
  `#2026-11-02T06:00:00+09:00`. Represents an instant; two offset
  datetimes compare equal iff they identify the same instant AND
  carry the same offset.

Examples:

```
#2026-11-02
#2026-11-02T06:00:00
#2026-11-02T06:00:00Z
#2026-11-02T06:00:00+09:00
#2026-11-02T06:00:00.123456+09:00
```

The year MUST be in the range 0001 to 9999 inclusive. Years outside
this range MUST be rejected. (This bounds the memory a parser can
be forced to allocate for a timestamp.) The fractional second may
have up to 9 digits (nanosecond precision), with between 1 and 9
digits after the `.`.

**Offset range.** The UTC offset MUST satisfy
`-14:00 <= OFFSET <= +14:00`. Values outside this range MUST be
rejected. (The IANA tz database defines no civil zone with an
offset outside this range; this bound is conservative.)

**Offset minutes.** The minute component of an offset MUST be in
the range `00` to `59` inclusive. `+09:60` MUST be rejected.

**Leap seconds.** The seconds field value `60` is accepted as a
positive leap second insertion when and only when the resulting
instant falls at 23:59:60 UTC. Specifically:

- For an **offset datetime** with offset `off` (in minutes), `:60`
  is accepted iff `(hour * 60 + minute - off) mod 1440 == 23*60+59`.
  So `#2016-12-31T23:59:60Z` and `#2017-01-01T08:59:60+09:00` are
  both accepted (same instant), while `#2026-11-02T00:00:60Z` is
  rejected.
- For a **naive datetime**, `:60` is accepted iff `hour == 23 and
  minute == 59`. The naive form carries no offset so UTC rule is
  applied directly.

**Canonical normalisation.** When formatting canonically (§7), the
following transformations are applied:

1. Offsets equal to UTC (`+00:00`, `-00:00`) are formatted as `Z`.
2. Weft offsets are whole-minute by syntax; implementations that
   receive sub-minute offsets from other sources (e.g. historical
   tz data via a connector) MUST convert to UTC and emit `Z` on
   output, so the canonical bytes stay within the Weft syntax.
3. Fractional seconds: trailing zeros are stripped. If every
   fractional digit is zero (`.0`, `.000`), the whole fractional
   part including the `.` is removed: `06:00:00.000Z` →
   `06:00:00Z`. (Rationale: preserving an all-zero `.0` would
   require parsers to carry a "had a fraction" flag that no value
   model needs for anything else; the fraction's presence is not
   part of the abstract value.)
4. **Leap seconds are preserved, not rolled over.** The canonical
   form of `#2016-12-31T23:59:60Z` is `#2016-12-31T23:59:60Z`, not
   `#2017-01-01T00:00:00Z`. A `:60` second is first-class in the
   canonical form so (a) the information that this was a leap
   second is kept, (b) hosts with native "no leap second" types
   can keep the original text through a lossless round-trip, and
   (c) `format(parse(x)) == format(parse(format(parse(x))))` holds
   bytewise. Hosts that need the instant value MAY compute it from
   the parsed fields but MUST not lose the `:60` on output.
5. The three shapes (date-only, naive, offset) are preserved. A
   parser MUST NOT invent a time for a date-only input, add an
   offset to a naive input, or drop an offset from an offset input.

Beyond these normalisations, a parser MUST NOT change the
represented instant.

### 5.10. Symbol

**Syntax**:

```
SYMBOL = SYMSTART SYMCONT*
SYMSTART = ASCII-LETTER | "_" | "+" | "-" | "*" | "/" | "<" | ">" | "=" | "!" | "?"
         | UNICODE-L
SYMCONT  = SYMSTART | ASCII-DIGIT
```

A symbol is an identifier. The character classes are:

- `ASCII-LETTER` = U+0041–U+005A, U+0061–U+007A (A–Z, a–z)
- `ASCII-DIGIT` = U+0030–U+0039 (0–9)
- `UNICODE-L` = any code point whose Unicode General Category is
  `L*` (`Lu`, `Ll`, `Lt`, `Lm`, `Lo`), per the Unicode version
  pinned by this specification (see §14). Supplementary-plane
  letters (e.g. U+1D400 MATHEMATICAL BOLD CAPITAL A) are included.
- Operator-like ASCII punctuation: `+ - * / < > = ! ?` and `_`.

Explicitly **excluded** from `UNICODE-L`:

- Decimal digits of any script (`Nd`), e.g. U+FF11 FULLWIDTH DIGIT
  ONE, U+0661 ARABIC-INDIC DIGIT ONE. (Fullwidth digits in an
  identifier would make `:x１` look like a keyword with a
  numeric-looking continuation.)
- Letter-numbers (`Nl`), e.g. U+2160 ROMAN NUMERAL ONE.
- Other numbers (`No`), e.g. U+2460 CIRCLED DIGIT ONE.
- Marks (`Mn`, `Mc`, `Me`), spacing (`Zs`, `Zl`, `Zp`), controls
  (`Cc`, `Cf`), surrogates (`Cs`), private use (`Co`), and all
  symbol categories (`So`, `Sm`, `Sc`, `Sk`).

The symbols `true`, `false`, `nil`, `nan`, `inf`, `-inf` are
reserved and MUST be parsed as the corresponding scalar literals.

Symbols are case-sensitive: `Foo` and `foo` are distinct, and
`TRUE` is a Symbol, not the Boolean `true` (§4.3).

### 5.11. Keyword

**Syntax**: `:` followed by a keyword name.

```
KEYWORD = ":" KWNAME
KWNAME  = SYMSTART-NOSIGN SYMCONT*
        | SYMSTART-SIGN SYMCONT-NODIGIT SYMCONT*
SYMSTART-NOSIGN = SYMSTART minus {"+", "-"}
SYMSTART-SIGN   = "+" | "-"
SYMCONT-NODIGIT = SYMCONT minus ASCII-DIGIT
```

Keywords are used as map keys and S-expression attribute names.
They are distinct from strings: `:name` is not `"name"`.

Compared with Symbol (§5.10), keyword names add one restriction:
**a keyword name MUST NOT begin with `+` or `-` immediately
followed by an ASCII digit.** Thus `:+1`, `:-7`, `:+42abc` MUST be
rejected. The reason is parser ambiguity: `[:+1 2]` would otherwise
be indistinguishable from `[: +1 2]` (keyword with name `:` applied
to signed number). All four reference implementations enforce this.
`:+x`, `:-foo` remain valid (sign followed by non-digit).

The reserved spellings `true`, `false`, `nil`, `nan`, `inf` MAY be
used as keyword names (`:true`, `:nil`, `:nan`), because the leading
`:` disambiguates them from the top-level literals. Lossless JSON
decoders (§11.2) MUST accept these spellings in `$kw` payloads and
`$map` keys. The empty keyword name (`:` with no following name) is
NOT a keyword; `:` by itself is a syntax error.

### 5.12. List

**Syntax**: `[` zero or more values `]`.

```
LIST = "[" VALUE* "]"
```

Lists are ordered sequences. A list may contain values of any
mixed type, including other lists, maps, and S-expressions.

The empty list is `[]`.

### 5.13. Map

**Syntax**: `{` zero or more (keyword, value) pairs `}`.

```
MAP = "{" (KEYWORD VALUE)* "}"
```

Maps have Keyword keys only. String keys, integer keys, and other
value types as keys are NOT permitted. (If a string-keyed map is
required, encode it as a list of two-element lists.)

Keys in a map MUST be unique. A map literal with duplicate keys
MUST be rejected by the parser. The map is unordered as an abstract
value, but a formatter MUST preserve the input order on round-trip.

The empty map is `{}`.

### 5.14. Sexpr

**Syntax**: `(` symbol value* `)`.

```
SEXPR = "(" SYMBOL (VALUE)* ")"
```

An S-expression is a tagged node. The symbol immediately after `(`
is the tag; it is distinguished from the rest of the items. The tag
MUST be a symbol (not a keyword, not a nested S-expr).

Within the items, keyword-value pairs are conventionally used for
attributes:

```
(fn fib (n) :return Int :pure true (if ...))
```

A parser MUST preserve the full item sequence. Interpretation of
positional vs keyword items is the application's responsibility.

### 5.15. TypedValue

**Syntax**: any value immediately followed by `::` and a type
expression.

```
TYPED = VALUE "::" TYPE_EXPR
TYPE_EXPR = SYMBOL | SEXPR            ; type expressions are symbols or
                                      ; S-expressions whose tag is a type name
```

Examples:

```
42::Int
"hello"::String
[1 2 3]::(List Int)
(f x)::Int
```

The type expression is a Weft value, with the restriction that it
is either a Symbol (a type name) or an Sexpr whose tag is a Symbol
(a type constructor).

A TypedValue is a pair (inner value, type expression). Interpretation
is application-defined; Weft Core performs no type checking.

Nested type annotations are permitted: `42::Int::Any` is a
TypedValue whose inner value is itself a TypedValue. A parser MUST
preserve this nesting.

**Whitespace around `::`.** Horizontal whitespace is permitted
before and after the `::` operator:

```
42::Int          ; preferred form
42 :: Int        ; valid, equivalent to 42::Int
42::  Int        ; valid
```

Line terminators within a `::` sequence (e.g. a newline between
`42` and `::`) are also permitted. The canonical form (§7) emits
no whitespace around `::`.

### 5.16. Equality

Weft defines a value-level equality relation `≡` used by:

- schema `:unique` and other uniqueness constraints,
- map key uniqueness (§5.13),
- lossless JSON round-trip identity (§11.2),
- host-visible equality APIs (`weft.equals(a, b)`).

Equality is defined by the following rules. Two values of
different categories (e.g. `Integer` vs `Decimal`, `String` vs
`Symbol`) are NEVER equal, except for the Integer/BigInt case
below.

| Category            | `a ≡ b` iff |
|---------------------|-------------|
| `nil`               | both are `nil` |
| `Boolean`           | same truth value |
| `Integer`/`BigInt`  | numeric values are equal (cross-type: `42 ≡ 42n`, §5.4). The `n` suffix and base form (hex/octal/binary) are presentation hints and do NOT affect equality. |
| `Float`             | IEEE 754 bit-identical. `-0.0 ≢ 0.0` (different bits). `nan ≢ nan` (any NaN compares unequal to itself and all other floats). |
| `Decimal`           | normalised texts are identical. Normalisation (applied at parse/construction, §7.2) strips a leading `+` and lowercases the exponent marker. `1m ≡ 1m`, `+1.5m ≡ 1.5m`, `1E2m ≡ 1e2m`; but `1m ≢ 1.0m` and `1m ≢ 1.00m` (significant digits differ) and `1.5e1m ≢ 15m` (different textual forms of the same number). |
| `String`            | Unicode scalar sequences identical (no NFC). |
| `Binary`            | underlying byte sequences identical. Encoding (`base64` vs `hex`) is a presentation hint and does NOT affect equality. |
| `DateTime`          | same shape (date-only / naive / offset, §5.9), AND all field values equal, AND (for offset form) offsets textually equal. `#2026-11-02T00:00:00Z ≢ #2026-11-02T09:00:00+09:00` (same instant, different offset). |
| `Symbol`            | names identical (code-point-wise). |
| `Keyword`           | names identical. |
| `List`              | same length AND element-wise `≡`. |
| `Map`               | same key set AND for every key, values `≡`. Insertion order does NOT affect equality (§5.13). |
| `Sexpr`             | tag names equal AND same item count AND element-wise `≡`. |
| `TypedValue`        | inner values `≡` AND type expressions `≡`. |

**Note on Float equality.** The IEEE-754 bit rule means `nan ≡ nan`
is **false**; schema `:unique [nan nan]` therefore sees no
duplicate. This is intentional and matches the behaviour of every
IEEE 754 host language. Applications that need "both NaNs are the
same" MUST wrap in a canonical representative before comparing.

**Note on map comparison.** Map equality is unordered. The
canonical formatter preserves insertion order; it does not sort.
Two maps that differ only in insertion order are equal values that
format to different textual bytes under the non-strict canonical
form. Under the **strict** canonical form (§7, `sort_keys=true`)
they format to the same bytes.

---

## 6. Grammar (ABNF)

The following ABNF (RFC 5234) describes the full syntax. Lexical
rules (whitespace, comments) are implicit between tokens except
where noted.

**All literal strings below are case-sensitive**, using the `%s`
prefix convention of RFC 5234's updated string literal rules. In
particular `%s"true"` is the four bytes `t`, `r`, `u`, `e` — not
`TRUE`, not `True`.

```abnf
document       = *WS *(value *WS)                 ; stream mode
                 / *WS value *WS                  ; single-value mode
                                                  ; (parser chooses which)

value          = annotated

; A value can carry any number of ::type annotations.
annotated      = primary *(annot)
annot          = *WS "::" *WS type-expr

primary        = scalar / list / map / sexpr

scalar         = null / bool / integer / bigint / float / decimal
               / string / binary / datetime
               / symbol / keyword

null           = %s"nil"
bool           = %s"true" / %s"false"

integer        = [sign] (dec-int / hex-int / oct-int / bin-int)
sign           = "+" / "-"
dec-int        = dec-digit *(["_"] dec-digit)
hex-int        = (%s"0x" / %s"0X") hex-digit *(["_"] hex-digit)
oct-int        = (%s"0o" / %s"0O") oct-digit *(["_"] oct-digit)
bin-int        = (%s"0b" / %s"0B") bin-digit *(["_"] bin-digit)

bigint         = integer %s"n"

float          = nan-literal / inf-literal / finite-float
nan-literal    = %s"nan"
inf-literal    = [sign] %s"inf"
finite-float   = [sign] dec-int "." dec-int [exp]
               / [sign] dec-int exp
exp            = (%s"e" / %s"E") [sign] dec-int

decimal        = (finite-float / [sign] dec-int) %s"m"

string         = raw-string / triple-string / dquote-string
dquote-string  = DQUOTE *schar DQUOTE
triple-string  = 3DQUOTE *tchar 3DQUOTE
raw-string     = %s"r" DQUOTE *rchar DQUOTE

binary         = %s"b" DQUOTE (%s"base64:" *b64char
                              / %s"hex:" *hex-digit) DQUOTE

datetime       = "#" date [ %s"T" time [zone] ]
date           = 4DIGIT "-" 2DIGIT "-" 2DIGIT
time           = 2DIGIT ":" 2DIGIT ":" 2DIGIT ["." 1*9DIGIT]
zone           = %s"Z" / (("+" / "-") 2DIGIT ":" 2DIGIT)

symbol         = sym-start *sym-cont
sym-start      = ascii-letter / "_" / unicode-L
               / "+" / "-" / "*" / "/" / "<" / ">" / "=" / "!" / "?"
sym-cont       = sym-start / ascii-digit
ascii-letter   = %x41-5A / %x61-7A
ascii-digit    = %x30-39
unicode-L      = <any Unicode scalar whose General Category is L*
                  per the Unicode version pinned in §14>

keyword        = ":" kw-name
kw-name        = kw-start-plain *sym-cont
               / kw-start-sign kw-cont-nodigit *sym-cont
kw-start-plain = sym-start minus {"+", "-"}
kw-start-sign  = "+" / "-"
kw-cont-nodigit= sym-cont minus ascii-digit

list           = "[" *WS *(value *WS) "]"
map            = "{" *WS *(keyword *WS value *WS) "}"
sexpr          = "(" *WS symbol *(*WS value) *WS ")"

type-expr      = symbol / sexpr

WS             = SP / HTAB / LF / CR / ","
```

**Lexical ambiguity notes.**

- `-inf` is the infinity literal, not the symbol `inf` prefixed with
  `-`. `-0.5` is the float, not the symbol `0.5` prefixed with `-`.
  The lexer uses longest-match, with the special lowercase literals
  (`nan`, `inf`, `-inf`, `nil`, `true`, `false`) recognised before
  general symbols.
- Any following byte or symbol-continuation character after a
  numeric, datetime, or binary literal starts a new token only at
  one of the boundary characters listed in §4.3; otherwise the
  literal has not ended yet and the lexer reports a syntax error.
- `::` is a two-character lexical token. The grammar permits
  whitespace on either side of `::` via `annot = *WS "::" *WS
  type-expr`, but no byte may appear between the two colons of
  `::` itself.

---

## 7. Round-trip Rules

Weft defines two levels of canonical output.

- **Canonical** is the human-readable default. It MUST satisfy the
  invariants below. Two implementations' canonical outputs are NOT
  required to be byte-identical.
- **Strict canonical** is the subset used for digital signatures,
  content hashing, and other byte-exact use cases. All conforming
  implementations MUST emit the same bytes in this mode.

### 7.1. Shared canonical invariants

Every canonical output (both modes) MUST satisfy:

1. **Idempotence**: `format(parse(format(x))) == format(parse(x))`
   bytewise.
2. **Fidelity**: for every value category in §5, formatting followed
   by parsing yields a value equal (`≡`, §5.16) to the input.
3. **No lossy coercion**: an Integer remains an Integer; a BigInt
   remains a BigInt; a Decimal remains a Decimal; Symbol vs Keyword
   vs String are preserved; `-0.0` remains `-0.0` (sign of zero,
   §5.16).
4. **Line separator** is `\n`. No trailing whitespace on any line.
   Document ends with exactly one `\n`.
5. **UTF-8** throughout; no BOM.

### 7.2. Canonical (default)

- **Indentation**: 2 spaces per structural level. The formatter
  soft-wraps compound values when the inline form would exceed 80
  **Unicode code points** on a line (not UTF-8 bytes, not UTF-16
  units). Scalars never wrap.
- **Integers**: positive integers emit no `+` sign; negative
  integers emit `-`. Decimal only. Underscore separators and
  non-decimal bases (`0x`, `0o`, `0b`) are NOT re-emitted. (A
  reserved option `preserve_integer_format=true` is defined for
  future use but has no observable effect in 1.0; see §13.)
- **BigInt** emits the `n` suffix.
- **Float**: shortest decimal that round-trips to the same binary64
  value. Special values are `nan`, `inf`, `-inf` (lowercase, per
  §5.5). For non-strict canonical, the formatter MAY choose any
  shortest round-trip spelling; strict canonical fixes the choice
  (§7.3).
- **Decimal**: the exact textual form from the input is preserved,
  with two normalisations: (a) a leading `+` sign is dropped
  (`+1.5m` → `1.5m`), matching the Integer rule; (b) the exponent
  marker, if any, is lower-case `e` (`1E2m` → `1e2m`).
- **String** output form, in order of preference:
  1. If the value contains U+000A (LF), it MUST be emitted in
     one-line `"..."` form with `\n` escapes for each newline, even
     when `"""..."""` would be shorter. Multi-line (`"""..."""`)
     string values are **never re-indented** by the formatter;
     emitting a multi-line string inside a soft-wrapped compound
     would otherwise introduce leading spaces into the string
     content, silently changing the value. The one-line form avoids
     this entirely.
  2. If the value contains the sequence `"""` or ends in `"`,
     one-line `"..."` form is used with `\"` escapes.
  3. Otherwise, the one-line `"..."` form is used.

  Multi-line (`"""..."""`) output is reserved for a future stable
  layout that pins indentation; it is NOT emitted in Weft 1.0's
  canonical output. The lexer continues to accept it on input.
- **DateTime**: emitted in the shape in which it was parsed
  (date-only / naive / offset). Offset `+00:00` is emitted as `Z`.
  Fractional seconds: trailing zeros are stripped; an all-zero
  fractional (`.000`) is removed entirely, including the `.`
  (§5.9). Leap second `:60` is preserved as `:60` (§5.9).
- **Map**: keys appear in **input order** on round-trip.
  Non-strict canonical MUST NOT reorder keys.

A formatter MAY vary within these rules; two non-strict canonical
outputs for the same value may differ in whitespace and in the
choice between equally-shortest float spellings.

### 7.3. Strict canonical

Strict canonical is non-strict canonical with the following
additional rules. The intent is byte-exact output across all
conforming implementations for a given abstract value.

**Layout.**

- Compound values use a fixed single-line form; strict canonical
  does NOT wrap at 80 columns. Line length is unbounded. A single
  line is emitted for the whole document, followed by one `\n`.
- Items in Lists and Sexprs are separated by one SP (`U+0020`).
- Map entries are separated by one SP; within an entry, the
  keyword and value are separated by one SP.
- No indentation, no comments, no block comments, no raw strings.
- Multi-line strings (`"""..."""`) are not used in strict canonical;
  all strings use `"..."` with escapes.

**Keys sort order.**

- Maps are emitted with keys in **ascending Unicode code-point
  order** of the keyword name. This is a code-point comparison, not
  UTF-16 nor UTF-8 lexicographic (which happen to agree on BMP but
  differ on supplementary planes).

**Strings — escapes.**

- All strings use `"..."`. Every code point in the following set
  MUST be escaped using the shortest form from `\n`, `\t`, `\r`,
  `\0`, `\\`, `\"`, or `\u{HEX}` (hex is 1–6 UPPERCASE digits —
  `\u{9B}`, `\u{202E}` — with no zero padding):
  - C0 controls U+0000–U+001F
  - DEL U+007F
  - C1 controls U+0080–U+009F
  - BOM U+FEFF
  - BMP noncharacters U+FFFE and U+FFFF
  - Bidirectional override/isolate controls U+202A–U+202E and
    U+2066–U+2069
- All other code points are emitted literally (as valid UTF-8),
  including supplementary-plane letters.

**Integers / BigInt.**

- All integers use decimal, no separators, no `+` sign on positive
  values. `BigInt(0) ≡ Integer(0)` by §5.16, but emits `0n` (the
  `n` suffix is preserved to retain the host-type hint).

**Float — strict shortest with fixed tie-break.**

- Non-finite values emit as `nan`, `inf`, `-inf`.
- Zero: emit `0.0` for `+0.0`, `-0.0` for the negative zero bit.
- For finite non-zero `f`: emit the **shortest decimal that
  round-trips to `f`**. When two decimal strings of equal length
  both round-trip (a "shortest tie"), pick the one whose exact
  decimal value is **closest to the exact binary value of `f`**;
  if the two are exactly equidistant, pick the one with the even
  last significant digit. (The closest-value rule is what CPython's
  ``repr``, V8's ``toString``, and Go's ``strconv`` shortest mode
  produce — verified by differential fuzzing; an "always even"
  banker's rule does NOT match those hosts and must not be used.)
- Choice between fixed-point and exponent:
  - If `|f|` lies in `[1e-4, 1e16)`, emit fixed-point form with
    at least one digit on each side of the `.` (e.g. `1.0`,
    `0.0001`, `12345678901234.0`, `9.999999999999999e15`
    for 1e16 exactly, etc.).
  - Otherwise emit exponent form `D.DDDe±NN`: lowercase `e`,
    explicit sign, exponent magnitude zero-padded to at least two
    digits (`1e-07`, `1e+20`, `1e+100`).
- The leading mantissa has no `+` sign.

**Decimal.**

- Emitted exactly as its canonical text (§5.6 / §7.2): leading
  `+` stripped, exponent marker lowercase `e`, significant digits
  preserved.

**DateTime.**

- Shape preserved (date-only / naive / offset).
- UTC offset `+00:00` emitted as `Z`.
- Leap second `:60` preserved (§5.9).
- Fractional seconds canonicalised per §7.2.

**Symbols / Keywords.**

- Emitted literally in UTF-8. Must consist of the character class
  defined in §5.10 and §5.11 (Unicode `L` only, no `Nd`/`No`/`Nl`).

**Documents and separators.**

- No leading whitespace, no trailing whitespace, no blank lines,
  single terminating `\n`.

---

## 8. Type Annotations

Type annotations are a syntactic feature of Weft. The format itself
assigns no meaning beyond the pair (value, type-expression). An
application layered on top of Weft (such as the Lattice language)
provides the type system.

A parser MUST preserve all type annotations. A parser invoked with
`strip_types=true` discards them, returning only the inner values.

Type expressions are either Symbols or Sexprs whose tag is a Symbol.
Keywords, Integers, Lists, Maps, Strings, and other scalars are NOT
valid type expressions.

**Chained annotations.** A value may carry any number of `::T`
annotations: `42::Int::Any::Positive` parses as
`Typed(Typed(Typed(42, Int), Any), Positive)`. Each `::` is a
distinct wrapper and counts as one level of structural depth
against `max_depth` (§9) in addition to whatever nesting its
type expression contains.

**Depth counting.** The `max_depth` limit counts *structural
levels in the resulting value tree*, not the input's balanced
bracket count. Each List, Map, Sexpr, and `::T` annotation adds
one level; a scalar adds none. Precisely:

```
depth(scalar)             = 0
depth(List xs)            = 1 + max(depth(x) for x in xs; 0)
depth(Map m)              = 1 + max(depth(v) for v in m.values; 0)
depth(Sexpr tag items)    = 1 + max(depth(it) for it in items; 0)
depth(Typed value type)   = 1 + max(depth(value), depth(type))
```

Worked examples:

```
0                 ; depth 0 (scalar)
[0]               ; depth 1
[[[[0]]]]         ; depth 4
0::T              ; depth 1 (one Typed)
0::T::U           ; depth 2 (two Typeds)
0::(T [[0]])      ; depth 4
    ; Typed(1) → Sexpr(1) → List(1) → List(1), scalar at bottom
{:a [0]}          ; depth 2 (Map + List)
```

A conforming parser MUST reject any input whose completed tree
depth would exceed `max_depth`, including depth contributed by
annotations and by nested data inside a type expression.

**Identifier characters.** Symbol and Keyword names use only
ASCII operator / underscore characters and ASCII digits for the
continue positions, plus any Unicode Letter (General Category
`L`) above U+007F for both start and continue positions. Unicode
digit categories (`Nd`, `No`), fractions, Roman-numeral forms,
and other non-letter blocks are NOT permitted — including the
full-width ASCII digit block (`U+FF10..U+FF19`), the circled and
parenthesised digit forms, and the vulgar fractions. This keeps
identifier acceptance byte-for-byte identical to what the
canonical formatter can emit.

---

## 9. Resource Limits

Every parser MUST accept and enforce the following limits. The
defaults are mandatory lower bounds for conforming parsers; a
parser MAY offer higher limits via explicit configuration.

| Parameter             | Default      | Hard max        |
|-----------------------|--------------|-----------------|
| `max_depth`           | 128          | 1024            |
| `max_size_bytes`      | 10,485,760   | 1,073,741,824   |
| `max_items_per_coll`  | 1,048,576    | 16,777,216      |
| `max_string_bytes`    | 10,485,760   | 1,073,741,824   |
| `max_int_digits`      | 10,000       | 1,000,000       |
| `max_total_items`     | 10,000,000   | 100,000,000     |

Rules:

- Exceeding any limit MUST cause an error with a specific error
  code (`WEFT_LIMIT_DEPTH`, `WEFT_LIMIT_SIZE`, etc.).
- Limits are checked incrementally. A parser MUST NOT allocate a
  buffer sized to the input before validating the input is within
  limits.
- **Depth** is defined by §8. It counts structural levels in the
  value tree (List/Map/Sexpr/Typed add one; scalars add none), not
  the number of open brackets in the source. The root value's
  depth is 0 for a scalar, 1 for `[0]`, 2 for `{:a [0]}`, etc.
- **Every entry point** that constructs a Weft tree from input
  MUST enforce these limits with the SAME codes: `parse`,
  `parse_stream`, `from_json`, `from_json_lossless`, schema
  validation, and any other tree-building API. In particular,
  limits apply to lossless JSON decoding (§11.2) and to type
  expressions attached by `::`; neither path is exempt.
- `max_items_per_coll` applies independently to Lists, Maps, and
  the item count of a single Sexpr.
- `max_total_items` applies to the whole document and prevents DoS
  via a wide-but-flat structure.
- `max_int_digits` is a budget expressed in *decimal digits*.
  For a non-decimal literal, the parser MUST check against
  `ceil(len * log10(base))` — an upper bound on the decimal digit
  count — rather than the actual decimal length (which would
  require performing the conversion first). This bound is
  conservative: `0x1` with `max_int_digits=1` is rejected because
  `ceil(1 * log10(16)) = 2 > 1`, even though the decimal value
  (`1`) has one digit. Applications that care about the exact
  decimal length MUST perform the parse and then inspect.

---

## 10. Security Considerations

Weft parsers MUST implement all of the following.

### 10.1. No code execution

A Weft parser never:

- Instantiates host-language objects by name supplied in the
  document (unlike YAML's `!!python/object/apply`).
- Calls host-language constructors with user data (unlike
  pickle's `__reduce__`).
- Resolves tags, prefixes, or URIs (unlike XML's external
  entities and YAML's tags).
- Opens files, network sockets, or environment variables.
- Evaluates expressions.

A parser invocation on an untrusted document is as safe as decoding
an untrusted JPEG into pixels: it reads data and nothing more.

### 10.2. Bounded resources

All limits in §9 are mandatory. A parser is non-conforming if any
limit can be set to "unlimited" (`-1`, `None`, infinity) in its
public API.

### 10.3. UTF-8 strict mode

All UTF-8 validation per §3 is mandatory. In particular:

- Overlong encodings MUST be rejected.
- Lone surrogate halves MUST be rejected.
- U+FEFF (BOM) MUST be rejected if it appears anywhere in the
  document, including the beginning.
- Null bytes (U+0000) MUST be rejected outside string literals.

### 10.4. Hash collision resistance

Map parsing MUST use associative data structures that are resistant
to algorithmic-complexity attacks on key insertion. Any of the
following are acceptable:

- Hash maps with per-process randomised seeding (Python 3's `dict`
  default, Rust's `std::collections::HashMap` with `RandomState`,
  Go's `map`).
- Hash maps with constant-time quadratic or Robin-Hood probing and
  per-key tag mitigations (V8's internal object property stores and
  `Map`, including JavaScriptCore's; these do not expose a seed but
  resist adversarial collision patterns in practice).
- Ordered tree-based maps whose comparison function is deterministic
  but whose insertion cost is O(log n) regardless of key
  distribution (e.g. `BTreeMap`).

A parser that uses fixed-seed open-addressing hashing on untrusted
input is non-conforming. Implementations SHOULD document which
mitigation they rely on.

### 10.5. Control characters in comments

The character classes rejected inside comments are defined in §4.2.
In addition to the C0 controls listed there, DEL (U+007F), C1
controls (U+0080–U+009F including CSI U+009B), and bidirectional
override/isolate controls (U+202A–U+202E, U+2066–U+2069) MUST be
rejected inside comments. This prevents terminal escape sequence
injection and "Trojan Source"-style attacks when a document is
later displayed in a tty or editor.

### 10.6. Integer overflow

Parsers MUST use arbitrary-precision integers internally during
parsing. The `max_int_digits` limit bounds the computation. A parser
MUST NOT use a fixed-width integer type (int32, int64) for length
counts, nesting depth, or digit counts.

### 10.7. Allocation discipline

A parser MUST NOT pre-allocate buffers sized by document
declarations. In Weft there are no size declarations (lists and
maps are terminator-delimited), so this reduces to: never
pre-allocate based on claims in the input.

### 10.8. Threat model (informative)

Weft is designed to be safe to parse untrusted input in a process
that handles no secrets. Weft is NOT designed to be safe against
side-channel attacks (timing, cache), nor against malicious
formatters that produce documents designed to compromise a
downstream consumer that is more permissive than this spec. Users
with secret material (keys, PII) in the same process as the parser
should treat parsing as any other input-handling boundary.

### 10.9. Reporting

Security issues in a conforming implementation should be reported
privately to the implementation's maintainer via the mechanism its
`SECURITY.md` describes. The Weft specification itself has no
runtime, so specification-level issues (ambiguities that enable
attack) should be filed as issues against the specification
repository.

---

## 11. JSON Interoperability

A conforming implementation MUST provide a `to_json` function that
produces a lossy mapping from Weft to JSON, and a `from_json`
function that produces a Weft tree from JSON input.

Two JSON representations are defined:

- **Lossy** (§11.1): a best-effort mapping to idiomatic JSON that
  loses type distinctions (e.g. Symbol vs Keyword vs String).
  Intended for display and for consumption by JSON tools that do
  not understand Weft.
- **Lossless** (§11.2): a tagged mapping using JSON objects with
  reserved `$`-prefixed keys. Round-trips Weft → JSON → Weft
  byte-identical (modulo canonical formatting defined in §7) for
  all Weft values.

Implementations MUST provide both. The lossless form MUST be the
default for `to-json` / `from-json` round-trip operations.

### 11.1. Lossy (idiomatic) JSON

| Weft              | JSON                                              |
|-------------------|---------------------------------------------------|
| `nil`             | `null`                                            |
| `true`, `false`   | `true`, `false`                                   |
| `Integer` (safe)  | `number`                                          |
| `Integer` (big)   | `string` (decimal representation)                 |
| `BigInt`          | `string` (decimal representation)                 |
| `Float` finite    | `number`                                          |
| `Float` nan/inf   | `string` (`"nan"`/`"inf"`/`"-inf"`)               |
| `Decimal`         | `string`                                          |
| `String`          | `string`                                          |
| `Binary`          | `string` (`"base64:..."`)                         |
| `DateTime`        | `string` (RFC 3339)                               |
| `Symbol`          | `string` (prefixed with `#sym:`)                  |
| `Keyword`         | `string` (prefixed with `#kw:`)                   |
| `List`            | `array`                                           |
| `Map`             | `object`                                          |
| `Sexpr`           | `object` with `"$sexpr": [tag, items...]`         |
| `TypedValue`      | `object` with `"$typed": {"v": ..., "t": ...}`    |

The "safe range" for JSON integers is -(2^53-1) to 2^53-1
(JavaScript's `Number.MAX_SAFE_INTEGER`). Weft `Integer` values
outside that range MUST be encoded as JSON strings in lossy mode to
avoid precision loss in JSON consumers that use IEEE 754 doubles.

Lossy mode is a one-way projection. Weft → JSON → Weft via lossy
mode is NOT guaranteed to round-trip: Symbol/Keyword/String collapse
to String, Integer/BigInt/Decimal/Float collapse to Number or
String, DateTime collapses to String, etc. Lossy mode is for
display and interoperability with JSON tools; it is NOT suitable
for data interchange where fidelity matters.

**Prefix collision.** The `#sym:` and `#kw:` prefixes used for
Symbol/Keyword encoding are a convention, not a reversible mapping.
A Weft `String("#sym:x")` and a Weft `Symbol("x")` both encode to
the JSON string `"#sym:x"`. Lossy decoders (§11.3) MUST NOT reverse
this: on decode, a JSON string becomes a Weft `String` with its
exact text, prefix included. Applications that need the distinction
MUST use the lossless form (§11.2).

### 11.2. Lossless (tagged) JSON

The lossless form uses JSON objects with a single `$`-prefixed key
as **type tags**. Each tag identifies the Weft type and carries
enough information to reconstruct the original value byte-for-byte
after canonical re-formatting (§7). This form is **strict**: every
compound Weft value is tagged, so parsing a plain JSON array or
object as a Weft value is unambiguous.

Reserved tag names:

| Tag        | Weft type         | Payload                                  |
|------------|-------------------|------------------------------------------|
| `$sym`     | `Symbol`          | `string` (symbol name)                   |
| `$kw`      | `Keyword`         | `string` (keyword name, no `:`)          |
| `$bigint`  | `BigInt`          | `string` (decimal `-?[0-9]+`)            |
| `$int-big` | `Integer` (outside JSON safe range) | `string` (decimal `-?[0-9]+`) |
| `$dec`     | `Decimal`         | `string` (canonical Decimal text, §7)    |
| `$str`     | `String`          | `string`                                 |
| `$bin`     | `Binary`          | `{"enc": "base64"\|"hex", "data": string}` |
| `$dt`      | `DateTime`        | `string` (canonical RFC 3339, §7)        |
| `$float`   | `Float`           | `string` — `"nan"`, `"inf"`, `"-inf"`, or canonical finite float text (§7) |
| `$sexpr`   | `Sexpr`           | `array` `[tag_name_string, items...]`    |
| `$typed`   | `TypedValue`      | `{"v": <value>, "t": <type>}`            |
| `$list`    | `List`            | `array` (items encoded lossless)         |
| `$map`     | `Map`             | `object` (keys are JSON strings; see §11.2.3) |

#### 11.2.1. Encoding rules

1. **Nil / Boolean / Integer (safe)**: `nil` → JSON `null`;
   `true`/`false` → JSON `true`/`false`; a Weft `Integer` within
   the JSON safe range → JSON `number` (integer form only; see
   rule 2). These are the only Weft values that use plain JSON
   scalars; every other value is tagged.

2. **Integer**: encoded as a JSON `number` when within the JSON
   safe range (-(2^53-1) to 2^53-1), else as `{"$int-big": "..."}`.
   Plain JSON `number` on the wire therefore means Weft `Integer`
   (within the safe range). Lossless decoders MUST reject bare
   JSON integers outside the safe range (producer MUST have emitted
   `$int-big`).

3. **BigInt**: ALWAYS encoded as `{"$bigint": "..."}`. The `n`
   suffix in Weft syntax is a presentation hint (§5.4), but the
   lossless JSON form uses a distinct tag because host runtimes
   use distinct native types for `Integer` and `BigInt`
   (JavaScript's `number` vs `BigInt`, Python's `int` and the Weft
   `BigInt` wrapper, etc.). Preserving the tag is what carries that
   host-type hint across a JSON round-trip; the equality rule in
   §5.16 (`42 ≡ 42n`) is unaffected.

4. **Float**: ALWAYS encoded as `{"$float": "..."}`. The payload
   is:
   - `"nan"`, `"inf"`, or `"-inf"` for non-finite values, or
   - the strict canonical finite float text (§7.3), including the
     sign for negative zero (`"-0.0"`).

   Plain JSON `number` is reserved for `Integer` (rule 2);
   this is what makes `Int` vs `Float` survive the round trip.
   Lossless decoders MUST reject bare JSON floats, defined
   **lexically** as: a JSON `number` token that contains `.`, `e`,
   or `E`. The decoder MUST NOT inspect only the numeric value
   (otherwise `1.0` and `1` would be indistinguishable after a
   lossy parse to float64). Implementations whose JSON parser does
   not expose the original token text MUST use a JSON tokeniser
   or `UseNumber`-style option that does.

5. **Decimal**: `{"$dec": "..."}`. The payload is the canonical
   Decimal text (§7); the `m` suffix is NOT present in the payload.

6. **String**: ALWAYS encoded as `{"$str": "..."}`. Plain JSON
   `string` on the wire is reserved for payload content inside
   `$sym`, `$kw`, `$bigint`, `$int-big`, `$dec`, `$dt`, `$float`,
   `$bin.data`, `$bin.enc`, and `$sexpr` tag names; it is NOT a
   lossless Weft `String`. This prevents a String payload like
   `"#sym:x"` from being silently promoted to a Symbol.

7. **Binary**: `{"$bin": {"enc": "base64", "data": "..."}}` or
   `{"$bin": {"enc": "hex", "data": "..."}}`. The `$bin` payload
   MUST contain exactly the keys `enc` and `data`.

8. **DateTime**: `{"$dt": "..."}` with payload being the strict
   canonical form (§7.3, §5.9). All three shapes are preserved
   (date-only, naive, offset). The leap second `:60` is preserved
   (§5.9) — the lossless form MUST NOT roll `:60` into the next
   day, so a parsed leap-second value round-trips to itself.

9. **Symbol / Keyword**: `{"$sym": "name"}` / `{"$kw": "name"}`.
   The payload is the bare name without the Weft prefix (no `#`,
   no `:`).

10. **Sexpr**: `{"$sexpr": [tag_name, item1, item2, ...]}`. The
    first element is the tag symbol's **name** as a plain JSON
    string (not a `$sym` wrapper, for compactness). Items are
    encoded lossless.

11. **TypedValue** (`v::T`): `{"$typed": {"v": <value>, "t": <type>}}`.
    Both `v` and `t` are encoded lossless; `t` MUST resolve to a
    Symbol or Sexpr. Chained annotations (`v::T1::T2`) nest:
    `{"$typed": {"v": {"$typed": {"v": V, "t": T1}}, "t": T2}}`.

12. **List**: ALWAYS encoded as `{"$list": [...]}`. Plain JSON
    arrays are reserved for `$sexpr` payloads and for the inner
    array of `$list` itself; they are NOT a lossless Weft `List`.

13. **Map**: ALWAYS encoded as `{"$map": {...}}`. See §11.2.3 for
    key handling.

14. **Rejection of unknown tags**: a JSON object whose single key
    begins with `$` but is NOT one of the reserved tag names above
    MUST be rejected by lossless decoders. This reserves the
    `$`-prefix namespace for future minor versions (see §12).

15. **Object shape**: a lossless JSON object MUST have EXACTLY one
    key, and that key MUST be one of the reserved tags. The empty
    object `{}` is not a valid lossless form; multi-key objects
    are not valid lossless forms.

#### 11.2.2. Lossless identity

For any Weft value `v`:

```
parse(format(from_json_lossless(to_json_lossless(v)))) == v
```

where `==` is Weft equality (§5.4). Furthermore, after canonical
formatting (§7):

```
format_canonical(from_json_lossless(to_json_lossless(v))) ==
  format_canonical(v)
```

byte-for-byte. This is the lossless round-trip identity property;
all reference implementations MUST satisfy it, and the differential
fuzz MUST test it.

#### 11.2.3. `$map` key handling

By §5.13, Weft Map keys are ALWAYS Keywords. The `$map` payload
takes advantage of this: each JSON object key is the keyword's
**name** (without the leading `:` of the Weft syntax), spelled
exactly as it would be after the `:` in Weft source.

```
;; Weft
{:name "Ada" :age 42}

;; Lossless JSON
{"$map": {"name": {"$str": "Ada"}, "age": 42}}
```

Decoders MUST validate that every `$map` key is a valid Weft
keyword name (same character class as a Symbol, §5.10); keys that
are not valid keyword names MUST be rejected. This keeps the
lossless form in sync with what Weft maps can natively hold.

(Future minor versions may add a tagged form for non-keyword-keyed
maps; see §12.)

#### 11.2.4. Lossless to Weft

The reverse direction accepts ONLY the strict tagged forms above
plus the untagged plain-JSON scalars `null`, `true`, `false`, and
`number` (JSON-safe `Integer` only). Any other plain JSON form —
bare string, bare array, bare object (even with no `$`-tag), bare
non-integer number, bare out-of-range integer — MUST be rejected
in strict lossless mode.

Implementations MAY expose a non-default loose mode that accepts
plain JSON structures (strings become `String`, arrays become
`List`, objects become `Map` with keyword keys per §11.3), but
this loose mode is OUTSIDE the lossless-identity guarantee of
§11.2.2.

#### 11.2.5. Duplicate keys

JSON itself does not forbid duplicate object keys (RFC 8259 §4
"names within an object SHOULD be unique"), but most JSON libraries
silently drop duplicates to last-write. Weft Map rejects duplicates
at parse time (§5.13); the lossless JSON path MUST preserve that
behaviour.

Lossless decoders MUST reject duplicate keys in:

- A `$map` payload object (would otherwise bypass §5.13).
- The lossless tagged object itself (`{"$str":"a","$str":"b"}`
  must be rejected, not silently collapsed to `$str = "b"`).
- The `$bin` payload (`{"enc":..., "data":..., "enc":...}` must
  be rejected).
- The `$typed` payload (`{"v":..., "t":..., "v":...}` must be
  rejected).

An implementation whose JSON parser silently collapses duplicates
MUST use a token-level API (Python's `object_pairs_hook`, Go's
`json.Decoder` tokenization, Rust `serde_json`'s raw-value or
custom deserializer, JavaScript's reviver + external detection)
to detect duplicates before Weft construction. A `WeftSyntaxError`
with code `WEFT_SYNTAX_JSON_DUPKEY` MUST be raised.

### 11.3. Lossy JSON to Weft

The reverse of §11.1:

| JSON              | Weft                                              |
|-------------------|---------------------------------------------------|
| `null`            | `nil`                                             |
| `true`/`false`    | `true`/`false`                                    |
| `number` integer  | `Integer`                                         |
| `number` float    | `Float`                                           |
| `string`          | `String`                                          |
| `array`           | `List`                                            |
| `object`          | `Map` (keys become keywords)                      |

The distinction between `number integer` and `number float` is
**lexical**, not value-based: a number token is an *integer* iff it
contains no `.`, `e`, or `E`, otherwise it is a *float*. So JSON
`1` → `Integer(1)` and JSON `1.0` → `Float(1.0)`, and the lossy
decoder MUST preserve this distinction. Implementations whose JSON
library collapses both to a single numeric host type (e.g. Go's
`encoding/json` → `float64` by default) MUST use a token-level API
(e.g. Go's `json.Decoder.UseNumber`) to see the original lexical
shape.

JSON object keys become Weft keywords. If a JSON object key is not
a valid Weft keyword name (§5.11 — ASCII letters/operators plus
Unicode `L` only, no digit-start, no sign+digit start), the parser
MUST either reject the input or map it to a string-keyed
representation via a documented host-side convention. The default
is to reject with `WEFT_TYPE_JSON_KEY`.

JSON object key order is preserved in the resulting Map: lossy
decoders MUST iterate keys in their encounter order (using a
token-level API when the host JSON library would otherwise sort or
hash them, as in Go).

Non-finite floats (`nan`, `inf`, `-inf`) cannot appear in valid
JSON. Lossy decoders MUST reject the non-standard JSON extensions
`NaN`, `Infinity`, `-Infinity` by default (as emitted by some
JavaScript and Python JSON libraries). An opt-in flag MAY be
provided to accept them, but the default MUST be rejection with
`WEFT_SYNTAX_JSON`.

### 11.4. Lossiness summary

Weft → JSON → Weft round-trips are:

- **Lossless mode (§11.2)**: byte-identical after canonical
  formatting (§7). All types preserved. REQUIRED default for
  data-interchange operations.
- **Lossy mode (§11.1 / §11.3)**: lossy for Symbol/Keyword/String,
  Integer/BigInt/Decimal, DateTime/String, Binary/String. Suitable
  for display and for one-way export to JSON-only tools.

JSON tools that are unaware of the lossless tag conventions will
see tagged objects as plain `{"$...": ...}` objects; this is a
deliberate graceful-degradation property.

---

## 12. Reserved Syntax

The following syntactic forms are reserved for future minor versions
of Weft and MUST be rejected by 1.0 parsers:

- A `#` followed by any byte other than an ASCII digit (opening a
  DateTime, §5.9) or `|` (opening a block comment, §4.2). All other
  `#`-prefixed forms are reserved; this includes `#uuid:`, `#url:`,
  `#ref:`, `#!`, and so on. Future minor versions MAY define any
  of these; 1.0 parsers MUST reject them all with
  `WEFT_RESERVED_HASH`.
- Symbols beginning with `$` (reserved for schema-defined magic
  values).
- Symbols beginning with `@` (reserved for annotations).
- Numeric literal suffixes other than `n` (BigInt) and `m`
  (Decimal). `42i32`, `42u64`, `42f32` are reserved; they MUST be
  rejected in 1.0 with `WEFT_RESERVED_NUMBER_SUFFIX`.
- Any character outside the alphabets defined in §4 and §5
  appearing as a top-level token.
- A JSON lossless tag (§11.2) with a `$`-prefixed name that is not
  in the reserved tag table. Reserved for future minor versions.

A 1.0 parser that accepts any of these is non-conforming.

---

## 13. Versioning and Compatibility

Weft follows semantic versioning:

- **Patch version**: no textual or semantic changes to the
  specification. Patch versions exist for clarifications only.
- **Minor version** (1.x): adds new syntactic forms (new reserved
  tag types, new numeric suffixes, new comment forms) in a way
  that is backward-compatible. Documents valid under 1.0 remain
  valid and parse to the same abstract value under 1.x. New forms
  are among those listed in §12.
- **Major version** (2.x): any backward-incompatible change.

A minor-version parser MUST accept documents produced by any older
minor-version formatter in the same major series. A minor-version
formatter MAY produce documents that older parsers reject (those
would use the new syntactic forms).

Documents SHOULD NOT contain a version declaration. The format is
designed so that a value in the document uniquely identifies the
minimum version required; a parser rejecting a value signals that
to the application. If a document-level version pin is required,
applications may use the `(weft-version "1.0" ...)` convention
inside a surrounding S-expression; this is not part of 1.0 itself.

---

## 14. IANA Considerations

**Unicode version.** Identifier character classes (§5.10, §6) are
defined against **Unicode 15.0**. An implementation MAY use a newer
Unicode version for the `L*` lookup; because new Unicode versions
only add letter assignments to previously-unassigned code points,
documents restricted to assigned Unicode 15.0 letters parse
identically everywhere. Producers that need byte-stable identifier
acceptance across implementations built on different Unicode
versions SHOULD restrict identifiers to ASCII. A future minor
version of this specification may re-pin the Unicode version.

Suggested media type: `application/weft`.

Suggested file extensions: `.weft` for canonical text; `.weftl` for
line-delimited streaming (one top-level value per line).

Encoding: always UTF-8.

---

## Appendix A: Example Document

```weft
; Example Weft document showing most value categories

{:name "Shift roster"
 :version 1
 :week-starting #2026-11-02
 :entries [
   {:person "Toshiki"
    :shift :day
    :hours 8.0
    :notes "Normal rotation"}
   {:person "Alex"
    :shift :night
    :hours 10.5m          ; decimal for payroll
    :notes nil}
   {:person "Priya"
    :shift :on-call
    :hours 0.0
    :notes """
Multiple lines
are permitted here
"""}
 ]
 :budget 1_250_000::Int
 :coverage-fn (fn match-coverage [(name String) (date DateTime)] Bool Pure
                (and (not-empty? (lookup name))
                     (within-range date)))
 :last-updated #2026-11-02T06:00:00+09:00}
```

## Appendix B: Non-goals

Weft 1.0 explicitly does NOT provide:

- A standard way to reference anchors (YAML `&`/`*`). The format
  is a tree; cycles are not representable. Applications requiring
  reference semantics should encode them as explicit keys and
  resolve them at a layer above Weft.
- A standard way to embed arbitrary binary opaquely with no
  encoding overhead. Binary data must be encoded as base64 or hex;
  the lexical form is text.
- Streaming output format other than line-delimited (`.weftl`).
- A standard way to attach schemas inline. See the companion
  Weft-Schema document.
- A standard way to embed executable code or references to code.
  By design.

## Appendix C: Comparison with JSON

| Aspect                    | JSON              | Weft 1.0                     |
|---------------------------|-------------------|------------------------------|
| Comments                  | No                | Line + nestable block        |
| Trailing commas           | No                | Yes (commas are whitespace)  |
| Multi-line strings        | No                | Triple-quoted                |
| Raw strings               | No                | `r"..."`                     |
| Integer precision         | ~2^53             | Arbitrary                    |
| Decimal (exact) numbers   | No                | `3.14m`                      |
| Date-time literal         | No                | `#2026-11-02T...`            |
| Binary blobs              | No                | `b"base64:..." / b"hex:..."` |
| NaN / infinity            | Not standard      | Literal `nan`, `inf`, `-inf` |
| Map key types             | String            | Keyword                      |
| Type annotations          | No                | `value::Type`                |
| Tagged nodes (sexpr)      | No                | `(tag items...)`             |
| Hard-coded parse limits   | Implementation    | Mandated (§9)                |
| Code execution risk       | None (standard)   | None (by spec)               |

---

End of specification.
