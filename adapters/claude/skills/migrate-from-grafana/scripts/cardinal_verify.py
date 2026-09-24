#!/usr/bin/env python3
"""Run every migrated panel's and alert's query against Cardinal and report which return data.

Uses lakerunner's native query API through Maestro (the same one Cardinal's UI
uses). Dashboard variables are replaced with `.+` ("All").

Usage:
  cardinal_verify.py --plan ./plan --catalog ./catalog [--window 1h] [--env-file .env.cardinal]

Writes plan/verify.json and prints a per-dashboard summary.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_catalog import Cardinal, NativeQuery, load_env_file  # noqa: E402


def window_ms(w):
    n, u = int(w[:-1]), w[-1]
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[u]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--window", default="1h")
    ap.add_argument("--env-file")
    args = ap.parse_args()
    load_env_file(args.env_file)
    c = Cardinal.from_env()
    inst = json.load(open(os.path.join(args.catalog, "instance.json")))["chosen"]
    nq = NativeQuery(c, inst["id"], window_ms(args.window))

    results = {"dashboards": [], "alerts": []}
    ddir = os.path.join(args.plan, "dashboards")
    for fn in sorted(os.listdir(ddir)):
        d = json.load(open(os.path.join(ddir, fn)))
        rows = []
        for pid, p in d["spec"]["panels"].items():
            qs = [(q["query"], q.get("queryKind", "prometheus")) for q in p.get("queries", [])]
            if p.get("kind") == "log-events" and p.get("rawLogql"):
                qs = [(p["rawLogql"], "loki")]
            for expr, kind in qs:
                expr = re.sub(r"\$\{?\w+\}?", ".+", expr)
                n, err = nq.query("logs" if kind == "loki" else "metrics", expr)
                rows.append({"panel": p["title"], "query": expr, "points": n, "error": err})
        ok = sum(1 for r in rows if r["points"] and not r["error"])
        results["dashboards"].append({"name": d["name"], "queries": len(rows), "with_data": ok, "rows": rows})
        print(f"{d['name']}: {ok}/{len(rows)} queries return data")
        for r in rows:
            if r["error"] or not r["points"]:
                print(f"   - {r['panel']}: {'ERROR ' + r['error'] if r['error'] else 'no data'}")

    apath = os.path.join(args.plan, "alerts.json")
    for a in (json.load(open(apath)) if os.path.exists(apath) else []):
        spec = a["rule_spec"]
        n, err = nq.query(spec["signal_type"], spec["query"]["expr"])
        results["alerts"].append({"name": a["name"], "points": n, "error": err})
        status = "ERROR " + err if err else ("data" if n else "no data")
        print(f"alert {a['name']}: {status}")

    json.dump(results, open(os.path.join(args.plan, "verify.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
