# weft-format (Rust)

Rust reference implementation of **Weft 1.0**, an AI-first textual data
format: rich types (BigInt, Decimal, Binary, DateTime, Symbol, Keyword,
typed values), byte-identical strict-canonical output across implementations
(safe to hash and sign), a lossless JSON bridge, and parsing hardened
against hostile input.

The crate is published as `weft-format` (the name `weft` was taken by an
unrelated project); the library is used as `weft`:

```rust
use weft::{parse, format_value, FormatOptions, loads_json_lossless, to_json_lossless, equals, Limits};

let v = parse(r#"{:name "weft" :precise 0.1m :big 9007199254740993n}"#)?;
let canonical = format_value(&v, FormatOptions { strict_canonical: true, ..Default::default() });
let json = serde_json::to_string(&to_json_lossless(&v))?;
assert!(equals(&v, &loads_json_lossless(&json, Limits::default())?));
```

Specification, three more reference implementations (Python, TypeScript,
Go), and the conformance suite: <https://github.com/mossbenchlab/weft>
