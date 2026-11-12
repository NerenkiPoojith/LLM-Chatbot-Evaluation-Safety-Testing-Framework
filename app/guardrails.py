"""Safety guardrails around the LLM.

The chatbot uses defence in depth: several independent layers, so an attack
has to get past all of them.

    user question --> [1. input check] --> retrieval --> [2. context check]
        --> LLM (with hardened system prompt) --> [3. output check] --> answer

1. Input check: block obvious prompt-injection and jailbreak attempts before
   they reach the model (saves money and removes the easy attacks).
2. Context check: drop retrieved chunks that contain instructions aimed at the
   AI (indirect prompt injection hidden in documents) or that look like
   customer records (personal data indexed by mistake).
3. Output check: redact PII, remove unknown links, and block the answer if the
   system-prompt canary leaked.

Pattern rules are transparent and fast, but attackers can rephrase around them.
That is why the system prompt and the output checks remain as later layers, and
why the adversarial test set measures the whole system, not each rule.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.pii import Allowlist, PIIFinding, find_pii, redact_pii
from app.prompts import OUT_OF_SCOPE_REPLY, SYSTEM_PROMPT_CANARY

# (rule name, regex). Matching any rule flags the text.
INJECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|all|any|your|the|system)\b[^.\n]{0,25}\b"
            r"(instructions?|rules?|prompts?|guidelines?|polic(y|ies)|restrictions?)",
            re.I,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"\b(you are now|from now on,? you|act as an? (unrestricted|unfiltered|uncensored|evil|different)|"
            r"pretend (to be|you are|you're)|roleplay as|developer mode|god mode|jailbr(ea|o)k|"
            r"do anything now|no (restrictions|rules|limits|filters))\b",
            re.I,
        ),
    ),
    ("dan_persona", re.compile(r"\bDAN\b")),
    (
        "prompt_extraction",
        re.compile(
            r"\b(reveal|show|print|repeat|display|output|tell me|give me|what (is|are|were)|share|leak|spell out)\b"
            r"[^.\n]{0,30}\b(system prompt|your (initial |original |hidden |secret |internal )?"
            r"(instructions|rules|prompt|guidelines)|the (initial|original|hidden|secret|internal|system) "
            r"(instructions|rules|prompt|guidelines)|(the )?(text|words|instructions) above|confidential reference)",
            re.I,
        ),
    ),
    (
        "delimiter_injection",
        re.compile(r"(</?\s*(system|context|instructions?|excerpt)\s*>|\[/?INST\]|<\|im_start\|>|^\s*#{2,}\s*system)", re.I | re.M),
    ),
    ("addresses_the_ai", re.compile(r"\b(attention|note to|message for)\s+(the\s+)?(ai|assistant|chatbot|llm|model)\b", re.I)),
]

BASE64_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}")
URL_RE = re.compile(r"\bhttps?://[^\s)>\]]+", re.I)


@dataclass
class InjectionCheck:
    flagged: bool
    matched_rules: list[str] = field(default_factory=list)


def _decoded_base64_segments(text: str) -> list[str]:
    """Attackers sometimes hide instructions in base64. Decode and scan them too."""
    decoded = []
    for blob in BASE64_BLOB_RE.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            candidate = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if candidate.isprintable():
            decoded.append(candidate)
    return decoded


def detect_prompt_injection(text: str) -> InjectionCheck:
    matched: list[str] = []
    for segment, prefix in [(text, "")] + [(d, "base64:") for d in _decoded_base64_segments(text)]:
        for name, pattern in INJECTION_RULES:
            if pattern.search(segment) and prefix + name not in matched:
                matched.append(prefix + name)
    return InjectionCheck(flagged=bool(matched), matched_rules=matched)


PII_DENSE_THRESHOLD = 2  # a manual chunk has no reason to hold 2+ personal contact details


def filter_contexts(
    contexts: list[dict], allowlist: Allowlist | Callable[[dict], Allowlist]
) -> tuple[list[dict], list[dict]]:
    """Split retrieved chunks into (safe, dropped).

    A chunk is dropped when it:
    * contains instructions aimed at the AI (indirect prompt injection), or
    * is "PII-dense": two or more non-allowlisted phones, emails or addresses.
      Product manuals don't hold personal records, so a chunk like this is
      almost certainly customer data that was indexed by mistake. Dropping the
      whole chunk also removes the customer names, which regexes cannot detect.

    The source (file name) is scanned with the text, because it reaches the model too.
    `allowlist` can be a function of the chunk, so an uploaded manual's own support
    contacts are allowed only in that manual's chunks.
    """
    allowlist_for = allowlist if callable(allowlist) else (lambda ctx: allowlist)
    kept, dropped = [], []
    for ctx in contexts:
        scanned = f"{ctx.get('source', '')}\n{ctx['text']}"
        check = detect_prompt_injection(scanned)
        pii_count = len(find_pii(scanned, allowlist_for(ctx)))
        if check.flagged:
            dropped.append({**ctx, "reason": "prompt_injection", "matched_rules": check.matched_rules})
        elif pii_count >= PII_DENSE_THRESHOLD:
            dropped.append({**ctx, "reason": "pii_dense", "matched_rules": [f"{pii_count} PII items"]})
        else:
            kept.append(ctx)
    return kept, dropped


@dataclass
class OutputCheck:
    text: str
    pii_findings: list[PIIFinding] = field(default_factory=list)
    removed_urls: list[str] = field(default_factory=list)
    canary_leaked: bool = False


def strip_unknown_urls(text: str, allowlist: Allowlist) -> tuple[str, list[str]]:
    removed = []

    def _replace(match: re.Match[str]) -> str:
        url = match.group().rstrip(".,;")
        host = (urlparse(url).hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in allowlist.url_domains):
            return match.group()
        removed.append(url)
        return "[link removed]"

    return URL_RE.sub(_replace, text), removed


def check_output(text: str, allowlist: Allowlist) -> OutputCheck:
    if SYSTEM_PROMPT_CANARY.lower() in text.lower():
        # The model leaked its instructions: throw the whole answer away.
        return OutputCheck(text=OUT_OF_SCOPE_REPLY, canary_leaked=True)
    text, removed_urls = strip_unknown_urls(text, allowlist)
    text, findings = redact_pii(text, allowlist)
    return OutputCheck(text=text, pii_findings=findings, removed_urls=removed_urls)
