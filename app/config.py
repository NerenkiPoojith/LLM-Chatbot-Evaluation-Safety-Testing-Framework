"""Central configuration.

Every setting can be overridden with an environment variable or a line in a
`.env` file at the project root (see `.env.example`). Keeping all knobs in one
place makes it easy to compare runs: the evaluation report records these values,
so you can see exactly which model / prompt / retrieval settings produced a score.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# The two planted risk documents in data/manuals (customer records and a poisoned forum
# post). They stay indexed so the guardrails can be tested, but they are not sample manuals.
PLANTED_DOCS = frozenset({"internal_warranty_registrations", "community_forum_tips"})


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in _env(name, default).split(",") if item.strip())


def _resolve_provider(requested: str) -> str:
    """'auto' means: use Claude if credentials exist, otherwise the offline mock."""
    requested = requested.lower()
    if requested in {"claude", "mock"}:
        return requested
    has_credentials = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return "claude" if has_credentials else "mock"


@dataclass(frozen=True)
class Settings:
    # Where the knowledge base lives
    manuals_dir: Path
    uploads_dir: Path  # manuals uploaded through the API / chat page are saved here
    max_upload_mb: int
    ocr_max_pages: int  # scanned pages or photos read with OCR per manual (about 6 s each)
    ocr_model_dir: Path  # the English OCR model is downloaded here once
    pii_allowlist_path: Path

    # LLM settings
    llm_provider: str  # "claude" or "mock"
    chat_model: str
    judge_model: str
    chat_effort: str
    judge_effort: str

    # Retrieval settings
    embedding_model: str
    embedding_cache_dir: Path
    chunk_size: int
    chunk_overlap: int
    top_k: int
    min_retrieval_score: float

    # Safety
    guardrails_enabled: bool
    # Host names the server answers to. Any other Host header is refused, so a web page on
    # another domain cannot reach this local API through DNS rebinding.
    allowed_hosts: tuple[str, ...]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        manuals_dir=Path(_env("MANUALS_DIR", str(PROJECT_ROOT / "data" / "manuals"))),
        uploads_dir=Path(_env("UPLOADS_DIR", str(PROJECT_ROOT / "data" / "uploads"))),
        max_upload_mb=int(_env("MAX_UPLOAD_MB", "20")),
        ocr_max_pages=int(_env("OCR_MAX_PAGES", "60")),
        ocr_model_dir=Path(_env("OCR_MODEL_DIR", str(PROJECT_ROOT / ".cache" / "ocr"))),
        pii_allowlist_path=Path(_env("PII_ALLOWLIST", str(PROJECT_ROOT / "data" / "pii_allowlist.json"))),
        llm_provider=_resolve_provider(_env("LLM_PROVIDER", "auto")),
        chat_model=_env("CHAT_MODEL", "claude-opus-5-5"),
        judge_model=_env("JUDGE_MODEL", "claude-opus-5-5"),
        chat_effort=_env("CHAT_EFFORT", "low"),
        judge_effort=_env("JUDGE_EFFORT", "medium"),
        embedding_model=_env("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        embedding_cache_dir=Path(_env("EMBEDDING_CACHE_DIR", str(PROJECT_ROOT / ".cache" / "fastembed"))),
        chunk_size=int(_env("CHUNK_SIZE", "700")),
        chunk_overlap=int(_env("CHUNK_OVERLAP", "100")),
        top_k=int(_env("TOP_K", "4")),
        min_retrieval_score=float(_env("MIN_RETRIEVAL_SCORE", "0.55")),
        guardrails_enabled=_env_bool("GUARDRAILS_ENABLED", True),
        allowed_hosts=_env_list("ALLOWED_HOSTS", "127.0.0.1,localhost"),
    )
