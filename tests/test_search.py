import pytest

from app.broadcaster import Broadcaster
from app.search import search_buffer


def _item(**overrides):
    base = {
        "id": "1",
        "sourceUrl": "https://x/1",
        "category": "India",
        "title": "Chip factory opens in Gujarat",
        "excerpt": "A new semiconductor plant begins operations.",
        "source": "NDTV",
        "tags": ["Technology", "Business"],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_search_buffer_matches_title_case_insensitively():
    b = Broadcaster(buffer_size=10)
    await b.publish(_item())
    results = await search_buffer(b, "CHIP")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_search_buffer_matches_tags_and_source():
    b = Broadcaster(buffer_size=10)
    await b.publish(_item())
    assert len(await search_buffer(b, "technology")) == 1
    assert len(await search_buffer(b, "ndtv")) == 1


@pytest.mark.asyncio
async def test_search_buffer_no_match_returns_empty():
    b = Broadcaster(buffer_size=10)
    await b.publish(_item())
    assert await search_buffer(b, "cricket") == []


@pytest.mark.asyncio
async def test_search_buffer_blank_query_returns_empty():
    b = Broadcaster(buffer_size=10)
    await b.publish(_item())
    assert await search_buffer(b, "   ") == []
