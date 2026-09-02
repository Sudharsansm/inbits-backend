from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import bitscrape

from ..config import FeedConfig
from ..topics import classify_topic

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return _WS_RE.sub(" ", text).strip()


def _parse_date(raw: str | None) -> str:
    """Best-effort parse of RFC-822 (RSS) or ISO-8601 (Atom) dates. Always
    returns a valid ISO string, falling back to "now" so a single malformed
    date never breaks an item."""
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError):
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _make_id(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def _estimate_read_time(excerpt: str) -> int:
    words = len(excerpt.split())
    return max(1, round(words / 40)) if words else 1


def _find_image(entry: ET.Element) -> str:
    media = entry.find("media:content", _NS)
    if media is not None and media.get("url"):
        return media.get("url") or ""
    thumb = entry.find("media:thumbnail", _NS)
    if thumb is not None and thumb.get("url"):
        return thumb.get("url") or ""
    enclosure = entry.find("enclosure")
    if enclosure is not None and (enclosure.get("type") or "").startswith("image"):
        return enclosure.get("url") or ""
    content_encoded = entry.find("content:encoded", _NS)
    if content_encoded is not None and content_encoded.text:
        match = _IMG_SRC_RE.search(content_encoded.text)
        if match:
            return match.group(1)
    return ""


class NewsFeedSpider(bitscrape.Spider):
    """Crawls a configurable set of public RSS/Atom feeds and yields
    normalized news items (plain dicts matching app.models.NewsItem).

    Nothing is ever written to disk here: no exporter is attached, and the
    only consumer of yielded items is the `item_scraped` plugin hook (see
    app/crawler.py), which forwards each item straight to the WebSocket
    broadcaster.
    """

    name = "news_feed"

    def __init__(self, feeds: list[FeedConfig], settings: "bitscrape.Settings | None" = None):
        self._feeds = list(feeds)
        self.start_urls = [f.url for f in self._feeds]
        super().__init__(settings=settings)

    def start_requests(self):
        # Override the default (which just uses start_urls with no meta) so
        # each request carries the source/category it belongs to — parse()
        # reads that back off response.request.meta.
        return [
            self.make_requests_from_url(feed.url).model_copy(
                update={
                    "meta": {
                        "source": feed.source,
                        "category": feed.category,
                        "language": feed.language,
                        "location": feed.location,
                    }
                }
            )
            for feed in self._feeds
        ]

    async def parse(self, response):
        meta = response.request.meta or {}
        source = meta.get("source", response.url)
        category = meta.get("category", "General")
        language = meta.get("language", "en")
        location = meta.get("location", "")

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            self.logger.warning("Could not parse feed XML from %s", response.url)
            return

        # RSS 2.0 puts items at channel/item; Atom puts them at feed/entry.
        entries = root.findall("./channel/item")
        is_atom = False
        if not entries:
            entries = root.findall("atom:entry", _NS)
            is_atom = True

        for entry in entries:
            if is_atom:
                title = entry.findtext("atom:title", default="", namespaces=_NS)
                link_el = entry.find("atom:link[@rel='alternate']", _NS)
                if link_el is None:
                    link_el = entry.find("atom:link", _NS)
                link = link_el.get("href", "") if link_el is not None else ""
                summary = entry.findtext("atom:summary", default="", namespaces=_NS) or entry.findtext(
                    "atom:content", default="", namespaces=_NS
                )
                published = entry.findtext("atom:published", default="", namespaces=_NS)
                updated = entry.findtext("atom:updated", default="", namespaces=_NS) or published
                author = entry.findtext("atom:author/atom:name", default="", namespaces=_NS)
                tags = [
                    (c.get("term") or c.text or "").strip()
                    for c in entry.findall("atom:category", _NS)
                    if (c.get("term") or c.text)
                ]
            else:
                title = entry.findtext("title", default="")
                link = (entry.findtext("link", default="") or "").strip()
                summary = entry.findtext("description", default="") or entry.findtext(
                    "content:encoded", default="", namespaces=_NS
                )
                published = entry.findtext("pubDate", default="")
                updated = (
                    entry.findtext("atom:updated", default="", namespaces=_NS)
                    or entry.findtext("dc:date", default="", namespaces=_NS)
                    or published
                )
                author = entry.findtext("author", default="") or entry.findtext(
                    "dc:creator", default="", namespaces=_NS
                )
                tags = [
                    _strip_html(c.text)
                    for c in entry.findall("category")
                    if c.text and c.text.strip()
                ]

            title = _strip_html(title)
            link = (link or "").strip()
            if not title or not link:
                continue

            excerpt = _strip_html(summary)[:240]
            article_id = _make_id(link)
            clean_tags = [t for t in dict.fromkeys(tags) if t]
            topic = classify_topic(title, excerpt, clean_tags)

            yield {
                "id": article_id,
                "originalArticleId": article_id,
                "category": category,
                "topic": topic,
                "title": title,
                "excerpt": excerpt,
                # Full body isn't available from RSS/Atom itself — populated
                # downstream by app/content_fetcher.py from the live page.
                # Defaults to the excerpt so the field is never blank.
                "content": excerpt,
                "author": _strip_html(author) or source,
                "source": source,
                "sourceUrl": link,
                "readTime": _estimate_read_time(excerpt or title),
                "image": _find_image(entry),
                "images": [],
                "publishedAt": _parse_date(published),
                "updatedAt": _parse_date(updated) if updated else _parse_date(published),
                "likes": 0,
                "views": 0,
                "tags": clean_tags,  # de-duped, order-preserved
                "language": language,
                "location": location,
                "status": "published",
            }
