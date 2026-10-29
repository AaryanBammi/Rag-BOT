"""Templated responses (architecture.md §4.6).

Refusals and fallbacks are fixed text, never model-generated. Each carries exactly one link and
the `Last updated from sources` footer with the linked document's date, except "service
unavailable", which is an error state with neither.
"""

from __future__ import annotations

from app.nav import NavRow
from app.registry import Registry, Source
from app.validator import DATE_FORMAT, Reply, format_reply, source_date
from ingest.manifest import Manifest

ADVISORY_ASIDE = "I can't advise on whether to invest; I share facts only."

TEXTS = {
    "advisory": (
        "I can only share facts about mutual fund schemes, so I can't say whether to invest or "
        "which fund suits you. For help understanding how mutual funds work, see the investor "
        "education page linked below."
    ),
    "performance": (
        "I don't share returns or performance figures. You can see {scheme}'s official performance "
        "in the factsheet linked below."
    ),
    "nav_value": (
        "The NAV of {name} was ₹{nav} per unit on {date}. NAVs are published once per business day."
    ),
    "nav": (
        "I couldn't fetch the latest NAV of {scheme} just now. Please check it on its official HDFC "
        "Mutual Fund page linked below."
    ),
    "nav_other_plan": (
        "I only look up the NAV of {scheme}'s Direct Plan – Growth option. Please check the NAV of "
        "other plans and options on its official HDFC Mutual Fund page linked below."
    ),
    "multi_scheme": (
        "I answer questions about one scheme at a time and don't compare or rank schemes. Please ask "
        "about one of these: {schemes}."
    ),
    "clarification": "Which scheme do you mean? I cover these: {schemes}.",
    "greeting": (
        "Hello! I answer factual questions about six HDFC Mutual Fund schemes, such as expense ratios, "
        "exit loads, minimum SIP amounts, lock-in periods and benchmarks. I share facts only, not "
        "investment advice."
    ),
    "out_of_scope": (
        "I can only answer factual questions about these HDFC Mutual Fund schemes: {schemes}. For "
        "general mutual fund information, see the investor education page linked below."
    ),
    "not_found": (
        "I couldn't find this in the official documents I use. Please check the official page linked "
        "below for the latest information."
    ),
    "pii": (
        "Please don't share personal details such as PAN, Aadhaar, account or folio numbers, OTPs, "
        "email addresses or phone numbers; I haven't processed your message. To learn how to keep your "
        "investment account safe, see the guidance linked below."
    ),
    "service_unavailable": (
        "Sorry, I can't answer right now because a service I depend on is unavailable. Please try "
        "again in a minute."
    ),
}


class Templates:
    def __init__(self, registry: Registry, manifest: Manifest) -> None:
        self.registry = registry
        self.manifest = manifest

    # --- names and links ----------------------------------------------------------------

    def short_name(self, scheme_id: str) -> str:
        return self.registry.scheme(scheme_id).name.split(" – ")[0]

    def scheme_list(self) -> str:
        names = [self.short_name(s) for s in self.registry.scheme_ids]
        return ", ".join(names[:-1]) + " and " + names[-1]

    def _general(self, *types: str) -> Source:
        for t in types:
            found = self.registry.general_of_type(t)
            if found:
                return found[0]
        raise LookupError(f"registry has no general source of type {types}")

    def _education(self) -> Source:
        return self._general("amfi_education", "sebi_education")

    def _factsheet(self, scheme_id: str | None) -> Source:
        if scheme_id:
            own = self.registry.scheme(scheme_id).source("fund_facts")
            if own:
                return own
        return self._general("factsheet")

    def _date(self, source: Source, scheme_id: str | None = None) -> str:
        """The linked document's own date; for pages we don't download (scheme pages), the latest
        fetch of that scheme's documents, else of the general documents."""
        row = self.manifest.get(source.id)
        if row:
            return source_date(row.as_of, row.fetched_at)
        rows = [r for r in self.manifest.all() if r.scheme_id == scheme_id] or self.manifest.all()
        latest = max((r.fetched_at for r in rows), default=None)
        return source_date(None, latest) if latest else "unknown"

    def _reply(self, kind: str, text: str, source: Source, scheme_id: str | None = None, **extra) -> Reply:
        footer = self._date(source, scheme_id)
        return Reply(kind, format_reply(text, source.url, footer), source.url, footer, scheme_id=scheme_id, **extra)

    # --- templates ----------------------------------------------------------------------

    def advisory(self) -> Reply:
        return self._reply("advisory", TEXTS["advisory"], self._general("sebi_education", "amfi_education"))

    def performance(self, scheme_id: str | None) -> Reply:
        scheme = self.short_name(scheme_id) if scheme_id else "each scheme"
        return self._reply("performance", TEXTS["performance"].format(scheme=scheme), self._factsheet(scheme_id), scheme_id)

    def nav(self, scheme_id: str, other_plan: bool = False) -> Reply:
        """No figure: the NAV feed is unavailable, or the user asked about a plan we don't track."""
        page = self.registry.scheme(scheme_id).source("scheme_page")
        text = TEXTS["nav_other_plan" if other_plan else "nav"].format(scheme=self.short_name(scheme_id))
        return self._reply("nav", text, page, scheme_id)

    def nav_value(self, scheme_id: str, row: NavRow) -> Reply:
        """The latest NAV, copied from AMFI's file; the footer is the NAV's own date."""
        feed = self._general("nav_feed")
        when = row.date.strftime(DATE_FORMAT)
        text = TEXTS["nav_value"].format(name=self.registry.scheme(scheme_id).name, nav=row.nav, date=when)
        return Reply("nav_value", format_reply(text, feed.url, when), feed.url, when, scheme_id=scheme_id)

    def multi_scheme(self) -> Reply:
        return self._reply("multi_scheme", TEXTS["multi_scheme"].format(schemes=self.scheme_list()), self._education())

    def clarification(self) -> Reply:
        return self._reply("clarification", TEXTS["clarification"].format(schemes=self.scheme_list()), self._education())

    def greeting(self) -> Reply:
        return self._reply("greeting", TEXTS["greeting"], self._education())

    def out_of_scope(self) -> Reply:
        return self._reply("out_of_scope", TEXTS["out_of_scope"].format(schemes=self.scheme_list()), self._education())

    def not_found(self, scheme_id: str | None, top_similarity: float | None = None) -> Reply:
        source = self.registry.scheme(scheme_id).source("scheme_page") if scheme_id else self._education()
        return self._reply("not_found", TEXTS["not_found"], source, scheme_id, top_similarity=top_similarity)

    def pii(self) -> Reply:
        return self._reply("pii", TEXTS["pii"], self._general("investor_safety"))

    @staticmethod
    def service_unavailable() -> Reply:
        return Reply("service_unavailable", TEXTS["service_unavailable"], None, None)
