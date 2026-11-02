//! Weft 1.0 — Rust reference implementation.
//!
//! Parses, formats, and interconverts Weft documents and JSON. Mirrors the
//! Python reference implementation; passes the language-neutral conformance
//! suite in `tests/conformance/`.

#![warn(missing_debug_implementations)]
#![warn(rust_2018_idioms)]

pub mod error;
pub mod limits;
pub mod types;
pub mod lexer;
pub mod parser;
pub mod formatter;
pub mod json;

pub use error::{WeftError, WeftErrorKind};
pub use limits::Limits;
pub use types::{equals, BigIntValue, Binary, Keyword, Sexpr, Symbol, Typed, Value, WeftDate, Decimal};
pub use parser::{parse, parse_stream};
pub use formatter::{format_value, format_inline, FormatOptions};
pub use json::{to_json, to_json_lossless, from_json, from_json_lossless, loads_json, loads_json_lossless, parse_json_lexical, JsonLex};

pub const VERSION: &str = "1.0.1";
pub const SPEC_VERSION: &str = "1.0";
