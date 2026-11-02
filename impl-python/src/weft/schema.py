"""Weft-Schema validator.

Parses a schema (itself a Weft document) and validates target documents
against it.

``:pattern`` constraints use the linear-time engine in :mod:`weft.repat`
(see its docstring for the exact supported subset). Patterns outside the
subset — backreferences, lookaround, named groups, inline flags — are
reported as ``WEFT_SCHEMA_PATTERN`` violations, never silently matched
with a backtracking host engine.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import repat as _repat
from ._trampoline import drive as _vdrive
from .errors import WeftSchemaError
from .parser import parse
from .types import BigInt, Binary, Decimal, Keyword, Sexpr, Symbol, Typed, WeftDate


# Pattern matching (``:pattern``) uses the linear-time engine in
# ``repat`` — a Thompson-NFA simulation with no backtracking, so a
# hostile pattern cannot stall the caller and no timeout machinery is
# needed. Compiled patterns are cached per schema-load process.

_PATTERN_CACHE: Dict[str, "_repat.Pattern"] = {}


def _compiled_pattern(src_pat: str) -> "_repat.Pattern":
    p = _PATTERN_CACHE.get(src_pat)
    if p is None:
        p = _repat.compile_pattern(src_pat)
        if len(_PATTERN_CACHE) < 1024:
            _PATTERN_CACHE[src_pat] = p
    return p


def _diag(v, limit: int = 64) -> str:
    """Render a value for a diagnostic message without host traps: a
    huge int must not hit CPython's int_max_str_digits ValueError
    (``f"{value}"`` does), and containers must not drag such ints in
    through repr. Output is length-capped — diagnostics identify, they
    don't reproduce."""
    from .jsonio import int_to_dec_str
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, BigInt):
        s = int_to_dec_str(v.value) + "n"
    elif isinstance(v, int):
        s = int_to_dec_str(v)
    elif isinstance(v, str):
        s = repr(v)
    elif isinstance(v, list):
        return f"<list of {len(v)} items>"
    elif isinstance(v, dict):
        return f"<map of {len(v)} entries>"
    else:
        try:
            s = repr(v)
        except Exception:
            return f"<{type(v).__name__}>"
    if len(s) > limit:
        s = s[: limit // 2] + f"...({len(s)} chars)"
    return s


class SchemaError(WeftSchemaError):
    def __init__(self, violations: List["Violation"]):
        self.violations = violations
        msg = f"{len(violations)} schema violation(s):\n" + "\n".join(f"  {v}" for v in violations[:10])
        if len(violations) > 10:
            msg += f"\n  ... and {len(violations) - 10} more"
        super().__init__(msg, code="WEFT_SCHEMA")


@dataclass
class Violation:
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass
class SchemaDef:
    name: str
    fields: List["Field"]


@dataclass
class Field:
    name: str
    type_: Any  # type expression (Symbol or Sexpr)
    required: bool
    default: Any
    constraints: Dict[str, Any]


class SchemaRegistry:
    def __init__(self) -> None:
        self.schemas: Dict[str, SchemaDef] = {}


# --------------------------------------------------------------------------
# Parsing the schema file
# --------------------------------------------------------------------------

def load_schema(text: str) -> SchemaRegistry:
    tree = parse(text)
    registry = SchemaRegistry()
    forms = tree if isinstance(tree, list) else [tree]
    # Accept a single top-level schema, or a list of schemas, or an Sexpr with
    # tag `schemas` containing multiple.
    if isinstance(tree, Sexpr) and tree.tag.name == "schema":
        forms = [tree]
    elif isinstance(tree, Sexpr) and tree.tag.name == "schemas":
        forms = [x for x in tree.items if isinstance(x, Sexpr) and x.tag.name == "schema"]
    for form in forms:
        if not isinstance(form, Sexpr) or form.tag.name != "schema":
            raise WeftSchemaError(f"expected (schema ...), got {_diag(form)}", code="WEFT_SCHEMA_PARSE")
        sd = _parse_schema_form(form)
        registry.schemas[sd.name] = sd
    return registry


def _parse_schema_form(form: Sexpr) -> SchemaDef:
    if len(form.items) < 1:
        raise WeftSchemaError("(schema ...) needs a name", code="WEFT_SCHEMA_PARSE")
    name_node = form.items[0]
    if not isinstance(name_node, Symbol):
        raise WeftSchemaError("schema name must be a symbol", code="WEFT_SCHEMA_PARSE")
    fields: List[Field] = []
    for item in form.items[1:]:
        if not isinstance(item, Sexpr) or item.tag.name != "field":
            raise WeftSchemaError(f"expected (field ...), got {_diag(item)}", code="WEFT_SCHEMA_PARSE")
        fields.append(_parse_field(item))
    return SchemaDef(name=name_node.name, fields=fields)


_SENTINEL = object()


# Expected Python type for each known constraint. Checked at
# schema-parse time so that a mis-typed constraint value (``:pattern 1``,
# ``:min-length "2"``) is rejected up front rather than crashing with a
# TypeError inside the validator.
_CONSTRAINT_SPEC: Dict[str, tuple] = {
    "min":           ((int, float, BigInt, Decimal),),
    "max":           ((int, float, BigInt, Decimal),),
    "exclusive-min": ((int, float, BigInt, Decimal),),
    "exclusive-max": ((int, float, BigInt, Decimal),),
    "min-length":    ((int, BigInt),),
    "max-length":    ((int, BigInt),),
    "items-min":     ((int, BigInt),),
    "items-max":     ((int, BigInt),),
    "unique":        ((bool,),),
    "pattern":       ((str,),),
}


def _parse_field(form: Sexpr) -> Field:
    pos, kw = form.positional_and_keyword()
    if len(pos) < 2:
        raise WeftSchemaError("(field name Type ...) needs name and type", code="WEFT_SCHEMA_PARSE")
    name_node, type_node = pos[0], pos[1]
    if not isinstance(name_node, Symbol):
        raise WeftSchemaError("field name must be a symbol", code="WEFT_SCHEMA_PARSE")
    required = True
    default = _SENTINEL
    constraints: Dict[str, Any] = {}
    for k, v in kw.items():
        if k == "required":
            required = bool(v)
        elif k == "optional":
            required = not bool(v)
        elif k == "default":
            default = v
            required = False
        else:
            spec = _CONSTRAINT_SPEC.get(k)
            if spec is not None:
                expected_types = spec[0]
                # bool is a subclass of int in Python; a bool constraint
                # value is only acceptable where ``bool`` is listed
                # explicitly.
                if isinstance(v, bool) and bool not in expected_types:
                    raise WeftSchemaError(
                        f"constraint :{k} expects {expected_types}, got bool",
                        code="WEFT_SCHEMA_CONSTRAINT",
                    )
                if not isinstance(v, expected_types):
                    raise WeftSchemaError(
                        f"constraint :{k} expects {expected_types}, got {type(v).__name__}",
                        code="WEFT_SCHEMA_CONSTRAINT",
                    )
                # Normalise BigInt-valued constraints to plain ``int`` so
                # the validator can use ``<``/``>`` against native ints
                # (``BigInt`` is a Weft wrapper, not an ``int`` subclass).
                if isinstance(v, BigInt):
                    v = v.value
            constraints[k] = v
    return Field(name=name_node.name, type_=type_node, required=required, default=default, constraints=constraints)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_MAX_VALIDATION_STEPS = 2_000_000


class _VCtx:
    """Per-validate() state: the registry, a (value, type) result memo,
    and an explicit work budget.

    The memo makes validation polynomial — O(container values x type
    nodes) — where a naive Union retries the same subtree against the
    same type exponentially ((Union R R) doubles the work per nesting
    level). Only container values (map/list/sexpr) are memoized: they
    have unique object identities in a parsed tree, whereas small ints
    and strings may be interned by the host and shared across paths.
    Memo entries hold violations with paths RELATIVE to the validation
    root of that entry, re-prefixed on replay.

    The step budget is the backstop the memo cannot be: it bounds total
    validation work outright, so no schema/document pair can consume
    unbounded CPU (WEFT_SCHEMA_BUDGET)."""

    __slots__ = ("registry", "memo", "steps")

    def __init__(self, registry: SchemaRegistry):
        self.registry = registry
        self.memo: dict = {}
        self.steps = 0

    def step(self) -> None:
        self.steps += 1
        if self.steps > _MAX_VALIDATION_STEPS:
            raise WeftSchemaError(
                f"schema validation exceeded {_MAX_VALIDATION_STEPS} steps "
                "(schema/document combination too expensive)",
                code="WEFT_SCHEMA_BUDGET")


def _type_key(type_expr):
    """Memo key component for a type expression. Record references and
    primitive names key by name (two Symbols naming the same record hit
    the same entry); structural Sexpr type expressions key by node
    identity (the schema tree is fixed for the whole validate call)."""
    if isinstance(type_expr, Symbol):
        return ("s", type_expr.name)
    return ("x", id(type_expr))


def _join_path(base: str, rel: str) -> str:
    if not rel:
        return base
    if not base:
        return rel
    if rel[0] in "[#":
        return base + rel
    return base + "." + rel


def validate(registry: SchemaRegistry, document: Any, root_schema: str) -> List[Violation]:
    if root_schema not in registry.schemas:
        return [Violation("", "WEFT_SCHEMA_RECORD", f"schema {root_schema!r} not defined")]
    violations: List[Violation] = []
    ctx = _VCtx(registry)
    _vdrive(_validate_record(ctx, document, registry.schemas[root_schema], root_schema, violations))
    return violations


def _validate_record(
    ctx: "_VCtx", value: Any, sd: SchemaDef,
    path: str, violations: List[Violation],
):
    """Step generator (run via weft._trampoline.drive)."""
    ctx.step()
    if not isinstance(value, dict):
        violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"expected record (map), got {_type_name(value)}"))
        return
    for field in sd.fields:
        fpath = f"{path}.{field.name}" if path else field.name
        if field.name not in value:
            if field.required:
                violations.append(Violation(fpath, "WEFT_SCHEMA_REQUIRED", f"required field {field.name!r} missing"))
            continue
        val = value[field.name]
        # Guard: constraints only run when the type check passed. A min/max
        # on something that is not numeric is noise at best and a crash at
        # worst — a type violation already names the real problem.
        before = len(violations)
        yield _validate_against_type(ctx, val, field.type_, fpath, violations)
        if len(violations) == before:
            _apply_constraints(val, field.constraints, fpath, violations, ctx)
    allowed = {f.name for f in sd.fields}
    for key in value.keys():
        if key not in allowed:
            fpath = f"{path}.{key}" if path else key
            violations.append(Violation(fpath, "WEFT_SCHEMA_UNKNOWN_FIELD", f"unknown field {key!r}"))


def _validate_against_type(ctx, value, type_expr, path, violations):
    """Step generator. Container values are validated once per
    (value identity, type) and replayed from the memo afterwards —
    see _VCtx."""
    ctx.step()
    registry = ctx.registry
    if isinstance(value, (dict, list, Sexpr)):
        key = (id(value), _type_key(type_expr))
        hit = ctx.memo.get(key)
        if hit is not None:
            for rel, code, msg in hit:
                violations.append(Violation(_join_path(path, rel), code, msg))
            return
        local: List[Violation] = []
        yield _validate_against_type_inner(ctx, value, type_expr, "", local)
        ctx.memo[key] = tuple((v.path, v.code, v.message) for v in local)
        for v in local:
            violations.append(Violation(_join_path(path, v.path), v.code, v.message))
        return
    yield _validate_against_type_inner(ctx, value, type_expr, path, violations)


def _validate_against_type_inner(ctx, value, type_expr, path, violations):
    registry = ctx.registry
    # Primitive symbol types
    if isinstance(type_expr, Symbol):
        name = type_expr.name
        if name == "Null":
            if value is not None:
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected nil"))
            return
        if name == "Bool":
            if not isinstance(value, bool):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Bool"))
            return
        if name == "Int":
            if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, BigInt)):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Int"))
            return
        if name == "Float":
            if not isinstance(value, float) or isinstance(value, bool):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Float"))
            return
        if name == "Decimal":
            if not isinstance(value, Decimal):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Decimal"))
            return
        if name == "String":
            if not isinstance(value, str):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected String"))
            return
        if name == "Binary":
            if not isinstance(value, Binary):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Binary"))
            return
        if name == "Date":
            if not isinstance(value, (WeftDate, _dt.datetime, _dt.date)):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Date"))
            return
        if name == "Symbol":
            if not isinstance(value, Symbol):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Symbol"))
            return
        if name == "Keyword":
            if not isinstance(value, Keyword):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Keyword"))
            return
        if name == "Any":
            return
        # Record reference
        if name in registry.schemas:
            yield _validate_record(ctx, value, registry.schemas[name], path, violations)
            return
        violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"unknown type {name!r}"))
        return

    if isinstance(type_expr, Sexpr):
        tag = type_expr.tag.name
        if tag == "List":
            if not isinstance(value, list):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected List"))
                return
            if not type_expr.items:
                return
            item_type = type_expr.items[0]
            for i, v in enumerate(value):
                yield _validate_against_type(ctx, v, item_type, f"{path}[{i}]", violations)
            return
        if tag == "Map":
            if not isinstance(value, dict):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Map"))
                return
            if len(type_expr.items) < 2:
                return
            value_type = type_expr.items[1]
            for k, v in value.items():
                yield _validate_against_type(ctx, v, value_type, _join_path(path, k), violations)
            return
        if tag == "Optional":
            if value is None:
                return
            if type_expr.items:
                yield _validate_against_type(ctx, value, type_expr.items[0], path, violations)
            return
        if tag == "Union":
            saved = list(violations)
            for alt in type_expr.items:
                local: List[Violation] = []
                yield _validate_against_type(ctx, value, alt, path, local)
                if not local:
                    violations[:] = saved
                    return
            violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "no union alternative matched"))
            return
        if tag == "Enum":
            if not isinstance(value, str):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "enum requires String value"))
                return
            allowed = [x for x in type_expr.items if isinstance(x, str)]
            if value not in allowed:
                violations.append(Violation(path, "WEFT_SCHEMA_ENUM", f"must be one of {allowed}, got {_diag(value)}"))
            return
        if tag == "Tuple":
            if not isinstance(value, list):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected List (tuple)"))
                return
            if len(value) != len(type_expr.items):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"tuple length mismatch: want {len(type_expr.items)}, got {len(value)}"))
                return
            for i, (v, t) in enumerate(zip(value, type_expr.items)):
                yield _validate_against_type(ctx, v, t, f"{path}[{i}]", violations)
            return
        if tag == "Sexpr":
            if not isinstance(value, Sexpr):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Sexpr"))
                return
            # First item is the expected tag symbol
            if type_expr.items:
                expected_tag = type_expr.items[0]
                if isinstance(expected_tag, Symbol) and value.tag.name != expected_tag.name:
                    violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"expected sexpr tag {expected_tag.name!r}, got {value.tag.name!r}"))
                    return
                item_types = type_expr.items[1:]
                if len(value.items) != len(item_types):
                    violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"sexpr item count mismatch"))
                    return
                for i, (v, t) in enumerate(zip(value.items, item_types)):
                    yield _validate_against_type(ctx, v, t, f"{path}#{i}", violations)
            return
        violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"unknown compound type {tag!r}"))
        return

    violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "invalid type expression"))


def _to_num(x):
    """Normalise a numeric value for ordered comparison. Returns ``None`` for
    anything that cannot participate in a well-defined ordering (NaN, non-
    numeric). Infinities ARE legal and return their own float value."""
    import math as _math
    if isinstance(x, bool):
        return None
    if isinstance(x, BigInt):
        return x.value
    if isinstance(x, Decimal):
        try:
            return x.to_stdlib()
        except Exception:
            return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        if _math.isnan(x):
            return None  # NaN is not ordered; caller treats as "fails bound"
        return x
    return None


_NAN_SENTINEL = object()


def _weft_equality_key(x, ctx=None):
    """Return a hashable key such that two Weft values share the key iff
    they are **canonically equal** (SPEC §5.16):

    * Integer/BigInt compare numerically (``1`` ≡ ``1n``); the ``n``
      suffix is a presentation hint.
    * Decimal: equal iff the normalised text matches (``3.14`` ≠ ``3.140``).
    * Binary: equal iff the content bytes match (encoding is a hint).
    * Float: IEEE-754 bit identity (``-0.0`` ≠ ``0.0``); any NaN —
      including inside a composite — makes the value unequal to
      everything, so the key is a fresh sentinel object.
    * Map key order is normalised; composites compare structurally.

    Two properties matter for hostile input, and both are why the key is
    a flat **string** built iteratively rather than a nested tuple of raw
    ints:

    * CPython hashes ``int`` as the value mod 2^61-1 — an attacker can
      mint unlimited distinct integers with identical hashes and turn a
      ``:unique`` set into quadratic probing. Strings hash with the
      process-keyed SipHash, which hostile input cannot target. Integers
      are therefore keyed by their decimal text.
    * The walk uses an explicit stack, so a deep (but §9-admitted) value
      cannot overflow the host stack. Each visited node counts against
      the validation step budget when a ctx is supplied.

    Tokens are length-prefixed (netstring style), so distinct structures
    cannot collide by concatenation.
    """
    import math as _math
    import struct as _struct
    from .jsonio import int_to_dec_str

    parts: List[str] = []
    stack = [x]
    while stack:
        node = stack.pop()
        if ctx is not None:
            ctx.step()
        if node is None:
            parts.append("z;")
        elif node is True:
            parts.append("b1;")
        elif node is False:
            parts.append("b0;")
        elif isinstance(node, BigInt):
            s = int_to_dec_str(node.value)
            parts.append(f"i{len(s)}:{s}")
        elif isinstance(node, int):
            s = int_to_dec_str(node)
            parts.append(f"i{len(s)}:{s}")
        elif isinstance(node, float):
            if _math.isnan(node):
                # NaN is unequal to everything, itself included — the
                # whole value gets a globally unique identity key.
                return object()
            parts.append("f" + _struct.pack("<d", node).hex() + ";")
        elif isinstance(node, Decimal):
            parts.append(f"d{len(node.text)}:{node.text}")
        elif isinstance(node, str):
            parts.append(f"s{len(node)}:{node}")
        elif isinstance(node, Symbol):
            parts.append(f"y{len(node.name)}:{node.name}")
        elif isinstance(node, Keyword):
            parts.append(f"w{len(node.name)}:{node.name}")
        elif isinstance(node, Binary):
            h = bytes(node.data).hex()
            parts.append(f"n{len(h)}:{h}")
        elif isinstance(node, WeftDate):
            t = (f"{node.year},{node.month},{node.day},{node.has_time},"
                 f"{node.hour},{node.minute},{node.second},{node.nano},"
                 f"{node.tz_offset_minutes}")
            parts.append(f"D{len(t)}:{t}")
        elif isinstance(node, (_dt.datetime, _dt.date)):
            t = node.isoformat()
            parts.append(f"E{len(t)}:{t}")
        elif isinstance(node, list):
            parts.append(f"L{len(node)};")
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            parts.append(f"M{len(node)};")
            for k in sorted(node.keys(), reverse=True):
                stack.append(node[k])
                stack.append(("#key", k))
        elif isinstance(node, Sexpr):
            parts.append(f"X{len(node.items)};y{len(node.tag.name)}:{node.tag.name}")
            stack.extend(reversed(node.items))
        elif isinstance(node, Typed):
            parts.append("T;")
            stack.append(node.value)
            stack.append(node.type)
        elif isinstance(node, tuple) and len(node) == 2 and node[0] == "#key":
            k = node[1]
            parts.append(f"k{len(k)}:{k}")
        else:
            parts.append(f"o{_diag(node)};")
    return "".join(parts)


def _apply_constraints(value, constraints, path, violations, ctx=None):
    import math as _math
    for k, v in constraints.items():
        if k in ("min", "max", "exclusive-min", "exclusive-max"):
            # NaN never satisfies any numeric bound — spec §9.3.
            if isinstance(value, float) and _math.isnan(value):
                code = "WEFT_SCHEMA_MIN" if k in ("min", "exclusive-min") else "WEFT_SCHEMA_MAX"
                violations.append(Violation(path, code, f"NaN is not within numeric bound {k}={_diag(v)}"))
                continue
            num_val = _to_num(value)
            num_lim = _to_num(v)
            if num_val is None or num_lim is None:
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"cannot compare {_diag(value)} with {_diag(v)}"))
                continue
            if k == "min" and num_val < num_lim:
                violations.append(Violation(path, "WEFT_SCHEMA_MIN", f"{_diag(value)} < {_diag(v)}"))
            elif k == "max" and num_val > num_lim:
                violations.append(Violation(path, "WEFT_SCHEMA_MAX", f"{_diag(value)} > {_diag(v)}"))
            elif k == "exclusive-min" and not (num_val > num_lim):
                violations.append(Violation(path, "WEFT_SCHEMA_MIN", f"{_diag(value)} not > {_diag(v)}"))
            elif k == "exclusive-max" and not (num_val < num_lim):
                violations.append(Violation(path, "WEFT_SCHEMA_MAX", f"{_diag(value)} not < {_diag(v)}"))
        elif k == "min-length":
            vn = v.value if isinstance(v, BigInt) else v
            if hasattr(value, "__len__") and len(value) < vn:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"length {len(value)} < {_diag(v)}"))
        elif k == "max-length":
            vn = v.value if isinstance(v, BigInt) else v
            if hasattr(value, "__len__") and len(value) > vn:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"length {len(value)} > {_diag(v)}"))
        elif k == "pattern":
            if isinstance(value, str):
                try:
                    if not _compiled_pattern(v).fullmatch(value):
                        violations.append(Violation(path, "WEFT_SCHEMA_PATTERN", f"does not match /{v}/"))
                except WeftSchemaError as e:
                    violations.append(Violation(path, "WEFT_SCHEMA_PATTERN", f"invalid pattern: {e}"))
        elif k == "items-min":
            vn = v.value if isinstance(v, BigInt) else v
            if isinstance(value, list) and len(value) < vn:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"items {len(value)} < {_diag(v)}"))
        elif k == "items-max":
            vn = v.value if isinstance(v, BigInt) else v
            if isinstance(value, list) and len(value) > vn:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"items {len(value)} > {_diag(v)}"))
        elif k == "unique":
            if isinstance(value, list) and v:
                seen = set()
                for i, x in enumerate(value):
                    key = _weft_equality_key(x, ctx)
                    if key in seen:
                        violations.append(Violation(f"{path}[{i}]", "WEFT_SCHEMA_UNIQUE", f"duplicate value {_diag(x)}"))
                    else:
                        seen.add(key)


def _type_name(v: Any) -> str:
    if v is None: return "Null"
    if isinstance(v, bool): return "Bool"
    if isinstance(v, int): return "Int"
    if isinstance(v, float): return "Float"
    if isinstance(v, Decimal): return "Decimal"
    if isinstance(v, str): return "String"
    if isinstance(v, Binary): return "Binary"
    if isinstance(v, (WeftDate, _dt.datetime, _dt.date)): return "Date"
    if isinstance(v, Symbol): return "Symbol"
    if isinstance(v, Keyword): return "Keyword"
    if isinstance(v, list): return "List"
    if isinstance(v, dict): return "Map"
    if isinstance(v, Sexpr): return "Sexpr"
    if isinstance(v, Typed): return "Typed"
    return type(v).__name__


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------

def validate_file(schema_path: str, doc_path: str) -> None:
    """Validate a document file against a schema file. Raise SchemaError on failure."""
    from pathlib import Path
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    doc_text = Path(doc_path).read_text(encoding="utf-8")
    registry = load_schema(schema_text)
    if not registry.schemas:
        raise WeftSchemaError("no schemas defined", code="WEFT_SCHEMA_PARSE")
    root = next(iter(registry.schemas))
    doc = parse(doc_text)
    violations = validate(registry, doc, root)
    if violations:
        raise SchemaError(violations)
