"""Consistency checks for the eval data files (Docs/eval.md §2)."""

import yaml

from app.config import PROJECT_ROOT
from app.registry import load_registry

GOLD = yaml.safe_load((PROJECT_ROOT / "tests" / "gold_facts.yaml").read_text(encoding="utf-8"))
QUESTIONS = yaml.safe_load((PROJECT_ROOT / "tests" / "eval_questions.yaml").read_text(encoding="utf-8"))
REGISTRY = load_registry()
SOURCE_IDS = {s.id for s in REGISTRY.all_sources()}


def test_gold_facts_reference_registry_sources_and_schemes():
    for key, fact in GOLD.items():
        scheme = key.split(".")[0]
        assert scheme == "general" or scheme in REGISTRY.scheme_ids, key
        assert fact["source_doc"] in SOURCE_IDS, key


def test_gold_facts_with_values_have_accept_forms_and_hash():
    for key, fact in GOLD.items():
        if fact.get("value"):
            assert fact["accept"] and fact.get("doc_hash"), key
        else:
            assert fact.get("status") in {"pending_source", "deferred"}, key


def test_question_ids_unique_and_gold_keys_exist():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    for q in QUESTIONS:
        gold_key = q["expected"].get("gold_fact")
        if gold_key:
            assert gold_key in GOLD, q["id"]
        scheme = q["expected"]["scheme_id"]
        assert scheme is None or scheme in REGISTRY.scheme_ids, q["id"]
