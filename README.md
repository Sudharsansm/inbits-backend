# InBits News Backend (Python)

A real-time news backend built on **Bitscrape** (`pip install bitscrape==0.8.0`,
https://github.com/Sudharsansm/Bitscrape) and **FastAPI WebSockets**. It
continuously crawls a set of public news feeds and pushes new articles to
every connected client the instant they're scraped — nothing is written to
a database or disk; everything lives in a small in-memory rolling buffer
that exists only to (a) hand new clients an instant feed and (b) serve
scroll pagination.

## How it works

```
┌──────────────┐   every N seconds    ┌─────────────────┐
│  RSS/Atom    │ ───────────────────▶ │ bitscrape.Engine │
│  feeds (8)   │                      │  + NewsFeedSpider│
└──────────────┘                      └────────┬─────────┘
                                                │ item_scraped (per item,
                                                │ mid-crawl — not batched)
                                                ▼
                                       ┌──────────────────┐
                                       │   Broadcaster     │  in-memory only,
                                       │ (ring buffer +    │  never persisted
                                       │  WebSocket fanout)│
                                       └────────┬──────────┘
                                                │
                              ┌─────────────────┼─────────────────┐
                              ▼                 ▼                 ▼
                        WS client A       WS client B       GET /api/feed
                        (live push)       (live push)       (REST fallback)
```

`NewsFeedSpider` (`app/spiders/news_spider.py`) is a real `bitscrape.Spider`
subclass — it uses Bitscrape's actual `Engine`, `Request`/`Response`,
`Settings`, and plugin/signal system (`item_scraped`), not a mock. A small
`BasePlugin` (`app/crawler.py`) bridges Bitscrape's `item_scraped` signal
straight to the WebSocket broadcaster, so updates go out **while the crawl
is still running**, not only after it finishes.

## Setup

**With `uv` (recommended):**
```bash
uv sync                          # creates .venv and installs everything from pyproject.toml
cp .env.example .env             # tweak feeds/interval/CORS if you want
uv run python run.py             # or: uv run uvicorn app.main:app --reload
```

**With plain pip:**
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Server starts on `http://localhost:8000` and begins crawling immediately.

## Run the tests

**With `uv`:**
```bash
uv sync --group dev
uv run pytest -q
```

**With plain pip:**
```bash
pip install pytest pytest-asyncio httpx2
pytest -q
```

11 tests cover the RSS/Atom parser (against real `bitscrape` objects — no
mocking of the library itself), the broadcaster's dedup/category-routing/
bounded-buffer behavior, and the full WebSocket protocol end-to-end via
FastAPI's `TestClient`.

## Requires Python 3.11+

`bitscrape==0.8.0` requires Python ≥3.11. `uv sync` resolves/manages this
for you automatically; with plain venv/pip, make sure your interpreter is
3.11+.

## HTTP endpoints

| Method | Path | What |
|---|---|---|
| GET | `/api/health` | `{"status": "ok", "connected_clients": N}` |
| GET | `/api/feed?category=Tech` | Current in-memory snapshot (REST fallback for non-WebSocket clients) |
| WS | `/ws/feed?category=All` | Live feed — see protocol below |

## WebSocket protocol (`/ws/feed`)

**On connect**, the server immediately sends the current buffer:
```json
{"type": "initial", "items": [ {NewsItem}, ... ]}
```

**Whenever a new article is scraped**, every client whose selected category
matches (or is `"All"`) gets, unprompted:
```json
{"type": "new_item", "item": {NewsItem}}
```

**When the user scrolls near the bottom of the feed**, the client asks for
more of the buffered items:
```json
// client sends:
{"type": "load_more", "cursor": 20, "page_size": 10, "category": "Tech"}
// server replies:
{"type": "more_items", "items": [...], "next_cursor": 30, "has_more": true}
```

**When the user switches a category tab**, the client sends:
```json
{"type": "set_category", "category": "Sports"}
```
and gets back a fresh `{"type": "initial", "items": [...]}` for that
category; future live pushes are re-targeted to it too.

A `NewsItem` looks like this (field names deliberately match the existing
frontend's `Post` type in `src/lib/content.ts`, so wiring it up later is a
drop-in):
```json
{
  "id": "6597b5120957c60c",
  "originalArticleId": "6597b5120957c60c",
  "category": "Tech",
  "title": "Scientists discover new exoplanet",
  "excerpt": "A team of astronomers announced today...",
  "content": "A team of astronomers announced today the discovery of a new exoplanet twice the size of Jupiter. Researchers at the observatory said further observations are planned...",
  "author": "Jane Doe",
  "source": "BBC News",
  "sourceUrl": "https://bbc.co.uk/news/...",
  "readTime": 3,
  "image": "https://...",
  "publishedAt": "2026-08-25T09:30:00+00:00",
  "updatedAt": "2026-08-25T09:30:00+00:00",
  "likes": 0,
  "views": 0,
  "tags": ["Space", "Science"],
  "language": "en",
  "location": "United Kingdom",
  "status": "published"
}
```

`id` / `originalArticleId` are both the sha1 of the canonical article link —
kept as two fields so a future source with its own native ID can populate
`originalArticleId` separately while `id` stays our internal dedup key.

`content` is the one field RSS/Atom never gives you. `app/content_fetcher.py`
fetches the live article page and extracts the main body text (falling back
to `excerpt` if the fetch or extraction fails, so it's never blank) — wired
in at `app/crawler.py`'s `item_scraped` hook, right before an item is
broadcast, with a concurrency cap so it doesn't hammer any one publisher.

`tags` comes from each entry's `<category>` (RSS) / `<atom:category>`
(Atom) elements, if the feed includes them. `language` and `location` come
from `FeedConfig` (per-feed, since RSS itself rarely states this reliably).
`status` is currently always `"published"` — draft/archived would need an
editorial workflow this app doesn't have.

## Minimal frontend example

```js
const ws = new WebSocket("ws://localhost:8000/ws/feed?category=All");

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "initial") renderFeed(msg.items);
  if (msg.type === "new_item") prependToFeed(msg.item);
  if (msg.type === "more_items") appendToFeed(msg.items);
};

// call this from your onScroll / IntersectionObserver handler
function loadMore(cursor) {
  ws.send(JSON.stringify({ type: "load_more", cursor, page_size: 10 }));
}

function switchCategory(category) {
  ws.send(JSON.stringify({ type: "set_category", category }));
}
```

## Configuring feeds / Bitscrape crawl behavior

- Feed list: `app/config.py` → `DEFAULT_FEEDS` (add/remove publishers freely).
- Crawl politeness/concurrency: `app/config.py` → `Settings`, passed straight
  into `bitscrape.Settings` in `app/crawler.py`
  (`concurrent_requests`, `download_delay`, `robotstxt_obey=True`, etc.).
- Re-crawl interval: `REFRESH_INTERVAL_SECONDS` env var (default 120s). New
  items are pushed the moment they're scraped regardless of this value —
  it only controls how often a full pass over all feeds starts.

## Notes on "don't store any data"

The only in-memory state is `Broadcaster._buffer`, a `deque(maxlen=BUFFER_SIZE)`
— a rolling window that exists solely to serve instant initial feeds and
scroll pagination. Nothing is written to a file or database, and restarting
the process clears it, by design.

## Deployment

This is a long-running process (the crawl loop is a background `asyncio`
task started in FastAPI's lifespan), so it needs a host that keeps a Node/
Python process alive — a VM, Docker container, Railway, Render, Fly.io,
etc. It is **not** a fit for serverless/edge functions that spin down
between requests, since both the crawl loop and the WebSocket connections
need a persistent process.
