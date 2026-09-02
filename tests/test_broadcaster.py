import pytest

from app.broadcaster import Broadcaster


class FakeWebSocket:
    """Minimal stand-in for fastapi.WebSocket, just enough for Broadcaster."""

    def __init__(self):
        self.accepted = False
        self.sent: list[str] = []
        self.fail = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_publish_routes_by_category_and_dedupes():
    b = Broadcaster(buffer_size=5)
    ws_all = FakeWebSocket()
    ws_tech = FakeWebSocket()
    await b.connect(ws_all, category="All")
    await b.connect(ws_tech, category="Tech")

    item_tech = {"sourceUrl": "https://x/1", "category": "Tech", "title": "t1"}
    item_world = {"sourceUrl": "https://x/2", "category": "World", "title": "t2"}

    await b.publish(item_tech)
    await b.publish(item_world)
    await b.publish(item_tech)  # duplicate URL, should be ignored

    assert len(ws_all.sent) == 2  # sees everything
    assert len(ws_tech.sent) == 1  # only Tech items

    snap_all = await b.snapshot("All")
    assert len(snap_all) == 2
    snap_tech = await b.snapshot("Tech")
    assert len(snap_tech) == 1
    assert snap_tech[0]["title"] == "t1"


@pytest.mark.asyncio
async def test_buffer_is_bounded():
    b = Broadcaster(buffer_size=5)
    for i in range(10):
        await b.publish({"sourceUrl": f"https://x/{i}", "category": "World", "title": f"e{i}"})

    snap = await b.snapshot("All")
    assert len(snap) == 5


@pytest.mark.asyncio
async def test_get_by_id_finds_and_misses():
    b = Broadcaster(buffer_size=5)
    await b.publish({"id": "abc123", "sourceUrl": "https://x/1", "category": "Tech", "title": "t1"})

    found = await b.get_by_id("abc123")
    assert found is not None
    assert found["title"] == "t1"

    assert await b.get_by_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_disconnect_removes_client():
    b = Broadcaster()
    ws = FakeWebSocket()
    await b.connect(ws)
    assert await b.client_count() == 1
    b.disconnect(ws)
    assert await b.client_count() == 0


@pytest.mark.asyncio
async def test_send_failure_prunes_stale_client_without_raising():
    b = Broadcaster()
    good = FakeWebSocket()
    bad = FakeWebSocket()
    bad.fail = True
    await b.connect(good)
    await b.connect(bad)

    await b.publish({"sourceUrl": "https://x/1", "category": "World", "title": "t"})

    assert len(good.sent) == 1
    assert await b.client_count() == 1  # the failing client was pruned
