// Weft 1.0 — TypeScript reference implementation
//
// This is a self-contained module that parses, formats, and converts Weft
// documents. It mirrors the Python reference implementation and passes the
// language-neutral conformance suite in tests/conformance/.
//
// Public exports are at the bottom of the file.

/* --------------------------------------------------------------------------
 * 0. Value types
 * -------------------------------------------------------------------------- */

export class Symbol_ {
  readonly name: string;
  readonly __brand = "Symbol";
  constructor(name: string) {
    validateIdentifierName(name, "Symbol");
    this.name = name;
  }
  toString(): string { return this.name; }
}

export class Keyword {
  readonly name: string;
  readonly __brand = "Keyword";
  constructor(name: string) {
    validateIdentifierName(name, "Keyword");
    this.name = name;
  }
  toString(): string { return `:${this.name}`; }
}

function validateIdentifierName(name: unknown, label: "Symbol" | "Keyword"): asserts name is string {
  if (typeof name !== "string" || name.length === 0) {
    throw new TypeError(`${label} name must be a non-empty string`);
  }
  // Iterate by code point so a supplementary-plane letter (``𐐀``) is
  // validated as the whole character, not as its surrogate halves.
  // ``for..of`` on a string yields code points as one or two-unit
  // strings — that matches what isSymbolStart / isSymbolCont expect.
  let first: string | null = null;
  let i = 0;
  for (const ch of name) {
    if (first === null) {
      first = ch;
      if (!isSymbolStart(ch)) {
        throw new TypeError(`${label} name ${JSON.stringify(name)}: invalid first character ${JSON.stringify(ch)}`);
      }
    } else if (!isSymbolCont(ch)) {
      throw new TypeError(`${label} name ${JSON.stringify(name)}: invalid character ${JSON.stringify(ch)} at index ${i}`);
    }
    i += ch.length;
  }
  if (label === "Symbol" && RESERVED_SYMBOL_LITERALS.has(name)) {
    throw new TypeError(`${label} name ${JSON.stringify(name)} is reserved as a literal`);
  }
  if (name.length >= 2 && (name[0] === "+" || name[0] === "-")
      && name[1]! >= "0" && name[1]! <= "9") {
    throw new TypeError(
      `${label} name ${JSON.stringify(name)} starts with sign+digit, ` +
      `which ambiguates with a signed number literal`);
  }
}

export class BigIntValue {
  readonly value: bigint;
  readonly __brand = "BigInt";
  constructor(value: bigint) {
    if (typeof value !== "bigint") {
      throw new TypeError("BigIntValue.value must be bigint");
    }
    this.value = value;
  }
}

export type BinaryEncoding = "base64" | "hex";

export class Binary {
  readonly data: Uint8Array;
  readonly encoding: BinaryEncoding;
  readonly __brand = "Binary";
  constructor(data: Uint8Array, encoding: BinaryEncoding = "base64") {
    if (!(data instanceof Uint8Array)) {
      throw new TypeError("Binary.data must be Uint8Array");
    }
    if (encoding !== "base64" && encoding !== "hex") {
      throw new TypeError("Binary.encoding must be 'base64' or 'hex'");
    }
    this.data = data;
    this.encoding = encoding;
  }
}

export class WeftDate {
  // Year is 1..9999. If hasTime=false this represents a plain calendar date.
  readonly year: number;
  readonly month: number;
  readonly day: number;
  readonly hasTime: boolean;
  readonly hour: number;
  readonly minute: number;
  readonly second: number;
  readonly nano: number; // 0..999999999
  readonly tzOffsetMinutes: number | null; // null = no zone
  readonly __brand = "WeftDate";

  constructor(opts: {
    year: number; month: number; day: number;
    hasTime?: boolean; hour?: number; minute?: number; second?: number;
    nano?: number; tzOffsetMinutes?: number | null;
  }) {
    this.year = opts.year;
    this.month = opts.month;
    this.day = opts.day;
    this.hasTime = opts.hasTime ?? false;
    this.hour = opts.hour ?? 0;
    this.minute = opts.minute ?? 0;
    this.second = opts.second ?? 0;
    this.nano = opts.nano ?? 0;
    this.tzOffsetMinutes = opts.tzOffsetMinutes ?? null;
  }
}

export class Decimal {
  // Represented as the exact textual form. The regex is anchored with
  // ``^``/``$`` plus ``/s`` would still match before a trailing newline;
  // we instead use a byte-count check (``\n`` and ``\r`` are forbidden
  // outright) and ASCII digit classes so a wide-digit like ``１`` is
  // rejected (SPEC §5.5). The exponent marker is normalised to
  // lower-case so ``1E2`` and ``1e2`` share ``.text``.
  readonly text: string;
  readonly __brand = "Decimal";
  constructor(text: string) {
    if (typeof text !== "string"
        || !/^[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(text)
        || text.includes("\n") || text.includes("\r")) {
      throw new TypeError(`invalid decimal text: ${JSON.stringify(text)}`);
    }
    // SPEC §7.2 canonicalisation at construction time: leading '+' is
    // dropped (matching the Integer rule) and the exponent marker is
    // lowercased. §5.16 defines Decimal equality on this normalised text.
    if (text.startsWith("+")) text = text.slice(1);
    this.text = text.includes("E") ? text.replace(/E/g, "e") : text;
  }
}

export class Sexpr {
  readonly tag: Symbol_;
  readonly items: WeftValue[];
  readonly __brand = "Sexpr";
  constructor(tag: Symbol_, items: WeftValue[]) {
    this.tag = tag;
    this.items = items;
  }
}

export class Typed {
  readonly value: WeftValue;
  readonly type: Symbol_ | Sexpr;
  readonly __brand = "Typed";
  constructor(value: WeftValue, type: Symbol_ | Sexpr) {
    this.value = value;
    this.type = type;
  }
}

export type WeftScalar =
  | null
  | boolean
  | number       // Float (and small Integer)
  | bigint       // Integer (wide)
  | BigIntValue  // Integer that was written with 'n' suffix
  | Decimal
  | string
  | Binary
  | WeftDate
  | Symbol_
  | Keyword;

export type WeftValue =
  | WeftScalar
  | WeftValue[]
  | { [key: string]: WeftValue }
  | Sexpr
  | Typed;

/* --------------------------------------------------------------------------
 * 1. Errors
 * -------------------------------------------------------------------------- */

export class WeftError extends Error {
  readonly line?: number;
  readonly column?: number;
  readonly offset?: number;
  /** UTF-8 byte offset of the error. For ASCII input this equals ``offset``;
   *  with non-ASCII characters it's the authoritative byte position for
   *  pointing into on-disk files (SPEC §11). */
  readonly byteOffset?: number;
  readonly code?: string;
  constructor(msg: string, opts: {line?: number; column?: number; offset?: number; byteOffset?: number; code?: string} = {}) {
    const prefix = (opts.line !== undefined && opts.column !== undefined)
      ? `[line ${opts.line}:${opts.column}] ` : "";
    super(prefix + msg);
    this.name = this.constructor.name;
    this.line = opts.line;
    this.column = opts.column;
    this.offset = opts.offset;
    this.byteOffset = opts.byteOffset;
    this.code = opts.code;
  }
}
export class WeftSyntaxError extends WeftError {}
export class WeftLexError extends WeftSyntaxError {}
export class WeftLimitError extends WeftError {}
export class WeftEncodingError extends WeftError {}
export class WeftTypeError extends WeftError {}

/* --------------------------------------------------------------------------
 * 2. Limits
 * -------------------------------------------------------------------------- */

export const DEFAULT_MAX_DEPTH = 128;
export const DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024;
export const DEFAULT_MAX_ITEMS_PER_COLL = 1 << 20;
export const DEFAULT_MAX_STRING_BYTES = 10 * 1024 * 1024;
export const DEFAULT_MAX_INT_DIGITS = 10_000;  // spec §9 mandatory default
export const DEFAULT_MAX_TOTAL_ITEMS = 10_000_000;

export const HARD_MAX_DEPTH = 1024;
export const HARD_MAX_SIZE_BYTES = 1 << 30;
export const HARD_MAX_ITEMS_PER_COLL = 1 << 24;
export const HARD_MAX_STRING_BYTES = 1 << 30;
export const HARD_MAX_INT_DIGITS = 1_000_000;
export const HARD_MAX_TOTAL_ITEMS = 100_000_000;

export interface Limits {
  max_depth: number;
  max_size_bytes: number;
  max_items_per_coll: number;
  max_string_bytes: number;
  max_int_digits: number;
  max_total_items: number;
}

export const DEFAULT_LIMITS: Limits = Object.freeze({
  max_depth: DEFAULT_MAX_DEPTH,
  max_size_bytes: DEFAULT_MAX_SIZE_BYTES,
  max_items_per_coll: DEFAULT_MAX_ITEMS_PER_COLL,
  max_string_bytes: DEFAULT_MAX_STRING_BYTES,
  max_int_digits: DEFAULT_MAX_INT_DIGITS,
  max_total_items: DEFAULT_MAX_TOTAL_ITEMS,
});

export function makeLimits(partial: Partial<Limits> = {}): Limits {
  const limits: Limits = { ...DEFAULT_LIMITS, ...partial };
  const checks: Array<[keyof Limits, number, number]> = [
    ["max_depth", 1, HARD_MAX_DEPTH],
    ["max_size_bytes", 1, HARD_MAX_SIZE_BYTES],
    ["max_items_per_coll", 1, HARD_MAX_ITEMS_PER_COLL],
    ["max_string_bytes", 1, HARD_MAX_STRING_BYTES],
    ["max_int_digits", 1, HARD_MAX_INT_DIGITS],
    ["max_total_items", 1, HARD_MAX_TOTAL_ITEMS],
  ];
  for (const [name, lo, hi] of checks) {
    const v = limits[name];
    if (typeof v !== "number" || !Number.isInteger(v)) {
      throw new RangeError(`${name} must be an integer`);
    }
    if (v < lo) throw new RangeError(`${name}=${v} is below minimum ${lo}`);
    if (v > hi) throw new RangeError(`${name}=${v} is above hard max ${hi}`);
  }
  return Object.freeze(limits);
}

/* --------------------------------------------------------------------------
 * 3. Lexer
 * -------------------------------------------------------------------------- */

export const TK = {
  LPAREN: "LPAREN", RPAREN: "RPAREN",
  LBRACK: "LBRACK", RBRACK: "RBRACK",
  LBRACE: "LBRACE", RBRACE: "RBRACE",
  COLONCOLON: "COLONCOLON",
  INT: "INT", BIGINT: "BIGINT",
  FLOAT: "FLOAT", DECIMAL: "DECIMAL",
  STRING: "STRING", BINARY: "BINARY", DATETIME: "DATETIME",
  BOOL: "BOOL", NIL: "NIL", NAN: "NAN", INF: "INF", NEG_INF: "NEG_INF",
  SYMBOL: "SYMBOL", KEYWORD: "KEYWORD",
} as const;
export type TokenKind = keyof typeof TK;

export interface Token {
  kind: TokenKind;
  value: unknown;
  line: number;
  column: number;
  offset: number;
  /** UTF-8 byte offset at the token start. Defaults to -1 if not populated. */
  byteOffset?: number;
}

const WS = new Set(["\t", "\n", "\r", " ", ","]);
const RESERVED_LITERALS = new Set(["true", "false", "nil", "nan", "inf"]);
const SYM_START_ASCII = new Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_+-*/<>=!?");
const SYM_CONT_ASCII = new Set([...SYM_START_ASCII, ..."0123456789"]);
const FORBIDDEN_CTRL = new Set<string>();
for (let c = 0x00; c <= 0x08; c++) FORBIDDEN_CTRL.add(String.fromCharCode(c));
FORBIDDEN_CTRL.add("\x0B"); FORBIDDEN_CTRL.add("\x0C");
for (let c = 0x0E; c <= 0x1F; c++) FORBIDDEN_CTRL.add(String.fromCharCode(c));

// SPEC §4.2 / §10.5: C1 controls (U+0080–U+009F, which include CSI
// U+009B — a terminal-escape injection vector that bypasses C0 CSI's
// \e-based form) and DEL (U+007F) are also rejected inside comments.
const C1_AND_DEL = new Set<string>();
for (let c = 0x7F; c <= 0x9F; c++) C1_AND_DEL.add(String.fromCharCode(c));

// Unicode bidirectional controls (CVE-2021-42574 Trojan Source).
// Rejected in comments where they could reorder visible text.
const BIDI_CONTROL = new Set<string>([
  "‪", "‫", "‬", "‭", "‮",
  "⁦", "⁧", "⁨", "⁩",
]);

const COMMENT_FORBIDDEN = new Set<string>([...FORBIDDEN_CTRL, ...BIDI_CONTROL, ...C1_AND_DEL]);

function isSymbolStart(ch: string): boolean {
  // ``ch`` is a full code-point string: one UTF-16 unit for BMP, two
  // for supplementary (``𐐀`` etc.). ``/\p{L}/u`` with the two-unit
  // string matches the whole code point.
  if (ch.length === 1 && SYM_START_ASCII.has(ch)) return true;
  const cp = ch.codePointAt(0);
  if (cp === undefined || cp <= 0x7F) return false;
  return /\p{L}/u.test(ch);
}
function isSymbolCont(ch: string): boolean {
  if (ch.length === 1 && SYM_CONT_ASCII.has(ch)) return true;
  const cp = ch.codePointAt(0);
  if (cp === undefined) return false;
  if (cp <= 0x7F) return false;
  // Above ASCII: Letters only. Unicode digits (Nd / No, e.g. ``１`` /
  // ``①`` / ``¼``) are NOT symbol-cont — the formatter cannot
  // round-trip them, so the lexer refuses them too (SPEC §5.10).
  return /\p{L}/u.test(ch);
}

/** Read a code point starting at ``pos``. Returns the one- or two-unit
 *  string and the number of UTF-16 code units it occupies. */
function readCodePointAt(text: string, pos: number): {ch: string; units: number} | null {
  if (pos >= text.length) return null;
  const cp = text.codePointAt(pos);
  if (cp === undefined) return null;
  const units = cp > 0xFFFF ? 2 : 1;
  return {ch: String.fromCodePoint(cp), units};
}

function isRejectedWhitespace(ch: string): boolean {
  if (WS.has(ch)) return false;
  const cp = ch.charCodeAt(0);
  if (cp === 0x0B || cp === 0x0C) return true;
  if (cp === 0xA0) return true;
  if (cp >= 0x2000 && cp <= 0x200A) return true;
  if (cp === 0x2028 || cp === 0x2029 || cp === 0x205F || cp === 0x3000) return true;
  return false;
}

function posFromOffset(text: string, offset: number): [number, number] {
  let line = 1, col = 1;
  const n = Math.min(offset, text.length);
  for (let i = 0; i < n; i++) {
    const ch = text[i]!;
    if (ch === "\n") { line++; col = 1; }
    else if (ch === "\r") {
      // CR as line terminator; CRLF handled by the following \n.
      if (i + 1 < n && text[i + 1] === "\n") { /* defer */ }
      else { line++; col = 1; }
    } else {
      col++;
    }
  }
  return [line, col];
}

function isDelOrC1(ch: string): boolean {
  const cc = ch.charCodeAt(0);
  return cc === 0x7f || (cc >= 0x80 && cc <= 0x9f);
}

class Lexer {
  text: string;
  pos = 0;
  bytePos = 0;  // UTF-8 byte offset, kept in step with pos
  line = 1;
  col = 1;
  limits: Limits;

  constructor(text: string, limits: Limits = DEFAULT_LIMITS) {
    if (typeof text !== "string") throw new TypeError("Lexer input must be string");

    // BOM check
    if (text.includes("﻿")) {
      const idx = text.indexOf("﻿");
      const [line, col] = posFromOffset(text, idx);
      throw new WeftEncodingError("U+FEFF (BOM) is not permitted anywhere in a Weft document",
        {line, column: col, offset: idx, code: "WEFT_ENCODING_BOM"});
    }
    for (const bad of ["￾", "￿"]) {
      if (text.includes(bad)) {
        const idx = text.indexOf(bad);
        const [line, col] = posFromOffset(text, idx);
        throw new WeftEncodingError(`noncharacter U+${bad.charCodeAt(0).toString(16).toUpperCase()} is not permitted`,
          {line, column: col, offset: idx, code: "WEFT_ENCODING_NONCHAR"});
      }
    }
    // Size check
    const size = new TextEncoder().encode(text).length;
    if (size > limits.max_size_bytes) {
      throw new WeftLimitError(
        `document size ${size} exceeds max_size_bytes=${limits.max_size_bytes}`,
        {code: "WEFT_LIMIT_SIZE"});
    }
    // Surrogate check — JS strings may contain unpaired surrogates.
    for (let i = 0; i < text.length; i++) {
      const cp = text.charCodeAt(i);
      if (cp >= 0xD800 && cp <= 0xDBFF) {
        // High surrogate — must be followed by low surrogate
        const nxt = i + 1 < text.length ? text.charCodeAt(i + 1) : 0;
        if (!(nxt >= 0xDC00 && nxt <= 0xDFFF)) {
          const [line, col] = posFromOffset(text, i);
          throw new WeftEncodingError(`unpaired surrogate U+${cp.toString(16).toUpperCase()} is not permitted`,
            {line, column: col, offset: i, code: "WEFT_ENCODING_SURROGATE"});
        }
        i++; // skip low surrogate
      } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
        const [line, col] = posFromOffset(text, i);
        throw new WeftEncodingError(`unpaired surrogate U+${cp.toString(16).toUpperCase()} is not permitted`,
          {line, column: col, offset: i, code: "WEFT_ENCODING_SURROGATE"});
      }
    }

    this.text = text;
    this.limits = limits;
  }

  private peek(o = 0): string | undefined {
    return this.text[this.pos + o];
  }
  private advance(n = 1): void {
    for (let i = 0; i < n; i++) {
      if (this.pos >= this.text.length) return;
      const ch = this.text[this.pos]!;
      if (ch === "\n") {
        this.line++; this.col = 1;
      } else if (ch === "\r") {
        // CRLF: skip, the \n handles the line count.
        // Lone CR: counts as a line terminator (SPEC §3).
        if (this.pos + 1 < this.text.length && this.text[this.pos + 1] === "\n") {
          // defer line++ to the next \n
        } else {
          this.line++; this.col = 1;
        }
      } else {
        this.col++;
      }
      // UTF-8 byte width of this UTF-16 code unit:
      // 1 byte for ASCII, 2 for U+0080..U+07FF, 3 for U+0800..U+FFFF
      // (non-surrogate), 2 per surrogate half (4 bytes split across the
      // high + low surrogates for a supplementary code point).
      const cp = ch.charCodeAt(0);
      if (cp < 0x80) this.bytePos += 1;
      else if (cp < 0x800) this.bytePos += 2;
      else if (cp >= 0xD800 && cp <= 0xDFFF) this.bytePos += 2;
      else this.bytePos += 3;
      this.pos++;
    }
  }
  private err(msg: string, code = "WEFT_SYNTAX"): WeftLexError {
    return new WeftLexError(msg, {line: this.line, column: this.col, offset: this.pos, byteOffset: this.bytePos, code});
  }
  private limitErr(msg: string, code: string): WeftLimitError {
    return new WeftLimitError(msg, {line: this.line, column: this.col, offset: this.pos, byteOffset: this.bytePos, code});
  }

  *tokenize(): Generator<Token> {
    while (true) {
      const tok = this.nextToken();
      if (tok === null) return;
      yield tok;
    }
  }

  private nextToken(): Token | null {
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      if (WS.has(ch)) { this.advance(); continue; }
      if (isRejectedWhitespace(ch)) {
        throw this.err(`Unicode whitespace U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted outside strings/comments`,
          "WEFT_SYNTAX_WS");
      }
      if (ch === ";") { this.skipLineComment(); continue; }
      if (ch === "#" && this.peek(1) === "|") { this.skipBlockComment(); continue; }
      break;
    }
    if (this.pos >= this.text.length) return null;

    const sl = this.line, sc = this.col, so = this.pos;
    const ch = this.text[this.pos]!;
    const nxt = this.peek(1);

    if (ch === "(") { this.advance(); return {kind: "LPAREN", value: null, line: sl, column: sc, offset: so}; }
    if (ch === ")") { this.advance(); return {kind: "RPAREN", value: null, line: sl, column: sc, offset: so}; }
    if (ch === "[") { this.advance(); return {kind: "LBRACK", value: null, line: sl, column: sc, offset: so}; }
    if (ch === "]") { this.advance(); return {kind: "RBRACK", value: null, line: sl, column: sc, offset: so}; }
    if (ch === "{") { this.advance(); return {kind: "LBRACE", value: null, line: sl, column: sc, offset: so}; }
    if (ch === "}") { this.advance(); return {kind: "RBRACE", value: null, line: sl, column: sc, offset: so}; }
    if (ch === ":" && nxt === ":") { this.advance(2); return {kind: "COLONCOLON", value: null, line: sl, column: sc, offset: so}; }

    if (ch === ":") {
      this.advance();
      const name = this.readSymbolName();
      if (!name) {
        throw new WeftLexError("':' must be followed by a symbol name",
          {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_KEYWORD"});
      }
      // SPEC §5.10: a name beginning with sign+digit is reserved for
      // signed-number literals — ``:+1`` must not parse, even though
      // ``+`` is a symbol-start and ``1`` is a symbol-cont. Keeps the
      // lexer's acceptance set in step with the formatter's output set.
      if (name.length >= 2 && (name[0] === "+" || name[0] === "-")
          && name[1]! >= "0" && name[1]! <= "9") {
        throw new WeftLexError(
          `keyword name ${JSON.stringify(name)} starts with sign+digit, ` +
          `which ambiguates with a signed number literal`,
          {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_KEYWORD"});
      }
      return {kind: "KEYWORD", value: name, line: sl, column: sc, offset: so};
    }

    if (ch === '"') {
      if (this.peek(1) === '"' && this.peek(2) === '"') {
        const s = this.readTripleString();
        return {kind: "STRING", value: s, line: sl, column: sc, offset: so};
      }
      const s = this.readDquoteString();
      return {kind: "STRING", value: s, line: sl, column: sc, offset: so};
    }
    if (ch === "r" && nxt === '"') {
      this.advance();
      const s = this.readRawString();
      return {kind: "STRING", value: s, line: sl, column: sc, offset: so};
    }
    if (ch === "b" && nxt === '"') {
      this.advance();
      const b = this.readBinary();
      return {kind: "BINARY", value: b, line: sl, column: sc, offset: so};
    }

    if (ch === "#") {
      if (nxt === undefined) throw this.err("stray '#' at end of input", "WEFT_SYNTAX_HASH");
      if (nxt === "!") throw this.err("'#!' at document start is reserved", "WEFT_RESERVED_SHEBANG");
      if (/\d/.test(nxt)) {
        return this.readDatetime(sl, sc, so);
      }
      throw this.err(`'#${nxt}' is a reserved syntax; see SPEC §12`, "WEFT_RESERVED_HASH");
    }

    if (ch === "+" || ch === "-" || /\d/.test(ch)) {
      const tok = this.readNumberOrSignLit(sl, sc, so);
      if (tok !== null) return tok;
    }

    // Check symbol-start on the full code point so a supplementary-plane
    // letter (``𐐀``) isn't mis-dispatched as its high surrogate.
    const cp0 = readCodePointAt(this.text, this.pos);
    if (cp0 && isSymbolStart(cp0.ch)) {
      const name = this.readSymbolName();
      if (RESERVED_LITERALS.has(name)) {
        if (name === "true") return {kind: "BOOL", value: true, line: sl, column: sc, offset: so};
        if (name === "false") return {kind: "BOOL", value: false, line: sl, column: sc, offset: so};
        if (name === "nil") return {kind: "NIL", value: null, line: sl, column: sc, offset: so};
        if (name === "nan") return {kind: "NAN", value: NaN, line: sl, column: sc, offset: so};
        if (name === "inf") return {kind: "INF", value: Infinity, line: sl, column: sc, offset: so};
      }
      if (name.startsWith("$")) {
        throw new WeftLexError(`symbols beginning with '$' are reserved: ${name}`,
          {line: sl, column: sc, offset: so, code: "WEFT_RESERVED_DOLLAR"});
      }
      if (name.length >= 2 && (name[0] === "+" || name[0] === "-") && /\d/.test(name[1]!)) {
        throw new WeftLexError(`invalid symbol ${name}: starts with sign+digit`,
          {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_SYMBOL_AMBIGUOUS"});
      }
      return {kind: "SYMBOL", value: name, line: sl, column: sc, offset: so};
    }

    // Error path: report the full code point, not just the first UTF-16 unit.
    const errCp = this.text.codePointAt(this.pos) ?? ch.charCodeAt(0);
    throw new WeftLexError(`unexpected character U+${errCp.toString(16).toUpperCase()}`,
      {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_CHAR"});
  }

  private skipLineComment(): void {
    this.advance();
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      // Any Weft line terminator ends a line comment: LF, CRLF, or lone CR
      // (SPEC §3). We stop here and let the main loop consume the newline
      // so it bumps the line counter via advance().
      if (ch === "\n" || ch === "\r") return;
      if (COMMENT_FORBIDDEN.has(ch)) {
        throw this.err(`character U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted in comment`,
          "WEFT_SYNTAX_COMMENT_CTRL");
      }
      this.advance();
    }
  }

  private skipBlockComment(): void {
    this.advance(2);
    let depth = 1;
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      const nxt = this.peek(1);
      if (ch === "#" && nxt === "|") { depth++; this.advance(2); continue; }
      if (ch === "|" && nxt === "#") { depth--; this.advance(2); if (depth === 0) return; continue; }
      if (COMMENT_FORBIDDEN.has(ch)) {
        throw this.err(`character U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted in comment`,
          "WEFT_SYNTAX_COMMENT_CTRL");
      }
      this.advance();
    }
    throw this.err("unterminated block comment", "WEFT_SYNTAX_COMMENT_UNTERMINATED");
  }

  private readSymbolName(): string {
    const start = this.pos;
    // Advance by full code point so a supplementary-plane letter (``𐐀``)
    // consumes both of its UTF-16 surrogate units in one step.
    const first = readCodePointAt(this.text, this.pos);
    if (!first || !isSymbolStart(first.ch)) return "";
    this.advance(first.units);
    while (this.pos < this.text.length) {
      const c = readCodePointAt(this.text, this.pos);
      if (!c || !isSymbolCont(c.ch)) break;
      this.advance(c.units);
    }
    return this.text.slice(start, this.pos);
  }

  private readDquoteString(): string {
    this.advance();
    const out: string[] = [];
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      if (ch === '"') {
        this.advance();
        const s = out.join("");
        this.checkStringLimit(s);
        return s;
      }
      if (FORBIDDEN_CTRL.has(ch)) {
        throw this.err(`control character U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted in string`,
          "WEFT_SYNTAX_STR_CTRL");
      }
      if (ch === "\n" || ch === "\t" || ch === "\r") {
        throw this.err("literal tab/newline/CR not permitted in standard string", "WEFT_SYNTAX_STR_CTRL");
      }
      if (ch === "\\") { out.push(this.readEscape()); continue; }
      out.push(ch);
      this.advance();
    }
    throw this.err("unterminated string literal", "WEFT_SYNTAX_STR_UNTERMINATED");
  }

  // SPEC §5.7: literal DEL (U+007F) and C1 (U+0080-U+009F) are forbidden
  // inside raw and triple-quoted strings (C0 except TAB/LF/CR already is).
  private readTripleString(): string {
    this.advance(3);
    const out: string[] = [];
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      if (ch === '"' && this.peek(1) === '"' && this.peek(2) === '"') {
        this.advance(3);
        const s = out.join("");
        this.checkStringLimit(s);
        return s;
      }
      if (FORBIDDEN_CTRL.has(ch) || isDelOrC1(ch)) {
        throw this.err(`control character U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted in string`,
          "WEFT_SYNTAX_STR_CTRL");
      }
      if (ch === "\\") { out.push(this.readEscape()); continue; }
      out.push(ch);
      this.advance();
    }
    throw this.err("unterminated triple-quoted string", "WEFT_SYNTAX_STR_UNTERMINATED");
  }

  private readRawString(): string {
    this.advance();
    const out: string[] = [];
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      if (ch === '"') {
        this.advance();
        const s = out.join("");
        this.checkStringLimit(s);
        return s;
      }
      if (FORBIDDEN_CTRL.has(ch) || isDelOrC1(ch)) {
        throw this.err(`control character U+${ch.charCodeAt(0).toString(16).toUpperCase()} not permitted in raw string`,
          "WEFT_SYNTAX_STR_CTRL");
      }
      out.push(ch);
      this.advance();
    }
    throw this.err("unterminated raw string", "WEFT_SYNTAX_STR_UNTERMINATED");
  }

  private readEscape(): string {
    this.advance();
    if (this.pos >= this.text.length) throw this.err("truncated escape", "WEFT_SYNTAX_ESCAPE");
    const c = this.text[this.pos]!;
    const simple: Record<string, string> = {n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "0": "\x00"};
    if (c in simple) { this.advance(); return simple[c]!; }
    if (c === "x") {
      this.advance();
      const h = this.readHexDigits(2);
      const cp = parseInt(h, 16);
      if (cp > 0x7F) throw this.err(`\\x${h} escapes bytes 00-7F only`, "WEFT_SYNTAX_ESCAPE_XRANGE");
      return String.fromCodePoint(cp);
    }
    if (c === "u") {
      this.advance();
      let cp: number;
      if (this.text[this.pos] === "{") {
        this.advance();
        const hex = this.readHexUntilBrace();
        this.advance(); // consume }
        cp = parseInt(hex, 16);
      } else {
        const hex = this.readHexDigits(4);
        cp = parseInt(hex, 16);
      }
      if (cp > 0x10FFFF) throw this.err(`\\u escape U+${cp.toString(16).toUpperCase()} exceeds Unicode max`, "WEFT_SYNTAX_ESCAPE_MAX");
      if (cp >= 0xD800 && cp <= 0xDFFF) throw this.err(`\\u escape U+${cp.toString(16).toUpperCase()} is a surrogate half`, "WEFT_SYNTAX_ESCAPE_SURROGATE");
      return String.fromCodePoint(cp);
    }
    throw this.err(`unknown escape \\${c}`, "WEFT_SYNTAX_ESCAPE_UNKNOWN");
  }

  private readHexDigits(n: number): string {
    const start = this.pos;
    for (let i = 0; i < n; i++) {
      if (this.pos >= this.text.length) throw this.err("truncated hex escape", "WEFT_SYNTAX_ESCAPE");
      const ch = this.text[this.pos]!;
      if (!/[0-9a-fA-F]/.test(ch)) throw this.err(`invalid hex digit ${ch}`, "WEFT_SYNTAX_ESCAPE");
      this.advance();
    }
    return this.text.slice(start, this.pos);
  }

  private readHexUntilBrace(): string {
    const start = this.pos;
    let count = 0;
    while (this.pos < this.text.length && this.text[this.pos] !== "}") {
      const ch = this.text[this.pos]!;
      if (!/[0-9a-fA-F]/.test(ch)) throw this.err("invalid hex digit in \\u{...}", "WEFT_SYNTAX_ESCAPE");
      this.advance();
      count++;
      if (count > 6) throw this.err("\\u{...} has more than 6 hex digits", "WEFT_SYNTAX_ESCAPE");
    }
    if (count < 1) throw this.err("\\u{...} has no hex digits", "WEFT_SYNTAX_ESCAPE");
    if (this.pos >= this.text.length) throw this.err("missing } in \\u{...}", "WEFT_SYNTAX_ESCAPE");
    return this.text.slice(start, this.pos);
  }

  private checkStringLimit(s: string): void {
    const bytes = new TextEncoder().encode(s).length;
    if (bytes > this.limits.max_string_bytes) {
      throw this.limitErr(`string ${bytes} bytes exceeds max_string_bytes=${this.limits.max_string_bytes}`, "WEFT_LIMIT_STRING");
    }
  }

  private readBinary(): Binary {
    this.advance();
    const start = this.pos;
    while (this.pos < this.text.length && this.text[this.pos] !== '"') {
      const ch = this.text[this.pos]!;
      if (FORBIDDEN_CTRL.has(ch) || ch === " " || ch === "\n" || ch === "\t" || ch === "\r") {
        throw this.err("whitespace/control chars not permitted in binary literal", "WEFT_SYNTAX_BINARY");
      }
      this.advance();
    }
    if (this.pos >= this.text.length) throw this.err("unterminated binary", "WEFT_SYNTAX_BINARY");
    const body = this.text.slice(start, this.pos);
    this.advance();
    if (body.startsWith("base64:")) {
      const payload = body.slice("base64:".length);
      if (payload === "") return new Binary(new Uint8Array(0), "base64");
      try {
        const bytes = base64Decode(payload);
        return new Binary(bytes, "base64");
      } catch (e) {
        throw this.err(`invalid base64: ${(e as Error).message}`, "WEFT_SYNTAX_BINARY_BASE64");
      }
    }
    if (body.startsWith("hex:")) {
      const payload = body.slice("hex:".length);
      if (payload.length % 2 !== 0) throw this.err("hex binary must have even number of digits", "WEFT_SYNTAX_BINARY_HEX");
      try {
        const bytes = hexDecode(payload);
        return new Binary(bytes, "hex");
      } catch (e) {
        throw this.err(`invalid hex: ${(e as Error).message}`, "WEFT_SYNTAX_BINARY_HEX");
      }
    }
    throw this.err("binary literal must start with 'base64:' or 'hex:'", "WEFT_SYNTAX_BINARY");
  }

  private readDatetime(sl: number, sc: number, so: number): Token {
    this.advance();
    const start = this.pos;
    while (this.pos < this.text.length) {
      const ch = this.text[this.pos]!;
      const nx = this.text[this.pos + 1];
      if (WS.has(ch) || ch === ")" || ch === "]" || ch === "}"
          || ch === "(" || ch === "[" || ch === "{") break;
      // SPEC §4.3: `"` is a token boundary — a string literal may directly
      // follow a datetime ([#2026-11-02"a"]).
      if (ch === '"') break;
      // ``;`` starts a line comment and ``#|`` starts a block comment —
      // both terminate the datetime literal, otherwise the body would
      // swallow the comment and produce silently wrong output.
      if (ch === ";") break;
      if (ch === "#" && nx === "|") break;
      // Terminate on "::" so a type annotation following a datetime is lexed separately.
      if (ch === ":" && nx === ":") break;
      this.advance();
    }
    const body = this.text.slice(start, this.pos);
    const value = parseDatetime(body, (msg, code) => new WeftLexError(msg, {line: sl, column: sc, offset: so, code}));
    return {kind: "DATETIME", value, line: sl, column: sc, offset: so};
  }

  private readNumberOrSignLit(sl: number, sc: number, so: number): Token | null {
    const text = this.text;
    const n = text.length;
    let p = this.pos;
    let sign = "";
    if (text[p] === "+" || text[p] === "-") { sign = text[p]!; p++; }

    // Check for signed inf
    if (text.slice(p, p + 3) === "inf" && (p + 3 === n || !isSymbolCont(text[p + 3]!))) {
      const diff = p + 3 - this.pos;
      for (let i = 0; i < diff; i++) this.advance();
      if (sign === "-") return {kind: "NEG_INF", value: -Infinity, line: sl, column: sc, offset: so};
      return {kind: "INF", value: Infinity, line: sl, column: sc, offset: so};
    }

    if (p >= n || !/\d/.test(text[p]!)) return null;

    // Hex / Oct / Bin
    if (text[p] === "0" && p + 1 < n && /[xXoObB]/.test(text[p + 1]!)) {
      const base_char = text[p + 1]!.toLowerCase() as "x" | "o" | "b";
      p += 2;
      const digit_start = p;
      const allowed: Record<string, RegExp> = {
        x: /[0-9a-fA-F_]/,
        o: /[0-7_]/,
        b: /[01_]/,
      };
      while (p < n && allowed[base_char]!.test(text[p]!)) p++;
      const digits_text = text.slice(digit_start, p);
      validateUnderscores(digits_text, sl, sc, so);
      const cleaned = digits_text.replace(/_/g, "");
      if (!cleaned) throw new WeftLexError(`base-${base_char} integer has no digits`, {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_INT"});
      const base = base_char === "x" ? 16 : base_char === "o" ? 8 : 2;
      // Check against decimal-equivalent digit count so a 4,000-digit
      // ``0xf…f`` cannot sneak past max_int_digits only to blow it up
      // when the formatter emits the same value in base 10.
      this.checkIntDigits(cleaned, sl, sc, so, base);
      let bigVal = BigInt("0") * BigInt(1);
      // Parse via BigInt to avoid precision loss
      bigVal = baseToBigInt(cleaned, base);
      if (sign === "-") bigVal = -bigVal;
      if (p < n && text[p] === "n") {
        p++;
        const diff = p - this.pos;
        for (let i = 0; i < diff; i++) this.advance();
        if (this.pos < n && isSymbolCont(text[this.pos]!)) throw this.err("unexpected char after 'n' suffix", "WEFT_SYNTAX_INT");
        return {kind: "BIGINT", value: new BigIntValue(bigVal), line: sl, column: sc, offset: so};
      }
      const diff = p - this.pos;
      for (let i = 0; i < diff; i++) this.advance();
      if (this.pos < n && isSymbolCont(text[this.pos]!)) throw this.err("unexpected char after integer; only 'n' (BigInt) and 'm' (Decimal) suffixes are supported", "WEFT_RESERVED_NUMBER_SUFFIX");
      return intToken(bigVal, sl, sc, so);
    }

    // Decimal
    const int_start = p;
    while (p < n && (/\d/.test(text[p]!) || text[p] === "_")) p++;
    const int_text = text.slice(int_start, p);
    validateUnderscores(int_text, sl, sc, so);
    const int_cleaned = int_text.replace(/_/g, "");
    if (!int_cleaned) throw new WeftLexError("number has no digits", {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_INT"});

    let is_float = false;
    let frac_cleaned = "";
    if (p < n && text[p] === ".") {
      if (p + 1 >= n || !/\d/.test(text[p + 1]!)) {
        throw new WeftLexError("float literal must have digits after '.'",
          {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_FLOAT_DOT"});
      }
      is_float = true;
      p++;
      const frac_start = p;
      while (p < n && (/\d/.test(text[p]!) || text[p] === "_")) p++;
      const frac_text = text.slice(frac_start, p);
      validateUnderscores(frac_text, sl, sc, so);
      frac_cleaned = frac_text.replace(/_/g, "");
    }

    let exp_text = "";
    let exp_sign = "";
    if (p < n && (text[p] === "e" || text[p] === "E")) {
      is_float = true;
      p++;
      if (p < n && (text[p] === "+" || text[p] === "-")) { exp_sign = text[p]!; p++; }
      const exp_start = p;
      while (p < n && (/\d/.test(text[p]!) || text[p] === "_")) p++;
      const exp_raw = text.slice(exp_start, p);
      validateUnderscores(exp_raw, sl, sc, so);
      exp_text = exp_raw.replace(/_/g, "");
      if (!exp_text) throw new WeftLexError("exponent has no digits", {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_FLOAT_EXP"});
      // Exponent digits are counted against max_int_digits (prevents mega-exp DoS).
      this.checkIntDigits(exp_text, sl, sc, so);
    }

    let suffix = "";
    if (p < n && (text[p] === "n" || text[p] === "m")) {
      suffix = text[p]!;
      p++;
      if (p < n && isSymbolCont(text[p]!)) {
        const diff = p - this.pos;
        for (let i = 0; i < diff; i++) this.advance();
        throw this.err(`unexpected char after '${suffix}' suffix`, "WEFT_SYNTAX_NUMBER_SUFFIX");
      }
      if (suffix === "n" && is_float) {
        const diff = p - this.pos;
        for (let i = 0; i < diff; i++) this.advance();
        throw new WeftLexError("'n' suffix is only valid on integer literals",
          {line: sl, column: sc, offset: so, code: "WEFT_SYNTAX_NUMBER_SUFFIX"});
      }
    } else if (p < n && isSymbolCont(text[p]!)) {
      const diff = p - this.pos;
      for (let i = 0; i < diff; i++) this.advance();
      throw this.err("unexpected char after number; only 'n' (BigInt) and 'm' (Decimal) suffixes supported",
        "WEFT_RESERVED_NUMBER_SUFFIX");
    }

    this.checkIntDigits(int_cleaned + frac_cleaned, sl, sc, so);

    const diff = p - this.pos;
    for (let i = 0; i < diff; i++) this.advance();

    if (suffix === "n") {
      let v = BigInt(int_cleaned);
      if (sign === "-") v = -v;
      return {kind: "BIGINT", value: new BigIntValue(v), line: sl, column: sc, offset: so};
    }
    if (suffix === "m") {
      const txt = (sign === "-" ? "-" : "") + int_cleaned +
        (frac_cleaned ? "." + frac_cleaned : "") +
        (exp_text ? "e" + exp_sign + exp_text : "");
      return {kind: "DECIMAL", value: new Decimal(txt), line: sl, column: sc, offset: so};
    }
    if (is_float) {
      const txt = (sign === "-" ? "-" : "") + int_cleaned +
        (frac_cleaned ? "." + frac_cleaned : "") +
        (exp_text ? "e" + exp_sign + exp_text : "");
      return {kind: "FLOAT", value: Number(txt), line: sl, column: sc, offset: so};
    }
    let v = BigInt(int_cleaned);
    if (sign === "-") v = -v;
    return intToken(v, sl, sc, so);
  }

  private checkIntDigits(cleaned: string, sl: number, sc: number, so: number, base: number = 10): void {
    // SPEC §9.5: ``max_int_digits`` is a decimal-digit budget. For
    // non-decimal bases we must compare against the decimal-equivalent
    // length — ceil(len * log10(base)) is a tight upper bound on the
    // base-10 string that the formatter would emit.
    const BASE_LOG10: Record<number, number> = {
      2:  0.30102999566398114,
      8:  0.9030899869919435,
      10: 1.0,
      16: 1.2041199826559248,
    };
    const decLen = base === 10
      ? cleaned.length
      : Math.ceil(cleaned.length * BASE_LOG10[base]!);
    if (decLen > this.limits.max_int_digits) {
      const detail = base === 10
        ? `integer has ${decLen} digits`
        : `integer has ${decLen} decimal digits (${cleaned.length} base-${base} digits)`;
      throw new WeftLimitError(
        `${detail}, exceeds max_int_digits=${this.limits.max_int_digits}`,
        {line: sl, column: sc, offset: so, code: "WEFT_LIMIT_INT"});
    }
  }
}

function intToken(v: bigint, sl: number, sc: number, so: number): Token {
  // All Weft Integer values are represented as bigint in TS, regardless of
  // range. This keeps Integer and Float distinct at the type level, which
  // matters because JS has one Number type for both.
  return {kind: "INT", value: v, line: sl, column: sc, offset: so};
}

function validateUnderscores(text: string, line: number, col: number, off: number): void {
  if (text.includes("__")) {
    throw new WeftLexError("consecutive underscores not permitted in numeric literal",
      {line, column: col, offset: off, code: "WEFT_SYNTAX_NUMBER_UNDERSCORE"});
  }
  if (text.startsWith("_") || text.endsWith("_")) {
    throw new WeftLexError("underscore cannot be first or last digit",
      {line, column: col, offset: off, code: "WEFT_SYNTAX_NUMBER_UNDERSCORE"});
  }
}

function baseToBigInt(digits: string, base: number): bigint {
  const b = BigInt(base);
  let v = BigInt(0);
  for (const ch of digits) {
    const d = parseInt(ch, 16);
    v = v * b + BigInt(d);
  }
  return v;
}

function base64Decode(s: string): Uint8Array {
  // SPEC §5.8: strict base64 with required padding AND canonical
  // trailing bits (RFC 4648 §3.5). The canonical check is enforced by
  // re-encoding the decoded bytes and comparing to the input: a
  // non-canonical string like ``AB==`` decodes to the same bytes as
  // ``AA==`` and so round-trips to ``AA==``, failing the equality.
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s)) throw new Error("invalid base64 character");
  if (s.length % 4 !== 0) throw new Error("base64 length not a multiple of 4");
  // Use Buffer in Node, atob in browser
  let bytes: Uint8Array;
  if (typeof Buffer !== "undefined") {
    bytes = new Uint8Array(Buffer.from(s, "base64"));
  } else {
    const bin = atob(s);
    bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  }
  // W4-16: canonical check — re-encode and compare.
  const reEncoded = base64Encode(bytes);
  if (reEncoded !== s) {
    throw new Error("non-canonical base64 (trailing bits must be zero)");
  }
  return bytes;
}

function base64Encode(bytes: Uint8Array): string {
  if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function hexDecode(s: string): Uint8Array {
  if (!/^[0-9a-fA-F]*$/.test(s)) throw new Error("invalid hex character");
  // Odd-length hex is ambiguous — the lexer rejects ``b"hex:abc"`` so the
  // JSON lossless decoder must do the same, else the same data would be
  // accepted via one surface and silently truncated via the other.
  if (s.length % 2 !== 0) throw new Error("hex must have even number of digits");
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function hexEncode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += b.toString(16).padStart(2, "0");
  return s;
}

const DATETIME_RE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?)?$/;

function parseDatetime(body: string, errFactory: (msg: string, code: string) => WeftError): WeftDate {
  const m = DATETIME_RE.exec(body);
  if (!m) throw errFactory(`invalid datetime literal: ${body}`, "WEFT_SYNTAX_DATETIME");
  const year = parseInt(m[1]!, 10);
  const month = parseInt(m[2]!, 10);
  const day = parseInt(m[3]!, 10);
  if (year < 1 || year > 9999) throw errFactory(`datetime year ${year} out of range 1-9999`, "WEFT_SYNTAX_DATETIME_YEAR");
  if (month < 1 || month > 12) throw errFactory(`invalid month ${month}`, "WEFT_SYNTAX_DATETIME");
  const daysInMonth = [31, (isLeap(year) ? 29 : 28), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (day < 1 || day > daysInMonth[month - 1]!) throw errFactory(`invalid day ${day} for month ${month}`, "WEFT_SYNTAX_DATETIME");
  if (m[4] === undefined) {
    return new WeftDate({year, month, day, hasTime: false});
  }
  const hour = parseInt(m[4]!, 10);
  const minute = parseInt(m[5]!, 10);
  const second = parseInt(m[6]!, 10);
  if (hour > 23 || minute > 59 || second > 60) throw errFactory(`invalid time ${hour}:${minute}:${second}`, "WEFT_SYNTAX_DATETIME");
  const frac = m[7] ?? "";
  let nano = 0;
  if (frac) {
    nano = parseInt((frac + "000000000").slice(0, 9), 10);
  }
  const zone = m[8];
  let tzOffsetMinutes: number | null = null;
  if (zone === "Z") tzOffsetMinutes = 0;
  else if (zone !== undefined) {
    const sign = zone[0] === "+" ? 1 : -1;
    const hh = parseInt(zone.slice(1, 3), 10);
    const mm = parseInt(zone.slice(4, 6), 10);
    if (hh > 14 || mm > 59 || (hh === 14 && mm > 0)) {
      throw errFactory(`timezone offset ${zone} out of range (|offset| <= 14:00)`, "WEFT_SYNTAX_DATETIME");
    }
    tzOffsetMinutes = sign * (hh * 60 + mm);
  }
  // Leap seconds (:60) only occur at the end of a UTC day — 23:59:60Z —
  // and in local time they appear at the hour / minute that maps back to
  // 23:59 UTC. ``08:59:60+09:00`` is legal; ``00:00:60Z`` is not. If
  // the offset is unknown we fall back to the UTC rule.
  if (second === 60) {
    const off = tzOffsetMinutes ?? 0;
    let totalMinutes = hour * 60 + minute - off;
    totalMinutes = ((totalMinutes % 1440) + 1440) % 1440;
    if (totalMinutes !== 23 * 60 + 59) {
      const zoneLabel = zone === "Z" ? "Z" : (zone ?? "local");
      throw errFactory(
        `leap-second :60 only valid at 23:59:60 UTC ` +
        `(got ${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:60 at offset ${zoneLabel})`,
        "WEFT_SYNTAX_DATETIME");
    }
  }
  return new WeftDate({year, month, day, hasTime: true, hour, minute, second, nano, tzOffsetMinutes});
}

function isLeap(y: number): boolean {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}

/* --------------------------------------------------------------------------
 * 4. Parser
 * -------------------------------------------------------------------------- */

const SCALAR_KINDS = new Set<TokenKind>(["INT", "BIGINT", "FLOAT", "DECIMAL", "STRING", "BINARY", "DATETIME", "BOOL", "NIL", "NAN", "INF", "NEG_INF"]);

class Parser {
  tokens: Token[];
  pos = 0;
  limits: Limits;
  totalItems = 0;
  depth = 0;

  constructor(tokens: Token[], limits: Limits) {
    this.tokens = tokens;
    this.limits = limits;
  }

  peek(): Token | undefined { return this.tokens[this.pos]; }
  advance(): Token { return this.tokens[this.pos++]!; }

  private error(msg: string, tok?: Token, code = "WEFT_SYNTAX"): WeftSyntaxError {
    const t = tok ?? this.peek() ?? this.tokens[this.tokens.length - 1]!;
    return new WeftSyntaxError(msg, {line: t.line, column: t.column, offset: t.offset, code});
  }

  private bumpTotal(): void {
    this.totalItems++;
    if (this.totalItems > this.limits.max_total_items) {
      throw new WeftLimitError(`document has more than max_total_items=${this.limits.max_total_items} values`, {code: "WEFT_LIMIT_TOTAL"});
    }
  }

  private enter(): void {
    this.depth++;
    if (this.depth > this.limits.max_depth) {
      throw new WeftLimitError(`nesting depth exceeds max_depth=${this.limits.max_depth}`, {code: "WEFT_LIMIT_DEPTH"});
    }
  }
  private leave(): void { this.depth--; }

  parseValue(): WeftValue {
    const tok = this.peek();
    if (!tok) throw new WeftSyntaxError("unexpected end of input", {code: "WEFT_SYNTAX_EOF"});
    this.bumpTotal();

    let value: WeftValue;
    if (SCALAR_KINDS.has(tok.kind)) {
      this.advance();
      if (tok.kind === "INT") value = tok.value as number | bigint;
      else if (tok.kind === "BIGINT") value = tok.value as BigIntValue;
      else if (tok.kind === "FLOAT") value = tok.value as number;
      else if (tok.kind === "DECIMAL") value = tok.value as Decimal;
      else if (tok.kind === "STRING") value = tok.value as string;
      else if (tok.kind === "BINARY") value = tok.value as Binary;
      else if (tok.kind === "DATETIME") value = tok.value as WeftDate;
      else if (tok.kind === "BOOL") value = tok.value as boolean;
      else if (tok.kind === "NIL") value = null;
      else if (tok.kind === "NAN") value = NaN;
      else if (tok.kind === "INF") value = Infinity;
      else if (tok.kind === "NEG_INF") value = -Infinity;
      else throw new Error("unreachable");
    } else if (tok.kind === "SYMBOL") {
      this.advance();
      value = new Symbol_(tok.value as string);
    } else if (tok.kind === "KEYWORD") {
      this.advance();
      value = new Keyword(tok.value as string);
    } else if (tok.kind === "LPAREN") {
      value = this.parseSexpr(tok);
    } else if (tok.kind === "LBRACK") {
      value = this.parseList(tok);
    } else if (tok.kind === "LBRACE") {
      value = this.parseMap(tok);
    } else {
      throw this.error(`unexpected token ${tok.kind}`, tok);
    }

    // Type annotations: each ``::T`` wraps the value in a Typed node, so
    // each one adds a level of structural depth. Count every annotation
    // against max_depth so a long chain like ``0::T::T::T::…`` cannot
    // bypass the limit.
    let nxt = this.peek();
    let annotCount = 0;
    try {
      while (nxt && nxt.kind === "COLONCOLON") {
        this.advance();
        this.enter();
        annotCount++;
        const typeExpr = this.parseTypeExpr();
        value = new Typed(value, typeExpr);
        this.bumpTotal();
        nxt = this.peek();
      }
    } finally {
      for (let i = 0; i < annotCount; i++) this.leave();
    }
    return value;
  }

  private parseSexpr(open: Token): Sexpr {
    this.advance();
    this.enter();
    try {
      const tok = this.peek();
      if (!tok) throw this.error("unterminated '('", open, "WEFT_SYNTAX_UNCLOSED");
      if (tok.kind !== "SYMBOL") throw this.error(`S-expression tag must be a symbol, got ${tok.kind}`, tok, "WEFT_SYNTAX_SEXPR_TAG");
      this.advance();
      const tag = new Symbol_(tok.value as string);
      const items: WeftValue[] = [];
      while (true) {
        const t = this.peek();
        if (!t) throw this.error("unterminated '('", open, "WEFT_SYNTAX_UNCLOSED");
        if (t.kind === "RPAREN") { this.advance(); break; }
        items.push(this.parseValue());
        if (items.length > this.limits.max_items_per_coll) {
          throw new WeftLimitError(`sexpr exceeds max_items_per_coll=${this.limits.max_items_per_coll}`, {code: "WEFT_LIMIT_ITEMS"});
        }
      }
      return new Sexpr(tag, items);
    } finally { this.leave(); }
  }

  private parseList(open: Token): WeftValue[] {
    this.advance();
    this.enter();
    try {
      const items: WeftValue[] = [];
      while (true) {
        const t = this.peek();
        if (!t) throw this.error("unterminated '['", open, "WEFT_SYNTAX_UNCLOSED");
        if (t.kind === "RBRACK") { this.advance(); break; }
        items.push(this.parseValue());
        if (items.length > this.limits.max_items_per_coll) {
          throw new WeftLimitError(`list exceeds max_items_per_coll=${this.limits.max_items_per_coll}`, {code: "WEFT_LIMIT_ITEMS"});
        }
      }
      return items;
    } finally { this.leave(); }
  }

  private parseMap(open: Token): {[k: string]: WeftValue} {
    this.advance();
    this.enter();
    try {
      // Object.create(null) so a Weft map key named ``__proto__`` lands
      // as an own property instead of mutating the resulting object's
      // prototype. Using ``{}`` here causes ``{:__proto__ {:admin true}}``
      // to parse into an object where ``Object.keys(x)`` is ``[]`` but
      // ``x.admin`` reads through the prototype — a silent data shift.
      const d: {[k: string]: WeftValue} = Object.create(null);
      let count = 0;
      while (true) {
        const t = this.peek();
        if (!t) throw this.error("unterminated '{'", open, "WEFT_SYNTAX_UNCLOSED");
        if (t.kind === "RBRACE") { this.advance(); break; }
        if (t.kind !== "KEYWORD") throw this.error(`map key must be a keyword, got ${t.kind}`, t, "WEFT_SYNTAX_MAP_KEY");
        this.advance();
        this.bumpTotal();
        const keyName = t.value as string;
        const vTok = this.peek();
        if (!vTok || vTok.kind === "RBRACE") throw this.error(`map key :${keyName} has no value`, t, "WEFT_SYNTAX_MAP_VALUE");
        const value = this.parseValue();
        // ``in`` on a null-prototype object only sees own properties, so
        // this check cannot be fooled by ``hasOwnProperty`` injection or
        // ``__proto__`` ambiguity.
        if (keyName in d) {
          throw this.error(`duplicate map key :${keyName}`, t, "WEFT_SYNTAX_MAP_DUPKEY");
        }
        // Define as a plain own property; ``d[keyName] = ...`` would again
        // route through the prototype for forbidden keys even on a
        // null-proto object in some engines, so define explicitly.
        Object.defineProperty(d, keyName, {
          value, enumerable: true, writable: true, configurable: true,
        });
        count++;
        if (count > this.limits.max_items_per_coll) {
          throw new WeftLimitError(`map exceeds max_items_per_coll=${this.limits.max_items_per_coll}`, {code: "WEFT_LIMIT_ITEMS"});
        }
      }
      return d;
    } finally { this.leave(); }
  }

  private parseTypeExpr(): Symbol_ | Sexpr {
    const tok = this.peek();
    if (!tok) throw new WeftSyntaxError("type expression expected after '::'", {code: "WEFT_SYNTAX_TYPE"});
    if (tok.kind === "SYMBOL") {
      this.advance();
      return new Symbol_(tok.value as string);
    }
    if (tok.kind === "LPAREN") return this.parseSexpr(tok);
    throw this.error(`type expression must be a symbol or sexpr, got ${tok.kind}`, tok, "WEFT_SYNTAX_TYPE");
  }
}

/* --------------------------------------------------------------------------
 * 5. Public parse API
 * -------------------------------------------------------------------------- */

/**
 * Walk the parsed tree iteratively and refuse any value whose nesting
 * depth exceeds ``max_depth``. Each list, map, sexpr, or Typed wrapper
 * adds one level; leaf scalars do not. The parser's inline counter only
 * tracks balanced bracket nesting — a chain of ``::T`` annotations on
 * the way out of a container adds structural depth that this walk
 * covers.
 */
function checkTreeDepth(value: WeftValue, maxDepth: number): void {
  const stack: Array<[WeftValue, number]> = [[value, 0]];
  while (stack.length) {
    const [node, d] = stack.pop()!;
    if (Array.isArray(node)) {
      const nd = d + 1;
      if (nd > maxDepth) throw new WeftLimitError(
        `tree depth ${nd} exceeds max_depth=${maxDepth}`, {code: "WEFT_LIMIT_DEPTH"});
      for (const it of node) stack.push([it, nd]);
    } else if (node instanceof Sexpr) {
      const nd = d + 1;
      if (nd > maxDepth) throw new WeftLimitError(
        `tree depth ${nd} exceeds max_depth=${maxDepth}`, {code: "WEFT_LIMIT_DEPTH"});
      for (const it of node.items) stack.push([it, nd]);
    } else if (node instanceof Typed) {
      // Unroll chained ::T annotations in a loop. Each wrapper's type
      // expression may itself hold nested data (``(T [[[...]]])``), so
      // the type side has to be walked exactly like the value side.
      let dd = d;
      let cur: WeftValue = node;
      while (cur instanceof Typed) {
        dd += 1;
        if (dd > maxDepth) throw new WeftLimitError(
          `tree depth ${dd} exceeds max_depth=${maxDepth}`, {code: "WEFT_LIMIT_DEPTH"});
        // Push the type expression at the same depth; its items / wrappers
        // will add their own +1 as the walk continues.
        stack.push([cur.type as WeftValue, dd]);
        cur = cur.value;
      }
      stack.push([cur, dd]);
    } else if (isPlainObject(node)) {
      // Plain object = map.
      const nd = d + 1;
      if (nd > maxDepth) throw new WeftLimitError(
        `tree depth ${nd} exceeds max_depth=${maxDepth}`, {code: "WEFT_LIMIT_DEPTH"});
      for (const v of Object.values(node)) stack.push([v, nd]);
    }
    // scalars don't contribute to depth
  }
}

export function parse(text: string, opts: {limits?: Limits} = {}): WeftValue {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const toks = [...new Lexer(text, limits).tokenize()];
  const p = new Parser(toks, limits);
  if (!p.peek()) {
    throw new WeftSyntaxError("empty document: single-value mode requires exactly one top-level value",
      {code: "WEFT_SYNTAX_EMPTY"});
  }
  const value = p.parseValue();
  const leftover = p.peek();
  if (leftover) {
    throw new WeftSyntaxError("unexpected content after top-level value",
      {line: leftover.line, column: leftover.column, offset: leftover.offset, code: "WEFT_SYNTAX_LEFTOVER"});
  }
  checkTreeDepth(value, limits.max_depth);
  return value;
}

export function parseStream(text: string, opts: {limits?: Limits} = {}): WeftValue[] {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const toks = [...new Lexer(text, limits).tokenize()];
  const p = new Parser(toks, limits);
  const out: WeftValue[] = [];
  while (p.peek()) {
    const v = p.parseValue();
    checkTreeDepth(v, limits.max_depth);
    out.push(v);
  }
  return out;
}

/* --------------------------------------------------------------------------
 * 6. Formatter
 * -------------------------------------------------------------------------- */

export interface FormatOptions {
  max_width?: number;
  indent?: number;
  sort_keys?: boolean;
  strict_canonical?: boolean;
}

export function formatValue(value: WeftValue, options: FormatOptions = {}): string {
  const opts: Required<FormatOptions> = {
    max_width: options.max_width ?? 80,
    indent: options.indent ?? 2,
    sort_keys: options.sort_keys ?? false,
    strict_canonical: options.strict_canonical ?? false,
  };
  if (opts.strict_canonical) {
    // SPEC §7.3: strict canonical is a single-line, unbounded-width
    // layout. Setting max_width to effectively infinite ensures that
    // formatRec() never falls into the multi-line wrap path.
    opts.sort_keys = true;
    opts.max_width = 1_000_000_000_000;
  }
  let rendered = formatRec(value, 0, opts);
  if (!rendered.endsWith("\n")) rendered += "\n";
  return rendered;
}

export function formatInline(value: WeftValue, options: FormatOptions = {}): string {
  const opts: Required<FormatOptions> = {
    max_width: options.max_width ?? 80,
    indent: options.indent ?? 2,
    sort_keys: options.sort_keys ?? false,
    strict_canonical: options.strict_canonical ?? false,
  };
  // Mirror formatValue: strict_canonical forces sort_keys. Without this,
  // formatInline(v, {strict_canonical:true}) would differ from
  // formatValue(v, {strict_canonical:true}).trimEnd() on maps.
  if (opts.strict_canonical) opts.sort_keys = true;
  return inlineRec(value, opts);
}

class MultilineNeededError extends Error {}

/** Number of Unicode code points in a string (SPEC §7.2: the 80-column
 *  soft-wrap counts code points, not UTF-16 units nor UTF-8 bytes). */
function cpLen(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}


// SPEC §7.3: strict canonical sorts map keys in ascending Unicode
// *code-point* order, not UTF-16 code-unit order (JS's default
// String.prototype.<). The two agree on the BMP but diverge for
// supplementary-plane characters, where surrogate halves (U+D800–U+DBFF)
// sort before U+E000–U+FFFF in UTF-16 but after in code-point order.
function codePointCompare(a: string, b: string): number {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  while (true) {
    const ra = ia.next(), rb = ib.next();
    if (ra.done && rb.done) return 0;
    if (ra.done) return -1;
    if (rb.done) return 1;
    const ca = (ra.value as string).codePointAt(0)!;
    const cb = (rb.value as string).codePointAt(0)!;
    if (ca !== cb) return ca - cb;
  }
}

function formatRec(value: WeftValue, depth: number, opts: Required<FormatOptions>): string {
  // Peel off any Typed wrapper(s) iteratively — a deep type-annotation
  // chain cannot blow the recursion stack. Innermost type appears first
  // in the source, outermost last; peeling from outside gives us the
  // chain in reverse order, so we reverse when joining. Type expressions
  // use strict (single-line) mode so a newline string inside the type
  // doesn't throw MultilineNeededError and crash the whole value.
  const strictOpts: Required<FormatOptions> = {...opts, strict_canonical: true};
  const typeChain: string[] = [];
  while (value instanceof Typed) {
    typeChain.push(inlineRec(value.type, strictOpts));
    value = value.value;
  }
  const typeSuffix = typeChain.length > 0 ? ("::" + typeChain.reverse().join("::")) : "";

  let oneline: string | null = null;
  try { oneline = inlineRec(value, opts); } catch (e) {
    if (!(e instanceof MultilineNeededError)) throw e;
  }
  if (oneline !== null && depth * opts.indent + cpLen(oneline) + cpLen(typeSuffix) <= opts.max_width && !oneline.includes("\n")) {
    return oneline + typeSuffix;
  }
  if (Array.isArray(value)) return formatListMulti(value, depth, opts) + typeSuffix;
  if (isPlainObject(value)) return formatMapMulti(value, depth, opts) + typeSuffix;
  if (value instanceof Sexpr) return formatSexprMulti(value, depth, opts) + typeSuffix;
  try { return inlineRec(value, opts) + typeSuffix; } catch (e) {
    if (e instanceof MultilineNeededError && typeof value === "string") {
      return formatStringTriple(value, opts) + typeSuffix;
    }
    throw e;
  }
}

function inlineRec(value: WeftValue, opts: Required<FormatOptions>): string {
  if (value === null) return "nil";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") return formatNumber(value);
  if (typeof value === "bigint") return value.toString(10);
  if (value instanceof BigIntValue) return value.value.toString(10) + "n";
  if (value instanceof Decimal) return formatDecimal(value);
  if (typeof value === "string") return formatStringDquote(value, opts);
  if (value instanceof Binary) return formatBinary(value);
  if (value instanceof WeftDate) return formatDate(value);
  if (value instanceof Symbol_) return value.name;
  if (value instanceof Keyword) return ":" + value.name;
  if (value instanceof Typed) {
    // Peel iteratively to avoid stack blow-up on long type chains, and
    // join in source order (innermost type first, outermost last).
    // Type expressions are always rendered strict-single-line so a
    // newline string inside the type doesn't throw MultilineNeeded —
    // ``0::(T "a\nb")`` must round-trip.
    const strictOpts: Required<FormatOptions> = {...opts, strict_canonical: true};
    const chain: string[] = [];
    let cur: WeftValue = value;
    while (cur instanceof Typed) {
      chain.push(inlineRec(cur.type, strictOpts));
      cur = cur.value;
    }
    return inlineRec(cur, opts) + "::" + chain.reverse().join("::");
  }
  if (Array.isArray(value)) {
    const parts = value.map(x => inlineRec(x, opts));
    return "[" + parts.join(" ") + "]";
  }
  if (value instanceof Sexpr) {
    const parts = [value.tag.name, ...value.items.map(it => inlineRec(it, opts))];
    return "(" + parts.join(" ") + ")";
  }
  if (isPlainObject(value)) {
    let keys = Object.keys(value);
    if (opts.sort_keys) keys = keys.slice().sort(codePointCompare);
    const parts = keys.map(k => ":" + k + " " + inlineRec((value as any)[k], opts));
    return "{" + parts.join(" ") + "}";
  }
  throw new WeftTypeError(`cannot format value of type ${typeof value}`);
}

function formatNumber(f: number): string {
  if (Number.isNaN(f)) return "nan";
  if (!Number.isFinite(f)) return f < 0 ? "-inf" : "inf";
  // Preserve the sign of zero — ``Number.prototype.toString`` strips the
  // ``-`` on ``-0``, which would silently drop information. Weft Float
  // treats ``-0.0`` and ``0.0`` as distinct bit patterns (SPEC §5.3).
  if (f === 0) return Object.is(f, -0) ? "-0.0" : "0.0";
  // Match the Python/Rust/Go canonical formula so all four impls emit
  // byte-identical output: fixed-point for |f| in [1e-4, 1e16), exponent
  // otherwise. Exponent form uses two-digit zero-padded magnitude so
  // ``1e-7`` → ``1e-07``.
  const abs = Math.abs(f);
  if (abs >= 1e-4 && abs < 1e16) {
    let s = f.toString();
    // ``toString`` may still produce exponent form for a few values near
    // the edge of the window; force fixed-point when we're inside.
    if (s.includes("e") || s.includes("E")) s = f.toFixed(20).replace(/0+$/, "").replace(/\.$/, ".0");
    if (!s.includes(".")) s += ".0";
    return s;
  }
  // Exponent form.
  // ``toExponential`` without an argument gives shortest round-trip but
  // JS writes ``1e+21`` with a single-digit exponent — pad to at least 2.
  let s = f.toExponential();
  // Normalise: lower-case e, explicit sign, ≥2-digit magnitude.
  s = s.replace("E", "e").replace(/e(-?)(\d+)/, (_m, sign, digits) => {
    const padded = digits.length < 2 ? ("0".repeat(2 - digits.length) + digits) : digits;
    return "e" + (sign || "+") + padded;
  });
  // Ensure the mantissa has a ``.`` or at least a decimal point implied
  // by the exponent form.
  return s;
}

function formatDecimal(d: Decimal): string {
  // Preserve the exact source text — Decimal values are significant-
  // digit aware, so synthesising a trailing ``.0`` would change the
  // represented precision (``1m`` and ``1.0m`` are not the same Weft
  // Decimal). The ``m`` suffix alone makes the literal unambiguous.
  return d.text + "m";
}

// SPEC §7.3: in strict canonical, C0, DEL, C1, BOM, BMP noncharacters,
// and bidirectional override/isolate controls MUST be escaped.
function strictMustEscape(cp: number): boolean {
  return (cp <= 0x1F)
    || (cp === 0x7F)
    || (cp >= 0x80 && cp <= 0x9F)
    || (cp === 0xFEFF || cp === 0xFFFE || cp === 0xFFFF)
    || (cp >= 0x202A && cp <= 0x202E)
    || (cp >= 0x2066 && cp <= 0x2069);
}

function formatStringDquote(s: string, opts: Required<FormatOptions>): string {
  // SPEC §7.2: a string containing U+000A MUST be emitted as the one-line
  // dquote-escape form, never triple-quoted. Triple-quoted strings inside
  // a soft-wrapped compound get re-indented by every known formatter,
  // which silently inserts leading spaces into the string body (W4-04
  // data corruption). Keeping it inline with ``\n`` escapes removes that
  // attack surface entirely.
  const out: string[] = ['"'];
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (ch === '"') out.push('\\"');
    else if (ch === "\\") out.push("\\\\");
    else if (ch === "\n") out.push("\\n");
    else if (ch === "\t") out.push("\\t");
    else if (ch === "\r") out.push("\\r");
    else if (ch === "\x00") out.push("\\0");
    else if (opts.strict_canonical && strictMustEscape(cp)) {
      out.push(`\\u{${cp.toString(16).toUpperCase()}}`);
    }
    else if (cp < 0x20) out.push(`\\u{${cp.toString(16).toUpperCase()}}`);
    // BOM (U+FEFF) and the two noncharacters at the end of the BMP are
    // always escaped — the lexer rejects them literal in the document,
    // so emitting them raw would break round-trip.
    else if (cp === 0xFEFF || cp === 0xFFFE || cp === 0xFFFF) {
      out.push(`\\u{${cp.toString(16).toUpperCase()}}`);
    }
    else out.push(ch);
  }
  out.push('"');
  return out.join("");
}

function formatStringTriple(s: string, opts: Required<FormatOptions>): string {
  // Fall back to dquote when the triple form would be ambiguous:
  //   - the string contains ``"""`` (would prematurely close the literal)
  //   - the string ends in ``"`` (would merge with the closing ``"""``
  //     to form ``""""``, making the delimiter ambiguous on re-parse)
  //   - strict canonical mode is requested
  if (s.includes('"""') || s.endsWith('"') || opts.strict_canonical) {
    return formatStringDquote(s, {...opts, strict_canonical: true});
  }
  const out: string[] = ['"""'];
  for (const ch of s) {
    const cp = ch.codePointAt(0)!;
    if (ch === "\n" || ch === "\t") out.push(ch);
    else if (ch === "\\") out.push("\\\\");
    else if (cp < 0x20 && ch !== "\r") out.push(`\\u{${cp.toString(16).toUpperCase()}}`);
    else out.push(ch);
  }
  out.push('"""');
  return out.join("");
}

function formatBinary(b: Binary): string {
  // SPEC §5.8 and §7.2: canonical form always emits base64 regardless
  // of the input encoding. Equality is defined on the byte sequence
  // (§5.16), so the two spellings are the same abstract value and
  // canonical output picks one.
  return `b"base64:${base64Encode(b.data)}"`;
}

function formatDate(d: WeftDate): string {
  const y = String(d.year).padStart(4, "0");
  const mo = String(d.month).padStart(2, "0");
  const da = String(d.day).padStart(2, "0");
  if (!d.hasTime) return `#${y}-${mo}-${da}`;
  const h = String(d.hour).padStart(2, "0");
  const mi = String(d.minute).padStart(2, "0");
  const s = String(d.second).padStart(2, "0");
  let base = `${y}-${mo}-${da}T${h}:${mi}:${s}`;
  if (d.nano) {
    let frac = String(d.nano).padStart(9, "0");
    frac = frac.replace(/0+$/, "");
    base += "." + frac;
  }
  if (d.tzOffsetMinutes === null) return "#" + base;
  if (d.tzOffsetMinutes === 0) return "#" + base + "Z";
  const sign = d.tzOffsetMinutes >= 0 ? "+" : "-";
  const abs = Math.abs(d.tzOffsetMinutes);
  return `#${base}${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

function isPlainObject(v: unknown): v is {[k: string]: WeftValue} {
  if (v === null || typeof v !== "object") return false;
  if (Array.isArray(v)) return false;
  if (v instanceof Symbol_ || v instanceof Keyword || v instanceof BigIntValue ||
      v instanceof Decimal || v instanceof Binary || v instanceof WeftDate ||
      v instanceof Sexpr || v instanceof Typed) return false;
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function formatListMulti(items: WeftValue[], depth: number, opts: Required<FormatOptions>): string {
  if (items.length === 0) return "[]";
  const padInner = " ".repeat((depth + 1) * opts.indent);
  const padClose = " ".repeat(depth * opts.indent);
  const lines = items.map(it => padInner + formatRec(it, depth + 1, opts));
  return "[\n" + lines.join("\n") + "\n" + padClose + "]";
}

function formatMapMulti(d: {[k: string]: WeftValue}, depth: number, opts: Required<FormatOptions>): string {
  const keys = opts.sort_keys
    ? Object.keys(d).slice().sort(codePointCompare)
    : Object.keys(d);
  if (keys.length === 0) return "{}";
  const padInner = " ".repeat((depth + 1) * opts.indent);
  const padClose = " ".repeat(depth * opts.indent);
  const lines: string[] = [];
  for (const k of keys) {
    const v = d[k]!;
    const keyS = ":" + k;
    let vInline: string | null = null;
    try { vInline = inlineRec(v, opts); } catch (e) {
      if (!(e instanceof MultilineNeededError)) throw e;
    }
    if (vInline !== null && padInner.length + cpLen(keyS) + 1 + cpLen(vInline) <= opts.max_width && !vInline.includes("\n")) {
      lines.push(padInner + keyS + " " + vInline);
    } else {
      const vBlock = formatRec(v, depth + 1, opts);
      if (vBlock.includes("\n")) {
        const deeper = " ".repeat((depth + 2) * opts.indent);
        lines.push(padInner + keyS);
        for (const ln of vBlock.split("\n")) lines.push(ln ? deeper + ln : "");
      } else {
        lines.push(padInner + keyS + " " + vBlock);
      }
    }
  }
  return "{\n" + lines.join("\n") + "\n" + padClose + "}";
}

function formatSexprMulti(s: Sexpr, depth: number, opts: Required<FormatOptions>): string {
  const tag = s.tag.name;
  if (s.items.length === 0) return "(" + tag + ")";
  const padInner = " ".repeat((depth + 1) * opts.indent);
  const padClose = " ".repeat(depth * opts.indent);
  const lines = s.items.map(it => padInner + formatRec(it, depth + 1, opts));
  return "(" + tag + "\n" + lines.join("\n") + "\n" + padClose + ")";
}

/* --------------------------------------------------------------------------
 * 7. JSON interop
 * -------------------------------------------------------------------------- */

const SAFE_INT_MAX = BigInt(Number.MAX_SAFE_INTEGER);
const SAFE_INT_MIN = -SAFE_INT_MAX;
const SYMBOL_RE = /^[A-Za-z_+\-*/<>=!?][\w+\-*/<>=!?]*$/;

const RESERVED_SYMBOL_LITERALS = new Set(["true", "false", "nil", "nan", "inf", "-inf", "+inf"]);

/** Return true if ``name`` is a legal Weft Symbol/Keyword name per SPEC §5.10.
 *  Rejects empty strings, names starting with a digit, reserved literals,
 *  signed-number lookalikes (+1, -0), and any character that is not a
 *  symbol-start / symbol-continue code point. */
export function isValidKeywordName(name: unknown): boolean {
  if (typeof name !== "string" || name.length === 0) return false;
  // SPEC §5.11: keyword names use the same character class as symbols,
  // with one extra restriction (sign+digit start rejected below). Reserved
  // top-level literals (true, false, nil, nan, inf, -inf) ARE allowed as
  // keyword names because the leading ``:`` disambiguates them.
  //
  // W4-07: iterate by Unicode code points (for..of), not UTF-16 units
  // (name[i]), so supplementary-plane Letters like U+1D400 are accepted
  // consistently with the lexer and the Symbol constructor.
  let first = true;
  for (const ch of name) {
    if (first) {
      if (!isSymbolStart(ch)) return false;
      first = false;
    } else {
      if (!isSymbolCont(ch)) return false;
    }
  }
  // Sign+digit start is forbidden (ambiguates with signed number literal).
  if (name.length >= 2 && (name[0] === "+" || name[0] === "-")
      && name[1]! >= "0" && name[1]! <= "9") return false;
  return true;
}

export function toJson(value: WeftValue): unknown {
  if (value === null) return null;
  if (value === true || value === false) return value;
  if (value instanceof BigIntValue) return value.value.toString(10);
  if (typeof value === "bigint") {
    if (value >= SAFE_INT_MIN && value <= SAFE_INT_MAX) return Number(value);
    return value.toString(10);
  }
  if (typeof value === "number") {
    if (Number.isNaN(value)) return "nan";
    if (value === Infinity) return "inf";
    if (value === -Infinity) return "-inf";
    return value;
  }
  if (value instanceof Decimal) return value.text;
  if (typeof value === "string") return value;
  if (value instanceof Binary) {
    if (value.encoding === "base64") return "base64:" + base64Encode(value.data);
    return "hex:" + hexEncode(value.data);
  }
  if (value instanceof WeftDate) return formatDate(value).slice(1);
  if (value instanceof Symbol_) return "#sym:" + value.name;
  if (value instanceof Keyword) return "#kw:" + value.name;
  if (Array.isArray(value)) return value.map(toJson);
  if (value instanceof Sexpr) {
    return {$sexpr: [value.tag.name, ...value.items.map(toJson)]};
  }
  if (value instanceof Typed) {
    return {$typed: {v: toJson(value.value), t: toJson(value.type)}};
  }
  if (isPlainObject(value)) {
    // W4-01/W5-07: a key named __proto__ must become an own property, not
    // trigger the Object.prototype setter (which silently drops the entry).
    const out: {[k: string]: unknown} = Object.create(null);
    for (const [k, v] of Object.entries(value)) {
      Object.defineProperty(out, k, {value: toJson(v), enumerable: true, writable: true, configurable: true});
    }
    return out;
  }
  throw new WeftTypeError(`cannot convert ${typeof value} to JSON`);
}

/**
 * Lossless JSON mapping. Every Weft type becomes a tagged JSON object
 * (except plain-JSON types: null, bool, native-safe number).
 *
 * Tag conventions (one tag per object):
 *   {$sym: "name"}                          Symbol
 *   {$kw:  "name"}                          Keyword
 *   {$bigint: "..."}                        BigInt (decimal digits)
 *   {$"int-big": "..."}                     plain bigint outside the safe range
 *   {$dec: "..."}                           Decimal (preserved text)
 *   {$str: "..."}                           String (always wrapped)
 *   {$bin: {enc:"base64"|"hex", data:"..."}} Binary
 *   {$dt: "ISO-8601"}                       DateTime
 *   {$float: "nan"|"inf"|"-inf"}            non-finite Float
 *   {$sexpr: [tag, items...]}               Sexpr
 *   {$typed: {v:..., t:...}}                Typed
 *   {$list: [...]}                          List
 *   {$map: {k:v, ...}}                      Map
 *
 * ``fromJsonLossless(toJsonLossless(x)) == x`` for every valid Weft tree.
 */
export function toJsonLossless(value: WeftValue): unknown {
  if (value === null) return null;
  if (value === true || value === false) return value;
  if (value instanceof BigIntValue) return {$bigint: value.value.toString(10)};
  if (typeof value === "bigint") {
    if (value >= SAFE_INT_MIN && value <= SAFE_INT_MAX) return Number(value);
    return {"$int-big": value.toString(10)};
  }
  if (typeof value === "number") {
    // Even finite floats are tagged: a plain JSON number is reserved
    // for Weft Integer so ``1`` (Int) and ``1.0`` (Float) survive the
    // round trip as different types.
    if (Number.isNaN(value)) return {$float: "nan"};
    if (value === Infinity) return {$float: "inf"};
    if (value === -Infinity) return {$float: "-inf"};
    return {$float: floatCanonicalText(value)};
  }
  if (value instanceof Decimal) return {$dec: value.text};
  if (typeof value === "string") return {$str: value};
  if (value instanceof Binary) {
    if (value.encoding === "base64") return {$bin: {enc: "base64", data: base64Encode(value.data)}};
    return {$bin: {enc: "hex", data: hexEncode(value.data)}};
  }
  if (value instanceof WeftDate) return {$dt: formatDate(value).slice(1)};
  if (value instanceof Symbol_) return {$sym: value.name};
  if (value instanceof Keyword) return {$kw: value.name};
  if (Array.isArray(value)) return {$list: value.map(toJsonLossless)};
  if (value instanceof Sexpr) {
    return {$sexpr: [value.tag.name, ...value.items.map(toJsonLossless)]};
  }
  if (value instanceof Typed) {
    return {$typed: {v: toJsonLossless(value.value), t: toJsonLossless(value.type)}};
  }
  if (isPlainObject(value)) {
    // W4-01: use a null-prototype object and defineProperty so keys like
    // __proto__ become own properties rather than triggering the setter
    // and either silently dropping or polluting the prototype chain.
    const out: {[k: string]: unknown} = Object.create(null);
    for (const [k, v] of Object.entries(value)) {
      Object.defineProperty(out, k, { value: toJsonLossless(v), enumerable: true, writable: true, configurable: true });
    }
    // Wrap as $map with the same defensive construction.
    const wrapper: {[k: string]: unknown} = Object.create(null);
    Object.defineProperty(wrapper, "$map", { value: out, enumerable: true, writable: true, configurable: true });
    return wrapper;
  }
  throw new WeftTypeError(`cannot losslessly convert ${typeof value}`);
}

// Strict RFC-3339-like shape for ``$dt`` payloads. Trailing whitespace
// or a comment must not smuggle in.
const DT_PAYLOAD_RE = /^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$/;

// Finite-float decimal text for ``$float`` payloads.
const FLOAT_DEC_RE = /^[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$/;

function floatCanonicalText(f: number): string {
  // SPEC §11.2.1 rule 4: the $float payload is the §7.3 strict canonical
  // text. Delegate to the strict formatter so JSON payloads and canonical
  // documents can never drift apart (``1e-7`` → ``"1e-07"``, ``1e20`` →
  // ``"1e+20"``), matching the other implementations byte for byte.
  return formatNumber(f);
}

const LOSSLESS_TAGS = new Set([
  "$sym", "$kw", "$bigint", "$int-big", "$dec", "$str",
  "$bin", "$dt", "$float", "$sexpr", "$typed", "$list", "$map",
]);

/* --------------------------------------------------------------------------
 * 7b. Lexical JSON reader (SPEC §11.2.1 rule 4, §11.2.5, §9)
 *
 * JSON.parse collapses ``1`` and ``1.0`` to the same JS number and silently
 * drops duplicate object keys, so a conforming lossless decoder cannot be
 * built on top of it. This reader:
 *   - wraps every number token in ``JsonNum`` so its lexical shape survives,
 *   - rejects duplicate object keys (WEFT_SYNTAX_JSON_DUPKEY),
 *   - enforces max_size_bytes / max_depth / max_int_digits /
 *     max_string_bytes while reading,
 *   - rejects lone surrogates in string escapes (Weft strings are Unicode
 *     scalar sequences, §5.7).
 * -------------------------------------------------------------------------- */

/** A JSON number token with its original lexical form preserved. */
export class JsonNum {
  readonly raw: string;
  readonly __brand = "JsonNum";
  constructor(raw: string) { this.raw = raw; }
  /** True iff the token is lexically an integer (no '.', 'e', or 'E'). */
  isInteger(): boolean { return !/[.eE]/.test(this.raw); }
}

class JsonReader {
  private s: string;
  private i = 0;
  private limits: Limits;
  constructor(s: string, limits: Limits) { this.s = s; this.limits = limits; }

  private err(msg: string, code = "WEFT_SYNTAX_JSON"): WeftSyntaxError {
    return new WeftSyntaxError(msg, {offset: this.i, code});
  }

  private ws(): void {
    while (this.i < this.s.length && " \t\n\r".includes(this.s[this.i]!)) this.i++;
  }

  parse(): unknown {
    this.ws();
    const v = this.value(0);
    this.ws();
    if (this.i !== this.s.length) throw this.err("unexpected trailing content in JSON");
    return v;
  }

  private value(depth: number): unknown {
    // JSON structural depth bound: each Weft level costs at most two JSON
    // levels ({"$list": [...]}), so 2*max_depth+8 bounds the reader's own
    // recursion long before the host stack is at risk. The precise Weft
    // depth rule (§8) is enforced again during conversion.
    if (depth > this.limits.max_depth * 2 + 8) {
      throw new WeftLimitError(
        `JSON nesting exceeds max_depth=${this.limits.max_depth}`,
        {code: "WEFT_LIMIT_DEPTH"});
    }
    this.ws();
    const c = this.s[this.i];
    if (c === undefined) throw this.err("unexpected end of JSON");
    if (c === "{") return this.object(depth);
    if (c === "[") return this.array(depth);
    if (c === '"') return this.string();
    if (c === "t") { this.lit("true"); return true; }
    if (c === "f") { this.lit("false"); return false; }
    if (c === "n") { this.lit("null"); return null; }
    if (c === "-" || (c >= "0" && c <= "9")) return this.number();
    throw this.err(`unexpected character ${JSON.stringify(c)} in JSON`);
  }

  private lit(word: string): void {
    if (this.s.slice(this.i, this.i + word.length) !== word) {
      throw this.err(`invalid JSON literal at offset ${this.i}`);
    }
    this.i += word.length;
  }

  private object(depth: number): {[k: string]: unknown} {
    this.i++; // {
    const out: {[k: string]: unknown} = Object.create(null);
    this.ws();
    if (this.s[this.i] === "}") { this.i++; return out; }
    let count = 0;
    while (true) {
      this.ws();
      if (this.s[this.i] !== '"') throw this.err("expected string key in JSON object");
      const k = this.string();
      // SPEC §11.2.5: duplicate keys MUST be rejected, not last-win.
      if (Object.prototype.hasOwnProperty.call(out, k)) {
        throw new WeftSyntaxError(`duplicate key ${JSON.stringify(k)} in JSON object`,
          {code: "WEFT_SYNTAX_JSON_DUPKEY"});
      }
      this.ws();
      if (this.s[this.i] !== ":") throw this.err("expected ':' in JSON object");
      this.i++;
      const v = this.value(depth + 1);
      Object.defineProperty(out, k, {value: v, enumerable: true, writable: true, configurable: true});
      count++;
      // Structural bound only: a lossless tag object wraps the Weft value,
      // so a conforming encoding may hold up to one extra member/element
      // beyond the Weft item count ({"$sexpr": [tag, ...items]}, the
      // two-key $typed/$bin payload objects). The exact §9 Weft limit is
      // enforced during conversion against the *Weft* item count.
      if (count > this.limits.max_items_per_coll * 2 + 8) {
        throw new WeftLimitError(`JSON object exceeds structural bound (max_items_per_coll=${this.limits.max_items_per_coll})`,
          {code: "WEFT_LIMIT_ITEMS"});
      }
      this.ws();
      if (this.s[this.i] === ",") { this.i++; continue; }
      if (this.s[this.i] === "}") { this.i++; return out; }
      throw this.err("expected ',' or '}' in JSON object");
    }
  }

  private array(depth: number): unknown[] {
    this.i++; // [
    const out: unknown[] = [];
    this.ws();
    if (this.s[this.i] === "]") { this.i++; return out; }
    while (true) {
      out.push(this.value(depth + 1));
      // Structural bound — see object(); a $sexpr array holds items+1.
      if (out.length > this.limits.max_items_per_coll * 2 + 8) {
        throw new WeftLimitError(`JSON array exceeds structural bound (max_items_per_coll=${this.limits.max_items_per_coll})`,
          {code: "WEFT_LIMIT_ITEMS"});
      }
      this.ws();
      if (this.s[this.i] === ",") { this.i++; continue; }
      if (this.s[this.i] === "]") { this.i++; return out; }
      throw this.err("expected ',' or ']' in JSON array");
    }
  }

  private string(): string {
    this.i++; // "
    let out = "";
    let pendingHigh = -1; // high surrogate waiting for its low half
    const flushPending = () => {
      if (pendingHigh >= 0) {
        throw this.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)");
      }
    };
    while (true) {
      const c = this.s[this.i];
      if (c === undefined) throw this.err("unterminated JSON string");
      if (c === '"') { flushPending(); this.i++; break; }
      if (c === "\\") {
        this.i++;
        const e = this.s[this.i];
        if (e === undefined) throw this.err("truncated JSON escape");
        if (e === "u") {
          const hex = this.s.slice(this.i + 1, this.i + 5);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw this.err("invalid \\u escape in JSON");
          const cu = parseInt(hex, 16);
          this.i += 5;
          if (cu >= 0xD800 && cu <= 0xDBFF) {
            flushPending();
            pendingHigh = cu;
            continue;
          }
          if (cu >= 0xDC00 && cu <= 0xDFFF) {
            if (pendingHigh < 0) throw this.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)");
            out += String.fromCodePoint(0x10000 + ((pendingHigh - 0xD800) << 10) + (cu - 0xDC00));
            pendingHigh = -1;
            continue;
          }
          flushPending();
          out += String.fromCharCode(cu);
          continue;
        }
        flushPending();
        const map: Record<string, string> = {'"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t"};
        if (!(e in map)) throw this.err(`invalid JSON escape \\${e}`);
        out += map[e]!;
        this.i++;
        continue;
      }
      flushPending();
      const cc = c.charCodeAt(0);
      if (cc < 0x20) throw this.err("unescaped control character in JSON string");
      out += c;
      this.i++;
      if (out.length > this.strCap()) {
        // Structural bound only — a JSON string here may be a tag name, a
        // map key, a $bigint/$dec digit payload (bounded by
        // max_int_digits) or a $str payload (bounded by
        // max_string_bytes). The exact §9 limit for each role is enforced
        // during conversion; this cap just stops unbounded reading.
        throw new WeftLimitError(`JSON string exceeds structural bound (max_string_bytes=${this.limits.max_string_bytes})`,
          {code: "WEFT_LIMIT_STRING"});
      }
    }
    return out;
  }

  /** Structural cap for raw JSON strings — comfortably above every
   *  legitimate role a string can play in the §11.2 encoding. */
  private strCap(): number {
    return Math.max(2 * this.limits.max_string_bytes,
                    this.limits.max_int_digits + 16, 64) + 16;
  }

  private number(): JsonNum {
    const start = this.i;
    const re = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
    re.lastIndex = this.i;
    const m = re.exec(this.s);
    if (!m || m.index !== this.i) throw this.err("invalid JSON number");
    this.i += m[0].length;
    const raw = m[0];
    // max_int_digits applies to the digits of integer tokens (and bounds
    // the mantissa of float tokens as a side effect).
    const digits = raw.replace(/^-/, "").replace(/[.eE+-]/g, "").length;
    if (digits > this.limits.max_int_digits) {
      throw new WeftLimitError(`JSON number has ${digits} digits, exceeds max_int_digits=${this.limits.max_int_digits}`,
        {code: "WEFT_LIMIT_INT"});
    }
    void start;
    return new JsonNum(raw);
  }
}

function utf8Len(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x80) n += 1;
    else if (c < 0x800) n += 2;
    else if (c >= 0xD800 && c <= 0xDFFF) n += 2; // half of a 4-byte pair
    else n += 3;
  }
  return n;
}

/** Parse JSON text with the lexical reader (numbers become JsonNum). */
export function parseJsonLexical(text: string, limits: Limits = DEFAULT_LIMITS): unknown {
  const size = utf8Len(text);
  if (size > limits.max_size_bytes) {
    throw new WeftLimitError(`JSON input ${size} bytes exceeds max_size_bytes=${limits.max_size_bytes}`,
      {code: "WEFT_LIMIT_SIZE"});
  }
  return new JsonReader(text, limits).parse();
}

/* --------------------------------------------------------------------------
 * 7c. Iterative conversion machinery (SPEC §9: limits on every entry point)
 * -------------------------------------------------------------------------- */

type ConvGen = Generator<[unknown, number], WeftValue, WeftValue>;

function runTrampoline(conv: (node: unknown, depth: number) => ConvGen, root: unknown): WeftValue {
  const stack: ConvGen[] = [conv(root, 0)];
  let sendVal: WeftValue = null;
  while (stack.length > 0) {
    const gen = stack[stack.length - 1]!;
    const r = gen.next(sendVal);
    if (r.done) {
      stack.pop();
      sendVal = r.value;
      continue;
    }
    const [child, childDepth] = r.value;
    stack.push(conv(child, childDepth));
    sendVal = null;
  }
  return sendVal;
}

class TotalCounter {
  n = 0;
  constructor(private limit: number) {}
  bump(): void {
    this.n++;
    if (this.n > this.limit) {
      throw new WeftLimitError(`document has more than max_total_items=${this.limit} values`,
        {code: "WEFT_LIMIT_TOTAL"});
    }
  }
}

function checkDepth(depth: number, limits: Limits): void {
  if (depth > limits.max_depth) {
    throw new WeftLimitError(`tree depth ${depth} exceeds max_depth=${limits.max_depth}`,
      {code: "WEFT_LIMIT_DEPTH"});
  }
}

function checkIntDigits(v: bigint, limits: Limits): void {
  const digits = (v < 0n ? -v : v).toString().length;
  if (digits > limits.max_int_digits) {
    throw new WeftLimitError(`integer has ${digits} digits, exceeds max_int_digits=${limits.max_int_digits}`,
      {code: "WEFT_LIMIT_INT"});
  }
}

function checkStrBytes(s: string, limits: Limits): void {
  if (utf8Len(s) > limits.max_string_bytes) {
    throw new WeftLimitError(`string exceeds max_string_bytes=${limits.max_string_bytes}`,
      {code: "WEFT_LIMIT_STRING"});
  }
}

function checkColl(n: number, limits: Limits): void {
  if (n > limits.max_items_per_coll) {
    throw new WeftLimitError(`collection has ${n} items, exceeds max_items_per_coll=${limits.max_items_per_coll}`,
      {code: "WEFT_LIMIT_ITEMS"});
  }
}

/** Strict inverse of :func:`toJsonLossless` (SPEC §11.2.4), with the §9
 *  resource limits enforced and an iterative driver so deep input raises
 *  WEFT_LIMIT_DEPTH instead of overflowing the host stack.
 *
 *  Lexical int/float distinction: a ``JsonNum`` (from ``parseJsonLexical``
 *  / ``loadsJson``) carries the original token, so ``1.0`` is rejected and
 *  ``1`` becomes Integer per §11.2.1. A plain JS ``number`` (a tree from
 *  ``JSON.parse`` or built in memory) cannot carry that distinction; it is
 *  accepted iff integer-valued and in the safe range, which keeps
 *  ``fromJsonLossless(toJsonLossless(x)) === x`` but is weaker than the
 *  spec's lexical rule — use ``loadsJson`` for wire input. */
export function fromJsonLossless(value: unknown, limits: Limits = DEFAULT_LIMITS): WeftValue {
  const counter = new TotalCounter(limits.max_total_items);

  function* conv(node: unknown, depth: number): ConvGen {
    counter.bump();
    if (node === null) return null;
    if (node === true || node === false) return node;
    if (node instanceof JsonNum) {
      if (!node.isInteger()) {
        throw new WeftTypeError(
          'fractional number must use the {"$float": ...} tagged form (plain JSON number is reserved for Weft Integer)');
      }
      const v = BigInt(node.raw);
      if (v < SAFE_INT_MIN || v > SAFE_INT_MAX) {
        throw new WeftTypeError('integer outside JSON-safe range must use the {"$int-big": ...} tagged form');
      }
      checkIntDigits(v, limits);
      return v;
    }
    if (typeof node === "number") {
      if (Number.isNaN(node) || !Number.isFinite(node)) {
        throw new WeftTypeError('non-finite float must use the {"$float": ...} tagged form');
      }
      if (!Number.isInteger(node)) {
        throw new WeftTypeError(
          'fractional number must use the {"$float": ...} tagged form (plain JSON number is reserved for Weft Integer)');
      }
      if (node < Number(SAFE_INT_MIN) || node > Number(SAFE_INT_MAX)) {
        throw new WeftTypeError('integer outside JSON-safe range must use the {"$int-big": ...} tagged form');
      }
      const v = BigInt(node);
      checkIntDigits(v, limits);
      return v;
    }
    if (typeof node === "bigint") {
      if (node >= SAFE_INT_MIN && node <= SAFE_INT_MAX) return node;
      throw new WeftTypeError('bigint outside JSON-safe range must use the {"$int-big": ...} tagged form');
    }
    if (typeof node === "string") {
      throw new WeftTypeError('bare JSON string is not a lossless form; wrap it as {"$str":...}, {"$sym":...}, or {"$kw":...}');
    }
    if (Array.isArray(node)) {
      throw new WeftTypeError('bare JSON array is not a lossless form; use {"$list":[...]} or {"$sexpr":[...]}');
    }
    if (typeof node !== "object") {
      throw new WeftTypeError("unsupported JSON value");
    }
    const obj = node as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length !== 1) {
      throw new WeftTypeError(
        `lossless JSON object must have exactly one key (got ${keys.length}: ${JSON.stringify(keys)}); ` +
        `a plain mapping should be {"$map": {...}}`);
    }
    const tag = keys[0]!;
    const payload = obj[tag];
    if (!LOSSLESS_TAGS.has(tag)) {
      throw new WeftTypeError(`unknown lossless tag ${JSON.stringify(tag)}; expected one of ${[...LOSSLESS_TAGS].sort().join(", ")}`);
    }
    switch (tag) {
      case "$sym": {
        const s = unwrapStr(payload, "$sym");
        try { return new Symbol_(s); }
        catch (e) { throw new WeftTypeError(`$sym payload invalid: ${(e as Error).message}`); }
      }
      case "$kw": {
        const s = unwrapStr(payload, "$kw");
        try { return new Keyword(s); }
        catch (e) { throw new WeftTypeError(`$kw payload invalid: ${(e as Error).message}`); }
      }
      case "$bigint":
      case "$int-big": {
        const s = unwrapStr(payload, tag);
        if (!/^-?[0-9]+$/.test(s)) {
          throw new WeftTypeError(`${tag} payload is not a decimal integer: ${JSON.stringify(s)} (must match -?[0-9]+)`);
        }
        const nDigits = s.length - (s.startsWith("-") ? 1 : 0);
        if (nDigits > limits.max_int_digits) {
          throw new WeftLimitError(`${tag} payload has ${nDigits} digits, exceeds max_int_digits=${limits.max_int_digits}`,
            {code: "WEFT_LIMIT_INT"});
        }
        const v = BigInt(s);
        return tag === "$bigint" ? new BigIntValue(v) : v;
      }
      case "$dec": {
        const s = unwrapStr(payload, "$dec");
        const digitCount = (s.match(/[0-9]/g) ?? []).length;
        if (digitCount > limits.max_int_digits) {
          throw new WeftLimitError(`$dec payload has ${digitCount} digits, exceeds max_int_digits=${limits.max_int_digits}`,
            {code: "WEFT_LIMIT_INT"});
        }
        try { return new Decimal(s); }
        catch (e) { throw new WeftTypeError(`$dec payload invalid: ${(e as Error).message}`); }
      }
      case "$str": {
        const s = unwrapStr(payload, "$str");
        if (utf8Len(s) > limits.max_string_bytes) {
          throw new WeftLimitError(`$str payload exceeds max_string_bytes=${limits.max_string_bytes}`,
            {code: "WEFT_LIMIT_STRING"});
        }
        return s;
      }
      case "$bin": {
        if (!isPlainObject(payload)) throw new WeftTypeError("$bin payload must be an object with 'enc' and 'data'");
        const p = payload as Record<string, unknown>;
        const pKeys = Object.keys(p);
        if (pKeys.length !== 2 || !("enc" in p) || !("data" in p)) {
          throw new WeftTypeError("$bin payload must have exactly {enc, data}");
        }
        if (typeof p.data !== "string") throw new WeftTypeError("$bin.data must be a string");
        try {
          if (p.enc === "base64") return new Binary(base64Decode(p.data), "base64");
          if (p.enc === "hex") return new Binary(hexDecode(p.data), "hex");
        } catch (e) {
          throw new WeftTypeError(`$bin.data invalid: ${(e as Error).message}`);
        }
        throw new WeftTypeError(`$bin.enc must be 'base64' or 'hex', got ${JSON.stringify(p.enc)}`);
      }
      case "$dt": {
        const s = unwrapStr(payload, "$dt");
        if (!DT_PAYLOAD_RE.test(s)) {
          throw new WeftTypeError(`$dt payload is not a canonical datetime: ${JSON.stringify(s)}`);
        }
        try {
          const r = parse("#" + s);
          if (r instanceof Typed) {
            throw new WeftTypeError(`$dt payload must be a datetime scalar, not a type-annotated value: ${JSON.stringify(s)}`);
          }
          if (!(r instanceof WeftDate)) {
            throw new WeftTypeError(`$dt payload is not a datetime: ${JSON.stringify(s)}`);
          }
          return r;
        } catch (e) {
          if (e instanceof WeftTypeError) throw e;
          throw new WeftTypeError(`$dt payload invalid: ${JSON.stringify(s)} (${(e as Error).message})`);
        }
      }
      case "$float": {
        const s = unwrapStr(payload, "$float");
        if (s === "nan") return NaN;
        if (s === "inf") return Infinity;
        if (s === "-inf") return -Infinity;
        if (!FLOAT_DEC_RE.test(s)) {
          throw new WeftTypeError(`$float payload is not a finite decimal: ${JSON.stringify(s)}`);
        }
        const f = Number(s);
        if (Number.isNaN(f) || !Number.isFinite(f)) {
          throw new WeftTypeError(`$float payload parsed to non-finite: ${JSON.stringify(s)}`);
        }
        return (s === "-0.0" || s === "-0") ? -0 : f;
      }
      case "$sexpr": {
        if (!Array.isArray(payload) || payload.length === 0) throw new WeftTypeError("$sexpr payload must be a non-empty array");
        const tagName = payload[0];
        if (typeof tagName !== "string") throw new WeftTypeError("$sexpr tag (payload[0]) must be a string");
        let tagSym: Symbol_;
        try { tagSym = new Symbol_(tagName); }
        catch (e) { throw new WeftTypeError(`$sexpr tag is not a valid Symbol name: ${(e as Error).message}`); }
        checkDepth(depth + 1, limits);
        checkColl(payload.length - 1, limits);
        const items: WeftValue[] = [];
        for (const x of payload.slice(1)) items.push(yield [x, depth + 1]);
        return new Sexpr(tagSym, items);
      }
      case "$typed": {
        if (!isPlainObject(payload)) throw new WeftTypeError('$typed payload must be {"v":..., "t":...}');
        const p = payload as Record<string, unknown>;
        const pKeys = Object.keys(p);
        if (pKeys.length !== 2 || !("v" in p) || !("t" in p)) {
          throw new WeftTypeError('$typed payload must be {"v":..., "t":...}');
        }
        checkDepth(depth + 1, limits);
        const innerType = yield [p.t, depth + 1];
        if (!(innerType instanceof Symbol_) && !(innerType instanceof Sexpr)) {
          throw new WeftTypeError("$typed.t must resolve to a Symbol or Sexpr");
        }
        const innerValue = yield [p.v, depth + 1];
        return new Typed(innerValue, innerType);
      }
      case "$list": {
        if (!Array.isArray(payload)) throw new WeftTypeError("$list payload must be an array");
        checkDepth(depth + 1, limits);
        checkColl(payload.length, limits);
        const out: WeftValue[] = [];
        for (const x of payload) out.push(yield [x, depth + 1]);
        return out;
      }
      case "$map": {
        if (!isPlainObject(payload)) throw new WeftTypeError("$map payload must be an object");
        const p = payload as Record<string, unknown>;
        checkDepth(depth + 1, limits);
        checkColl(Object.keys(p).length, limits);
        const out: {[k: string]: WeftValue} = Object.create(null);
        for (const [k, v] of Object.entries(p)) {
          if (!isValidKeywordName(k)) {
            throw new WeftTypeError(`$map key ${JSON.stringify(k)} is not a valid Weft keyword name (would break structure on re-parse)`);
          }
          const cv = yield [v, depth + 1];
          Object.defineProperty(out, k, {value: cv, enumerable: true, writable: true, configurable: true});
        }
        return out;
      }
    }
    throw new WeftTypeError(`internal: unhandled lossless tag ${tag}`);
  }

  return runTrampoline(conv, value);
}

/** True when the UTF-16 sequence contains an unpaired surrogate half —
 *  i.e. it is not a valid Unicode scalar sequence (SPEC §5.7). */
function hasLoneSurrogate(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = i + 1 < s.length ? s.charCodeAt(i + 1) : -1;
      if (d < 0xdc00 || d > 0xdfff) return true;
      i++;
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function unwrapStr(payload: unknown, tag: string): string {
  if (typeof payload !== "string") throw new WeftTypeError(`${tag} payload must be a string`);
  // Every decoded-object entry point must enforce this, not only the
  // text reader: a host string from JSON.parse (or built by the caller)
  // can carry lone surrogates, which a Weft String must never hold —
  // UTF-8 output would silently turn them into U+FFFD.
  if (hasLoneSurrogate(payload)) {
    throw new WeftSyntaxError(`${tag} payload contains a lone surrogate (Weft strings are Unicode scalar sequences)`,
      {code: "WEFT_SYNTAX_JSON"});
  }
  return payload;
}

/** Lossy JSON → Weft (SPEC §11.3), iterative with §9 limits. ``JsonNum``
 *  values (from ``loadsJson``) carry the lexical int/float distinction the
 *  spec requires: ``1`` → Integer, ``1.0`` → Float. Plain JS numbers map
 *  to Float (the host type cannot witness the integer token shape). */
export function fromJson(value: unknown, opts: {strict?: boolean; limits?: Limits} = {}): WeftValue {
  const strict = opts.strict ?? true;
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const counter = new TotalCounter(limits.max_total_items);

  function* conv(node: unknown, depth: number): ConvGen {
    counter.bump();
    if (node === null) return null;
    if (node === true || node === false) return node;
    if (node instanceof JsonNum) {
      // SPEC §11.3: lexical distinction — integer token → Integer.
      if (node.isInteger()) {
        const v = BigInt(node.raw);
        checkIntDigits(v, limits);
        return v;
      }
      return Number(node.raw);
    }
    if (typeof node === "number") return node;
    if (typeof node === "bigint") { checkIntDigits(node, limits); return node; }
    if (typeof node === "string") {
      if (hasLoneSurrogate(node)) {
        throw new WeftSyntaxError("JSON string contains a lone surrogate (Weft strings are Unicode scalar sequences)",
          {code: "WEFT_SYNTAX_JSON"});
      }
      checkStrBytes(node, limits);
      return node;
    }
    if (Array.isArray(node)) {
      checkDepth(depth + 1, limits);
      checkColl(node.length, limits);
      const out: WeftValue[] = [];
      for (const x of node) out.push(yield [x, depth + 1]);
      return out;
    }
    if (typeof node === "object") {
      const obj = node as Record<string, unknown>;
      checkDepth(depth + 1, limits);
      checkColl(Object.keys(obj).length, limits);
      const out: {[k: string]: WeftValue} = Object.create(null);
      for (const [k, v] of Object.entries(obj)) {
        if (strict && !isValidKeywordName(k)) {
          throw new WeftTypeError(`JSON key ${JSON.stringify(k)} is not a valid Weft keyword`,
            {code: "WEFT_TYPE_JSON_KEY"});
        }
        const cv = yield [v, depth + 1];
        Object.defineProperty(out, k, {value: cv, enumerable: true, writable: true, configurable: true});
      }
      return out;
    }
    throw new WeftTypeError(`unsupported JSON value`);
  }

  return runTrampoline(conv, value);
}


/* --------------------------------------------------------------------------
 * 9. Equality (SPEC §5.16)
 * -------------------------------------------------------------------------- */

const _f64buf = new DataView(new ArrayBuffer(8));
function floatBits(f: number): string {
  _f64buf.setFloat64(0, f, true);
  return `${_f64buf.getUint32(0, true)}:${_f64buf.getUint32(4, true)}`;
}

/** Weft value equality ``a ≡ b`` per SPEC §5.16. Iterative (explicit work
 *  stack) so adversarially deep trees cannot exhaust the host call stack.
 *  Float equality is IEEE-754 bit identity except NaN ≢ NaN; -0.0 ≢ 0.0.
 *  Integer/BigInt compare numerically across host shapes (42 ≡ 42n). */
export function equals(a: WeftValue, b: WeftValue): boolean {
  const stack: Array<[WeftValue, WeftValue]> = [[a, b]];
  while (stack.length > 0) {
    const [x, y] = stack.pop()!;
    if (x === null || y === null) {
      if (!(x === null && y === null)) return false;
      continue;
    }
    if (typeof x === "boolean" || typeof y === "boolean") {
      if (!(typeof x === "boolean" && typeof y === "boolean" && x === y)) return false;
      continue;
    }
    const xi = x instanceof BigIntValue ? x.value : (typeof x === "bigint" ? x : null);
    const yi = y instanceof BigIntValue ? y.value : (typeof y === "bigint" ? y : null);
    if (xi !== null || yi !== null) {
      if (xi === null || yi === null || xi !== yi) return false;
      continue;
    }
    if (typeof x === "number" || typeof y === "number") {
      if (!(typeof x === "number" && typeof y === "number")) return false;
      if (Number.isNaN(x) || Number.isNaN(y)) return false; // NaN ≢ NaN
      if (floatBits(x) !== floatBits(y)) return false;
      continue;
    }
    if (x instanceof Decimal || y instanceof Decimal) {
      if (!(x instanceof Decimal && y instanceof Decimal && x.text === y.text)) return false;
      continue;
    }
    if (typeof x === "string" || typeof y === "string") {
      if (!(typeof x === "string" && typeof y === "string" && x === y)) return false;
      continue;
    }
    if (x instanceof Binary || y instanceof Binary) {
      if (!(x instanceof Binary && y instanceof Binary)) return false;
      if (x.data.length !== y.data.length) return false;
      for (let i = 0; i < x.data.length; i++) if (x.data[i] !== y.data[i]) return false;
      continue;
    }
    if (x instanceof WeftDate || y instanceof WeftDate) {
      if (!(x instanceof WeftDate && y instanceof WeftDate)) return false;
      if (x.year !== y.year || x.month !== y.month || x.day !== y.day
          || x.hasTime !== y.hasTime || x.hour !== y.hour || x.minute !== y.minute
          || x.second !== y.second || x.nano !== y.nano
          || x.tzOffsetMinutes !== y.tzOffsetMinutes) return false;
      continue;
    }
    if (x instanceof Symbol_ || y instanceof Symbol_) {
      if (!(x instanceof Symbol_ && y instanceof Symbol_ && x.name === y.name)) return false;
      continue;
    }
    if (x instanceof Keyword || y instanceof Keyword) {
      if (!(x instanceof Keyword && y instanceof Keyword && x.name === y.name)) return false;
      continue;
    }
    if (Array.isArray(x) || Array.isArray(y)) {
      if (!(Array.isArray(x) && Array.isArray(y) && x.length === y.length)) return false;
      for (let i = 0; i < x.length; i++) stack.push([x[i]!, y[i]!]);
      continue;
    }
    if (x instanceof Sexpr || y instanceof Sexpr) {
      if (!(x instanceof Sexpr && y instanceof Sexpr
            && x.tag.name === y.tag.name && x.items.length === y.items.length)) return false;
      for (let i = 0; i < x.items.length; i++) stack.push([x.items[i]!, y.items[i]!]);
      continue;
    }
    if (x instanceof Typed || y instanceof Typed) {
      if (!(x instanceof Typed && y instanceof Typed)) return false;
      stack.push([x.value, y.value]);
      stack.push([x.type as WeftValue, y.type as WeftValue]);
      continue;
    }
    if (isPlainObject(x) && isPlainObject(y)) {
      const xk = Object.keys(x), yk = Object.keys(y);
      if (xk.length !== yk.length) return false;
      for (const k of xk) {
        if (!Object.prototype.hasOwnProperty.call(y, k)) return false;
        stack.push([(x as any)[k], (y as any)[k]]);
      }
      continue;
    }
    return false;
  }
  return true;
}

export function dumpsJson(value: WeftValue,
                          opts: number | {lossless?: boolean; indent?: number} = {}): string {
  // Defaults to the LOSSLESS mapping (SPEC §11.2) so that
  // ``loadsJson(dumpsJson(v))`` round-trips for every value — symmetric
  // with ``loadsJson``'s default. Pass ``{lossless: false}`` for the
  // lossy §11.3 mapping. A bare number argument is accepted as
  // ``indent`` for backward compatibility.
  const o = typeof opts === "number" ? {indent: opts} : opts;
  const lossless = o.lossless ?? true;
  const j = lossless ? toJsonLossless(value) : toJson(value);
  return JSON.stringify(j, null, o.indent);
}

/** JSON text → Weft tree. SPEC §11.2: the LOSSLESS mapping is the default
 *  for round-trip operations; pass ``lossless: false`` for the lossy §11.3
 *  mapping. Uses the lexical reader, so duplicate keys are rejected
 *  (WEFT_SYNTAX_JSON_DUPKEY), ``1`` vs ``1.0`` keep their lexical shapes,
 *  and the §9 limits are enforced while reading. */
export function loadsJson(text: string, opts: {lossless?: boolean; limits?: Limits} = {}): WeftValue {
  const limits = opts.limits ?? DEFAULT_LIMITS;
  const j = parseJsonLexical(text, limits);
  if (opts.lossless ?? true) return fromJsonLossless(j, limits);
  return fromJson(j, {limits});
}

/* --------------------------------------------------------------------------
 * 8. Module metadata
 * -------------------------------------------------------------------------- */

export const VERSION = "1.0.1";
export const SPEC_VERSION = "1.0";
