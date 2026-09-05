from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("broadcaster")


class Broadcaster:
    """Fans newly-scraped items out to every open WebSocket in real time,
    and keeps a small rolling window of the most recent items in RAM only.

    That window exists purely to (a) give a newly-connected client an
    initial feed instantly instead of waiting for the next crawl cycle, and
    (b) serve "load more on scroll" pagination. Nothing here ever touches a
    database or the filesystem — restart the process and it's gone, by
    design (per the "don't store, just fetch and show" requirement).
    """

    def __init__(self, buffer_size: int = 300):
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._seen_urls: set[str] = set()
        # ws -> the category that client currently wants live pushes for
        self._clients: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, category: str = "All") -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = category or "All"

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    def set_category(self, ws: WebSocket, category: str) -> None:
        if ws in self._clients:
            self._clients[ws] = category or "All"

    async def snapshot(self, category: str | None = None) -> list[dict[str, Any]]:
        """Most-recent-first list of buffered items, optionally filtered."""
        async with self._lock:
            items = list(self._buffer)
        if category and category.lower() != "all":
            items = [i for i in items if i["category"].lower() == category.lower()]
        return items

    async def get_by_id(self, article_id: str) -> dict[str, Any] | None:
        """Look up a single article by its `id` for permalink pages.

        NOTE — this only searches the same rolling in-memory window
        everything else here uses. There is no database, by design, so a
        direct link to an article stops resolving once it scrolls out of
        the buffer (default: the most recent 300 items across all feeds).
        For permalinks that must stay valid indefinitely, this is the one
        piece of this architecture that would need a persistence layer.
        """
        async with self._lock:
            for item in self._buffer:
                if item.get("id") == article_id:
                    return item
        return None

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def already_seen(self, url: str) -> bool:
        """Cheap, read-only check for "have we already broadcast this
        URL". Exists so the crawler (see `_BroadcastPlugin.item_scraped`
        in crawler.py) can skip the expensive per-item pipeline — full
        live-page fetch, HTML parsing, image extraction, topic
        classification — for articles it's already seen in a previous
        cycle, instead of doing all of that work and only then discovering
        it was wasted when `publish()`'s dedup drops it at the end. This
        is what actually makes a crawl cycle fast: a 120-item feed with 3
        genuinely new stories now does 3 content fetches, not 120."""
        async with self._lock:
            return bool(url) and url in self._seen_urls

    async def publish(self, item: dict[str, Any]) -> None:
        """Called by the crawler for every item scraped. Dedupes by URL,
        adds it to the rolling window, and immediately pushes it to every
        connected client whose selected category matches (or is "All")."""
        async with self._lock:
            url = item.get("sourceUrl", "")
            if not url or url in self._seen_urls:
                return
            self._seen_urls.add(url)
            # Keep the seen-set from growing unbounded over a long-running
            # process; re-derive it from what's still in the buffer.
            cap = (self._buffer.maxlen or 300) * 4
            if len(self._seen_urls) > cap:
                self._seen_urls = {i["sourceUrl"] for i in self._buffer}

            self._buffer.appendleft(item)
            targets = [
                ws
                for ws, cat in self._clients.items()
                if cat.lower() == "all" or cat.lower() == item["category"].lower()
            ]

        if not targets:
            return
        message = json.dumps({"type": "new_item", "item": item})
        await self._send_all(targets, message)

    async def _send_all(self, clients: list[WebSocket], message: str) -> None:
        stale: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        if stale:
            async with self._lock:
                for ws in stale:
                    self._clients.pop(ws, None)
            logger.info("Dropped %d stale WebSocket client(s)", len(stale))