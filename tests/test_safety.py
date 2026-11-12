"""Safety tests: prompt injection, jailbreaks and PII leakage.

Every prompt in adversarial.jsonl and pii_probes.jsonl becomes one test. A
test fails if the attack works (a fail marker appears in the answer), the
system prompt leaks (the canary appears), any personal data appears anywhere in
the response (answer or cited sources), or the bot refuses a harmless question.
"""

from __future__ import annotations

import pytest

from evaluation.checks import check_safety, known_issue_for, load_jsonl
from tests.conftest import mark_known_issue

ADVERSARIAL = load_jsonl("adversarial.jsonl")
PII_PROBES = load_jsonl("pii_probes.jsonl")


def _run(case, suite, api, health, allowlist, sensitive, record, request):
    known_issue = known_issue_for(case, health["llm_provider"])
    mark_known_issue(request, known_issue)

    result = check_safety(case, api.chat(case["prompt"]), allowlist, sensitive, suite)
    result.known_issue = known_issue
    record(result)

    assert result.passed, f"{case['prompt']!r}\nanswer: {result.answer!r}\n" + "\n".join(result.reasons)


@pytest.mark.safety
@pytest.mark.parametrize("case", ADVERSARIAL, ids=[c["id"] for c in ADVERSARIAL])
def test_adversarial_prompt(case, api, health, allowlist, sensitive, record, request):
    _run(case, "adversarial", api, health, allowlist, sensitive, record, request)


@pytest.mark.safety
@pytest.mark.parametrize("case", PII_PROBES, ids=[c["id"] for c in PII_PROBES])
def test_pii_probe(case, api, health, allowlist, sensitive, record, request):
    _run(case, "pii", api, health, allowlist, sensitive, record, request)
