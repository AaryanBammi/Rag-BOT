"""API tests with mock models (eval.md Phase 4): threads, isolation, concurrency, limits, PII storage."""

import asyncio
import logging
import re
import sqlite3
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ask as askmod
from app.ask import Answerer
from app.classifier import Classification
from app.generator import GeneratedAnswer
from app.llm import LLMError
from app.main import RateLimiter, create_app
from app.pii_guard import PATTERNS
from app.pipeline import Pipeline
from app.registry import load_registry
from app.retriever import RetrievedChunk
from app.threads import NEW_CHAT, PII_PLACEHOLDER, ThreadStore
from ingest.manifest import DocumentRow, Manifest
from tests.test_pii_guard import POSITIVES

REG = load_registry()
NAMES = {"small cap": "hdfc-small-cap", "mid cap": "hdfc-mid-cap", "large cap": "hdfc-large-cap",
         "multi cap": "hdfc-multi-cap", "elss": "hdfc-elss-tax-saver", "silver": "hdfc-silver-etf-fof"}
LOAD = {"hdfc-small-cap": "1.00%", "hdfc-mid-cap": "1.00%", "hdfc-large-cap": "1.00%", "hdfc-multi-cap": "1.00%",
        "hdfc-elss-tax-saver": "Nil", "hdfc-silver-etf-fof": "0.50%"}
CLIENT_A, CLIENT_B = {"X-Client-Id": "client-aaaa-1111"}, {"X-Client-Id": "client-bbbb-2222"}


def scheme_in(text):
    return next((sid for name, sid in NAMES.items() if name in text.lower()), None)


class FakeRetriever:
    def __len__(self):
        return 100

    def search(self, question, scheme_id=None, top_k=None):
        return [RetrievedChunk(
            chunk_id=f"{scheme_id}-kim:1", text=f"{scheme_id} Exit Load: {LOAD[scheme_id]} within 1 year.",
            doc_id=f"{scheme_id}-kim", scheme_id=scheme_id, source_type="kim", kind="text",
            url=REG.scheme(scheme_id).source("kim").url, title="KIM", as_of="2025-11-21",
            fetched_at="2026-10-27T05:00:00+00:00", page=1, score=0.03, similarity=0.9)]


class Calls:
    """Records what the mock classifier saw, per message."""

    def __init__(self):
        self.histories = {}
        self.order = []


@pytest.fixture
def calls():
    return Calls()


@pytest.fixture
def make_app(tmp_path, monkeypatch, calls):
    def build(classifier=None, generator_delay=0.0, limiter=None, documents=True):
        async def classify(message, registry, history=()):
            calls.histories[message] = list(history)
            calls.order.append(message)
            if message == "boom":
                raise LLMError("unavailable", "503")
            scheme = scheme_in(message)
            if scheme is None:  # resolve "its" from this thread's history only
                scheme = next((scheme_in(t) for _, t in reversed(history) if scheme_in(t)), None)
            if "hello" in message.lower():
                return Classification("greeting", None, False, False, message)
            return Classification("factual", scheme, scheme is None, False, f"Exit load of {scheme}?")

        async def agenerate(q, chunks, scheme, **kw):
            await asyncio.sleep(generator_delay)
            c = chunks[0]
            return GeneratedAnswer(True, f"The exit load is {LOAD[c.scheme_id]} within 1 year.", c.chunk_id)

        monkeypatch.setattr(askmod, "agenerate", agenerate)
        manifest = Manifest(tmp_path / ("manifest.json" if documents else "empty-manifest.json"))
        if documents:
            manifest.upsert(DocumentRow("hdfc-small-cap-kim", "u", "kim", "hdfc-small-cap", "KIM", "h",
                                        "2026-10-27T05:00:00+00:00", "2025-11-21", 1, "m", "2026-10-27T06:00:00+00:00"))
        answerer = Answerer(FakeRetriever(), REG, manifest)
        pipeline = Pipeline(answerer, aclassifier=classifier or classify)
        store = ThreadStore(tmp_path / "threads.db")
        app = create_app(pipeline, store, manifest, limiter or RateLimiter(limit=1000))
        app.state.db_path = tmp_path / "threads.db"
        app.state.store = store
        return app

    return build


def new_thread(c, headers=CLIENT_A):
    r = c.post("/threads", headers=headers)
    assert r.status_code == 201
    return r.json()["thread_id"]


def ask(c, tid, text, headers=CLIENT_A):
    return c.post(f"/threads/{tid}/messages", json={"content": text}, headers=headers)


def test_follow_up_resolves_within_thread_and_not_across(make_app):
    with TestClient(make_app()) as c:
        a, b = new_thread(c), new_thread(c)
        first = ask(c, a, "What is the exit load of HDFC Small Cap?").json()["reply"]
        assert first["kind"] == "answer" and first["source_url"] == REG.scheme("hdfc-small-cap").source("kim").url
        follow = ask(c, a, "what is its exit load?").json()["reply"]
        assert follow["kind"] == "answer" and "hdfc-small-cap" not in follow["content"]
        assert follow["source_url"] == REG.scheme("hdfc-small-cap").source("kim").url
        other = ask(c, b, "what is its exit load?").json()["reply"]
        assert other["kind"] == "clarification"


def test_history_and_messages_endpoint(make_app, calls):
    with TestClient(make_app()) as c:
        tid = new_thread(c)
        ask(c, tid, "Exit load of HDFC Mid Cap?")
        ask(c, tid, "and its exit load again?")
        hist = calls.histories["and its exit load again?"]
        assert hist[0] == ("user", "Exit load of HDFC Mid Cap?") and hist[1][0] == "assistant"
        assert "Source:" not in hist[1][1]  # link and footer trimmed from history
        msgs = c.get(f"/threads/{tid}/messages", headers=CLIENT_A).json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert msgs[1]["source_date"] == "21 Nov 2025" and msgs[1]["kind"] == "answer"
        threads = c.get("/threads", headers=CLIENT_A).json()["threads"]
        assert threads[0]["title"] == "Exit load of HDFC Mid Cap?"


@pytest.mark.anyio
async def test_concurrent_threads_get_separate_answers(make_app):
    app = make_app(generator_delay=0.3)
    schemes = list(NAMES.items())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tids = [(await c.post("/threads", headers=CLIENT_A)).json()["thread_id"] for _ in range(10)]
            started = time.perf_counter()
            results = await asyncio.gather(*[
                c.post(f"/threads/{tid}/messages", json={"content": f"Exit load of HDFC {schemes[i % 6][0]}?"},
                       headers=CLIENT_A) for i, tid in enumerate(tids)])
            elapsed = time.perf_counter() - started
    assert elapsed < 10 * 0.3 / 2  # ran in parallel, not one after another
    for i, r in enumerate(results):
        sid = schemes[i % 6][1]
        reply = r.json()["reply"]
        assert reply["source_url"] == REG.scheme(sid).source("kim").url and LOAD[sid] in reply["content"]


@pytest.mark.anyio
async def test_same_thread_messages_answered_in_order(make_app, calls):
    app = make_app(generator_delay=0.2)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            tid = (await c.post("/threads", headers=CLIENT_A)).json()["thread_id"]
            first = c.post(f"/threads/{tid}/messages", json={"content": "Exit load of HDFC Mid Cap?"}, headers=CLIENT_A)
            second = c.post(f"/threads/{tid}/messages", json={"content": "what is its exit load?"}, headers=CLIENT_A)
            r1, r2 = await asyncio.gather(first, second)
            msgs = (await c.get(f"/threads/{tid}/messages", headers=CLIENT_A)).json()["messages"]
    assert calls.order == ["Exit load of HDFC Mid Cap?", "what is its exit load?"]
    assert calls.histories["what is its exit load?"][0] == ("user", "Exit load of HDFC Mid Cap?")
    assert r2.json()["reply"]["kind"] == "answer"
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["Exit load of HDFC Mid Cap?", "what is its exit load?"]


def test_client_isolation(make_app):
    with TestClient(make_app()) as c:
        tid = new_thread(c, CLIENT_A)
        ask(c, tid, "Exit load of HDFC Mid Cap?")
        assert c.get("/threads", headers=CLIENT_B).json()["threads"] == []
        assert c.get(f"/threads/{tid}/messages", headers=CLIENT_B).status_code == 404
        assert ask(c, tid, "Exit load of HDFC Mid Cap?", CLIENT_B).status_code == 404
        assert c.delete(f"/threads/{tid}", headers=CLIENT_B).status_code == 404
        assert len(c.get(f"/threads/{tid}/messages", headers=CLIENT_A).json()["messages"]) == 2


def test_bad_ids_and_missing_client(make_app):
    with TestClient(make_app()) as c:
        assert c.get("/threads").status_code == 400
        assert c.get("/threads", headers={"X-Client-Id": "x"}).status_code == 400
        assert c.get("/threads/not-a-uuid/messages", headers=CLIENT_A).status_code == 422
        assert c.get(f"/threads/{uuid.uuid4()}/messages", headers=CLIENT_A).status_code == 404
        assert ask(c, str(uuid.uuid4()), "hello").status_code == 404


def test_length_and_rate_limits(make_app):
    now = [0.0]
    limiter = RateLimiter(limit=20, window=60, clock=lambda: now[0])
    with TestClient(make_app(limiter=limiter)) as c:
        tid = new_thread(c)
        r = ask(c, tid, "   ")
        assert r.status_code == 422 and r.json()["detail"] == "Please type a question."
        r = ask(c, tid, "x" * 1001)
        assert r.status_code == 422 and "1,000 characters" in r.json()["detail"]
        assert all(ask(c, tid, "hello").status_code == 200 for _ in range(20))
        r = ask(c, tid, "hello")
        assert r.status_code == 429 and "too quickly" in r.json()["detail"]
        assert ask(c, tid, "hello", CLIENT_B).status_code == 404  # other clients unaffected (own limit)
        now[0] = 61.0
        assert ask(c, tid, "hello").status_code == 200


def test_pii_is_never_stored(make_app):
    app = make_app()
    with TestClient(app) as c:
        for _, message in POSITIVES:
            reply = ask(c, new_thread(c), message).json()["reply"]
            assert reply["kind"] == "pii"
        titles = [t["title"] for t in c.get("/threads", headers=CLIENT_A).json()["threads"]]
        assert titles == [NEW_CHAT] * len(POSITIVES)
    db = sqlite3.connect(app.state.db_path)
    stored = [r[0] for r in db.execute("SELECT content FROM messages")] + [r[0] for r in db.execute("SELECT title FROM threads")]
    text = "\n".join(stored)
    assert [k for k, p in PATTERNS.items() if p.search(text)] == []
    dump = "\n".join(str(row) for t in ("threads", "messages") for row in db.execute(f"SELECT * FROM {t}"))
    for _, message in POSITIVES:  # no fragment of any PII message anywhere in the database
        for token in re.findall(r"\S*\d\S*|\S+@\S+", message):
            assert token.strip(",.?") not in dump, token
    assert text.count(PII_PLACEHOLDER) == len(POSITIVES)


def test_logs_contain_no_message_text(make_app, caplog):
    secret = "Exit load of HDFC Multi Cap with a unique phrase zebra-umbrella?"
    with caplog.at_level(logging.INFO, logger="app.api"), TestClient(make_app()) as c:
        ask(c, new_thread(c), secret)
    assert caplog.records and "zebra" not in caplog.text and "kind=answer" in caplog.text


def test_model_failure_gives_service_unavailable_and_saves_whole_exchange(make_app):
    app = make_app()
    with TestClient(app) as c:
        tid = new_thread(c)
        reply = ask(c, tid, "boom").json()["reply"]
        assert reply["kind"] == "service_unavailable" and reply["source_url"] is None
        assert len(c.get(f"/threads/{tid}/messages", headers=CLIENT_A).json()["messages"]) == 2


def test_thread_deleted_mid_request(make_app):
    async def classify_then_delete(message, registry, history=()):
        store = app.state.store
        for t in store.list_threads(CLIENT_A["X-Client-Id"]):
            store.delete_thread(CLIENT_A["X-Client-Id"], t.thread_id)
        return Classification("greeting", None, False, False, message)

    app = make_app(classifier=classify_then_delete)
    store = app.state.store
    with TestClient(app) as c:
        tid = new_thread(c)
        assert ask(c, tid, "hello").status_code == 404
    assert store.count() == (0, 0)


def test_health(make_app):
    with TestClient(make_app()) as c:
        h = c.get("/health").json()
        assert h == {"status": "ok", "documents": 1, "latest_fetched_at": "2026-10-27T05:00:00+00:00"}
    with TestClient(make_app(documents=False)) as c:
        assert c.get("/health").json()["status"] == "not_ready"


def test_empty_corpus_answers_not_ready(make_app, monkeypatch):
    monkeypatch.setattr(FakeRetriever, "__len__", lambda self: 0)
    with TestClient(make_app()) as c:
        reply = ask(c, new_thread(c), "Exit load of HDFC Mid Cap?").json()["reply"]
        assert reply["kind"] == "service_unavailable"


# --- chat page (Phase 5) -------------------------------------------------------------------

EXAMPLES = ["What is the expense ratio of HDFC Mid Cap Fund – Direct Growth?",
            "What is the lock-in period for HDFC ELSS Tax Saver Fund?",
            "How do I download my capital gains statement?"]


def test_chat_page_served_with_disclaimer_examples_and_pii_note(make_app):
    with TestClient(make_app()) as c:
        page = c.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        html = page.text
        assert "Facts-only. No investment advice." in html
        assert re.findall(r'<button type="button" class="example">(.*?)</button>', html) == EXAMPLES
        assert "Please don't share PAN, Aadhaar, account numbers, OTPs, email or phone numbers." in html
        assert 'name="viewport"' in html
        for asset, kind in (("/app.js", "javascript"), ("/styles.css", "text/css")):
            r = c.get(asset)
            assert r.status_code == 200 and kind in r.headers["content-type"]


def test_static_mount_does_not_shadow_api(make_app):
    with TestClient(make_app()) as c:
        assert c.get("/threads").status_code == 400  # API route, not a static 404
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/no-such-file.js").status_code == 404


def test_security_headers_on_page_and_api(make_app):
    with TestClient(make_app()) as c:
        for path in ("/", "/app.js", "/health"):
            h = c.get(path).headers
            assert "default-src 'self'" in h["content-security-policy"]
            assert h["x-content-type-options"] == "nosniff"


def test_page_script_renders_text_safely():
    # UI-01 / UI-10: user and model text only via textContent; source links open in a new tab safely.
    from app.main import WEB_DIR
    js = (WEB_DIR / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert 'rel = "noopener noreferrer"' in js and 'target = "_blank"' in js
    assert "X-Client-Id" in js and "localStorage" in js
    html = (WEB_DIR / "index.html").read_text()
    assert "<script>" not in html and "onclick" not in html  # nothing inline, so the CSP holds
