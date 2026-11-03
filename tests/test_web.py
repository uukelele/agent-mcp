from __future__ import annotations

import asyncio
import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ddgs
import pytest
from fastmcp.utilities.types import File, Image

from agent_mcp.context import ToolError
from agent_mcp.tools import webread
from agent_mcp.tools.web import webfetch, websearch

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2Z5kAAAAASUVORK5CYII="
)
PDF = b"%PDF-1.4\n%%EOF\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/page":
            body = (
                b"<html><head><title>Local Page</title>"
                b'<meta name="description" content="A local test page">'
                b'<meta property="article:published_time" content="2025-01-02T03:04:05Z">'
                b'<meta name="author" content="Test Author"></head><body>'
                b"<nav>menu</nav><article><h1>Local</h1>"
                b'<p>Hello <a href="/other">world</a> and <strong>friends</strong>.</p>'
                b"<ul><li>One</li><li>Two</li></ul>"
                b"</article><footer>footer</footer></body></html>"
            )
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/docs/page":
            body = b"<html><body><h1>HTML page</h1><p>HTML body</p></body></html>"
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/docs/page.md":
            self._send(200, "text/markdown; charset=utf-8", b"# Markdown twin\n\nFrom .md\n")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
        elif self.path == "/redirect-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/":
            body = b"<html><body><article><p>Root page</p></article></body></html>"
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/llms.txt":
            self._send(200, "text/plain; charset=utf-8", b"# llms\n\nUse the API.")
        elif self.path == "/image.png":
            self._send(200, "image/png", PNG)
        elif self.path == "/file.pdf":
            self._send(200, "application/pdf", PDF)
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence test server
        pass


@pytest.fixture()
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_extract_html_main_content() -> None:
    source = (
        "<html><head><title>Example</title></head><body>"
        "<nav>navigation</nav><article><h1>Title</h1>"
        "<p>Hello <a href='/a'>world</a>.</p><ul><li>One</li></ul>"
        "</article><footer>footer</footer></body></html>"
    )
    content, meta, diagnostics = webread.extract_html(source, "https://example.com/")
    assert meta["title"] == "Example"
    assert "# Title" in content
    assert "[world](/a)" in content
    assert "- One" in content
    assert "navigation" not in content
    assert "footer" not in content
    assert diagnostics["strategy"].startswith("semantic")


def test_decode_bom_and_meta_charset() -> None:
    assert webread.decode(b"\xef\xbb\xbfhello", None) == "hello"
    assert webread.decode(b"<meta charset='utf-8'>caf\xc3\xa9", None).endswith("café")


def test_private_url_is_blocked_by_default(local_server: str) -> None:
    with pytest.raises(ToolError, match="private, loopback, or reserved"):
        webfetch(local_server + "/page")
    with pytest.raises(webread.FetchError, match="private, loopback, or reserved"):
        asyncio.run(
            webread.validate_and_resolve_url(
                local_server + "/page", allow_private=False
            )
        )


def test_metadata_address_is_blocked_even_when_private_is_allowed() -> None:
    with pytest.raises(webread.FetchError, match="metadata"):
        asyncio.run(
            webread.validate_and_resolve_url(
                "http://169.254.169.254/latest/meta-data/", allow_private=True
            )
        )


def test_redirect_to_metadata_is_blocked(local_server: str) -> None:
    with pytest.raises(ToolError, match="metadata"):
        webfetch(local_server + "/redirect-metadata", allow_private=True)


def test_invalid_scheme() -> None:
    with pytest.raises(ToolError, match="only http"):
        webfetch("file:///etc/passwd")


def test_webfetch_markdown_extracts_article(local_server: str) -> None:
    markdown = webfetch(local_server + "/page", allow_private=True)
    assert isinstance(markdown, str)
    assert "# Local Page" in markdown
    assert "By Test Author" in markdown
    assert "Published 2025-01-02" in markdown
    assert "# Local" in markdown
    assert "[world](/other)" in markdown
    assert "- One" in markdown
    assert "menu" not in markdown
    assert "footer" not in markdown


def test_webfetch_prefers_markdown_twin(local_server: str) -> None:
    markdown = webfetch(local_server + "/docs/page", allow_private=True)
    assert isinstance(markdown, str)
    assert "# Markdown twin" in markdown
    assert "From .md" in markdown
    assert "HTML body" not in markdown


def test_webfetch_root_includes_llms_txt(local_server: str) -> None:
    markdown = webfetch(local_server + "/", allow_private=True)
    assert isinstance(markdown, str)
    assert "Root page" in markdown
    assert "This site's /llms.txt" in markdown
    assert "Use the API." in markdown


def test_webfetch_formats(local_server: str) -> None:
    text = webfetch(local_server + "/page", format="text", allow_private=True)
    assert isinstance(text, str)
    assert "Local" in text
    assert "<h1>" not in text

    html = webfetch(local_server + "/page", format="html", allow_private=True)
    assert isinstance(html, str)
    assert "<h1>Local</h1>" in html
    assert "<nav>menu</nav>" in html


def test_webfetch_reports_final_url(local_server: str) -> None:
    markdown = webfetch(local_server + "/redirect", allow_private=True)
    assert isinstance(markdown, str)
    assert "URL: " + local_server + "/page" in markdown
    assert "(from " + local_server + "/redirect)" in markdown


def test_webfetch_returns_binary_media(local_server: str) -> None:
    image = webfetch(local_server + "/image.png", allow_private=True)
    assert isinstance(image, Image)
    assert image.data == PNG

    pdf = webfetch(local_server + "/file.pdf", allow_private=True)
    assert isinstance(pdf, File)
    assert pdf.data == PDF

    with pytest.raises(ToolError, match="only supports textual"):
        webfetch(local_server + "/image.png", format="text", allow_private=True)


def test_webfetch_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    page = webread.Page(
        url="https://example.com/",
        final_url="https://example.com/",
        title="Long",
        content="word " * 1000,
    )
    rendered = webread.render(page, 0, 100)
    assert "Call webfetch with offset=" in rendered
    assert rendered.startswith("# Long")


def test_websearch_formats_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query: str, **kwargs):
            calls.append(("text", kwargs))
            return [
                {
                    "title": "Example",
                    "href": "https://example.com/",
                    "body": "A result\nwith a newline",
                }
            ]

        def news(self, query: str, **kwargs):
            calls.append(("news", kwargs))
            return [
                {
                    "title": "News",
                    "url": "https://example.com/news",
                    "excerpt": "Fresh",
                    "source": "Example News",
                    "date": "2025-01-02T03:04:05Z",
                }
            ]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    result = websearch("python", max_results=99, recency="week")
    assert "Results for 'python':" in result
    assert "1. Example" in result
    assert "https://example.com/" in result
    assert "A result with a newline" in result
    assert calls == [("text", {"max_results": 15, "timelimit": "w"})]

    result = websearch("python", news=True)
    assert "1. News" in result
    assert "Example News" in result
    assert "2025-01-02" in result
    assert calls[-1] == ("news", {"max_results": 8})


def test_websearch_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ToolError, match="query"):
        websearch("   ")
