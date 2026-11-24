"""Exercise CCS HTTP requests against a loopback-only server with synthetic credentials."""

from __future__ import annotations

import http.client
import http.server
import json
import re
import threading
import urllib.request

import pytest

from cc_switcher import paths, usage

# Capture the real stdlib functions before the autouse network-blocking fixture runs.
_REAL_URLOPEN = urllib.request.urlopen
_REAL_OPENER_OPEN = urllib.request.OpenerDirector.open


@pytest.fixture
def loopback_server(monkeypatch):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            result = json.dumps({"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 3600}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(paths, "USAGE_URL", root + "/api/oauth/usage")
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _REAL_OPENER_OPEN)
    monkeypatch.setattr(urllib.request, "_opener", None)
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_test_guard_blocks_custom_openers(monkeypatch):
    def forbid_connection(*_args, **_kwargs):
        raise RuntimeError("network guard was bypassed")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", forbid_connection)
    with pytest.raises(AssertionError, match="network blocked"):
        urllib.request.build_opener().open("http://127.0.0.1:9/", timeout=1)


def test_transport_removes_an_explicit_user_agent(loopback_server):
    from cc_switcher import http_transport

    request = urllib.request.Request(paths.USAGE_URL, headers={"User-Agent": "accidental/1.0"})
    http_transport.open_without_user_agent(request, timeout=5).close()
    headers = {k.lower(): v for k, v in loopback_server[0]["headers"].items()}
    assert "user-agent" not in headers


def test_redirected_requests_still_omit_user_agent(loopback_server):
    from cc_switcher import http_transport

    root = paths.USAGE_URL.removesuffix("/api/oauth/usage")
    request = urllib.request.Request(root + "/redirect", headers={"User-Agent": "accidental/1.0"})
    http_transport.open_without_user_agent(request, timeout=5).close()
    assert [r["path"] for r in loopback_server] == ["/redirect", "/redirected"]
    for event in loopback_server:
        assert "user-agent" not in {k.lower() for k in event["headers"]}


def test_usage_and_reset_requests_send_no_user_agent_or_python_version(loopback_server):
    usage.fetch_usage_raw("fake-access")
    usage.fetch_resets_raw("fake-access")

    assert [r["method"] for r in loopback_server] == ["GET", "GET"]
    assert loopback_server[0]["path"] == "/api/oauth/usage"
    assert loopback_server[1]["path"].startswith("/api/oauth/usage?")
    for request in loopback_server:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert "user-agent" not in headers
        assert not any(re.search(r"(?i)python(?:-urllib)?[/ ]\d", str(value)) for value in headers.values())
    assert loopback_server[0]["headers"].get("Authorization") == "Bearer fake-access"
