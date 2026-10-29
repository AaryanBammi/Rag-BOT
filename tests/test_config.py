import pytest

from app import config
from app.config import ConfigError, is_allowed_url, load_settings


def test_defaults_load(monkeypatch):
    for name in (
        "CLASSIFIER_MODEL", "GENERATOR_MODEL", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL",
        "RETRIEVAL_TOP_K", "MIN_SIMILARITY",
    ):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert s.classifier_model == "openai/gpt-oss-20b"
    assert s.generator_model == "openai/gpt-oss-120b"
    assert s.embedding_provider == "local"
    assert s.embedding_model == "BAAI/bge-small-en-v1.5"
    assert s.retrieval_top_k == 8
    assert 0 <= s.min_similarity <= 1
    assert s.data_dir.is_absolute() and s.sources_path.is_absolute()


def test_voyage_provider_defaults_to_voyage_model(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "voyage")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert load_settings().embedding_model == "voyage-4"


@pytest.mark.parametrize(
    "name,value",
    [
        ("RETRIEVAL_TOP_K", "five"), ("RETRIEVAL_TOP_K", "0"), ("MIN_SIMILARITY", "1.5"),
        ("MIN_SIMILARITY", "x"), ("EMBEDDING_PROVIDER", "openai"),
    ],
)
def test_invalid_values_raise(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load_settings()


def test_require_names_missing_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("VOYAGE_API_KEY", "")
    s = load_settings()
    with pytest.raises(ConfigError, match="GROQ_API_KEY, VOYAGE_API_KEY"):
        s.require("groq_api_key", "voyage_api_key")


def test_masked_hides_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secretvalue1234")
    masked = load_settings().masked()
    assert "secretvalue" not in masked["GROQ_API_KEY"]
    assert masked["GROQ_API_KEY"].endswith("1234)")


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://files.hdfcfund.com/s3fs-public/x.pdf", True),
        ("https://www.amfiindia.com/investor", True),
        ("https://investor.sebi.gov.in/exit_load.html", True),
        ("http://files.hdfcfund.com/x.pdf", False),  # not https
        ("https://groww.in/mutual-funds/x", False),
        ("https://evil.hdfcfund.com.example.com/x", False),
        ("https://hdfcfund.com/x", False),  # bare domain not listed
    ],
)
def test_allowed_urls(url, ok):
    assert is_allowed_url(url) is ok


def test_import_exposes_settings():
    assert config.settings.sources_path.name == "sources.yaml"
