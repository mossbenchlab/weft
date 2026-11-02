#!/usr/bin/env node
// TypeScript implementation self-test (runs against the compiled out/index.js).
// Regression checks for every bug class fixed in 1.0.1.
"use strict";
const path = require("path");
const weft = require(path.join(__dirname, "..", "dist", "index.js"));

let fails = 0;
function check(name, cond) {
  if (cond) console.log("ok   " + name);
  else { fails++; console.log("FAIL " + name); }
}
function canon(text) {
  return weft.formatValue(weft.parse(text), { strict_canonical: true }).replace(/\n$/, "");
}
function expectErr(name, fn, code) {
  try { fn(); } catch (e) {
    check(name, code === undefined || e.code === code);
    return;
  }
  check(name + " (no error thrown)", false);
}

// --- lossless JSON reader hardening -------------------------------------
expectErr("json dup keys rejected",
  () => weft.loadsJson('{"$map": {"a": 1, "a": 2}}'), "WEFT_SYNTAX_JSON_DUPKEY");
expectErr("json huge int digits rejected",
  () => weft.loadsJson('{"$bigint": "1' + "0".repeat(10001) + '"}'));
expectErr("json deep nesting early-rejected",
  () => weft.loadsJson("[".repeat(5000) + "]".repeat(5000)), "WEFT_LIMIT_DEPTH");
expectErr("json lone surrogate in $str rejected",
  () => weft.loadsJson('{"$str": "\\ud800"}'));

// lexical 1 vs 1.0 via the token-level reader (lossy mapping)
const one = weft.loadsJson("1", { lossless: false });
const onef = weft.loadsJson("1.0", { lossless: false });
check("json 1 vs 1.0 lexical",
  weft.formatValue(one).trim() === "1" && weft.formatValue(onef).trim() === "1.0");

// depth-128 Weft doc survives the JSON round trip
const deep = "[".repeat(128) + "]".repeat(128);
const v = weft.parse(deep);
const jt = JSON.stringify(weft.toJsonLossless(v));
check("depth-128 lossless round trip",
  weft.formatValue(weft.loadsJson(jt), { strict_canonical: true }).replace(/\n$/, "") === canon(deep));

// --- prototype pollution -------------------------------------------------
const polluted = weft.toJson(weft.parse('{:__proto__ 1 :constructor 2}'));
check("toJson __proto__ is own data property",
  Object.getOwnPropertyNames(polluted).includes("__proto__") &&
  Object.getPrototypeOf(polluted) === null &&
  ({}).x === undefined);

// --- equality (§5.16) ----------------------------------------------------
check("equals: Integer == BigInt", weft.equals(weft.parse("5"), weft.parse("5n")));
check("equals: NaN != NaN", !weft.equals(weft.parse("nan"), weft.parse("nan")));
check("equals: -0.0 != 0.0", !weft.equals(weft.parse("-0.0"), weft.parse("0.0")));
check("equals: decimal +1.5m == 1.5m", weft.equals(weft.parse("+1.5m"), weft.parse("1.5m")));

// --- canonical formatting -------------------------------------------------
check("float shortest round trip", canon("0.3") === "0.3");
check("strict escapes \\u{...}", canon('"\\u{1}"') === '"\\u{1}"');
check("datetime quote boundary", canon("#2024-01-01") === "#2024-01-01");
check("width counts code points", weft.formatValue(weft.parse('["あいうえお"]')).length > 0);

// --- parser limits ---------------------------------------------------------
expectErr("weft deep nesting early-rejected",
  () => weft.parse("[".repeat(100000)), "WEFT_LIMIT_DEPTH");
expectErr("weft huge int digits rejected",
  () => weft.parse("1".repeat(20000)), "WEFT_LIMIT_INT");

console.log();
if (fails) { console.log(fails + " FAILURES"); process.exit(1); }
console.log("ALL TYPESCRIPT SELF-TESTS PASSED");
