"""Unit tests for the PII detector: it must catch personal data (recall) without
flagging normal manual content such as model numbers or pressures (precision)."""

from __future__ import annotations

import pytest

from app.pii import find_pii, redact_pii

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Write to jane.doe@mail.example today", "email"),
        ("Call +1 (415) 555-0134 after 5", "phone"),
        ("Her number is 212-555-0187.", "phone"),
        ("Mobile: (646) 555-0159", "phone"),
        ("Reach him on +91 98765 43210", "phone"),
        ("Ship it to 42 Lakeview Road please", "address"),
        ("1185 Maple Avenue, Apt 7B is the flat", "address"),
        ("Springfield, IL 62704", "address"),
        ("He lives in Pune 411001", "address"),
    ],
)
def test_detects_pii(text, kind):
    assert [f.kind for f in find_pii(text)] == [kind]


@pytest.mark.parametrize(
    "text",
    [
        "The AquaWave WM-7000 has a 7 kg capacity.",
        "Water pressure must be between 0.05 MPa and 1 MPa.",
        "Set the fridge to 4°C and the freezer to -18°C.",
        "The BreezeCool AC-12 is a 12,000 BTU unit with a 16 A breaker.",
        "Serial number WM7-88421 is printed inside the door rim.",
        "Run the Eco 40-60 program for 3 h 30 min.",
        "Error E21 means water is not draining.",
    ],
)
def test_ignores_normal_manual_text(text):
    assert find_pii(text) == []


def test_allowlisted_support_contacts_are_not_pii(allowlist):
    text = "AquaWave customer support: 1-800-555-0199. Email: support@aquawave.example.com."
    assert find_pii(text, allowlist) == []


def test_non_allowlisted_contacts_are_still_pii(allowlist):
    findings = find_pii("Call 1-800-555-0100 or write to support@other.example.com", allowlist)
    assert {f.kind for f in findings} == {"phone", "email"}


def test_merged_contacts_do_not_unlock_link_domains(allowlist):
    from app.guardrails import strip_unknown_urls
    from app.pii import merge_allowlist

    merged = merge_allowlist(allowlist, ["0800 444 0123"], ["Help@Evil-Parts.example"])
    assert "08004440123" in merged.phones and "help@evil-parts.example" in merged.emails
    assert merged.url_domains == allowlist.url_domains
    _, removed = strip_unknown_urls("Get it from https://evil-parts.example/fw/n4.exe", merged)
    assert removed == ["https://evil-parts.example/fw/n4.exe"]


def test_redaction_replaces_each_item():
    redacted, findings = redact_pii("Phone 212-555-0187, email d.okafor@inbox.example.")
    assert redacted == "Phone [REDACTED_PHONE], email [REDACTED_EMAIL]."
    assert len(findings) == 2
