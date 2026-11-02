//! Resource limits (SPEC §9).

use crate::error::{WeftError, Result};

pub const DEFAULT_MAX_DEPTH: usize = 128;
pub const DEFAULT_MAX_SIZE_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_MAX_ITEMS_PER_COLL: usize = 1 << 20;
pub const DEFAULT_MAX_STRING_BYTES: usize = 10 * 1024 * 1024;
pub const DEFAULT_MAX_INT_DIGITS: usize = 10_000;  // spec §9 mandatory default
pub const DEFAULT_MAX_TOTAL_ITEMS: usize = 10_000_000;

pub const HARD_MAX_DEPTH: usize = 1024;
pub const HARD_MAX_SIZE_BYTES: usize = 1 << 30;
pub const HARD_MAX_ITEMS_PER_COLL: usize = 1 << 24;
pub const HARD_MAX_STRING_BYTES: usize = 1 << 30;
pub const HARD_MAX_INT_DIGITS: usize = 1_000_000;
pub const HARD_MAX_TOTAL_ITEMS: usize = 100_000_000;

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub max_depth: usize,
    pub max_size_bytes: usize,
    pub max_items_per_coll: usize,
    pub max_string_bytes: usize,
    pub max_int_digits: usize,
    pub max_total_items: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_depth: DEFAULT_MAX_DEPTH,
            max_size_bytes: DEFAULT_MAX_SIZE_BYTES,
            max_items_per_coll: DEFAULT_MAX_ITEMS_PER_COLL,
            max_string_bytes: DEFAULT_MAX_STRING_BYTES,
            max_int_digits: DEFAULT_MAX_INT_DIGITS,
            max_total_items: DEFAULT_MAX_TOTAL_ITEMS,
        }
    }
}

impl Limits {
    pub fn validate(&self) -> Result<()> {
        let checks: [(&str, usize, usize, usize); 6] = [
            ("max_depth", self.max_depth, 1, HARD_MAX_DEPTH),
            ("max_size_bytes", self.max_size_bytes, 1, HARD_MAX_SIZE_BYTES),
            ("max_items_per_coll", self.max_items_per_coll, 1, HARD_MAX_ITEMS_PER_COLL),
            ("max_string_bytes", self.max_string_bytes, 1, HARD_MAX_STRING_BYTES),
            ("max_int_digits", self.max_int_digits, 1, HARD_MAX_INT_DIGITS),
            ("max_total_items", self.max_total_items, 1, HARD_MAX_TOTAL_ITEMS),
        ];
        for (name, v, lo, hi) in checks {
            if v < lo {
                return Err(WeftError::limit(format!("{}={} below minimum {}", name, v, lo), "WEFT_LIMIT_CONFIG"));
            }
            if v > hi {
                return Err(WeftError::limit(format!("{}={} above hard maximum {}", name, v, hi), "WEFT_LIMIT_CONFIG"));
            }
        }
        Ok(())
    }
}
