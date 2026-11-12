"""Groundedness scoring and hallucination detection.

An answer is "grounded" when everything it says can be found in the context the
bot retrieved. A "hallucination" is a claim the context does not support.

We combine three independent checks, each strong where the others are weak:

1. Semantic similarity (always on). Split the answer into sentences, embed
   each one, and find its closest context sentence. A sentence whose best
   cosine similarity is below `sentence_threshold` is "unsupported".
   semantic_support = supported sentences / all sentences.

2. Fact consistency (always on). Numbers and codes are where hallucinations
   hurt most ("set the fridge to 7°C"), and embeddings barely notice when one
   number is swapped for another. Every number or code in the answer must
   appear in the context (or the question).

3. LLM-as-judge (when Claude is configured). Claude reads the answer and the
   context and returns a verdict and a 0-1 score (see judge.py).

Final score:
    score = semantic_support                        (no judge)
    score = 0.5 * semantic_support + 0.5 * judge    (with judge)
    score = min(score, 0.4) if any number/code is unsupported
    hallucination = score < flag_threshold (0.7)

If the two methods disagree strongly, the average lands near 0.5 and the answer
is flagged: a false alarm costs a human a quick look, while a missed
hallucination reaches a customer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import numpy as np

from app.embeddings import FastEmbedEmbeddings, cosine_similarity_matrix
from evaluation.judge import ClaudeJudge, JudgeVerdict

ABSTENTION_PATTERNS = re.compile(
    r"(couldn't find that in the product manuals|can only help with questions about your home appliances|"
    r"(not|isn't|is not) (mentioned|covered|included|stated|specified|available) in the (manual|context|excerpts)|"
    r"(manuals?|excerpts?|context) (do|does) not (contain|mention|include|say|specify)|"
    r"i (don't|do not) have (that|this|any) information)",
    re.I,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+|;\s+")
LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.M)
MARKDOWN_RE = re.compile(r"[*_`#]+")
# A number: 4, -18, 0.05, 12,000. A leading "-" only counts as a minus sign
# after a space or bracket, so "1-800-555" is read as 1, 800, 555. Digits glued
# to letters (E21, WM-7000) are codes, handled by CODE_RE instead.
NUMBER_RE = re.compile(r"(?:(?<![\w-])[-−])?(?<![\w.,])(?<![A-Za-z]-)\d+(?:[.,]\d+)*")
CODE_RE = re.compile(r"\b[A-Z]{1,4}-?\d{1,5}[A-Za-z]?\b")  # E21, F1, WM-7000, AC-12


def is_abstention(answer: str) -> bool:
    return bool(ABSTENTION_PATTERNS.search(answer))


def split_claims(text: str) -> list[str]:
    text = MARKDOWN_RE.sub("", LIST_MARKER_RE.sub("", text))
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text)]
    return [p for p in parts if len(p.split()) >= 3]


def split_context_units(contexts: list[str]) -> list[str]:
    units: list[str] = []
    for ctx in contexts:
        for line in ctx.splitlines():
            line = LIST_MARKER_RE.sub("", line).strip()
            if len(line.split()) < 3:
                continue
            units.append(line)
            # Long lines (e.g. linearised table rows) also count piece by piece.
            units.extend(p.strip() for p in re.split(r"(?<=[.!?])\s+|;\s+", line) if len(p.split()) >= 3)
    return list(dict.fromkeys(units))  # de-duplicate, keep order


def _normalise_number(token: str) -> str:
    token = token.replace("−", "-")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+", token):
        token = token.replace(",", "")  # 12,000 -> 12000
    return token


def extract_facts(text: str) -> set[str]:
    text = LIST_MARKER_RE.sub("", text)  # "1. Remove the bolts" -> the 1 is not a fact
    numbers = {_normalise_number(n) for n in NUMBER_RE.findall(text)}
    codes = {c.upper().replace("-", "") for c in CODE_RE.findall(text)}
    return numbers | codes


@dataclass
class SentenceScore:
    sentence: str
    best_similarity: float
    supported: bool


@dataclass
class GroundednessResult:
    score: float
    hallucination: bool
    abstained: bool
    method: str
    semantic_support: float
    unsupported_numbers: list[str] = field(default_factory=list)
    sentences: list[SentenceScore] = field(default_factory=list)
    judge: dict | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["sentences"] = [asdict(s) for s in self.sentences]
        return data


class GroundednessEvaluator:
    def __init__(
        self,
        embeddings: FastEmbedEmbeddings,
        judge: ClaudeJudge | None = None,
        sentence_threshold: float = 0.75,  # calibrated: see tests/unit/test_groundedness_calibration.py
        flag_threshold: float = 0.70,
    ) -> None:
        self.embeddings = embeddings
        self.judge = judge
        self.sentence_threshold = sentence_threshold
        self.flag_threshold = flag_threshold

    # -- check 1 -------------------------------------------------------------
    def semantic_check(self, answer: str, contexts: list[str]) -> list[SentenceScore]:
        claims = split_claims(answer)
        units = split_context_units(contexts)
        if not claims:
            return []
        if not units:
            return [SentenceScore(c, 0.0, False) for c in claims]
        sims = cosine_similarity_matrix(self.embeddings.embed_array(claims), self.embeddings.embed_array(units))
        best = np.max(sims, axis=1)
        return [
            SentenceScore(claim, round(float(b), 4), bool(b >= self.sentence_threshold)) for claim, b in zip(claims, best)
        ]

    # -- check 2 -------------------------------------------------------------
    @staticmethod
    def fact_check(question: str, answer: str, contexts: list[str]) -> list[str]:
        allowed = extract_facts(question)
        for ctx in contexts:
            allowed |= extract_facts(ctx)
        return sorted(extract_facts(answer) - allowed)

    # -- combined ------------------------------------------------------------
    def evaluate(self, question: str, answer: str, contexts: list[str], use_judge: bool = True) -> GroundednessResult:
        if is_abstention(answer):
            return GroundednessResult(
                score=1.0, hallucination=False, abstained=True, method="abstention", semantic_support=1.0,
                reasons=["The bot declined to answer, so it made no claims."],
            )

        sentences = self.semantic_check(answer, contexts)
        semantic_support = (sum(s.supported for s in sentences) / len(sentences)) if sentences else 1.0
        unsupported_numbers = self.fact_check(question, answer, contexts)

        reasons: list[str] = []
        unsupported = [s for s in sentences if not s.supported]
        if unsupported:
            reasons.append(f"semantic: {len(unsupported)}/{len(sentences)} sentence(s) not found in the context")
        if unsupported_numbers:
            reasons.append(f"facts: numbers/codes not in the context: {', '.join(unsupported_numbers)}")

        score, method, judge_dict = semantic_support, "semantic+facts", None
        verdict: JudgeVerdict | None = None
        if use_judge and self.judge is not None:
            verdict = self.judge.judge(question, answer, contexts)
        if verdict is not None:
            judge_dict = verdict.model_dump()
            score = 0.5 * semantic_support + 0.5 * verdict.score
            method = "semantic+facts+llm_judge"
            if verdict.verdict != "grounded":
                reasons.append(f"judge: {verdict.verdict} ({verdict.reasoning})")

        if unsupported_numbers:
            score = min(score, 0.4)

        return GroundednessResult(
            score=round(score, 4),
            hallucination=score < self.flag_threshold,
            abstained=False,
            method=method,
            semantic_support=round(semantic_support, 4),
            unsupported_numbers=unsupported_numbers,
            sentences=sentences,
            judge=judge_dict,
            reasons=reasons,
        )


def build_evaluator(settings, embeddings: FastEmbedEmbeddings) -> GroundednessEvaluator:
    """Create the evaluator, with the Claude judge only if Claude is configured."""
    judge = ClaudeJudge(settings.judge_model, settings.judge_effort) if settings.llm_provider == "claude" else None
    return GroundednessEvaluator(embeddings, judge=judge)
