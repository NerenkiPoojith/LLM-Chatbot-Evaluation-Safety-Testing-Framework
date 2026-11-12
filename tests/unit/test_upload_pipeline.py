"""Unit tests for uploaded manuals inside the pipeline: saving, restarting, duplicates, limits
and which support contacts each answer may contain. Every test uses its own uploads folder."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings

import app.rag as rag
from app.ingest import ManualError
from app.rag import ChatbotPipeline

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "lumina_lw9_manual.pdf"


@pytest.fixture
def isolated(tmp_path, settings):
    return dataclasses.replace(settings, uploads_dir=tmp_path)


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory, settings):
    """One pipeline for the tests that clean up after themselves (building one takes seconds)."""
    return ChatbotPipeline(dataclasses.replace(settings, uploads_dir=tmp_path_factory.mktemp("uploads")))


class RecordingEmbeddings(Embeddings):
    """Wraps the real model and remembers every text it was asked to embed."""

    def __init__(self, inner) -> None:
        self.inner, self.texts = inner, []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)

    def embed_array(self, texts):
        return self.inner.embed_array(texts)


def test_uploads_survive_a_restart(isolated, tmp_path):
    first = ChatbotPipeline(isolated)
    record, duplicate = first.add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")
    assert duplicate is False
    assert (tmp_path / f"{record.manual_id}.store.json").is_file()

    restarted = ChatbotPipeline(isolated)
    assert restarted.registry[record.manual_id].public() == record.public()
    answer = restarted.ask("What does error F07 mean?", manual_id=record.manual_id).answer
    assert "drain pump" in answer

    restarted.delete_manual(record.manual_id)
    assert list(tmp_path.iterdir()) == []


def test_a_restart_does_not_embed_saved_uploads_again(isolated, embeddings, monkeypatch):
    record, _ = ChatbotPipeline(isolated).add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")

    spy = RecordingEmbeddings(embeddings)
    monkeypatch.setattr(rag, "get_embeddings", lambda *args: spy)
    restarted = ChatbotPipeline(isolated)
    assert spy.texts, "the built-in manuals are still embedded at start-up"
    assert not any("Lumina" in text for text in spy.texts)
    assert "drain pump" in restarted.ask("What does error F07 mean?", manual_id=record.manual_id).answer


@pytest.mark.parametrize("damage", ["missing", "corrupt", "other-model"])
def test_missing_or_unusable_saved_vectors_are_rebuilt(isolated, tmp_path, embeddings, monkeypatch, damage):
    record, _ = ChatbotPipeline(isolated).add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")
    store = tmp_path / f"{record.manual_id}.store.json"
    if damage == "missing":
        store.unlink()
    elif damage == "corrupt":
        store.write_text(store.read_text(encoding="utf-8")[:500], encoding="utf-8")
    else:
        saved = json.loads(store.read_text(encoding="utf-8"))
        saved["settings"]["embedding_model"] = "some/other-model"
        store.write_text(json.dumps(saved), encoding="utf-8")

    spy = RecordingEmbeddings(embeddings)
    monkeypatch.setattr(rag, "get_embeddings", lambda *args: spy)
    restarted = ChatbotPipeline(isolated)
    assert any("Lumina" in text for text in spy.texts)
    assert "drain pump" in restarted.ask("What does error F07 mean?", manual_id=record.manual_id).answer
    assert json.loads(store.read_text(encoding="utf-8"))["settings"] == restarted._store_settings()


def test_a_json_upload_keeps_its_file_and_can_be_read_again(isolated, tmp_path):
    record, _ = ChatbotPipeline(isolated).add_manual(b"Kettle K9\n\nDescale the kettle every month.", "specs.json")
    assert (tmp_path / f"{record.manual_id}.json.bin").is_file()            # not overwritten by its metadata

    other_settings = dataclasses.replace(isolated, chunk_size=isolated.chunk_size - 50)   # forces a new read
    restarted = ChatbotPipeline(other_settings)
    assert record.manual_id in restarted.registry
    restarted.delete_manual(record.manual_id)
    assert list(tmp_path.iterdir()) == []


def test_the_same_file_sent_again_while_it_is_read_waits_for_the_first(pipeline, monkeypatch):
    reading, release, calls = threading.Event(), threading.Event(), []
    real_parse = rag.parse_manual

    def slow_parse(*args, **kwargs):           # stands in for a long OCR run
        calls.append(args[1])
        reading.set()
        release.wait(10)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(rag, "parse_manual", slow_parse)
    data = b"# Kettle K7\n\n## Descaling\n\nDescale the kettle every month."
    results, stages = {}, []
    first = threading.Thread(target=lambda: results.update(first=pipeline.add_manual(data, "kettle.md")))
    first.start()
    reading.wait(10)
    second = threading.Thread(target=lambda: results.update(second=pipeline.add_manual(
        data, "kettle (1).md", progress=lambda stage, done, total: stages.append(stage))))
    second.start()
    for _ in range(100):                       # until the second upload is queued
        if stages:
            break
        time.sleep(0.05)
    release.set()
    first.join(10)
    second.join(10)
    try:
        assert calls == ["kettle.md"]                                   # read once
        assert results["first"][1] is False and results["second"] == (results["first"][0], True)
        assert stages == ["waiting"]
    finally:
        pipeline.delete_manual(results["first"][0].manual_id)


def test_the_same_file_under_another_name_is_a_duplicate(isolated):
    first = ChatbotPipeline(isolated)
    record, _ = first.add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")
    again, duplicate = first.add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual (1).pdf")
    assert duplicate is True and again.manual_id == record.manual_id

    restarted = ChatbotPipeline(isolated)
    again, duplicate = restarted.add_manual(FIXTURE.read_bytes(), "copy.pdf")
    assert duplicate is True and again.manual_id == record.manual_id
    assert len([r for r in restarted.registry.values() if r.uploaded]) == 1


def test_a_copy_of_a_built_in_manual_is_a_duplicate(pipeline, settings):
    data = (settings.manuals_dir / "aquawave_wm7000_washing_machine.md").read_bytes()
    record, duplicate = pipeline.add_manual(data, "my washer.md")
    assert duplicate is True
    assert record.manual_id == "aquawave_wm7000_washing_machine" and record.uploaded is False


def test_pages_is_the_real_page_count(pipeline):
    record, _ = pipeline.add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")
    try:
        assert record.pages == 3
    finally:
        pipeline.delete_manual(record.manual_id)


def test_a_manual_with_too_many_chunks_is_refused(pipeline, monkeypatch):
    monkeypatch.setattr(rag, "MAX_MANUAL_CHUNKS", 3)
    with pytest.raises(ManualError, match="too much text") as error:
        pipeline.add_manual(FIXTURE.read_bytes(), "lumina_lw9_manual.pdf")
    assert error.value.status == 413
    assert not [r for r in pipeline.registry.values() if r.uploaded]


def test_a_file_without_a_name_is_refused(pipeline):
    with pytest.raises(ManualError, match="no name"):
        pipeline.add_manual(b"# Kettle\n\nDescale monthly.", "")


def test_odd_file_names_are_saved_with_the_right_suffix(pipeline):
    record, _ = pipeline.add_manual(b"# Kettle K9\n\n## Descaling\n\nDescale monthly.", "notes/trailing.md/")
    try:
        assert record.source == "trailing.md"
        assert (pipeline.settings.uploads_dir / f"{record.manual_id}.md").is_file()
    finally:
        pipeline.delete_manual(record.manual_id)


def test_a_manual_that_cannot_be_saved_is_not_added(pipeline, monkeypatch):
    def disk_full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(pipeline, "_write_store", disk_full)
    entries_before = set(pipeline.vector_store.store)
    with pytest.raises(ManualError, match="could not be saved") as error:
        pipeline.add_manual(b"# Kettle K8\n\n## Descaling\n\nDescale monthly.", "kettle_k8.md")
    assert error.value.status == 500
    assert not [r for r in pipeline.registry.values() if r.uploaded]
    assert set(pipeline.vector_store.store) == entries_before
    assert list(pipeline.settings.uploads_dir.iterdir()) == []


def test_support_contacts_apply_only_to_their_own_manual(pipeline):
    support, _ = pipeline.add_manual(
        b"# Crumbly Toaster T2 Manual\n\n## Support\n\nFor help, phone 0800 444 0123 or email care@crumbly.example.\n",
        "crumbly.md")
    other, _ = pipeline.add_manual(
        b"# Zephyr Blender B6 Manual\n\n## Repairs\n\nFor repairs, phone the installer on 0800 444 0123.\n", "zephyr.md")
    try:
        assert support.support_phones == {"08004440123"} and other.support_phones == frozenset()
        own = pipeline._allowlist_for([support.manual_id])
        assert "08004440123" in own.phones and "care@crumbly.example" in own.emails
        assert own.url_domains == pipeline.base_allowlist.url_domains  # email domains never unlock links
        assert pipeline._allowlist_for([other.manual_id]) == pipeline.base_allowlist

        context = {"manual_id": other.manual_id, "source": "zephyr.md", "section": "Repairs", "chunk_id": 0,
                   "score": 1.0, "title": "", "text": "[Zephyr — Repairs]\nPhone 0800 444 0123 or care@crumbly.example."}
        state = pipeline._context_guard({"contexts": [context], "guardrails": {"dropped_chunks": []}})
        assert state["contexts"] == [] and state["guardrails"]["dropped_chunks"][0]["reason"] == "pii_dense"
    finally:
        pipeline.delete_manual(support.manual_id)
        pipeline.delete_manual(other.manual_id)
