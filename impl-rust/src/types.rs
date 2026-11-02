//! Weft value types.
//!
//! `Value` is a tagged enum covering every category in SPEC §5. BigInt is
//! represented via `num_bigint::BigInt`; Decimal preserves the original
//! textual form; Binary stores both bytes and the original encoding tag.

use indexmap::IndexMap;
use num_bigint::BigInt;
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Symbol(pub String);

impl Symbol {
    /// Construct and validate a Symbol name per SPEC §5.10.
    pub fn new(name: impl Into<String>) -> Result<Self, String> {
        let name = name.into();
        validate_identifier_name(&name, "Symbol")?;
        Ok(Self(name))
    }
}

impl fmt::Display for Symbol {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Keyword(pub String);

impl Keyword {
    /// Construct and validate a Keyword name per SPEC §5.10.
    pub fn new(name: impl Into<String>) -> Result<Self, String> {
        let name = name.into();
        validate_identifier_name(&name, "Keyword")?;
        Ok(Self(name))
    }
}

impl fmt::Display for Keyword {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, ":{}", self.0)
    }
}

const RESERVED_SYMBOL_LITERALS: &[&str] = &["true", "false", "nil", "nan", "inf", "-inf", "+inf"];

fn validate_identifier_name(name: &str, label: &str) -> Result<(), String> {
    if name.is_empty() {
        return Err(format!("{} name must be a non-empty string", label));
    }
    let mut iter = name.chars();
    let first = iter.next().unwrap();
    if !is_sym_start(first) {
        return Err(format!("{} name {:?}: invalid first character {:?}", label, name, first));
    }
    for (i, c) in iter.enumerate() {
        if !is_sym_cont(c) {
            return Err(format!("{} name {:?}: invalid character {:?} at index {}", label, name, c, i + 1));
        }
    }
    if label == "Symbol" && RESERVED_SYMBOL_LITERALS.contains(&name) {
        return Err(format!("{} name {:?} is reserved as a literal", label, name));
    }
    let bytes = name.as_bytes();
    if bytes.len() >= 2 && (bytes[0] == b'+' || bytes[0] == b'-') && bytes[1].is_ascii_digit() {
        return Err(format!(
            "{} name {:?} starts with sign+digit, which ambiguates with a signed number literal",
            label, name));
    }
    Ok(())
}

fn is_sym_start(c: char) -> bool {
    matches!(c, 'A'..='Z' | 'a'..='z' | '_' | '+' | '-' | '*' | '/' | '<' | '>' | '=' | '!' | '?')
        || ((c as u32) > 0x7F && is_unicode_letter_only(c))
}

fn is_sym_cont(c: char) -> bool {
    is_sym_start(c) || c.is_ascii_digit()
        || ((c as u32) > 0x7F && is_unicode_letter_only(c))
}

/// SPEC §5.10: Unicode General Category L* only — explicitly NOT Nd
/// (decimal digits like U+FF11), Nl (letter numbers like Roman U+2160),
/// or No (other numbers like U+2460). Rust's ``char::is_alphabetic`` uses
/// the Alphabetic *property* which is a superset of L (includes Nl); we
/// subtract ``char::is_numeric`` (which spans Nd/Nl/No) to pin acceptance
/// to L only.
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

pub fn is_valid_keyword_name(name: &str) -> bool {
    validate_identifier_name(name, "Keyword").is_ok()
}

/// Integer that was written with the `n` suffix.
///
/// Semantically equivalent to a plain `Value::Int`, but the tag lets a
/// formatter re-emit the `n` suffix.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct BigIntValue(pub BigInt);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum BinaryEncoding {
    Base64,
    Hex,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Binary {
    pub data: Vec<u8>,
    pub encoding: BinaryEncoding,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Decimal(pub String);

impl Decimal {
    /// Construct and validate a Decimal text per SPEC §5.5. The exponent
    /// marker is canonicalised to lower-case so ``1E2`` and ``1e2`` share
    /// ``.0``.
    pub fn new(text: impl Into<String>) -> Result<Self, String> {
        let text = text.into();
        validate_decimal_text(&text)?;
        // SPEC §7.2 canonicalisation at construction time: leading '+'
        // stripped (matches the Integer rule), exponent marker lowercased.
        // §5.16 defines Decimal equality on this normalised text.
        let text = text.strip_prefix('+').map(str::to_string).unwrap_or(text);
        let normalised = if text.contains('E') { text.replace('E', "e") } else { text };
        Ok(Self(normalised))
    }
}

fn validate_decimal_text(text: &str) -> Result<(), String> {
    // Forbid newlines outright (anchors ``\A``/``\Z`` cover trailing ``\n``
    // across regex engines).
    if text.is_empty() || text.contains('\n') || text.contains('\r') {
        return Err(format!("invalid decimal text: {:?}", text));
    }
    let bytes = text.as_bytes();
    let mut i = 0usize;
    if bytes[0] == b'+' || bytes[0] == b'-' { i += 1; }
    // integer part
    let int_start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() { i += 1; }
    if i == int_start { return Err(format!("invalid decimal text: {:?}", text)); }
    // optional fractional part
    if i < bytes.len() && bytes[i] == b'.' {
        i += 1;
        let frac_start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() { i += 1; }
        if i == frac_start { return Err(format!("invalid decimal text: {:?}", text)); }
    }
    // optional exponent
    if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
        i += 1;
        if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') { i += 1; }
        let exp_start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() { i += 1; }
        if i == exp_start { return Err(format!("invalid decimal text: {:?}", text)); }
    }
    if i != bytes.len() {
        return Err(format!("invalid decimal text: {:?}", text));
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct WeftDate {
    pub year: u16,
    pub month: u8,
    pub day: u8,
    pub has_time: bool,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub nano: u32,
    pub tz_offset_minutes: Option<i16>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Sexpr {
    pub tag: Symbol,
    pub items: Vec<Value>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Typed {
    pub value: Value,
    pub type_: TypeExpr,
}

#[derive(Debug, Clone, PartialEq)]
pub enum TypeExpr {
    Name(Symbol),
    Form(Sexpr),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(BigInt),
    BigInt(BigIntValue),
    Float(f64),
    Decimal(Decimal),
    String(String),
    Binary(Binary),
    DateTime(WeftDate),
    Symbol(Symbol),
    Keyword(Keyword),
    List(Vec<Value>),
    Map(IndexMap<String, Value>),
    Sexpr(Sexpr),
    Typed(Box<Typed>),
}

impl Value {
    pub fn is_scalar(&self) -> bool {
        matches!(self,
            Value::Null | Value::Bool(_) | Value::Int(_) | Value::BigInt(_) |
            Value::Float(_) | Value::Decimal(_) | Value::String(_) |
            Value::Binary(_) | Value::DateTime(_) | Value::Symbol(_) |
            Value::Keyword(_)
        )
    }
}

// ---------------------------------------------------------------------------
// Equality (SPEC §5.16)
// ---------------------------------------------------------------------------

/// Weft value equality ``a ≡ b`` per SPEC §5.16. Iterative (explicit work
/// stack) so adversarially deep trees cannot exhaust the host call stack.
///
/// - Integer/BigInt compare numerically across the two shapes (42 ≡ 42n).
/// - Float equality is IEEE-754 bit identity, except any NaN is unequal to
///   everything including itself; -0.0 ≢ 0.0.
/// - Decimal equality is normalised-text equality (the constructor strips a
///   leading '+' and lowercases the exponent marker).
/// - Binary compares content bytes; base64/hex is presentation only.
/// - Map equality is unordered; List/Sexpr are ordered.
pub fn equals(a: &Value, b: &Value) -> bool {
    let mut stack: Vec<(&Value, &Value)> = vec![(a, b)];
    while let Some((x, y)) = stack.pop() {
        match (x, y) {
            (Value::Null, Value::Null) => {}
            (Value::Bool(p), Value::Bool(q)) => { if p != q { return false; } }
            (Value::Int(p), Value::Int(q)) => { if p != q { return false; } }
            (Value::BigInt(p), Value::BigInt(q)) => { if p.0 != q.0 { return false; } }
            (Value::Int(p), Value::BigInt(q)) | (Value::BigInt(q), Value::Int(p)) => {
                if *p != q.0 { return false; }
            }
            (Value::Float(p), Value::Float(q)) => {
                if p.is_nan() || q.is_nan() { return false; } // NaN ≢ NaN
                if p.to_bits() != q.to_bits() { return false; }
            }
            (Value::Decimal(p), Value::Decimal(q)) => { if p.0 != q.0 { return false; } }
            (Value::String(p), Value::String(q)) => { if p != q { return false; } }
            (Value::Binary(p), Value::Binary(q)) => { if p.data != q.data { return false; } }
            (Value::DateTime(p), Value::DateTime(q)) => { if p != q { return false; } }
            (Value::Symbol(p), Value::Symbol(q)) => { if p.0 != q.0 { return false; } }
            (Value::Keyword(p), Value::Keyword(q)) => { if p.0 != q.0 { return false; } }
            (Value::List(p), Value::List(q)) => {
                if p.len() != q.len() { return false; }
                for (i, j) in p.iter().zip(q.iter()) { stack.push((i, j)); }
            }
            (Value::Map(p), Value::Map(q)) => {
                if p.len() != q.len() { return false; }
                for (k, v) in p {
                    match q.get(k) { Some(w) => stack.push((v, w)), None => return false }
                }
            }
            (Value::Sexpr(p), Value::Sexpr(q)) => {
                if p.tag.0 != q.tag.0 || p.items.len() != q.items.len() { return false; }
                for (i, j) in p.items.iter().zip(q.items.iter()) { stack.push((i, j)); }
            }
            (Value::Typed(p), Value::Typed(q)) => {
                stack.push((&p.value, &q.value));
                match (&p.type_, &q.type_) {
                    (TypeExpr::Name(s1), TypeExpr::Name(s2)) => { if s1.0 != s2.0 { return false; } }
                    (TypeExpr::Form(s1), TypeExpr::Form(s2)) => {
                        if s1.tag.0 != s2.tag.0 || s1.items.len() != s2.items.len() { return false; }
                        for (i, j) in s1.items.iter().zip(s2.items.iter()) { stack.push((i, j)); }
                    }
                    _ => return false,
                }
            }
            _ => return false,
        }
    }
    true
}
