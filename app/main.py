from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ValidationError

from .broadcaster import Broadcaster
from .config import DEFAULT_FEEDS, settings
from .crawler import crawl_loop
from .http_client import close_http_client
from .jobs import get_job, get_jobs
from .models import ClientMessage
from .search import search as search_news
from .translate import translate_many

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

broadcaster = Broadcaster(buffer_size=settings.buffer_size)
_stop_event = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _stop_event.clear()
    task = asyncio.create_task(crawl_loop(broadcaster, _stop_event))
    logger.info(
        "Startup: crawling %d feeds every %ss",
        len(DEFAULT_FEEDS),
        settings.refresh_interval_seconds,
    )
    try:
        yield
    finally:
        _stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await close_http_client()
        logger.info("Shutdown: crawl loop stopped")


app = FastAPI(title="InBits News Backend", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The feed/search/jobs responses are JSON arrays of full articles — often
# tens to hundreds of KB per request. Compressing those in transit is one
# of the cheapest wins available for "handle more users, faster": less
# data on the wire means faster responses under load and lower bandwidth
# cost per request, with no change to how any endpoint behaves.
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "connected_clients": await broadcaster.client_count()}


@app.get("/api/feed")
async def get_feed(response: Response, category: str = Query("All")) -> dict:
    """Non-WebSocket fallback: current in-memory snapshot, optionally
    filtered by category. Nothing here is read from a database — it's the
    same rolling RAM buffer the WebSocket clients see."""
    items = await broadcaster.snapshot(category)
    # A short, shared cache window: real-time freshness for any one
    # article still comes from the WebSocket (or a manual pull-to-
    # refresh) — this endpoint only needs to survive a burst of many
    # people loading the same page/category within the same few seconds
    # (e.g. a route SSR-loading on every request) without every one of
    # them hitting the broadcaster and re-serializing the same snapshot.
    # `stale-while-revalidate` lets a shared cache (CDN, browser) keep
    # serving the last snapshot instantly while it quietly refetches.
    response.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=30"
    return {"items": items, "total": len(items)}


@app.get("/api/article/{article_id}")
async def get_article(article_id: str, response: Response) -> dict:
    """Single-article lookup for permalink pages (e.g. the frontend's
    /post/:id route). Searches the same in-memory buffer as everything
    else — see `Broadcaster.get_by_id` for the one real limitation that
    follows from that (no persistence beyond the rolling window)."""
    item = await broadcaster.get_by_id(article_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Article not found")
    # A published article's own content never changes after the fact —
    # only "does this id still exist in the rolling buffer" can. Safe to
    # let a shared cache (nginx, CDN, browser) hold onto it for longer
    # than the feed snapshot: many readers opening the same trending link
    # at once now costs one broadcaster lookup, not one per reader.
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return item


@app.get("/api/search")
async def search(q: str = Query(..., min_length=2)) -> dict:
    """Search the live feed for `q`. Checks the in-memory buffer first —
    that's instant. If that comes up thin, it fetches fresh results from
    the web on the spot rather than returning an empty result, and folds
    the new finds into the live buffer so they're real, clickable
    articles afterward (see app/search.py)."""
    items = await search_news(broadcaster, q)
    return {"items": items, "total": len(items), "query": q}


@app.get("/api/jobs")
async def list_jobs(response: Response) -> dict:
    """Real, currently-open listings from independent public job boards
    (see app/jobs.py) — no mock/sample postings."""
    items = await get_jobs()
    # Job boards refresh far less often than the news feed — a longer
    # shared-cache window here means a traffic spike on the Jobs tab is
    # absorbed by nginx/browser cache instead of re-hitting the upstream
    # boards (or even this process) on every request.
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return {"items": items, "total": len(items)}


@app.get("/api/jobs/{job_id}")
async def get_job_detail(job_id: str, response: Response) -> dict:
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return job


# ISO 639-1 codes this app's language picker offers. English needs no
# translation call at all — it's what every article is authored in.
_SUPPORTED_LANGUAGES = {"en", "hi", "ta", "es"}


class TranslateRequest(BaseModel):
    texts: list[str]
    target: str
    source: str = "en"


@app.post("/api/translate")
async def translate(req: TranslateRequest) -> dict:
    """Translates a batch of independent strings (e.g. a story's title and
    excerpt together) in one call. Best-effort against a free, rate-limited
    public API (see app/translate.py) — any text that fails to translate
    comes back unchanged rather than the request failing outright, since a
    story shown in the wrong language for one field beats a broken page."""
    target = req.target if req.target in _SUPPORTED_LANGUAGES else "en"
    if not req.texts:
        return {"translations": []}
    if len(req.texts) > 20:
        raise HTTPException(status_code=400, detail="Too many texts in one request (max 20)")
    translations = await translate_many(req.texts, target=target, source=req.source)
    return {"translations": translations, "target": target}


@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket, category: str = Query("All")) -> None:
    """
    Protocol
    --------
    On connect: server immediately sends
        {"type": "initial", "items": [...]}

    Whenever a new article is scraped: server pushes, unprompted, to every
    client whose category matches (or is "All"):
        {"type": "new_item", "item": {...}}

    Client can send, e.g. when the user scrolls near the bottom of the
    feed:
        {"type": "load_more", "cursor": 20, "page_size": 10, "category": "Tech"}
    and receives:
        {"type": "more_items", "items": [...], "next_cursor": 30, "has_more": true}

    Client can switch category (e.g. tapping a filter tab) with:
        {"type": "set_category", "category": "Sports"}
    which re-sends a fresh {"type": "initial", ...} for that category and
    re-targets future live pushes to it.
    """
    await broadcaster.connect(websocket, category=category)
    try:
        initial = await broadcaster.snapshot(category)
        await websocket.send_text(json.dumps({"type": "initial", "items": initial}))

        while True:
            raw = await websocket.receive_text()

            try:
                payload = json.loads(raw)
                msg = ClientMessage.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
                continue

            if msg.type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif msg.type == "set_category":
                new_category = msg.category or "All"
                broadcaster.set_category(websocket, new_category)
                items = await broadcaster.snapshot(new_category)
                await websocket.send_text(json.dumps({"type": "initial", "items": items}))

            elif msg.type == "load_more":
                cat = msg.category or category
                items = await broadcaster.snapshot(cat)
                start = max(0, msg.cursor or 0)
                page_size = max(1, min(50, msg.page_size or 10))
                page = items[start : start + page_size]
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "more_items",
                            "items": page,
                            "next_cursor": start + page_size,
                            "has_more": start + page_size < len(items),
                        }
                    )
                )

    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
    except Exception:
        logger.exception("WebSocket connection error")
        broadcaster.disconnect(websocket)