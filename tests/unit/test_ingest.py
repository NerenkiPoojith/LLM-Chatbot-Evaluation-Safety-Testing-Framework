"""Unit tests for loading and chunking the manuals."""

from __future__ import annotations

import pytest

from app.ingest import linearize_markdown_tables, load_manuals, split_documents

pytestmark = pytest.mark.unit


def test_tables_become_self_describing_lines():
    table = "| Code | Meaning |\n|---|---|\n| E21 | Water is not draining |\n| E30 | Door is not locked |"
    assert linearize_markdown_tables(table).splitlines() == [
        "- Code: E21; Meaning: Water is not draining",
        "- Code: E30; Meaning: Door is not locked",
    ]


def test_html_comments_are_not_indexed(settings):
    docs = load_manuals(settings.manuals_dir)
    assert not any("TEST FIXTURE" in d.page_content for d in docs)


def test_every_chunk_carries_its_manual_and_section(settings):
    chunks = split_documents(load_manuals(settings.manuals_dir), settings.chunk_size, settings.chunk_overlap)
    assert len(chunks) > 20
    for chunk in chunks:
        assert chunk.page_content.startswith("[")  # contextual header
        assert chunk.metadata["source"] and chunk.metadata["title"]
        # chunk text + header stays close to the configured size
        assert len(chunk.page_content) <= settings.chunk_size + 150
    error_chunks = [c for c in chunks if "E21" in c.page_content]
    assert error_chunks and all("AquaWave" in c.page_content for c in error_chunks)
