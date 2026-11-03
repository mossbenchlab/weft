//! Weft formatter (Rust).

use crate::types::{TypeExpr, Value, WeftDate};
use base64::Engine;
use std::fmt::Write;

#[derive(Debug, Clone, Copy)]
pub struct FormatOptions {
    pub max_width: usize,
    pub indent: usize,
    pub sort_keys: bool,
    pub strict_canonical: bool,
}

impl Default for FormatOptions {
    fn default() -> Self {
        Self { max_width: 80, indent: 2, sort_keys: false, strict_canonical: false }
    }
}

pub fn format_value(value: &Value, options: FormatOptions) -> String {
    let mut opts = options;
    if opts.strict_canonical {
        // SPEC §7.3: strict canonical is a single-line, unbounded-width
        // layout. Setting max_width to an effectively infinite value
        // forces format_rec to take the inline branch rather than
        // falling into format_*_multi.
        opts.sort_keys = true;
        opts.max_width = usize::MAX / 4;
    }
    let mut out = format_rec(value, 0, opts);
    if !out.ends_with('\n') { out.push('\n'); }
    out
}

pub fn format_inline(value: &Value, options: FormatOptions) -> Result<String, FormatError> {
    // Mirror format_value: strict_canonical forces sort_keys so a map
    // rendered inline matches the same map rendered by format_value.
    let mut opts = options;
    if opts.strict_canonical { opts.sort_keys = true; }
    inline_rec(value, opts)
}

#[derive(Debug)]
pub enum FormatError {
    MultilineNeeded,
    UnrepresentableValue(String),
}

fn format_rec(value: &Value, depth: usize, opts: FormatOptions) -> String {
    // Peel off any Typed wrapper(s) iteratively — a deep type-annotation
    // chain cannot blow the recursion stack. Innermost type appears first
    // in source, outermost last; peeling from outside gives the chain in
    // reverse order, so we reverse when joining.
    // Type expressions render in strict single-line mode so a newline
    // string inside the type doesn't surface as MultilineNeeded and
    // crash the whole value. Also propagate the type-expression result
    // instead of ``unwrap_or_default`` which would silently drop the
    // type on any error (noted by the external reviewer as information
    // loss, not an exception).
    let strict_opts = FormatOptions { strict_canonical: true, ..opts };
    let mut inner_val: &Value = value;
    let mut type_chain: Vec<String> = Vec::new();
    while let Value::Typed(t) = inner_val {
        match inline_type_expr(&t.type_, strict_opts) {
            Ok(s) => type_chain.push(s),
            Err(_) => type_chain.push("<unformatable>".into()),
        }
        inner_val = &t.value;
    }
    let type_suffix = if type_chain.is_empty() {
        String::new()
    } else {
        type_chain.reverse();
        format!("::{}", type_chain.join("::"))
    };

    let oneline = inline_rec(inner_val, opts).ok();
    if let Some(s) = &oneline {
        // SPEC §7.2: the 80-column soft-wrap counts Unicode CODE POINTS,
        // not UTF-8 bytes (which over-count CJK three-fold).
        if depth * opts.indent + s.chars().count() + type_suffix.chars().count() <= opts.max_width && !s.contains('\n') {
            return format!("{}{}", s, type_suffix);
        }
    }
    let body = match inner_val {
        Value::List(xs) => format_list_multi(xs, depth, opts),
        Value::Map(m) => format_map_multi(m, depth, opts),
        Value::Sexpr(s) => format_sexpr_multi(s, depth, opts),
        // SPEC §7.2: strings always use one-line dquote-escape form,
        // never triple-quoted (prevents the W4-04 re-indent hazard).
        _ => oneline.unwrap_or_else(String::new),
    };
    format!("{}{}", body, type_suffix)
}

fn inline_rec(value: &Value, opts: FormatOptions) -> Result<String, FormatError> {
    match value {
        Value::Null => Ok("nil".into()),
        Value::Bool(true) => Ok("true".into()),
        Value::Bool(false) => Ok("false".into()),
        Value::Int(b) => Ok(b.to_string()),
        Value::BigInt(b) => Ok(format!("{}n", b.0)),
        Value::Float(f) => Ok(format_float(*f)),
        Value::Decimal(d) => {
            // SPEC §7.2: Decimal canonicalisation strips a leading ``+``
            // and lowercases the exponent marker. Significant digits are
            // preserved (``1m`` vs ``1.0m`` are distinct values).
            let mut text = d.0.clone();
            if text.starts_with('+') { text.remove(0); }
            text = text.replace('E', "e");
            Ok(format!("{}m", text))
        }
        Value::String(s) => format_string_dquote(s, opts),
        Value::Binary(b) => {
            // SPEC §5.8 and §7.2: canonical form always emits base64,
            // regardless of the input encoding. Equality is defined on
            // the byte sequence, so the two spellings are the same
            // abstract value and canonical output picks one.
            let _ = b.encoding;
            Ok(format!("b\"base64:{}\"",
                base64::engine::general_purpose::STANDARD.encode(&b.data)))
        }
        Value::DateTime(d) => Ok(format_date(d)),
        Value::Symbol(s) => Ok(s.0.clone()),
        Value::Keyword(k) => Ok(format!(":{}", k.0)),
        Value::Typed(_) => {
            // Peel iteratively; join innermost-first (reverse of the
            // outside-in peel order). Type expressions use strict single-
            // line mode so a newline string inside the type doesn't throw
            // MultilineNeeded.
            let strict_opts = FormatOptions { strict_canonical: true, ..opts };
            let mut cur: &Value = value;
            let mut chain: Vec<String> = Vec::new();
            while let Value::Typed(t) = cur {
                chain.push(inline_type_expr(&t.type_, strict_opts)?);
                cur = &t.value;
            }
            let base = inline_rec(cur, opts)?;
            chain.reverse();
            Ok(format!("{}::{}", base, chain.join("::")))
        }
        Value::List(xs) => {
            let parts: Vec<String> = xs.iter().map(|x| inline_rec(x, opts)).collect::<Result<_, _>>()?;
            Ok(format!("[{}]", parts.join(" ")))
        }
        Value::Map(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            if opts.sort_keys { keys.sort(); }
            let mut parts = Vec::with_capacity(keys.len());
            for k in keys {
                let v = &m[k];
                parts.push(format!(":{} {}", k, inline_rec(v, opts)?));
            }
            Ok(format!("{{{}}}", parts.join(" ")))
        }
        Value::Sexpr(s) => {
            let mut parts = vec![s.tag.0.clone()];
            for it in &s.items { parts.push(inline_rec(it, opts)?); }
            Ok(format!("({})", parts.join(" ")))
        }
    }
}

fn inline_type_expr(t: &TypeExpr, opts: FormatOptions) -> Result<String, FormatError> {
    match t {
        TypeExpr::Name(s) => Ok(s.0.clone()),
        TypeExpr::Form(sx) => {
            let mut parts = vec![sx.tag.0.clone()];
            for it in &sx.items { parts.push(inline_rec(it, opts)?); }
            Ok(format!("({})", parts.join(" ")))
        }
    }
}

pub fn format_float(f: f64) -> String {
    if f.is_nan() { return "nan".into(); }
    if f.is_infinite() { return if f < 0.0 { "-inf".into() } else { "inf".into() }; }
    // Preserve the sign of zero (SPEC §7.3).
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let neg = f < 0.0;
    let (digits, exp10) = canonical_digits(f.abs());
    render_float(neg, &digits, exp10)
}

// ---------------------------------------------------------------------------
// Strict-canonical float formatting (SPEC §7.3)
//
// Shortest decimal that round-trips, with the tie rule pinned: when two
// equal-length decimal strings both round-trip to `f`, pick the one whose
// exact decimal value is CLOSEST to the exact binary value of `f`; if they
// are exactly equidistant, pick the even last digit. This matches what
// CPython (repr), V8 (toString/toExponential) and Go (strconv -1 precision)
// produce, verified by differential fuzzing; Rust's std Display uses a
// different tie choice in rare cases, so we post-process its digits.
// ---------------------------------------------------------------------------

/// Decompose the shortest representation of a positive finite f64 into
/// (digits, exp10) where value = 0.d1d2... * 10^(exp10+1) — i.e. digits
/// d1..dn with the decimal point after the first digit matching
/// `d1.d2..dn * 10^exp10`.
fn shortest_digits(f: f64) -> (String, i32) {
    debug_assert!(f > 0.0 && f.is_finite());
    let s = format!("{:e}", f); // e.g. "1.2345e17", "5e-324"
    let (mant, exp) = s.split_once('e').expect("LowerExp always emits 'e'");
    let exp: i32 = exp.parse().expect("exponent parses");
    let digits: String = mant.chars().filter(|c| c.is_ascii_digit()).collect();
    (digits, exp)
}

/// Parse (digits, exp10) back to f64 via the host's correctly-rounded parser.
fn digits_to_f64(digits: &str, exp10: i32) -> f64 {
    let mut lit = String::with_capacity(digits.len() + 8);
    lit.push(digits.as_bytes()[0] as char);
    if digits.len() > 1 {
        lit.push('.');
        lit.push_str(&digits[1..]);
    }
    lit.push('e');
    lit.push_str(&exp10.to_string());
    lit.parse().unwrap_or(f64::NAN)
}

/// Compare |f| to the exact decimal number `digits × 10^(exp10-len+1)+5ulp/2`
/// — i.e. the midpoint between the two tie candidates — exactly, using
/// big-integer cross multiplication. Returns Ordering of f vs midpoint.
fn cmp_f64_to_decimal(f: f64, digits: &str, exp10: i32) -> std::cmp::Ordering {
    use num_bigint::BigInt;
    // Exact binary value of f: m2 * 2^e2.
    let bits = f.to_bits();
    let exp_bits = ((bits >> 52) & 0x7FF) as i64;
    let frac = bits & 0xF_FFFF_FFFF_FFFF;
    let (m2, e2): (u64, i64) = if exp_bits == 0 {
        (frac, -1074)
    } else {
        (frac | (1u64 << 52), exp_bits - 1075)
    };
    // Exact decimal: m10 * 10^e10 with the convention value = d1.d2.. * 10^exp10
    // → m10 = digits-as-int, e10 = exp10 - (len-1).
    let m10: BigInt = digits.parse().expect("digits parse");
    let e10 = exp10 as i64 - (digits.len() as i64 - 1);
    // Compare m2 * 2^e2  vs  m10 * 10^e10.
    let mut lhs = BigInt::from(m2);
    let mut rhs = m10;
    if e2 > 0 { lhs <<= e2 as u64; } else { rhs <<= (-e2) as u64; }
    // 10^e10 = 2^e10 * 5^e10
    if e10 > 0 {
        rhs *= BigInt::from(5u8).pow(e10 as u32);
        rhs <<= e10 as u64;
    } else if e10 < 0 {
        lhs *= BigInt::from(5u8).pow((-e10) as u32);
        lhs <<= (-e10) as u64;
    }
    lhs.cmp(&rhs)
}

/// Shortest round-trip digits for |f| with the SPEC §7.3 tie rule applied.
fn canonical_digits(f: f64) -> (String, i32) {
    let (digits, exp10) = shortest_digits(f);
    let bytes = digits.as_bytes();
    let last = (bytes[bytes.len() - 1] - b'0') as i8;
    for delta in [-1i8, 1i8] {
        let nd = last + delta;
        if !(0..=9).contains(&nd) { continue; }
        // A same-length neighbour (no carry/borrow across digits).
        let mut cand = digits.clone();
        unsafe { cand.as_bytes_mut()[bytes.len() - 1] = b'0' + nd as u8; }
        if cand.as_bytes()[0] == b'0' { continue; } // leading zero: not same form
        if digits_to_f64(&cand, exp10) == f {
            // Tie: both round-trip. Pick the one closest to the exact value
            // of f; the midpoint between them has one extra digit '5'.
            let (lo, hi) = if delta < 0 { (cand.clone(), digits.clone()) } else { (digits.clone(), cand.clone()) };
            let mut mid = lo.clone();
            mid.push('5');
            use std::cmp::Ordering::*;
            let chosen = match cmp_f64_to_decimal(f, &mid, exp10) {
                Less => lo,
                Greater => hi,
                Equal => {
                    // Exactly equidistant: even last digit (banker's).
                    let ld = |s: &str| (s.as_bytes()[s.len() - 1] - b'0') % 2;
                    if ld(&lo) == 0 { lo } else { hi }
                }
            };
            return (chosen, exp10);
        }
    }
    (digits, exp10)
}

/// Render (digits, exp10) in the §7.3 fixed/exponent window.
fn render_float(neg: bool, digits: &str, exp10: i32) -> String {
    let sign = if neg { "-" } else { "" };
    let n = digits.len() as i32;
    // |f| in [1e-4, 1e16) → fixed point. exp10 is floor(log10(|f|)).
    if (-4..16).contains(&exp10) {
        let point = exp10 + 1; // digits before the decimal point
        if point >= n {
            // pad with zeros, then ".0"
            let zeros = "0".repeat((point - n) as usize);
            format!("{}{}{}.0", sign, digits, zeros)
        } else if point > 0 {
            format!("{}{}.{}", sign, &digits[..point as usize], &digits[point as usize..])
        } else {
            let zeros = "0".repeat((-point) as usize);
            format!("{}0.{}{}", sign, zeros, digits)
        }
    } else {
        let mantissa = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.to_string()
        };
        let (esign, emag) = if exp10 < 0 { ('-', -exp10) } else { ('+', exp10) };
        format!("{}{}e{}{:02}", sign, mantissa, esign, emag)
    }
}

// SPEC §7.3: in strict canonical, C0, DEL, C1, BOM, BMP noncharacters,
// and bidirectional override/isolate controls MUST be escaped.
fn strict_must_escape(cp: u32) -> bool {
    cp <= 0x1F
        || cp == 0x7F
        || (0x80..=0x9F).contains(&cp)
        || cp == 0xFEFF || cp == 0xFFFE || cp == 0xFFFF
        || (0x202A..=0x202E).contains(&cp)
        || (0x2066..=0x2069).contains(&cp)
}

fn format_string_dquote(s: &str, opts: FormatOptions) -> Result<String, FormatError> {
    // SPEC §7.2: a string containing U+000A is emitted as the one-line
    // dquote-escape form (never triple-quoted). Triple-quoted strings
    // inside a soft-wrapped compound get re-indented by every known
    // formatter, which silently inserts leading spaces into the string
    // body (W4-04 data corruption). The one-line form removes that
    // attack surface entirely.
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        let cp = ch as u32;
        if ch == '"' { out.push_str("\\\""); }
        else if ch == '\\' { out.push_str("\\\\"); }
        else if ch == '\n' { out.push_str("\\n"); }
        else if ch == '\t' { out.push_str("\\t"); }
        else if ch == '\r' { out.push_str("\\r"); }
        else if ch == '\0' { out.push_str("\\0"); }
        else if opts.strict_canonical && strict_must_escape(cp) {
            write!(out, "\\u{{{:X}}}", cp).unwrap();
        }
        else if cp < 0x20 { write!(out, "\\u{{{:X}}}", cp).unwrap(); }
        // BOM and the two BMP noncharacters are always escaped — the
        // lexer rejects them literal in the document, so emitting them
        // raw would break round-trip.
        else if cp == 0xFEFF || cp == 0xFFFE || cp == 0xFFFF {
            write!(out, "\\u{{{:X}}}", cp).unwrap();
        }
        else { out.push(ch); }
    }
    out.push('"');
    Ok(out)
}
fn format_date(d: &WeftDate) -> String {
    let mut s = format!("#{:04}-{:02}-{:02}", d.year, d.month, d.day);
    if !d.has_time { return s; }
    write!(s, "T{:02}:{:02}:{:02}", d.hour, d.minute, d.second).unwrap();
    if d.nano != 0 {
        let frac = format!("{:09}", d.nano);
        let trimmed = frac.trim_end_matches('0');
        write!(s, ".{}", trimmed).unwrap();
    }
    match d.tz_offset_minutes {
        None => {}
        Some(0) => s.push('Z'),
        Some(off) => {
            let sign = if off >= 0 { '+' } else { '-' };
            let a = off.abs();
            write!(s, "{}{:02}:{:02}", sign, a / 60, a % 60).unwrap();
        }
    }
    s
}
fn format_list_multi(items: &[Value], depth: usize, opts: FormatOptions) -> String {
    if items.is_empty() { return "[]".into(); }
    let pad_inner = " ".repeat((depth + 1) * opts.indent);
    let pad_close = " ".repeat(depth * opts.indent);
    let lines: Vec<String> = items.iter().map(|it| {
        format!("{}{}", pad_inner, format_rec(it, depth + 1, opts))
    }).collect();
    format!("[\n{}\n{}]", lines.join("\n"), pad_close)
}

fn format_map_multi(m: &indexmap::IndexMap<String, Value>, depth: usize, opts: FormatOptions) -> String {
    if m.is_empty() { return "{}".into(); }
    let mut keys: Vec<&String> = m.keys().collect();
    if opts.sort_keys { keys.sort(); }
    let pad_inner = " ".repeat((depth + 1) * opts.indent);
    let pad_close = " ".repeat(depth * opts.indent);
    let mut lines = Vec::new();
    for k in keys {
        let v = &m[k];
        let key_s = format!(":{}", k);
        let v_inline = inline_rec(v, opts).ok();
        if let Some(vi) = &v_inline {
            if pad_inner.len() + key_s.chars().count() + 1 + vi.chars().count() <= opts.max_width && !vi.contains('\n') {
                lines.push(format!("{}{} {}", pad_inner, key_s, vi));
                continue;
            }
        }
        let v_block = format_rec(v, depth + 1, opts);
        if v_block.contains('\n') {
            let deeper = " ".repeat((depth + 2) * opts.indent);
            lines.push(format!("{}{}", pad_inner, key_s));
            for ln in v_block.split('\n') {
                if ln.is_empty() { lines.push(String::new()); } else { lines.push(format!("{}{}", deeper, ln)); }
            }
        } else {
            lines.push(format!("{}{} {}", pad_inner, key_s, v_block));
        }
    }
    format!("{{\n{}\n{}}}", lines.join("\n"), pad_close)
}

fn format_sexpr_multi(s: &crate::types::Sexpr, depth: usize, opts: FormatOptions) -> String {
    if s.items.is_empty() { return format!("({})", s.tag.0); }
    let pad_inner = " ".repeat((depth + 1) * opts.indent);
    let pad_close = " ".repeat(depth * opts.indent);
    let lines: Vec<String> = s.items.iter().map(|it| {
        format!("{}{}", pad_inner, format_rec(it, depth + 1, opts))
    }).collect();
    format!("({}\n{}\n{})", s.tag.0, lines.join("\n"), pad_close)
}
