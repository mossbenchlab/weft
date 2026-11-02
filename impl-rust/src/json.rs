//! JSON interop (SPEC §11).

use crate::error::{Result, WeftError};
use crate::types::{BigIntValue, Binary, BinaryEncoding, Decimal, Keyword, Sexpr, Symbol, TypeExpr, Typed, Value, WeftDate};
use num_bigint::BigInt;
use serde_json::{Map as JMap, Value as JVal};

// SPEC §11.2 strict payload: ``-?[0-9]+`` only. ASCII digits, optional
// leading '-', nothing else. ``num_bigint::BigInt::from_str`` is
// too lenient (accepts '+' and '_'), so we validate shape first.
fn is_strict_int_payload(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.is_empty() { return false; }
    let digits_start = if bytes[0] == b'-' { 1 } else { 0 };
    if digits_start == bytes.len() { return false; }
    bytes[digits_start..].iter().all(|b| b.is_ascii_digit())
}

// SPEC §5.8 / W4-16: RFC 4648 §3.5 canonical check — non-zero trailing
// bits are rejected. Verify by re-encoding the decoded bytes and
// comparing to the original.
fn canonical_base64_decode(s: &str) -> std::result::Result<Vec<u8>, String> {
    use base64::Engine;
    let eng = base64::engine::general_purpose::STANDARD;
    let bytes = eng.decode(s).map_err(|e| format!("not valid base64: {}", e))?;
    let re = eng.encode(&bytes);
    if re != s {
        return Err("non-canonical base64 (trailing bits must be zero)".into());
    }
    Ok(bytes)
}

const JSON_SAFE_INT_MIN: i64 = -((1_i64 << 53) - 1);
const JSON_SAFE_INT_MAX: i64 = (1_i64 << 53) - 1;

/// Convert a Weft value to its canonical JSON representation.
pub fn to_json(v: &Value) -> JVal {
    match v {
        Value::Null => JVal::Null,
        Value::Bool(b) => JVal::Bool(*b),
        Value::Int(b) => {
            if let Some(n) = bigint_to_safe_json_number(b) { n }
            else { JVal::String(b.to_string()) }
        }
        Value::BigInt(b) => JVal::String(b.0.to_string()),
        Value::Float(f) => {
            if f.is_nan() { JVal::String("nan".into()) }
            else if *f == f64::INFINITY { JVal::String("inf".into()) }
            else if *f == f64::NEG_INFINITY { JVal::String("-inf".into()) }
            else { JVal::from(*f) }
        }
        Value::Decimal(d) => JVal::String(d.0.clone()),
        Value::String(s) => JVal::String(s.clone()),
        Value::Binary(b) => match b.encoding {
            BinaryEncoding::Base64 => {
                use base64::Engine;
                JVal::String(format!("base64:{}", base64::engine::general_purpose::STANDARD.encode(&b.data)))
            }
            BinaryEncoding::Hex => {
                let mut s = String::from("hex:");
                for byte in &b.data { s.push_str(&format!("{:02x}", byte)); }
                JVal::String(s)
            }
        }
        Value::DateTime(d) => JVal::String(date_canonical(d)),
        Value::Symbol(s) => JVal::String(format!("#sym:{}", s.0)),
        Value::Keyword(k) => JVal::String(format!("#kw:{}", k.0)),
        Value::List(xs) => JVal::Array(xs.iter().map(to_json).collect()),
        Value::Map(m) => {
            let mut o = JMap::new();
            for (k, v) in m { o.insert(k.clone(), to_json(v)); }
            JVal::Object(o)
        }
        Value::Sexpr(s) => {
            let mut arr = vec![JVal::String(s.tag.0.clone())];
            for it in &s.items { arr.push(to_json(it)); }
            let mut o = JMap::new();
            o.insert("$sexpr".into(), JVal::Array(arr));
            JVal::Object(o)
        }
        Value::Typed(t) => {
            let mut inner = JMap::new();
            inner.insert("v".into(), to_json(&t.value));
            inner.insert("t".into(), match &t.type_ {
                TypeExpr::Name(s) => JVal::String(format!("#sym:{}", s.0)),
                TypeExpr::Form(sx) => {
                    let mut arr = vec![JVal::String(sx.tag.0.clone())];
                    for it in &sx.items { arr.push(to_json(it)); }
                    let mut o = JMap::new();
                    o.insert("$sexpr".into(), JVal::Array(arr));
                    JVal::Object(o)
                }
            });
            let mut o = JMap::new();
            o.insert("$typed".into(), JVal::Object(inner));
            JVal::Object(o)
        }
    }
}

fn bigint_to_safe_json_number(b: &BigInt) -> Option<JVal> {
    let max_safe: BigInt = BigInt::from((1u64 << 53) - 1);
    let min_safe = -&max_safe;
    if b >= &min_safe && b <= &max_safe {
        if let Ok(n) = i64::try_from(b) { return Some(JVal::from(n)); }
    }
    None
}

fn date_canonical(d: &WeftDate) -> String {
    let mut s = format!("{:04}-{:02}-{:02}", d.year, d.month, d.day);
    if !d.has_time { return s; }
    s.push_str(&format!("T{:02}:{:02}:{:02}", d.hour, d.minute, d.second));
    if d.nano != 0 {
        let frac = format!("{:09}", d.nano);
        let trimmed = frac.trim_end_matches('0');
        s.push_str(&format!(".{}", trimmed));
    }
    match d.tz_offset_minutes {
        None => {}
        Some(0) => s.push('Z'),
        Some(off) => {
            let sign = if off >= 0 { '+' } else { '-' };
            let a = off.abs();
            s.push_str(&format!("{}{:02}:{:02}", sign, a / 60, a % 60));
        }
    }
    s
}

/// Convert a JSON value to Weft (lossy).
pub fn from_json(v: &JVal) -> Result<Value> {
    from_json_impl(v, true)
}

pub fn from_json_impl(v: &JVal, strict: bool) -> Result<Value> {
    let mut ctx = ConvCtx { limits: Limits::default(), total: 0 };
    from_json_ctx(v, strict, 0, &mut ctx)
}

/// `from_json` with explicit SPEC §9 limits — the value-level entry point
/// enforces the same depth / total / per-collection / scalar limits as
/// the text loader `loads_json`.
pub fn from_json_with_limits(v: &JVal, limits: Limits) -> Result<Value> {
    let mut ctx = ConvCtx { limits, total: 0 };
    from_json_ctx(v, true, 0, &mut ctx)
}

fn from_json_ctx(v: &JVal, strict: bool, depth: usize, ctx: &mut ConvCtx) -> Result<Value> {
    ctx.bump()?;
    match v {
        JVal::Null => Ok(Value::Null),
        JVal::Bool(b) => Ok(Value::Bool(*b)),
        JVal::Number(n) => {
            if let Some(i) = n.as_i64() { Ok(Value::Int(BigInt::from(i))) }
            else if let Some(u) = n.as_u64() { Ok(Value::Int(BigInt::from(u))) }
            else if let Some(f) = n.as_f64() { Ok(Value::Float(f)) }
            else { Err(WeftError::type_err("unrepresentable JSON number", "WEFT_TYPE_NUMBER")) }
        }
        JVal::String(s) => {
            if s.len() > ctx.limits.max_string_bytes {
                return Err(WeftError::limit(
                    format!("string exceeds max_string_bytes={}", ctx.limits.max_string_bytes),
                    "WEFT_LIMIT_STRING"));
            }
            Ok(Value::String(s.clone()))
        }
        JVal::Array(xs) => {
            ctx.depth(depth + 1)?;
            ctx.coll(xs.len())?;
            let mut out = Vec::with_capacity(xs.len());
            for x in xs { out.push(from_json_ctx(x, strict, depth + 1, ctx)?); }
            Ok(Value::List(out))
        }
        JVal::Object(obj) => {
            ctx.depth(depth + 1)?;
            ctx.coll(obj.len())?;
            let mut m = indexmap::IndexMap::new();
            for (k, v) in obj {
                if strict && !is_valid_symbol(k) {
                    return Err(WeftError::type_err(format!("JSON key {:?} is not a valid Weft keyword", k), "WEFT_TYPE_KEY"));
                }
                m.insert(k.clone(), from_json_ctx(v, strict, depth + 1, ctx)?);
            }
            Ok(Value::Map(m))
        }
    }
}

/// Lossless JSON mapping. Every Weft type becomes a tagged JSON object
/// (except plain-JSON types: null, bool, native-safe number).
///
/// Tag conventions (one tag per object):
///   `{"$sym":  "name"}`                               Symbol
///   `{"$kw":   "name"}`                               Keyword
///   `{"$bigint": "..."}`                              BigInt (decimal digits)
///   `{"$int-big": "..."}`                             plain int outside safe range
///   `{"$dec":  "..."}`                                Decimal (preserved text)
///   `{"$str":  "..."}`                                String (always wrapped)
///   `{"$bin":  {"enc":"base64"|"hex","data":"..."}}`  Binary
///   `{"$dt":   "ISO-8601"}`                           DateTime
///   `{"$float": "nan" | "inf" | "-inf"}`              non-finite Float
///   `{"$sexpr": [tag, items...]}`                     Sexpr
///   `{"$typed": {"v":..., "t":...}}`                  Typed
///   `{"$list":  [...]}`                               List
///   `{"$map":   {"k": v, ...}}`                       Map
pub fn to_json_lossless(v: &Value) -> JVal {
    match v {
        Value::Null => JVal::Null,
        Value::Bool(b) => JVal::Bool(*b),
        Value::Int(b) => {
            if let Some(n) = bigint_to_safe_json_number(b) { n }
            else { tagged("$int-big", JVal::String(b.to_string())) }
        }
        Value::BigInt(b) => tagged("$bigint", JVal::String(b.0.to_string())),
        Value::Float(f) => {
            // Every float is tagged — a plain JSON number is reserved for
            // Weft Integer so ``1`` (Int) and ``1.0`` (Float) survive the
            // round trip as different types.
            if f.is_nan() { tagged("$float", JVal::String("nan".into())) }
            else if *f == f64::INFINITY { tagged("$float", JVal::String("inf".into())) }
            else if *f == f64::NEG_INFINITY { tagged("$float", JVal::String("-inf".into())) }
            else { tagged("$float", JVal::String(float_canonical_text(*f))) }
        }
        Value::Decimal(d) => {
            // SPEC §7.2: lossless $dec payload carries the canonical
            // Decimal text — leading '+' stripped, exponent marker 'e'.
            let mut text = d.0.clone();
            if text.starts_with('+') { text.remove(0); }
            text = text.replace('E', "e");
            tagged("$dec", JVal::String(text))
        },
        Value::String(s) => tagged("$str", JVal::String(s.clone())),
        Value::Binary(b) => {
            let mut inner = JMap::new();
            match b.encoding {
                BinaryEncoding::Base64 => {
                    use base64::Engine;
                    inner.insert("enc".into(), JVal::String("base64".into()));
                    inner.insert("data".into(), JVal::String(base64::engine::general_purpose::STANDARD.encode(&b.data)));
                }
                BinaryEncoding::Hex => {
                    let mut s = String::with_capacity(b.data.len() * 2);
                    for byte in &b.data { s.push_str(&format!("{:02x}", byte)); }
                    inner.insert("enc".into(), JVal::String("hex".into()));
                    inner.insert("data".into(), JVal::String(s));
                }
            }
            tagged("$bin", JVal::Object(inner))
        }
        Value::DateTime(d) => tagged("$dt", JVal::String(date_canonical(d))),
        Value::Symbol(s) => tagged("$sym", JVal::String(s.0.clone())),
        Value::Keyword(k) => tagged("$kw", JVal::String(k.0.clone())),
        Value::List(xs) => tagged("$list", JVal::Array(xs.iter().map(to_json_lossless).collect())),
        Value::Map(m) => {
            let mut o = JMap::new();
            for (k, v) in m { o.insert(k.clone(), to_json_lossless(v)); }
            tagged("$map", JVal::Object(o))
        }
        Value::Sexpr(s) => {
            let mut arr = vec![JVal::String(s.tag.0.clone())];
            for it in &s.items { arr.push(to_json_lossless(it)); }
            tagged("$sexpr", JVal::Array(arr))
        }
        Value::Typed(t) => {
            let mut inner = JMap::new();
            inner.insert("v".into(), to_json_lossless(&t.value));
            inner.insert("t".into(), match &t.type_ {
                TypeExpr::Name(s) => tagged("$sym", JVal::String(s.0.clone())),
                TypeExpr::Form(sx) => {
                    let mut arr = vec![JVal::String(sx.tag.0.clone())];
                    for it in &sx.items { arr.push(to_json_lossless(it)); }
                    tagged("$sexpr", JVal::Array(arr))
                }
            });
            tagged("$typed", JVal::Object(inner))
        }
    }
}

fn tagged(tag: &str, payload: JVal) -> JVal {
    let mut o = JMap::new();
    o.insert(tag.into(), payload);
    JVal::Object(o)
}

/// Shortest round-trip decimal text for a finite ``f64``. Preserves the
/// sign of zero so ``-0.0`` survives the JSON round-trip.
fn float_canonical_text(f: f64) -> String {
    // SPEC §11.2.1 rule 4: the $float payload is the strict canonical
    // finite float text — delegate to the formatter so the tie rule and
    // the fixed/exponent window stay in one place.
    crate::formatter::format_float(f)
}

/// Strict inverse of [`to_json_lossless`]. Rejects bare strings, arrays,
/// and multi-key objects: a round-trip through [`to_json_lossless`] always
/// uses the tagged form, so untagged inputs are rejected to preserve the
/// round-trip identity ``from_json_lossless(to_json_lossless(x)) == x``.
pub fn from_json_lossless(v: &JVal) -> Result<Value> {
    from_json_lossless_with_limits(v, Limits::default())
}

/// `from_json_lossless` with explicit SPEC §9 limits — the value-level
/// entry point enforces the same limits as the text loader
/// `loads_json_lossless`.
pub fn from_json_lossless_with_limits(v: &JVal, limits: Limits) -> Result<Value> {
    let mut ctx = ConvCtx { limits, total: 0 };
    from_json_lossless_ctx(v, 0, &mut ctx)
}

fn from_json_lossless_ctx(v: &JVal, depth: usize, ctx: &mut ConvCtx) -> Result<Value> {
    ctx.bump()?;
    match v {
        JVal::Null => Ok(Value::Null),
        JVal::Bool(b) => Ok(Value::Bool(*b)),
        JVal::Number(n) => {
            // JSON numbers that round-trip as integers are Weft Integer.
            // SPEC §11.2.1 rule 2 + W4-11: plain integers outside the
            // JSON safe range (±(2^53-1)) MUST use ``$int-big``, else a
            // consumer that routes through float64 (e.g. Go) silently
            // truncates 2^53+1 to 2^53. The lexical check uses as_i64
            // first, which succeeds within i64, then enforces the
            // narrower JSON safe-integer bound.
            if let Some(i) = n.as_i64() {
                if (JSON_SAFE_INT_MIN..=JSON_SAFE_INT_MAX).contains(&i) {
                    Ok(Value::Int(BigInt::from(i)))
                } else {
                    Err(WeftError::type_err(
                        r#"int outside JSON-safe range must use the {"$int-big": ...} tagged form"#,
                        "WEFT_TYPE_INT"))
                }
            } else if n.as_u64().is_some() {
                // u64 beyond i64 is definitely outside the safe range.
                Err(WeftError::type_err(
                    r#"int outside JSON-safe range must use the {"$int-big": ...} tagged form"#,
                    "WEFT_TYPE_INT"))
            } else {
                Err(WeftError::type_err(
                    r#"fractional / non-finite number must use the {"$float": ...} tagged form (plain JSON number is reserved for Weft Integer)"#,
                    "WEFT_TYPE_FLOAT"))
            }
        }
        JVal::String(_) => Err(WeftError::type_err(
            r#"bare JSON string is not a lossless form; wrap it as {"$str":...}, {"$sym":...}, or {"$kw":...}"#,
            "WEFT_TYPE_STRING")),
        JVal::Array(_) => Err(WeftError::type_err(
            r#"bare JSON array is not a lossless form; use {"$list":[...]} or {"$sexpr":[...]}"#,
            "WEFT_TYPE_ARRAY")),
        JVal::Object(obj) => {
            if obj.len() != 1 {
                return Err(WeftError::type_err(
                    format!("lossless JSON object must have exactly one key (got {}); a plain mapping should be {{\"$map\": {{...}}}}", obj.len()),
                    "WEFT_TYPE_LOSSLESS"));
            }
            let (tag, payload) = obj.iter().next().unwrap();
            match tag.as_str() {
                "$sym" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$sym payload must be a string", "WEFT_TYPE_SYM"))?;
                    Symbol::new(s)
                        .map(Value::Symbol)
                        .map_err(|e| WeftError::type_err(format!("$sym payload invalid: {}", e), "WEFT_TYPE_SYM"))
                }
                "$kw" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$kw payload must be a string", "WEFT_TYPE_KW"))?;
                    Keyword::new(s)
                        .map(Value::Keyword)
                        .map_err(|e| WeftError::type_err(format!("$kw payload invalid: {}", e), "WEFT_TYPE_KW"))
                }
                "$bigint" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$bigint payload must be a string", "WEFT_TYPE_BIGINT"))?;
                    if !is_strict_int_payload(s) {
                        return Err(WeftError::type_err(
                            format!("$bigint payload is not a decimal integer: {:?} (must match -?[0-9]+)", s),
                            "WEFT_TYPE_BIGINT"));
                    }
                    let n_digits = s.trim_start_matches('-').len();
                    if n_digits > ctx.limits.max_int_digits {
                        return Err(WeftError::limit(
                            format!("$bigint payload has {} digits, exceeds max_int_digits={}", n_digits, ctx.limits.max_int_digits),
                            "WEFT_LIMIT_INT"));
                    }
                    let b: BigInt = s.parse().map_err(|_| WeftError::type_err(format!("$bigint payload invalid: {:?}", s), "WEFT_TYPE_BIGINT"))?;
                    Ok(Value::BigInt(BigIntValue(b)))
                }
                "$int-big" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$int-big payload must be a string", "WEFT_TYPE_INT"))?;
                    if !is_strict_int_payload(s) {
                        return Err(WeftError::type_err(
                            format!("$int-big payload is not a decimal integer: {:?} (must match -?[0-9]+)", s),
                            "WEFT_TYPE_INT"));
                    }
                    let n_digits = s.trim_start_matches('-').len();
                    if n_digits > ctx.limits.max_int_digits {
                        return Err(WeftError::limit(
                            format!("$int-big payload has {} digits, exceeds max_int_digits={}", n_digits, ctx.limits.max_int_digits),
                            "WEFT_LIMIT_INT"));
                    }
                    let b: BigInt = s.parse().map_err(|_| WeftError::type_err(format!("$int-big payload invalid: {:?}", s), "WEFT_TYPE_INT"))?;
                    Ok(Value::Int(b))
                }
                "$dec" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$dec payload must be a string", "WEFT_TYPE_DEC"))?;
                    Decimal::new(s)
                        .map(Value::Decimal)
                        .map_err(|e| WeftError::type_err(format!("$dec payload invalid: {}", e), "WEFT_TYPE_DEC"))
                }
                "$str" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$str payload must be a string", "WEFT_TYPE_STR"))?;
                    if s.len() > ctx.limits.max_string_bytes {
                        return Err(WeftError::limit(
                            format!("$str payload exceeds max_string_bytes={}", ctx.limits.max_string_bytes),
                            "WEFT_LIMIT_STRING"));
                    }
                    Ok(Value::String(s.into()))
                }
                "$bin" => {
                    let inner = payload.as_object().ok_or_else(|| WeftError::type_err("$bin payload must be an object with 'enc' and 'data'", "WEFT_TYPE_BIN"))?;
                    if inner.len() != 2 || !inner.contains_key("enc") || !inner.contains_key("data") {
                        return Err(WeftError::type_err("$bin payload must have exactly {enc, data}", "WEFT_TYPE_BIN"));
                    }
                    let enc = inner.get("enc").and_then(|v| v.as_str()).ok_or_else(|| WeftError::type_err("$bin.enc must be a string", "WEFT_TYPE_BIN"))?;
                    let data = inner.get("data").and_then(|v| v.as_str()).ok_or_else(|| WeftError::type_err("$bin.data must be a string", "WEFT_TYPE_BIN"))?;
                    match enc {
                        "base64" => {
                            // W4-16 / SPEC §5.8: canonical check — reject
                            // non-canonical trailing bits (`AB==` which
                            // decodes the same bytes as `AA==`).
                            let bytes = canonical_base64_decode(data)
                                .map_err(|e| WeftError::type_err(format!("$bin.data {}", e), "WEFT_TYPE_BIN"))?;
                            Ok(Value::Binary(Binary { data: bytes, encoding: BinaryEncoding::Base64 }))
                        }
                        "hex" => {
                            let bytes = hex_decode(data)?;
                            Ok(Value::Binary(Binary { data: bytes, encoding: BinaryEncoding::Hex }))
                        }
                        _ => Err(WeftError::type_err(format!("$bin.enc must be 'base64' or 'hex', got {:?}", enc), "WEFT_TYPE_BIN")),
                    }
                }
                "$dt" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$dt payload must be a string", "WEFT_TYPE_DT"))?;
                    if !is_canonical_datetime(s) {
                        return Err(WeftError::type_err(
                            format!("$dt payload is not a canonical datetime: {:?}", s),
                            "WEFT_TYPE_DT"));
                    }
                    let doc = crate::parser::parse(&format!("#{}", s))
                        .map_err(|e| WeftError::type_err(format!("$dt payload invalid: {:?} ({})", s, e), "WEFT_TYPE_DT"))?;
                    match doc {
                        Value::DateTime(d) => Ok(Value::DateTime(d)),
                        Value::Typed(_) => Err(WeftError::type_err(
                            format!("$dt payload must be a datetime scalar, not a type-annotated value: {:?}", s),
                            "WEFT_TYPE_DT")),
                        _ => Err(WeftError::type_err(format!("$dt payload is not a datetime: {:?}", s), "WEFT_TYPE_DT")),
                    }
                }
                "$float" => {
                    let s = payload.as_str().ok_or_else(|| WeftError::type_err("$float payload must be a string", "WEFT_TYPE_FLOAT"))?;
                    match s {
                        "nan" => Ok(Value::Float(f64::NAN)),
                        "inf" => Ok(Value::Float(f64::INFINITY)),
                        "-inf" => Ok(Value::Float(f64::NEG_INFINITY)),
                        _ => {
                            if !is_canonical_finite_float(s) {
                                return Err(WeftError::type_err(format!("$float payload is not a finite decimal: {:?}", s), "WEFT_TYPE_FLOAT"));
                            }
                            let f: f64 = s.parse()
                                .map_err(|_| WeftError::type_err(format!("$float payload invalid: {:?}", s), "WEFT_TYPE_FLOAT"))?;
                            if f.is_nan() || f.is_infinite() {
                                return Err(WeftError::type_err(format!("$float payload parsed to non-finite: {:?}", s), "WEFT_TYPE_FLOAT"));
                            }
                            // Preserve sign of zero (-0.0 → -0.0, not 0.0).
                            if s == "-0.0" || s == "-0" { Ok(Value::Float(-0.0)) } else { Ok(Value::Float(f)) }
                        }
                    }
                },
                "$sexpr" => {
                    let arr = payload.as_array().ok_or_else(|| WeftError::type_err("$sexpr payload must be a non-empty array", "WEFT_TYPE_SEXPR"))?;
                    if arr.is_empty() { return Err(WeftError::type_err("$sexpr payload must be a non-empty array", "WEFT_TYPE_SEXPR")); }
                    let tag_name = arr[0].as_str().ok_or_else(|| WeftError::type_err("$sexpr tag (payload[0]) must be a string", "WEFT_TYPE_SEXPR"))?;
                    // W4-12: validate tag through Symbol::new; raw
                    // Symbol construction with an invalid name used to
                    // produce text that re-parses as something else.
                    let tag_sym = Symbol::new(tag_name)
                        .map_err(|e| WeftError::type_err(format!("$sexpr tag is not a valid Symbol name: {}", e), "WEFT_TYPE_SEXPR"))?;
                    ctx.depth(depth + 1)?;
                    ctx.coll(arr.len() - 1)?;
                    let mut items = Vec::with_capacity(arr.len() - 1);
                    for a in &arr[1..] { items.push(from_json_lossless_ctx(a, depth + 1, ctx)?); }
                    Ok(Value::Sexpr(Sexpr { tag: tag_sym, items }))
                }
                "$typed" => {
                    let inner = payload.as_object().ok_or_else(|| WeftError::type_err("$typed payload must be {v,t}", "WEFT_TYPE_TYPED"))?;
                    if inner.len() != 2 || !inner.contains_key("v") || !inner.contains_key("t") {
                        return Err(WeftError::type_err("$typed payload must be {v,t}", "WEFT_TYPE_TYPED"));
                    }
                    ctx.depth(depth + 1)?;
                    let value = from_json_lossless_ctx(&inner["v"], depth + 1, ctx)?;
                    let t_val = from_json_lossless_ctx(&inner["t"], depth + 1, ctx)?;
                    let te = match t_val {
                        Value::Symbol(s) => TypeExpr::Name(s),
                        Value::Sexpr(s) => TypeExpr::Form(s),
                        _ => return Err(WeftError::type_err("$typed.t must resolve to a Symbol or Sexpr", "WEFT_TYPE_TYPED")),
                    };
                    Ok(Value::Typed(Box::new(Typed { value, type_: te })))
                }
                "$list" => {
                    let arr = payload.as_array().ok_or_else(|| WeftError::type_err("$list payload must be an array", "WEFT_TYPE_LIST"))?;
                    ctx.depth(depth + 1)?;
                    ctx.coll(arr.len())?;
                    let mut out = Vec::with_capacity(arr.len());
                    for a in arr { out.push(from_json_lossless_ctx(a, depth + 1, ctx)?); }
                    Ok(Value::List(out))
                }
                "$map" => {
                    let inner = payload.as_object().ok_or_else(|| WeftError::type_err("$map payload must be an object", "WEFT_TYPE_MAP"))?;
                    ctx.depth(depth + 1)?;
                    ctx.coll(inner.len())?;
                    let mut m = indexmap::IndexMap::new();
                    for (k, v) in inner {
                        if !is_valid_symbol(k) {
                            return Err(WeftError::type_err(format!("$map key {:?} is not a valid Weft keyword name (would break structure on re-parse)", k), "WEFT_TYPE_KEY"));
                        }
                        m.insert(k.clone(), from_json_lossless_ctx(v, depth + 1, ctx)?);
                    }
                    Ok(Value::Map(m))
                }
                _ => Err(WeftError::type_err(format!("unknown lossless tag {:?}", tag), "WEFT_TYPE_LOSSLESS")),
            }
        }
    }
}

fn is_valid_symbol(s: &str) -> bool {
    crate::types::is_valid_keyword_name(s)
}

/// Strict finite-decimal shape for ``$float`` payloads.
fn is_canonical_finite_float(s: &str) -> bool {
    let b = s.as_bytes();
    let mut i = 0usize;
    if b.is_empty() { return false; }
    if b[0] == b'+' || b[0] == b'-' { i += 1; }
    // integer part
    let int_start = i;
    while i < b.len() && b[i].is_ascii_digit() { i += 1; }
    if i == int_start { return false; }
    // optional fractional part
    if i < b.len() && b[i] == b'.' {
        i += 1;
        let frac_start = i;
        while i < b.len() && b[i].is_ascii_digit() { i += 1; }
        if i == frac_start { return false; }
    }
    // optional exponent
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') { i += 1; }
        let exp_start = i;
        while i < b.len() && b[i].is_ascii_digit() { i += 1; }
        if i == exp_start { return false; }
    }
    i == b.len()
}

/// Strict RFC-3339-like shape for ``$dt`` payloads. Rejects trailing
/// whitespace, comments, type annotations, and any non-ASCII characters.
fn is_canonical_datetime(s: &str) -> bool {
    let b = s.as_bytes();
    // Date part: YYYY-MM-DD
    if b.len() < 10 { return false; }
    let is_d = |x: u8| x.is_ascii_digit();
    if !(is_d(b[0]) && is_d(b[1]) && is_d(b[2]) && is_d(b[3])) { return false; }
    if b[4] != b'-' || !(is_d(b[5]) && is_d(b[6])) || b[7] != b'-' { return false; }
    if !(is_d(b[8]) && is_d(b[9])) { return false; }
    if b.len() == 10 { return true; }
    // Time part: T HH:MM:SS
    if b[10] != b'T' { return false; }
    if b.len() < 19 { return false; }
    if !(is_d(b[11]) && is_d(b[12])) || b[13] != b':' { return false; }
    if !(is_d(b[14]) && is_d(b[15])) || b[16] != b':' { return false; }
    if !(is_d(b[17]) && is_d(b[18])) { return false; }
    let mut i = 19usize;
    // Optional fractional seconds .ddd...
    if i < b.len() && b[i] == b'.' {
        i += 1;
        let frac_start = i;
        while i < b.len() && is_d(b[i]) { i += 1; }
        if i == frac_start { return false; }
    }
    if i == b.len() { return true; }
    // Timezone: Z or ±HH:MM
    if b[i] == b'Z' { return i + 1 == b.len(); }
    if (b[i] == b'+' || b[i] == b'-') && i + 6 == b.len() {
        if !(is_d(b[i+1]) && is_d(b[i+2])) { return false; }
        if b[i+3] != b':' { return false; }
        if !(is_d(b[i+4]) && is_d(b[i+5])) { return false; }
        return true;
    }
    false
}

fn hex_decode(s: &str) -> Result<Vec<u8>> {
    if s.len() % 2 != 0 { return Err(WeftError::type_err("odd hex", "WEFT_TYPE_HEX")); }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    for i in (0..bytes.len()).step_by(2) {
        let hi = (bytes[i] as char).to_digit(16);
        let lo = (bytes[i + 1] as char).to_digit(16);
        match (hi, lo) {
            (Some(h), Some(l)) => out.push((h * 16 + l) as u8),
            _ => return Err(WeftError::type_err("invalid hex", "WEFT_TYPE_HEX")),
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Lexical JSON reader (SPEC §11.2.1 rule 4, §11.2.5, §9)
//
// serde_json's Value collapses duplicate keys (last-write) and its Number
// type hides whether the token was ``1`` or ``1.0`` when arbitrary_precision
// is off. A conforming lossless decoder needs the token text, duplicate-key
// rejection, insertion order, and the §9 limits — this reader provides all
// four and is what `loads_json_lossless` / `loads_json` build on.
// ---------------------------------------------------------------------------

use crate::limits::Limits;

#[derive(Debug, Clone)]
pub enum JsonLex {
    Null,
    Bool(bool),
    /// A number token with its original lexical form.
    Num(String),
    Str(String),
    Arr(Vec<JsonLex>),
    Obj(Vec<(String, JsonLex)>),
}

struct JsonReader<'a> {
    b: &'a [u8],
    s: &'a str,
    i: usize,
    limits: Limits,
}

impl<'a> JsonReader<'a> {
    fn err(&self, msg: &str) -> WeftError {
        WeftError::syntax(format!("{} (at byte {})", msg, self.i), "WEFT_SYNTAX_JSON")
    }

    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn value(&mut self, depth: usize) -> Result<JsonLex> {
        // Each Weft level costs at most two JSON levels ({"$list": [...]}),
        // so 2*max_depth+8 bounds the reader's recursion; the precise Weft
        // depth rule (§8) is enforced again during conversion.
        if depth > self.limits.max_depth * 2 + 8 {
            return Err(WeftError::limit(
                format!("JSON nesting exceeds max_depth={}", self.limits.max_depth),
                "WEFT_LIMIT_DEPTH"));
        }
        self.ws();
        match self.b.get(self.i) {
            None => Err(self.err("unexpected end of JSON")),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => Ok(JsonLex::Str(self.string()?)),
            Some(b't') => { self.lit("true")?; Ok(JsonLex::Bool(true)) }
            Some(b'f') => { self.lit("false")?; Ok(JsonLex::Bool(false)) }
            Some(b'n') => { self.lit("null")?; Ok(JsonLex::Null) }
            Some(c) if *c == b'-' || c.is_ascii_digit() => self.number(),
            Some(c) => Err(self.err(&format!("unexpected character {:?} in JSON", *c as char))),
        }
    }

    fn lit(&mut self, w: &str) -> Result<()> {
        if self.b.len() >= self.i + w.len() && &self.b[self.i..self.i + w.len()] == w.as_bytes() {
            self.i += w.len();
            Ok(())
        } else {
            Err(self.err("invalid JSON literal"))
        }
    }

    fn object(&mut self, depth: usize) -> Result<JsonLex> {
        self.i += 1;
        let mut out: Vec<(String, JsonLex)> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        self.ws();
        if self.b.get(self.i) == Some(&b'}') { self.i += 1; return Ok(JsonLex::Obj(out)); }
        loop {
            self.ws();
            if self.b.get(self.i) != Some(&b'"') { return Err(self.err("expected string key in JSON object")); }
            let k = self.string()?;
            // SPEC §11.2.5: duplicate keys MUST be rejected.
            if !seen.insert(k.clone()) {
                return Err(WeftError::syntax(
                    format!("duplicate key {:?} in JSON object", k),
                    "WEFT_SYNTAX_JSON_DUPKEY"));
            }
            self.ws();
            if self.b.get(self.i) != Some(&b':') { return Err(self.err("expected ':' in JSON object")); }
            self.i += 1;
            let v = self.value(depth + 1)?;
            out.push((k, v));
            if out.len() > self.limits.max_items_per_coll {
                return Err(WeftError::limit(
                    format!("JSON object exceeds max_items_per_coll={}", self.limits.max_items_per_coll),
                    "WEFT_LIMIT_ITEMS"));
            }
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => { self.i += 1; }
                Some(b'}') => { self.i += 1; return Ok(JsonLex::Obj(out)); }
                _ => return Err(self.err("expected ',' or '}' in JSON object")),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<JsonLex> {
        self.i += 1;
        let mut out = Vec::new();
        self.ws();
        if self.b.get(self.i) == Some(&b']') { self.i += 1; return Ok(JsonLex::Arr(out)); }
        loop {
            out.push(self.value(depth + 1)?);
            if out.len() > self.limits.max_items_per_coll {
                return Err(WeftError::limit(
                    format!("JSON array exceeds max_items_per_coll={}", self.limits.max_items_per_coll),
                    "WEFT_LIMIT_ITEMS"));
            }
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => { self.i += 1; }
                Some(b']') => { self.i += 1; return Ok(JsonLex::Arr(out)); }
                _ => return Err(self.err("expected ',' or ']' in JSON array")),
            }
        }
    }

    fn string(&mut self) -> Result<String> {
        self.i += 1;
        let mut out = String::new();
        let mut pending_high: i64 = -1;
        loop {
            let c = *self.b.get(self.i).ok_or_else(|| self.err("unterminated JSON string"))?;
            if c == b'"' {
                if pending_high >= 0 {
                    return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
                }
                self.i += 1;
                break;
            }
            if c == b'\\' {
                self.i += 1;
                let e = *self.b.get(self.i).ok_or_else(|| self.err("truncated JSON escape"))?;
                if e == b'u' {
                    if self.i + 5 > self.b.len() { return Err(self.err("truncated \\u escape")); }
                    let hex = &self.s[self.i + 1..self.i + 5];
                    let cu = u32::from_str_radix(hex, 16).map_err(|_| self.err("invalid \\u escape in JSON"))?;
                    self.i += 5;
                    if (0xD800..=0xDBFF).contains(&cu) {
                        if pending_high >= 0 {
                            return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
                        }
                        pending_high = cu as i64;
                        continue;
                    }
                    if (0xDC00..=0xDFFF).contains(&cu) {
                        if pending_high < 0 {
                            return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
                        }
                        let cp = 0x10000 + (((pending_high as u32 - 0xD800) << 10) | (cu - 0xDC00));
                        out.push(char::from_u32(cp).ok_or_else(|| self.err("invalid surrogate pair"))?);
                        pending_high = -1;
                        continue;
                    }
                    if pending_high >= 0 {
                        return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
                    }
                    out.push(char::from_u32(cu).ok_or_else(|| self.err("invalid \\u escape"))?);
                    continue;
                }
                if pending_high >= 0 {
                    return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
                }
                let r = match e {
                    b'"' => '"', b'\\' => '\\', b'/' => '/', b'b' => '\u{8}',
                    b'f' => '\u{c}', b'n' => '\n', b'r' => '\r', b't' => '\t',
                    _ => return Err(self.err("invalid JSON escape")),
                };
                out.push(r);
                self.i += 1;
                continue;
            }
            if pending_high >= 0 {
                return Err(self.err("lone surrogate in JSON string escape (Weft strings are Unicode scalar sequences)"));
            }
            if c < 0x20 {
                return Err(self.err("unescaped control character in JSON string"));
            }
            // Multi-byte UTF-8: copy the whole scalar (input &str is valid UTF-8).
            let ch_len = if c < 0x80 { 1 } else {
                let ch = self.s[self.i..].chars().next().unwrap();
                ch.len_utf8()
            };
            out.push_str(&self.s[self.i..self.i + ch_len]);
            self.i += ch_len;
            if out.len() > self.limits.max_string_bytes {
                return Err(WeftError::limit(
                    format!("JSON string exceeds max_string_bytes={}", self.limits.max_string_bytes),
                    "WEFT_LIMIT_STRING"));
            }
        }
        Ok(out)
    }

    fn number(&mut self) -> Result<JsonLex> {
        let start = self.i;
        if self.b.get(self.i) == Some(&b'-') { self.i += 1; }
        // int part
        match self.b.get(self.i) {
            Some(b'0') => { self.i += 1; }
            Some(c) if c.is_ascii_digit() => {
                while self.b.get(self.i).is_some_and(|c| c.is_ascii_digit()) { self.i += 1; }
            }
            _ => return Err(self.err("invalid JSON number")),
        }
        if self.b.get(self.i) == Some(&b'.') {
            self.i += 1;
            if !self.b.get(self.i).is_some_and(|c| c.is_ascii_digit()) {
                return Err(self.err("invalid JSON number"));
            }
            while self.b.get(self.i).is_some_and(|c| c.is_ascii_digit()) { self.i += 1; }
        }
        if matches!(self.b.get(self.i), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.b.get(self.i), Some(b'+') | Some(b'-')) { self.i += 1; }
            if !self.b.get(self.i).is_some_and(|c| c.is_ascii_digit()) {
                return Err(self.err("invalid JSON number"));
            }
            while self.b.get(self.i).is_some_and(|c| c.is_ascii_digit()) { self.i += 1; }
        }
        let raw = &self.s[start..self.i];
        let digits = raw.bytes().filter(|b| b.is_ascii_digit()).count();
        if digits > self.limits.max_int_digits {
            return Err(WeftError::limit(
                format!("JSON number has {} digits, exceeds max_int_digits={}", digits, self.limits.max_int_digits),
                "WEFT_LIMIT_INT"));
        }
        Ok(JsonLex::Num(raw.to_string()))
    }
}

/// Parse JSON text with the lexical reader. SPEC §9 limits are enforced
/// while reading (size, depth, items, string bytes, int digits); duplicate
/// object keys are rejected (SPEC §11.2.5, WEFT_SYNTAX_JSON_DUPKEY).
pub fn parse_json_lexical(text: &str, limits: Limits) -> Result<JsonLex> {
    limits.validate()?;
    if text.len() > limits.max_size_bytes {
        return Err(WeftError::limit(
            format!("JSON input {} bytes exceeds max_size_bytes={}", text.len(), limits.max_size_bytes),
            "WEFT_LIMIT_SIZE"));
    }
    let mut r = JsonReader { b: text.as_bytes(), s: text, i: 0, limits };
    let v = r.value(0)?;
    r.ws();
    if r.i != r.b.len() {
        return Err(r.err("unexpected trailing content in JSON"));
    }
    Ok(v)
}

fn lex_to_jval(node: &JsonLex) -> Result<JVal> {
    // Bridge for the tag payload handlers that operate on serde values.
    Ok(match node {
        JsonLex::Null => JVal::Null,
        JsonLex::Bool(b) => JVal::Bool(*b),
        JsonLex::Num(raw) => {
            serde_json::from_str::<JVal>(raw)
                .map_err(|e| WeftError::syntax(format!("invalid JSON number {:?}: {}", raw, e), "WEFT_SYNTAX_JSON"))?
        }
        JsonLex::Str(s) => JVal::String(s.clone()),
        JsonLex::Arr(xs) => {
            let mut out = Vec::with_capacity(xs.len());
            for x in xs { out.push(lex_to_jval(x)?); }
            JVal::Array(out)
        }
        JsonLex::Obj(kvs) => {
            let mut m = JMap::new();
            for (k, v) in kvs { m.insert(k.clone(), lex_to_jval(v)?); }
            JVal::Object(m)
        }
    })
}

struct ConvCtx {
    limits: Limits,
    total: usize,
}

impl ConvCtx {
    fn bump(&mut self) -> Result<()> {
        self.total += 1;
        if self.total > self.limits.max_total_items {
            return Err(WeftError::limit(
                format!("document has more than max_total_items={} values", self.limits.max_total_items),
                "WEFT_LIMIT_TOTAL"));
        }
        Ok(())
    }
    fn depth(&self, d: usize) -> Result<()> {
        if d > self.limits.max_depth {
            return Err(WeftError::limit(
                format!("tree depth {} exceeds max_depth={}", d, self.limits.max_depth),
                "WEFT_LIMIT_DEPTH"));
        }
        Ok(())
    }
    fn coll(&self, n: usize) -> Result<()> {
        if n > self.limits.max_items_per_coll {
            return Err(WeftError::limit(
                format!("collection has {} items, exceeds max_items_per_coll={}", n, self.limits.max_items_per_coll),
                "WEFT_LIMIT_ITEMS"));
        }
        Ok(())
    }
}

fn from_lex_lossless(node: &JsonLex, depth: usize, ctx: &mut ConvCtx) -> Result<Value> {
    ctx.bump()?;
    match node {
        JsonLex::Null => Ok(Value::Null),
        JsonLex::Bool(b) => Ok(Value::Bool(*b)),
        JsonLex::Num(raw) => {
            // SPEC §11.2.1 rule 4: the distinction is LEXICAL. A token with
            // '.', 'e', or 'E' is a float and must come through {"$float":...}.
            if raw.contains(['.', 'e', 'E']) {
                return Err(WeftError::type_err(
                    r#"fractional number must use the {"$float": ...} tagged form (plain JSON number is reserved for Weft Integer)"#,
                    "WEFT_TYPE_FLOAT"));
            }
            let b: BigInt = raw.parse().map_err(|_| WeftError::type_err("invalid integer token", "WEFT_TYPE_INT"))?;
            let max_safe: BigInt = BigInt::from((1u64 << 53) - 1);
            if b < -&max_safe || b > max_safe {
                return Err(WeftError::type_err(
                    r#"int outside JSON-safe range must use the {"$int-big": ...} tagged form"#,
                    "WEFT_TYPE_INT"));
            }
            Ok(Value::Int(b))
        }
        JsonLex::Str(_) => Err(WeftError::type_err(
            r#"bare JSON string is not a lossless form; wrap it as {"$str":...}, {"$sym":...}, or {"$kw":...}"#,
            "WEFT_TYPE_STRING")),
        JsonLex::Arr(_) => Err(WeftError::type_err(
            r#"bare JSON array is not a lossless form; use {"$list":[...]} or {"$sexpr":[...]}"#,
            "WEFT_TYPE_ARRAY")),
        JsonLex::Obj(kvs) => {
            if kvs.len() != 1 {
                return Err(WeftError::type_err(
                    format!(r#"lossless JSON object must have exactly one key (got {}); a plain mapping should be {{"$map": {{...}}}}"#, kvs.len()),
                    "WEFT_TYPE_LOSSLESS"));
            }
            let (tag, payload) = &kvs[0];
            match tag.as_str() {
                "$list" => {
                    let xs = match payload {
                        JsonLex::Arr(xs) => xs,
                        _ => return Err(WeftError::type_err("$list payload must be an array", "WEFT_TYPE_LIST")),
                    };
                    ctx.depth(depth + 1)?;
                    ctx.coll(xs.len())?;
                    let mut out = Vec::with_capacity(xs.len());
                    for x in xs { out.push(from_lex_lossless(x, depth + 1, ctx)?); }
                    Ok(Value::List(out))
                }
                "$sexpr" => {
                    let xs = match payload {
                        JsonLex::Arr(xs) if !xs.is_empty() => xs,
                        _ => return Err(WeftError::type_err("$sexpr payload must be a non-empty array", "WEFT_TYPE_SEXPR")),
                    };
                    let tag_name = match &xs[0] {
                        JsonLex::Str(s) => s.clone(),
                        _ => return Err(WeftError::type_err("$sexpr tag (payload[0]) must be a string", "WEFT_TYPE_SEXPR")),
                    };
                    let tag_sym = Symbol::new(tag_name)
                        .map_err(|e| WeftError::type_err(format!("$sexpr tag is not a valid Symbol name: {}", e), "WEFT_TYPE_SEXPR"))?;
                    ctx.depth(depth + 1)?;
                    ctx.coll(xs.len() - 1)?;
                    let mut items = Vec::with_capacity(xs.len() - 1);
                    for x in &xs[1..] { items.push(from_lex_lossless(x, depth + 1, ctx)?); }
                    Ok(Value::Sexpr(Sexpr { tag: tag_sym, items }))
                }
                "$map" => {
                    let kvs2 = match payload {
                        JsonLex::Obj(kvs2) => kvs2,
                        _ => return Err(WeftError::type_err("$map payload must be an object", "WEFT_TYPE_MAP")),
                    };
                    ctx.depth(depth + 1)?;
                    ctx.coll(kvs2.len())?;
                    let mut m = indexmap::IndexMap::new();
                    for (k, v) in kvs2 {
                        if !is_valid_symbol(k) {
                            return Err(WeftError::type_err(
                                format!("$map key {:?} is not a valid Weft keyword name (would break structure on re-parse)", k),
                                "WEFT_TYPE_KEY"));
                        }
                        let cv = from_lex_lossless(v, depth + 1, ctx)?;
                        m.insert(k.clone(), cv);
                    }
                    Ok(Value::Map(m))
                }
                "$typed" => {
                    let kvs2 = match payload {
                        JsonLex::Obj(kvs2) if kvs2.len() == 2 => kvs2,
                        _ => return Err(WeftError::type_err(r#"$typed payload must be {"v":..., "t":...}"#, "WEFT_TYPE_TYPED")),
                    };
                    let v_node = kvs2.iter().find(|(k, _)| k == "v");
                    let t_node = kvs2.iter().find(|(k, _)| k == "t");
                    let (v_node, t_node) = match (v_node, t_node) {
                        (Some((_, v)), Some((_, t))) => (v, t),
                        _ => return Err(WeftError::type_err(r#"$typed payload must be {"v":..., "t":...}"#, "WEFT_TYPE_TYPED")),
                    };
                    ctx.depth(depth + 1)?;
                    let t_val = from_lex_lossless(t_node, depth + 1, ctx)?;
                    let te = match t_val {
                        Value::Symbol(s) => TypeExpr::Name(s),
                        Value::Sexpr(s) => TypeExpr::Form(s),
                        _ => return Err(WeftError::type_err("$typed.t must resolve to a Symbol or Sexpr", "WEFT_TYPE_TYPED")),
                    };
                    let v_val = from_lex_lossless(v_node, depth + 1, ctx)?;
                    Ok(Value::Typed(Box::new(Typed { value: v_val, type_: te })))
                }
                "$bigint" | "$int-big" => {
                    let sv = match payload {
                        JsonLex::Str(sv) => sv,
                        _ => return Err(WeftError::type_err(format!("{} payload must be a string", tag), "WEFT_TYPE_INT")),
                    };
                    if !is_strict_int_payload(sv) {
                        return Err(WeftError::type_err(
                            format!("{} payload is not a decimal integer: {:?} (must match -?[0-9]+)", tag, sv),
                            if tag == "$bigint" { "WEFT_TYPE_BIGINT" } else { "WEFT_TYPE_INT" }));
                    }
                    let n_digits = sv.trim_start_matches('-').len();
                    if n_digits > ctx.limits.max_int_digits {
                        return Err(WeftError::limit(
                            format!("{} payload has {} digits, exceeds max_int_digits={}", tag, n_digits, ctx.limits.max_int_digits),
                            "WEFT_LIMIT_INT"));
                    }
                    let b: BigInt = sv.parse().map_err(|_| WeftError::type_err(format!("{} payload invalid", tag), "WEFT_TYPE_INT"))?;
                    if tag == "$bigint" { Ok(Value::BigInt(BigIntValue(b))) } else { Ok(Value::Int(b)) }
                }
                "$str" => {
                    let sv = match payload {
                        JsonLex::Str(sv) => sv,
                        _ => return Err(WeftError::type_err("$str payload must be a string", "WEFT_TYPE_STR")),
                    };
                    if sv.len() > ctx.limits.max_string_bytes {
                        return Err(WeftError::limit(
                            format!("$str payload exceeds max_string_bytes={}", ctx.limits.max_string_bytes),
                            "WEFT_LIMIT_STRING"));
                    }
                    Ok(Value::String(sv.clone()))
                }
                "$dec" => {
                    let sv = match payload {
                        JsonLex::Str(sv) => sv,
                        _ => return Err(WeftError::type_err("$dec payload must be a string", "WEFT_TYPE_DEC")),
                    };
                    let digit_count = sv.bytes().filter(|b| b.is_ascii_digit()).count();
                    if digit_count > ctx.limits.max_int_digits {
                        return Err(WeftError::limit(
                            format!("$dec payload has {} digits, exceeds max_int_digits={}", digit_count, ctx.limits.max_int_digits),
                            "WEFT_LIMIT_INT"));
                    }
                    Decimal::new(sv.clone())
                        .map(Value::Decimal)
                        .map_err(|e| WeftError::type_err(format!("$dec payload invalid: {}", e), "WEFT_TYPE_DEC"))
                }
                _ => {
                    // Remaining scalar tags ($sym/$kw/$bin/$dt/$float) share
                    // the serde-based handlers; their payloads are small by
                    // construction, so bridging to JVal is safe.
                    let j = lex_to_jval(node)?;
                    from_json_lossless_ctx(&j, depth, ctx)
                }
            }
        }
    }
}

/// JSON text → Weft tree via the LOSSLESS mapping (SPEC §11.2), with
/// duplicate-key rejection, lexical int/float distinction, insertion-order
/// preservation, and the §9 limits enforced. This is the conforming entry
/// point for wire input; `from_json_lossless(&serde_json::Value)` remains
/// for in-memory values produced by `to_json_lossless`.
pub fn loads_json_lossless(text: &str, limits: Limits) -> Result<Value> {
    let lex = parse_json_lexical(text, limits)?;
    let mut ctx = ConvCtx { limits, total: 0 };
    from_lex_lossless(&lex, 0, &mut ctx)
}

fn from_lex_lossy(node: &JsonLex, depth: usize, ctx: &mut ConvCtx, strict: bool) -> Result<Value> {
    ctx.bump()?;
    match node {
        JsonLex::Null => Ok(Value::Null),
        JsonLex::Bool(b) => Ok(Value::Bool(*b)),
        JsonLex::Num(raw) => {
            // SPEC §11.3: lexical distinction — integer token → Integer,
            // fractional/exponent token → Float.
            if raw.contains(['.', 'e', 'E']) {
                let f: f64 = raw.parse().map_err(|_| WeftError::type_err("invalid float token", "WEFT_TYPE_FLOAT"))?;
                Ok(Value::Float(f))
            } else {
                let b: BigInt = raw.parse().map_err(|_| WeftError::type_err("invalid integer token", "WEFT_TYPE_INT"))?;
                Ok(Value::Int(b))
            }
        }
        JsonLex::Str(s) => Ok(Value::String(s.clone())),
        JsonLex::Arr(xs) => {
            ctx.depth(depth + 1)?;
            ctx.coll(xs.len())?;
            let mut out = Vec::with_capacity(xs.len());
            for x in xs { out.push(from_lex_lossy(x, depth + 1, ctx, strict)?); }
            Ok(Value::List(out))
        }
        JsonLex::Obj(kvs) => {
            ctx.depth(depth + 1)?;
            ctx.coll(kvs.len())?;
            let mut m = indexmap::IndexMap::new();
            for (k, v) in kvs {
                if strict && !is_valid_symbol(k) {
                    return Err(WeftError::type_err(
                        format!("JSON key {:?} is not a valid Weft keyword", k),
                        "WEFT_TYPE_JSON_KEY"));
                }
                m.insert(k.clone(), from_lex_lossy(v, depth + 1, ctx, strict)?);
            }
            Ok(Value::Map(m))
        }
    }
}

/// JSON text → Weft tree via the LOSSY mapping (SPEC §11.3), with the
/// lexical int/float distinction (JSON `1` → Integer, `1.0` → Float),
/// duplicate-key rejection, and the §9 limits enforced.
pub fn loads_json(text: &str, limits: Limits) -> Result<Value> {
    let lex = parse_json_lexical(text, limits)?;
    let mut ctx = ConvCtx { limits, total: 0 };
    from_lex_lossy(&lex, 0, &mut ctx, true)
}
