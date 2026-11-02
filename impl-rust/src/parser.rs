//! Weft parser (Rust).

use crate::error::{Result, WeftError};
use crate::lexer::{Lexer, Token, TokenKind};
use crate::limits::Limits;
use crate::types::{Keyword, Sexpr, Symbol, TypeExpr, Typed, Value};
use indexmap::IndexMap;

pub fn parse(text: &str) -> Result<Value> {
    parse_with_limits(text, Limits::default())
}

pub fn parse_with_limits(text: &str, limits: Limits) -> Result<Value> {
    limits.validate()?;
    let toks = Lexer::new(text, limits)?.tokenize()?;
    let mut p = ParserState::new(toks, limits);
    if p.peek().is_none() {
        return Err(WeftError::syntax("empty document", "WEFT_SYNTAX_EMPTY"));
    }
    let v = p.parse_value()?;
    if let Some(tok) = p.peek() {
        return Err(WeftError::syntax("unexpected content after top-level value", "WEFT_SYNTAX_LEFTOVER")
            .with_pos(tok.line, tok.column, tok.offset));
    }
    check_tree_depth(&v, limits.max_depth)?;
    Ok(v)
}

pub fn parse_stream(text: &str) -> Result<Vec<Value>> {
    parse_stream_with_limits(text, Limits::default())
}

pub fn parse_stream_with_limits(text: &str, limits: Limits) -> Result<Vec<Value>> {
    limits.validate()?;
    let toks = Lexer::new(text, limits)?.tokenize()?;
    let mut p = ParserState::new(toks, limits);
    let mut out = Vec::new();
    while p.peek().is_some() {
        let v = p.parse_value()?;
        check_tree_depth(&v, limits.max_depth)?;
        out.push(v);
    }
    Ok(out)
}

/// Walk the parsed tree iteratively and refuse any value whose nesting
/// depth exceeds `max_depth`. Each list, map, sexpr, or Typed wrapper
/// adds one level; scalars do not. The in-parser depth counter only
/// tracks balanced bracket nesting; a chain of `::T` annotations on the
/// way out of a container adds structural depth that this walk covers.
fn check_tree_depth(value: &Value, max_depth: usize) -> Result<()> {
    let mut stack: Vec<(&Value, usize)> = vec![(value, 0)];
    while let Some((node, d)) = stack.pop() {
        match node {
            Value::List(xs) => {
                let nd = d + 1;
                if nd > max_depth {
                    return Err(WeftError::limit(
                        format!("tree depth {} exceeds max_depth={}", nd, max_depth),
                        "WEFT_LIMIT_DEPTH"));
                }
                for item in xs { stack.push((item, nd)); }
            }
            Value::Map(m) => {
                let nd = d + 1;
                if nd > max_depth {
                    return Err(WeftError::limit(
                        format!("tree depth {} exceeds max_depth={}", nd, max_depth),
                        "WEFT_LIMIT_DEPTH"));
                }
                for v in m.values() { stack.push((v, nd)); }
            }
            Value::Sexpr(s) => {
                let nd = d + 1;
                if nd > max_depth {
                    return Err(WeftError::limit(
                        format!("tree depth {} exceeds max_depth={}", nd, max_depth),
                        "WEFT_LIMIT_DEPTH"));
                }
                for item in &s.items { stack.push((item, nd)); }
            }
            Value::Typed(_) => {
                // Unroll chained ::T annotations in a tight loop. Each
                // wrapper's type expression may itself hold nested data
                // (``(T [[[...]]])``), so when the type is a Sexpr its
                // items must count against max_depth just like the value
                // side — a leaf Symbol type does not.
                let mut dd = d;
                let mut cur: &Value = node;
                while let Value::Typed(inner) = cur {
                    dd += 1;
                    if dd > max_depth {
                        return Err(WeftError::limit(
                            format!("tree depth {} exceeds max_depth={}", dd, max_depth),
                            "WEFT_LIMIT_DEPTH"));
                    }
                    if let TypeExpr::Form(sx) = &inner.type_ {
                        // Treat the type-Sexpr like the Sexpr arm above:
                        // items are at ``dd + 1``.
                        let nd = dd + 1;
                        if nd > max_depth {
                            return Err(WeftError::limit(
                                format!("tree depth {} exceeds max_depth={}", nd, max_depth),
                                "WEFT_LIMIT_DEPTH"));
                        }
                        for item in &sx.items { stack.push((item, nd)); }
                    }
                    cur = &inner.value;
                }
                stack.push((cur, dd));
            }
            _ => {} // scalars contribute nothing to depth
        }
    }
    Ok(())
}

struct ParserState {
    tokens: Vec<Token>,
    pos: usize,
    limits: Limits,
    total: usize,
    depth: usize,
}

impl ParserState {
    fn new(tokens: Vec<Token>, limits: Limits) -> Self {
        Self { tokens, pos: 0, limits, total: 0, depth: 0 }
    }

    fn peek(&self) -> Option<&Token> { self.tokens.get(self.pos) }

    fn advance(&mut self) -> &Token {
        let i = self.pos;
        self.pos += 1;
        &self.tokens[i]
    }

    fn bump_total(&mut self) -> Result<()> {
        self.total += 1;
        if self.total > self.limits.max_total_items {
            return Err(WeftError::limit(
                format!("exceeds max_total_items={}", self.limits.max_total_items),
                "WEFT_LIMIT_TOTAL"
            ));
        }
        Ok(())
    }

    fn enter(&mut self) -> Result<()> {
        self.depth += 1;
        if self.depth > self.limits.max_depth {
            return Err(WeftError::limit(
                format!("nesting exceeds max_depth={}", self.limits.max_depth),
                "WEFT_LIMIT_DEPTH"
            ));
        }
        Ok(())
    }
    fn leave(&mut self) { self.depth -= 1; }

    fn parse_value(&mut self) -> Result<Value> {
        {
            let _tok = self.peek().ok_or_else(|| WeftError::syntax("unexpected end", "WEFT_SYNTAX_EOF"))?;
        }
        self.bump_total()?;
        let tok = self.peek().unwrap();
        let line = tok.line; let col = tok.column; let off = tok.offset;
        let mut value: Value = match &tok.kind {
            TokenKind::Int(_) | TokenKind::BigInt(_) | TokenKind::Float(_)
            | TokenKind::Decimal(_) | TokenKind::Str(_) | TokenKind::Binary(_)
            | TokenKind::DateTime(_) | TokenKind::Bool(_) | TokenKind::Nil
            | TokenKind::Nan | TokenKind::Inf | TokenKind::NegInf => {
                let tok = self.advance().clone();
                match tok.kind {
                    TokenKind::Int(b) => Value::Int(b),
                    TokenKind::BigInt(b) => Value::BigInt(b),
                    TokenKind::Float(f) => Value::Float(f),
                    TokenKind::Decimal(d) => Value::Decimal(d),
                    TokenKind::Str(s) => Value::String(s),
                    TokenKind::Binary(b) => Value::Binary(b),
                    TokenKind::DateTime(d) => Value::DateTime(d),
                    TokenKind::Bool(b) => Value::Bool(b),
                    TokenKind::Nil => Value::Null,
                    TokenKind::Nan => Value::Float(f64::NAN),
                    TokenKind::Inf => Value::Float(f64::INFINITY),
                    TokenKind::NegInf => Value::Float(f64::NEG_INFINITY),
                    _ => unreachable!(),
                }
            }
            TokenKind::Symbol(_) => {
                let tok = self.advance().clone();
                if let TokenKind::Symbol(s) = tok.kind { Value::Symbol(Symbol(s)) } else { unreachable!() }
            }
            TokenKind::Keyword(_) => {
                let tok = self.advance().clone();
                if let TokenKind::Keyword(k) = tok.kind { Value::Keyword(Keyword(k)) } else { unreachable!() }
            }
            TokenKind::LParen => Value::Sexpr(self.parse_sexpr(line, col, off)?),
            TokenKind::LBrack => Value::List(self.parse_list(line, col, off)?),
            TokenKind::LBrace => Value::Map(self.parse_map(line, col, off)?),
            _ => {
                let t = tok.clone();
                return Err(WeftError::syntax(format!("unexpected {:?}", t.kind), "WEFT_SYNTAX")
                    .with_pos(t.line, t.column, t.offset));
            }
        };

        // Type annotations count against max_depth so a long chain like
        // `0::T::T::T::…` cannot bypass the limit. We track the depth we
        // pushed so we can unwind it on both the success and error paths.
        let mut annot_count: usize = 0;
        let outcome: Result<Value> = loop {
            match self.peek() {
                Some(tok) if matches!(tok.kind, TokenKind::ColonColon) => {}
                _ => break Ok(value),
            }
            self.advance();
            if let Err(e) = self.enter() { break Err(e); }
            annot_count += 1;
            let te = match self.parse_type_expr() {
                Ok(t) => t,
                Err(e) => break Err(e),
            };
            value = Value::Typed(Box::new(Typed { value, type_: te }));
            if let Err(e) = self.bump_total() { break Err(e); }
        };
        for _ in 0..annot_count { self.leave(); }
        outcome
    }

    fn parse_sexpr(&mut self, open_line: usize, open_col: usize, open_off: usize) -> Result<Sexpr> {
        self.advance();
        self.enter()?;
        let result = (|| -> Result<Sexpr> {
            let tok = self.peek().ok_or_else(|| WeftError::syntax("unterminated '('", "WEFT_SYNTAX_UNCLOSED")
                .with_pos(open_line, open_col, open_off))?;
            let tag_sym = if let TokenKind::Symbol(s) = &tok.kind { s.clone() } else {
                let t = tok.clone();
                return Err(WeftError::syntax("sexpr tag must be a symbol", "WEFT_SYNTAX_SEXPR_TAG")
                    .with_pos(t.line, t.column, t.offset));
            };
            self.advance();
            let tag = Symbol(tag_sym);
            let mut items = Vec::new();
            loop {
                let t = self.peek().ok_or_else(|| WeftError::syntax("unterminated '('", "WEFT_SYNTAX_UNCLOSED")
                    .with_pos(open_line, open_col, open_off))?;
                if matches!(t.kind, TokenKind::RParen) { self.advance(); break; }
                items.push(self.parse_value()?);
                if items.len() > self.limits.max_items_per_coll {
                    return Err(WeftError::limit("sexpr too many items", "WEFT_LIMIT_ITEMS"));
                }
            }
            Ok(Sexpr { tag, items })
        })();
        self.leave();
        result
    }

    fn parse_list(&mut self, open_line: usize, open_col: usize, open_off: usize) -> Result<Vec<Value>> {
        self.advance();
        self.enter()?;
        let result = (|| -> Result<Vec<Value>> {
            let mut items = Vec::new();
            loop {
                let t = self.peek().ok_or_else(|| WeftError::syntax("unterminated '['", "WEFT_SYNTAX_UNCLOSED")
                    .with_pos(open_line, open_col, open_off))?;
                if matches!(t.kind, TokenKind::RBrack) { self.advance(); break; }
                items.push(self.parse_value()?);
                if items.len() > self.limits.max_items_per_coll {
                    return Err(WeftError::limit("list too many items", "WEFT_LIMIT_ITEMS"));
                }
            }
            Ok(items)
        })();
        self.leave();
        result
    }

    fn parse_map(&mut self, open_line: usize, open_col: usize, open_off: usize) -> Result<IndexMap<String, Value>> {
        self.advance();
        self.enter()?;
        let result = (|| -> Result<IndexMap<String, Value>> {
            let mut m = IndexMap::new();
            let mut count = 0usize;
            loop {
                let t = self.peek().ok_or_else(|| WeftError::syntax("unterminated '{'", "WEFT_SYNTAX_UNCLOSED")
                    .with_pos(open_line, open_col, open_off))?;
                if matches!(t.kind, TokenKind::RBrace) { self.advance(); break; }
                let key = match &t.kind {
                    TokenKind::Keyword(k) => k.clone(),
                    _ => {
                        let t = t.clone();
                        return Err(WeftError::syntax("map key must be a keyword", "WEFT_SYNTAX_MAP_KEY")
                            .with_pos(t.line, t.column, t.offset));
                    }
                };
                let key_line = t.line; let key_col = t.column; let key_off = t.offset;
                self.advance();
                self.bump_total()?;
                let vtok = self.peek();
                if vtok.map_or(true, |t| matches!(t.kind, TokenKind::RBrace)) {
                    return Err(WeftError::syntax(format!("map key :{} has no value", key), "WEFT_SYNTAX_MAP_VALUE")
                        .with_pos(key_line, key_col, key_off));
                }
                let v = self.parse_value()?;
                if m.contains_key(&key) {
                    return Err(WeftError::syntax(format!("duplicate map key :{}", key), "WEFT_SYNTAX_MAP_DUPKEY")
                        .with_pos(key_line, key_col, key_off));
                }
                m.insert(key, v);
                count += 1;
                if count > self.limits.max_items_per_coll {
                    return Err(WeftError::limit("map too many items", "WEFT_LIMIT_ITEMS"));
                }
            }
            Ok(m)
        })();
        self.leave();
        result
    }

    fn parse_type_expr(&mut self) -> Result<TypeExpr> {
        let tok = self.peek().ok_or_else(|| WeftError::syntax("type expr expected after '::'", "WEFT_SYNTAX_TYPE"))?;
        let line = tok.line; let col = tok.column; let off = tok.offset;
        match &tok.kind {
            TokenKind::Symbol(s) => {
                let s = s.clone();
                self.advance();
                Ok(TypeExpr::Name(Symbol(s)))
            }
            TokenKind::LParen => {
                let sx = self.parse_sexpr(line, col, off)?;
                Ok(TypeExpr::Form(sx))
            }
            _ => {
                let t = tok.clone();
                Err(WeftError::syntax("type expression must be a symbol or sexpr", "WEFT_SYNTAX_TYPE")
                    .with_pos(t.line, t.column, t.offset))
            }
        }
    }
}
