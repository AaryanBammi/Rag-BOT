# Data Research: Chunking, Embedding Model, and Vector Database

Research on the cleaned corpus (`data/clean/`, 29 documents) to choose the chunking strategy, embedding model, search method and vector database for Phase 2. Run on 2026-10-27; scripts are in [`research/`](../research/).

## 1. Recommendation

| Decision | Current (Phase 1) | Recommended | Evidence |
|---|---|---|---|
| **Chunk size** | ~380 words, 40 overlap | **~200 words, 30 overlap, hard cap 480 tokens** (counted with the model's tokenizer) | 37% of current chunks exceed the model's 512-token limit and are silently truncated; 200 words scored best (§4.1) |
| **Fact cards** | none | **Add one deterministic "fact card" chunk per scheme** from the factsheet (expense ratio, benchmark, exit load, lock-in) | Fixes the one fact every method missed; top-5 hit rate 96% → 98% (§4.4) |
| **Search** | dense only | **Hybrid: dense + BM25, merged with reciprocal rank fusion (RRF)** | Beats dense-only in every configuration, by up to 22 points (§4.1) |
| **Embedding model** | `BAAI/bge-small-en-v1.5` (local) | **Keep `bge-small-en-v1.5`** | `bge-base` scored lower and is 4× slower to embed (§4.2) |
| **Reranker** | none | **None by default.** Re-test `bge-reranker-v2-m3` if a GPU or hosted reranker becomes available | Best quality (MRR .84) but ~11.5 s per question on CPU, over the 5 s budget (§4.3) |
| **Source-type priority** (architecture §4.3) | planned for all questions | **Don't apply a blanket boost.** Fact cards cover the "latest figure" need instead | Blanket boost dropped the top-5 hit rate from 96% to 67% (§4.3) |
| **Vector database** | Chroma (local, persistent) | **Keep Chroma.** Run BM25 in-process over the same chunks | ~5 ms filtered query at this scale; the corpus is too small for a database change to matter (§5) |
| **Text cleaning** | none | **Not now.** Re-test in Phase 6 with a larger eval set | Removing table-of-contents dot leaders and joining hyphenated words improved first-place hits but cost one top-5 hit (§4.2); the 480-token cap fixes the truncation those lines caused |

**Expected result:** right chunk in the top 5 for **98%** of answerable questions (44/45), with **~10 ms** retrieval latency and no extra API keys. **Implemented result:** 96% (43/45) at 14 ms on the real index (§8). The one remaining miss is a former-name question ("HDFC TaxSaver"), which the classifier's query rewrite resolves before search (architecture §4.2).

## 2. Corpus Profile

### 2.1 Size

| Source type | Docs | Tokens | Share |
|---|---|---|---|
| SID | 6 | 259,577 | 35% |
| Combined factsheet (all HDFC schemes; only our 6 schemes' pages are indexed) | 1 | 189,535 | 26% |
| SAI | 1 | 184,319 | 25% |
| KIM | 6 | 77,169 | 10% |
| SEBI education | 6 | 11,783 | 2% |
| Fund Facts | 5 | 8,471 | 1% |
| AMFI education | 3 | 4,764 | 1% |
| Investor safety | 1 | 118 | <1% |
| **Total** | **29** | **735,736** | |

Tokens are counted with the bge tokenizer. The corpus averages **~1.5 tokens per word**, so word-based chunk budgets underestimate model input size by half.

### 2.2 Findings

- **Truncation.** With the current ~380-word chunks, median chunk length is 497 tokens, p90 is 599, and **480 of 1,283 chunks (37%) exceed 512 tokens**. bge models silently drop everything past token 512, so facts near the end of those chunks can't be found by dense search.
- **Boilerplate.** After normalising scheme names, **82% of SID lines and 75% of KIM lines are identical in five of the six schemes' documents**. Scheme-specific facts are a small part of each document, so filtering search to the asked-about scheme is essential, not optional.
- **The SAI dominates general content.** As a general document (it covers every scheme), its ~400 chunks are search candidates for every question. It's needed (it is the only source for minimum SIP amounts) and it didn't hurt results in testing, but it's the main source of noise.
- **Table-of-contents pages.** SID contents pages use dot leaders ("SECTION I ………"), which cost ~170 tokens per line. One such chunk reached 3,873 tokens. These are pure noise for search.
- **Layout.** Factsheet text is mostly short lines (median 4 words per line, 58% of lines ≤4 words): label/value pairs and table cells, not prose. This favours keyword (BM25) matching for exact figures and labels.
- **Extraction artifacts** are minor: ~300 hyphenated line breaks (mostly SIDs/KIMs) and a few dozen glued words. The Silver ETF FoF factsheet's exit-load percentage is drawn as a graphic, so it has no text (its KIM states it).
- **Page headers** ("HDFC Mid Cap Fund-SID") repeat on every page. These help, because they keep the scheme name in chunks that otherwise wouldn't mention it.

## 3. Method

- **Questions:** the 45 answerable factual eval questions (`tests/eval_questions.yaml`), each linked to a gold fact (`tests/gold_facts.yaml`). They cover expense ratio, exit load, benchmark, minimum investment, minimum SIP, fund manager and ELSS lock-in for all six schemes, plus variants (former names, typos, abbreviations, Hinglish). The 8 riskometer/statement questions are excluded until their source pages are saved.
- **Candidates:** filtered as the architecture specifies: the asked-about scheme's chunks plus general chunks (general only when no scheme is named). Questions are searched as written, without the classifier's query rewrite.
- **Hit:** a retrieved chunk counts if it contains an accepted form of the gold value (plus any required context such as "1 year"), comes from an acceptable source type for that question, and belongs to the right scheme or is general.
- **Metrics:** hit@1 / hit@3 / hit@5 (share of questions with a hit in the top 1/3/5) and MRR@10. **hit@5 matters most**, because the generator reads the top 5 chunks.
- **Hardware:** CPU-only laptop; all models run locally via fastembed (ONNX).

## 4. Results

### 4.1 Chunk size × search method (`bge-small`)

| Chunking | Chunks | BM25 | Dense | Hybrid (RRF) |
|---|---|---|---|---|
| A: 380 words (current) | 1,283 | .49 / .84 / .93, MRR .67 | .36 / .69 / .76, MRR .54 | .42 / .78 / .93, MRR .61 |
| **B: 200 words** | 2,574 | .24 / .78 / .87, MRR .52 | .51 / .71 / .80, MRR .64 | **.53 / .93 / .96, MRR .70** |
| C: 120 words | 4,540 | .31 / .64 / .73, MRR .49 | .47 / .67 / .80, MRR .59 | .58 / .76 / .78, MRR .69 |

Values are hit@1 / hit@3 / hit@5.

- Dense-only search is weakest everywhere: it misses exact figures and table-style text (all six minimum-investment questions, which BM25 finds).
- BM25 alone is strong on large chunks but weak on small ones.
- Hybrid combines both and is best or tied-best in every row.
- 200 words is the best size: small enough to stay under the token limit and keep facts focused, large enough to keep labels next to their values.

### 4.2 Text cleaning and embedding model (200-word chunks with cleaning)

| Model | Embed time (2,557 chunks) | Dense | Hybrid |
|---|---|---|---|
| `bge-small-en-v1.5` (384-d) | 4.8 min | .51 / .71 / .82, MRR .63 | .58 / .89 / .93, MRR .72 |
| `bge-base-en-v1.5` (768-d) | 20.8 min | .40 / .51 / .69, MRR .50 | .53 / .71 / .84, MRR .66 |
| `snowflake-arctic-embed-m` | not completed (stopped to save time) | – | – |

- **Cleaning** (stripping table-of-contents dot leaders, joining hyphenated words) vs. no cleaning, both with `bge-small` + hybrid: hit@1 .58 vs .53 and MRR .72 vs .70, but hit@5 .93 vs .96. These are one-question differences on a 45-question set, so not conclusive.
- **`bge-base` was worse and 4× slower.** On this domain the smaller model isn't a bottleneck; the chunking and the search method matter far more.

### 4.3 Reranking and source-type priority (hybrid top 20 → rerank)

| Variant | 200 words | 200 words, cleaned | Latency per question (CPU) |
|---|---|---|---|
| Hybrid | .53 / .93 / .96, MRR .70 | .58 / .89 / .93, MRR .72 | ~10 ms |
| + source-type priority (factsheet/fund facts/scheme page boosted) | .47 / .64 / .67, MRR .57 | .47 / .64 / .67, MRR .57 | ~10 ms |
| + `ms-marco-MiniLM-L-12-v2` reranker | .53 / .84 / .93, MRR .70 | .51 / .82 / .93, MRR .68 | ~2.5 s |
| + `bge-reranker-v2-m3-int8` reranker | .71 / .89 / .98, MRR .81 | **.76 / .91 / .98, MRR .84** | **~11.5 s** |

- A **blanket source-type boost** buries answers that live in the KIM and SAI (minimum investment, minimum SIP): 15 misses.
- **MiniLM** (English, small) adds latency without gains.
- **`bge-reranker-v2-m3`** is clearly the most accurate, and it also helps the Hinglish questions, but its CPU latency is ~2× the whole 5-second response budget.

### 4.4 Fact cards (per-scheme summary chunks from the factsheet)

Every method missed the HDFC ELSS Tax Saver expense ratio: its factsheet chunk is a dense block of numbers, outranked by SID/SAI prose about expense-ratio rules. A fact card is a short chunk built deterministically by pattern extraction from each scheme's factsheet pages, for example:

```
[Fact card | HDFC ELSS Tax Saver Fund | HDFC MF Factsheet - September 2026]
HDFC ELSS Tax Saver Fund - Expense ratio: September 30, 2026 Regular: 1.70% Direct: 1.13%
HDFC ELSS Tax Saver Fund - Benchmark: NIFTY 500 Index (TRI)
HDFC ELSS Tax Saver Fund - Exit load: Nil
HDFC ELSS Tax Saver Fund - Lock-in period: 3 years from the date of allotment of the respective Units
```

| Variant (hybrid, `bge-small`) | Result | Misses |
|---|---|---|
| 200 words | .53 / .93 / .96, MRR .70 | ELSS expense ratio, "HDFC TaxSaver" lock-in |
| **200 words + fact cards** | **.58 / .91 / .98, MRR .74** | "HDFC TaxSaver" lock-in only |
| 200 words cleaned + fact cards | .60 / .91 / .93, MRR .75 | Mid Cap minimum investment, Silver minimum SIP, "HDFC TaxSaver" lock-in |

Fact cards cite the factsheet URL and its `as_of` date like any other chunk, so the footer and grounding checks are unchanged. They're extracted, not generated: values are copied exactly from the text, and a field whose pattern doesn't match is left out.

## 5. Vector Database

| Measure | Value |
|---|---|
| Index size on disk | 32 MB (1,283 chunks × 384-d) |
| Open time | ~200 ms |
| Filtered top-5 query | median 4.9 ms, p95 6.7 ms |
| Query embedding (`bge-small`) | ~9 ms |

At ~2,600 chunks (after the move to 200-word chunks), any local vector store is fast enough. Requirements are metadata filtering (scheme, source type, document version), persistence, versioned upserts and deletes (already built), and no server to run. Chroma meets all of them.

The one capability Chroma doesn't give locally is built-in keyword (BM25) search. Over a few thousand chunks, BM25 in Python takes milliseconds and can be rebuilt at startup from the chunks already in Chroma, so this doesn't justify a migration.

| Option | Verdict |
|---|---|
| **Chroma** (current) | **Keep.** Already integrated and tested; meets every requirement |
| Qdrant (local mode) | Native dense + sparse hybrid and good filtering; worth considering only if the corpus grows by orders of magnitude or hybrid moves server-side |
| LanceDB | Built-in full-text search; no advantage at this scale |
| FAISS | No metadata filtering or persistence model; would need extra code |
| pgvector / hosted services | Need a server or account; unnecessary for a local, single-AMC corpus |

## 6. Changes Needed (if approved)

**Phase 1 (ingestion)**

- `ingest/chunk.py`: target ~200 words / 30 overlap, plus a hard cap of 480 tokens counted with the embedding model's tokenizer.
- New fact-card step for the combined factsheet: one card per scheme with expense ratio, benchmark, exit load and lock-in where present. Cards are tagged with their scheme and cite the factsheet.
- Re-run `python -m ingest --rebuild`; re-check fact recall.

**Phase 2 (retrieval)**

- `app/retriever.py`: hybrid search using Chroma dense results plus an in-process BM25 index over the same chunks, merged with RRF, top 5 to the generator.
- Drop the blanket source-type boost from architecture §4.3. The "most recent figure wins" rule stays for genuine conflicts between documents, applied after retrieval.
- Keep a reranker hook (off by default) so `bge-reranker-v2-m3` can be switched on if a faster option becomes available.

**Docs:** update architecture §3.2 (chunking), §4.3 (retriever) and §8 (stack), and record the decision in the problemStatement Decisions Log.

## 7. Limitations

- **Small question set.** 45 questions, so a 2-point difference is one question. The recommendations rest on large, consistent gaps (truncation, hybrid vs dense, blanket priority, reranker latency), not on small ones. Phase 6 re-runs this with the full eval set.
- **Single-fact questions only.** Process questions (statements, KYC) and riskometer questions aren't covered until their source pages are saved.
- **Questions searched as written.** The classifier's query rewrite (aliases, Hinglish) should improve dense results further; this study doesn't include it.
- **`snowflake-arctic-embed-m` was not completed**, and Voyage AI was not tested (no API key).
- **String-match scoring** checks that the right value is in a retrieved chunk, not that the generator uses it correctly; generation is measured in Phase 2.

## 8. Implementation Results (2026-10-27)

The recommendation was implemented and measured on the real index (`python -m tests.retrieval_eval`, same 45 questions and hit criteria):

| Version | Chunks | hit@1 / hit@3 / hit@5 | MRR@10 | Latency (median) |
|---|---|---|---|---|
| Phase 1 baseline (380 words, dense only) | 1,283 | .36 / .69 / .76 (research run) | .54 | – |
| Chunker v2: 200 words + 480-token cap + fact cards, hybrid | 2,646 | .56 / .80 / .93 | .70 | 13 ms |
| **Chunker v3: + numbered section headings** | **2,706** | **.60 / .87 / .96** | **.75** | **14 ms** |

- **No chunk exceeds the model limit.** Max 477 tokens (from 3,873), median 272; 0 chunks over 512 (from 37%).
- **Chunker v3** also starts a chunk at numbered/roman-numeral section titles ("11. Minimum Application Amount / Number of Units"), which keeps KIM/SID section titles with their values. v2 had split that title from its amounts.
- **Remaining misses:**
  - HDFC Large Cap minimum investment (rank 7).
  - The former-name question "HDFC TaxSaver" (rank 6). Searched with the name the classifier will rewrite it to ("HDFC ELSS Tax Saver Fund"), it ranks **1st** via the fact card.
- **Implementation vs. experiment.** The real pipeline's chunk boundaries differ from the experiment's (token cap, overlap rule), so v2 scored 93% top-5 against the experiment's 98% until the heading fix. Widening the fusion pool from 50 to 200 or 2,000 per method didn't help (91%).
- **Fact recall** remains 38/38.

## 9. Reproducing

```bash
.venv/bin/python research/profile_corpus.py                       # corpus profile (§2)
.venv/bin/python research/retrieval_experiment.py BAAI/bge-small-en-v1.5 A_380w,B_200w,C_120w   # §4.1
.venv/bin/python research/retrieval_experiment.py BAAI/bge-small-en-v1.5,BAAI/bge-base-en-v1.5 D_200w_clean  # §4.2
.venv/bin/python research/rerank_experiment.py B_200w BAAI/bge-small-en-v1.5                    # §4.3
.venv/bin/python research/factcard_experiment.py B_200w                                         # §4.4
```

Embeddings are cached in `research/*.npy` (git-ignored), so re-runs only re-embed when a configuration changes.
