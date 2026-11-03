"""
Reading web pages for the agent (the `webfetch` tool).

Fetching
  A browser-like request (real User-Agent and Accept headers), redirects
  followed by hand (at most 6 hops) so the final URL is known, a 5 MB body cap
  (25 MB for PDFs and images) enforced while streaming. Every hop is checked
  against SSRF: only http(s), no credentials or control characters in the URL,
  no internal hostnames (localhost, *.local, *.internal, single-label names,
  ...), no sensitive ports, and the host must resolve to public addresses only;
  the connection is pinned to the checked address, so DNS rebinding can't swap it.

Markdown first
  Many sites serve Markdown to agents. In parallel with the page itself:
  - the page is asked for with `Accept: text/markdown` first (if that request
    fails, it's retried as plain HTML);
  - the `.md` twin of the URL (`/docs/intro` -> `/docs/intro.md`,
    `/a.html` -> `/a.md`) is tried, and used if it really is Markdown;
  - for a site's root, `/llms.txt` (the site's guide for AI agents) is
    fetched too and appended.

Extraction (HTML)
  Metadata from <meta>, Open Graph and JSON-LD. Boilerplate goes first:
  scripts, styles, forms, dialogs, media, navigation, banners, sidebars,
  footers, hidden elements, and class/id patterns (ads, cookie banners, share
  bars, comments, related links, newsletter prompts, pagination, code line
  numbers, screen-reader-only text, ...). A pattern match never deletes the
  document: an element holding more than 35% of the page's text is kept.
  The main block is <article>, [role=main] or <main> when one holds the
  page's prose; otherwise paragraphs are scored (Readability-style: length,
  commas, link density, class/id hints) and the best container wins; if that
  covers under a quarter of the prose (feeds, indexes, table layouts) the
  tightest element covering most of it is used. Then it's converted to
  Markdown: headings, emphasis, inline code and fenced code (with language),
  nested lists, quotes, GFM tables (layout tables become prose), definition
  lists, details, figures, images and links (absolute URLs).
"""

from __future__ import annotations

import asyncio
import codecs
import contextvars
import ipaddress
import json
import re
import socket
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import lxml.html
from lxml import etree

# ``webfetch`` is synchronous from FastMCP's point of view, but the reader is
# asynchronous.  This context variable carries the operator's
# ``--allow-private-web`` choice through the nested reader/site helpers.
ALLOW_PRIVATE = contextvars.ContextVar("agent_mcp_allow_private_web", default=False)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_MD = "text/markdown,text/html;q=0.9,application/xhtml+xml;q=0.9,*/*;q=0.8"
TIMEOUT = 15
MAX_HOPS = 6
MAX_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024  # PDFs and images returned as MCP media
MAX_HTML_CHARS = 1_500_000  # parsed (lxml is C: ~1.5M chars is well under a second)
PAGE_CHARS = 30_000  # content returned per call (more with `offset`)

# --- URL safety --------------------------------------------------------------------

_BAD_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102,
    103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 445,
    465, 512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990,
    993, 995, 1080, 1433, 1434, 1521, 1719, 1720, 1723, 2049, 2375, 2376, 2379, 2380, 3128, 3306,
    3389, 3659, 4045, 4190, 5060, 5061, 5432, 5672, 5984, 6000, 6379, 6443, 6566, 6665, 6666, 6667,
    6668, 6669, 6697, 7001, 9042, 9092, 9200, 9300, 10080, 10250, 10255, 11211, 15672, 27017, 50070,
}
_BAD_HOSTS = {"localhost", "metadata.google.internal", "metadata", "kubernetes.default", "kubernetes"}
_BAD_SUFFIXES = (
    ".localhost", ".local", ".internal", ".intranet", ".lan", ".corp", ".home", ".home.arpa",
    ".onion", ".localdomain", ".svc", ".cluster.local", ".test", ".invalid", ".example",
)


class FetchError(Exception):
    pass


# Metadata services are never reachable, even when the operator allows private
# addresses: they hand out cloud credentials.
_METADATA_IPS = {
    "169.254.169.254",  # AWS, GCP, Azure, OCI, DigitalOcean, Hetzner, ...
    "169.254.170.2",  # AWS ECS task role
    "169.254.170.23",  # AWS EKS Pod Identity
    "168.63.129.16",  # Azure WireServer (a public address)
    "100.100.100.200",  # Alibaba Cloud
    "192.0.0.192",  # Oracle Cloud
    "169.254.42.42",  # Scaleway
    "fd00:ec2::254",  # AWS IMDS IPv6
    "fd00:ec2::23",  # AWS EKS Pod Identity IPv6
    "fd20:ce::254",  # GCP IPv6
    "fd00:42::42",  # Scaleway IPv6
}


def _is_blocked_ip(value: str, allow_private: bool) -> str | None:
    """Return a reason when *value* must not be connected to."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return f"invalid address {value!r}"
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            candidates.append(mapped)
    if any(str(candidate) in _METADATA_IPS for candidate in candidates):
        return f"{ip} is a cloud metadata address"
    if not allow_private and any(not candidate.is_global for candidate in candidates):
        return f"{ip} is a private, loopback, or reserved address"
    return None


def check_url(url: str, allow_private: bool | None = None) -> None:
    """Refuse malformed URLs and, by default, internal-looking hosts.

    DNS answers are checked separately on every redirect hop.  Passing
    ``allow_private=True`` permits loopback and LAN destinations, but cloud
    metadata addresses remain blocked.
    """
    if allow_private is None:
        allow_private = ALLOW_PRIVATE.get()
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise FetchError("the URL contains spaces or control characters")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError as exc:
        raise FetchError("that isn't a valid URL") from exc
    if p.scheme not in ("http", "https"):
        raise FetchError("only http(s) URLs can be read")
    if p.username or p.password:
        raise FetchError("URLs with a username or password aren't allowed")
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError("the URL has no host")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip and not allow_private:
        if host in _BAD_HOSTS or host.endswith(_BAD_SUFFIXES):
            raise FetchError("internal hostnames can't be read")
        if "." not in host:
            raise FetchError("single-label hostnames can't be read")
        if re.fullmatch(r"[0-9.]+|0x[0-9a-f.x]+", host):
            raise FetchError("numeric hostnames can't be read")
    if port is not None and port in _BAD_PORTS and not allow_private:
        raise FetchError(f"port {port} can't be read")


@dataclass
class ResolvedUrl:
    resolved_ip: str
    hostname: str
    port: int
    is_https: bool
    path: str


async def _resolve_hostname(hostname: str, port: int) -> list[str]:
    try:
        loop = asyncio.get_running_loop()
        results = await loop.getaddrinfo(
            hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise FetchError(f'DNS resolution failed for "{hostname}": {exc}') from exc
    seen: set[str] = set()
    addresses: list[str] = []
    for result in results:
        address = str(result[4][0])
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise FetchError(f'DNS resolution failed for "{hostname}"')
    return addresses


def _build_url_with_ip(resolved: ResolvedUrl) -> str:
    address = resolved.resolved_ip
    try:
        if isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address):
            host = f"[{address}]"
        else:
            host = address
    except ValueError:  # pragma: no cover - resolved addresses are parsed already
        host = address
    default_port = 443 if resolved.is_https else 80
    if resolved.port != default_port:
        host += f":{resolved.port}"
    scheme = "https" if resolved.is_https else "http"
    return f"{scheme}://{host}{resolved.path}"


def _host_header(hostname: str, port: int, is_https: bool) -> str:
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if is_https else 80
    return host if port == default_port else f"{host}:{port}"


async def validate_and_resolve_url(url: str, allow_private: bool | None = None) -> ResolvedUrl:
    """Check a URL and return a connection target pinned to a checked IP."""
    if allow_private is None:
        allow_private = ALLOW_PRIVATE.get()
    check_url(url, allow_private=allow_private)
    parsed = urlsplit(url)
    is_https = parsed.scheme == "https"
    hostname = (parsed.hostname or "").rstrip(".")
    if not hostname:
        raise FetchError("the URL has no host")
    try:
        port = parsed.port or (443 if is_https else 80)
    except ValueError as exc:
        raise FetchError("that isn't a valid URL") from exc
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    try:
        address = hostname.strip("[]")
        ipaddress.ip_address(address)
        addresses = [address]
    except ValueError:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        hostname = ascii_host
        addresses = await _resolve_hostname(hostname, port)

    for address in addresses:
        reason = _is_blocked_ip(address, allow_private)
        if reason:
            if "metadata" in reason:
                raise FetchError(f"refusing to fetch {hostname}: {reason}")
            suffix = (
                ""
                if allow_private
                else " Start the server with --allow-private-web to permit it."
            )
            raise FetchError(f"refusing to fetch {hostname} ({reason}){suffix}")
    return ResolvedUrl(
        resolved_ip=addresses[0],
        hostname=hostname,
        port=port,
        is_https=is_https,
        path=path,
    )


# --- fetching ---------------------------------------------------------------------


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes
    truncated: bool
    redirected: list[str] = field(default_factory=list)


@asynccontextmanager
async def open_stream(
    url: str,
    accept: str = ACCEPT_HTML,
    ua: str | None = None,
    timeout: int | float = TIMEOUT,
):
    """GET `url` with redirects followed by hand, every hop SSRF-checked and
    its connection pinned to the checked address. Yields (response with its
    body unread, final URL, redirect hops)."""
    current = url
    hops: list[str] = []
    # ``trust_env=False`` matters: an HTTP(S)_PROXY would otherwise bypass the
    # address checks because the proxy, not this process, would connect.
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, follow_redirects=False
    ) as client:
        for _ in range(MAX_HOPS + 1):
            resolved = await validate_and_resolve_url(current)
            headers = {
                "User-Agent": ua or USER_AGENT,
                "Accept": accept,
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Host": _host_header(
                    resolved.hostname, resolved.port, resolved.is_https
                ),
            }
            extensions = (
                {"sni_hostname": resolved.hostname} if resolved.is_https else {}
            )
            request = client.build_request(
                "GET",
                _build_url_with_ip(resolved),
                headers=headers,
                extensions=extensions,
            )
            try:
                response = await client.send(
                    request, follow_redirects=False, stream=True
                )
            except httpx.TimeoutException as exc:
                raise FetchError("the site took too long to respond") from exc
            except httpx.RequestError as exc:
                raise FetchError(f"couldn't connect ({type(exc).__name__})") from exc
            if response.is_redirect:
                await response.aclose()
                location = response.headers.get("location")
                if not location:
                    raise FetchError("a redirect without a Location header")
                current = urljoin(current, location)
                hops.append(current)
                continue
            try:
                yield response, current, hops
            finally:
                await response.aclose()
            return
    raise FetchError(f"too many redirects (more than {MAX_HOPS})")


async def fetch(
    url: str,
    accept: str = ACCEPT_HTML,
    ua: str | None = None,
    timeout: int | float = TIMEOUT,
) -> Response:
    async with open_stream(url, accept, ua, timeout) as (resp, final_url, hops):
        ctype = resp.headers.get("content-type", "")
        cap = (
            MAX_FILE_BYTES
            if re.match(r"\s*(application/pdf|image/)", ctype, re.I)
            else MAX_BYTES
        )
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > cap:
                    truncated = True
                    break
        except httpx.TimeoutException as exc:
            if not chunks:
                raise FetchError("the site took too long to respond") from exc
            truncated = True
        except httpx.RequestError as exc:
            if not chunks:
                raise FetchError(f"the download failed ({type(exc).__name__})") from exc
            truncated = True
        match = re.search(r"charset=[\"']?([\w.:-]+)", ctype, re.I)
        return Response(
            url=url,
            final_url=final_url,
            status=resp.status_code,
            content_type=ctype.split(";")[0].strip().lower(),
            charset=match.group(1) if match else None,
            body=b"".join(chunks)[:cap],
            truncated=truncated,
            redirected=hops,
        )


# --- decoding ---------------------------------------------------------------------

# What browsers actually use for these labels.
_CHARSET_ALIASES = {
    "iso-8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252", "us-ascii": "cp1252",
    "ascii": "cp1252", "iso8859-1": "cp1252", "l1": "cp1252",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030",
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949",
    "big5": "big5hkscs",
}


def _codec(label: str | None) -> str | None:
    if not label:
        return None
    label = label.strip().strip("\"'").lower()
    label = _CHARSET_ALIASES.get(label, label)
    try:
        info = codecs.lookup(label)
    except LookupError:
        return None
    return info.name if getattr(info, "_is_text_encoding", True) else None


def decode(body: bytes, charset: str | None) -> str:
    """BOM, then the Content-Type charset, then <meta charset> in the first
    1 KiB, then UTF-8 if it decodes cleanly, then a statistical guess."""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if body.startswith(bom):
            return body[len(bom):].decode(enc, errors="replace")
    enc = _codec(charset)
    if not enc:
        head = body[:1024].decode("ascii", errors="replace")
        m = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", head, re.I)
        enc = _codec(m.group(1)) if m else None
    if enc:
        return body.decode(enc, errors="replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body[:200_000]).best()
        if best and best.encoding:
            return body.decode(best.encoding, errors="replace")
    except Exception:
        pass
    return body.decode("cp1252", errors="replace")


# --- metadata ---------------------------------------------------------------------


def _jsonld(doc) -> list[dict]:
    out: list[dict] = []

    def add(x):
        if isinstance(x, list):
            for y in x:
                add(y)
        elif isinstance(x, dict):
            out.append(x)
            if "@graph" in x:
                add(x["@graph"])

    for s in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            add(json.loads(s.text_content().strip() or "null"))
        except Exception:
            continue
    return out


def _name(x) -> str:
    if isinstance(x, list):
        return ", ".join(n for n in (_name(y) for y in x) if n)
    if isinstance(x, dict):
        return str(x.get("name") or "")
    return str(x or "")


def metadata(doc, base: str) -> dict:
    meta: dict[str, str] = {}
    for m in doc.iter("meta"):
        k = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        v = (m.get("content") or "").strip()
        if k and v and k not in meta:
            meta[k] = v
    ld = _jsonld(doc)
    art = next(
        (x for x in ld if any(t in str(x.get("@type", "")) for t in ("Article", "Posting", "Report", "WebPage", "Recipe"))),
        {},
    )
    title_el = doc.find(".//title")
    h1 = doc.find(".//h1")
    title = (
        meta.get("og:title") or meta.get("twitter:title") or (title_el.text_content().strip() if title_el is not None else "")
        or (h1.text_content().strip() if h1 is not None else "")
    )
    byline = _name(art.get("author")) or meta.get("author") or meta.get("article:author") or meta.get("sailthru.author") or ""
    if byline.startswith("http"):
        byline = ""
    if not byline:
        el = doc.xpath('//*[@rel="author" or @itemprop="author" or contains(@class,"byline")][1]')
        if el:
            byline = " ".join(el[0].text_content().split())[:120]
    published = (
        meta.get("article:published_time") or str(art.get("datePublished") or "") or meta.get("datepublished")
        or meta.get("date") or meta.get("dc.date") or meta.get("pubdate") or ""
    )
    if not published:
        t = doc.xpath("//time[@datetime][1]")
        published = t[0].get("datetime") if t else ""

    def link(rel):
        for lk in doc.iter("link"):
            rels = (lk.get("rel") or "").lower().split()
            if rel in rels and lk.get("href"):
                return urljoin(base, lk.get("href"))
        return ""

    html = doc.getroottree().getroot()
    kw = meta.get("keywords") or meta.get("news_keywords") or ""
    return {
        "title": " ".join(title.split()),
        "byline": byline.strip(),
        "publishedAt": published.strip(),
        "modifiedAt": (meta.get("article:modified_time") or str(art.get("dateModified") or "")).strip(),
        "siteName": meta.get("og:site_name") or _name(art.get("publisher")) or "",
        "lang": (html.get("lang") or "").strip().lower() if html is not None else "",
        "description": meta.get("description") or meta.get("og:description") or "",
        "ogImage": urljoin(base, meta["og:image"]) if meta.get("og:image") else "",
        "canonical": link("canonical"),
        "favicon": link("icon") or urljoin(base, "/favicon.ico"),
        "type": meta.get("og:type", ""),
        "section": meta.get("article:section", ""),
        "keywords": [k.strip() for k in kw.split(",") if k.strip()][:20],
        "themeColor": meta.get("theme-color", ""),
    }


# --- boilerplate removal -----------------------------------------------------------

_ALWAYS_DROP = {
    "script", "style", "noscript", "template", "link", "meta", "iframe", "object", "embed", "canvas",
    "video", "audio", "source", "track", "map", "svg", "button", "input", "select", "textarea",
    "option", "datalist", "dialog", "head", "title", "base", "param", "frame", "frameset",
}
_STRUCTURAL = {"nav", "aside", "footer", "header", "form", "menu"}
_DROP_ROLES = {
    "navigation", "banner", "complementary", "contentinfo", "search", "menu", "menubar", "toolbar",
    "dialog", "alertdialog", "tooltip", "tablist", "form",
}
# Matched against each class name and the id (lowercased).
_DROP_RE = re.compile(
    "|".join(
        [
            r"^ads?$", r"^ad[-_]", r"[-_]ads?$", r"[-_]ad[-_]", r"advert", r"adsbygoogle", r"adslot", r"ad-?unit",
            r"dfp", r"doubleclick", r"sponsor", r"^promo", r"[-_]promo", r"outbrain", r"taboola", r"revcontent",
            r"cookie", r"consent", r"gdpr", r"^cmp[-_]", r"onetrust", r"didomi", r"usercentrics", r"truste",
            r"social", r"^share(?:s|bar|this|[-_]|$)", r"[-_]share(?:s|bar|[-_]|$)", r"sharing", r"addthis",
            r"comment", r"disqus", r"^respond$", r"discussion",
            r"related", r"recommend", r"read-?more", r"read-?next", r"up-?next", r"more-?stories",
            r"most-?(read|popular|viewed)", r"trending", r"you-?may", r"also-?like",
            r"newsletter", r"subscribe", r"subscription", r"sign-?up", r"paywall", r"regwall", r"piano",
            r"popup", r"pop-?over", r"^modal", r"[-_]modal", r"lightbox", r"overlay",
            r"breadcrumb", r"pagination", r"^pager", r"[-_]pager", r"page-?nav",
            r"skip-?(link|to|nav)", r"sr-?only", r"screen-?reader", r"visually-?hidden", r"visuallyhidden",
            r"a11y-?hidden", r"offscreen", r"assistive",
            r"line-?num", r"linenos", r"lineno", r"gutter", r"blob-num", r"hljs-ln-n",
            r"sidebar", r"side-?bar", r"^aside", r"widget", r"^footer", r"[-_]footer", r"site-?header", r"masthead",
            r"global-?(nav|header)", r"^nav$", r"^navbar", r"[-_]nav$", r"navigation", r"^menu", r"[-_]menu",
            r"mega-?menu", r"hamburger", r"dropdown", r"toolbar",
            r"mw-editsection", r"catlinks", r"navbox", r"noprint", r"mw-jump", r"^toc$", r"^toc[-_]", r"[-_]toc$",
            r"table-?of-?contents", r"vector-(menu|toc|header|page-toolbar)", r"printfooter", r"^hatnote",
            r"author-?bio", r"^tags?$", r"tag-?list", r"^byline-?share",
            r"back-?to-?top", r"app-?banner", r"smart-?banner", r"download-?app", r"cta$", r"call-?to-?action",
            r"chat-?widget", r"intercom", r"hubspot", r"feedback", r"survey", r"print-?only",
        ]
    )
)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_POSITIVE = re.compile(r"article|body|content|entry|hentry|h-entry|main|page|post|text|blog|story|prose|markdown|rich-?text|docs?\b", re.I)
_NEGATIVE = re.compile(
    r"hidden|banner|combx|comment|com-|contact|foot|footnote|masthead|media|meta|outbrain|promo|related|"
    r"scroll|share|shoutbox|sidebar|skyscraper|sponsor|shopping|tags|tool|widget|nav|menu|byline|author",
    re.I,
)


def _ws(s: str | None) -> str:
    return " ".join((s or "").split())


def _hints(el) -> str:
    return f"{el.get('class') or ''} {el.get('id') or ''}"


def _text_len(el) -> int:
    return len(_ws(el.text_content()))


def _inside(el, tags: set[str]) -> bool:
    p = el.getparent()
    while p is not None:
        if p.tag in tags or (p.get("role") or "").lower() == "main":
            return True
        p = p.getparent()
    return False


def clean(body, doc_chars: int) -> int:
    """Remove boilerplate in place. Returns how many elements were removed."""
    limit = max(1, int(doc_chars * 0.35))
    removed = 0
    stack = [body]
    while stack:
        el = stack.pop()
        for child in list(el):
            tag = child.tag if isinstance(child.tag, str) else ""
            if not tag:  # comment / processing instruction
                child.drop_tree() if hasattr(child, "drop_tree") else el.remove(child)
                continue
            tag = tag.lower()
            drop = tag in _ALWAYS_DROP
            guarded = False
            if not drop:
                role = (child.get("role") or "").lower()
                cls = (child.get("class") or "").lower()
                if (
                    child.get("hidden") is not None
                    or (child.get("aria-hidden") or "").lower() == "true"
                    or _HIDDEN_STYLE.search(child.get("style") or "")
                ):
                    drop = guarded = True
                elif tag == "sup" and "reference" in cls:
                    drop = True
                elif tag in _STRUCTURAL or role in _DROP_ROLES:
                    # A page header or aside inside the article is part of it.
                    keep = tag in ("header", "aside") and _inside(child, {"article", "main"})
                    drop = guarded = not keep
                elif tag not in ("body", "html", "article", "main") and role != "main":
                    hints = cls.split() + ([child.get("id").lower()] if child.get("id") else [])
                    if any(_DROP_RE.search(h) for h in hints):
                        drop = guarded = True
            if drop and guarded and _text_len(child) > limit:
                # Never let a guess delete the document: keep it, score it down.
                child.set("data-wr-penalty", "1")
                drop = False
            if drop:
                child.drop_tree()
                removed += 1
            else:
                stack.append(child)
    return removed


# --- choosing the main block -------------------------------------------------------


def _measure(root) -> tuple[dict, dict]:
    """Text and link-text length of every element (one post-order pass)."""
    text: dict = {}
    links: dict = {}
    order = []
    stack = [root]
    while stack:
        el = stack.pop()
        order.append(el)
        stack.extend(c for c in el if isinstance(c.tag, str))
    for el in reversed(order):
        t = len(_ws(el.text))
        lk = 0
        for c in el:
            if isinstance(c.tag, str):
                t += text.get(c, 0)
                lk += links.get(c, 0)
            t += len(_ws(c.tail))
        text[el] = t
        links[el] = t if el.tag == "a" else lk
    return text, links


def _class_weight(el) -> int:
    h = _hints(el)
    w = 0
    if _NEGATIVE.search(h):
        w -= 25
    if _POSITIVE.search(h):
        w += 25
    if el.get("data-wr-penalty"):
        w -= 50
    return w


def choose(body) -> tuple[object, str]:
    text, links = _measure(body)
    prose = lambda el: max(0, text.get(el, 0) - links.get(el, 0))  # noqa: E731
    total = prose(body) or 1

    # 1. Semantic containers, if one holds most of the prose.
    arts = [a for a in body.iter("article")]
    if arts:
        big = max(arts, key=prose)
        if prose(big) >= 0.5 * total:
            return big, "semantic:article"
    for xp, name in (('.//*[@role="main"]', "semantic:role=main"), (".//main", "semantic:main")):
        found = body.xpath(xp)
        if found:
            el = max(found, key=prose)
            if prose(el) >= 0.5 * total:
                return el, name

    # 2. Readability-style scoring of paragraph containers.
    scores: dict = {}

    def base(el) -> float:
        tag = el.tag
        s = {"div": 5, "article": 10, "section": 5, "main": 8, "pre": 3, "td": 3, "blockquote": 3}.get(tag, 0)
        s -= {"ol": 3, "ul": 3, "dl": 3, "dd": 3, "dt": 3, "li": 3, "form": 3, "address": 3, "th": 5}.get(tag, 0)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            s -= 5
        return s + _class_weight(el)

    for p in body.iter("p", "pre", "td", "blockquote", "li", "h2", "h3"):
        n = text.get(p, 0)
        if n < 25:
            continue
        inner = p.text_content()
        score = 1 + inner.count(",") + inner.count("，") + min(3, n // 100)
        if p.tag == "pre":
            score += 3
        if p.tag in ("li", "h2", "h3"):
            score *= 0.5
        parent = p.getparent()
        grand = parent.getparent() if parent is not None else None
        for anc, share in ((parent, 1.0), (grand, 0.5)):
            if anc is None or not isinstance(anc.tag, str):
                continue
            if anc not in scores:
                scores[anc] = base(anc)
            scores[anc] += score * share
    best, best_score = None, 0.0
    for el, s in scores.items():
        t = text.get(el, 0) or 1
        s *= 1 - min(1.0, links.get(el, 0) / t)
        if s > best_score:
            best, best_score = el, s
    if best is not None and prose(best) >= 0.25 * total:
        # Climb while the parent adds little but text (the article's own wrapper).
        while best is not body and (p := best.getparent()) is not None and prose(p) <= prose(best) * 1.25:
            best = p
        return best, "scored"

    # 3. A feed, index or table layout: the tightest element covering most of the prose.
    el = body
    while True:
        kids = [c for c in el if isinstance(c.tag, str)]
        if not kids:
            break
        k = max(kids, key=prose)
        if prose(k) >= 0.7 * total:
            el = k
        else:
            break
    return el, "coverage" if el is not body else "body"


# --- Markdown ---------------------------------------------------------------------

_BLOCK = {
    "p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "blockquote", "pre", "ul",
    "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "figure",
    "figcaption", "details", "summary", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "address", "center",
    "fieldset", "hgroup", "body", "html", "form", "picture",
}
_TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "igshid", "_hs")
_LANG_RE = re.compile(r"(?:^|\s)(?:language|lang|highlight-source|highlight|brush:?)-?([\w+#.-]+)", re.I)


def _escape(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = re.sub(r"([*`])", r"\\\1", s)
    s = re.sub(r"(?<![\w\\])_|_(?!\w)", r"\\_", s)
    return s.replace("[", "\\[").replace("]", "\\]") if "](" in s else s


def _fence(code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


class Markdown:
    def __init__(self, base: str, images: bool = True, links: bool = True):
        self.base = base
        self.images = images
        self.links = links
        self.n_images = 0
        self.n_links = 0

    # Blocks are joined with blank lines; "\x00" marks a hard line break.
    def convert(self, el) -> str:
        out = self.block(el)
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.replace("\x00", "\\\n").strip()

    def block(self, el) -> str:
        parts: list[str] = []
        inline: list[str] = []

        def flush():
            s = "".join(inline).strip()
            s = re.sub(r"[ \t]*\x00[ \t]*", "\x00", re.sub(r"[ \t]{2,}", " ", s)).strip("\x00 ")
            if s:
                parts.append(s)
            inline.clear()

        if el.text:
            inline.append(_escape(re.sub(r"\s+", " ", el.text)))
        for c in el:
            if isinstance(c.tag, str) and c.tag in _BLOCK:
                flush()
                s = self.element(c)
                if s.strip():
                    parts.append(s.strip("\n"))
            elif isinstance(c.tag, str):
                inline.append(self.element(c))
            if c.tail:
                inline.append(_escape(re.sub(r"\s+", " ", c.tail)))
        flush()
        return "\n\n".join(parts)

    def inline(self, el) -> str:
        """Everything inside `el` on one line (headings, cells, link text)."""
        s = self.block(el)
        return " ".join(s.replace("\x00", " ").split())

    def element(self, el) -> str:
        tag = el.tag
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = self.inline(el)
            return f"{'#' * int(tag[1])} {t}" if t else ""
        if tag == "p" or tag in ("div", "section", "article", "main", "header", "footer", "aside", "nav", "center",
                                 "address", "fieldset", "hgroup", "body", "html", "form", "picture", "thead",
                                 "tbody", "tfoot", "tr", "td", "th", "caption", "figcaption", "dd", "dt"):
            if tag == "figcaption":
                t = self.inline(el)
                return f"*{t}*" if t else ""
            if tag == "dt":
                t = self.inline(el)
                return f"**{t}**" if t else ""
            if tag == "dd":
                return "\n".join(": " + ln if i == 0 else "  " + ln for i, ln in enumerate(self.block(el).splitlines()))
            return self.block(el)
        if tag == "br":
            return "\x00"
        if tag == "hr":
            return "---"
        if tag == "pre":
            return self.pre(el)
        if tag == "blockquote":
            body = self.block(el)
            return "\n".join(("> " + ln) if ln else ">" for ln in body.splitlines())
        if tag in ("ul", "ol"):
            return self.listing(el)
        if tag == "li":
            return self.block(el)
        if tag == "dl":
            return self.deflist(el)
        if tag == "table":
            return self.table(el)
        if tag == "figure":
            return self.block(el)
        if tag == "details":
            return self.block(el)
        if tag == "summary":
            t = self.inline(el)
            return f"**{t}**" if t else ""
        if tag == "img":
            return self.img(el)
        if tag == "a":
            return self.anchor(el)
        if tag in ("strong", "b"):
            return self.wrap(el, "**")
        if tag in ("em", "i", "cite", "dfn"):
            return self.wrap(el, "*")
        if tag in ("del", "s", "strike"):
            return self.wrap(el, "~~")
        if tag in ("code", "kbd", "samp", "tt"):
            return self.code(el)
        if tag == "math":
            alt = el.get("alttext")
            return f"${alt}$" if alt else _escape(_ws(el.text_content()))
        if tag == "q":
            return f"“{self.inline(el)}”"
        if tag in ("sup", "sub"):
            t = self.inline(el)
            return f"^{t}" if tag == "sup" and t and len(t) <= 4 and t.isalnum() else t
        # span, abbr, mark, small, time, label, font, u, ins, ...
        return self.block(el).replace("\n\n", " ")

    def wrap(self, el, mark: str) -> str:
        t = self.block(el).replace("\n\n", " ")
        core = t.strip()
        if not core or "\n" in core or "\x00" in core:
            return t
        lead = " " if t[:1].isspace() else ""
        trail = " " if t[-1:].isspace() else ""
        if core.startswith(mark) and core.endswith(mark):
            return t
        return f"{lead}{mark}{core}{mark}{trail}"

    def code(self, el) -> str:
        t = el.text_content()
        if not t.strip():
            return ""
        t = re.sub(r"\s+", " ", t)
        longest = max((len(m) for m in re.findall(r"`+", t)), default=0)
        ticks = "`" * (longest + 1)
        pad = " " if t.startswith("`") or t.endswith("`") else ""
        return f"{ticks}{pad}{t}{pad}{ticks}"

    def pre(self, el) -> str:
        for br in el.iter("br"):
            br.tail = "\n" + (br.tail or "")
        code = el.text_content().strip("\n")
        if not code.strip():
            return ""
        lang = ""
        node = el
        for _ in range(3):
            if node is None:
                break
            for cand in [node, *node.iter("code")]:
                m = _LANG_RE.search(cand.get("class") or "") if cand is not None else None
                lang = lang or (m.group(1) if m else "") or (cand.get("data-lang") or cand.get("data-language") or "")
            node = node.getparent()
        lang = re.sub(r"[^\w+#.-]", "", lang)[:20]
        if lang.lower() in ("none", "text", "plaintext", "nohighlight"):
            lang = ""
        f = _fence(code)
        return f"{f}{lang}\n{code}\n{f}"

    def listing(self, el) -> str:
        ordered = el.tag == "ol"
        try:
            n = int(el.get("start") or 1)
        except ValueError:
            n = 1
        items = []
        for li in el:
            if not isinstance(li.tag, str):
                continue
            if li.tag != "li":
                s = self.element(li)
                if s.strip():
                    items.append(s)
                continue
            if ordered and li.get("value"):
                try:
                    n = int(li.get("value"))
                except ValueError:
                    pass
            marker = f"{n}. " if ordered else "- "
            n += 1
            body = self.block(li).replace("\x00", "\\\n").strip()
            if not body:
                continue
            lines = body.splitlines()
            pad = " " * len(marker)
            items.append("\n".join([marker + lines[0]] + [(pad + ln) if ln else "" for ln in lines[1:]]))
        loose = any("\n\n" in it for it in items)
        return ("\n\n" if loose else "\n").join(items)

    def deflist(self, el) -> str:
        """`**Term**: definition` per entry (<div> groups inside <dl> allowed)."""
        items: list[str] = []
        term = ""

        def walk(node):
            nonlocal term
            for c in node:
                if not isinstance(c.tag, str):
                    continue
                if c.tag == "div":
                    walk(c)
                elif c.tag == "dt":
                    if term:
                        items.append(f"**{term}**")
                    term = self.inline(c)
                elif c.tag == "dd":
                    body = self.block(c).strip()
                    if term:
                        items.append(f"**{term}**: {body}" if body else f"**{term}**")
                        term = ""
                    elif body:
                        items.append(body)

        walk(el)
        if term:
            items.append(f"**{term}**")
        return "\n\n".join(items)

    def img(self, el) -> str:
        if not self.images:
            return ""
        cls = el.get("class") or ""
        alt = _ws(el.get("alt") or el.get("title") or "")
        if "mwe-math" in cls and alt:
            tex = alt.strip("{} ").removeprefix("\\displaystyle").strip()
            return f"${tex}$"
        src = el.get("src") or el.get("data-src") or el.get("data-lazy-src") or el.get("data-original") or ""
        if (not src or src.startswith("data:")) and el.get("srcset"):
            src = el.get("srcset").split(",")[0].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            return ""
        if re.search(r"shields\.io|camo\.githubusercontent|/badges?[/.?]|badge\.svg|badgen\.net", src):
            return _escape(alt)  # a status badge: its label is the information
        for dim in ("width", "height"):
            v = (el.get(dim) or "").strip()
            if v.isdigit() and int(v) <= 32:
                return ""  # an icon or a tracking pixel
        self.n_images += 1
        src = self.url(src)
        alt = alt.replace("[", "").replace("]", "")
        return f"![{alt}]({src})"

    def url(self, ref: str) -> str:
        """Absolute, without tracking parameters; same-site URLs as a path
        (shorter: the page's URL is given above the content)."""
        u = urlsplit(urljoin(self.base, ref))
        query = "&".join(q for q in u.query.split("&") if q and not q.lower().startswith(_TRACKING))
        here = urlsplit(self.base)
        if (u.scheme, u.netloc) == (here.scheme, here.netloc):
            out = urlunsplit(("", "", u.path or "/", query, u.fragment))
        else:
            out = urlunsplit((u.scheme, u.netloc, u.path, query, u.fragment))
        return out.replace(" ", "%20").replace("(", "%28").replace(")", "%29")

    def anchor(self, el) -> str:
        text = self.block(el).replace("\n\n", " ").strip()
        href = (el.get("href") or "").strip()
        if not self.links or not href or href.startswith(("javascript:", "#", "data:")):
            return text
        if not text:
            return ""
        url = self.url(href)
        self.n_links += 1
        if text in (url, href, urljoin(self.base, href)):
            return f"<{urljoin(self.base, href)}>"
        return f"[{text}]({url})"

    def table(self, el) -> str:
        rows = []
        for tr in el.iter("tr"):
            # Only this table's rows (not a nested table's).
            p = tr.getparent()
            while p is not None and p.tag != "table":
                p = p.getparent()
            if p is not el:
                continue
            cells = [c for c in tr if isinstance(c.tag, str) and c.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return self.block(el)
        width = max(sum(_span(c) for c in r) for r in rows)
        nested = el.find(".//table") is not None
        if width <= 1 or nested or (len(rows) == 1 and width <= 2 and _text_len(el) > 200):
            # Layout, not data: render the cells as prose.
            return "\n\n".join(s for r in rows for c in r if (s := self.block(c).strip()))
        caption = el.find("caption")
        out = []
        if caption is not None and (cap := self.inline(caption)):
            out.append(f"**{cap}**")
            out.append("")
        grid = []
        for r in rows:
            line = []
            for c in r:
                cell = self.block(c).replace("\x00", "<br>").replace("\n\n", "<br>").replace("\n", " ")
                cell = cell.replace("|", "\\|").strip()
                line.append(cell)
                line.extend([""] * (_span(c) - 1))
            line += [""] * (width - len(line))
            grid.append(line[:width])
        head = rows[0]
        aligns = []
        for c in head:
            a = (c.get("align") or "").lower() or (re.search(r"text-align\s*:\s*(\w+)", c.get("style") or "") or [None, ""])[1]
            aligns.extend([a] + [""] * (_span(c) - 1))
        aligns = (aligns + [""] * width)[:width]
        sep = [{"left": ":---", "center": ":---:", "right": "---:"}.get(a, "---") for a in aligns]
        out.append("| " + " | ".join(grid[0]) + " |")
        out.append("| " + " | ".join(sep) + " |")
        for g in grid[1:]:
            out.append("| " + " | ".join(g) + " |")
        return "\n".join(out)


def _span(c) -> int:
    try:
        return max(1, min(50, int(c.get("colspan") or 1)))
    except ValueError:
        return 1


# --- extraction -------------------------------------------------------------------


@dataclass
class Page:
    url: str
    final_url: str
    title: str = ""
    content: str = ""
    content_format: str = "markdown"
    meta: dict = field(default_factory=dict)
    extraction: dict = field(default_factory=dict)
    note: str = ""
    llms_txt: str = ""


def extract_html(html: str, base: str, images: bool = True, links: bool = True) -> tuple[str, dict, dict]:
    """(markdown, metadata, diagnostics) for an HTML document."""
    parsed_chars = min(len(html), MAX_HTML_CHARS)
    html = re.sub(r"^\s*<\?xml[^>]*\?>", "", html[:MAX_HTML_CHARS])
    try:
        doc = lxml.html.document_fromstring(html or "<html></html>")
    except (etree.ParserError, ValueError):
        doc = lxml.html.document_fromstring("<html><body></body></html>")
    meta = metadata(doc, base)
    base_el = doc.find(".//base")
    if base_el is not None and base_el.get("href"):
        base = urljoin(base, base_el.get("href"))
    body = doc.find("body")
    if body is None:
        body = doc
    for el in body.xpath(".//script|.//style|.//noscript|.//template|.//svg"):
        el.drop_tree()
    doc_chars = _text_len(body)
    removed = clean(body, doc_chars)
    root, strategy = choose(body)
    md = Markdown(base, images, links)
    content = md.convert(root)
    if len(content) < 200 and root is not body:
        # The chosen block came out nearly empty: take the whole page instead.
        full = Markdown(base, images, links).convert(body)
        if len(full) > len(content) * 2:
            content, strategy = full, strategy + "+body"
    diag = {
        "strategy": strategy,
        "container": f"{root.tag}{'#' + root.get('id') if root.get('id') else ''}",
        "documentTextChars": doc_chars,
        "candidateTextChars": _text_len(root),
        "removed": removed,
        "images": md.n_images,
        "links": md.n_links,
        "htmlCharsParsed": parsed_chars,
    }
    return content, meta, diag


def _looks_like_html(text: str) -> bool:
    head = text[:600].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<head", "<?xml")) or "<body" in head


def _looks_like_markdown(text: str) -> bool:
    return not _looks_like_html(text) and bool(
        re.search(r"^#{1,6} \S|^\s*[-*] \S|\[[^\]]+\]\([^)]+\)|^```", text[:5000], re.M)
    )


def md_twin(url: str) -> str | None:
    """The `.md` version of a page URL, if it has a plausible one."""
    p = urlsplit(url)
    path = p.path or "/"
    if path == "/":
        return None
    if path.endswith("/"):
        path = path.rstrip("/") + ".md"
    else:
        m = re.search(r"\.([A-Za-z0-9]{1,5})$", path.rsplit("/", 1)[-1])
        if m:
            if m.group(1).lower() not in ("html", "htm", "php", "aspx", "asp", "jsp", "shtml", "xhtml"):
                return None
            path = path[: -len(m.group(0))] + ".md"
        else:
            path += ".md"
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


_BLOCKED_HINTS = re.compile(
    r"just a moment|attention required|enable javascript and cookies|cf-chl|verify you are human|"
    r"access denied|captcha|are you a robot|unusual traffic|request blocked",
    re.I,
)


async def _try(
    url: str, accept: str = ACCEPT_HTML, timeout: int | float = TIMEOUT
) -> Response | None:
    try:
        return await fetch(url, accept, timeout=timeout)
    except FetchError:
        return None


async def _preview(url: str, reason: str) -> Page | None:
    """Blocked: what a link-preview bot is shown (sites let those through)."""
    from . import websites

    try:
        return await websites.preview(url, "", websites.DISCORDBOT, reason)
    except Exception:
        return None


async def read(
    url: str,
    images: bool = True,
    links: bool = True,
    *,
    timeout: int | float = TIMEOUT,
) -> Page | bytes | tuple[bytes, str]:
    """Fetch and extract one page. Raises FetchError with a reason the agent can act on."""
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    check_url(url)
    from . import websites  # (imports this module)

    special = await websites.special(url)
    if isinstance(special, Page):
        return special
    if isinstance(special, str):
        url = special
    twin = md_twin(url)
    is_root = (urlsplit(url).path or "/") == "/"

    async def main() -> Response:
        try:
            r = await fetch(url, ACCEPT_MD, timeout=timeout)
            if r.status < 400:
                return r
        except FetchError:
            r = None
        # Some servers fail on an Accept they don't expect: ask again plainly.
        try:
            r2 = await fetch(url, ACCEPT_HTML, timeout=timeout)
        except FetchError:
            if r is None:
                raise
            return r
        return r2

    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    jobs = [
        main(),
        _try(twin, ACCEPT_MD, timeout) if twin else asyncio.sleep(0, None),
        (
            _try(
                origin + "/llms.txt",
                "text/plain,text/markdown;q=0.9,*/*;q=0.5",
                timeout,
            )
            if is_root
            else asyncio.sleep(0, None)
        ),
    ]
    res, twin_r, llms_r = await asyncio.gather(*jobs, return_exceptions=True)
    if isinstance(res, BaseException):
        if isinstance(res, FetchError):
            raise res
        raise FetchError(f"the request failed ({type(res).__name__})")

    page = Page(url=url, final_url=res.final_url)
    page.meta = {"contentType": res.content_type, "charset": res.charset, "redirected": res.redirected, "status": res.status}
    if res.status >= 400:
        txt = decode(res.body[:20000], res.charset)
        blocked = res.status in (401, 403, 429, 503) or bool(_BLOCKED_HINTS.search(txt[:5000]))
        if blocked and (fallback := await _preview(url, f"The site blocked reading it (HTTP {res.status})")):
            return fallback
        raise FetchError(
            f"HTTP {res.status}" + (" (the site blocks automated access or needs a login)" if blocked else "")
        )

    if not isinstance(llms_r, BaseException) and llms_r is not None and llms_r.status == 200 and llms_r.content_type not in ("text/html", "application/xhtml+xml"):
        t = decode(llms_r.body[:200_000], llms_r.charset).strip()
        if t and not _looks_like_html(t):
            page.llms_txt = t

    md_types = ("text/markdown", "text/x-markdown")
    twin_ok = (
        not isinstance(twin_r, BaseException) and twin_r is not None and twin_r.status == 200
        and (twin_r.content_type in md_types
             or (twin_r.content_type == "text/plain" and _looks_like_markdown(decode(twin_r.body[:20000], twin_r.charset))))
    )
    if res.content_type in md_types:
        page.content = decode(res.body, res.charset)
        page.extraction = {"strategy": "markdown:negotiated"}
    elif twin_ok:
        page.content = decode(twin_r.body, twin_r.charset)
        page.final_url = twin_r.final_url
        page.extraction = {"strategy": "markdown:.md"}
    if page.content:
        fm = re.match(r"---\s*\n(.*?)\n---", page.content, re.S)
        m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", fm.group(1), re.M) if fm else None
        m = m or re.search(r"^#\s+(.+)$", page.content, re.M)
        page.title = m.group(1).strip() if m else ""
        return page

    ct = res.content_type
    if ct in ("text/html", "application/xhtml+xml") or (not ct and _looks_like_html(decode(res.body[:2000], res.charset))):
        html = decode(res.body, res.charset)
        content, meta, diag = await asyncio.to_thread(extract_html, html, res.final_url, images, links)
        page.content, page.title = content, meta.get("title", "")
        page.meta.update(meta)
        page.extraction = diag
        if res.truncated:
            page.note = "The page is over 5 MB: only its start was read."
        text_chars = diag.get("documentTextChars", 0)
        if _BLOCKED_HINTS.search(page.title or "") or (text_chars < 400 and _BLOCKED_HINTS.search(html[:20000])):
            if fallback := await _preview(url, "The site showed a bot check instead of the page"):
                return fallback
            raise FetchError("the site showed a bot check instead of the page")
        if text_chars < 250 and len(html) > 20_000:
            page.note = (
                "The page has almost no text in its HTML: it's probably built by JavaScript in the browser, "
                "which isn't run here. Try another source, or an API/JSON or .md version of it."
            )
        return page
    if ct == "application/json" or ct.endswith("+json"):
        text = decode(res.body, res.charset)
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
        page.content, page.content_format = f"```json\n{text}\n```", "json"
        return page
    if ct.startswith("text/") or ct in ("application/xml", "application/rss+xml", "application/atom+xml", "application/javascript"):
        page.content, page.content_format = decode(res.body, res.charset), "text"
        return page
    if ct == "image/svg+xml":
        page.content, page.content_format = "```svg\n" + decode(res.body, res.charset) + "\n```", "text"
        page.note = "An SVG image: this is its source."
        return page
    if ct == "application/pdf" or ct.startswith("image/") or res.body[:5] == b"%PDF-":
        if res.truncated:
            raise FetchError(f"the file is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        return (res.body, "application/pdf" if res.body[:5] == b"%PDF-" else ct)
    raise FetchError(f"it's a {ct or 'binary'} file, not a web page")


# --- the tool ---------------------------------------------------------------------

_CACHE: OrderedDict[str, tuple[float, Page]] = OrderedDict()
_CACHE_TTL = 600
_CACHE_MAX = 64


def _cut(text: str, start: int, n: int) -> tuple[str, int]:
    """`n` chars of `text` from `start`, ending on a word boundary."""
    end = start + n
    if end >= len(text):
        return text[start:], len(text)
    space = text.rfind(" ", start + n // 2, end)
    nl = text.rfind("\n", start + n // 2, end)
    end = max(space, nl) if max(space, nl) > 0 else end
    return text[start:end], end


def render(page: Page, offset: int = 0, limit: int = PAGE_CHARS) -> str:
    m = page.meta
    head = [f"# {page.title}" if page.title else "", f"URL: {page.final_url}"]
    if page.final_url != page.url:
        head[-1] += f" (from {page.url})"
    facts = [x for x in (
        f"By {m['byline']}" if m.get("byline") else "",
        f"Published {m['publishedAt'][:10]}" if m.get("publishedAt") else "",
        f"Updated {m['modifiedAt'][:10]}" if m.get("modifiedAt") and m.get("modifiedAt")[:10] != (m.get("publishedAt") or "")[:10] else "",
        m.get("siteName") or "",
    ) if x]
    if facts:
        head.append(" · ".join(facts))
    if offset == 0 and m.get("description") and m["description"][:80] not in page.content[:3000]:
        head.append(f"Description: {m['description'][:300]}")
    if page.note:
        head.append(f"Note: {page.note}")
    body, end = _cut(page.content, max(0, offset), limit)
    total = len(page.content)
    words = len(page.content.split())
    out = "\n".join(h for h in head if h) + f"\n({words:,} words)\n\n---\n\n" + (body or "(no text)")
    if offset > 0:
        out = f"[Continuing from character {offset:,} of {total:,}]\n\n" + out
    if end < total:
        out += (
            f"\n\n---\n[Showing characters {offset:,}–{end:,} of {total:,}. "
            f"Call webfetch with offset={end} to read on.]"
        )
    if page.llms_txt and offset == 0:
        out += (
            "\n\n---\n## This site's /llms.txt (its guide for AI agents)\n\n"
            + _cut(page.llms_txt, 0, 12_000)[0]
        )
    return out


def _remember(key: str, page: Page) -> None:
    _CACHE[key] = (time.time(), page)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


async def web_fetch(
    url: str, offset: int = 0, *, timeout: int | float = TIMEOUT
) -> str | tuple[bytes, str]:
    """Read a web page as clean Markdown: the main content without menus,
    ads, cookie banners or footers, with its title, author and date. A PDF or
    image URL comes back as the file itself. Long pages come in parts: the
    result says which offset to pass to read on. If a page can't be read
    (blocked, needs JavaScript, gone), use another source rather than
    retrying it.
    """
    key = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", key):
        key = "https://" + key
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        _CACHE.move_to_end(key)
        return render(hit[1], max(0, int(offset)))
    try:
        page = await read(key, timeout=timeout)
    except FetchError as exc:
        raise FetchError(
            f"couldn't read {key}: {str(exc).rstrip('.')}. Don't retry the same URL: use another "
            "source from your search results, or continue without it."
        ) from exc
    if isinstance(page, tuple):
        return page
    _remember(key, page)
    return render(page, max(0, int(offset)))
