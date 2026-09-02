from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedConfig:
    """One publisher's RSS/Atom feed and the category it's mapped to."""

    url: str
    source: str
    category: str
    language: str = "en"
    location: str = ""  # Country/region this outlet's coverage represents.


# Real, public RSS/Atom feeds — this is the "which publishers do we pull
# from" list for the whole app. Add or remove entries freely; nothing else
# needs to change.
#
# Per request, every feed below maps to one of these seven outlets:
# NDTV, The Hindu, Indian Express, Times of India, BBC, Reuters, NYTimes.
#
# NOTE ON REUTERS: reuters.com discontinued its public RSS feeds years ago
# (confirmed still dead as of 2026 — there is no official reuters.com/*/rss
# endpoint left to poll). To still surface Reuters coverage, the entry below
# uses Google News' RSS search restricted to reuters.com articles. It's a
# widely used, currently-working workaround, but two things follow from
# that: (1) `sourceUrl` for these items is a news.google.com redirect that
# resolves to the reuters.com article rather than a raw reuters.com link,
# and (2) it's an undocumented Google endpoint, so if you need a hard
# guarantee of uptime, drop this line and the app will keep working fine
# with the other six feeds.
DEFAULT_FEEDS: list[FeedConfig] = [
    FeedConfig("https://feeds.feedburner.com/ndtvnews-top-stories", "NDTV", "India", "en", "India"),
    FeedConfig("https://www.thehindu.com/feeder/default.rss", "The Hindu", "India", "en", "India"),
    FeedConfig("https://indianexpress.com/feed/", "Indian Express", "India", "en", "India"),
    FeedConfig(
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "Times of India", "India", "en", "India"
    ),
    FeedConfig("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News", "World", "en", "United Kingdom"),
    FeedConfig(
        "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com&hl=en-IN&gl=IN&ceid=IN:en",
        "Reuters",
        "World",
        "en",
        "Global",
    ),
    FeedConfig(
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "The New York Times", "World", "en", "United States"
    ),
]


def _split_origins(raw: str) -> list[str]:
    return [o.strip() for o in raw.split(",") if o.strip()]


class Settings:
    """Environment-driven runtime configuration. Override any of these via
    a `.env` file or real environment variables — see `.env.example`."""

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    # How often (seconds) the whole feed list is re-crawled. Each new item
    # discovered mid-crawl is pushed to clients immediately — this interval
    # is "how long before we look for new items again", not a batch delay.
    refresh_interval_seconds: int = int(os.getenv("REFRESH_INTERVAL_SECONDS", "120"))

    # Size of the in-memory rolling window used only to (a) hand new
    # WebSocket connections an initial feed and (b) serve scroll/pagination.
    # This is never written to disk — it's just recent RAM state.
    buffer_size: int = int(os.getenv("BUFFER_SIZE", "300"))

    cors_origins: list[str] = _split_origins(os.getenv("CORS_ORIGINS", "*"))

    concurrent_requests: int = int(os.getenv("CONCURRENT_REQUESTS", "8"))
    concurrent_requests_per_domain: int = int(os.getenv("CONCURRENT_REQUESTS_PER_DOMAIN", "2"))
    download_delay: float = float(os.getenv("DOWNLOAD_DELAY", "0.5"))
    download_timeout: float = float(os.getenv("DOWNLOAD_TIMEOUT", "15"))

    # Optional. Adzuna (https://developer.adzuna.com) runs a free, official
    # India job-search API — real listings, not scraped — but requires a
    # free account to get an app_id/app_key pair. Left blank, the Jobs page
    # simply runs on Remotive + RemoteOK only; no crash, no fake data filling
    # the gap. See app/jobs.py for why this exists instead of pulling from
    # Naukri/Indeed/etc. directly.
    adzuna_app_id: str = os.getenv("ADZUNA_APP_ID", "")
    adzuna_app_key: str = os.getenv("ADZUNA_APP_KEY", "")
    # Adzuna country code for the search — "in" for India. See
    # https://developer.adzuna.com/docs/search for the full list of
    # countries Adzuna covers if you'd rather target a different one.
    adzuna_country: str = os.getenv("ADZUNA_COUNTRY", "in")


settings = Settings()