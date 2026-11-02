"""Linear-time pattern matching for schema ``:pattern`` (SPEC §8).

This is a Thompson-NFA engine: the pattern is compiled once into a
graph of at most ``MAX_NODES`` nodes, and matching simulates the node
set over the input — worst case O(len(text) * nodes), with **no
backtracking and no timeouts**. A hostile pattern therefore cannot
stall the caller, which a wall-clock timeout around a backtracking
host engine cannot guarantee (the host match holds the GIL / the
event loop and the timer never fires).

Supported syntax (the SPEC §8 pattern subset — identical in every
reference implementation):

* literals, ``.`` (any code point except LF)
* escapes: ``\\d \\D \\w \\W \\s \\S \\n \\t \\r \\f \\v``,
  ``\\xHH``, ``\\uHHHH``, ``\\u{HEX}``, and escaped metacharacters
* character classes ``[...]`` / ``[^...]`` with ranges
* quantifiers ``* + ? {m} {m,} {m,n}`` (an appended ``?`` is accepted
  and ignored — greediness cannot change a boolean full match)
* alternation ``|``; groups ``(...)`` and ``(?:...)``
* anchors ``^`` (start) and ``$`` (end)

Deliberately **not** supported (rejected at compile time with
``WEFT_SCHEMA_PATTERN``): backreferences, lookaround, named groups,
inline flags. ``\\d \\w \\s`` are the ASCII sets ``[0-9]``,
``[A-Za-z0-9_]``, ``[ \\t\\n\\r\\f\\v]`` — pinned so every host
matches identical strings.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .errors import WeftSchemaError

MAX_NODES = 10000
# {m,n} repetition is expanded structurally; cap the counters so the
# expansion cannot be used to blow up the compiled size.
MAX_REPEAT = 1000

# Node kinds
_CHAR = 0      # match one code point in `ranges`, goto next
_SPLIT = 1     # epsilon to out1 and out2
_MATCH = 2     # accept
_ASSERT_BOL = 3  # epsilon, only at position 0
_ASSERT_EOL = 4  # epsilon, only at end of input

_D = (("0", "9"),)
_W = (("0", "9"), ("A", "Z"), ("a", "z"), ("_", "_"))
_S = ((" ", " "), ("\t", "\t"), ("\n", "\n"), ("\r", "\r"),
      ("\x0b", "\x0b"), ("\x0c", "\x0c"))

_MAX_CP = 0x10FFFF


def _err(msg: str) -> WeftSchemaError:
    return WeftSchemaError(msg, code="WEFT_SCHEMA_PATTERN")


def _negate(ranges: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[str, str], ...]:
    pts = sorted((ord(a), ord(b)) for a, b in ranges)
    out: List[Tuple[str, str]] = []
    prev = 0
    for a, b in pts:
        if a > prev:
            out.append((chr(prev), chr(a - 1)))
        prev = max(prev, b + 1)
    if prev <= _MAX_CP:
        out.append((chr(prev), chr(_MAX_CP)))
    return tuple(out)


class _Node:
    __slots__ = ("kind", "ranges", "nxt", "out1", "out2")

    def __init__(self, kind: int):
        self.kind = kind
        self.ranges: Tuple[Tuple[str, str], ...] = ()
        self.nxt = -1
        self.out1 = -1
        self.out2 = -1


class Pattern:
    """A compiled SPEC §8 pattern. ``fullmatch(text)`` is linear-time."""

    def __init__(self, source: str):
        self.source = source
        self._nodes: List[_Node] = []
        self._start = self._compile(source)

    # --- compilation ------------------------------------------------

    def _new(self, kind: int) -> int:
        if len(self._nodes) >= MAX_NODES:
            raise _err(f"pattern compiles to more than {MAX_NODES} states")
        self._nodes.append(_Node(kind))
        return len(self._nodes) - 1

    def _compile(self, src: str) -> int:
        self._src = src
        self._pos = 0
        frag = self._alt()
        if self._pos != len(src):
            raise _err(f"unexpected {src[self._pos]!r} at index {self._pos}")
        start, outs = frag
        m = self._new(_MATCH)
        self._patch(outs, m)
        return start

    # A fragment is (start_node, [(node_index, slot)]) where slot is
    # "nxt"/"out1"/"out2" dangling arrows to patch.
    def _patch(self, outs, target: int) -> None:
        for idx, slot in outs:
            setattr(self._nodes[idx], slot, target)

    def _peek(self) -> Optional[str]:
        return self._src[self._pos] if self._pos < len(self._src) else None

    def _take(self) -> str:
        ch = self._src[self._pos]
        self._pos += 1
        return ch

    def _alt(self):
        frag = self._concat()
        while self._peek() == "|":
            self._take()
            right = self._concat()
            s = self._new(_SPLIT)
            self._nodes[s].out1 = frag[0]
            self._nodes[s].out2 = right[0]
            frag = (s, frag[1] + right[1])
        return frag

    def _concat(self):
        frags = []
        while True:
            ch = self._peek()
            if ch is None or ch in "|)":
                break
            frags.append(self._repeat())
        if not frags:
            # empty concatenation: a split node with both arrows dangling
            # would loop; use an epsilon via SPLIT with one arrow.
            s = self._new(_SPLIT)
            return (s, [(s, "out1"), (s, "out2")])
        start, outs = frags[0]
        for nstart, nouts in frags[1:]:
            self._patch(outs, nstart)
            outs = nouts
        return (start, outs)

    def _repeat(self):
        frag = self._atom()
        ch = self._peek()
        if ch not in ("*", "+", "?", "{"):
            return frag
        if ch == "{":
            save = self._pos
            lo, hi = self._braces()
            if lo is None:
                self._pos = save  # literal '{'
                return frag
            frag = self._repeat_counted(frag, lo, hi)
        else:
            self._take()
            if ch == "*":
                frag = self._star(frag)
            elif ch == "+":
                frag = self._plus(frag)
            else:
                frag = self._opt(frag)
        if self._peek() == "?":  # lazy marker — irrelevant for a boolean match
            self._take()
        if self._peek() in ("*", "+", "?", "{") and self._peek() != "{":
            raise _err("nested bare quantifier (e.g. 'a++'); group the inner repeat")
        return frag

    def _braces(self):
        # at '{'; returns (lo, hi) or (None, None) if not a counted repeat
        assert self._take() == "{"
        digits = ""
        while self._peek() is not None and self._peek().isdigit():
            digits += self._take()
        if self._peek() == "}":
            if not digits:
                return (None, None)
            self._take()
            n = int(digits)
            self._check_rep(n)
            return (n, n)
        if self._peek() != ",":
            return (None, None)
        self._take()
        lo = int(digits) if digits else 0
        self._check_rep(lo)
        digits2 = ""
        while self._peek() is not None and self._peek().isdigit():
            digits2 += self._take()
        if self._peek() != "}":
            return (None, None)
        self._take()
        if digits2:
            hi = int(digits2)
            self._check_rep(hi)
            if hi < lo:
                raise _err("bad repeat range {m,n} with n < m")
            return (lo, hi)
        return (lo, -1)  # open-ended

    @staticmethod
    def _check_rep(n: int) -> None:
        if n > MAX_REPEAT:
            raise _err(f"repeat count {n} exceeds {MAX_REPEAT}")

    def _star(self, frag):
        s = self._new(_SPLIT)
        self._nodes[s].out1 = frag[0]
        self._patch(frag[1], s)
        return (s, [(s, "out2")])

    def _plus(self, frag):
        s = self._new(_SPLIT)
        self._nodes[s].out1 = frag[0]
        self._patch(frag[1], s)
        return (frag[0], [(s, "out2")])

    def _opt(self, frag):
        s = self._new(_SPLIT)
        self._nodes[s].out1 = frag[0]
        return (s, frag[1] + [(s, "out2")])

    def _clone(self, frag):
        """Deep-copy a fragment (for counted repetition)."""
        start, outs = frag
        # collect reachable nodes
        seen: dict = {}
        stack = [start]
        order = []
        while stack:
            i = stack.pop()
            if i in seen or i < 0:
                continue
            seen[i] = None
            order.append(i)
            n = self._nodes[i]
            stack.extend(x for x in (n.nxt, n.out1, n.out2) if x >= 0)
        for i in order:
            seen[i] = self._new(self._nodes[i].kind)
        for i in order:
            src, dst = self._nodes[i], self._nodes[seen[i]]
            dst.ranges = src.ranges
            dst.nxt = seen.get(src.nxt, -1) if src.nxt >= 0 else -1
            dst.out1 = seen.get(src.out1, -1) if src.out1 >= 0 else -1
            dst.out2 = seen.get(src.out2, -1) if src.out2 >= 0 else -1
        new_outs = [(seen[i], slot) for i, slot in outs]
        return (seen[start], new_outs)

    def _repeat_counted(self, frag, lo: int, hi: int):
        # lo required copies, then either a star (open-ended) or
        # (hi - lo) optional copies. The first part reuses `frag`
        # itself; every further copy is a structural clone.
        parts = []
        used_orig = False
        for _ in range(lo):
            parts.append(self._clone(frag) if used_orig else frag)
            used_orig = True
        if hi == -1:
            src = self._clone(frag) if used_orig else frag
            parts.append(self._star(src))
        else:
            for _ in range(hi - lo):
                src = self._clone(frag) if used_orig else frag
                used_orig = True
                parts.append(self._opt(src))
        if not parts:  # {0} / {0,0} — matches the empty string
            s = self._new(_SPLIT)
            return (s, [(s, "out1"), (s, "out2")])
        start, outs = parts[0]
        for nstart, nouts in parts[1:]:
            self._patch(outs, nstart)
            outs = nouts
        return (start, outs)

    def _atom(self):
        ch = self._take()
        if ch == "(":
            if self._peek() == "?":
                self._take()
                nxt = self._peek()
                if nxt == ":":
                    self._take()
                else:
                    raise _err("only (?:...) groups are supported "
                               "(no lookaround, flags, or named groups)")
            frag = self._alt()
            if self._peek() != ")":
                raise _err("unterminated group")
            self._take()
            return frag
        if ch == "[":
            return self._charclass()
        if ch == ".":
            n = self._new(_CHAR)
            self._nodes[n].ranges = _negate((("\n", "\n"),))
            return (n, [(n, "nxt")])
        if ch == "^":
            n = self._new(_ASSERT_BOL)
            return (n, [(n, "nxt")])
        if ch == "$":
            n = self._new(_ASSERT_EOL)
            return (n, [(n, "nxt")])
        if ch == "\\":
            return self._escape_atom()
        if ch in ")|*+?":
            raise _err(f"unexpected {ch!r}")
        n = self._new(_CHAR)
        self._nodes[n].ranges = ((ch, ch),)
        return (n, [(n, "nxt")])

    def _escape_atom(self):
        ranges, negated = self._escape_ranges()
        n = self._new(_CHAR)
        n_ = self._nodes[n]
        n_.ranges = _negate(ranges) if negated else ranges
        return (n, [(n, "nxt")])

    def _escape_ranges(self):
        """Consume one escape after '\\'. Returns (ranges, negated)."""
        if self._peek() is None:
            raise _err("dangling backslash")
        e = self._take()
        if e == "d":
            return (_D, False)
        if e == "D":
            return (_D, True)
        if e == "w":
            return (_W, False)
        if e == "W":
            return (_W, True)
        if e == "s":
            return (_S, False)
        if e == "S":
            return (_S, True)
        simple = {"n": "\n", "t": "\t", "r": "\r", "f": "\x0c", "v": "\x0b",
                  "0": "\x00"}
        if e in simple:
            c = simple[e]
            return (((c, c),), False)
        if e == "x":
            h = self._hex(2)
            c = chr(int(h, 16))
            return (((c, c),), False)
        if e == "u":
            if self._peek() == "{":
                self._take()
                h = ""
                while self._peek() is not None and self._peek() != "}":
                    h += self._take()
                if self._peek() != "}" or not (1 <= len(h) <= 6):
                    raise _err("bad \\u{...} escape")
                self._take()
            else:
                h = self._hex(4)
            cp = int(h, 16)
            if cp > _MAX_CP or 0xD800 <= cp <= 0xDFFF:
                raise _err("escape names an invalid code point")
            c = chr(cp)
            return (((c, c),), False)
        if e.isdigit():
            raise _err("backreferences are not supported")
        if e.isalpha():
            raise _err(f"unsupported escape \\{e}")
        return (((e, e),), False)  # escaped metacharacter / punctuation

    def _hex(self, n: int) -> str:
        h = ""
        for _ in range(n):
            if self._peek() is None or self._peek() not in "0123456789abcdefABCDEF":
                raise _err("bad hex escape")
            h += self._take()
        return h

    def _charclass(self):
        negated = False
        if self._peek() == "^":
            self._take()
            negated = True
        ranges: List[Tuple[str, str]] = []
        first = True
        while True:
            ch = self._peek()
            if ch is None:
                raise _err("unterminated character class")
            if ch == "]" and not first:
                self._take()
                break
            first = False
            self._take()
            if ch == "\\":
                rs, neg = self._escape_ranges()
                if neg:
                    rs = _negate(rs)
                if self._peek() == "-" and self._pos + 1 < len(self._src) \
                        and self._src[self._pos + 1] != "]":
                    raise _err("range endpoint cannot be a multi-char class")
                ranges.extend(rs)
                continue
            lo = ch
            if self._peek() == "-" and self._pos + 1 < len(self._src) \
                    and self._src[self._pos + 1] != "]":
                self._take()  # '-'
                hi = self._take()
                if hi == "\\":
                    rs, neg = self._escape_ranges()
                    if neg or len(rs) != 1 or rs[0][0] != rs[0][1]:
                        raise _err("range endpoint cannot be a multi-char class")
                    hi = rs[0][0]
                if ord(hi) < ord(lo):
                    raise _err(f"bad range {lo!r}-{hi!r}")
                ranges.append((lo, hi))
            else:
                ranges.append((lo, lo))
        if not ranges:
            raise _err("empty character class")
        n = self._new(_CHAR)
        self._nodes[n].ranges = _negate(tuple(ranges)) if negated \
            else tuple(ranges)
        return (n, [(n, "nxt")])

    # --- simulation ---------------------------------------------------

    def _add(self, states: set, i: int, pos: int, end: int) -> None:
        stack = [i]
        while stack:
            j = stack.pop()
            if j < 0 or j in states:
                continue
            node = self._nodes[j]
            if node.kind == _SPLIT:
                states.add(j)
                stack.append(node.out1)
                stack.append(node.out2)
            elif node.kind == _ASSERT_BOL:
                states.add(j)
                if pos == 0:
                    stack.append(node.nxt)
            elif node.kind == _ASSERT_EOL:
                states.add(j)
                if pos == end:
                    stack.append(node.nxt)
            else:
                states.add(j)

    def fullmatch(self, text: str) -> bool:
        cps = list(text)
        end = len(cps)
        current: set = set()
        self._add(current, self._start, 0, end)
        for pos, ch in enumerate(cps):
            nxt: set = set()
            o = ord(ch)
            for i in current:
                node = self._nodes[i]
                if node.kind == _CHAR:
                    for a, b in node.ranges:
                        if ord(a) <= o <= ord(b):
                            self._add(nxt, node.nxt, pos + 1, end)
                            break
            if not nxt:
                return False
            current = nxt
        return any(self._nodes[i].kind == _MATCH for i in current)


def compile_pattern(source: str) -> Pattern:
    """Compile a SPEC §8 pattern; raises WeftSchemaError
    (WEFT_SCHEMA_PATTERN) on unsupported or oversized patterns."""
    return Pattern(source)
