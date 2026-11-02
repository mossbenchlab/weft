// Conformance driver (Rust implementation). See driver.py for protocol.
use std::io::Read;
use weft::*;

fn stable(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::Array(a) => {
            let parts: Vec<String> = a.iter().map(stable).collect();
            format!("[{}]", parts.join(","))
        }
        serde_json::Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys.iter()
                .map(|k| format!("{}:{}", serde_json::to_string(k).unwrap(), stable(&o[*k])))
                .collect();
            format!("{{{}}}", parts.join(","))
        }
        _ => serde_json::to_string(v).unwrap(),
    }
}

fn perr(e: WeftError) {
    println!("ERR {}", e.code.as_deref().unwrap_or("WEFT"));
}

fn main() {
    let mode = std::env::args().nth(1).unwrap();
    let mut data = String::new();
    std::io::stdin().read_to_string(&mut data).unwrap();
    let strict = FormatOptions { strict_canonical: true, ..Default::default() };
    match mode.as_str() {
        "canon" | "canon2" => match parse(&data) {
            Err(e) => perr(e),
            Ok(v) => {
                let mut out = format_value(&v, strict);
                if mode == "canon2" {
                    match parse(&out) {
                        Err(e) => { perr(e); return; }
                        Ok(v2) => out = format_value(&v2, strict),
                    }
                }
                print!("OK\n{}", out);
            }
        },
        "fmt" => match parse(&data) {
            Err(e) => perr(e),
            Ok(v) => print!("OK\n{}", format_value(&v, FormatOptions::default())),
        },
        "jroundtrip" => match parse(&data) {
            Err(e) => perr(e),
            Ok(v) => {
                let txt = serde_json::to_string(&to_json_lossless(&v)).unwrap();
                match loads_json_lossless(&txt, Limits::default()) {
                    Err(e) => perr(e),
                    Ok(v2) => {
                        let c1 = format_value(&v, strict);
                        let c2 = format_value(&v2, strict);
                        if c1 != c2 {
                            println!("CRASH lossless identity violated (canonical bytes)");
                            return;
                        }
                        // §5.16: NaN ≢ NaN, so equals() is only asserted for NaN-free docs.
                        if !c1.contains("nan") && !equals(&v, &v2) {
                            println!("CRASH lossless identity violated (equals)");
                            return;
                        }
                        print!("OK\n{}", format_value(&v2, strict));
                    }
                }
            }
        },
        "tolossless" => match parse(&data) {
            Err(e) => perr(e),
            Ok(v) => println!("OK\n{}", stable(&to_json_lossless(&v))),
        },
        "fromlossless" => match loads_json_lossless(&data, Limits::default()) {
            Err(e) => perr(e),
            Ok(v) => print!("OK\n{}", format_value(&v, strict)),
        },
        _ => std::process::exit(2),
    }
}
