"""Phase 3 eval (Docs/eval.md Phase 3): `python -m tests.pipeline_eval [--factual | --quick-factual]`.

Sends the guardrail questions (advisory, performance, nav, multi_scheme, greeting, out_of_scope,
clarification, pii, looks_advisory, mixed) through the full pipeline and checks:
  - the reply kind is the expected template (or a factual answer where one is expected)
  - factual answers contain the gold value; mixed questions also carry the advisory aside
  - exactly one link, from the registry, and a footer; at most 3 sentences
  - advisory refusals link AMFI/SEBI education; performance and NAV replies contain no figures
`--factual` also re-runs every Phase 2 factual question through the pipeline (regression; uses
most of Groq's free daily quota), `--quick-factual` one per fact type. Writes
tests/results/<date>-phase3.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

from app.config import PROJECT_ROOT, settings
from app.pipeline import Pipeline
from app.templates import ADVISORY_ASIDE
from app.validator import count_sentences
from tests.answer_eval import normalise, quick_sample
from tests.retrieval_eval import GOLD, QUESTIONS, answerable

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
PHASE3 = {"advisory", "performance", "nav", "multi_scheme", "greeting", "out_of_scope", "clarification",
          "pii", "looks_advisory", "mixed"}
PACE_SECONDS = 1.0
EDUCATION = {"amfi_education", "sebi_education"}


def accepted(q: dict) -> set[str]:
    t = q["expected"].get("template")
    if t is None:
        return {"answer", "not_found"}  # factual: an honest "not found" is allowed, a refusal is not
    return set(t) if isinstance(t, list) else {t}


def grade(q: dict, reply, source_types: dict[str, str]) -> dict:
    exp = q["expected"]
    out = {"id": q["id"], "category": q["category"], "question": q["question"], "kind": reply.kind,
           "intent": reply.intent, "scheme_id": reply.scheme_id, "text": reply.text, "problems": []}
    body = reply.text.split("\n\nSource:")[0]

    if reply.kind not in accepted(q):
        out["problems"].append(f"expected {'/'.join(sorted(accepted(q)))}, got {reply.kind}")
    links = len(re.findall(r"https?://", reply.text))
    if links != 1:
        out["problems"].append(f"{links} links")
    if reply.url and reply.url not in source_types:
        out["problems"].append("link not in registry")
    if "\nLast updated from sources: " not in reply.text:
        out["problems"].append("no footer")
    if count_sentences(body.replace(ADVISORY_ASIDE, "")) > 3:
        out["problems"].append("more than 3 sentences")
    if reply.kind == "advisory" and source_types.get(reply.url) not in EDUCATION:
        out["problems"].append("advisory link is not AMFI/SEBI education")
    if reply.kind in {"performance", "nav"} and re.search(r"\d", body):
        out["problems"].append("figure in performance/NAV reply")

    if reply.kind == "answer":
        gold = GOLD.get(exp.get("gold_fact") or "", {})
        if gold.get("accept") and not any(normalise(a) in normalise(body) for a in gold["accept"]):
            out["problems"].append("wrong value")
        if exp.get("advisory_aside") and ADVISORY_ASIDE not in body:
            out["problems"].append("missing advisory aside")
        if count_sentences(body.replace(ADVISORY_ASIDE, "")) > (2 if exp.get("advisory_aside") else 3):
            out["problems"].append("too many sentences for the aside")
    if exp.get("advisory_aside") and reply.kind != "answer":
        out["problems"].append("mixed question not answered")
    if exp.get("scheme_id") and reply.kind != "pii":
        out["scheme_ok"] = reply.scheme_id == exp["scheme_id"]
    out["result"] = "pass" if not out["problems"] else "fail"
    return out


def run(pipeline: Pipeline, questions: list[dict], source_types: dict[str, str]) -> list[dict]:
    rows = []
    for q in questions:
        for attempt in range(3):
            reply = pipeline.handle(q["question"])
            if reply.kind != "service_unavailable" or attempt == 2:
                break
            time.sleep(20)  # Groq per-minute limit; the pipeline maps the 429 to this template
        if reply.kind == "service_unavailable":
            rows.append({"id": q["id"], "category": q["category"], "result": "error",
                         "problems": ["service unavailable (Groq limit?)"]})
        else:
            rows.append(grade(q, reply, source_types))
        r = rows[-1]
        print(f"  {r['result']:5} {r['id']:30} {r.get('kind', ''):14} {'; '.join(r['problems'])}", flush=True)
        if q["category"] != "pii":
            time.sleep(PACE_SECONDS)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.pipeline_eval")
    parser.add_argument("--factual", action="store_true", help="also every Phase 2 factual question")
    parser.add_argument("--quick-factual", action="store_true", help="also one factual question per fact type")
    parser.add_argument("--ids", nargs="+", help="only these question ids")
    parser.add_argument("--categories", nargs="+", choices=sorted(PHASE3), help="only these categories")
    args = parser.parse_args(argv)

    pipeline = Pipeline()
    source_types = {s.url: s.type for s in pipeline.registry.all_sources()}
    questions = [q for q in QUESTIONS if q["category"] in PHASE3]
    if args.factual:
        questions += answerable()
    elif args.quick_factual:
        questions += quick_sample(answerable())
    if args.ids:
        questions = [q for q in QUESTIONS if q["id"] in set(args.ids)]
    if args.categories:
        questions = [q for q in questions if q["category"] in set(args.categories)]
    print(f"{len(questions)} questions")
    rows = run(pipeline, questions, source_types)

    by_cat: dict[str, Counter] = {}
    for r in rows:
        by_cat.setdefault(r["category"], Counter())[r["result"]] += 1
    scheme_checks = [r["scheme_ok"] for r in rows if "scheme_ok" in r]
    summary = {
        "date": date.today().isoformat(), "classifier_model": settings.classifier_model,
        "generator_model": settings.generator_model, "questions": len(rows),
        "passed": sum(r["result"] == "pass" for r in rows), "failed": sum(r["result"] == "fail" for r in rows),
        "errors": sum(r["result"] == "error" for r in rows),
        "scheme_id_accuracy": round(sum(scheme_checks) / len(scheme_checks), 3) if scheme_checks else None,
        "by_category": {c: dict(v) for c, v in sorted(by_cat.items())},
    }
    print("\n" + json.dumps(summary, indent=1))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = RESULTS_DIR / f"{summary['date']}-phase3{'-subset' if args.ids or args.categories else ''}"
    Path(f"{stem}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False))
    lines = [f"# Phase 3 pipeline eval ({summary['date']})", "",
             f"{summary['passed']} passed, {summary['failed']} failed, {summary['errors']} errors "
             f"out of {summary['questions']}; scheme_id accuracy {summary['scheme_id_accuracy']}.", "",
             f"Classifier `{summary['classifier_model']}`, generator `{summary['generator_model']}`.", "",
             "| Category | Pass | Fail | Error |", "|---|---|---|---|"]
    lines += [f"| {c} | {v.get('pass', 0)} | {v.get('fail', 0)} | {v.get('error', 0)} |" for c, v in summary["by_category"].items()]
    lines += ["", "## Not passing", ""]
    lines += [f"- **{r['id']}** ({r.get('kind', r['result'])}): {'; '.join(r['problems'])}" for r in rows if r["result"] != "pass"]
    Path(f"{stem}.md").write_text("\n".join(lines) + "\n")
    print(f"Saved {stem}.json and .md")
    return 0 if summary["failed"] == 0 and summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
