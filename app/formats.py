"""Read a manual in whatever form it came in, so it can be indexed.

A manual may arrive as a PDF, a Word file, a slide deck, a spreadsheet, a saved web page,
an e-book, a scan or a phone photo. Every reader here turns its format into the same thing:
Markdown-style text, with headings as "##" lines and tables as "| a | b |" rows. One splitter
and one table rule (app/ingest.py) then handle every format alike.

The format is recognised from the file's content, not only its name, so a manual with a
wrong or missing extension still works. Scans and photos are read with offline OCR
(RapidOCR, ONNX models run on this computer; nothing is sent anywhere). PDFs keep their own
reader in app/ingest.py; this module adds OCR for their scanned pages.
"""

from __future__ import annotations

import codecs
import csv
import email
import email.policy
import hashlib
import io
import logging
import os
import posixpath
import re
import struct
import threading
import unicodedata
import urllib.request
import warnings
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# progress(stage, done, total): lets the API report "reading page 12 of 45" during a long OCR run
Progress = Callable[[str, int, int], None] | None

OCR_MAX_PAGES = 60                       # about 6 s per page on a laptop CPU
OCR_LINE_PX = 32                         # text-line height the recogniser reads best (see ocr_image)
OCR_MAX_SIDE = 4200                      # largest image side passed to OCR
# The recogniser bundled with RapidOCR is trained mostly on Chinese and drops the spaces between
# English words ("Donotputhandsintotheairvents"). RapidOCR's English model keeps them. It is fetched
# once, like the embedding model, checked against this hash, and kept in the model cache folder.
OCR_EN_MODEL_URL = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"
OCR_EN_MODEL_SHA256 = "ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea"
OCR_EN_MODEL_NAME = "en_PP-OCRv3_rec_infer.onnx"
MAX_ZIP_ENTRIES = 5_000                  # .docx, .pptx, .xlsx, .odt and .epub are ZIP files
MAX_ZIP_BYTES = 300 * 1024 * 1024        # total unpacked size: stops "zip bombs"
# One XML part (a Word file's body, a sheet, a chapter) is parsed whole, into several times its size
# in memory, before any text is counted: a 1 MB upload must not unpack into a 300 MB part.
MAX_XML_PART_BYTES = 50 * 1024 * 1024
MAX_TABLE_CELLS = 1_000_000              # table cells with content per document (a row counts up to its last value)
MAX_SCANNED_CELLS = 2_000_000            # spreadsheet cells looked at per document, formatted blanks included
                                         # (a styled 50-sheet workbook needs about 1.1 M)
MAX_IMAGE_PIXELS = 60_000_000            # about 7700 x 7700: bigger images are refused
# A realistic 400-page manual has about 2.5 M characters. The cap stops a tiny, hostile file
# (one huge content stream shared by every page) from expanding into hours of embedding.
MAX_MANUAL_CHARS = 4_000_000
TOO_MUCH_TEXT = "This manual has too much text to index."

SUPPORTED_SUMMARY = (
    "PDF (including scans), Word (.docx, .doc), Rich Text, OpenDocument, PowerPoint, Excel, CSV, "
    "web pages, EPUB e-books, XPS, Markdown, plain text, and photos or scans (JPEG, PNG, TIFF, HEIC and more)"
)

LABELS = {
    "pdf": "PDF", "docx": "Word document", "doc": "Word 97-2003 document", "rtf": "Rich Text document",
    "odt": "OpenDocument text", "odp": "OpenDocument presentation", "ods": "OpenDocument spreadsheet",
    "pptx": "PowerPoint presentation", "ppt": "PowerPoint 97-2003 presentation", "xlsx": "Excel workbook",
    "xls": "Excel 97-2003 workbook", "csv": "CSV table", "epub": "EPUB e-book", "html": "web page",
    "mhtml": "saved web page", "xps": "XPS document", "iwork": "Apple iWork document", "image": "image",
    "markdown": "Markdown file", "text": "text file",
}

TEXT_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".txt", ".text", ".rst", ".csv", ".tsv", ".html", ".htm",
                 ".xhtml", ".shtml", ".mht", ".mhtml"}

# Extensions that promise a binary format. If the content is none of the binary formats below,
# the file is damaged or misnamed, and saying so helps more than reading it as text.
BINARY_SUFFIXES = {
    ".pdf": "PDF", ".docx": "Word document", ".docm": "Word document", ".dotx": "Word document",
    ".dotm": "Word document", ".doc": "Word document", ".rtf": "Rich Text document",
    ".pptx": "PowerPoint file", ".pptm": "PowerPoint file", ".ppsx": "PowerPoint file", ".potx": "PowerPoint file",
    ".ppt": "PowerPoint file", ".pps": "PowerPoint file", ".xlsx": "Excel workbook", ".xlsm": "Excel workbook",
    ".xls": "Excel workbook", ".odt": "OpenDocument file", ".ods": "OpenDocument file", ".odp": "OpenDocument file",
    ".epub": "EPUB e-book", ".xps": "XPS document", ".oxps": "XPS document",
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
                     ".heif", ".avif"), "image"),
}
# Every extension a manual can have here (the content decides in the end; this list is for
# picking manual files out of a folder, such as data/manuals/).
KNOWN_SUFFIXES = set(BINARY_SUFFIXES) | TEXT_SUFFIXES

# Files that are not documents, or that no free library reads well: say what to do instead.
REFUSED_SUFFIXES = {
    **dict.fromkeys((".pages", ".key", ".numbers"), "Export it from the Apple app as PDF or Word, then upload that."),
    **dict.fromkeys((".wpd", ".wps", ".pub", ".vsd", ".vsdx", ".one", ".indd"),
                    "Save or export it as PDF or Word, then upload that."),
    **dict.fromkeys((".zip", ".rar", ".7z", ".gz", ".tar"), "Unzip it and upload the manual inside."),
    **dict.fromkeys((".exe", ".msi", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".apk", ".dmg", ".iso"),
                    "Programs and installers are not manuals."),
    **dict.fromkeys((".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".flac"),
                    "Audio and video can't be read as text."),
}

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy Office files (.doc, .xls, .ppt)
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"}

XML_PART_SUFFIXES = (".xml", ".rels", ".fpage", ".xhtml", ".html", ".htm", ".opf", ".ncx")
WML_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PML_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


class ManualError(ValueError):
    """A file that cannot be used as a manual. The message is safe to show to users;
    `status` is the HTTP status the API returns for it."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Converted:
    """One document, read. Either `markdown` (most formats), `pages` (photos, scans, XPS: one
    text per page, laid out like PDF page text) or `pdf` (a PDF found inside the file)."""

    kind: str
    markdown: str = ""
    pages: list[tuple[int, str]] = field(default_factory=list)
    pdf: bytes | None = None
    title: str | None = None
    page_count: int | None = None
    ocr_pages: int = 0

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, "document")


def unsupported(advice: str | None = None) -> ManualError:
    message = "This file type can't be read as a manual."
    return ManualError(f"{message} {advice or 'Supported: ' + SUPPORTED_SUMMARY + '.'}", status=415)


# --------------------------------------------------------------------------- detection
def detect_format(data: bytes, filename: str) -> str:
    """The file's real format, from its first bytes; the extension only breaks ties for text."""
    suffix = Path(filename).suffix.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return _zip_kind(data)
    if data.startswith(OLE_MAGIC):
        return _ole_kind(data)
    if data.lstrip()[:5] == b"{\\rtf":
        return "rtf"
    if _is_image(data):
        return "image"
    if suffix in BINARY_SUFFIXES:
        raise ManualError(f"This file is not a valid {BINARY_SUFFIXES[suffix]}. It may be damaged or misnamed.")
    if suffix in REFUSED_SUFFIXES:
        raise unsupported(REFUSED_SUFFIXES[suffix])
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if suffix in {".mht", ".mhtml"} or (head.startswith((b"from:", b"mime-version:")) and b"multipart/related" in head):
        return "mhtml"
    if suffix in {".html", ".htm", ".xhtml", ".shtml"} or head.startswith((b"<!doctype html", b"<html")):
        return "html"
    if suffix in {".csv", ".tsv"}:
        return "csv"
    if suffix in {".md", ".markdown", ".mdown", ".mkd"}:
        return "markdown"
    return "text"


def _is_image(data: bytes) -> bool:
    if data.startswith(IMAGE_MAGIC):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return True
    # BMP: "BM" alone would match a text file starting "BMW...", so also check the header's size field.
    return data[:2] == b"BM" and len(data) > 26 and data[6:10] == b"\0\0\0\0" and \
        int.from_bytes(data[2:6], "little") in (0, len(data))


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ManualError("This file is damaged and could not be opened.") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES or any(
            i.file_size > MAX_XML_PART_BYTES for i in infos if i.filename.lower().endswith(XML_PART_SUFFIXES)):
        raise ManualError("This file unpacks to too much data to read.", status=413)
    return archive


def _zip_kind(data: bytes) -> str:
    names = set(_open_zip(data).namelist())
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "mimetype" in names:
        mimetype = _open_zip(data).read("mimetype")[:100].decode("ascii", "ignore").strip()
        if mimetype == "application/epub+zip":
            return "epub"
        if mimetype.startswith("application/vnd.oasis.opendocument."):
            kind = mimetype.rsplit(".", 1)[-1]
            return {"spreadsheet": "ods", "presentation": "odp"}.get(kind.split("-")[0], "odt")
    if any(n.lower().endswith(".fpage") for n in names):
        return "xps"
    if "QuickLook/Preview.pdf" in names:
        return "iwork"
    if any(n.startswith("Index/") and n.endswith(".iwa") for n in names):
        raise unsupported("Export it from Pages, Keynote or Numbers as PDF or Word, then upload that.")
    raise unsupported("This is a ZIP archive, not a document. Unzip it and upload the manual inside.")


def _ole_kind(data: bytes) -> str:
    import olefile

    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as exc:
        raise ManualError("This file is damaged and could not be opened.") from exc
    with ole:
        if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
            raise ManualError("This Office file is password-protected. Remove the password and upload it again.")
        if ole.exists("WordDocument"):
            return "doc"
        if ole.exists("Workbook") or ole.exists("Book"):
            return "xls"
        if ole.exists("PowerPoint Document"):
            return "ppt"
    raise unsupported("Save it as PDF or Word, then upload that.")


# --------------------------------------------------------------------------- entry point
def convert(data: bytes, filename: str, *, ocr_max_pages: int = OCR_MAX_PAGES, progress: Progress = None) -> Converted:
    """Read any supported file. Raises ManualError with a message for the user."""
    if not data.strip():
        raise ManualError("This file is empty.")
    kind = detect_format(data, filename)
    if kind == "pdf":
        return Converted(kind="pdf", pdf=data)
    try:
        if kind == "image":
            result = _read_image(data, filename, ocr_max_pages, progress)
        else:
            result = READERS[kind](data, filename, _Budget())
    except ManualError:
        raise
    except RecursionError as exc:
        raise ManualError("This file is nested too deeply to read.") from exc
    except Exception as exc:  # each library raises its own types for damaged files
        raise ManualError(f"This {LABELS[kind]} could not be read. It may be damaged.") from exc
    if not result.pdf and not result.pages and not result.markdown.strip():
        raise ManualError(f"No text could be found in this {LABELS[kind]}.")
    return result


# --------------------------------------------------------------------------- shared helpers
class _Budget:
    """What one document may hold, counted while it is read: MAX_MANUAL_CHARS of text and
    MAX_TABLE_CELLS table cells in all its sheets, tables and chapters. A small hostile file (a sheet
    listed 4,000 times, rows repeated a million times, one chapter listed 20,000 times) is refused at
    once, instead of after minutes of work and gigabytes of memory."""

    def __init__(self) -> None:
        self._chars = self._cells = self._scanned = 0

    def scanned(self, count: int) -> None:
        """Spreadsheet cells looked at, blanks included. Formatted but empty cells are common in real
        workbooks, so they do not count as content, only against this larger limit on the work done."""
        self._scanned += count
        if self._scanned > MAX_SCANNED_CELLS:
            raise ManualError("The tables in this file are too large to index.", status=413)

    def chars(self, count: int) -> None:
        self._chars += count
        if self._chars > MAX_MANUAL_CHARS:
            raise ManualError(TOO_MUCH_TEXT, status=413)

    def text(self, text: str) -> str:
        self.chars(len(text))
        return text

    def cells(self, count: int) -> None:
        self._cells += count
        if self._cells > MAX_TABLE_CELLS:
            raise ManualError("The tables in this file are too large to index.", status=413)


def _cp1252_bytes(error: UnicodeDecodeError) -> tuple[str, int]:
    return error.object[error.start:error.end].decode("cp1252", errors="replace"), error.end


codecs.register_error("cp1252-bytes", _cp1252_bytes)


def decode_text(data: bytes) -> str:
    """Bytes of a text file as a string. UTF-16 with a byte-order mark ("Unicode" in Notepad), else
    UTF-8, where only bytes that are not valid UTF-8 are read as Windows-1252: one smart quote pasted
    from Word must not turn every "60°C" in an otherwise UTF-8 file into "60Â°C".
    Binary data (a program or a photo renamed to .txt) is refused instead of indexed as garbage."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="cp1252-bytes")
    controls = len(re.findall(r"[\x00-\x08\x0e-\x1f]", text[:200_000]))
    if "\x00" in text or controls > 10 or text.count("�") > max(3, len(text) // 100):
        raise unsupported("It does not look like a document.")
    return text


# A letter or digit of any script first ("Übersicht", "安装说明"); a lower-case start is refused separately.
HEADING_LINE_RE = re.compile(r"^[^\W_][\w/&()'’,.\- ]{1,70}$")
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6} ", re.M)
LIST_ITEM_RE = re.compile(r"^(\d+[.)]|[-•*·])\s")
LIST_START_RE = re.compile(r"^(1[.)]|[-•*·])\s")
CLOSING = tuple(".:,;?!。：，；？！")
SENTENCE_END = tuple(".:。：")


def guess_headings(text: str, paragraph_lines: bool = False) -> str:
    """Mark short, title-like lines followed by body text as "##" headings, for formats that keep
    no heading styles (plain text, Rich Text, Word 97). "Error Codes" above a table becomes a section.
    `paragraph_lines`: every line is a whole paragraph (Word 97, Rich Text), not a hard-wrapped line."""
    if MARKDOWN_HEADING_RE.search(text):
        return text
    lines = text.splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    stripped = [""] + [lines[i].strip() for i in filled] + ["", ""]
    for k, i in enumerate(filled):
        if _is_heading(*stripped[k:k + 4], paragraph_lines=paragraph_lines):
            lines[i] = f"## {stripped[k + 1]}"
    return "\n".join(lines)


def _is_heading(before: str, line: str, after: str, then: str, *, paragraph_lines: bool = False) -> bool:
    """A short, title-like line (never a numbered step or a bullet) followed by body text: a sentence,
    a table row, the start of a list, or a long line that wraps on.

    Word 97 and Rich Text keep no bullets, so there a list survives only as a run of unpunctuated
    paragraphs: a long one followed by another such line is an item, not body text, and a short one
    right after an unpunctuated item of four words or more is the next item, not a heading. Plain
    text is hard-wrapped, where a long unpunctuated line is just a line of a paragraph."""
    if not after or len(line.split()) > 8 or line.endswith(CLOSING) or "|" in line or LIST_ITEM_RE.match(line):
        return False
    if line[:1].islower() or not HEADING_LINE_RE.match(line):
        return False
    if "|" in after or after.endswith(SENTENCE_END) or LIST_START_RE.match(after):
        return True
    if paragraph_lines:
        if before and not before.endswith(CLOSING) and len(before.split()) >= 4 and "|" not in before:
            return False
        next_item = then[:1].isupper() and not then.endswith(CLOSING) and "|" not in then
        return len(after.split()) > 8 and not next_item
    return len(after.split()) > 8


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _cell(value) -> str:
    """A spreadsheet or table cell as text: 4.0 -> "4", dates without the midnight time."""
    import datetime as dt

    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.10g}"
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(" ", "minutes")
    if isinstance(value, dt.date):
        return value.isoformat()
    return _clean(value).replace("|", "/")


def _table_markdown(rows: list[list[str]]) -> str:
    """Rows -> a Markdown table whose first row is the header (app/ingest.py then rewrites each row as
    "Code: F07; Meaning: ..."). One-column and page-layout tables become plain paragraphs."""
    rows = [[_cell(c) for c in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    used = [i for i in range(width) if any(i < len(row) and row[i] for row in rows)]
    rows = [[row[i] if i < len(row) else "" for i in used] for row in rows]
    if len(used) > 1 and len(rows) > 2 and sum(1 for c in rows[0] if c) == 1 and all(rows[1]):
        # A caption merged across the columns ("LW-9 error codes") above the real header row. The row under
        # it is a header only if it reads like one; under "Technical data" it is a first "Width | 60 cm" row.
        caption = next(c for c in rows[0] if c)
        digits = lambda row: any(ch.isdigit() for c in row for ch in c)   # noqa: E731
        if len(used) >= 3:     # short labels; a year or model number in them is fine
            header_like = all(len(c) <= 30 for c in rows[1])
        else:                  # "Code | Meaning" over "F07 | ..." rows, but not "Width | 60 cm" over more values
            header_like = not digits(rows[1]) and all(digits(row) for row in rows[2:])
        if header_like:
            return caption + "\n\n" + _table_markdown(rows[1:])
        return caption + "\n\n" + "\n".join(" | ".join(c for c in row) for row in rows[1:])
    cells = [c for row in rows for c in row if c]
    if len(used) == 1 or sum(map(len, cells)) / len(cells) > 150:   # a layout table, not a data table
        return "\n\n".join(cells)
    if len(rows) == 1 or not all(rows[0]):
        return "\n".join(" | ".join(row) for row in rows)
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(used)]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _render(blocks: list[tuple]) -> tuple[str, str | None]:
    """Blocks -> Markdown, and the document title if the body starts with one.

    Blocks: ("h", level, text) with level 0 = a "Title" style, ("p", text), ("li", text),
    ("table", rows), ("raw", text). The top heading level becomes "##" (one section each); a single
    top-level heading at the very start, above deeper ones, is the document's title instead.
    """
    title = None
    titles = [b for b in blocks if b[0] == "h" and b[1] == 0]
    if titles:
        title = titles[0][2]
        blocks = [b for b in blocks if not (b[0] == "h" and b[1] == 0)]
    levels = [b[1] for b in blocks if b[0] == "h"]
    if levels:
        top = min(levels)
        first = next(i for i, b in enumerate(blocks) if b[0] == "h")
        if levels.count(top) == 1 and blocks[first][1] == top and len(set(levels)) > 1:
            title = title or blocks[first][2]
            blocks = blocks[:first] + blocks[first + 1:]
            top = min(level for level in levels if level != top)
    else:
        top = 1
    parts = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            parts.append(("## " if block[1] <= top else "### ") + _clean(block[2]))
        elif kind == "li":
            parts.append("- " + _clean(block[1]))
        elif kind == "table":
            parts.append(_table_markdown(block[1]))
        elif kind == "raw":
            parts.append(block[1].strip())
        else:
            parts.append(_clean(block[1]))
    return "\n\n".join(p for p in parts if p), title


def _with_main_content_type(data: bytes, part: str, content_type: str) -> bytes:
    """Macro-enabled files and templates (.docm, .dotx, .pptm, .ppsx...) differ from .docx/.pptx only in
    the content type of the main part, which python-docx and python-pptx refuse. Rewrite that one
    value in memory. Macros are never run: only the XML text is read."""
    from defusedxml import ElementTree as DET
    import xml.etree.ElementTree as ET

    archive = _open_zip(data)
    root = DET.fromstring(archive.read("[Content_Types].xml"))
    override = next((o for o in root if o.get("PartName") == part), None)
    if override is None or override.get("ContentType") == content_type:
        return data
    override.set("ContentType", content_type)
    ET.register_namespace("", CT_NS)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as rewritten:
        for info in archive.infolist():
            body = ET.tostring(root, xml_declaration=True, encoding="UTF-8") \
                if info.filename == "[Content_Types].xml" else archive.read(info)
            rewritten.writestr(info, body)
    return out.getvalue()


# --------------------------------------------------------------------------- text formats
COLUMN_GAP_RE = re.compile(r"(?<=\S)(?: {3,}|\t+)(?=\S)")


def _columns_to_cells(text: str) -> str:
    """Columns lined up with spaces or tabs ("F07   Drain pump blocked") become "F07 | Drain pump blocked",
    as for PDF tables, so a row stays one unit and is not mistaken for a heading."""
    return "\n".join(COLUMN_GAP_RE.sub(" | ", line) for line in text.splitlines())


def _read_text(data: bytes, filename: str, _budget: _Budget) -> Converted:
    return Converted(kind="text", markdown=guess_headings(_columns_to_cells(decode_text(data))))


def _read_markdown(data: bytes, filename: str, _budget: _Budget) -> Converted:
    return Converted(kind="markdown", markdown=decode_text(data))


def _read_csv(data: bytes, filename: str, budget: _Budget) -> Converted:
    text = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:20_000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if filename.lower().endswith(".tsv") else csv.excel
    rows = []
    for row in csv.reader(io.StringIO(text), dialect):
        budget.cells(max(len(row), 1))
        rows.append(row)
    return Converted(kind="csv", markdown=_table_markdown(rows))


def _read_rtf(data: bytes, filename: str, _budget: _Budget) -> Converted:
    from striprtf.striprtf import rtf_to_text

    text = rtf_to_text(data.decode("latin-1"), encoding="cp1252", errors="replace")
    # table cells come out as "F07|Drain pump blocked|": space them like every other table row
    text = "\n".join(" | ".join(c.strip() for c in line.rstrip("|").split("|")) if "|" in line else line
                     for line in text.splitlines())
    return Converted(kind="rtf", markdown=guess_headings(_columns_to_cells(text), paragraph_lines=True))


# --------------------------------------------------------------------------- web pages and e-books
class _HtmlBlocks(HTMLParser):
    """HTML -> blocks. Skips scripts, styles, navigation menus and forms; keeps headings, paragraphs,
    list items and tables. A table with headings or other tables in its cells lays out the page:
    what its cells hold is read as ordinary blocks."""

    SKIP = {"script", "style", "noscript", "template", "svg", "math", "head", "nav", "select", "iframe", "object"}
    HEAD = {"title", "meta", "link", "style", "script", "base", "noscript", "template"}   # what <head> may hold
    VOID = {"br", "hr", "img", "meta", "link", "input", "wbr", "source", "area", "col", "base", "embed", "param", "track"}
    BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "aside", "blockquote", "pre", "ul", "ol",
             "dl", "dt", "dd", "figure", "figcaption", "address", "hr", "br", "caption", "details", "summary",
             "center", "body", "form", "fieldset", "legend"}

    def __init__(self, budget: _Budget) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple] = []
        self.title: str | None = None
        self._budget = budget
        self._buf: list[str] = []
        self._skip: list[str] = []
        self._skipping: Counter[str] = Counter()   # the tags on _skip: an end tag is looked up at once
        self._title: list[str] | None = None
        self._heading: tuple[int, list[str]] | None = None
        self._list_item = False
        self._pre = 0
        self._tables: list[dict] = []

    def _cell_open(self) -> bool:
        return bool(self._tables) and self._tables[-1]["cell"] is not None

    def _flush(self) -> None:
        if self._cell_open():
            self._tables[-1]["cell"].append(" ")
            return
        text = "".join(self._buf).strip()
        if text:
            self.blocks.append(("li" if self._list_item else "p", text))
        self._buf, self._list_item = [], False

    def _push_skip(self, tag: str) -> None:
        self._skip.append(tag)
        self._skipping[tag] += 1

    def _pop_skip(self, tag: str) -> None:
        while self._skip:
            popped = self._skip.pop()
            self._skipping[popped] -= 1
            if popped == tag:
                return

    def _close_cell(self, table: dict) -> None:
        if table["cell"] is not None:
            table["row"].append(_clean("".join(table["cell"])))
            table["cell"] = None
            self._budget.cells(1)

    def _close_row(self, table: dict) -> None:
        """End the open row: </td> and </tr> may be left out, so a new row or row group ends it too."""
        self._close_cell(table)
        if table["row"] is not None:
            table["rows"].append(table["row"])
            table["row"] = None

    def _to_layout(self, table: dict) -> None:
        """A heading or a table inside a cell: the table lays out the page. What its cells held so far
        becomes paragraphs, and what they hold from now on is read as ordinary blocks."""
        self._close_row(table)
        self.blocks += [("p", cell) for row in table["rows"] for cell in row if cell]
        table.update(rows=[], layout=True)

    def handle_starttag(self, tag, attrs):
        if self._skip == ["head"] and tag not in self.HEAD:   # </head> and <body> may both be left out
            self._pop_skip("head")
        if self._skip == ["head"] and tag == "title":
            self._title = []
            return
        if self._skip:
            if tag in self.SKIP:
                self._push_skip(tag)
            return
        if tag == "title":
            self._title = []
        elif tag in self.SKIP:
            self._push_skip(tag)
        elif tag == "table":
            inline = False
            if self._cell_open():
                # A table in a cell of a data table that already has a whole row is a small detail table:
                # its text joins that cell. Before the first whole row, it means the outer table lays out the page.
                if self._tables[-1]["rows"]:
                    inline = True
                else:
                    self._to_layout(self._tables[-1])
            if not inline:
                self._flush()
            self._tables.append({"rows": [], "row": None, "cell": None, "layout": False, "inline": inline})
        elif tag in ("tr", "td", "th", "thead", "tbody", "tfoot") and self._tables:
            table = self._tables[-1]
            if table["layout"]:
                self._flush()
            elif tag in ("td", "th"):
                self._close_cell(table)
                if table["row"] is None:
                    table["row"] = []
                table["cell"] = []
                if tag == "th":
                    table["header"] = True
            else:
                self._close_row(table)
                if tag == "tr":
                    table["row"] = []
        elif re.fullmatch(r"h[1-6]", tag):
            if self._cell_open():
                self._to_layout(self._tables[-1])
            self._flush()
            self._heading = (int(tag[1]), [])
        elif tag == "li":
            self._flush()
            self._list_item = not self._cell_open()
        elif tag in self.BLOCK:
            self._flush()
        if tag == "pre":
            self._pre += 1

    def handle_endtag(self, tag):
        if tag == "title" and self._title is not None:
            self.title = _clean("".join(self._title)) or None
            self._title = None
            return
        if self._skip:
            if self._skipping[tag]:
                self._pop_skip(tag)
            return
        if self._heading and tag == f"h{self._heading[0]}":
            text = _clean("".join(self._heading[1]))
            if text:
                self.blocks.append(("h", self._heading[0], text))
            self._heading = None
        elif tag in ("td", "th", "tr") and self._tables and not self._tables[-1]["layout"]:
            if tag == "tr":
                self._close_row(self._tables[-1])
            else:
                self._close_cell(self._tables[-1])
        elif tag == "table" and self._tables:
            table = self._tables.pop()
            if table.get("inline") and self._cell_open():
                self._close_row(table)
                cells = [c for row in table["rows"] for c in row if c]
                if not table.get("header") and (len(table["rows"]) <= 1 or len(cells) <= 4):
                    self._tables[-1]["cell"].append(" " + " ".join(cells) + " ")   # a small detail table
                    return
                # A real table (a header row, or several rows) in a cell: the outer table lays out the page.
                self._to_layout(self._tables[-1])
                if table["rows"]:
                    self.blocks.append(("table", table["rows"]))
                return
            self._flush()
            if not table["layout"]:
                self._close_row(table)
                if table["rows"]:
                    self.blocks.append(("table", table["rows"]))
        elif tag in self.BLOCK or tag in ("li", "td", "th", "tr"):
            self._flush()
        if tag == "pre":
            self._pre = max(0, self._pre - 1)

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
            return
        if self._skip:
            return
        if not self._pre:
            data = re.sub(r"\s+", " ", data)
        if data.strip():
            self._budget.text(data)
        if self._heading:
            self._heading[1].append(data)
        elif self._cell_open():
            self._tables[-1]["cell"].append(data)
        else:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


SLOW_CODECS = {"punycode", "idna", "utf-7", "unicode-escape", "raw-unicode-escape"}


def _decode_declared(data: bytes, charset: str | None) -> str:
    """Bytes in the charset a page declares, if that is a real text encoding, else read as a text file.
    Python also knows codecs that no page is written in, such as punycode, whose decoder takes
    hours on a long run of letters."""
    try:
        if charset and codecs.lookup(charset).name not in SLOW_CODECS:
            return data.decode(charset, errors="replace")
    except (LookupError, UnicodeError):     # unknown, not a text encoding ("base64"), or one that always fails
        pass
    return decode_text(data)


def _decode_html(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    declared = re.search(rb"""(?:charset|encoding)\s*=\s*["']?([\w.:-]+)""", data[:4096], re.I)
    return _decode_declared(data, declared.group(1).decode("ascii") if declared else None)


def html_blocks(html: str, budget: _Budget) -> tuple[list[tuple], str | None]:
    parser = _HtmlBlocks(budget)
    parser.feed(html)
    parser.close()
    return parser.blocks, parser.title


def _read_html(data: bytes, filename: str, budget: _Budget) -> Converted:
    blocks, page_title = html_blocks(_decode_html(data), budget)
    markdown, title = _render(blocks)
    return Converted(kind="html", markdown=markdown, title=title or page_title)


def _read_mhtml(data: bytes, filename: str, budget: _Budget) -> Converted:
    message = email.message_from_bytes(data, policy=email.policy.default)
    parts = [p for p in message.walk() if p.get_content_type() in ("text/html", "text/plain")]
    part = next((p for p in parts if p.get_content_type() == "text/html"), parts[0] if parts else None)
    if part is None:
        raise ManualError("No page was found inside this saved web page.")
    content = _decode_declared(part.get_payload(decode=True) or b"", part.get_content_charset())
    if part.get_content_type() == "text/plain":
        return Converted(kind="mhtml", markdown=guess_headings(content))
    blocks, page_title = html_blocks(content, budget)
    markdown, title = _render(blocks)
    return Converted(kind="mhtml", markdown=markdown, title=title or page_title)


def _read_epub(data: bytes, filename: str, budget: _Budget) -> Converted:
    from defusedxml import ElementTree as DET

    archive = _open_zip(data)
    container = DET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile")).get("full-path")
    package = DET.fromstring(archive.read(rootfile))
    base = posixpath.dirname(rootfile)
    manifest = {item.get("id"): item.get("href") for item in package.iter() if item.tag.endswith("}item")}
    title_el = next((e for e in package.iter() if e.tag == "{http://purl.org/dc/elements/1.1/}title"), None)
    blocks: list[tuple] = []
    names = set(archive.namelist())
    hrefs = (manifest.get(e.get("idref")) for e in package.iter() if e.tag.endswith("}itemref"))
    # each chapter once, however often the spine lists it
    paths = dict.fromkeys(posixpath.normpath(posixpath.join(base, unquote(href))) for href in hrefs if href)
    for path in paths:
        if path in names:
            blocks.extend(html_blocks(_decode_html(archive.read(path)), budget)[0])
    markdown, title = _render(blocks)
    book_title = _clean(title_el.text) if title_el is not None and title_el.text else None
    return Converted(kind="epub", markdown=markdown, title=book_title or title)


# --------------------------------------------------------------------------- Word, PowerPoint, Excel
def _w(name: str) -> str:
    return "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}" + name


W_P, W_TBL, W_T, W_TXBX = _w("p"), _w("tbl"), _w("t"), _w("txbxContent")
# Run children that stand for a character, as python-docx reads them.
DOCX_CHARS = {_w("tab"): "\t", _w("ptab"): "\t", _w("br"): "\n", _w("cr"): "\n", _w("noBreakHyphen"): "-"}
# Never read: deleted or moved-away text, properties (tab stops are w:tab too), and the copy of a
# drawing kept for old Word versions (mc:Fallback), which repeats the text box of its mc:Choice.
DOCX_SKIP = {_w("del"), _w("moveFrom"), _w("pPr"), _w("rPr"), _w("sdtPr"),
             "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"}


def _docx_text(element, boxes: list) -> str:
    """A paragraph's text as Word shows it with tracked changes accepted. python-docx's paragraph.text
    reads only plain runs and hyperlinks, so inserted text (w:ins), content controls, smart tags and
    simple fields were lost. Text boxes are put in `boxes`, to be read after the paragraph."""
    parts = []
    for child in element:
        if child.tag == W_T:
            parts.append(child.text or "")
        elif child.tag in DOCX_CHARS:
            parts.append(DOCX_CHARS[child.tag])
        elif child.tag == W_TXBX:
            boxes.append(child)
        elif child.tag not in DOCX_SKIP:
            parts.append(_docx_text(child, boxes))
    return "".join(parts)


def _docx_content(container) -> Iterator[tuple]:
    """(paragraph, its text) and (table, None) in reading order, from the body, a cell or a text box.
    Content controls (w:sdt, as on Word cover pages) and custom XML are opened, and the text boxes
    anchored in a paragraph follow it."""
    for child in container:
        if child.tag == W_P:
            boxes: list = []
            yield child, _docx_text(child, boxes)
            for box in boxes:
                yield from _docx_content(box)
        elif child.tag == W_TBL:
            yield child, None
        elif child.tag == _w("sdt"):
            content = child.find(_w("sdtContent"))
            if content is not None:
                yield from _docx_content(content)
        elif child.tag == _w("customXml"):
            yield from _docx_content(child)


def _docx_styles(document) -> tuple[dict[str, tuple[str, str | None]], str | None]:
    """Paragraph styles by id, as (lower-case name, id of the style it is based on), and the default
    style's id. Read once: python-docx searches every style at each lookup, which took minutes for
    a file with thousands of styles and paragraphs."""
    styles, default = {}, None
    for style in document.styles.element.findall(_w("style")):
        if style.get(_w("type"), "paragraph") != "paragraph":
            continue
        name, based = style.find(_w("name")), style.find(_w("basedOn"))
        style_id = style.get(_w("styleId"))
        styles[style_id] = ((name.get(_w("val")) or "").lower() if name is not None else "",
                            based.get(_w("val")) if based is not None else None)
        if style.get(_w("default")) in ("1", "true", "on"):
            default = style_id
    return styles, default


def _docx_heading_level(paragraph, style_id: str | None, styles: dict) -> int | None:
    for _ in range(6):                    # custom heading styles are often based on "Heading N"
        if style_id not in styles:
            break
        name, style_id = styles[style_id]
        if name == "title":
            return 0
        match = re.fullmatch(r"heading (\d)", name)
        if match:
            return int(match.group(1))
    outline = paragraph.xpath("./w:pPr/w:outlineLvl/@w:val")
    if outline and outline[0].isdigit() and int(outline[0]) < 9:
        return int(outline[0]) + 1
    return None


def _docx_rows(table, budget: _Budget) -> list[list[str]]:
    """A Word table's rows with each cell once, however many grid columns it spans (python-docx's
    row.cells repeats it per column: two billion times for a hostile w:gridSpan). A cell merged
    down from the row above repeats that cell's text, as python-docx gives it."""
    rows, above = [], {}
    for tr in table.tr_lst:
        cells, column = [], tr.grid_before
        for tc in tr.tc_lst:
            text = above.get(column, "") if tc.vMerge == "continue" else _docx_cell_text(tc, budget)
            above[column] = text
            cells.append(budget.text(text))
            column += tc.grid_span
        budget.cells(max(len(cells), 1))
        rows.append(cells)
    return rows


def _docx_cell_text(tc, budget: _Budget) -> str:
    parts = []
    for element, text in _docx_content(tc):
        if text is None:                  # a table inside the cell: its rows, flattened
            parts += [" ".join(row) for row in _docx_rows(element, budget)]
        else:
            parts.append(text)
    return "\n".join(parts)


def _read_docx(data: bytes, filename: str, budget: _Budget) -> Converted:
    import docx
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(_with_main_content_type(data, "/word/document.xml", WML_MAIN)))
    styles, default = _docx_styles(document)
    blocks: list[tuple] = []
    bold_lines: set[int] = set()
    for element, text in _docx_content(document.element.body):
        if text is None:
            blocks.append(("table", _docx_rows(element, budget)))
            continue
        text = budget.text(text.strip())
        if not text:
            continue
        style_id = element.style if element.style in styles else default
        level = _docx_heading_level(element, style_id, styles)
        if level is not None:
            blocks.append(("h", level, text))
            continue
        runs = [r for r in Paragraph(element, document).runs if r.text.strip()]
        if runs and all(r.bold for r in runs) and len(text.split()) <= 8 and not text.endswith((".", ":", ",")):
            bold_lines.add(len(blocks))
        numbered = element.pPr is not None and element.pPr.numPr is not None
        blocks.append(("li" if numbered or "list" in styles.get(style_id, ("",))[0] else "p", text))
    if not any(b[0] == "h" for b in blocks):   # no heading styles: short bold lines act as headings
        blocks = [("h", 2, b[1]) if i in bold_lines else b for i, b in enumerate(blocks)]
    markdown, title = _render(blocks)
    return Converted(kind="docx", markdown=markdown, title=document.core_properties.title or title)


def _pptx_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in sorted(shapes, key=lambda s: (s.top or 0, s.left or 0)):
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _pptx_shapes(shape.shapes)
        else:
            yield shape


def _read_pptx(data: bytes, filename: str, budget: _Budget) -> Converted:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(_with_main_content_type(data, "/ppt/presentation.xml", PML_MAIN)))
    blocks: list[tuple] = []
    read: set[str] = set()
    for slide in deck.slides:
        if slide.part.partname in read:   # the deck lists this slide again: read it once
            continue
        read.add(slide.part.partname)
        number = len(read)
        title_shape = slide.shapes.title
        heading = budget.text(title_shape.text_frame.text.strip()) if title_shape is not None else ""
        blocks.append(("h", 1, f"Slide {number}: {_clean(heading)}" if heading else f"Slide {number}"))
        for shape in _pptx_shapes(slide.shapes):
            if title_shape is not None and shape.shape_id == title_shape.shape_id:
                continue
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[budget.text(c.text) for c in row.cells] for row in shape.table.rows]
                budget.cells(sum(max(len(row), 1) for row in rows))
                blocks.append(("table", rows))
            elif shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = budget.text(paragraph.text.strip())   # .text keeps line breaks (a:br) as \v
                    if text:
                        blocks.append(("li" if paragraph.level else "p", text))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = budget.text(slide.notes_slide.notes_text_frame.text.strip())
            if notes:
                blocks.append(("p", f"Speaker notes: {notes}"))
    markdown, _ = _render(blocks)
    return Converted(kind="pptx", markdown=markdown, title=deck.core_properties.title or None,
                     page_count=len(read))


def _sheet_blocks(name: str, rows, budget: _Budget) -> list[tuple]:
    """One sheet as a table. Each row is cut after its last value, and only those cells count as content;
    every cell looked at, empty rows included, counts against the budget for work done."""
    table = []
    for row in rows:
        budget.scanned(max(len(row), 1))   # a sheet can list a million empty rows
        used = len(row)
        while used and (row[used - 1] is None or row[used - 1] == ""):
            used -= 1
        if used:
            budget.cells(used)
            budget.chars(sum(len(value) for value in row[:used] if isinstance(value, str)))
            table.append(list(row[:used]))
    return [("h", 1, f"Sheet: {name}"), ("table", table)] if table else []


def _read_xlsx(data: bytes, filename: str, budget: _Budget) -> Converted:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        blocks, read = [], set()
        for ws in workbook.worksheets:
            if ws._worksheet_path in read:        # several sheet names for one sheet: read it once
                continue
            read.add(ws._worksheet_path)
            # In read-only mode openpyxl believes the sheet's own <dimension>: a wrong one cuts the sheet
            # short, and a huge one pads every row to 16,384 cells.
            ws.reset_dimensions()
            blocks += _sheet_blocks(ws.title, ws.iter_rows(values_only=True), budget)
        title = workbook.properties.title
    finally:
        workbook.close()
    markdown, _ = _render(blocks)
    return Converted(kind="xlsx", markdown=markdown, title=title or None)


def _xls_row(sheet, row: int, datemode: int) -> list:
    """An .xls row with its date cells as dates: xlrd gives Excel's day numbers, such as 45413."""
    import xlrd

    values = sheet.row_values(row)
    for i, kind in enumerate(sheet.row_types(row)):
        if kind == xlrd.XL_CELL_DATE:
            try:
                moment = xlrd.xldate.xldate_as_datetime(values[i], datemode)
            except (ValueError, OverflowError):
                continue
            values[i] = moment.time() if values[i] < 1 else moment   # a time of day has no date
    return values


def _read_xls(data: bytes, filename: str, budget: _Budget) -> Converted:
    import xlrd

    try:
        # ragged_rows: a single cell at row 65,536, column 256 would otherwise fill the whole grid
        book = xlrd.open_workbook(file_contents=data, on_demand=True, ragged_rows=True)
    except xlrd.biffh.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise ManualError("This Excel file is password-protected. Remove the password and upload it again.") from exc
        raise
    try:
        blocks = []
        for index in range(book.nsheets):      # one sheet in memory at a time
            sheet = book.sheet_by_index(index)
            rows = (_xls_row(sheet, r, book.datemode) for r in range(sheet.nrows))
            blocks += _sheet_blocks(sheet.name, rows, budget)
            book.unload_sheet(index)
    finally:
        book.release_resources()
    markdown, _ = _render(blocks)
    return Converted(kind="xls", markdown=markdown)


# --------------------------------------------------------------------------- legacy Word and PowerPoint
def _read_doc(data: bytes, filename: str, _budget: _Budget) -> Converted:
    """Word 97-2003 (.doc): the text is stored in pieces, listed in a "piece table" ([MS-DOC] 2.9.177).
    Formatting is not decoded, so headings are guessed from the text."""
    import olefile

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        word = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", word, 0x0A)[0]
        if struct.unpack_from("<H", word, 0)[0] != 0xA5EC:
            raise ManualError("This Word document could not be read. It may be damaged.")
        if flags & 0x0100:
            raise ManualError("This Word document is password-protected. Remove the password and upload it again.")
        table = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()
        meta_title = ole.get_metadata().title
    pos = 32                                              # FibBase is 32 bytes
    pos += 2 + 2 * struct.unpack_from("<H", word, pos)[0]  # skip FibRgW97
    lw_count = struct.unpack_from("<H", word, pos)[0]
    ccp_text = struct.unpack_from("<I", word, pos + 2 + 3 * 4)[0]   # FibRgLw97.ccpText: main text length
    pos += 2 + 4 * lw_count
    pos += 2                                              # cbRgFcLcb
    fc_clx, lcb_clx = struct.unpack_from("<II", word, pos + 33 * 8)  # FibRgFcLcb97.fcClx / lcbClx
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:                # skip Prc entries (formatting)
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise ManualError("This Word document could not be read. It may be damaged.")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + lcb]
    count = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    if ccp_text > MAX_MANUAL_CHARS:
        raise ManualError(TOO_MUCH_TEXT, status=413)
    # Only the main text's ccpText characters are read: thousands of pieces can point at the same
    # megabytes, which decoded in full would take gigabytes.
    pieces, remaining = [], ccp_text
    for k in range(count):
        if remaining <= 0:
            break
        fc_raw = struct.unpack_from("<I", plc, 4 * (count + 1) + 8 * k + 2)[0]
        length = min(cps[k + 1] - cps[k], remaining)
        if length <= 0:
            continue
        offset = fc_raw & 0x3FFFFFFF
        if fc_raw & 0x40000000:                           # compressed: one byte per character
            piece = word[offset // 2:offset // 2 + length].decode("cp1252", errors="replace")
        else:
            piece = word[offset:offset + 2 * length].decode("utf-16-le", errors="replace")
        pieces.append(piece)
        remaining -= len(piece)
    text = _clean_word_text("".join(pieces))
    title = meta_title.decode("cp1252", errors="ignore").strip() if meta_title else None
    return Converted(kind="doc", markdown=guess_headings(text, paragraph_lines=True), title=title or None)


def _clean_word_text(text: str) -> str:
    """Word's control characters: fields keep their result, not their code (a hyperlink keeps its words);
    cell marks become " | "; paragraph, line and page breaks become new lines."""
    out, fields = [], []
    for ch in text:
        if ch == "\x13":
            fields.append(False)
        elif ch == "\x14":
            if fields:
                fields[-1] = True
        elif ch == "\x15":
            if fields:
                fields.pop()
        elif not all(fields):
            continue
        else:
            out.append(ch)
    text = "".join(out).replace("\r\x07", "\x07").replace("\x07\x07", "\n").replace("\x07", " | ")
    text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = text.replace("\x1e", "-").replace("\x1f", "").replace("\xa0", " ")
    return re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)


PPT_PLACEHOLDER_RE = re.compile(r"^(click to edit|second level|third level|fourth level|fifth level|\*$)", re.I)


def _ppt_persist_offsets(stream: bytes) -> dict[int, int]:
    """Where each persist object (a slide, a notes page) starts in the stream, from the top-level
    PersistDirectoryAtoms ([MS-PPT] 2.3.4); a later one, written by a quick save, wins."""
    offsets: dict[int, int] = {}
    pos = 0
    while pos + 8 <= len(stream):
        rec_type, length = struct.unpack_from("<HI", stream, pos + 2)
        body, stop = pos + 8, min(pos + 8 + length, len(stream))
        while rec_type == 0x1772 and body + 4 <= stop:      # entries: first id (20 bits), count (12 bits)
            entry = struct.unpack_from("<I", stream, body)[0]
            count = min(entry >> 20, (stop - body - 4) // 4)
            offsets.update(zip(range(entry & 0xFFFFF, (entry & 0xFFFFF) + count),
                               struct.unpack_from(f"<{count}I", stream, body + 4)))
            body += 4 + 4 * count
        pos += 8 + length
    return offsets


def _read_ppt(data: bytes, filename: str, budget: _Budget) -> Converted:
    """PowerPoint 97-2003 (.ppt): walk the record tree of the "PowerPoint Document" stream and collect the
    text atoms ([MS-PPT] TextCharsAtom / TextBytesAtom). The slide list (SlidePersistAtoms) gives the
    slides in order; each slide's drawing, stored elsewhere, is found through the persist directory.
    Speaker notes come after all the slides, each naming the slideId of the slide it belongs to."""
    import olefile

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        stream = ole.openstream("PowerPoint Document").read()
        meta_title = ole.get_metadata().title
    persist = _ppt_persist_offsets(stream)
    slides: list[list[str]] = [[]]
    at: dict[int, list[str]] = {}                         # stream offset of a slide's drawing -> its texts
    by_id: dict[int, list[str]] = {}                      # slideId -> that slide's texts
    seen: set[tuple[int, str]] = set()                    # (id of a slide's text list, text)
    records = [200_000]                                   # records visited, against malformed files

    def walk(start: int, end: int, depth: int, texts: list[str] | None = None, slide_list: bool = False) -> None:
        pos = start
        while pos + 8 <= end and records[0] > 0:
            records[0] -= 1
            ver_instance, rec_type, length = struct.unpack_from("<HHI", stream, pos)
            body, stop = pos + 8, pos + 8 + length
            if stop > end:
                return
            if (ver_instance & 0x000F) == 0x000F:            # a container
                # skip slide masters (0x03F8) and the master and notes lists (SlideListWithText 1 and 2)
                if depth >= 32 or rec_type == 0x03F8 or (rec_type == 0x0FF0 and ver_instance >> 4 != 0):
                    pass
                elif rec_type == 0x03F0:                     # Notes: its NotesAtom names the slide
                    notes: list[str] = []
                    walk(body, stop, depth + 1, notes)
                    is_atom = length >= 12 and struct.unpack_from("<H", stream, body + 2)[0] == 0x03F1
                    slide = by_id.get(struct.unpack_from("<I", stream, body + 8)[0]) if is_atom else None
                    if slide is not None and notes:
                        slide.append("Speaker notes: " + " ".join(notes))
                elif rec_type == 0x03EE:                     # a slide's drawing
                    if pos not in at:
                        slides.append([])
                    walk(body, stop, depth + 1, at.get(pos, slides[-1]))
                else:
                    walk(body, stop, depth + 1, texts, rec_type == 0x0FF0)
            elif rec_type == 0x03F3 and slide_list and length >= 16:   # SlidePersistAtom: the next slide
                texts = []
                slides.append(texts)
                persist_id, slide_id = struct.unpack_from("<I8xI", stream, body)
                by_id[slide_id] = texts
                if persist_id in persist:
                    at[persist[persist_id]] = texts
            elif rec_type in (0x0FA0, 0x0FA8):
                raw = stream[body:stop]
                text = raw.decode("utf-16-le" if rec_type == 0x0FA0 else "cp1252", errors="replace")
                text = text.replace("\r", "\n").replace("\x0b", "\n").strip()
                target = slides[-1] if texts is None else texts
                # the slide list and the slide's drawing can both hold a text: keep it once per slide
                if text and (id(target), text) not in seen and not PPT_PLACEHOLDER_RE.match(text):
                    seen.add((id(target), text))
                    target.append(budget.text(text))
            pos = stop

    walk(0, len(stream), 0)
    blocks: list[tuple] = []
    for number, texts in enumerate((s for s in slides if s), start=1):
        blocks.append(("h", 1, f"Slide {number}: {_clean(texts[0])[:80]}"))
        blocks += [("p", t) for t in texts[1:]]
    markdown, _ = _render(blocks)
    title = meta_title.decode("cp1252", errors="ignore").strip() if meta_title else None
    return Converted(kind="ppt", markdown=markdown, title=title or None,
                     page_count=sum(1 for b in blocks if b[0] == "h") or None)


# --------------------------------------------------------------------------- OpenDocument
ODF = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
}


def _q(prefix: str, name: str) -> str:
    return f"{{{ODF[prefix]}}}{name}"


ODF_SKIP = {_q("text", "table-of-content"), _q("text", "sequence-decls"), _q("office", "forms"),
            _q("table", "named-expressions"), _q("text", "tracked-changes"), _q("office", "annotation"),
            _q("text", "note")}


def _odf_inline(element) -> str:
    parts = [element.text or ""]
    for child in element:
        if child.tag == _q("text", "s"):
            parts.append(" " * min(int(child.get(_q("text", "c"), "1") or 1), 10))
        elif child.tag in (_q("text", "tab"), _q("text", "line-break")):
            parts.append(" ")
        elif child.tag not in ODF_SKIP:
            parts.append(_odf_inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odf_rows(table, budget: _Budget) -> list[list[str]]:
    rows = []
    for row in table.iter(_q("table", "table-row")):
        cells = []
        for cell in row:
            if cell.tag in (_q("table", "table-cell"), _q("table", "covered-table-cell")):
                repeat = min(int(cell.get(_q("table", "number-columns-repeated"), "1") or 1), 50)
                cells += [_clean(" ".join(_odf_inline(p) for p in cell))] * repeat
        # A repeated row is read once per repeat, so it counts that often. Empty rows are not kept,
        # whatever their repeat (every LibreOffice sheet ends with a million of them).
        repeat = min(int(row.get(_q("table", "number-rows-repeated"), "1") or 1), 100) if any(cells) else 0
        budget.cells(max(len(cells), 1) * max(repeat, 1))
        budget.chars(sum(map(len, cells)) * repeat)
        rows += [cells] * repeat
    return rows


def _odf_walk(element, blocks: list[tuple], state: dict) -> None:
    budget = state["budget"]
    for child in element:
        tag = child.tag
        if tag in ODF_SKIP:
            continue
        if tag == _q("text", "h"):
            text = budget.text(_clean(_odf_inline(child)))
            if text:
                blocks.append(("h", int(child.get(_q("text", "outline-level"), "1") or 1), text))
        elif tag == _q("text", "p"):
            text = budget.text(_clean(_odf_inline(child)))
            if text:
                is_title = (child.get(_q("text", "style-name")) or "").lower() == "title"
                blocks.append(("h", 0, text) if is_title else ("p", text))
        elif tag == _q("text", "list-item"):
            item: list[tuple] = []
            _odf_walk(child, item, state)
            if item and item[0][0] == "p":
                item[0] = ("li", item[0][1])
            blocks += item
        elif tag == _q("table", "table"):
            if state["spreadsheet"]:
                blocks.append(("h", 1, f"Sheet: {child.get(_q('table', 'name'))}"))
            blocks.append(("table", _odf_rows(child, budget)))
        elif tag == _q("draw", "page"):
            state["slide"] += 1
            frames = list(child.iter(_q("draw", "frame")))
            title = next((_clean(_odf_inline(f)) for f in frames if f.get(_q("presentation", "class")) == "title"), "")
            blocks.append(("h", 1, f"Slide {state['slide']}: {title}" if title else f"Slide {state['slide']}"))
            for frame in child:
                if frame.get(_q("presentation", "class")) != "title":
                    _odf_walk(frame, blocks, state)
        else:
            _odf_walk(child, blocks, state)


def _read_odf(data: bytes, filename: str, budget: _Budget) -> Converted:
    from defusedxml import ElementTree as DET

    archive = _open_zip(data)
    kind = _zip_kind(data)
    body = DET.fromstring(archive.read("content.xml")).find(_q("office", "body"))
    blocks: list[tuple] = []
    state = {"spreadsheet": kind == "ods", "slide": 0, "budget": budget}
    if body is not None:
        _odf_walk(body, blocks, state)
    title = None
    if "meta.xml" in archive.namelist():
        meta = DET.fromstring(archive.read("meta.xml"))
        element = next((e for e in meta.iter() if e.tag == "{http://purl.org/dc/elements/1.1/}title"), None)
        title = _clean(element.text) if element is not None and element.text else None
    markdown, body_title = _render(blocks)
    return Converted(kind=kind, markdown=markdown, title=title or body_title,
                     page_count=state["slide"] or None)


# --------------------------------------------------------------------------- fixed-layout formats
XPS_ADVANCE_RE = re.compile(r"(?:^|;)[^,;]*,(\d+(?:\.\d+)?)")   # the advance of each glyph in Indices


def _read_xps(data: bytes, filename: str, budget: _Budget) -> Converted:
    """XPS: each page is XML with one <Glyphs UnicodeString="..."> per run of text; group runs into lines."""
    from defusedxml import ElementTree as DET

    archive = _open_zip(data)
    names = [n for n in archive.namelist() if n.lower().endswith(".fpage")]
    names.sort(key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    pages = []
    for number, name in enumerate(names, start=1):
        lines: dict[int, list[tuple]] = {}
        for glyphs in DET.fromstring(archive.read(name)).iter():
            if glyphs.tag.endswith("Glyphs") and glyphs.get("UnicodeString"):
                text = glyphs.get("UnicodeString").removeprefix("{}")
                em = float(glyphs.get("FontRenderingEmSize") or 12)
                # Width: the advances given in Indices (hundredths of an em), and their average for the
                # glyphs without one (Word leaves out the last). A run of spaces is taken as no width,
                # so that it never hides the gap between two table cells.
                given = [float(a) for a in XPS_ADVANCE_RE.findall(glyphs.get("Indices") or "")]
                guess = sum(given) / len(given) if given else 50
                width = (sum(given) + guess * max(len(text) - len(given), 0)) / 100 * em if text.strip() else 0
                rtl = int(glyphs.get("BidiLevel") or 0) % 2 == 1   # right to left: OriginX is the right end
                left = float(glyphs.get("OriginX") or 0) - (width if rtl else 0)
                line = lines.setdefault(round(float(glyphs.get("OriginY") or 0) / 3), [])
                line.append((left, left + width, rtl, text, em, not given))
        ys = sorted(lines)
        gaps = sorted(b - a for a, b in zip(ys, ys[1:]))
        step = gaps[len(gaps) // 4] if gaps else 0   # usual line spacing (short sections make paragraph gaps common)
        out: list[str] = []
        for previous, y in zip([None, *ys], ys):
            if previous is not None and step and y - previous > 1.5 * step:
                out.append("")      # a wider gap than usual: a new block, so headings are recognised
            out.append(_xps_line(sorted(lines[y], key=lambda run: run[0] + run[1])))   # by centre
        text = budget.text(_columns_to_cells("\n".join(out)))
        if text.strip():
            pages.append((number, text))
    return Converted(kind="xps", pages=pages, page_count=len(names))


def _xps_line(runs: list[tuple]) -> str:
    """The text of one line's runs (left, right, right-to-left, text, em, width estimated), in order
    from left to right.

    Runs that touch belong to one word: Word puts each hyphen in a run of its own ("LW", "-", "9").
    A gap of a sixth of an em is a word space, and a gap of an em starts another column or table cell
    (three spaces, which _columns_to_cells turns into " | "). A run without advances in Indices has
    only an estimated width, so the gap after it says little: such runs are kept apart by a space,
    except around a lone hyphen or punctuation mark. Right-to-left words (Hebrew, Arabic), with the
    spaces between them, are laid out from the right, so such a group is read backwards."""
    def lone_mark(text: str) -> bool:
        return len(text.strip()) == 1 and not text.strip().isalnum()

    # A run of spaces takes the direction of the text on both sides of it, as in the Unicode bidi rules.
    rtl, last = [], None
    for run in runs:
        rtl.append(last)
        last = run[2] if run[3].strip() else last
    last = None
    for i in range(len(runs) - 1, -1, -1):
        if runs[i][3].strip():
            rtl[i] = last = runs[i][2]
        else:
            rtl[i] = bool(rtl[i] and last)
    groups: list[tuple[bool, list[tuple]]] = []
    for backwards, run in zip(rtl, runs):
        if groups and groups[-1][0] == backwards:
            groups[-1][1].append(run)
        else:
            groups.append((backwards, [run]))
    parts: list[str] = []
    for g, (backwards, group) in enumerate(groups):
        order = group[::-1] if backwards else group
        for i, run in enumerate(order):
            if not parts:
                parts.append(run[3])
                continue
            if i:
                gap = order[i - 1][0] - run[1] if backwards else run[0] - order[i - 1][1]
            else:
                gap = group[0][0] - groups[g - 1][1][-1][1]   # between two groups, as laid out
            before = order[i - 1] if i else groups[g - 1][1][-1]
            if gap > run[4]:
                parts.append("   ")
            elif parts[-1][-1:].isspace() or run[3][:1].isspace():
                pass                                   # the space is already in the text
            elif before[5] or run[5]:
                if not (lone_mark(before[3]) or lone_mark(run[3])):
                    parts.append(" ")
            elif gap > run[4] / 6:
                parts.append(" ")
            parts.append(run[3])
    return "".join(parts)


def _read_iwork(data: bytes, filename: str, _budget: _Budget) -> Converted:
    """Older Pages and Keynote files carry a PDF preview of the whole document; read that."""
    return Converted(kind="iwork", pdf=_open_zip(data).read("QuickLook/Preview.pdf"))


# --------------------------------------------------------------------------- OCR: scans and photos
_OCR_LOCK = threading.RLock()       # one page at a time: OCR uses every core, and pdfium is not thread-safe
_OCR_ENGINE = None


def _model_dir() -> Path:
    from app.config import get_settings

    return get_settings().ocr_model_dir


def english_model() -> Path | None:
    """The English recognition model: from the cache, or downloaded once (about 9 MB) and checked
    against its SHA-256. None when it cannot be had (offline): OCR then uses the bundled model,
    which reads the words but often runs them together."""
    path = _model_dir() / OCR_EN_MODEL_NAME
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == OCR_EN_MODEL_SHA256:
        return path
    if os.environ.get("OCR_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return None
    try:
        with urllib.request.urlopen(OCR_EN_MODEL_URL, timeout=60) as response:   # noqa: S310 (fixed https URL)
            body = response.read(20 * 1024 * 1024)
        if hashlib.sha256(body).hexdigest() != OCR_EN_MODEL_SHA256:
            logger.warning("The downloaded English OCR model does not match its expected hash; not using it.")
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(body)
        partial.replace(path)
        return path
    except OSError as exc:
        logger.warning("Could not fetch the English OCR model (%s); using the bundled model.", exc)
        return None


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise ManualError("Reading scans and photos needs the OCR package. "
                              "Install it with: pip install -r requirements.txt") from exc
        model = english_model()
        options = {"rec_model_path": str(model)} if model else {}
        _OCR_ENGINE = RapidOCR(max_side_len=OCR_MAX_SIDE, **options)
    return _OCR_ENGINE


DEGREE_RE = re.compile(r"(?<=\d) ?[°º] ?(?=[CF]\b)|(?<=\d)[oO](?=[CF]\b)")


def _tidy_ocr(text: str) -> str:
    """NFKC turns full-width punctuation into ASCII ("（1）" -> "(1)"); the English model has no "°",
    so "37oF" becomes "37°F" again ("2 OF 5" is left alone)."""
    return DEGREE_RE.sub("°", unicodedata.normalize("NFKC", text))


def _measure(image, engine) -> tuple[float, bool] | None:
    """A quick detection pass on a small copy: the typical text-line height in `image` pixels, and
    whether the page lies sideways (most text boxes taller than wide). None if no text is found."""
    from statistics import median

    probe = image.copy()
    probe.thumbnail((1600, 1600))
    boxes, _ = engine(probe, use_cls=False, use_rec=False)
    if not boxes:
        return None
    sizes = [(max(p[0] for p in b) - min(p[0] for p in b), max(p[1] for p in b) - min(p[1] for p in b)) for b in boxes]
    sideways = sum(h > 1.5 * w for w, h in sizes) > len(sizes) / 2
    line = median(w if sideways else h for w, h in sizes) * image.width / probe.width
    return line, sideways


def _recognise(image, engine) -> tuple[float, list]:
    result, _ = engine(image, use_cls=False)   # the per-line 180° classifier flips long English lines
    result = result or []
    return sum(float(score) * len(text) for _, text, score in result), result


def ocr_image(image) -> str:
    """Text of one page image, laid out like PDF page text: one line per text row, cells of a table row
    joined with " | ", a blank line between blocks, and the two columns of a two-column page read in turn.

    The page is first resized so a typical text line is about OCR_LINE_PX tall: the recogniser drops
    spaces and whole lines when text is small (a 10 pt manual scanned at 150 dpi) and wastes time when
    it is huge, and page size says little about text size. Sideways and upside-down pages are turned
    (EXIF rotation first, as phone photos store it separately; then the better-scoring of two turns)."""
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    with _OCR_LOCK:
        engine = _ocr_engine()
        measured = _measure(image, engine)
        if measured is None:
            return ""
        line, sideways = measured
        factor = min(3.0, max(0.4, OCR_LINE_PX / max(line, 1)), OCR_MAX_SIDE / max(image.size))
        if abs(factor - 1) >= 0.05:
            image = image.resize((round(image.width * factor), round(image.height * factor)), Image.LANCZOS)
        turns = (90, 270) if sideways else (0,)
        best = max(((*_recognise(image.rotate(t, expand=True), engine), t) for t in turns), key=lambda r: r[0])
        score, result, turn = best
        if sum(float(s) for _, _, s in result) < 0.8 * max(len(result), 1):   # weak: maybe upside down
            flipped = _recognise(image.rotate(turn + 180, expand=True), engine)
            if flipped[0] > score:
                score, result, turn = (*flipped, turn + 180)
        width = image.rotate(turn, expand=True).width if turn % 180 else image.width
    boxes = []
    for box, text, score in result:
        if float(score) >= 0.5 and text.strip():
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            boxes.append((min(xs), max(xs), min(ys), max(ys), _tidy_ocr(text.strip())))
    if not boxes:
        return ""
    heights = sorted(b[3] - b[2] for b in boxes)
    line_height = max(heights[len(heights) // 2], 1)
    return "\n\n".join(_rows_text(column, line_height) for column in _columns(boxes, width, line_height))


def _columns(boxes: list[tuple], width: int, line_height: float) -> list[list[tuple]]:
    """Split at a vertical gutter near the middle that no text beside the columns crosses (a title above
    them or a full-width footer below may), unless the two sides are the columns of a table: a table's
    narrow code column, or the wrapped cells of a "Problem | What to do" table, must stay with their row.
    Tuned on 29 OCR'd test pages (two-column prose with and without titles, footers and equal paragraphs;
    tables with short, wrapped and two-paragraph cells)."""
    step = max(1, width // 100)
    for x in range(int(width * 0.3), int(width * 0.7), step):
        crossing = [b for b in boxes if b[0] < x < b[1]]
        left = [b for b in boxes if b[1] <= x]
        right = [b for b in boxes if b[0] >= x]
        if min(len(left), len(right)) < 0.25 * len(boxes):
            continue
        top, bottom = min(b[2] for b in left + right), max(b[3] for b in left + right)
        if any(b[3] > top and b[2] < bottom for b in crossing):
            continue
        if sorted(b[1] - b[0] for b in left)[len(left) // 2] < 0.12 * width:   # a code column ("F07")
            continue
        if _is_table(left, right, line_height):
            continue
        above = [b for b in crossing if b[3] <= top]
        below = [b for b in crossing if b[2] >= bottom]
        return [c for c in (above, left, right, below) if c]
    return [boxes]


def _blocks(boxes: list[tuple], line_height: float) -> list[list[tuple]]:
    """Boxes grouped into blocks of lines: a gap of more than 0.9 line height starts a new block."""
    found: list[list[tuple]] = []
    bottom = None
    for box in sorted(boxes, key=lambda b: (b[2], b[0])):
        if bottom is None or box[2] - bottom > 0.9 * line_height:
            found.append([])
            bottom = None
        found[-1].append(box)
        bottom = box[3] if bottom is None else max(bottom, box[3])
    return found


def _is_table(left: list[tuple], right: list[tuple], line_height: float) -> bool:
    """True when the two sides are the columns of a table, not two columns of prose.

    The deciding sign is how the left-hand blocks end. Paragraphs of prose end a sentence ("."), apart
    from the odd heading; the left cells of a table ("Water leaks from the door") mostly do not. A left
    side whose blocks never end a sentence is a table. Otherwise it is a table only when its blocks also
    start level with the right-hand ones, row after row (in the band both sides share: a page title above
    one column is not a row; the first blocks are left out, as both columns start at the top)."""
    left_blocks = _blocks(left, line_height)
    ends = sum(max(block, key=lambda b: (b[2], b[0]))[4].rstrip()[-1:] in ".!?:)。" for block in left_blocks)
    share = ends / len(left_blocks)
    if share == 0 and len(left_blocks) >= 2:
        return True
    if share >= 0.65:
        return False
    left_starts = [block[0][2] for block in left_blocks]
    right_starts = [block[0][2] for block in _blocks(right, line_height)]
    band_top = max(left_starts[0], right_starts[0]) - 0.5 * line_height
    later = [s for s in left_starts if s >= band_top][1:]
    level = sum(any(abs(s - r) <= 0.5 * line_height for r in right_starts) for s in later)
    return len(later) >= 2 and level * 3 >= 2 * len(later)


def _rows_text(boxes: list[tuple], line_height: float) -> str:
    rows: list[list[tuple]] = []
    for box in sorted(boxes, key=lambda b: (b[2] + b[3]) / 2):
        centre = (box[2] + box[3]) / 2
        if rows and abs(centre - sum((b[2] + b[3]) / 2 for b in rows[-1]) / len(rows[-1])) < 0.5 * line_height:
            rows[-1].append(box)
        else:
            rows.append([box])
    lines, previous_bottom = [], None
    for row in rows:
        row.sort()
        parts = [row[0][4]]
        for before, after in zip(row, row[1:]):
            parts.append((" | " if after[0] - before[1] > 1.2 * line_height else " ") + after[4])
        top = min(b[2] for b in row)
        if previous_bottom is not None and top - previous_bottom > 0.9 * line_height:
            lines.append("")
        lines.append("".join(parts))
        previous_bottom = max(b[3] for b in row)
    return "\n".join(lines)


@contextmanager
def _ocr_turn(progress: Progress) -> Iterator[None]:
    """Hold the OCR lock for a whole manual. While another manual's OCR has it, progress says
    "waiting", so the page can tell the user this one is queued rather than stuck."""
    if not _OCR_LOCK.acquire(blocking=False):
        if progress:
            progress("waiting", 0, 0)
        _OCR_LOCK.acquire()
    try:
        yield
    finally:
        _OCR_LOCK.release()


def ocr_pdf_pages(data: bytes, numbers: list[int], progress: Progress = None) -> dict[int, str]:
    """OCR the given 1-based pages of a PDF (its scanned pages)."""
    import pypdfium2 as pdfium

    texts = {}
    with _ocr_turn(progress):
        document = pdfium.PdfDocument(data)
        try:
            for done, number in enumerate(numbers):
                if progress:
                    progress("ocr", done, len(numbers))
                page = document[number - 1]
                width, height = page.get_size()
                image = page.render(scale=min(4.0, 2800 / max(width, height, 1))).to_pil()   # ~240 dpi for A4
                page.close()
                texts[number] = ocr_image(image)
        finally:
            document.close()
    return texts


def _read_image(data: bytes, filename: str, ocr_max_pages: int, progress: Progress = None) -> Converted:
    from PIL import Image

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()     # iPhone photos (.heic)
    except ImportError:                        # not installed, or blocked by the computer's app policy
        if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS - {b"avif", b"avis"}:
            raise ManualError("iPhone photos (HEIC) can't be read on this computer. Share the photo as JPEG "
                              "(or set the camera to 'Most Compatible') and upload that.", status=415) from None
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ManualError("This image is too large to read.", status=413) from exc
        except Exception as exc:
            raise ManualError("This image could not be opened. It may be damaged, or its format "
                              "is not supported.") from exc
    # A TIFF's frames are the pages of a scan. A phone JPEG's extra MPO pictures (a preview, a depth or
    # HDR gain map) are not pages: only its first picture is read.
    frames = getattr(image, "n_frames", 1) if image.format == "TIFF" else 1
    if frames > ocr_max_pages:
        raise ManualError(f"This scan has {frames} pages; text recognition is limited to {ocr_max_pages} "
                          "pages per manual. Split it into parts and upload each one.")
    pages = []
    with _ocr_turn(progress):
        for index in range(frames):
            if progress:
                progress("ocr", index, frames)
            image.seek(index)
            text = ocr_image(image.copy())
            if text.strip():
                pages.append((index + 1, text))
    if not pages:
        raise ManualError("No text could be found in this image. Try a sharper, well-lit photo of the page.")
    return Converted(kind="image", pages=pages, page_count=frames, ocr_pages=frames)


READERS = {
    "text": _read_text, "markdown": _read_markdown, "csv": _read_csv, "rtf": _read_rtf, "html": _read_html,
    "mhtml": _read_mhtml, "epub": _read_epub, "docx": _read_docx, "pptx": _read_pptx, "xlsx": _read_xlsx,
    "xls": _read_xls, "doc": _read_doc, "ppt": _read_ppt, "odt": _read_odf, "ods": _read_odf, "odp": _read_odf,
    "xps": _read_xps, "iwork": _read_iwork,
}
