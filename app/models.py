from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class NewsItem(BaseModel):
    """Normalized news item. Field names intentionally match the frontend's
    existing `Post` shape (see the InBits frontend's src/lib/content.ts) so
    wiring this up later is a drop-in, not a rewrite."""

    id: str
    originalArticleId: str  # dedup key — same as `id` today (sha1 of the
    # canonical article link), kept as its own field so a future source
    # that already has its own native ID can populate this separately.
    category: str  # Tech / World / Business / Culture / Sports / India / etc.
    topic: str = ""  # Real subject-matter classification (Politics/Sports/
    # Technology/etc, see app/topics.py) — separate from `category`, which
    # only encodes which regional edition a source belongs to.
    title: str
    excerpt: str
    content: str = ""  # Full article body. Populated by app/content_fetcher.py
    # from the live page (falls back to `excerpt` if the fetch/extraction
    # fails, so this is never left blank).
    author: str
    source: str
    sourceUrl: str
    readTime: int
    image: str
    images: list[str] = []  # Any additional in-article images found while
    # fetching full content (see app/content_fetcher.py), for posts that
    # genuinely carry more than one photo. `image` is always the first/
    # cover shot and is not duplicated into this list.
    publishedAt: str
    updatedAt: str = ""  # Falls back to publishedAt when a feed has no
    # separate "updated" timestamp (true for almost all RSS 2.0 feeds).
    likes: int = 0
    views: int = 0
    tags: list[str] = []  # From <category>/<dc:subject> elements, if present.
    language: str = "en"  # From feed config today; could be detected later.
    location: str = ""  # Country/region the source covers, from feed config.
    status: Literal["draft", "published", "archived"] = "published"


class InitialMessage(BaseModel):
    type: Literal["initial"] = "initial"
    items: list[NewsItem]


class NewItemMessage(BaseModel):
    type: Literal["new_item"] = "new_item"
    item: NewsItem


class MoreItemsMessage(BaseModel):
    type: Literal["more_items"] = "more_items"
    items: list[NewsItem]
    next_cursor: int
    has_more: bool


class ClientMessage(BaseModel):
    """What we accept from the client over the WebSocket."""

    type: Literal["load_more", "set_category", "ping"]
    category: str | None = None
    cursor: int | None = None
    page_size: int | None = None
    extra: dict[str, Any] | None = None
