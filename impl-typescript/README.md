# weft-format (TypeScript)

TypeScript reference implementation of **Weft 1.0**, an AI-first textual
data format: rich types (BigInt, Decimal, Binary, DateTime, Symbol,
Keyword, typed values), byte-identical strict-canonical output across
implementations (safe to hash and sign), a lossless JSON bridge, and
parsing hardened against hostile input.

The package is published as `weft-format` (the name `weft` was taken by
an unrelated project). Zero runtime dependencies; ships with type
declarations.

```ts
import { parse, formatValue, toJsonLossless, fromJsonLossless, equals } from "weft-format";

const v = parse('{:name "weft" :precise 0.1m :big 9007199254740993n}');
const canonical = formatValue(v, { strict_canonical: true });
const json = JSON.stringify(toJsonLossless(v));
console.assert(equals(v, fromJsonLossless(JSON.parse(json))));
```

Specification, three more reference implementations (Python, Rust, Go),
and the conformance suite: <https://github.com/mossbenchlab/weft>
