"""Live follow-up check (implementation_plan.md Phase 4 exit criteria, run in Phase 6).

    python -m tests.followup_eval

Drives the real API (real classifier, generator and index) with a throwaway thread database:
thread A asks about HDFC Small Cap, then "what is its exit load?"; thread B, created at the same
time, asks only "what is its exit load?". A must answer for Small Cap (cites a Small Cap
document, contains the gold exit load); B must ask which scheme. Uses about 4 model calls.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.registry import load_registry
from app.threads import ThreadStore
from tests.answer_eval import normalise
from tests.retrieval_eval import GOLD

HEADERS = {"X-Client-Id": "followup-eval-client"}
FIRST = "What is the expense ratio of HDFC Small Cap Fund – Direct Growth?"
FOLLOW_UP = "what is its exit load?"


def main() -> int:
    registry = load_registry()
    allowed = {s.url for s in registry.scheme("hdfc-small-cap").sources} | {s.url for s in registry.general_of_type("factsheet")}

    def has_gold(reply: dict, fact: str) -> bool:
        body = normalise(reply["content"].split("\n\nSource:")[0])
        return reply["kind"] == "answer" and reply["source_url"] in allowed and \
            any(normalise(x) in body for x in GOLD[fact]["accept"])

    with tempfile.TemporaryDirectory() as tmp, \
            TestClient(create_app(store=ThreadStore(Path(tmp) / "threads.db"))) as c:
        a = c.post("/threads", headers=HEADERS).json()["thread_id"]
        b = c.post("/threads", headers=HEADERS).json()["thread_id"]

        def ask(tid: str, text: str) -> dict:
            reply = c.post(f"/threads/{tid}/messages", json={"content": text}, headers=HEADERS).json()["reply"]
            if reply["kind"] == "service_unavailable":
                time.sleep(60)
                reply = c.post(f"/threads/{tid}/messages", json={"content": text}, headers=HEADERS).json()["reply"]
            return reply

        replies = {"A1": ask(a, FIRST), "A2": ask(a, FOLLOW_UP), "B1": ask(b, FOLLOW_UP)}

    checks = {
        "A1 gives Small Cap's expense ratio": has_gold(replies["A1"], "hdfc-small-cap.expense_ratio_direct"),
        "A2 follow-up gives Small Cap's exit load": has_gold(replies["A2"], "hdfc-small-cap.exit_load"),
        "B1 fresh thread asks which scheme": replies["B1"]["kind"] in {"clarification", "not_found"},
    }
    for key, r in replies.items():
        print(f"--- {key} [{r['kind']}]\n{r['content']}\n")
    for name, ok in checks.items():
        print(f"  {'ok ' if ok else 'NO '} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
