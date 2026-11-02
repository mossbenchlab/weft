// Conformance driver (TypeScript implementation). See driver.py for protocol.
import * as weft from "../../../impl-typescript/src/index";
const fs = require("fs");

function main() {
  const mode = process.argv[2];
  const data: string = fs.readFileSync(0, "utf-8");
  const strict = {strict_canonical: true};
  try {
    let out: string;
    if (mode === "canon") {
      out = weft.formatValue(weft.parse(data), strict);
    } else if (mode === "canon2") {
      const once = weft.formatValue(weft.parse(data), strict);
      out = weft.formatValue(weft.parse(once), strict);
    } else if (mode === "fmt") {
      out = weft.formatValue(weft.parse(data));
    } else if (mode === "jroundtrip") {
      const v = weft.parse(data);
      const jtext = JSON.stringify(weft.toJsonLossless(v));
      const v2 = weft.loadsJson(jtext);
      const c1 = weft.formatValue(v, strict);
      const c2 = weft.formatValue(v2, strict);
      if (c1 !== c2) { console.log("CRASH lossless identity violated (canonical bytes)"); return; }
      // §5.16: NaN ≢ NaN, so equals() is only asserted for NaN-free docs.
      if (!c1.includes("nan") && !weft.equals(v, v2)) { console.log("CRASH lossless identity violated (equals)"); return; }
      out = weft.formatValue(v2, strict);
    } else if (mode === "tolossless") {
      out = stable(weft.toJsonLossless(weft.parse(data))) + "\n";
    } else if (mode === "fromlossless") {
      out = weft.formatValue(weft.loadsJson(data), strict);
    } else { process.exit(2); return; }
    process.stdout.write("OK\n" + out);
  } catch (e: any) {
    if (e instanceof weft.WeftError) console.log("ERR " + (e.code ?? "WEFT"));
    else if (e instanceof RangeError && /call stack/i.test(String(e.message))) console.log("CRASH StackOverflow");
    else console.log("CRASH " + (e?.constructor?.name ?? "?") + ": " + String(e?.message ?? e).slice(0, 160));
  }
}
function stable(x: any): string {
  if (x === null || typeof x !== "object") return JSON.stringify(x);
  if (Array.isArray(x)) return "[" + x.map(stable).join(",") + "]";
  const keys = Object.keys(x).sort();
  return "{" + keys.map(k => JSON.stringify(k) + ":" + stable(x[k])).join(",") + "}";
}
main();
