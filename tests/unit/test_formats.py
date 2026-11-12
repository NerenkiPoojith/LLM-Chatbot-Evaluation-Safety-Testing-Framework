"""Unit tests for app/formats.py: every document format is read into the same text.

Each fixture in tests/fixtures/formats/ is the same fictional Lumina LW-9 washer manual
(make_format_fixtures.py), so one set of facts is checked across all formats.
"""

from __future__ import annotations

import io
import json
import re
import struct
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app import formats
from app.formats import ManualError, convert, detect_format, guess_headings

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "formats"
TITLE = "Lumina LW-9 Top-Load Washing Machine - User Manual"
FULL_MANUAL = ("drain pump", "4 seconds", "12-year", "1-800-555-0110")
ERROR_TABLE_ONLY = ("drain pump",)


def text_of(converted: formats.Converted) -> str:
    return converted.markdown or "\n".join(text for _, text in converted.pages)


def quickly(read, seconds: float = 3.0):
    """Run `read()` and fail if it takes longer than `seconds` (hostile files used to take minutes)."""
    started = time.perf_counter()
    try:
        return read()
    finally:
        assert time.perf_counter() - started < seconds, "took too long"


def zipped(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def rezipped(data: bytes, name: str, change) -> bytes:
    """A copy of an Office file with one part's XML text changed by `change`."""
    source = zipfile.ZipFile(io.BytesIO(data))
    return zipped({n: change(source.read(n).decode("utf-8")) if n == name else source.read(n) for n in source.namelist()})


@pytest.mark.parametrize(
    "name, kind, facts, has_title",
    [
        ("lumina.docx", "docx", FULL_MANUAL, True),
        ("lumina.doc", "doc", FULL_MANUAL, True),
        ("lumina.rtf", "rtf", FULL_MANUAL, False),
        ("lumina.odt", "odt", FULL_MANUAL, True),
        ("lumina.pptx", "pptx", FULL_MANUAL, True),
        ("lumina.ppt", "ppt", FULL_MANUAL, True),
        ("lumina.odp", "odp", FULL_MANUAL, True),
        ("lumina.xlsx", "xlsx", ERROR_TABLE_ONLY, True),
        ("lumina.xls", "xls", ERROR_TABLE_ONLY, False),
        ("lumina.ods", "ods", ERROR_TABLE_ONLY, True),
        ("lumina_errors.csv", "csv", ERROR_TABLE_ONLY, False),
        ("lumina.html", "html", FULL_MANUAL, True),
        ("lumina.mhtml", "mhtml", FULL_MANUAL, True),
        ("lumina.epub", "epub", FULL_MANUAL, True),
        ("lumina.xps", "xps", FULL_MANUAL, False),
        ("lumina.txt", "text", FULL_MANUAL, False),
    ],
)
def test_every_format_yields_the_manuals_facts(name, kind, facts, has_title):
    converted = convert((FIXTURES / name).read_bytes(), name)
    assert converted.kind == kind
    text = text_of(converted)
    for fact in facts:
        assert fact in text, f"{fact!r} missing from {name}"
    if has_title:
        assert converted.title == TITLE


@pytest.mark.parametrize("name", ["lumina.docx", "lumina.odt", "lumina.html", "lumina.txt", "lumina.rtf", "lumina.doc"])
def test_headings_become_sections(name):
    text = convert((FIXTURES / name).read_bytes(), name).markdown
    assert "## Child Lock" in text and "## Error Codes" in text


@pytest.mark.parametrize("name", ["lumina.docx", "lumina.pptx", "lumina.xlsx", "lumina.html", "lumina.epub"])
def test_tables_keep_their_header_row(name):
    text = convert((FIXTURES / name).read_bytes(), name).markdown
    assert "| Code | Meaning | What to do |" in text
    assert "| F07 | The drain pump is blocked |" in text


@pytest.mark.parametrize("name", ["lumina.txt", "lumina.rtf", "lumina.doc"])
def test_rows_without_markup_stay_on_one_line(name):
    text = convert((FIXTURES / name).read_bytes(), name).markdown
    row = next(line for line in text.splitlines() if line.startswith("F07"))
    assert "drain pump" in row and "pump filter" in row


def test_web_page_skips_scripts_and_menus():
    text = convert((FIXTURES / "lumina.html").read_bytes(), "lumina.html").markdown
    assert "tracking" not in text and "Shop" not in text


@pytest.mark.parametrize("name, extension", [("lumina.docx", ""), ("lumina.docx", ".pdf"), ("lumina.png", ".dat"),
                                             ("lumina.rtf", ".doc"), ("lumina.xlsx", ".csv")])
def test_format_comes_from_the_content_not_the_name(name, extension):
    data = (FIXTURES / name).read_bytes()
    assert detect_format(data, f"manual{extension}") == detect_format(data, name)


@pytest.mark.parametrize(
    "filename, data, status, message",
    [
        ("manual.pdf", b"this is plain text, not a PDF", 422, "not a valid PDF"),
        ("manual.docx", b"plain text", 422, "not a valid Word document"),
        ("setup.exe", b"MZ\x90\x00\x03", 415, "Programs and installers"),
        ("song.mp3", b"ID3\x04\x00", 415, "Audio and video"),
        ("report.pages", b"anything", 415, "Export it from the Apple app"),
        ("data.bin", bytes(range(256)) * 4, 415, "does not look like a document"),
        ("empty.txt", b"  \n ", 422, "empty"),
    ],
)
def test_unreadable_files_get_a_specific_reason(filename, data, status, message):
    with pytest.raises(ManualError, match=message) as error:
        convert(data, filename)
    assert error.value.status == status


def test_a_zip_archive_is_not_a_document():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manual/readme.txt", "hello")
    with pytest.raises(ManualError, match="Unzip it") as error:
        convert(buffer.getvalue(), "manuals.zip")
    assert error.value.status == 415


def test_password_protected_office_file_is_explained():
    with pytest.raises(ManualError, match="password-protected"):
        convert((FIXTURES / "lumina_locked.docx").read_bytes(), "lumina_locked.docx")


def test_zip_bombs_are_refused(monkeypatch):
    monkeypatch.setattr(formats, "MAX_ZIP_BYTES", 10_000)
    with pytest.raises(ManualError, match="too much data") as error:
        convert((FIXTURES / "lumina.docx").read_bytes(), "lumina.docx")
    assert error.value.status == 413


def test_macro_enabled_word_files_are_read_without_running_macros():
    source = zipfile.ZipFile(FIXTURES / "lumina.docx")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as docm:
        for info in source.infolist():
            body = source.read(info)
            if info.filename == "[Content_Types].xml":
                body = body.replace(b"wordprocessingml.document.main+xml",
                                    b"wordprocessingml.document.main+xml".replace(b"document.main", b"template.main"))
            docm.writestr(info, body)
    converted = convert(buffer.getvalue(), "lumina.dotx")
    assert converted.kind == "docx" and "drain pump" in converted.markdown


def test_guess_headings_marks_short_lines_above_text():
    text = "Child Lock\nPress and hold Start/Pause and Spin for 4 seconds to lock the controls.\nCotton\nWool\n"
    marked = guess_headings(text)
    assert "## Child Lock" in marked
    assert "## Cotton" not in marked          # a list item followed by another short line


def test_layout_tables_become_paragraphs():
    long = "A long paragraph of manual text that sits in a table cell only for page layout. " * 3
    assert "|" not in formats._table_markdown([[long, long]])


def test_a_caption_merged_across_a_table_is_not_its_header():
    rows = [["LW-9 error codes", None, None], ["Code", "Meaning", "What to do"], ["F07", "Drain pump blocked", "Clean it"]]
    text = formats._table_markdown(rows)
    assert text.startswith("LW-9 error codes\n\n| Code | Meaning | What to do |")
    assert "| F07 | Drain pump blocked | Clean it |" in text


# --------------------------------------------------------------------------- headings in plain text
@pytest.mark.parametrize(
    "text, headings, not_headings",
    [
        # list items without markers (Word 97, Rich Text) are not headings
        ("Place the LW-9 on a level floor.\nConnect the inlet hose to a cold-water tap\n"
         "Keep the drain hose between 80 cm and 110 cm above the floor\nOpen the tap fully\n"
         "Run a Tub Clean cycle with the drum empty\nError Codes\nF07 | Drain pump blocked",
         ["Error Codes"], ["Connect the inlet hose", "Open the tap fully"]),
        # a heading above a numbered list is one; the steps are not
        ("Cleaning the filter\n1. Unplug the appliance\n2. Open the filter cover, lift out the filter and rinse it.",
         ["Cleaning the filter"], ["1. Unplug"]),
        ("Accessories\n- Inlet hose\n- Drain hose bracket", ["Accessories"], ["- Inlet hose"]),
        # headings in other scripts
        ("Übersicht der Programme\nDas Gerät bei 60 °C waschen und danach die Tür einen Spalt offen lassen.",
         ["Übersicht der Programme"], []),
        ("Échange du filtre\nRetirez le filtre en le tournant vers la gauche puis rincez-le sous l'eau.",
         ["Échange du filtre"], []),
        ("安装说明\n请将洗衣机放在平整的地面上，并调节四个支脚。", ["安装说明"], []),
        # a hard-wrapped first sentence still makes the line above it a heading
        ("Cleaning the filter\nRemove the filter by turning it anticlockwise and pull it\nout of the housing.",
         ["Cleaning the filter"], []),
        # hard-wrapped plain text: a wrapped line that starts with a name is not a list item
        ("Warranty\nThe LW-9 has a 3-year warranty on parts and labour, and the motor made by\n"
         "Lumina Appliances has a 12-year warranty.", ["Warranty"], []),
    ],
)
def test_guess_headings_tells_headings_from_list_items(text, headings, not_headings):
    # The first case is a Word 97 / Rich Text list, where every line is a paragraph and bullets are lost.
    paragraph_lines = text.startswith("Place the LW-9")
    marked = guess_headings(text, paragraph_lines=paragraph_lines)
    for heading in headings:
        assert f"## {heading}" in marked
    for line in not_headings:
        assert f"## {line}" not in marked


# --------------------------------------------------------------------------- web pages
@pytest.mark.parametrize("table", [
    "<table><tr><th>Code<th>Meaning<tr><td>F07<td>Drain pump blocked<tr><td>F12<td>Lid lock failed</table>",
    "<table><tr><td>Code</td><td>Meaning</td><tr><td>F07</td><td>Drain pump blocked</td>"
    "<tbody><tr><td>F12</td><td>Lid lock failed</td></table>",
])
def test_web_page_tables_may_leave_out_closing_tags(table):
    text = convert(f"<h2>Error codes</h2>{table}<p>Call service if a code returns.</p>".encode(), "f.html").markdown
    assert "| Code | Meaning |" in text
    assert "| F07 | Drain pump blocked |" in text and "| F12 | Lid lock failed |" in text


def test_the_page_title_comes_from_the_head():
    page = b"<head><meta charset=utf-8><title>LG LFXS26973S Refrigerator Owner's Manual</title></head><body><p>Hi</p>"
    assert convert(page, "fridge.html").title == "LG LFXS26973S Refrigerator Owner's Manual"


def test_a_page_without_closing_head_or_body_tags_is_read():
    page = (b"<!doctype html><html lang=en><head><meta charset=utf-8><title>Washer help</title><h1>Washer</h1>"
            b"<h2>Cleaning</h2><p>Rinse the filter every month.</p>")
    converted = convert(page, "washer.html")
    assert "## Cleaning" in converted.markdown and "Rinse the filter" in converted.markdown


def test_headings_inside_a_layout_table_still_make_sections():
    page = ("<table><tr><td><a>Home</a></td><td><h1>Cleaning the filter</h1><p>Open the flap at the lower "
            "right.</p><h1>Descaling</h1><p>Run the descaling program every month.</p></td></tr></table>")
    text = convert(page.encode(), "help.html").markdown
    assert "## Cleaning the filter\n\nOpen the flap" in text and "## Descaling\n\nRun the descaling" in text


def test_paragraphs_in_table_cells_keep_a_data_table():
    page = ("<table><tr><td><p class=MsoNormal>Code</p></td><td><p class=MsoNormal>Meaning</p></td></tr>"
            "<tr><td><p class=MsoNormal>F07</p></td><td><p class=MsoNormal>Drain pump blocked</p></td></tr></table>")
    assert "| F07 | Drain pump blocked |" in convert(page.encode(), "word.htm").markdown


def test_thousands_of_unclosed_skipped_tags_are_read_quickly():
    page = ("<p>Rinse the filter every month.</p>" + "<svg>" * 40_000 + "</x>" * 40_000).encode()
    assert "Rinse the filter" in quickly(lambda: convert(page, "skip.html")).markdown


def test_a_page_cannot_choose_a_slow_python_codec():
    letters = "a" * 200_000
    page = f'<html><head><meta charset="punycode"></head><body><p>{letters}</p>'.encode()
    assert letters in quickly(lambda: convert(page, "puny.html")).markdown
    saved = (b"From: <Saved by Blink>\nMIME-Version: 1.0\nContent-Type: multipart/related; boundary=\"B\"\n\n--B\n"
             b"Content-Type: text/html; charset=punycode\n\n<p>" + letters.encode() + b"</p>\n--B--\n")
    assert letters in quickly(lambda: convert(saved, "puny.mhtml")).markdown


def _epub(spine: str, chapter: str) -> bytes:
    return zipped({
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                                  '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        "OEBPS/book.opf": '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                          '<item id="c1" href="ch1.xhtml"/><item id="again" href="ch1.xhtml"/></manifest>'
                          f"<spine>{spine}</spine></package>",
        "OEBPS/ch1.xhtml": chapter,
    })


def test_a_chapter_listed_many_times_is_read_once():
    data = _epub('<itemref idref="c1"/><itemref idref="again"/>' * 5_000,
                 "<html><body><h1>Cleaning</h1><p>Rinse the filter every month.</p></body></html>")
    assert quickly(lambda: convert(data, "book.epub")).markdown.count("Rinse the filter") == 1


def test_text_is_counted_while_it_is_read(monkeypatch):
    monkeypatch.setattr(formats, "MAX_MANUAL_CHARS", 5_000)
    data = _epub('<itemref idref="c1"/>', "<html><body>" + "<p>Rinse the filter every month.</p>" * 500 + "</body></html>")
    with pytest.raises(ManualError, match="too much text") as error:
        convert(data, "book.epub")
    assert error.value.status == 413


# --------------------------------------------------------------------------- Word
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC_NS = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def _docx(body: str = "", *, styles: str = "", build=None) -> bytes:
    """A Word file: python-docx's default document plus raw WordprocessingML for the body and styles."""
    import docx
    from docx.oxml import parse_xml

    document = docx.Document()
    if build:
        build(document)
    for element in parse_xml(f"<w:body {W_NS} {MC_NS}>{body}</w:body>"):
        document.element.body.insert(len(document.element.body) - 1, element)   # before w:sectPr
    for element in parse_xml(f"<w:styles {W_NS}>{styles}</w:styles>"):
        document.styles.element.append(element)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def test_word_text_outside_plain_runs_is_read():
    box = f"<w:txbxContent><w:p>{_run('WARNING: Never use petrol or solvents.')}</w:p></w:txbxContent>"
    body = (
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{_run("Warranty")}</w:p>'
        f'<w:p>{_run("The motor has a ")}<w:ins w:id="1" w:author="a">{_run("12-year")}</w:ins>'
        f'<w:del w:id="2" w:author="a"><w:r><w:delText>5-year</w:delText></w:r></w:del>{_run(" warranty.")}</w:p>'
        f"<w:sdt><w:sdtPr><w:alias w:val='Serial'/></w:sdtPr><w:sdtContent>"
        f"<w:p>{_run('The serial number is on a label inside the lid.')}</w:p></w:sdtContent></w:sdt>"
        f'<w:p><w:fldSimple w:instr="HYPERLINK x">{_run("Call")}</w:fldSimple>'
        f'<w:smartTag w:uri="u" w:element="phone">{_run(" 1-800-555-0110")}</w:smartTag>'
        f"<w:r><w:t>, model LW</w:t><w:noBreakHyphen/><w:t>9.</w:t></w:r></w:p>"
        f"<w:p><w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:drawing>{box}</w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict>{box}</w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )
    text = convert(_docx(body), "word.docx").markdown
    assert "The motor has a 12-year warranty." in text and "5-year" not in text
    assert "The serial number is on a label inside the lid." in text
    assert "Call 1-800-555-0110, model LW-9." in text
    assert text.count("WARNING: Never use petrol or solvents.") == 1        # the drawing, not its fallback too


def test_word_table_cells_are_read_once_however_wide_they_are():
    def build(document):
        table = document.add_table(rows=3, cols=3)
        for row, values in zip(table.rows, [("Code", "Meaning", "What to do"), ("F07", "Drain pump blocked", "Call"),
                                            ("F12", "Lid lock failed", "")]):
            for cell, value in zip(row.cells, values):
                cell.text = value
        table.cell(1, 2).merge(table.cell(2, 2))                          # "Call" for both rows
        table.rows[0].cells[0]._tc.grid_span = 2_000_000_000

    text = quickly(lambda: convert(_docx(build=build), "table.docx")).markdown
    assert "Code | Meaning | What to do" in text
    assert "| F12 | Lid lock failed | Call |" in text


def test_word_files_with_thousands_of_styles_are_read_quickly():
    styles = "".join(f'<w:style w:type="paragraph" w:styleId="S{i}"><w:name w:val="Style {i}"/>'
                     f'<w:basedOn w:val="{"Heading1" if i == 7 else "Normal"}"/></w:style>' for i in range(2_000))
    body = "".join(f'<w:p><w:pPr><w:pStyle w:val="S{i}"/></w:pPr>{_run(f"Paragraph {i} of the manual.")}</w:p>'
                   for i in range(2_000))
    text = quickly(lambda: convert(_docx(body, styles=styles), "styles.docx"), 5.0).markdown
    assert "## Paragraph 7 of the manual." in text          # a style based on Heading 1 is still a heading


def test_word_body_text_is_counted_while_it_is_read(monkeypatch):
    data = _docx("".join(f"<w:p>{_run('Rinse the filter under running water every month.')}</w:p>" for _ in range(400)))
    monkeypatch.setattr(formats, "MAX_MANUAL_CHARS", 5_000)
    with pytest.raises(ManualError, match="too much text") as error:
        convert(data, "long.docx")
    assert error.value.status == 413
    monkeypatch.setattr(formats, "MAX_XML_PART_BYTES", 10_000)
    with pytest.raises(ManualError, match="too much data") as error:
        convert(data, "long.docx")
    assert error.value.status == 413


class FakeOle:
    """Stands in for olefile.OleFileIO, so a legacy Office stream can be crafted in a test."""

    def __init__(self, streams: dict[str, bytes]) -> None:
        self.streams = streams

    def __call__(self, _file):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def openstream(self, name: str):
        return io.BytesIO(self.streams[name])

    def get_metadata(self):
        return type("Meta", (), {"title": None})()


def _word97(text: bytes, pieces: int, ccp_text: int) -> dict[str, bytes]:
    """WordDocument and 1Table streams whose piece table lists `pieces` pieces, all over the same text."""
    fib = bytearray(2048)
    struct.pack_into("<HHH", fib, 0, 0xA5EC, 0, 0)
    struct.pack_into("<H", fib, 0x0A, 0x0200)               # the table stream is 1Table
    struct.pack_into("<H", fib, 32, 14)                     # FibRgW97: 14 words
    struct.pack_into("<H", fib, 62, 22)                     # FibRgLw97: 22 longs
    struct.pack_into("<I", fib, 76, ccp_text)
    struct.pack_into("<H", fib, 152, 93)
    plc = struct.pack(f"<{pieces + 1}I", *(k * len(text) for k in range(pieces + 1)))
    plc += struct.pack("<HIH", 0, 0x40000000 | 2 * len(fib), 0) * pieces   # compressed, at offset 2048
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", fib, 154 + 33 * 8, 0, len(clx))
    return {"WordDocument": bytes(fib) + text, "1Table": clx}


def test_word97_pieces_that_repeat_the_text_are_read_only_up_to_its_length(monkeypatch):
    import olefile

    text = b"Rinse the filter every month. " * 20_000                 # 600 KB, listed by 3,000 pieces
    monkeypatch.setattr(olefile, "OleFileIO", FakeOle(_word97(text, 3_000, 60)))
    converted = quickly(lambda: formats._read_doc(b"", "x.doc", formats._Budget()))
    assert converted.markdown.strip() == "Rinse the filter every month. Rinse the filter every month."
    monkeypatch.setattr(formats, "MAX_MANUAL_CHARS", 50)
    with pytest.raises(ManualError, match="too much text"):
        formats._read_doc(b"", "x.doc", formats._Budget())


# --------------------------------------------------------------------------- PowerPoint
def _pptx(build) -> bytes:
    from pptx import Presentation

    deck = Presentation()
    build(deck)
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


def test_slide_line_breaks_separate_words():
    from pptx.util import Inches

    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Cleaning"
        slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "WARNING\vDo not use solvents"

    assert "WARNING Do not use solvents" in convert(_pptx(build), "deck.pptx").markdown


def test_a_slide_listed_many_times_is_read_once():
    def build(deck):
        deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Cleaning the filter"

    def repeat(xml: str) -> str:
        entry = re.search(r"<p:sldId [^>]*/>", xml).group(0)
        return xml.replace(entry, entry * 5_000)

    converted = quickly(lambda: convert(rezipped(_pptx(build), "ppt/presentation.xml", repeat), "deck.pptx"))
    assert converted.page_count == 1 and converted.markdown.count("Cleaning the filter") == 1


def _record(rec_type: int, body: bytes = b"", instance: int = 0, container: bool = False) -> bytes:
    return struct.pack("<HHI", instance << 4 | (0xF if container else 0), rec_type, len(body)) + body


def _chars(text: str) -> bytes:
    return _record(0x0FA0, text.encode("utf-16-le"))


def test_powerpoint97_speaker_notes_stay_with_their_slide(monkeypatch):
    import olefile

    def persist(persist_id: int, slide_id: int) -> bytes:
        return _record(0x03F3, struct.pack("<IIIII", persist_id, 0, 0, slide_id, 0))

    def notes(slide_id: int, text: str) -> bytes:
        return _record(0x03F0, _record(0x03F1, struct.pack("<IHH", slide_id, 0, 0)) + _chars(text), container=True)

    slide_list = _record(0x0FF0, persist(2, 256) + persist(3, 257), container=True)
    document = _record(0x03E8, slide_list, container=True)
    # The second slide's drawing comes first in the stream, and the notes come after both, in reverse order.
    second = _record(0x03EE, _chars("Cleaning the filter") + _chars("Open the flap."), container=True)
    first = _record(0x03EE, _chars("Lumina LW-9") + _chars("Quick guide."), container=True)
    body = document + second + first + notes(257, "The filter is behind the flap.") + notes(256, "Mention the warranty.")
    offsets = [len(document), len(document + second)]                    # persist ids 2 and 3
    stream = body + _record(0x1772, struct.pack("<III", 2 | 2 << 20, offsets[1], offsets[0]))
    monkeypatch.setattr(olefile, "OleFileIO", FakeOle({"PowerPoint Document": stream}))
    text = formats._read_ppt(b"", "deck.ppt", formats._Budget()).markdown
    assert text.index("## Slide 1: Lumina LW-9") < text.index("Speaker notes: Mention the warranty.") \
        < text.index("## Slide 2: Cleaning the filter") < text.index("Speaker notes: The filter is behind the flap.")


# --------------------------------------------------------------------------- spreadsheets
def _xlsx(build) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    build(workbook)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_a_wrong_sheet_dimension_does_not_cut_the_sheet_short():
    def build(workbook):
        for row in [("Code", "Meaning", "What to do"), ("F07", "Drain pump blocked", "Clean the pump filter")]:
            workbook.active.append(row)

    data = rezipped(_xlsx(build), "xl/worksheets/sheet1.xml", lambda xml: re.sub(r'<dimension ref="[^"]*"/>',
                                                                                  '<dimension ref="A1"/>', xml))
    assert "| F07 | Drain pump blocked | Clean the pump filter |" in convert(data, "codes.xlsx").markdown


def test_a_caption_row_over_a_sheet_is_kept_apart():
    def build(workbook):
        sheet = workbook.active
        sheet.append(["LW-9 error codes"])
        sheet.merge_cells("A1:C1")
        sheet.append(["Code", "Meaning", "What to do"])
        sheet.append(["F07", "Drain pump blocked", "Clean the pump filter"])

    text = convert(_xlsx(build), "codes.xlsx").markdown
    assert "LW-9 error codes\n\n| Code | Meaning | What to do |" in text


def test_spreadsheet_cells_are_counted_for_the_whole_workbook(monkeypatch):
    def build(workbook):
        for n in range(4):
            sheet = workbook.active if n == 0 else workbook.create_sheet(f"S{n}")
            sheet["XFD1"] = "far right"                      # a 16,384-cell row

    data = _xlsx(build)
    monkeypatch.setattr(formats, "MAX_TABLE_CELLS", 40_000)
    with pytest.raises(ManualError, match="too large") as error:
        quickly(lambda: convert(data, "wide.xlsx"))
    assert error.value.status == 413


def test_a_sheet_listed_under_many_names_is_read_once():
    def build(workbook):
        workbook.active.append(["F07", "Drain pump blocked"])

    def repeat(xml: str) -> str:
        sheet = re.search(r"<sheet [^>]*/>", xml).group(0)
        return xml.replace(sheet, sheet + "".join(sheet.replace('name="Sheet" sheetId="1"', f'name="S{n}" sheetId="{n}"')
                                                  for n in range(2, 2_000)))

    data = rezipped(_xlsx(build), "xl/workbook.xml", repeat)
    assert quickly(lambda: convert(data, "codes.xlsx")).markdown.count("Drain pump blocked") == 1


def test_xls_date_cells_are_dates():
    import xlrd

    class Sheet:
        def row_values(self, row):
            return [45413.0, "Drain pump replaced", 0.5]

        def row_types(self, row):
            return [xlrd.XL_CELL_DATE, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_DATE]

    date, _, time_of_day = formats._xls_row(Sheet(), 0, 0)
    assert formats._cell(date) == "2024-05-01" and formats._cell(time_of_day) == "12:00:00"


def _biff_sheets(count: int) -> bytes:
    """A raw BIFF8 workbook stream (xlrd reads it without the OLE wrapper): every sheet holds one
    number in its last cell, row 65,536, column 256."""
    def record(rec_type: int, body: bytes = b"") -> bytes:
        return struct.pack("<HH", rec_type, len(body)) + body

    globals_bof = record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6))
    sheet = (record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 6))
             + record(0x0200, struct.pack("<IIHHH", 0, 65536, 0, 256, 0))
             + record(0x0203, struct.pack("<HHHd", 65535, 255, 0, 21.0)) + record(0x000A))
    names = [f"S{n}" for n in range(count)]
    head_size = len(globals_bof) + 6 + sum(4 + 8 + len(n) for n in names) + 4
    out = globals_bof + record(0x0042, struct.pack("<H", 1200))
    for n, name in enumerate(names):
        out += record(0x0085, struct.pack("<IHBB", head_size + n * len(sheet), 0, len(name), 0) + name.encode())
    return out + record(0x000A) + sheet * count


def test_xls_sheets_with_one_far_cell_are_read_without_their_empty_grid(monkeypatch):
    assert "21" in formats._read_xls(_biff_sheets(1), "far.xls", formats._Budget()).markdown
    # every sheet has 65,536 rows: the empty ones count as cells looked at, across the whole workbook
    monkeypatch.setattr(formats, "MAX_SCANNED_CELLS", 200_000)
    with pytest.raises(ManualError, match="too large") as error:
        quickly(lambda: formats._read_xls(_biff_sheets(4), "far.xls", formats._Budget()))
    assert error.value.status == 413


def test_repeated_opendocument_rows_count_as_often_as_they_repeat():
    row = ('<table:table-row table:number-rows-repeated="100"><table:table-cell table:number-columns-repeated="50">'
           "<text:p>F07</text:p></table:table-cell></table:table-row>")
    content = ('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
               f'<table:table table:name="Codes">{row * 300}</table:table></office:spreadsheet></office:body>'
               "</office:document-content>")
    data = zipped({"mimetype": "application/vnd.oasis.opendocument.spreadsheet", "content.xml": content})
    with pytest.raises(ManualError) as error:
        quickly(lambda: convert(data, "codes.ods"))
    assert error.value.status == 413


# --------------------------------------------------------------------------- XPS
def _glyphs(x: float, y: float, text: str, indices: str = "", bidi: int = 0) -> str:
    return (f'<Glyphs OriginX="{x}" OriginY="{y}" FontRenderingEmSize="12" BidiLevel="{bidi}" '
            f'UnicodeString="{text}" Indices="{indices}"/>')


def test_xps_runs_are_joined_as_word_lays_them_out():
    hebrew = [(268.99, "הסחיטה", "676,59.8;689,57.5;679,60.2;681,24.7;680,58.8;676"), (292.75, "בזמן", "673,54;678,39.1;686,60.4;687"),
              (329.35, "המכסה", "676,61.1;686,61;683,45.7;689,57.2;676"), (346.19, "את", "672,56;698"),
              (378.59, "לפתוח", "684,46.7;692,54.3;698,64.6;677,24.8;679"), (393.71, "אין", "672,56.6;681,25;687")]
    runs = [
        _glyphs(72.024, 46.68, "Lumina LW", ",50;,56;,85;,24;,55;,53;,20;,46;"), _glyphs(129.26, 46.68, "-"),
        _glyphs(133.34, 46.68, "9 user manual ", ",50;" * 13),
        *(_glyphs(x, 100, word, indices, bidi=1) for x, word, indices in hebrew),
        *(_glyphs(x, 100, " ") for x in (268.85, 292.61, 329.23, 346.03, 378.43)), _glyphs(393.67, 100, "."),
        _glyphs(72.024, 150, "F07", ",52;,53;"), _glyphs(91.1, 150, " "),
        _glyphs(222.41, 150, "The drain pump is blocked", ",45;" * 24), _glyphs(357.67, 150, " "),
        _glyphs(372.91, 150, "Clean the pump filter ", ",45;" * 21),
    ]
    page = f'<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06">{"".join(runs)}</FixedPage>'
    text = text_of(convert(zipped({"Documents/1/Pages/1.fpage": page}), "word.xps"))
    assert "Lumina LW-9 user manual" in text
    assert "אין לפתוח את המכסה בזמן הסחיטה." in text
    assert "F07 | The drain pump is blocked | Clean the pump filter" in text


# --------------------------------------------------------------------------- OCR (a few seconds each)
@pytest.mark.parametrize(
    "name, facts, pages",
    [
        ("lumina.png", ("drain pump", "12-year", "1-800-555-0110"), 1),
        ("lumina_photo.jpg", ("4 seconds", "Child Lock"), 1),          # a sideways phone photo (EXIF rotation)
        ("lumina_scan.tiff", FULL_MANUAL, 3),                          # a 3-page black-and-white scan
    ],
)
def test_photos_and_scans_are_read_with_ocr(name, facts, pages):
    converted = convert((FIXTURES / name).read_bytes(), name)
    assert converted.kind == "image" and converted.ocr_pages == pages
    text = text_of(converted)
    for fact in facts:
        assert fact in text, f"{fact!r} missing from OCR text of {name}"


def test_ocr_keeps_table_rows_together():
    text = text_of(convert((FIXTURES / "lumina.png").read_bytes(), "lumina.png"))
    row = next(line for line in text.splitlines() if line.startswith("F07"))
    assert row.count(" | ") == 2 and "drain pump" in row


def test_too_many_scanned_pages_are_refused():
    with pytest.raises(ManualError, match="limited to 2 pages"):
        convert((FIXTURES / "lumina_scan.tiff").read_bytes(), "scan.tiff", ocr_max_pages=2)


def test_a_blank_photo_has_no_text():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buffer, "PNG")
    with pytest.raises(ManualError, match="No text could be found in this image"):
        convert(buffer.getvalue(), "blank.png")


def test_a_phone_photos_second_picture_is_not_a_page(monkeypatch):
    from PIL import Image

    pictures = []
    monkeypatch.setattr(formats, "ocr_image", lambda image: pictures.append(image.size) or "Error codes")
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buffer, "MPO", save_all=True,
                                               append_images=[Image.new("RGB", (400, 300), "grey")])
    converted = convert(buffer.getvalue(), "IMG_2041.jpg")
    assert converted.page_count == converted.ocr_pages == 1 and pictures == [(800, 600)]


def test_a_queued_ocr_job_reports_that_it_is_waiting():
    stages, held, release = [], threading.Event(), threading.Event()

    def other_manual():
        with formats._OCR_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=other_manual)
    holder.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    with formats._ocr_turn(lambda stage, done, total: stages.append(stage)):
        pass
    holder.join()
    assert stages == ["waiting"]


# --------------------------------------------------------------------------- page layout of OCR text
def _lines(x0: int, x1: int, tops: list[int], text: str = "wrapped text of the column") -> list[tuple]:
    """OCR boxes (left, right, top, bottom, text) of 20-pixel lines."""
    return [(x0, x1, top, top + 20, text) for top in tops]


def test_a_full_width_footer_does_not_stop_two_columns_being_read_in_turn():
    prose = "wrapped text of the column, ending a sentence."     # paragraphs of prose end with a full stop
    left = _lines(50, 480, [100, 125, 150, 200, 225, 250], prose)
    right = _lines(520, 950, [100, 125, 175, 200, 250, 275], prose)
    footer = _lines(50, 950, [400], "Lumina LW-9 user manual - customer support 1-800-555-0110")
    title = _lines(50, 950, [40], "Care and maintenance")
    columns = formats._columns(title + left + right + footer, 1000, 20)
    assert columns == [title, left, right, footer]


def test_a_two_column_table_with_wrapped_cells_keeps_its_rows():
    rows = [100, 175, 250, 325]                       # each cell wraps over two lines; a gap between rows
    left = [box for top in rows for box in _lines(50, 480, [top, top + 25])]
    right = [box for top in rows for box in _lines(520, 950, [top, top + 25])]
    assert formats._columns(left + right, 1000, 20) == [left + right]


# --------------------------------------------------------------------------- regressions found by the second check
def test_a_key_value_table_under_a_title_keeps_its_rows():
    rows = [["Technical data", None], ["Width", "60 cm"], ["Height", "85 cm"], ["Capacity", "9 kg"]]
    text = formats._table_markdown(rows)
    assert text.startswith("Technical data\n\n")
    assert "Width | 60 cm" in text and "Height | 85 cm" in text and "Width: Height" not in text


def test_formatted_blank_cells_do_not_use_up_a_workbooks_budget():
    budget = formats._Budget()
    wide_but_empty = [("Code", "Meaning") + (None,) * 998] * 600      # 600,000 cells, only 1,200 with values
    for name in ("Sheet1", "Sheet2"):
        formats._sheet_blocks(name, iter(wide_but_empty), budget)
    assert budget._cells == 2 * 600 * 2


def test_a_small_table_inside_a_data_table_cell_stays_in_that_cell():
    page = ("<table><tr><th>Code</th><th>Meaning</th></tr>"
            "<tr><td>F07</td><td>Drain pump blocked <table><tr><td>see page 12</td></tr></table></td></tr>"
            "<tr><td>F12</td><td>Lid lock failed</td></tr></table>")
    text = convert(page.encode(), "codes.html").markdown
    assert "| Code | Meaning |" in text and "| F12 | Lid lock failed |" in text
    assert "see page 12" in text


def test_a_charset_whose_decoder_always_fails_falls_back():
    page = b'<html><head><meta charset="undefined"></head><body><h1>Error codes</h1><p>F07 means the drain pump is blocked.</p></body></html>'
    assert "drain pump" in convert(page, "page.html").markdown


@pytest.mark.parametrize("name, expected", [("IMG_2041.jpg", False), ("scan0001.pdf", False),
                                            ("LFXS26973S.pdf", True), ("DSC01234.jpg", False)])
def test_camera_and_scanner_names_are_not_model_numbers(name, expected):
    from app.ingest import is_model_number_name

    assert is_model_number_name(name) is expected


def test_a_cover_line_that_names_the_model_keeps_its_words():
    from app.ingest import _fallback_title

    assert _fallback_title("LFXS26973S.pdf", "LFXS26973S French Door Refrigerator") == "LFXS26973S French Door Refrigerator"
    assert _fallback_title("LFXS26973S.pdf", "French Door Refrigerator") == "LFXS26973S"


def test_numbered_steps_and_values_do_not_start_sections():
    from app.ingest import _pdf_sections

    page = ("1 Safety\n\nRead every warning before use.\n\n2 Installation\n\nPlace the washer on a level floor.\n\n"
            "Steps\n\n1 Open the door\n\n2 Remove the filter\n\n3 Rinse it under the tap\n\nWarranty\n\n12 Months")
    labels = [label for label, _ in _pdf_sections(page, 4)]
    assert "1 Safety (page 4)" in labels and "2 Installation (page 4)" in labels
    assert not any(label.startswith(("1 Open", "2 Remove", "3 Rinse", "12 Months")) for label in labels)


def test_spanish_o_at_a_line_start_is_a_word_not_a_bullet():
    from app.ingest import join_wrapped_lines

    assert join_wrapped_lines("Limpie el filtro con agua tibia\no con un cepillo suave.") == \
        "Limpie el filtro con agua tibia o con un cepillo suave."
    assert join_wrapped_lines("Accesorios\no Manguera\no Soporte") == "Accesorios\no Manguera\no Soporte"


def test_xps_runs_with_estimated_widths_keep_their_spaces():
    page = ('<FixedPage xmlns="http://schemas.microsoft.com/xps/2005/06" Width="816" Height="1056">'
            + _glyphs(96, 120, "Drain") + _glyphs(126, 120, "pump") + _glyphs(150, 120, "LW") + _glyphs(162, 120, "-")
            + _glyphs(166, 120, "9") + "</FixedPage>")
    data = zipped({"Documents/1/Pages/1.fpage": page})
    text = convert(data, "page.xps").pages[0][1]
    assert "Drain pump" in text and "LW-9" in text


# --------------------------------------------------------------------------- OCR columns, pinned without OCR
COLUMN_PAGES = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "ocr_column_pages.json")
                          .read_text(encoding="utf-8"))


@pytest.mark.parametrize("page", COLUMN_PAGES, ids=[p["page"] for p in COLUMN_PAGES])
def test_two_column_pages_are_read_in_turn_and_tables_keep_their_rows(page):
    """The text boxes OCR found on 29 test pages (made during the reviews): two-column prose with titles,
    footers and equal-length paragraphs must be split; "Problem | What to do" tables must not."""
    boxes = [tuple(b) for b in page["boxes"]]
    groups = formats._columns(boxes, page["width"], page["line_height"])
    assert (len(groups) > 1) == (page["expected"] == "columns")


def test_chapter_numbers_are_told_from_values_and_steps():
    from app.ingest import _pdf_sections

    page = ("1 Mains connection\n\nUse a grounded socket.\n\n2 Installation\n\n1. Unpack the washer.\n\n"
            "3 Programs\n\nChoose a program.\n\nRating\n\n230 Volt\n\nPower\n\n100 Watts")
    labels = [label for label, _ in _pdf_sections(page, 1)]
    assert {"1 Mains connection (page 1)", "2 Installation (page 1)", "3 Programs (page 1)"} <= set(labels)
    assert not any(label.startswith(("230 Volt", "100 Watts")) for label in labels)


def test_a_wrapped_first_o_bullet_is_not_joined_to_its_heading():
    from app.ingest import join_wrapped_lines

    assert join_wrapped_lines("Accessories\no Inlet hose with a seal\nand a clamp") == \
        "Accessories\no Inlet hose with a seal and a clamp"


def test_a_real_table_nested_in_a_layout_cell_stays_a_table():
    page = ("<table><tr><td colspan=2>Lumina service pages</td></tr><tr><td>Menu</td><td>"
            "<table><tr><th>Code</th><th>Meaning</th></tr><tr><td>F07</td><td>Drain pump blocked</td></tr>"
            "<tr><td>F12</td><td>Lid lock failed</td></tr></table></td></tr></table>")
    text = convert(page.encode(), "service.html").markdown
    assert "| Code | Meaning |" in text and "| F12 | Lid lock failed |" in text


def test_a_captioned_two_column_code_table_keeps_its_header():
    rows = [["LW-9 error codes", None], ["Code", "Meaning"], ["F07", "Drain pump blocked"], ["F12", "Lid lock failed"]]
    text = formats._table_markdown(rows)
    assert text.startswith("LW-9 error codes\n\n| Code | Meaning |") and "| F07 | Drain pump blocked |" in text


@pytest.mark.parametrize("name", ["DSCF1234.jpg", "CIMG0042.jpg", "PICT0001.jpg", "IMGP1234.jpg", "GOPR0001.jpg",
                                  "HPIM0001.jpg", "Book1.pdf"])
def test_more_camera_and_office_default_names_are_not_model_numbers(name):
    from app.ingest import is_model_number_name

    assert not is_model_number_name(name)
