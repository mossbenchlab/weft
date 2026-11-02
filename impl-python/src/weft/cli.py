"""Weft CLI.

Subcommands:
  weft parse <path>              Parse a Weft file and print its JSON shape
  weft format <path>             Reformat a Weft file (canonical form)
  weft validate <path>           Validate without producing output
  weft to-json <path>            Convert Weft to JSON
  weft from-json <path>          Convert JSON to Weft
  weft schema validate <schema> <doc>   Validate a doc against a schema
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    __spec_version__,
    __version__,
    FormatOptions,
    Limits,
    WeftError,
    dumps_json,
    format_value,
    from_json,
    loads_json,
    parse,
    to_json,
    to_json_lossless,
)


def _read_text(path: str) -> str:
    """Read UTF-8 text from ``path`` or stdin. Invalid UTF-8 is reported
    with a Weft-style diagnostic rather than a bare ``UnicodeDecodeError``
    traceback from Python's own codecs."""
    try:
        if path == "-":
            data = sys.stdin.buffer.read()
            return data.decode("utf-8")
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        _die(f"input is not valid UTF-8 ({e.reason} at byte {e.start})", 2)
        raise  # unreachable; _die exits


def _write_text(text: str, path: str | None) -> None:
    """Write ``text`` to ``path`` or stdout. For stdout we go through
    ``sys.stdout.buffer`` so platform newline translation (Python's
    text-mode CRLF on Windows) does not alter the Weft canonical form,
    and append a single LF rather than a potentially-translated newline."""
    if path is None or path == "-":
        encoded = text.encode("utf-8")
        try:
            buf = sys.stdout.buffer
        except AttributeError:
            buf = None
        if buf is not None:
            buf.write(encoded)
            if not text.endswith("\n"):
                buf.write(b"\n")
            buf.flush()
        else:
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
    else:
        # Explicit newline='' so ``\n`` in Weft output isn't translated
        # to the host's convention — the canonical form uses LF only.
        with open(path, "w", encoding="utf-8", newline="") as fp:
            fp.write(text)


def _die(msg: str, code: int = 2) -> None:
    print(f"weft: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_parse(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    try:
        v = parse(text)
    except WeftError as e:
        _die(str(e), 2)
    j = to_json(v)
    _write_text(json.dumps(j, indent=2 if args.pretty else None, ensure_ascii=False), args.output)
    return 0


def cmd_format(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    try:
        v = parse(text)
    except WeftError as e:
        _die(str(e), 2)
    opts = FormatOptions(
        max_width=args.width,
        indent=args.indent,
        sort_keys=args.sort_keys,
        strict_canonical=args.strict_canonical,
    )
    out = format_value(v, opts)
    if args.in_place:
        # LF only — ``write_text`` under the default text-mode newline
        # would translate ``\n`` to CRLF on Windows.
        with open(args.path, "w", encoding="utf-8", newline="") as fp:
            fp.write(out)
    else:
        _write_text(out.rstrip("\n"), args.output)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    try:
        parse(text)
    except WeftError as e:
        _die(str(e), 2)
    print(f"{args.path}: OK")
    return 0


def cmd_to_json(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    try:
        v = parse(text)
    except WeftError as e:
        _die(str(e), 2)
    if args.lossy:
        out = dumps_json(v, indent=2 if args.pretty else None)
    else:
        out = json.dumps(to_json_lossless(v), indent=2 if args.pretty else None,
                         ensure_ascii=False)
    _write_text(out, args.output)
    return 0


def cmd_from_json(args: argparse.Namespace) -> int:
    text = _read_text(args.path)
    # SPEC §11.2: the lossless mapping is the default for from-json;
    # --lossy opts into the §11.3 mapping. loads_json applies the §9
    # limits (size, depth, duplicate keys, NaN rejection, int digits).
    try:
        if args.lossy:
            v = loads_json(text, lossless=False)
        else:
            v = loads_json(text, lossless=True)
        out = format_value(v)
    except WeftError as e:
        _die(str(e), 2)
        return 2  # unreachable
    _write_text(out.rstrip("\n"), args.output)
    return 0


def cmd_schema_validate(args: argparse.Namespace) -> int:
    from .schema import validate_file
    try:
        validate_file(args.schema, args.doc)
    except WeftError as e:
        # Covers SchemaError, WeftSchemaError (regex rejection, bad
        # constraint) and any parse-time WeftError raised while
        # loading the schema or document.
        _die(str(e), 2)
    print(f"{args.doc}: OK")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"weft {__version__} (SPEC {__spec_version__})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="weft", description="Weft toolchain")
    p.add_argument("--version", action="version", version=f"weft {__version__} (SPEC {__spec_version__})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("parse", help="parse a Weft file and print its JSON shape")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    sp.set_defaults(func=cmd_parse)

    sp = sub.add_parser("format", help="reformat a Weft file (canonical)")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("-i", "--in-place", action="store_true")
    sp.add_argument("--width", type=int, default=80)
    sp.add_argument("--indent", type=int, default=2)
    sp.add_argument("--sort-keys", action="store_true")
    sp.add_argument("--strict-canonical", action="store_true")
    sp.set_defaults(func=cmd_format)

    sp = sub.add_parser("validate", help="validate without producing output")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("to-json", help="convert Weft to JSON")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("--pretty", action="store_true")
    sp.add_argument("--lossy", action="store_true",
                    help="use the lossy §11.1 mapping instead of the lossless default")
    sp.set_defaults(func=cmd_to_json)

    sp = sub.add_parser("from-json", help="convert JSON to Weft")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("--lossy", action="store_true",
                    help="use the lossy §11.3 mapping instead of the lossless default")
    sp.set_defaults(func=cmd_from_json)

    schema = sub.add_parser("schema", help="schema operations")
    schema_sub = schema.add_subparsers(dest="schema_cmd", required=True)
    sv = schema_sub.add_parser("validate", help="validate a document against a schema")
    sv.add_argument("schema", help="path to .weft schema")
    sv.add_argument("doc", help="path to .weft document")
    sv.set_defaults(func=cmd_schema_validate)

    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
