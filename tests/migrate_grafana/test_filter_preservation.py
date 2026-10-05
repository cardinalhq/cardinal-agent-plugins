"""migrate-from-grafana: a migrated query must never be wider than the Grafana one it
came from without the user choosing that.

Run: python3 -m unittest discover -s tests/migrate_grafana -v

Each case is a way the skill used to drop or widen a filter silently: a rare value
(no 500s in the last hour) or a failed label lookup removing the filter, a LogQL
parsed field treated as a missing label, one missing value removing every filter on
that label, an emptied log selector turned into "every service", ad-hoc filters and a
variable's narrower "All" lost, and widened alert rules passing validation.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "common", "migrate-from-grafana", "scripts"))

import cardinal_catalog as cc  # noqa: E402
import cardinal_verify as cv  # noqa: E402
import convert  # noqa: E402
import grafana_compare as gc  # noqa: E402


def tr(**mapping):
    return convert.Translator(mapping)


def decide(metrics=(), logs=(), metric_labels=(), log_labels=(), values=None):
    """decide_label_drops over real expressions, as cardinal_catalog.main feeds it."""
    by = {"metrics": list(metrics), "logs": [cc.stored_label_filters(e) for e in logs]}
    matchers = {s: set().union(*map(cc.matchers_in, ex)) if ex else set() for s, ex in by.items()}
    used = {s: set().union(*(cc.labels_in(e, grouping=False) for e in ex)) if ex else set() for s, ex in by.items()}
    ml = set(metric_labels) if metric_labels is not None else None
    ll = set(log_labels) if log_labels is not None else None
    return cc.decide_label_drops(matchers, used, ml, ll, lambda s, l: (values or {}).get((s, l)))


class CatalogProposesOnlyTest(unittest.TestCase):
    def test_rare_value_is_kept_and_only_proposed_for_that_value(self):
        props, review = decide(['sum(rate(http_requests_total{job="api", status="500"}[5m]))'],
                               metric_labels={"job", "status"}, values={("metrics", "status"): {"200", "404"}})
        self.assertEqual(props["metrics"], {"status=500"})  # never "status": that would drop every status filter
        self.assertIn("filter is kept", review[0]["issue"])

    def test_failed_label_lookup_checks_nothing(self):
        # tags() failing used to look like "no labels", removing every filter in the org.
        props, review = decide(['rate(x{job="api", env="prod"}[5m])'], ['{app="api"} |= "err"'],
                               metric_labels=None, log_labels=None)
        self.assertEqual(props, {"metrics": set(), "logs": set()})
        self.assertEqual({r["signal"] for r in review}, {"metrics", "logs"})
        self.assertTrue(all("weren't checked" in r["issue"] or "was checked" in r["issue"] for r in review))

    def test_tags_failure_is_none_not_empty(self):
        class C:
            def req(self, *a, **k):
                return 500, "boom"
        self.assertIsNone(cc.NativeQuery(C(), "i").tags("logs"))

    def test_parsed_log_fields_are_not_missing_labels(self):
        e = 'sum(count_over_time({app="api"} | json | status_code="500" [5m]))'
        self.assertNotIn("status_code", cc.stored_label_filters(e))
        props, _ = decide(logs=[e], log_labels={"app"})
        self.assertEqual(props["logs"], set())

    def test_stream_filter_before_the_parser_is_still_checked(self):
        props, _ = decide(logs=['{app="api"} | env="prod" | json | code="500"'], log_labels={"app"})
        self.assertEqual(props["logs"], {"env"})

    def test_missing_label_is_proposed_not_applied(self):
        props, review = decide(['rate(x{cluster="eu"}[5m])'], metric_labels={"job"})
        self.assertEqual(props["metrics"], {"cluster"})
        self.assertEqual(review[0]["suggested_drop"], "cluster")

    def test_grouping_label_is_not_a_filter(self):
        props, _ = decide(['sum by (pod) (rate(x{job="a"}[5m]))'], metric_labels={"job"})
        self.assertEqual(props["metrics"], set())

    def test_classifiers_agree(self):
        for e in ['{job="x"}', 'rate({app="a"} |= "e" [5m])', 'sum(rate(x{a="b"}[5m]))', '{app="a"} | json']:
            self.assertEqual(cc.is_log_query(e), convert.guess_kind_from_query(e) == "loki", e)


class DropScopeTest(unittest.TestCase):
    def test_label_drop_keeps_negative_filters(self):
        t = tr(drop_labels=["env"])
        self.assertEqual(t.promql('rate(x{env="prod"}[5m])')[0], "rate(x[5m])")
        self.assertEqual(t.promql('rate(x{env!="dev"}[5m])')[0], 'rate(x{env!="dev"}[5m])')
        self.assertEqual(t.promql('rate(x{env!~"dev|qa"}[5m])')[0], 'rate(x{env!~"dev|qa"}[5m])')

    def test_value_drop_only_touches_that_value(self):
        t = tr(drop_labels=["env=qa"])
        self.assertEqual(t.promql('rate(x{env="qa"}[5m])')[0], "rate(x[5m])")
        self.assertEqual(t.promql('rate(x{env="prod"}[5m])')[0], 'rate(x{env="prod"}[5m])')
        self.assertEqual(t.promql('rate(x{env=~"qa"}[5m])')[0], 'rate(x{env=~"qa"}[5m])')

    def test_note_marks_the_wider_query(self):
        _, notes = tr(drop_labels=["env"]).promql('rate(x{env="prod"}[5m])')
        self.assertTrue(any(convert.FILTER_REMOVED in n for n in notes))

    def test_brace_inside_a_regex_value(self):
        out, _ = tr(drop_labels=["env"]).promql('rate(x{env="prod", code=~"5[0-9]{2}"}[5m])')
        self.assertEqual(out, 'rate(x{code=~"5[0-9]{2}"}[5m])')

    def test_bare_selector_emptied_is_skipped(self):
        out, notes = tr(drop_labels=["job"]).promql('{job="api"}')
        self.assertIsNone(out)
        self.assertIn(convert.EMPTIED, notes)

    def test_parsed_log_field_is_never_dropped(self):
        t = tr(drop_log_labels=["status_code"])
        e = 'sum(count_over_time({app="api"} | json | status_code="500" [5m]))'
        self.assertEqual(t.logql(e)[0], e)

    def test_chained_log_filter_is_not_broken(self):
        e = '{app="api"} | env="prod" and method="GET"'
        out, _ = tr(drop_log_labels=["env"]).logql(e)
        self.assertEqual(out, e)  # removing half of a chain would leave invalid LogQL

    def test_single_log_stage_is_dropped(self):
        out, _ = tr(drop_log_labels=["env"]).logql('{app="api"} | env="prod" |= "error"')
        self.assertEqual(out, '{app="api"}  |= "error"')


class RenameInsideValuesTest(unittest.TestCase):
    def test_promql_value_untouched(self):
        out, _ = tr(labels={"service": "service_name"}).promql(
            'rate(x{service="a", route=~".*service=checkout.*"}[5m])')
        self.assertEqual(out, 'rate(x{service_name="a", route=~".*service=checkout.*"}[5m])')

    def test_logql_line_filter_untouched(self):
        out, _ = tr(labels={"service": "service_name"}).logql('{app="a"} |= "service=checkout" | service="c"')
        self.assertEqual(out, '{app="a"} |= "service=checkout" | service_name="c"')


def dash(variables, panels=None):
    return {"uid": "d", "title": "D", "templating": {"list": variables},
            "panels": panels or [{"id": 1, "type": "timeseries", "title": "T", "datasource": {"type": "prometheus"},
                                  "targets": [{"refId": "A", "expr": 'sum(rate(http_requests_total{ns=~"$ns"}[5m]))'}]}]}


def convert_dash(d):
    out, rep = convert.convert_dashboard(d, tr(), {})
    p = out["spec"]["panels"].get("p1")
    return (p["queries"][0]["query"] if p and p.get("queries") else None), rep


class AdhocFilterTest(unittest.TestCase):
    ADHOC = {"name": "Filters", "type": "adhoc", "datasource": {"type": "prometheus"},
             "filters": [{"key": "cluster", "operator": "=", "value": "prod-eu"}]}

    def test_added_to_selectors_and_bare_metrics(self):
        q, rep = convert_dash(dash([self.ADHOC], [{
            "id": 1, "type": "timeseries", "title": "T", "datasource": {"type": "prometheus"},
            "targets": [{"refId": "A", "expr": 'sum(rate(a{job="x"}[5m])) / sum(rate(b[5m]))'}]}]))
        self.assertEqual(q, 'sum(rate(a{job="x", cluster="prod-eu"}[5m])) / sum(rate(b{cluster="prod-eu"}[5m]))')
        self.assertTrue(any("ad-hoc" in n for n in rep["variables"]))

    def test_not_injected_into_strings_or_keywords(self):
        q, _ = convert_dash(dash([self.ADHOC], [{
            "id": 1, "type": "timeseries", "title": "T", "datasource": {"type": "prometheus"},
            "targets": [{"refId": "A", "expr": 'label_replace(sum by (pod) (up), "dst label", "$1", "pod", "(.*)")'}]}]))
        self.assertEqual(q, 'label_replace(sum by(pod) (up{cluster="prod-eu"}), "dst label", "$1", "pod", "(.*)")')

    def test_logs(self):
        a = dict(self.ADHOC, datasource={"type": "loki"})
        q, _ = convert_dash(dash([a], [{
            "id": 1, "type": "timeseries", "title": "T", "datasource": {"type": "loki"},
            "targets": [{"refId": "A", "expr": 'sum(count_over_time({app="a"} | line_format "{{.x}}" [5m]))'}]}]))
        self.assertEqual(q, 'sum(count_over_time({app="a", cluster="prod-eu"} | line_format "{{.x}}" [5m]))')

    def test_unsupported_operator_skips_rather_than_widens(self):
        a = dict(self.ADHOC, filters=[{"key": "latency", "operator": ">", "value": "5"}])
        q, rep = convert_dash(dash([a]))
        self.assertIsNone(q)
        self.assertEqual(rep["panels"][0]["status"], "skipped")


class VariableAllTest(unittest.TestCase):
    def var(self, **over):
        v = {"name": "ns", "type": "query", "query": "label_values(kube_pod_info, ns)", "includeAll": True,
             "current": {"value": "$__all"}, "datasource": {"type": "prometheus"}}
        v.update(over)
        return v

    def test_all_value_kept(self):
        q, _ = convert_dash(dash([self.var(allValue="team-a-.*")]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"$ns", ns=~"team-a-.*"}[5m]))')

    def test_variable_regex_kept(self):
        q, _ = convert_dash(dash([self.var(regex="/^team-a-\\d+/")]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"$ns", ns=~".*(?:^team-a-\\\\d+).*"}[5m]))')

    def test_capture_group_regex_is_reported(self):
        q, rep = convert_dash(dash([self.var(regex="/team-(.*)/")]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"$ns"}[5m]))')
        self.assertTrue(any("capture group" in n for n in rep["variables"]))

    def test_unconstrained_all_unchanged(self):
        q, _ = convert_dash(dash([self.var()]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"$ns"}[5m]))')

    def test_custom_all_is_its_options(self):
        v = {"name": "ns", "type": "custom", "includeAll": True, "current": {"value": ["$__all"]},
             "options": [{"value": "$__all"}, {"value": "a.prod"}, {"value": "b"}]}
        q, _ = convert_dash(dash([v]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"a\\.prod|b"}[5m]))')

    def test_custom_without_selection_uses_first_option(self):
        v = {"name": "ns", "type": "custom", "query": "a,b", "current": {}, "options": [{"value": "a"}, {"value": "b"}]}
        q, _ = convert_dash(dash([v]))
        self.assertEqual(q, 'sum(rate(http_requests_total{ns=~"a"}[5m]))')


class PanelDisplayFilterTest(unittest.TestCase):
    def test_transformations_and_repeat_reported(self):
        p = {"id": 1, "type": "timeseries", "title": "T", "repeat": "svc", "datasource": {"type": "prometheus"},
             "targets": [{"refId": "A", "expr": "sum(rate(x[5m]))"}],
             "transformations": [{"id": "filterByValue", "options": {}}, {"id": "renameByRegex", "disabled": True}]}
        _, rep = convert_dash(dash([], [p]))
        notes = " ".join(rep["panels"][0]["notes"])
        self.assertIn("'filterByValue' is not carried over: the panel can show", notes)
        self.assertNotIn("renameByRegex", notes)
        self.assertIn("repeated per $svc", notes)


def rule(expr):
    return {"grafana_alert": {"title": "5xx", "uid": "r1", "condition": "C", "data": [
        {"refId": "A", "datasourceUid": "prom", "model": {"expr": expr}},
        {"refId": "C", "datasourceUid": "__expr__", "model": {
            "type": "threshold", "expression": "A",
            "conditions": [{"evaluator": {"type": "gt", "params": [1]}}]}}]}}


GROUP = {"folder": "f", "group": "g", "interval": "1m"}
DS = {"prom": {"type": "prometheus", "name": "Prom"}}


class AlertTest(unittest.TestCase):
    def test_rule_losing_a_filter_is_skipped(self):
        out, rep = convert.convert_rule(rule('sum(rate(x{status="500"}[5m]))'), GROUP, tr(drop_labels=["status"]), DS)
        self.assertIsNone(out)
        self.assertEqual(rep["status"], "skipped")

    def test_rule_keeps_the_grafana_query_for_validation(self):
        out, _ = convert.convert_rule(rule('sum(rate(x{status="500"}[5m]))'), GROUP, tr(), DS)
        self.assertEqual(out["rule_spec"]["query"]["expr"], 'sum(rate(x{status="500"}[5m]))')
        self.assertEqual(out["source"]["grafana_expr"], 'sum(rate(x{status="500"}[5m]))')

    def test_widened_rule_warns(self):
        out, rep = convert.convert_rule(rule('sum by (status) (rate(x{status="500"}[5m]))'), GROUP, tr(), DS)
        k = lambda s: (("status", s),)  # noqa: E731

        class G:
            def resolve_ds(self, *a):
                return "prom"

            def series(self, *a):
                return {k("500"): {0: 2.0, 60: 2.0}}, None

        class C:
            def series(self, *a):
                return {k("500"): {0: 2.0, 60: 2.0}, k("200"): {0: 90.0, 60: 90.0}}, None
        row = gc.compare_alert(out, rep, G(), C(), {})
        self.assertEqual(row["verdict"], "extra")
        rows = [{"panel": "rule query", "query": out["rule_spec"]["query"]["expr"], "points": 2, "error": None}]
        self.assertEqual(cv.verdict(True, rows, [row]), "WARN")  # data alone would PASS

    def test_older_plan_is_not_compared(self):
        self.assertIsNone(gc.compare_alert({"rule_spec": {"signal_type": "metrics", "query": {"expr": "x"}},
                                            "source": {}}, None, None, None, {}))


if __name__ == "__main__":
    unittest.main()
