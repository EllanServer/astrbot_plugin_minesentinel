"""Term normalization and matching helpers for dialogue analysis."""

from __future__ import annotations

import re
from typing import Iterable


NEGATION_PREFIXES = (
    "不",
    "没",
    "没有",
    "不是",
    "并不",
    "不太",
)

REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")


def normalize_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def message_fingerprint(text: str) -> str:
    normalized = normalize_text(text)
    compact = "".join(ch for ch in normalized if ch.isalnum())
    return REPEATED_CHAR_RE.sub(r"\1\1", compact)


def matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    matched = []
    for term in terms:
        normalized = term.lower()
        if normalized in text and not term_is_negated(text, normalized):
            matched.append(term)
    return matched


def term_is_negated(text: str, term: str) -> bool:
    start = 0
    saw_negated = False
    while True:
        index = text.find(term, start)
        if index < 0:
            return saw_negated
        prefix_window = text[max(0, index - 4) : index]
        if any(prefix_window.endswith(prefix) for prefix in NEGATION_PREFIXES):
            saw_negated = True
            start = index + len(term)
            continue
        return False


class RuleTermMatcher:
    """Match many keyword rules against a text in a single pass.

    Each rule's keywords are compiled into one alternation regex so that a
    single ``re.finditer`` scan reports every hit and its position, instead of
    looping over rules × keywords with repeated ``str.find`` calls. Negation
    is still applied per hit via :func:`term_is_negated`.
    """

    def __init__(self, rules: Iterable[tuple[object, tuple[str, ...], tuple[str, ...]]]):
        # rules: iterable of (rule_obj, keywords, urgent_terms)
        self._rules: list[tuple[object, tuple[str, ...], tuple[str, ...]]] = []
        # lowered term -> owning rule objects (a term may belong to several rules).
        self._keyword_owners: dict[str, list[object]] = {}
        self._urgent_owners: dict[str, list[object]] = {}
        # lowered term -> original (display) term, so hits map back to the form
        # the rule author wrote (matched_terms preserves original casing).
        self._keyword_display: dict[str, str] = {}
        self._urgent_display: dict[str, str] = {}
        keyword_terms: set[str] = set()
        urgent_terms: set[str] = set()
        for rule, keywords, urgent in rules:
            self._rules.append((rule, keywords, urgent))
            for term in keywords:
                lowered = term.lower()
                keyword_terms.add(lowered)
                self._keyword_owners.setdefault(lowered, []).append(rule)
                self._keyword_display.setdefault(lowered, term)
            for term in urgent:
                lowered = term.lower()
                urgent_terms.add(lowered)
                self._urgent_owners.setdefault(lowered, []).append(rule)
                self._urgent_display.setdefault(lowered, term)
        self._keyword_re = _compile_term_pattern(keyword_terms)
        self._urgent_re = _compile_term_pattern(urgent_terms)

    def scan(
        self, text: str
    ) -> dict[object, tuple[list[str], list[str]]]:
        """Return {rule: (matched_keywords, matched_urgent_terms)} for the text."""
        result: dict[object, tuple[list[str], list[str]]] = {}
        if not text:
            return result

        keyword_hits = _collect_non_negated_hits(text, self._keyword_re)
        for lowered in keyword_hits:
            display = self._keyword_display.get(lowered, lowered)
            for rule in self._keyword_owners.get(lowered, ()):
                kw, ug = result.setdefault(rule, ([], []))
                kw.append(display)

        urgent_hits = _collect_non_negated_hits(text, self._urgent_re)
        for lowered in urgent_hits:
            display = self._urgent_display.get(lowered, lowered)
            for rule in self._urgent_owners.get(lowered, ()):
                kw, ug = result.setdefault(rule, ([], []))
                ug.append(display)
        return result

    def matched_keywords(self, text: str) -> dict[object, list[str]]:
        """Return {rule: matched_keywords} ignoring urgent terms."""
        hits = _collect_non_negated_hits(text, self._keyword_re)
        out: dict[object, list[str]] = {}
        for lowered in hits:
            display = self._keyword_display.get(lowered, lowered)
            for rule in self._keyword_owners.get(lowered, ()):
                out.setdefault(rule, []).append(display)
        return out


def _compile_term_pattern(terms: set[str]) -> re.Pattern[str]:
    if not terms:
        return re.compile(r"(?!)")
    # Sort by length desc so longer terms match first at a given position;
    # escape each term for regex safety.
    alternation = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return re.compile(alternation)


def _collect_non_negated_hits(
    text: str, pattern: re.Pattern[str]
) -> dict[str, list[object]]:
    """Collect non-negated term hits keyed by the matched (lowered) term string.

    A term may appear multiple times; we keep one entry but remember it matched.
    Negated occurrences are ignored, matching :func:`matched_terms` semantics
    (a non-negated occurrence anywhere makes the term count).
    """
    hits: dict[str, list[object]] = {}
    for match in pattern.finditer(text):
        term = match.group(0)
        if term_is_negated(text, term):
            continue
        hits.setdefault(term, [])
    return hits
