# InBits News Backend — production image
#
# Runs the FastAPI app under uvicorn. This container is stateless by
# design (everything lives in the in-memory rolling buffer described in
# app/broadcaster.py) — you can scale it horizontally, but note that each
# replica keeps its own independent buffer and WebSocket clients, so a
# client's "load more" pagination cursor is only valid against whichever
# replica it's connected to. For multi-replica deployments, put a
# sticky-session-aware load balancer (or a single replica) in front of the
# WebSocket route, or graduate the buffer to a shared store (Redis) —
# neither is needed for a single-instance deploy.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for lxml (used by app/content_fetcher.py's HTML parsing)
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      libxml2-dev \
      libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as a non-root user in production.
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
