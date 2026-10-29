"""Settings loaded from environment variables (and `.env`), plus the source domain allow-list.

Importing this module validates every non-secret setting. API keys are checked only where a
component needs them (`settings.require(...)`), so offline work such as registry validation
doesn't need keys. Run `python -m app.config` to check the full configuration, keys included.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Downloaded local embedding models (kept out of data/, git-ignored).
MODELS_CACHE_DIR = PROJECT_ROOT / ".cache" / "fastembed"

load_dotenv(PROJECT_ROOT / ".env")

# Hosts the fetcher may download from and the assistant may cite (architecture.md §3.1).
# Checked against the final URL after redirects (edge case ING-03).
ALLOWED_HOSTS = frozenset(
    {
        "www.hdfcfund.com",
        "files.hdfcfund.com",
        "www.amfiindia.com",
        "portal.amfiindia.com",  # daily NAV file (app/nav.py)
        "investor.sebi.gov.in",
        "www.sebi.gov.in",
    }
)


class ConfigError(Exception):
    """Raised when a setting is missing or invalid."""


def is_allowed_url(url: str) -> bool:
    """True if `url` is https and its host is on the allow-list."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS


EMBEDDING_PROVIDERS = {"local", "voyage"}
DEFAULT_EMBEDDING_MODELS = {"local": "BAAI/bge-small-en-v1.5", "voyage": "voyage-4"}


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None
    voyage_api_key: str | None
    classifier_model: str
    generator_model: str
    embedding_provider: str
    embedding_model: str
    data_dir: Path
    sources_path: Path
    retrieval_top_k: int
    min_similarity: float
    reranker_model: str | None  # optional cross-encoder; off by default (Docs/data_research.md §4.3)

    # data/ layout: raw downloads, cleaned (parsed) text, the vector index, and the manifest.
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.data_dir / "clean"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def threads_path(self) -> Path:
        """SQLite chat thread store (Phase 4)."""
        return self.data_dir / "threads.db"

    def require(self, *names: str) -> None:
        """Raise ConfigError naming every listed setting that is unset."""
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(
                f"Missing required setting(s): {', '.join(missing)}. "
                "Set them in .env (see .env.example) or the environment."
            )

    def masked(self) -> dict[str, str]:
        """Settings as strings, with API keys masked, for display."""
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name.endswith("_api_key"):
                value = f"set (…{value[-4:]})" if value else "NOT SET"
            out[f.name.upper()] = str(value)
        return out


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw) if raw else default
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings() -> Settings:
    provider = _env_str("EMBEDDING_PROVIDER", "local").lower()
    if provider not in EMBEDDING_PROVIDERS:
        raise ConfigError(f"EMBEDDING_PROVIDER must be one of {sorted(EMBEDDING_PROVIDERS)}, got {provider!r}")
    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
        voyage_api_key=os.getenv("VOYAGE_API_KEY", "").strip() or None,
        classifier_model=_env_str("CLASSIFIER_MODEL", "openai/gpt-oss-20b"),
        generator_model=_env_str("GENERATOR_MODEL", "openai/gpt-oss-120b"),
        embedding_provider=provider,
        embedding_model=_env_str("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODELS[provider]),
        data_dir=_env_path("DATA_DIR", PROJECT_ROOT / "data"),
        sources_path=_env_path("SOURCES_PATH", PROJECT_ROOT / "data" / "sources.yaml"),
        retrieval_top_k=_env_int("RETRIEVAL_TOP_K", 8, minimum=1, maximum=50),
        # Calibrated on bge-small: answerable eval questions score >= 0.74, general off-topic
        # questions <= 0.61. Re-tuned against the full eval set in Phase 6.
        min_similarity=_env_float("MIN_SIMILARITY", 0.65, minimum=0.0, maximum=1.0),
        reranker_model=os.getenv("RERANKER_MODEL", "").strip() or None,
    )


settings = load_settings()


if __name__ == "__main__":
    import sys

    for key, value in settings.masked().items():
        print(f"{key:18} {value}")
    try:
        required = ["groq_api_key"] + (["voyage_api_key"] if settings.embedding_provider == "voyage" else [])
        settings.require(*required)
    except ConfigError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print("\nConfiguration OK.")
