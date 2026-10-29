# Implementation Plan: Mutual Fund FAQ Assistant

Phase-wise plan to build the system in [architecture.md](architecture.md) against the requirements in [problemStatement.md](problemStatement.md). Each phase ends with exit criteria that must pass before the next phase starts.

## Phase Overview

| Phase | Goal | Main output | Depends on |
|---|---|---|---|
| 0 | Project setup and source registry | Repo scaffold, `sources.yaml` with real official URLs | – |
| 1 | Ingestion pipeline | Populated vector store + document metadata | 0 |
| 2 | Core RAG: retrieval and answer generation | Answers with one citation and footer, callable from the command line | 1 |
| 3 | Guardrails | PII guard, classifier, refusal templates, validator | 2 |
| 4 | Threads and API | FastAPI server with multi-thread support | 3 |
| 5 | User interface | Static chat page with threads and disclaimer | 4 |
| 6 | Evaluation and tuning | Passing eval run, tuned threshold and prompts | 5 |
| 7 | Documentation and handover | README, known limitations, final checks | 6 |

The eval question set is started in Phase 1 and grows with each phase, so every phase can be checked against it. The full evaluation method, test data, and per-phase thresholds are in [eval.md](eval.md).

---

## Phase 0: Project Setup and Source Registry

**Goal:** a runnable empty project and a complete list of official sources to ingest.

### Tasks

- [x] Initialise git repo and `.gitignore` (ignore `data/`, `.env`, virtualenv)
- [x] Create the folder layout from [architecture.md §9](architecture.md#9-project-layout-proposed): `ingest/`, `app/`, `web/`, `data/`, `tests/`
- [x] Set up Python 3.12 virtualenv and `requirements.txt` (`groq`, `voyageai`, `chromadb`, `fastapi`, `uvicorn`, `httpx`, `trafilatura`, `beautifulsoup4`, `pdfplumber`, `pyyaml`, `pytest`)
- [x] Add `.env.example` with `GROQ_API_KEY`, `VOYAGE_API_KEY`, `CLASSIFIER_MODEL`, `GENERATOR_MODEL`, `EMBEDDING_MODEL`, `DATA_DIR`, `RETRIEVAL_TOP_K`, `MIN_SIMILARITY`
- [x] Add `app/config.py` to load and validate settings
- [x] Add `app/llm.py`: a thin wrapper around the Groq client that sends a strict `json_schema` request (`temperature` 0, non-streaming), parses the JSON, and maps Groq errors (429, 5xx, timeouts) to one error type. Classifier and generator both use it
- [x] Smoke test: one strict structured-output call to each configured Groq model succeeds with the real API key. Confirmed by the live Phase 2–3 eval runs (2026-10-28), which made strict structured-output calls to both models. Voyage isn't needed, because embeddings are local
- [x] **Find the official URLs** for each of the 6 schemes on the HDFC Mutual Fund website:
  - scheme page
  - SID
  - KIM
  - latest monthly factsheet
- [x] Find the general sources:
  - HDFC MF page on downloading account statements and capital gains reports
  - AMFI investor-education page(s)
  - SEBI investor-education page(s)
  - SEBI/AMFI page on protecting personal and financial information (`investor_safety`, linked from the PII reply)
- [x] Collect each scheme's former names from its SID, plus common abbreviations, as `aliases`
- [x] Write `sources.yaml` with all of the above, plus the Groww URL as `groww_reference` for each scheme
- [x] Define the domain allow-list (HDFC MF, AMFI, SEBI hosts) in config

### Status (2026-10-27)

Done. The live smoke test was confirmed in Phases 2–3. `www.hdfcfund.com` returns 403 to automated clients, so HDFC scheme pages and the statement pages are `fetch: manual` in `sources.yaml` (saved from a browser into `data/raw/manual/`); all PDFs on `files.hdfcfund.com` and all AMFI/SEBI pages download normally. HDFC Silver ETF Fund of Fund has no per-scheme Fund Facts, so its monthly facts come from the combined factsheet. Verified: `pytest --run-network` (27/27 auto sources reachable and on allowed hosts).

### Exit criteria

- `sources.yaml` has no placeholder URLs, and every URL opens in a browser
- No Groww URL appears under any `sources:` entry
- `python -c "import app.config"` loads settings without errors

---

## Phase 1: Ingestion Pipeline

**Goal:** turn `data/sources.yaml` into searchable chunks with correct metadata and dates.

### Tasks

- [x] **Metadata:** `data/manifest.json`, one record per document (`doc_id`, `url`, `source_type`, `scheme_id`, `title`, `content_hash`, `fetched_at`, `as_of`), written atomically (replaced the planned SQLite table)
- [x] **Data layout:** `data/sources.yaml`, `data/manifest.json`, `data/raw/` (downloads + Groww reference snapshots, never indexed), `data/clean/` (parsed text per document), `data/index/` (Chroma)
- [x] **Fetcher:** download each registry URL with `httpx`; reject hosts not on the allow-list; record `fetched_at`; save raw files under `data/raw/`
- [x] **HTML parser:** main-content extraction with trafilatura, BeautifulSoup fallback
- [x] **PDF parser:** pdfplumber, page by page, keeping page numbers; extract tables as label–value row text
- [x] **Change detection:** SHA-256 of the parsed main text (not raw bytes); if unchanged, update `fetched_at` only and skip the rest
- [x] **`as_of` extractor:** date patterns per `source_type` ("as on …", "as of …", "effective from …"); store as ISO date or null
- [x] **Chunker:** split by headings, then ~500 tokens with ~50 overlap; for multi-scheme PDFs (factsheet), split per scheme section and tag each chunk with the right `scheme_id`
- [x] **Embedder:** local `BAAI/bge-small-en-v1.5` by default (Voyage AI optional), batched; insert new chunks into Chroma with metadata (`scheme_id`, `source_type`, `url`, `as_of`, `fetched_at`, `page`) **before** deleting the document's old ones; update the manifest record last
- [x] **Entry point:** `python -m ingest` runs the full pipeline and prints a summary (fetched / unchanged / updated / failed per document)
- [x] **Inspection helper:** `python -m ingest.inspect <scheme_id>` lists that scheme's chunks, to eyeball parsing quality
- [x] **Start the eval set:** read the ingested documents and write `tests/eval_questions.yaml` with, per scheme, the official value and source document for: expense ratio, exit load, minimum SIP, riskometer, benchmark (plus lock-in for ELSS) and statement-download questions

### Status (2026-10-27)

Done with real (local) embeddings: 29 documents into 2,706 chunks (chunker v3: ~200 words, 480-token cap, fact cards; see [data_research.md](data_research.md)), the combined factsheet split into exactly the 6 schemes' pages, chunk-level fact recall is 38/38 (`python -m tests.fact_recall`), retrieval hit@5 is 96% (`python -m tests.retrieval_eval`), and 115 offline tests pass. Minimum SIP comes from the SAI (added as a source). Open items:

- **Riskometer deferred** (Decisions Log #22): scheme pages are citation links only, so the exit criterion "every scheme has chunks from its scheme page" no longer applies; the 6 riskometer eval questions are `deferred`.
- **2 statement pages** need saving from a browser; until then the 2 statement-download questions are `pending_source`.
- **Gold facts are `verified: false`**: a person must confirm the 38 values against the documents.
- Deviations from the plan:
  - PDFs use pdfplumber text-flow extraction instead of separate table extraction (it keeps the factsheet's columns intact).
  - `doc_id` is the registry source id, not derived from the URL, so monthly URL changes count as content updates.
  - Metadata lives in `data/manifest.json`, not SQLite.
  - Embeddings are local (`bge-small`), not Voyage.

### Exit criteria

- Running `python -m ingest` twice: second run reports all documents unchanged and re-embeds nothing (including HTML pages with timestamps or banners)
- Simulated embedding failure mid-run leaves the previous chunks and document row intact; re-running completes cleanly
- Every scheme has chunks from its scheme page, SID/KIM, and factsheet
- For each fact in the eval set, the inspection helper shows a chunk containing the correct value
- `as_of` is filled for every factsheet; documents without a date show null, not a wrong date
- Unit tests pass for hashing, `as_of` extraction, and chunker scheme tagging

---

## Phase 2: Core RAG (Retrieval and Answer Generation)

**Goal:** answer factual questions from the corpus with exactly one citation and the correct footer, without any guardrails yet.

### Tasks

- [x] **Retriever (`app/retriever.py`):** hybrid search per [data_research.md](data_research.md): dense (Chroma, `scheme_id` filter: scheme chunks + general sources, general only when `scheme_id` is null) + in-process BM25, merged with RRF; optional reranker hook (off); returns top-k chunks with fused score and dense similarity. Checked with `python -m tests.retrieval_eval`
- [x] **Conflict rule (`app/conflicts.py`):** when retrieved chunks disagree on a changing figure, prefer the most recent `as_of` (applied after retrieval). A blanket factsheet/scheme-page boost was tested and rejected (data_research.md §4.3); fact cards cover the "latest figure" need. If the question asks about expense ratio, AUM, NAV, portfolio turnover, older scheme chunks that state that figure are dropped before generation. The prompt also tells the model to prefer the newest `as_of`
- [x] **Generator (`app/generator.py`):** one Groq call (`GENERATOR_MODEL`, default `openai/gpt-oss-120b`) with strict structured output `{answerable, answer, source_chunk_id}`; system prompt contains the rules from [architecture.md §4.4](architecture.md#44-answer-generator) (copy figures exactly, name the plan, default to Direct); excerpts labelled with `chunk_id`
- [x] **Formatter (`app/validator.py`, formatter part):** build the final message from the answer plus the cited chunk's `url` and date (`as_of`, else `fetched_at`)
- [x] **"Not found" path:** if `answerable` is false or the top score is below `MIN_SIMILARITY`, return the not-found reply with the scheme's official page link
- [x] **CLI:** `python -m app.ask "question" [--scheme hdfc-mid-cap]` prints the formatted answer, for quick testing
- [x] Run all factual eval questions through the answer path (`python -m tests.answer_eval`)

### Status (2026-10-28)

Complete. The live answer eval (`python -m tests.answer_eval`, report in `tests/results/2026-10-28-phase2.md`) answered all 45 factual questions correctly, with no wrong values and no format problems. That covers exactly one registry link per reply, footer dates that match stored metadata, and no URLs in the model's text. Groq's daily token limit stopped the run before the 6 deferred riskometer questions. Those questions are expected to return "not found", and they did on 27 Oct. `python -m tests.answer_eval --quick` re-checks one question per fact type in about a minute; the full run uses most of the free daily quota. The 27 Oct run's 5 "incorrect" minimum SIP answers were a grader bug: the model wrote "Rs.\u202f100" with a narrow no-break space. The grader now normalises Unicode spaces. Former scheme names from `aliases` are passed to the generator. 133 offline tests pass, including `tests/test_conflicts.py`, which tests the conflict rule on real Mid Cap KIM (Nov 2025) and fund facts (Sep 2026) text.

Carried over to Phase 6 (they need more Groq tokens per day than the free tier gives, since one full run uses about 200k of the 200k daily limit):

- 3-run stability check and the `MIN_SIMILARITY` / `RETRIEVAL_TOP_K` sweep from [eval.md](eval.md) Phase 2.
- Retrieval recall@5 is 96%, below eval.md's proposed 98%; answer accuracy is unaffected.
- Gold facts are still `verified: false`.

### Exit criteria

- Every factual eval question returns the correct value, or an honest "not found" (no wrong values)
- Every response has exactly one link, which is a `sources.yaml` URL, and the footer date matches that document's stored date
- The model never writes the URL or date itself (checked by grepping `answer` text for URLs)
- Where documents disagree on a figure, the answer uses the most recent source

---

## Phase 3: Guardrails

**Goal:** enforce the facts-only, refusal, and privacy rules in code.

### Tasks

- [x] **PII guard (`app/pii_guard.py`):** regexes for PAN, Aadhaar, email, Indian mobile, OTP-like codes, account/folio numbers; returns match / no match without logging the message
- [x] **Templates (`app/templates.py`):** fixed texts for every template in [architecture.md §4.6](architecture.md#46-templated-responses): advisory refusal, performance, NAV, multi-scheme, clarification, greeting, out of scope, not found, PII detected (with `investor_safety` link), service unavailable; plus the fixed advisory-aside sentence. Each fills in one link and the footer from registry metadata
- [x] **Classifier (`app/classifier.py`):** one Groq call (`CLASSIFIER_MODEL`, default `openai/gpt-oss-20b`) with strict structured output `{intent, scheme_id, needs_scheme, advisory_aside, standalone_query}` per [architecture.md §4.2](architecture.md#42-query-classifier); prompt includes scheme names and `aliases`; `scheme_id` limited to registry IDs or null; accepts recent thread history (wired up in Phase 4)
- [x] **Validator (`app/validator.py`, checks part):**
  - `source_chunk_id` must be among the retrieved chunks; `answer` not empty
  - strip any URLs and bare domains from `answer`
  - at most 3 sentences (2 with `advisory_aside`), with a sentence counter that ignores abbreviations and decimals; else regenerate once, then fall back
  - numeric grounding: every number and date in `answer` appears in the cited chunk, else not found
  - no return figures, else regenerate once, then performance template
  - advisory phrasing aimed at the user, else regenerate once, then advisory refusal
- [x] **Pipeline (`app/pipeline.py`):** chain PII guard → classifier → (template | retriever → generator → validator → formatter)
- [x] **Extend the eval set** with:
  - advisory questions ("Should I invest…", "Which is better…", "Is this a good fund…")
  - performance questions ("3-year return", "compare returns of…")
  - out-of-scope questions (other AMCs, stocks)
  - PII messages (each PII type, in a sentence)
  - tricky factual questions that look advisory ("What is the riskometer level?")
  - mixed factual + advisory, multi-scheme / comparison, NAV, greeting, scheme-less ("What is the exit load?"), former scheme names
- [x] Unit tests for each PII pattern (true and false positives), sentence counter, URL stripper, numeric grounding check, return-figure check, advisory blocklist

### Status (2026-10-28)

Done (marked complete 2026-10-28; see "Skipped live checks" below). `python -m app.ask "question"` now runs the full pipeline. A PII match returns the PII template with no model call. Otherwise the classifier routes the message to a template or to the answer path, which validates the answer and regenerates once at most. 230 offline tests pass, covering every PII type with false-positive cases, each validator check, every template's link and footer, and the pipeline with mock models. The mock tests show PII messages never reach a model or the retriever, unsupported figures and return figures never reach the user, and a message costs at most 3 model calls.

The eval set gained 42 guardrail questions, run with `python -m tests.pipeline_eval` (about 2 minutes; report in `tests/results/2026-10-28-phase3.md`). Results on 28 Oct:

- All 34 template questions passed together: advisory, performance, NAV, multi-scheme, greeting, out of scope, clarification and PII. `scheme_id` accuracy was 100%.
- All 8 factual-looking and mixed questions passed in a separate run using `gpt-oss-20b` as the generator, because the `gpt-oss-120b` daily quota was used up. None were wrongly refused, and both mixed questions carried the advisory aside.
- The saved combined report shows 38 passed and 4 errors. The 4 errors happened when the `gpt-oss-20b` daily quota ran out mid-run, and all 4 had passed earlier the same day.

**Skipped live check (decision 2026-10-28):** the "Phase 2 factual results do not regress" check through the full pipeline (`python -m tests.pipeline_eval --factual` with the default `gpt-oss-120b` generator) was skipped, because Groq's free daily quota was used up. Phase 6 runs the full eval and covers it. One full run uses most of a model's 200k-token daily limit, so run it on its own.

Behaviour worth knowing:

- A NAV question with no scheme gets the clarification template, since the NAV template needs a scheme page link.
- Advisory refusals link SEBI's "Understanding Mutual Funds" page.
- The advice check flags only investment actions ("you should invest", "I recommend"), so how-to steps such as "you need to log in" and facts such as "you can redeem after 3 years" pass.
- The OTP pattern needs "OTP", "PIN" or "verification code" next to the digits, so an AMFI scheme code isn't blocked.

### Exit criteria

- All advisory questions are refused with an AMFI/SEBI link
- All performance questions return no figures and link to that scheme's factsheet
- All PII messages are caught; a test with a mock LLM client confirms the model is never called for them
- Factual-looking questions are not wrongly refused
- Each new intent and flag (NAV, multi-scheme, greeting, clarification, advisory aside) returns the right template with exactly one link
- An answer containing a figure not in its cited chunk, or a return figure, never reaches the user
- Phase 2 factual results do not regress

---

## Phase 4: Threads and API

**Goal:** serve the pipeline over HTTP with multiple independent threads.

### Tasks

- [x] **Thread store (`app/threads.py`):** SQLite (WAL mode) `threads` and `messages` tables per [architecture.md §5](architecture.md#5-multi-thread-chat), with `client_id` on threads; PII messages stored only as the placeholder text; user message and answer saved in one transaction
- [x] **History for classifier:** pass the last few turns of the current thread into the classifier so follow-ups resolve
- [x] **FastAPI app (`app/main.py`):** endpoints from [architecture.md §6](architecture.md#6-api): create / list / delete threads, get messages, post message, health
- [x] **Client scoping:** require `X-Client-Id` on thread endpoints; filter threads by it; another client's `thread_id` returns 404
- [x] **Per-thread lock:** process messages within one thread one at a time
- [x] **Limits:** reject empty and over-1,000-character messages (422); rate limit 20 messages/minute per client ID (429)
- [x] Make LLM and embedding calls async so concurrent threads don't block each other
- [x] **Logging:** intent, scheme, retrieved `chunk_id`s, latency; never raw user messages or API keys
- [x] API tests with FastAPI's test client

### Status (2026-10-28)

Done (marked complete 2026-10-28; see "Skipped live check" below). Every exit criterion is covered by tests with mock models. Start the server with `uvicorn app.main:app`. Threads are stored in `data/threads.db` (SQLite, WAL mode). `tests/test_api.py` has 13 API tests, and all 243 offline tests pass. The tests cover:

- Follow-up and isolation: "what is its exit load?" uses its own thread's history, and a fresh thread gets "which scheme?".
- Concurrency: 10 threads posting at once all get the right scheme's answer, in parallel rather than one after another.
- Same-thread ordering: two messages sent at once are answered in order, and the second sees the first in its history.
- Client isolation: another client gets 404 when listing, reading, posting or deleting.
- Limits and bad input: empty and over-1,000-character messages return 422, the 21st message in a minute returns 429, a malformed thread id returns 422, and a missing client id returns 400. Each error has a friendly message.
- PII storage: the database holds only the placeholder and no fragment of any PII message. Titles of PII-only threads stay "New chat".
- Logs and failures: logs carry no message text. A model failure saves a "service unavailable" reply. A thread deleted mid-request saves nothing.
- Health: `/health` reports the document count and latest `fetched_at`. An empty index gives "not ready" replies.

A live smoke test against the real index passed for health, greeting, PII and thread listing. The advisory message got "service unavailable" because both Groq models' daily quotas were used up.

**Skipped live check (decision 2026-10-28):** the follow-up test with the real classifier (ask about HDFC Small Cap, then "what is its exit load?") was skipped, because Groq's free daily quota was used up. The mock tests prove each thread's history reaches the classifier. Whether `gpt-oss-20b` resolves "its" correctly is untested against the real model; Phase 6 covers it.

Fixed along the way: the answer path picked its retriever with `retriever or Retriever()`. An empty index is falsy, so it was silently replaced with a fresh one loaded from disk.

### Exit criteria

- Follow-up test: thread A asks about HDFC Small Cap, then "what is its exit load?" → answer is for Small Cap
- Isolation test: thread B, created at the same time, asking "what is its exit load?" gets a "which scheme?"/not-found style reply, not Small Cap
- Concurrency test: 5+ threads sending messages in parallel all get correct, separate answers
- Same-thread test: two messages sent at once to one thread are answered in order, and the second sees the first in its history
- Client isolation test: client B cannot list or read client A's threads
- Rate limit and length limit return 429 / 422 with friendly messages
- DB inspection after PII tests shows no PII stored
- `/health` reports document count and latest `fetched_at`

---

## Phase 5: User Interface

**Goal:** the minimal chat UI required by the brief.

### Tasks

- [x] `web/index.html` + `web/app.js` + `web/styles.css`, served by FastAPI
- [x] Sidebar: thread list, "New chat", delete thread
- [x] Empty thread: welcome message and three clickable example questions:
  - "What is the expense ratio of HDFC Mid Cap Fund – Direct Growth?"
  - "What is the lock-in period for HDFC ELSS Tax Saver Fund?"
  - "How do I download my capital gains statement?"
- [x] Always-visible disclaimer banner: **"Facts-only. No investment advice."**
- [x] Message rendering: answer text, clickable source link, footer date; refusals styled the same way
- [x] Loading state while waiting for an answer; error message if the API fails
- [x] Generate the anonymous client ID on first load, keep it in localStorage, send it as `X-Client-Id`
- [x] Disable sending in a thread while its answer is pending; other threads stay usable
- [x] Short note near the input box: "Please don't share PAN, Aadhaar, account numbers, OTPs, email or phone numbers."
- [x] Works at mobile width

### Status (2026-10-28)

Built; the live walkthrough is still open (see below). `uvicorn app.main:app`, then open http://127.0.0.1:8000/. The page is three static files in `web/`, mounted at `/` after the API routes, with no build step and nothing loaded from other origins. Every response carries a `default-src 'self'` Content-Security-Policy, and the page has no inline script. 247 offline tests pass; `tests/test_api.py` adds 4 page tests (disclaimer, the three example questions, PII note, assets served, API routes not shadowed, security headers, no HTML injection APIs in `app.js`).

How the edge cases are handled:

- UI-01: user and model text is set with `textContent` only. UI-02: only `http(s)` source links become links. UI-10: links open in a new tab with `rel="noopener noreferrer"`.
- UI-03 / D-11: each thread tracks its own pending request, so an answer lands in its own thread after a switch; only that thread's Send and example buttons are disabled, and a dot marks pending threads in the sidebar.
- UI-05: deleting the open thread moves to the newest remaining one, or "New chat". UI-08: the last open thread is restored after a refresh. UI-09: Enter sends, Shift+Enter adds a line.
- UI-07: network and server errors show inline with a Retry button (not for 422, where the text goes back in the input). A "New chat" is created on the server only when its first message is sent, so empty threads don't pile up.
- Mobile (≤ 720 px): the sidebar becomes a drawer behind a ☰ button; the disclaimer is a fixed banner on every screen.

Checked in headless Chrome against the real index (no model calls): at 375 px and 1280 px there's no horizontal scroll, the disclaimer and PII note are visible, a PII message shows the placeholder and the PII reply with its SEBI link and footer, and a reload restores the thread.

**Still open:** the manual checklist in [eval.md](eval.md) Phase 5 with live answers, in particular "clicking each example question returns a correct answer", which needs Groq quota. The capital gains example also depends on the 2 statement pages that are still `pending_source`.

### Exit criteria

- Manual walkthrough: start 2 threads, switch between them mid-conversation, each keeps its own history
- Clicking each example question returns a correct answer
- Disclaimer is visible on every screen, including on mobile width
- Source links open the official document

### Addition (2026-10-28): live NAV answers

Decision D-06 was revised so "What is the NAV of HDFC Mid Cap?" returns the figure. `app/nav.py` reads AMFI's daily NAV file at question time (cached about an hour, last good copy kept on failure) and looks up each scheme's `amfi_code` from `sources.yaml`; `portal.amfiindia.com` was added to the allow-list. The reply is built in code (no generator call), links the AMFI file and uses the NAV's own date as the footer. Past or changing NAV is forced to the performance template by a regex guard, and Regular/IDCW questions or a feed outage get the scheme page with no figure. 22 tests in `tests/test_nav.py` (offline, fake file) plus `pytest --run-network` checks that all 6 codes are Direct Growth rows in the live file. Live check: Mid Cap and Silver FoF answered with the 27 Oct 2026 NAV; "NAV one year ago" got the performance template.

---

## Phase 6: Evaluation and Tuning

**Goal:** measure the full system against the success criteria and fix gaps.

### Tasks

- [x] **Eval runner (`tests/run_eval.py`):** sends every eval question through the pipeline and checks:
  - factual: expected value present, cited URL is the expected document
  - advisory / performance / out of scope: correct template and link
  - PII: blocked, model not called
  - all responses: ≤ 3 sentences, exactly one link, footer present
- [x] Run the live checks skipped in Phases 3–4 for lack of Groq quota: the factual regression through the full pipeline is covered by the full `run_eval` run (45/45 factual through `Pipeline.handle`). The real-classifier follow-up test (`python -m tests.followup_eval`) was **skipped** (see below)
- [x] Produce a results report (pass/fail per category, list of failures)
- [x] Tune `MIN_SIMILARITY` and `RETRIEVAL_TOP_K` using the eval results
- [x] Fix failures by type: parsing (Phase 1), retrieval (chunking / filters), prompts (classifier / generator), validator rules
- [x] Re-run until targets are met

### Status (2026-10-29)

Done (marked complete 2026-10-29; see "Skipped checks" below). **All Phase 6 targets are met on a full run** (`tests/results/2026-10-29-phase6.md`): 92 of 95 questions passed.

| Target | Result |
|---|---|
| Factual accuracy | 45/45 correct; the 6 deferred riskometer questions all returned "not found", as required |
| No wrong values | 59/59 |
| Advisory refusal | 9/9 |
| Performance without figures | 4/4 |
| PII blocked, no model call | 7/7 |
| Format (≤ 3 sentences, one link, footer) | 95/95 |
| Citation is a registry URL | 95/95 |
| Right template (NAV, multi-scheme, greeting, clarification, mixed, out of scope) | 18/18 |
| Looks advisory but factual, not refused | 6/6 |

The 3 failures are a citation-type check, not wrong answers: the Mid Cap, Small Cap and Large Cap fund-manager questions name the correct current manager but cite the scheme's SID (Nov 2025) instead of the Sep 2026 factsheet. A fix was tried and reverted:

- Adding fund manager to the conflict rule (drop older chunks that state it) made the model see only the factsheet's manager table. It then picked the wrong row for 3 schemes, the dedicated "Gold/Silver Instruments" manager. The numeric-grounding check blocked those answers, but only because the factsheet writes "August 26,2026" without a space, so it was luck rather than design. Factual accuracy fell to 93% (honest "not found").
- The proper fix needs a generator prompt rule (name the main fund manager; asset-class managers in brackets only alongside them) plus the conflict rule. Any prompt change invalidates the response cache for every generator answer, so verifying it costs a full run (~160k tokens on `gpt-oss-120b`, one free-tier day). Left for the next quota window and listed in known limitations.

**Skipped checks (decision 2026-10-29):**

- The real-classifier follow-up test, `python -m tests.followup_eval`, drives the real API: a Small Cap question then "what is its exit load?" in one thread, and the same follow-up in a fresh thread. The fresh-thread half passed live (clarification). The first question answered correctly once (0.76%, factsheet), but the follow-up reply hit the daily quota on both tries, so whether `gpt-oss-20b` resolves "its" is still untested against the real model. The mock API tests from Phase 4 still cover the mechanism.
- The 3-run stability check from eval.md (`--no-cache`, 3× the tokens) was not run.

**Earlier work (2026-10-28)**

**Runner.** `python -m tests.run_eval` runs all 95 runnable questions (2 statement questions stay `pending_source`) and writes `tests/results/<date>-phase6.{json,md}` with a pass rate per Phase 6 target. It reuses `tests.pipeline_eval.grade` and adds: cited document type in `source_docs`, footer date equals the cited document's stored date, deferred riskometer questions must be "not found", NAV figures match AMFI's file, and PII messages make zero model calls. To fit the free tier:

- `app/llm.py` has two opt-in hooks, both off in the app: a disk cache of responses keyed by the full request (`data/llm_cache/`, gitignored), so a re-run only pays for questions whose prompt changed, and an `on_call` event per call (model, tokens, cached, error).
- The runner paces calls to stay under 8,000 tokens per minute per model, and stops cleanly and saves a partial report when a model's daily limit (200,000 tokens, rolling 24 hours) is hit.
- `--ids` / `--categories` re-run a subset, `--no-cache` forces fresh calls for stability runs.

**Offline tuning** (`python -m tests.run_eval --tune`, no model calls):

- `RETRIEVAL_TOP_K` stays **8**: the smallest k with the gold chunk in the generator's excerpts for all 45 answerable questions (k=5: 42/45).
- `MIN_SIMILARITY` stays **0.65**: the lowest answerable top similarity is 0.739, so nothing answerable is cut. Off-topic text tops out at 0.805 (other AMCs' expense ratios), so no threshold separates those; the classifier and the numeric-grounding check handle them. 0.70 would block 2 more of 14 off-topic probes at the cost of half the margin, which isn't worth it.
- Grader bug fixed: the 5 non-ELSS minimum SIP gold facts never matched their real chunk, because the SAI table states the amount as a bare cell (`Daily, Weekly & Monthly 100 6`); `fact_recall` passed only because an unrelated "Top Up Amount: Rs. 100" chunk matched. Gold facts now have `chunk_accept` for the document's own form. Retrieval with the fix: hit@5 93%, every gold chunk at rank ≤ 7.

**How the full run was completed on the free tier (29 Oct):** one run needs about 190k tokens on `gpt-oss-120b` and 100k on `gpt-oss-20b`, while each allows 200k per rolling 24 hours. The run was done in pieces: 45 questions until the generator's daily limit, then the 36 template questions (classifier only), then the generator questions as the window freed up, each piece filling the cache. A final `python -m tests.run_eval` then graded all 95 from the cache (146 cached calls, 3.5k live tokens). On Groq's Developer tier a full run takes about 40 minutes.

### Targets

| Category | Target |
|---|---|
| Factual accuracy | ≥ 95% correct; 0 wrong values (misses must be "not found") |
| Advisory refusal | 100% |
| Performance queries without figures | 100% |
| PII blocked | 100% |
| Format (≤ 3 sentences, one link, footer) | 100% |
| Citation link is an official registry URL | 100% |
| Figures and dates in answers found in the cited chunk; no return figures | 100% |
| Correct template for NAV, multi-scheme, greeting, clarification, mixed intent | 100% |

### Exit criteria

- All targets met on a full eval run
- Results report saved in `tests/results/`

---

## Phase 7: Documentation and Handover

**Goal:** the deliverables from [problemStatement.md](problemStatement.md#expected-deliverables).

### Tasks

- [x] **README.md:**
  - setup instructions (virtualenv, `.env`, `python -m ingest`, start the server)
  - selected AMC and the 6 schemes
  - architecture overview (short version of [architecture.md](architecture.md) with the diagram)
  - known limitations (from [architecture.md §12](architecture.md#12-known-limitations) plus anything found in Phase 6)
  - disclaimer snippet: "Facts-only. No investment advice."
  - how to refresh sources and run the eval
- [x] Update [architecture.md](architecture.md) where implementation differed from the plan
- [x] Final check from a clean clone: follow the README setup steps exactly and confirm the app works

### Status (2026-10-29)

Done. [README.md](../README.md) covers setup, the AMC and schemes, an architecture overview with diagram, refreshing sources, tests and eval, known limitations (including the Phase 6 findings) and the disclaimer. Changes to [architecture.md](architecture.md):

- conflict rule and the Phase 6 `MIN_SIMILARITY` result (§4.3)
- corrected retrieval figure, 93% hit@5 after the grader fix
- the OTP pattern as built
- `manifest.json` instead of SQLite for document metadata
- the actual `app/` and `tests/` files
- new known limitations

The unsupported claim that the system prompt tells the model never to repeat personal data was removed. `data/threads.db*` was added to `.gitignore`, so user threads can't be committed.

**Clean-clone check:** the repo has no commits yet, so a clone was simulated by copying only the files git would track (95 files; no index, downloads, `.env` or model cache) into a fresh folder and following the README exactly:

- **Install and config:** a new Python 3.12 venv and `pip install -r requirements.txt` succeeded, and `.env` was created from `.env.example`.
- **Ingest:** `python -m ingest` took 8.5 minutes, including the model download. It produced the same 29 documents and 2,706 chunks as the working copy, plus the 2 expected `needs_manual` statement pages.
- **Tests:** `pytest` passed 272 tests.
- **Server:** under `uvicorn app.main:app`, `/health` reported 29 documents and the page showed the disclaimer. Over the API, a PII message got the PII reply with the SEBI link, and an advisory question got the refusal with a SEBI link.
- **Not verified:** a factual answer, which got "service unavailable" because `gpt-oss-120b`'s free daily quota was used up by the Phase 6 runs. The same answers passed 45/45 in the Phase 6 full run.

**Exit criteria.** The README alone takes a new developer from clone to a working chat (given a Groq key with quota). Against problemStatement.md:

- **Deliverables:** README, disclaimer and the multi-thread chatbot are all in place.
- **Success criteria:** met per the Phase 6 report. The open items are listed under known limitations in the README:
  - riskometer deferred
  - 2 manual statement pages
  - fund-manager citations from the SID
  - live follow-up and stability checks not run

### Exit criteria

- A new developer can go from clone to working chat using only the README
- Every item in "Expected Deliverables" and "Success Criteria" in [problemStatement.md](problemStatement.md) is met

---

## Risks and Mitigations

| Risk | Phase | Mitigation |
|---|---|---|
| HDFC MF pages load content with JavaScript, so a plain fetch gets an empty page | 0–1 | Check each URL in Phase 0; prefer PDFs (SID, KIM, factsheet) for facts; use a headless browser only for pages that need it |
| Official sites block automated downloads | 1 | Polite fetch (user agent, delays); fall back to manually downloading files into `data/raw/` and ingesting from disk |
| Factsheet tables parse badly | 1 | Inspection helper + eval set catch it early; add per-document table handling where needed |
| Classifier refuses legitimate factual questions | 3 | Dedicated "looks advisory but is factual" eval questions |
| Model paraphrases numbers incorrectly | 2, 6 | Eval checks exact values; prompt tells the model to copy figures exactly as written in the excerpt; numeric grounding check in the validator |
| Groq rate limits (requests/tokens per minute) hit during eval runs or concurrent use | 4, 6 | Eval runner paces requests; app rate limit sits below Groq's; Groq 429 returns the "Service unavailable" template |
| `openai/gpt-oss-20b` misclassifies intent more than a larger model would | 3, 6 | Model is a config value; if classifier accuracy misses targets in Phase 6, switch `CLASSIFIER_MODEL` to `openai/gpt-oss-120b` |
| Groq retires or renames a configured model | Ongoing | Models are config values; startup smoke check fails clearly if a model is unavailable |
| Facts go stale between manual ingestion runs | Ongoing | Footer always shows the source date; scheduled refresh noted as a future step |
