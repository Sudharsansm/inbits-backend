from __future__ import annotations

import asyncio
import logging
import re

import httpx
from lxml import html as lxml_html

from .http_client import get_http_client

logger = logging.getLogger("content_fetcher")

_WS_RE = re.compile(r"\s+")

# Lines publisher templates commonly leave behind inside the article body
# itself (share prompts, newsletter pitches, related-reading callouts,
# byline/copyright boilerplate) — these aren't part of the story, so they're
# dropped so the reading view only ever shows the actual article text.
_BOILERPLATE_RE = re.compile(
    r"^(also read|read more|read next|related|advertisement|subscribe|"
    r"sign up|follow us|share this|click here|watch|listen|"
    r"copyright \u00a9|all rights reserved|for more (news|updates)|"
    r"download the .* app|this story (has been|was) published)\b",
    re.IGNORECASE,
)

# Tags whose text is never part of the article body.
_JUNK_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "form",
    "aside",
    "figure",
    "iframe",
    "button",
}

# A handful of RSS-feed dependent selectors is not a real fit here — this
# picks the largest text block, which works reasonably well across most
# publisher templates without per-source scraping rules.
_CANDIDATE_SELECTORS = [
    "article",
    "[itemprop='articleBody']",
    "main",
    ".article-body",
    ".story-body",
    "#content",
]


def _clean_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _is_boilerplate(paragraph: str) -> bool:
    if _BOILERPLATE_RE.match(paragraph.strip()):
        return True
    # A "paragraph" that's just a handful of words with no sentence
    # punctuation is almost always a stray caption, byline, or nav label
    # that slipped through the container selector, not real body text.
    words = paragraph.split()
    if len(words) <= 4 and not any(c in paragraph for c in ".?!"):
        return True
    return False


def _drop_boilerplate(paragraphs: list[str]) -> list[str]:
    return [p for p in paragraphs if p and not _is_boilerplate(p)]


def _resolve_url(src: str, base_url: str) -> str:
    """Normalizes a scraped image URL to an absolute https(-preferring)
    URL: protocol-relative (`//host/...`) and root-relative (`/path`)
    URLs are resolved against the page they came from, same as a browser
    would resolve them."""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        from urllib.parse import urljoin

        return urljoin(base_url, src)
    return src


def _first_srcset_url(srcset: str) -> str:
    """`srcset="a.jpg 480w, b.jpg 960w"` -> "a.jpg". Several publisher
    templates only lazy-load via `srcset`/`data-srcset` and leave `src`
    empty or pointing at a 1x1 placeholder, so this is checked as a
    fallback wherever `src`/`data-src` comes up empty."""
    first = srcset.strip().split(",")[0].strip()
    return first.split(" ")[0] if first else ""


def _extract_og_image(tree, base_url: str) -> str:
    """The single most reliable, universal source for an article's lead
    photo: virtually every modern publisher sets `og:image` (or the
    Twitter Card / `link rel="image_src"` equivalent) in `<head>` for
    social-sharing previews, and — unlike the visible page body — these
    tags are almost always present in the raw server-rendered HTML even
    on JS-heavy sites this scraper can't otherwise render.

    This exists because relying on RSS's own image fields (see
    `_find_image` in spiders/news_spider.py) plus scanning the article
    body for <img> tags (`_extract_images_from_tree` below) still misses
    a lot of real articles: many RSS feeds carry no image data at all,
    and a lot of publisher templates put the hero/featured image in a
    <figure> or header block outside whichever container the body-text
    selectors matched. og:image is checked first for exactly that gap —
    see crawler.py, which uses this to fill `item["image"]` whenever the
    RSS feed didn't already provide one.
    """
    for xpath in (
        "//meta[@property='og:image']/@content",
        "//meta[@property='og:image:secure_url']/@content",
        "//meta[@name='twitter:image']/@content",
        "//meta[@name='twitter:image:src']/@content",
        "//link[@rel='image_src']/@href",
    ):
        for raw in tree.xpath(xpath):
            src = _resolve_url(raw, base_url)
            if src and not src.startswith("data:"):
                return src
    return ""


def _extract_images_from_tree(tree, base_url: str, *, limit: int = 4) -> list[str]:
    """Pulls extra in-article photos (not the RSS cover image, and not
    the og:image lead photo handled separately by `_extract_og_image`)
    so posts that genuinely carry a photo set can be shown like a
    multi-image Instagram post instead of just their single lead image.
    """
    seen: list[str] = []
    for selector in _CANDIDATE_SELECTORS:
        try:
            nodes = tree.cssselect(selector)
        except Exception:
            nodes = []
        if not nodes:
            continue
        node = max(nodes, key=lambda n: len("".join(n.itertext())))
        for img in node.findall(".//img"):
            src = img.get("data-src") or img.get("src") or ""
            src = src.strip()
            if not src or src.startswith("data:"):
                # Lazy-loaded images often leave src/data-src empty (or a
                # blank placeholder) and only carry the real URL in a
                # srcset variant -- check those before giving up on this
                # <img> entirely.
                src = _first_srcset_url(img.get("data-srcset") or img.get("srcset") or "")
            if not src or src.startswith("data:"):
                continue
            src = _resolve_url(src, base_url)
            if src not in seen:
                seen.append(src)
            if len(seen) >= limit:
                break
        if seen:
            break
    return seen


def _extract_from_tree(tree) -> str:
    for selector in _CANDIDATE_SELECTORS:
        try:
            nodes = tree.cssselect(selector)
        except Exception:
            nodes = []
        if not nodes:
            continue
        node = max(nodes, key=lambda n: len("".join(n.itertext())))
        for junk in node.iter(*_JUNK_TAGS):
            junk.drop_tree()
        paragraphs = [_clean_text(p.text_content()) for p in node.findall(".//p")]
        paragraphs = _drop_boilerplate(paragraphs)
        if paragraphs:
            return "\n\n".join(paragraphs)

    # Fallback: every <p> on the page, in case no container matched.
    paragraphs = [_clean_text(p.text_content()) for p in tree.findall(".//p")]
    body = "\n\n".join(_drop_boilerplate(paragraphs))
    if body:
        return body

    # Last resort: the single element (div/section) with the most text of
    # any on the page. Publisher markup varies wildly, and this catches
    # articles that don't use any of the common selectors above — this is
    # what keeps "full article unavailable" cases rare rather than common.
    best_text = ""
    for el in tree.iter("div", "section"):
        if el.tag in _JUNK_TAGS:
            continue
        text = _clean_text(" ".join(el.itertext()))
        if len(text) > len(best_text):
            best_text = text
    return best_text if len(best_text.split()) >= 30 else ""


async def fetch_full_content(
    url: str,
    *,
    fallback: str = "",
    timeout: float = 10.0,
    max_chars: int = 20_000,
) -> str:
    """Best-effort fetch of an article's full body text — see
    `fetch_full_content_and_images` for the version that also returns
    extra in-article images. This wrapper exists for callers (e.g.
    app/search.py) that only need the text."""
    content, _images, _og_image = await fetch_full_content_and_images(
        url, fallback=fallback, timeout=timeout, max_chars=max_chars
    )
    return content


async def fetch_full_content_and_images(
    url: str,
    *,
    fallback: str = "",
    timeout: float = 10.0,
    max_chars: int = 20_000,
) -> tuple[str, list[str], str]:
    """Best-effort fetch of an article's full body text, any extra
    in-article photos, and the page's og:image lead photo, all from its
    live page.

    Never raises — on any network error, timeout, or empty extraction this
    returns `(fallback, [], "")` (normally the RSS excerpt, no extra
    images, no lead image) instead, since a slow or blocked publisher
    should never break the crawl or a single item.
    """
    try:
        client = get_http_client()
        resp = await client.get(url, follow_redirects=True, timeout=timeout)
        resp.raise_for_status()
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("content fetch failed for %s: %s", url, exc)
        return fallback, [], ""

    try:
        tree = lxml_html.fromstring(resp.text)
        body = _extract_from_tree(tree)
        images = _extract_images_from_tree(tree, str(resp.url))
        og_image = _extract_og_image(tree, str(resp.url))
    except Exception as exc:  # malformed HTML, parser edge cases, etc.
        logger.debug("content extraction failed for %s: %s", url, exc)
        return fallback, [], ""

    if not body:
        return fallback, images, og_image
    return body[:max_chars], images, og_image