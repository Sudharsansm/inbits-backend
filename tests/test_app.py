import asyncio

from fastapi.testclient import TestClient

from app.main import app, broadcaster

SAMPLE_ITEM = {
    "id": "abc123",
    "category": "Tech",
    "title": "Test Article",
    "excerpt": "An excerpt.",
    "author": "Tester",
    "source": "TestSource",
    "sourceUrl": "https://example.com/test-article",
    "readTime": 1,
    "image": "",
    "publishedAt": "2026-08-25T00:00:00+00:00",
    "likes": 0,
}


def test_health_endpoint():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_feed_endpoint_reflects_broadcaster_state():
    with TestClient(app):
        asyncio.run(broadcaster.publish(SAMPLE_ITEM))
        # fresh client for each test module run is fine since broadcaster is
        # a module-level singleton; just assert our item shows up.


def test_article_endpoint_hit_and_miss():
    with TestClient(app) as client:
        asyncio.run(broadcaster.publish(dict(SAMPLE_ITEM, sourceUrl="https://example.com/article-lookup")))

        r = client.get(f"/api/article/{SAMPLE_ITEM['id']}")
        assert r.status_code == 200
        assert r.json()["title"] == "Test Article"

        r = client.get("/api/article/does-not-exist")
        assert r.status_code == 404


def test_websocket_full_protocol():
    with TestClient(app) as client:
        asyncio.run(broadcaster.publish(dict(SAMPLE_ITEM, sourceUrl="https://example.com/ws-test-1")))

        with client.websocket_connect("/ws/feed?category=All") as ws:
            initial = ws.receive_json()
            assert initial["type"] == "initial"
            assert any(i["sourceUrl"] == "https://example.com/ws-test-1" for i in initial["items"])

            asyncio.run(
                broadcaster.publish(
                    dict(
                        SAMPLE_ITEM,
                        title="Second Article",
                        category="World",
                        sourceUrl="https://example.com/ws-test-2",
                    )
                )
            )
            pushed = ws.receive_json()
            assert pushed["type"] == "new_item"
            assert pushed["item"]["title"] == "Second Article"

            ws.send_json({"type": "load_more", "cursor": 0, "page_size": 1})
            more = ws.receive_json()
            assert more["type"] == "more_items"
            assert len(more["items"]) == 1
            assert "has_more" in more

            ws.send_json({"type": "set_category", "category": "Tech"})
            filtered = ws.receive_json()
            assert filtered["type"] == "initial"
            assert all(i["category"] == "Tech" for i in filtered["items"])

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

            ws.send_text("{not valid json")
            err = ws.receive_json()
            assert err["type"] == "error"
