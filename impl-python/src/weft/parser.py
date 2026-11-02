"""Weft parser.

The parser consumes a token stream from ``lexer.Lexer`` and produces a
Python-native tree (per ``types.py``).

Parsing is streaming-friendly: nothing is pre-allocated based on claims
in the input, and resource limits (depth, items, total) are checked
incrementally.
"""
from __future__ import annotations

from typing import Any, Iterator, List, Optional

from .errors import WeftSyntaxError, WeftLimitError
from .lexer import (
    Lexer, Token,
    TK_LPAREN, TK_RPAREN, TK_LBRACK, TK_RBRACK, TK_LBRACE, TK_RBRACE,
    TK_COLONCOLON, TK_INT, TK_BIGINT, TK_FLOAT, TK_DECIMAL, TK_STRING,
    TK_BINARY, TK_DATETIME, TK_BOOL, TK_NIL, TK_NAN, TK_INF, TK_NEG_INF,
    TK_SYMBOL, TK_KEYWORD,
)
from .limits import Limits, DEFAULT_LIMITS
from .types import Keyword, Sexpr, Symbol, Typed


_SCALAR_TOKENS = frozenset((
    TK_INT, TK_BIGINT, TK_FLOAT, TK_DECIMAL, TK_STRING, TK_BINARY,
    TK_DATETIME, TK_BOOL, TK_NIL, TK_NAN, TK_INF, TK_NEG_INF,
))


class _Parser:
    """Streaming parser.

    The parser pulls tokens on demand from an iterator rather than
    consuming a pre-built list, so a resource-limit violation can abort
    the lexer mid-stream without the whole token stream (and its memory
    footprint) having been realised. The single-token look-ahead
    required by the grammar is held in ``_lookahead``.
    """

    def __init__(self, tokens: Iterator[Token], limits: Limits, source_text: str | None = None) -> None:
        self._tokens = iter(tokens)
        self._lookahead: list[Token] = []  # at most 1 element
        self._last_tok: Optional[Token] = None  # for error context past EOF
        self.limits = limits
        self.total_items = 0
        self.depth = 0
        self.source_text = source_text  # for char→byte offset conversion

    # --- token stream helpers ---

    def peek(self) -> Token | None:
        if not self._lookahead:
            try:
                self._lookahead.append(next(self._tokens))
            except StopIteration:
                return None
        return self._lookahead[0]

    def advance(self) -> Token:
        if self._lookahead:
            tok = self._lookahead.pop(0)
        else:
            tok = next(self._tokens)
        self._last_tok = tok
        return tok

    def _byte_offset_for(self, tok: Token) -> int | None:
        """Prefer the token's own byte_offset (set by the lexer); fall back
        to computing from source text when the token predates that field."""
        if getattr(tok, "byte_offset", -1) >= 0:
            return tok.byte_offset
        if self.source_text is None:
            return None
        try:
            return len(self.source_text[:tok.offset].encode("utf-8", errors="strict"))
        except Exception:
            return None

    def error(self, msg: str, tok: Token | None = None, code: str = "WEFT_SYNTAX") -> WeftSyntaxError:
        if tok is None:
            tok = self.peek() or self._last_tok
            if tok is None:
                return WeftSyntaxError(msg, code=code)
        return WeftSyntaxError(
            msg, line=tok.line, column=tok.column,
            offset=tok.offset, byte_offset=self._byte_offset_for(tok),
            code=code,
        )

    def _bump_total(self) -> None:
        self.total_items += 1
        if self.total_items > self.limits.max_total_items:
            raise WeftLimitError(
                f"document has more than max_total_items="
                f"{self.limits.max_total_items} values",
                code="WEFT_LIMIT_TOTAL",
            )

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > self.limits.max_depth:
            raise WeftLimitError(
                f"nesting depth exceeds max_depth={self.limits.max_depth}",
                code="WEFT_LIMIT_DEPTH",
            )

    def _leave(self) -> None:
        self.depth -= 1

    # --- grammar ---

    def parse_value(self) -> Any:
        tok = self.peek()
        if tok is None:
            raise WeftSyntaxError(
                "unexpected end of input while parsing value",
                code="WEFT_SYNTAX_EOF",
            )

        self._bump_total()
        value: Any

        if tok.kind in _SCALAR_TOKENS:
            self.advance()
            if tok.kind == TK_INT:
                value = tok.value
            elif tok.kind == TK_BIGINT:
                value = tok.value  # BigInt wrapper
            elif tok.kind == TK_FLOAT:
                value = tok.value
            elif tok.kind == TK_DECIMAL:
                value = tok.value
            elif tok.kind == TK_STRING:
                value = tok.value
            elif tok.kind == TK_BINARY:
                value = tok.value
            elif tok.kind == TK_DATETIME:
                value = tok.value
            elif tok.kind == TK_BOOL:
                value = tok.value
            elif tok.kind == TK_NIL:
                value = None
            elif tok.kind == TK_NAN:
                value = float("nan")
            elif tok.kind == TK_INF:
                value = float("inf")
            elif tok.kind == TK_NEG_INF:
                value = float("-inf")
            else:
                raise RuntimeError(f"unhandled scalar kind {tok.kind}")
        elif tok.kind == TK_SYMBOL:
            self.advance()
            value = Symbol(tok.value)  # type: ignore[arg-type]
        elif tok.kind == TK_KEYWORD:
            self.advance()
            value = Keyword(tok.value)  # type: ignore[arg-type]
            # Keywords only make sense inside collections; a top-level keyword
            # is syntactically fine but the caller must handle it.
        elif tok.kind == TK_LPAREN:
            value = self._parse_sexpr(tok)
        elif tok.kind == TK_LBRACK:
            value = self._parse_list(tok)
        elif tok.kind == TK_LBRACE:
            value = self._parse_map(tok)
        else:
            raise self.error(
                f"unexpected token {tok.kind} while parsing value",
                tok=tok,
            )

        # Type annotation(s). Each ``::`` counts against max_depth so that
        # a deeply chained annotation can't produce a tree beyond what the
        # formatter / consumer can handle (per spec §9 bounded-parse rule).
        nxt = self.peek()
        annot_count = 0
        while nxt is not None and nxt.kind == TK_COLONCOLON:
            self.advance()
            self._enter()
            annot_count += 1
            type_expr = self._parse_type_expr()
            value = Typed(value=value, type=type_expr)
            self._bump_total()
            nxt = self.peek()
        for _ in range(annot_count):
            self._leave()

        return value

    def _parse_sexpr(self, open_tok: Token) -> Sexpr:
        self.advance()  # consume (
        self._enter()
        try:
            tok = self.peek()
            if tok is None:
                raise self.error("unterminated '('", tok=open_tok, code="WEFT_SYNTAX_UNCLOSED")
            if tok.kind != TK_SYMBOL:
                raise self.error(
                    f"S-expression tag must be a symbol, got {tok.kind}",
                    tok=tok, code="WEFT_SYNTAX_SEXPR_TAG",
                )
            self.advance()
            tag = Symbol(tok.value)  # type: ignore[arg-type]
            items: list = []
            while True:
                t = self.peek()
                if t is None:
                    raise self.error(
                        "unterminated '('", tok=open_tok,
                        code="WEFT_SYNTAX_UNCLOSED",
                    )
                if t.kind == TK_RPAREN:
                    self.advance()
                    break
                items.append(self.parse_value())
                if len(items) > self.limits.max_items_per_coll:
                    raise WeftLimitError(
                        f"S-expression has more items than "
                        f"max_items_per_coll={self.limits.max_items_per_coll}",
                        code="WEFT_LIMIT_ITEMS",
                    )
            return Sexpr(tag=tag, items=items)
        finally:
            self._leave()

    def _parse_list(self, open_tok: Token) -> list:
        self.advance()  # consume [
        self._enter()
        try:
            items: list = []
            while True:
                t = self.peek()
                if t is None:
                    raise self.error(
                        "unterminated '['", tok=open_tok,
                        code="WEFT_SYNTAX_UNCLOSED",
                    )
                if t.kind == TK_RBRACK:
                    self.advance()
                    break
                items.append(self.parse_value())
                if len(items) > self.limits.max_items_per_coll:
                    raise WeftLimitError(
                        f"list has more items than "
                        f"max_items_per_coll={self.limits.max_items_per_coll}",
                        code="WEFT_LIMIT_ITEMS",
                    )
            return items
        finally:
            self._leave()

    def _parse_map(self, open_tok: Token) -> dict:
        self.advance()  # consume {
        self._enter()
        try:
            d: dict = {}
            count = 0
            while True:
                t = self.peek()
                if t is None:
                    raise self.error(
                        "unterminated '{'", tok=open_tok,
                        code="WEFT_SYNTAX_UNCLOSED",
                    )
                if t.kind == TK_RBRACE:
                    self.advance()
                    break
                if t.kind != TK_KEYWORD:
                    raise self.error(
                        f"map key must be a keyword (:name), got {t.kind}",
                        tok=t, code="WEFT_SYNTAX_MAP_KEY",
                    )
                self.advance()
                self._bump_total()  # keyword counts
                key_name = t.value  # str
                v_tok = self.peek()
                if v_tok is None or v_tok.kind == TK_RBRACE:
                    raise self.error(
                        f"map key :{key_name} has no value",
                        tok=t, code="WEFT_SYNTAX_MAP_VALUE",
                    )
                value = self.parse_value()
                if key_name in d:
                    raise self.error(
                        f"duplicate map key :{key_name}",
                        tok=t, code="WEFT_SYNTAX_MAP_DUPKEY",
                    )
                d[key_name] = value
                count += 1
                if count > self.limits.max_items_per_coll:
                    raise WeftLimitError(
                        f"map has more items than "
                        f"max_items_per_coll={self.limits.max_items_per_coll}",
                        code="WEFT_LIMIT_ITEMS",
                    )
            return d
        finally:
            self._leave()

    def _parse_type_expr(self):
        tok = self.peek()
        if tok is None:
            raise WeftSyntaxError(
                "type expression expected after '::'",
                code="WEFT_SYNTAX_TYPE",
            )
        if tok.kind == TK_SYMBOL:
            self.advance()
            return Symbol(tok.value)  # type: ignore[arg-type]
        if tok.kind == TK_LPAREN:
            s = self._parse_sexpr(tok)
            return s
        raise self.error(
            f"type expression must be a symbol or S-expression, got {tok.kind}",
            tok=tok, code="WEFT_SYNTAX_TYPE",
        )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _check_tree_depth(value: Any, max_depth: int) -> None:
    """Walk the parsed tree iteratively and refuse any value whose
    nesting depth exceeds ``max_depth``.

    ``max_depth`` counts *structural* layers only: each list, map, sexpr,
    or Typed wrapper adds one level; leaf scalars do not. A plain integer
    is depth 0; ``[0]`` is depth 1; ``[[[0]]]`` is depth 3; ``0::T::U``
    is depth 2 (two Typed wrappers). This mirrors what the parse-time
    counter *would* track if it included annotations — which is the hole
    this walk closes.

    The walk is iterative so a 10,000-level chain costs memory, not
    stack. A chain of Typed wrappers is unrolled in a tight loop to keep
    the cost linear.
    """
    stack: list = [(value, 0)]
    while stack:
        node, d = stack.pop()
        if isinstance(node, list):
            nd = d + 1
            if nd > max_depth:
                raise WeftLimitError(
                    f"tree depth {nd} exceeds max_depth={max_depth}",
                    code="WEFT_LIMIT_DEPTH",
                )
            for item in node:
                stack.append((item, nd))
        elif isinstance(node, dict):
            nd = d + 1
            if nd > max_depth:
                raise WeftLimitError(
                    f"tree depth {nd} exceeds max_depth={max_depth}",
                    code="WEFT_LIMIT_DEPTH",
                )
            for v in node.values():
                stack.append((v, nd))
        elif isinstance(node, Sexpr):
            nd = d + 1
            if nd > max_depth:
                raise WeftLimitError(
                    f"tree depth {nd} exceeds max_depth={max_depth}",
                    code="WEFT_LIMIT_DEPTH",
                )
            for item in node.items:
                stack.append((item, nd))
        elif isinstance(node, Typed):
            # Unroll chained ::T annotations in a loop to avoid pushing
            # one stack frame per annotation. For each wrapper the
            # type expression is pushed as its own branch — a type like
            # ``(T [[[...]]])`` can carry arbitrary nested data that
            # must count against max_depth just like the value side.
            dd = d
            cur: Any = node
            while isinstance(cur, Typed):
                dd += 1
                if dd > max_depth:
                    raise WeftLimitError(
                        f"tree depth {dd} exceeds max_depth={max_depth}",
                        code="WEFT_LIMIT_DEPTH",
                    )
                # The type expression is a Symbol or Sexpr. A Symbol is a
                # leaf; a Sexpr may hold further structure, so stack it
                # at the same depth as the Typed wrapper (its items will
                # add +1 each).
                stack.append((cur.type, dd))
                cur = cur.value
            stack.append((cur, dd))
        # scalars contribute nothing to depth and don't need stacking


def _strip_types(value: Any) -> Any:
    """Recursively unwrap every ``Typed`` node, returning only the inner
    value. Walks iteratively via an explicit workqueue so a deeply
    nested tree cannot blow the Python recursion limit."""
    if not isinstance(value, (Typed, list, dict, Sexpr)):
        return value

    # Peel outer Typed wrappers from the root.
    while isinstance(value, Typed):
        value = value.value

    # BFS replace in place for containers.
    stack: list = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            for i, v in enumerate(node):
                while isinstance(v, Typed):
                    v = v.value
                node[i] = v
                if isinstance(v, (list, dict, Sexpr)):
                    stack.append(v)
        elif isinstance(node, dict):
            for k, v in list(node.items()):
                while isinstance(v, Typed):
                    v = v.value
                node[k] = v
                if isinstance(v, (list, dict, Sexpr)):
                    stack.append(v)
        elif isinstance(node, Sexpr):
            for i, v in enumerate(node.items):
                while isinstance(v, Typed):
                    v = v.value
                node.items[i] = v
                if isinstance(v, (list, dict, Sexpr)):
                    stack.append(v)
    return value


def parse(
    text: str,
    *,
    limits: Limits = DEFAULT_LIMITS,
    strip_types: bool = False,
    preserve_integer_format: bool = False,
) -> Any:
    """Parse a single-value Weft document.

    Returns the single top-level value, or raises :class:`WeftSyntaxError`
    if the document is empty, has leftover tokens, or is otherwise invalid.

    ``strip_types=True`` (SPEC §8) discards every ``::`` annotation and
    returns just the inner values. ``preserve_integer_format=True``
    (SPEC §7) is accepted for forward compatibility — the formatter
    emits decimal by default; non-decimal round-trip is not yet wired
    through, and the option currently has no observable effect beyond
    not raising ``TypeError``.
    """
    _ = preserve_integer_format  # Accepted; no-op in current formatter.
    # Pass the lexer's generator directly — the parser pulls tokens on
    # demand, so a resource-limit violation aborts the lexer before the
    # whole token stream materialises in memory (SPEC §9 incremental).
    toks = Lexer(text, limits=limits).tokenize()
    p = _Parser(toks, limits=limits, source_text=text)
    if p.peek() is None:
        raise WeftSyntaxError(
            "empty document: single-value mode requires exactly one top-level value",
            code="WEFT_SYNTAX_EMPTY",
        )
    value = p.parse_value()
    leftover = p.peek()
    if leftover is not None:
        raise WeftSyntaxError(
            "unexpected content after top-level value "
            "(use parse_stream for multi-value documents)",
            line=leftover.line, column=leftover.column,
            offset=leftover.offset,
            byte_offset=p._byte_offset_for(leftover),
            code="WEFT_SYNTAX_LEFTOVER",
        )
    _check_tree_depth(value, limits.max_depth)
    if strip_types:
        value = _strip_types(value)
    return value


def parse_stream(
    text: str,
    *,
    limits: Limits = DEFAULT_LIMITS,
    strip_types: bool = False,
    preserve_integer_format: bool = False,
) -> list:
    """Parse zero or more top-level values. Returns a list."""
    _ = preserve_integer_format
    toks = Lexer(text, limits=limits).tokenize()
    p = _Parser(toks, limits=limits, source_text=text)
    values: list = []
    while p.peek() is not None:
        v = p.parse_value()
        _check_tree_depth(v, limits.max_depth)
        if strip_types:
            v = _strip_types(v)
        values.append(v)
    return values
