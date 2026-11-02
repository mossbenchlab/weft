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
