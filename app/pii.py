"""Detect and redact personal data (PII) in text.

We look for three kinds of PII with regular expressions:

* email addresses
* phone numbers (US, international and Indian formats)
* street addresses ("42 Lakeview Road", "Apt 7B", "IL 62704")

Official business contacts (the support hotlines printed in the manuals) are on
an allowlist, so an answer like "call 1-800-555-0199" is not treated as a leak.

Regexes are fast, transparent and easy to test, but they miss things (names, for
example). In production you would add an NER-based detector such as Microsoft
Presidio. The interface below (`find_pii` / `redact_pii`) would stay the same.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# A run of digits with optional separators: +1 (415) 555-0134, 212-555-0187, +91 98765 43210.
# We accept the match only if it contains 10 to 13 digits (see _is_phone).
PHONE_CANDIDATE_RE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{7,}\d(?![\w-])")

STREET_SUFFIX = (
    r"(?:Road|Rd|Street|St|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl|Terrace|Highway|Hwy)"
)
STREET_ADDRESS_RE = re.compile(
    rf"\b\d{{1,5}}\s+(?:[A-Z][A-Za-z]*\.?\s+){{1,3}}{STREET_SUFFIX}\b\.?(?:,?\s+(?:Apt|Apartment|Suite|Unit)\s*\w+)?"
)
US_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
INDIA_PIN_RE = re.compile(r"\b(?:[A-Z][a-z]+)\s+\d{6}\b")  # "Pune 411001"


@dataclass(frozen=True)
class PIIFinding:
    kind: str  # "email" | "phone" | "address"
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class Allowlist:
    emails: frozenset[str] = field(default_factory=frozenset)
    phones: frozenset[str] = field(default_factory=frozenset)  # digits only
    url_domains: frozenset[str] = field(default_factory=frozenset)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def merge_allowlist(base: Allowlist, phones=(), emails=()) -> Allowlist:
    """Return `base` plus extra allowed contacts (e.g. an uploaded manual's own support line).

    Link domains are not widened: an email address in a file says nothing about which
    websites are safe to link to, and one line of an uploaded file would otherwise unlock
    links in every answer."""
    return Allowlist(
        emails=base.emails | {e.lower() for e in emails},
        phones=base.phones | {_digits(p) for p in phones},
        url_domains=base.url_domains,
    )


def contact_key(finding: PIIFinding) -> str:
    """The form a phone or email takes in an Allowlist: digits only, or lower case."""
    return finding.value.lower() if finding.kind == "email" else _digits(finding.value)


def _is_phone(candidate: str) -> bool:
    return 10 <= len(_digits(candidate)) <= 13


@lru_cache(maxsize=4)
def load_allowlist(path: str) -> Allowlist:
    file = Path(path)
    if not file.exists():
        return Allowlist()
    data = json.loads(file.read_text(encoding="utf-8"))
    return Allowlist(
        emails=frozenset(e.lower() for e in data.get("emails", [])),
        phones=frozenset(_digits(p) for p in data.get("phones", [])),
        url_domains=frozenset(d.lower() for d in data.get("url_domains", [])),
    )


def find_pii(text: str, allowlist: Allowlist | None = None) -> list[PIIFinding]:
    """Return every non-allowlisted PII match in `text`, sorted by position."""
    allowlist = allowlist or Allowlist()
    findings: list[PIIFinding] = []

    for m in EMAIL_RE.finditer(text):
        if m.group().lower() not in allowlist.emails:
            findings.append(PIIFinding("email", m.group(), m.start(), m.end()))

    for m in PHONE_CANDIDATE_RE.finditer(text):
        value = m.group().strip(" .-")
        if _is_phone(value) and _digits(value) not in allowlist.phones:
            findings.append(PIIFinding("phone", value, m.start(), m.start() + len(value)))

    for pattern in (STREET_ADDRESS_RE, US_STATE_ZIP_RE, INDIA_PIN_RE):
        for m in pattern.finditer(text):
            findings.append(PIIFinding("address", m.group(), m.start(), m.end()))

    # Drop matches that sit inside a bigger match (e.g. a ZIP inside an address).
    findings.sort(key=lambda f: (f.start, -(f.end - f.start)))
    merged: list[PIIFinding] = []
    for f in findings:
        if merged and f.start < merged[-1].end:
            continue
        merged.append(f)
    return merged


def redact_pii(text: str, allowlist: Allowlist | None = None) -> tuple[str, list[PIIFinding]]:
    """Replace each PII match with a placeholder like [REDACTED_PHONE]."""
    findings = find_pii(text, allowlist)
    if not findings:
        return text, []
    pieces, cursor = [], 0
    for f in findings:
        pieces.append(text[cursor : f.start])
        pieces.append(f"[REDACTED_{f.kind.upper()}]")
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces), findings
