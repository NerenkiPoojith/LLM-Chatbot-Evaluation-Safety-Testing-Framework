"""Test the hallucination detector itself (meta-evaluation).

An evaluator is only useful if it can tell good answers from bad ones. The
calibration set contains hand-labelled answers: some grounded, some with
planted hallucinations (wrong numbers, invented features, extra claims). We
check that the detector labels each one correctly.

Two cases are marked `requires_judge`: a swapped error code and a negated
instruction. Embeddings cannot catch these (the words are all in the
context), so without the Claude judge they are skipped, which documents
exactly why the LLM judge exists.
"""

from __future__ import annotations

import pytest

from evaluation.checks import load_jsonl
from evaluation.groundedness import extract_facts

pytestmark = pytest.mark.unit
CALIBRATION = load_jsonl("groundedness_calibration.jsonl")


@pytest.mark.parametrize("case", CALIBRATION, ids=[c["id"] for c in CALIBRATION])
def test_detector_labels_calibration_case(case, evaluator):
    if case.get("requires_judge") and evaluator.judge is None:
        pytest.skip("needs the LLM judge (set ANTHROPIC_API_KEY): embeddings cannot see this error")
    result = evaluator.evaluate(case["question"], case["answer"], case["contexts"])
    expected_flag = case["label"] == "hallucinated"
    assert result.hallucination == expected_flag, f"score={result.score} reasons={result.reasons}"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Set it to -18°C", {"-18"}),
        ("Call 1-800-555-0199", {"1", "800", "555", "0199"}),
        ("A 12,000 BTU unit", {"12000"}),
        ("Error E21 on the WM-7000", {"E21", "WM7000"}),
        ("1. Remove the bolts\n2. Level the machine", set()),
        ("Use 0.05 MPa", {"0.05"}),
    ],
)
def test_fact_extraction(text, expected):
    assert extract_facts(text) == expected
