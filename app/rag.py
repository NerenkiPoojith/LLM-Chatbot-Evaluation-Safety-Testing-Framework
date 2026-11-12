"""The RAG pipeline, built as a LangChain (LCEL) chain.

    {"question"} -> input_guard -> retrieve -> context_guard -> generate -> output_guard -> result

Each step is a small function wrapped in `RunnableLambda`, and the `|` operator
joins them into one Runnable. Every step receives a dict ("state"), adds its
results, and passes the dict on. If the input guard blocks a question, later
steps see `blocked=True` and do nothing, so a blocked question never reaches
retrieval or the LLM.

Building it as a chain (rather than one long function) gives each step a name,
lets LangChain tracing tools show the steps separately, and makes it easy to
swap or add steps, such as a re-ranker after retrieval.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda

from app.config import PLANTED_DOCS, Settings, get_settings
from app.embeddings import get_embeddings
from app.guardrails import check_output, detect_prompt_injection, filter_contexts
from app.ingest import (
    SUPPORTED_SUFFIXES,
    TOO_MUCH_TEXT,
    ManualError,
    build_vector_store,
    builtin_manual_id,
    chunk_ids,
    clean_filename,
    content_digest,
    format_label,
    load_manuals,
    make_manual_id,
    manual_suffix,
    parse_manual,
    split_documents,
)
from app.formats import Progress
from app.llm import AnswerGenerator, ClaudeGenerator, ExtractiveGenerator, Generation
from app.pii import Allowlist, contact_key, find_pii, load_allowlist, merge_allowlist, redact_pii
from app.prompts import NOT_FOUND_REPLY, OUT_OF_SCOPE_REPLY, PROMPT_VERSION

logger = logging.getLogger("appliancehelp")

# A section about support or contact details is the manual's own support block; its phones
# and emails are allowed in answers from that manual (see support_contacts).
SUPPORT_TEXT_RE = re.compile(
    r"\bsupport\b|\bcontact(s| us)?\b|customer (service|care)|helpline|hotline|service cent(re|er)|call us|"
    r"service department",
    re.I,
)
MAX_SUPPORT_CONTACTS = 3  # per section: a support page lists a phone and an email, not a customer list
MAX_SUPPORT_SECTIONS = 2  # e.g. "Customer support" and "Spare parts"; more looks like a list of records
MAX_MANUAL_CHUNKS = 6_000  # about five minutes of embedding on a laptop CPU
STORE_SUFFIX = ".store.json"  # an upload's saved vectors, next to the file itself


@dataclass
class ManualRecord:
    manual_id: str
    title: str
    source: str
    chunks: int
    uploaded: bool
    pages: int | None = None
    planted: bool = False
    format: str | None = None       # e.g. "Word document"
    ocr_pages: int = 0              # pages read from images with OCR
    ocr_skipped_pages: int = 0      # picture-only pages of a PDF left unread: past the OCR page limit
    chunk_ids: list[str] = field(default_factory=list)
    support_phones: frozenset[str] = frozenset()
    support_emails: frozenset[str] = frozenset()

    def public(self) -> dict:
        return {"manual_id": self.manual_id, "title": self.title, "source": self.source, "chunks": self.chunks,
                "uploaded": self.uploaded, "pages": self.pages, "planted": self.planted,
                "format": self.format, "ocr_pages": self.ocr_pages, "ocr_skipped_pages": self.ocr_skipped_pages}


def support_contacts(
    chunks: list[Document], allowlist: Allowlist, never: Iterable[str] = ()
) -> tuple[set[str], set[str]]:
    """Phones and emails from an uploaded manual's own support section, normalised
    (phones as digits, emails in lower case).

    Without this, a real manual's hotline would be redacted as personal data, and its support
    chunk (phone + email) would even be dropped as "PII-dense". Only a chunk's section heading
    and text are searched for support words, never the manual title that every chunk repeats.
    Contacts are counted per section (all chunks of one section together): a manual gets none if
    any section holds more than 3, or more than 2 sections hold some, so a list of customers never
    qualifies, even spread over many chunks, while "Customer support" plus "Spare parts" still does.
    Values in `never` (the contacts printed in the built-in documents, including the planted
    customer records) are never allowed.
    """
    never = set(never)
    sections: dict[str, dict[str, str]] = defaultdict(dict)
    for chunk in chunks:
        section = chunk.metadata.get("section", "")
        body = chunk.page_content.split("\n", 1)[-1]  # without the "[title — section]" header
        if not SUPPORT_TEXT_RE.search(f"{section}\n{body}"):
            continue
        for f in find_pii(body, allowlist):
            if f.kind in {"phone", "email"}:
                sections[section][contact_key(f)] = f.kind
    sections = {name: found for name, found in sections.items() if found}
    if len(sections) > MAX_SUPPORT_SECTIONS or any(len(f) > MAX_SUPPORT_CONTACTS for f in sections.values()):
        return set(), set()
    found = {key: kind for f in sections.values() for key, kind in f.items()}
    allowed = {key: kind for key, kind in found.items() if key not in never}
    return {k for k, kind in allowed.items() if kind == "phone"}, {k for k, kind in allowed.items() if kind == "email"}


def _without(allowlist: Allowlist, contacts: set[str]) -> Allowlist:
    """`allowlist` minus some contacts (phones as digits, emails in lower case)."""
    if not contacts:
        return allowlist
    return Allowlist(emails=allowlist.emails - contacts, phones=allowlist.phones - contacts,
                     url_domains=allowlist.url_domains)


@dataclass
class ChatResult:
    answer: str
    sources: list[dict]
    guardrails: dict
    model: str
    llm_provider: str
    prompt_version: str
    latency_ms: float
    usage: dict = field(default_factory=dict)


class ChatbotPipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        guardrails_enabled: bool | None = None,
        generator: AnswerGenerator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.guardrails_enabled = (
            self.settings.guardrails_enabled if guardrails_enabled is None else guardrails_enabled
        )
        self.embeddings = get_embeddings(self.settings.embedding_model, str(self.settings.embedding_cache_dir))
        self.base_allowlist = load_allowlist(str(self.settings.pii_allowlist_path))
        self._lock = threading.RLock()  # uploads and questions can arrive at the same time
        self.registry: dict[str, ManualRecord] = {}
        self._in_flight: dict[str, threading.Event] = {}  # content digest -> set when that upload ends

        # Built-in manuals are indexed at start-up, then any manuals uploaded earlier.
        self.vector_store = build_vector_store([], self.embeddings)
        documents = load_manuals(self.settings.manuals_dir)
        chunks = split_documents(documents, self.settings.chunk_size, self.settings.chunk_overlap)
        # Contacts printed in the built-in documents: an uploaded file can never allowlist them.
        self._builtin_contacts = {
            contact_key(f) for c in chunks for f in find_pii(c.page_content) if f.kind in {"phone", "email"}
        }
        # Content hash -> id, so a copy of a built-in manual is reported as a duplicate.
        self._builtin_digests = {
            content_digest(path.read_bytes()): builtin_manual_id(path.name)
            for path in self.settings.manuals_dir.iterdir() if path.suffix.lower() in SUPPORTED_SUFFIXES
        }
        for manual_id in dict.fromkeys(c.metadata["manual_id"] for c in chunks):
            group = [c for c in chunks if c.metadata["manual_id"] == manual_id]
            self._publish(self._record(group, uploaded=False), self._embed(group))
        self._dimensions = len(next(iter(self.vector_store.store.values()))["vector"])
        self._load_saved_uploads()

        self.generator = generator or self._make_generator()
        self.llm_provider = "claude" if isinstance(self.generator, ClaudeGenerator) else "mock"
        self.chain = (
            RunnableLambda(self._input_guard, name="input_guard")
            | RunnableLambda(self._retrieve, name="retrieve")
            | RunnableLambda(self._context_guard, name="context_guard")
            | RunnableLambda(self._generate, name="generate")
            | RunnableLambda(self._output_guard, name="output_guard")
        )

    def _make_generator(self) -> AnswerGenerator:
        if self.settings.llm_provider == "claude":
            return ClaudeGenerator(self.settings.chat_model, self.settings.chat_effort)
        return ExtractiveGenerator(self.embeddings)

    # ------------------------------------------------------------------ steps
    def _input_guard(self, state: dict) -> dict:
        state["guardrails"] = {
            "enabled": self.guardrails_enabled,
            "input_flagged": False,
            "input_rules": [],
            "dropped_chunks": [],
            "pii_redacted_in_answer": 0,
            "pii_redacted_in_sources": 0,
            "removed_urls": [],
            "canary_leaked": False,
            "blocked": False,
        }
        if self.guardrails_enabled:
            check = detect_prompt_injection(state["question"])
            if check.flagged:
                state["guardrails"].update(input_flagged=True, input_rules=check.matched_rules, blocked=True)
        return state

    def _retrieve(self, state: dict) -> dict:
        state["contexts"] = []
        if state["guardrails"]["blocked"]:
            return state
        manual_id = state.get("manual_id")
        only_this_manual = (lambda doc: doc.metadata.get("manual_id") == manual_id) if manual_id else None
        query_vector = self.embeddings.embed_query(state["question"])
        with self._lock:
            hits = self.vector_store.similarity_search_with_score_by_vector(
                query_vector, k=state["top_k"], filter=only_this_manual
            )
        state["contexts"] = [
            {
                "source": doc.metadata["source"],
                "manual_id": doc.metadata.get("manual_id"),
                "title": doc.metadata.get("title", ""),
                "section": doc.metadata.get("section", ""),
                "chunk_id": doc.metadata.get("chunk_id"),
                "score": round(float(score), 4),
                "text": doc.page_content,
            }
            for doc, score in hits
            if score >= self.settings.min_retrieval_score  # too-weak matches are noise
        ]
        return state

    def _allowlist_for(self, manual_ids: Iterable[str | None]) -> Allowlist:
        """The base allowlist plus the support contacts of these manuals. An uploaded manual's
        own hotline is allowed only in chunks and answers that come from that manual."""
        records = [record for m in set(manual_ids) if (record := self.registry.get(m))]
        phones = {p for r in records for p in r.support_phones}
        emails = {e for r in records for e in r.support_emails}
        return merge_allowlist(self.base_allowlist, phones, emails) if phones or emails else self.base_allowlist

    def _context_guard(self, state: dict) -> dict:
        if self.guardrails_enabled and state["contexts"]:
            kept, dropped = filter_contexts(state["contexts"], lambda ctx: self._allowlist_for([ctx["manual_id"]]))
            state["contexts"] = kept
            state["guardrails"]["dropped_chunks"] = [
                {"source": d["source"], "section": d["section"], "reason": d["reason"], "matched_rules": d["matched_rules"]}
                for d in dropped
            ]
        return state

    def _generate(self, state: dict) -> dict:
        if state["guardrails"]["blocked"]:
            state["generation"] = Generation(text=OUT_OF_SCOPE_REPLY, model="guardrail", stop_reason="blocked")
        else:
            state["generation"] = self.generator.generate(state["question"], state["contexts"])
        return state

    def _output_guard(self, state: dict) -> dict:
        answer = state["generation"].text
        if self.guardrails_enabled:
            # Only the manuals the answer was built from may contribute their support contacts, and a
            # contact that is personal data in any retrieved chunk (printed in another manual) stays
            # redacted everywhere: one manual's "support" number must not unmask it in another's text.
            contexts = state["contexts"]
            own = [self._allowlist_for([ctx["manual_id"]]) for ctx in contexts]
            foreign = {contact_key(f) for ctx, allowed in zip(contexts, own)
                       for f in find_pii(ctx["text"], allowed) if f.kind in {"phone", "email"}}
            allowlist = _without(self._allowlist_for(ctx["manual_id"] for ctx in contexts), foreign)
            out = check_output(answer, allowlist)
            answer = out.text
            # Citations can leak too: redact the source snippets we return, each with its own manual's contacts.
            source_redactions = 0
            for ctx, allowed in zip(contexts, own):
                ctx["text"], findings = redact_pii(ctx["text"], _without(allowed, foreign))
                source_redactions += len(findings)
            state["guardrails"].update(
                pii_redacted_in_answer=len(out.pii_findings),
                pii_redacted_in_sources=source_redactions,
                removed_urls=out.removed_urls,
                canary_leaked=out.canary_leaked,
            )
            if out.canary_leaked:
                state["guardrails"]["blocked"] = True
        state["answer"] = answer
        return state

    # ---------------------------------------------------------------- public
    def ask(self, question: str, top_k: int | None = None, manual_id: str | None = None) -> ChatResult:
        """Answer a question. With `manual_id`, only that manual is searched.
        Raises KeyError for an unknown manual_id."""
        if manual_id and manual_id not in self.registry:
            raise KeyError(manual_id)
        started = time.perf_counter()
        state = self.chain.invoke(
            {"question": question.strip(), "top_k": top_k or self.settings.top_k, "manual_id": manual_id}
        )
        generation: Generation = state["generation"]
        return ChatResult(
            answer=state["answer"],
            sources=state["contexts"],
            guardrails=state["guardrails"],
            model=generation.model,
            llm_provider=self.llm_provider,
            prompt_version=PROMPT_VERSION,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            usage={"input_tokens": generation.input_tokens, "output_tokens": generation.output_tokens},
        )

    @property
    def chunk_count(self) -> int:
        return sum(r.chunks for r in self.registry.values())

    def manuals(self) -> list[dict]:
        """Built-in manuals first (by file name), then uploads (by title)."""
        records = sorted(self.registry.values(), key=lambda r: (r.uploaded, r.title.lower() if r.uploaded else r.source))
        return [r.public() for r in records]

    # ----------------------------------------------------- manual management
    def _record(self, chunks: list[Document], *, uploaded: bool) -> ManualRecord:
        first = chunks[0].metadata
        phones, emails = (
            support_contacts(chunks, self.base_allowlist, self._builtin_contacts) if uploaded else (set(), set())
        )
        return ManualRecord(
            manual_id=first["manual_id"], title=first.get("title", ""), source=first["source"], chunks=len(chunks),
            uploaded=uploaded, pages=first.get("page_count"), chunk_ids=chunk_ids(chunks),
            format=format_label(first.get("format")), ocr_pages=first.get("ocr_pages") or 0,
            ocr_skipped_pages=first.get("ocr_skipped_pages") or 0,
            planted=not uploaded and first["manual_id"] in PLANTED_DOCS,
            support_phones=frozenset(phones), support_emails=frozenset(emails),
        )

    def _embed(self, chunks: list[Document]) -> dict:
        """Embed into a scratch store (slow for a big PDF), so questions are not blocked meanwhile."""
        return build_vector_store(chunks, self.embeddings).store

    def _publish(self, record: ManualRecord, entries: dict) -> None:
        """Merge embedded chunks into the live store while holding the lock (instant)."""
        with self._lock:
            self.vector_store.store.update(entries)
            self.registry[record.manual_id] = record

    def _find_by_content(self, data: bytes) -> ManualRecord | None:
        """The manual already indexed with exactly these bytes, under any file name. Upload ids
        end with the content hash, so this also finds uploads saved before a restart."""
        digest = content_digest(data)
        if digest in self._builtin_digests:
            return self.registry.get(self._builtin_digests[digest])
        return next((r for r in list(self.registry.values()) if r.uploaded and r.manual_id.endswith(f"-{digest}")), None)

    def add_manual(self, data: bytes, filename: str, title: str | None = None,
                   progress: Progress = None) -> tuple[ManualRecord, bool]:
        """Index an uploaded manual (any supported format) and save it so it survives a restart.

        Returns (record, duplicate). A file whose content is already indexed (an earlier upload,
        even under another name, or a built-in manual) returns that record with duplicate=True; so
        does one whose content another request is indexing right now, once that request is done.
        Raises ManualError for files that cannot be used. `progress(stage, done, total)` is told
        how far reading ("reading", "ocr") and indexing ("indexing") have got, and when the upload
        is queued ("waiting").
        """
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise ManualError(f"This file is larger than the {self.settings.max_upload_mb} MB limit.", status=413)
        filename = clean_filename(filename)
        if not filename:
            raise ManualError("The file has no name.")
        digest = content_digest(data)
        while True:
            with self._lock:
                existing = self._find_by_content(data)
                if existing:
                    return existing, True
                running = self._in_flight.get(digest)
                if running is None:
                    self._in_flight[digest] = threading.Event()
                    break
            # The same file is being read already, for example by the request the page sent before
            # the user reloaded it: wait for that one instead of reading it (and its OCR) twice.
            if progress:
                progress("waiting", 0, 0)
            running.wait()
        try:
            return self._add_new_manual(data, filename, title, progress), False
        finally:
            with self._lock:
                self._in_flight.pop(digest).set()

    def _add_new_manual(self, data: bytes, filename: str, title: str | None, progress: Progress) -> ManualRecord:
        manual_id = make_manual_id(filename, data)
        documents = parse_manual(data, filename, manual_id=manual_id, title=title,
                                 ocr_max_pages=self.settings.ocr_max_pages, progress=progress)
        chunks = split_documents(documents, self.settings.chunk_size, self.settings.chunk_overlap)
        if not chunks:
            raise ManualError("No usable text was found in this file.")
        if len(chunks) > MAX_MANUAL_CHUNKS:
            raise ManualError(TOO_MUCH_TEXT, status=413)
        if progress:
            progress("indexing", 0, len(chunks))
        record = self._record(chunks, uploaded=True)
        entries = self._embed(chunks)
        # Saved before it is published: a manual that could not be saved is never listed
        # or searched, so it cannot vanish at the next restart.
        self._save_upload(record, data, filename, entries)
        self._publish(record, entries)
        return record

    def delete_manual(self, manual_id: str) -> None:
        """Remove an uploaded manual. KeyError if unknown, PermissionError for a built-in manual."""
        record = self.registry.get(manual_id)
        if record is None:
            raise KeyError(manual_id)
        if not record.uploaded:
            raise PermissionError(manual_id)
        with self._lock:
            self.vector_store.delete(record.chunk_ids)
            del self.registry[manual_id]
        self._remove_files(manual_id)

    def _remove_files(self, manual_id: str) -> None:
        for path in self.settings.uploads_dir.glob(f"{manual_id}.*"):  # the file, its vectors, its metadata
            path.unlink(missing_ok=True)

    def _save_upload(self, record: ManualRecord, data: bytes, filename: str, entries: dict) -> None:
        """Save the file, its vectors and its metadata. The metadata file is written last and is
        what start-up looks for, so a half-saved upload is never loaded; on failure nothing is
        left behind."""
        folder = self.settings.uploads_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
            # Every saved upload has a suffix (".bin" if its name had none), so start-up finds it. A ".json"
            # upload is saved as ".json.bin": "<id>.json" is its metadata file, written next.
            suffix = manual_suffix(filename) or ".bin"
            (folder / f"{record.manual_id}{'.json.bin' if suffix == '.json' else suffix}").write_bytes(data)
            self._write_store(record.manual_id, entries)
            meta = {"filename": filename, "title": record.title,
                    "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            (folder / f"{record.manual_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.exception("Could not save upload %s", record.manual_id)
            self._remove_files(record.manual_id)
            raise ManualError("The manual could not be saved on the server, so it was not added.", status=500) from exc

    def _store_settings(self) -> dict:
        """Saved vectors are reused only while the settings that produced them are unchanged."""
        return {"embedding_model": self.settings.embedding_model,
                "chunk_size": self.settings.chunk_size, "chunk_overlap": self.settings.chunk_overlap}

    def _write_store(self, manual_id: str, entries: dict) -> None:
        saved = {"settings": self._store_settings(), "entries": list(entries.values())}
        path = self.settings.uploads_dir / f"{manual_id}{STORE_SUFFIX}"
        path.write_text(json.dumps(saved, separators=(",", ":")), encoding="utf-8")

    def _read_store(self, manual_id: str) -> dict | None:
        """An upload's saved vectors, or None when they are missing, damaged or were made with
        other settings (the caller then embeds the manual again)."""
        try:
            saved = json.loads((self.settings.uploads_dir / f"{manual_id}{STORE_SUFFIX}").read_text(encoding="utf-8"))
            if saved["settings"] != self._store_settings():
                return None
            entries = {e["id"]: e for e in saved["entries"]}
            valid = bool(entries) and all(
                entry_id == f"{manual_id}:{e['metadata']['chunk_id']}" and e["metadata"]["manual_id"] == manual_id
                and isinstance(e["text"], str) and len(e["vector"]) == self._dimensions
                for entry_id, e in entries.items()
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return entries if valid else None

    def _load_saved_uploads(self) -> None:
        """Index the manuals uploaded before this start-up. Their saved vectors are used as they
        are, so start-up does not wait for the embedding model. Only an upload without usable
        saved vectors is read and embedded again, and its vectors are saved for next time."""
        folder = self.settings.uploads_dir
        if not folder.is_dir():
            return
        for meta_path in sorted(folder.glob("*.json")):
            if meta_path.name.endswith(STORE_SUFFIX):
                continue
            manual_id = meta_path.stem
            try:
                entries = self._read_store(manual_id)
                if entries is None:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    data_path = next((p for p in folder.glob(f"{manual_id}.*") if not p.name.endswith(".json")), None)
                    if data_path is None:
                        logger.warning("Skipping saved upload %s: its file is missing from %s, so it cannot be "
                                       "read again with the new settings. Upload the file again to restore it.",
                                       manual_id, folder)
                        continue
                    documents = parse_manual(data_path.read_bytes(), meta["filename"], manual_id=manual_id,
                                             title=meta.get("title"), ocr_max_pages=self.settings.ocr_max_pages)
                    chunks = split_documents(documents, self.settings.chunk_size, self.settings.chunk_overlap)
                    entries = self._embed(chunks)
                    try:
                        self._write_store(manual_id, entries)
                    except OSError:
                        logger.warning("Could not save the vectors of %s; it is embedded again at each start-up.",
                                       manual_id)
                else:
                    chunks = [Document(page_content=e["text"], metadata=e["metadata"]) for e in entries.values()]
                self._publish(self._record(chunks, uploaded=True), entries)
            except Exception:  # a broken saved upload must not stop the server
                logger.exception("Skipping saved upload %s", manual_id)


def is_abstention(answer: str) -> bool:
    """True when the bot declined to answer (not found, or out of scope)."""
    normalized = answer.strip().lower()
    return normalized.startswith(NOT_FOUND_REPLY.lower()[:30]) or normalized.startswith(OUT_OF_SCOPE_REPLY.lower()[:30])
