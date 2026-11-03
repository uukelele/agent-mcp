"""Web tools: ``webfetch`` and ``websearch``.

``webfetch`` is a port of the reader used by the Obsidian project: it prefers
Markdown the site offers, extracts the main article from HTML, follows
SSRF-checked redirects with a pinned connection, and paginates long pages.
``websearch`` uses DuckDuckGo through ``ddgs`` and needs no API key.
"""

from __future__ import annotations

import asyncio
import html as html_module
import re
from typing import Literal
from urllib.parse import urlsplit

import lxml.html
from fastmcp.utilities.types import File, Image

from ..context import ToolError
from . import webread

MAX_OUTPUT = 200_000
_TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}


# ---------------------------------------------------------------------------
# webfetch
# ---------------------------------------------------------------------------


_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


def _walk_text(element) -> str:
    chunks: list[str] = []
    if element.text:
        chunks.append(element.text)
    for child in element:
        if not isinstance(child.tag, str):
            continue
        block = child.tag in _BLOCK_TAGS
        if block:
            chunks.append("\n")
        chunks.append(_walk_text(child))
        if block:
            chunks.append("\n")
        if child.tail:
            chunks.append(child.tail)
    return "".join(chunks)


def _html_to_text(source: str) -> str:
    try:
        document = lxml.html.document_fromstring(source or "<html></html>")
        for element in document.xpath(
            ".//script|.//style|.//noscript|.//template|.//svg|.//title"
        ):
            element.drop_tree()
        root = document.find("body")
        if root is None:
            root = document
        value = _walk_text(root)
    except Exception:
        value = html_module.unescape(re.sub(r"<[^>]+>", " ", source))
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _download_name(url: str, content_type: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1] or "download"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "download"
    if content_type == "application/pdf" and not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


async def _fetch_markdown(
    url: str, offset: int, timeout: float
) -> str | File | Image:
    result = await webread.web_fetch(url, offset=offset, timeout=timeout)
    if not isinstance(result, tuple):
        return result

    data, content_type = result
    name = _download_name(url, content_type)
    if content_type == "application/pdf" or data[:5] == b"%PDF-":
        return File(data=data, format="pdf", name=name)

    subtype = content_type.split("/", 1)[-1].lower() if "/" in content_type else "png"
    if subtype == "jpeg":
        subtype = "jpg"
    return Image(data=data, format=subtype)


async def _fetch_raw(url: str, format: str, timeout: float) -> str:
    response = await webread.fetch(url, webread.ACCEPT_HTML, timeout=timeout)
    if response.status >= 400:
        raise ToolError(
            f"HTTP {response.status} fetching {response.final_url or url}"
        )
    text = webread.decode(response.body, response.charset)
    content_type = response.content_type
    if content_type.startswith(("image/", "audio/", "video/")) or content_type in {
        "application/pdf",
        "application/octet-stream",
    }:
        raise ToolError(
            f"webfetch format={format!r} only supports textual responses; got "
            f"{content_type}. Use format='markdown' to receive PDFs and images as media."
        )
    is_html = content_type in {"text/html", "application/xhtml+xml"} or (
        content_type in {"", "text/plain"} and text.lstrip().startswith("<")
    )
    if format == "html":
        output = text
    elif is_html:
        output = _html_to_text(text)
    else:
        output = text

    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n... [output truncated at {MAX_OUTPUT} chars]"
    if response.truncated:
        output += "\n... [download truncated while streaming]"
    if response.final_url != url:
        output = f"Final URL: {response.final_url}\n\n{output}"
    return output


def webfetch(
    url: str,
    format: str = "markdown",
    timeout: int | float = 30,
    offset: int = 0,
    *,
    allow_private: bool = False,
) -> str | File | Image:
    """Fetch a URL as clean Markdown, text, or raw HTML.

    Markdown mode uses the full reader (main-content extraction, metadata,
    Markdown negotiation, ``/llms.txt``, pagination with ``offset``, and PDF or
    image pass-through).  ``format="text"`` and ``format="html"`` are retained
    for OpenCode compatibility and use a straightforward fetch.
    """

    if format not in {"markdown", "text", "html"}:
        raise ToolError("format must be one of: markdown, text, html")
    if offset < 0:
        raise ToolError("offset must be zero or greater")
    timeout = max(1.0, min(float(timeout), 120.0))
    url = url.strip()
    if not url:
        raise ToolError("url must not be empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url

    token = webread.ALLOW_PRIVATE.set(allow_private)
    try:
        if format == "markdown":
            return asyncio.run(_fetch_markdown(url, int(offset), timeout))
        return asyncio.run(_fetch_raw(url, format, timeout))
    except webread.FetchError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        webread.ALLOW_PRIVATE.reset(token)


# ---------------------------------------------------------------------------
# websearch
# ---------------------------------------------------------------------------


def websearch(
    query: str,
    max_results: int = 8,
    recency: Literal["day", "week", "month", "year"] | None = None,
    news: bool = False,
    *,
    timeout: int | float = 25,
) -> str:
    """Search the web through DuckDuckGo and return title/URL/snippet results."""

    query = query.strip()
    if not query:
        raise ToolError("query must not be empty")
    try:
        from ddgs import DDGS
    except ImportError as exc:  # pragma: no cover - ddgs is a dependency
        raise ToolError(
            "websearch needs the 'ddgs' package. Install agent-mcp with its "
            "normal dependencies and restart."
        ) from exc

    count = max(1, min(int(max_results or 8), 15))
    options: dict[str, object] = {"max_results": count}
    if recency in _TIMELIMIT:
        options["timelimit"] = _TIMELIMIT[recency]

    try:
        with DDGS(timeout=float(timeout)) as search:
            results = list(
                search.news(query, **options) if news else search.text(query, **options)
            )
    except Exception as exc:
        raise ToolError(
            f"Search failed ({type(exc).__name__}: {exc}). Try rephrasing the query."
        ) from exc

    if not results:
        return f"No results for {query!r}. Try different keywords."

    lines = [f"Results for {query!r}:"]
    for index, result in enumerate(results, 1):
        title = (result.get("title") or "").strip()
        url = result.get("href") or result.get("url") or ""
        body = (result.get("body") or result.get("excerpt") or "").strip()
        body = body.replace("\n", " ")
        meta = " · ".join(
            value
            for value in (result.get("source"), (result.get("date") or "")[:10])
            if value
        )
        extra = f"  ({meta})" if meta else ""
        lines.append(f"{index}. {title}\n   {url}{extra}\n   {body[:400]}")
    return "\n".join(lines)
