from __future__ import annotations

import re
from urllib.parse import urlparse

# Domains that exist to serve or redirect through ads/affiliate links —
# if an item's source URL resolves through one of these, it's not an
# editorial article no matter what its title says.
_AD_DOMAINS = {
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "taboola.com",
    "outbrain.com",
    "criteo.com",
    "amazon-adsystem.com",
    "pubmatic.com",
    "media.net",
    "adroll.com",
    "revcontent.com",
    "mgid.com",
    "zergnet.com",
}

# Phrases publishers are required to disclose sponsored/paid content
# with — a real news story doesn't open by calling itself an ad.
_AD_PHRASE_RE = re.compile(
    r"\b(sponsored content|sponsored post|paid partnership|paid post|"
    r"advertorial|advertisement feature|in partnership with|promoted content|"
    r"presented by|brand voice|affiliate disclosure|shop this (story|post)|"
    r"use code [a-z0-9]+ for|% off.{0,20}(use code|shop now)|buy now.{0,20}limited time)\b",
    re.IGNORECASE,
)

# A bare "Advertisement" / "Sponsored" label as the entire title or the
# entire excerpt (rather than a phrase from a real headline) is a strong
# signal on its own.
_AD_LABEL_ONLY_RE = re.compile(r"^\s*(advertisement|sponsored|promoted|ad)\s*[:\-–]?\s*$", re.IGNORECASE)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_advertisement(item: dict) -> bool:
    """True if `item` looks like an ad/sponsored placement rather than an
    editorial story — used to drop it from the live feed and from search
    results entirely (see crawler.py / search.py). This app doesn't run
    ads at all yet, so anything that reads as one is noise, not content."""
    host = _host(item.get("sourceUrl", ""))
    if any(host == d or host.endswith(f".{d}") for d in _AD_DOMAINS):
        return True

    title = (item.get("title") or "").strip()
    excerpt = (item.get("excerpt") or "").strip()
    content = (item.get("content") or "").strip()

    if _AD_LABEL_ONLY_RE.match(title):
        return True

    haystack = f"{title} {excerpt} {content[:500]}"
    if _AD_PHRASE_RE.search(haystack):
        return True

    tags = [str(t).lower() for t in (item.get("tags") or [])]
    if any(t in ("sponsored", "advertisement", "promoted", "advertorial") for t in tags):
        return True

    return False
