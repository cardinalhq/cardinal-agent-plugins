#!/usr/bin/env python3
"""Create (or update) the converted dashboards and alert rules in Cardinal.

Dry run by default: prints what would be created/updated. Pass --apply to write.
Idempotent: a dashboard or alert rule with the same name is updated in place,
so re-running after fixing the mapping does not create duplicates.

Reads CARDINAL_URL, CARDINAL_ORG_ID and CARDINAL_API_KEY or CARDINAL_TOKEN (env or --env-file).

Usage:
  cardinal_apply.py --plan ./plan --catalog ./catalog [--apply] [--only dashboards|alerts]
                    [--name-prefix "Migrated - "] [--env-file .env.cardinal]

Writes plan/applied.json with the Cardinal id + URL of everything created/updated.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_catalog import Cardinal, load_env_file  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--catalog", required=True, help="dir written by cardinal_catalog.py (for the instance id)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", choices=["dashboards", "alerts"])
    ap.add_argument("--name-prefix", default="")
    ap.add_argument("--env-file")
    args = ap.parse_args()
    load_env_file(args.env_file)
    c = Cardinal.from_env()
    org = c.org
    if not org:
        sys.exit("CARDINAL_ORG_ID must be set")
    instance = json.load(open(os.path.join(args.catalog, "instance.json")))["chosen"]
    mode = "APPLY" if args.apply else "DRY RUN"
    results = {"mode": mode, "dashboards": [], "alerts": []}

    if args.only != "alerts":
        code, existing = c.req("GET", f"/api/orgs/{org}/dashboards")
        if code != 200:
            sys.exit(f"cannot list Cardinal dashboards ({code}): {existing}. Dashboards need a login token "
                     "for a Member/Owner of the org, or an org API key with admin:all scope.")
        by_name = {d["name"]: d for d in existing}
        ddir = os.path.join(args.plan, "dashboards")
        for fn in sorted(os.listdir(ddir)):
            d = json.load(open(os.path.join(ddir, fn)))
            name = args.name_prefix + d["name"]
            prior = by_name.get(name)
            action = "update" if prior else "create"
            entry = {"name": name, "action": action, "panels": len(d["spec"]["panels"])}
            if args.apply:
                if prior:
                    code, body = c.req("PUT", f"/api/orgs/{org}/dashboards/{prior['id']}", body={"name": name, "spec": d["spec"]})
                else:
                    code, body = c.req("POST", f"/api/orgs/{org}/dashboards", body={"name": name, "spec": d["spec"]})
                entry["status"] = code
                if code in (200, 201):
                    entry["id"] = body["id"]
                    entry["url"] = f"{c.url}/dashboards/{body['id']}"
                else:
                    entry["error"] = str(body)[:300]
            results["dashboards"].append(entry)

    if args.only != "dashboards":
        code, existing = c.req("GET", f"/api/orgs/{org}/alert-rules")
        if code != 200:
            sys.exit(f"cannot list Cardinal alert rules ({code}): {existing}")
        rules = existing if isinstance(existing, list) else existing.get("rules", existing.get("data", []))
        by_name = {}
        for r in rules:
            spec = r.get("ruleSpec") or r.get("rule_spec") or {}
            by_name[spec.get("name")] = r
        for a in json.load(open(os.path.join(args.plan, "alerts.json"))):
            spec = dict(a["rule_spec"], name=args.name_prefix + a["rule_spec"]["name"])
            prior = by_name.get(spec["name"])
            entry = {"name": spec["name"], "action": "update" if prior else "create",
                     "query": spec["query"]["expr"][:120]}
            if args.apply:
                body = {"integration_id": instance["id"], "rule_spec": spec}
                if prior:
                    code, resp = c.req("PUT", f"/api/orgs/{org}/alert-rules/{prior['id']}", body=body)
                else:
                    code, resp = c.req("POST", f"/api/orgs/{org}/alert-rules", body=body)
                entry["status"] = code
                if code in (200, 201):
                    entry["id"] = resp.get("id")
                    if a.get("paused") and resp.get("id"):
                        c.req("PUT", f"/api/orgs/{org}/alert-rules/{resp['id']}", body={"enabled": False})
                        entry["note"] = "disabled (was paused in Grafana)"
                else:
                    entry["error"] = str(resp)[:300]
            results["alerts"].append(entry)

    json.dump(results, open(os.path.join(args.plan, "applied.json" if args.apply else "dry-run.json"), "w"), indent=2)
    print(json.dumps(results, indent=2))
    failed = [x for x in results["dashboards"] + results["alerts"] if x.get("error")]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
