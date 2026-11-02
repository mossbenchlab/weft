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

    println!("rust selftest OK");
}
