"""Small HTTP route adapter for an existing authenticated application host.

The standalone handler is useful for local integration tests. Deployments can call
QualifiedHost.dispatch from their existing tenant-authenticated router instead.
"""
import hmac
import json
from http.server import BaseHTTPRequestHandler


PREFIX = '/api/v1/qualified-behaviors/'


def handler(bindings):
    """bindings: private server-owned sequence of (bearer token, principal, host)."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log credentials or raw agent inputs.

        def do_POST(self):
            authorization = self.headers.get('Authorization', '')
            api_key = self.headers.get('x-cardinalhq-api-key', '')
            selected = next(((principal, host) for token, principal, host in bindings
                             if token and (hmac.compare_digest(authorization, 'Bearer ' + token)
                                           or hmac.compare_digest(api_key, token))), None)
            if selected is None:
                self.send_error(401)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024 or not self.path.startswith(PREFIX):
                    raise ValueError('invalid qualified request')
                operation = self.path[len(PREFIX):]
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('qualified request must be an object')
                principal, host = selected
                result = (host.submit(principal, operation, payload) if operation in ('create', 'execute')
                          else host.dispatch(principal, operation, payload))
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
                self.send_response(200)
            except (ValueError, KeyError, FileNotFoundError, PermissionError) as exc:
                body = json.dumps({'error': type(exc).__name__, 'reason': str(exc)}).encode()
                self.send_response(409)
            except Exception:
                body = b'{"error":"qualified lifecycle failed; inspect host receipts"}'
                self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler
