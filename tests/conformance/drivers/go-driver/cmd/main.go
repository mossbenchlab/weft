// Conformance driver (Go implementation). See driver.py for protocol.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	weft "github.com/mossbenchlab/weft/impl-go"
)

func stable(x any) string {
	switch v := x.(type) {
	case nil:
		return "null"
	case bool:
		if v { return "true" }
		return "false"
	case json.Number:
		return v.String()
	case float64:
		b, _ := json.Marshal(v)
		return string(b)
	case int64:
		return fmt.Sprintf("%d", v)
	case string:
		b, _ := json.Marshal(v)
		return string(b)
	case []any:
		parts := make([]string, len(v))
		for i, e := range v { parts[i] = stable(e) }
		return "[" + strings.Join(parts, ",") + "]"
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v { keys = append(keys, k) }
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			kb, _ := json.Marshal(k)
			parts = append(parts, string(kb)+":"+stable(v[k]))
		}
		return "{" + strings.Join(parts, ",") + "}"
	default:
		b, err := json.Marshal(v)
		if err != nil { return "<unmarshalable>" }
		var y any
		json.Unmarshal(b, &y)
		return stable(y)
	}
}

func werr(err error) {
	if e, ok := err.(*weft.Error); ok {
		code := e.Code
		if code == "" { code = "WEFT" }
		fmt.Println("ERR " + code)
	} else {
		fmt.Printf("CRASH %T: %.160s\n", err, err.Error())
	}
}

func main() {
	defer func() {
		if r := recover(); r != nil {
			fmt.Printf("CRASH panic: %.160s\n", fmt.Sprint(r))
		}
	}()
	mode := os.Args[1]
	raw, _ := io.ReadAll(os.Stdin)
	data := string(raw)
	strict := weft.FormatOptions{MaxWidth: 80, Indent: 2, StrictCanonical: true}
	switch mode {
	case "canon", "canon2":
		v, err := weft.Parse(data)
		if err != nil { werr(err); return }
		out := weft.FormatValue(v, strict)
		if mode == "canon2" {
			v2, err := weft.Parse(out)
			if err != nil { werr(err); return }
			out = weft.FormatValue(v2, strict)
		}
		fmt.Print("OK\n" + out)
	case "fmt":
		v, err := weft.Parse(data)
		if err != nil { werr(err); return }
		fmt.Print("OK\n" + weft.FormatValue(v, weft.FormatOptions{MaxWidth: 80, Indent: 2}))
	case "jroundtrip":
		v, err := weft.Parse(data)
		if err != nil { werr(err); return }
		b, err := json.Marshal(weft.ToJSONLossless(v))
		if err != nil { fmt.Printf("CRASH marshal: %v\n", err); return }
		v2, err := weft.LoadsJSONLossless(string(b))
		if err != nil { werr(err); return }
		c1 := weft.FormatValue(v, weft.FormatOptions{StrictCanonical: true})
		c2 := weft.FormatValue(v2, weft.FormatOptions{StrictCanonical: true})
		if c1 != c2 { fmt.Println("CRASH lossless identity violated (canonical bytes)"); return }
		// §5.16: NaN ≢ NaN, so Equals is only asserted for NaN-free docs.
		if !strings.Contains(c1, "nan") && !weft.Equals(v, v2) { fmt.Println("CRASH lossless identity violated (equals)"); return }
		fmt.Print("OK\n" + weft.FormatValue(v2, strict))
	case "tolossless":
		v, err := weft.Parse(data)
		if err != nil { werr(err); return }
		b, _ := json.Marshal(weft.ToJSONLossless(v))
		var y any
		d := json.NewDecoder(strings.NewReader(string(b)))
		d.UseNumber()
		d.Decode(&y)
		fmt.Print("OK\n" + stable(y) + "\n")
	case "fromlossless":
		v, err := weft.LoadsJSONLossless(data)
		if err != nil { werr(err); return }
		fmt.Print("OK\n" + weft.FormatValue(v, strict))
	default:
		os.Exit(2)
	}
}
