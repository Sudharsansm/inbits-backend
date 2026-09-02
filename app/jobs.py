from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .config import settings
from .content_fetcher import _clean_text  # reuse the same whitespace cleanup
from .http_client import get_http_client

logger = logging.getLogger("jobs")

# Real, free, no-key-required public job board APIs. Remotive + RemoteOK
# cover remote-first roles; Adzuna (optional — needs a free API key, see
# config.py) adds real on-site/hybrid listings for a specific country
# (India by default).
#
# Naukri, Indeed, Foundit, Instahyre, FlexJobs, Internshala, and
# SurelyRemote are NOT fetched here: none of them offer a free, official,
# public API for pulling search results. The only "APIs" for those are
# unofficial services that scrape their internal endpoints without
# permission, which breaks their Terms of Service, is fragile (gets
# IP-blocked, breaks on any markup change), and carries real legal risk.
# That's not something this app does. Those seven are instead offered as
# real "search on their site" deep links in the frontend — see
# lib/externalJobBoards.ts.
_REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
_REMOTEOK_URL = "https://remoteok.com/api"
_ADZUNA_URL_TMPL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

_TAG_RE = re.compile(r"<[^>]+>")
_JOB_TYPE_LABELS = {
    "full_time": "Full-time",
    "part_time": "Part-time",
    "contract": "Contract",
    "internship": "Internship",
    "freelance": "Freelance",
}
_ADZUNA_COUNTRY_NAMES = {
    "in": "India",
    "gb": "United Kingdom",
    "us": "United States",
    "au": "Australia",
    "ca": "Canada",
    "de": "Germany",
    "fr": "France",
    "sg": "Singapore",
    "nl": "Netherlands",
    "za": "South Africa",
    "nz": "New Zealand",
}
_ADZUNA_CURRENCY_SYMBOLS = {"in": "₹", "gb": "£", "us": "$", "au": "A$", "ca": "C$", "de": "€", "fr": "€", "nl": "€"}

# Refetching on every request would be both slow and unfriendly to a free
# public API — cache the normalized list for a while, same spirit as the
# news broadcaster's in-memory buffer.
_CACHE_TTL_SECONDS = 900
_cache: dict[str, Any] = {"items": [], "fetched_at": 0.0}
_cache_lock = asyncio.Lock()

# Coarse, best-effort mapping from the free-text "candidate location"
# string these boards use down to a short list of countries/regions a
# filter dropdown can actually work with. Order matters — checked in
# order, first match wins — and anything that matches nothing keeps its
# own (trimmed) text as its own filter option rather than being dropped,
# so real listings never disappear just because they didn't fit a bucket.
_COUNTRY_PATTERNS: list[tuple[str, str]] = [
    (r"\bworldwide\b|\banywhere\b", "Worldwide"),
    (r"\bunited states\b|\busa\b|\bu\.s\.a?\.?\b|\bus only\b", "United States"),
    (r"\bunited kingdom\b|\buk\b|\bu\.k\.\b", "United Kingdom"),
    (r"\bcanada\b", "Canada"),
    (r"\baustralia\b", "Australia"),
    (r"\bindia\b", "India"),
    (r"\bgermany\b", "Germany"),
    (r"\bnetherlands\b", "Netherlands"),
    (r"\bfrance\b", "France"),
    (r"\bspain\b", "Spain"),
    (r"\bportugal\b", "Portugal"),
    (r"\bsingapore\b", "Singapore"),
    (r"\bbrazil\b", "Brazil"),
    (r"\bmexico\b", "Mexico"),
    (r"\beurope\b|\bemea\b", "Europe"),
    (r"\blatam\b|\blatin america\b", "Latin America"),
    (r"\bapac\b|\basia\b", "Asia"),
]


def _infer_country(location: str) -> str:
    text = (location or "").strip()
    if not text:
        return "Worldwide"
    lowered = text.lower()
    for pattern, label in _COUNTRY_PATTERNS:
        if re.search(pattern, lowered):
            return label
    # No known bucket matched — keep the board's own text (trimmed to the
    # first clause) rather than losing the listing or mislabeling it.
    return text.split(",")[0].split(";")[0].strip()[:40] or "Worldwide"


def _infer_workplace(*texts: str, default: str = "Remote") -> str:
    """`default` should match what's actually typical of the source board:
    Remotive/RemoteOK are remote-first (default "Remote"), while Adzuna is
    a general market aggregator (default "On-site") — only relabel when
    the listing itself explicitly says otherwise, either way."""
    blob = " ".join(t or "" for t in texts).lower()
    if "hybrid" in blob:
        return "Hybrid"
    if "on-site" in blob or "onsite" in blob or "in office" in blob or "in-office" in blob:
        return "On-site"
    if "remote" in blob or "work from home" in blob or re.search(r"\bwfh\b", blob):
        return "Remote"
    return default


def _initials(company: str) -> str:
    words = [w for w in re.split(r"\s+", company.strip()) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _html_to_text(html: str) -> str:
    return _clean_text(_TAG_RE.sub(" ", html or ""))


def _extract_bullets(html: str, *, limit: int = 6) -> list[str]:
    """Descriptions are raw HTML with <li> bullet lists for
    responsibilities/requirements — pull those out so the job page can
    show real structured content instead of a wall of text."""
    items = re.findall(r"<li[^>]*>(.*?)</li>", html or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = [_html_to_text(i) for i in items]
    return [c for c in cleaned if c][:limit]


def _normalize_remotive(raw: dict[str, Any]) -> dict[str, Any]:
    description_html = raw.get("description", "") or ""
    bullets = _extract_bullets(description_html)
    about = _html_to_text(description_html)[:600]
    job_type = _JOB_TYPE_LABELS.get(raw.get("job_type", ""), (raw.get("job_type") or "Full-time").replace("_", " ").title())
    location = raw.get("candidate_required_location") or "Remote"
    category = raw.get("category") or "General"

    return {
        "id": f"remotive-{raw['id']}",
        "title": raw.get("title", "Untitled role"),
        "company": raw.get("company_name", "Unknown company"),
        "location": location,
        "country": _infer_country(location),
        "workplaceType": _infer_workplace(location, raw.get("title", ""), default="Remote"),
        "type": job_type,
        "category": category,
        "salary": raw.get("salary") or "Not disclosed",
        "logo": _initials(raw.get("company_name", "?")),
        "logoUrl": raw.get("company_logo") or raw.get("company_logo_url") or "",
        "posted": raw.get("publication_date", ""),
        "tags": (raw.get("tags") or [])[:6],
        "applyUrl": raw.get("url", ""),
        "about": about or "No description provided.",
        # Remotive doesn't separate these from a free-text description the
        # way a curated listing would — reusing the same bullet list for
        # both is honest about that rather than inventing a split.
        "responsibilities": bullets,
        "requirements": bullets,
        "perks": [job_type, location, category],
        "source": "Remotive",
    }


def _guess_job_type(tags: list[str], title: str) -> str:
    blob = " ".join([*tags, title]).lower()
    if "intern" in blob:
        return "Internship"
    if "part time" in blob or "part-time" in blob:
        return "Part-time"
    if "contract" in blob or "freelance" in blob:
        return "Contract"
    return "Full-time"


def _normalize_remoteok(raw: dict[str, Any]) -> dict[str, Any] | None:
    job_id = raw.get("id")
    title = raw.get("position") or raw.get("title")
    if not job_id or not title:
        return None  # first element of the RemoteOK feed is a legal notice, not a job

    description_html = raw.get("description", "") or ""
    bullets = _extract_bullets(description_html)
    about = _html_to_text(description_html)[:600]
    company = raw.get("company") or "Unknown company"
    location = raw.get("location") or "Worldwide"
    tags = [t for t in (raw.get("tags") or []) if isinstance(t, str)][:6]
    job_type = _guess_job_type(tags, title)
    category = tags[0].title() if tags else "General"

    salary_min, salary_max = raw.get("salary_min"), raw.get("salary_max")
    salary = f"${salary_min:,} – ${salary_max:,}" if salary_min and salary_max else "Not disclosed"

    return {
        "id": f"remoteok-{job_id}",
        "title": title,
        "company": company,
        "location": location,
        "country": _infer_country(location),
        "workplaceType": _infer_workplace(location, title, default="Remote"),
        "type": job_type,
        "category": category,
        "salary": salary,
        "logo": _initials(company),
        "logoUrl": raw.get("company_logo") or raw.get("logo") or "",
        "posted": raw.get("date", ""),
        "tags": tags,
        "applyUrl": raw.get("apply_url") or raw.get("url") or "",
        "about": about or "No description provided.",
        "responsibilities": bullets,
        "requirements": bullets,
        "perks": [job_type, location, category],
        "source": "RemoteOK",
    }


def _normalize_adzuna(raw: dict[str, Any], *, country_code: str) -> dict[str, Any] | None:
    job_id = raw.get("id")
    title = raw.get("title")
    if not job_id or not title:
        return None

    description = raw.get("description", "") or ""
    bullets = _extract_bullets(description)
    about = _html_to_text(description)[:600]
    company = (raw.get("company") or {}).get("display_name") or "Unknown company"
    location = (raw.get("location") or {}).get("display_name") or _ADZUNA_COUNTRY_NAMES.get(
        country_code, country_code.upper()
    )
    category = (raw.get("category") or {}).get("label") or "General"

    contract_time = raw.get("contract_time")
    contract_type = raw.get("contract_type")
    if contract_time == "part_time":
        job_type = "Part-time"
    elif contract_type == "contract":
        job_type = "Contract"
    else:
        job_type = "Full-time"

    salary_min, salary_max = raw.get("salary_min"), raw.get("salary_max")
    currency = _ADZUNA_CURRENCY_SYMBOLS.get(country_code, "")
    salary = (
        f"{currency}{salary_min:,.0f} – {currency}{salary_max:,.0f}"
        if salary_min and salary_max
        else "Not disclosed"
    )

    return {
        "id": f"adzuna-{job_id}",
        "title": title,
        "company": company,
        "location": location,
        "country": _ADZUNA_COUNTRY_NAMES.get(country_code, country_code.upper()),
        # Adzuna is a general market aggregator, not a remote-jobs board —
        # "On-site" is the honest default unless the listing itself says
        # otherwise, unlike Remotive/RemoteOK above.
        "workplaceType": _infer_workplace(location, title, description, default="On-site"),
        "type": job_type,
        "category": category,
        "salary": salary,
        "logo": _initials(company),
        "logoUrl": "",
        "posted": raw.get("created", ""),
        "tags": [category] if category else [],
        "applyUrl": raw.get("redirect_url", ""),
        "about": about or "No description provided.",
        "responsibilities": bullets,
        "requirements": bullets,
        "perks": [job_type, location, category],
        "source": "Adzuna",
    }


async def _fetch_remotive(limit: int = 80) -> list[dict[str, Any]]:
    try:
        client = get_http_client()
        resp = await client.get(_REMOTIVE_URL, params={"limit": limit}, timeout=settings.download_timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Remotive fetch failed: %s", exc)
        return []

    raw_jobs = data.get("jobs", [])
    return [_normalize_remotive(j) for j in raw_jobs if j.get("id") and j.get("title")]


async def _fetch_remoteok(limit: int = 80) -> list[dict[str, Any]]:
    try:
        client = get_http_client()
        resp = await client.get(_REMOTEOK_URL, timeout=settings.download_timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("RemoteOK fetch failed: %s", exc)
        return []

    if not isinstance(data, list):
        return []
    normalized = [_normalize_remoteok(j) for j in data if isinstance(j, dict)]
    return [j for j in normalized if j][:limit]


async def _fetch_adzuna(limit: int = 80) -> list[dict[str, Any]]:
    """Real, official Adzuna listings — skipped entirely (not an error)
    when no API key is configured, so the app works fine without it."""
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        return []

    country_code = (settings.adzuna_country or "in").lower()
    url = _ADZUNA_URL_TMPL.format(country=country_code)
    try:
        client = get_http_client()
        resp = await client.get(
            url,
            params={
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "results_per_page": min(limit, 50),
                "content-type": "application/json",
            },
            timeout=settings.download_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Adzuna fetch failed: %s", exc)
        return []

    raw_jobs = data.get("results", [])
    normalized = [_normalize_adzuna(j, country_code=country_code) for j in raw_jobs]
    return [j for j in normalized if j][:limit]


def _india_relevance(job: dict[str, Any]) -> int:
    """0 = explicitly India, 1 = Worldwide (open to India too), 2 =
    everything else. Used only to order the default list — nothing is
    ever dropped, every job stays reachable through the filters."""
    country = job.get("country", "")
    if country == "India":
        return 0
    if country == "Worldwide":
        return 1
    return 2


async def get_jobs(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Real, currently-open job listings from independent public job
    boards (Remotive + RemoteOK always; Adzuna too if configured) —
    cached in memory for `_CACHE_TTL_SECONDS` so every page load doesn't
    hit any upstream directly. Sorted (not filtered) so India/Worldwide
    roles show first by default — see _india_relevance."""
    async with _cache_lock:
        stale = (time.time() - _cache["fetched_at"]) > _CACHE_TTL_SECONDS
        if force_refresh or stale or not _cache["items"]:
            remotive, remoteok, adzuna = await asyncio.gather(
                _fetch_remotive(), _fetch_remoteok(), _fetch_adzuna()
            )
            fresh = remotive + remoteok + adzuna
            if fresh:  # keep the previous list on a failed refresh rather than going empty
                fresh.sort(key=_india_relevance)
                _cache["items"] = fresh
                _cache["fetched_at"] = time.time()
        return list(_cache["items"])


async def get_job(job_id: str) -> dict[str, Any] | None:
    for job in await get_jobs():
        if job["id"] == job_id:
            return job
    return None