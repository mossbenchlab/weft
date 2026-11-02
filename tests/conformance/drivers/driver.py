#!/usr/bin/env python3
"""Conformance driver (Python implementation).

stdin: input document (UTF-8). argv[1]: mode.
stdout: "OK\n<output>" or "ERR <code>" or "CRASH <info>".
Modes: canon | canon2 | fmt | jroundtrip | tolossless | fromlossless | equals-self
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "impl-python", "src"))
import weft
from weft import FormatOptions

def main():
    mode = sys.argv[1]
    data = sys.stdin.buffer.read().decode("utf-8", errors="surrogatepass")
    strict = FormatOptions(strict_canonical=True)
    try:
        if mode == "canon":
            out = weft.format_value(weft.parse(data), strict)
        elif mode == "canon2":
            once = weft.format_value(weft.parse(data), strict)
            out = weft.format_value(weft.parse(once), strict)
        elif mode == "fmt":
            out = weft.format_value(weft.parse(data))
        elif mode == "jroundtrip":
            v = weft.parse(data)
            jtext = json.dumps(weft.to_json_lossless(v), ensure_ascii=False)
            v2 = weft.loads_json(jtext)
            c1 = weft.format_value(v, strict)
            c2 = weft.format_value(v2, strict)
            if c1 != c2:
                print("CRASH lossless identity violated (canonical bytes)")
                return
            # §5.16: NaN ≢ NaN, so equals() is only asserted for NaN-free docs.
            if "nan" not in c1 and not weft.equals(v, v2):
                print("CRASH lossless identity violated (equals)")
                return
            out = weft.format_value(v2, strict)
        elif mode == "tolossless":
            v = weft.parse(data)
            out = json.dumps(weft.to_json_lossless(v), ensure_ascii=False,
                             sort_keys=True) + "\n"
        elif mode == "fromlossless":
            out = weft.format_value(weft.loads_json(data), strict)
        else:
            print("bad mode", file=sys.stderr); sys.exit(2)
        sys.stdout.buffer.write(b"OK\n" + out.encode("utf-8"))
    except weft.WeftError as e:
        print("ERR " + (e.code or "WEFT"))
    except RecursionError:
        print("CRASH RecursionError")
    except Exception as e:
        print("CRASH " + type(e).__name__ + ": " + str(e)[:160])

main()
