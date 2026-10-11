"""Exercise real CLI and HTTP transport, including fail-closed token mode."""
import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_codex_storyboard as fixtures
from test_codex_storyboard import CLI, SESSION
from test_codex_investigation import INV, SB


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')


class SupervisionTests(fixtures.CodexStoryboardTests):
    def scoped_cli(self, *args, **env):
        return subprocess.run([sys.executable, str(CLI), 'investigation', *args], text=True,
                              capture_output=True, env=self.env(**env), cwd=self.repo, timeout=20)

    def credential(self):
        gid = 'grt_' + 'c' * 24
        token = encoded({'typ': 'CardinalInvestigation'}) + '.' + encoded(
            {'org': 'test-org', 'inv': INV, 'gid': gid, 'scopes': ['read', 'advise']}) + '.signature'
        path = self.repo / 'grant.json'
        path.write_text(json.dumps({'token': token, 'origin': self.fake.origin}))
        path.chmod(0o600)
        return path, token

    def test_scoped_read_and_post_never_load_owner_key_or_claim_session(self):
        path, token = self.credential()
        self.fake.routes['read-investigation-events'] = (200, {
            'investigation_id': INV, 'events': [], 'next_after': 0, 'head_seq': 0})
        out = self.scoped_cli('events', '--credential-file', str(path))
        self.assertEqual(out.returncode, 0, out.stderr)
        request = self.fake.requests[-1]
        self.assertEqual(request['headers']['authorization'], 'CardinalInvestigation ' + token)
        self.assertNotIn('to_session_id', request['body'])
        self.fake.routes['append-investigation-event'] = (200, {
            'event': {'investigation_id': INV, 'seq': 1}})
        args = ['post', 'challenge', 'Check the missing negative path', '--credential-file', str(path),
                '--to-session', SESSION, '--outbox-file', str(self.repo / 'outbox.json')]
        out = self.scoped_cli(*args)
        self.assertEqual(out.returncode, 0, out.stderr)
        first = self.fake.requests[-1]['body']
        self.assertNotIn('session_id', first)
        out = self.scoped_cli(*args)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(first, self.fake.requests[-1]['body'])
        self.assertNotIn(token, out.stdout + out.stderr)

    def test_refused_or_invalid_token_never_falls_back_to_owner(self):
        self.connect()
        path, _ = self.credential()
        for status, code in ((403, 'grant_revoked'), (401, 'token_expired')):
            before = len(self.fake.requests)
            self.fake.routes['read-investigation-events'] = (status, {'error': code})
            out = self.scoped_cli('events', '--credential-file', str(path))
            self.assertNotEqual(out.returncode, 0)
            self.assertEqual(len(self.fake.requests), before + 1)
            self.assertIn('CardinalInvestigation', self.fake.requests[-1]['headers']['authorization'])
        path.write_text('{"token":"invalid","origin":"https://example.test"}')
        before = len(self.fake.requests)
        out = self.scoped_cli('events', '--credential-file', str(path))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(len(self.fake.requests), before)
        out = self.scoped_cli('checkpoint', '--session', SESSION, '--credential-file', str(path))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(len(self.fake.requests), before)

    def test_grant_writes_private_file_without_printing_token(self):
        self.connect()
        self.fake.routes['ensure-session-investigation'] = (200, {
            'investigation_id': INV, 'storyboard_id': SB, 'is_author': True,
            'view_url': self.fake.origin + '/storyboards/' + SB})
        out = self.scoped_cli('link', '--session', SESSION)
        self.assertEqual(out.returncode, 0, out.stderr)
        source, token = self.credential()
        self.fake.routes['grant-investigation-access'] = (200, {
            'investigation_id': INV, 'grant_id': 'grt_' + 'c' * 24,
            'token': token, 'scopes': ['read', 'advise']})
        dest = self.repo / 'new-grant.json'
        out = self.scoped_cli('grant', '--session', SESSION, '--out', str(dest))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(token, out.stdout + out.stderr)
        self.assertEqual(json.loads(dest.read_text())['token'], token)
        before = len(self.fake.requests)
        out = self.scoped_cli('grant', '--session', SESSION, '--out', str(dest))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(len(self.fake.requests), before)
