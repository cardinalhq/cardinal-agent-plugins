#!/usr/bin/env python3
"""End-to-end test of the rendered Cardinal branch against a real Alloy, in Docker.

Renders the e2e fixture with render.py, then runs:
  SeaweedFS (S3)  <-  Alloy (the rendered config)  ->  "Grafana" stub (otelcol-contrib, file exporter)
and sends synthetic traces, logs and metrics with telemetrygen.

Phase 1 — happy path. Asserts:
  * objects land under otel-raw/<org>/<cluster>/, named logs_/metrics_/traces_, gzipped OTLP protobuf
  * Cardinal (S3) got the same spans/log records/data points as Grafana (the stub)
  * metrics in S3 are delta; metrics at the stub are still cumulative (Grafana untouched)
  * k8s.cluster.name is set in S3 data only
Phase 2 — S3 down (fault injection). Asserts Grafana still receives everything and
Alloy keeps running.

With --target saas, the S3 store is replaced by a Cardinal SaaS intake stub (a small
OTLP/HTTP server that rejects requests without the right x-cardinalhq-api-key), and
the same checks run against what it received; phase 2 stops the intake instead.

Usage:
  python3 tests/onboard_alloy/e2e/run_e2e.py [--target s3|saas] [--workdir DIR] [--keep] [--alloy-image grafana/alloy:latest]

Needs Docker. Pulls public images only; touches no cloud account. Exit 0 = PASS.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterator, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SCRIPTS = os.path.join(ROOT, "common", "onboard-alloy", "scripts")
FIXTURE = os.path.join(HERE, "..", "fixtures", "e2e_otlp.alloy")
sys.path.insert(0, SCRIPTS)
import alloy_config as ac  # noqa: E402
import alloy_inventory as inv  # noqa: E402
import render  # noqa: E402

ORG = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
CLUSTER = "e2e-local"
BUCKET = "cardinal-lake"
NET = "oa-e2e"
S3_IMAGE = "chrislusf/seaweedfs:latest"
STUB_IMAGE = "otel/opentelemetry-collector-contrib:latest"
AWS_IMAGE = "amazon/aws-cli:latest"
GEN_IMAGE = "ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest"
INTAKE_IMAGE = "python:3.12-alpine"
API_KEY = "e2e-test-key"
CONTAINERS = ("oa-alloy", "grafana-stub", "oa-s3", "oa-intake")
AWS_ENV = ["-e", "AWS_ACCESS_KEY_ID=test", "-e", "AWS_SECRET_ACCESS_KEY=test", "-e", "AWS_DEFAULT_REGION=us-east-1"]

STUB_CONFIG = """receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
exporters:
  file/metrics: { path: /out/metrics.json }
  file/logs: { path: /out/logs.json }
  file/traces: { path: /out/traces.json }
service:
  pipelines:
    metrics: { receivers: [otlp], exporters: [file/metrics] }
    logs: { receivers: [otlp], exporters: [file/logs] }
    traces: { receivers: [otlp], exporters: [file/traces] }
"""

# Cardinal SaaS intake stub: stores each accepted OTLP/HTTP request body (protobuf,
# gunzipped) as /out/<signal>_<n>.pb; counts requests with a wrong or missing key.
INTAKE_SERVER = r"""
import gzip, http.server, itertools, os
KEY = os.environ["EXPECTED_KEY"]
SIGNALS = {"/v1/logs": "logs", "/v1/metrics": "metrics", "/v1/traces": "traces"}
n = itertools.count()
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("x-cardinalhq-api-key") != KEY:
            open("/out/unauthorized", "a").write(self.path + "\n")
            self.send_response(401); self.end_headers(); return
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        sig = SIGNALS.get(self.path)
        if sig is None:
            self.send_response(404); self.end_headers(); return
        open(f"/out/{sig}_{next(n)}.pb", "wb").write(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf"); self.end_headers()
http.server.ThreadingHTTPServer(("0.0.0.0", 4318), H).serve_forever()
"""


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def sh(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}… failed ({p.returncode}):\n{p.stderr or p.stdout}")
    return p


def cleanup() -> None:
    for c in CONTAINERS:
        sh("docker", "rm", "-f", c, check=False)
    sh("docker", "network", "rm", NET, check=False)


def telemetrygen(kind: str, n: int) -> None:
    extra = {"traces": ["--traces", str(n)], "logs": ["--logs", str(n)],
             "metrics": ["--metrics", str(n), "--metric-type", "Sum"]}[kind]
    # --rate 0: unthrottled (telemetrygen defaults to 1 item/s per worker).
    sh("docker", "run", "--rm", "--network", NET, GEN_IMAGE, kind,
       "--otlp-endpoint", "oa-alloy:4317", "--otlp-insecure", "--rate", "0", *extra)


def send_all() -> float:
    """Send the standard batch of traces, logs and metrics; return seconds taken."""
    t0 = time.time()
    for kind, n in (("traces", 50), ("logs", 100), ("metrics", 20)):
        telemetrygen(kind, n)
    return time.time() - t0


def export_failures(exporter: str = render.S3) -> List[str]:
    """Cardinal export failures. Alloy logs these at level=info ("Exporting failed. Will retry…")."""
    p = sh("docker", "logs", "oa-alloy", check=False)
    return [l for l in (p.stdout + p.stderr).splitlines()
            if exporter in l and "Exporting failed" in l]


def alloy_running() -> bool:
    return sh("docker", "inspect", "-f", "{{.State.Running}}", "oa-alloy", check=False).stdout.strip() == "true"


# ---------------------------------------------------------------------------
# Minimal protobuf reader for OTLP
# ---------------------------------------------------------------------------

def _varint(b: bytes, i: int) -> Tuple[int, int]:
    shift = val = 0
    while True:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7


def fields(b: bytes) -> Iterator[Tuple[int, object]]:
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield num, v


def sub(b: bytes, num: int) -> List[bytes]:
    return [v for n, v in fields(b) if n == num]  # type: ignore[misc]


def resource_attrs(resource: bytes) -> Dict[str, str]:
    out = {}
    for kv in sub(resource, 1):
        key = sub(kv, 1)[0].decode()
        vals = sub(kv, 2)
        s = sub(vals[0], 1) if vals else []
        out[key] = s[0].decode() if s else ""
    return out


def summarize_proto(signal: str, b: bytes) -> Dict:
    s = {"items": 0, "temporality": set(), "clusters": set()}
    for rs in sub(b, 1):
        res = sub(rs, 1)
        s["clusters"].add(resource_attrs(res[0]).get("k8s.cluster.name", "") if res else "")
        for scope in sub(rs, 2):
            for item in sub(scope, 2):
                if signal != "metrics":
                    s["items"] += 1
                    continue
                for num, v in fields(item):
                    if num == 7:  # sum
                        s["items"] += len(sub(v, 1))
                        s["temporality"].update(t for n, t in fields(v) if n == 2)
                    elif num == 5:  # gauge
                        s["items"] += len(sub(v, 1))
    return s


def summarize_json(signal: str, path: str) -> Dict:
    s = {"items": 0, "temporality": set(), "clusters": set()}
    rkey, skey, ikey = {"metrics": ("resourceMetrics", "scopeMetrics", "metrics"),
                        "logs": ("resourceLogs", "scopeLogs", "logRecords"),
                        "traces": ("resourceSpans", "scopeSpans", "spans")}[signal]
    if not os.path.exists(path):
        return s
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        for rs in json.loads(line).get(rkey, []):
            attrs = {a["key"]: a["value"].get("stringValue", "") for a in rs.get("resource", {}).get("attributes", [])}
            s["clusters"].add(attrs.get("k8s.cluster.name", ""))
            for scope in rs.get(skey, []):
                for item in scope.get(ikey, []):
                    if signal != "metrics":
                        s["items"] += 1
                    elif "sum" in item:
                        s["items"] += len(item["sum"].get("dataPoints", []))
                        s["temporality"].add(item["sum"].get("aggregationTemporality"))
                    elif "gauge" in item:
                        s["items"] += len(item["gauge"].get("dataPoints", []))
    return s


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, what: str) -> None:
        print(f"  {'✓' if ok else '✗'} {what}")
        self.failed += 0 if ok else 1


def s3_download(dest: str) -> List[str]:
    os.makedirs(dest, exist_ok=True)
    sh("docker", "run", "--rm", "--network", NET, *AWS_ENV, "-v", f"{dest}:/dl", AWS_IMAGE,
       "--endpoint-url", "http://oa-s3:8333", "s3", "cp", "--recursive", f"s3://{BUCKET}/", "/dl/")
    return sorted(p for p in glob.glob(os.path.join(dest, "**", "*"), recursive=True) if os.path.isfile(p))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workdir")
    ap.add_argument("--keep", action="store_true", help="leave containers running")
    ap.add_argument("--alloy-image", default="grafana/alloy:latest")
    ap.add_argument("--target", choices=("s3", "saas"), default="s3")
    ap.add_argument("--stability-level",
                    help="Alloy --stability.level (default: what render.py requires for the target)")
    args = ap.parse_args(argv)
    saas = args.target == "saas"
    work = args.workdir or tempfile.mkdtemp(prefix="onboard-alloy-e2e-")
    shutil.rmtree(work, ignore_errors=True)
    for d in ("stub-out", "intake-out"):
        os.makedirs(os.path.join(work, d))
        os.chmod(os.path.join(work, d), 0o777)

    # Render exactly as the skill would.
    src = open(FIXTURE, encoding="utf-8").read()
    plan = inv.suggested_plan(inv.inventory(ac.graph(ac.parse(src))), ORG, CLUSTER)
    if saas:
        plan.update(target="saas", values="literal", ingest_endpoint="http://oa-intake:4318",
                    api_key_env="CARDINAL_API_KEY", k8sattributes=False)
    else:
        plan.update(values="literal", bucket=BUCKET, region="us-east-1", endpoint="http://oa-s3:8333", k8sattributes=False)
    r = render.render(src, plan)
    level = args.stability_level or r["stability_level"]
    exporter = render.exporter_id(plan)
    if any(f.severity == "error" for f in r["findings"]):
        print("render lint failed:", *r["findings"], sep="\n  ")
        return 1
    cfg = os.path.join(work, "config.alloy")
    open(cfg, "w").write(r["config"])
    stub = os.path.join(work, "stub.yaml")
    open(stub, "w").write(STUB_CONFIG)
    intake = os.path.join(work, "intake.py")
    open(intake, "w").write(INTAKE_SERVER)

    c = Checks()
    cleanup()
    try:
        alloy_ver = sh("docker", "run", "--rm", args.alloy_image, "--version").stdout.splitlines()[0]
        print(f"{alloy_ver}; target {args.target}; --stability.level={level or '(default)'}")
        sh("docker", "network", "create", NET)
        sh("docker", "run", "-d", "--name", "grafana-stub", "--network", NET, "-v", f"{stub}:/etc/stub.yaml",
           "-v", f"{work}/stub-out:/out", STUB_IMAGE, "--config", "/etc/stub.yaml")
        if saas:
            sh("docker", "run", "-d", "--name", "oa-intake", "--network", NET, "-e", f"EXPECTED_KEY={API_KEY}",
               "-v", f"{intake}:/intake.py", "-v", f"{work}/intake-out:/out", INTAKE_IMAGE, "python3", "-u", "/intake.py")
        else:
            sh("docker", "run", "-d", "--name", "oa-s3", "--network", NET, S3_IMAGE, "server", "-s3", "-dir=/data")
            for _ in range(30):
                if sh("docker", "run", "--rm", "--network", NET, *AWS_ENV, AWS_IMAGE, "--endpoint-url",
                      "http://oa-s3:8333", "s3", "mb", f"s3://{BUCKET}", check=False).returncode == 0:
                    break
                time.sleep(2)
        alloy_env = ["-e", f"CARDINAL_API_KEY={API_KEY}"] if saas else AWS_ENV
        sh("docker", "run", "-d", "--name", "oa-alloy", "--network", NET, "-v", f"{cfg}:/etc/alloy/config.alloy",
           *alloy_env, args.alloy_image, "run", "/etc/alloy/config.alloy",
           "--server.http.listen-addr=0.0.0.0:12345", *([f"--stability.level={level}"] if level else []))
        time.sleep(6)
        if not alloy_running():
            print(sh("docker", "logs", "oa-alloy", check=False).stderr[-3000:])
            c.check(False, "Alloy loads the rendered config")
            return 1
        c.check(True, "Alloy loads the rendered config")

        print("Phase 1 — happy path")
        base_secs = send_all()
        print(f"  (sent in {base_secs:.1f}s)")
        time.sleep(16)   # Cardinal batch timeout is 10s
        if saas:
            files = sorted(glob.glob(os.path.join(work, "intake-out", "*.pb")))
            c.check(bool(files), f"{len(files)} request(s) accepted by the intake")
            c.check(not os.path.exists(os.path.join(work, "intake-out", "unauthorized")),
                    "every request carried the right x-cardinalhq-api-key")
        else:
            files = s3_download(os.path.join(work, "s3"))
            prefix = os.path.join(work, "s3", "otel-raw", ORG, CLUSTER) + os.sep
            c.check(bool(files) and all(f.startswith(prefix) for f in files),
                    f"{len(files)} object(s), all under otel-raw/{ORG}/{CLUSTER}/")
        by_signal: Dict[str, Dict] = {}
        for sig in ("logs", "metrics", "traces"):
            mine = [f for f in files if os.path.basename(f).startswith(sig + "_")]
            agg = {"items": 0, "temporality": set(), "clusters": set()}
            gz_ok = True
            for f in mine:
                raw = open(f, "rb").read()
                if not saas:
                    gz_ok &= raw[:2] == b"\x1f\x8b"
                    raw = gzip.decompress(raw)
                s = summarize_proto(sig, raw)
                agg["items"] += s["items"]
                agg["temporality"] |= s["temporality"]
                agg["clusters"] |= s["clusters"]
            by_signal[sig] = agg
            c.check(bool(mine) and gz_ok, f"{sig}: {len(mine)} {'request(s)' if saas else 'file(s) named ' + sig + '_*, gzip'}")
        if not saas:
            others = [f for f in files if not os.path.basename(f).split("_")[0] in ("logs", "metrics", "traces")]
            c.check(not others, f"no unexpected file names {[os.path.basename(f) for f in others][:3]}")

        stub_s = {sig: summarize_json(sig, os.path.join(work, "stub-out", f"{sig}.json"))
                  for sig in ("logs", "metrics", "traces")}
        for sig in ("logs", "traces"):
            c.check(by_signal[sig]["items"] == stub_s[sig]["items"] > 0,
                    f"{sig}: Cardinal {by_signal[sig]['items']} == Grafana {stub_s[sig]['items']}")
        m_card, m_graf = by_signal["metrics"]["items"], stub_s["metrics"]["items"]
        # cumulativetodelta may drop the first point of each series (no previous value).
        c.check(m_graf > 0 and m_graf - 1 <= m_card <= m_graf,
                f"metrics: Cardinal {m_card} data points vs Grafana {m_graf} (first point may be dropped)")
        c.check(by_signal["metrics"]["temporality"] == {1}, f"metrics at Cardinal are delta (temporality {by_signal['metrics']['temporality']})")
        c.check(stub_s["metrics"]["temporality"] == {2}, f"metrics at Grafana still cumulative (temporality {stub_s['metrics']['temporality']})")
        c.check(all(by_signal[s]["clusters"] == {CLUSTER} for s in by_signal), "k8s.cluster.name set on every Cardinal record")
        c.check(all(stub_s[s]["clusters"] == {""} for s in stub_s), "Grafana data has no added k8s.cluster.name")

        down = "oa-intake" if saas else "oa-s3"
        print(f"Phase 2 — {'Cardinal intake' if saas else 'S3'} down")
        sh("docker", "stop", down)
        before = {s: stub_s[s]["items"] for s in stub_s}
        failures_before = len(export_failures(exporter))
        send_secs = send_all()
        time.sleep(20)
        after = {sig: summarize_json(sig, os.path.join(work, "stub-out", f"{sig}.json"))["items"]
                 for sig in ("logs", "metrics", "traces")}
        for sig in ("logs", "metrics", "traces"):
            c.check(after[sig] - before[sig] == before[sig],
                    f"{sig}: Grafana still received {after[sig] - before[sig]} (expected {before[sig]})")
        c.check(alloy_running(), "Alloy still running")
        # Blocking would show as the send taking far longer than with S3 up.
        limit = max(2 * base_secs, base_secs + 15)
        c.check(send_secs <= limit, f"senders not slowed: {send_secs:.1f}s with Cardinal down vs {base_secs:.1f}s with it up (limit {limit:.1f}s)")
        failures: List[str] = []
        for _ in range(30):   # batch timeout + client retries can take a while
            failures = export_failures(exporter)[failures_before:]
            if failures:
                break
            time.sleep(2)
        c.check(bool(failures), f"fault really hit: {len(failures)} failed export(s) logged while {down} was down")
    finally:
        if not args.keep:
            cleanup()

    print(f"RESULT: {'FAIL' if c.failed else 'PASS'} — {c.failed} check(s) failed; work dir {work}")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
