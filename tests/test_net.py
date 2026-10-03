import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quotaglance.net import Http, HttpError


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        targets = {"/elsewhere": f"http://localhost:{port}/echo", "/same": "/echo"}
        if self.path in targets:
            self.send_response(302)
            self.send_header("Location", targets[self.path])
            self.end_headers()
            return
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy",
                 "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


SECRETS = {"Authorization": "Bearer secret", "X-Api-Key": "k", "Cookie": "session=1",
           "Accept-Language": "en"}


def test_credentials_are_dropped_when_a_redirect_changes_host(server):
    echoed = Http().get_json(f"{server}/elsewhere", headers=SECRETS)
    assert "authorization" not in echoed
    assert "x-api-key" not in echoed
    assert "cookie" not in echoed
    assert echoed["accept-language"] == "en"


def test_same_host_redirect_keeps_credentials(server):
    echoed = Http().get_json(f"{server}/same", headers=SECRETS)
    assert echoed["authorization"] == "Bearer secret"


def test_redirects_can_be_refused(server):
    with pytest.raises(HttpError) as caught:
        Http().get(f"{server}/same", headers=SECRETS, follow_redirects=False)
    assert caught.value.status == 302
