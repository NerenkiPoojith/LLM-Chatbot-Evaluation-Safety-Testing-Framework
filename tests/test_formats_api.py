"""Manuals in every supported format, uploaded through the real HTTP API and then asked about.

The fixtures (tests/fixtures/formats/, made by make_format_fixtures.py) all hold the same fictional
Lumina LW-9 washer manual, so each upload must answer the same questions from its own text.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.api
FIXTURES = Path(__file__).parent / "fixtures" / "formats"
F07 = ("What does error F07 mean?", "drain pump")
CHILD_LOCK = ("How do I turn on the child lock?", "4 seconds")


def upload(api, name: str, data: bytes | None = None, **form):
    data = (FIXTURES / name).read_bytes() if data is None else data
    return api.session.post(f"{api.base_url}/v1/manuals", files={"file": (name, data)}, data=form or None,
                            timeout=600)


def ask(api, question: str, manual_id: str) -> dict:
    return api.chat_raw({"question": question, "manual_id": manual_id}).json()


def delete(api, manual_id: str) -> None:
    api.session.delete(f"{api.base_url}/v1/manuals/{manual_id}", timeout=30)


@pytest.mark.parametrize(
    "name, label, question",
    [
        ("lumina.docx", "Word document", CHILD_LOCK),
        ("lumina.doc", "Word 97-2003 document", CHILD_LOCK),
        ("lumina.rtf", "Rich Text document", F07),
        ("lumina.odt", "OpenDocument text", CHILD_LOCK),
        ("lumina.pptx", "PowerPoint presentation", F07),
        ("lumina.ppt", "PowerPoint 97-2003 presentation", CHILD_LOCK),
        ("lumina.odp", "OpenDocument presentation", F07),
        ("lumina.xlsx", "Excel workbook", F07),
        ("lumina.xls", "Excel 97-2003 workbook", F07),
        ("lumina.ods", "OpenDocument spreadsheet", F07),
        ("lumina_errors.csv", "CSV table", F07),
        ("lumina.html", "web page", CHILD_LOCK),
        ("lumina.mhtml", "saved web page", F07),
        ("lumina.epub", "EPUB e-book", CHILD_LOCK),
        ("lumina.xps", "XPS document", F07),
        ("lumina.txt", "text file", F07),
    ],
)
def test_each_format_is_uploaded_and_answered(api, name, label, question):
    response = upload(api, name)
    assert response.status_code == 201, response.text
    manual = response.json()
    try:
        assert manual["format"] == label and manual["ocr_pages"] == 0
        body = ask(api, question[0], manual["manual_id"])
        assert question[1] in body["answer"], body["answer"]
        assert {s["manual_id"] for s in body["sources"]} == {manual["manual_id"]}
    finally:
        delete(api, manual["manual_id"])


def test_a_file_with_the_wrong_extension_is_read_by_its_content(api):
    response = upload(api, "manual.pdf", (FIXTURES / "lumina.docx").read_bytes())
    assert response.status_code == 201, response.text
    manual = response.json()
    try:
        assert manual["format"] == "Word document"
        assert "drain pump" in ask(api, F07[0], manual["manual_id"])["answer"]
    finally:
        delete(api, manual["manual_id"])


def test_scanned_pdf_is_read_with_ocr_and_reports_progress(api):
    """An image-only PDF (3 scanned pages): OCR reads it, and GET /v1/manuals/progress follows it."""
    upload_id = uuid.uuid4().hex
    seen: list[dict] = []
    finished = threading.Event()

    def watch():
        while not finished.is_set():
            reply = api.session.get(f"{api.base_url}/v1/manuals/progress/{upload_id}", timeout=10)
            if reply.status_code == 200:
                seen.append(reply.json())
            time.sleep(0.3)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        response = upload(api, "lumina_scanned.pdf", upload_id=upload_id)
    finally:
        finished.set()
        watcher.join(timeout=10)
    assert response.status_code == 201, response.text
    manual = response.json()
    try:
        assert manual["format"] == "PDF" and manual["pages"] == 3 and manual["ocr_pages"] == 3
        assert any(p["stage"] == "ocr" and p["total"] == 3 for p in seen), seen
        assert "drain pump" in ask(api, F07[0], manual["manual_id"])["answer"]
        assert "4 seconds" in ask(api, CHILD_LOCK[0], manual["manual_id"])["answer"]
    finally:
        delete(api, manual["manual_id"])
    # finished uploads leave no progress behind
    assert api.session.get(f"{api.base_url}/v1/manuals/progress/{upload_id}", timeout=10).status_code == 404


def test_phone_photo_of_a_page_is_read_with_ocr(api):
    response = upload(api, "lumina_photo.jpg")
    assert response.status_code == 201, response.text
    manual = response.json()
    try:
        assert manual["format"] == "image" and manual["ocr_pages"] == 1 and manual["ocr_skipped_pages"] == 0
        assert "4 seconds" in ask(api, CHILD_LOCK[0], manual["manual_id"])["answer"]
    finally:
        delete(api, manual["manual_id"])


def test_the_same_photo_sent_again_while_it_is_read_waits_and_is_a_duplicate(api):
    """A reloaded page sends the file again while the first request is still in OCR: the second
    request waits ("waiting"), then gets the first one's manual instead of reading the photo twice."""
    data = (FIXTURES / "lumina_photo.jpg").read_bytes()
    first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
    replies, stages = {}, []

    def send(key: str, name: str, upload_id: str) -> None:
        replies[key] = upload(api, name, data, upload_id=upload_id)

    def progress(upload_id: str) -> str | None:
        reply = api.session.get(f"{api.base_url}/v1/manuals/progress/{upload_id}", timeout=10)
        return reply.json()["stage"] if reply.status_code == 200 else None

    first = threading.Thread(target=send, args=("first", "lumina_photo.jpg", first_id))
    first.start()
    deadline = time.monotonic() + 60
    while progress(first_id) is None and first.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    second = threading.Thread(target=send, args=("second", "photo copy.jpg", second_id))
    second.start()
    while second.is_alive() and time.monotonic() < deadline:
        stages.append(progress(second_id))
        time.sleep(0.1)
    first.join(60)
    second.join(60)
    assert replies["first"].status_code == 201, replies["first"].text
    manual = replies["first"].json()
    try:
        assert replies["second"].status_code == 200
        assert replies["second"].json()["duplicate"] is True
        assert replies["second"].json()["manual_id"] == manual["manual_id"]
        assert "waiting" in stages, stages
    finally:
        delete(api, manual["manual_id"])


@pytest.mark.parametrize(
    "name, data, status, message",
    [
        ("lumina_locked.docx", None, 422, "password-protected"),
        ("archive.zip", b"PK\x05\x06" + b"\x00" * 18, 415, "Unzip it"),
        ("movie.mp4", b"\x00\x00\x00\x18ftypmp42", 415, "Audio and video"),
        ("report.pages", b"not really", 415, "Export it from the Apple app"),
    ],
)
def test_unreadable_formats_are_explained(api, name, data, status, message):
    response = upload(api, name, data)
    assert response.status_code == status, response.text
    assert message in response.json()["detail"]


@pytest.mark.parametrize("bad_id", ["UPPER", "has space", "short", "x" * 65])
def test_progress_ids_are_validated(api, bad_id):
    response = upload(api, "lumina.txt", upload_id=bad_id)
    assert response.status_code == 422


def test_unknown_progress_id_is_404(api):
    assert api.session.get(f"{api.base_url}/v1/manuals/progress/{uuid.uuid4().hex}", timeout=10).status_code == 404
