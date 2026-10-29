"""HTTP API (architecture.md §6).

    uvicorn app.main:app            # or: python -m app.main; then open http://127.0.0.1:8000/

Every thread endpoint needs an `X-Client-Id` header (an anonymous id the browser generates) and
only sees that client's threads; anyone else's thread id is a 404. Messages in one thread are
answered one at a time; different threads run concurrently. Logs record intent, scheme, cited
chunk and latency, never the message text. The chat page in `web/` is served at `/`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.pipeline import Pipeline
from app.templates import Templates
from app.threads import Message, Thread, ThreadStore
from ingest.manifest import Manifest

log = logging.getLogger("app.api")

MAX_MESSAGE_CHARS = 1000
RATE_LIMIT = 20  # messages per client per minute
RATE_WINDOW_SECONDS = 60.0
HISTORY_TURNS = 6
_CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# The page loads nothing from other origins and never runs inline script (edge_case.md UI-01).
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class NewMessage(BaseModel):
    content: str


class RateLimiter:
    """Sliding window: at most `limit` messages per `window` seconds per client."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client_id: str) -> bool:
        now = self.clock()
        hits = self._hits[client_id]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def _thread_json(t: Thread) -> dict:
    return {"thread_id": t.thread_id, "title": t.title, "created_at": t.created_at, "updated_at": t.updated_at}


def _message_json(m: Message) -> dict:
    return {"message_id": m.message_id, "role": m.role, "content": m.content, "kind": m.kind,
            "source_url": m.source_url, "source_date": m.source_date, "created_at": m.created_at}


def _has_corpus(pipeline: Pipeline) -> bool:
    try:
        return len(pipeline.answerer.retriever) > 0
    except TypeError:  # a test double without __len__
        return True


def create_app(pipeline: Pipeline | None = None, store: ThreadStore | None = None,
               manifest: Manifest | None = None, rate_limiter: RateLimiter | None = None) -> FastAPI:
    """Build the app. Tests pass a pipeline with mock models and a temporary store."""
    state: dict = {"pipeline": pipeline, "store": store, "manifest": manifest, "not_ready": None}
    limiter = rate_limiter or RateLimiter()
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not log.handlers and not logging.getLogger().handlers:  # e.g. under plain `uvicorn app.main:app`
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
        if state["manifest"] is None:
            state["manifest"] = Manifest(settings.manifest_path)
        if state["store"] is None:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            state["store"] = ThreadStore(settings.threads_path)
        if state["pipeline"] is None:
            try:
                state["pipeline"] = await asyncio.to_thread(Pipeline)
            except Exception as e:  # missing or mismatched index: serve, but answer "not ready"
                state["not_ready"] = f"{type(e).__name__}: {e}"
                log.error("pipeline not ready: %s", state["not_ready"])
        yield

    app = FastAPI(title="HDFC MF Facts Assistant", lifespan=lifespan)

    def client_id(x_client_id: str | None = Header(default=None)) -> str:
        if not x_client_id or not _CLIENT_ID.match(x_client_id):
            raise HTTPException(400, "Missing or invalid X-Client-Id header.")
        return x_client_id

    def store() -> ThreadStore:
        return state["store"]

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(HTTPException)
    async def friendly_errors(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # --- threads ------------------------------------------------------------------------

    @app.post("/threads", status_code=201)
    def create_thread(client: str = Depends(client_id)) -> dict:
        return _thread_json(store().create_thread(client))

    @app.get("/threads")
    def list_threads(client: str = Depends(client_id)) -> dict:
        return {"threads": [_thread_json(t) for t in store().list_threads(client)]}

    @app.get("/threads/{thread_id}/messages")
    def get_messages(thread_id: uuid.UUID, client: str = Depends(client_id)) -> dict:
        msgs = store().messages(client, str(thread_id))
        if msgs is None:
            raise HTTPException(404, "Thread not found.")
        return {"thread_id": str(thread_id), "messages": [_message_json(m) for m in msgs]}

    @app.delete("/threads/{thread_id}", status_code=204)
    def delete_thread(thread_id: uuid.UUID, client: str = Depends(client_id)) -> Response:
        if not store().delete_thread(client, str(thread_id)):
            raise HTTPException(404, "Thread not found.")
        locks.pop(str(thread_id), None)
        return Response(status_code=204)

    @app.post("/threads/{thread_id}/messages")
    async def post_message(thread_id: uuid.UUID, body: NewMessage, client: str = Depends(client_id)) -> dict:
        tid = str(thread_id)
        text = body.content.strip()
        if not text:
            raise HTTPException(422, "Please type a question.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(422, f"Please keep your question under {MAX_MESSAGE_CHARS:,} characters.")
        if store().get_thread(client, tid) is None:
            raise HTTPException(404, "Thread not found.")
        if not limiter.allow(client):
            raise HTTPException(429, "You're sending messages too quickly. Please wait a minute and try again.")

        lock = locks.setdefault(tid, asyncio.Lock())
        async with lock:  # one message at a time per thread, so the next one sees this one in history
            started = time.perf_counter()
            history = store().history(client, tid, HISTORY_TURNS)
            pipeline: Pipeline | None = state["pipeline"]
            if pipeline is None or not _has_corpus(pipeline):
                reply = Templates.service_unavailable()
            else:
                try:
                    reply = await pipeline.ahandle(text, history)
                except Exception as e:  # never half-save, never leak internals
                    log.error("pipeline error: %s", type(e).__name__)
                    reply = Templates.service_unavailable()
            saved = store().add_exchange(client, tid, text, reply)
            log.info("reply kind=%s intent=%s scheme=%s cited=%s latency_ms=%d", reply.kind, reply.intent,
                     reply.scheme_id, reply.cited.chunk_id if reply.cited else None,
                     (time.perf_counter() - started) * 1000)
        if saved is None:
            raise HTTPException(404, "Thread not found.")
        user, bot = saved
        return {"thread_id": tid, "user_message": _message_json(user), "reply": _message_json(bot)}

    # --- health -------------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        rows = state["manifest"].all() if state["manifest"] else []
        ready = state["pipeline"] is not None and _has_corpus(state["pipeline"]) and bool(rows)
        return {
            "status": "ok" if ready else "not_ready",
            "documents": len(rows),
            "latest_fetched_at": max((r.fetched_at for r in rows), default=None),
        }

    # --- chat page (registered last, so the API routes above take precedence) ----------

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)
