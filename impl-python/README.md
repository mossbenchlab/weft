# weft-format

Python reference implementation of **Weft 1.0**, an AI-first textual
data format: rich types (BigInt, Decimal, Binary, DateTime, Symbol,
Keyword, typed values), byte-identical strict-canonical output across
implementations (safe to hash and sign), a lossless JSON bridge, and
parsers hardened against hostile input.

```python
import weft

v = weft.parse('{:name "weft" :precise 0.1m :big 9007199254740993n}')
print(weft.format_value(v, weft.FormatOptions(strict_canonical=True)))
j = weft.dumps_json(v)            # lossless JSON encoding
assert weft.equals(v, weft.loads_json(j))
```

The import name is `weft` (the distribution is `weft-format` because
the name `weft` was taken on PyPI by an unrelated project).

Specification, three more reference implementations (TypeScript, Rust,
Go), and the conformance suite:
https://github.com/mossbenchlab/weft
