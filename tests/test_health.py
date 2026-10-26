import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from db_cutover.commands.inspect import HealthRedirectHandler


def test_health_token_is_not_forwarded_to_redirect_target():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("X-Health-Check-Token")))
            self.send_response(302)
            self.send_header("Location", "/unexpected-target")
            self.end_headers()

        def log_message(self, *args):
            pass  # synthetic local requests do not need HTTP access logs

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = urllib.request.build_opener(HealthRedirectHandler())
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/health", headers={"X-Health-Check-Token": "synthetic-token"}
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            client.open(request, timeout=5)
        assert caught.value.code == 302
        assert requests == [("/health", "synthetic-token")]
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
