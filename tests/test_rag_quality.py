"""Golden Q&A tests: one test per question in evaluation/datasets/golden_qa.jsonl.

For an answerable question the answer must:
  * not be flagged as a hallucination (groundedness check),
  * contain the required facts (keyword recall = 1.0),
  * cite the expected manual (retrieval hit).
For an unanswerable question the bot must say it could not find the answer.
"""

from __future__ import annotations

import pytest

from evaluation.checks import check_golden, known_issue_for, load_jsonl
from tests.conftest import mark_known_issue

GOLDEN = load_jsonl("golden_qa.jsonl")


@pytest.mark.quality
@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_golden_question(case, api, health, evaluator, record, request):
    known_issue = known_issue_for(case, health["llm_provider"])
    mark_known_issue(request, known_issue)

    result = check_golden(case, api.chat(case["question"]), evaluator)
    result.known_issue = known_issue
    record(result)

    assert result.passed, f"{case['question']!r}\nanswer: {result.answer!r}\n" + "\n".join(result.reasons)
