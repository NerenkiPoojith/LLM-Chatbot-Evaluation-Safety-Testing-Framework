"""LLM-as-judge: ask Claude whether an answer is supported by its context.

Why use an LLM as the judge? Embedding similarity catches answers that drift
off-topic, but it is nearly blind to negation ("never use bleach" vs "use
bleach") and to swapped facts that use the same words ("E21 means the door is
not locked" when the manual says E30 means that). A capable LLM reads the
claims and checks them against the context, the way a human reviewer would.

We use structured outputs (`messages.parse` with a Pydantic model), so the
reply is guaranteed to match the JudgeVerdict schema and can be used directly
in code without fragile text parsing.
"""

from __future__ import annotations

from html import escape
from typing import Literal

from pydantic import BaseModel, Field

JUDGE_SYSTEM_PROMPT = """You are a strict fact-checking judge for a customer-support chatbot about home appliances.

You receive a CONTEXT (excerpts from product manuals), the user's QUESTION and the chatbot's ANSWER. Decide whether every factual claim in the ANSWER is supported by the CONTEXT.

Rules:
- Use only the CONTEXT. Do not use outside knowledge: a claim that is plausible but not stated in the CONTEXT is unsupported.
- Paraphrases are fine. Changed numbers, codes, settings, durations or units are unsupported.
- An answer that contradicts the CONTEXT (for example, recommends something the CONTEXT forbids) is hallucinated.
- An answer saying the information is not available is grounded if the CONTEXT really does not contain it.

Verdicts and scores:
- "grounded" (score 0.9 to 1.0): every claim is supported.
- "partially_grounded" (score 0.4 to 0.8): the main point is supported, but at least one claim is not.
- "hallucinated" (score 0.0 to 0.3): the main claim is unsupported or contradicts the CONTEXT.

List each unsupported claim word for word from the ANSWER. Keep the reasoning to one or two sentences."""


class JudgeVerdict(BaseModel):
    verdict: Literal["grounded", "partially_grounded", "hallucinated"]
    score: float = Field(description="Groundedness from 0.0 (hallucinated) to 1.0 (fully grounded).")
    unsupported_claims: list[str] = Field(description="Claims from the answer that the context does not support.")
    reasoning: str = Field(description="One or two sentences explaining the verdict.")


def build_judge_message(question: str, answer: str, contexts: list[str]) -> str:
    context_block = "\n\n".join(f"[{i}] {escape(c, quote=False)}" for i, c in enumerate(contexts, start=1))
    return (
        f"<context>\n{context_block}\n</context>\n\n"
        f"<question>{escape(question, quote=False)}</question>\n\n"
        f"<answer>{escape(answer, quote=False)}</answer>"
    )


class ClaudeJudge:
    def __init__(self, model: str, effort: str = "medium", client=None) -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=3)

    def judge(self, question: str, answer: str, contexts: list[str]) -> JudgeVerdict | None:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_judge_message(question, answer, contexts)}],
            output_format=JudgeVerdict,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return None  # the caller falls back to the semantic + numeric checks
        verdict = response.parsed_output
        verdict.score = min(max(verdict.score, 0.0), 1.0)
        return verdict
