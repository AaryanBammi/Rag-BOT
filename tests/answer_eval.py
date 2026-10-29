"""Phase 2 answer eval (Docs/eval.md Phase 2): `python -m tests.answer_eval [--runs N]`.

Runs every answerable factual eval question through app.ask (retrieve -> generate -> format)
and checks the Phase 2 exit criteria:
  - correct value, or an honest "not found"; never a wrong value
  - exactly one link, and it's a registry URL
  - footer date equals the cited document's stored date (as_of, else fetched_at)
  - the model's own answer text contains no URL or domain
  - at most 3 sentences
Deferred riskometer questions must come back "not found". Calls Groq; paced to stay under
rate limits. Writes tests/results/<date>-phase2.{json,md}. Use --quick for a ~1 minute
spot check (one question per fact type); the full run uses most of Groq's free daily quota.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from app.ask import Answerer, Reply
from app.config import PROJECT_ROOT
from app.llm import LLMError
from app.validator import source_date
from tests.retrieval_eval import GOLD, QUESTIONS, answerable

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
PACE_SECONDS = 2.0
URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|in|gov\.in|org)\b", re.I)
# Sentence ends: . ! ? followed by space/end, not inside numbers ("0.71%") or common abbreviations.
SENTENCE_END = re.compile(r"(?<!\bRs)(?<!\bNo)(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)[.!?](?=\s|$)")


# Unicode spaces the model sometimes emits inside figures ("Rs.\u202f100") count as plain spaces.
_SPACES = re.compile(r"[\u00a0\u2007\u2009\u202f]")


def normalise(text: str) -> str:
    return _SPACES.sub(" ", text).lower()


def sentences(text: str) -> int:
    return max(1, len(SENTENCE_END.findall(text.strip())))


def grade(q: dict, reply: Reply, answerer: Answerer, registry_urls: set[str]) -> dict:
    out = {"id": q["id"], "question": q["question"], "kind": reply.kind, "url": reply.url,
           "footer": reply.footer_date, "text": reply.text, "problems": []}
    links = len(re.findall(r"https?://", reply.text))
    if links != 1:
        out["problems"].append(f"{links} links")
    if reply.url not in registry_urls:
        out["problems"].append("link not in registry")

    if q.get("status") == "deferred":
        out["result"] = "correct_not_found" if reply.kind == "not_found" else "should_be_not_found"
        return out

    if reply.kind == "not_found":
        out["result"] = "not_found"
        return out
    if reply.kind != "answer":  # validator fell back to the performance or advisory template
        out["result"] = "incorrect"
        out["problems"].append(f"fell back to {reply.kind} template")
        return out

    gold = GOLD[q["expected"]["gold_fact"]]
    answer = reply.generated.answer
    text = normalise(answer)
    ok = any(normalise(a) in text for a in gold["accept"]) and all(normalise(e) in text for e in gold.get("must_also_contain", []))
    out["result"] = "correct" if ok else "incorrect"
    out["answer"] = answer
    out["cited"] = {"doc_id": reply.cited.doc_id, "type": reply.cited.source_type, "kind": reply.cited.kind}
    if URL_RE.search(answer):
        out["problems"].append("model wrote a URL/domain")
    if sentences(answer) > 3:
        out["problems"].append(f"{sentences(answer)} sentences")
    row = answerer.manifest.get(reply.cited.doc_id)
    if reply.footer_date != source_date(row.as_of, row.fetched_at):
        out["problems"].append("footer date mismatch")
    return out


def quick_sample(questions: list[dict]) -> list[dict]:
    """First question of each kind: ids look like F-<SCHEME>-<FACT>-01 or F-VAR-<KIND>-<SCHEME>-01."""
    seen, out = set(), []
    for q in questions:
        parts = q["id"].split("-")
        kind = "-".join(parts[1:3]) if parts[1] in {"VAR", "GEN"} else "-".join(parts[2:-1])
        if kind not in seen:
            seen.add(kind)
            out.append(q)
    return out


def run_once(answerer: Answerer, questions: list[dict], registry_urls: set[str]) -> list[dict]:
    rows = []
    for q in questions:
        for attempt in range(3):
            try:
                reply = answerer.ask(q["question"], q["expected"]["scheme_id"])
                break
            except LLMError as e:
                if e.kind == "rate_limited" and "per day" in str(e):
                    # Daily quota (TPD) is exhausted: retrying won't help for hours.
                    print(f"  STOP: Groq daily token limit reached ({e})", flush=True)
                    return rows
                if e.kind == "rate_limited" and attempt < 2:
                    time.sleep(20)
                    continue
                rows.append({"id": q["id"], "result": "error", "problems": [str(e)]})
                reply = None
                break
        if reply is not None:
            rows.append(grade(q, reply, answerer, registry_urls))
        r = rows[-1]
        detail = f"  -> {r.get('answer') or r.get('text', '')[:80]!r}" if r["result"] not in {"correct", "correct_not_found"} else ""
        print(f"  {r['result']:20} {r['id']:36} {'; '.join(r['problems'])}{detail}", flush=True)
        time.sleep(PACE_SECONDS)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.answer_eval")
    parser.add_argument("--runs", type=int, default=1, help="repeat to measure stability (eval.md uses 3)")
    parser.add_argument("--ids", nargs="+", help="only these question ids (saves Groq tokens)")
    parser.add_argument("--quick", action="store_true",
                        help="one question per fact type (~1 minute, a fraction of the Groq daily quota)")
    args = parser.parse_args(argv)

    answerer = Answerer()
    registry_urls = {s.url for s in answerer.registry.all_sources()}
    questions = answerable() + [q for q in QUESTIONS if q.get("status") == "deferred"]
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    if args.quick:
        questions = quick_sample(questions)
    all_runs = []
    for run in range(1, args.runs + 1):
        print(f"Run {run}/{args.runs}: {len(questions)} questions")
        all_runs.append(run_once(answerer, questions, registry_urls))

    factual = [r for rows in all_runs for r in rows if not r["id"] in {q["id"] for q in QUESTIONS if q.get("status") == "deferred"}]
    deferred = [r for rows in all_runs for r in rows if r not in factual]
    n = max(len(factual), 1)
    count = lambda res: sum(1 for r in factual if r["result"] == res)
    with_problems = [r for rows in all_runs for r in rows if r.get("problems")]
    summary = {
        "date": date.today().isoformat(), "runs": args.runs, "questions": len(questions),
        "answered": len(factual) + len(deferred),
        "correct": count("correct") / n, "not_found": count("not_found") / n,
        "incorrect": count("incorrect") / n, "errors": count("error"),
        "deferred_correct_not_found": sum(r["result"] == "correct_not_found" for r in deferred) / max(len(deferred), 1),
        "responses_with_format_problems": len(with_problems),
    }
    print("\n" + json.dumps(summary, indent=1))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = RESULTS_DIR / f"{date.today().isoformat()}-phase2{'-subset' if args.ids or args.quick else ''}"
    Path(f"{stem}.json").write_text(json.dumps({"summary": summary, "runs": all_runs}, indent=1, ensure_ascii=False))
    lines = [f"# Phase 2 answer eval ({summary['date']})", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v:.0%} |" if isinstance(v, float) else f"| {k} | {v} |" for k, v in summary.items() if k != "date"]
    lines += ["", "## Not correct", ""]
    lines += [f"- **{r['id']}** ({r['result']}): {r.get('answer', '')} {'; '.join(r['problems'])}"
              for rows in all_runs for r in rows if r["result"] not in {"correct", "correct_not_found"}]
    lines += ["", "## Format problems", ""] + [f"- {r['id']}: {'; '.join(r['problems'])}" for r in with_problems]
    Path(f"{stem}.md").write_text("\n".join(lines) + "\n")
    print(f"Saved {stem}.json and .md")
    return 0 if summary["incorrect"] == 0 and not with_problems else 1


if __name__ == "__main__":
    sys.exit(main())
