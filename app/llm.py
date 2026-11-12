"""Answer generators: the "G" in RAG.

Two interchangeable implementations share one method, `generate(question, contexts)`:

* ClaudeGenerator: calls Claude through the official Anthropic Python SDK.
* ExtractiveGenerator: an offline stand-in that needs no API key. It picks the
  context sentences most similar to the question. Its answers are copied from
  the manuals, so they are grounded by construction. Use it to run the API and
  all the tests for free; switch to Claude for real generative answers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from app.embeddings import FastEmbedEmbeddings, cosine_similarity_matrix
from app.prompts import NOT_FOUND_REPLY, OUT_OF_SCOPE_REPLY, SYSTEM_PROMPT, build_user_message


@dataclass
class Generation:
    text: str
    model: str
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    extra: dict = field(default_factory=dict)


class AnswerGenerator(Protocol):
    model_name: str

    def generate(self, question: str, contexts: list[dict]) -> Generation: ...


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
class ClaudeGenerator:
    """Generate answers with Claude.

    Request settings:
    * thinking={"type": "adaptive"}: Claude decides how much to reason before it answers.
    * output_config.effort: how hard it thinks. "low" keeps chat replies fast.
    * fallbacks="default" (beta): if a safety classifier declines the request,
      the API retries it on a suitable fallback model in the same call.
    """

    def __init__(self, model: str, effort: str = "low", client=None) -> None:
        import anthropic  # imported here so the mock mode never needs the package configured

        self.model_name = model
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=3)

    def generate(self, question: str, contexts: list[dict]) -> Generation:
        if not contexts:
            # Nothing relevant was retrieved, so don't let the model guess.
            return Generation(text=NOT_FOUND_REPLY, model=self.model_name, stop_reason="no_context")

        started = time.perf_counter()
        response = self.client.beta.messages.create(
            model=self.model_name,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(question, contexts)}],
        )
        latency_ms = (time.perf_counter() - started) * 1000

        if response.stop_reason == "refusal":
            # Every model in the fallback chain declined: answer with our standard refusal.
            text = OUT_OF_SCOPE_REPLY
        else:
            # The response also contains (empty) thinking blocks; keep only the text.
            text = "".join(block.text for block in response.content if block.type == "text").strip()

        return Generation(
            text=text or NOT_FOUND_REPLY,
            model=response.model,
            stop_reason=response.stop_reason or "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            extra={"request_id": getattr(response, "_request_id", None)},
        )


# ---------------------------------------------------------------------------
# Offline extractive generator
# ---------------------------------------------------------------------------
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from how i if in is it its my of on or should so that the "
    "this to was what when where which who why will with you your me much many there their them about into "
    "use using get make".split()
)


PRONOUN_START_RE = re.compile(r"^(they|it|this|these|that|those)\b", re.I)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip(" -*\t") for p in SENTENCE_SPLIT_RE.split(text)]
    return [p for p in parts if len(p.split()) >= 2 and not p.startswith("[")]


def content_words(text: str, ignore: frozenset[str] = frozenset()) -> set[str]:
    """Lower-case words minus stop-words, cut to 5 letters as a crude stemmer
    (so "filters" and "filter" match)."""
    return {w[:5] for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and w[:5] not in ignore}


class ExtractiveGenerator:
    """Answer by quoting the context sentences closest in meaning to the question.

    It combines two signals (a "hybrid" match):
    * semantic: cosine similarity between question and sentence embeddings;
    * lexical: the sentence must share at least one content word with the
      question, not counting words from the product title. Without this,
      "How much does the FL-500 cost?" would match any sentence that mentions
      FL-500, even though the manual never states a price.
    Sentences come only from the manual of the best-matching chunk, so a
    dishwasher question is not answered with air-conditioner sentences.
    If a chosen sentence is followed by one that starts with a pronoun
    ("They can explode..."), that follow-up is added, because it is still about
    the same subject (a tiny "sentence window").
    """

    model_name = "extractive-mock"

    def __init__(
        self, embeddings: FastEmbedEmbeddings, min_similarity: float = 0.60, max_sentences: int = 3, margin: float = 0.10
    ) -> None:
        self.embeddings = embeddings
        self.min_similarity = min_similarity
        self.max_sentences = max_sentences
        self.margin = margin

    def generate(self, question: str, contexts: list[dict]) -> Generation:
        started = time.perf_counter()
        if not contexts:
            return Generation(text=NOT_FOUND_REPLY, model=self.model_name, stop_reason="no_context")

        top_source = contexts[0]["source"]  # contexts arrive sorted by retrieval score
        title_words = content_words(contexts[0].get("title", "") + " user manual")
        question_words = content_words(question, ignore=frozenset(title_words))

        candidates: list[str] = []
        follow_ups: dict[str, str] = {}  # sentence -> the pronoun sentence right after it
        for ctx in contexts:
            if ctx["source"] != top_source:
                continue
            sentences = split_sentences(ctx["text"])
            for i, sentence in enumerate(sentences):
                if i + 1 < len(sentences) and PRONOUN_START_RE.match(sentences[i + 1]):
                    follow_ups[sentence] = sentences[i + 1]
                if sentence not in candidates and question_words & content_words(sentence):
                    candidates.append(sentence)
        if not candidates:
            return Generation(text=NOT_FOUND_REPLY, model=self.model_name, stop_reason="no_overlap")

        q = self.embeddings.embed_array([question])
        s = self.embeddings.embed_array(candidates)
        scores = cosine_similarity_matrix(q, s)[0]
        ranked = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)

        best = float(scores[ranked[0]])
        if best < self.min_similarity:
            text = NOT_FOUND_REPLY
        else:
            # Keep sentences close to the best one, then restore reading order.
            chosen = [i for i in ranked[: self.max_sentences] if scores[i] >= max(self.min_similarity, best - self.margin)]
            parts: list[str] = []
            for i in sorted(chosen):
                for sentence in (candidates[i], follow_ups.get(candidates[i])):
                    if sentence and _as_sentence(sentence) not in parts:
                        parts.append(_as_sentence(sentence))
            text = " ".join(parts)

        return Generation(
            text=text,
            model=self.model_name,
            latency_ms=(time.perf_counter() - started) * 1000,
            extra={"best_similarity": round(best, 3)},
        )


def _as_sentence(text: str) -> str:
    text = text.replace(" | ", " — ")  # PDF table rows arrive as "F07 | Meaning | Action"
    return text if text.endswith((".", "!", "?")) else text + "."
