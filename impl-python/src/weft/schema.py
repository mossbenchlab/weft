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
            raise WeftSchemaError(f"expected (schema ...), got {form!r}", code="WEFT_SCHEMA_PARSE")
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
            raise WeftSchemaError(f"expected (field ...), got {item!r}", code="WEFT_SCHEMA_PARSE")
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

def validate(registry: SchemaRegistry, document: Any, root_schema: str) -> List[Violation]:
    if root_schema not in registry.schemas:
        return [Violation("", "WEFT_SCHEMA_RECORD", f"schema {root_schema!r} not defined")]
    violations: List[Violation] = []
    _validate_record(registry, document, registry.schemas[root_schema], root_schema, violations)
    return violations


def _validate_record(
    registry: SchemaRegistry, value: Any, sd: SchemaDef,
    path: str, violations: List[Violation],
) -> None:
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
        _validate_against_type(registry, val, field.type_, fpath, violations)
        if len(violations) == before:
            _apply_constraints(val, field.constraints, fpath, violations)
    allowed = {f.name for f in sd.fields}
    for key in value.keys():
        if key not in allowed:
            fpath = f"{path}.{key}" if path else key
            violations.append(Violation(fpath, "WEFT_SCHEMA_UNKNOWN_FIELD", f"unknown field {key!r}"))


def _validate_against_type(registry, value, type_expr, path, violations):
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
            _validate_record(registry, value, registry.schemas[name], path, violations)
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
                _validate_against_type(registry, v, item_type, f"{path}[{i}]", violations)
            return
        if tag == "Map":
            if not isinstance(value, dict):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected Map"))
                return
            if len(type_expr.items) < 2:
                return
            value_type = type_expr.items[1]
            for k, v in value.items():
                _validate_against_type(registry, v, value_type, f"{path}.{k}", violations)
            return
        if tag == "Optional":
            if value is None:
                return
            if type_expr.items:
                _validate_against_type(registry, value, type_expr.items[0], path, violations)
            return
        if tag == "Union":
            saved = list(violations)
            for alt in type_expr.items:
                local: List[Violation] = []
                _validate_against_type(registry, value, alt, path, local)
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
                violations.append(Violation(path, "WEFT_SCHEMA_ENUM", f"must be one of {allowed}, got {value!r}"))
            return
        if tag == "Tuple":
            if not isinstance(value, list):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", "expected List (tuple)"))
                return
            if len(value) != len(type_expr.items):
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"tuple length mismatch: want {len(type_expr.items)}, got {len(value)}"))
                return
            for i, (v, t) in enumerate(zip(value, type_expr.items)):
                _validate_against_type(registry, v, t, f"{path}[{i}]", violations)
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
                    _validate_against_type(registry, v, t, f"{path}#{i}", violations)
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


def _weft_equality_key(x):
    """Return a hashable key such that two Weft values share the key iff
    they are **canonically equal** under the following rules (spec §13):

    * Scalars: equal iff same Weft category **and** same value, except
      Integer/BigInt which compare numerically (``1`` ≡ ``1n``, §5.4); ``1``
      and ``1.0`` likewise.
    * Decimal: equal iff the preserved text form matches
      (``3.14`` ≠ ``3.140``).
    * Binary: equal iff the content bytes match. The ``base64``/``hex``
      flag is a presentation hint only.
    * Float NaN: every NaN is distinct from every other value, including
      itself and other NaNs; the key uses a fresh sentinel each time.
    * Composites (list / map / sexpr / typed): walked recursively using
      the same rules. Map key order is normalised (``{:a 1 :b 2}`` ==
      ``{:b 2 :a 1}``). A NaN leaf inside a composite still forces
      inequality.
    """
    import math as _math
    if x is None:
        return ("null",)
    if isinstance(x, bool):
        return ("bool", x)
    # Weft Integer is one abstract type: SPEC §5.4 says ``42`` and ``42n``
    # denote the same value. The ``n`` suffix is a presentation hint
    # (preserved for round-trip of the exact source form), not a type
    # tag — so ``int`` and ``BigInt`` share the same equality key.
    if isinstance(x, BigInt):
        return ("int", x.value)
    if isinstance(x, int):
        return ("int", x)
    if isinstance(x, float):
        if _math.isnan(x):
            # Fresh sentinel so no two NaN keys ever collide (SPEC §5.16).
            return ("nan", object())
        # SPEC §5.16: Float equality is IEEE-754 bit identity, so
        # -0.0 and 0.0 are distinct values.
        import struct as _struct
        return ("float", _struct.pack("<d", x))
    if isinstance(x, Decimal):
        return ("decimal", x.text)
    if isinstance(x, str):
        return ("string", x)
    if isinstance(x, Symbol):
        return ("symbol", x.name)
    if isinstance(x, Keyword):
        return ("keyword", x.name)
    if isinstance(x, Binary):
        # Encoding is a presentation hint; compare on content bytes.
        return ("binary", bytes(x.data))
    if isinstance(x, WeftDate):
        return ("date", x.year, x.month, x.day, x.has_time, x.hour, x.minute,
                x.second, x.nano, x.tz_offset_minutes)
    if isinstance(x, _dt.datetime):
        return ("date-std", x.isoformat())
    if isinstance(x, _dt.date):
        return ("date-std", x.isoformat())
    if isinstance(x, list):
        return ("list", tuple(_weft_equality_key(it) for it in x))
    if isinstance(x, dict):
        # Sort by key so order doesn't affect identity.
        items = sorted(x.items(), key=lambda kv: kv[0])
        return ("map", tuple((k, _weft_equality_key(v)) for k, v in items))
    if isinstance(x, Sexpr):
        return ("sexpr", x.tag.name,
                tuple(_weft_equality_key(it) for it in x.items))
    if isinstance(x, Typed):
        return ("typed",
                _weft_equality_key(x.type),
                _weft_equality_key(x.value))
    return ("other", repr(x))


def _apply_constraints(value, constraints, path, violations):
    import math as _math
    for k, v in constraints.items():
        if k in ("min", "max", "exclusive-min", "exclusive-max"):
            # NaN never satisfies any numeric bound — spec §9.3.
            if isinstance(value, float) and _math.isnan(value):
                code = "WEFT_SCHEMA_MIN" if k in ("min", "exclusive-min") else "WEFT_SCHEMA_MAX"
                violations.append(Violation(path, code, f"NaN is not within numeric bound {k}={v}"))
                continue
            num_val = _to_num(value)
            num_lim = _to_num(v)
            if num_val is None or num_lim is None:
                violations.append(Violation(path, "WEFT_SCHEMA_TYPE", f"cannot compare {value!r} with {v!r}"))
                continue
            if k == "min" and num_val < num_lim:
                violations.append(Violation(path, "WEFT_SCHEMA_MIN", f"{value} < {v}"))
            elif k == "max" and num_val > num_lim:
                violations.append(Violation(path, "WEFT_SCHEMA_MAX", f"{value} > {v}"))
            elif k == "exclusive-min" and not (num_val > num_lim):
                violations.append(Violation(path, "WEFT_SCHEMA_MIN", f"{value} not > {v}"))
            elif k == "exclusive-max" and not (num_val < num_lim):
                violations.append(Violation(path, "WEFT_SCHEMA_MAX", f"{value} not < {v}"))
        elif k == "min-length":
            if hasattr(value, "__len__") and len(value) < v:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"length {len(value)} < {v}"))
        elif k == "max-length":
            if hasattr(value, "__len__") and len(value) > v:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"length {len(value)} > {v}"))
        elif k == "pattern":
            if isinstance(value, str):
                try:
                    if not _compiled_pattern(v).fullmatch(value):
                        violations.append(Violation(path, "WEFT_SCHEMA_PATTERN", f"does not match /{v}/"))
                except WeftSchemaError as e:
                    violations.append(Violation(path, "WEFT_SCHEMA_PATTERN", f"invalid pattern: {e}"))
        elif k == "items-min":
            if isinstance(value, list) and len(value) < v:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"items {len(value)} < {v}"))
        elif k == "items-max":
            if isinstance(value, list) and len(value) > v:
                violations.append(Violation(path, "WEFT_SCHEMA_LEN", f"items {len(value)} > {v}"))
        elif k == "unique":
            if isinstance(value, list) and v:
                seen = set()
                for i, x in enumerate(value):
                    key = _weft_equality_key(x)
                    if key in seen:
                        violations.append(Violation(f"{path}[{i}]", "WEFT_SCHEMA_UNIQUE", f"duplicate value {x!r}"))
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
