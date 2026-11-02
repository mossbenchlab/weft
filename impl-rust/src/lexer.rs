//! Weft lexer (Rust).
//!
//! Converts UTF-8 text to a token stream. Validates the character-level
//! rules from SPEC §3-§5 (BOM, surrogates, control chars, overlong UTF-8
//! is handled by the fact that we accept `&str`).

use crate::error::{Result, WeftError};
use crate::limits::Limits;
use crate::types::{BigIntValue, Binary, BinaryEncoding, Decimal, WeftDate};
use num_bigint::BigInt;
use num_traits::Num;

#[derive(Debug, Clone, PartialEq)]
pub enum TokenKind {
    LParen, RParen, LBrack, RBrack, LBrace, RBrace,
    ColonColon,
    Int(BigInt),
    BigInt(BigIntValue),
    Float(f64),
    Decimal(Decimal),
    Str(String),
    Binary(Binary),
    DateTime(WeftDate),
    Bool(bool),
    Nil,
    Nan,
    Inf,
    NegInf,
    Symbol(String),
    Keyword(String),
}

#[derive(Debug, Clone)]
pub struct Token {
    pub kind: TokenKind,
    pub line: usize,
    pub column: usize,
    pub offset: usize,
}

pub struct Lexer<'a> {
    text: &'a str,
    chars: Vec<(usize, char)>,
    idx: usize,
    line: usize,
    col: usize,
    limits: Limits,
}

impl<'a> Lexer<'a> {
    pub fn new(text: &'a str, limits: Limits) -> Result<Self> {
        if text.contains('\u{FEFF}') {
            let bidx = text.find('\u{FEFF}').unwrap();
            let (l, c) = pos_from_byte_offset(text, bidx);
            return Err(WeftError::encoding(
                "U+FEFF (BOM) is not permitted anywhere in a Weft document",
                "WEFT_ENCODING_BOM"
            ).with_pos(l, c, bidx));
        }
        for bad in ['\u{FFFE}', '\u{FFFF}'] {
            if let Some(bidx) = text.find(bad) {
                let (l, c) = pos_from_byte_offset(text, bidx);
                return Err(WeftError::encoding(
                    format!("noncharacter U+{:04X} is not permitted", bad as u32),
                    "WEFT_ENCODING_NONCHAR"
                ).with_pos(l, c, bidx));
            }
        }
        if text.len() > limits.max_size_bytes {
            return Err(WeftError::limit(
                format!("document size {} exceeds max_size_bytes={}", text.len(), limits.max_size_bytes),
                "WEFT_LIMIT_SIZE",
            ));
        }
        let chars: Vec<(usize, char)> = text.char_indices().collect();
        Ok(Self { text, chars, idx: 0, line: 1, col: 1, limits })
    }

    pub fn tokenize(mut self) -> Result<Vec<Token>> {
        let mut toks = Vec::new();
        while let Some(tok) = self.next_token()? {
            toks.push(tok);
        }
        Ok(toks)
    }

    fn peek(&self, offset: usize) -> Option<char> {
        self.chars.get(self.idx + offset).map(|&(_, c)| c)
    }

    fn current_byte_offset(&self) -> usize {
        self.chars.get(self.idx).map(|&(b, _)| b).unwrap_or(self.text.len())
    }

    fn advance(&mut self, n: usize) {
        for _ in 0..n {
            if self.idx >= self.chars.len() { return; }
            let c = self.chars[self.idx].1;
            if c == '\n' {
                self.line += 1;
                self.col = 1;
            } else if c == '\r' {
                // CRLF: defer the line bump to the \n; lone CR counts as a
                // line terminator by itself (SPEC §3).
                let next = self.chars.get(self.idx + 1).map(|&(_, ch)| ch);
                if next != Some('\n') {
                    self.line += 1;
                    self.col = 1;
                }
            } else {
                self.col += 1;
            }
            self.idx += 1;
        }
    }

    fn err(&self, msg: impl Into<String>, code: &str) -> WeftError {
        // In this implementation `offset` has always been a UTF-8 byte
        // position (that's what `current_byte_offset` returns). The new
        // `byte_offset` field is populated to match, so callers can
        // migrate to the explicit name without a semantic change.
        let bo = self.current_byte_offset();
        WeftError::lex(msg, code)
            .with_pos(self.line, self.col, bo)
            .with_byte_offset(bo)
    }

    fn next_token(&mut self) -> Result<Option<Token>> {
        loop {
            match self.peek(0) {
                None => return Ok(None),
                Some(c) if is_ws(c) => { self.advance(1); continue; }
                Some(c) if is_rejected_ws(c) => {
                    return Err(self.err(
                        format!("Unicode whitespace U+{:04X} not permitted outside strings/comments", c as u32),
                        "WEFT_SYNTAX_WS"
                    ));
                }
                Some(';') => { self.skip_line_comment()?; continue; }
                Some('#') if self.peek(1) == Some('|') => { self.skip_block_comment()?; continue; }
                _ => break,
            }
        }

        let line = self.line;
        let col = self.col;
        let offset = self.current_byte_offset();
        let ch = self.peek(0).unwrap();
        let nxt = self.peek(1);

        macro_rules! single {
            ($kind:expr) => {{ self.advance(1); return Ok(Some(Token { kind: $kind, line, column: col, offset })); }};
        }

        match ch {
            '(' => single!(TokenKind::LParen),
            ')' => single!(TokenKind::RParen),
            '[' => single!(TokenKind::LBrack),
            ']' => single!(TokenKind::RBrack),
            '{' => single!(TokenKind::LBrace),
            '}' => single!(TokenKind::RBrace),
            _ => {}
        }
        if ch == ':' && nxt == Some(':') {
            self.advance(2);
            return Ok(Some(Token { kind: TokenKind::ColonColon, line, column: col, offset }));
        }
        if ch == ':' {
            self.advance(1);
            let name = self.read_symbol_name();
            if name.is_empty() {
                return Err(WeftError::lex(
                    "':' must be followed by a symbol name", "WEFT_SYNTAX_KEYWORD"
                ).with_pos(line, col, offset));
            }
            // SPEC §5.10: a name beginning with sign+digit is reserved
            // for signed-number literals — ``:+1`` must not parse.
            let bytes = name.as_bytes();
            if bytes.len() >= 2 && (bytes[0] == b'+' || bytes[0] == b'-')
                && bytes[1].is_ascii_digit()
            {
                return Err(WeftError::lex(
                    format!("keyword name {:?} starts with sign+digit, which ambiguates with a signed number literal", name),
                    "WEFT_SYNTAX_KEYWORD"
                ).with_pos(line, col, offset));
            }
            return Ok(Some(Token { kind: TokenKind::Keyword(name), line, column: col, offset }));
        }

        if ch == '"' {
            if self.peek(1) == Some('"') && self.peek(2) == Some('"') {
                let s = self.read_triple_string()?;
                return Ok(Some(Token { kind: TokenKind::Str(s), line, column: col, offset }));
            }
            let s = self.read_dquote_string()?;
            return Ok(Some(Token { kind: TokenKind::Str(s), line, column: col, offset }));
        }
        if ch == 'r' && nxt == Some('"') {
            self.advance(1);
            let s = self.read_raw_string()?;
            return Ok(Some(Token { kind: TokenKind::Str(s), line, column: col, offset }));
        }
        if ch == 'b' && nxt == Some('"') {
            self.advance(1);
            let b = self.read_binary()?;
            return Ok(Some(Token { kind: TokenKind::Binary(b), line, column: col, offset }));
        }

        if ch == '#' {
            match nxt {
                None => return Err(self.err("stray '#' at end of input", "WEFT_SYNTAX_HASH")),
                Some('!') => return Err(self.err("'#!' is reserved", "WEFT_RESERVED_SHEBANG")),
                Some(c) if c.is_ascii_digit() => {
                    return Ok(Some(self.read_datetime(line, col, offset)?));
                }
                Some(c) => return Err(self.err(format!("'#{}' is a reserved syntax", c), "WEFT_RESERVED_HASH")),
            }
        }

        if ch == '+' || ch == '-' || ch.is_ascii_digit() {
            if let Some(tok) = self.read_number_or_sign_lit(line, col, offset)? {
                return Ok(Some(tok));
            }
        }

        if is_symbol_start(ch) {
            let name = self.read_symbol_name();
            let kind = match name.as_str() {
                "true" => TokenKind::Bool(true),
                "false" => TokenKind::Bool(false),
                "nil" => TokenKind::Nil,
                "nan" => TokenKind::Nan,
                "inf" => TokenKind::Inf,
                _ => {
                    if name.starts_with('$') {
                        return Err(WeftError::lex(
                            format!("symbols beginning with '$' are reserved: {}", name),
                            "WEFT_RESERVED_DOLLAR"
                        ).with_pos(line, col, offset));
                    }
                    // Reject sign+digit prefixes (would ambiguate with signed numbers).
                    if name.len() >= 2 {
                        let mut cs = name.chars();
                        let a = cs.next().unwrap();
                        let b = cs.next().unwrap();
                        if (a == '+' || a == '-') && b.is_ascii_digit() {
                            return Err(WeftError::lex(
                                format!("invalid symbol {}: starts with sign+digit", name),
                                "WEFT_SYNTAX_SYMBOL_AMBIGUOUS"
                            ).with_pos(line, col, offset));
                        }
                    }
                    TokenKind::Symbol(name)
                }
            };
            return Ok(Some(Token { kind, line, column: col, offset }));
        }

        Err(WeftError::lex(
            format!("unexpected character U+{:04X}", ch as u32),
            "WEFT_SYNTAX_CHAR"
        ).with_pos(line, col, offset))
    }

    fn skip_line_comment(&mut self) -> Result<()> {
        self.advance(1);
        while let Some(c) = self.peek(0) {
            // Any line terminator ends a comment: LF, CR, or CRLF. We stop
            // without consuming the terminator so advance() handles line
            // counting.
            if c == '\n' || c == '\r' { return Ok(()); }
            if is_comment_forbidden(c) {
                return Err(self.err(
                    format!("control character U+{:04X} not permitted in comment", c as u32),
                    "WEFT_SYNTAX_COMMENT_CTRL"
                ));
            }
            self.advance(1);
        }
        Ok(())
    }

    fn skip_block_comment(&mut self) -> Result<()> {
        self.advance(2);
        let mut depth = 1usize;
        while let Some(c) = self.peek(0) {
            let n = self.peek(1);
            if c == '#' && n == Some('|') { depth += 1; self.advance(2); continue; }
            if c == '|' && n == Some('#') {
                depth -= 1; self.advance(2);
                if depth == 0 { return Ok(()); }
                continue;
            }
            if is_comment_forbidden(c) {
                return Err(self.err(
                    format!("control character U+{:04X} not permitted in comment", c as u32),
                    "WEFT_SYNTAX_COMMENT_CTRL"
                ));
            }
            self.advance(1);
        }
        Err(self.err("unterminated block comment", "WEFT_SYNTAX_COMMENT_UNTERMINATED"))
    }

    fn read_symbol_name(&mut self) -> String {
        let start_idx = self.idx;
        if start_idx >= self.chars.len() { return String::new(); }
        let first = self.chars[start_idx].1;
        if !is_symbol_start(first) { return String::new(); }
        self.advance(1);
        while let Some(c) = self.peek(0) {
            if is_symbol_cont(c) { self.advance(1); } else { break; }
        }
        self.chars[start_idx..self.idx].iter().map(|(_, c)| *c).collect()
    }

    fn read_dquote_string(&mut self) -> Result<String> {
        self.advance(1);
        let mut out = String::new();
        while let Some(c) = self.peek(0) {
            if c == '"' { self.advance(1); self.check_string_limit(&out)?; return Ok(out); }
            if is_forbidden_ctrl(c) {
                return Err(self.err(format!("control char U+{:04X} not permitted in string", c as u32), "WEFT_SYNTAX_STR_CTRL"));
            }
            if c == '\n' || c == '\t' || c == '\r' {
                return Err(self.err("literal tab/newline/CR not permitted in standard string", "WEFT_SYNTAX_STR_CTRL"));
            }
            if c == '\\' { out.push(self.read_escape()?); continue; }
            out.push(c);
            self.advance(1);
        }
        Err(self.err("unterminated string literal", "WEFT_SYNTAX_STR_UNTERMINATED"))
    }

    fn read_triple_string(&mut self) -> Result<String> {
        self.advance(3);
        let mut out = String::new();
        while let Some(c) = self.peek(0) {
            if c == '"' && self.peek(1) == Some('"') && self.peek(2) == Some('"') {
                self.advance(3);
                self.check_string_limit(&out)?;
                return Ok(out);
            }
            if is_forbidden_ctrl(c) {
                return Err(self.err(format!("control char U+{:04X} not permitted in string", c as u32), "WEFT_SYNTAX_STR_CTRL"));
            }
            if c == '\\' { out.push(self.read_escape()?); continue; }
            out.push(c);
            self.advance(1);
        }
        Err(self.err("unterminated triple-quoted string", "WEFT_SYNTAX_STR_UNTERMINATED"))
    }

    fn read_raw_string(&mut self) -> Result<String> {
        self.advance(1);
        let mut out = String::new();
        while let Some(c) = self.peek(0) {
            if c == '"' { self.advance(1); self.check_string_limit(&out)?; return Ok(out); }
            if is_forbidden_ctrl(c) {
                return Err(self.err(format!("control char U+{:04X} not permitted in raw string", c as u32), "WEFT_SYNTAX_STR_CTRL"));
            }
            out.push(c);
            self.advance(1);
        }
        Err(self.err("unterminated raw string", "WEFT_SYNTAX_STR_UNTERMINATED"))
    }

    fn read_escape(&mut self) -> Result<char> {
        self.advance(1);
        let c = self.peek(0).ok_or_else(|| self.err("truncated escape", "WEFT_SYNTAX_ESCAPE"))?;
        let simple = match c {
            'n' => Some('\n'), 't' => Some('\t'), 'r' => Some('\r'),
            '\\' => Some('\\'), '"' => Some('"'), '0' => Some('\0'),
            _ => None,
        };
        if let Some(ch) = simple { self.advance(1); return Ok(ch); }
        if c == 'x' {
            self.advance(1);
            let h = self.read_hex_digits(2)?;
            let cp = u32::from_str_radix(&h, 16).unwrap();
            if cp > 0x7F {
                return Err(self.err(format!("\\x{} escapes bytes 00-7F only", h), "WEFT_SYNTAX_ESCAPE_XRANGE"));
            }
            return Ok(char::from_u32(cp).unwrap());
        }
        if c == 'u' {
            self.advance(1);
            let hex = if self.peek(0) == Some('{') {
                self.advance(1);
                let h = self.read_hex_until_brace()?;
                self.advance(1); // consume }
                h
            } else {
                self.read_hex_digits(4)?
            };
            let cp = u32::from_str_radix(&hex, 16).map_err(|_| self.err("invalid hex escape", "WEFT_SYNTAX_ESCAPE"))?;
            if cp > 0x10FFFF {
                return Err(self.err(format!("\\u escape U+{:X} exceeds Unicode max", cp), "WEFT_SYNTAX_ESCAPE_MAX"));
            }
            if (0xD800..=0xDFFF).contains(&cp) {
                return Err(self.err(format!("\\u escape U+{:04X} is a surrogate half", cp), "WEFT_SYNTAX_ESCAPE_SURROGATE"));
            }
            return Ok(char::from_u32(cp).unwrap());
        }
        Err(self.err(format!("unknown escape \\{}", c), "WEFT_SYNTAX_ESCAPE_UNKNOWN"))
    }

    fn read_hex_digits(&mut self, n: usize) -> Result<String> {
        let mut s = String::new();
        for _ in 0..n {
            let c = self.peek(0).ok_or_else(|| self.err("truncated hex escape", "WEFT_SYNTAX_ESCAPE"))?;
            if !c.is_ascii_hexdigit() {
                return Err(self.err(format!("invalid hex digit {}", c), "WEFT_SYNTAX_ESCAPE"));
            }
            s.push(c);
            self.advance(1);
        }
        Ok(s)
    }

    fn read_hex_until_brace(&mut self) -> Result<String> {
        let mut s = String::new();
        while let Some(c) = self.peek(0) {
            if c == '}' { break; }
            if !c.is_ascii_hexdigit() {
                return Err(self.err("invalid hex digit in \\u{...}", "WEFT_SYNTAX_ESCAPE"));
            }
            s.push(c);
            self.advance(1);
            if s.len() > 6 {
                return Err(self.err("\\u{...} has more than 6 hex digits", "WEFT_SYNTAX_ESCAPE"));
            }
        }
        if s.is_empty() {
            return Err(self.err("\\u{...} has no hex digits", "WEFT_SYNTAX_ESCAPE"));
        }
        if self.peek(0) != Some('}') {
            return Err(self.err("missing } in \\u{...}", "WEFT_SYNTAX_ESCAPE"));
        }
        Ok(s)
    }

    fn check_string_limit(&self, s: &str) -> Result<()> {
        if s.len() > self.limits.max_string_bytes {
            return Err(self.err(
                format!("string {} bytes exceeds max_string_bytes={}", s.len(), self.limits.max_string_bytes),
                "WEFT_LIMIT_STRING"
            ));
        }
        Ok(())
    }

    fn read_binary(&mut self) -> Result<Binary> {
        self.advance(1);
        let mut body = String::new();
        while let Some(c) = self.peek(0) {
            if c == '"' { break; }
            if is_forbidden_ctrl(c) || c == ' ' || c == '\n' || c == '\t' || c == '\r' {
                return Err(self.err("whitespace/control chars not permitted in binary literal", "WEFT_SYNTAX_BINARY"));
            }
            body.push(c);
            self.advance(1);
        }
        if self.peek(0).is_none() { return Err(self.err("unterminated binary", "WEFT_SYNTAX_BINARY")); }
        self.advance(1);
        if let Some(payload) = body.strip_prefix("base64:") {
            if payload.is_empty() { return Ok(Binary { data: vec![], encoding: BinaryEncoding::Base64 }); }
            if !payload.chars().all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/' || c == '=') {
                return Err(self.err("invalid base64 character", "WEFT_SYNTAX_BINARY_BASE64"));
            }
            if payload.len() % 4 != 0 {
                return Err(self.err("base64 length not multiple of 4", "WEFT_SYNTAX_BINARY_BASE64"));
            }
            use base64::Engine;
            let data = base64::engine::general_purpose::STANDARD.decode(payload)
                .map_err(|e| self.err(format!("invalid base64: {}", e), "WEFT_SYNTAX_BINARY_BASE64"))?;
            // W4-16 / SPEC §5.8: canonical check — reject non-canonical
            // trailing bits (``AB==`` which decodes the same bytes as
            // ``AA==`` would otherwise break equality).
            let re = base64::engine::general_purpose::STANDARD.encode(&data);
            if re != payload {
                return Err(self.err(
                    format!("non-canonical base64 in binary literal (trailing bits must be zero): {:?}", payload),
                    "WEFT_SYNTAX_BINARY_BASE64"));
            }
            return Ok(Binary { data, encoding: BinaryEncoding::Base64 });
        }
        if let Some(payload) = body.strip_prefix("hex:") {
            if payload.len() % 2 != 0 {
                return Err(self.err("hex binary must have even number of digits", "WEFT_SYNTAX_BINARY_HEX"));
            }
            let mut data = Vec::with_capacity(payload.len() / 2);
            let bytes = payload.as_bytes();
            for i in (0..bytes.len()).step_by(2) {
                let hi = (bytes[i] as char).to_digit(16);
                let lo = (bytes[i + 1] as char).to_digit(16);
                match (hi, lo) {
                    (Some(h), Some(l)) => data.push((h * 16 + l) as u8),
                    _ => return Err(self.err("invalid hex digit", "WEFT_SYNTAX_BINARY_HEX")),
                }
            }
            return Ok(Binary { data, encoding: BinaryEncoding::Hex });
        }
        Err(self.err("binary literal must start with 'base64:' or 'hex:'", "WEFT_SYNTAX_BINARY"))
    }

    fn read_datetime(&mut self, sl: usize, sc: usize, so: usize) -> Result<Token> {
        self.advance(1);
        let start = self.idx;
        while let Some(c) = self.peek(0) {
            if is_ws(c) || c == ')' || c == ']' || c == '}' || c == '(' || c == '[' || c == '{' { break; }
            // SPEC §4.3: `"` is a token boundary — a string literal may
            // directly follow a datetime ([#2026-11-02"a"]).
            if c == '"' { break; }
            // ``;`` starts a line comment and ``#|`` starts a block comment —
            // both terminate the datetime, else the body would swallow the
            // comment and parse_datetime fail (``#2026-11-02;hello``).
            if c == ';' { break; }
            if c == '#' && self.peek(1) == Some('|') { break; }
            // Terminate on "::" so a datetime followed by a type annotation lexes correctly.
            if c == ':' && self.peek(1) == Some(':') { break; }
            self.advance(1);
        }
        let body: String = self.chars[start..self.idx].iter().map(|(_, c)| *c).collect();
        let dt = parse_datetime(&body, sl, sc, so)?;
        Ok(Token { kind: TokenKind::DateTime(dt), line: sl, column: sc, offset: so })
    }

    fn read_number_or_sign_lit(&mut self, sl: usize, sc: usize, so: usize) -> Result<Option<Token>> {
        let start = self.idx;
        let mut sign = String::new();
        let mut p = start;
        if matches!(self.chars.get(p).map(|&(_, c)| c), Some('+') | Some('-')) {
            sign.push(self.chars[p].1);
            p += 1;
        }
        // Check signed inf
        let tail: String = self.chars.get(p..p+3).map(|xs| xs.iter().map(|(_, c)| *c).collect()).unwrap_or_default();
        if tail == "inf" {
            let after = self.chars.get(p + 3).map(|&(_, c)| c);
            if after.map_or(true, |c| !is_symbol_cont(c)) {
                let adv = p + 3 - self.idx;
                for _ in 0..adv { self.advance(1); }
                let kind = if sign == "-" { TokenKind::NegInf } else { TokenKind::Inf };
                return Ok(Some(Token { kind, line: sl, column: sc, offset: so }));
            }
        }
        // Must have digit after optional sign
        let c0 = self.chars.get(p).map(|&(_, c)| c);
        if c0.map_or(true, |c| !c.is_ascii_digit()) { return Ok(None); }

        // Hex/Oct/Bin
        if c0 == Some('0') {
            let c1 = self.chars.get(p + 1).map(|&(_, c)| c);
            if matches!(c1, Some('x') | Some('X') | Some('o') | Some('O') | Some('b') | Some('B')) {
                let base_char = c1.unwrap().to_ascii_lowercase();
                p += 2;
                let digit_start = p;
                let is_allowed = |c: char| match base_char {
                    'x' => c.is_ascii_hexdigit() || c == '_',
                    'o' => ('0'..='7').contains(&c) || c == '_',
                    'b' => c == '0' || c == '1' || c == '_',
                    _ => false,
                };
                while let Some(&(_, c)) = self.chars.get(p) { if is_allowed(c) { p += 1; } else { break; } }
                let digits_text: String = self.chars[digit_start..p].iter().map(|(_, c)| *c).collect();
                validate_underscores(&digits_text, sl, sc, so)?;
                let cleaned: String = digits_text.chars().filter(|&c| c != '_').collect();
                if cleaned.is_empty() {
                    return Err(WeftError::lex(format!("base-{} integer has no digits", base_char), "WEFT_SYNTAX_INT").with_pos(sl, sc, so));
                }
                let base = match base_char { 'x' => 16, 'o' => 8, _ => 2 };
                // Check against decimal-equivalent digit count so a 4000-
                // digit ``0xf…f`` cannot sneak past max_int_digits only to
                // blow it up when the formatter emits the same value in
                // base 10.
                self.check_int_digits_base(&cleaned, sl, sc, so, base)?;
                let mut big = BigInt::from_str_radix(&cleaned, base).map_err(|_| WeftError::lex("invalid integer", "WEFT_SYNTAX_INT").with_pos(sl, sc, so))?;
                if sign == "-" { big = -big; }
                // n suffix?
                if self.chars.get(p).map(|&(_, c)| c) == Some('n') {
                    p += 1;
                    let adv = p - self.idx;
                    for _ in 0..adv { self.advance(1); }
                    if let Some(&(_, c)) = self.chars.get(self.idx) {
                        if is_symbol_cont(c) {
                            return Err(self.err("unexpected char after 'n' suffix", "WEFT_SYNTAX_INT"));
                        }
                    }
                    return Ok(Some(Token { kind: TokenKind::BigInt(BigIntValue(big)), line: sl, column: sc, offset: so }));
                }
                let adv = p - self.idx;
                for _ in 0..adv { self.advance(1); }
                if let Some(&(_, c)) = self.chars.get(self.idx) {
                    if is_symbol_cont(c) {
                        return Err(self.err("unexpected char after integer; only 'n' (BigInt) and 'm' (Decimal) suffixes are supported", "WEFT_RESERVED_NUMBER_SUFFIX"));
                    }
                }
                return Ok(Some(Token { kind: TokenKind::Int(big), line: sl, column: sc, offset: so }));
            }
        }

        // Decimal number
        let int_start = p;
        while let Some(&(_, c)) = self.chars.get(p) {
            if c.is_ascii_digit() || c == '_' { p += 1; } else { break; }
        }
        let int_text: String = self.chars[int_start..p].iter().map(|(_, c)| *c).collect();
        validate_underscores(&int_text, sl, sc, so)?;
        let int_cleaned: String = int_text.chars().filter(|&c| c != '_').collect();
        if int_cleaned.is_empty() {
            return Err(WeftError::lex("number has no digits", "WEFT_SYNTAX_INT").with_pos(sl, sc, so));
        }

        let mut is_float = false;
        let mut frac_cleaned = String::new();
        if self.chars.get(p).map(|&(_, c)| c) == Some('.') {
            let next = self.chars.get(p + 1).map(|&(_, c)| c);
            if next.map_or(true, |c| !c.is_ascii_digit()) {
                return Err(WeftError::lex("float literal must have digits after '.'", "WEFT_SYNTAX_FLOAT_DOT").with_pos(sl, sc, so));
            }
            is_float = true;
            p += 1;
            let frac_start = p;
            while let Some(&(_, c)) = self.chars.get(p) { if c.is_ascii_digit() || c == '_' { p += 1; } else { break; } }
            let frac_text: String = self.chars[frac_start..p].iter().map(|(_, c)| *c).collect();
            validate_underscores(&frac_text, sl, sc, so)?;
            frac_cleaned = frac_text.chars().filter(|&c| c != '_').collect();
        }

        let mut exp_text = String::new();
        let mut exp_sign = String::new();
        if matches!(self.chars.get(p).map(|&(_, c)| c), Some('e') | Some('E')) {
            is_float = true;
            p += 1;
            if matches!(self.chars.get(p).map(|&(_, c)| c), Some('+') | Some('-')) {
                exp_sign.push(self.chars[p].1);
                p += 1;
            }
            let exp_start = p;
            while let Some(&(_, c)) = self.chars.get(p) { if c.is_ascii_digit() || c == '_' { p += 1; } else { break; } }
            let exp_raw: String = self.chars[exp_start..p].iter().map(|(_, c)| *c).collect();
            validate_underscores(&exp_raw, sl, sc, so)?;
            exp_text = exp_raw.chars().filter(|&c| c != '_').collect();
            if exp_text.is_empty() {
                return Err(WeftError::lex("exponent has no digits", "WEFT_SYNTAX_FLOAT_EXP").with_pos(sl, sc, so));
            }
            // Exponent digit count also bounded by max_int_digits (DoS prevention).
            self.check_int_digits(&exp_text, sl, sc, so)?;
        }

        let mut suffix = ' ';
        if matches!(self.chars.get(p).map(|&(_, c)| c), Some('n') | Some('m')) {
            suffix = self.chars[p].1;
            p += 1;
            if let Some(&(_, c)) = self.chars.get(p) {
                if is_symbol_cont(c) {
                    let adv = p - self.idx;
                    for _ in 0..adv { self.advance(1); }
                    return Err(self.err(format!("unexpected char after '{}' suffix", suffix), "WEFT_SYNTAX_NUMBER_SUFFIX"));
                }
            }
            if suffix == 'n' && is_float {
                let adv = p - self.idx;
                for _ in 0..adv { self.advance(1); }
                return Err(WeftError::lex("'n' suffix is only valid on integer literals", "WEFT_SYNTAX_NUMBER_SUFFIX").with_pos(sl, sc, so));
            }
        } else if let Some(&(_, c)) = self.chars.get(p) {
            if is_symbol_cont(c) {
                let adv = p - self.idx;
                for _ in 0..adv { self.advance(1); }
                return Err(self.err("unexpected char after number; only 'n' (BigInt) and 'm' (Decimal) suffixes supported", "WEFT_RESERVED_NUMBER_SUFFIX"));
            }
        }

        let total: String = int_cleaned.clone() + &frac_cleaned;
        self.check_int_digits(&total, sl, sc, so)?;

        let adv = p - self.idx;
        for _ in 0..adv { self.advance(1); }

        if suffix == 'n' {
            let mut big = BigInt::from_str_radix(&int_cleaned, 10).unwrap();
            if sign == "-" { big = -big; }
            return Ok(Some(Token { kind: TokenKind::BigInt(BigIntValue(big)), line: sl, column: sc, offset: so }));
        }
        if suffix == 'm' {
            // SPEC §7.2: '+' is not part of the canonical Decimal text.
            let sign_norm = if sign == "+" { String::new() } else { sign.clone() };
            let s_text = sign_norm + &int_cleaned
                + &(if !frac_cleaned.is_empty() { format!(".{}", frac_cleaned) } else { String::new() })
                + &(if !exp_text.is_empty() { format!("e{}{}", exp_sign, exp_text) } else { String::new() });
            return Ok(Some(Token { kind: TokenKind::Decimal(Decimal(s_text)), line: sl, column: sc, offset: so }));
        }
        if is_float {
            let s_text = sign.clone() + &int_cleaned
                + &(if !frac_cleaned.is_empty() { format!(".{}", frac_cleaned) } else { String::new() })
                + &(if !exp_text.is_empty() { format!("e{}{}", exp_sign, exp_text) } else { String::new() });
            let f: f64 = s_text.parse().map_err(|_| WeftError::lex(format!("invalid float {}", s_text), "WEFT_SYNTAX_FLOAT").with_pos(sl, sc, so))?;
            return Ok(Some(Token { kind: TokenKind::Float(f), line: sl, column: sc, offset: so }));
        }
        let mut big = BigInt::from_str_radix(&int_cleaned, 10).unwrap();
        if sign == "-" { big = -big; }
        Ok(Some(Token { kind: TokenKind::Int(big), line: sl, column: sc, offset: so }))
    }

    fn check_int_digits(&self, cleaned: &str, sl: usize, sc: usize, so: usize) -> Result<()> {
        self.check_int_digits_base(cleaned, sl, sc, so, 10)
    }

    fn check_int_digits_base(&self, cleaned: &str, sl: usize, sc: usize, so: usize, base: u32) -> Result<()> {
        // SPEC §9.5: ``max_int_digits`` is a decimal-digit budget. For
        // non-decimal bases, compare against the decimal-equivalent length
        // — ceil(len * log10(base)) is a tight upper bound.
        let dec_len = match base {
            10 => cleaned.len(),
            2  => ((cleaned.len() as f64) * 0.30102999566398114).ceil() as usize,
            8  => ((cleaned.len() as f64) * 0.9030899869919435).ceil() as usize,
            16 => ((cleaned.len() as f64) * 1.2041199826559248).ceil() as usize,
            _  => cleaned.len(),
        };
        if dec_len > self.limits.max_int_digits {
            let msg = if base == 10 {
                format!("integer has {} digits, exceeds max_int_digits={}", dec_len, self.limits.max_int_digits)
            } else {
                format!("integer has {} decimal digits ({} base-{} digits), exceeds max_int_digits={}",
                    dec_len, cleaned.len(), base, self.limits.max_int_digits)
            };
            return Err(WeftError::limit(msg, "WEFT_LIMIT_INT").with_pos(sl, sc, so));
        }
        Ok(())
    }
}

fn pos_from_byte_offset(text: &str, offset: usize) -> (usize, usize) {
    let mut line = 1usize;
    let mut col = 1usize;
    for (i, c) in text.char_indices() {
        if i >= offset { break; }
        if c == '\n' { line += 1; col = 1; } else { col += 1; }
    }
    (line, col)
}

fn is_ws(c: char) -> bool { matches!(c, '\t' | '\n' | '\r' | ' ' | ',') }

fn is_rejected_ws(c: char) -> bool {
    let cp = c as u32;
    if is_ws(c) { return false; }
    if cp == 0x0B || cp == 0x0C { return true; }
    if cp == 0xA0 { return true; }
    if (0x2000..=0x200A).contains(&cp) { return true; }
    matches!(cp, 0x2028 | 0x2029 | 0x205F | 0x3000)
}

fn is_forbidden_ctrl(c: char) -> bool {
    let cp = c as u32;
    (cp <= 0x08) || cp == 0x0B || cp == 0x0C || (0x0E..=0x1F).contains(&cp)
}

/// Unicode bidirectional override / isolate controls (CVE-2021-42574).
/// Rejected in comments.
fn is_bidi_control(c: char) -> bool {
    let cp = c as u32;
    (0x202A..=0x202E).contains(&cp) || (0x2066..=0x2069).contains(&cp)
}

fn is_comment_forbidden(c: char) -> bool {
    // SPEC §4.2 / §10.5: comment-forbidden characters are
    //   C0 controls except TAB/LF/CR
    //   DEL (U+007F)
    //   C1 controls (U+0080–U+009F, including CSI U+009B)
    //   Bidi override/isolate (U+202A–U+202E, U+2066–U+2069)
    let cp = c as u32;
    is_forbidden_ctrl(c)
        || is_bidi_control(c)
        || cp == 0x7F
        || (0x80..=0x9F).contains(&cp)
}

fn is_symbol_start(c: char) -> bool {
    if matches!(c,
        'A'..='Z' | 'a'..='z' | '_' | '+' | '-' | '*' | '/' | '<' | '>' | '=' | '!' | '?'
    ) { return true; }
    if (c as u32) > 0x7F && is_unicode_letter_only(c) { return true; }
    false
}

fn is_symbol_cont(c: char) -> bool {
    if is_symbol_start(c) || c.is_ascii_digit() { return true; }
    // Above ASCII: Unicode General Category L* only. Unicode digits
    // (Nd — fullwidth '１' / Arabic-Indic '١'), letter numbers (Nl —
    // Roman 'Ⅰ'), and other numbers (No — circled '①', fractions '¼')
    // are NOT symbol-cont — SPEC §5.10.
    if (c as u32) > 0x7F && is_unicode_letter_only(c) { return true; }
    false
}

/// SPEC §5.10: Unicode General Category L* only. Rust's
/// ``char::is_alphabetic`` matches the Alphabetic *property* (a superset
/// of L that includes Nl); subtracting ``char::is_numeric`` (Nd/Nl/No)
/// pins acceptance to L.
fn is_unicode_letter_only(c: char) -> bool {
    // SPEC §5.10: General Category L* ONLY (Lu/Ll/Lt/Lm/Lo). The previous
    // ``is_alphabetic && !is_numeric`` under-filtered: Other_Alphabetic
    // combining marks (e.g. U+0345, General Category Mn) passed, which the
    // other three implementations reject. Use the real category table.
    use unicode_general_category::{get_general_category, GeneralCategory as G};
    matches!(get_general_category(c),
        G::UppercaseLetter | G::LowercaseLetter | G::TitlecaseLetter
        | G::ModifierLetter | G::OtherLetter)
}

fn validate_underscores(text: &str, line: usize, col: usize, off: usize) -> Result<()> {
    if text.contains("__") {
        return Err(WeftError::lex("consecutive underscores not permitted", "WEFT_SYNTAX_NUMBER_UNDERSCORE").with_pos(line, col, off));
    }
    if text.starts_with('_') || text.ends_with('_') {
        return Err(WeftError::lex("underscore cannot be first or last digit", "WEFT_SYNTAX_NUMBER_UNDERSCORE").with_pos(line, col, off));
    }
    Ok(())
}

fn parse_datetime(body: &str, line: usize, col: usize, offset: usize) -> Result<WeftDate> {
    // Validate ASCII before any byte-indexed slicing — otherwise a
    // multi-byte UTF-8 character at a shape-defined position would
    // panic from a non-boundary ``&body[0..4]``.
    let err = |msg: &str, code: &str| WeftError::lex(msg.to_string(), code).with_pos(line, col, offset);
    if !body.is_ascii() {
        return Err(err(&format!("invalid datetime: {} (non-ASCII)", body), "WEFT_SYNTAX_DATETIME"));
    }
    let bytes = body.as_bytes();
    // Format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS[.fff...][Z or +HH:MM]
    if body.len() < 10 {
        return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME"));
    }
    let is_d = |b: u8| b.is_ascii_digit();
    for i in [0, 1, 2, 3, 5, 6, 8, 9] {
        if !is_d(bytes[i]) {
            return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME"));
        }
    }
    if bytes[4] != b'-' || bytes[7] != b'-' {
        return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME"));
    }
    let year: u16 = body[0..4].parse().unwrap();
    let month: u8 = body[5..7].parse().unwrap();
    let day: u8 = body[8..10].parse().unwrap();
    if year < 1 { return Err(err("year out of range", "WEFT_SYNTAX_DATETIME_YEAR")); }
    if !(1..=12).contains(&month) { return Err(err("invalid month", "WEFT_SYNTAX_DATETIME")); }
    let max_day = days_in_month(year, month);
    if !(1..=max_day).contains(&day) { return Err(err("invalid day", "WEFT_SYNTAX_DATETIME")); }

    if body.len() == 10 {
        return Ok(WeftDate { year, month, day, has_time: false, hour: 0, minute: 0, second: 0, nano: 0, tz_offset_minutes: None });
    }
    if bytes[10] != b'T' || body.len() < 19 {
        return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME"));
    }
    // Validate strict 2-digit ASCII at each slot before parsing. Without
    // this ``T+1:00:00Z`` was being accepted (``"+1".parse::<u8>()`` =
    // Ok(1)) and the sign silently dropped.
    for i in [11, 12, 14, 15, 17, 18] {
        if !is_d(bytes[i]) {
            return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME"));
        }
    }
    if bytes[13] != b':' || bytes[16] != b':' { return Err(err(&format!("invalid datetime: {}", body), "WEFT_SYNTAX_DATETIME")); }
    let hour: u8 = body[11..13].parse().unwrap();
    let minute: u8 = body[14..16].parse().unwrap();
    let second: u8 = body[17..19].parse().unwrap();
    if hour > 23 || minute > 59 || second > 60 { return Err(err("time out of range", "WEFT_SYNTAX_DATETIME")); }
    let mut pos = 19usize;
    let mut nano: u32 = 0;
    if pos < body.len() && bytes[pos] == b'.' {
        pos += 1;
        let frac_start = pos;
        while pos < body.len() && bytes[pos].is_ascii_digit() { pos += 1; }
        let frac = &body[frac_start..pos];
        if frac.is_empty() || frac.len() > 9 {
            return Err(err("invalid datetime fraction", "WEFT_SYNTAX_DATETIME"));
        }
        let mut padded = frac.to_string();
        while padded.len() < 9 { padded.push('0'); }
        nano = padded.parse().map_err(|_| err("bad fraction", "WEFT_SYNTAX_DATETIME"))?;
    }
    let tz = if pos < body.len() {
        let tzs = &body[pos..];
        if tzs == "Z" { Some(0i16) }
        else {
            // Strict ±HH:MM: six ASCII chars, sign+digit+digit+colon+digit+digit.
            // Without the per-byte digit check ``+-1:00`` or ``+-:00`` was
            // accepted because ``"-1".parse::<i16>()`` succeeds as -1.
            let tbytes = tzs.as_bytes();
            if tbytes.len() != 6
                || (tbytes[0] != b'+' && tbytes[0] != b'-')
                || !is_d(tbytes[1]) || !is_d(tbytes[2])
                || tbytes[3] != b':'
                || !is_d(tbytes[4]) || !is_d(tbytes[5])
            {
                return Err(err(&format!("invalid datetime zone: {}", tzs), "WEFT_SYNTAX_DATETIME"));
            }
            let sign: i16 = if tbytes[0] == b'+' { 1 } else { -1 };
            let hh: i16 = tzs[1..3].parse().unwrap();
            let mm: i16 = tzs[4..6].parse().unwrap();
            if hh > 14 || mm > 59 || (hh == 14 && mm > 0) {
                return Err(err(&format!("tz offset {} out of range (|offset| <= 14:00)", tzs), "WEFT_SYNTAX_DATETIME"));
            }
            Some(sign * (hh * 60 + mm))
        }
    } else { None };
    // Leap seconds (:60) only occur at 23:59:60 UTC. In local time they
    // appear at the hour / minute that maps back to 23:59 UTC after
    // applying the stated offset. ``08:59:60+09:00`` is thus legal.
    if second == 60 {
        let off = tz.unwrap_or(0) as i32;
        let mut total = hour as i32 * 60 + minute as i32 - off;
        total = ((total % 1440) + 1440) % 1440;
        if total != 23 * 60 + 59 {
            let zone_label: String = match tz {
                Some(0) => "Z".to_string(),
                Some(_) => {
                    // Reconstruct from the original zone slice if we have it.
                    body.get(pos..).unwrap_or("local").to_string()
                }
                None => "local".to_string(),
            };
            return Err(err(&format!(
                "leap-second :60 only valid at 23:59:60 UTC (got {:02}:{:02}:60 at offset {})",
                hour, minute, zone_label), "WEFT_SYNTAX_DATETIME"));
        }
    }
    Ok(WeftDate { year, month, day, has_time: true, hour, minute, second, nano, tz_offset_minutes: tz })
}

fn days_in_month(y: u16, m: u8) -> u8 {
    const DAYS: [u8; 12] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut d = DAYS[(m - 1) as usize];
    if m == 2 && is_leap(y) { d = 29; }
    d
}

fn is_leap(y: u16) -> bool { (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 }
