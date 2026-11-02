#!/usr/bin/env python3
"""Corpus generator (maintainer tool).

Runs candidate inputs through ALL FOUR implementations; a case enters the
corpus only when the four agree. Any disagreement is printed and the
corpus is NOT written — fix the implementations (or the case) first.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run import impl_commands, run_driver  # noqa: E402

def norm_lossless(out: str):
    """Normalize a driver's ``tolossless`` output (OK\n<json>) to a
    canonical compact sorted JSON text, so host serializer differences
    (spacing, HTML escaping) vanish while payload-text differences —
    e.g. {"$float": "1e-7"} vs "1e-07" — remain visible."""
    if not out.startswith("OK\n"):
        return None
    try:
        j = json.loads(out[3:])
    except ValueError:
        return None
    return json.dumps(j, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main():
    cmds = impl_commands(["py", "ts", "rs", "go"], build=False)
    cand = json.load(open(os.path.join(HERE, "candidates.json"), encoding="utf-8"))
    valid, invalid, jvalid, jinvalid = [], [], [], []
    disagreements = []
    for c in cand:
        cid, kind, data = c["id"], c["kind"], c["input"]
        mode = "fromlossless" if kind.startswith("json") else "canon"
        outs = {i: run_driver(cmds[i], mode, data) for i in cmds}
        kinds = {i: ("OK" if o.startswith("OK\n") else "ERR" if o.startswith("ERR") else "OTHER")
                 for i, o in outs.items()}
        # Agreement: same outcome kind everywhere; for OK also byte-equal
        # output. Error-code strings are recorded only when unanimous
        # (implementations may use different specific type-error codes).
        agreed = len(set(kinds.values())) == 1 and (
            kinds["py"] != "OK" or len({o for o in outs.values()}) == 1)
        if not agreed or kinds["py"] == "OTHER":
            disagreements.append((cid, outs))
            continue
        out = outs["py"]
        if kinds["py"] == "OK":
            entry = {"id": cid, "spec": c.get("spec", ""), 
                     ("json" if kind.startswith("json") else "input"): data,
                     "canonical": out[3:]}
            if not kind.startswith("json"):
                # SPEC §11.2 payload texts must agree across hosts too
                # (normalized JSON — catches e.g. a host-specific $float
                # payload like "1e-7" where the spec requires "1e-07").
                louts = {i: norm_lossless(run_driver(cmds[i], "tolossless", data))
                         for i in cmds}
                if None in louts.values() or len(set(louts.values())) != 1:
                    disagreements.append((cid + " (tolossless)", 
                                          {i: repr(v)[:140] for i, v in louts.items()}))
                    continue
                entry["lossless"] = louts["py"]
            (jvalid if kind.startswith("json") else valid).append(entry)
        elif kinds["py"] == "ERR":
            codes = {o.strip()[4:] for o in outs.values()}
            entry = {"id": cid, "spec": c.get("spec", ""),
                     ("json" if kind.startswith("json") else "input"): data}
            if len(codes) == 1:
                code = codes.pop()
                if code and code != "WEFT":
                    entry["code"] = code
            (jinvalid if kind.startswith("json") else invalid).append(entry)
        else:
            disagreements.append((cid, outs))
    if disagreements:
        print(f"{len(disagreements)} DISAGREEMENTS — corpus not written:")
        for cid, outs in disagreements:
            print(f"== {cid}")
            for i, o in outs.items():
                print(f"   {i}: {o.strip()[:140]!r}")
        sys.exit(1)
    os.makedirs(os.path.join(HERE, "corpus"), exist_ok=True)
    for name, data in [("valid.json", valid), ("invalid.json", invalid),
                       ("json_valid.json", jvalid), ("json_invalid.json", jinvalid)]:
        with open(os.path.join(HERE, "corpus", name), "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=1)
        print(f"{name}: {len(data)} cases")

if __name__ == "__main__":
    main()
