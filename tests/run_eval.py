"""Phase 6 eval (Docs/eval.md Phase 6): every eval question through the full pipeline.

    python -m tests.run_eval --tune              # offline: MIN_SIMILARITY and TOP_K from retrieval scores
    python -m tests.run_eval                     # live: all questions, cached, paced under Groq limits
    python -m tests.run_eval --ids F-... N-...   # re-run some questions
    python -m tests.run_eval --no-cache          # fresh model calls (stability runs)

Checks per question (on top of tests.pipeline_eval.grade: reply kind, one registry link, footer,
<= 3 sentences, no figures in performance/NAV-fallback replies, AMFI/SEBI link on refusals):
  - factual: gold value present; cited document type is one of `source_docs`; footer date equals
    the cited document's stored date; deferred questions must be "not found"
  - nav_value: the figure equals AMFI's file for the scheme's code
  - pii: no model call at all
Model responses are cached in data/llm_cache/ (keyed by the full request), so re-runs only pay
for questions whose prompt changed. Calls are paced to stay under each model's tokens-per-minute
limit; when Groq's daily limit is hit the run stops and saves what it has. Writes
tests/results/<date>-phase6.{json,md} with pass rates against the Phase 6 targets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, deque
from datetime import date
from pathlib import Path

from app import llm
from app.config import PROJECT_ROOT, settings
from app.pipeline import Pipeline
from app.validator import count_sentences, source_date
from tests.answer_eval import URL_RE, normalise
from tests.pipeline_eval import grade as grade_template
from tests.retrieval_eval import GOLD, QUESTIONS, is_hit

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
CACHE_DIR = settings.data_dir / "llm_cache"
TPM_LIMIT = 8000          # Groq free tier, per model (x-ratelimit-limit-tokens)
TOKENS_PER_CALL = {settings.classifier_model: 2000, settings.generator_model: 5000}  # pessimistic budget

TARGETS = [  # (name, categories or None for all, metric, target)
    ("Factual accuracy (correct or honest not found, ≥95% correct)", {"factual"}, "factual", 0.95),
    ("No wrong values", {"factual", "looks_advisory", "mixed"}, "no_wrong", 1.0),
    ("Advisory refusal", {"advisory"}, "pass", 1.0),
    ("Performance queries without figures", {"performance"}, "pass", 1.0),
    ("PII blocked, model never called", {"pii"}, "pass", 1.0),
    ("Format: ≤3 sentences, one link, footer", None, "format", 1.0),
    ("Citation is an official registry URL", None, "registry_link", 1.0),
    ("Right template: NAV, multi-scheme, greeting, clarification, mixed, out of scope",
     {"nav", "multi_scheme", "greeting", "clarification", "mixed", "out_of_scope"}, "pass", 1.0),
    ("Looks advisory but factual: not refused", {"looks_advisory"}, "pass", 1.0),
]
FORMAT_PROBLEMS = re.compile(r"links$|^no footer$|more than 3 sentences|too many sentences")


# --- pacing -----------------------------------------------------------------------------------

class Meter:
    """Tracks each model's token use for pacing and the report; spots the daily limit."""

    def __init__(self) -> None:
        self.window: dict[str, deque[tuple[float, int]]] = {}
        self.tokens: Counter = Counter()
        self.calls: Counter = Counter()
        self.cached: Counter = Counter()
        self.daily_limit_hit: str | None = None

    def __call__(self, event: dict) -> None:
        model = event["model"]
        if event["cached"]:
            self.cached[model] += 1
            return
        self.calls[model] += 1
        self.tokens[model] += event["tokens"]
        self.window.setdefault(model, deque()).append((time.monotonic(), event["tokens"]))
        if event["error"] == "rate_limited" and "per day" in (event["message"] or "").lower():
            self.daily_limit_hit = event["message"]

    def used_last_minute(self, model: str) -> int:
        q = self.window.get(model, deque())
        while q and time.monotonic() - q[0][0] > 60:
            q.popleft()
        return sum(t for _, t in q)

    def wait_for_room(self) -> None:
        """Sleep until every model has room for one more call within its per-minute budget."""
        while True:
            waits = []
            for model, need in TOKENS_PER_CALL.items():
                q = self.window.get(model)
                if q and self.used_last_minute(model) + need > TPM_LIMIT:
                    waits.append(60 - (time.monotonic() - q[0][0]) + 0.5)
            if not waits:
                return
            time.sleep(max(1.0, min(waits)))


# --- grading ----------------------------------------------------------------------------------

def accepted(q: dict) -> set[str] | None:
    if q.get("status") == "deferred":
        return {"not_found"}
    return None


def grade(q: dict, reply, pipeline: Pipeline, source_types: dict[str, str], model_calls: int) -> dict:
    if q.get("status") == "deferred":  # riskometer: must be an honest not found
        q = {**q, "expected": {**q["expected"], "template": "not_found"}}
    out = grade_template(q, reply, source_types)
    exp = q["expected"]
    out["registry_link"] = bool(reply.url) and reply.url in source_types
    out["format_ok"] = not any(FORMAT_PROBLEMS.search(p) for p in out["problems"])
    out["model_calls"] = model_calls

    if reply.kind == "answer" and reply.cited is not None:
        out["cited"] = f"{reply.cited.doc_id} [{reply.cited.source_type}]"
        docs = exp.get("source_docs")
        if docs and reply.cited.source_type not in docs:
            out["problems"].append(f"cited {reply.cited.source_type}, expected {'/'.join(docs)}")
        row = pipeline.answerer.manifest.get(reply.cited.doc_id)
        if row and reply.footer_date != source_date(row.as_of, row.fetched_at):
            out["problems"].append("footer date mismatch")
        if reply.generated and URL_RE.search(reply.generated.answer):
            out["problems"].append("model wrote a URL")
        gold = GOLD.get(exp.get("gold_fact") or "", {})
        if gold.get("accept"):
            body = normalise(reply.text.split("\n\nSource:")[0])
            out["correct"] = any(normalise(a) in body for a in gold["accept"])
            out["wrong_value"] = not out["correct"]
    if q["category"] == "factual":
        out.setdefault("correct", False)
        out["honest_miss"] = reply.kind == "not_found"
    if reply.kind == "nav_value":
        code = pipeline.registry.scheme(reply.scheme_id).amfi_code
        row = pipeline.nav.get(code)
        if not row or f"₹{row.nav} " not in reply.text:
            out["problems"].append("NAV does not match AMFI file")
    if q["category"] == "pii" and model_calls:
        out["problems"].append(f"{model_calls} model call(s) for a PII message")
    out["result"] = "pass" if not out["problems"] else "fail"
    return out


def score(rows: list[dict]) -> list[dict]:
    graded = [r for r in rows if r["result"] != "error"]
    out = []
    for name, cats, metric, target in TARGETS:
        pool = [r for r in graded if cats is None or r["category"] in cats]
        if metric == "factual":
            pool = [r for r in pool if r.get("expected_template") is None]
            ok = sum(r.get("correct", False) for r in pool)
        elif metric == "no_wrong":
            ok = sum(not r.get("wrong_value", False) for r in pool)
        elif metric == "format":
            ok = sum(r["format_ok"] for r in pool)
        elif metric == "registry_link":
            ok = sum(r["registry_link"] for r in pool)
        else:
            ok = sum(r["result"] == "pass" for r in pool)
        rate = ok / len(pool) if pool else None
        out.append({"target": name, "ok": ok, "n": len(pool), "rate": rate, "goal": target,
                    "met": None if rate is None else rate >= target})
    return out


# --- runs -------------------------------------------------------------------------------------

def select(args) -> tuple[list[dict], list[dict]]:
    skipped = [q for q in QUESTIONS if q.get("status") == "pending_source"]
    questions = [q for q in QUESTIONS if q.get("status") != "pending_source"]
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    if args.categories:
        questions = [q for q in questions if q["category"] in set(args.categories)]
    return questions, skipped


def run(pipeline: Pipeline, questions: list[dict], meter: Meter) -> list[dict]:
    source_types = {s.url: s.type for s in pipeline.registry.all_sources()}
    rows = []
    for n, q in enumerate(questions, 1):
        if meter.daily_limit_hit:
            break
        meter.wait_for_room()
        before = sum(meter.calls.values()) + sum(meter.cached.values())
        reply = pipeline.handle(q["question"])
        calls = sum(meter.calls.values()) + sum(meter.cached.values()) - before
        if reply.kind == "service_unavailable":
            if meter.daily_limit_hit:
                print(f"  STOP: Groq daily limit reached at {q['id']}", flush=True)
                break
            time.sleep(30)  # per-minute limit despite pacing: one retry
            reply = pipeline.handle(q["question"])
        if reply.kind == "service_unavailable":
            row = {"id": q["id"], "category": q["category"], "result": "error",
                   "problems": ["service unavailable"], "question": q["question"]}
        else:
            row = grade(q, reply, pipeline, source_types, calls)
            row["expected_template"] = q["expected"].get("template") if q.get("status") != "deferred" else "not_found"
        rows.append(row)
        print(f"  [{n}/{len(questions)}] {row['result']:5} {row['id']:34} {row.get('kind', ''):13} "
              f"{'; '.join(row['problems'])}", flush=True)
    return rows


def report(rows: list[dict], targets: list[dict], meter: Meter, skipped: list[dict], args, elapsed: float) -> Path:
    today = date.today().isoformat()
    subset = bool(args.ids or args.categories)
    stem = RESULTS_DIR / f"{today}-phase6{'-subset' if subset else ''}"
    summary = {
        "date": today, "classifier_model": settings.classifier_model, "generator_model": settings.generator_model,
        "min_similarity": settings.min_similarity, "top_k": settings.retrieval_top_k,
        "questions": len(rows), "passed": sum(r["result"] == "pass" for r in rows),
        "failed": sum(r["result"] == "fail" for r in rows), "errors": sum(r["result"] == "error" for r in rows),
        "skipped_pending_source": [q["id"] for q in skipped],
        "stopped_on_daily_limit": bool(meter.daily_limit_hit),
        "tokens": dict(meter.tokens), "live_calls": dict(meter.calls), "cached_calls": dict(meter.cached),
        "minutes": round(elapsed / 60, 1),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    Path(f"{stem}.json").write_text(json.dumps({"summary": summary, "targets": targets, "rows": rows},
                                               indent=1, ensure_ascii=False, default=str))
    by_cat: dict[str, Counter] = {}
    for r in rows:
        by_cat.setdefault(r["category"], Counter())[r["result"]] += 1
    pct = lambda t: "–" if t["rate"] is None else f"{100 * t['rate']:.0f}%"
    lines = [f"# Phase 6 eval ({today}){' (subset)' if subset else ''}", "",
             f"{summary['passed']} passed, {summary['failed']} failed, {summary['errors']} errors out of "
             f"{summary['questions']} questions"
             + (" (stopped early: Groq daily limit)" if summary["stopped_on_daily_limit"] else "") + ".", "",
             f"Classifier `{summary['classifier_model']}`, generator `{summary['generator_model']}`, "
             f"`MIN_SIMILARITY={summary['min_similarity']}`, `RETRIEVAL_TOP_K={summary['top_k']}`. "
             f"Live tokens: {', '.join(f'{m} {t:,}' for m, t in summary['tokens'].items()) or 'none'}; "
             f"cached calls: {sum(summary['cached_calls'].values())}; {summary['minutes']} min.", "",
             "## Targets", "", "| Target | Result | Goal | Met |", "|---|---|---|---|"]
    met = lambda t: "–" if t["met"] is None else "yes" if t["met"] else "NO"
    lines += [f"| {t['target']} | {t['ok']}/{t['n']} ({pct(t)}) | {100 * t['goal']:.0f}% | {met(t)} |"
              for t in targets if t["n"] or not subset]
    lines += ["", "## By category", "", "| Category | Pass | Fail | Error |", "|---|---|---|---|"]
    lines += [f"| {c} | {v.get('pass', 0)} | {v.get('fail', 0)} | {v.get('error', 0)} |" for c, v in sorted(by_cat.items())]
    lines += ["", "## Not passing", ""]
    lines += [f"- **{r['id']}** ({r.get('kind', r['result'])}): {'; '.join(r['problems'])}  \n  Q: {r['question']}"
              + (f"  \n  A: {r['text'].split(chr(10) + chr(10) + 'Source:')[0]}" if r.get("text") else "")
              for r in rows if r["result"] != "pass"] or ["None."]
    if skipped:
        lines += ["", f"Skipped (pending_source, statement pages not saved yet): {', '.join(q['id'] for q in skipped)}."]
    Path(f"{stem}.md").write_text("\n".join(lines) + "\n")
    return stem


# --- offline tuning ---------------------------------------------------------------------------

def tune() -> int:
    """MIN_SIMILARITY and TOP_K from retrieval alone (no model calls)."""
    from app.retriever import Retriever

    retriever = Retriever()
    answerable = [q for q in QUESTIONS if q["category"] == "factual" and q.get("status") is None
                  and GOLD.get(q["expected"].get("gold_fact", ""), {}).get("value")]
    # Questions that reach retrieval but have no answer in the corpus, plus off-topic text.
    negatives = [q["question"] for q in QUESTIONS if q["category"] in {"out_of_scope", "greeting"}]
    negatives += ["What is the capital of France?", "Write me a poem about the sea", "How do I bake bread?",
                  "What is the share price of Reliance Industries?", "Who won the cricket world cup?",
                  "What is the expense ratio of SBI Bluechip Fund?", "What is the exit load of Axis Midcap Fund?"]
    ks = (3, 5, 8, 10, 12, 15)
    first_hit, pos_sims = [], []
    for q in answerable:
        res = retriever.search(q["question"], q["expected"]["scheme_id"], top_k=max(ks))
        pos_sims.append(max(c.similarity for c in res[:settings.retrieval_top_k]))
        first_hit.append(next((i for i, c in enumerate(res, 1) if is_hit(c, q)), None))
    neg_sims = [max(c.similarity for c in retriever.search(t, None, top_k=settings.retrieval_top_k)) for t in negatives]

    n = len(answerable)
    print(f"\nTOP_K (hit@k = gold chunk among the k chunks the generator sees; {n} answerable questions)")
    words = sorted(len(c.split()) for c in retriever._texts) if hasattr(retriever, "_texts") else None
    for k in ks:
        hit = sum(1 for f in first_hit if f and f <= k)
        extra = f"   ~{int(k * (sum(words) / len(words)) * 1.4):,} prompt tokens of excerpts" if words else ""
        print(f"  k={k:2}  hit@k {hit}/{n} = {hit / n:.1%}{extra}")
    misses = [q["id"] for q, f in zip(answerable, first_hit) if not f or f > settings.retrieval_top_k]
    print(f"  misses at current k={settings.retrieval_top_k}: {misses or 'none'}")

    print("\nMIN_SIMILARITY (top dense similarity)")
    print(f"  answerable: min {min(pos_sims):.3f}, 5th pct {sorted(pos_sims)[max(0, n // 20 - 1)]:.3f}, median {sorted(pos_sims)[n // 2]:.3f}")
    print(f"  off-topic : max {max(neg_sims):.3f}, median {sorted(neg_sims)[len(neg_sims) // 2]:.3f}")
    for t in (0.60, 0.62, 0.65, 0.68, 0.70, 0.72):
        fn = sum(s < t for s in pos_sims)
        fp = sum(s >= t for s in neg_sims)
        print(f"  threshold {t:.2f}: answerable wrongly 'not found' {fn}/{n}, off-topic passed to generator {fp}/{len(neg_sims)}")
    print(f"  current MIN_SIMILARITY={settings.min_similarity}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.run_eval")
    parser.add_argument("--tune", action="store_true", help="offline MIN_SIMILARITY / TOP_K analysis, no model calls")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--categories", nargs="+")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    if args.tune:
        return tune()

    meter = Meter()
    llm.on_call = meter
    llm.enable_cache(None if args.no_cache else CACHE_DIR)
    pipeline = Pipeline()
    questions, skipped = select(args)
    print(f"{len(questions)} questions ({len(skipped)} pending_source skipped); cache {'off' if args.no_cache else CACHE_DIR}")
    started = time.monotonic()
    rows = run(pipeline, questions, meter)
    targets = score(rows)
    stem = report(rows, targets, meter, skipped, args, time.monotonic() - started)
    for t in targets:
        if t["n"]:
            print(f"  {'ok ' if t['met'] else 'NO '} {t['target']}: {t['ok']}/{t['n']} ({t['rate']:.0%})")
    print(f"Tokens: {dict(meter.tokens)}  cached calls: {sum(meter.cached.values())}\nSaved {stem}.md and .json")
    return 0 if all(t["met"] is not False for t in targets) and not meter.daily_limit_hit else 1


if __name__ == "__main__":
    sys.exit(main())
