"""migrate-from-grafana: histogram translation, per-signal label drops, the catalog
probes, and the value comparison against Grafana.

Run: python3 -m unittest discover -s tests/migrate_grafana -v

The expected behaviour comes from a real migration (Grafana Cloud -> Cardinal SaaS,
OTLP delta histograms): rate(M) on a Cardinal histogram read seconds of request time
(Grafana's rate(_sum)), max by (...) over a histogram answered a merged-sketch value
(20 s for one service, -1 for zero-valued observations), and dropping an environment
label for metrics also removed it from log queries, letting another source's logs in.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "common", "migrate-from-grafana", "scripts"))

import cardinal_catalog as cc  # noqa: E402
import cardinal_verify as cv  # noqa: E402
import convert  # noqa: E402
import grafana_compare as gc  # noqa: E402

H = "http_server_request_duration_seconds"
MS = "griffin_cart_operation_duration_ms_milliseconds"
ENV = 'deployment_environment="alloy-demo"'


def mapping(**over):
    m = {
        "metrics": {H: "http_server_request_duration", MS: "griffin_cart_operation_duration_ms",
                    "go_goroutine_count": "go_goroutine_count"},
        "labels": {}, "log_labels": {"detected_level": "level"},
        "native_histograms": [H, MS],
        "histogram_quantile_ok": {H: True, MS: False},
        "histogram_rate_mode": "per_minute",
        "supports_or_vector": False,
        "supports_histogram_quantile": False,
        "drop_labels": ["deployment_environment"], "drop_log_labels": [],
    }
    m.update(over)
    return m


def tr(**over):
    return convert.Translator(mapping(**over))


class HistogramTranslationTest(unittest.TestCase):
    def q(self, expr, **over):
        out, notes = tr(**over).promql(expr)
        return out, " ".join(notes)

    def test_request_rate_per_minute_mode(self):
        out, notes = self.q(f'sum(rate({H}_count{{{ENV}, service_name=~"$service"}}[$__rate_interval]))')
        self.assertEqual(out, 'sum((histogram_count(rate(http_server_request_duration{service_name=~"$service"}[5m])) / 60))')
        self.assertIn(convert.RATE_PER_MINUTE, notes)

    def test_request_rate_per_second_mode(self):
        out, notes = self.q(f"sum by (service_name) (rate({H}_count[5m]))", histogram_rate_mode="per_second")
        self.assertEqual(out, "sum by(service_name) (histogram_count(rate(http_server_request_duration[5m])))")
        self.assertNotIn(convert.RATE_PER_MINUTE, notes)

    def test_request_rate_unknown_mode_says_check(self):
        out, notes = self.q(f"sum(rate({H}_count[5m]))", histogram_rate_mode="unknown")
        self.assertIn("histogram_count(rate(http_server_request_duration[5m]))", out)
        self.assertIn("check the values against Grafana", notes)

    def test_increase_per_minute(self):
        out, _ = self.q(f"sum(increase({H}_count[10m]))")
        self.assertEqual(out, "sum((histogram_count(rate(http_server_request_duration[10m])) / 60 * 600))")

    def test_error_ratio_both_sides(self):
        out, _ = self.q(f'sum(rate({H}_count{{http_response_status_code=~"5.."}}[5m])) / sum(rate({H}_count[5m]))')
        self.assertEqual(out.count("histogram_count(rate(http_server_request_duration"), 2)
        self.assertNotRegex(out, r"(?<!histogram_count\()rate\(http_server_request_duration[\[{]")

    def test_percentile_where_usable(self):
        out, notes = self.q(f"histogram_quantile(0.95, sum by (le, service_name) (rate({H}_bucket{{{ENV}}}[5m])))")
        self.assertEqual(out, "histogram_quantile(0.95, sum by(service_name) (rate(http_server_request_duration[5m])))")
        self.assertIn(convert.PCT_ESTIMATE, notes)
        self.assertNotIn(convert.PCT_AS_AVG, notes)

    def test_percentile_falls_back_to_average(self):
        out, notes = self.q(f"histogram_quantile(0.99, sum by (le, operation) (rate({MS}_bucket[5m])))")
        self.assertEqual(out, "histogram_avg(sum by(operation) (rate(griffin_cart_operation_duration_ms[5m])))")
        self.assertIn(convert.PCT_AS_AVG, notes)

    def test_older_mapping_without_probe_uses_average(self):
        out, notes = self.q(f"histogram_quantile(0.95, sum by (le) (rate({H}_bucket[5m])))", histogram_quantile_ok={})
        self.assertTrue(out.startswith("histogram_avg("), out)

    def test_average_uses_histogram_avg(self):
        out, notes = self.q(f"sum by (service_name, http_request_method) (rate({H}_sum[5m])) / "
                            f"sum by (service_name, http_request_method) (rate({H}_count[5m]))")
        self.assertEqual(out, "histogram_avg(sum by(service_name, http_request_method) "
                              "(rate(http_server_request_duration[5m])))")
        self.assertIn("histogram_avg", notes)

    def test_rate_of_sum(self):
        out, notes = self.q(f"sum(rate({H}_sum[5m]))")
        self.assertEqual(out, "sum(rate(http_server_request_duration[5m]))")
        self.assertIn("sum of observed values", notes)

    def test_counting_series(self):
        out, _ = self.q(f"count(count by (service_name) ({H}_count))")
        self.assertEqual(out, "count(count by(service_name) (http_server_request_duration))")

    def test_heatmap_buckets_skipped(self):
        out, notes = self.q(f"sum by (le) (rate({H}_bucket[5m]))")
        self.assertIsNone(out)
        self.assertIn("_bucket", notes)

    def test_never_max_over_a_histogram(self):
        exprs = [f"histogram_quantile(0.5, sum by (le) (rate({H}_bucket[5m])))",
                 f"histogram_quantile(0.95, sum by (le, operation) (rate({MS}_bucket[5m])))",
                 f"sum(rate({H}_sum[5m])) / sum(rate({H}_count[5m]))"]
        for e in exprs:
            out, _ = self.q(e)
            self.assertNotRegex(out, r"\b(max|min)\b", e)

    def test_or_vector_flagged_when_unsupported(self):
        e = f"sum(rate({H}_count[5m])) or vector(0)"
        _, notes = self.q(e)
        self.assertIn(convert.OR_VECTOR_NOOP, notes)
        _, notes = self.q(e, supports_or_vector=True)
        self.assertNotIn(convert.OR_VECTOR_NOOP, notes)


class PerSignalDropTest(unittest.TestCase):
    def test_metric_drop_leaves_logs_alone(self):
        t = tr()
        m, _ = t.promql(f"sum(go_goroutine_count{{{ENV}}})")
        self.assertEqual(m, "sum(go_goroutine_count)")
        l, notes = t.logql(f"sum by (service_name) (count_over_time({{{ENV}}}[1m]))")
        self.assertIn(ENV, l)
        self.assertFalse(any("removed" in n for n in notes))

    def test_log_drop(self):
        l, notes = tr(drop_log_labels=["deployment_environment"]).logql(f'{{{ENV}, service_name="cart"}}')
        self.assertEqual(l, '{service_name="cart"}')
        self.assertTrue(any("Cardinal's logs" in n for n in notes))

    def test_log_drop_that_empties_the_selector_skips(self):
        # {service_name=~".+"} would read every service's logs in the org.
        l, notes = tr(drop_log_labels=["deployment_environment"]).logql(f"{{{ENV}}}")
        self.assertIsNone(l)
        self.assertIn(convert.EMPTIED, notes)

    def test_older_mapping_drops_both(self):
        m = mapping()
        del m["drop_log_labels"]
        l, _ = convert.Translator(m).logql(f'{{{ENV}, service_name="cart"}}')
        self.assertNotIn("deployment_environment", l)


def grafana_dash():
    t = lambda ref, e, legend=None: dict({"refId": ref, "expr": e, "datasource": {"uid": "prom"}},  # noqa: E731
                                         **({"legendFormat": legend} if legend else {}))
    return {"uid": "d1", "title": "Overview", "panels": [
        {"id": 11, "type": "stat", "title": "Requests / sec", "datasource": {"uid": "prom"},
         "targets": [t("A", f"sum(rate({H}_count{{{ENV}}}[5m]))")]},
        {"id": 12, "type": "timeseries", "title": "Cart op latency",
         "targets": [t("A", f"histogram_quantile(0.5, sum by (le) (rate({MS}_bucket[5m])))", "p50"),
                     t("B", f"histogram_quantile(0.95, sum by (le) (rate({MS}_bucket[5m])))", "p95"),
                     t("C", f"histogram_quantile(0.99, sum by (le) (rate({MS}_bucket[5m])))", "p99")]},
        {"id": 13, "type": "stat", "title": "p95 latency",
         "targets": [t("A", f"histogram_quantile(0.95, sum by (le) (rate({MS}_bucket[5m])))")]},
        {"id": 14, "type": "logs", "title": "Logs", "datasource": {"uid": "logs"},
         "targets": [{"refId": "Q", "expr": f"{{{ENV}}}", "datasource": {"uid": "logs"}}]},
    ]}


DATASOURCES = {"prom": {"type": "prometheus", "name": "Prom"}, "logs": {"type": "loki", "name": "Loki"}}


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.out, self.rep = convert.convert_dashboard(grafana_dash(), tr(), DATASOURCES)
        self.panels = self.out["spec"]["panels"]
        self.entries = {e["grafana_id"]: e for e in self.rep["panels"]}

    def test_average_panels_are_retitled_and_merged(self):
        p = self.panels[self.entries[12]["cardinal_id"]]
        self.assertEqual(p["title"], "Cart op latency (avg)")
        self.assertEqual(len(p["queries"]), 1)  # p50/p95/p99 became the same average
        self.assertEqual(self.entries[12]["query_refs"], ["A"])
        self.assertEqual(self.entries[12]["cardinal_title"], "Cart op latency (avg)")
        self.assertIn("queries that became identical were merged", self.entries[12]["notes"])
        self.assertEqual(self.panels[self.entries[13]["cardinal_id"]]["title"], "avg latency")

    def test_refs_recorded(self):
        self.assertEqual(self.entries[11]["query_refs"], ["A"])
        self.assertEqual(self.entries[14]["query_refs"], ["Q"])
        self.assertIn(ENV, self.panels[self.entries[14]["cardinal_id"]]["rawLogql"])

    def test_avg_title(self):
        self.assertEqual(convert.avg_title("p95 latency by service"), "avg latency by service")
        self.assertEqual(convert.avg_title("Latency (P99)"), "Latency (avg)")
        self.assertEqual(convert.avg_title("Lookup latency"), "Lookup latency (avg)")


class FakeNative:
    """NativeQuery stand-in: answers by expression (and step) from a table."""

    def __init__(self, table):
        self.table = table

    def values(self, signal, expr, step=60):
        v = self.table.get((expr, step), self.table.get(expr, []))
        return (v, None) if isinstance(v, list) else ([], v)


class CatalogProbeTest(unittest.TestCase):
    def test_quantile_usable(self):
        self.assertTrue(cc.quantile_usable([0.0025, 0.0025], [0.0049]))
        self.assertFalse(cc.quantile_usable([-1.0005], [-1.0005]))      # zero-valued observations
        self.assertFalse(cc.quantile_usable([0.5], [0.1]))              # p50 above p99
        self.assertFalse(cc.quantile_usable([], [0.1]))
        self.assertFalse(cc.quantile_usable([0.1], [0.2], "VALIDATION_FAILED"))

    def test_rate_mode(self):
        self.assertEqual(cc.rate_mode([91, 91], [185, 181]), "per_minute")   # doubles with the step
        self.assertEqual(cc.rate_mode([1.52], [1.50]), "per_second")
        self.assertEqual(cc.rate_mode([91], []), "unknown")
        self.assertEqual(cc.rate_mode([91], [500]), "unknown")

    def test_probe_histograms(self):
        q = lambda p, m: f"histogram_quantile({p}, sum(rate({m}[5m])))"  # noqa: E731
        cnt = "sum(histogram_count(rate(h[5m])))"
        native = FakeNative({q(0.5, "h"): [0.0025], q(0.99, "h"): [0.0049],
                             q(0.5, "ms"): [-1.0005], q(0.99, "ms"): [-1.0005],
                             (cnt, 60): [91.0], (cnt, 120): [185.0]})
        out = cc.probe_histograms(native, {"H": "h", "MS": "ms"})
        self.assertEqual(out["histogram_quantile_ok"], {"H": True, "MS": False})
        self.assertEqual(out["histogram_rate_mode"], "per_minute")

    def test_probe_or_vector(self):
        e = 'sum(rate(m{migrate_probe="none"}[5m])) or vector(0)'
        self.assertFalse(cc.probe_or_vector(FakeNative({e: []}), "m"))
        self.assertTrue(cc.probe_or_vector(FakeNative({e: [0.0]}), "m"))


class LabelDropDecisionTest(unittest.TestCase):
    def decide(self, matchers, used=None, metric_labels=(), log_labels=(), values=None, renames=None):
        return cc.decide_label_drops(matchers, used or {}, set(metric_labels), set(log_labels),
                                     lambda sig, label: (values or {}).get((sig, label)), renames)

    def test_label_on_logs_only(self):
        m = {"metrics": {("deployment_environment", "alloy-demo")}, "logs": {("deployment_environment", "alloy-demo")}}
        drops, review = self.decide(m, metric_labels={"service_name"}, log_labels={"deployment_environment"},
                                    values={("logs", "deployment_environment"): {"alloy-demo", "prod"}})
        self.assertEqual(drops, {"metrics": {"deployment_environment"}, "logs": set()})
        self.assertEqual(len(review), 1)
        self.assertTrue(review[0]["present_on_other_signal"])
        self.assertEqual(review[0]["signal"], "metrics")

    def test_value_missing_on_one_signal(self):
        m = {"logs": {("env", "staging")}}
        drops, review = self.decide(m, log_labels={"env"}, values={("logs", "env"): {"prod"}})
        self.assertEqual(drops["logs"], {"env=staging"})  # that one filter, not every env filter
        self.assertIn("values_in_cardinal", review[0])

    def test_renamed_label_is_not_dropped(self):
        drops, _ = self.decide({}, used={"metrics": {"service"}}, metric_labels={"service_name"},
                               renames={"service": "service_name"})
        self.assertEqual(drops["metrics"], set())

    def test_unknown_filter_label_proposed_for_its_signal(self):
        drops, _ = self.decide({}, used={"metrics": {"pod"}, "logs": {"pod"}}, log_labels={"pod"})
        self.assertEqual(drops, {"metrics": {"pod"}, "logs": set()})

    def test_exprs_by_signal(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "dashboards"))
            json.dump(grafana_dash(), open(os.path.join(d, "dashboards", "d1.json"), "w"))
            json.dump(DATASOURCES, open(os.path.join(d, "datasources.json"), "w"))
            by = cc.exprs_by_signal(d)
        self.assertEqual(by["logs"], [f"{{{ENV}}}"])
        self.assertEqual(len(by["metrics"]), 5)


def series(**by_key):
    """series(svc_a={0: 1.0, 60: 2.0}) -> {(("service_name", "svc_a"),): {...}}; key 'all' = no labels."""
    return {(() if k == "all" else (("service_name", k),)): v for k, v in by_key.items()}


class CompareTest(unittest.TestCase):
    pts = {0: 14.0, 60: 13.9, 120: 14.1}

    def test_match(self):
        r = gc.compare(series(all=self.pts), series(all={t: v * 1.05 for t, v in self.pts.items()}))
        self.assertEqual(r["verdict"], "match")

    def test_request_time_instead_of_requests(self):
        r = gc.compare(series(all=self.pts), series(all={t: 1.95 for t in self.pts}))
        self.assertEqual(r["verdict"], "differs")
        self.assertIn("×0.139", r["detail"])

    def test_missing_and_extra(self):
        g = series(cart=self.pts, catalog=self.pts)
        self.assertEqual(gc.compare(g, series(cart=self.pts))["verdict"], "missing")
        r = gc.compare(series(cart=self.pts), series(cart=self.pts, **{"claude-code": {0: 57.0}}))
        self.assertEqual(r["verdict"], "extra")
        self.assertIn("claude-code", r["detail"])

    def test_zero_and_empty(self):
        self.assertEqual(gc.compare(series(all={0: 0.0, 60: 0.0}), {})["verdict"], "zero")
        self.assertEqual(gc.compare({}, {})["verdict"], "empty")
        self.assertEqual(gc.compare(series(all=self.pts), {})["verdict"], "missing")

    def test_percentiles(self):
        g = series(all={0: 0.00475, 60: 0.00475})
        near = series(all={0: 0.0025, 60: 0.0025})
        far = series(all={0: 20.0, 60: 20.0})
        notes = [f"p95: {convert.PCT_ESTIMATE}"]
        self.assertEqual(gc.compare(g, near, notes=notes)["verdict"], "estimate")
        self.assertEqual(gc.compare(g, far, notes=notes)["verdict"], "differs")
        self.assertEqual(gc.compare(g, far, notes=[convert.PCT_AS_AVG])["verdict"], "skipped")

    def test_label_renames_and_case(self):
        g = {(("detected_level", "info"),): {0: 1303.0}, (("detected_level", "error"),): {0: 5.0}}
        c = {(("level", "INFO"),): {0: 1298.0}, (("level", "ERROR"),): {0: 6.0}}
        self.assertEqual(gc.compare(g, c, renames={"detected_level": "level"})["verdict"], "match")

    def test_bare_selector_extra_labels(self):
        g = {(("job", "cart"), ("service_name", "cart")): {0: 35.0}}
        c = {(("k8s_cluster_name", "local"), ("service_name", "cart")): {0: 35.0}}
        self.assertEqual(gc.compare(g, c)["verdict"], "match")

    def test_expressions(self):
        self.assertEqual(gc.grafana_expr('rate(x{svc=~"$service", a=~"${b}"}[$__rate_interval])'),
                         'rate(x{svc=~".+", a=~".+"}[5m])')
        self.assertEqual(gc.cardinal_expr('x{svc=~"$service"}'), 'x{svc=~".+"}')
        start, end = gc.window(now=10_000)
        self.assertEqual((end % 60, end - start), (0, 900))


class FakeGrafana:
    def __init__(self, table):
        self.table = table

    def resolve_ds(self, ref, kind):
        return "prom"

    def series(self, expr, ds, start, end):
        return self.table.get(expr, ({}, None))


class FakeCardinal:
    def __init__(self, table):
        self.table = table

    def series(self, signal, expr, step=60, rng=None):
        return self.table.get(expr, ({}, None))


class CompareDashboardTest(unittest.TestCase):
    def test_end_to_end(self):
        out, rep = convert.convert_dashboard(grafana_dash(), tr(), DATASOURCES)
        plan = {"name": out["name"], "spec": out["spec"]}
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "dashboards"))
            json.dump(grafana_dash(), open(os.path.join(d, "dashboards", "d1.json"), "w"))
            g_req = gc.grafana_expr(f"sum(rate({H}_count{{{ENV}}}[5m]))")
            c_req = plan["spec"]["panels"][next(e["cardinal_id"] for e in rep["panels"] if e["grafana_id"] == 11)]["queries"][0]["query"]
            gq = FakeGrafana({g_req: (series(all={0: 14.0, 60: 14.0}), None)})
            nq = FakeCardinal({gc.cardinal_expr(c_req): (series(all={0: 2.0, 60: 2.0}), None)})
            rows = gc.compare_dashboard("d1", plan, rep, d, gq, nq, mapping())
        by = {r["panel"]: r for r in rows}
        self.assertEqual(by["Requests / sec"]["verdict"], "differs")
        self.assertEqual(by["Cart op latency (avg)"]["verdict"], "skipped")
        self.assertEqual(by["Logs"]["verdict"], "skipped")


class VerifyVerdictTest(unittest.TestCase):
    def row(self, panel, query, points, error=None):
        return {"panel": panel, "query": query, "points": points, "error": error}

    def test_or_vector_empty_is_not_a_warning(self):
        rows = [self.row("5xx / sec", "sum(rate(x[5m])) or vector(0)", 0), self.row("rps", "sum(rate(x[5m]))", 10)]
        self.assertEqual(cv.verdict(True, rows), "PASS")

    def test_empty_in_grafana_too(self):
        rows = [self.row("Errors", 'sum(rate(x{code="5.."}[5m]))', 0), self.row("rps", "sum(rate(x[5m]))", 10)]
        self.assertEqual(cv.verdict(True, rows), "WARN")
        compared = [{"panel": "Errors", "query": 'sum(rate(x{code="5.."}[5m]))', "verdict": "empty"}]
        cv.agreed_empty(rows, compared)
        self.assertEqual(cv.verdict(True, rows, compared), "PASS")

    def test_data_that_disagrees_warns(self):
        rows = [self.row("rps", "sum(rate(x[5m]))", 10)]
        self.assertEqual(cv.verdict(True, rows), "PASS")
        self.assertEqual(cv.verdict(True, rows, [{"panel": "rps", "verdict": "differs"}]), "WARN")
        self.assertEqual(cv.verdict(True, rows, [{"panel": "rps", "verdict": "estimate"}]), "PASS")


if __name__ == "__main__":
    unittest.main()
