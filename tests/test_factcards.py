from ingest.chunk import scheme_name_index
from ingest.factcards import build_fact_cards
from ingest.parse import Page, ParsedDoc

NAMES = {"hdfc-mid-cap": "HDFC Mid Cap Fund", "hdfc-silver-etf-fof": "HDFC Silver ETF Fund of Fund"}
INDEX = scheme_name_index({k: [v] for k, v in NAMES.items()})

MID_PAGE = """13 | September 2026
HDFC Mid Cap Fund
EXPENSE RATIO
(As On September 30, 2026)
Base expense Ratio Including statutory levies
Regular: 1.26% Direct: 0.71%
#BENCHMARK INDEX
NIFTY MIDCAP 150 (TRI)
##ADDL. BENCHMARK INDEX
Nifty 50 Index (TRI)
EXIT LOAD$$
• In respect of each purchase / switch-in of
Units, an Exit Load of 1.00% is payable within 1 year.
PORTFOLIO
• ICICI Bank Ltd. Banks 9.06"""

SILVER_PAGE = """HDFC Silver ETF Fund of Fund
EXPENSE RATIO
(As On September 30, 2026)
Regular: 0.55% Direct: 0.21%
#BENCHMARK INDEX
Domestic Prices of physical Silver
(derived as per regulatory norms)
EXIT LOAD$$
• an Exit Load of is payable if Units are
redeemed/ switched-out within 15 days"""


def _cards(*pages):
    doc = ParsedDoc(kind="pdf", pages=tuple(Page(i, t) for i, t in enumerate(pages, start=13)))
    return {c.scheme_id: c for c in build_fact_cards(doc, "Factsheet", INDEX, NAMES, start_index=7)}


def test_fields_extracted_verbatim():
    card = _cards(MID_PAGE)["hdfc-mid-cap"]
    assert card.kind == "fact_card" and card.index == 7 and card.page == 13
    assert "HDFC Mid Cap Fund - Expense ratio: (as on September 30, 2026) Regular: 1.26% Direct: 0.71%" in card.text
    assert "HDFC Mid Cap Fund - Benchmark: NIFTY MIDCAP 150 (TRI)" in card.text
    assert "Nifty 50" not in card.text  # additional benchmark is a separate section
    assert "Exit Load of 1.00% is payable within 1 year." in card.text
    assert "ICICI" not in card.text  # stops at the next section heading


def test_value_missing_from_text_is_left_out():
    card = _cards(SILVER_PAGE)["hdfc-silver-etf-fof"]
    assert "Exit load" not in card.text  # "an Exit Load of is payable": percentage is a graphic
    assert "Domestic Prices of physical Silver (derived as per regulatory norms)" in card.text


def test_pages_for_unknown_schemes_produce_no_cards():
    assert _cards("HDFC Value Fund\nEXPENSE RATIO\n(As On September 30, 2026)\nRegular: 1.0% Direct: 0.5%") == {}
