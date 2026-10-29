"""Loads and validates the source registry (`sources.yaml`, architecture.md §3.1).

Validation fails fast with every problem listed, so a bad registry never reaches ingestion
(edge cases ING-04, ING-05). Run `python -m app.registry` to validate and print a summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from app.config import ALLOWED_HOSTS, is_allowed_url, settings

SCHEME_SOURCE_TYPES = {"scheme_page", "sid", "kim", "fund_facts"}
GENERAL_SOURCE_TYPES = {
    "factsheet",
    "sai",
    "amfi_education",
    "sebi_education",
    "amc_service",
    "investor_safety",
    "nav_feed",
}
FETCH_MODES = {"auto", "manual", "none"}

# Every scheme needs these; monthly facts come from `fund_facts` or the multi-scheme factsheet.
REQUIRED_SCHEME_TYPES = {"scheme_page", "sid", "kim"}
REQUIRED_GENERAL_TYPES = GENERAL_SOURCE_TYPES - {"sai", "nav_feed"}

GROWW_HOSTS = {"groww.in", "www.groww.in"}


class RegistryError(Exception):
    """Raised when sources.yaml is invalid. The message lists every problem found."""


@dataclass(frozen=True)
class Source:
    id: str
    type: str
    url: str
    fetch: str
    title: str
    scheme_id: str | None = None
    multi_scheme: bool = False


@dataclass(frozen=True)
class Scheme:
    id: str
    name: str
    aliases: tuple[str, ...]
    groww_reference: str
    sources: tuple[Source, ...]
    amfi_code: str | None = None  # AMFI scheme code of the Direct Growth plan, for the NAV lookup

    def source(self, type_: str) -> Source | None:
        return next((s for s in self.sources if s.type == type_), None)


@dataclass(frozen=True)
class Registry:
    schemes: tuple[Scheme, ...]
    general: tuple[Source, ...]
    _by_id: dict[str, Scheme] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_id.update({s.id: s for s in self.schemes})

    def scheme(self, scheme_id: str) -> Scheme:
        return self._by_id[scheme_id]

    @property
    def scheme_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.schemes)

    def all_sources(self) -> list[Source]:
        return [src for s in self.schemes for src in s.sources] + list(self.general)

    def general_of_type(self, type_: str) -> list[Source]:
        return [s for s in self.general if s.type == type_]


def _check_url(url: object, where: str, errors: list[str]) -> str:
    if not isinstance(url, str) or not url:
        errors.append(f"{where}: missing url")
        return ""
    if "<" in url or ">" in url:
        errors.append(f"{where}: placeholder url {url!r}")
    elif urlsplit(url).hostname in GROWW_HOSTS:
        errors.append(f"{where}: Groww URL is not an allowed source (use groww_reference): {url}")
    elif not is_allowed_url(url):
        errors.append(
            f"{where}: url must be https on an allowed host "
            f"({', '.join(sorted(ALLOWED_HOSTS))}): {url}"
        )
    return url


def _check_fetch(value: object, where: str, errors: list[str]) -> str:
    if value not in FETCH_MODES:
        errors.append(f"{where}: fetch must be one of {sorted(FETCH_MODES)}, got {value!r}")
        return "none"
    return value


def parse_registry(data: dict) -> Registry:
    """Build a Registry from parsed YAML, raising RegistryError listing all problems."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise RegistryError("sources.yaml must be a mapping with 'schemes' and 'general'")

    schemes: list[Scheme] = []
    seen_ids: set[str] = set()
    seen_urls: dict[str, str] = {}

    def note_url(url: str, where: str) -> None:
        if url and url in seen_urls:
            errors.append(f"{where}: duplicate url (also in {seen_urls[url]})")
        elif url:
            seen_urls[url] = where

    for i, raw in enumerate(data.get("schemes") or []):
        sid = raw.get("id")
        where = f"schemes[{i}] ({sid})"
        if not sid or not raw.get("name"):
            errors.append(f"{where}: id and name are required")
            continue
        if sid in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(sid)

        aliases = raw.get("aliases") or []
        if not aliases or not all(isinstance(a, str) and a.strip() for a in aliases):
            errors.append(f"{where}: aliases must be a non-empty list of names")

        groww = raw.get("groww_reference", "")
        if urlsplit(groww).hostname not in GROWW_HOSTS:
            errors.append(f"{where}: groww_reference must be a groww.in URL")

        amfi_code = raw.get("amfi_code")
        if amfi_code is not None:
            amfi_code = str(amfi_code)
            if not amfi_code.isdigit():
                errors.append(f"{where}: amfi_code must be digits, got {amfi_code!r}")

        sources: list[Source] = []
        for j, rs in enumerate(raw.get("sources") or []):
            swhere = f"{where}.sources[{j}]"
            type_ = rs.get("type")
            if type_ not in SCHEME_SOURCE_TYPES:
                errors.append(f"{swhere}: type must be one of {sorted(SCHEME_SOURCE_TYPES)}")
                continue
            url = _check_url(rs.get("url"), swhere, errors)
            note_url(url, swhere)
            sources.append(
                Source(
                    id=f"{sid}-{type_}",
                    type=type_,
                    url=url,
                    fetch=_check_fetch(rs.get("fetch"), swhere, errors),
                    title=f"{raw['name']} - {type_}",
                    scheme_id=sid,
                )
            )

        types = [s.type for s in sources]
        for missing in sorted(REQUIRED_SCHEME_TYPES - set(types)):
            errors.append(f"{where}: missing required source type {missing!r}")
        for dup in sorted({t for t in types if types.count(t) > 1}):
            errors.append(f"{where}: more than one {dup!r} source")

        schemes.append(
            Scheme(
                id=sid,
                name=raw["name"],
                aliases=tuple(aliases),
                groww_reference=groww,
                sources=tuple(sources),
                amfi_code=amfi_code,
            )
        )

    if not schemes:
        errors.append("schemes: at least one scheme is required")

    general: list[Source] = []
    for i, rg in enumerate(data.get("general") or []):
        gid = rg.get("id")
        where = f"general[{i}] ({gid})"
        if not gid or not rg.get("title"):
            errors.append(f"{where}: id and title are required")
            continue
        if gid in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(gid)
        type_ = rg.get("type")
        if type_ not in GENERAL_SOURCE_TYPES:
            errors.append(f"{where}: type must be one of {sorted(GENERAL_SOURCE_TYPES)}")
            continue
        url = _check_url(rg.get("url"), where, errors)
        note_url(url, where)
        general.append(
            Source(
                id=gid,
                type=type_,
                url=url,
                fetch=_check_fetch(rg.get("fetch"), where, errors),
                title=rg["title"],
                multi_scheme=bool(rg.get("multi_scheme", False)),
            )
        )

    for missing in sorted(REQUIRED_GENERAL_TYPES - {g.type for g in general}):
        errors.append(f"general: missing required source type {missing!r}")
    if any(s.amfi_code for s in schemes) and not any(g.type == "nav_feed" for g in general):
        errors.append("general: schemes have amfi_code but there is no nav_feed source")

    # Every scheme needs a monthly facts document: its own Fund Facts or a multi-scheme factsheet.
    has_multi_factsheet = any(g.type == "factsheet" and g.multi_scheme for g in general)
    for s in schemes:
        if not s.source("fund_facts") and not has_multi_factsheet:
            errors.append(f"scheme {s.id}: needs fund_facts or a multi_scheme factsheet")

    if errors:
        raise RegistryError("Invalid sources.yaml:\n  - " + "\n  - ".join(errors))
    return Registry(schemes=tuple(schemes), general=tuple(general))


def load_registry(path: Path | None = None) -> Registry:
    path = path or settings.sources_path
    with open(path, encoding="utf-8") as f:
        return parse_registry(yaml.safe_load(f))


if __name__ == "__main__":
    reg = load_registry()
    print(f"{len(reg.schemes)} schemes, {len(reg.general)} general sources\n")
    for s in reg.schemes:
        kinds = ", ".join(f"{src.type}[{src.fetch}]" for src in s.sources)
        print(f"  {s.id:22} {kinds}")
    print()
    for g in reg.general:
        flag = " (multi-scheme)" if g.multi_scheme else ""
        print(f"  {g.id:28} {g.type:16} [{g.fetch}]{flag}")
    print("\nRegistry OK.")
