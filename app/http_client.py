from __future__ import annotations

import httpx

# One pooled, keep-alive HTTP client shared across the whole process,
# instead of every fetch (crawl cycle, live search, job board poll,
# per-article content fetch) opening and tearing down its own
# httpx.AsyncClient. Under real traffic that difference matters a lot:
# a fresh client means a fresh TCP + TLS handshake for every single
# outbound request, which is pure added latency and file-descriptor
# churn that serves nobody. Reusing one pooled client lets httpx keep
# connections to the same publishers (feeds.bbci.co.uk, etc.) warm
# across requests, which is the single biggest "make outbound fetches
# faster under load" win available without touching the app's actual
# architecture.
#
# Limits are generous but bounded — this process talks to a modest,
# fixed set of publisher domains, so a large pool doesn't buy much, but
# an *unbounded* one risks piling up connections under a burst of
# concurrent user requests (search, job listing, article opens).
_limits = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """The shared client. Created lazily on first use (not at import
    time) so it's always bound to the event loop that's actually
    running, and reused for the lifetime of the process after that."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=_limits,
            headers={"User-Agent": "InBitsNewsBot/1.0 (+https://inbits.app)"},
        )
    return _client


async def close_http_client() -> None:
    """Called once from the FastAPI lifespan on shutdown so connections
    are closed cleanly instead of left dangling when the process exits."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
