"""Unit tests for the Claude code paths, using a fake client.

These run without an API key or network. A fake client records the request
and returns a canned response, so we can check that:
  * we send the request settings we intend (model, adaptive thinking, fallbacks),
  * we extract the text correctly and ignore thinking blocks,
  * a refusal turns into our standard refusal message,
  * the judge returns None (not a crash) when it gets no verdict.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import ClaudeGenerator
from app.prompts import NOT_FOUND_REPLY, OUT_OF_SCOPE_REPLY, SYSTEM_PROMPT
from evaluation.judge import ClaudeJudge, JudgeVerdict

pytestmark = pytest.mark.unit

CONTEXTS = [{"source": "wm.md", "section": "Errors", "text": "Code: E21; Meaning: Water is not draining"}]


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    parse = create


def fake_client(response):
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def message(stop_reason="end_turn", blocks=None, parsed=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=blocks or [],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        parsed_output=parsed,
    )


def test_generator_sends_expected_request_and_returns_text():
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="E21 means water is not draining.")]
    client, calls = fake_client(message(blocks=blocks))
    gen = ClaudeGenerator("claude-opus-5-5", "low", client=client).generate("What is E21?", CONTEXTS)

    assert gen.text == "E21 means water is not draining."
    assert (gen.input_tokens, gen.output_tokens) == (120, 30)
    sent = calls.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"] == SYSTEM_PROMPT
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["fallbacks"] == "default"
    assert "<context>" in sent["messages"][0]["content"]


def test_generator_maps_refusal_to_standard_reply():
    client, _ = fake_client(message(stop_reason="refusal"))
    gen = ClaudeGenerator("claude-opus-5-5", client=client).generate("anything", CONTEXTS)
    assert gen.text == OUT_OF_SCOPE_REPLY


def test_generator_skips_the_api_when_nothing_was_retrieved():
    client, calls = fake_client(message())
    gen = ClaudeGenerator("claude-opus-5-5", client=client).generate("What is the capital of Australia?", [])
    assert gen.text == NOT_FOUND_REPLY
    assert calls.calls == []


def test_judge_returns_parsed_verdict_and_clamps_score():
    verdict = JudgeVerdict(verdict="grounded", score=1.3, unsupported_claims=[], reasoning="All claims supported.")
    client, calls = fake_client(message(parsed=verdict))
    result = ClaudeJudge("claude-opus-5-5", client=client).judge("q", "a", ["ctx"])
    assert result is not None and result.score == 1.0
    assert calls.calls[0]["output_format"] is JudgeVerdict


def test_judge_returns_none_on_refusal():
    client, _ = fake_client(message(stop_reason="refusal"))
    assert ClaudeJudge("claude-opus-5-5", client=client).judge("q", "a", ["ctx"]) is None
