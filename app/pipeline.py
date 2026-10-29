"""Query pipeline (architecture.md §4): PII guard -> classifier -> template | answer path.

    reply = Pipeline().handle("What is the exit load of HDFC Mid Cap Fund?")

A message costs at most 3 model calls: classifier, generator, and one regeneration. PII messages
cost none: they are answered with the PII template before anything is classified or embedded.
NAV questions cost only the classifier call: the figure comes from AMFI's daily file (app/nav.py).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Awaitable, Callable, Sequence

from app.ask import Answerer
from app.classifier import Classification, aclassify, classify
from app.llm import LLMError
from app.nav import NavFeed, NavRow, NavUnavailable
from app.pii_guard import contains_pii
from app.validator import Reply

Classifier = Callable[..., Classification]
AsyncClassifier = Callable[..., Awaitable[Classification]]

# The registry tracks Direct Plan - Growth only; other plans/options get the "check the page" reply.
_OTHER_PLAN = re.compile(r"\bregular\b|\bidcw\b|\bdividend\b|\bpayout\b|\breinvest", re.I)
# NAV on a past date or over time is performance (no figures), whatever the classifier says.
_PAST_NAV = re.compile(
    r"\b(ago|last|previous|yesterday|history|historical|since|launch|inception|change[ds]?|grow(th|n)?|"
    r"increase[ds]?|decrease[ds]?|rose|rise|fell|fall|compare[ds]?|versus|vs|trend|high|low|(19|20)\d\d)\b",
    re.I)


class Pipeline:
    def __init__(self, answerer: Answerer | None = None, classifier: Classifier = classify,
                 aclassifier: AsyncClassifier = aclassify, nav: NavFeed | None = None) -> None:
        self.answerer = answerer if answerer is not None else Answerer()
        self.templates = self.answerer.templates
        self.registry = self.answerer.registry
        self.nav = nav if nav is not None else NavFeed.from_registry(self.registry)
        self._classify = classifier
        self._aclassify = aclassifier

    def handle(self, message: str, history: Sequence[tuple[str, str]] = ()) -> Reply:
        if contains_pii(message):
            return self.templates.pii()
        try:
            route = self._past_nav_is_performance(message, self._classify(message, self.registry, history))
            code = self._nav_code(message, route)
            reply = self._nav_reply(route, self._lookup(code)) if code else self._route(message, route)
        except LLMError:
            return self.templates.service_unavailable()
        return replace(reply, intent=route.intent, scheme_id=route.scheme_id)

    async def ahandle(self, message: str, history: Sequence[tuple[str, str]] = ()) -> Reply:
        """Async `handle` for the API, so concurrent threads don't block each other."""
        if contains_pii(message):
            return self.templates.pii()
        try:
            route = self._past_nav_is_performance(message, await self._aclassify(message, self.registry, history))
            code = self._nav_code(message, route)
            reply = self._nav_reply(route, await asyncio.to_thread(self._lookup, code)) if code else self._template(route)
            if reply is None:
                reply = await self.answerer.aask(route.standalone_query or message, route.scheme_id,
                                                 route.advisory_aside)
        except LLMError:
            return self.templates.service_unavailable()
        return replace(reply, intent=route.intent, scheme_id=route.scheme_id)

    def _route(self, message: str, route: Classification) -> Reply:
        return self._template(route) or self.answerer.ask(
            route.standalone_query or message, route.scheme_id, route.advisory_aside)

    @staticmethod
    def _past_nav_is_performance(message: str, route: Classification) -> Classification:
        return replace(route, intent="performance") if route.intent == "nav" and _PAST_NAV.search(message) else route

    def _nav_code(self, message: str, route: Classification) -> str | None:
        """The AMFI code to look up, when this is a NAV question about a Direct Growth plan we track."""
        if route.intent != "nav" or not route.scheme_id or _OTHER_PLAN.search(message):
            return None
        return self.registry.scheme(route.scheme_id).amfi_code

    def _lookup(self, code: str) -> NavRow | None:
        try:
            row = self.nav.get(code)
        except NavUnavailable:
            return None
        return row if row is not None and row.is_direct_growth else None  # guard against a reused code

    def _nav_reply(self, route: Classification, row: NavRow | None) -> Reply:
        return self.templates.nav_value(route.scheme_id, row) if row else self.templates.nav(route.scheme_id)

    def _template(self, route: Classification) -> Reply | None:
        """The templated reply for this route, or None when it needs the answer path."""
        t = self.templates
        if route.intent == "advisory":
            return t.advisory()
        if route.intent == "greeting":
            return t.greeting()
        if route.intent == "out_of_scope":
            return t.out_of_scope()
        if route.intent == "multi_scheme":
            return t.multi_scheme()
        if route.intent == "performance":
            return t.performance(route.scheme_id)
        if route.intent == "nav":  # a lookup was not possible (see _nav_code): no scheme, or another plan
            return t.nav(route.scheme_id, other_plan=True) if route.scheme_id else t.clarification()
        if route.needs_scheme:
            return t.clarification()
        return None
