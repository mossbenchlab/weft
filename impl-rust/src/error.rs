//! Weft error type.
//!
//! One `WeftError` type with a `WeftErrorKind` discriminator keeps the
//! public surface small while allowing callers to distinguish syntax,
//! lexical, encoding, limit, and type errors.

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WeftErrorKind {
    Syntax,
    Lex,
    Encoding,
    Limit,
    Type,
    Schema,
}

#[derive(Debug, Clone)]
pub struct WeftError {
    pub kind: WeftErrorKind,
    pub message: String,
    pub code: Option<String>,
    pub line: Option<usize>,
    pub column: Option<usize>,
    /// Character offset into the input (equivalent to `.chars().count()`).
    pub offset: Option<usize>,
    /// UTF-8 byte offset into the input. Authoritative for pointing into
    /// an on-disk file; coincides with `offset` on ASCII input.
    pub byte_offset: Option<usize>,
}

impl WeftError {
    pub fn new(kind: WeftErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into(), code: None, line: None, column: None, offset: None, byte_offset: None }
    }

    pub fn with_code(mut self, code: impl Into<String>) -> Self {
        self.code = Some(code.into());
        self
    }

    pub fn with_pos(mut self, line: usize, column: usize, offset: usize) -> Self {
        self.line = Some(line);
        self.column = Some(column);
        self.offset = Some(offset);
        self
    }

    pub fn with_byte_offset(mut self, byte_offset: usize) -> Self {
        self.byte_offset = Some(byte_offset);
        self
    }

    pub fn syntax(msg: impl Into<String>, code: &str) -> Self {
        Self::new(WeftErrorKind::Syntax, msg).with_code(code)
    }

    pub fn lex(msg: impl Into<String>, code: &str) -> Self {
        Self::new(WeftErrorKind::Lex, msg).with_code(code)
    }

    pub fn encoding(msg: impl Into<String>, code: &str) -> Self {
        Self::new(WeftErrorKind::Encoding, msg).with_code(code)
    }

    pub fn limit(msg: impl Into<String>, code: &str) -> Self {
        Self::new(WeftErrorKind::Limit, msg).with_code(code)
    }

    pub fn type_err(msg: impl Into<String>, code: &str) -> Self {
        Self::new(WeftErrorKind::Type, msg).with_code(code)
    }
}

impl fmt::Display for WeftError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let (Some(l), Some(c)) = (self.line, self.column) {
            write!(f, "[line {}:{}] {}", l, c, self.message)
        } else {
            f.write_str(&self.message)
        }
    }
}

impl std::error::Error for WeftError {}

pub type Result<T> = std::result::Result<T, WeftError>;
