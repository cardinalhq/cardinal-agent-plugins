"""The Checks preview behind cardinal-behavior MCP tools, on every runtime.

The label ledger and the range-run reads are MCP tools so a sandboxed agent
(Codex, Cursor, Gemini CLI) never writes ~/.cardinal or ~/.<runtime> itself:
the stdio server runs outside the sandbox. The connection comes from the
runtime's own connect files, with the environment as an override.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checks_ledger
import server
from range_runner import RangeRunner
from server import Behavior, default_config

ORG = '12345678-1234-1234-1234-123456789abc'
TRACE = 'a' * 32
OTHER = 'b' * 32
RANGE = 'c' * 32


def connect_files(home: Path, runtime: str, url=f'https://app.example/api/orgs/{ORG}/mcp', key='file-key'):
    agent = home / f'.{runtime}'
    agent.mkdir(parents=True, exist_ok=True)
    (agent / 'cardinal.json').write_text(json.dumps({'mcp_url': url}))
    (agent / 'cardinal-secrets.json').write_text(json.dumps({'mcp_api_key': key}))


class ConnectionByRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def test_codex_cursor_gemini_read_their_own_connect_files(self):
        for runtime in ('codex', 'cursor', 'gemini'):
            with self.subTest(runtime=runtime):
                connect_files(self.home, runtime, key=f'{runtime}-key')
                config = default_config({'CARDINAL_BEHAVIOR_OUTPUT_DIR': str(self.home / 'out')},
                                        runtime=runtime, home=self.home)
                self.assertEqual(config['base_url'], f'https://app.example/api/orgs/{ORG}/behavior')
                self.assertEqual(config['api_key'], f'{runtime}-key')
                self.assertEqual(config['runtime'], runtime)

    def test_the_file_key_is_sent_even_when_another_key_is_in_the_environment(self):
        connect_files(self.home, 'codex')
        config = default_config({'CARDINAL_BEHAVIOR_OUTPUT_DIR': str(self.home / 'out')}, runtime='codex', home=self.home)
        with patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'stray-env-key'}), \
                patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'{}')) as opened:
            Behavior(config).request('GET', '/api/v1/behavior-programs/sdk')
        self.assertEqual(opened.call_args.args[0].get_header('X-cardinalhq-api-key'), 'file-key')
        self.assertNotIn('file-key', json.dumps({k: v for k, v in config.items() if k != 'api_key'}))

    def test_environment_connection_overrides_the_files(self):
        connect_files(self.home, 'gemini', url='https://files.example/api/orgs/%s/mcp' % ORG)
        config = default_config({'CARDINAL_MCP_URL': f'https://env.example/api/orgs/{ORG}/mcp'},
                                runtime='gemini', home=self.home)
        self.assertEqual(config['base_url'], f'https://env.example/api/orgs/{ORG}/behavior')
        self.assertNotIn('api_key', config)
        self.assertEqual(config['api_key_env'], 'CARDINAL_MCP_API_KEY')

    def test_claude_keeps_its_environment_only_connection(self):
        connect_files(self.home, 'claude')
        config = default_config({}, runtime='claude', home=self.home)
        self.assertIsNone(config['base_url'])
        self.assertNotIn('api_key', config)
        self.assertEqual(default_config({})['runtime'], 'claude')

    def test_unconnected_runtime_says_connect(self):
        config = default_config({'CARDINAL_BEHAVIOR_OUTPUT_DIR': str(self.home / 'out')}, runtime='cursor', home=self.home)
        self.assertIsNone(config['base_url'])
        with self.assertRaisesRegex(ValueError, 'Connect the Cardinal plugin'):
            Behavior(config).request('GET', '/api/v1/behavior-programs/sdk')

    def test_runtime_comes_from_the_environment_and_is_validated(self):
        self.assertEqual(default_config({'CARDINAL_BEHAVIOR_RUNTIME': 'codex'}, home=self.home)['runtime'], 'codex')
        with self.assertRaises(ValueError):
            default_config({}, runtime='vim')
        # A connect file that names no organization endpoint leaves the server
        # unconnected (it still starts; every API tool says to connect).
        connect_files(self.home, 'codex', url='https://app.example/api/orgs/foreign/mcp')
        config = default_config({}, runtime='codex', home=self.home)
        self.assertIsNone(config['base_url'])
        self.assertNotIn('api_key', config)


class RangeWaitingByRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = temp.name
        self.version = 'a' * 64

    def behavior(self, runtime):
        behavior = Behavior({'output_dir': self.output, 'base_url': 'https://query.example', 'runtime': runtime})
        path = behavior.program_path(self.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'diagnostic_version': self.version, 'acceptance_id': 'acc-1',
                                    'source': 'def evaluate(run, recorder, judge):\n    pass\n',
                                    'behavior_contract': {'clauses': []}}))
        behavior.context = lambda version: None
        return behavior

    def start(self, runtime):
        with patch.object(RangeRunner, 'spawn', lambda runner, config_path=None: True):
            return self.behavior(runtime).start_range(self.version, 'svc', '2026-10-11T00:00:00Z', '2026-10-11T01:00:00Z')

    def test_claude_listens_with_monitor_other_runtimes_poll_the_status_tool(self):
        claude = self.start('claude')
        self.assertEqual(claude['watch_command'], f"cardinal-checks range watch {claude['range_id']}")
        self.assertNotIn('poll', claude)
        self.assertIn('Monitor', claude['next_action'])
        for runtime in ('codex', 'cursor', 'gemini'):
            with self.subTest(runtime=runtime):
                view = self.start(runtime)
                self.assertNotIn('watch_command', view)
                self.assertEqual(view['poll'], {'tool': 'get_behavior_range', 'arguments': {
                    'range_id': view['range_id'], 'after_event': 0, 'wait_seconds': 20}})
                self.assertIn('get_behavior_range', view['next_action'])
                self.assertNotIn('Monitor', view['next_action'])

    def test_the_worker_is_started_for_the_same_runtime(self):
        behavior = self.behavior('gemini')
        with patch.object(RangeRunner, 'spawn', lambda runner, config_path=None: True):
            view = behavior.start_range(self.version, 'svc', '2026-10-11T00:00:00Z', '2026-10-11T01:00:00Z')
        with patch('range_runner.subprocess.Popen') as popen:
            RangeRunner(behavior, view['range_id']).spawn()
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index('--runtime') + 1], 'gemini')
        self.assertFalse(any('key' in part for part in command))


class LedgerToolTests(unittest.TestCase):
    """The ledger as the MCP tools drive it, through the stdio server."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.output = self.home / '.cardinal' / 'behavior-executions'

    def rpc(self, runtime, *calls):
        requests = [{'jsonrpc': '2.0', 'id': i + 1, 'method': 'tools/call',
                     'params': {'name': name, 'arguments': arguments}}
                    for i, (name, arguments) in enumerate(calls)]
        stdout = io.StringIO()
        env = {'HOME': str(self.home), 'CARDINAL_BEHAVIOR_OUTPUT_DIR': str(self.output)}
        with patch.object(sys, 'argv', ['cardinal-behavior', '--runtime', runtime]), \
                patch.dict('os.environ', env), \
                patch.object(sys, 'stdin', io.StringIO(''.join(json.dumps(r) + '\n' for r in requests))), \
                patch.object(sys, 'stdout', stdout):
            server.main()
        out = []
        for line in stdout.getvalue().splitlines():
            result = json.loads(line)['result']
            out.append((bool(result.get('isError')), json.loads(result['content'][0]['text'])))
        return out

    def test_tools_list_names_the_checks_tools(self):
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['cardinal-behavior', '--runtime', 'codex']), \
                patch.dict('os.environ', {'HOME': str(self.home), 'CARDINAL_BEHAVIOR_OUTPUT_DIR': str(self.output)}), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n')), \
                patch.object(sys, 'stdout', stdout):
            server.main()
        names = [t['name'] for t in json.loads(stdout.getvalue())['result']['tools']]
        for name in ('get_behavior_range', 'list_accepted_checks', 'set_check_example', 'approve_check_example',
                     'remove_check_example', 'get_check_draft', 'lock_check_examples'):
            self.assertIn(name, names)
        # Gemini CLI names a tool mcp_<server>_<tool> and truncates past 63 characters.
        self.assertTrue(all(len(f'mcp_cardinal-behavior_{n}') <= 63 for n in names))

    def test_label_flow_writes_this_runtimes_ledger(self):
        results = self.rpc('codex',
            ('set_check_example', {'draft': 'd1', 'trace_id': TRACE, 'evidence': 'insufficient', 'proposed': 'UNKNOWN',
                                   'missing': 'deciding span not retained', 'reason': 'manifest has gaps'}),
            ('approve_check_example', {'draft': 'd1', 'trace_id': TRACE, 'label': 'MATCH'}),
            ('set_check_example', {'draft': 'd1', 'trace_id': OTHER, 'evidence': 'sufficient', 'proposed': 'MATCH',
                                   'reason': 'number not in any query result'}),
            ('lock_check_examples', {'draft': 'd1'}),
            ('approve_check_example', {'draft': 'd1', 'trace_id': OTHER, 'label': 'NON_MATCH'}),
            ('lock_check_examples', {'draft': 'd1'}),
            ('approve_check_example', {'draft': 'd1', 'trace_id': OTHER, 'label': 'MATCH'}),
            ('get_check_draft', {'draft': 'd1'}))
        errors = [r[0] for r in results]
        self.assertEqual(errors, [False, True, False, True, False, False, True, False])
        self.assertIn('deciding span not retained', results[1][1]['error'])
        self.assertIn('approve every example', results[3][1]['error'])
        self.assertTrue(results[4][1]['corrected'])
        self.assertEqual(results[5][1], {'draft': 'd1', 'expected_verdicts': {TRACE: 'UNKNOWN', OTHER: 'NON_MATCH'},
                                         'locked': True})
        self.assertIn('Revise the contract', results[6][1]['error'])
        self.assertTrue(results[7][1]['locked'])
        draft = self.home / '.codex' / 'cardinal' / 'checks' / 'drafts' / 'd1.json'
        self.assertTrue(draft.is_file())
        self.assertFalse((self.home / '.claude').exists())

    def test_approval_records_its_provenance(self):
        before = time.time()
        results = self.rpc('cursor',
            ('set_check_example', {'draft': 'd3', 'trace_id': TRACE, 'evidence': 'sufficient', 'proposed': 'MATCH',
                                   'reason': 'r'}),
            ('approve_check_example', {'draft': 'd3', 'trace_id': TRACE,
                                       'user_reply': '  yes, that one is a match  '}),
            ('set_check_example', {'draft': 'd3', 'trace_id': OTHER, 'evidence': 'sufficient', 'proposed': 'MATCH',
                                   'reason': 'r'}),
            ('approve_check_example', {'draft': 'd3', 'trace_id': OTHER, 'label': 'NON_MATCH'}),
            ('approve_check_example', {'draft': 'd3', 'trace_id': OTHER, 'user_reply': 7}))
        self.assertEqual([r[0] for r in results], [False, False, False, False, True])
        approved, corrected = results[1][1], results[3][1]
        self.assertEqual((approved['approved_via'], approved['user_reply']), ('mcp', 'yes, that one is a match'))
        self.assertGreaterEqual(approved['approved_at'], before)
        self.assertEqual(corrected['approved_via'], 'mcp')
        self.assertNotIn('user_reply', corrected)
        stored = json.loads((self.home / '.cursor' / 'cardinal' / 'checks' / 'drafts' / 'd3.json').read_text())
        self.assertEqual(stored['examples'][TRACE]['user_reply'], 'yes, that one is a match')
        # The CLI front end records cli; the reply is capped.
        state = self.home / '.cursor' / 'cardinal' / 'checks'
        via_cli = checks_ledger.approve('d3', OTHER, 'NON_MATCH', state=state, user_reply='x' * 5000)
        self.assertEqual(via_cli['approved_via'], 'cli')
        self.assertEqual(len(via_cli['user_reply']), checks_ledger.MAX_USER_REPLY)
        with self.assertRaises(ValueError):
            checks_ledger.approve('d3', OTHER, state=state, approved_via='agent')
        schema = server.tools_list(Behavior({'output_dir': str(self.output)}))
        approve = next(t for t in schema['tools'] if t['name'] == 'approve_check_example')
        self.assertIn('user_reply', approve['inputSchema']['properties'])

    def test_remove_and_insufficient_rules(self):
        results = self.rpc('gemini',
            ('set_check_example', {'draft': 'd2', 'trace_id': TRACE, 'evidence': 'insufficient', 'proposed': 'NON_MATCH',
                                   'missing': 'x', 'reason': 'y'}),
            ('set_check_example', {'draft': 'd2', 'trace_id': TRACE, 'evidence': 'sufficient', 'proposed': 'MATCH',
                                   'reason': 'y'}),
            ('remove_check_example', {'draft': 'd2', 'trace_id': TRACE}),
            ('remove_check_example', {'draft': 'd2', 'trace_id': TRACE}),
            ('set_check_example', {'draft': '../escape', 'trace_id': TRACE, 'evidence': 'sufficient',
                                   'proposed': 'MATCH', 'reason': 'y'}))
        self.assertTrue(results[0][0])
        self.assertIn('UNKNOWN by construction', results[0][1]['error'])
        self.assertEqual(results[2][1], {'removed': True})
        self.assertEqual(results[3][1], {'removed': False})
        self.assertTrue(results[4][0])
        self.assertTrue((self.home / '.gemini' / 'cardinal' / 'checks' / 'drafts' / 'd2.json').is_file())

    def test_accepted_checks_are_listed_from_the_shared_output(self):
        (self.output / 'programs').mkdir(parents=True)
        context = {'description': 'Flag answers stating facts no query returned', 'service_name': 'megan'}
        (self.output / 'programs' / f"{'1' * 64}.json").write_text(json.dumps(
            {'diagnostic_version': '1' * 64, 'compilation_context': context, 'acceptance_id': 'acc-1'}))
        (listed,) = self.rpc('cursor', ('list_accepted_checks', {'service_name': 'megan'}))
        self.assertEqual([c['diagnostic_version'] for c in listed[1]['checks']], ['1' * 64])
        (none,) = self.rpc('cursor', ('list_accepted_checks', {'service_name': 'traefik'}))
        self.assertEqual(none[1]['checks'], [])


class RangePollTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        self.dir = self.output / 'ranges' / RANGE
        self.dir.mkdir(parents=True)
        (self.dir / 'state.json').write_text(json.dumps({'phase': 'windows', 'population': 'svc',
                                                         'windows': [{}], 'pending': [{}]}))

    def write(self, *events):
        with (self.dir / 'events.jsonl').open('a') as handle:
            for event in events:
                handle.write(json.dumps({'at': time.time(), **event}) + '\n')

    def poll(self, **kwargs):
        return checks_ledger.range_poll(RANGE, output=self.output, **kwargs)

    def test_events_after_a_cursor_one_line_each_until_a_terminal_event(self):
        self.write({'event': 'started', 'population': 'svc', 'start': 'S', 'end': 'E', 'windows': 2},
                   {'event': 'window_submitted', 'window': ['a', 'b'], 'execution_id': 'x'},
                   {'event': 'window_done', 'window': ['a', 'b'], 'status': 'COMPLETED',
                    'verdicts': {'NON_MATCH': 3}, 'progress': {'windows_done': 1, 'windows_left': 1}})
        first = self.poll()
        self.assertEqual(len(first['events']), 2)
        self.assertEqual(first['next_event'], 3)
        self.assertFalse(first['done'])
        self.assertIn('after_event 3', first['next_action'])
        self.assertEqual(self.poll(after_event=3)['events'], [])
        (self.dir / 'summary.json').write_text(json.dumps({'counts': {'MATCH': 1}, 'flagged_count': 1}))
        self.write({'event': 'completed', 'population': 4, 'counts': {'MATCH': 1}, 'flagged': 1, 'edge_traces': 0,
                    'conflicts': 0, 'coverage_holes': 0, 'judge_calls': 0, 'summary_file': '/s.json'})
        done = self.poll(after_event=3)
        self.assertTrue(done['done'])
        self.assertEqual(done['latest_event'], 'completed')
        self.assertTrue(done['events'][0].startswith('completed:'))
        self.assertEqual(done['summary'], {'counts': {'MATCH': 1}, 'flagged_count': 1})
        self.assertEqual(done['status']['phase'], 'windows')

    def test_a_first_poll_after_continue_starts_at_the_new_segment(self):
        self.write({'event': 'started', 'population': 'p', 'start': 'S', 'end': 'E', 'windows': 1},
                   {'event': 'needs_extents', 'multi_window_trace_count': 1, 'multi_window_traces': [TRACE],
                    'boundaries': ['S', 'E']})
        needs = self.poll()
        self.assertTrue(needs['done'])
        self.assertEqual(needs['needs_extents']['multi_window_traces'], [TRACE])
        self.assertIn('continue_behavior_range', needs['next_action'])
        self.write({'event': 'extents_received', 'traces': 1, 'whole_trace_windows': 1})
        resumed = self.poll()
        self.assertFalse(resumed['done'])
        self.assertEqual(len(resumed['events']), 1)
        self.assertTrue(resumed['events'][0].startswith('extents received'))

    def test_waits_for_a_new_event(self):
        self.write({'event': 'started', 'population': 'p', 'start': 'S', 'end': 'E', 'windows': 1})
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                self.write({'event': 'failed', 'error': 'bound to another service'})
        out = self.poll(after_event=1, wait_seconds=30, sleep=sleep)
        self.assertEqual(len(slept), 2)
        self.assertEqual(out['events'], ['failed: bound to another service'])
        self.assertTrue(out['done'])
        self.assertIn('verbatim', out['next_action'])

    def test_a_quiet_run_without_a_worker_is_stalled(self):
        self.write({'event': 'started', 'population': 'p', 'start': 'S', 'end': 'E', 'windows': 1})
        later = time.time() + 31
        out = self.poll(after_event=1, clock=lambda: later)
        self.assertTrue(out['done'])
        self.assertEqual(out['latest_event'], 'stalled')
        self.assertIn('continue_behavior_range', out['events'][-1])
        self.assertFalse(self.poll(after_event=1)['done'])

    def test_unknown_or_malformed_ranges_are_refused(self):
        with self.assertRaises(ValueError):
            checks_ledger.range_poll('d' * 32, output=self.output)
        with self.assertRaises(ValueError):
            checks_ledger.range_poll('../x', output=self.output)
        with self.assertRaises(ValueError):
            self.poll(after_event=-1)


if __name__ == '__main__':
    unittest.main()
