import bitscrape
import pytest

from app.config import FeedConfig
from app.spiders.news_spider import NewsFeedSpider

RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>Sample Feed</title>
  <item>
    <title>Scientists discover new exoplanet &amp; it's huge</title>
    <link>https://example.com/news/exoplanet</link>
    <description><![CDATA[<p>A team of astronomers announced <b>today</b> the discovery of a new exoplanet twice the size of Jupiter.</p>]]></description>
    <pubDate>Mon, 25 Aug 2026 09:30:00 GMT</pubDate>
    <author>jane@example.com (Jane Doe)</author>
    <media:thumbnail url="https://example.com/img/exoplanet.jpg" />
  </item>
  <item>
    <title>Local bakery wins award</title>
    <link>https://example.com/news/bakery</link>
    <description>A small neighborhood bakery took home the top prize.</description>
    <pubDate>Sun, 24 Aug 2026 14:00:00 GMT</pubDate>
  </item>
</channel>
</rss>
"""

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Sample</title>
  <entry>
    <title>Atom-based article title</title>
    <link rel="alternate" href="https://example.com/atom/article-1" />
    <summary>This is a short summary of the atom article.</summary>
    <published>2026-08-25T08:00:00Z</published>
    <author><name>Atom Author</name></author>
  </entry>
</feed>
"""


def _make_spider() -> NewsFeedSpider:
    feeds = [
        FeedConfig("https://example.com/rss.xml", "Example Source", "Tech"),
        FeedConfig("https://example.com/atom.xml", "Atom Source", "World"),
    ]
    return NewsFeedSpider(feeds=feeds, settings=bitscrape.Settings())


def _parsed(url: str, body: bytes, meta: dict) -> "bitscrape.ParsedResponse":
    req = bitscrape.Request(url=url, meta=meta)
    resp = bitscrape.Response(url=url, status=200, body=body, request=req)
    return bitscrape.ParsedResponse(resp)


@pytest.mark.asyncio
async def test_rss_parsing_produces_expected_items():
    spider = _make_spider()
    parsed = _parsed(
        "https://example.com/rss.xml", RSS_SAMPLE, {"source": "Example Source", "category": "Tech"}
    )

    items = [item async for item in spider.parse(parsed)]

    assert len(items) == 2
    first, second = items

    assert first["title"] == "Scientists discover new exoplanet & it's huge"
    assert first["sourceUrl"] == "https://example.com/news/exoplanet"
    assert first["image"] == "https://example.com/img/exoplanet.jpg"
    assert first["category"] == "Tech"
    assert first["author"] == "jane@example.com (Jane Doe)"
    assert "astronomers announced" in first["excerpt"]
    assert first["publishedAt"].startswith("2026-08-25")
    assert first["likes"] == 0
    assert first["readTime"] >= 1

    # No <media:thumbnail>/<enclosure> present -> empty string, not a crash.
    assert second["image"] == ""
    assert second["author"] == "Example Source"  # falls back to source name


@pytest.mark.asyncio
async def test_atom_parsing_produces_expected_items():
    spider = _make_spider()
    parsed = _parsed(
        "https://example.com/atom.xml", ATOM_SAMPLE, {"source": "Atom Source", "category": "World"}
    )

    items = [item async for item in spider.parse(parsed)]

    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Atom-based article title"
    assert item["sourceUrl"] == "https://example.com/atom/article-1"
    assert item["category"] == "World"
    assert item["publishedAt"].startswith("2026-08-25")


@pytest.mark.asyncio
async def test_malformed_xml_yields_nothing_without_raising():
    spider = _make_spider()
    parsed = _parsed("https://example.com/bad.xml", b"not xml <<<", {"source": "Bad", "category": "X"})

    items = [item async for item in spider.parse(parsed)]

    assert items == []


@pytest.mark.asyncio
async def test_entries_missing_title_or_link_are_skipped():
    spider = _make_spider()
    body = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>No link here</title></item>
      <item><link>https://example.com/no-title</link></item>
    </channel></rss>
    """
    parsed = _parsed("https://example.com/rss.xml", body, {"source": "S", "category": "C"})

    items = [item async for item in spider.parse(parsed)]

    assert items == []
