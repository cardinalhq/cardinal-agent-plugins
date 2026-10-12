"""Range execution: one-hour windows, halving on size rejection, and the
window-edge canonicalization rules (range_run.py docstring, rules 1-6)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import range_run
from range_run import (EDGE_REASON, align_whole, canonicalize, format_time, halve, is_size_rejection,
                       judge_usage, parse_extents, parse_time, split_windows)
from server import APIRejected, Behavior, tools_list
from range_runner import RangeRunner
from unittest.mock import patch

T0 = parse_time('2026-10-11T00:00:00Z')
A, B, C, D = 'a' * 32, 'b' * 32, 'c' * 32, 'd' * 32


def row(trace, verdict, execution='e', gaps=None, reason=None):
    return {'trace_id': trace, 'verdict': verdict, 'reason': reason or f'{verdict} reason',
            'witness_refs': ['span1'], 'coverage_gaps': gaps or [], 'execution_id': execution}


class WindowTests(unittest.TestCase):
    def test_split_covers_the_range_in_windows_of_at_most_an_hour(self):
        windows = split_windows(T0, T0 + 24 * 3600)
        self.assertEqual(len(windows), 24)
        self.assertTrue(all(e - s == 3600 for s, e in windows))
        self.assertEqual(split_windows(T0, T0 + 5400), [[T0, T0 + 3600], [T0 + 3600, T0 + 5400]])
        with self.assertRaises(ValueError):
            split_windows(T0, T0)

    def test_halving_stops_at_the_minimum_window(self):
        self.assertEqual(halve([T0, T0 + 3600]), [[T0, T0 + 1800], [T0 + 1800, T0 + 3600]])
        self.assertIsNone(halve([T0, T0 + 100]))

    def test_size_rejection_matches_the_lakerunner_messages_only(self):
        self.assertTrue(is_size_rejection('Cardinal API rejected the request (HTTP 400): behavior population '
                                          'exceeds 256 MiB; narrow the time window'))
        self.assertTrue(is_size_rejection('behavior population exceeds 256 selected trace objects; narrow the time window'))
        self.assertFalse(is_size_rejection('accepted version is bound to service megan, not traefik'))

    def test_time_parsing_and_formatting(self):
        self.assertEqual(parse_time('2026-10-11T00:00:00+00:00'), T0)
        self.assertEqual(parse_time('2026-10-11T02:00:00+02:00'), T0)
        self.assertAlmostEqual(parse_time('2026-10-11T00:00:00.25Z'), T0 + 0.25)
        self.assertEqual(format_time(T0), '2026-10-11T00:00:00Z')
        self.assertEqual(format_time(T0 + 0.5), '2026-10-11T00:00:00.5Z')
        # The API accepts at most milliseconds.
        self.assertEqual(format_time(T0 + 0.9652481), '2026-10-11T00:00:00.965Z')
        self.assertEqual(format_time(T0 + 59.9999), '2026-10-11T00:01:00Z')
        with self.assertRaises(ValueError):
            parse_time('2026-10-11 00:00')

    def test_extents_are_validated(self):
        self.assertEqual(parse_extents({A: {'start': '2026-10-11T00:00:00Z', 'end': '2026-10-11T00:01:00Z'}}),
                         {A: [T0, T0 + 60]})
        for bad in ({'nothex': {'start': '2026-10-11T00:00:00Z', 'end': '2026-10-11T00:01:00Z'}},
                    {A: {'start': '2026-10-11T00:02:00Z', 'end': '2026-10-11T00:01:00Z'}}, [A]):
            with self.assertRaises(ValueError):
                parse_extents(bad)

    def test_aligned_windows_are_whole_seconds_around_nanosecond_extents(self):
        extent = parse_extents({A: {'start': '2026-10-11T21:47:25.166000Z', 'end': '2026-10-11T21:47:34.965248Z'}})
        groups, _ = align_whole(extent)
        lo, hi = groups[0]['window']
        self.assertEqual((format_time(lo), format_time(hi)), ('2026-10-11T21:47:24Z', '2026-10-11T21:47:36Z'))

    def test_halving_keeps_whole_second_boundaries(self):
        first, second = halve([T0, T0 + 2100])
        self.assertEqual(first[1], T0 + 1050)
        self.assertEqual(halve(first)[0][1], T0 + 525)
        self.assertEqual(halve([T0, T0 + 525])[0][1] % 1, 0)

    def test_judge_use_is_read_from_the_source(self):
        judged = ('def evaluate(run, recorder, judge):\n'
                  '    d = judge.decide(proposition="Is the claim supported?", evidence=[])\n')
        rules = 'def evaluate(run, recorder, judge):\n    recorder.unknown("no events")\n'
        self.assertTrue(range_run.uses_judge(judged))
        self.assertFalse(range_run.uses_judge(rules))
        self.assertTrue(range_run.uses_judge('def broken(:'))

    def test_align_groups_extents_into_windows_of_at_most_an_hour(self):
        groups, too_long = align_whole({A: [T0 + 10, T0 + 70], B: [T0 + 100, T0 + 200],
                                        C: [T0 + 5000, T0 + 5100], D: [T0, T0 + 7200]})
        self.assertEqual(too_long, [D])
        self.assertEqual([g['trace_ids'] for g in groups], [[A, B], [C]])
        for group in groups:
            self.assertLessEqual(group['window'][1] - group['window'][0], 3600)
        self.assertLessEqual(groups[0]['window'][0], T0 + 10)
        self.assertGreaterEqual(groups[0]['window'][1], T0 + 200)


class CanonicalizeTests(unittest.TestCase):
    windows = [[T0, T0 + 3600], [T0 + 3600, T0 + 7200]]

    def test_a_trace_inside_one_window_keeps_its_verdict(self):
        out = canonicalize(self.windows, [[row(A, 'MATCH', 'w0')], [row(B, 'NON_MATCH', 'w1')]], {}, [])
        self.assertEqual(out['counts']['MATCH'], 1)
        self.assertEqual(out['counts']['NON_MATCH'], 1)
        self.assertEqual(out['edge_traces'], [])
        self.assertEqual(out['conflicts'], [])

    def test_an_edge_trace_is_re_evaluated_whole_and_the_whole_verdict_is_canonical(self):
        whole = [{'window': [T0 + 3500, T0 + 3700], 'trace_ids': [A], 'status': 'COMPLETED',
                  'results': [row(A, 'MATCH', 'whole')]}]
        out = canonicalize(self.windows, [[row(A, 'UNKNOWN', 'w0')], [row(A, 'NON_MATCH', 'w1')]],
                           {A: [T0 + 3550, T0 + 3650]}, whole)
        trace = out['traces'][0]
        self.assertTrue(trace['edge'])
        self.assertEqual(trace['canonical']['verdict'], 'MATCH')
        self.assertEqual(trace['canonical']['source'], 'whole')
        self.assertEqual(trace['canonical']['execution_id'], 'whole')
        # Rule 6: both raw per-window verdicts stay; canonical only adds a column.
        self.assertEqual([w['verdict'] for w in trace['windows']], ['UNKNOWN', 'NON_MATCH'])
        self.assertEqual(out['raw_window_counts']['UNKNOWN'], 1)
        self.assertEqual(out['raw_window_counts']['NON_MATCH'], 1)
        # Rule 5: the disagreement is reported with its per-window verdicts.
        self.assertEqual(out['conflicts'], [{'trace_id': A, 'canonical': 'MATCH', 'whole': 'MATCH', 'windows': [
            {'window': ['2026-10-11T00:00:00Z', '2026-10-11T01:00:00Z'], 'verdict': 'UNKNOWN'},
            {'window': ['2026-10-11T01:00:00Z', '2026-10-11T02:00:00Z'], 'verdict': 'NON_MATCH'}]}])

    def test_an_edge_trace_that_cannot_be_evaluated_whole_is_unknown(self):
        out = canonicalize(self.windows, [[row(A, 'MATCH')], [row(A, 'MATCH')]], {A: [T0 + 10, T0 + 7000]}, [])
        canonical = out['traces'][0]['canonical']
        self.assertEqual(canonical['verdict'], 'UNKNOWN')
        self.assertTrue(canonical['reason'].startswith(EDGE_REASON))
        self.assertIn('more than one hour', canonical['reason'])
        # Agreeing partial MATCHes still conflict with the UNKNOWN canonical verdict.
        self.assertEqual(len(out['conflicts']), 1)

    def test_a_partial_unknown_is_never_upgraded_by_another_partial_window(self):
        out = canonicalize(self.windows, [[row(A, 'UNKNOWN')], [row(A, 'MATCH')]], {}, [])
        trace = out['traces'][0]
        self.assertEqual(trace['canonical']['verdict'], 'UNKNOWN')
        self.assertEqual(trace['canonical']['source'], 'edge-unresolved')
        self.assertIn('no span extent', trace['canonical']['reason'])
        self.assertEqual(out['counts']['MATCH'], 0)
        self.assertEqual(out['conflicts'][0]['windows'][1]['verdict'], 'MATCH')

    def test_a_whole_evaluation_that_still_reports_missing_evidence_is_unknown(self):
        whole = [{'window': [T0 + 3500, T0 + 3700], 'trace_ids': [A], 'status': 'COMPLETED',
                  'results': [row(A, 'NON_MATCH', 'whole', gaps=[{'ref': 'run', 'reason': 'manifest missing'}])]}]
        out = canonicalize(self.windows, [[row(A, 'NON_MATCH')], [row(A, 'NON_MATCH')]], {A: [T0 + 3550, T0 + 3650]}, whole)
        canonical = out['traces'][0]['canonical']
        self.assertEqual(canonical['verdict'], 'UNKNOWN')
        self.assertIn('manifest missing', canonical['reason'])
        self.assertEqual(out['traces'][0]['whole']['verdict'], 'NON_MATCH')

    def test_a_single_window_trace_whose_spans_cross_the_edge_is_an_edge_trace(self):
        whole_absent = [{'window': [T0 + 3000, T0 + 4000], 'trace_ids': [A], 'status': 'COMPLETED', 'results': []}]
        out = canonicalize(self.windows, [[row(A, 'NON_MATCH')], []], {A: [T0 + 3300, T0 + 3900]}, whole_absent)
        trace = out['traces'][0]
        self.assertEqual(trace['edge_why'], ['span extent crosses a window edge'])
        self.assertEqual(trace['canonical']['verdict'], 'UNKNOWN')
        self.assertIn('did not select it', trace['canonical']['reason'])

    def test_unknown_reasons_are_grouped(self):
        out = canonicalize(self.windows, [[row(A, 'UNKNOWN', reason='manifest missing'),
                                           row(B, 'UNKNOWN', reason='manifest missing'),
                                           row(C, 'UNKNOWN', reason='judge uncertain')], []], {}, [])
        self.assertEqual(range_run.unknown_reasons(out['traces']),
                         [{'reason': 'manifest missing', 'count': 2}, {'reason': 'judge uncertain', 'count': 1}])


class JudgeUsageTests(unittest.TestCase):
    def test_usage_is_summed_from_receipts_and_cost_is_never_estimated(self):
        raw = [{'jev_receipts': [{'usage': {'input_tokens': 900, 'output_tokens': 40}, 'cost_usd': None,
                                  'attempts': [{}], 'config': {'model': 'jev-1.13.0'}},
                                 {'usage': {'input_tokens': 100, 'output_tokens': 10}, 'cost_usd': 0.01,
                                  'attempts': [{}, {}]}]},
               {'jev_receipts': []}]
        usage = judge_usage(raw)
        self.assertEqual((usage['judge_calls'], usage['attempts'], usage['input_tokens'], usage['output_tokens']),
                         (2, 3, 1000, 50))
        self.assertIsNone(usage['cost_usd'])
        self.assertEqual(usage['cost'], 'not available')
        self.assertEqual(usage['models'], {'jev-1.13.0': 1})

    def test_rules_only_runs_report_zero_judge_calls(self):
        usage = judge_usage([{'jev_receipts': []}, {}])
        self.assertEqual(usage['judge_calls'], 0)
        self.assertEqual(usage['cost'], '0 USD')


class FakeAPI:
    """Stands in for Behavior.start/poll/read: traces by time, size limit by window length."""

    def __init__(self, traces, max_window=3600, reject=None):
        self.traces = traces          # [(trace_id, span_start, span_end, verdict_fn(window) -> row fields)]
        self.max_window = max_window
        self.reject = reject
        self.submitted = []
        self.executions = {}

    def start(self, version, population, start, end):
        lo, hi = parse_time(start), parse_time(end)
        if self.reject:
            raise APIRejected(self.reject)
        if hi - lo > self.max_window:
            raise APIRejected('Cardinal API rejected the request (HTTP 400): behavior population exceeds 256 MiB; '
                              'narrow the time window')
        execution = format(len(self.submitted) + 1, '032x')
        self.submitted.append((start, end))
        results = []
        for trace_id, s, e, verdict in self.traces:
            if s < hi and e >= lo:
                whole = s >= lo and e <= hi
                results.append({'trace_id': trace_id, 'verdict': verdict(whole), 'witness_refs': ['w'],
                                'records': [], 'coverage_gaps': [] if whole else [{'ref': 'run', 'reason': 'parent span missing'}],
                                'jev_receipts': [{'usage': {'input_tokens': 10, 'output_tokens': 1}, 'cost_usd': None}]})
        self.executions[execution] = results
        return {'execution_id': execution}

    def poll(self, execution_id, wait_seconds=20):
        return {'execution_status': 'COMPLETED', 'receipt': execution_id}

    def read(self, execution_id):
        return {'results': self.executions[execution_id]}


class RangeOrchestrationTests(unittest.TestCase):
    """The background worker, driven synchronously (spawn is stubbed)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.behavior = Behavior({'output_dir': self.temp.name, 'base_url': 'https://query.example'})
        self.version = 'a' * 64
        path = self.behavior.program_path(self.version)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'diagnostic_version': self.version, 'acceptance_id': 'acc-1',
                                    'source': 'def evaluate(run, recorder, judge):\n    pass\n',
                                    'behavior_contract': {'clauses': [{'judge_site': ''}]}}))
        self.behavior.context = lambda version: None
        self.spawned = []
        patcher = patch.object(RangeRunner, 'spawn', lambda runner, config_path=None: self.spawned.append(runner.range_id) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, api):
        self.behavior.start, self.behavior.poll, self.behavior.read = api.start, api.poll, api.read

    def drive(self, range_id):
        RangeRunner(self.behavior, range_id).run(sleep=lambda s: None)
        return self.events(range_id)

    def events(self, range_id):
        path = Path(self.temp.name) / 'ranges' / range_id / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def summary(self, range_id):
        return json.loads((Path(self.temp.name) / 'ranges' / range_id / 'summary.json').read_text())

    def test_start_returns_at_once_with_files_to_listen_to(self):
        self.use(FakeAPI([]))
        view = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z', '2026-10-11T02:00:00Z')
        self.assertEqual(self.spawned, [view['range_id']])
        self.assertEqual(view['phase'], 'windows')
        self.assertTrue(view['events_file'].endswith('events.jsonl'))
        self.assertEqual(view['watch_command'], f"cardinal-checks range watch {view['range_id']}")
        self.assertEqual(self.events(view['range_id'])[0]['event'], 'started')

    def test_a_range_runs_in_windows_halves_on_size_and_resolves_edges_whole(self):
        # A crosses the 01:00 boundary; B lives inside the first window; C needs two hours.
        traces = [(A, T0 + 3500, T0 + 3700, lambda whole: 'MATCH' if whole else 'UNKNOWN'),
                  (B, T0 + 100, T0 + 200, lambda whole: 'NON_MATCH'),
                  (C, T0 + 1000, T0 + 6000, lambda whole: 'MATCH')]
        api = FakeAPI(traces, max_window=1800)
        self.use(api)
        range_id = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z',
                                             '2026-10-11T02:00:00Z')['range_id']
        events = self.drive(range_id)
        kinds = [e['event'] for e in events]
        self.assertEqual(kinds.count('window_split'), 2)
        self.assertEqual(kinds.count('window_done'), 4)
        self.assertEqual(kinds[-1], 'needs_extents')
        self.assertEqual(sorted(events[-1]['multi_window_traces']), [A, C])
        self.assertIn('2026-10-11T01:00:00Z', events[-1]['boundaries'])
        extents = {A: {'start': format_time(T0 + 3500), 'end': format_time(T0 + 3700)},
                   C: {'start': format_time(T0 + 1000), 'end': format_time(T0 + 6000)}}
        view = self.behavior.continue_range(range_id, trace_extents=extents)
        self.assertEqual(view['phase'], 'whole')
        self.assertTrue(view['worker_restarted'])
        events = self.drive(range_id)
        self.assertEqual(events[-1]['event'], 'completed')
        self.assertEqual(len(api.submitted), 5)   # one aligned whole-trace window, for A only
        summary = self.summary(range_id)
        self.assertEqual(summary['counts']['MATCH'], 1)
        self.assertEqual(summary['counts']['NON_MATCH'], 1)
        self.assertEqual(summary['counts']['UNKNOWN'], 1)
        self.assertEqual([f['trace_id'] for f in summary['flagged']], [A])
        self.assertEqual(summary['flagged'][0]['source'], 'whole')
        self.assertEqual(summary['edge_traces'], [A, C])
        self.assertIn(EDGE_REASON, summary['unknown_reasons'][0]['reason'])
        self.assertEqual(summary['judge_usage']['judge_calls'], sum(len(r) for r in api.executions.values()))
        self.assertEqual(summary['judge_usage']['cost'], 'not available')
        # The fake program is rules-only, but judge receipts were observed.
        self.assertIn('experimental', summary)
        full = json.loads(Path(summary['result_file']).read_text())
        a = next(t for t in full['traces'] if t['trace_id'] == A)
        self.assertEqual([w['verdict'] for w in a['windows']], ['UNKNOWN', 'UNKNOWN'])
        self.assertEqual(a['canonical']['verdict'], 'MATCH')

    def test_a_window_whose_results_cannot_be_read_is_split_and_rerun(self):
        # Results of executions over 30 minutes never arrive (the gateway times out).
        api = FakeAPI([(A, T0 + 100, T0 + 200, lambda whole: 'NON_MATCH'),
                       (B, T0 + 2000, T0 + 2100, lambda whole: 'MATCH')])
        lengths = {}
        start, poll = api.start, api.poll
        def tracking_start(version, population, s, e):
            out = start(version, population, s, e)
            lengths[out['execution_id']] = parse_time(e) - parse_time(s)
            return out
        def slow_poll(execution_id, wait_seconds=20):
            if lengths[execution_id] > 1800:
                raise RuntimeError('Cardinal API closed the connection or timed out before answering')
            return poll(execution_id)
        api.start, api.poll = tracking_start, slow_poll
        self.use(api)
        range_id = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z',
                                             '2026-10-11T01:00:00Z')['range_id']
        events = self.drive(range_id)
        self.assertEqual([e['failures'] for e in events if e['event'] == 'read_retry'], [1, 2, 3])
        self.assertIn('could not be read', next(e for e in events if e['event'] == 'window_split')['reason'])
        self.behavior.continue_range(range_id, trace_extents={})
        self.drive(range_id)
        summary = self.summary(range_id)
        self.assertEqual(summary['counts']['MATCH'], 1)
        self.assertEqual(summary['counts']['NON_MATCH'], 1)
        self.assertEqual(summary['coverage_holes'], [])
        self.assertFalse(summary['judge_usage']['usage_complete'])
        self.assertEqual(len(summary['judge_usage']['unreadable_executions']), 1)
        self.assertIn('abandoned-window', [e['kind'] for e in summary['executions']])

    def test_an_unreadable_minimum_window_is_a_coverage_hole(self):
        api = FakeAPI([(A, T0 + 10, T0 + 20, lambda whole: 'NON_MATCH')])
        def failing_poll(execution_id, wait_seconds=20):
            raise RuntimeError('Cardinal API closed the connection or timed out before answering')
        api.poll = failing_poll
        self.use(api)
        range_id = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z',
                                             '2026-10-11T00:01:30Z')['range_id']
        self.drive(range_id)
        self.behavior.continue_range(range_id, trace_extents={})
        self.drive(range_id)
        summary = self.summary(range_id)
        self.assertEqual([h['status'] for h in summary['coverage_holes']], ['UNREADABLE'])
        self.assertIn('could not be read', summary['coverage_holes'][0]['error'])

    def test_a_dropped_connection_is_a_clear_runtime_error(self):
        import http.client
        import urllib.request
        behavior = Behavior({'output_dir': self.temp.name, 'base_url': 'https://query.example',
                             'api_key_env': 'TEST_BEHAVIOR_KEY'})
        opener = urllib.request.build_opener()
        with patch.dict('os.environ', {'TEST_BEHAVIOR_KEY': 'k'}), \
                patch('urllib.request.build_opener', return_value=opener), \
                patch.object(opener, 'open', side_effect=http.client.RemoteDisconnected('closed')):
            with self.assertRaisesRegex(RuntimeError, 'closed the connection or timed out'):
                behavior.request('GET', '/api/v1/behavior-executions/x/results')

    def test_a_non_size_rejection_fails_the_range_with_the_api_reason(self):
        self.use(FakeAPI([], reject='Cardinal API rejected the request (HTTP 422): version is bound to service '
                                    'megan, not traefik'))
        range_id = self.behavior.start_range(self.version, 'traefik', '2026-10-11T00:00:00Z',
                                             '2026-10-11T03:00:00Z')['range_id']
        events = self.drive(range_id)
        self.assertEqual(events[-1]['event'], 'failed')
        self.assertIn('bound to service megan', events[-1]['error'])
        view = self.behavior.continue_range(range_id)
        self.assertEqual(view['phase'], 'failed')
        self.assertFalse(view['worker_restarted'])

    def test_a_crashed_worker_reports_stalled_and_resumes_from_state(self):
        api = FakeAPI([(B, T0 + 10, T0 + 20, lambda whole: 'NON_MATCH')])
        poll = api.poll
        calls = []
        def crashing_poll(execution_id, wait_seconds=20):
            calls.append(execution_id)
            if len(calls) == 1:
                raise KeyError('unexpected payload')
            return poll(execution_id)
        api.poll = crashing_poll
        self.use(api)
        range_id = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z',
                                             '2026-10-11T00:30:00Z')['range_id']
        with self.assertRaises(KeyError):
            RangeRunner(self.behavior, range_id).run(sleep=lambda s: None)
        self.assertEqual(self.events(range_id)[-1]['event'], 'stalled')
        self.assertTrue(self.behavior.continue_range(range_id)['worker_restarted'])
        self.assertEqual(self.drive(range_id)[-1]['event'], 'needs_extents')
        self.assertEqual(len(api.submitted), 1)   # the saved execution was resumed, not resubmitted

    def test_judge_based_checks_carry_the_experimental_label(self):
        # judge_site empty (as in real accepted programs): the source's judge call decides.
        path = self.behavior.program_path(self.version)
        program = json.loads(path.read_text())
        program['source'] = ('def evaluate(run, recorder, judge):\n'
                             '    judge.decide(proposition="Supported?", evidence=[])\n')
        path.write_text(json.dumps(program))
        api = FakeAPI([(B, T0 + 10, T0 + 20, lambda whole: 'NON_MATCH')])
        start = api.start
        def start_without_receipts(*args):
            out = start(*args)
            for result in api.executions[out['execution_id']]:
                result['jev_receipts'] = []
            return out
        api.start = start_without_receipts
        self.use(api)
        view = self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z', '2026-10-11T00:30:00Z')
        self.assertTrue(view['judge_based'])
        self.drive(view['range_id'])
        self.behavior.continue_range(view['range_id'], trace_extents={})
        events = self.drive(view['range_id'])
        self.assertTrue(events[-1]['experimental'])
        summary = self.summary(view['range_id'])
        self.assertEqual(summary['judge_usage']['judge_calls'], 0)
        self.assertIn('experimental', summary)

    def test_unaccepted_versions_and_overlong_ranges_are_refused(self):
        path = self.behavior.program_path(self.version)
        path.write_text(json.dumps({'diagnostic_version': self.version}))
        with self.assertRaises(ValueError):
            self.behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z', '2026-10-11T01:00:00Z')
        path.write_text(json.dumps({'diagnostic_version': self.version, 'acceptance_id': 'x'}))
        with self.assertRaises(ValueError):
            self.behavior.start_range(self.version, 'svc', '2026-01-01T00:00:00Z', '2026-03-01T00:00:00Z')

    def test_range_tools_are_listed_for_authored_checks(self):
        names = [t['name'] for t in tools_list(self.behavior)['tools']]
        self.assertIn('execute_behavior_range', names)
        self.assertIn('continue_behavior_range', names)
        self.assertNotIn('next_behavior_range_result', names)

    def test_spawn_starts_a_detached_worker_once(self):
        runner = RangeRunner.create(self.behavior, self.version, 'svc', '2026-10-11T00:00:00Z',
                                    '2026-10-11T00:30:00Z', False, 10)
        patch.stopall()
        with patch('range_runner.subprocess.Popen') as popen:
            self.assertTrue(runner.spawn())
            command = popen.call_args[0][0]
            self.assertTrue(command[1].endswith('range_runner.py'))
            self.assertEqual(command[2:4], ['--range', runner.range_id])
            self.assertTrue(popen.call_args[1]['start_new_session'])
        import fcntl
        with (runner.dir / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch('range_runner.subprocess.Popen') as popen:
                self.assertFalse(runner.spawn())
                popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
