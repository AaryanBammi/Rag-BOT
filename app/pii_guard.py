"""PII guard (architecture.md §4.1): regex checks that run before any model or embedding call.

`find_pii` returns only the *kinds* of personal data found, never the matched text, and nothing
here logs the message. On a match the pipeline returns the PII template without classifying,
embedding or storing the message.
"""

from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern[str]] = {
    # ABCDE1234F; case-insensitive because people type it in lower case too.
    "pan": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I),
    # 12 digits, first digit 2-9, optionally in groups of 4 separated by spaces or hyphens.
    "aadhaar": re.compile(r"(?<![\d.,])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d.,]\d)"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # 10 digits starting 6-9, optional +91 / 91 / 0 prefix, optionally split 5+5.
    "mobile": re.compile(r"(?<![\d.,])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?![\d.,]\d)"),
    # 4-8 digit code next to an OTP-like word, either order.
    "otp": re.compile(
        r"\b(?:otp|one[- ]time[- ]password|verification\s+code|pin)\b\D{0,20}?\b\d{4,8}\b"
        r"|\b\d{4,8}\b\D{0,15}?\b(?:is\s+(?:my|the)\s+)?(?:otp|one[- ]time[- ]password)\b",
        re.I,
    ),
    # Folio numbers: the word "folio" followed by an id with digits.
    "folio": re.compile(r"\bfolio\b\D{0,20}?\d[\dA-Z/-]{4,}", re.I),
    # Bank/demat account numbers: a run of 9-18 digits (amounts use commas, so they don't match).
    "account": re.compile(r"(?<![\d.,])\d{9,18}(?![\d.,]\d)"),
}


def find_pii(message: str) -> list[str]:
    """Kinds of personal data in `message` (e.g. ["pan", "email"]); empty if none."""
    return [kind for kind, pattern in PATTERNS.items() if pattern.search(message)]


def contains_pii(message: str) -> bool:
    return bool(find_pii(message))
