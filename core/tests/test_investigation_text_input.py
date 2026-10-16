"""File/stdin transport preserves prose without asking a shell to interpret it."""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cardinal_core import investigation_cli as cli


class TextInputTests(unittest.TestCase):
    def parse(self, argv):
        return cli.build_parser(SimpleNamespace(environ={})).parse_args(cli.normalize_argv(argv))

    def test_file_forms_keep_quotes_unicode_and_newlines(self):
        value = "Don't run `at` or $(touch /tmp/unwanted).\nSource → image.\n"
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'text.txt'; file.write_text(value)
            forms = [(['post', 'challenge'], '--text-file', 'text'),
                     (['question'], '--text-file', 'text'),
                     (['ack', '7', 'accepted'], '--note-file', 'note'),
                     (['rule', '7', '--uphold'], '--text-file', 'text'),
                     (['propose-decision', '--to-session', 'worker'], '--text-file', 'text')]
            for argv, flag, field in forms:
                with self.subTest(argv=argv):
                    args = self.parse(argv + [flag, str(file)])
                    cli.read_text_input(args)
                    self.assertEqual(getattr(args, field), value)

    def test_stdin_and_limits_and_conflicting_sources(self):
        args = self.parse(['post', 'cue', '--stdin'])
        with patch('sys.stdin', io.StringIO('one\ntwo')): cli.read_text_input(args)
        self.assertEqual(args.text, 'one\ntwo')
        args = self.parse(['ack', '7', 'accepted', '--stdin'])
        with patch('sys.stdin', io.StringIO('x' * 2001)), self.assertRaises(ValueError):
            cli.read_text_input(args)
        args = self.parse(['question', 'inline', '--stdin'])
        with self.assertRaises(ValueError): cli.read_text_input(args)
