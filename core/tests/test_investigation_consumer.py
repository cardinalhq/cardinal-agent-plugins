"""Restart, retry and credential-boundary tests for supervisor consumption."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import investigation_consumer as c

INV = 'inv_' + 'a' * 24
CONN = {'origin': 'https://example.test', 'org': 'org', 'investigation_id': INV,
        'grant_id': 'grt_' + 'b' * 24, 'token': 'secret'}


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cursor.json'

    def page(self, start=0, end=2):
        return {'investigation_id': INV, 'events': [{'seq': end}], 'last_seq': end,
                'page_size': 1, 'head_seq': end}

    def test_pending_replayed_after_restart_then_explicitly_committed(self):
        with patch.object(c.ie, 'read_events', return_value=self.page()) as read:
            page = c.read(CONN, self.path, client='test')
            self.assertEqual(c.read(CONN, self.path, client='test'), page)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(c.private_read(self.path)['after'], 0)
            with self.assertRaises(ValueError):
                c.reviewed(CONN, self.path, 'unread-batch')
            self.assertEqual(c.reviewed(CONN, self.path, page['batch'])['after'], 2)
            with self.assertRaises(ValueError):
                c.reviewed(CONN, self.path, page['batch'])
        with patch.object(c.ie, 'read_events', return_value=self.page(end=4)) as read:
            c.read(CONN, self.path, client='test')
            self.assertEqual(read.call_args.kwargs['after'], 2)
            self.assertNotIn('class_', read.call_args.kwargs)  # ACKs included
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_error_does_not_advance_and_other_grant_cannot_reuse_cursor(self):
        with patch.object(c.ie, 'read_events', side_effect=RuntimeError('network')):
            with self.assertRaises(RuntimeError):
                c.read(CONN, self.path, client='test')
        self.assertFalse(self.path.exists())
        with patch.object(c.ie, 'read_events', return_value=self.page()):
            c.read(CONN, self.path, client='test')
        for field in ('origin', 'org', 'investigation_id', 'grant_id'):
            with self.assertRaises(ValueError):
                c.read({**CONN, field: 'other'}, self.path, client='test')

    def test_empty_page_does_not_pin_polling_and_hidden_rows_can_commit(self):
        with patch.object(c.ie, 'read_events', return_value={**self.page(end=0), 'events': []}) as read:
            c.read(CONN, self.path, client='test')
            c.read(CONN, self.path, client='test')
            self.assertEqual(read.call_count, 2)
        with patch.object(c.ie, 'read_events', return_value={**self.page(end=5), 'events': []}):
            page = c.read(CONN, self.path, client='test')
            self.assertEqual(c.reviewed(CONN, self.path, page['batch'])['after'], 5)

    def test_timeout_after_acceptance_retries_same_key_and_no_author_session(self):
        outbox = Path(self.tmp.name) / 'advice.json'
        args = (CONN, 'challenge.added', 'Check revocation', 'worker-session', [])
        with patch.object(c.ie, 'append_event', side_effect=TimeoutError()) as post:
            with self.assertRaises(TimeoutError):
                c.post(*args, client='test', outbox=outbox)
            first = post.call_args
        self.assertTrue(outbox.exists())
        with patch.object(c.ie, 'append_event', return_value={'event': {'seq': 3}, 'duplicate': True}) as post:
            c.post(*args, client='test', outbox=outbox)
            self.assertEqual(first, post.call_args)
            self.assertNotIn('session_id', post.call_args.kwargs)
            with self.assertRaises(ValueError):
                c.post(CONN, 'challenge.added', 'Changed', 'worker-session', [], client='test', outbox=outbox)
            self.assertEqual(post.call_count, 1)

    def test_private_credentials_reject_symlink_public_mode_and_missing_token(self):
        self.path.write_text('{}')
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            c.credential_connection(self.path)
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            c.credential_connection(self.path)
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            c.private_read(link)

    def test_bad_watermark_does_not_store_pending(self):
        with patch.object(c.ie, 'read_events', return_value={**self.page(), 'head_seq': 1}):
            with self.assertRaises(ValueError):
                c.read(CONN, self.path, client='test')
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
