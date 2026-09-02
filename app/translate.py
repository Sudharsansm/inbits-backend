from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import httpx

from .config import settings

logger = logging.getLogger("translate")

# MyMemory's translation API: free, no API key required. Anonymous usage is
# rate-limited (roughly 5,000 words/day per IP) — fine for a "translate the
# story you're currently reading" feature, not for bulk-translating the
# entire live buffer up front. Every call here is best-effort: on any
# failure (network, rate limit, empty response) the original text is
# returned unchanged rather than raising, so a translation hiccup never
# breaks the page.
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"

# MyMemory's free tier caps a single query around ~500 bytes — longer text
# (a full article body) has to be split into chunks under that, translated
# piece by piece, and rejoined.
_MAX_CHUNK_CHARS = 450

# In-memory cache, never expired — a given (text, target language) pair
# always translates to the same thing, so once it's fetched there's no
# reason to spend another call (or another slice of the daily rate limit)
# translating it again.
_cache: dict[str, str] = {}
_CACHE_MAX_ENTRIES = 20_000

# Only a handful of network calls in flight at once, so translating a full
# article (many chunks) doesn't fire dozens of simultaneous requests at a
# free, shared API.
_semaphore = asyncio.Semaphore(4)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _cache_key(text: str, target: str, source: str) -> str:
    digest = hashlib.sha1(f"{source}:{target}:{text}".encode("utf-8")).hexdigest()
    return digest


def _chunk_text(text: str, limit: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Splits on sentence boundaries first, packing sentences into chunks
    under `limit` chars — keeps each chunk grammatically whole so the
    translator isn't asked to translate half a sentence. Falls back to a
    hard split for any single "sentence" that's still too long on its own
    (e.g. no punctuation at all)."""
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(sentence) <= limit:
            current = sentence
        else:
            for i in range(0, len(sentence), limit):
                chunks.append(sentence[i : i + limit])
            current = ""
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


async def _translate_chunk(client: httpx.AsyncClient, text: str, target: str, source: str) -> str:
    key = _cache_key(text, target, source)
    if key in _cache:
        return _cache[key]

    async with _semaphore:
        try:
            resp = await client.get(
                _MYMEMORY_URL,
                params={"q": text, "langpair": f"{source}|{target}"},
                timeout=settings.download_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Translate chunk failed (%s -> %s): %s", source, target, exc)
            return text

    translated = (data.get("responseData") or {}).get("translatedText")
    if not translated or not isinstance(translated, str):
        return text
    # MyMemory returns an HTML-escaped/error-flavoured string for some
    # failure modes (e.g. quota exceeded) instead of a clean HTTP error —
    # a suspiciously long response full of the word "QUOTA" is that case.
    if "MYMEMORY WARNING" in translated.upper():
        return text

    if len(_cache) < _CACHE_MAX_ENTRIES:
        _cache[key] = translated
    return translated


async def translate_text(text: str, target: str, source: str = "en") -> str:
    """Translates `text` (any length) into `target`. Never raises — worst
    case, returns `text` unchanged (per-chunk, so a partial failure on a
    long article only leaves a few sentences untranslated rather than
    losing the whole thing)."""
    text = text or ""
    if not text.strip() or target == source:
        return text

    chunks = _chunk_text(text)
    if not chunks:
        return text

    async with httpx.AsyncClient(headers={"User-Agent": "InBitsNewsBot/1.0 (+https://inbits.app)"}) as client:
        translated_chunks = await asyncio.gather(
            *(_translate_chunk(client, c, target, source) for c in chunks)
        )
    return " ".join(translated_chunks)


async def translate_many(texts: list[str], target: str, source: str = "en") -> list[str]:
    """Translates several independent strings (e.g. a title and an
    excerpt) concurrently — same semaphore/cache as `translate_text`, so
    this doesn't multiply the effective request rate."""
    if target == source:
        return list(texts)
    return list(await asyncio.gather(*(translate_text(t, target, source) for t in texts)))
