from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from .broadcaster import Broadcaster
from .config import settings
from .content_fetcher import fetch_full_content_and_images
from .ad_filter import is_advertisement
from .http_client import get_http_client
from .spiders.news_spider import _estimate_read_time, _find_image, _make_id, _parse_date, _strip_html
from .topics import classify_topic

logger = logging.getLogger("search")

_SEARCH_FIELDS = ("title", "excerpt", "source", "category", "topic", "tags")

# Google News' RSS search — same well-tested workaround the Reuters feed
# already uses in app/config.py. No API key, no scraping a search results
# page directly; just a documented-in-practice RSS endpoint.
_GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

# How many freshly-fetched results get their full article body fetched
# before being returned/published — keeps an on-demand search snappy
# instead of blocking on 15+ slow publisher pages.
_CONTENT_FETCH_LIMIT = 6


def _matches(item: dict[str, Any], needle: str) -> bool:
    for field in _SEARCH_FIELDS:
        value = item.get(field)
        if isinstance(value, list):
            if any(needle in str(v).lower() for v in value):
                return True
        elif value and needle in str(value).lower():
            return True
    return False


async def search_buffer(broadcaster: Broadcaster, query: str) -> list[dict[str, Any]]:
    """Search whatever's already in the live in-memory buffer — instant,
    no network call. This is the first thing every search tries."""
    needle = query.strip().lower()
    if not needle:
        return []
    items = await broadcaster.snapshot("All")
    return [i for i in items if _matches(i, needle) and not is_advertisement(i)]


def _parse_google_news_rss(xml_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Could not parse Google News search RSS")
        return []

    results: list[dict[str, Any]] = []
    for entry in root.findall("./channel/item"):
        title = _strip_html(entry.findtext("title", default=""))
        link = (entry.findtext("link", default="") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(entry.findtext("description", default=""))[:240]
        published = entry.findtext("pubDate", default="")
        source_el = entry.find("source")
        source = (source_el.text or "").strip() if source_el is not None else "Google News"
        article_id = _make_id(link)
        image = _find_image(entry)
        if not image:
            # Google News search results rarely carry inline media the same
            # way publisher RSS does — fall back to a neutral placeholder so
            # the card never renders a broken image icon.
            image = "https://images.unsplash.com/photo-1495020689067-958852a7765e?auto=format&fit=crop&w=800&h=600&q=70"

        results.append(
            {
                "id": article_id,
                "originalArticleId": article_id,
                "category": "Search",
                "topic": classify_topic(title, summary),
                "title": title,
                "excerpt": summary,
                "content": summary,
                "author": source,
                "source": source,
                "sourceUrl": link,
                "readTime": _estimate_read_time(summary or title),
                "image": image,
                "images": [],
                "publishedAt": _parse_date(published),
                "updatedAt": _parse_date(published),
                "likes": 0,
                "views": 0,
                "tags": [],
                "language": "en",
                "location": "",
                "status": "published",
            }
        )
    return results


async def search_live(query: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Nothing relevant in the buffer yet — go fetch it live instead of
    just saying "no results". Queries Google News' RSS search, pulls full
    article bodies for the top few hits (best-effort), and returns
    ready-to-display items. Never raises: any failure just yields []."""
    url = _GOOGLE_NEWS_SEARCH.format(q=quote(query))
    try:
        client = get_http_client()
        resp = await client.get(url, follow_redirects=True, timeout=settings.download_timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("Live search fetch failed for %r: %s", query, exc)
        return []

    items = _parse_google_news_rss(resp.text)[:limit]
    items = [i for i in items if not is_advertisement(i)]

    # Best-effort full-content fetch for the first handful so the article
    # page isn't stuck showing just the RSS snippet — same content fetcher
    # the regular crawl uses, so this reads no differently from any other
    # story on the site.
    to_hydrate = items[:_CONTENT_FETCH_LIMIT]
    if to_hydrate:
        results = await asyncio.gather(
            *(
                fetch_full_content_and_images(i["sourceUrl"], fallback=i["excerpt"], timeout=settings.download_timeout)
                for i in to_hydrate
            ),
            return_exceptions=True,
        )
        for item, result in zip(to_hydrate, results):
            if isinstance(result, tuple):
                body, images, og_image = result
                if body:
                    item["content"] = body
                # Same og:image fallback as the regular crawl (see
                # crawler.py) -- live search results should show a real
                # photo just as reliably as the main feed does.
                if not item.get("image") and og_image:
                    item["image"] = og_image
                item["images"] = [img for img in images if img != item.get("image")]

    return [i for i in items if not is_advertisement(i)]


async def search(broadcaster: Broadcaster, query: str) -> list[dict[str, Any]]:
    """Search whatever's live already; if that comes up empty (or thin),
    fetch fresh results from the web instead of returning nothing. New
    results are also published into the broadcaster so they're a real,
    clickable part of the feed afterward — not a dead-end search result."""
    buffered = await search_buffer(broadcaster, query)
    if len(buffered) >= 3:
        return buffered

    fetched = await search_live(query)
    seen_urls = {i["sourceUrl"] for i in buffered}
    fresh = [i for i in fetched if i["sourceUrl"] not in seen_urls]

    for item in fresh:
        await broadcaster.publish(item)

    return buffered + fresh