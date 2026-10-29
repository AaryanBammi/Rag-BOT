# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

## Overview

Build a **facts-only FAQ assistant** for mutual fund schemes, using **Groww** as the reference product context. The assistant answers objective, verifiable questions about mutual funds by retrieving information only from official public sources (AMC websites, AMFI, SEBI).

The assistant must **never** give investment advice, opinions, or recommendations. Every response carries exactly one clear source link and follows fixed rules for length, accuracy, and compliance.

> Guiding principle: **accuracy over intelligence.** Users get only verified, source-backed information, with no advisory bias or speculation.

## Objective

Design and implement a lightweight **Retrieval-Augmented Generation (RAG)** assistant that:

- Answers factual queries about mutual fund schemes
- Uses a curated corpus of official documents
- Gives concise, source-backed responses

## Target Users

- Retail investors comparing mutual fund schemes
- Customer support and content teams handling repetitive mutual fund queries

## Scope of Work

### 1. Corpus Definition

- **AMC:** HDFC Mutual Fund (one AMC only)
- **Schemes (Groww reference pages):**

| Scheme | URL |
|---|---|
| HDFC Mid Cap Fund – Direct Growth | https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth |
| HDFC Small Cap Fund – Direct Growth | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| HDFC Large Cap Fund – Direct Growth | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| HDFC Silver ETF FoF – Direct Growth | https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth |
| HDFC Multi Cap Fund – Direct Growth | https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth |
| HDFC ELSS Tax Saver Fund – Direct Plan Growth | https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth |

The ELSS scheme is an addition to the original brief so that ELSS lock-in questions have a scheme in the corpus to answer from.

**Role of Groww vs. official sources:**

- **Groww pages** are used only to pick the schemes and as product context (how a retail investor sees the scheme). They are **not** cited in answers.
- **Answers and citations** come from official sources only:
  - HDFC Mutual Fund (AMC): scheme pages, Scheme Information Document (SID), Key Information Memorandum (KIM), monthly factsheets
  - AMFI: investor-education and regulatory-category pages
  - SEBI: circulars and investor-education pages

### 2. FAQ Assistant Requirements

The assistant answers facts-only queries, for example:

- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- How to download statements or capital gains reports

**Every response must:**

- Be **at most 3 sentences**
- Include **exactly one** citation link
- End with the footer: `Last updated from sources: <date>`

### 3. Refusal Handling

The assistant refuses non-factual or advisory queries, for example:

- "Should I invest in this fund?"
- "Which fund is better?"

**Refusals must:**

- Be polite and clearly worded
- Restate the facts-only limitation
- Include a relevant educational link (e.g. an AMFI or SEBI investor-education resource)

### 4. User Interface (Minimal)

A simple interface with:

- A welcome message
- Three example questions
- A visible disclaimer: **"Facts-only. No investment advice."**
- Support for **multiple independent chat threads** running at the same time

## Constraints

### Data and Sources

- Use only official public sources: AMC, AMFI, SEBI
- No third-party blogs or aggregator websites

### Privacy and Security

Do not collect, store, or process:

- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

### Content Restrictions

- No investment advice or recommendations
- No performance comparisons or return calculations
- For performance-related queries, link to the official factsheet only

### Transparency

- Responses are short, factual, and verifiable
- Every answer includes a source link and a last-updated date

## Expected Deliverables

1. **README** covering:
   - Setup instructions
   - Selected AMC and schemes
   - Architecture overview (RAG approach)
   - Known limitations
2. **Disclaimer snippet:** "Facts-only. No investment advice."
3. **Multi-thread RAG chatbot** that handles several independent conversations simultaneously

## Success Criteria

- Accurate retrieval of factual mutual fund information
- Strict adherence to facts-only responses
- Consistent inclusion of valid source citations
- Correct refusal of advisory queries
- Clean, minimal, user-friendly interface

## Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | Groww pages vs. "official sources only" | Groww is used to pick schemes and for product context only. Answers and citations come from HDFC AMC pages (scheme pages, SID/KIM, factsheets), AMFI, and SEBI. |
| 2 | ELSS lock-in example with no ELSS scheme in the corpus | Added HDFC ELSS Tax Saver Fund – Direct Plan Growth to the corpus. |
| 3 | Source freshness | Each document stores `fetched_at` and `as_of` dates, and refreshes re-embed only documents whose content changed. Re-fetching is manual for now (details below). |

The decisions below were approved on 2026-10-27 from [edge_case.md §11](edge_case.md#11-decisions) (IDs in brackets).

| # | Question | Decision |
|---|---|---|
| 4 | Message mixes a factual and an advisory question (D-01) | Answer only the factual part, plus one fixed sentence saying advice is out of scope, within the 3-sentence limit. |
| 5 | Questions needing several schemes, e.g. comparisons or "all six" (D-02) | Template asking the user to ask about one scheme at a time, listing the scheme names. No rankings or "which is lower" statements. |
| 6 | Greetings and small talk (D-03) | Short welcome template stating what the bot covers, with the AMFI education link and footer. |
| 7 | Scheme-specific question with no scheme known (D-04) | Clarification template asking which scheme, listing the 6 names. No unfiltered search. |
| 8 | PII reply has no link, but every response needs one (D-05) | PII template links to a SEBI/AMFI investor-education page on protecting personal and financial information. |
| 9 | Current NAV questions (D-06, revised 2026-10-28) | The latest NAV of the scheme's Direct Plan – Growth option is looked up live in AMFI's daily NAV file (`portal.amfiindia.com/spages/NAVAll.txt`) and copied exactly; the reply links that file and its footer is the NAV's own date. No LLM generates the figure. Past NAV, NAV change or growth is performance (no figure). Other plans/options (Regular, IDCW) and a feed outage get the scheme page link with no figure. Originally: no figure, scheme page link only. |
| 10 | Conflicting values across documents (D-07) | For changing figures, prefer the most recent `as_of`, and factsheet/scheme page over SID/KIM. SID/KIM are fine for structural facts. |
| 11 | Figures not in the cited source (D-08) | Validator checks that every number and date in the answer appears in the cited chunk. |
| 12 | Return figures leaking into answers (D-09) | Validator rejects return-like content ("CAGR", "% return", "1Y/3Y/5Y", "since inception"). |
| 13 | Abuse and cost control (D-10) | Per-client rate limit (e.g. 20 messages/minute) and a message length limit (e.g. 1,000 characters). |
| 14 | Concurrent messages in one thread (D-11) | Per-thread lock on the server; UI disables sending in a thread while its answer is pending. |
| 15 | Thread privacy without accounts (D-12) | Anonymous browser-generated client ID (localStorage) sent with every request; threads are filtered by it. |
| 16 | Former scheme names (D-13) | `aliases` list per scheme in `sources.yaml`, confirmed from the SIDs, included in the classifier prompt. |
| 17 | Re-ingestion safety (D-14) | Insert new chunks before deleting old ones; update SQLite last, so a failure leaves the previous version intact. |
| 18 | Change detection for HTML pages (D-15) | Hash the parsed main text, not raw bytes (updates the Source Freshness section below). |
| 19 | LLM provider (2026-10-27) | Groq: `openai/gpt-oss-20b` for classification, `openai/gpt-oss-120b` for answer generation, both with strict structured outputs. Embeddings: see row 20. |
| 20 | Embedding provider (2026-10-27) | Local `BAAI/bge-small-en-v1.5` via fastembed by default, so the corpus can be built without any API key (Groq has no embeddings endpoint). Voyage AI remains an option (`EMBEDDING_PROVIDER=voyage`). |
| 21 | Chunking and retrieval (2026-10-27, from [data_research.md](data_research.md)) | ~200-word chunks with a 480-token cap; one fact card per scheme from the factsheet; hybrid search (dense + BM25, RRF); keep `bge-small` and Chroma; no reranker by default; no blanket source-type boost. Implemented and measured on the real index: right chunk in the top 5 for 96% of answerable eval questions (43/45), 14 ms median. |
| 22 | Riskometer classification (2026-10-27) | **Deferred.** The level is stated as text only on HDFC scheme pages, which block automated downloads; every PDF shows it as an image. Riskometer questions get the "not found in official sources" reply with the scheme page link. Scheme pages stay in the registry as citation links (`fetch: none`). To enable later: set them to `fetch: manual`, save the 6 pages, re-ingest. |

## Source Freshness

**Per-document dates.** At ingestion, every document records two dates, copied into the metadata of each of its chunks:

- `fetched_at`: when the pipeline downloaded the document
- `as_of`: the date stated in the document itself (e.g. "as on 30 Sep 2026" on a factsheet, or the effective date of an expense-ratio change)

The response footer uses the cited source's date: `Last updated from sources: <as_of>`, falling back to `fetched_at` when the document states no date.

**Change detection.** The parsed main text of each fetched document is hashed (not the raw bytes, so cosmetic HTML changes such as timestamps or banners don't count as changes). If the hash matches the previous fetch, only `fetched_at` is updated, with no re-chunking or re-embedding. Only documents whose content changed are re-processed.

**Out of scope for now:**

- Scheduled re-fetching per source type (re-fetching is a manual script run)
- A staleness warning in answers when a cited source's `as_of` is older than a threshold
