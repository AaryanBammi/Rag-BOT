# Phase 6 eval (2026-10-28) (subset)

7 passed, 0 failed, 0 errors out of 7 questions.

Classifier `openai/gpt-oss-20b`, generator `openai/gpt-oss-120b`, `MIN_SIMILARITY=0.65`, `RETRIEVAL_TOP_K=8`. Live tokens: none; cached calls: 0; 0.0 min.

## Targets

| Target | Result | Goal | Met |
|---|---|---|---|
| PII blocked, model never called | 7/7 (100%) | 100% | yes |
| Format: ≤3 sentences, one link, footer | 7/7 (100%) | 100% | yes |
| Citation is an official registry URL | 7/7 (100%) | 100% | yes |

## By category

| Category | Pass | Fail | Error |
|---|---|---|---|
| pii | 7 | 0 | 0 |

## Not passing

None.

Skipped (pending_source, statement pages not saved yet): F-GEN-CAPGAINS-01, F-GEN-STATEMENT-01.
