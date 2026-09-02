from __future__ import annotations

import asyncio
import logging

from bitscrape import BasePlugin, Engine, PluginManager
from bitscrape import Settings as BitscrapeSettings

from .broadcaster import Broadcaster
from .config import DEFAULT_FEEDS, settings
from .content_fetcher import fetch_full_content_and_images
from .ad_filter import is_advertisement
from .spiders.news_spider import NewsFeedSpider
from .topics import classify_topic

logger = logging.getLogger("crawler")

# Full-article fetches happen on the live publisher site, on top of the RSS
# poll — cap how many run at once so one crawl cycle doesn't hammer any
# single site or the process's own connection pool.
_CONTENT_FETCH_CONCURRENCY = 5


class _BroadcastPlugin(BasePlugin):
    """Bridges Bitscrape's `item_scraped` signal straight to the
    WebSocket broadcaster — this is what makes updates "continuous" rather
    than only available at the end of a crawl cycle: it fires the instant
    each item is parsed, while the rest of the crawl is still running."""

    def __init__(self, broadcaster: Broadcaster) -> None:
        self._broadcaster = broadcaster
        self._content_semaphore = asyncio.Semaphore(_CONTENT_FETCH_CONCURRENCY)

    async def item_scraped(self, item, spider) -> None:  # noqa: ANN001 - bitscrape's own signature
        # This app doesn't run ads at all yet — if an RSS item is itself a
        # sponsored/promoted placement rather than an editorial story, it
        # never enters the feed. No half-measure (labeling it, etc.) since
        # there's no ad product here for it to belong to.
        if is_advertisement(item):
            logger.info("Dropped ad/sponsored item: %s", item.get("sourceUrl"))
            return

        # RSS/Atom only ever gives us an excerpt and a single cover image.
        # Fetch the live article page for the full body (and any extra
        # in-article photos) before broadcasting — best-effort; falls back
        # to the excerpt already on `item` if the fetch/extraction fails,
        # so a slow or blocked publisher never drops the item.
        async with self._content_semaphore:
            content, images = await fetch_full_content_and_images(
                item["sourceUrl"],
                fallback=item.get("content") or item.get("excerpt", ""),
                timeout=settings.download_timeout,
            )
        item["content"] = content
        # Don't repeat the cover image as an "extra" image.
        item["images"] = [img for img in images if img != item.get("image")]
        item["topic"] = classify_topic(item.get("title", ""), item.get("excerpt", ""), item.get("tags"))

        # The fetched full page occasionally turns out to be an ad
        # interstitial or "sponsored" wrapper page even though the RSS
        # entry itself looked clean — re-check now that content is in.
        if is_advertisement(item):
            logger.info("Dropped ad/sponsored item after content fetch: %s", item.get("sourceUrl"))
            return

        await self._broadcaster.publish(item)


def _build_bitscrape_settings() -> BitscrapeSettings:
    return BitscrapeSettings(
        concurrent_requests=settings.concurrent_requests,
        concurrent_requests_per_domain=settings.concurrent_requests_per_domain,
        download_delay=settings.download_delay,
        download_timeout=settings.download_timeout,
        robotstxt_obey=True,
        user_agent="InBitsNewsBot/1.0 (+https://inbits.app)",
    )


async def run_crawl_cycle(broadcaster: Broadcaster) -> None:
    """Runs one full pass over every configured feed. No exporter is
    attached, so Bitscrape never writes anything to disk — items only ever
    flow through the plugin hook above."""
    bs_settings = _build_bitscrape_settings()
    spider = NewsFeedSpider(feeds=DEFAULT_FEEDS, settings=bs_settings)

    plugins = PluginManager()
    plugins.register_plugin(_BroadcastPlugin(broadcaster))

    engine = Engine(spider=spider, settings=bs_settings, plugin_manager=plugins)

    try:
        stats = await engine.run()
        logger.info(
            "Crawl cycle finished: %d items scraped, %d requests (%d failed)",
            stats.items_scraped,
            stats.requests_made,
            stats.requests_failed,
        )
    except Exception:
        # A crawl cycle failing (network blip, one feed down, etc.) should
        # never take the server down — log it and let the next cycle retry.
        logger.exception("Crawl cycle raised an unexpected error")


async def crawl_loop(broadcaster: Broadcaster, stop_event: asyncio.Event) -> None:
    """Re-crawls all feeds on a fixed interval for as long as the app is
    running, so the feed keeps getting fresh items without ever polling
    the same feed constantly."""
    while not stop_event.is_set():
        await run_crawl_cycle(broadcaster)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.refresh_interval_seconds)
        except asyncio.TimeoutError:
            pass  # normal: means it's time for the next cycle
