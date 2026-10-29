"""PII guard: every PII type is caught in a sentence; fund facts with numbers are not (eval.md Phase 3)."""

import pytest

from app.pii_guard import find_pii

POSITIVES = [
    ("pan", "My PAN is ABCDE1234F, what is the exit load?"),
    ("pan", "pan abcde1234f"),
    ("aadhaar", "Aadhaar 2345 6789 0123 linked to my folio"),
    ("aadhaar", "my aadhaar is 234567890123"),
    ("email", "Send the statement to ravi.kumar@example.com please"),
    ("mobile", "Call me on 9876543210"),
    ("mobile", "my number is +91 98765 43210"),
    ("otp", "The OTP is 482913, why isn't it working?"),
    ("otp", "482913 is my OTP"),
    ("folio", "My folio number is 1234567/89, show my holdings"),
    ("account", "Bank account 001234567890 not linked"),
]

NEGATIVES = [
    "What is the minimum SIP amount for HDFC Mid Cap Fund? Is it Rs. 100?",
    "Is the expense ratio 0.71% for the Direct Plan?",
    "AUM is ₹101,856.06 Cr as of 30 Sep 2026",
    "Exit load is 1% if redeemed within 365 days",
    "Lock-in period of 3 years for ELSS",
    "Benchmark is NIFTY 500 Multicap 50:25:25 Index TRI",
    "Deduction under section 80C up to Rs 1,50,000",
    "Fund managed since June 25, 2007 by Chirag Setalvad",
    "What is a folio?",
    "How do I get my capital gains statement for FY 2025-26?",
    "HDFC MF toll free number 1800 3010 6767",
    "What is the AMFI scheme code 118989 for HDFC Mid Cap?",
]


@pytest.mark.parametrize("kind,message", POSITIVES)
def test_each_pii_type_caught(kind, message):
    assert kind in find_pii(message)


@pytest.mark.parametrize("message", NEGATIVES)
def test_fund_facts_are_not_pii(message):
    assert find_pii(message) == []


def test_verification_code_caught():
    assert "otp" in find_pii("The verification code 5521 failed")
