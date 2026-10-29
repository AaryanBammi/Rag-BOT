"""End-to-end ingestion tests with an in-memory HTTP transport and the fake embedder."""

import os

import httpx
import pytest

from app.registry import parse_registry
from ingest.embed import FakeEmbedder
from ingest.manifest import Manifest
from ingest.pipeline import Ingestor
from ingest.store import ChunkStore, StoreError
from tests.pdf_fixture import filler, make_pdf

BASE = "https://files.hdfcfund.com/"
URLS = {
    "sid": BASE + "sid.pdf",
    "kim": BASE + "kim.pdf",
    "factsheet": BASE + "factsheet.pdf",
    "amfi": "https://www.amfiindia.com/investor/a",
    "sebi": "https://investor.sebi.gov.in/b.html",
    "safety": "https://investor.sebi.gov.in/c.html",
}
GROWW = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"


def registry_data(include_kim=True):
    sources = [
        {"type": "scheme_page", "url": "https://www.hdfcfund.com/explore/mid/direct", "fetch": "manual"},
        {"type": "sid", "url": URLS["sid"], "fetch": "auto"},
    ]
    sources.append({"type": "kim", "url": URLS["kim"], "fetch": "auto" if include_kim else "none"})
    return {
        "schemes": [
            {
                "id": "hdfc-mid-cap",
                "name": "HDFC Mid Cap Fund – Direct Growth",
                "aliases": ["HDFC Mid-Cap Opportunities Fund"],
                "groww_reference": "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
                "sources": sources,
            }
        ],
        "general": [
            {"id": "factsheet", "type": "factsheet", "title": "Factsheet", "url": URLS["factsheet"], "fetch": "auto", "multi_scheme": True},
            {"id": "statement", "type": "amc_service", "title": "Statements", "url": "https://www.hdfcfund.com/s", "fetch": "manual"},
            {"id": "amfi", "type": "amfi_education", "title": "AMFI", "url": URLS["amfi"], "fetch": "auto"},
            {"id": "sebi", "type": "sebi_education", "title": "SEBI", "url": URLS["sebi"], "fetch": "auto"},
            {"id": "safety", "type": "investor_safety", "title": "Safety", "url": URLS["safety"], "fetch": "auto"},
        ],
    }


def html_page(body: str, noise: str = "") -> bytes:
    paras = "".join(f"<p>{body} Sentence {i} about mutual funds and exit loads.</p>" for i in range(10))
    return f"<html><head><script>var t='{noise}';</script></head><body><article>{paras}</article></body></html>".encode()


def default_content():
    return {
        URLS["sid"]: make_pdf([["SCHEME INFORMATION DOCUMENT dated November 21, 2025", filler(500, "sid")]]),
        URLS["kim"]: make_pdf([["KEY INFORMATION MEMORANDUM dated November 21, 2025", "Exit Load of 1.00%", filler(100, "kim")]]),
        URLS["factsheet"]: make_pdf(
            [
                ["Contents", filler(50, "toc")],
                ["HDFC Mid Cap Fund", "EXPENSE RATIO", "(As On September 30, 2026)", "Regular: 1.26% Direct: 0.71%"],
                ["HDFC Value Fund", "not a registry scheme", filler(50, "value")],
            ]
        ),
        URLS["amfi"]: html_page("AMFI education."),
        URLS["sebi"]: html_page("SEBI education."),
        URLS["safety"]: html_page("Never share your OTP."),
        GROWW: b"<html><body>Groww page for HDFC Mid Cap Fund</body></html>",
    }


class Site:
    """In-memory website: tests change `content`/`status`/`redirect` between runs."""

    def __init__(self):
        self.content = default_content()
        self.status: dict[str, int] = {}
        self.redirect: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in self.redirect:
            return httpx.Response(302, headers={"Location": self.redirect[url]})
        if url in self.status:
            return httpx.Response(self.status[url])
        if url in self.content:
            return httpx.Response(200, content=self.content[url])
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    site = Site()
    embedder = FakeEmbedder()
    manifest = Manifest(tmp_path / "manifest.json")
    store = ChunkStore(tmp_path / "index", embedder.model)

    def make(data=None):
        client = httpx.Client(transport=httpx.MockTransport(site.handler), follow_redirects=True)
        return Ingestor(
            parse_registry(data or registry_data()), manifest, store, embedder,
            raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean", client=client, sleep=lambda s: None,
        )

    yield type("Env", (), {"site": site, "embedder": embedder, "manifest": manifest, "store": store, "make": make, "tmp": tmp_path})


def statuses(summary):
    return {o.doc_id: o.status for o in summary.outcomes}


def test_first_run_ingests_auto_sources_and_flags_manual(env):
    s = statuses(env.make().run())
    assert s["hdfc-mid-cap-sid"] == s["hdfc-mid-cap-kim"] == s["factsheet"] == "updated"
    assert s["amfi"] == s["sebi"] == s["safety"] == "updated"
    assert s["hdfc-mid-cap-scheme_page"] == s["statement"] == "needs_manual"
    row = env.manifest.get("hdfc-mid-cap-sid")
    assert row.as_of == "2025-11-21" and row.chunk_count == env.store.count("hdfc-mid-cap-sid")


def test_factsheet_keeps_only_registry_scheme_pages(env):
    env.make().run()
    chunks = env.store.get({"doc_id": "factsheet"})
    assert chunks and {c["scheme_id"] for c in chunks} == {"hdfc-mid-cap"}
    assert all("HDFC Value Fund" not in c["text"] for c in chunks)
    assert env.manifest.get("factsheet").as_of == "2026-09-30"


def test_second_run_is_unchanged_and_embeds_nothing(env):
    env.make().run()
    calls = env.embedder.calls
    s = statuses(env.make().run())
    assert {v for k, v in s.items() if not k.endswith(("scheme_page", "statement", "groww"))} == {"unchanged"}
    assert env.embedder.calls == calls


def test_cosmetic_html_change_is_unchanged(env):
    env.make().run()
    env.site.content[URLS["amfi"]] = html_page("AMFI education.", noise="timestamp-2026-10-27T10:00")
    assert statuses(env.make().run())["amfi"] == "unchanged"  # PRS-10 / D-15


def test_changed_document_replaces_old_chunks(env):
    env.make().run()
    env.site.content[URLS["kim"]] = make_pdf([["KIM dated November 21, 2025", "Exit Load of 0.50%", filler(100, "new")]])
    assert statuses(env.make().run())["hdfc-mid-cap-kim"] == "updated"
    texts = [c["text"] for c in env.store.get({"doc_id": "hdfc-mid-cap-kim"})]
    assert any("0.50%" in t for t in texts) and not any("1.00%" in t for t in texts)
    assert len(texts) == env.manifest.get("hdfc-mid-cap-kim").chunk_count


def test_embedding_failure_keeps_previous_version(env):
    env.make().run()
    before_hash = env.manifest.get("hdfc-mid-cap-kim").content_hash
    env.site.content[URLS["kim"]] = make_pdf([["KIM changed", "Exit Load of 0.50%", filler(100, "new")]])

    def boom(texts):
        raise RuntimeError("Voyage down")

    real = env.embedder.embed_documents
    env.embedder.embed_documents = boom
    out = env.make().run()
    assert statuses(out)["hdfc-mid-cap-kim"] == "failed" and not out.ok
    assert env.manifest.get("hdfc-mid-cap-kim").content_hash == before_hash  # IDX-01
    assert any("1.00%" in c["text"] for c in env.store.get({"doc_id": "hdfc-mid-cap-kim"}))

    env.embedder.embed_documents = real
    assert statuses(env.make().run())["hdfc-mid-cap-kim"] == "updated"  # IDX-02: re-run recovers


def test_parse_failure_keeps_previous_version(env):
    env.make().run()
    count = env.store.count("hdfc-mid-cap-sid")
    env.site.content[URLS["sid"]] = make_pdf([[]])  # now a "scanned" PDF
    outcome = env.make().run()
    assert statuses(outcome)["hdfc-mid-cap-sid"] == "failed"
    assert env.store.count("hdfc-mid-cap-sid") == count


@pytest.mark.parametrize("status,hint", [(404, "HTTP 404"), (403, "use fetch: manual")])
def test_http_errors_fail_without_deleting(env, status, hint):
    env.make().run()
    env.site.status[URLS["sebi"]] = status
    out = env.make().run()
    sebi = next(o for o in out.outcomes if o.doc_id == "sebi")
    assert sebi.status == "failed" and hint in sebi.note
    assert env.store.count("sebi") > 0


def test_redirect_off_allowlist_rejected(env):
    env.site.redirect[URLS["amfi"]] = "https://example.com/phish"
    out = env.make().run()
    amfi = next(o for o in out.outcomes if o.doc_id == "amfi")
    assert amfi.status == "failed" and "not allowed" in amfi.note  # ING-03


def test_server_errors_retried_then_fail(env):
    calls = []
    original = env.site.handler

    def flaky(request):
        if str(request.url) == URLS["sebi"]:
            calls.append(1)
            return httpx.Response(503)
        return original(request)

    env.site.handler = flaky
    out = env.make().run()
    assert statuses(out)["sebi"] == "failed" and len(calls) == 3  # ING-02


def test_removed_source_is_cleaned_up(env):
    env.make().run()
    assert env.store.count("hdfc-mid-cap-kim") > 0
    out = env.make(registry_data(include_kim=False)).run()
    assert statuses(out)["hdfc-mid-cap-kim"] == "removed"  # IDX-03
    assert env.store.count("hdfc-mid-cap-kim") == 0 and env.manifest.get("hdfc-mid-cap-kim") is None


def test_manual_file_is_ingested_with_its_save_time(env):
    manual = env.tmp / "raw" / "manual"
    manual.mkdir(parents=True)
    path = manual / "statement.html"
    path.write_bytes(html_page("Download your capital gains statement from the investor portal."))
    os.utime(path, (1_790_000_000, 1_790_000_000))
    env.make().run()
    row = env.manifest.get("statement")
    assert row is not None and row.fetched_at.startswith("2026-09-21")


def test_leftover_old_version_cleaned_on_unchanged_run(env):
    env.make().run()
    env.store.upsert(
        ids=["amfi:oldversion:0"], embeddings=[[0.1] * FakeEmbedder.dims], documents=["stale"],
        metadatas=[{"doc_id": "amfi", "version": "oldversion", "chunk_index": 99}],
    )
    assert statuses(env.make().run())["amfi"] == "unchanged"
    assert all(c["text"] != "stale" for c in env.store.get({"doc_id": "amfi"}))


def test_store_rejects_different_embedding_model(env):
    with pytest.raises(StoreError, match="--rebuild"):
        ChunkStore(env.tmp / "index", "voyage-4")  # IDX-07


def test_clean_text_written_per_document(env):
    import json

    env.make().run()
    record = json.loads((env.tmp / "clean" / "hdfc-mid-cap-kim.json").read_text())
    assert record["content_hash"] == env.manifest.get("hdfc-mid-cap-kim").content_hash
    assert record["as_of"] == "2025-11-21" and record["pages"][0]["number"] == 1
    assert "Exit Load of 1.00%" in record["pages"][0]["text"]


def test_groww_reference_saved_but_never_indexed(env):
    out = env.make().run()
    assert statuses(out)["hdfc-mid-cap-groww"] == "reference"
    assert (env.tmp / "raw" / "hdfc-mid-cap-groww.html").exists()
    assert env.manifest.references()["hdfc-mid-cap"]["file"] == "raw/hdfc-mid-cap-groww.html"
    assert all("groww" not in c["url"] for c in env.store.get())
    assert not (env.tmp / "clean" / "hdfc-mid-cap-groww.json").exists()


def test_groww_failure_does_not_fail_run(env):
    env.site.status[GROWW] = 403
    out = env.make().run()
    assert statuses(out)["hdfc-mid-cap-groww"] == "reference_failed"
    assert out.ok


def test_manifest_survives_reload(env):
    env.make().run()
    reloaded = Manifest(env.tmp / "manifest.json")
    assert reloaded.get("hdfc-mid-cap-sid") == env.manifest.get("hdfc-mid-cap-sid")


def test_factsheet_gets_fact_cards(env):
    env.make().run()
    cards = [c for c in env.store.get({"doc_id": "factsheet"}) if c["kind"] == "fact_card"]
    assert len(cards) == 1 and cards[0]["scheme_id"] == "hdfc-mid-cap"
    assert "Direct: 0.71%" in cards[0]["text"]
    assert env.manifest.get("factsheet").chunk_count == len(env.store.get({"doc_id": "factsheet"}))


def test_chunker_version_change_rechunks_unchanged_content(env, monkeypatch):
    import ingest.pipeline as pipeline

    env.make().run()
    calls = env.embedder.calls
    monkeypatch.setattr(pipeline, "CHUNKER_VERSION", "test-next")
    out = env.make().run()
    assert statuses(out)["hdfc-mid-cap-sid"] == "updated" and env.embedder.calls > calls
    assert env.manifest.get("hdfc-mid-cap-sid").chunker_version == "test-next"
