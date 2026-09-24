#!/usr/bin/env python3
"""Read Cardinal's metric/label catalog and suggest a Grafana -> Cardinal name mapping.

Grafana (Mimir/Prometheus) and Cardinal (lakerunner) often store the same OTel
metric under different names: Prometheus conversion appends `_total` and unit
suffixes (`_seconds`, `_milliseconds`, `_bytes`, ...) and turns dots into
underscores. This script finds, for every metric/label referenced by the export,
the name Cardinal actually has, so queries keep returning data after migration.

Reads CARDINAL_URL, CARDINAL_ORG_ID and CARDINAL_API_KEY or CARDINAL_TOKEN (env or --env-file).

Usage:
  cardinal_catalog.py --export ./export --out ./catalog [--instance <id-or-slug>] [--env-file .env.cardinal]

Writes:
  catalog/instance.json          chosen lakerunner instance {id, slug, name}
  catalog/metrics.json           Cardinal metric names
  catalog/labels.json            Cardinal metric label names
  catalog/log_labels.json        Cardinal log label names (if the logs API answered)
  catalog/mapping.suggested.json mapping in convert.py's format + "_review" notes
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UNIT_SUFFIXES = ["_seconds", "_milliseconds", "_microseconds", "_nanoseconds", "_bytes", "_bits",
                 "_ratio", "_percent", "_celsius", "_meters", "_hertz", "_volts", "_amperes",
                 "_joules", "_grams", "_minutes", "_hours", "_days"]
HIST_SUFFIXES = ["_bucket", "_sum", "_count"]


def load_env_file(path):
    if path:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class Cardinal:
    """Maestro client. Auth is either an org API key (admin:all scope, sent as
    X-CardinalHQ-API-Key) or the user's own login token (sent as Bearer), which
    carries their org role: Member can write dashboards, Owner also alert rules."""

    def __init__(self, url, key, org, token=None):
        self.url, self.key, self.org, self.token = url.rstrip("/"), key, org, token

    @classmethod
    def from_env(cls):
        url, key, token, org = (os.environ.get(k) for k in
                                ("CARDINAL_URL", "CARDINAL_API_KEY", "CARDINAL_TOKEN", "CARDINAL_ORG_ID"))
        if not url or not (key or token):
            sys.exit("CARDINAL_URL and one of CARDINAL_API_KEY / CARDINAL_TOKEN must be set (env or --env-file)")
        if token and token.lower().startswith("bearer "):
            token = token[7:]
        return cls(url, key, org, token)

    def req(self, method, path, params=None, body=None):
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-CardinalHQ-API-Key"] = self.key
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.org:
            headers["X-Org-Id"] = self.org
        r = urllib.request.Request(self.url + path, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw or b"null")
                except json.JSONDecodeError:
                    return resp.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code == 401 and self.token and not self.key:
                sys.exit("Cardinal rejected the login token (401) - it has probably expired. "
                         "Copy a fresh one from the browser and re-run.")
            return e.code, body
        except urllib.error.URLError as e:
            return 0, str(e.reason)


class NativeQuery:
    """lakerunner's native query API via Maestro: POST {q, s, e, step} to
    /api/lakerunner/<instance>/query/<signal>/<op>; results stream back as SSE."""

    def __init__(self, cardinal, instance_id, window_ms=3600_000):
        import time
        self.c, self.base = cardinal, f"/api/lakerunner/{instance_id}/query"
        now = int(time.time() * 1000)
        self.range = {"s": str(now - window_ms), "e": str(now)}

    @staticmethod
    def events(body):
        out = []
        for line in (body if isinstance(body, str) else "").split("\n"):
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
        return out

    def tags(self, signal, metric=None):
        body = dict(self.range, **({"q": metric} if metric else {}))
        code, resp = self.c.req("POST", f"{self.base}/{signal}/tags", body=body)
        return resp.get("tags", []) if code == 200 and isinstance(resp, dict) else []

    def tag_values(self, signal, tag):
        code, resp = self.c.req("POST", f"{self.base}/{signal}/tagvalues",
                                params={"tagName": tag}, body=dict(self.range))
        if code != 200:
            return None
        vals = []
        for e in self.events(resp):
            d = e.get("data", {})
            if e.get("type") == "result" and isinstance(d, dict) and d.get("value") is not None:
                vals.append(str(d["value"]))
        return vals

    def query(self, signal, expr, step=60):
        """Return (number of result points, error message or None)."""
        body = dict(self.range, q=expr, step=step)
        if signal == "logs":
            body.update(limit=50, reverse=True)
        code, resp = self.c.req("POST", f"{self.base}/{signal}/query", body=body)
        if code != 200:
            return 0, f"HTTP {code}: {str(resp)[:200]}"
        ev = self.events(resp)
        errs = [e for e in ev if e.get("type") == "error"]
        if errs:
            return 0, json.dumps(errs[0].get("data", errs[0]))[:200]
        return sum(1 for e in ev if e.get("type") == "result"), None


def norm(name):
    return re.sub(r"[.\-/]", "_", name).lower()


def metric_candidates(name):
    """Plausible Cardinal spellings of a Grafana metric name, most specific first."""
    out = [name]
    base = name
    if base.endswith("_total"):
        base = base[: -len("_total")]
        out.append(base)
    for sfx in UNIT_SUFFIXES:
        if base.endswith(sfx):
            out.append(base[: -len(sfx)])
            if name.endswith("_total"):
                out.append(base[: -len(sfx)] + "_total")
    return list(dict.fromkeys(out))


def all_exprs(export):
    exprs = []
    ddir = os.path.join(export, "dashboards")
    for fn in os.listdir(ddir):
        stack = list(json.load(open(os.path.join(ddir, fn))).get("panels", []))
        while stack:
            p = stack.pop()
            stack.extend(p.get("panels", []))
            exprs += [t["expr"] for t in p.get("targets", []) if t.get("expr")]
    apath = os.path.join(export, "alerts.json")
    if os.path.exists(apath):
        for g in json.load(open(apath)):
            for r in g["rules"]:
                exprs += [d["model"]["expr"] for d in r.get("grafana_alert", {}).get("data", [])
                          if d.get("model", {}).get("expr")]
    return exprs


def exact_matchers(export):
    """(label, value) pairs for literal equality matchers without variables."""
    out = set()
    for e in all_exprs(export):
        for label, value in re.findall(r'([a-zA-Z_][\w.]*)\s*=\s*"([^"$]*)"', e):
            if value:
                out.add((label, value))
    return out


def referenced_names(export, with_histograms=False):
    """Collect metric identifiers and label names used by the exported queries."""
    exprs = []
    ddir = os.path.join(export, "dashboards")
    for fn in os.listdir(ddir):
        dash = json.load(open(os.path.join(ddir, fn)))
        stack = list(dash.get("panels", []))
        while stack:
            p = stack.pop()
            stack.extend(p.get("panels", []))
            for t in p.get("targets", []):
                if t.get("expr"):
                    exprs.append(t["expr"])
        for v in dash.get("templating", {}).get("list", []):
            q = v.get("query")
            q = q.get("query") if isinstance(q, dict) else q
            if isinstance(q, str):
                exprs.append(q)
    apath = os.path.join(export, "alerts.json")
    if os.path.exists(apath):
        for g in json.load(open(apath)):
            for r in g["rules"]:
                for d in r.get("grafana_alert", {}).get("data", []):
                    if d.get("model", {}).get("expr"):
                        exprs.append(d["model"]["expr"])
    metrics, labels = set(), set()
    for e in exprs:
        stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', e)
        for block in re.findall(r"\{([^}]*)\}", stripped):
            labels.update(re.findall(r"([a-zA-Z_][\w.]*)\s*(?:=~|!~|!=|=)", block))
        for grp in re.findall(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)", stripped):
            labels.update(x.strip() for x in grp.split(",") if x.strip())
        for m in re.findall(r'\| *([a-zA-Z_]\w*) *(?:=~|!~|!=|=)', stripped):
            labels.add(m)
        body = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", stripped)
        body = re.sub(r"\|\s*[a-zA-Z_]\w*", " ", body)  # LogQL pipeline labels (| detected_level=...) aren't metrics
        body = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", body)
        body = re.sub(r"\blabel_values\s*\(([^,)]*),[^)]*\)", r" \1 ", body)
        for tok in re.findall(r"(?<![\w:.$])([a-zA-Z_:][\w:]*)(?![\w(])", body):
            if "_" in tok and tok.lower() == tok:
                metrics.add(tok)
    # Collapse X_bucket/X_sum/X_count to the histogram family X, but only when
    # the family really is a histogram (a _bucket series is referenced).
    # Otherwise a gauge like go_goroutine_count would lose its real suffix.
    hist_families = {m[: -len("_bucket")] for m in metrics if m.endswith("_bucket")}
    families = set()
    for m in metrics:
        fam = m
        for sfx in HIST_SUFFIXES:
            if m.endswith(sfx) and m[: -len(sfx)] in hist_families:
                fam = m[: -len(sfx)]
        families.add(fam)
    if with_histograms:
        return sorted(families), sorted(labels - {"le", "__name__"}), hist_families
    return sorted(families), sorted(labels - {"le", "__name__"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--instance")
    ap.add_argument("--env-file")
    args = ap.parse_args()
    load_env_file(args.env_file)
    c = Cardinal.from_env()
    os.makedirs(args.out, exist_ok=True)

    code, body = c.req("GET", "/api/lakerunner/instances")
    if code != 200:
        sys.exit(f"could not list Cardinal instances ({code}): {body}")
    instances = body.get("instances", [])
    if not instances:
        sys.exit("this Cardinal org has no lakerunner (data lake) instance; connect one before migrating")
    inst = instances[0]
    if args.instance:
        match = [i for i in instances if args.instance in (i["id"], i.get("slug"), i.get("name"))]
        if not match:
            sys.exit(f"instance '{args.instance}' not found; available: {[i.get('slug') for i in instances]}")
        inst = match[0]
    elif len(instances) > 1:
        print(f"note: {len(instances)} instances; using the default '{inst.get('slug')}'. "
              f"Pass --instance to choose: {[i.get('slug') for i in instances]}", file=sys.stderr)
    json.dump({"chosen": inst, "all": instances}, open(os.path.join(args.out, "instance.json"), "w"), indent=2)
    prom = f"/api/lakerunner/{inst['id']}/prometheus/api/v1"

    # Metric names come from the Prometheus-compatible metadata route; everything
    # else uses lakerunner's native query API (POST JSON, SSE responses), which is
    # what Cardinal's own UI calls.
    code, body = c.req("GET", f"{prom}/label/__name__/values")
    if code != 200:
        sys.exit(f"could not read Cardinal metric names ({code}): {body}")
    cardinal_metrics = body.get("data", []) if isinstance(body, dict) else []
    if not cardinal_metrics:
        sys.exit("Cardinal returned no metrics for this instance: it isn't receiving data yet")
    native = NativeQuery(c, inst["id"])

    by_norm = {}
    for m in cardinal_metrics:
        by_norm.setdefault(norm(m), []).append(m)

    families, used_labels, hist_families = referenced_names(args.export, with_histograms=True)
    mapping = {"metrics": {}, "labels": {}, "log_labels": {}, "drop_labels": [],
               "native_histograms": [], "_review": []}
    for fam in families:
        hit = None
        for cand in metric_candidates(fam):
            if norm(cand) in by_norm:
                hit = by_norm[norm(cand)][0]
                break
        mapping["metrics"][fam] = hit
        if hit is None:
            close = [m for m in cardinal_metrics if norm(fam).split("_")[0] in norm(m)][:8]
            mapping["_review"].append({"metric": fam, "issue": "no Cardinal metric matched",
                                       "similar_in_cardinal": close})
        elif hit != fam:
            mapping["_review"].append({"metric": fam, "mapped_to": hit, "issue": "renamed (check it's the same series)"})
        if fam in hist_families and hit and (hit + "_bucket") not in cardinal_metrics:
            mapping["native_histograms"].append(fam)

    # Labels actually present on the mapped metrics, and on logs.
    cardinal_labels = set()
    for target in {v for v in mapping["metrics"].values() if v}:
        cardinal_labels.update(native.tags("metrics", target))
    log_labels = set(native.tags("logs"))
    label_norm = {}
    for l in sorted(cardinal_labels | log_labels):
        label_norm.setdefault(norm(l), l)
        label_norm.setdefault(norm(re.sub(r"^resource[._]", "", l)), l)
    # Exact-value filters (label="value") must match data that exists in Cardinal,
    # or every panel using them goes blank: e.g. an environment label whose value
    # differs between the old and new pipelines.
    for label, value in sorted(exact_matchers(args.export)):
        if label in ("detected_level", "__name__") or label in mapping["drop_labels"]:
            continue
        present = [sig for sig, names in (("metrics", cardinal_labels), ("logs", log_labels)) if label in names]
        seen_values = set()
        for sig in present:
            seen_values.update(native.tag_values(sig, label) or [])
        if label not in cardinal_labels or (seen_values and value not in seen_values):
            mapping["drop_labels"].append(label)
            mapping["_review"].append({"label": label, "value": value,
                                       "issue": "filter value not found in Cardinal: the filter will be removed "
                                                "(keep it by editing drop_labels if that is wrong)",
                                       "values_in_cardinal": sorted(seen_values)[:10]})
    for l in used_labels:
        if l == "detected_level" or l in mapping["drop_labels"]:
            continue
        target = label_norm.get(norm(l)) or label_norm.get(norm(re.sub(r"^resource[._]", "", l)))
        if target and target != l:
            mapping["labels"][l] = target
        elif not target:
            mapping["drop_labels"].append(l)
            mapping["_review"].append({"label": l, "issue": "label not present in Cardinal: filters on it "
                                       "will be removed (edit drop_labels / labels if it exists under another name)"})
    # Loki's `detected_level` is a Grafana-side derived field; lakerunner keeps severity on `level`.
    if "detected_level" in used_labels:
        mapping["log_labels"]["detected_level"] = "level"

    json.dump(sorted(cardinal_metrics), open(os.path.join(args.out, "metrics.json"), "w"), indent=1)
    json.dump(sorted(cardinal_labels), open(os.path.join(args.out, "labels.json"), "w"), indent=1)
    json.dump(sorted(log_labels), open(os.path.join(args.out, "log_labels.json"), "w"), indent=1)

    # histogram_quantile: native histograms have no buckets, so it can't work;
    # otherwise probe it on a classic histogram.
    supports_hq = False
    if hist_families and not mapping["native_histograms"]:
        probe = next((mapping["metrics"][f] for f in hist_families if mapping["metrics"].get(f)), None)
        if probe:
            n, err = native.query("metrics", f"histogram_quantile(0.95, sum by (le) (rate({probe}_bucket[5m])))")
            supports_hq = n > 0 and not err
    mapping["supports_histogram_quantile"] = supports_hq
    json.dump(mapping, open(os.path.join(args.out, "mapping.suggested.json"), "w"), indent=2)

    print(json.dumps({
        "instance": inst, "native_histograms": mapping["native_histograms"],
        "drop_labels": mapping["drop_labels"], "cardinal_metrics": len(cardinal_metrics), "grafana_metric_families": len(families),
        "matched": sum(1 for v in mapping["metrics"].values() if v), "unmatched": [k for k, v in mapping["metrics"].items() if not v],
        "renamed": {k: v for k, v in mapping["metrics"].items() if v and v != k},
        "label_renames": mapping["labels"], "supports_histogram_quantile": supports_hq,
    }, indent=2))


if __name__ == "__main__":
    main()
