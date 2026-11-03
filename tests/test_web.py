from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_mcp.context import ToolError
from agent_mcp.tools.web import (
    _assert_public_url,
    html_to_markdown,
    html_to_text,
    webfetch,
)


def test_html_to_markdown() -> None:
    source = (
        "<h1>Title</h1><p>Hello <a href='https://example.com'>world</a></p>"
        "<ul><li>One</li><li>Two</li></ul><script>bad()</script>"
    )
    result = html_to_markdown(source)
    assert "# Title" in result
    assert "[world](https://example.com)" in result
    assert "- One" in result
    assert "bad()" not in result


def test_html_to_text() -> None:
    source = "<h1>Title</h1><p>Hello <b>world</b></p><script>bad()</script>"
    result = html_to_text(source)
    assert "Title" in result
    assert "Hello world" in result
    assert "# Title" not in result
    assert "bad()" not in result


def test_private_url_is_blocked_by_default() -> None:
    with pytest.raises(ToolError, match="private, loopback, or reserved"):
        _assert_public_url("http://127.0.0.1:1/", allow_private=False)
    _assert_public_url("http://127.0.0.1:1/", allow_private=True)


def test_invalid_scheme() -> None:
    with pytest.raises(ToolError, match="Only http"):
        _assert_public_url("file:///etc/passwd", allow_private=True)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/page":
            body = b"<html><body><h1>Local</h1><p>Hello</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence test server
        pass


@pytest.fixture()
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/page"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_webfetch_formats(local_server: str) -> None:
    markdown = webfetch(local_server, format="markdown", allow_private=True)
    assert "# Local" in markdown
    assert "Hello" in markdown

    text = webfetch(local_server, format="text", allow_private=True)
    assert "Local" in text
    assert "# Local" not in text

    html = webfetch(local_server, format="html", allow_private=True)
    assert "<h1>Local</h1>" in html


def test_webfetch_blocks_localhost_by_default(local_server: str) -> None:
    with pytest.raises(ToolError, match="private, loopback, or reserved"):
        webfetch(local_server)
