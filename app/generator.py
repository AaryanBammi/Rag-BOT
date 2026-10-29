"""Answer generator (architecture.md §4.4).

One Groq call with strict structured output {answerable, answer, source_chunk_id}. The schema
restricts source_chunk_id to the ids of the retrieved excerpts (or "" when not answerable), so
the model can only cite a chunk it was actually given. The model never writes the link or the
date: the formatter takes both from the cited chunk's metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.llm import acomplete_json, complete_json
from app.retriever import RetrievedChunk

MAX_COMPLETION_TOKENS = 1500

SYSTEM_PROMPT = """\
You answer factual questions about HDFC Mutual Fund schemes using ONLY the excerpts provided.
The excerpts come from official HDFC Mutual Fund, AMFI and SEBI documents.

Rules:
1. Use only facts stated in the excerpts. Never use outside knowledge. If the excerpts don't \
contain the answer, set "answerable" to false, "answer" to "" and "source_chunk_id" to "".
2. Copy figures exactly as written in the excerpt, including units and symbols (e.g. "0.71%", \
"Rs. 100", "1 year"). Only use figures the excerpt states as the scheme's actual terms. Never \
use numbers from illustrations, worked examples or sample calculations (e.g. "Amount invested \
at the beginning of the year (Rs.) 10,000"), or figures that belong to a different scheme.
3. "Minimum investment" or "minimum application amount" means the minimum amount for a purchase \
(lump sum), not the minimum SIP instalment; answer the one that was asked.
4. Expense ratios and other figures can differ between the Regular and Direct plans. Say which \
plan a figure belongs to. If the question doesn't name a plan, give the Direct plan figure.
5. Answer in at most 3 short, plain sentences. No lists, no headings, no links or website \
addresses, no dates of your own.
6. Facts only: no advice, recommendations, opinions, predictions, comparisons between schemes, \
or return/performance figures or calculations.
7. Base the answer on ONE excerpt and give its id in "source_chunk_id". Choose the excerpt that \
states the fact most directly. If excerpts disagree on a figure that changes over time (such as \
an expense ratio), use the one with the most recent "as of" date.
8. Answer in English even if the question uses Hindi or Hinglish words.
9. The question may use a former name or short name of the scheme. The "Scheme the question is \
about" line lists those names; treat them all as the same scheme and answer with its current name.
"""


@dataclass(frozen=True)
class GeneratedAnswer:
    answerable: bool
    answer: str
    source_chunk_id: str  # "" when not answerable


def answer_schema(chunk_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "answerable": {"type": "boolean"},
            "answer": {"type": "string"},
            "source_chunk_id": {"type": "string", "enum": [*chunk_ids, ""]},
        },
        "required": ["answerable", "answer", "source_chunk_id"],
        "additionalProperties": False,
    }


def build_user_prompt(question: str, scheme_name: str | None, chunks: list[RetrievedChunk],
                      max_sentences: int = 3, reminder: str | None = None) -> str:
    parts = [f"Question: {question}"]
    if scheme_name:
        parts.append(f"Scheme the question is about: {scheme_name}")
    if max_sentences != 3:
        parts.append(f"Answer in at most {max_sentences} sentences.")
    if reminder:
        parts.append(f"Your previous answer was rejected because {reminder}. Follow the rules exactly.")
    parts.append("\nExcerpts:")
    for c in chunks:
        as_of = c.as_of or "not stated"
        parts.append(f'<excerpt id="{c.chunk_id}" source="{c.title}" type="{c.source_type}" as_of="{as_of}">')
        parts.append(c.text)
        parts.append("</excerpt>")
    return "\n".join(parts)


def _parse(result: dict) -> GeneratedAnswer:
    return GeneratedAnswer(
        answerable=bool(result["answerable"]),
        answer=str(result["answer"]).strip(),
        source_chunk_id=str(result["source_chunk_id"]),
    )


def generate(question: str, chunks: list[RetrievedChunk], scheme_name: str | None = None,
             max_sentences: int = 3, reminder: str | None = None) -> GeneratedAnswer:
    """Raises app.llm.LLMError on Groq failures."""
    result = complete_json(
        settings.generator_model,
        SYSTEM_PROMPT,
        build_user_prompt(question, scheme_name, chunks, max_sentences, reminder),
        "FactAnswer",
        answer_schema([c.chunk_id for c in chunks]),
        max_completion_tokens=MAX_COMPLETION_TOKENS,
    )
    return _parse(result)


async def agenerate(question: str, chunks: list[RetrievedChunk], scheme_name: str | None = None,
                    max_sentences: int = 3, reminder: str | None = None) -> GeneratedAnswer:
    result = await acomplete_json(
        settings.generator_model,
        SYSTEM_PROMPT,
        build_user_prompt(question, scheme_name, chunks, max_sentences, reminder),
        "FactAnswer",
        answer_schema([c.chunk_id for c in chunks]),
        max_completion_tokens=MAX_COMPLETION_TOKENS,
    )
    return _parse(result)
