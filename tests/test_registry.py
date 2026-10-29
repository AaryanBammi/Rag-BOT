import copy

import pytest
import yaml

from app.config import settings
from app.registry import RegistryError, load_registry, parse_registry


@pytest.fixture(scope="module")
def raw():
    with open(settings.sources_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_real_registry_is_valid():
    reg = load_registry()
    assert len(reg.schemes) == 6
    assert set(reg.scheme_ids) == {
        "hdfc-mid-cap",
        "hdfc-small-cap",
        "hdfc-large-cap",
        "hdfc-silver-etf-fof",
        "hdfc-multi-cap",
        "hdfc-elss-tax-saver",
    }


def test_no_groww_in_sources():
    for src in load_registry().all_sources():
        assert "groww" not in src.url.lower()


def test_every_scheme_has_required_types_and_aliases():
    for s in load_registry().schemes:
        assert {"scheme_page", "sid", "kim"} <= {src.type for src in s.sources}
        assert s.aliases


def test_general_has_required_types():
    types = {g.type for g in load_registry().general}
    assert {"factsheet", "amfi_education", "sebi_education", "amc_service", "investor_safety"} <= types


def _expect_error(data, match):
    with pytest.raises(RegistryError, match=match):
        parse_registry(data)


def test_groww_url_under_sources_rejected(raw):
    data = copy.deepcopy(raw)
    data["schemes"][0]["sources"][1]["url"] = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
    _expect_error(data, "Groww URL is not an allowed source")


def test_placeholder_rejected(raw):
    data = copy.deepcopy(raw)
    data["general"][0]["url"] = "<official HDFC MF URL>"
    _expect_error(data, "placeholder url")


def test_off_allowlist_host_rejected(raw):
    data = copy.deepcopy(raw)
    data["general"][0]["url"] = "https://www.valueresearchonline.com/funds/x"
    _expect_error(data, "allowed host")


def test_duplicate_url_rejected(raw):
    data = copy.deepcopy(raw)
    data["general"][1]["url"] = data["general"][2]["url"]
    _expect_error(data, "duplicate url")


def test_missing_required_type_rejected(raw):
    data = copy.deepcopy(raw)
    data["schemes"][0]["sources"] = [s for s in data["schemes"][0]["sources"] if s["type"] != "kim"]
    _expect_error(data, "missing required source type 'kim'")


def test_bad_fetch_mode_rejected(raw):
    data = copy.deepcopy(raw)
    data["general"][0]["fetch"] = "sometimes"
    _expect_error(data, "fetch must be one of")


def test_all_errors_reported_together(raw):
    data = copy.deepcopy(raw)
    data["general"][0]["url"] = "<placeholder>"
    data["general"][1]["fetch"] = "bogus"
    with pytest.raises(RegistryError) as e:
        parse_registry(data)
    assert "placeholder url" in str(e.value) and "fetch must be one of" in str(e.value)
