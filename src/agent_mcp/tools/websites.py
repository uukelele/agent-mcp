"""
Sites that block scrapers or only render with JavaScript, read another way
(ai/webread.py calls `special()` before the generic reader):

- X / Twitter: posts and profiles from the fxtwitter API (full text, media,
  stats, quoted post).
- YouTube videos: title, channel, date, length, views and the full
  description from the watch page's player data.
- Reddit: posts through vxReddit (no comments), other pages as served to
  link-preview bots.
- Stack Exchange sites: the question and top answers from the public API.
- TikTok: caption and author from oEmbed.
- Instagram, Facebook, Threads, LinkedIn, Pinterest: only their link-preview
  (Open Graph) data, fetched as a link-preview bot, and the agent is told
  that's all it is.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import lxml.html

from . import webread as W

DISCORDBOT = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
FACEBOOKBOT = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

_OG_ONLY = {
    "instagram.com": ("Instagram", FACEBOOKBOT),
    "facebook.com": ("Facebook", FACEBOOKBOT),
    "fb.com": ("Facebook", FACEBOOKBOT),
    "threads.net": ("Threads", DISCORDBOT),
    "threads.com": ("Threads", DISCORDBOT),
    "linkedin.com": ("LinkedIn", DISCORDBOT),
    "pinterest.com": ("Pinterest", DISCORDBOT),
    "snapchat.com": ("Snapchat", DISCORDBOT),
}


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    for p in ("www.", "m.", "mobile.", "old.", "new."):
        h = h.removeprefix(p)
    return h


def _num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n or "")


async def _json(url: str):
    r = await W.fetch(url, "application/json")
    if r.status >= 400:
        raise W.FetchError(f"HTTP {r.status}")
    return json.loads(W.decode(r.body, r.charset))


def _page(url: str, final: str, title: str, content: str, note: str, **meta) -> W.Page:
    p = W.Page(url=url, final_url=final, title=title, content=content.strip(), note=note)
    p.meta = {k: v for k, v in meta.items() if v}
    p.extraction = {"strategy": "site"}
    return p


async def special(url: str) -> W.Page | str | None:
    """A Page for a site read another way, a rewritten URL to read instead,
    or None for the generic reader."""
    host = _host(url)
    try:
        if host in ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com", "nitter.net"):
            return await x(url)
        if host in ("youtube.com", "youtu.be", "music.youtube.com"):
            return await youtube(url)
        if host in ("reddit.com", "redd.it") or host.endswith(".reddit.com"):
            return await reddit(url)
        if host in ("stackoverflow.com", "superuser.com", "serverfault.com", "askubuntu.com", "mathoverflow.net",
                    "stackapps.com") or host.endswith(".stackexchange.com"):
            return await stackexchange(url)
        if host in ("tiktok.com", "vm.tiktok.com"):
            return await tiktok(url)
        for domain, (name, ua) in _OG_ONLY.items():
            if host == domain or host.endswith("." + domain):
                return await preview(url, name, ua)
    except W.FetchError:
        return None  # the generic reader has a go (and reports properly)
    except (ValueError, KeyError, TypeError):
        return None
    return None


# --- X ------------------------------------------------------------------------------


def _tweet_md(t: dict, depth: int = 0) -> str:
    a = t.get("author") or {}
    lines = []
    when = t.get("created_at", "")
    stats = " · ".join(
        f"{_num(t[k])} {label}" for k, label in (("replies", "replies"), ("retweets", "reposts"), ("likes", "likes"),
                                                  ("views", "views")) if t.get(k) is not None
    )
    head = f"**{a.get('name', '')}** (@{a.get('screen_name', '')})" + (f" · {when}" if when else "")
    lines.append(head)
    if t.get("replying_to"):
        lines.append(f"Replying to @{t['replying_to']}")
    text = (t.get("raw_text") or {}).get("text") if isinstance(t.get("raw_text"), dict) else None
    lines.append(text or t.get("text") or "")
    media = t.get("media") or {}
    for ph in media.get("photos") or []:
        lines.append(f"![{ph.get('altText') or 'photo'}]({ph.get('url')})")
    for v in media.get("videos") or []:
        lines.append(f"Video ({v.get('duration', '?')}s): {v.get('url')}")
    if t.get("poll"):
        for c in t["poll"].get("choices") or []:
            lines.append(f"- {c.get('label')}: {c.get('percentage')}%")
    if t.get("article"):
        art = t["article"]
        lines.append(f"Article: **{art.get('title', '')}**\n\n{art.get('preview_text', '')}")
    if stats:
        lines.append(stats)
    if t.get("quote") and depth < 2:
        q = _tweet_md(t["quote"], depth + 1)
        lines.append("Quoting:\n\n" + "\n".join("> " + ln if ln else ">" for ln in q.splitlines()))
    return "\n\n".join(x for x in lines if x)


async def x(url: str) -> W.Page | None:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if len(parts) >= 3 and parts[1] in ("status", "statuses") and parts[2].isdigit():
        data = await _json(f"https://api.fxtwitter.com/{quote(parts[0])}/status/{parts[2]}")
        t = data.get("tweet")
        if not t:
            raise W.FetchError(data.get("message") or "post not found")
        a = t.get("author") or {}
        return _page(
            url, t.get("url") or url, f"Post by {a.get('name', '')} (@{a.get('screen_name', '')})", _tweet_md(t),
            "Read through the fxtwitter API (X blocks direct reading). Replies aren't included.",
            siteName="X", publishedAt=t.get("created_at", ""),
        )
    if len(parts) == 1 and re.fullmatch(r"\w{1,15}", parts[0]) and parts[0].lower() not in ("home", "explore", "search", "i", "settings"):
        data = await _json(f"https://api.fxtwitter.com/{quote(parts[0])}")
        u = data.get("user")
        if not u:
            raise W.FetchError(data.get("message") or "account not found")
        body = [
            u.get("description") or "",
            " · ".join(x for x in (
                f"{_num(u.get('followers'))} followers", f"{_num(u.get('following'))} following",
                f"{_num(u.get('tweets'))} posts", f"Joined {u.get('joined', '')}" if u.get("joined") else "",
                u.get("location") or "", (u.get("website") or {}).get("url", "") if isinstance(u.get("website"), dict) else "",
            ) if x),
        ]
        return _page(
            url, u.get("url") or url, f"{u.get('name', '')} (@{u.get('screen_name', '')}) on X", "\n\n".join(body),
            "Profile read through the fxtwitter API; X doesn't let tools list an account's posts. Search the web "
            "for specific posts, then read their x.com/<user>/status/<id> links.",
            siteName="X",
        )
    return None


# --- YouTube ---------------------------------------------------------------------------


def _video_id(url: str) -> str | None:
    u = urlsplit(url)
    if (u.hostname or "").endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    elif u.path == "/watch":
        vid = (parse_qs(u.query).get("v") or [""])[0]
    else:
        m = re.match(r"^/(?:shorts|live|embed|v)/([\w-]{6,})", u.path)
        vid = m.group(1) if m else ""
    return vid if re.fullmatch(r"[\w-]{6,20}", vid or "") else None


def _player_response(html: str) -> dict:
    i = html.find("ytInitialPlayerResponse")
    if i < 0:
        return {}
    j = html.find("{", i)
    try:
        return json.JSONDecoder().raw_decode(html, j)[0]
    except ValueError:
        return {}


async def youtube(url: str) -> W.Page | None:
    vid = _video_id(url)
    if not vid:
        return None
    watch = f"https://www.youtube.com/watch?v={vid}"
    r = await W.fetch(watch)
    html = W.decode(r.body, r.charset)
    pr = _player_response(html)
    d = pr.get("videoDetails") or {}
    mf = (pr.get("microformat") or {}).get("playerMicroformatRenderer") or {}
    if not d:
        o = await _json(f"https://www.youtube.com/oembed?url={quote(watch, safe='')}&format=json")
        return _page(url, watch, o.get("title", ""), f"Channel: [{o.get('author_name')}]({o.get('author_url')})",
                     "Only the title and channel were available.", siteName="YouTube")
    secs = int(d.get("lengthSeconds") or 0)
    length = f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"
    facts = " · ".join(x for x in (
        f"Channel: {d.get('author', '')}", f"Published {mf.get('publishDate', '')[:10]}" if mf.get("publishDate") else "",
        f"Length {length}" if secs else "", f"{_num(d.get('viewCount'))} views" if d.get("viewCount") else "",
        mf.get("category") or "", "Live" if d.get("isLiveContent") else "",
    ) if x)
    kw = ", ".join((d.get("keywords") or [])[:15])
    content = f"{facts}\n\n## Description\n\n{d.get('shortDescription') or '(none)'}" + (f"\n\nTags: {kw}" if kw else "")
    return _page(
        url, watch, d.get("title", ""), content,
        "Video details from YouTube's page data. The video itself and its transcript can't be watched or read here.",
        siteName="YouTube", byline=d.get("author", ""), publishedAt=mf.get("publishDate", ""),
    )


# --- Reddit ----------------------------------------------------------------------------
# Reddit shows browsers a JavaScript challenge and link-preview bots only a
# post's title (old.reddit.com now wants a login, the .json API is blocked).
# Posts come from vxReddit (an embed fixer whose Discord embed data carries the
# whole post as Markdown); subreddit and user pages are server-rendered for
# link-preview bots, so those go through the normal reader as one.


async def reddit(url: str) -> W.Page | None:
    u = urlsplit(url)
    path = u.path
    if (u.hostname or "").endswith("redd.it"):
        path = "/comments/" + path.strip("/")
    m = re.search(r"/comments/(\w+)", path)
    if not m:
        r = await W.fetch(urlunsplit(("https", "www.reddit.com", path or "/", u.query, "")), ua=DISCORDBOT)
        if r.status >= 400:
            raise W.FetchError(f"HTTP {r.status}")
        content, meta, diag = await asyncio.to_thread(W.extract_html, W.decode(r.body, r.charset), r.final_url)
        p = _page(url, r.final_url, meta.get("title", ""), content,
                  "Reddit page as served to link previews. Open a post's /comments/ link to read the post itself.",
                  siteName="Reddit")
        p.extraction = diag
        return p
    try:
        post = await _vxreddit(path)
    except W.FetchError:
        post = None
    if post:
        title, head, body = post
        return _page(url, f"https://www.reddit.com/comments/{m.group(1)}", title, f"{head}\n\n{body}".strip(),
                     "Post read through vxReddit (Reddit blocks direct reading). Comments aren't available here: "
                     "for the discussion, search the web or say so.", siteName="Reddit")
    return await preview(url, "Reddit", DISCORDBOT)


async def _vxreddit(path: str) -> tuple[str, str, str] | None:
    r = await W.fetch("https://vxreddit.com" + path, W.ACCEPT_HTML, ua=DISCORDBOT)
    if r.status >= 400:
        return None
    doc = lxml.html.document_fromstring(W.decode(r.body, r.charset))
    meta = W.metadata(doc, r.final_url)
    raw = doc.xpath('//script[@id="discord:component-embed"]/text()')
    texts: list[str] = []
    if raw:
        try:
            comp = json.loads(raw[0])["component"]
            stack = list(comp.get("components") or [])
            while stack:
                c = stack.pop(0)
                if isinstance(c, dict):
                    if c.get("content"):
                        texts.append(c["content"])
                    for k in ("components", "items"):
                        stack[0:0] = c.get(k) or []
                    media = (c.get("media") or {}).get("url") if isinstance(c.get("media"), dict) else None
                    if media:
                        texts.append(f"![image]({media})")
        except (ValueError, KeyError, TypeError):
            texts = []
    texts = [t for t in texts if not t.startswith("-# vxReddit")]
    title = meta.get("title", "")
    head = ""
    if texts and texts[0].startswith("**"):
        head = re.sub(r"\s*\[\(link\)\]\([^)]*\)", "", texts.pop(0).strip("* "))
        head = head.replace("⬆️", "score").replace("💬", "comments:").replace(" - ", " · ")
    texts = [t for t in texts if t.strip("# ").strip() != title]
    body = "\n\n".join(texts) or meta.get("description", "")
    if not (title or body):
        return None
    return title, head or meta.get("siteName", ""), body


# --- Stack Exchange --------------------------------------------------------------------


def _se_site(host: str) -> str:
    if host.endswith(".stackexchange.com"):
        return host.removesuffix(".stackexchange.com")
    return {"mathoverflow.net": "mathoverflow.net"}.get(host, host.removesuffix(".com"))


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _html_md(html: str, base: str) -> str:
    return W.Markdown(base).convert(lxml.html.fragment_fromstring(html or "<p></p>", create_parent="div"))


async def stackexchange(url: str) -> W.Page | None:
    m = re.search(r"/(?:questions|q|a)/(\d+)", urlsplit(url).path)
    if not m:
        return None
    site = _se_site(_host(url))
    qid = m.group(1)
    if "/a/" in urlsplit(url).path:
        a = await _json(f"https://api.stackexchange.com/2.3/answers/{qid}?site={site}")
        if not a.get("items"):
            return None
        qid = str(a["items"][0]["question_id"])
    q = await _json(f"https://api.stackexchange.com/2.3/questions/{qid}?site={site}&filter=withbody")
    if not q.get("items"):
        raise W.FetchError("question not found")
    q = q["items"][0]
    ans = await _json(
        f"https://api.stackexchange.com/2.3/questions/{qid}/answers?site={site}&sort=votes&order=desc&pagesize=6&filter=withbody"
    )
    base = q.get("link") or url
    import html as _h

    title = _h.unescape(q.get("title", ""))
    parts = [
        " · ".join(x for x in (f"{q.get('score', 0)} votes", f"{q.get('answer_count', 0)} answers",
                               f"asked {_date(q.get('creation_date'))} by {_h.unescape((q.get('owner') or {}).get('display_name', ''))}",
                               "Tags: " + ", ".join(q.get("tags") or [])) if x),
        _html_md(q.get("body", ""), base),
    ]
    for a in ans.get("items") or []:
        who = _h.unescape((a.get("owner") or {}).get("display_name", ""))
        mark = " · ✓ accepted" if a.get("is_accepted") else ""
        parts.append(f"## Answer: {a.get('score', 0)} votes{mark} · {who} · {_date(a.get('creation_date'))}\n\n"
                     + _html_md(a.get("body", ""), base))
    return _page(url, base, title, "\n\n".join(parts),
                 "Read through the Stack Exchange API (the site blocks direct reading): the question and its top answers.",
                 siteName="Stack Exchange", publishedAt=_date(q.get("creation_date")))


# --- TikTok ----------------------------------------------------------------------------


async def tiktok(url: str) -> W.Page | None:
    o = await _json(f"https://www.tiktok.com/oembed?url={quote(url, safe='')}")
    if not o.get("title") and not o.get("author_name"):
        return await preview(url, "TikTok", DISCORDBOT)
    content = f"Caption: {o.get('title') or '(none)'}\n\nBy [{o.get('author_name')}]({o.get('author_url')})"
    if o.get("thumbnail_url"):
        content += f"\n\n![thumbnail]({o['thumbnail_url']})"
    return _page(url, url, f"TikTok by {o.get('author_name', '')}", content,
                 "Only the caption and author (TikTok's embed data): the video can't be watched here.", siteName="TikTok")


# --- link previews only ----------------------------------------------------------------


async def preview(url: str, site: str, ua: str = DISCORDBOT, reason: str = "") -> W.Page | None:
    """The page's link-preview data (Open Graph / JSON-LD), fetched as a
    link-preview bot. None if there's nothing useful."""
    r = await W.fetch(url, W.ACCEPT_HTML, ua=ua)
    if r.status >= 400:
        raise W.FetchError(f"HTTP {r.status}")
    doc = lxml.html.document_fromstring(W.decode(r.body[: W.MAX_HTML_CHARS * 2], r.charset) or "<html></html>")
    meta = W.metadata(doc, r.final_url)
    if not (meta.get("title") or meta.get("description")) or W._BLOCKED_HINTS.search(meta.get("title", "")):
        return None
    lines = []
    if meta.get("description"):
        lines.append(meta["description"])
    if meta.get("ogImage"):
        lines.append(f"![preview image]({meta['ogImage']})")
    extra = [f"Type: {meta['type']}" if meta.get("type") else "", f"Canonical: {meta['canonical']}" if meta.get("canonical") else ""]
    lines.append(" · ".join(x for x in extra if x))
    note = reason or (f"{site} blocks automated reading and needs JavaScript"
                      if site else "This page couldn't be read directly")
    note += (": this is only its link-preview (Open Graph) data, the same summary a chat app shows for the link. "
             "Don't present it as the full page; for more, search the web for other sources.")
    p = _page(url, r.final_url, meta.get("title", ""), "\n\n".join(x for x in lines if x), note,
              siteName=meta.get("siteName") or site, byline=meta.get("byline", ""), publishedAt=meta.get("publishedAt", ""))
    p.meta["description"] = ""  # already the content
    return p
