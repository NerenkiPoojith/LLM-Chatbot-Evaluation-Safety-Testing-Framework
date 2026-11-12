"""Unit tests for reading uploaded manuals and the support-contact allowlisting."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from langchain_core.documents import Document
from pypdf import PdfReader, PdfWriter

import app.ingest as ingest
from app.ingest import ManualError, clean_filename, load_manuals, make_manual_id, parse_manual, split_documents
from app.pii import Allowlist
from app.rag import support_contacts
from app.schemas import ChatRequest

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "lumina_lw9_manual.pdf"


def test_pdf_sections_use_the_manuals_own_headings():
    docs = parse_manual(FIXTURE.read_bytes(), "lumina.pdf", manual_id="lumina")
    sections = {d.metadata["section"] for d in docs}
    assert {"Error Codes (page 3)", "Child Lock (page 2)", "Customer Support (page 3)"} <= sections


def test_pdf_table_rows_stay_on_one_line():
    docs = parse_manual(FIXTURE.read_bytes(), "lumina.pdf", manual_id="lumina")
    error_text = next(d.page_content for d in docs if d.metadata["section"].startswith("Error Codes"))
    row = next(line for line in error_text.splitlines() if line.startswith("F07"))
    assert "drain pump is blocked" in row and "pump filter" in row


def test_chunks_carry_the_manual_id_and_a_context_header():
    docs = parse_manual(FIXTURE.read_bytes(), "lumina.pdf", manual_id="lumina")
    chunks = split_documents(docs, 700, 100)
    assert chunks and all(c.metadata["manual_id"] == "lumina" for c in chunks)
    assert all(c.page_content.startswith("[Lumina LW-9") for c in chunks)


@pytest.mark.parametrize("filename", ["../../evil name.PDF", "C:\\temp\\x.pdf", "süße Anleitung.md", ".md"])
def test_manual_ids_are_safe_whatever_the_file_name(filename):
    manual_id = make_manual_id(filename, b"same bytes")
    assert all(ch.isalnum() or ch == "-" for ch in manual_id)
    assert manual_id == make_manual_id(filename, b"same bytes")
    assert manual_id != make_manual_id(filename, b"other bytes")


@pytest.mark.parametrize(
    "sent, stored",
    [
        ("../../notes/kettle.md", "kettle.md"),
        ("C:\\Users\\me\\kettle.pdf", "kettle.pdf"),
        ("trailing.md/", "trailing.md"),
        ("tab\tand\x00nul.md", "tabandnul.md"),
        ("x" * 300 + ".pdf", "x" * 116 + ".pdf"),
        ("", ""),
        ("/", ""),
    ],
)
def test_uploaded_file_names_are_reduced_to_a_short_bare_name(sent, stored):
    assert clean_filename(sent) == stored


# ---------------------------------------------------------------- PDF layouts and titles
def _pdf(lines_per_page: list[list[str] | None], title: str | None = None) -> bytes:
    """A simple text PDF; an empty list makes a page that only draws a shape, None one that only
    shows a picture (as a scanned page does)."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    if title:
        pdf.setTitle(title)
    for lines in lines_per_page:
        if lines is None:
            pdf.drawImage(ImageReader(Image.new("RGB", (64, 64), "grey")), 100, 500, 64, 64)
            lines = []
        elif not lines:
            pdf.rect(100, 500, 300, 200, fill=1)
        for number, line in enumerate(lines):
            if line:
                pdf.drawString(72, 800 - 18 * number, line)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _two_column_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import BaseDocTemplate, Frame, FrameBreak, PageTemplate, Paragraph, Spacer

    styles = getSampleStyleSheet()
    buffer = io.BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, title="Brightline Kettle K7 Manual")
    width, height = A4
    column = (width - 5 * cm) / 2
    doc.addPageTemplates([PageTemplate(frames=[
        Frame(2 * cm, 2 * cm, column, height - 4 * cm), Frame(3 * cm + column, 2 * cm, column, height - 4 * cm),
    ])])
    heading, body = styles["Heading2"], styles["BodyText"]
    doc.build([
        Paragraph("Descaling", heading),
        Paragraph("Descale the kettle every four weeks in hard water areas. Fill it with equal parts of water "
                  "and white vinegar, boil once and leave it to stand for an hour before rinsing.", body),
        Spacer(1, 18),
        Paragraph("Filter", heading),
        Paragraph("The limescale filter sits behind the spout. Pull it upwards to remove it and rinse it under "
                  "the tap once a week.", body),
        FrameBreak(),
        Paragraph("Warranty", heading),
        Paragraph("The kettle has a two-year warranty from the date of purchase. Keep the receipt as proof of "
                  "purchase and register the product online.", body),
        Spacer(1, 18),
        Paragraph("Safety", heading),
        Paragraph("Do not fill the kettle above the MAX mark. Boiling water can spray out of the spout.", body),
    ])
    return buffer.getvalue()


def test_two_column_pages_are_read_one_column_at_a_time():
    docs = parse_manual(_two_column_pdf(), "kettle.pdf", manual_id="kettle")
    sections = {d.metadata["section"]: d.page_content for d in docs}
    assert set(sections) == {"Descaling (page 1)", "Filter (page 1)", "Warranty (page 1)", "Safety (page 1)"}
    assert "four weeks" in sections["Descaling (page 1)"] and "warranty" not in sections["Descaling (page 1)"]
    assert "two-year warranty" in sections["Warranty (page 1)"] and "|" not in sections["Warranty (page 1)"]


def test_generator_default_titles_are_ignored_and_the_cover_line_is_kept():
    data = _pdf([["Acme T-1 Toaster Manual", "", "Cleaning", "", "Empty the crumb tray once a week."]], title="untitled")
    docs = parse_manual(data, "toaster.pdf", manual_id="toaster")
    assert docs[0].metadata["title"] == "Acme T-1 Toaster Manual"
    assert any("Acme T-1 Toaster Manual" in d.page_content for d in docs)


@pytest.mark.parametrize("pdf_title", ["Microsoft Word - toaster.docx", "Untitled-1", "toaster"])
def test_other_generator_titles_fall_back_to_the_cover_line(pdf_title):
    data = _pdf([["Acme T-1 Toaster Manual", "", "Cleaning", "", "Empty the crumb tray once a week."]], title=pdf_title)
    assert parse_manual(data, "toaster.pdf", manual_id="t")[0].metadata["title"] == "Acme T-1 Toaster Manual"


def test_page_count_includes_pages_without_text():
    data = _pdf([["Cleaning", "", "Empty the crumb tray once a week."], []])
    docs = parse_manual(data, "toaster.pdf", manual_id="toaster")
    assert {d.metadata["page_count"] for d in docs} == {2}


def test_a_pdf_with_text_and_more_picture_pages_than_ocr_may_read_is_indexed(monkeypatch):
    asked: list[int] = []

    def fake_ocr(data, numbers, progress=None):
        asked.extend(numbers)
        return {n: "" for n in numbers}

    monkeypatch.setattr(ingest, "ocr_pdf_pages", fake_ocr)
    data = _pdf([["Cleaning", "", "Empty the crumb tray once a week."], None, None, None])
    docs = parse_manual(data, "toaster.pdf", manual_id="toaster", ocr_max_pages=2)
    assert asked == [2, 3]
    assert "crumb tray" in docs[0].page_content
    assert docs[0].metadata["ocr_pages"] == 2 and docs[0].metadata["ocr_skipped_pages"] == 1
    with pytest.raises(ManualError, match="limited to 2 pages"):         # no text layer at all: refused
        parse_manual(_pdf([None, None, None]), "scan.pdf", manual_id="scan", ocr_max_pages=2)


@pytest.mark.parametrize(
    "cover, filename, title",
    [
        (["USE & CARE GUIDE", "French Door Refrigerator", "Model LFXS26973S"], "LFXS26973S.pdf", "LFXS26973S"),
        (["OWNER’S MANUAL", "", "Dishwasher"], "dw80r5060.pdf", "DW80R5060"),
        (["Owner's Manual & Installation Instructions", "", "Dishwasher"], "kitchen_help.pdf", "Kitchen help"),
        (["French Door Refrigerator", "", "Cleaning"], "LFXS26973S.pdf", "LFXS26973S"),   # the model number wins
        (["French Door Refrigerator", "", "Cleaning"], "fridge_manual.pdf", "French Door Refrigerator"),
    ],
)
def test_generic_cover_lines_and_model_number_file_names(cover, filename, title):
    data = _pdf([cover + ["", "Keep the door seal clean and dry."]])
    assert parse_manual(data, filename, manual_id="m")[0].metadata["title"] == title


@pytest.mark.parametrize("line", ["Use and Care Guide", "INSTALLATION MANUAL", "Installation Instructions",
                                  "Quick Reference Guide", "OWNER’S MANUAL", "Users Guide", "1 Safety",
                                  "Owner's Manual & Installation Instructions"])
def test_generic_or_numbered_first_lines_never_name_a_manual(line):
    assert ingest._cover_line(f"{line}\nThe rest of the page.") is None


@pytest.mark.parametrize(
    "filename, name",
    [("wm7000_user_guide.pdf", "WM7000 user guide"), ("lfxs24623s_owners_manual.pdf", "LFXS24623S owners manual"),
     ("dw80-manual.pdf", "DW80 manual"), ("LFXS24623.pdf", "LFXS24623"), ("washer_manual.pdf", "Washer manual"),
     ("lumina_lw-9.pdf", "Lumina LW-9"), ("my-kettle.md", "My kettle")],
)
def test_file_names_become_readable_titles(filename, name):
    assert ingest.pretty_name(filename) == name


@pytest.mark.parametrize("line, heading", [
    ("3.2 Installation", True), ("1 Safety", True), ("Sécurité", True), ("Türverriegelung", True),
    ("Écran de contrôle", True), ("Error Codes", True),
    ("1. Open the tap fully", False), ("2) Close the lid", False), ("drain pump", False), ("12", False),
    ("5 kg", False),
])
def test_pdf_headings_may_be_numbered_or_accented(line, heading):
    assert ingest._looks_like_heading(line) is heading


def test_numbered_pdf_headings_start_sections():
    data = _pdf([["Cleaning", "", "Empty the crumb tray once a week.", "", "3.2 Installation", "",
                  "Place the toaster on a dry, level surface."]])
    sections = {d.metadata["section"]: d.page_content for d in parse_manual(data, "toaster.pdf", manual_id="t")}
    assert "dry, level surface" in sections["3.2 Installation (page 1)"]


@pytest.mark.parametrize(
    "text, lines",
    [
        ("Before cleaning\na) Unplug the refrigerator.\nb) Remove all food.",
         ["Before cleaning", "a) Unplug the refrigerator.", "b) Remove all food."]),
        ("Ice maker\no press the ICE button for 3 seconds\no check that the light goes out",
         ["Ice maker", "o press the ICE button for 3 seconds", "o check that the light goes out"]),
        ("Replace the filter\napproximately every 6 months.", ["Replace the filter approximately every 6 months."]),
    ],
)
def test_list_items_are_not_joined_to_the_line_above(text, lines):
    assert ingest.join_wrapped_lines(text).splitlines() == lines


def test_built_in_manual_ids_fit_the_chat_request_pattern(tmp_path, settings):
    (tmp_path / "Kettle V2 Manual.md").write_text("# Kettle\n\n## Descaling\n\nDescale monthly.", encoding="utf-8")
    (tmp_path / "kettle.v3.md").write_text("# Kettle 3\n\n## Filter\n\nRinse the filter.", encoding="utf-8")
    ids = {d.metadata["manual_id"] for d in load_manuals(tmp_path)}
    assert ids == {"kettle-v2-manual", "kettle-v3"}
    for manual_id in ids:
        ChatRequest(question="q", manual_id=manual_id)
    # The bundled manuals keep the ids they always had.
    assert {d.metadata["manual_id"] for d in load_manuals(settings.manuals_dir)} == {
        p.stem for p in settings.manuals_dir.iterdir() if p.suffix in {".md", ".pdf", ".txt"}
    }


def test_two_files_with_the_same_id_are_refused(tmp_path):
    (tmp_path / "kettle v2.md").write_text("# A\n\nText one.", encoding="utf-8")
    (tmp_path / "Kettle-V2.txt").write_text("Text two.", encoding="utf-8")
    with pytest.raises(ValueError, match="kettle-v2"):
        load_manuals(tmp_path)


# ---------------------------------------------------------------- encrypted PDFs
def _encrypted(user_password: str, algorithm: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(str(FIXTURE)))
    writer.encrypt(user_password=user_password, owner_password="owner-secret", algorithm=algorithm)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_password_protected_pdf_is_rejected():
    with pytest.raises(ManualError, match="password"):
        parse_manual(_encrypted("secret", "RC4-128"), "locked.pdf", manual_id="locked")


def test_aes_pdf_with_a_password_is_rejected_as_password_protected():
    with pytest.raises(ManualError, match="password-protected"):
        parse_manual(_encrypted("secret", "AES-256"), "locked.pdf", manual_id="locked")


def test_aes_pdf_that_opens_without_a_password_is_read():
    # "Restrict editing" PDFs: AES-encrypted, but every viewer opens them without asking.
    docs = parse_manual(_encrypted("", "AES-256"), "restricted.pdf", manual_id="restricted")
    assert any("drain pump is blocked" in d.page_content for d in docs)


def test_missing_crypto_package_is_reported_as_such(monkeypatch):
    from pypdf.errors import DependencyError

    def needs_cryptography(page):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(ingest, "_pdf_page_text", needs_cryptography)
    with pytest.raises(ManualError, match="cryptography") as error:
        parse_manual(_encrypted("", "AES-256"), "restricted.pdf", manual_id="restricted")
    assert "damaged" not in str(error.value)


# ---------------------------------------------------------------- text files and size limits
@pytest.mark.parametrize(
    "data, expected",
    [
        ("Descale every 4 weeks at 60°C – use “white vinegar”.".encode("cp1252"), "60°C – use “white vinegar”"),
        ("# Kettle care\n\nDescale every 4 weeks at 60°C.".encode("utf-16"), "60°C"),
        ("Détartrez la bouilloire à 60°C.".encode("latin-1"), "Détartrez"),
        ("\ufeffDescale at 60°C.".encode("utf-8"), "Descale at 60°C"),
    ],
    ids=["cp1252", "utf-16", "latin-1", "utf-8-bom"],
)
def test_text_manuals_in_other_encodings_are_read_correctly(data, expected):
    text = parse_manual(data, "kettle.txt", manual_id="kettle")[0].page_content
    assert expected in text and "\ufffd" not in text


def test_utf16_title_comes_from_the_heading():
    data = "# Kettle care\n\nDescale every 4 weeks.".encode("utf-16")
    assert parse_manual(data, "utf16.md", manual_id="k")[0].metadata["title"] == "Kettle care"


def test_binary_file_renamed_to_txt_is_rejected():
    with pytest.raises(ManualError, match="does not look like a document"):
        parse_manual(bytes(range(256)) * 40, "photo.txt", manual_id="photo")


def test_text_manual_with_too_much_text_is_rejected():
    with pytest.raises(ManualError, match="too much text") as error:
        parse_manual(b"word " * (ingest.MAX_MANUAL_CHARS // 5 + 1), "huge.txt", manual_id="huge")
    assert error.value.status == 413


def test_pdf_text_is_counted_page_by_page(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_MANUAL_CHARS", 1500)  # Lumina has about 1,000 characters per page
    with pytest.raises(ManualError, match="too much text") as error:
        parse_manual(FIXTURE.read_bytes(), "lumina.pdf", manual_id="lumina")
    assert error.value.status == 413


# ---------------------------------------------------------------- support contacts
def _chunk(section: str, body: str, title: str = "Gadget G1 Manual") -> Document:
    """A chunk shaped like split_documents makes it: a "[title — section]" header, then the text."""
    return Document(page_content=f"[{title} — {section}]\n{body}", metadata={"manual_id": "m", "section": section})


def test_support_contacts_are_taken_only_from_small_support_sections():
    support = _chunk("Customer Support", "Customer support: call 1-800-555-0110 or email help@brand.example.com.")
    no_keyword = _chunk("Installation", "Installer: call 646-555-0101.")
    phones, emails = support_contacts([support, no_keyword], Allowlist())
    assert phones == {"18005550110"}
    assert emails == {"help@brand.example.com"}


def test_a_plain_support_heading_is_enough():
    chunk = _chunk("Support", "For help, phone 0800 444 0123 or email care@crumbly.example.")
    assert support_contacts([chunk], Allowlist()) == ({"08004440123"}, {"care@crumbly.example"})


def test_a_support_word_in_the_manual_title_does_not_count():
    tickets = [
        _chunk(f"Ticket {n}", f"Callback phone 212-555-01{n}, email person{n}@mail.example.",
               title="Customer Service Callbacks")
        for n in range(10, 16)
    ]
    assert support_contacts(tickets, Allowlist()) == (set(), set())


def test_the_contact_limit_counts_the_whole_manual_not_each_chunk():
    tickets = [
        _chunk(f"Ticket {n}", f"Logged by customer service. Callback phone 212-555-01{n}, email person{n}@mail.example.")
        for n in range(10, 16)
    ]
    assert all(body.page_content.count("@") == 1 for body in tickets)  # each chunk alone is under the limit
    assert support_contacts(tickets, Allowlist()) == (set(), set())


def test_contacts_printed_in_built_in_documents_are_never_allowed():
    chunk = _chunk("Customer support", "Call 212-555-0187 or email d.okafor@inbox.example, or care@kelvin.example.")
    never = {"2125550187", "d.okafor@inbox.example"}
    assert support_contacts([chunk], Allowlist(), never) == (set(), {"care@kelvin.example"})
