// Package weft is the Go reference implementation of the Weft 1.0 data format.
// It parses, formats, and interconverts Weft documents and JSON. It passes
// the language-neutral conformance suite in tests/conformance/.
package weft

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

const Version = "1.0.2"
const SpecVersion = "1.0"

/* --------------------------------------------------------------------------
 * Errors
 * -------------------------------------------------------------------------- */

type ErrorKind int

const (
	ErrSyntax ErrorKind = iota
	ErrLex
	ErrEncoding
	ErrLimit
	ErrType
	ErrSchema
)

type Error struct {
	Kind    ErrorKind
	Message string
	Code    string
	Line    int
	Column  int
	// Offset is the UTF-8 byte offset into the input (also returned by
	// ByteOffset below for cross-impl field-name compatibility).
	Offset     int
	ByteOffset int
}

func (e *Error) Error() string {
	if e.Line > 0 && e.Column > 0 {
		return fmt.Sprintf("[line %d:%d] %s", e.Line, e.Column, e.Message)
	}
	return e.Message
}

func newErr(k ErrorKind, msg, code string) *Error {
	return &Error{Kind: k, Message: msg, Code: code}
}

// withPos sets Line, Column, and Offset (byte offset). ByteOffset is also
// populated so callers that use that explicit name read the same value.
func withPos(e *Error, line, col, off int) *Error {
	e.Line = line
	e.Column = col
	e.Offset = off
	e.ByteOffset = off
	return e
}

/* --------------------------------------------------------------------------
 * Limits
 * -------------------------------------------------------------------------- */

const (
	DefaultMaxDepth         = 128
	DefaultMaxSizeBytes     = 10 * 1024 * 1024
	DefaultMaxItemsPerColl  = 1 << 20
	DefaultMaxStringBytes   = 10 * 1024 * 1024
	DefaultMaxIntDigits     = 10_000  // spec §9 mandatory default
	DefaultMaxTotalItems    = 10_000_000

	HardMaxDepth        = 1024
	HardMaxSizeBytes    = 1 << 30
	HardMaxItemsPerColl = 1 << 24
	HardMaxStringBytes  = 1 << 30
	HardMaxIntDigits    = 1_000_000
	HardMaxTotalItems   = 100_000_000
)

type Limits struct {
	MaxDepth        int
	MaxSizeBytes    int
	MaxItemsPerColl int
	MaxStringBytes  int
	MaxIntDigits    int
	MaxTotalItems   int
}

func DefaultLimits() Limits {
	return Limits{
		MaxDepth:        DefaultMaxDepth,
		MaxSizeBytes:    DefaultMaxSizeBytes,
		MaxItemsPerColl: DefaultMaxItemsPerColl,
		MaxStringBytes:  DefaultMaxStringBytes,
		MaxIntDigits:    DefaultMaxIntDigits,
		MaxTotalItems:   DefaultMaxTotalItems,
	}
}

func (l Limits) validate() error {
	checks := []struct {
		name     string
		v        int
		lo, hi   int
	}{
		{"MaxDepth", l.MaxDepth, 1, HardMaxDepth},
		{"MaxSizeBytes", l.MaxSizeBytes, 1, HardMaxSizeBytes},
		{"MaxItemsPerColl", l.MaxItemsPerColl, 1, HardMaxItemsPerColl},
		{"MaxStringBytes", l.MaxStringBytes, 1, HardMaxStringBytes},
		{"MaxIntDigits", l.MaxIntDigits, 1, HardMaxIntDigits},
		{"MaxTotalItems", l.MaxTotalItems, 1, HardMaxTotalItems},
	}
	for _, c := range checks {
		if c.v < c.lo {
			return newErr(ErrLimit, fmt.Sprintf("%s=%d below min %d", c.name, c.v, c.lo), "WEFT_LIMIT_CONFIG")
		}
		if c.v > c.hi {
			return newErr(ErrLimit, fmt.Sprintf("%s=%d above max %d", c.name, c.v, c.hi), "WEFT_LIMIT_CONFIG")
		}
	}
	return nil
}

/* --------------------------------------------------------------------------
 * Value types
 * -------------------------------------------------------------------------- */

type Symbol string
type Keyword string

type BigIntVal struct{ Value *big.Int }

type BinaryEncoding int

const (
	Base64Enc BinaryEncoding = iota
	HexEnc
)

type Binary struct {
	Data     []byte
	Encoding BinaryEncoding
}

type Decimal struct{ Text string }

type Date struct {
	Year            int
	Month           int
	Day             int
	HasTime         bool
	Hour            int
	Minute          int
	Second          int
	Nano            int
	TZOffsetMinutes *int // nil = no zone
}

// Sexpr is a tagged node.
type Sexpr struct {
	Tag   Symbol
	Items []Value
}

// TypeExpr is Symbol or Sexpr.
type TypeExpr interface{ isTypeExpr() }

func (Symbol) isTypeExpr() {}
func (Sexpr) isTypeExpr()  {}

type Typed struct {
	Value Value
	Type  TypeExpr
}

// Value is a sum type over all Weft values. It is represented as interface{}
// (any) with concrete types below. Primitives (nil, bool, float64, string)
// use their native Go types.
type Value = any

// MapOrdered preserves map insertion order.
type MapOrdered struct {
	Keys  []string
	Items map[string]Value
}

func NewMap() *MapOrdered { return &MapOrdered{Items: map[string]Value{}} }

func (m *MapOrdered) Set(k string, v Value) bool {
	if _, exists := m.Items[k]; exists { return false }
	m.Keys = append(m.Keys, k)
	m.Items[k] = v
	return true
}
func (m *MapOrdered) Get(k string) (Value, bool) {
	v, ok := m.Items[k]
	return v, ok
}
func (m *MapOrdered) Len() int { return len(m.Keys) }

/* --------------------------------------------------------------------------
 * Lexer
 * -------------------------------------------------------------------------- */

type tokKind int

const (
	tkLParen tokKind = iota
	tkRParen
	tkLBrack
	tkRBrack
	tkLBrace
	tkRBrace
	tkColonColon
	tkInt
	tkBigInt
	tkFloat
	tkDecimal
	tkString
	tkBinary
	tkDateTime
	tkBool
	tkNil
	tkNan
	tkInf
	tkNegInf
	tkSymbol
	tkKeyword
)

type token struct {
	kind   tokKind
	value  any
	line   int
	column int
	offset int
}

type lexer struct {
	runes  []rune
	byteOf []int // byte offset of each rune
	idx    int
	line   int
	col    int
	limits Limits
}

func newLexer(text string, limits Limits) (*lexer, error) {
	if strings.ContainsRune(text, '\uFEFF') {
		idx := strings.IndexRune(text, '\uFEFF')
		line, col := posFromByteOffset(text, idx)
		return nil, withPos(newErr(ErrEncoding, "U+FEFF (BOM) is not permitted anywhere in a Weft document", "WEFT_ENCODING_BOM"), line, col, idx)
	}
	for _, bad := range []rune{'\uFFFE', '\uFFFF'} {
		if idx := strings.IndexRune(text, bad); idx >= 0 {
			line, col := posFromByteOffset(text, idx)
			return nil, withPos(newErr(ErrEncoding, fmt.Sprintf("noncharacter U+%04X not permitted", bad), "WEFT_ENCODING_NONCHAR"), line, col, idx)
		}
	}
	if len(text) > limits.MaxSizeBytes {
		return nil, newErr(ErrLimit, fmt.Sprintf("document size %d exceeds MaxSizeBytes=%d", len(text), limits.MaxSizeBytes), "WEFT_LIMIT_SIZE")
	}
	if !utf8.ValidString(text) {
		return nil, newErr(ErrEncoding, "input is not valid UTF-8", "WEFT_ENCODING_UTF8")
	}
	runes := []rune(text)
	byteOf := make([]int, len(runes)+1)
	b := 0
	for i, r := range runes {
		byteOf[i] = b
		b += utf8.RuneLen(r)
	}
	byteOf[len(runes)] = len(text)
	return &lexer{runes: runes, byteOf: byteOf, idx: 0, line: 1, col: 1, limits: limits}, nil
}

func posFromByteOffset(text string, offset int) (int, int) {
	line, col := 1, 1
	for i, r := range text {
		if i >= offset { break }
		if r == '\n' { line++; col = 1 } else { col++ }
	}
	return line, col
}

func (l *lexer) peek(o int) rune {
	if l.idx+o < len(l.runes) { return l.runes[l.idx+o] }
	return -1
}

func (l *lexer) advance(n int) {
	for i := 0; i < n; i++ {
		if l.idx >= len(l.runes) { return }
		r := l.runes[l.idx]
		switch {
		case r == '\n':
			l.line++; l.col = 1
		case r == '\r':
			// CRLF: defer line++ to the \n. Lone CR: counts as a line
			// terminator by itself (SPEC §3).
			var next rune = -1
			if l.idx+1 < len(l.runes) { next = l.runes[l.idx+1] }
			if next != '\n' { l.line++; l.col = 1 }
		default:
			l.col++
		}
		l.idx++
	}
}

func (l *lexer) err(msg, code string) *Error {
	return withPos(newErr(ErrLex, msg, code), l.line, l.col, l.byteOf[l.idx])
}

func isWS(r rune) bool { return r == '\t' || r == '\n' || r == '\r' || r == ' ' || r == ',' }

func isRejectedWS(r rune) bool {
	if isWS(r) { return false }
	if r == 0x0B || r == 0x0C { return true }
	if r == 0xA0 { return true }
	if r >= 0x2000 && r <= 0x200A { return true }
	switch r {
	case 0x2028, 0x2029, 0x205F, 0x3000: return true
	}
	return false
}

func isForbiddenCtrl(r rune) bool {
	return r <= 0x08 || r == 0x0B || r == 0x0C || (r >= 0x0E && r <= 0x1F)
}

// Unicode bidirectional overrides / isolates (CVE-2021-42574).
// Rejected inside comments to prevent Trojan Source attacks.
func isBidiControl(r rune) bool {
	return (r >= 0x202A && r <= 0x202E) || (r >= 0x2066 && r <= 0x2069)
}

func isCommentForbidden(r rune) bool {
	// SPEC §4.2 / §10.5: comment-forbidden characters are:
	//   C0 controls except TAB/LF/CR (isForbiddenCtrl)
	//   DEL (U+007F)
	//   C1 controls (U+0080-U+009F, including CSI U+009B)
	//   Bidi override/isolate (isBidiControl)
	return isForbiddenCtrl(r) ||
		isBidiControl(r) ||
		r == 0x7F ||
		(r >= 0x80 && r <= 0x9F)
}

func isSymbolStart(r rune) bool {
	if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') { return true }
	if r == '_' || r == '+' || r == '-' || r == '*' || r == '/' ||
		r == '<' || r == '>' || r == '=' || r == '!' || r == '?' { return true }
	if r > 0x7F && unicode.IsLetter(r) { return true }
	return false
}

func isSymbolCont(r rune) bool {
	if isSymbolStart(r) || (r >= '0' && r <= '9') { return true }
	// Above ASCII: letters only. Unicode digits (Nd / No, e.g. '１' / '①'
	// / '¼') are NOT symbol-cont — the formatter cannot round-trip them,
	// so the lexer refuses them too (SPEC §5.10).
	if r > 0x7F && unicode.IsLetter(r) { return true }
	return false
}

func (l *lexer) tokenize() ([]token, error) {
	var toks []token
	for {
		t, err := l.nextToken()
		if err != nil { return nil, err }
		if t == nil { return toks, nil }
		toks = append(toks, *t)
	}
}

func (l *lexer) nextToken() (*token, error) {
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		if isWS(r) { l.advance(1); continue }
		if isRejectedWS(r) {
			return nil, l.err(fmt.Sprintf("Unicode whitespace U+%04X not permitted", r), "WEFT_SYNTAX_WS")
		}
		if r == ';' { if err := l.skipLineComment(); err != nil { return nil, err }; continue }
		if r == '#' && l.peek(1) == '|' { if err := l.skipBlockComment(); err != nil { return nil, err }; continue }
		break
	}
	if l.idx >= len(l.runes) { return nil, nil }

	sl, sc, so := l.line, l.col, l.byteOf[l.idx]
	r := l.runes[l.idx]
	nxt := l.peek(1)

	simple := map[rune]tokKind{
		'(': tkLParen, ')': tkRParen, '[': tkLBrack, ']': tkRBrack, '{': tkLBrace, '}': tkRBrace,
	}
	if k, ok := simple[r]; ok {
		l.advance(1)
		return &token{kind: k, line: sl, column: sc, offset: so}, nil
	}
	if r == ':' && nxt == ':' {
		l.advance(2)
		return &token{kind: tkColonColon, line: sl, column: sc, offset: so}, nil
	}
	if r == ':' {
		l.advance(1)
		name := l.readSymbolName()
		if name == "" {
			return nil, withPos(newErr(ErrLex, "':' must be followed by a symbol name", "WEFT_SYNTAX_KEYWORD"), sl, sc, so)
		}
		// SPEC §5.10: a name beginning with sign+digit is reserved for
		// signed-number literals — ``:+1`` must not parse.
		if len(name) >= 2 && (name[0] == '+' || name[0] == '-') && name[1] >= '0' && name[1] <= '9' {
			return nil, withPos(newErr(ErrLex,
				fmt.Sprintf("keyword name %q starts with sign+digit, which ambiguates with a signed number literal", name),
				"WEFT_SYNTAX_KEYWORD"), sl, sc, so)
		}
		return &token{kind: tkKeyword, value: name, line: sl, column: sc, offset: so}, nil
	}

	if r == '"' {
		if l.peek(1) == '"' && l.peek(2) == '"' {
			s, err := l.readTripleString()
			if err != nil { return nil, err }
			return &token{kind: tkString, value: s, line: sl, column: sc, offset: so}, nil
		}
		s, err := l.readDquoteString()
		if err != nil { return nil, err }
		return &token{kind: tkString, value: s, line: sl, column: sc, offset: so}, nil
	}
	if r == 'r' && nxt == '"' {
		l.advance(1)
		s, err := l.readRawString()
		if err != nil { return nil, err }
		return &token{kind: tkString, value: s, line: sl, column: sc, offset: so}, nil
	}
	if r == 'b' && nxt == '"' {
		l.advance(1)
		b, err := l.readBinary()
		if err != nil { return nil, err }
		return &token{kind: tkBinary, value: b, line: sl, column: sc, offset: so}, nil
	}
	if r == '#' {
		if nxt == -1 { return nil, l.err("stray '#' at end of input", "WEFT_SYNTAX_HASH") }
		if nxt == '!' { return nil, l.err("'#!' is reserved", "WEFT_RESERVED_SHEBANG") }
		if nxt >= '0' && nxt <= '9' {
			return l.readDatetime(sl, sc, so)
		}
		return nil, l.err(fmt.Sprintf("'#%c' is a reserved syntax", nxt), "WEFT_RESERVED_HASH")
	}

	if r == '+' || r == '-' || (r >= '0' && r <= '9') {
		t, err := l.readNumberOrSignLit(sl, sc, so)
		if err != nil { return nil, err }
		if t != nil { return t, nil }
	}

	if isSymbolStart(r) {
		name := l.readSymbolName()
		switch name {
		case "true":  return &token{kind: tkBool, value: true, line: sl, column: sc, offset: so}, nil
		case "false": return &token{kind: tkBool, value: false, line: sl, column: sc, offset: so}, nil
		case "nil":   return &token{kind: tkNil, line: sl, column: sc, offset: so}, nil
		case "nan":   return &token{kind: tkNan, line: sl, column: sc, offset: so}, nil
		case "inf":   return &token{kind: tkInf, line: sl, column: sc, offset: so}, nil
		}
		if strings.HasPrefix(name, "$") {
			return nil, withPos(newErr(ErrLex, fmt.Sprintf("symbols beginning with '$' are reserved: %s", name), "WEFT_RESERVED_DOLLAR"), sl, sc, so)
		}
		if len(name) >= 2 {
			rs := []rune(name)
			if (rs[0] == '+' || rs[0] == '-') && rs[1] >= '0' && rs[1] <= '9' {
				return nil, withPos(newErr(ErrLex, fmt.Sprintf("invalid symbol %s: starts with sign+digit", name), "WEFT_SYNTAX_SYMBOL_AMBIGUOUS"), sl, sc, so)
			}
		}
		return &token{kind: tkSymbol, value: name, line: sl, column: sc, offset: so}, nil
	}

	return nil, withPos(newErr(ErrLex, fmt.Sprintf("unexpected character U+%04X", r), "WEFT_SYNTAX_CHAR"), sl, sc, so)
}

func (l *lexer) skipLineComment() error {
	l.advance(1)
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		// Any line terminator ends a comment: LF, CR, or CRLF. We stop
		// without consuming it so advance() handles line counting next.
		if r == '\n' || r == '\r' { return nil }
		if isCommentForbidden(r) {
			return l.err(fmt.Sprintf("control char U+%04X not permitted in comment", r), "WEFT_SYNTAX_COMMENT_CTRL")
		}
		l.advance(1)
	}
	return nil
}

func (l *lexer) skipBlockComment() error {
	l.advance(2)
	depth := 1
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		n := l.peek(1)
		if r == '#' && n == '|' { depth++; l.advance(2); continue }
		if r == '|' && n == '#' { depth--; l.advance(2); if depth == 0 { return nil }; continue }
		if isCommentForbidden(r) {
			return l.err(fmt.Sprintf("control char U+%04X not permitted in comment", r), "WEFT_SYNTAX_COMMENT_CTRL")
		}
		l.advance(1)
	}
	return l.err("unterminated block comment", "WEFT_SYNTAX_COMMENT_UNTERMINATED")
}

func (l *lexer) readSymbolName() string {
	start := l.idx
	if start >= len(l.runes) { return "" }
	if !isSymbolStart(l.runes[start]) { return "" }
	l.advance(1)
	for l.idx < len(l.runes) && isSymbolCont(l.runes[l.idx]) { l.advance(1) }
	return string(l.runes[start:l.idx])
}

func (l *lexer) readDquoteString() (string, error) {
	l.advance(1)
	var sb strings.Builder
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		if r == '"' { l.advance(1); s := sb.String(); if err := l.checkStringLimit(s); err != nil { return "", err }; return s, nil }
		if isForbiddenCtrl(r) { return "", l.err(fmt.Sprintf("control char U+%04X not permitted in string", r), "WEFT_SYNTAX_STR_CTRL") }
		if r == '\n' || r == '\t' || r == '\r' {
			return "", l.err("literal tab/newline/CR not permitted in standard string", "WEFT_SYNTAX_STR_CTRL")
		}
		if r == '\\' {
			ch, err := l.readEscape()
			if err != nil { return "", err }
			sb.WriteRune(ch)
			continue
		}
		sb.WriteRune(r)
		l.advance(1)
	}
	return "", l.err("unterminated string literal", "WEFT_SYNTAX_STR_UNTERMINATED")
}

// isDelOrC1 reports whether r is DEL (U+007F) or a C1 control
// (U+0080-U+009F) — forbidden literal inside raw and triple-quoted
// strings (SPEC §5.7).
func isDelOrC1(r rune) bool { return r == 0x7F || (r >= 0x80 && r <= 0x9F) }

func (l *lexer) readTripleString() (string, error) {
	l.advance(3)
	var sb strings.Builder
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		if r == '"' && l.peek(1) == '"' && l.peek(2) == '"' {
			l.advance(3)
			s := sb.String()
			if err := l.checkStringLimit(s); err != nil { return "", err }
			return s, nil
		}
		if isForbiddenCtrl(r) || isDelOrC1(r) { return "", l.err(fmt.Sprintf("control char U+%04X in string", r), "WEFT_SYNTAX_STR_CTRL") }
		if r == '\\' {
			ch, err := l.readEscape()
			if err != nil { return "", err }
			sb.WriteRune(ch)
			continue
		}
		sb.WriteRune(r)
		l.advance(1)
	}
	return "", l.err("unterminated triple-quoted string", "WEFT_SYNTAX_STR_UNTERMINATED")
}

func (l *lexer) readRawString() (string, error) {
	l.advance(1)
	var sb strings.Builder
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		if r == '"' { l.advance(1); s := sb.String(); if err := l.checkStringLimit(s); err != nil { return "", err }; return s, nil }
		if isForbiddenCtrl(r) || isDelOrC1(r) { return "", l.err(fmt.Sprintf("control char U+%04X in raw string", r), "WEFT_SYNTAX_STR_CTRL") }
		sb.WriteRune(r)
		l.advance(1)
	}
	return "", l.err("unterminated raw string", "WEFT_SYNTAX_STR_UNTERMINATED")
}

func (l *lexer) readEscape() (rune, error) {
	l.advance(1)
	if l.idx >= len(l.runes) { return 0, l.err("truncated escape", "WEFT_SYNTAX_ESCAPE") }
	c := l.runes[l.idx]
	simple := map[rune]rune{'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', '0': 0}
	if r, ok := simple[c]; ok { l.advance(1); return r, nil }
	if c == 'x' {
		l.advance(1)
		h, err := l.readHexDigits(2)
		if err != nil { return 0, err }
		cp, _ := strconv.ParseInt(h, 16, 32)
		if cp > 0x7F {
			return 0, l.err(fmt.Sprintf("\\x%s escapes bytes 00-7F only", h), "WEFT_SYNTAX_ESCAPE_XRANGE")
		}
		return rune(cp), nil
	}
	if c == 'u' {
		l.advance(1)
		var hex string
		var err error
		if l.idx < len(l.runes) && l.runes[l.idx] == '{' {
			l.advance(1)
			hex, err = l.readHexUntilBrace()
			if err != nil { return 0, err }
			l.advance(1) // }
		} else {
			hex, err = l.readHexDigits(4)
			if err != nil { return 0, err }
		}
		cp, perr := strconv.ParseInt(hex, 16, 32)
		if perr != nil { return 0, l.err("invalid hex escape", "WEFT_SYNTAX_ESCAPE") }
		if cp > 0x10FFFF {
			return 0, l.err(fmt.Sprintf("\\u escape U+%X exceeds Unicode max", cp), "WEFT_SYNTAX_ESCAPE_MAX")
		}
		if cp >= 0xD800 && cp <= 0xDFFF {
			return 0, l.err(fmt.Sprintf("\\u escape U+%04X is surrogate half", cp), "WEFT_SYNTAX_ESCAPE_SURROGATE")
		}
		return rune(cp), nil
	}
	return 0, l.err(fmt.Sprintf("unknown escape \\%c", c), "WEFT_SYNTAX_ESCAPE_UNKNOWN")
}

func (l *lexer) readHexDigits(n int) (string, error) {
	var sb strings.Builder
	for i := 0; i < n; i++ {
		if l.idx >= len(l.runes) { return "", l.err("truncated hex escape", "WEFT_SYNTAX_ESCAPE") }
		r := l.runes[l.idx]
		if !isHexDigit(r) { return "", l.err(fmt.Sprintf("invalid hex digit %c", r), "WEFT_SYNTAX_ESCAPE") }
		sb.WriteRune(r)
		l.advance(1)
	}
	return sb.String(), nil
}

func (l *lexer) readHexUntilBrace() (string, error) {
	var sb strings.Builder
	for l.idx < len(l.runes) && l.runes[l.idx] != '}' {
		r := l.runes[l.idx]
		if !isHexDigit(r) { return "", l.err("invalid hex digit in \\u{...}", "WEFT_SYNTAX_ESCAPE") }
		sb.WriteRune(r)
		l.advance(1)
		if sb.Len() > 6 { return "", l.err("\\u{...} has more than 6 hex digits", "WEFT_SYNTAX_ESCAPE") }
	}
	if sb.Len() == 0 { return "", l.err("\\u{...} has no hex digits", "WEFT_SYNTAX_ESCAPE") }
	if l.idx >= len(l.runes) { return "", l.err("missing } in \\u{...}", "WEFT_SYNTAX_ESCAPE") }
	return sb.String(), nil
}

func isHexDigit(r rune) bool {
	return (r >= '0' && r <= '9') || (r >= 'a' && r <= 'f') || (r >= 'A' && r <= 'F')
}

func (l *lexer) checkStringLimit(s string) error {
	if len(s) > l.limits.MaxStringBytes {
		return l.err(fmt.Sprintf("string %d bytes exceeds MaxStringBytes=%d", len(s), l.limits.MaxStringBytes), "WEFT_LIMIT_STRING")
	}
	return nil
}

func (l *lexer) readBinary() (Binary, error) {
	l.advance(1)
	var body strings.Builder
	for l.idx < len(l.runes) && l.runes[l.idx] != '"' {
		r := l.runes[l.idx]
		if isForbiddenCtrl(r) || r == ' ' || r == '\n' || r == '\t' || r == '\r' {
			return Binary{}, l.err("whitespace/control in binary literal", "WEFT_SYNTAX_BINARY")
		}
		body.WriteRune(r)
		l.advance(1)
	}
	if l.idx >= len(l.runes) { return Binary{}, l.err("unterminated binary", "WEFT_SYNTAX_BINARY") }
	l.advance(1)
	b := body.String()
	if payload, ok := strings.CutPrefix(b, "base64:"); ok {
		if payload == "" { return Binary{Encoding: Base64Enc}, nil }
		if !isValidB64(payload) {
			return Binary{}, l.err("invalid base64", "WEFT_SYNTAX_BINARY_BASE64")
		}
		if len(payload)%4 != 0 {
			return Binary{}, l.err("base64 length not multiple of 4", "WEFT_SYNTAX_BINARY_BASE64")
		}
		data, err := base64.StdEncoding.DecodeString(payload)
		if err != nil { return Binary{}, l.err("invalid base64", "WEFT_SYNTAX_BINARY_BASE64") }
		// W4-16 / SPEC §5.8: canonical check — reject non-zero trailing
		// bits so a single byte sequence has exactly one base64 spelling.
		re := base64.StdEncoding.EncodeToString(data)
		if re != payload {
			return Binary{}, l.err(
				fmt.Sprintf("non-canonical base64 in binary literal (trailing bits must be zero): %q", payload),
				"WEFT_SYNTAX_BINARY_BASE64")
		}
		return Binary{Data: data, Encoding: Base64Enc}, nil
	}
	if payload, ok := strings.CutPrefix(b, "hex:"); ok {
		if len(payload)%2 != 0 { return Binary{}, l.err("hex binary must have even number of digits", "WEFT_SYNTAX_BINARY_HEX") }
		data, err := hex.DecodeString(payload)
		if err != nil { return Binary{}, l.err("invalid hex", "WEFT_SYNTAX_BINARY_HEX") }
		return Binary{Data: data, Encoding: HexEnc}, nil
	}
	return Binary{}, l.err("binary literal must start with 'base64:' or 'hex:'", "WEFT_SYNTAX_BINARY")
}

func isValidB64(s string) bool {
	for _, r := range s {
		if !((r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '+' || r == '/' || r == '=') {
			return false
		}
	}
	return true
}

var dtRe = regexp.MustCompile(`^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?)?$`)

func (l *lexer) readDatetime(sl, sc, so int) (*token, error) {
	l.advance(1)
	start := l.idx
	for l.idx < len(l.runes) {
		r := l.runes[l.idx]
		if isWS(r) || r == ')' || r == ']' || r == '}' || r == '(' || r == '[' || r == '{' { break }
		// SPEC §4.3: `"` is a token boundary — a string literal may
		// directly follow a datetime ([#2026-11-02"a"]).
		if r == '"' { break }
		// ``;`` starts a line comment and ``#|`` starts a block comment —
		// both terminate the datetime literal.
		if r == ';' { break }
		if r == '#' && l.peek(1) == '|' { break }
		if r == ':' && l.peek(1) == ':' { break }
		l.advance(1)
	}
	body := string(l.runes[start:l.idx])
	d, err := parseDatetime(body, sl, sc, so)
	if err != nil { return nil, err }
	return &token{kind: tkDateTime, value: d, line: sl, column: sc, offset: so}, nil
}

func parseDatetime(body string, sl, sc, so int) (Date, error) {
	m := dtRe.FindStringSubmatch(body)
	if m == nil {
		return Date{}, withPos(newErr(ErrLex, "invalid datetime", "WEFT_SYNTAX_DATETIME"), sl, sc, so)
	}
	y, _ := strconv.Atoi(m[1])
	mo, _ := strconv.Atoi(m[2])
	day, _ := strconv.Atoi(m[3])
	if y < 1 || y > 9999 { return Date{}, withPos(newErr(ErrLex, "year out of range", "WEFT_SYNTAX_DATETIME_YEAR"), sl, sc, so) }
	if mo < 1 || mo > 12 { return Date{}, withPos(newErr(ErrLex, "invalid month", "WEFT_SYNTAX_DATETIME"), sl, sc, so) }
	maxDay := daysInMonth(y, mo)
	if day < 1 || day > maxDay { return Date{}, withPos(newErr(ErrLex, "invalid day", "WEFT_SYNTAX_DATETIME"), sl, sc, so) }
	if m[4] == "" {
		return Date{Year: y, Month: mo, Day: day}, nil
	}
	h, _ := strconv.Atoi(m[4])
	mi, _ := strconv.Atoi(m[5])
	s, _ := strconv.Atoi(m[6])
	if h > 23 || mi > 59 || s > 60 { return Date{}, withPos(newErr(ErrLex, "time out of range", "WEFT_SYNTAX_DATETIME"), sl, sc, so) }
	nano := 0
	if m[7] != "" {
		padded := m[7] + strings.Repeat("0", 9-len(m[7]))
		nano, _ = strconv.Atoi(padded)
	}
	var tzOff *int
	if m[8] != "" {
		if m[8] == "Z" {
			z := 0
			tzOff = &z
		} else {
			sign := 1
			if m[8][0] == '-' { sign = -1 }
			hh, _ := strconv.Atoi(m[8][1:3])
			mm, _ := strconv.Atoi(m[8][4:6])
			if hh > 14 || mm > 59 || (hh == 14 && mm > 0) {
				return Date{}, withPos(newErr(ErrLex, fmt.Sprintf("tz offset %s out of range (|offset| <= 14:00)", m[8]), "WEFT_SYNTAX_DATETIME"), sl, sc, so)
			}
			z := sign * (hh*60 + mm)
			tzOff = &z
		}
	}
	// Leap seconds (:60) only occur at 23:59:60 UTC. In local time they
	// appear at the hour/minute that maps back to 23:59 UTC after
	// applying the stated offset. ``08:59:60+09:00`` is thus legal.
	if s == 60 {
		off := 0
		if tzOff != nil { off = *tzOff }
		total := h*60 + mi - off
		total = ((total % 1440) + 1440) % 1440
		if total != 23*60+59 {
			zoneLabel := "local"
			if tzOff != nil { zoneLabel = m[8] }
			return Date{}, withPos(newErr(ErrLex,
				fmt.Sprintf("leap-second :60 only valid at 23:59:60 UTC (got %02d:%02d:60 at offset %s)", h, mi, zoneLabel),
				"WEFT_SYNTAX_DATETIME"), sl, sc, so)
		}
	}
	return Date{Year: y, Month: mo, Day: day, HasTime: true, Hour: h, Minute: mi, Second: s, Nano: nano, TZOffsetMinutes: tzOff}, nil
}

func daysInMonth(y, m int) int {
	days := [12]int{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31}
	if m == 2 && isLeap(y) { return 29 }
	return days[m-1]
}
func isLeap(y int) bool { return (y%4 == 0 && y%100 != 0) || y%400 == 0 }

func (l *lexer) readNumberOrSignLit(sl, sc, so int) (*token, error) {
	p := l.idx
	sign := ""
	if p < len(l.runes) && (l.runes[p] == '+' || l.runes[p] == '-') {
		sign = string(l.runes[p]); p++
	}
	// Check signed inf
	if p+3 <= len(l.runes) && string(l.runes[p:p+3]) == "inf" {
		after := rune(-1)
		if p+3 < len(l.runes) { after = l.runes[p+3] }
		if after == -1 || !isSymbolCont(after) {
			adv := p + 3 - l.idx
			for i := 0; i < adv; i++ { l.advance(1) }
			if sign == "-" { return &token{kind: tkNegInf, line: sl, column: sc, offset: so}, nil }
			return &token{kind: tkInf, line: sl, column: sc, offset: so}, nil
		}
	}
	if p >= len(l.runes) || l.runes[p] < '0' || l.runes[p] > '9' { return nil, nil }

	// Hex/Oct/Bin
	if l.runes[p] == '0' && p+1 < len(l.runes) {
		bc := l.runes[p+1]
		if bc == 'x' || bc == 'X' || bc == 'o' || bc == 'O' || bc == 'b' || bc == 'B' {
			baseChar := unicode.ToLower(bc)
			p += 2
			digitStart := p
			allowed := func(r rune) bool {
				switch baseChar {
				case 'x': return isHexDigit(r) || r == '_'
				case 'o': return r >= '0' && r <= '7' || r == '_'
				case 'b': return r == '0' || r == '1' || r == '_'
				}
				return false
			}
			for p < len(l.runes) && allowed(l.runes[p]) { p++ }
			dt := string(l.runes[digitStart:p])
			if err := validateUnderscores(dt, sl, sc, so); err != nil { return nil, err }
			cleaned := strings.ReplaceAll(dt, "_", "")
			if cleaned == "" {
				return nil, withPos(newErr(ErrLex, fmt.Sprintf("base-%c integer has no digits", baseChar), "WEFT_SYNTAX_INT"), sl, sc, so)
			}
			base := map[rune]int{'x': 16, 'o': 8, 'b': 2}[baseChar]
			// Check against decimal-equivalent digit count so a 4000-
			// digit ``0xf…f`` cannot sneak past MaxIntDigits only to blow
			// it up when the formatter emits the same value in base 10.
			if err := l.checkIntDigitsBase(cleaned, sl, sc, so, base); err != nil { return nil, err }
			bi := new(big.Int)
			bi.SetString(cleaned, base)
			if sign == "-" { bi.Neg(bi) }
			if p < len(l.runes) && l.runes[p] == 'n' {
				p++
				adv := p - l.idx
				for i := 0; i < adv; i++ { l.advance(1) }
				if l.idx < len(l.runes) && isSymbolCont(l.runes[l.idx]) {
					return nil, l.err("unexpected char after 'n' suffix", "WEFT_SYNTAX_INT")
				}
				return &token{kind: tkBigInt, value: BigIntVal{Value: bi}, line: sl, column: sc, offset: so}, nil
			}
			adv := p - l.idx
			for i := 0; i < adv; i++ { l.advance(1) }
			if l.idx < len(l.runes) && isSymbolCont(l.runes[l.idx]) {
				return nil, l.err("unexpected char after integer; only 'n' (BigInt) and 'm' (Decimal) suffixes are supported", "WEFT_RESERVED_NUMBER_SUFFIX")
			}
			return &token{kind: tkInt, value: bi, line: sl, column: sc, offset: so}, nil
		}
	}

	// Decimal
	intStart := p
	for p < len(l.runes) && ((l.runes[p] >= '0' && l.runes[p] <= '9') || l.runes[p] == '_') { p++ }
	intText := string(l.runes[intStart:p])
	if err := validateUnderscores(intText, sl, sc, so); err != nil { return nil, err }
	intCleaned := strings.ReplaceAll(intText, "_", "")
	if intCleaned == "" { return nil, withPos(newErr(ErrLex, "number has no digits", "WEFT_SYNTAX_INT"), sl, sc, so) }

	isFloat := false
	fracCleaned := ""
	if p < len(l.runes) && l.runes[p] == '.' {
		if p+1 >= len(l.runes) || l.runes[p+1] < '0' || l.runes[p+1] > '9' {
			return nil, withPos(newErr(ErrLex, "float literal must have digits after '.'", "WEFT_SYNTAX_FLOAT_DOT"), sl, sc, so)
		}
		isFloat = true
		p++
		fracStart := p
		for p < len(l.runes) && ((l.runes[p] >= '0' && l.runes[p] <= '9') || l.runes[p] == '_') { p++ }
		fracText := string(l.runes[fracStart:p])
		if err := validateUnderscores(fracText, sl, sc, so); err != nil { return nil, err }
		fracCleaned = strings.ReplaceAll(fracText, "_", "")
	}

	expText := ""
	expSign := ""
	if p < len(l.runes) && (l.runes[p] == 'e' || l.runes[p] == 'E') {
		isFloat = true
		p++
		if p < len(l.runes) && (l.runes[p] == '+' || l.runes[p] == '-') {
			expSign = string(l.runes[p]); p++
		}
		expStart := p
		for p < len(l.runes) && ((l.runes[p] >= '0' && l.runes[p] <= '9') || l.runes[p] == '_') { p++ }
		expRaw := string(l.runes[expStart:p])
		if err := validateUnderscores(expRaw, sl, sc, so); err != nil { return nil, err }
		expText = strings.ReplaceAll(expRaw, "_", "")
		if expText == "" { return nil, withPos(newErr(ErrLex, "exponent has no digits", "WEFT_SYNTAX_FLOAT_EXP"), sl, sc, so) }
		if err := l.checkIntDigits(expText, sl, sc, so); err != nil { return nil, err }
	}

	suffix := rune(0)
	if p < len(l.runes) && (l.runes[p] == 'n' || l.runes[p] == 'm') {
		suffix = l.runes[p]; p++
		if p < len(l.runes) && isSymbolCont(l.runes[p]) {
			adv := p - l.idx
			for i := 0; i < adv; i++ { l.advance(1) }
			return nil, l.err(fmt.Sprintf("unexpected char after '%c' suffix", suffix), "WEFT_SYNTAX_NUMBER_SUFFIX")
		}
		if suffix == 'n' && isFloat {
			adv := p - l.idx
			for i := 0; i < adv; i++ { l.advance(1) }
			return nil, withPos(newErr(ErrLex, "'n' suffix is only valid on integers", "WEFT_SYNTAX_NUMBER_SUFFIX"), sl, sc, so)
		}
	} else if p < len(l.runes) && isSymbolCont(l.runes[p]) {
		adv := p - l.idx
		for i := 0; i < adv; i++ { l.advance(1) }
		return nil, l.err("unexpected char after number; only 'n' (BigInt) and 'm' (Decimal) suffixes supported", "WEFT_RESERVED_NUMBER_SUFFIX")
	}

	if err := l.checkIntDigits(intCleaned+fracCleaned, sl, sc, so); err != nil { return nil, err }

	adv := p - l.idx
	for i := 0; i < adv; i++ { l.advance(1) }

	if suffix == 'n' {
		bi := new(big.Int)
		bi.SetString(intCleaned, 10)
		if sign == "-" { bi.Neg(bi) }
		return &token{kind: tkBigInt, value: BigIntVal{Value: bi}, line: sl, column: sc, offset: so}, nil
	}
	if suffix == 'm' {
		// SPEC §7.2: '+' is not part of the canonical Decimal text.
		signNorm := sign
		if signNorm == "+" { signNorm = "" }
		txt := signNorm + intCleaned
		if fracCleaned != "" { txt += "." + fracCleaned }
		if expText != "" { txt += "e" + expSign + expText }
		return &token{kind: tkDecimal, value: Decimal{Text: txt}, line: sl, column: sc, offset: so}, nil
	}
	if isFloat {
		txt := sign + intCleaned
		if fracCleaned != "" { txt += "." + fracCleaned }
		if expText != "" { txt += "e" + expSign + expText }
		f, err := strconv.ParseFloat(txt, 64)
		if err != nil {
			// Overflow/underflow produces ErrRange; the clamped ±Inf (or 0 for underflow)
			// is still a valid IEEE 754 value, consistent with other impls.
			if ne, ok := err.(*strconv.NumError); !ok || ne.Err != strconv.ErrRange {
				return nil, withPos(newErr(ErrLex, "invalid float", "WEFT_SYNTAX_FLOAT"), sl, sc, so)
			}
		}
		return &token{kind: tkFloat, value: f, line: sl, column: sc, offset: so}, nil
	}
	bi := new(big.Int)
	bi.SetString(intCleaned, 10)
	if sign == "-" { bi.Neg(bi) }
	return &token{kind: tkInt, value: bi, line: sl, column: sc, offset: so}, nil
}

func (l *lexer) checkIntDigits(cleaned string, sl, sc, so int) error {
	return l.checkIntDigitsBase(cleaned, sl, sc, so, 10)
}

func (l *lexer) checkIntDigitsBase(cleaned string, sl, sc, so, base int) error {
	// SPEC §9.5: MaxIntDigits is a decimal-digit budget. For non-decimal
	// bases compare against the decimal-equivalent length —
	// ceil(len * log10(base)) is a tight upper bound on the base-10
	// string the formatter would emit.
	var decLen int
	switch base {
	case 10:
		decLen = len(cleaned)
	case 2:
		decLen = int(math.Ceil(float64(len(cleaned)) * 0.30102999566398114))
	case 8:
		decLen = int(math.Ceil(float64(len(cleaned)) * 0.9030899869919435))
	case 16:
		decLen = int(math.Ceil(float64(len(cleaned)) * 1.2041199826559248))
	default:
		decLen = len(cleaned)
	}
	if decLen > l.limits.MaxIntDigits {
		var msg string
		if base == 10 {
			msg = fmt.Sprintf("integer has %d digits, exceeds MaxIntDigits=%d", decLen, l.limits.MaxIntDigits)
		} else {
			msg = fmt.Sprintf("integer has %d decimal digits (%d base-%d digits), exceeds MaxIntDigits=%d",
				decLen, len(cleaned), base, l.limits.MaxIntDigits)
		}
		return withPos(newErr(ErrLimit, msg, "WEFT_LIMIT_INT"), sl, sc, so)
	}
	return nil
}

func validateUnderscores(text string, sl, sc, so int) error {
	if strings.Contains(text, "__") {
		return withPos(newErr(ErrLex, "consecutive underscores not permitted", "WEFT_SYNTAX_NUMBER_UNDERSCORE"), sl, sc, so)
	}
	if strings.HasPrefix(text, "_") || strings.HasSuffix(text, "_") {
		return withPos(newErr(ErrLex, "underscore cannot be first or last digit", "WEFT_SYNTAX_NUMBER_UNDERSCORE"), sl, sc, so)
	}
	return nil
}

/* --------------------------------------------------------------------------
 * Parser
 * -------------------------------------------------------------------------- */

type parserState struct {
	toks   []token
	pos    int
	limits Limits
	total  int
	depth  int
}

func Parse(text string) (Value, error) { return ParseWithLimits(text, DefaultLimits()) }

func ParseWithLimits(text string, limits Limits) (Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	l, err := newLexer(text, limits)
	if err != nil { return nil, err }
	toks, err := l.tokenize()
	if err != nil { return nil, err }
	p := &parserState{toks: toks, limits: limits}
	if p.peek() == nil {
		return nil, newErr(ErrSyntax, "empty document", "WEFT_SYNTAX_EMPTY")
	}
	v, err := p.parseValue()
	if err != nil { return nil, err }
	if t := p.peek(); t != nil {
		return nil, withPos(newErr(ErrSyntax, "unexpected content after top-level value", "WEFT_SYNTAX_LEFTOVER"), t.line, t.column, t.offset)
	}
	if err := checkTreeDepth(v, limits.MaxDepth); err != nil { return nil, err }
	return v, nil
}

func ParseStream(text string) ([]Value, error) {
	return ParseStreamWithLimits(text, DefaultLimits())
}

func ParseStreamWithLimits(text string, limits Limits) ([]Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	l, err := newLexer(text, limits)
	if err != nil { return nil, err }
	toks, err := l.tokenize()
	if err != nil { return nil, err }
	p := &parserState{toks: toks, limits: limits}
	var out []Value
	for p.peek() != nil {
		v, err := p.parseValue()
		if err != nil { return nil, err }
		if err := checkTreeDepth(v, limits.MaxDepth); err != nil { return nil, err }
		out = append(out, v)
	}
	return out, nil
}

// checkTreeDepth walks the parsed tree iteratively and refuses any value
// whose nesting depth exceeds maxDepth. Each list, map, sexpr, or Typed
// wrapper adds one level; scalars do not. Closes the hole where a chain
// of ::T annotations on the way out of a container adds structural depth
// not tracked by the in-parser counter.
func checkTreeDepth(root Value, maxDepth int) error {
	type frame struct { v Value; d int }
	stack := []frame{{root, 0}}
	for len(stack) > 0 {
		f := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		switch n := f.v.(type) {
		case []Value:
			nd := f.d + 1
			if nd > maxDepth {
				return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", nd, maxDepth), "WEFT_LIMIT_DEPTH")
			}
			for _, it := range n { stack = append(stack, frame{it, nd}) }
		case *MapOrdered:
			nd := f.d + 1
			if nd > maxDepth {
				return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", nd, maxDepth), "WEFT_LIMIT_DEPTH")
			}
			for _, k := range n.Keys { stack = append(stack, frame{n.Items[k], nd}) }
		case Sexpr:
			nd := f.d + 1
			if nd > maxDepth {
				return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", nd, maxDepth), "WEFT_LIMIT_DEPTH")
			}
			for _, it := range n.Items { stack = append(stack, frame{it, nd}) }
		case Typed:
			// Unroll chained ::T annotations in a tight loop. Each
			// wrapper's type expression may hold nested data
			// (``(T [[[...]]])``), so when the type is a Sexpr its items
			// count against MaxDepth too — a leaf Symbol type does not.
			dd := f.d
			var cur Value = n
			for {
				t, ok := cur.(Typed)
				if !ok { break }
				dd++
				if dd > maxDepth {
					return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", dd, maxDepth), "WEFT_LIMIT_DEPTH")
				}
				if sx, ok := t.Type.(Sexpr); ok {
					nd := dd + 1
					if nd > maxDepth {
						return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", nd, maxDepth), "WEFT_LIMIT_DEPTH")
					}
					for _, it := range sx.Items { stack = append(stack, frame{it, nd}) }
				}
				cur = t.Value
			}
			stack = append(stack, frame{cur, dd})
		}
		// scalars don't contribute to depth
	}
	return nil
}

func (p *parserState) peek() *token {
	if p.pos < len(p.toks) { return &p.toks[p.pos] }
	return nil
}
func (p *parserState) advance() *token { t := &p.toks[p.pos]; p.pos++; return t }

func (p *parserState) bumpTotal() error {
	p.total++
	if p.total > p.limits.MaxTotalItems {
		return newErr(ErrLimit, fmt.Sprintf("exceeds MaxTotalItems=%d", p.limits.MaxTotalItems), "WEFT_LIMIT_TOTAL")
	}
	return nil
}
func (p *parserState) enter() error {
	p.depth++
	if p.depth > p.limits.MaxDepth {
		return newErr(ErrLimit, fmt.Sprintf("depth exceeds MaxDepth=%d", p.limits.MaxDepth), "WEFT_LIMIT_DEPTH")
	}
	return nil
}
func (p *parserState) leave() { p.depth-- }

func (p *parserState) parseValue() (Value, error) {
	tok := p.peek()
	if tok == nil { return nil, newErr(ErrSyntax, "unexpected end", "WEFT_SYNTAX_EOF") }
	if err := p.bumpTotal(); err != nil { return nil, err }
	var v Value
	switch tok.kind {
	case tkInt: p.advance(); v = tok.value.(*big.Int)
	case tkBigInt: p.advance(); v = tok.value.(BigIntVal)
	case tkFloat: p.advance(); v = tok.value.(float64)
	case tkDecimal: p.advance(); v = tok.value.(Decimal)
	case tkString: p.advance(); v = tok.value.(string)
	case tkBinary: p.advance(); v = tok.value.(Binary)
	case tkDateTime: p.advance(); v = tok.value.(Date)
	case tkBool: p.advance(); v = tok.value.(bool)
	case tkNil: p.advance(); v = nil
	case tkNan: p.advance(); v = nan64()
	case tkInf: p.advance(); v = pinf()
	case tkNegInf: p.advance(); v = ninf()
	case tkSymbol: p.advance(); v = Symbol(tok.value.(string))
	case tkKeyword: p.advance(); v = Keyword(tok.value.(string))
	case tkLParen:
		s, err := p.parseSexpr(tok)
		if err != nil { return nil, err }
		v = s
	case tkLBrack:
		xs, err := p.parseList(tok)
		if err != nil { return nil, err }
		v = xs
	case tkLBrace:
		m, err := p.parseMap(tok)
		if err != nil { return nil, err }
		v = m
	default:
		return nil, withPos(newErr(ErrSyntax, "unexpected token", "WEFT_SYNTAX"), tok.line, tok.column, tok.offset)
	}
	// Type annotations: each ``::T`` wraps the value in a Typed node, so
	// each one adds a level of structural depth. Count every annotation
	// against max_depth so a long chain like ``0::T::T::T::…`` cannot
	// bypass the limit.
	annotCount := 0
	for p.peek() != nil && p.peek().kind == tkColonColon {
		p.advance()
		if err := p.enter(); err != nil {
			for i := 0; i < annotCount; i++ { p.leave() }
			return nil, err
		}
		annotCount++
		te, err := p.parseTypeExpr()
		if err != nil {
			for i := 0; i < annotCount; i++ { p.leave() }
			return nil, err
		}
		v = Typed{Value: v, Type: te}
		if err := p.bumpTotal(); err != nil {
			for i := 0; i < annotCount; i++ { p.leave() }
			return nil, err
		}
	}
	for i := 0; i < annotCount; i++ { p.leave() }
	return v, nil
}

func (p *parserState) parseSexpr(open *token) (Sexpr, error) {
	p.advance()
	if err := p.enter(); err != nil { return Sexpr{}, err }
	defer p.leave()
	tok := p.peek()
	if tok == nil {
		return Sexpr{}, withPos(newErr(ErrSyntax, "unterminated '('", "WEFT_SYNTAX_UNCLOSED"), open.line, open.column, open.offset)
	}
	if tok.kind != tkSymbol {
		return Sexpr{}, withPos(newErr(ErrSyntax, "sexpr tag must be a symbol", "WEFT_SYNTAX_SEXPR_TAG"), tok.line, tok.column, tok.offset)
	}
	p.advance()
	tag := Symbol(tok.value.(string))
	var items []Value
	for {
		t := p.peek()
		if t == nil {
			return Sexpr{}, withPos(newErr(ErrSyntax, "unterminated '('", "WEFT_SYNTAX_UNCLOSED"), open.line, open.column, open.offset)
		}
		if t.kind == tkRParen { p.advance(); break }
		v, err := p.parseValue()
		if err != nil { return Sexpr{}, err }
		items = append(items, v)
		if len(items) > p.limits.MaxItemsPerColl {
			return Sexpr{}, newErr(ErrLimit, "sexpr too many items", "WEFT_LIMIT_ITEMS")
		}
	}
	return Sexpr{Tag: tag, Items: items}, nil
}

func (p *parserState) parseList(open *token) ([]Value, error) {
	p.advance()
	if err := p.enter(); err != nil { return nil, err }
	defer p.leave()
	var items []Value
	for {
		t := p.peek()
		if t == nil {
			return nil, withPos(newErr(ErrSyntax, "unterminated '['", "WEFT_SYNTAX_UNCLOSED"), open.line, open.column, open.offset)
		}
		if t.kind == tkRBrack { p.advance(); break }
		v, err := p.parseValue()
		if err != nil { return nil, err }
		items = append(items, v)
		if len(items) > p.limits.MaxItemsPerColl {
			return nil, newErr(ErrLimit, "list too many items", "WEFT_LIMIT_ITEMS")
		}
	}
	if items == nil { items = []Value{} }
	return items, nil
}

func (p *parserState) parseMap(open *token) (*MapOrdered, error) {
	p.advance()
	if err := p.enter(); err != nil { return nil, err }
	defer p.leave()
	m := NewMap()
	count := 0
	for {
		t := p.peek()
		if t == nil {
			return nil, withPos(newErr(ErrSyntax, "unterminated '{'", "WEFT_SYNTAX_UNCLOSED"), open.line, open.column, open.offset)
		}
		if t.kind == tkRBrace { p.advance(); break }
		if t.kind != tkKeyword {
			return nil, withPos(newErr(ErrSyntax, "map key must be a keyword", "WEFT_SYNTAX_MAP_KEY"), t.line, t.column, t.offset)
		}
		keyLine, keyCol, keyOff := t.line, t.column, t.offset
		key := t.value.(string)
		p.advance()
		if err := p.bumpTotal(); err != nil { return nil, err }
		v := p.peek()
		if v == nil || v.kind == tkRBrace {
			return nil, withPos(newErr(ErrSyntax, fmt.Sprintf("map key :%s has no value", key), "WEFT_SYNTAX_MAP_VALUE"), keyLine, keyCol, keyOff)
		}
		val, err := p.parseValue()
		if err != nil { return nil, err }
		if !m.Set(key, val) {
			return nil, withPos(newErr(ErrSyntax, fmt.Sprintf("duplicate map key :%s", key), "WEFT_SYNTAX_MAP_DUPKEY"), keyLine, keyCol, keyOff)
		}
		count++
		if count > p.limits.MaxItemsPerColl {
			return nil, newErr(ErrLimit, "map too many items", "WEFT_LIMIT_ITEMS")
		}
	}
	return m, nil
}

func (p *parserState) parseTypeExpr() (TypeExpr, error) {
	tok := p.peek()
	if tok == nil { return nil, newErr(ErrSyntax, "type expr expected after '::'", "WEFT_SYNTAX_TYPE") }
	switch tok.kind {
	case tkSymbol:
		p.advance()
		return Symbol(tok.value.(string)), nil
	case tkLParen:
		s, err := p.parseSexpr(tok)
		if err != nil { return nil, err }
		return s, nil
	}
	return nil, withPos(newErr(ErrSyntax, "type expression must be a symbol or sexpr", "WEFT_SYNTAX_TYPE"), tok.line, tok.column, tok.offset)
}

/* --------------------------------------------------------------------------
 * Formatter
 * -------------------------------------------------------------------------- */

type FormatOptions struct {
	MaxWidth        int
	Indent          int
	SortKeys        bool
	StrictCanonical bool
}

func DefaultFormatOptions() FormatOptions {
	return FormatOptions{MaxWidth: 80, Indent: 2}
}

func FormatValue(v Value, opts FormatOptions) string {
	if opts.MaxWidth == 0 { opts.MaxWidth = 80 }
	if opts.Indent == 0 { opts.Indent = 2 }
	if opts.StrictCanonical {
		// SPEC §7.3: strict canonical is a single-line, unbounded-width
		// layout. Setting MaxWidth to an effectively infinite value
		// makes formatRec take the inline branch rather than falling
		// into format*Multi.
		opts.SortKeys = true
		opts.MaxWidth = 1 << 30
	}
	s := formatRec(v, 0, opts)
	if !strings.HasSuffix(s, "\n") { s += "\n" }
	return s
}

func FormatInline(v Value, opts FormatOptions) (string, error) {
	if opts.MaxWidth == 0 { opts.MaxWidth = 80 }
	// Mirror FormatValue: strict_canonical forces sort_keys so that a map
	// rendered inline matches the same map rendered by FormatValue.
	if opts.StrictCanonical { opts.SortKeys = true }
	return inlineRec(v, opts)
}

var errMultilineNeeded = errors.New("multiline needed")

func formatRec(v Value, depth int, opts FormatOptions) string {
	// Peel off any Typed wrapper(s) iteratively — a deep type-annotation
	// chain cannot blow the stack. Innermost type appears first in
	// source, outermost last; peeling from outside gives the chain in
	// reverse order, so we reverse when joining.
	// Type expressions render in strict single-line mode so a newline
	// string inside the type doesn't surface as errMultilineNeeded and
	// crash the whole value.
	strictOpts := opts; strictOpts.StrictCanonical = true
	cur := v
	var typeChain []string
	for {
		t, ok := cur.(Typed)
		if !ok { break }
		ty, err := inlineTypeExpr(t.Type, strictOpts)
		if err != nil { ty = "<unformatable>" }
		typeChain = append(typeChain, ty)
		cur = t.Value
	}
	typeSuffix := ""
	if n := len(typeChain); n > 0 {
		// reverse in place
		for i, j := 0, n-1; i < j; i, j = i+1, j-1 { typeChain[i], typeChain[j] = typeChain[j], typeChain[i] }
		typeSuffix = "::" + strings.Join(typeChain, "::")
	}
	v = cur

	if s, err := inlineRec(v, opts); err == nil {
		// SPEC §7.2: the 80-column soft-wrap counts Unicode CODE POINTS,
		// not UTF-8 bytes (which over-count CJK three-fold).
		if depth*opts.Indent+utf8.RuneCountInString(s)+utf8.RuneCountInString(typeSuffix) <= opts.MaxWidth && !strings.Contains(s, "\n") {
			return s + typeSuffix
		}
	}
	switch val := v.(type) {
	case []Value: return formatListMulti(val, depth, opts) + typeSuffix
	case *MapOrdered: return formatMapMulti(val, depth, opts) + typeSuffix
	case Sexpr: return formatSexprMulti(val, depth, opts) + typeSuffix
	}
	// SPEC §7.2: strings — including those containing newlines — always
	// use one-line dquote-escape form (never triple-quoted), to avoid
	// W4-04 silent re-indentation data corruption.
	s, _ := inlineRec(v, opts)
	return s + typeSuffix
}

func inlineRec(v Value, opts FormatOptions) (string, error) {
	switch val := v.(type) {
	case nil: return "nil", nil
	case bool: if val { return "true", nil } else { return "false", nil }
	case *big.Int: return val.String(), nil
	case BigIntVal: return val.Value.String() + "n", nil
	case float64: return formatFloat(val), nil
	case Decimal:
		// Preserve the exact source text — Decimal is significant-digit
		// aware, so synthesising a trailing ``.0`` would change the
		// represented precision (``1m`` and ``1.0m`` are not the same).
		// SPEC §7.2: strip leading '+' and lowercase 'E' marker.
		text := val.Text
		if strings.HasPrefix(text, "+") { text = text[1:] }
		text = strings.ReplaceAll(text, "E", "e")
		return text + "m", nil
	case string: return formatStringDquote(val, opts)
	case Binary:
		// SPEC §5.8 and §7.2: canonical form always emits base64,
		// regardless of input encoding. Equality is defined on bytes
		// (§5.16), so the two spellings are the same abstract value.
		_ = val.Encoding
		return `b"base64:` + base64.StdEncoding.EncodeToString(val.Data) + `"`, nil
	case Date: return formatDate(val), nil
	case Symbol: return string(val), nil
	case Keyword: return ":" + string(val), nil
	case Typed:
		// Peel iteratively; join innermost-first (reverse of the
		// outside-in peel order) to preserve source order. Type
		// expressions use strict single-line mode so a newline string
		// inside the type doesn't throw errMultilineNeeded.
		strictOpts := opts; strictOpts.StrictCanonical = true
		var chain []string
		cur := Value(val)
		for {
			t, ok := cur.(Typed)
			if !ok { break }
			ty, err := inlineTypeExpr(t.Type, strictOpts)
			if err != nil { return "", err }
			chain = append(chain, ty)
			cur = t.Value
		}
		base, err := inlineRec(cur, opts)
		if err != nil { return "", err }
		// reverse chain
		for i, j := 0, len(chain)-1; i < j; i, j = i+1, j-1 { chain[i], chain[j] = chain[j], chain[i] }
		return base + "::" + strings.Join(chain, "::"), nil
	case []Value:
		parts := make([]string, len(val))
		for i, it := range val {
			s, err := inlineRec(it, opts)
			if err != nil { return "", err }
			parts[i] = s
		}
		return "[" + strings.Join(parts, " ") + "]", nil
	case *MapOrdered:
		keys := val.Keys
		if opts.SortKeys {
			keys = append([]string{}, keys...)
			sort.Strings(keys)
		}
		parts := make([]string, len(keys))
		for i, k := range keys {
			s, err := inlineRec(val.Items[k], opts)
			if err != nil { return "", err }
			parts[i] = ":" + k + " " + s
		}
		return "{" + strings.Join(parts, " ") + "}", nil
	case Sexpr:
		parts := make([]string, 0, len(val.Items)+1)
		parts = append(parts, string(val.Tag))
		for _, it := range val.Items {
			s, err := inlineRec(it, opts)
			if err != nil { return "", err }
			parts = append(parts, s)
		}
		return "(" + strings.Join(parts, " ") + ")", nil
	}
	return "", fmt.Errorf("cannot format %T", v)
}

func inlineTypeExpr(t TypeExpr, opts FormatOptions) (string, error) {
	switch te := t.(type) {
	case Symbol: return string(te), nil
	case Sexpr:
		parts := []string{string(te.Tag)}
		for _, it := range te.Items {
			s, err := inlineRec(it, opts)
			if err != nil { return "", err }
			parts = append(parts, s)
		}
		return "(" + strings.Join(parts, " ") + ")", nil
	}
	return "", fmt.Errorf("bad type expr")
}

func formatFloat(f float64) string {
	if isNaN(f) { return "nan" }
	if isPosInf(f) { return "inf" }
	if isNegInf(f) { return "-inf" }
	// Preserve the sign of zero so ``-0.0`` survives (SPEC §5.3).
	// ``math.Signbit`` reports the sign bit even for 0.
	if f == 0 {
		if math.Signbit(f) { return "-0.0" }
		return "0.0"
	}
	// Match Python's repr: use fixed-point for abs(f) in [1e-4, 1e16),
	// otherwise scientific. Within each form, use shortest round-trip.
	abs := math.Abs(f)
	var s string
	if abs >= 1e-4 && abs < 1e16 {
		s = strconv.FormatFloat(f, 'f', -1, 64)
	} else {
		s = strconv.FormatFloat(f, 'e', -1, 64)
	}
	if !strings.ContainsAny(s, ".eE") { s += ".0" }
	return s
}

// SPEC §7.3: in strict canonical, C0, DEL, C1, BOM, BMP noncharacters,
// and bidirectional override/isolate controls MUST be escaped.
func strictMustEscape(cp int) bool {
	return cp <= 0x1F ||
		cp == 0x7F ||
		(cp >= 0x80 && cp <= 0x9F) ||
		cp == 0xFEFF || cp == 0xFFFE || cp == 0xFFFF ||
		(cp >= 0x202A && cp <= 0x202E) ||
		(cp >= 0x2066 && cp <= 0x2069)
}

func formatStringDquote(s string, opts FormatOptions) (string, error) {
	// SPEC §7.2: a string containing U+000A is emitted as the one-line
	// dquote-escape form (never triple-quoted). Triple-quoted strings
	// inside a soft-wrapped compound get re-indented by every known
	// formatter, which silently inserts leading spaces into the string
	// body (W4-04 data corruption). The one-line form removes that
	// attack surface entirely.
	var sb strings.Builder
	sb.WriteByte('"')
	for _, r := range s {
		cp := int(r)
		switch {
		case r == '"': sb.WriteString(`\"`)
		case r == '\\': sb.WriteString(`\\`)
		case r == '\n': sb.WriteString(`\n`)
		case r == '\t': sb.WriteString(`\t`)
		case r == '\r': sb.WriteString(`\r`)
		case r == 0: sb.WriteString(`\0`)
		case opts.StrictCanonical && strictMustEscape(cp): fmt.Fprintf(&sb, `\u{%X}`, cp)
		case cp < 0x20: fmt.Fprintf(&sb, `\u{%X}`, cp)
		// BOM and the two BMP noncharacters are always escaped — the
		// lexer rejects them literal, so emitting them raw would break
		// round-trip.
		case cp == 0xFEFF || cp == 0xFFFE || cp == 0xFFFF: fmt.Fprintf(&sb, `\u{%X}`, cp)
		default: sb.WriteRune(r)
		}
	}
	sb.WriteByte('"')
	return sb.String(), nil
}

func formatStringTriple(s string, opts FormatOptions) string {
	// Fall back to dquote when the triple form would be ambiguous:
	//   - contains ``"""`` (would prematurely close)
	//   - ends in ``"`` (would merge with closing ``"""`` into ``""""``)
	//   - strict canonical mode is requested
	if strings.Contains(s, `"""`) || strings.HasSuffix(s, `"`) || opts.StrictCanonical {
		opts2 := opts
		opts2.StrictCanonical = true
		result, _ := formatStringDquote(s, opts2)
		return result
	}
	var sb strings.Builder
	sb.WriteString(`"""`)
	for _, r := range s {
		cp := int(r)
		switch {
		case r == '\n' || r == '\t': sb.WriteRune(r)
		case r == '\\': sb.WriteString(`\\`)
		case cp < 0x20 && r != '\r': fmt.Fprintf(&sb, `\u{%X}`, cp)
		default: sb.WriteRune(r)
		}
	}
	sb.WriteString(`"""`)
	return sb.String()
}

func formatDate(d Date) string {
	s := fmt.Sprintf("#%04d-%02d-%02d", d.Year, d.Month, d.Day)
	if !d.HasTime { return s }
	s += fmt.Sprintf("T%02d:%02d:%02d", d.Hour, d.Minute, d.Second)
	if d.Nano != 0 {
		frac := fmt.Sprintf("%09d", d.Nano)
		frac = strings.TrimRight(frac, "0")
		s += "." + frac
	}
	if d.TZOffsetMinutes == nil { return s }
	off := *d.TZOffsetMinutes
	if off == 0 { s += "Z"; return s }
	sign := "+"
	if off < 0 { sign = "-"; off = -off }
	s += fmt.Sprintf("%s%02d:%02d", sign, off/60, off%60)
	return s
}

func formatListMulti(items []Value, depth int, opts FormatOptions) string {
	if len(items) == 0 { return "[]" }
	padInner := strings.Repeat(" ", (depth+1)*opts.Indent)
	padClose := strings.Repeat(" ", depth*opts.Indent)
	lines := make([]string, len(items))
	for i, it := range items {
		lines[i] = padInner + formatRec(it, depth+1, opts)
	}
	return "[\n" + strings.Join(lines, "\n") + "\n" + padClose + "]"
}

func formatMapMulti(m *MapOrdered, depth int, opts FormatOptions) string {
	if m.Len() == 0 { return "{}" }
	keys := m.Keys
	if opts.SortKeys { keys = append([]string{}, keys...); sort.Strings(keys) }
	padInner := strings.Repeat(" ", (depth+1)*opts.Indent)
	padClose := strings.Repeat(" ", depth*opts.Indent)
	var lines []string
	for _, k := range keys {
		v := m.Items[k]
		keyS := ":" + k
		if vi, err := inlineRec(v, opts); err == nil && len(padInner)+len(keyS)+1+len(vi) <= opts.MaxWidth && !strings.Contains(vi, "\n") {
			lines = append(lines, padInner+keyS+" "+vi)
			continue
		}
		vBlock := formatRec(v, depth+1, opts)
		if strings.Contains(vBlock, "\n") {
			deeper := strings.Repeat(" ", (depth+2)*opts.Indent)
			lines = append(lines, padInner+keyS)
			for _, ln := range strings.Split(vBlock, "\n") {
				if ln == "" { lines = append(lines, "") } else { lines = append(lines, deeper+ln) }
			}
		} else {
			lines = append(lines, padInner+keyS+" "+vBlock)
		}
	}
	return "{\n" + strings.Join(lines, "\n") + "\n" + padClose + "}"
}

func formatSexprMulti(s Sexpr, depth int, opts FormatOptions) string {
	if len(s.Items) == 0 { return "(" + string(s.Tag) + ")" }
	padInner := strings.Repeat(" ", (depth+1)*opts.Indent)
	padClose := strings.Repeat(" ", depth*opts.Indent)
	lines := make([]string, len(s.Items))
	for i, it := range s.Items {
		lines[i] = padInner + formatRec(it, depth+1, opts)
	}
	return "(" + string(s.Tag) + "\n" + strings.Join(lines, "\n") + "\n" + padClose + ")"
}

/* --------------------------------------------------------------------------
 * JSON interop
 * -------------------------------------------------------------------------- */

var safeIntMax = big.NewInt((1 << 53) - 1)
var safeIntMin = new(big.Int).Neg(safeIntMax)

func ToJSON(v Value) any {
	switch val := v.(type) {
	case nil: return nil
	case bool: return val
	case *big.Int:
		if val.Cmp(safeIntMin) >= 0 && val.Cmp(safeIntMax) <= 0 {
			return val.Int64()
		}
		return val.String()
	case BigIntVal: return val.Value.String()
	case float64:
		if isNaN(val) { return "nan" }
		if isPosInf(val) { return "inf" }
		if isNegInf(val) { return "-inf" }
		return val
	case Decimal: return val.Text
	case string: return val
	case Binary:
		if val.Encoding == Base64Enc {
			return "base64:" + base64.StdEncoding.EncodeToString(val.Data)
		}
		return "hex:" + hex.EncodeToString(val.Data)
	case Date: return dateCanonical(val)
	case Symbol: return "#sym:" + string(val)
	case Keyword: return "#kw:" + string(val)
	case []Value:
		out := make([]any, len(val))
		for i, x := range val { out[i] = ToJSON(x) }
		return out
	case *MapOrdered:
		// Use ordered emission via json.Encoder trick: emit a json.RawMessage array ordering.
		// But standard json emits unordered map; produce an ordered marshaler.
		return orderedMap(val)
	case Sexpr:
		arr := make([]any, 0, len(val.Items)+1)
		arr = append(arr, string(val.Tag))
		for _, it := range val.Items { arr = append(arr, ToJSON(it)) }
		return map[string]any{"$sexpr": arr}
	case Typed:
		var typeJ any
		switch te := val.Type.(type) {
		case Symbol: typeJ = "#sym:" + string(te)
		case Sexpr:
			arr := make([]any, 0, len(te.Items)+1)
			arr = append(arr, string(te.Tag))
			for _, it := range te.Items { arr = append(arr, ToJSON(it)) }
			typeJ = map[string]any{"$sexpr": arr}
		}
		return map[string]any{"$typed": map[string]any{"v": ToJSON(val.Value), "t": typeJ}}
	}
	return nil
}

// orderedMap is a wrapper that marshals to JSON preserving insertion order.
type orderedMapT struct{ m *MapOrdered }

func orderedMap(m *MapOrdered) *orderedMapT { return &orderedMapT{m: m} }

func (o *orderedMapT) MarshalJSON() ([]byte, error) {
	var sb strings.Builder
	sb.WriteByte('{')
	for i, k := range o.m.Keys {
		if i > 0 { sb.WriteByte(',') }
		kb, _ := json.Marshal(k)
		sb.Write(kb)
		sb.WriteByte(':')
		vb, err := json.Marshal(ToJSON(o.m.Items[k]))
		if err != nil { return nil, err }
		sb.Write(vb)
	}
	sb.WriteByte('}')
	return []byte(sb.String()), nil
}

// W4-10: lossless map output must preserve insertion order. Go's
// ``map[string]any`` is unordered and ``encoding/json`` sorts keys
// alphabetically, which would silently reorder ``{:b 1 :a 2}`` to
// ``{"a":2,"b":1}`` on the JSON side and corrupt the round-trip. This
// adapter writes keys in the MapOrdered's insertion order.
type orderedMapLosslessT struct{ m *MapOrdered }

func orderedMapLossless(m *MapOrdered) *orderedMapLosslessT { return &orderedMapLosslessT{m: m} }

func (o *orderedMapLosslessT) MarshalJSON() ([]byte, error) {
	var sb strings.Builder
	sb.WriteByte('{')
	for i, k := range o.m.Keys {
		if i > 0 { sb.WriteByte(',') }
		kb, _ := json.Marshal(k)
		sb.Write(kb)
		sb.WriteByte(':')
		vb, err := json.Marshal(ToJSONLossless(o.m.Items[k]))
		if err != nil { return nil, err }
		sb.Write(vb)
	}
	sb.WriteByte('}')
	return []byte(sb.String()), nil
}

func dateCanonical(d Date) string {
	s := fmt.Sprintf("%04d-%02d-%02d", d.Year, d.Month, d.Day)
	if !d.HasTime { return s }
	s += fmt.Sprintf("T%02d:%02d:%02d", d.Hour, d.Minute, d.Second)
	if d.Nano != 0 {
		frac := fmt.Sprintf("%09d", d.Nano)
		frac = strings.TrimRight(frac, "0")
		s += "." + frac
	}
	if d.TZOffsetMinutes == nil { return s }
	off := *d.TZOffsetMinutes
	if off == 0 { s += "Z"; return s }
	sign := "+"
	if off < 0 { sign = "-"; off = -off }
	s += fmt.Sprintf("%s%02d:%02d", sign, off/60, off%60)
	return s
}

// MarshalJSON marshals a Weft value to canonical JSON.
func DumpJSON(v Value) ([]byte, error) {
	return json.Marshal(ToJSON(v))
}

// DumpJSONLossless is the write-side counterpart of LoadsJSONLossless:
// LoadsJSONLossless(string(DumpJSONLossless(v))) round-trips every value.
// (DumpJSON writes the lossy §11.3 mapping and pairs with LoadsJSON.)
func DumpJSONLossless(v Value) ([]byte, error) {
	return json.Marshal(ToJSONLossless(v))
}

// FromJSON converts a JSON value (as unmarshaled by encoding/json) to Weft
// via the LOSSY mapping (SPEC §11.3). Prefer LoadsJSON for wire input: it
// preserves the lexical int/float distinction (JSON ``1`` → Integer,
// ``1.0`` → Float), rejects duplicate keys, and keeps object key order —
// a plain ``json.Unmarshal`` tree cannot witness any of those.
func FromJSON(j any) (Value, error) {
	return fromJSON(j, true, 0, &convCtx{limits: DefaultLimits()})
}

// FromJSONWithLimits is FromJSON with explicit SPEC §9 limits — the
// value-level entry point enforces the same scalar and structural
// limits as the text loaders.
func FromJSONWithLimits(j any, limits Limits) (Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	return fromJSON(j, true, 0, &convCtx{limits: limits})
}

// LoadsJSON parses JSON text via the LOSSY mapping (SPEC §11.3) with the
// token-level reader: lexical int/float distinction, duplicate-key
// rejection (WEFT_SYNTAX_JSON_DUPKEY), insertion-order preservation, and
// the §9 limits.
func LoadsJSON(text string) (Value, error) {
	return LoadsJSONWithLimits(text, DefaultLimits())
}

func LoadsJSONWithLimits(text string, limits Limits) (Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	if len(text) > limits.MaxSizeBytes {
		return nil, newErr(ErrLimit, fmt.Sprintf("JSON input %d bytes exceeds MaxSizeBytes=%d", len(text), limits.MaxSizeBytes), "WEFT_LIMIT_SIZE")
	}
	if err := scanJSONSurrogates(text); err != nil { return nil, err }
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	raw, err := decodeAnyOrdered(dec, 0, limits)
	if err != nil {
		if we, ok := err.(*Error); ok { return nil, we }
		return nil, newErr(ErrSyntax, fmt.Sprintf("JSON decode failed: %v", err), "WEFT_SYNTAX_JSON")
	}
	// EOF check — mirror the lossless loader: a clean EOF is the only
	// acceptable outcome. A further token ("1 x") and a malformed tail
	// ("1 \"unterminated") are both syntax errors, never silently dropped.
	if _, err := dec.Token(); err == nil {
		return nil, newErr(ErrSyntax, "unexpected trailing content in JSON", "WEFT_SYNTAX_JSON")
	} else if err.Error() != "EOF" {
		return nil, newErr(ErrSyntax, fmt.Sprintf("JSON decode failed: %v", err), "WEFT_SYNTAX_JSON")
	}
	return fromJSON(raw, true, 0, &convCtx{limits: limits})
}

func fromJSON(j any, strict bool, depth int, ctx *convCtx) (Value, error) {
	if err := ctx.bump(); err != nil { return nil, err }
	switch val := j.(type) {
	case nil: return nil, nil
	case bool: return val, nil
	case json.Number:
		// SPEC §11.3: the int/float distinction is lexical — a token with
		// '.', 'e', or 'E' is a Float, otherwise an Integer.
		sv := string(val)
		if strings.ContainsAny(sv, ".eE") {
			f, err := strconv.ParseFloat(sv, 64)
			if err != nil {
				if ne, ok := err.(*strconv.NumError); !ok || ne.Err != strconv.ErrRange {
					return nil, newErr(ErrType, fmt.Sprintf("invalid float token %q", sv), "WEFT_TYPE_FLOAT")
				}
			}
			return f, nil
		}
		if n := len(strings.TrimPrefix(sv, "-")); n > ctx.limits.MaxIntDigits {
			return nil, newErr(ErrLimit, fmt.Sprintf("JSON integer has %d digits, exceeds MaxIntDigits=%d", n, ctx.limits.MaxIntDigits), "WEFT_LIMIT_INT")
		}
		b, ok := new(big.Int).SetString(sv, 10)
		if !ok {
			return nil, newErr(ErrType, fmt.Sprintf("invalid integer token %q", sv), "WEFT_TYPE_INT")
		}
		return b, nil
	case float64:
		// A tree from plain json.Unmarshal: the lexical shape is already
		// lost. Map to Float — §11.3's integer rule needs the token text,
		// which only the LoadsJSON path has.
		return val, nil
	case string:
		// Value-level entry points receive caller-built strings, which a
		// Go program can fill with invalid UTF-8 (e.g. WTF-8 surrogate
		// bytes). A Weft String is a Unicode scalar sequence (§5.7).
		if !utf8.ValidString(val) {
			return nil, newErr(ErrSyntax, "JSON string is not valid UTF-8 (Weft strings are Unicode scalar sequences)", "WEFT_SYNTAX_JSON")
		}
		if len(val) > ctx.limits.MaxStringBytes {
			return nil, newErr(ErrLimit, fmt.Sprintf("string exceeds MaxStringBytes=%d", ctx.limits.MaxStringBytes), "WEFT_LIMIT_STRING")
		}
		return val, nil
	case []any:
		if err := ctx.depth(depth + 1); err != nil { return nil, err }
		if err := ctx.coll(len(val)); err != nil { return nil, err }
		out := make([]Value, len(val))
		for i, x := range val {
			v, err := fromJSON(x, strict, depth+1, ctx)
			if err != nil { return nil, err }
			out[i] = v
		}
		return out, nil
	case *MapOrdered:
		if err := ctx.depth(depth + 1); err != nil { return nil, err }
		if err := ctx.coll(len(val.Keys)); err != nil { return nil, err }
		m := NewMap()
		for _, k := range val.Keys {
			if strict && !isValidKeyword(k) {
				return nil, newErr(ErrType, fmt.Sprintf("JSON key %q is not a valid Weft keyword", k), "WEFT_TYPE_JSON_KEY")
			}
			v, err := fromJSON(val.Items[k], strict, depth+1, ctx)
			if err != nil { return nil, err }
			m.Set(k, v)
		}
		return m, nil
	case map[string]any:
		if err := ctx.depth(depth + 1); err != nil { return nil, err }
		if err := ctx.coll(len(val)); err != nil { return nil, err }
		m := NewMap()
		keys := make([]string, 0, len(val))
		for k := range val { keys = append(keys, k) }
		sort.Strings(keys)
		for _, k := range keys {
			if strict && !isValidKeyword(k) {
				return nil, newErr(ErrType, fmt.Sprintf("JSON key %q is not a valid Weft keyword", k), "WEFT_TYPE_JSON_KEY")
			}
			v, err := fromJSON(val[k], strict, depth+1, ctx)
			if err != nil { return nil, err }
			m.Set(k, v)
		}
		return m, nil
	}
	return nil, newErr(ErrType, fmt.Sprintf("unsupported JSON value: %T", j), "WEFT_TYPE_JSON")
}

// ToJSONLossless converts a Weft value to a fully-tagged JSON representation.
// Every Weft type is wrapped in a `$`-prefixed tagged object so that
// `FromJSONLossless(ToJSONLossless(x)) == x` for every valid Weft tree.
//
// Tag conventions (one tag per object):
//
//	{"$sym":  "name"}                               Symbol
//	{"$kw":   "name"}                               Keyword
//	{"$bigint": "..."}                              BigInt (decimal digits)
//	{"$int-big": "..."}                             *big.Int outside safe range
//	{"$dec":  "..."}                                Decimal (preserved text)
//	{"$str":  "..."}                                String (always wrapped)
//	{"$bin":  {"enc":"base64"|"hex","data":"..."}}  Binary
//	{"$dt":   "ISO-8601"}                           DateTime
//	{"$float": "nan"|"inf"|"-inf"}                  non-finite Float
//	{"$sexpr": [tag, items...]}                     Sexpr
//	{"$typed": {"v":..., "t":...}}                  Typed
//	{"$list":  [...]}                               List
//	{"$map":   {"k":v, ...}}                        Map
func ToJSONLossless(v Value) any {
	switch val := v.(type) {
	case nil:
		return nil
	case bool:
		return val
	case *big.Int:
		if val.Cmp(safeIntMin) >= 0 && val.Cmp(safeIntMax) <= 0 {
			// Return as json.Number so FromJSONLossless + round-trip
			// preserves the "safe integer" status without accidentally
			// routing through float64 (W4-11). encoding/json emits
			// json.Number verbatim.
			return json.Number(val.String())
		}
		return map[string]any{"$int-big": val.String()}
	case BigIntVal:
		return map[string]any{"$bigint": val.Value.String()}
	case float64:
		// Every float is tagged — a plain JSON number is reserved for
		// Weft Integer so ``1`` (Int) and ``1.0`` (Float) survive the
		// round trip as different types.
		if isNaN(val) {
			return map[string]any{"$float": "nan"}
		}
		if isPosInf(val) {
			return map[string]any{"$float": "inf"}
		}
		if isNegInf(val) {
			return map[string]any{"$float": "-inf"}
		}
		return map[string]any{"$float": floatCanonicalText(val)}
	case Decimal:
		// SPEC §7.2: canonicalise — strip leading '+', lowercase 'E'.
		text := val.Text
		if strings.HasPrefix(text, "+") { text = text[1:] }
		text = strings.ReplaceAll(text, "E", "e")
		return map[string]any{"$dec": text}
	case string:
		return map[string]any{"$str": val}
	case Binary:
		if val.Encoding == Base64Enc {
			return map[string]any{"$bin": map[string]any{"enc": "base64", "data": base64.StdEncoding.EncodeToString(val.Data)}}
		}
		return map[string]any{"$bin": map[string]any{"enc": "hex", "data": hex.EncodeToString(val.Data)}}
	case Date:
		return map[string]any{"$dt": dateCanonical(val)}
	case Symbol:
		return map[string]any{"$sym": string(val)}
	case Keyword:
		return map[string]any{"$kw": string(val)}
	case []Value:
		arr := make([]any, len(val))
		for i, x := range val {
			arr[i] = ToJSONLossless(x)
		}
		return map[string]any{"$list": arr}
	case *MapOrdered:
		// W4-10 / SPEC §11.2.2: lossless map output MUST preserve
		// insertion order. encoding/json sorts map keys alphabetically
		// so we wrap in orderedMapLosslessT which writes keys in the
		// MapOrdered's own insertion order.
		return map[string]any{"$map": orderedMapLossless(val)}
	case Sexpr:
		arr := make([]any, 0, len(val.Items)+1)
		arr = append(arr, string(val.Tag))
		for _, it := range val.Items {
			arr = append(arr, ToJSONLossless(it))
		}
		return map[string]any{"$sexpr": arr}
	case Typed:
		var typeJ any
		switch te := val.Type.(type) {
		case Symbol:
			typeJ = map[string]any{"$sym": string(te)}
		case Sexpr:
			arr := make([]any, 0, len(te.Items)+1)
			arr = append(arr, string(te.Tag))
			for _, it := range te.Items {
				arr = append(arr, ToJSONLossless(it))
			}
			typeJ = map[string]any{"$sexpr": arr}
		}
		return map[string]any{"$typed": map[string]any{"v": ToJSONLossless(val.Value), "t": typeJ}}
	}
	return nil
}

var losslessTags = map[string]struct{}{
	"$sym": {}, "$kw": {}, "$bigint": {}, "$int-big": {}, "$dec": {}, "$str": {},
	"$bin": {}, "$dt": {}, "$float": {}, "$sexpr": {}, "$typed": {}, "$list": {}, "$map": {},
}


// convCtx carries the SPEC §9 limit state through a JSON→Weft conversion.
type convCtx struct {
	limits Limits
	total  int
}

func (c *convCtx) bump() error {
	c.total++
	if c.total > c.limits.MaxTotalItems {
		return newErr(ErrLimit, fmt.Sprintf("document has more than MaxTotalItems=%d values", c.limits.MaxTotalItems), "WEFT_LIMIT_TOTAL")
	}
	return nil
}

func (c *convCtx) depth(d int) error {
	if d > c.limits.MaxDepth {
		return newErr(ErrLimit, fmt.Sprintf("tree depth %d exceeds MaxDepth=%d", d, c.limits.MaxDepth), "WEFT_LIMIT_DEPTH")
	}
	return nil
}

func (c *convCtx) coll(n int) error {
	if n > c.limits.MaxItemsPerColl {
		return newErr(ErrLimit, fmt.Sprintf("collection has %d items, exceeds MaxItemsPerColl=%d", n, c.limits.MaxItemsPerColl), "WEFT_LIMIT_ITEMS")
	}
	return nil
}

// FromJSONLossless is a strict inverse of ToJSONLossless. Bare strings,
// arrays, and multi-key objects are rejected: a round-trip through
// ToJSONLossless always uses the tagged form, so untagged inputs would
// break round-trip identity.
// safeIntMinJSON / safeIntMaxJSON — JSON interchange safe integer range
// (SPEC §11.2.1 rule 2): ±(2^53 - 1), the largest range of exact-integer
// values in IEEE 754 double. Integers outside this range MUST use
// {"$int-big": "..."}.
const safeIntMinJSONv = -((1 << 53) - 1)
const safeIntMaxJSONv = (1 << 53) - 1

// isStrictIntPayload matches SPEC §11.2's integer payload shape:
// ``-?[0-9]+``. ASCII digits, optional leading '-', nothing else.
func isStrictIntPayload(s string) bool {
	if s == "" { return false }
	start := 0
	if s[0] == '-' { start = 1 }
	if start == len(s) { return false }
	for i := start; i < len(s); i++ {
		c := s[i]
		if c < '0' || c > '9' { return false }
	}
	return true
}

// canonicalBase64Decode decodes `s` and verifies the RFC 4648 §3.5
// canonical check — non-zero trailing bits are rejected (SPEC §5.8).
func canonicalBase64Decode(s string) ([]byte, error) {
	bytes, err := base64.StdEncoding.DecodeString(s)
	if err != nil { return nil, err }
	re := base64.StdEncoding.EncodeToString(bytes)
	if re != s {
		return nil, fmt.Errorf("non-canonical base64 (trailing bits must be zero)")
	}
	return bytes, nil
}

// asStringMap flattens a decoded JSON object (either map[string]any
// from json.Unmarshal, or *MapOrdered from decodeAnyOrdered) into a
// plain map. Order is lost in the output, so callers that need order
// (like $map decoding) must use asOrderedMap instead.
func asStringMap(payload any) (map[string]any, bool) {
	switch v := payload.(type) {
	case map[string]any:
		return v, true
	case *MapOrdered:
		out := make(map[string]any, len(v.Keys))
		for _, k := range v.Keys { out[k] = v.Items[k] }
		return out, true
	}
	return nil, false
}

// asOrderedMap returns a *MapOrdered representation; a plain
// map[string]any input is promoted with an iteration-order snapshot
// (lossy — caller should prefer LoadsJSONLossless to preserve order).
func asOrderedMap(payload any) (*MapOrdered, bool) {
	switch v := payload.(type) {
	case *MapOrdered:
		return v, true
	case *orderedMapLosslessT:
		// ToJSONLossless wraps the $map payload in this adapter so that
		// json.Marshal writes keys in order. The adapter still holds the
		// *un-converted* Weft values (it converts lazily at marshal
		// time), so for the direct value-level round trip
		// FromJSONLossless(ToJSONLossless(x)) we convert each value here,
		// exactly as MarshalJSON would.
		m := NewMap()
		for _, k := range v.m.Keys {
			m.Items[k] = ToJSONLossless(v.m.Items[k])
			m.Keys = append(m.Keys, k)
		}
		return m, true
	case map[string]any:
		m := NewMap()
		for k, val := range v {
			m.Items[k] = val
			m.Keys = append(m.Keys, k)
		}
		return m, true
	}
	return nil, false
}

// LoadsJSONLossless parses a JSON string into a Weft tree, preserving
// (a) the ``1`` vs ``1.0`` lexical distinction via UseNumber, and
// (b) object key insertion order via a token-stream walker.
// Both are required by SPEC §11.2.2 and W4-10/W4-11; the plain
// ``json.Unmarshal`` route loses both.
func LoadsJSONLossless(text string) (Value, error) {
	return LoadsJSONLosslessWithLimits(text, DefaultLimits())
}


// scanJSONSurrogates walks raw JSON text (string-aware, escape-aware) and
// rejects lone UTF-16 surrogate escapes (\uD800 without a following low
// half, or a low half without a preceding high half). encoding/json would
// otherwise silently replace them with U+FFFD, mutating the value — Weft
// strings are Unicode scalar sequences (SPEC §5.7).
func scanJSONSurrogates(text string) error {
	b := []byte(text)
	inStr := false
	i := 0
	hexVal := func(p int) (int, bool) {
		v := 0
		for k := p; k < p+4; k++ {
			if k >= len(b) { return 0, false }
			c := b[k]
			switch {
			case c >= '0' && c <= '9': v = v*16 + int(c-'0')
			case c >= 'a' && c <= 'f': v = v*16 + int(c-'a'+10)
			case c >= 'A' && c <= 'F': v = v*16 + int(c-'A'+10)
			default: return 0, false
			}
		}
		return v, true
	}
	for i < len(b) {
		c := b[i]
		if !inStr {
			if c == '"' { inStr = true }
			i++
			continue
		}
		switch c {
		case '"':
			inStr = false
			i++
		case '\\':
			if i+1 < len(b) && b[i+1] == 'u' {
				v, ok := hexVal(i + 2)
				if !ok { i += 2; continue }
				if v >= 0xD800 && v <= 0xDBFF {
					// high surrogate: must be followed by \uDC00-\uDFFF
					if i+6+6 <= len(b) && b[i+6] == '\\' && b[i+7] == 'u' {
						w, ok2 := hexVal(i + 8)
						if ok2 && w >= 0xDC00 && w <= 0xDFFF {
							i += 12
							continue
						}
					}
					return newErr(ErrSyntax, "lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)", "WEFT_SYNTAX_JSON")
				}
				if v >= 0xDC00 && v <= 0xDFFF {
					return newErr(ErrSyntax, "lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)", "WEFT_SYNTAX_JSON")
				}
				i += 6
				continue
			}
			i += 2
		default:
			i++
		}
	}
	return nil
}

// LoadsJSONLosslessWithLimits is LoadsJSONLossless with explicit SPEC §9
// limits. Size is checked up front; nesting depth is checked while the
// token stream is walked (W5-02: the walker is recursive, and Go kills the
// whole process on goroutine-stack exhaustion, so the bound must hold
// BEFORE recursion gets anywhere near the runtime limit).
func LoadsJSONLosslessWithLimits(text string, limits Limits) (Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	if len(text) > limits.MaxSizeBytes {
		return nil, newErr(ErrLimit, fmt.Sprintf("JSON input %d bytes exceeds MaxSizeBytes=%d", len(text), limits.MaxSizeBytes), "WEFT_LIMIT_SIZE")
	}
	if err := scanJSONSurrogates(text); err != nil { return nil, err }
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	raw, err := decodeAnyOrdered(dec, 0, limits)
	if err != nil {
		if we, ok := err.(*Error); ok { return nil, we }
		return nil, newErr(ErrSyntax, fmt.Sprintf("JSON decode failed: %v", err), "WEFT_SYNTAX_JSON")
	}
	// EOF check — any trailing tokens would be a stream, not a single value.
	if _, err := dec.Token(); err == nil {
		return nil, newErr(ErrSyntax, "unexpected trailing content in JSON", "WEFT_SYNTAX_JSON")
	} else if err.Error() != "EOF" {
		// not EOF — propagate
		return nil, newErr(ErrSyntax, fmt.Sprintf("JSON decode failed: %v", err), "WEFT_SYNTAX_JSON")
	}
	return fromJSONLosslessLimited(raw, 0, &convCtx{limits: limits})
}

// decodeAnyOrdered walks tokens from a json.Decoder (with UseNumber on)
// and reconstructs the value — but preserves object key insertion
// order by using *MapOrdered for every JSON object. Returned values
// pass directly to FromJSONLossless.
func decodeAnyOrdered(dec *json.Decoder, depth int, limits Limits) (any, error) {
	// W5-02 / SPEC §9: each Weft level costs at most two JSON levels
	// ({"$list": [...]}), so 2*MaxDepth+8 bounds this walker's recursion
	// far below the goroutine stack limit; the precise Weft depth rule
	// (§8) is enforced again during conversion.
	if depth > limits.MaxDepth*2+8 {
		return nil, newErr(ErrLimit, fmt.Sprintf("JSON nesting exceeds MaxDepth=%d", limits.MaxDepth), "WEFT_LIMIT_DEPTH")
	}
	tok, err := dec.Token()
	if err != nil { return nil, err }
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '[':
			arr := []any{}
			for dec.More() {
				v, err := decodeAnyOrdered(dec, depth+1, limits)
				if err != nil { return nil, err }
				arr = append(arr, v)
				// Structural bound only: a $sexpr array holds items+1 and
				// tag objects wrap values, so the exact §9 Weft limit is
				// enforced during conversion, not against the raw JSON.
				if len(arr) > limits.MaxItemsPerColl*2+8 {
					return nil, newErr(ErrLimit, fmt.Sprintf("JSON array exceeds structural bound (MaxItemsPerColl=%d)", limits.MaxItemsPerColl), "WEFT_LIMIT_ITEMS")
				}
			}
			if _, err := dec.Token(); err != nil { return nil, err } // ']'
			return arr, nil
		case '{':
			m := NewMap()
			for dec.More() {
				kTok, err := dec.Token()
				if err != nil { return nil, err }
				k, ok := kTok.(string)
				if !ok { return nil, newErr(ErrSyntax, "JSON object key must be a string", "WEFT_SYNTAX_JSON") }
				// SPEC §11.2.5: duplicate keys MUST be rejected.
				if _, dup := m.Items[k]; dup {
					return nil, newErr(ErrSyntax, fmt.Sprintf("duplicate key %q in JSON object", k), "WEFT_SYNTAX_JSON_DUPKEY")
				}
				v, err := decodeAnyOrdered(dec, depth+1, limits)
				if err != nil { return nil, err }
				m.Items[k] = v
				m.Keys = append(m.Keys, k)
				// Structural bound only — see the array case above.
				if len(m.Keys) > limits.MaxItemsPerColl*2+8 {
					return nil, newErr(ErrLimit, fmt.Sprintf("JSON object exceeds structural bound (MaxItemsPerColl=%d)", limits.MaxItemsPerColl), "WEFT_LIMIT_ITEMS")
				}
			}
			if _, err := dec.Token(); err != nil { return nil, err } // '}'
			return m, nil
		}
		return nil, newErr(ErrSyntax, fmt.Sprintf("unexpected delim %v", t), "WEFT_SYNTAX_JSON")
	default:
		return tok, nil
	}
}

func FromJSONLossless(j any) (Value, error) {
	return fromJSONLosslessLimited(j, 0, &convCtx{limits: DefaultLimits()})
}

// FromJSONLosslessWithLimits is FromJSONLossless with explicit SPEC §9
// limits (depth / total items / items per collection / string bytes /
// int digits are all enforced on this entry point, per §9).
func FromJSONLosslessWithLimits(j any, limits Limits) (Value, error) {
	if err := limits.validate(); err != nil { return nil, err }
	return fromJSONLosslessLimited(j, 0, &convCtx{limits: limits})
}

func fromJSONLosslessLimited(j any, depth int, ctx *convCtx) (Value, error) {
	if err := ctx.bump(); err != nil { return nil, err }
	return fromJSONLosslessInner(j, depth, ctx)
}

// fromJSONLosslessInner is the body of fromJSONLosslessLimited WITHOUT
// the total-items bump — used when re-dispatching a different
// representation of the SAME value (the *MapOrdered tag wrapper), which
// has already been counted once. One Weft value is one bump.
func fromJSONLosslessInner(j any, depth int, ctx *convCtx) (Value, error) {
	switch val := j.(type) {
	case nil:
		return nil, nil
	case bool:
		return val, nil
	case json.Number:
		// Preferred path: ``json.Decoder.UseNumber()`` preserves the
		// original lexical form, so we can distinguish ``1`` (Integer)
		// from ``1.0`` (Float) and catch 2^53+1 before it rounds.
		s := string(val)
		if strings.ContainsAny(s, ".eE") {
			return nil, newErr(ErrType,
				`fractional number must use the {"$float": ...} tagged form (plain JSON number is reserved for Weft Integer)`,
				"WEFT_TYPE_FLOAT")
		}
		// Validate strict -?[0-9]+
		if !isStrictIntPayload(s) {
			return nil, newErr(ErrType, fmt.Sprintf("invalid integer form: %q", s), "WEFT_TYPE_INT")
		}
		if n := len(strings.TrimPrefix(s, "-")); n > ctx.limits.MaxIntDigits {
			return nil, newErr(ErrLimit, fmt.Sprintf("integer has %d digits, exceeds MaxIntDigits=%d", n, ctx.limits.MaxIntDigits), "WEFT_LIMIT_INT")
		}
		// SPEC §11.2.1 rule 2 + W4-11: enforce JSON-safe range.
		b, ok := new(big.Int).SetString(s, 10)
		if !ok {
			return nil, newErr(ErrType, fmt.Sprintf("invalid integer: %q", s), "WEFT_TYPE_INT")
		}
		if b.Cmp(big.NewInt(safeIntMinJSONv)) < 0 || b.Cmp(big.NewInt(safeIntMaxJSONv)) > 0 {
			return nil, newErr(ErrType,
				`int outside JSON-safe range must use the {"$int-big": ...} tagged form`,
				"WEFT_TYPE_INT")
		}
		return b, nil
	case *big.Int:
		// ToJSONLossless leaves JSON-safe integers as raw *big.Int in the
		// value tree (json.Marshal writes them as plain numbers), so the
		// direct round trip FromJSONLossless(ToJSONLossless(x)) must
		// accept them. Outside the safe range the encoder emits the
		// {"$int-big": ...} tagged form, so a raw out-of-range *big.Int
		// is rejected exactly like an out-of-range number token.
		if n := len(strings.TrimPrefix(val.String(), "-")); n > ctx.limits.MaxIntDigits {
			return nil, newErr(ErrLimit, fmt.Sprintf("integer has %d digits, exceeds MaxIntDigits=%d", n, ctx.limits.MaxIntDigits), "WEFT_LIMIT_INT")
		}
		if val.Cmp(big.NewInt(safeIntMinJSONv)) < 0 || val.Cmp(big.NewInt(safeIntMaxJSONv)) > 0 {
			return nil, newErr(ErrType,
				`int outside JSON-safe range must use the {"$int-big": ...} tagged form`,
				"WEFT_TYPE_INT")
		}
		return new(big.Int).Set(val), nil
	case float64:
		// Fallback: caller did not use UseNumber() and encoding/json
		// has already collapsed the token to float64. We can't
		// distinguish 1 vs 1.0 at this point, and 2^53+1 has already
		// been truncated to 2^53. Reject *all* bare float64 here so
		// callers are forced through the json.Number path; this
		// preserves the W4-11 silent-precision-loss guarantee.
		if isNaN(val) || isPosInf(val) || isNegInf(val) {
			return nil, newErr(ErrType, `non-finite float must use the {"$float": ...} tagged form`, "WEFT_TYPE_FLOAT")
		}
		return nil, newErr(ErrType,
			`decoder must use json.Decoder.UseNumber() so Int/Float and safe-range distinctions survive — pass through FromJSONLosslessString or LoadsJSONLossless`,
			"WEFT_TYPE_FLOAT")
	case string:
		return nil, newErr(ErrType,
			`bare JSON string is not a lossless form; wrap it as {"$str":...}, {"$sym":...}, or {"$kw":...}`,
			"WEFT_TYPE_STRING")
	case []any:
		return nil, newErr(ErrType,
			`bare JSON array is not a lossless form; use {"$list":[...]} or {"$sexpr":[...]}`,
			"WEFT_TYPE_ARRAY")
	case *MapOrdered:
		// W4-10: produced by decodeAnyOrdered so key insertion order is
		// preserved through the lossless decode.
		if len(val.Keys) != 1 {
			return nil, newErr(ErrType,
				fmt.Sprintf(`lossless JSON object must have exactly one key (got %d); a plain mapping should be {"$map": {...}}`, len(val.Keys)),
				"WEFT_TYPE_LOSSLESS")
		}
		// Re-dispatch the single tagged entry; depth is unchanged (the
		// wrapper object itself is not a Weft structural level) and the
		// value was already counted by the bump at the entry point.
		return fromJSONLosslessInner(map[string]any{val.Keys[0]: val.Items[val.Keys[0]]}, depth, ctx)
	case map[string]any:
		if len(val) != 1 {
			return nil, newErr(ErrType,
				fmt.Sprintf(`lossless JSON object must have exactly one key (got %d); a plain mapping should be {"$map": {...}}`, len(val)),
				"WEFT_TYPE_LOSSLESS")
		}
		var tag string
		var payload any
		for k, v := range val {
			tag, payload = k, v
		}
		if _, ok := losslessTags[tag]; !ok {
			return nil, newErr(ErrType, fmt.Sprintf("unknown lossless tag %q", tag), "WEFT_TYPE_LOSSLESS")
		}
		switch tag {
		case "$sym":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$sym payload must be a string", "WEFT_TYPE_SYM")
			}
			sym, err := NewSymbol(s)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$sym payload invalid: %v", err), "WEFT_TYPE_SYM")
			}
			return sym, nil
		case "$kw":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$kw payload must be a string", "WEFT_TYPE_KW")
			}
			kw, err := NewKeyword(s)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$kw payload invalid: %v", err), "WEFT_TYPE_KW")
			}
			return kw, nil
		case "$bigint":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$bigint payload must be a string", "WEFT_TYPE_BIGINT")
			}
			if !isStrictIntPayload(s) {
				return nil, newErr(ErrType,
					fmt.Sprintf("$bigint payload is not a decimal integer: %q (must match -?[0-9]+)", s),
					"WEFT_TYPE_BIGINT")
			}
			if n := len(strings.TrimPrefix(s, "-")); n > ctx.limits.MaxIntDigits {
				return nil, newErr(ErrLimit, fmt.Sprintf("%s payload has %d digits, exceeds MaxIntDigits=%d", "$bigint", n, ctx.limits.MaxIntDigits), "WEFT_LIMIT_INT")
			}
			bi := new(big.Int)
			if _, ok := bi.SetString(s, 10); !ok {
				return nil, newErr(ErrType, fmt.Sprintf("$bigint payload invalid: %q", s), "WEFT_TYPE_BIGINT")
			}
			return BigIntVal{Value: bi}, nil
		case "$int-big":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$int-big payload must be a string", "WEFT_TYPE_INT")
			}
			if !isStrictIntPayload(s) {
				return nil, newErr(ErrType,
					fmt.Sprintf("$int-big payload is not a decimal integer: %q (must match -?[0-9]+)", s),
					"WEFT_TYPE_INT")
			}
			if n := len(strings.TrimPrefix(s, "-")); n > ctx.limits.MaxIntDigits {
				return nil, newErr(ErrLimit, fmt.Sprintf("%s payload has %d digits, exceeds MaxIntDigits=%d", "$int-big", n, ctx.limits.MaxIntDigits), "WEFT_LIMIT_INT")
			}
			bi := new(big.Int)
			if _, ok := bi.SetString(s, 10); !ok {
				return nil, newErr(ErrType, fmt.Sprintf("$int-big payload invalid: %q", s), "WEFT_TYPE_INT")
			}
			return bi, nil
		case "$dec":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$dec payload must be a string", "WEFT_TYPE_DEC")
			}
			d, err := NewDecimal(s)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$dec payload invalid: %v", err), "WEFT_TYPE_DEC")
			}
			return d, nil
		case "$str":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$str payload must be a string", "WEFT_TYPE_STR")
			}
			if !utf8.ValidString(s) {
				return nil, newErr(ErrSyntax, "$str payload is not valid UTF-8 (Weft strings are Unicode scalar sequences)", "WEFT_SYNTAX_JSON")
			}
			if len(s) > ctx.limits.MaxStringBytes {
				return nil, newErr(ErrLimit, fmt.Sprintf("$str payload exceeds MaxStringBytes=%d", ctx.limits.MaxStringBytes), "WEFT_LIMIT_STRING")
			}
			return s, nil
		case "$bin":
			inner, ok := asStringMap(payload)
			if !ok {
				return nil, newErr(ErrType, "$bin payload must be an object with 'enc' and 'data'", "WEFT_TYPE_BIN")
			}
			if len(inner) != 2 {
				return nil, newErr(ErrType, "$bin payload must have exactly {enc, data}", "WEFT_TYPE_BIN")
			}
			enc, _ := inner["enc"].(string)
			data, dataOk := inner["data"].(string)
			if !dataOk {
				return nil, newErr(ErrType, "$bin.data must be a string", "WEFT_TYPE_BIN")
			}
			switch enc {
			case "base64":
				// SPEC §5.8 / W4-16: canonical base64 — reject non-zero
				// trailing bits so a single byte sequence has exactly one
				// base64 spelling.
				b, err := canonicalBase64Decode(data)
				if err != nil {
					return nil, newErr(ErrType, fmt.Sprintf("$bin.data invalid: %v", err), "WEFT_TYPE_BIN")
				}
				return Binary{Data: b, Encoding: Base64Enc}, nil
			case "hex":
				b, err := hex.DecodeString(data)
				if err != nil {
					return nil, newErr(ErrType, fmt.Sprintf("$bin.data is not valid hex: %v", err), "WEFT_TYPE_BIN")
				}
				return Binary{Data: b, Encoding: HexEnc}, nil
			default:
				return nil, newErr(ErrType, fmt.Sprintf("$bin.enc must be 'base64' or 'hex', got %q", enc), "WEFT_TYPE_BIN")
			}
		case "$dt":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$dt payload must be a string", "WEFT_TYPE_DT")
			}
			if !dtPayloadRe.MatchString(s) {
				return nil, newErr(ErrType,
					fmt.Sprintf("$dt payload is not a canonical datetime: %q", s),
					"WEFT_TYPE_DT")
			}
			doc, err := Parse("#" + s)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$dt payload invalid: %q (%v)", s, err), "WEFT_TYPE_DT")
			}
			if _, ok := doc.(Typed); ok {
				return nil, newErr(ErrType,
					fmt.Sprintf("$dt payload must be a datetime scalar, not a type-annotated value: %q", s),
					"WEFT_TYPE_DT")
			}
			if _, ok := doc.(Date); !ok {
				return nil, newErr(ErrType, fmt.Sprintf("$dt payload is not a datetime: %q", s), "WEFT_TYPE_DT")
			}
			return doc, nil
		case "$float":
			s, ok := payload.(string)
			if !ok {
				return nil, newErr(ErrType, "$float payload must be a string", "WEFT_TYPE_FLOAT")
			}
			switch s {
			case "nan":
				return nan64(), nil
			case "inf":
				return pinf(), nil
			case "-inf":
				return ninf(), nil
			}
			if !floatDecRe.MatchString(s) {
				return nil, newErr(ErrType, fmt.Sprintf("$float payload is not a finite decimal: %q", s), "WEFT_TYPE_FLOAT")
			}
			f, err := strconv.ParseFloat(s, 64)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$float payload invalid: %q (%v)", s, err), "WEFT_TYPE_FLOAT")
			}
			if isNaN(f) || isPosInf(f) || isNegInf(f) {
				return nil, newErr(ErrType, fmt.Sprintf("$float payload parsed to non-finite: %q", s), "WEFT_TYPE_FLOAT")
			}
			// Preserve the sign of zero (strconv.ParseFloat handles -0 correctly,
			// but keep the branch explicit so this isn't accidentally broken by
			// future refactoring).
			if s == "-0.0" || s == "-0" {
				return math.Copysign(0, -1), nil
			}
			return f, nil
		case "$sexpr":
			arr, ok := payload.([]any)
			if !ok || len(arr) == 0 {
				return nil, newErr(ErrType, "$sexpr payload must be a non-empty array", "WEFT_TYPE_SEXPR")
			}
			tagName, ok := arr[0].(string)
			if !ok {
				return nil, newErr(ErrType, "$sexpr tag (payload[0]) must be a string", "WEFT_TYPE_SEXPR")
			}
			// W4-12: validate tag through NewSymbol; raw ``Symbol(name)``
			// cast used to produce S-expressions whose text form
			// re-parses as something else (``(not a sym!)`` → tag=not,
			// items=[a, sym!]).
			tagSym, err := NewSymbol(tagName)
			if err != nil {
				return nil, newErr(ErrType, fmt.Sprintf("$sexpr tag is not a valid Symbol name: %v", err), "WEFT_TYPE_SEXPR")
			}
			if err := ctx.depth(depth + 1); err != nil { return nil, err }
			if err := ctx.coll(len(arr) - 1); err != nil { return nil, err }
			items := make([]Value, 0, len(arr)-1)
			for _, x := range arr[1:] {
				v, err := fromJSONLosslessLimited(x, depth+1, ctx)
				if err != nil {
					return nil, err
				}
				items = append(items, v)
			}
			return Sexpr{Tag: tagSym, Items: items}, nil
		case "$typed":
			inner, ok := asStringMap(payload)
			if !ok || len(inner) != 2 {
				return nil, newErr(ErrType, `$typed payload must be {"v":..., "t":...}`, "WEFT_TYPE_TYPED")
			}
			vJ, vok := inner["v"]
			tJ, tok := inner["t"]
			if !vok || !tok {
				return nil, newErr(ErrType, `$typed payload must be {"v":..., "t":...}`, "WEFT_TYPE_TYPED")
			}
			if err := ctx.depth(depth + 1); err != nil { return nil, err }
			v, err := fromJSONLosslessLimited(vJ, depth+1, ctx)
			if err != nil {
				return nil, err
			}
			t, err := fromJSONLosslessLimited(tJ, depth+1, ctx)
			if err != nil {
				return nil, err
			}
			var te TypeExpr
			switch tv := t.(type) {
			case Symbol:
				te = tv
			case Sexpr:
				te = tv
			default:
				return nil, newErr(ErrType, "$typed.t must resolve to a Symbol or Sexpr", "WEFT_TYPE_TYPED")
			}
			return Typed{Value: v, Type: te}, nil
		case "$list":
			arr, ok := payload.([]any)
			if !ok {
				return nil, newErr(ErrType, "$list payload must be an array", "WEFT_TYPE_LIST")
			}
			if err := ctx.depth(depth + 1); err != nil { return nil, err }
			if err := ctx.coll(len(arr)); err != nil { return nil, err }
			out := make([]Value, len(arr))
			for i, x := range arr {
				v, err := fromJSONLosslessLimited(x, depth+1, ctx)
				if err != nil {
					return nil, err
				}
				out[i] = v
			}
			return out, nil
		case "$map":
			// W4-10 / SPEC §11.2.2: preserve the insertion order of
			// the $map object. ``payload`` is a *MapOrdered when the
			// JSON was decoded via LoadsJSONLossless; otherwise it's a
			// plain map[string]any and the order is already lost
			// upstream (callers that need order must use the ordered
			// decoder).
			ordered, ok := asOrderedMap(payload)
			if !ok {
				return nil, newErr(ErrType, "$map payload must be an object", "WEFT_TYPE_MAP")
			}
			m := NewMap()
			if err := ctx.depth(depth + 1); err != nil { return nil, err }
			if err := ctx.coll(len(ordered.Keys)); err != nil { return nil, err }
			for _, k := range ordered.Keys {
				if !isValidKeyword(k) {
					return nil, newErr(ErrType, fmt.Sprintf("$map key %q is not a valid Weft keyword name (would break structure on re-parse)", k), "WEFT_TYPE_KEY")
				}
				v, err := fromJSONLosslessLimited(ordered.Items[k], depth+1, ctx)
				if err != nil {
					return nil, err
				}
				m.Set(k, v)
			}
			return m, nil
		}
	}
	return nil, newErr(ErrType, fmt.Sprintf("unsupported JSON value: %T", j), "WEFT_TYPE_JSON")
}

// Strict RFC-3339-like shape for ``$dt`` payloads. Rejects trailing
// whitespace, comments, type annotations, and any non-ASCII characters.
var dtPayloadRe = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?$`)

// Finite-float decimal shape for ``$float`` payloads.
var floatDecRe = regexp.MustCompile(`^[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$`)

// floatCanonicalText produces the shortest round-trip decimal text for
// a finite float, preserving the sign of zero so ``-0.0`` round-trips.
func floatCanonicalText(f float64) string {
	if f == 0 {
		if math.Signbit(f) { return "-0.0" }
		return "0.0"
	}
	abs := math.Abs(f)
	var s string
	if abs >= 1e-4 && abs < 1e16 {
		s = strconv.FormatFloat(f, 'f', -1, 64)
	} else {
		s = strconv.FormatFloat(f, 'e', -1, 64)
	}
	if !strings.ContainsAny(s, ".eE") { s += ".0" }
	return s
}

var reservedSymbolLiterals = map[string]struct{}{
	"true": {}, "false": {}, "nil": {}, "nan": {}, "inf": {}, "-inf": {}, "+inf": {},
}

// isValidSymbol validates a Symbol name per SPEC §5.10. It must start
// with a symbol-start char and continue with symbol-cont chars only
// (ASCII digits allowed; Unicode digits like ``１`` are not). It must
// not be a reserved literal, and must not start with sign+digit.
func isValidSymbol(s string) bool {
	return isValidIdentShape(s) && !isReservedLiteral(s)
}

// isValidKeyword validates a Keyword name per SPEC §5.11. Compared to a
// Symbol, a Keyword MAY use a reserved literal (``:true``, ``:nil``,
// ``:nan`` are permitted because the leading ``:`` disambiguates them
// from top-level literals, W4-06). The sign+digit restriction still
// applies.
func isValidKeyword(s string) bool {
	return isValidIdentShape(s)
}

func isValidIdentShape(s string) bool {
	if s == "" { return false }
	runes := []rune(s)
	if !isSymbolStart(runes[0]) { return false }
	for _, r := range runes[1:] {
		if !isSymbolCont(r) { return false }
	}
	if len(s) >= 2 && (s[0] == '+' || s[0] == '-') && s[1] >= '0' && s[1] <= '9' {
		return false
	}
	return true
}

func isReservedLiteral(s string) bool {
	_, ok := reservedSymbolLiterals[s]
	return ok
}

// decimalRe accepts the same text the lexer canonicalises on output.
var decimalRe = regexp.MustCompile(`^[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?$`)

// NewDecimal validates ``text`` and canonicalises the exponent marker to
// lower-case, matching the lexer's behaviour (``1E2`` and ``1e2`` share
// ``.Text``).
func NewDecimal(text string) (Decimal, error) {
	if strings.ContainsAny(text, "\n\r") || !decimalRe.MatchString(text) {
		return Decimal{}, fmt.Errorf("invalid decimal text: %q", text)
	}
	// SPEC §7.2 canonicalisation: leading '+' stripped, exponent lowered.
	text = strings.TrimPrefix(text, "+")
	if strings.Contains(text, "E") {
		text = strings.ReplaceAll(text, "E", "e")
	}
	return Decimal{Text: text}, nil
}

// NewSymbol validates ``name`` and constructs a Symbol per SPEC §5.10.
func NewSymbol(name string) (Symbol, error) {
	if !isValidSymbol(name) {
		return "", fmt.Errorf("invalid Symbol name: %q", name)
	}
	return Symbol(name), nil
}

// NewKeyword validates ``name`` and constructs a Keyword per SPEC §5.10.
func NewKeyword(name string) (Keyword, error) {
	// SPEC §5.11 (W4-06): keyword names use the symbol character class
	// but reserved literals (``true``, ``nil``, …) ARE permitted because
	// the leading ``:`` disambiguates them. Only the shape and the
	// sign+digit restriction apply.
	if !isValidKeyword(name) {
		return "", fmt.Errorf("invalid Keyword name: %q", name)
	}
	return Keyword(name), nil
}

/* --------------------------------------------------------------------------
 * Float helpers (keep IEEE 754 constants in one place)
 * -------------------------------------------------------------------------- */

func nan64() float64          { return math.NaN() }
func pinf() float64           { return math.Inf(1) }
func ninf() float64           { return math.Inf(-1) }
func isNaN(f float64) bool    { return math.IsNaN(f) }
func isPosInf(f float64) bool { return math.IsInf(f, 1) }
func isNegInf(f float64) bool { return math.IsInf(f, -1) }

/* --------------------------------------------------------------------------
 * Equality (SPEC §5.16)
 * -------------------------------------------------------------------------- */

type eqPair struct{ a, b Value }

// Equals reports Weft value equality ``a ≡ b`` per SPEC §5.16. Iterative
// (explicit work stack) so adversarially deep trees cannot exhaust the
// goroutine stack. Float equality is IEEE-754 bit identity except
// NaN ≢ NaN; -0.0 ≢ 0.0. Integer/BigInt compare numerically (42 ≡ 42n).
// Decimal compares normalised text; Binary compares content bytes.
func Equals(a, b Value) bool {
	stack := []eqPair{{a, b}}
	for len(stack) > 0 {
		p := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		x, y := p.a, p.b
		switch xv := x.(type) {
		case nil:
			if y != nil { return false }
		case bool:
			yv, ok := y.(bool)
			if !ok || xv != yv { return false }
		case *big.Int:
			switch yv := y.(type) {
			case *big.Int:
				if xv.Cmp(yv) != 0 { return false }
			case BigIntVal:
				if xv.Cmp(yv.Value) != 0 { return false }
			default:
				return false
			}
		case BigIntVal:
			switch yv := y.(type) {
			case *big.Int:
				if xv.Value.Cmp(yv) != 0 { return false }
			case BigIntVal:
				if xv.Value.Cmp(yv.Value) != 0 { return false }
			default:
				return false
			}
		case float64:
			yv, ok := y.(float64)
			if !ok { return false }
			if isNaN(xv) || isNaN(yv) { return false } // NaN ≢ NaN
			if math.Float64bits(xv) != math.Float64bits(yv) { return false }
		case Decimal:
			yv, ok := y.(Decimal)
			if !ok || xv.Text != yv.Text { return false }
		case string:
			yv, ok := y.(string)
			if !ok || xv != yv { return false }
		case Binary:
			yv, ok := y.(Binary)
			if !ok || len(xv.Data) != len(yv.Data) { return false }
			for i := range xv.Data {
				if xv.Data[i] != yv.Data[i] { return false }
			}
		case Date:
			yv, ok := y.(Date)
			if !ok { return false }
			if xv.Year != yv.Year || xv.Month != yv.Month || xv.Day != yv.Day ||
				xv.HasTime != yv.HasTime || xv.Hour != yv.Hour || xv.Minute != yv.Minute ||
				xv.Second != yv.Second || xv.Nano != yv.Nano { return false }
			xo, yo := xv.TZOffsetMinutes, yv.TZOffsetMinutes
			if (xo == nil) != (yo == nil) { return false }
			if xo != nil && *xo != *yo { return false }
		case Symbol:
			yv, ok := y.(Symbol)
			if !ok || xv != yv { return false }
		case Keyword:
			yv, ok := y.(Keyword)
			if !ok || xv != yv { return false }
		case []Value:
			yv, ok := y.([]Value)
			if !ok || len(xv) != len(yv) { return false }
			for i := range xv { stack = append(stack, eqPair{xv[i], yv[i]}) }
		case *MapOrdered:
			yv, ok := y.(*MapOrdered)
			if !ok || len(xv.Keys) != len(yv.Keys) { return false }
			for _, k := range xv.Keys {
				w, present := yv.Items[k]
				if !present { return false }
				stack = append(stack, eqPair{xv.Items[k], w})
			}
		case Sexpr:
			yv, ok := y.(Sexpr)
			if !ok || xv.Tag != yv.Tag || len(xv.Items) != len(yv.Items) { return false }
			for i := range xv.Items { stack = append(stack, eqPair{xv.Items[i], yv.Items[i]}) }
		case Typed:
			yv, ok := y.(Typed)
			if !ok { return false }
			stack = append(stack, eqPair{xv.Value, yv.Value})
			switch xt := xv.Type.(type) {
			case Symbol:
				yt, ok := yv.Type.(Symbol)
				if !ok || xt != yt { return false }
			case Sexpr:
				yt, ok := yv.Type.(Sexpr)
				if !ok || xt.Tag != yt.Tag || len(xt.Items) != len(yt.Items) { return false }
				for i := range xt.Items { stack = append(stack, eqPair{xt.Items[i], yt.Items[i]}) }
			default:
				return false
			}
		default:
			return false
		}
	}
	return true
}

// NegZero returns the IEEE-754 negative zero (test helper).
func NegZero() float64 { return math.Copysign(0, -1) }
