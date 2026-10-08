"""The scoped plugin key stays at its authenticated Cardinal host."""
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Behavior, default_config

ORG = '12345678-1234-1234-1234-123456789abc'


class ConnectionTests(unittest.TestCase):
    def test_normal_aggregator_and_per_driver_connections_route_to_org_proxy(self):
        for suffix in ('mcp', 'mcp/', 'integrations/lakerunner/mcp'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as output:
                config = default_config({'CARDINAL_MCP_URL': f'https://app.example/api/orgs/{ORG}/{suffix}',
                                         'CARDINAL_BEHAVIOR_OUTPUT_DIR': output})
                self.assertEqual(config['base_url'], f'https://app.example/api/orgs/{ORG}/behavior')
                with patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'scoped-test-key'}), \
                        patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'{}')) as opened:
                    Behavior(config).sdk()
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, f'https://app.example/api/orgs/{ORG}/behavior/api/v1/behavior-programs/sdk')
                self.assertEqual(request.get_header('X-cardinalhq-api-key'), 'scoped-test-key')
                self.assertNotIn('X-chq-internal-key', request.headers)

    def test_explicit_direct_url_takes_precedence(self):
        config = default_config({'CARDINAL_BEHAVIOR_API_URL': 'https://query.example',
                                 'CARDINAL_MCP_URL': 'not-used'})
        self.assertEqual(config['base_url'], 'https://query.example')
        self.assertEqual(config['api_key_env'], 'CARDINAL_QUERY_API_KEY')

    def test_direct_override_requires_query_key_and_never_falls_back_to_mcp_key(self):
        with tempfile.TemporaryDirectory() as output:
            config = default_config({'CARDINAL_BEHAVIOR_API_URL': 'https://query.example',
                                     'CARDINAL_BEHAVIOR_OUTPUT_DIR': output})
            with patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'scoped-test-key'}, clear=True), \
                    patch('urllib.request.OpenerDirector.open') as opened:
                with self.assertRaisesRegex(ValueError, 'CARDINAL_QUERY_API_KEY'):
                    Behavior(config).sdk()
                opened.assert_not_called()
            with patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'scoped-test-key',
                                           'CARDINAL_QUERY_API_KEY': 'data-plane-test-key'}, clear=True), \
                    patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'{}')) as opened:
                Behavior(config).sdk()
                self.assertEqual(opened.call_args.args[0].get_header('X-cardinalhq-api-key'), 'data-plane-test-key')

    def test_missing_connection_does_not_guess_a_data_plane_host(self):
        with tempfile.TemporaryDirectory() as output:
            config = default_config({'CARDINAL_BEHAVIOR_OUTPUT_DIR': output})
            self.assertIsNone(config['base_url'])
            with self.assertRaisesRegex(ValueError, 'Connect the Cardinal plugin'):
                Behavior(config).sdk()

    def test_malformed_or_ambiguous_connection_is_rejected(self):
        for url in (f'https://user:password@app.example/api/orgs/{ORG}/mcp',
                    f'https://app.example/api/orgs/{ORG}/mcp?redirect=other',
                    f'https://app.example/api/orgs/{ORG}/mcp#fragment',
                    'https://app.example/api/orgs/foreign/mcp',
                    f'file:///api/orgs/{ORG}/mcp',
                    f'https://app.example/api/orgs/{ORG}/mcp/../../admin',
                    f'https://app.example/api/orgs/{ORG}%2fother/mcp'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                default_config({'CARDINAL_MCP_URL': url})

    def test_redirect_is_not_followed_with_scoped_credential(self):
        seen = []
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/sink')
                self.end_headers()
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as output, patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'scoped-test-key'}):
                behavior = Behavior({'base_url': f'http://127.0.0.1:{httpd.server_port}', 'output_dir': output})
                with self.assertRaisesRegex(RuntimeError, 'HTTP 302'):
                    behavior.sdk()
            self.assertEqual(seen, ['/api/v1/behavior-programs/sdk'])
        finally:
            httpd.shutdown()
            worker.join(timeout=5)
            httpd.server_close()
