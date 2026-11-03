"""Web tools: ``webfetch`` and an optional ``websearch``."""

from __future__ import annotations

import html as html_module
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from ..context import ToolError

USER_AGENT = "agent-mcp/0.1 (+https://github.com/uukelele/agent-mcp)"
MAX_DOWNLOAD = 5 * 1024 * 1024
MAX_OUTPUT = 200_000


# ---------------------------------------------------------------------------
# HTML -> Markdown
# ---------------------------------------------------------------------------


class _MarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.in_pre = False
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "header", "footer", "tr"}:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "pre":
            self.parts.append("\n\n```\n")
            self.in_pre = True
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag == "a":
            self.href = attributes.get("href") or ""
            self.parts.append("[")
        elif tag == "img":
            alt = attributes.get("alt") or "image"
            src = attributes.get("src") or ""
            if src:
                self.parts.append(f"![{alt}]({src})")
        elif tag == "blockquote":
            self.parts.append("\n\n> ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "blockquote"}:
            self.parts.append("\n\n")
        elif tag == "pre":
            self.parts.append("\n```\n\n")
            self.in_pre = False
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag == "a":
            href = self.href or ""
            self.href = None
            self.parts.append(f"]({href})" if href else "]")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_pre:
            self.parts.append(data)
        else:
            self.parts.append(re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(source: str) -> str:
    parser = _MarkdownParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        # A malformed page should still produce something useful.
        return html_module.unescape(re.sub(r"<[^>]+>", " ", source))
    return parser.markdown()


class _TextParser(HTMLParser):
    """Very small HTML-to-text converter that keeps block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "tr", "li"}:
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n\n", value)
        return value.strip()


def html_to_text(source: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return html_module.unescape(re.sub(r"<[^>]+>", " ", source)).strip()
    return parser.text()


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def _assert_public_url(url: str, allow_private: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ToolError("Only http:// and https:// URLs are supported")
    if not parsed.hostname:
        raise ToolError(f"URL has no host: {url}")
    if allow_private:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ToolError(f"Cannot resolve {parsed.hostname}: {exc}") from exc

    for info in infos:
        address = info[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            raise ToolError(
                f"Refusing to fetch {parsed.hostname} ({ip}): private, loopback, or reserved "
                "address. Start the server with --allow-private-web to permit it."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(
        self, req, fp, code, msg, headers, newurl  # type: ignore[no-untyped-def]
    ):
        _assert_public_url(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------------------
# webfetch
# ---------------------------------------------------------------------------


def webfetch(
    url: str,
    format: str = "markdown",
    timeout: int | float = 30,
    *,
    allow_private: bool = False,
) -> str:
    """Fetch a URL as markdown, text, or raw HTML (OpenCode's ``webfetch``)."""

    if format not in {"markdown", "text", "html"}:
        raise ToolError("format must be one of: markdown, text, html")
    timeout = max(1.0, min(float(timeout), 120.0))
    _assert_public_url(url, allow_private)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json,application/xml;q=0.9,*/*;q=0.5",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_private))
    try:
        with opener.open(request, timeout=timeout) as response:
            _assert_public_url(response.geturl(), allow_private)
            content_type = (response.headers.get_content_type() or "").lower()
            if content_type.startswith(("image/", "audio/", "video/")) or content_type in {
                "application/octet-stream",
                "application/pdf",
            }:
                raise ToolError(
                    f"webfetch only supports textual responses; got {content_type}"
                )
            raw = response.read(MAX_DOWNLOAD + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except ToolError:
        raise
    except urllib.error.HTTPError as exc:
        raise ToolError(f"HTTP {exc.code} fetching {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Cannot fetch {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ToolError(f"Timed out after {timeout:g}s fetching {url}") from exc
    except OSError as exc:
        raise ToolError(f"Cannot fetch {url}: {exc}") from exc

    truncated = len(raw) > MAX_DOWNLOAD
    body = raw[:MAX_DOWNLOAD]

    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    is_html = content_type in {"text/html", "application/xhtml+xml"} or (
        content_type in {"", "text/plain"} and text.lstrip().startswith("<")
    )

    if format == "html":
        output = text
    else:
        converter = html_to_text if format == "text" else html_to_markdown
        output = converter(text) if is_html else text

    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n... [output truncated at {MAX_OUTPUT} chars]"
    if truncated:
        output += f"\n... [download truncated at {MAX_DOWNLOAD} bytes]"
    if final_url != url:
        output = f"Final URL: {final_url}\n\n{output}"
    return output


# ---------------------------------------------------------------------------
# websearch (optional provider)
# ---------------------------------------------------------------------------


def websearch(query: str, *, timeout: float = 30.0) -> str:
    """Search the web through Brave or Tavily if an API key is configured."""

    query = query.strip()
    if not query:
        raise ToolError("query must not be empty")

    brave_key = os.environ.get("BRAVE_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")

    if not brave_key and not tavily_key:
        raise ToolError(
            "websearch needs an API key. Set BRAVE_API_KEY or TAVILY_API_KEY in the "
            "server environment, then restart."
        )

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a dependency
        raise ToolError("httpx is required for websearch") from exc

    try:
        with httpx.Client(timeout=timeout) as client:
            if brave_key:
                response = client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 5, "text_decorations": "false"},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": brave_key,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                results = payload.get("web", {}).get("results", [])
                lines = [f"Search results for: {query}"]
                for index, item in enumerate(results[:5], start=1):
                    description = html_module.unescape(
                        re.sub(r"<[^>]+>", "", item.get("description", ""))
                    )
                    lines.append(
                        f"{index}. {item.get('title', '(untitled)')}\n"
                        f"   {item.get('url', '')}\n"
                        f"   {description}"
                    )
                return "\n".join(lines) if results else f"No results found for: {query}"

            response = client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": query,
                    "max_results": 5,
                    "include_answer": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
            lines = [f"Search results for: {query}"]
            answer = payload.get("answer")
            if answer:
                lines.extend([f"Answer: {answer}", ""])
            for index, item in enumerate(payload.get("results", [])[:5], start=1):
                lines.append(
                    f"{index}. {item.get('title', '(untitled)')}\n"
                    f"   {item.get('url', '')}\n"
                    f"   {item.get('content', '')}"
                )
            return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        raise ToolError(
            f"Search provider returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Search request failed: {exc}") from exc
