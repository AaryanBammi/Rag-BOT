from datetime import date

import pytest

from ingest.dates import extract_as_of, parse_date

TODAY = date(2026, 10, 27)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("September 30, 2026", date(2026, 9, 30)),
        ("Sept 30th 2026", date(2026, 9, 30)),
        ("30 September 2026", date(2026, 9, 30)),
        ("30th September, 2026", date(2026, 9, 30)),
        ("30-Sep-26", date(2026, 9, 30)),
        ("30-Sep-2026", date(2026, 9, 30)),
        ("30/09/2026", date(2026, 9, 30)),
        ("30.09.2026", date(2026, 9, 30)),
        ("September 2026", date(2026, 9, 30)),  # month only -> last day (PRS-08)
        ("February 2028", date(2028, 2, 29)),
        ("31/02/2026", None),  # impossible date
        ("no date here", None),
    ],
)
def test_parse_date_formats(text, expected):
    assert parse_date(text) == expected


def test_factsheet_uses_most_frequent_as_on_date():
    text = (
        "EXPENSE RATIO (As On September 30, 2026) ... Top holdings as on September 30, 2026 ... "
        "NAV as on October 26, 2026 ... AUM as on September 30, 2026"
    )
    assert extract_as_of(text, "factsheet", today=TODAY) == date(2026, 9, 30)


def test_sid_uses_dated():
    text = "SCHEME INFORMATION DOCUMENT HDFC Mid Cap Fund ... This SID is dated November 21, 2025. Effective from April 1, 2026"
    assert extract_as_of(text, "sid", today=TODAY) == date(2025, 11, 21)


def test_kim_falls_back_to_effective():
    assert extract_as_of("KIM. Effective from: 1st April 2026.", "kim", today=TODAY) == date(2026, 4, 1)


def test_future_date_rejected():
    # PRS-09: a misparsed future date must give None, not a wrong date.
    assert extract_as_of("data as on 30/09/2031", "fund_facts", today=TODAY) is None


def test_before_1990_rejected():
    assert extract_as_of("as on 01/01/1985", "factsheet", today=TODAY) is None


def test_no_date_gives_none():
    assert extract_as_of("Understanding Mutual Funds. A mutual fund pools money.", "sebi_education", today=TODAY) is None


def test_other_types_use_first_keyword_date():
    assert extract_as_of("Last updated on 12 March 2026", "amfi_education", today=TODAY) == date(2026, 3, 12)


def test_unanchored_date_is_ignored():
    # A bare date not introduced by "as on"/"dated"/etc. isn't the document's date.
    assert extract_as_of("Inception Date June 25, 2007", "fund_facts", today=TODAY) is None
