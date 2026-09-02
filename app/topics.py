from __future__ import annotations

import re

# Ordered so more specific/less ambiguous topics are checked first (e.g. a
# headline mentioning both "market" and "election" reads as Politics, not
# Business, since election coverage is the more specific match here).
_TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "Sports",
        (
            "cricket", "football", "soccer", "olympic", "tournament", "match",
            "wicket", "goal", "tennis", "hockey", "athlete", "medal", "championship",
            "ipl", "fifa", "nba", "premier league",
        ),
    ),
    (
        "Technology",
        (
            "ai ", "artificial intelligence", "startup", "app ", "software",
            "smartphone", "chip", "semiconductor", "cybersecurity", "tech ",
            "google", "microsoft", "apple ", "meta ", "openai", "robot",
        ),
    ),
    (
        "Business",
        (
            "market", "stocks", "economy", "inflation", "trade", "rupee",
            "dollar", "gdp", "ipo", "merger", "earnings", "investment",
            "bank ", "startup funding", "revenue",
        ),
    ),
    (
        "Politics",
        (
            "election", "parliament", "minister", "president", "senate",
            "government", "policy", "vote", "campaign", "congress", "diplomat",
            "supreme court", "legislation",
        ),
    ),
    (
        "Health",
        ("health", "hospital", "vaccine", "disease", "covid", "doctor", "medical", "who "),
    ),
    (
        "Science",
        ("nasa", "space", "research", "study finds", "climate", "scientist", "discovery"),
    ),
    (
        "Entertainment",
        ("film", "movie", "box office", "actor", "actress", "music", "celebrity", "netflix", "bollywood"),
    ),
]

_WORD_BOUNDARY = re.compile(r"[^a-z0-9]+")


def classify_topic(title: str, excerpt: str, tags: list[str] | None = None) -> str:
    """Best-effort subject classification from real article text — this is
    what groups the live feed into Journal "playlists"/categories, since
    the crawler's `category` field only distinguishes India/World (i.e.
    where a source is based, not what a story is about)."""
    haystack = " " + _WORD_BOUNDARY.sub(" ", f"{title} {excerpt} {' '.join(tags or [])}".lower()) + " "
    for topic, keywords in _TOPIC_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return topic
    return "General"
