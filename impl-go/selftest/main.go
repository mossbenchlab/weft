package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	weft "github.com/mossbenchlab/weft/impl-go"
)

func must(cond bool, msg string) { if !cond { panic(msg) } }

func main() {
	// W5-02: 深いJSONが WEFT_LIMIT_DEPTH で止まる（プロセスabortしない）
	deep := strings.Repeat(`{"$list":[`, 200000) + "null" + strings.Repeat("]}", 200000)
	_, err := weft.LoadsJSONLossless(deep)
	we, ok := err.(*weft.Error)
	must(ok && we.Code == "WEFT_LIMIT_DEPTH", fmt.Sprintf("deep: %v", err))

	// dup key コード
	_, err = weft.LoadsJSONLossless(`{"$map":{"a":1,"a":2}}`)
	we, ok = err.(*weft.Error)
	must(ok && we.Code == "WEFT_SYNTAX_JSON_DUPKEY", fmt.Sprintf("dup: %v", err))

	// lossy lexical
	v, err := weft.LoadsJSON("1")
	must(err == nil, "loads 1")
	_, isInt := v.(interface{ Int64() (int64, bool) })
	_ = isInt
	must(fmt.Sprintf("%T", v) == "*big.Int", fmt.Sprintf("1 -> %T", v))
	v, _ = weft.LoadsJSON("1.0")
	must(fmt.Sprintf("%T", v) == "float64", fmt.Sprintf("1.0 -> %T", v))

	// 順序保持 roundtrip
	p, _ := weft.Parse("{:b 1 :a 2}")
	b, _ := json.Marshal(weft.ToJSONLossless(p))
	v2, err := weft.LoadsJSONLossless(string(b))
	must(err == nil, fmt.Sprintf("rt: %v", err))
	out := weft.FormatValue(v2, weft.FormatOptions{MaxWidth: 80, Indent: 2})
	must(out == "{:b 1 :a 2}\n", "order: "+out)
	must(weft.Equals(p, v2), "equals rt")

	// equals §5.16
	must(!weft.Equals(0.0, weft.NegZero()), "neg zero")
	i1, _ := weft.Parse("42")
	i2, _ := weft.Parse("42n")
	must(weft.Equals(i1, i2), "42 ≡ 42n")
	d1, _ := weft.Parse("+1.5m")
	d2, _ := weft.Parse("1.5m")
	must(weft.Equals(d1, d2), "+1.5m ≡ 1.5m")

	// 幅: CJK 70字 1行
	cjk, _ := weft.Parse("[\"" + strings.Repeat("あ", 70) + "\"]")
	out = weft.FormatValue(cjk, weft.FormatOptions{MaxWidth: 80, Indent: 2})
	must(!strings.Contains(strings.TrimRight(out, "\n"), "\n"), "cjk one line")

	// datetime `"` boundary
	_, err = weft.Parse("[#2026-11-02\"a\"]")
	must(err == nil, "dt-quote boundary")

		// --- 1.0.1-rc2 regressions ---
	// direct value-level round trip (no marshal step)
	{
		v, _ := weft.Parse("{:a 1 :b [1.5 2n]}")
		v2, err := weft.FromJSONLossless(weft.ToJSONLossless(v))
		if err != nil || !weft.Equals(v, v2) {
			fmt.Println("FAIL direct FromJSONLossless(ToJSONLossless) round trip:", err)
			os.Exit(1)
		}
	}
	// lossy loader must reject trailing garbage
	if _, err := weft.LoadsJSON("1 x"); err == nil {
		fmt.Println("FAIL LoadsJSON accepted trailing token")
		os.Exit(1)
	}
	// dumps/loads lossless symmetry
	{
		v, _ := weft.Parse("\"abc\"")
		b, _ := weft.DumpJSONLossless(v)
		v2, err := weft.LoadsJSONLossless(string(b))
		if err != nil || !weft.Equals(v, v2) {
			fmt.Println("FAIL DumpJSONLossless/LoadsJSONLossless round trip")
			os.Exit(1)
		}
	}
	// tag overhead must not count against Weft limits
	{
		lim := weft.DefaultLimits()
		lim.MaxItemsPerColl = 1
		sx, _ := weft.ParseWithLimits("(f 0)", lim)
		b, _ := weft.DumpJSONLossless(sx)
		v2, err := weft.LoadsJSONLosslessWithLimits(string(b), lim)
		if err != nil || !weft.Equals(sx, v2) {
			fmt.Println("FAIL sexpr round trip under MaxItemsPerColl=1:", err)
			os.Exit(1)
		}
	}
	// invalid UTF-8 host string rejected at the value-level entry
	if _, err := weft.FromJSON(string([]byte{0xED, 0xA0, 0x80})); err == nil {
		fmt.Println("FAIL FromJSON accepted WTF-8 lone surrogate bytes")
		os.Exit(1)
	}
	// raw DEL rejected
	if _, err := weft.Parse("r\"a\x7fb\""); err == nil {
		fmt.Println("FAIL raw DEL accepted")
		os.Exit(1)
	}
	fmt.Println("go selftest OK")
}
