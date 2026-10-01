"""Tests for common/onboard-alloy/scripts: parser, inventory, render, lint.

Run: python3 -m unittest discover -s tests/onboard_alloy -v
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(HERE, "..", "..", "common", "onboard-alloy", "scripts"))

import alloy_config as ac  # noqa: E402
import alloy_inventory as inv  # noqa: E402
import lint_config  # noqa: E402
import onboard_env  # noqa: E402
import render  # noqa: E402

ORG = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
CLUSTER = "prod-us-east-1"


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name + ".alloy"), encoding="utf-8") as f:
        return f.read()


def plan_for(src: str, **over) -> dict:
    g = ac.graph(ac.parse(src))
    p = inv.suggested_plan(inv.inventory(g), ORG, CLUSTER)
    p.update(bucket="acme-cardinal-lake", region="us-east-1")
    p.update(over)
    return p


INGEST = "https://otelhttp.intake.us-east-2.aws.cardinalhq.io"


def saas_plan_for(src: str, **over) -> dict:
    g = ac.graph(ac.parse(src))
    p = inv.suggested_plan(inv.inventory(g), None, CLUSTER)
    p.update(target="saas", ingest_endpoint=INGEST, api_key_env="CARDINAL_API_KEY")
    p.update(over)
    return p


def rules(findings, severity="error"):
    return sorted({f.rule for f in findings if f.severity == severity})


class ParserTest(unittest.TestCase):
    def test_edges_and_sinks(self):
        g = ac.graph(ac.parse(fixture("prom_loki")))
        self.assertEqual(sorted(g.sinks()), ["loki.write.grafana", "otelcol.exporter.otlp.tempo",
                                             "prometheus.remote_write.mimir"])
        pairs = {(e.src, e.dst, e.signal) for e in g.edges}
        self.assertIn(("prometheus.relabel.drop_noise", "prometheus.remote_write.mimir", "metrics"), pairs)
        self.assertIn(("loki.process.default", "loki.write.grafana", "logs"), pairs)
        self.assertEqual(g.unresolved, [])

    def test_non_edge_references_ignored(self):
        g = ac.graph(ac.parse(fixture("otlp_grafana_cloud")))
        self.assertFalse(any(e.dst == "otelcol.auth.basic.grafana_cloud" for e in g.edges))

    def test_comments_strings_and_objects(self):
        src = ('// c\n/* block\ncomment */\nx.y "a" {\n  s = "has } brace // not comment"\n'
               '  r = `raw\n{ multi }`\n  o = { a = 1, b = [1, 2] }\n  e = "a" +\n    "b"\n'
               '  forward_to = []\n}\n')
        cfg = ac.parse(src)
        blk = cfg.blocks[0]
        self.assertEqual([a.name for a in blk.attrs], ["s", "r", "o", "e", "forward_to"])
        self.assertEqual(blk.attr("e").text(src), '"a" +\n    "b"')

    def test_syntax_errors(self):
        for bad in ['a.b "x" {', 'a.b "x" { v = "unterminated }', 'a.b "x" { v = }']:
            with self.assertRaises(ac.AlloySyntaxError, msg=bad):
                ac.parse(bad)

    def test_list_insertion_layouts(self):
        for before, after in [
            ("a.b \"x\" {\n  forward_to = []\n}\n", "[r.s.receiver]"),
            ("a.b \"x\" {\n  forward_to = [p.q.receiver]\n}\n", "[p.q.receiver, r.s.receiver]"),
            ("a.b \"x\" {\n  forward_to = [\n    p.q.receiver,\n  ]\n}\n", "[\n    p.q.receiver,\n    r.s.receiver,\n  ]"),
            # Trailing comments: a comma inside one isn't the list's, and the comment stays put.
            ("a.b \"x\" {\n  forward_to = [\n    p.q.receiver // primary, main\n  ]\n}\n",
             "[\n    p.q.receiver, // primary, main\n    r.s.receiver,\n  ]"),
            ("a.b \"x\" {\n  forward_to = [\n    p.q.receiver, // primary\n  ]\n}\n",
             "[\n    p.q.receiver, // primary\n    r.s.receiver,\n  ]"),
            ("a.b \"x\" {\n  forward_to = [\n    p.q.receiver]\n}\n", "[\n    p.q.receiver,\n    r.s.receiver,]"),
        ]:
            cfg = ac.parse(before)
            lst = ac.ref_list(cfg.blocks[0].attr("forward_to"))
            out = ac.apply_insertions(before, ac.list_insertion(before, lst, "r.s.receiver"))
            self.assertIn(after, out)
            ac.parse(out)

    def test_mask_secrets(self):
        masked = ac.mask_secrets(fixture("otlp_grafana_cloud"))
        self.assertNotIn("glc_", masked)
        self.assertIn('password = "<redacted>"', masked)
        self.assertIn('password = sys.env("GRAFANA_CLOUD_API_KEY")', ac.mask_secrets(fixture("prom_loki")))

    def test_mask_secrets_other_forms(self):
        cases = [
            'authorization {\n  type        = "Bearer"\n  credentials = "s3cr3t-1"\n}\n',
            'tls {\n  key_pem = `-----BEGIN KEY-----\ns3cr3t-2\n-----END KEY-----`\n}\n',
            'headers = {\n  "Authorization" = "Bearer s3cr3t-3",\n}\n',
            'basic_auth { username = "u"  password = "s3cr3t-4" }\n',
            'header {\n  key   = "X-Scope"\n  value = "Basic s3cr3t-5"\n}\n',
            '+  password = "s3cr3t-6"\n',
        ]
        for i, text in enumerate(cases, 1):
            with self.subTest(i=i):
                self.assertNotIn(f"s3cr3t-{i}", ac.mask_secrets(text))
        kept = 'password = sys.env("X")\nendpoint = "https://h"\ntype = "Bearer"\n'
        self.assertEqual(ac.mask_secrets(kept), kept)


class InventoryTest(unittest.TestCase):
    def taps(self, name):
        return {(t["component"], t["attr"], t["signal"], t["kind"])
                for t in inv.inventory(ac.graph(ac.parse(fixture(name))))["taps"]}

    def test_otlp_taps_after_processing(self):
        self.assertEqual(self.taps("otlp_grafana_cloud"), {
            ("otelcol.processor.batch.default", "output.metrics", "metrics", "otlp"),
            ("otelcol.processor.batch.default", "output.logs", "logs", "otlp"),
            ("otelcol.processor.batch.default", "output.traces", "traces", "otlp"),
        })

    def test_prometheus_and_loki_bridges(self):
        self.assertEqual(self.taps("prom_loki"), {
            ("prometheus.relabel.drop_noise", "forward_to", "metrics", "prometheus"),
            ("loki.process.default", "forward_to", "logs", "loki"),
            ("otelcol.processor.tail_sampling.default", "output.traces", "traces", "otlp"),
        })

    def test_converters_tapped_as_otlp(self):
        # Tapping before otelcol.exporter.prometheus/loki avoids an OTLP->Prom->OTLP round trip.
        self.assertEqual(self.taps("otel_converters"), {
            ("otelcol.processor.batch.default", "output.metrics", "metrics", "otlp"),
            ("otelcol.processor.batch.default", "output.logs", "logs", "otlp"),
            ("otelcol.processor.batch.default", "output.traces", "traces", "otlp"),
        })

    def test_k8sattributes_detection(self):
        self.assertFalse(plan_for(fixture("otlp_grafana_cloud"))["k8sattributes"])
        self.assertTrue(plan_for(fixture("otel_converters"))["k8sattributes"])

    def test_warnings(self):
        w = " ".join(inv.inventory(ac.graph(ac.parse(fixture("prom_loki"))))["warnings"])
        self.assertIn("tail-sampled", w)
        src = fixture("otlp_grafana_cloud") + '\nremotecfg {\n  url = "https://fleet"\n}\n'
        w = " ".join(inv.inventory(ac.graph(ac.parse(src)))["warnings"])
        self.assertIn("Fleet Management", w)

    def test_duplicate_exporter_warning(self):
        # Traces leave k8sattributes for a second exporter too: two taps would send them twice.
        src = fixture("otlp_grafana_cloud").replace(
            "traces  = [otelcol.processor.batch.default.input]",
            "traces  = [otelcol.processor.batch.default.input, otelcol.exporter.otlp.other.input]",
        ) + 'otelcol.exporter.otlp "other" {\n  client { endpoint = "x:4317" }\n}\n'
        self.assertIn("otlp.other.input", src)
        w = " ".join(inv.inventory(ac.graph(ac.parse(src)))["warnings"])
        self.assertIn("duplicates", w)


class RenderTest(unittest.TestCase):
    def test_all_fixtures_render_and_lint_clean(self):
        for name in ("otlp_grafana_cloud", "prom_loki", "otel_converters", "customer_cardinal_label"):
            for values in ("env", "literal"):
                with self.subTest(name=name, values=values):
                    src = fixture(name)
                    r = render.render(src, plan_for(src, values=values))
                    self.assertEqual(rules(r["findings"]), [], [str(f) for f in r["findings"]])
                    ac.parse(r["config"])

    def test_grafana_path_untouched(self):
        src = fixture("otel_converters")
        r = render.render(src, plan_for(src))
        g = ac.graph(ac.parse(r["config"]))
        for sink in ("prometheus.remote_write.mimir", "loki.write.grafana", "otelcol.exporter.otlp.tempo"):
            self.assertFalse(any(u.endswith(".cardinal_onboard") for u in g.upstream(sink)), sink)

    def test_bridges_rendered(self):
        src = fixture("prom_loki")
        r = render.render(src, plan_for(src))
        self.assertIn("forward_to = [prometheus.remote_write.mimir.receiver, otelcol.receiver.prometheus.cardinal_onboard.receiver]", r["config"])
        self.assertIn("forward_to = [loki.write.grafana.receiver, otelcol.receiver.loki.cardinal_onboard.receiver]", r["config"])
        self.assertIn('otelcol.receiver.prometheus "cardinal_onboard"', r["block"])
        self.assertIn('otelcol.receiver.loki "cardinal_onboard"', r["block"])

    def test_signals_limited_to_taps(self):
        src = fixture("otlp_grafana_cloud")
        p = plan_for(src)
        for t in p["taps"]:
            t["enabled"] = t["signal"] == "logs"
        r = render.render(src, p)
        self.assertEqual(r["signals"], ["logs"])
        self.assertNotIn("cumulativetodelta", r["block"])
        self.assertEqual(rules(r["findings"]), [])

    def test_env_vs_literal_values(self):
        src = fixture("otlp_grafana_cloud")
        env = render.render(src, plan_for(src))
        self.assertIn('sys.env("K8S_CLUSTER_NAME")', env["block"])
        self.assertNotIn("${", env["block"])  # Alloy doesn't expand ${VAR} inside strings
        self.assertEqual(env["env"]["K8S_CLUSTER_NAME"], CLUSTER)
        lit = render.render(src, plan_for(src, values="literal"))
        self.assertIn(f'"otel-raw/{ORG}/{CLUSTER}"', lit["block"])
        self.assertNotIn("sys.env", lit["block"])
        self.assertEqual(lit["env"], {})

    def test_rerender_is_idempotent(self):
        src = fixture("otel_converters")
        p = plan_for(src)
        first = render.render(src, p)
        second = render.render(first["config"], p)
        self.assertTrue(second["updated"])
        self.assertEqual(first["config"], second["config"])
        stripped, _ = render.strip_cardinal(first["config"])
        self.assertEqual(stripped.rstrip("\n"), src.rstrip("\n"))

    def test_customer_cardinal_label_left_alone(self):
        src = fixture("customer_cardinal_label")
        p = plan_for(src)
        self.assertEqual({t["component"] for t in p["taps"]}, {"otelcol.processor.batch.cardinal"})
        first = render.render(src, p)
        second = render.render(first["config"], p)
        self.assertEqual(rules(second["findings"]), [])
        self.assertEqual(first["config"], second["config"])
        self.assertEqual(second["config"].count("otelcol.processor.batch.cardinal.input"), 3)
        stripped, _ = render.strip_cardinal(first["config"])
        self.assertEqual(stripped.rstrip("\n"), src.rstrip("\n"))

    def test_reserved_label_outside_block_refused(self):
        src = fixture("otlp_grafana_cloud").replace('otelcol.processor.batch "default"',
                                                    'otelcol.processor.batch "cardinal_onboard"')
        src = src.replace("otelcol.processor.batch.default.input", "otelcol.processor.batch.cardinal_onboard.input")
        with self.assertRaisesRegex(render.PlanError, "reserves that label"):
            render.strip_cardinal(src)
        rendered = render.render(fixture("otlp_grafana_cloud"), plan_for(fixture("otlp_grafana_cloud")))["config"]
        with self.assertRaisesRegex(render.PlanError, "reserves that label"):
            render.strip_cardinal(rendered + '\notelcol.processor.batch "cardinal_onboard" {}\n')

    def test_iam_partition_follows_region(self):
        src = fixture("otlp_grafana_cloud")
        for region, arn in [("us-east-1", "arn:aws:s3"), ("us-gov-west-1", "arn:aws-us-gov:s3"),
                            ("cn-north-1", "arn:aws-cn:s3"), ("us-iso-east-1", "arn:aws-iso:s3"),
                            ("us-isob-east-1", "arn:aws-iso-b:s3")]:
            with self.subTest(region=region):
                pol = render.render(src, plan_for(src, region=region))["iam"]
                self.assertTrue(pol["Statement"][0]["Resource"].startswith(arn + ":::"))

    def test_customer_awss3_left_alone(self):
        src = fixture("otlp_grafana_cloud") + (
            '\notelcol.processor.batch "archive" {\n  output {\n'
            '    traces = [otelcol.exporter.awss3.archive.input]\n  }\n}\n'
            '\notelcol.exporter.awss3 "archive" {\n  s3_uploader {\n    region    = "us-east-1"\n'
            '    s3_bucket = "acme-archive"\n  }\n}\n')
        src = src.replace("traces  = [otelcol.exporter.otlphttp.grafana_cloud.input]",
                          "traces  = [otelcol.exporter.otlphttp.grafana_cloud.input, otelcol.processor.batch.archive.input]")
        r = render.render(src, plan_for(src))
        self.assertEqual([f for f in r["findings"] if f.severity == "error"], [])

    def test_iam_scoped_to_cluster_prefix(self):
        src = fixture("otlp_grafana_cloud")
        pol = render.render(src, plan_for(src, kms_key_arn="arn:aws:kms:us-east-1:1:key/abc"))["iam"]
        self.assertEqual(pol["Statement"][0]["Resource"],
                         f"arn:aws:s3:::acme-cardinal-lake/otel-raw/{ORG}/{CLUSTER}/*")
        self.assertEqual(pol["Statement"][0]["Action"], ["s3:PutObject"])
        self.assertEqual(pol["Statement"][1]["Action"], ["kms:GenerateDataKey"])

    def test_diff_is_masked(self):
        src = fixture("otlp_grafana_cloud")
        self.assertNotIn("glc_", render.render(src, plan_for(src))["diff"])

    def test_bad_plans(self):
        src = fixture("otlp_grafana_cloud")
        good = plan_for(src)
        for key, val in [("org_id", "not-a-uuid"), ("cluster", "Prod_1"), ("values", "yaml"),
                         ("batch", {"send_batch_size": "10000"})]:
            with self.subTest(key=key), self.assertRaises(render.PlanError):
                render.render(src, {**copy.deepcopy(good), key: val})
        with self.assertRaises(render.PlanError):
            render.render(src, {**copy.deepcopy(good), "values": "literal", "bucket": ""})
        stale = copy.deepcopy(good)
        stale["taps"][0]["component"] = "otelcol.processor.batch.gone"
        with self.assertRaises(render.PlanError):
            render.render(src, stale)
        none = copy.deepcopy(good)
        for t in none["taps"]:
            t["enabled"] = False
        with self.assertRaises(render.PlanError):
            render.render(src, none)

    def test_cli_writes_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(FIXTURES, "prom_loki.alloy")
            self.assertEqual(inv.main(["--config", cfg, "--out", d, "--org-id", ORG, "--cluster", CLUSTER]), 0)
            with open(os.path.join(d, "plan.json")) as f:
                p = json.load(f)
            p.update(bucket="acme-cardinal-lake", region="us-east-1")
            with open(os.path.join(d, "plan.json"), "w") as f:
                json.dump(p, f)
            out = os.path.join(d, "out")
            self.assertEqual(render.main(["--config", cfg, "--plan", os.path.join(d, "plan.json"), "--out", out]), 0)
            for f in ("config.alloy", "cardinal.alloy", "changes.review.diff", "env.json", "iam-policy.json", "render.json"):
                self.assertTrue(os.path.exists(os.path.join(out, f)), f)
            self.assertEqual(os.stat(os.path.join(out, "config.alloy")).st_mode & 0o777, 0o600)
            self.assertEqual(lint_config.main(["--config", os.path.join(out, "config.alloy"),
                                               "--original", cfg, "--plan", os.path.join(d, "plan.json")]), 0)


class EnvFileTest(unittest.TestCase):
    def write(self, d, **vals):
        path = os.path.join(d, ".env.onboard-alloy")
        self.assertEqual(onboard_env.main(["--init", path]), 0)
        with open(path) as f:
            text = f.read()
        for k, v in vals.items():
            text = re.sub(rf"(?m)^{k}=.*$", lambda _: f"{k}={v}", text)
        with open(path, "w") as f:
            f.write(text)
        return path

    def good(self):
        return dict(TARGET="s3", ALLOY_CONFIG=os.path.join(FIXTURES, "prom_loki.alloy"), CARDINAL_ORG_ID=ORG,
                    CLUSTER_NAME=CLUSTER, S3_BUCKET="acme-cardinal-lake", AWS_REGION="us-east-1")

    def test_init_template(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            vals = onboard_env.read(path)
            self.assertEqual(set(vals), set(onboard_env.KEYS))
            self.assertEqual(vals["VALUES_MODE"], "env")
            problems, _ = onboard_env.check(path, vals)
            # No default target: SaaS and in-VPC customers must say which.
            self.assertEqual(vals["TARGET"], "")
            self.assertEqual(len(problems), 1)
            self.assertTrue(problems[0].startswith("TARGET is empty"), problems)
            # A file from before TARGET existed still means s3.
            with open(path) as f:
                legacy = re.sub(r"(?m)^TARGET=.*\n", "", f.read())
            legacy_path = os.path.join(d, "legacy")
            with open(legacy_path, "w") as f:
                f.write(legacy)
            problems, _ = onboard_env.check(legacy_path, onboard_env.read(legacy_path))
            self.assertEqual(len(problems), len(onboard_env.REQUIRED["s3"]), problems)
            with open(path, "a") as f:
                f.write("# keep me\n")
            onboard_env.main(["--init", path])   # never overwrites
            with open(path) as f:
                self.assertIn("# keep me", f.read())

    def test_check_good_and_relative_path(self):
        with tempfile.TemporaryDirectory() as d:
            import shutil
            shutil.copy(os.path.join(FIXTURES, "prom_loki.alloy"), os.path.join(d, "config.alloy"))
            path = self.write(d, **{**self.good(), "ALLOY_CONFIG": "config.alloy"})
            self.assertEqual(onboard_env.main(["--check", path]), 0)

    def test_check_bad_values(self):
        cases = {
            "CARDINAL_ORG_ID": "ORG-123", "CLUSTER_NAME": "Prod_Cluster", "S3_BUCKET": "s3://acme/lake",
            "AWS_REGION": "virginia", "S3_ENDPOINT": "minio:9000", "KMS_KEY_ARN": "key-123",
            "VALUES_MODE": "yaml", "ALLOY_CONFIG": "/nope/config.alloy",
        }
        for key, bad in cases.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                path = self.write(d, **{**self.good(), key: bad})
                problems, _ = onboard_env.check(path, onboard_env.read(path))
                self.assertTrue(any(p.startswith(key) for p in problems), problems)

    def test_placeholder_counts_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, **{**self.good(), "S3_BUCKET": "<bucket>"})
            self.assertIn("S3_BUCKET is empty", onboard_env.check(path, onboard_env.read(path))[0])

    def test_scripts_use_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, **{**self.good(), "S3_ENDPOINT": "https://minio.local", "VALUES_MODE": "literal"})
            out = os.path.join(d, "onboard")
            self.assertEqual(inv.main(["--env-file", path, "--out", out]), 0)
            with open(os.path.join(out, "plan.json")) as f:
                plan = json.load(f)
            self.assertEqual((plan["org_id"], plan["cluster"], plan["bucket"], plan["endpoint"], plan["values"]),
                             (ORG, CLUSTER, "acme-cardinal-lake", "https://minio.local", "literal"))
            rout = os.path.join(out, "out")
            self.assertEqual(render.main(["--env-file", path, "--plan", os.path.join(out, "plan.json"), "--out", rout]), 0)
            with open(os.path.join(rout, "cardinal.alloy")) as f:
                self.assertIn('endpoint            = "https://minio.local"', f.read())

    def test_incomplete_file_stops_scripts(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d)
            with self.assertRaises(SystemExit) as cm:
                inv.main(["--env-file", path, "--out", os.path.join(d, "o")])
            self.assertEqual(cm.exception.code, 3)


class SaasEnvFileTest(unittest.TestCase):
    """TARGET=saas: no bucket/region; ingest endpoint and the API key's env var name instead."""

    write = EnvFileTest.write

    def good(self):
        return dict(TARGET="saas", ALLOY_CONFIG=os.path.join(FIXTURES, "prom_loki.alloy"),
                    CLUSTER_NAME=CLUSTER, CARDINAL_INGEST_ENDPOINT=INGEST)

    def test_saas_needs_no_bucket_or_org(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, **self.good())
            self.assertEqual(onboard_env.main(["--check", path]), 0)

    def test_saas_required_and_bad_values(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, TARGET="saas")
            problems, _ = onboard_env.check(path, onboard_env.read(path))
            self.assertIn("CARDINAL_INGEST_ENDPOINT is empty", problems)
            self.assertNotIn("S3_BUCKET is empty", problems)
            self.assertNotIn("CARDINAL_ORG_ID is empty", problems)
        cases = {"CARDINAL_INGEST_ENDPOINT": INGEST + "/v1/logs", "TARGET": "cloud",
                 "CARDINAL_API_KEY_ENV": "ck_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0", "CARDINAL_ORG_ID": "ORG-1"}
        for key, bad in cases.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                path = self.write(d, **{**self.good(), key: bad})
                problems, _ = onboard_env.check(path, onboard_env.read(path))
                self.assertTrue(any(p.startswith(key) for p in problems), problems)

    def test_pasted_key_not_echoed(self):
        key = "ck_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, **{**self.good(), "CARDINAL_API_KEY_ENV": key})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(onboard_env.main(["--check", path]), 3)
            self.assertNotIn(key, buf.getvalue())

    def test_saas_scripts_use_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.write(d, **{**self.good(), "CARDINAL_API_KEY_ENV": "CARDINAL_KEY"})
            out = os.path.join(d, "onboard")
            self.assertEqual(inv.main(["--env-file", path, "--out", out]), 0)
            with open(os.path.join(out, "plan.json")) as f:
                plan = json.load(f)
            self.assertEqual((plan["target"], plan["ingest_endpoint"], plan["api_key_env"]),
                             ("saas", INGEST, "CARDINAL_KEY"))
            rout = os.path.join(out, "out")
            os.makedirs(rout)
            with open(os.path.join(rout, "iam-policy.json"), "w") as f:
                f.write("{}")   # left over from an earlier s3 render
            self.assertEqual(render.main(["--env-file", path, "--plan", os.path.join(out, "plan.json"), "--out", rout]), 0)
            self.assertFalse(os.path.exists(os.path.join(rout, "iam-policy.json")))
            with open(os.path.join(rout, "cardinal.alloy")) as f:
                self.assertIn('"x-cardinalhq-api-key" = sys.env("CARDINAL_KEY")', f.read())
            with open(os.path.join(rout, "render.json")) as f:
                rj = json.load(f)
            self.assertEqual((rj["target"], rj["alloy"]["stability_level"]), ("saas", "public-preview"))


class ConnectionPrefillTest(unittest.TestCase):
    """--init --from-connection / --set: fill in what's already known, flag a stale file."""

    def conn(self, d, host="https://app.cardinalhq.io", **over):
        state = {"schema_version": 4, "host": host, "org_id": ORG, "org_slug": "acme",
                 "ingest_endpoint": INGEST + "/", "ingest_key_prefix": "a9d48cbe", **over}
        path = os.path.join(d, "cardinal.json")
        with open(path, "w") as f:
            json.dump(state, f)
        return path

    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = onboard_env.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_connection_never_decides_target(self):
        # SaaS and in-VPC customers both connect to app.cardinalhq.io: only the org is known.
        for host in ("https://app.cardinalhq.io", "https://maestro.acme.internal"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as d:
                env = os.path.join(d, ".env.onboard-alloy")
                code, out, _ = self.run_main(["--init", env, "--from-connection", self.conn(d, host=host)])
                self.assertEqual(code, 0)
                vals = onboard_env.read(env)
                self.assertEqual((vals["TARGET"], vals["CARDINAL_ORG_ID"], vals["CARDINAL_INGEST_ENDPOINT"]),
                                 ("", ORG, ""))
                self.assertIn("TARGET left empty: ask the user", out)
                self.assertTrue(onboard_env.check(env, vals)[0][0].startswith("TARGET is empty"))

    def test_saas_answer_prefills_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            code, out, _ = self.run_main(["--init", env, "--from-connection", self.conn(d), "--set", "TARGET=saas"])
            self.assertEqual(code, 0)
            vals = onboard_env.read(env)
            self.assertEqual((vals["TARGET"], vals["CARDINAL_ORG_ID"], vals["CARDINAL_INGEST_ENDPOINT"]),
                             ("saas", ORG, INGEST))
            self.assertIn("prefilled CARDINAL_INGEST_ENDPOINT", out)
            self.assertNotIn("TARGET left empty", out)
            self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)

    def test_vpc_customer_on_saas_control_plane(self):
        # Connected to app.cardinalhq.io, data lake in their own VPC: no SaaS endpoint.
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            self.run_main(["--init", env, "--from-connection", self.conn(d), "--set", "TARGET=s3"])
            vals = onboard_env.read(env)
            self.assertEqual((vals["TARGET"], vals["CARDINAL_ORG_ID"], vals["CARDINAL_INGEST_ENDPOINT"]),
                             ("s3", ORG, ""))
            problems = onboard_env.check(env, vals)[0]
            self.assertIn("S3_BUCKET is empty", problems)
            self.assertNotIn("CARDINAL_INGEST_ENDPOINT is empty", problems)

    def test_bad_target_answer(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            self.assertEqual(self.run_main(["--init", env, "--set", "TARGET=vpc"])[0], 2)
            self.assertFalse(os.path.exists(env))

    def test_not_connected_leaves_template(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            code, out, _ = self.run_main(["--init", env, "--from-connection", os.path.join(d, "missing.json")])
            self.assertEqual(code, 0)
            self.assertIn("not connected", out)
            with open(env) as f:
                self.assertEqual(f.read(), onboard_env.TEMPLATE)

    def test_agent_home_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            self.conn(d)
            old = os.environ.get("CARDINAL_AGENT_HOME")
            os.environ["CARDINAL_AGENT_HOME"] = d
            try:
                self.assertEqual(onboard_env.connection()["org_slug"], "acme")
            finally:
                if old is None:
                    del os.environ["CARDINAL_AGENT_HOME"]
                else:
                    os.environ["CARDINAL_AGENT_HOME"] = old

    def test_set_and_runtime_guess(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            self.run_main(["--init", env, "--set", "ALLOY_CONFIG=/opt/homebrew/etc/alloy/config.alloy",
                           "--set", "CLUSTER_NAME=alloy-local"])
            vals = onboard_env.read(env)
            self.assertEqual((vals["RUNTIME"], vals["CLUSTER_NAME"]), ("host", "alloy-local"))
            env2 = os.path.join(d, "two")
            self.run_main(["--init", env2, "--set", "ALLOY_CONFIG=/etc/alloy/config.alloy",
                           "--set", "RUNTIME=kubernetes"])
            self.assertEqual(onboard_env.read(env2)["RUNTIME"], "kubernetes")
            env3 = os.path.join(d, "three")
            self.run_main(["--init", env3, "--set", "ALLOY_CONFIG=gitops/alloy/config.alloy"])
            self.assertEqual(onboard_env.read(env3)["RUNTIME"], "kubernetes")

    def test_bad_set_rejected_without_echo(self):
        key = "ck_live_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            for pair in (f"CARDINAL_API_KEY_ENV={key}", "AWS_SECRET_ACCESS_KEY=x", "TARGET"):
                with self.subTest(pair=pair):
                    code, out, err = self.run_main(["--init", env, "--set", pair])
                    self.assertEqual(code, 2)
                    self.assertNotIn(key, out + err)
                    self.assertFalse(os.path.exists(env))

    def test_stale_file_differs_then_replace(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env.onboard-alloy")
            # An older template (no TARGET or RUNTIME line) for another org.
            old = "\n".join(f"{k}=" for k in onboard_env.KEYS if k not in ("TARGET", "RUNTIME"))
            old = old.replace("CARDINAL_ORG_ID=", "CARDINAL_ORG_ID=6f5e586d-6286-475e-9bd7-1a54391d3115")
            with open(env, "w") as f:
                f.write(old + "\n")
            conn = self.conn(d)
            code, out, _ = self.run_main(["--init", env, "--from-connection", conn])
            self.assertEqual(code, onboard_env.EXIT_DIFFERS)
            self.assertIn("DIFFERS  file is from an older template: no TARGET, RUNTIME line", out)
            self.assertIn(f"DIFFERS  CARDINAL_ORG_ID: file has 6f5e586d", out)
            with open(env) as f:
                self.assertEqual(f.read(), old + "\n")   # untouched
            code, out, _ = self.run_main(["--init", env, "--from-connection", conn, "--replace"])
            self.assertEqual(code, 0)
            self.assertEqual(onboard_env.read(env)["CARDINAL_ORG_ID"], ORG)
            baks = [n for n in os.listdir(d) if n.startswith(".env.onboard-alloy.bak-")]
            self.assertEqual(len(baks), 1)
            with open(os.path.join(d, baks[0])) as f:
                self.assertEqual(f.read(), old + "\n")
            # Now it matches: re-running is a no-op.
            self.assertEqual(self.run_main(["--init", env, "--from-connection", conn])[0], 0)

    def test_check_compares_with_connection(self):
        with tempfile.TemporaryDirectory() as d:
            path = EnvFileTest.write(self, d, TARGET="saas", ALLOY_CONFIG=os.path.join(FIXTURES, "prom_loki.alloy"),
                                     CLUSTER_NAME=CLUSTER, CARDINAL_INGEST_ENDPOINT=INGEST,
                                     CARDINAL_ORG_ID="6f5e586d-6286-475e-9bd7-1a54391d3115")
            code, out, _ = self.run_main(["--check", path, "--from-connection", self.conn(d)])
            self.assertEqual(code, 0)   # a note, not a problem: another org can be intended
            self.assertIn("CARDINAL_ORG_ID is 6f5e586d", out)
            self.assertIn("connection (acme on https://app.cardinalhq.io)", out)
            self.assertNotIn("CARDINAL_INGEST_ENDPOINT is", out)

    def test_check_notes_older_template_and_host_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env.onboard-alloy")
            with open(path, "w") as f:
                f.write("ALLOY_CONFIG=/opt/homebrew/etc/alloy/config.alloy\n")
            _, notes = onboard_env.check(path, onboard_env.read(path))
            self.assertTrue(any("older template" in n and "TARGET" in n for n in notes), notes)
            self.assertTrue(any("RUNTIME=host may fit better" in n for n in notes), notes)


class HostRuntimeTest(unittest.TestCase):
    """RUNTIME=host: Homebrew / Linux-package Alloy, no Kubernetes."""

    def test_host_turns_off_k8sattributes_and_says_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = EnvFileTest.write(self, d, TARGET="saas", ALLOY_CONFIG=os.path.join(FIXTURES, "otel_converters.alloy"),
                                     CLUSTER_NAME="alloy-local", CARDINAL_INGEST_ENDPOINT=INGEST, RUNTIME="host")
            out = os.path.join(d, "onboard")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(inv.main(["--env-file", path, "--out", out]), 0)
            with open(os.path.join(out, "plan.json")) as f:
                plan = json.load(f)
            self.assertEqual((plan["runtime"], plan["k8sattributes"]), ("host", False))
            # An older plan.json that still says true is overridden by the file.
            plan["k8sattributes"] = True
            with open(os.path.join(out, "plan.json"), "w") as f:
                json.dump(plan, f)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(render.main(["--env-file", path, "--plan", os.path.join(out, "plan.json"),
                                              "--out", os.path.join(out, "out")]), 0)
            text = buf.getvalue()
            self.assertNotIn("Kubernetes Secret", text)
            self.assertIn("in the env file the Alloy service loads", text)
            self.assertIn("K8S_CLUSTER_NAME, CARDINAL_INGEST_ENDPOINT, CARDINAL_API_KEY in the env file", text)
            with open(os.path.join(out, "out", "cardinal.alloy")) as f:
                self.assertNotIn("k8sattributes", f.read())
            with open(os.path.join(out, "out", "env.json")) as f:
                self.assertIn("env file the Alloy service loads", json.load(f)["CARDINAL_API_KEY"])

    def test_kubernetes_wording_unchanged(self):
        src = fixture("otlp_grafana_cloud")
        r = render.render(src, saas_plan_for(src))
        self.assertIn("Kubernetes Secret", r["env"]["CARDINAL_API_KEY"])

    def test_bad_runtime_plans(self):
        src = fixture("otel_converters")
        for over in ({"runtime": "vm"}, {"runtime": "host", "k8sattributes": True}):
            with self.subTest(over=over), self.assertRaises(render.PlanError):
                render.render(src, saas_plan_for(src, **over))
        render.render(src, saas_plan_for(src, runtime="host", k8sattributes=False))


class SaasRenderTest(unittest.TestCase):
    def test_existing_cardinal_exporter_not_tapped(self):
        # A hand-written SaaS exporter (the pre-skill workaround) isn't Grafana-bound.
        src = fixture("otlp_grafana_cloud").replace(
            "otelcol.exporter.otlphttp.grafana_cloud.input]",
            "otelcol.exporter.otlphttp.grafana_cloud.input, otelcol.exporter.otlphttp.cardinal_saas.input]")
        src += ('\notelcol.exporter.otlphttp "cardinal_saas" {\n  client {\n'
                '    endpoint = sys.env("CARDINAL_INGEST_ENDPOINT")\n'
                '    headers  = { "x-cardinalhq-api-key" = sys.env("CARDINAL_API_KEY") }\n  }\n}\n')
        i = inv.inventory(ac.graph(ac.parse(src)))
        self.assertTrue(any("cardinal_saas" in w and "already sends to Cardinal" in w for w in i["warnings"]))
        self.assertEqual({t["sink"] for t in i["taps"]}, {"otelcol.exporter.otlphttp.grafana_cloud"})
        # Fed only by a list that goes nowhere else: not a tap.
        alone = src.replace("otelcol.exporter.otlphttp.grafana_cloud.input, otelcol.exporter.otlphttp.cardinal_saas.input]",
                            "otelcol.exporter.otlphttp.cardinal_saas.input]", 1)
        self.assertNotEqual(alone, src)
        taps = inv.inventory(ac.graph(ac.parse(alone)))["taps"]
        self.assertFalse(any(t["sink"].endswith("cardinal_saas") for t in taps), taps)


    def test_all_fixtures_render_and_lint_clean(self):
        for name in ("otlp_grafana_cloud", "prom_loki", "otel_converters", "customer_cardinal_label"):
            for values in ("env", "literal"):
                with self.subTest(name=name, values=values):
                    src = fixture(name)
                    r = render.render(src, saas_plan_for(src, values=values))
                    self.assertEqual(rules(r["findings"]), [], [str(f) for f in r["findings"]])
                    g = ac.graph(ac.parse(r["config"]))
                    self.assertIn("otelcol.exporter.otlphttp.cardinal_onboard", g.nodes)
                    self.assertNotIn("otelcol.exporter.awss3.cardinal_onboard", g.nodes)
                    self.assertIsNone(r["iam"])

    def test_key_only_from_env(self):
        src = fixture("otlp_grafana_cloud")
        for values in ("env", "literal"):
            r = render.render(src, saas_plan_for(src, values=values))
            self.assertIn('"x-cardinalhq-api-key" = sys.env("CARDINAL_API_KEY")', r["block"])
            self.assertTrue(r["env"]["CARDINAL_API_KEY"].startswith("<secret"))
        env = render.render(src, saas_plan_for(src))
        self.assertIn('endpoint = sys.env("CARDINAL_INGEST_ENDPOINT")', env["block"])
        self.assertEqual(env["env"]["CARDINAL_INGEST_ENDPOINT"], INGEST)
        self.assertNotIn("LAKERUNNER_ORGANIZATION_ID", env["env"])
        lit = render.render(src, saas_plan_for(src, values="literal"))
        self.assertIn(f'endpoint = "{INGEST}"', lit["block"])

    def test_stability_level(self):
        src = fixture("otlp_grafana_cloud")
        self.assertEqual(render.render(src, saas_plan_for(src))["stability_level"], "public-preview")
        p = saas_plan_for(src)
        for t in p["taps"]:
            t["enabled"] = t["signal"] != "metrics"
        r = render.render(src, p)
        self.assertIsNone(r["stability_level"])
        self.assertIn("Uses only stable Alloy components", r["block"])
        self.assertEqual(render.render(src, plan_for(src))["stability_level"], "experimental")

    def test_bad_plans(self):
        src = fixture("otlp_grafana_cloud")
        for over in ({"target": "cloud"}, {"values": "literal", "ingest_endpoint": None},
                     {"ingest_endpoint": INGEST + "/v1/traces"}, {"api_key_env": "ck_live_abc"},
                     {"org_id": "not-a-uuid"}):
            with self.subTest(over=over), self.assertRaises(render.PlanError):
                render.render(src, saas_plan_for(src, **over))

    def test_switch_target_updates_in_place(self):
        src = fixture("prom_loki")
        s3 = render.render(src, plan_for(src))["config"]
        saas = render.render(s3, saas_plan_for(src))
        self.assertTrue(saas["updated"])
        self.assertNotIn("awss3", saas["block"])
        self.assertEqual(rules(saas["findings"]), [])
        self.assertEqual(saas["config"].count(">>> cardinal onboard-alloy"), 1)


class LintTest(unittest.TestCase):
    """Each rule fires on a config that breaks it."""

    def rendered(self, name="otlp_grafana_cloud"):
        src = fixture(name)
        p = plan_for(src)
        return src, render.render(src, p)["config"], p

    def lint(self, original, patched, plan):
        return lint_config.lint(ac.graph(ac.parse(patched)), ac.graph(ac.parse(original)), plan)

    def test_c001_cardinal_feeds_grafana(self):
        o, c, p = self.rendered()
        leaked = c.replace("logs    = [otelcol.processor.batch.cardinal_onboard.input]",
                           "logs    = [otelcol.processor.batch.cardinal_onboard.input, otelcol.exporter.otlphttp.grafana_cloud.input]")
        self.assertNotEqual(leaked, c)
        self.assertIn("C001", rules(self.lint(o, leaked, p)))

    def test_c002_delta_upstream_of_grafana(self):
        o, c, p = self.rendered()
        c = c.replace("metrics = [otelcol.exporter.otlphttp.grafana_cloud.input,",
                      "metrics = [otelcol.processor.cumulativetodelta.oops.input,")
        self.assertIn("cumulativetodelta.oops.input", c)
        c += ('\notelcol.processor.cumulativetodelta "oops" {\n  output {\n'
              '    metrics = [otelcol.exporter.otlphttp.grafana_cloud.input]\n  }\n}\n')
        self.assertIn("C002", rules(self.lint(o, c, p)))

    def test_c003_c004_batching(self):
        o, c, p = self.rendered()
        self.assertIn("C004", rules(self.lint(o, c.replace("send_batch_size     = 10000", "send_batch_size     = 100"), p)))
        bypass = c.replace("traces  = [otelcol.processor.batch.cardinal_onboard.input]",
                           "traces  = [otelcol.exporter.awss3.cardinal_onboard.input]")
        self.assertIn("C003", rules(self.lint(o, bypass, p)))

    def test_c005_c006_c007_exporter(self):
        o, c, p = self.rendered()
        self.assertIn("C005", rules(self.lint(o, c.replace('type = "otlp_proto"', 'type = "otlp_json"'), p)))
        self.assertIn("C005", rules(self.lint(o, c.replace('compression         = "gzip"', 'compression         = "none"'), p)))
        fp = c.replace('    s3_force_path_style = true', '    s3_force_path_style = true\n    file_prefix = "x"')
        self.assertIn("C006", rules(self.lint(o, fp, p)))
        self.assertIn("C007", rules(self.lint(o, c.replace('"otel-raw/" +', '"raw/" +'), p)))

    def test_c013_blocking_queue(self):
        o, c, p = self.rendered()
        for old, new in [("block_on_overflow = false", "block_on_overflow = true"),
                         ("wait_for_result   = false", "wait_for_result   = true"),
                         ("    enabled           = true\n    block", "    enabled           = false\n    block")]:
            broken = c.replace(old, new)
            self.assertNotEqual(broken, c, old)
            self.assertIn("C013", rules(self.lint(o, broken, p)), old)
        start = c.index("  sending_queue {")
        no_queue = c[:start] + c[c.index("  }\n", start) + 4:]
        self.assertIn("C013", rules(self.lint(o, no_queue, p)))

    def test_c008_metrics_skip_delta(self):
        o, c, p = self.rendered()
        c = c.replace("metrics = [otelcol.processor.cumulativetodelta.cardinal_onboard.input]",
                      "metrics = [otelcol.processor.batch.cardinal_onboard.input]")
        self.assertIn("C008", rules(self.lint(o, c, p)))

    def test_c009_customer_config_changed(self):
        o, c, p = self.rendered()
        self.assertIn("C009", rules(self.lint(o, c.replace('"0.0.0.0:4317"', '"0.0.0.0:14317"'), p)))
        self.assertIn("C009", rules(self.lint(o, c.replace(
            "metrics = [otelcol.processor.batch.default.input]", "metrics = []"), p)))
        self.assertIn("C009", rules(self.lint(o, c + 'otelcol.exporter.otlp "extra" {\n  client { endpoint = "x" }\n}\n', p)))

    def test_c010_dead_branch_warns(self):
        src = fixture("otlp_grafana_cloud")
        c = render.render(src, plan_for(src))["config"]
        c = c.replace(", otelcol.processor.transform.cardinal_onboard.input", "")
        self.assertIn("C010", rules(self.lint(src, c, plan_for(src)), "warn"))

    def test_c011_modules_warn(self):
        o, c, p = self.rendered()
        c = 'import.file "mods" {\n  filename = "/etc/alloy/mods"\n}\n' + c
        self.assertIn("C011", rules(self.lint(o, c, p), "warn"))


class SaasLintTest(unittest.TestCase):
    def rendered(self, name="otlp_grafana_cloud", **over):
        src = fixture(name)
        p = saas_plan_for(src, **over)
        return src, render.render(src, p)["config"], p

    def lint(self, original, patched, plan):
        return lint_config.lint(ac.graph(ac.parse(patched)), ac.graph(ac.parse(original)), plan)

    def test_c003_c013_apply_to_otlphttp(self):
        o, c, p = self.rendered()
        bypass = c.replace("traces  = [otelcol.processor.batch.cardinal_onboard.input]",
                           "traces  = [otelcol.exporter.otlphttp.cardinal_onboard.input]")
        self.assertNotEqual(bypass, c)
        self.assertIn("C003", rules(self.lint(o, bypass, p)))
        self.assertIn("C013", rules(self.lint(o, c.replace("block_on_overflow = false", "block_on_overflow = true"), p)))

    def test_c014_literal_key(self):
        o, c, p = self.rendered()
        leaked = c.replace('sys.env("CARDINAL_API_KEY")', '"ck_live_abcdef"')
        self.assertNotEqual(leaked, c)
        found = self.lint(o, leaked, p)
        self.assertIn("C014", rules(found))
        self.assertFalse(any("ck_live_abcdef" in str(f) for f in found))
        no_header = c.replace('"x-cardinalhq-api-key"', '"authorization"')
        self.assertIn("C014", rules(self.lint(o, no_header, p)))
        other_var = c.replace('sys.env("CARDINAL_API_KEY")', 'sys.env("OTHER_KEY")')
        self.assertIn("C014", rules(self.lint(o, other_var, p)))

    def test_c014_endpoint(self):
        o, c, p = self.rendered(values="literal")
        self.assertIn("C014", rules(self.lint(o, c.replace(INGEST, "https://evil.example.com"), p)))

    def test_c015_target_mismatch(self):
        o, c, p = self.rendered()
        self.assertIn("C015", rules(self.lint(o, c, {**p, "target": "s3"})))
        src = fixture("otlp_grafana_cloud")
        s3 = render.render(src, plan_for(src))["config"]
        self.assertIn("C015", rules(self.lint(src, s3, p)))

    def test_customer_otlphttp_left_alone(self):
        # The fixture's own Grafana Cloud exporter is otlphttp without the Cardinal header.
        o, c, p = self.rendered()
        self.assertIn('otelcol.exporter.otlphttp "grafana_cloud"', c)
        self.assertEqual(rules(self.lint(o, c, p)), [])


if __name__ == "__main__":
    unittest.main()
