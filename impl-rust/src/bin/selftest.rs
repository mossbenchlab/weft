use weft::*;
fn main() {
    // W5-04: float tie — py/ts/go と同じ側（真値に近い側）を選ぶこと
    let f: f64 = "-887777373534812.2".parse().unwrap();
    let out = format_value(&parse("[-887777373534812.2]").unwrap(),
        FormatOptions { strict_canonical: true, ..Default::default() });
    assert_eq!(out, "[-887777373534812.2]\n", "tie pick: got {}", out);
    let _ = f;

    // W5-03: U+0345 (Mn, Other_Alphabetic) は拒否
    assert!(parse("a\u{345}b").is_err(), "U+0345 must be rejected");
    // L* は受理（補助面含む）
    assert!(parse("{:\u{1D400}x 1}").is_ok());

    // lexical reader: dup keys / 1.0 / 深いネスト / 順序保持
    assert!(loads_json_lossless(r#"{"$map":{"a":1,"a":2}}"#, Limits::default()).is_err());
    assert!(loads_json_lossless("1.0", Limits::default()).is_err());
    assert!(loads_json_lossless("1", Limits::default()).is_ok());
    let deep = format!("{}null{}", r#"{"$list":["#.repeat(100000), "]}".repeat(100000));
    match loads_json_lossless(&deep, Limits::default()) {
        Err(e) => assert_eq!(e.code.as_deref(), Some("WEFT_LIMIT_DEPTH"), "{:?}", e.code),
        Ok(_) => panic!("deep accepted"),
    }
    let v = parse("{:b 1 :a 2}").unwrap();
    let txt = serde_json::to_string(&to_json_lossless(&v)).unwrap();
    let v2 = loads_json_lossless(&txt, Limits::default()).unwrap();
    let fmt = format_value(&v2, FormatOptions::default());
    assert_eq!(fmt, "{:b 1 :a 2}\n", "order: got {}", fmt);
    assert!(equals(&v, &v2));

    // lossy lexical: 1 → Int, 1.5 → Float
    assert!(matches!(loads_json("1", Limits::default()).unwrap(), Value::Int(_)));
    assert!(matches!(loads_json("1.5", Limits::default()).unwrap(), Value::Float(_)));

    // equals §5.16
    assert!(!equals(&Value::Float(0.0), &Value::Float(-0.0)));
    assert!(!equals(&Value::Float(f64::NAN), &Value::Float(f64::NAN)));
    assert!(equals(&parse("42").unwrap(), &parse("42n").unwrap()));
    assert!(equals(&parse("+1.5m").unwrap(), &parse("1.5m").unwrap()));

    // 幅: CJK 70文字はコードポイント計上で1行
    let cjk = format!("[\"{}\"]", "あ".repeat(70));
    let out = format_value(&parse(&cjk).unwrap(), FormatOptions::default());
    assert!(!out.trim_end().contains('\n'), "cjk should be one line: {}", out);

    // datetime `"` boundary
    assert!(parse("[#2026-11-02\"a\"]").is_ok());

    // --- 1.0.1-rc2 regressions ---
    // value-level entry limits
    {
        let mut lim = Limits::default();
        lim.max_string_bytes = 1;
        let j: serde_json::Value = serde_json::json!("ab");
        assert!(weft::from_json_with_limits(&j, lim).is_err(), "from_json string limit");
        let mut lim2 = Limits::default();
        lim2.max_int_digits = 1;
        let j2: serde_json::Value = serde_json::json!({"$bigint": "123"});
        assert!(weft::from_json_lossless_with_limits(&j2, lim2).is_err(), "lossless bigint digits");
    }
    // deep value tree -> WEFT_LIMIT_DEPTH, not a stack crash
    {
        let mut deep = serde_json::json!(0);
        for _ in 0..2000 { deep = serde_json::json!([deep]); }
        assert!(weft::from_json(&deep).is_err(), "deep from_json must hit depth limit");
        std::mem::forget(deep); // serde_json's own Drop is recursive
    }
    // raw/triple DEL/C1 rejected; dquote keeps accepting literal DEL
    assert!(parse("r\"a\u{7f}b\"").is_err(), "raw DEL");
    assert!(parse("\"\"\"a\u{85}b\"\"\"").is_err(), "triple C1");
    assert!(parse("\"a\u{7f}b\"").is_ok(), "dquote literal DEL stays legal");
    println!("rust selftest OK");
}
